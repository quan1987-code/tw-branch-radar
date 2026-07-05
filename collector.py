#!/usr/bin/env python3
"""collector.py — tw-branch-radar 台股分點雷達 資料抓取器

Phase 1（最小垂直切片）：用單一 dataset ``TaiwanStockTradingDailyReport``
（非彙總分點日報表，以 (securities_trader_id, date) 查詢）抓數個分點近幾個交易日的
逐筆買賣，落地 SQLite，逐筆精算各分點對個股單日「淨買超金額」後輸出一個 JSON。

註：整日物件（use_object）需 Sponsor Pro、SecIdAgg 需股+分點兩者，皆不合用；
非彙總報表以分點+日期查詢在 Sponsor 可用，且可逐筆精算 Σ(buy×price)−Σ(sell×price)
（即使用者原本偏好的較準口徑）。分點代碼預設用官方測試之真實代碼，可 --branches 覆寫。

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

# --- Phase 1 專用（非彙總分點日報表 TaiwanStockTradingDailyReport；逐筆精算） ---
# 實跑證實：整日物件 use_object 需 Sponsor Pro；SecIdAgg 需股+分點兩者。故改用非彙總報表以
# (securities_trader_id, date) 查詢（回該分點當日各股逐筆買賣，Sponsor 可用），逐筆精算。
# "1020" 為 FinMind 官方測試 (tests/data/test_data_loader.py) 使用之真實分點代碼。
PHASE1_BRANCHES = ["1020"]    # Phase 1 取樣分點（securities_trader_id）；可 --branches 覆寫
PHASE1_DAYS = 3               # 抓最近幾個交易日
WINDOW_CAL_DAYS = 15          # 往回找交易日的日曆天窗（需含 ≥ PHASE1_DAYS 個交易日）
TOP_N = 50                    # 輸出前 N 筆淨買超
REQ_TIMEOUT = 120             # 單次查詢逾時（秒）

DATASET = "TaiwanStockTradingDailyReport"
# 依 FinMind client v1.9.12 docstring 驗證之欄位（逐字）
EXPECTED_COLS = {
    "date", "stock_id", "securities_trader_id", "securities_trader",
    "price", "buy", "sell",
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
        -- 非彙總分點日報表：每列＝(交易日, 分點, 個股, 成交價) 的買/賣股數（逐筆）
        CREATE TABLE IF NOT EXISTS branch_daily(
            date                 TEXT,
            stock_id             TEXT,
            securities_trader_id TEXT,
            securities_trader    TEXT,
            price                REAL,
            buy                  INTEGER,
            sell                 INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_branch_daily_date ON branch_daily(date);
        CREATE INDEX IF NOT EXISTS idx_branch_daily_bd ON branch_daily(securities_trader_id, date);
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


def store_branch_day(conn: sqlite3.Connection, trader_id: str, day: str, df) -> int:
    """寫入某分點某日的逐筆列，並標記該 (分點,日) 已涵蓋（含非交易日=0 列）。

    先刪該 (分點,日) 舊列再插入（重跑同日安全、不重複）；並寫 fetched_keys，
    使非交易日下次不再重查（達成增量零重抓）。
    """
    cols = ["date", "stock_id", "securities_trader_id", "securities_trader",
            "price", "buy", "sell"]
    df = df.assign(date=day) if len(df) else df  # 統一日期字串
    records = list(df[cols].itertuples(index=False, name=None)) if len(df) else []
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            "DELETE FROM branch_daily WHERE securities_trader_id = ? AND date = ?",
            (trader_id, day),
        )
        conn.executemany(
            "INSERT INTO branch_daily"
            "(date, stock_id, securities_trader_id, securities_trader, price, buy, sell)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        conn.execute(
            "INSERT OR REPLACE INTO fetched_keys(securities_trader_id, date, fetched_at)"
            " VALUES (?, ?, ?)",
            (trader_id, day, now),
        )
    return len(records)


def validate_cols(df, key: str) -> None:
    missing = EXPECTED_COLS - set(df.columns)
    if missing:
        raise SystemExit(
            f"分點日報表欄位缺少 {sorted(missing)}（{key}）；實際欄位={sorted(df.columns)}。"
            " 欄位與 FinMind client 文件不符，請先核對再續作。"
        )


# ============ 抓取（增量，非彙總報表逐 (分點,日) 查詢） ============
def fetch_branch_day(dl: DataLoader, trader_id: str, day: str):
    """查某分點某日的非彙總分點日報表；回 DataFrame（可能空）。錯誤轉可讀訊息並中止。"""
    try:
        return dl.taiwan_stock_trading_daily_report(
            securities_trader_id=trader_id, date=day, timeout=REQ_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 轉可讀訊息
        raise SystemExit(
            f"分點日報表查詢失敗（分點 {trader_id} {day}）：{exc}\n"
            "若訊息提及 user level 代表需更高層級；其他參數錯誤請回報以便修正。")


def collect(dl: DataLoader, conn: sqlite3.Connection,
            branches: list[str], anchor: date, n_days: int) -> list[str]:
    """以 branches[0] 逐日往回偵測交易日並抓取，湊滿 n_days 個交易日；再對其餘分點補抓
    這些交易日。已涵蓋 (分點,日) 直接跳過（增量零重抓）。回傳交易日（升冪）。
    """
    window_desc = sorted(weekday_window(anchor, WINDOW_CAL_DAYS), reverse=True)  # 新到舊
    probe = branches[0]
    probe_checked = {r[0] for r in conn.execute(
        "SELECT date FROM fetched_keys WHERE securities_trader_id = ?", (probe,))}
    probe_has_rows = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM branch_daily WHERE securities_trader_id = ?", (probe,))}
    logged = False
    trading_days: list[str] = []
    for day in window_desc:
        if len(trading_days) >= n_days:
            break
        if day in probe_checked:  # 已查過（增量）
            if day in probe_has_rows:
                print(f"[skip] 分點 {probe} {day} 已在 DB（交易日），計入")
                trading_days.append(day)
            continue
        df = fetch_branch_day(dl, probe, day)
        if len(df):
            if not logged:
                print(f"[cols] {probe} {day} 欄位={sorted(df.columns)}，列數={len(df)}")
                logged = True
            validate_cols(df, f"{probe} {day}")
            stored = store_branch_day(conn, probe, day, df)
            print(f"[fetch] 分點 {probe} {day} 交易日，存入 {stored} 列")
            trading_days.append(day)
        else:
            store_branch_day(conn, probe, day, df)  # 標記非交易日已查（0 列）
            print(f"[nontrading] 分點 {probe} {day} 無資料，略過")
    trading_days = sorted(trading_days)
    for trader_id in branches[1:]:  # 其餘分點補抓已確認的交易日
        have = {r[0] for r in conn.execute(
            "SELECT date FROM fetched_keys WHERE securities_trader_id = ?", (trader_id,))}
        for day in trading_days:
            if day in have:
                print(f"[skip] 分點 {trader_id} {day} 已涵蓋（增量）")
                continue
            df = fetch_branch_day(dl, trader_id, day)
            if len(df):
                validate_cols(df, f"{trader_id} {day}")
            stored = store_branch_day(conn, trader_id, day, df)
            print(f"[fetch] 分點 {trader_id} {day} 存入 {stored} 列")
    return trading_days


# ============ 彙總輸出 ============
def export_summary(conn: sqlite3.Connection, days: list[str], api_info: dict) -> None:
    placeholders = ",".join("?" * len(days))
    query = f"""
        SELECT date, securities_trader_id, securities_trader, stock_id,
               SUM(buy  * price)                   AS buy_amount,
               SUM(sell * price)                   AS sell_amount,
               SUM(buy  * price) - SUM(sell*price) AS net_amount,
               SUM(buy)                            AS buy_shares,
               SUM(sell)                           AS sell_shares
        FROM branch_daily
        WHERE date IN ({placeholders})
        GROUP BY date, securities_trader_id, securities_trader, stock_id
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
            "buy_amount": round(r[4] or 0, 2),
            "sell_amount": round(r[5] or 0, 2),
            "net_amount": round(r[6] or 0, 2),
            "buy_shares": int(r[7] or 0),
            "sell_shares": int(r[8] or 0),
        }
        for r in rows
    ]
    rows_per_day = {
        d: conn.execute(
            "SELECT COUNT(*) FROM branch_daily WHERE date = ?", (d,)
        ).fetchone()[0]
        for d in days
    }
    payload = {
        "phase": 1,
        "dataset": DATASET,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_covered": days,
        "rows_per_day": rows_per_day,
        "net_buy_formula": "Σ(buy×price) − Σ(sell×price)（逐筆精算，新台幣）",
        "query_note": "非彙總報表以 (securities_trader_id, date) 查詢，逐筆精算。",
        # Phase 1 檢查點：對照 net_amount 量級是否合理（相對 EVENT_MIN_AMOUNT 門檻），
        # 並確認 buy/sell 單位是「股」非「張」。
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
                        help="逗號分隔分點代碼 securities_trader_id（預設 PHASE1_BRANCHES）")
    args = parser.parse_args()

    anchor = date.fromisoformat(args.anchor) if args.anchor else date.today()
    override = [s.strip() for s in args.branches.split(",") if s.strip()]
    branches = override or list(PHASE1_BRANCHES)
    print(f"=== tw-branch-radar collector Phase 1（逐筆精算）==="
          f"（錨定日={anchor}，分點={branches}，取最近 {args.days} 交易日）")

    dl = get_loader()
    api_info = report_api_limit(dl)

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        days = collect(dl, conn, branches, anchor, args.days)
        if len(days) < args.days:
            raise SystemExit(
                f"僅取得 {len(days)}/{args.days} 個交易日"
                f"（窗={WINDOW_CAL_DAYS} 日曆天，分點={branches}）。"
                " 可能錨定日太近假期、window 太短、或分點近期無交易；"
                " 請調整 --anchor / WINDOW_CAL_DAYS / --branches。")
        export_summary(conn, days, api_info)
    finally:
        conn.close()
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
