#!/usr/bin/env python3
"""collector.py — tw-branch-radar 台股分點雷達 資料抓取器

Phase 1（最小垂直切片）：核心用 ``TaiwanStockTradingDailyReportSecIdAgg``
（分點統計表，逐「分點」以日期範圍查詢）抓數個分點近幾個交易日的買賣彙總，落地
SQLite，計算各分點對個股單日「淨買超金額」後輸出一個 JSON，證明整條管線通。
分點代碼由 ``TaiwanSecuritiesTraderInfo`` 動態取得（不憑記憶編碼）。

註：原設計用整日物件（use_object）需 Sponsor Pro，本專案僅 Sponsor，故改用
SecIdAgg（買/賣均價估算淨買超；使用者已於決定 A 確認採此近似）。實跑證實 SecIdAgg
必須以 securities_trader_id 查詢（不可只給 stock_id）。

鐵律：
- FINMIND_TOKEN 只從環境變數讀，不寫死、不 commit。
- 原始分點明細只留 SQLite（actions/cache），不 commit 進 repo。
- 增量抓取：已涵蓋的 (分點,日) 不重抓（second run 應為零網路請求）。

資料來源：FinMind（https://finmind.github.io/）。個人非商業用途。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

# 注意：FinMind 於 get_loader() 內延遲 import，讓不需連外的邏輯（SQLite 彙總、
# JSON 輸出）在未安裝 finmind 時仍可被測試/執行。型別註解因 __future__ annotations
# 為字串，不會在載入時求值。

# ============ 可調常數（CONFIG） ============
# --- 勝率四參數（Phase 3 才使用；於此定義以符合「四參數做成 collector 常數」） ---
LOOKBACK_DAYS = 120           # 回看交易日數
EVENT_MIN_AMOUNT = 5_000_000  # 單日淨買超金額門檻（新台幣）
HOLD_DAYS = 5                 # 事件後持有交易日數
MIN_EVENTS = 10               # 列入排行的最少事件數

# --- Phase 1 專用（SecIdAgg 分點統計；整日物件需 Sponsor Pro，本專案僅 Sponsor） ---
# 實跑證實：SecIdAgg 必須以 securities_trader_id（分點代碼）查詢，故 Phase 1 取樣數個分點。
PHASE1_BRANCHES = 5           # Phase 1 取樣幾個分點（securities_trader_id）
PHASE1_DAYS = 3               # 最終輸出取最近幾個交易日
WINDOW_CAL_DAYS = 15          # 往回查的日曆天窗（需含 ≥ PHASE1_DAYS 個交易日）
TOP_N = 50                    # 輸出前 N 筆淨買超
REQ_TIMEOUT = 120             # 單次查詢逾時（秒）

DATASET = "TaiwanStockTradingDailyReportSecIdAgg"
# 依 FinMind client v1.9.12 docstring 驗證之欄位（逐字）
EXPECTED_COLS = {
    "date", "stock_id", "securities_trader_id", "securities_trader",
    "buy_volume", "sell_volume", "buy_price", "sell_price",
}

DB_PATH = os.environ.get("BRANCH_DB", os.path.join(".cache", "branch.db"))
DATA_DIR = os.environ.get("DATA_DIR", "data")
OUTPUT_JSON = os.path.join(DATA_DIR, "phase1_sample.json")


# ============ FinMind ============
def get_loader() -> "DataLoader":
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        sys.exit("ERROR: 環境變數 FINMIND_TOKEN 未設定（token 只走環境變數/secrets）")
    from FinMind.data import DataLoader  # 延遲 import：僅實際抓取時才需要
    return DataLoader(token=token)


def report_api_limit(dl: DataLoader) -> dict:
    """印出並回傳實際每小時請求上限（用以確認 Sponsor 層級數字）。"""
    used = limit = None
    try:
        used = dl.api_usage             # property（非方法）→ 本小時已用次數
    except Exception as exc:            # noqa: BLE001 - 僅為容錯記錄
        print(f"[api] 取得已用次數失敗：{exc}")
    try:
        limit = dl.api_usage_limit      # property → 每小時上限
    except Exception as exc:            # noqa: BLE001
        print(f"[api] 取得上限失敗：{exc}")
    print(f"[api] 每小時請求上限={limit}，本小時已用={used}")
    return {"used": used, "limit": limit}


# ============ SQLite ============
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- 分點統計（SecIdAgg）：每列即一 (交易日, 分點, 個股) 的當日買賣彙總
        CREATE TABLE IF NOT EXISTS branch_daily_agg(
            date                 TEXT,
            stock_id             TEXT,
            securities_trader_id TEXT,
            securities_trader    TEXT,
            buy_volume           INTEGER,
            sell_volume          INTEGER,
            buy_price            REAL,
            sell_price           REAL,
            PRIMARY KEY (date, stock_id, securities_trader_id)
        );
        CREATE INDEX IF NOT EXISTS idx_agg_date ON branch_daily_agg(date);
        -- 增量涵蓋標記：某分點某「平日」已查過（含非交易日，避免下次重查）
        CREATE TABLE IF NOT EXISTS fetched_keys(
            securities_trader_id TEXT,
            date                 TEXT,
            fetched_at           TEXT,
            PRIMARY KEY (securities_trader_id, date)
        );
        """
    )
    conn.commit()


def weekday_window(anchor: date, cal_days: int) -> list[str]:
    """anchor 往回 cal_days 內的所有平日（含 anchor），升冪 ISO 字串。"""
    days = [
        (anchor - timedelta(days=i)).isoformat()
        for i in range(cal_days)
        if (anchor - timedelta(days=i)).weekday() < 5  # 排除週末
    ]
    return sorted(days)


def store_branch_window(conn: sqlite3.Connection, trader_id: str,
                        window_days: list[str], df) -> int:
    """寫入某分點 window 內的 SecIdAgg 列，並標記整個 window 已涵蓋（含非交易日）。

    先刪該分點 window 內舊列再插入（重跑同窗安全、不重複）；window 內每個平日都
    寫入 fetched_keys，使非交易日下次不再重查（達成增量零重抓）。
    """
    cols = ["date", "stock_id", "securities_trader_id", "securities_trader",
            "buy_volume", "sell_volume", "buy_price", "sell_price"]
    records = list(df[cols].itertuples(index=False, name=None)) if len(df) else []
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.executemany(
            "DELETE FROM branch_daily_agg WHERE securities_trader_id = ? AND date = ?",
            [(trader_id, d) for d in window_days],
        )
        conn.executemany(
            "INSERT INTO branch_daily_agg"
            "(date, stock_id, securities_trader_id, securities_trader,"
            " buy_volume, sell_volume, buy_price, sell_price)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO fetched_keys(securities_trader_id, date, fetched_at)"
            " VALUES (?, ?, ?)",
            [(trader_id, d, now) for d in window_days],
        )
    return len(records)


def validate_cols(df, key: str) -> None:
    missing = EXPECTED_COLS - set(df.columns)
    if missing:
        raise SystemExit(
            f"SecIdAgg 欄位缺少 {sorted(missing)}（{key}）；實際欄位={sorted(df.columns)}。"
            " 欄位與 FinMind client 文件不符，請先核對再續作。"
        )


def get_branch_codes(dl: DataLoader, n: int, override: list[str]) -> list[str]:
    """取得要抓的分點代碼清單：override 非空則用之；否則由 TaiwanSecuritiesTraderInfo
    動態取前 n 個（不憑記憶編碼，符合鐵律 2）。"""
    if override:
        return override
    info = dl.taiwan_securities_trader_info()
    if info is None or not len(info):
        raise SystemExit("取得券商清單失敗（TaiwanSecuritiesTraderInfo 回空）。")
    codes: list[str] = []
    seen: set[str] = set()
    for cid in info["securities_trader_id"].tolist():
        cid = str(cid)
        if cid and cid not in seen:
            seen.add(cid)
            codes.append(cid)
        if len(codes) >= n:
            break
    print(f"[branches] 由 TaiwanSecuritiesTraderInfo 取樣 {len(codes)} 個分點：{codes}")
    return codes


# ============ 抓取（增量，SecIdAgg 逐分點日期範圍） ============
def ensure_coverage(dl: DataLoader, conn: sqlite3.Connection,
                    branches: list[str], anchor: date, n_days: int) -> list[str]:
    """對每個分點補齊 window 內缺的平日：用 SecIdAgg 一次查該分點整段日期範圍。

    已完整涵蓋（fetched_keys 已含 window 全部平日）的分點直接跳過，達成增量零重抓。
    SecIdAgg 走一般 get_data 路徑：層級不足或參數錯誤時 FinMind 回錯誤、client 端
    _extract_data 會 raise，我們在此轉成可讀訊息並中止（不會被誤當非交易日）。
    回傳 window 內實際有資料的平日（供輸出用）。
    """
    window = weekday_window(anchor, WINDOW_CAL_DAYS)
    start, end = window[0], window[-1]
    window_set = set(window)
    logged_cols = False
    for trader_id in branches:
        have = {r[0] for r in conn.execute(
            "SELECT date FROM fetched_keys WHERE securities_trader_id = ?", (trader_id,))}
        if window_set <= have:
            print(f"[skip] 分點 {trader_id} 窗 {start}~{end} 已涵蓋，跳過（增量）")
            continue
        try:
            df = dl.taiwan_stock_trading_daily_report_secid_agg(
                securities_trader_id=trader_id, start_date=start, end_date=end,
                timeout=REQ_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 轉可讀訊息
            raise SystemExit(
                f"SecIdAgg 查詢失敗（分點 {trader_id}）：{exc}\n"
                "若訊息提及 user level 代表需 Sponsor Pro；其他參數錯誤請回報以便修正。")
        if len(df):
            if not logged_cols:
                print(f"[cols] 分點 {trader_id} SecIdAgg 欄位={sorted(df.columns)}，列數={len(df)}")
                logged_cols = True
            validate_cols(df, trader_id)
            df = df[df["date"].astype(str).isin(window_set)]  # 保險：僅留窗內日期
        stored = store_branch_window(conn, trader_id, window, df)
        print(f"[fetch] 分點 {trader_id} 取得 {stored} 列（{start}~{end}）")
    return recent_trading_days(conn, window, n_days)


def recent_trading_days(conn: sqlite3.Connection, window: list[str], n: int) -> list[str]:
    """window 內實際有分點資料的日期，取最近 n 個（升冪）。"""
    ph = ",".join("?" * len(window))
    rows = conn.execute(
        f"SELECT DISTINCT date FROM branch_daily_agg WHERE date IN ({ph})"
        " ORDER BY date DESC LIMIT ?",
        (*window, n),
    ).fetchall()
    return sorted(r[0] for r in rows)


# ============ 彙總輸出 ============
def export_summary(conn: sqlite3.Connection, days: list[str], api_info: dict) -> None:
    placeholders = ",".join("?" * len(days))
    query = f"""
        SELECT date, securities_trader_id, securities_trader, stock_id,
               buy_volume, sell_volume, buy_price, sell_price,
               (buy_volume * buy_price)                              AS buy_amount,
               (sell_volume * sell_price)                            AS sell_amount,
               (buy_volume * buy_price) - (sell_volume * sell_price) AS net_amount
        FROM branch_daily_agg
        WHERE date IN ({placeholders})
        ORDER BY net_amount DESC
        LIMIT ?
    """
    rows = conn.execute(query, (*days, TOP_N)).fetchall()
    top = [
        {
            "date": r[0],
            "securities_trader_id": r[1],
            "securities_trader": r[2],
            "stock_id": r[3],
            "buy_volume": int(r[4] or 0),
            "sell_volume": int(r[5] or 0),
            "buy_price": round(r[6] or 0, 4),
            "sell_price": round(r[7] or 0, 4),
            "buy_amount": round(r[8] or 0, 2),
            "sell_amount": round(r[9] or 0, 2),
            "net_amount": round(r[10] or 0, 2),
        }
        for r in rows
    ]
    rows_per_day = {
        d: conn.execute(
            "SELECT COUNT(*) FROM branch_daily_agg WHERE date = ?", (d,)
        ).fetchone()[0]
        for d in days
    }
    payload = {
        "phase": 1,
        "dataset": DATASET,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_covered": days,
        "rows_per_day": rows_per_day,
        "net_buy_formula": "buy_volume×buy_price − sell_volume×sell_price（均價估算，新台幣）",
        "estimation_note": "SecIdAgg 買/賣均價估算（使用者決定 A）；非逐筆精算。",
        # Phase 1 檢查點：審視樣本時對照 net_amount 量級是否合理（相對 EVENT_MIN_AMOUNT
        # 門檻），確認 volume 單位是「股」非「張」。
        "phase3_event_threshold": EVENT_MIN_AMOUNT,
        "api_hourly_limit": api_info.get("limit"),
        "api_used": api_info.get("used"),
        "top_n": TOP_N,
        "top_net_buy": top,
        "source": "FinMind",
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[output] 寫出 {OUTPUT_JSON}；top {len(top)} 筆，涵蓋 {days}")


# ============ main ============
def main() -> None:
    parser = argparse.ArgumentParser(description="tw-branch-radar collector (Phase 1)")
    parser.add_argument("--days", type=int, default=PHASE1_DAYS, help="輸出取最近幾個交易日")
    parser.add_argument("--anchor", default="", help="錨定日 YYYY-MM-DD（預設今天）")
    parser.add_argument("--branches", default="",
                        help="逗號分隔分點代碼 securities_trader_id（預設由券商清單取樣）")
    args = parser.parse_args()

    anchor = date.fromisoformat(args.anchor) if args.anchor else date.today()
    override = [s.strip() for s in args.branches.split(",") if s.strip()]
    print(f"=== tw-branch-radar collector Phase 1（SecIdAgg 逐分點）==="
          f"（錨定日={anchor}，取最近 {args.days} 交易日）")

    dl = get_loader()
    api_info = report_api_limit(dl)
    branches = get_branch_codes(dl, PHASE1_BRANCHES, override)

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        days = ensure_coverage(dl, conn, branches, anchor, args.days)
        if len(days) < args.days:
            raise SystemExit(
                f"僅取得 {len(days)}/{args.days} 個交易日"
                f"（窗={WINDOW_CAL_DAYS} 日曆天，分點={branches}）。"
                " 可能錨定日太近假期、window 太短、或取樣分點近期無交易；"
                " 請調整 --anchor / WINDOW_CAL_DAYS / --branches。")
        export_summary(conn, days, api_info)
    finally:
        conn.close()
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
