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
WILSON_Z = 1.96               # Wilson 下界 z 值（95%）；排序用，避免小樣本灌水

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

# --- Phase 4/5：鉅額交易看板 + 成交資訊 ---
# 追蹤清單預設用真實大型股（2303/2603/3008/2330 皆出現於 run #9 實際資料）；可 --watchlist 覆寫
DEFAULT_WATCHLIST = ["2330", "2317", "2454", "2303", "2603", "3008", "2891"]
BLOCK_DAYS = 5                # 鉅額看板取最近幾個交易日
BLOCK_TOP_N = 50             # 鉅額看板輸出上限
# TWSE 每日市場成交資訊（大盤：發行量加權股價指數收盤＋成交金額）——FinMind 無現成大盤價量
TWSE_FMTQIK_URL = "https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK"

DB_PATH = os.environ.get("BRANCH_DB", os.path.join(".cache", "branch.db"))
DATA_DIR = os.environ.get("DATA_DIR", "data")
OUTPUT_JSON = os.path.join(DATA_DIR, "status.json")
RANKING_JSON = os.path.join(DATA_DIR, "ranking.json")
BLOCK_JSON = os.path.join(DATA_DIR, "block_trade.json")
MARKET_JSON = os.path.join(DATA_DIR, "market.json")


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
        -- 個股收盤價（勝率 +5 交易日比對用；來源 TaiwanStockPrice）
        CREATE TABLE IF NOT EXISTS stock_price(
            date     TEXT,
            stock_id TEXT,
            close    REAL,
            PRIMARY KEY (date, stock_id)
        );
        -- 個股價格已抓區間（增量：涵蓋 [start,end] 則不重抓）
        CREATE TABLE IF NOT EXISTS price_fetched(
            stock_id   TEXT PRIMARY KEY,
            start_date TEXT,
            end_date   TEXT,
            fetched_at TEXT
        );
        -- 鉅額交易（Phase 4；來源 TaiwanStockBlockTrade）
        CREATE TABLE IF NOT EXISTS block_trade(
            date          TEXT,
            stock_id      TEXT,
            trade_type    TEXT,
            price         REAL,
            volume        INTEGER,
            trading_money INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_block_date ON block_trade(date);
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


# ============ Phase 3：勝率分點排行 ============
def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    """勝率的 Wilson 分數下界（懲罰小樣本，避免 10 場 8 勝灌水贏過 200 場 62%）。"""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (centre - margin) / denom


def extract_events(conn: sqlite3.Connection, trading_days: list[str]) -> list[dict]:
    """從 branch_daily 抽事件：某 (分點,股,日) 淨買超金額 ≥ EVENT_MIN_AMOUNT。"""
    ph = ",".join("?" * len(trading_days))
    query = f"""
        SELECT date, securities_trader_id, securities_trader, stock_id,
               SUM(buy * price) - SUM(sell * price) AS net_amount
        FROM branch_daily
        WHERE date IN ({ph})
        GROUP BY date, securities_trader_id, securities_trader, stock_id
        HAVING net_amount >= ?
    """
    rows = conn.execute(query, (*trading_days, EVENT_MIN_AMOUNT)).fetchall()
    return [{"date": r[0], "securities_trader_id": r[1], "securities_trader": r[2],
             "stock_id": r[3], "net_amount": r[4]} for r in rows]


def fetch_stock_price(dl: DataLoader, stock_id: str, start: str, end: str):
    """查個股日價量（TaiwanStockPrice）；錯誤轉可讀訊息並中止。"""
    try:
        return dl.taiwan_stock_daily(
            stock_id=stock_id, start_date=start, end_date=end, timeout=REQ_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"股價查詢失敗（{stock_id}）：{exc}")


def _price_covered(conn, stock_id, start, end) -> bool:
    row = conn.execute(
        "SELECT start_date, end_date FROM price_fetched WHERE stock_id = ?", (stock_id,)).fetchone()
    return bool(row and row[0] <= start and row[1] >= end)


def ensure_prices(dl: DataLoader, conn: sqlite3.Connection,
                  stocks: list[str], start: str, end: str, budget: int) -> dict:
    """對 event 個股補齊 [start,end] 的收盤價；每檔一次請求（涵蓋整段），至多 budget 檔。"""
    fetched, stopped = 0, False
    for stock_id in stocks:
        if _price_covered(conn, stock_id, start, end):
            continue
        if fetched >= budget:
            stopped = True
            break
        df = fetch_stock_price(dl, stock_id, start, end)
        recs = []
        if len(df):
            if "close" not in df.columns:
                raise SystemExit(f"股價欄位缺 close（{stock_id}）；實際={sorted(df.columns)}")
            for d, c in zip(df["date"].astype(str), df["close"].tolist()):
                recs.append((str(d)[:10], stock_id, float(c)))
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stock_price(date, stock_id, close) VALUES (?, ?, ?)", recs)
            conn.execute(
                "INSERT OR REPLACE INTO price_fetched(stock_id, start_date, end_date, fetched_at)"
                " VALUES (?, ?, ?, ?)",
                (stock_id, start, end, datetime.now(timezone.utc).isoformat()))
        fetched += 1
    covered = sum(1 for s in stocks if _price_covered(conn, s, start, end))
    print(f"[prices] 本次抓 {fetched} 檔股價；已涵蓋 {covered}/{len(stocks)} 檔"
          f"{'（達上限，下次續跑）' if stopped else ''}")
    return {"fetched": fetched, "covered": covered, "target": len(stocks), "stopped_early": stopped}


def compute_ranking(conn: sqlite3.Connection, trading_days: list[str],
                    events: list[dict]) -> dict:
    """以事件＋+HOLD_DAYS 交易日 close 判勝負，彙總各分點勝率並以 Wilson 下界排序。"""
    idx = {d: i for i, d in enumerate(trading_days)}
    n = len(trading_days)
    stocks = {e["stock_id"] for e in events}
    close = {}
    for s in stocks:
        for d, c in conn.execute("SELECT date, close FROM stock_price WHERE stock_id = ?", (s,)):
            close[(d, s)] = c
    per: dict = {}
    for e in events:
        tid = e["securities_trader_id"]
        st = per.setdefault(tid, {"securities_trader": e["securities_trader"],
                                  "events": 0, "evaluable": 0, "wins": 0, "pending": 0})
        st["events"] += 1
        i = idx.get(e["date"])
        if i is None or i + HOLD_DAYS >= n:
            st["pending"] += 1
            continue
        c0 = close.get((e["date"], e["stock_id"]))
        c1 = close.get((trading_days[i + HOLD_DAYS], e["stock_id"]))
        if c0 is None or c1 is None:
            st["pending"] += 1
            continue
        st["evaluable"] += 1
        if c1 > c0:
            st["wins"] += 1
    ranking = []
    for tid, st in per.items():
        if st["evaluable"] >= MIN_EVENTS:
            wr = st["wins"] / st["evaluable"]
            ranking.append({
                "securities_trader_id": tid, "securities_trader": st["securities_trader"],
                "events": st["events"], "evaluable": st["evaluable"], "wins": st["wins"],
                "pending": st["pending"], "win_rate": round(wr, 4),
                "wilson_lb": round(wilson_lb(st["wins"], st["evaluable"]), 4),
            })
    ranking.sort(key=lambda x: x["wilson_lb"], reverse=True)
    return {"ranking": ranking, "branches_considered": len(per), "branches_ranked": len(ranking)}


def export_ranking(result: dict, api_info: dict) -> None:
    payload = {
        "phase": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {"lookback_days": LOOKBACK_DAYS, "event_min_amount": EVENT_MIN_AMOUNT,
                   "hold_days": HOLD_DAYS, "min_events": MIN_EVENTS},
        "win_definition": ("分點對個股單日淨買超 ≥ 門檻計 1 事件；事件日+HOLD_DAYS 交易日 "
                           "close > 事件日 close 為勝；排序用 Wilson 下界（懲罰小樣本）"),
        "branches_considered": result["branches_considered"],
        "branches_ranked": result["branches_ranked"],
        "ranking": result["ranking"],
        "api_hourly_limit": api_info.get("limit"),
        "source": "FinMind",
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RANKING_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[ranking] 寫出 {RANKING_JSON}；納入排行 {result['branches_ranked']}/"
          f"{result['branches_considered']} 個分點")
    for r in result["ranking"][:10]:
        print(f"[rank] {r['securities_trader_id']}({r['securities_trader']}) "
              f"勝率={r['win_rate']:.1%} Wilson={r['wilson_lb']:.3f} "
              f"(勝{r['wins']}/{r['evaluable']}，事件{r['events']}，pending{r['pending']})")


# ============ Phase 4：鉅額交易看板 ============
BLOCK_COLS = ["date", "stock_id", "trade_type", "price", "volume", "trading_money"]


def _df_to_block_rows(df) -> list:
    if df is None or not len(df):
        return []
    missing = set(BLOCK_COLS) - set(df.columns)
    if missing:
        raise SystemExit(f"鉅額欄位缺 {sorted(missing)}；實際={sorted(df.columns)}")
    return [tuple(r) for r in df[BLOCK_COLS].itertuples(index=False, name=None)]


def fetch_block_trades(dl: DataLoader, start: str, end: str, watchlist: list[str]) -> list:
    """抓鉅額交易：先試全市場（空 stock_id）；被拒或空則退回逐股查追蹤清單。"""
    try:
        df = dl.taiwan_stock_block_trade(start_date=start, end_date=end, timeout=REQ_TIMEOUT)
        rows = _df_to_block_rows(df)
        if rows:
            print(f"[block] 全市場查詢成功 {len(rows)} 筆（{start}~{end}）；欄位={sorted(df.columns)}")
            return rows
        print("[block] 全市場查詢回空，改逐股查追蹤清單")
    except Exception as exc:  # noqa: BLE001
        print(f"[block] 全市場查詢失敗（{exc}）；改逐股查追蹤清單")
    rows: list = []
    for s in watchlist:
        try:
            df = dl.taiwan_stock_block_trade(stock_id=s, start_date=start, end_date=end,
                                             timeout=REQ_TIMEOUT)
            rows += _df_to_block_rows(df)
        except Exception as exc:  # noqa: BLE001
            print(f"[block] {s} 查詢失敗：{exc}")
    print(f"[block] 追蹤清單查詢得 {len(rows)} 筆")
    return rows


def store_block_trades(conn: sqlite3.Connection, days: list[str], rows: list) -> None:
    with conn:
        conn.executemany("DELETE FROM block_trade WHERE date = ?", [(d,) for d in days])
        conn.executemany(
            "INSERT INTO block_trade(date, stock_id, trade_type, price, volume, trading_money)"
            " VALUES (?, ?, ?, ?, ?, ?)", rows)


def export_block(conn: sqlite3.Connection, days: list[str], api_info: dict) -> None:
    ph = ",".join("?" * len(days))
    rows = conn.execute(f"""
        SELECT b.date, b.stock_id, b.trade_type, b.price, b.volume, b.trading_money, p.close
        FROM block_trade b
        LEFT JOIN stock_price p ON p.date = b.date AND p.stock_id = b.stock_id
        WHERE b.date IN ({ph})
        ORDER BY b.date DESC, b.trading_money DESC
        LIMIT ?
    """, (*days, BLOCK_TOP_N)).fetchall()
    items = []
    for r in rows:
        close = r[6]
        prem = round((r[3] - close) / close, 4) if close else None
        items.append({"date": r[0], "stock_id": r[1], "trade_type": r[2],
                      "price": round(r[3], 4), "volume": int(r[4] or 0),
                      "trading_money": int(r[5] or 0),
                      "close": round(close, 4) if close else None,
                      "premium_discount": prem})
    payload = {
        "phase": 4, "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "count": len(items),
        "premium_note": "折溢價 = (鉅額成交價 − 當日收盤) / 當日收盤（正=溢價）",
        "block_trades": items, "source": "FinMind",
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BLOCK_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[block] 寫出 {BLOCK_JSON}；{len(items)} 筆（{days[0]}~{days[-1]}）")


# ============ Phase 5：成交資訊（大盤 + 追蹤清單） ============
def fetch_twse_fmtqik() -> dict | None:
    """TWSE 每日市場成交資訊（大盤：加權指數收盤＋成交金額）。沙盒 egress 擋、Actions 可達。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            TWSE_FMTQIK_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[market] TWSE FMTQIK 取得失敗：{exc}")
        return None
    if not isinstance(data, list) or not data:
        print(f"[market] TWSE FMTQIK 回傳非預期：{type(data)}")
        return None
    latest = data[-1]  # 最新一筆
    print(f"[market] TWSE FMTQIK 欄位鍵={list(latest.keys())}；筆數={len(data)}")
    return latest


def export_market(dl: DataLoader, conn: sqlite3.Connection, watchlist: list[str],
                  trading_days: list[str], api_info: dict) -> None:
    last = trading_days[-1]
    start = trading_days[max(0, len(trading_days) - 5)]
    # 追蹤清單：抓每檔近幾日 TaiwanStockPrice，取最新一日完整價量
    quotes = []
    for s in watchlist:
        try:
            df = dl.taiwan_stock_daily(stock_id=s, start_date=start, end_date=last,
                                       timeout=REQ_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"[market] {s} 價量查詢失敗：{exc}")
            continue
        if df is None or not len(df):
            continue
        row = df.sort_values("date").iloc[-1]
        quotes.append({
            "stock_id": s, "date": str(row["date"])[:10],
            "open": float(row.get("open", 0) or 0), "max": float(row.get("max", 0) or 0),
            "min": float(row.get("min", 0) or 0), "close": float(row.get("close", 0) or 0),
            "spread": float(row.get("spread", 0) or 0),
            "trading_volume": int(row.get("Trading_Volume", 0) or 0),
            "trading_money": int(row.get("Trading_money", 0) or 0),
        })
    market = fetch_twse_fmtqik()
    payload = {
        "phase": 5, "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": last,
        "market_twse_fmtqik": market,  # 原始鍵保留（Phase 6 依實測鍵渲染）
        "watchlist": watchlist, "quotes": quotes,
        "source": "FinMind + TWSE",
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MARKET_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[market] 寫出 {MARKET_JSON}；追蹤 {len(quotes)} 檔，大盤={'有' if market else '無'}")


# ============ main ============
def main() -> None:
    parser = argparse.ArgumentParser(description="tw-branch-radar collector")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="回補最近幾個交易日")
    parser.add_argument("--anchor", default="", help="錨定日 YYYY-MM-DD（預設今天）")
    parser.add_argument("--branches", default="",
                        help="逗號分隔分點代碼 securities_trader_id（預設 DEFAULT_BRANCHES）")
    parser.add_argument("--watchlist", default="",
                        help="逗號分隔追蹤股票代碼（預設 DEFAULT_WATCHLIST）")
    args = parser.parse_args()

    anchor = date.fromisoformat(args.anchor) if args.anchor else date.today()
    override = [s.strip() for s in args.branches.split(",") if s.strip()]
    branches = override or list(DEFAULT_BRANCHES)
    watchlist = [s.strip() for s in args.watchlist.split(",") if s.strip()] or list(DEFAULT_WATCHLIST)
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

        # Phase 3：勝率排行（僅在分點資料已完整回補時計算）
        if progress["remaining"] == 0:
            events = extract_events(conn, trading_days)
            stocks = sorted({e["stock_id"] for e in events})
            print(f"[events] 事件數={len(events)}，涉及 {len(stocks)} 檔個股"
                  f"（門檻淨買超 ≥ {EVENT_MIN_AMOUNT:,}）")
            budget_left = max(0, MAX_REQ_PER_RUN - progress["fetched_this_run"])
            ensure_prices(dl, conn, stocks, trading_days[0], trading_days[-1], budget_left)
            result = compute_ranking(conn, trading_days, events)
            export_ranking(result, api_info)
        else:
            print(f"[ranking] 分點資料尚未完整回補（剩 {progress['remaining']} 對），本次略過排行")

        # Phase 4：鉅額交易看板（最近 BLOCK_DAYS 交易日）
        block_days = trading_days[-BLOCK_DAYS:]
        block_rows = fetch_block_trades(dl, block_days[0], block_days[-1], watchlist)
        store_block_trades(conn, block_days, block_rows)
        block_stocks = sorted({r[1] for r in block_rows})
        if block_stocks:
            ensure_prices(dl, conn, block_stocks, block_days[0], block_days[-1], MAX_REQ_PER_RUN)
        export_block(conn, block_days, api_info)

        # Phase 5：成交資訊（大盤 TWSE FMTQIK + 追蹤清單價量）
        export_market(dl, conn, watchlist, trading_days, api_info)
    finally:
        conn.close()
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
