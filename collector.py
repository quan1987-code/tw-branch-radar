#!/usr/bin/env python3
"""collector.py — tw-branch-radar 台股分點雷達 資料抓取器

Phase 1（最小垂直切片）：只用單一 dataset ``TaiwanStockTradingDailyReport``
（整日物件 ``use_object=True``）抓最近 N 個交易日的券商分點買賣明細，落地
SQLite，彙總各分點對個股的單日「淨買超金額」後輸出一個 JSON，證明整條管線通。

鐵律：
- FINMIND_TOKEN 只從環境變數讀，不寫死、不 commit。
- 原始逐筆分點明細只留 SQLite（actions/cache），不 commit 進 repo。
- 增量抓取：已抓過的交易日不重抓（second run 應為零網路請求）。

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

# --- Phase 1 專用 ---
PHASE1_DAYS = 3               # 抓幾個交易日
MAX_LOOKBACK_CAL_DAYS = 25    # 從錨定日往回找交易日的日曆天上限（安全界）
TOP_N = 50                    # 輸出前 N 筆淨買超彙總
REQ_TIMEOUT = 300             # 單次整日物件下載逾時（秒）

DATASET = "TaiwanStockTradingDailyReport"
# 依 FinMind client v1.9.12 docstring 驗證之欄位（逐字）
EXPECTED_COLS = {
    "date", "stock_id", "securities_trader_id",
    "securities_trader", "price", "buy", "sell",
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
        CREATE TABLE IF NOT EXISTS fetched_days(
            date       TEXT PRIMARY KEY,
            rows       INTEGER,
            fetched_at TEXT
        );
        """
    )
    conn.commit()


def store_day(conn: sqlite3.Connection, day: str, df) -> int:
    """清掉該日舊資料後重新寫入（重跑同日安全、不重複累加）。"""
    cols = ["date", "stock_id", "securities_trader_id",
            "securities_trader", "price", "buy", "sell"]
    df = df.assign(date=day)  # 所有列皆屬 day，統一日期字串格式
    records = list(df[cols].itertuples(index=False, name=None))
    with conn:
        conn.execute("DELETE FROM branch_daily WHERE date = ?", (day,))
        conn.executemany(
            "INSERT INTO branch_daily"
            "(date, stock_id, securities_trader_id, securities_trader, price, buy, sell)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        conn.execute(
            "INSERT OR REPLACE INTO fetched_days(date, rows, fetched_at) VALUES (?, ?, ?)",
            (day, len(records), datetime.now(timezone.utc).isoformat()),
        )
    return len(records)


def validate_cols(df, day: str) -> None:
    missing = EXPECTED_COLS - set(df.columns)
    if missing:
        raise SystemExit(
            f"物件欄位缺少 {sorted(missing)}（{day}）；實際欄位={sorted(df.columns)}。"
            " 欄位與 FinMind client 文件不符，請先核對再續作。"
        )


# ============ 抓取（增量） ============
def ensure_days(dl: DataLoader, conn: sqlite3.Connection, n: int, anchor: date) -> list[str]:
    """確保 DB 含最近 n 個交易日的分點資料，回傳升冪日期清單。

    作法：從 anchor 往回逐日檢查——週末直接跳過；已在 fetched_days 者視為
    已涵蓋（不再抓取，達成增量零重抓）；否則抓整日物件，有資料即為交易日並
    存檔，空資料視為非交易日略過。
    """
    existing = {r[0] for r in conn.execute("SELECT date FROM fetched_days")}
    covered: list[str] = []
    logged_cols = False
    cur = anchor
    checked = 0
    while len(covered) < n and checked < MAX_LOOKBACK_CAL_DAYS:
        checked += 1
        ds, cur = cur.isoformat(), cur - timedelta(days=1)
        if date.fromisoformat(ds).weekday() >= 5:  # 週六(5)、週日(6)
            continue
        if ds in existing:
            print(f"[skip] {ds} 已在 DB，跳過抓取（增量）")
            covered.append(ds)
            continue
        df = dl.taiwan_stock_trading_daily_report(
            date=ds, use_object=True, timeout=REQ_TIMEOUT,
        )
        if df is None or len(df) == 0:
            print(f"[nontrading] {ds} 無資料（非交易日或當日尚未產生），略過")
            continue
        if not logged_cols:
            print(f"[cols] {ds} 物件欄位={sorted(df.columns)}，列數={len(df)}")
            logged_cols = True
        validate_cols(df, ds)
        stored = store_day(conn, ds, df)
        print(f"[fetch] {ds} 交易日，存入 {stored} 列")
        covered.append(ds)
    if len(covered) < n:
        raise SystemExit(
            f"僅取得 {len(covered)}/{n} 個交易日（往回查 {checked} 天）。"
            " 請調高 MAX_LOOKBACK_CAL_DAYS 或稍後再試。"
        )
    return sorted(covered)


# ============ 彙總輸出 ============
def export_summary(conn: sqlite3.Connection, days: list[str], api_info: dict) -> None:
    placeholders = ",".join("?" * len(days))
    query = f"""
        SELECT date, securities_trader_id, securities_trader, stock_id,
               SUM(buy  * price)                 AS buy_amount,
               SUM(sell * price)                 AS sell_amount,
               SUM(buy  * price) - SUM(sell*price) AS net_amount,
               SUM(buy)                          AS buy_shares,
               SUM(sell)                         AS sell_shares
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
        "net_buy_formula": "Σ(buy×price) − Σ(sell×price)（金額，新台幣）",
        # Phase 1 檢查點：buy/sell 依文件為「股數」；請於審視樣本時對照
        # net_amount 量級是否合理（相對 EVENT_MIN_AMOUNT 門檻），確認非以「張」計。
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
    parser.add_argument("--days", type=int, default=PHASE1_DAYS, help="抓幾個交易日")
    parser.add_argument("--anchor", default="", help="錨定日 YYYY-MM-DD（預設今天）")
    args = parser.parse_args()

    anchor = date.fromisoformat(args.anchor) if args.anchor else date.today()
    print(f"=== tw-branch-radar collector Phase 1 ===（錨定日={anchor}，目標 {args.days} 交易日）")

    dl = get_loader()
    api_info = report_api_limit(dl)

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        days = ensure_days(dl, conn, args.days, anchor)
        export_summary(conn, days, api_info)
    finally:
        conn.close()
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
