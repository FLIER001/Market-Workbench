"""回测报表层：把一条净值序列拆成月/年收益、回撤段、夏普置信区间、分段稳定性。

只做统计，不做判断：不给最优参数、不预测未来、不把某一段的表现说成能力。
输入都是「日期列表 + 净值列表」，与 ``factor_backtest`` 的输出同源，不重新取数。

口径：
- 月度收益 = 月末净值 ÷ 上月末净值 − 1；首月以 1.0 为基准。
- 回撤段 = 从净值创出新高到跌回该新高的完整过程；没涨回来的段把结束日记为最后一个交易日并标明未复原。
- 夏普置信区间用**有放回重抽日收益**的 bootstrap（默认 1000 次、固定随机种子，结果可复现）；
  它衡量的是「这段样本换一批日子会怎样」，不是未来的置信区间。
- 分段检验把区间切成等长的几段，各段单独算指标：段间差异大说明结论依赖特定时期。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

PERIODS_PER_YEAR = 244
#: bootstrap 重抽次数与固定种子：同日重跑必须给出同一区间
BOOTSTRAP_ITERS = 1000
BOOTSTRAP_SEED = 20260101
DRAWDOWN_TOP = 5


def _r(value: Any, digits: int = 4) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return round(out, digits) if math.isfinite(out) else None


def _returns(nav: list[float]) -> np.ndarray:
    series = np.asarray(nav, dtype=float)
    if series.size < 2:
        return np.asarray([], dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = series[1:] / series[:-1] - 1.0
    return out[np.isfinite(out)]


def monthly_returns(dates: list[str], nav: list[float]) -> dict[str, float]:
    """{"YYYY-MM": 月收益}。同月最后一条净值当月末。"""
    last_of_month: dict[str, float] = {}
    for day, value in zip(dates, nav, strict=True):
        if not day or len(day) < 7:
            continue
        last_of_month[day[:7]] = float(value)          # 日期升序，后写覆盖前写
    out: dict[str, float] = {}
    previous = 1.0
    for month in sorted(last_of_month):
        value = last_of_month[month]
        if previous > 0:
            out[month] = _r(value / previous - 1.0) or 0.0
        previous = value
    return out


def drawdown_episodes(dates: list[str], nav: list[float], top: int = DRAWDOWN_TOP) -> list[dict[str, Any]]:
    """最深的前 N 段回撤。每段：峰值日、谷底日、复原日（没复原给 null）、深度、持续交易日数。"""
    if len(nav) < 2:
        return []
    peak_value, peak_day = nav[0], dates[0]
    episodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for day, value in zip(dates[1:], nav[1:], strict=True):
        if value >= peak_value:
            if current is not None:
                current["recovered"] = day          # 天数统一在下面按日期位置算
                episodes.append(current)
                current = None
            peak_value, peak_day = value, day
            continue
        depth = value / peak_value - 1.0
        if current is None:
            current = {"peak": peak_day, "trough": day, "depth": _r(depth),
                       "recovered": None, "days": None}
        elif depth < (current["depth"] or 0):
            current["trough"] = day
            current["depth"] = _r(depth)
    if current is not None:
        episodes.append(current)

    # 天数按日期在序列里的位置算，不受上面字典里临时键的影响
    index_of = {day: i for i, day in enumerate(dates)}
    for episode in episodes:
        start = index_of.get(episode["peak"], 0)
        trough = index_of.get(episode["trough"], start)
        end = index_of.get(episode["recovered"], len(dates) - 1) if episode["recovered"] else len(dates) - 1
        episode["drawdown_days"] = max(0, trough - start)
        episode["days"] = max(0, end - start)
        if not episode["recovered"]:
            episode["note"] = "到区间结束还没涨回前高"
    episodes.sort(key=lambda item: item["depth"] or 0)
    return episodes[:top]


def sharpe_ci(nav: list[float], *, iters: int = BOOTSTRAP_ITERS, seed: int = BOOTSTRAP_SEED,
              level: float = 0.95) -> dict[str, Any]:
    """夏普的 bootstrap 区间：对日收益有放回重抽 iters 次，取 level 分位。"""
    returns = _returns(nav)
    if returns.size < 30:
        return {"sharpe": None, "low": None, "high": None, "iters": 0,
                "note": "日收益不足 30 个，不给区间"}
    std = float(returns.std(ddof=1))
    if std <= 0:
        return {"sharpe": None, "low": None, "high": None, "iters": 0, "note": "日收益波动为 0"}
    point = float(returns.mean() / std * math.sqrt(PERIODS_PER_YEAR))
    rng = np.random.default_rng(seed)
    draws = rng.choice(returns, size=(iters, returns.size), replace=True)
    means = draws.mean(axis=1)
    stds = draws.std(axis=1, ddof=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        samples = means / stds * math.sqrt(PERIODS_PER_YEAR)
    samples = samples[np.isfinite(samples)]
    if samples.size < 10:
        return {"sharpe": _r(point, 3), "low": None, "high": None, "iters": 0, "note": "重抽结果不可用"}
    tail = (1.0 - level) / 2.0
    return {
        "sharpe": _r(point, 3),
        "low": _r(float(np.quantile(samples, tail)), 3),
        "high": _r(float(np.quantile(samples, 1.0 - tail)), 3),
        "iters": int(samples.size),
        "level": level,
        "method": "日收益有放回重抽（bootstrap），固定随机种子，可复现",
        "note": "区间反映这段样本自身的抽样波动，不是对未来的置信区间",
    }


def segments(dates: list[str], nav: list[float], parts: int = 2) -> list[dict[str, Any]]:
    """把区间切成等长的 parts 段，各段单独算区间收益、年化、夏普、最大回撤。"""
    if parts < 2 or len(nav) < parts * 30:
        return []
    size = len(nav) // parts
    out = []
    for i in range(parts):
        lo = i * size
        hi = (i + 1) * size if i < parts - 1 else len(nav)
        chunk_dates = dates[lo:hi]
        chunk = np.asarray(nav[lo:hi], dtype=float)
        if chunk.size < 2:
            continue
        base = float(nav[lo - 1]) if lo > 0 else 1.0
        returns = _returns(chunk.tolist())
        std = float(returns.std(ddof=1)) if returns.size > 1 else 0.0
        peak = np.maximum.accumulate(np.concatenate([[base], chunk]))
        with np.errstate(invalid="ignore", divide="ignore"):
            drawdown = float((np.concatenate([[base], chunk]) / peak - 1.0).min())
        out.append({
            "label": f"第 {i + 1} 段",
            "start": chunk_dates[0] if chunk_dates else None,
            "end": chunk_dates[-1] if chunk_dates else None,
            "days": int(chunk.size),
            "return": _r(float(chunk[-1]) / base - 1.0),
            "annualized": _r(float(chunk[-1] / base) ** (PERIODS_PER_YEAR / chunk.size) - 1.0),
            "sharpe": _r(float(returns.mean() / std * math.sqrt(PERIODS_PER_YEAR)), 3) if std > 0 else None,
            "max_drawdown": _r(drawdown),
        })
    return out


def build(dates: list[str], nav: list[float], benchmark: list[float] | None = None) -> dict[str, Any]:
    """一次算齐报表需要的四块：月度、回撤段、夏普区间、分段。"""
    if not dates or not nav or len(dates) != len(nav):
        return {"monthly_returns": {}, "drawdowns": [], "sharpe_ci": {}, "segments": []}
    logic = {
        "monthly_returns": monthly_returns(dates, nav),
        "drawdowns": drawdown_episodes(dates, nav),
        "sharpe_ci": sharpe_ci(nav),
        "segments": segments(dates, nav),
    }
    if benchmark and len(benchmark) == len(nav):
        # 相对基准的超额：只给累计差，不做择时解读
        logic["excess_total"] = _r(float(nav[-1]) - float(benchmark[-1]))
    return logic
