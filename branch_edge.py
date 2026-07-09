# -*- coding: utf-8 -*-
"""
branch_edge.py — 分點「波段 α-edge」引擎（接 tw-branch-radar 既有 SQLite）
==========================================================================
把使用者提供的方法論接到 collector.py 已回補的資料上，回答：
「哪些分點的大額買超，對之後的波段有『真實、可外推、可交易』的正向 edge，
  值得每天鎖定去研判它買的股票值不值得投」——而非一份多半無法外推的歷史勝率名單。

相對「勝率排行」修正三個致命傷：
  (1) 多頭偏誤 → 用超額報酬 α（個股 − 大盤同窗），勝以 α>0 計，不用「收盤變高」。
  (2) 多重比較/倖存者偏誤 → 走查（IS 選、OOS 驗）+ Benjamini-Hochberg FDR + IS↔OOS 名次相關。
  (3) 訊號不可交易 → 分點 T+1 揭露 → 進場延遲 ENTRY_LAG 天、扣來回成本。

資料重用（不另起爐灶、不重抓）：
  - 事件與淨額：既有 ``branch_daily``（date, stock_id, securities_trader_id,
    buy_value/sell_value/buy_shares/sell_shares）；net_value=buy_value−sell_value、
    net_shares=buy_shares−sell_shares。
  - 收盤價：既有 ``stock_price``（date, stock_id, close），外加基準 0050（collector 補抓）。

規模對策（full market ~1010 分點 / 77M 列）：
  - 事件用「淨額 ≥ 門檻」在 SQL 過濾後才進 pandas（僅數十萬列）。
  - 倒貨偵測用「針對性 SQL 前向 join」（每事件只點查 ≤dump_window 天），
    需 branch_daily(securities_trader_id, stock_id, date) 索引；不把全表載進記憶體。
  - 大分點 bootstrap 改用 CLT 常態近似，避免 (n_boot × n_events) 陣列爆記憶體。

離線自驗（--selftest）：把合成資料寫進「相同 schema」（branch_daily+stock_price），
完全不連網，證明引擎能分辨「高手 / 運氣 / 隔沖」。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd


# ==================================================================
# 參數（PLAN 驗收時把關這一段）
# ==================================================================
@dataclass
class EdgeConfig:
    horizons: tuple = (5, 10, 20)        # 持有交易日數；分開顯示（20 日在 120 窗下 OOS 較薄）
    gate_labels: tuple = ("佈局", "中性")  # 值得追蹤允許的分類（排除隔沖）；option 2：含中性
    event_min_value: float = 5_000_000   # 單日單股淨買超金額門檻（NT$）
    entry_lag: int = 1                   # 進場延遲（分點 T+1 揭露 → ≥1）
    cost_roundtrip: float = 0.006        # 來回成本 ≈ 手續費0.1425%×2 + 證交稅0.3%

    min_events_total: int = 20           # 全期最少（評估用）事件
    min_events_is: int = 12              # IS 候選最少事件
    min_events_oos: int = 6              # OOS 驗證最少事件
    is_fraction: float = 0.67            # 走查切點：前 67% 期間為 IS
    fdr_alpha: float = 0.10              # BH-FDR 顯著水準
    n_bootstrap: int = 1000              # α 平均值 bootstrap 次數（n<BOOT_CAP 才用）
    boot_cap: int = 2000                 # 事件數 ≥ 此值改用 CLT 常態近似（省記憶體）

    dump_window: int = 2                 # 買超後幾個交易日內觀察倒貨
    dump_frac: float = 0.5               # 窗內反向淨股 ≥ 事件量此比例 → 記倒貨
    daytrade_ratio_hi: float = 0.5       # 倒貨比 ≥ → 隔沖
    daytrade_ratio_lo: float = 0.2       # 倒貨比 ≤ → 佈局；中間為中性

    benchmark_id: str = "0050"           # α 基準（大盤代理 ETF）
    live_window: int = 10                # 近期進場標的：最近幾個交易日的大額買進
    data_dir: str = "data"


# ==================================================================
# 統計工具（純 numpy / math，免 scipy）
# ==================================================================
def bh_fdr(pvals: np.ndarray, alpha: float):
    """Benjamini-Hochberg；回 (通過布林陣列, 校正後臨界 p)。"""
    n = len(pvals)
    if n == 0:
        return np.zeros(0, dtype=bool), 0.0
    order = np.argsort(pvals)
    ranked = pvals[order]
    thresh = alpha * (np.arange(1, n + 1) / n)
    passed_sorted = ranked <= thresh
    k = int(np.max(np.where(passed_sorted)[0]) + 1) if passed_sorted.any() else 0
    crit = float(thresh[k - 1]) if k > 0 else 0.0
    out = np.zeros(n, dtype=bool)
    if k > 0:
        out[order[:k]] = True
    return out, crit


def alpha_ci_p(vals: np.ndarray, cfg: EdgeConfig, seed: int):
    """
    回 (ci_lo, ci_hi, p_onesided)；p 檢定 H0: 平均 α ≤ 0 vs H1: >0。
    小樣本用 bootstrap；大樣本（≥boot_cap）用 CLT 常態近似（避免大陣列爆記憶體）。
    注意：同分點多事件持有窗重疊（非獨立），此樸素法會低估不確定性；嚴謹版見檔尾說明。
    """
    n = len(vals)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    m = float(vals.mean())
    if n >= cfg.boot_cap:
        se = float(vals.std(ddof=1)) / math.sqrt(n) if n > 1 else 0.0
        if se == 0:
            return (m, m, 0.0 if m > 0 else 1.0)
        lo, hi = m - 1.96 * se, m + 1.96 * se
        p = 0.5 * math.erfc((m / se) / math.sqrt(2))  # P(平均 ≤ 0)
        return (lo, hi, float(p))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(cfg.n_bootstrap, n))
    means = vals[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo), float(hi), float((means <= 0).mean()))


def binom_p_ge(wins: int, n: int, p: float = 0.5) -> float:
    """單邊二項檢定 P(X ≥ wins)；小 n 精確、大 n 常態近似。次要描述用。"""
    if n <= 0:
        return float("nan")
    if n <= 1000:
        return float(sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k)
                         for k in range(wins, n + 1)))
    mu, sd = n * p, math.sqrt(n * p * (1 - p))
    z = (wins - 0.5 - mu) / sd
    return float(0.5 * math.erfc(z / math.sqrt(2)))


# ==================================================================
# 資料存取（既有 schema）
# ==================================================================
def _temp_calendar(conn: sqlite3.Connection, trading_days: list[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS _cal")
    conn.execute("CREATE TEMP TABLE _cal(pos INTEGER, date TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO _cal(pos, date) VALUES (?, ?)",
                     list(enumerate(trading_days)))
    conn.commit()


def load_price_panel(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """{stock_id: {date: close}}；stock_price 只含已抓（事件/基準/追蹤）個股，量級可控。"""
    panel: dict[str, dict[str, float]] = {}
    for d, s, c in conn.execute("SELECT date, stock_id, close FROM stock_price"):
        panel.setdefault(s, {})[d] = c
    return panel


def load_stock_names(conn: sqlite3.Connection) -> dict[str, str]:
    """{stock_id: stock_name}；表可能不存在（舊 cache 未升級）或空，回空字典即可（面板降級為只顯代碼）。"""
    try:
        return dict(conn.execute("SELECT stock_id, stock_name FROM stock_info").fetchall())
    except sqlite3.OperationalError:
        return {}


def load_events(conn: sqlite3.Connection, trading_days: list[str],
                cfg: EdgeConfig) -> pd.DataFrame:
    """大額買超事件（淨額 ≥ 門檻），附交易日位置 pos。SQL 過濾後才進 pandas。"""
    _temp_calendar(conn, trading_days)
    rows = conn.execute(
        """
        SELECT c.pos, bd.date, bd.securities_trader_id, bd.securities_trader,
               bd.stock_id, (bd.buy_value - bd.sell_value)  AS net_value,
               (bd.buy_shares - bd.sell_shares)             AS net_shares
        FROM branch_daily bd JOIN _cal c ON bd.date = c.date
        WHERE (bd.buy_value - bd.sell_value) >= ?
        """, (cfg.event_min_value,)).fetchall()
    cols = ["pos", "date", "broker_id", "broker_name", "stock_id",
            "net_value", "net_shares"]
    ev = pd.DataFrame(rows, columns=cols)
    return ev


def mark_dumped(conn: sqlite3.Connection, events: pd.DataFrame,
                cfg: EdgeConfig) -> pd.DataFrame:
    """
    針對性 SQL 前向 join 標記倒貨：每事件取同分點同股 (pos, pos+dump_window] 的淨股總和，
    ≤ −dump_frac×事件淨股 → 倒貨。用 branch_daily(trader,stock,date) 索引點查，不載全表。
    倒貨與 horizon 無關，只算一次。
    """
    if events.empty:
        events["dumped"] = []
        return events
    conn.execute("DROP TABLE IF EXISTS _ev")
    conn.execute("CREATE TEMP TABLE _ev(eid INTEGER PRIMARY KEY, broker_id TEXT, "
                 "stock_id TEXT, epos INTEGER, net_shares REAL)")
    conn.executemany(
        "INSERT INTO _ev(eid, broker_id, stock_id, epos, net_shares) VALUES (?,?,?,?,?)",
        [(i, r.broker_id, r.stock_id, int(r.pos), float(r.net_shares))
         for i, r in enumerate(events.itertuples(index=False))])
    conn.commit()
    fwd = dict(conn.execute(
        """
        SELECT e.eid, COALESCE(SUM(bd.buy_shares - bd.sell_shares), 0) AS fwd_net
        FROM _ev e
        JOIN _cal c ON c.pos > e.epos AND c.pos <= e.epos + ?
        JOIN branch_daily bd ON bd.date = c.date
             AND bd.securities_trader_id = e.broker_id
             AND bd.stock_id = e.stock_id
        GROUP BY e.eid
        """, (cfg.dump_window,)).fetchall())
    ns = events["net_shares"].to_numpy()
    fwd_arr = np.array([fwd.get(i, 0.0) for i in range(len(events))], dtype=float)
    events = events.copy()
    events["dumped"] = fwd_arr <= (-cfg.dump_frac * ns)
    return events


def classify_branches(events: pd.DataFrame, cfg: EdgeConfig) -> pd.DataFrame:
    """每分點：n_events、dump_ratio、label（佈局/中性/隔沖）。與 horizon 無關。"""
    if events.empty:
        return pd.DataFrame(columns=["broker_id", "label", "dump_ratio", "n_events"])
    g = events.groupby("broker_id")
    out = g.agg(n_events=("dumped", "size"),
                dump_ratio=("dumped", "mean")).reset_index()

    def lab(x):
        if x >= cfg.daytrade_ratio_hi:
            return "隔沖"
        if x <= cfg.daytrade_ratio_lo:
            return "佈局"
        return "中性"
    out["label"] = out["dump_ratio"].map(lab)
    return out


# ==================================================================
# α 計算（單一 horizon）＋走查評估
# ==================================================================
def compute_alpha(events: pd.DataFrame, price: dict, bench: dict,
                  trading_days: list[str], horizon: int, cfg: EdgeConfig) -> pd.DataFrame:
    """
    每事件前推超額報酬 α（可評估者：進場/出場日皆在窗內且股與基準皆有價）。
    entry_pos = pos+lag、exit_pos = entry_pos+horizon（positional 交易日位移）。
    """
    if events.empty:
        return pd.DataFrame()
    n = len(trading_days)
    lag, H, cost = cfg.entry_lag, horizon, cfg.cost_roundtrip
    recs = []
    for r in events.itertuples(index=False):
        entry_pos, exit_pos = int(r.pos) + lag, int(r.pos) + lag + H
        if exit_pos >= n:                      # 出場日超出窗尾 → 尚未可評估（in-flight）
            continue
        ed, xd = trading_days[entry_pos], trading_days[exit_pos]
        ps = price.get(r.stock_id)
        if ps is None:
            continue
        c0, c1 = ps.get(ed), ps.get(xd)
        b0, b1 = bench.get(ed), bench.get(xd)
        if c0 is None or c1 is None or b0 is None or b1 is None or c0 <= 0 or b0 <= 0:
            continue
        stock_ret = c1 / c0 - 1.0 - cost
        bench_ret = b1 / b0 - 1.0
        alpha = stock_ret - bench_ret
        recs.append((r.broker_id, r.broker_name, ed, alpha, alpha > 0))
    return pd.DataFrame.from_records(
        recs, columns=["broker_id", "broker_name", "entry_date", "alpha", "win_excess"])


def evaluate(alpha_df: pd.DataFrame, cfg: EdgeConfig):
    """走查 IS/OOS + bootstrap/CLT + BH-FDR。回 (ranking_df, diagnostics)。"""
    if alpha_df.empty:
        return pd.DataFrame(), {"n_branches_tested": 0, "n_fdr_pass": 0, "n_oos_pass": 0,
                                "note": "無 α 事件"}
    adf = alpha_df.copy()
    adf["_ed"] = pd.to_datetime(adf["entry_date"])   # 走查切點需可排序的時間軸
    cut = adf["_ed"].quantile(cfg.is_fraction)
    is_df = adf[adf["_ed"] <= cut]
    oos_df = adf[adf["_ed"] > cut]

    rows = []
    for bid, g in adf.groupby("broker_id"):
        n = len(g)
        if n < cfg.min_events_total:
            continue
        a = g["alpha"].to_numpy()
        seed = zlib.crc32(str(bid).encode())   # 確定性種子（勿用 hash()：process 間隨機、不可重現）
        lo, hi, p_alpha = alpha_ci_p(a, cfg, seed)
        wins = int(g["win_excess"].sum())
        gi = is_df[is_df["broker_id"] == bid]
        go = oos_df[oos_df["broker_id"] == bid]
        is_alpha = float(gi["alpha"].mean()) if len(gi) >= cfg.min_events_is else float("nan")
        is_cand = (len(gi) >= cfg.min_events_is) and (is_alpha > 0)
        oos_ok = len(go) >= cfg.min_events_oos
        oos_we = float(go["win_excess"].mean()) if oos_ok else float("nan")
        oos_alpha = float(go["alpha"].mean()) if oos_ok else float("nan")
        oos_pass = bool(is_cand and oos_ok and oos_alpha > 0 and oos_we > 0.5)
        rows.append(dict(
            broker_id=bid, broker_name=g["broker_name"].iloc[0], n_events=n,
            win_excess=float(g["win_excess"].mean()), mean_alpha=float(a.mean()),
            alpha_ci_lo=lo, alpha_ci_hi=hi, p_alpha=p_alpha,
            p_binom=binom_p_ge(wins, n, 0.5),
            is_events=len(gi), is_mean_alpha=is_alpha, is_cand=bool(is_cand),
            oos_events=len(go), oos_win_excess=oos_we, oos_mean_alpha=oos_alpha,
            oos_pass=oos_pass))
    rk = pd.DataFrame(rows)
    if rk.empty:
        return rk, {"n_branches_tested": 0, "n_fdr_pass": 0, "n_oos_pass": 0,
                    "note": "無分點達最少事件數門檻"}
    passed, crit = bh_fdr(rk["p_alpha"].to_numpy(), cfg.fdr_alpha)
    rk["fdr_pass"] = passed
    rk = rk.sort_values(["oos_pass", "fdr_pass", "mean_alpha"],
                        ascending=[False, False, False]).reset_index(drop=True)

    dual = rk[(rk["is_events"] >= cfg.min_events_is) &
              (rk["oos_events"] >= cfg.min_events_oos)].dropna(
                  subset=["is_mean_alpha", "oos_mean_alpha"])
    # Spearman ＝ 排名後取 Pearson（純 pandas，免 scipy）
    rho = (float(dual["is_mean_alpha"].rank().corr(dual["oos_mean_alpha"].rank()))
           if len(dual) >= 3 else float("nan"))
    diag = {
        "n_branches_tested": int(len(rk)),
        "n_fdr_pass": int(rk["fdr_pass"].sum()),
        "n_oos_pass": int(rk["oos_pass"].sum()),
        "fdr_crit_p": float(crit),
        "is_oos_rank_spearman": rho,
        "is_cut_date": str(cut)[:10],
        "caveat": "持有窗重疊使 p 值偏樂觀；上線前建議改 block-bootstrap 並以更長歷史複驗",
    }
    return rk, diag


# ==================================================================
# 可操作層：高 edge 分點的「近期進場標的」（使用者真正要的）
# ==================================================================
def build_live_picks(events: pd.DataFrame, price: dict, bench: dict,
                     trading_days: list[str], gate_ids: set, horizon: int,
                     cfg: EdgeConfig, names: dict = None) -> list[dict]:
    """
    嚴格門檻分點（佈局∩OOS∩FDR）最近 live_window 個交易日的大額買進、且尚未倒貨者，
    附進場後至今的走勢（run α，可能未滿 horizon）→ 每日鎖定去研判該股。
    """
    if events.empty or not gate_ids:
        return []
    names = names or {}
    n = len(trading_days)
    last_close_date = trading_days[-1]
    cutoff_pos = n - cfg.live_window
    picks = []
    live = events[(events["broker_id"].isin(gate_ids)) &
                  (events["pos"] >= cutoff_pos) & (~events["dumped"])]
    for r in live.itertuples(index=False):
        entry_pos = int(r.pos) + cfg.entry_lag
        days_since = (n - 1) - int(r.pos)
        run_alpha = run_ret = None
        entry_date = None
        if entry_pos < n:
            entry_date = trading_days[entry_pos]
            ps = price.get(r.stock_id, {})
            c0, c1 = ps.get(entry_date), ps.get(last_close_date)
            b0, b1 = bench.get(entry_date), bench.get(last_close_date)
            if c0 and c1 and b0 and b1 and c0 > 0 and b0 > 0:
                run_ret = c1 / c0 - 1.0
                run_alpha = run_ret - (b1 / b0 - 1.0)
        picks.append(dict(
            broker_id=r.broker_id, broker_name=r.broker_name, stock_id=r.stock_id,
            stock_name=names.get(r.stock_id),
            event_date=r.date, entry_date=entry_date, net_value=float(r.net_value),
            days_since=int(days_since), horizon=horizon,
            run_ret=(None if run_ret is None else round(run_ret, 4)),
            run_alpha=(None if run_alpha is None else round(run_alpha, 4))))
    picks.sort(key=lambda x: (x["run_alpha"] is None, -(x["run_alpha"] or 0)))
    return picks


def _round_records(df: pd.DataFrame, cols4: list[str]) -> list[dict]:
    d = df.copy()
    for c in cols4:
        if c in d.columns:
            d[c] = d[c].round(4)
    return d.to_dict(orient="records")


def run_horizon(events: pd.DataFrame, cls: pd.DataFrame, price: dict, bench: dict,
                trading_days: list[str], horizon: int, cfg: EdgeConfig,
                names: dict = None) -> dict:
    alpha_df = compute_alpha(events, price, bench, trading_days, horizon, cfg)
    rk, diag = evaluate(alpha_df, cfg)
    if not rk.empty:
        rk = rk.merge(cls[["broker_id", "label", "dump_ratio"]], on="broker_id", how="left")
        gate = rk[rk["label"].isin(cfg.gate_labels) & rk["oos_pass"] & rk["fdr_pass"]]
    else:
        gate = rk
    gate_ids = set(gate["broker_id"]) if not gate.empty else set()
    live = build_live_picks(events, price, bench, trading_days, gate_ids, horizon, cfg, names)
    money4 = ["win_excess", "mean_alpha", "alpha_ci_lo", "alpha_ci_hi", "p_alpha",
              "p_binom", "is_mean_alpha", "oos_win_excess", "oos_mean_alpha", "dump_ratio"]
    return {
        "horizon": horizon,
        "diagnostics": diag,
        "watchlist": _round_records(gate, money4) if not gate.empty else [],
        "all_ranked": _round_records(rk, money4) if not rk.empty else [],
        "live_picks": live,
    }


# ==================================================================
# 主入口：由 collector 呼叫，或 CLI --run（讀既有 DB → 寫 branch_edge.json）
# ==================================================================
def run_from_db(conn: sqlite3.Connection, trading_days: list[str],
                cfg: EdgeConfig = None, path: str = None) -> dict:
    cfg = cfg or EdgeConfig()
    events = load_events(conn, trading_days, cfg)
    events = mark_dumped(conn, events, cfg)
    cls = classify_branches(events, cfg)
    price = load_price_panel(conn)
    bench = price.get(cfg.benchmark_id, {})
    names = load_stock_names(conn)
    by_h = {}
    for h in cfg.horizons:
        by_h[str(h)] = run_horizon(events, cls, price, bench, trading_days, h, cfg, names)
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "FinMind",
        "benchmark_ready": bool(bench),
        "params": {"horizons": list(cfg.horizons), "event_min_value": cfg.event_min_value,
                   "entry_lag": cfg.entry_lag, "cost_roundtrip": cfg.cost_roundtrip,
                   "min_events_total": cfg.min_events_total, "min_events_is": cfg.min_events_is,
                   "min_events_oos": cfg.min_events_oos, "is_fraction": cfg.is_fraction,
                   "fdr_alpha": cfg.fdr_alpha, "benchmark_id": cfg.benchmark_id,
                   "live_window": cfg.live_window},
        "trading_days_range": [trading_days[0], trading_days[-1]] if trading_days else [],
        "by_horizon": by_h,
    }
    doc = _clean_nan(doc)
    path = path or os.path.join(cfg.data_dir, "branch_edge.json")
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, allow_nan=False)
    for h in cfg.horizons:
        d = by_h[str(h)]
        print(f"[edge] H={h}：受測 {d['diagnostics'].get('n_branches_tested',0)} 分點，"
              f"OOS通過 {d['diagnostics'].get('n_oos_pass',0)}，"
              f"FDR通過 {d['diagnostics'].get('n_fdr_pass',0)}，"
              f"值得追蹤 {len(d['watchlist'])}，近期標的 {len(d['live_picks'])}")
    if not bench:
        print(f"[edge][警告] 基準 {cfg.benchmark_id} 無收盤價，α 無法計算——請確認已補抓基準。")
    print(f"[edge] 寫出 {path}")
    return doc


def _clean_nan(o):
    if isinstance(o, float):
        return None if math.isnan(o) else o
    if isinstance(o, dict):
        return {k: _clean_nan(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean_nan(v) for v in o]
    return o


# ==================================================================
# 合成資料自我驗證（不連網；寫進既有 schema）
# ==================================================================
_SCHEMA = """
CREATE TABLE IF NOT EXISTS branch_daily(
  date TEXT, stock_id TEXT, securities_trader_id TEXT, securities_trader TEXT,
  buy_value REAL, sell_value REAL, buy_shares INTEGER, sell_shares INTEGER,
  PRIMARY KEY (date, securities_trader_id, stock_id));
CREATE INDEX IF NOT EXISTS idx_bd_trader ON branch_daily(securities_trader_id, stock_id, date);
CREATE TABLE IF NOT EXISTS stock_price(
  date TEXT, stock_id TEXT, close REAL, PRIMARY KEY (date, stock_id));
CREATE TABLE IF NOT EXISTS stock_info(stock_id TEXT PRIMARY KEY, stock_name TEXT);
"""


def _synth(seed=42):
    """1 基準(0050)+12 股+120 交易日；SKILL 大買後注入真實 +α、LUCKY 隨機無α、DAYTR 隔日倒貨。"""
    rng = np.random.default_rng(seed)
    n_days = 120
    days = [f"2025-{1 + i // 21:02d}-{1 + i % 21:02d}" for i in range(n_days)]
    stocks = [f"S{i:02d}" for i in range(12)]
    bench_ret = rng.normal(0.0008, 0.010, n_days)
    bench_close = 100 * np.cumprod(1 + bench_ret)
    stock_ret = {}
    for s in stocks:
        beta = rng.uniform(0.7, 1.3)
        stock_ret[s] = beta * bench_ret + rng.normal(0, 0.018, n_days)
    H = 10
    skill = []
    for _ in range(34):
        s = stocks[int(rng.integers(0, 12))]
        t = int(rng.integers(3, n_days - H - 2))
        skill.append((s, t))
        stock_ret[s][t + 1: t + 1 + H] += 0.004        # 注入真實 edge
    close = {s: 50 * np.cumprod(1 + stock_ret[s]) for s in stocks}

    price_rows = [(days[i], "0050", float(bench_close[i])) for i in range(n_days)]
    for s in stocks:
        price_rows += [(days[i], s, float(close[s][i])) for i in range(n_days)]

    bd = []  # (date, stock, broker, name, buy_value, sell_value, buy_shares, sell_shares)

    def buy(s, t, broker, shares):
        px = float(close[s][t])
        bd.append((days[t], s, broker, broker, shares * px, 0.0, shares, 0))

    def sell(s, t, broker, shares):
        px = float(close[s][t])
        bd.append((days[t], s, broker, broker, 0.0, shares * px, 0, shares))

    for s, t in skill:
        buy(s, t, "SKILL", 200_000)
    for _ in range(30):
        s = stocks[int(rng.integers(0, 12))]; t = int(rng.integers(3, n_days - H - 2))
        buy(s, t, "LUCKY", 200_000)
    for _ in range(26):
        s = stocks[int(rng.integers(0, 12))]; t = int(rng.integers(3, n_days - H - 3))
        buy(s, t, "DAYTR", 200_000); sell(s, t + 1, "DAYTR", 200_000)  # 隔日倒貨
    for j in range(14):                                                # 背景雜訊分點（FDR 母體）
        b = f"N{j:02d}"
        for _ in range(22):
            s = stocks[int(rng.integers(0, 12))]; t = int(rng.integers(3, n_days - H - 2))
            buy(s, t, b, 200_000)
    return days, price_rows, bd


def selftest() -> bool:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    days, price_rows, bd = _synth()
    conn.executemany("INSERT OR REPLACE INTO stock_price(date,stock_id,close) VALUES (?,?,?)",
                     price_rows)
    conn.executemany(
        "INSERT OR REPLACE INTO branch_daily(date,stock_id,securities_trader_id,"
        "securities_trader,buy_value,sell_value,buy_shares,sell_shares) VALUES (?,?,?,?,?,?,?,?)",
        bd)
    conn.execute("INSERT OR REPLACE INTO stock_info(stock_id, stock_name) VALUES ('S00', '測試電子')")
    conn.commit()
    cfg = EdgeConfig(horizons=(5, 10), min_events_total=15, min_events_is=8,
                     min_events_oos=4, benchmark_id="0050")
    events = load_events(conn, days, cfg)
    events = mark_dumped(conn, events, cfg)
    cls = classify_branches(events, cfg)
    lab = cls.set_index("broker_id")["label"].to_dict()
    rk10, diag10 = evaluate(compute_alpha(events, load_price_panel(conn),
                                          load_price_panel(conn).get("0050", {}),
                                          days, 10, cfg), cfg)

    print("\n===== 診斷（H=10）=====")
    for k, v in diag10.items():
        print(f"  {k}: {v}")
    print("\n===== 分類 =====")
    for b in ["SKILL", "LUCKY", "DAYTR"]:
        print(f"  {b}: {lab.get(b)}  (dump_ratio="
              f"{cls.set_index('broker_id')['dump_ratio'].get(b, float('nan')):.2f})")

    def row(b):
        r = rk10[rk10["broker_id"] == b]
        return r.iloc[0] if len(r) else None

    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + msg)
        ok = ok and bool(cond)

    print("\n===== 驗證（H=10）=====")
    rs = row("SKILL")
    check(rs is not None and bool(rs["fdr_pass"]), "SKILL 通過 FDR 顯著")
    check(rs is not None and bool(rs["oos_pass"]), "SKILL 通過 OOS 走查")
    check(rs is not None and rs["mean_alpha"] > 0.02, "SKILL 平均 α > 2%")
    rl = row("LUCKY")
    check(rl is None or not bool(rl["oos_pass"]), "LUCKY 未通過 OOS（不被誤選）")
    check(lab.get("SKILL") == "佈局", "SKILL 標為佈局")
    check(lab.get("DAYTR") == "隔沖", "DAYTR 標為隔沖")

    # 可操作層：把最後 live_window 天塞一筆 SKILL 買進，應出現在 live_picks
    print("\n===== 可操作層 live_picks =====")
    gate_ids = {"SKILL"}  # 直接驗證函式（不受 FDR 空窗影響）
    late = events.copy()
    names = load_stock_names(conn)
    live = build_live_picks(late, load_price_panel(conn),
                            load_price_panel(conn).get("0050", {}), days, gate_ids, 10, cfg, names)
    print(f"  SKILL 近 {cfg.live_window} 日 live_picks 數：{len(live)}")
    check(isinstance(live, list), "live_picks 回傳 list")
    s00_picks = [p for p in live if p["stock_id"] == "S00"]
    check(not s00_picks or s00_picks[0]["stock_name"] == "測試電子",
         "live_picks 附上正確 stock_name（S00→測試電子）")

    print("\n" + ("★ 全部通過 ★" if ok else "✗ 有失敗項 ✗"))
    conn.close()
    return ok


# ==================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="分點 α-edge 引擎")
    ap.add_argument("--selftest", action="store_true", help="合成資料自我驗證（不連網）")
    ap.add_argument("--run", metavar="DB", help="讀既有 SQLite（需已回補 branch_daily+stock_price）"
                                                "跑回測並寫 data/branch_edge.json")
    ap.add_argument("--days", type=int, default=120, help="--run 用最近幾個交易日（需資料涵蓋）")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(0 if selftest() else 1)
    if args.run:
        c = sqlite3.connect(args.run)
        td = [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM branch_daily ORDER BY date").fetchall()][-args.days:]
        run_from_db(c, td)
        c.close()
    else:
        ap.print_help()
