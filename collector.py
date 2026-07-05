#!/usr/bin/env python3
"""collector.py — tw-branch-radar 台股分點雷達 資料抓取器

用 ``TaiwanStockTradingDailyReport``（非彙總分點日報表，以 (securities_trader_id, date)
查詢）抓分點對個股的逐筆買賣，落地 SQLite，逐筆精算「淨買超金額」後輸出彙總 JSON。
Phase 2：以 ``TaiwanStockTradingDate`` 交易日曆取最近 120 交易日、對 branches×交易日
增量回補（每次執行至多 MAX_REQ_PER_RUN 對，達上限下次續跑）。

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

# --- 抓取設定（非彙總分點日報表 TaiwanStockTradingDailyReport；逐筆精算） ---
# 實跑證實：整日物件 use_object 需 Sponsor Pro；SecIdAgg 需股+分點兩者。故用非彙總報表以
# (securities_trader_id, date) 查詢（回該分點當日各股逐筆買賣，Sponsor 可用），逐筆精算。
# "1020" 為 FinMind 官方測試 (tests/data/test_data_loader.py) 使用之真實分點代碼。
DEFAULT_BRANCHES = ["1020"]   # 預設分點宇宙（securities_trader_id）；可 --branches 覆寫
BACKFILL_CAL_SPAN = 200       # 取交易日曆往回查的日曆天數（需含 ≥ LOOKBACK_DAYS 個交易日）
MAX_REQ_PER_RUN = int(os.environ.get("MAX_REQ_PER_RUN", "2000"))  # 每次抓取上限（達上限下次續跑）
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
OUTPUT_JSON = os.path.join(DATA_DIR, "status.json")


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


def get_trading_days(dl: DataLoader, anchor: date, n: int) -> list[str]:
    """用 TaiwanStockTradingDate 交易日曆取 anchor 往回最近 n 個交易日（升冪 ISO 字串）。"""
    start = (anchor - timedelta(days=BACKFILL_CAL_SPAN)).isoformat()
    end = anchor.isoformat()
    df = dl.taiwan_stock_trading_date(start_date=start, end_date=end, timeout=REQ_TIMEOUT)
    if df is None or not len(df):
        raise SystemExit("取得交易日曆失敗（TaiwanStockTradingDate 回空）。")
    days = sorted({str(d)[:10] for d in df["date"].tolist() if str(d)[:10] <= end})
    if len(days) < n:
        raise SystemExit(
            f"交易日曆僅回 {len(days)} 個交易日 < 目標 {n}；請調大 BACKFILL_CAL_SPAN。")
    return days[-n:]


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


def backfill(dl: DataLoader, conn: sqlite3.Connection,
             branches: list[str], trading_days: list[str]) -> dict:
    """對 branches × trading_days 補齊缺的 (分點,日)；每次執行至多抓 MAX_REQ_PER_RUN 對，
    達上限即停（下次續跑）。已涵蓋者跳過（增量零重抓）。回傳進度/涵蓋統計。
    """
    fetched = 0
    logged = False
    stopped_early = False
    for trader_id in branches:
        have = {r[0] for r in conn.execute(
            "SELECT date FROM fetched_keys WHERE securities_trader_id = ?", (trader_id,))}
        missing = [d for d in trading_days if d not in have]
        if not missing:
            print(f"[skip] 分點 {trader_id} 已涵蓋全部 {len(trading_days)} 交易日（增量）")
            continue
        for day in missing:
            if fetched >= MAX_REQ_PER_RUN:
                stopped_early = True
                break
            df = fetch_branch_day(dl, trader_id, day)
            if len(df):
                if not logged:
                    print(f"[cols] {trader_id} {day} 欄位={sorted(df.columns)}，列數={len(df)}")
                    logged = True
                validate_cols(df, f"{trader_id} {day}")
            stored = store_branch_day(conn, trader_id, day, df)
            fetched += 1
            print(f"[fetch] 分點 {trader_id} {day} 存入 {stored} 列")
        if stopped_early:
            print(f"[budget] 達本次上限 MAX_REQ_PER_RUN={MAX_REQ_PER_RUN}，本次停止（下次續跑）")
            break

    coverage, remaining = {}, 0
    td_set = set(trading_days)
    for trader_id in branches:
        have = {r[0] for r in conn.execute(
            "SELECT date FROM fetched_keys WHERE securities_trader_id = ?", (trader_id,))}
        cov = len(td_set & have)
        coverage[trader_id] = cov
        remaining += len(trading_days) - cov
    print(f"[backfill] 本次抓 {fetched} 對；剩餘未涵蓋 {remaining} 對"
          f"{'（達上限，下次續跑）' if stopped_early else '（已全部涵蓋）'}")
    return {"fetched_this_run": fetched, "remaining": remaining,
            "stopped_early": stopped_early, "target_days": len(trading_days),
            "coverage": coverage}


# ============ 彙總輸出 ============
def export_status(conn: sqlite3.Connection, trading_days: list[str],
                  api_info: dict, progress: dict) -> None:
    placeholders = ",".join("?" * len(trading_days))
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
    rows = conn.execute(query, (*trading_days, TOP_N)).fetchall()
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
    total_rows = conn.execute(
        f"SELECT COUNT(*) FROM branch_daily WHERE date IN ({placeholders})",
        tuple(trading_days),
    ).fetchone()[0]
    payload = {
        "phase": 2,
        "dataset": DATASET,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trading_days_target": len(trading_days),
        "trading_days_range": [trading_days[0], trading_days[-1]] if trading_days else [],
        "branch_coverage": progress.get("coverage", {}),
        "backfill_remaining_pairs": progress.get("remaining"),
        "backfill_stopped_early": progress.get("stopped_early"),
        "fetched_this_run": progress.get("fetched_this_run"),
        "total_rows": total_rows,
        "net_buy_formula": "Σ(buy×price) − Σ(sell×price)（逐筆精算，新台幣）",
        "query_note": "非彙總報表以 (securities_trader_id, date) 查詢，逐筆精算。",
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
    print(f"[output] 寫出 {OUTPUT_JSON}；目標 {len(trading_days)} 交易日，"
          f"總列數 {total_rows}，top {len(top)} 筆")
    print(f"[coverage] 各分點涵蓋交易日數：{progress.get('coverage')}")
    for r in top[:5]:  # 印樣本供 log 檢視量級（確認 buy/sell 為股數、金額合理）
        print(f"[sample] {r['date']} 分點{r['securities_trader_id']} 股{r['stock_id']} "
              f"淨買超={r['net_amount']:,.0f} (買{r['buy_shares']:,}股/賣{r['sell_shares']:,}股)")


# ============ main ============
def main() -> None:
    parser = argparse.ArgumentParser(description="tw-branch-radar collector")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="回補最近幾個交易日")
    parser.add_argument("--anchor", default="", help="錨定日 YYYY-MM-DD（預設今天）")
    parser.add_argument("--branches", default="",
                        help="逗號分隔分點代碼 securities_trader_id（預設 DEFAULT_BRANCHES）")
    args = parser.parse_args()

    anchor = date.fromisoformat(args.anchor) if args.anchor else date.today()
    override = [s.strip() for s in args.branches.split(",") if s.strip()]
    branches = override or list(DEFAULT_BRANCHES)
    print(f"=== tw-branch-radar collector（逐筆精算）==="
          f"（錨定日={anchor}，分點={branches}，目標 {args.days} 交易日，本次上限 {MAX_REQ_PER_RUN} 對）")

    dl = get_loader()
    api_info = report_api_limit(dl)
    trading_days = get_trading_days(dl, anchor, args.days)
    print(f"[calendar] 取得 {len(trading_days)} 交易日：{trading_days[0]}~{trading_days[-1]}")

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        progress = backfill(dl, conn, branches, trading_days)
        export_status(conn, trading_days, api_info, progress)
    finally:
        conn.close()
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
