"""探索性组合回测：只多、等权、Top N、周/月调仓，T 收盘出信号 → T+1 开盘成交。

约束近似（探索级，非精算撮合）：
- 涨跌停：一字板（开=高=低）不买不卖，候选顺延下一名；
- 停牌：执行日无行情的新买跳过；持仓卖不掉则持有到下次调仓再试；
- 成本：佣金 0.025% 双边 + 印花税 0.05% 卖出 + 过户费 0.001% + 滑点 10bp 双边，按倍数 0/1/2/3 压力测试。

# ponytail: 逐日 Python 循环 + 宽表 pivot，全 A 10 年单次回测秒级；要参数扫描再向量化
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd

import factor_data
from factors import _factor_frame, MIN_CROSS_SECTION, factor_name_of

COST_BPS = {"commission": 2.5, "stamp_sell": 5.0, "transfer": 0.1, "slippage": 10.0}
RUNS_DIR = os.path.join(factor_data.DATA_DIR, "runs")


def run_backtest(factor: str, start: str | None = None, end: str | None = None,
                 top_n: int = 50, top_pct: float | None = None,
                 freq: str = "monthly", cost: float = 1.0,
                 min_days_listed: int = 60) -> dict:
    """跑一次组合回测。freq: weekly/monthly；top_pct 优先于 top_n（Top 20% = 0.2）。"""
    if freq not in ("weekly", "monthly"):
        raise ValueError("freq 必须是 weekly/monthly")

    # 因子需要 ~130 根 K 线预热：区间前多留 200 个交易日
    panel_all = factor_data.load_panel()
    dates_all = sorted(panel_all["date"].unique())
    window_dates = _window(dates_all, start, end)
    if len(window_dates) < 40:
        raise ValueError("回测区间太短（<40 交易日）")
    w_start, w_end = window_dates[0], window_dates[-1]
    warm_start = _shift_dates(dates_all, w_start, -200)
    panel = panel_all[(panel_all["date"] >= warm_start) & (panel_all["date"] <= w_end)]

    factors = _factor_frame(panel, factor)
    factors = factors[factors["days_since_list"] >= min_days_listed]

    close_wide = panel.pivot(index="date", columns="code", values="close").sort_index().ffill()
    open_wide = panel.pivot(index="date", columns="code", values="open").sort_index()
    high_wide = panel.pivot(index="date", columns="code", values="high").sort_index()
    low_wide = panel.pivot(index="date", columns="code", values="low").sort_index()

    # 基准：窗口起始日前已上市（首根 K 线 ≤ w_start）的存量池等权日收益
    first_bar = panel_all.groupby("code")["date"].min()
    bench_codes = set(first_bar[first_bar <= w_start].index) & set(close_wide.columns)

    rebalance_dates = _rebalance_dates(window_dates, freq)
    candidates_by_exec: dict[str, tuple[int, list[str]]] = {}  # 执行日 → (目标数, 按因子值降序候选)
    for sig in rebalance_dates:
        exec_day = _next_day(window_dates, sig)
        if exec_day is None:
            continue
        try:
            day = factors.xs(sig, level="date")
        except KeyError:
            continue
        day = day.dropna(subset=[factor])
        if len(day) < MIN_CROSS_SECTION:
            continue
        n = max(1, int(round(len(day) * top_pct))) if top_pct else top_n
        candidates_by_exec[exec_day] = (n, day[factor].nlargest(n + 30).index.tolist())
    if not candidates_by_exec:
        raise ValueError("给定区间无有效调仓信号日")

    results = {}
    for mult in (0.0, 1.0, 2.0, 3.0):
        results[mult] = _run_once(candidates_by_exec, close_wide, open_wide, high_wide, low_wide,
                                  window_dates, bench_codes, mult * cost)
    chosen = results[cost]

    payload = {
        "factor": factor,
        "factor_name": factor_name_of(factor),
        "params": {"start": w_start, "end": w_end, "top_n": top_n, "top_pct": top_pct,
                   "freq": freq, "cost_multiplier": cost, "min_days_listed": min_days_listed},
        "data_version": factor_data.data_version(),
        "metrics": chosen["metrics"],
        "cost_stress": {f"{m:g}x": r["metrics"] for m, r in results.items()},
        "nav": {"dates": chosen["dates"], "strategy": chosen["nav"], "benchmark": chosen["bench"]},
        "yearly_returns": chosen["yearly"],
        "biases": factor_data.BIAS_LABELS + ["探索级撮合：一字板顺延/停牌持仓近似，非精算"],
        "timing_note": "T 日收盘因子排名 → T+1 开盘等权成交",
    }
    _save_run(payload)
    return payload


def _run_once(candidates_by_exec: dict, close_wide, open_wide, high_wide, low_wide,
              window_dates: list[str], bench_codes: set, cost_mult: float) -> dict:
    """单条成本路径的逐日模拟。cost_mult=0 即无成本对照。"""
    buy_cost = (COST_BPS["commission"] + COST_BPS["transfer"] + COST_BPS["slippage"]) * 1e-4 * cost_mult
    sell_cost = (COST_BPS["commission"] + COST_BPS["stamp_sell"] + COST_BPS["transfer"]
                 + COST_BPS["slippage"]) * 1e-4 * cost_mult

    cash = 1_000_000.0
    shares: dict[str, float] = {}
    pending_sell: set[str] = set()
    nav_list: list[float] = []
    bench_list: list[float] = []
    dates_out: list[str] = []
    turnover_notional = 0.0  # 买入名义本金累计（单边换手口径）
    bench_nav = 1.0
    prev_close: pd.Series | None = None

    for d in window_dates:
        close = close_wide.loc[d]
        opens = open_wide.loc[d]
        highs = high_wide.loc[d]
        lows = low_wide.loc[d]

        # 1) 调仓日：先卖后买（等权目标 = 可成交候选前 N 只）
        cands = candidates_by_exec.get(d)
        target_set: set[str] | None = None
        if cands is not None:
            n_target, cand_list = cands
            target: list[str] = []
            for code in cand_list:
                if len(target) >= n_target:
                    break
                o, h, l = opens.get(code), highs.get(code), lows.get(code)
                if o is None or np.isnan(o):
                    continue  # 停牌
                if h == l == o:
                    continue  # 一字板买不进
                target.append(code)
            target_set = set(target)
            pending_sell = {c for c in shares if c not in target_set}

        for code in list(pending_sell):
            o, h, l = opens.get(code), highs.get(code), lows.get(code)
            if o is None or np.isnan(o):
                continue  # 停牌，下个调仓周期再试
            if h == l == o:
                continue  # 一字跌板卖不掉
            cash += shares[code] * o * (1 - sell_cost)
            del shares[code]
            pending_sell.discard(code)

        if cands is not None and target_set is not None:
            # 等权建仓：按目标集逐只补齐（首批用全部现金，避免预算=总权益导致现金不足逐只衰减）
            equity = cash + sum(q * close.get(c, 0.0) for c, q in shares.items())
            budget = equity / max(1, len(target_set))
            for code in target_set:
                if code in shares:
                    continue
                o = opens[code]
                spend = min(budget, cash)
                if spend <= 0:
                    break  # 现金耗尽（首批建仓后可能余量不足）
                qty = spend / (o * (1 + buy_cost))
                cash -= qty * o * (1 + buy_cost)
                shares[code] = qty
                turnover_notional += qty * o

        nav = cash + sum(q * close.get(c, 0.0) for c, q in shares.items())
        if prev_close is not None:
            prev = prev_close.reindex(close.index)
            rets = (close / prev - 1.0)
            rets = rets[rets.index.isin(bench_codes)].dropna()
            bench_nav *= (1 + (float(rets.mean()) if len(rets) else 0.0))
        prev_close = close
        nav_list.append(round(nav / 1_000_000.0, 6))
        bench_list.append(round(bench_nav, 6))
        dates_out.append(d)

    nav_s = pd.Series(nav_list)
    rets = nav_s.pct_change().dropna()
    n_years = max(len(nav_list) / 244.0, 1e-9)
    years = [d[:4] for d in dates_out]
    yearly = {}
    for y in sorted(set(years)):
        idx = [i for i, yy in enumerate(years) if yy == y]
        seg = nav_s.iloc[idx]
        yearly[y] = round(float(seg.iloc[-1] / seg.iloc[0] - 1.0), 4)
    metrics = {
        "total_return": round(float(nav_s.iloc[-1] - 1.0), 4),
        "ann_return": round(float((nav_s.iloc[-1]) ** (1 / n_years) - 1.0), 4),
        "ann_vol": round(float(rets.std() * np.sqrt(244)), 4) if len(rets) > 1 else None,
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(244)), 3) if len(rets) > 1 and rets.std() > 0 else None,
        "max_drawdown": round(float((nav_s / nav_s.cummax() - 1.0).min()), 4),
        "win_rate": round(float((rets > 0).mean()), 4) if len(rets) else None,
        "ann_turnover": round(turnover_notional / 1_000_000.0 / n_years, 2),
        "n_days": len(nav_list),
    }
    return {"metrics": metrics, "dates": dates_out, "nav": nav_list, "bench": bench_list, "yearly": yearly}


def _window(dates_all: list[str], start: str | None, end: str | None) -> list[str]:
    out = dates_all
    if start:
        out = [d for d in out if d >= start]
    if end:
        out = [d for d in out if d <= end]
    return out


def _shift_dates(dates_all: list[str], date: str, k: int) -> str:
    try:
        i = dates_all.index(date)
    except ValueError:
        return dates_all[0]
    return dates_all[max(0, i + k)]


def _rebalance_dates(window_dates: list[str], freq: str) -> list[str]:
    """每周/每月的最后一个交易日（作为信号日，T+1 执行）。"""
    dt = pd.DatetimeIndex(pd.to_datetime(window_dates))
    if freq == "weekly":
        keys = dt.strftime("%G-W%V")  # ISO 周避免跨年错组
    else:
        keys = dt.strftime("%Y-%m")
    s = pd.Series(window_dates, index=keys)
    return [g.iloc[-1] for _, g in s.groupby(level=0)]


def _next_day(window_dates: list[str], date: str) -> str | None:
    try:
        i = window_dates.index(date)
    except ValueError:
        return None
    return window_dates[i + 1] if i + 1 < len(window_dates) else None


def _save_run(payload: dict) -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}_{payload['factor']}_{payload['params']['freq']}.json"
    try:
        with open(os.path.join(RUNS_DIR, name), "w", encoding="utf-8") as f:
            json.dump({k: payload[k] for k in ("factor", "params", "data_version", "metrics", "cost_stress")},
                      f, ensure_ascii=False)
    except OSError:
        pass  # 工件写失败不影响结果返回
