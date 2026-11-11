"""持仓风险体检与历史情景回放（A 股 / ETF）。

两个入口都只服务「持仓」页，不需要模型：

- ``build_xray``：按当前持仓市值加权，算集中度、年化波动、最大回撤、单日 95/99% VaR 与 ES、
  分散度、两两相关性、每只的风险贡献、行业暴露。
- ``build_replay``：按现在的持仓比例，把 2015 股灾、2016 熔断、2018、2020-03、2021–2024 五段
  大跌重放一遍，给组合涨跌、期间最大回撤、最差单日，和同期沪深300并排，以及每只的贡献。

边界（照抄上游 Vibe-Research 的口径，改动要在注释里写清原因）：
- 只给数字，不出综合分、不评好坏；超没超线由用户自己设阈值判断。
- 缺数据进 ``gaps``，不当成 0，也不拿成本价凑市值；样本不够就不给结论。
- 只做 A 股与场内 ETF：本币美元 / 港元的持仓不换算，本版不算（``gaps`` 写明）。
- 统计只用滚动窗口内的历史收益，没有前视。
- 回放不是当时的真实持仓，不预测下一次，不给操作建议。

数据：个股与 ETF 走腾讯前复权日线；行业暴露走东财个股资料（一天缓存一次）；回放基准为沪深300。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

import astock
import cache_runtime
import factor_data
import portfolio as pf

PERIODS_PER_YEAR = 252
#: 一只至少要这么多根有效收盘价才进体检；不够的单独列进 gaps，不让它拉偏整组
MIN_HISTORY_DAYS = 60
#: 体检取多少根日线：约一年半，覆盖一轮像样的回撤
XRAY_KLINE_COUNT = 400
#: 有价格的持仓至少占这一组市值的这么多，才给组合回放数字
MIN_COVERAGE = 0.5
#: 回放起点前多取这么多天：起点恰好在停牌的，要能找到停牌前的收盘价
LEAD_DAYS = 30
#: 起止两头允许差几天（长假 + 周末），超过就当「当时还没上市」或「到结束还在停牌」
EDGE_DAYS = 10

BENCHMARK = {"secid": "1.000300", "label": "沪深300指数"}

#: 窗口 = 沪深300在这段时期收盘最高点到最低点（上游 2026-10-09 用真实数据逐段核对过，
#: 括号里是期间指数跌幅）。改日期要重新核一次首尾两天是不是真的极值。
SCENARIOS: list[dict[str, str]] = [
    {"id": "cn_2015", "label": "2015 年 6–8 月大跌", "start": "2015-06-08", "end": "2015-08-26"},      # −43.5%
    {"id": "cn_2016", "label": "2016 年初熔断", "start": "2015-12-22", "end": "2016-01-28"},          # −26.4%
    {"id": "cn_2018", "label": "2018 年下跌", "start": "2018-01-24", "end": "2019-01-03"},            # −32.5%
    {"id": "cn_2020", "label": "2020 年 3 月全球疫情冲击", "start": "2020-03-05", "end": "2020-03-23"},  # −16.1%
    {"id": "cn_2021_24", "label": "2021–2024 年长期回撤", "start": "2021-02-10", "end": "2024-02-02"},  # −45.3%
]

PRICE_NOTE = (
    "价格用腾讯前复权日线，分红送转不会被当成下跌；场内 ETF 同样走这条线。"
    "港股、美股、场外基金这一版不算：本机没有可比的日线序列。"
)


# ——— 取数 ———


def _kline(code: str, count: int) -> list[dict]:
    """腾讯前复权日线，缓存 30 分钟（盘中会变，不做日级缓存）。"""
    return cache_runtime.get(
        f"xray:kline:{code}:{count}",
        lambda: astock.tencent_kline(code, count=count),
        valid=lambda v: isinstance(v, list) and bool(v),
        ttl=1800,
        decorate=False,
    ) or []


def _full_history(code: str) -> list[dict]:
    """上市以来全部前复权日线，缓存 6 小时（回放要用到 2015 年）。"""
    return cache_runtime.get(
        f"xray:history:{code}",
        lambda: factor_data.fetch_stock_history(code),
        valid=lambda v: isinstance(v, list) and bool(v),
        ttl=6 * 3600,
        decorate=False,
    ) or []


def _series(rows: list[dict], field: str = "close") -> pd.Series:
    """[{date, close, ...}] → DatetimeIndex 的浮点序列（升序、丢非正值）。

    索引用 DatetimeIndex 而不是字符串：回放要按 Timestamp 切片，字符串索引和
    Timestamp 混用会在 searchsorted 里报「str 和 Timestamp 不能比」。
    """
    by: dict[pd.Timestamp, float] = {}
    for row in rows or []:
        date = str(row.get("date") or "")[:10]
        try:
            value = float(row.get(field))
            stamp = pd.Timestamp(date)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            by[stamp] = value
    if not by:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name="date"))
    return pd.Series(by, dtype="float64").sort_index()


def _day(value: Any) -> str:
    """日期写法统一成 YYYY-MM-DD（传进来的可能是 Timestamp、date 或字符串）。"""
    try:
        return str(pd.Timestamp(value).date())
    except (TypeError, ValueError):
        return str(value)


def _industry(code: str) -> str | None:
    """东财实时快照的行业字段（f127）；取不到返回 None（进缺口，不当成「其它」）。

    走 ``astock.em_get`` 而不是 akshare：它直连优先、与全站同一套限流，
    而且 akshare 自己建会话会读到系统代理（本机实测 push2 被代理掐断）。
    """
    def fetch() -> str | None:
        prefix = astock.get_prefix(code)
        if prefix == "bj":  # 北交所不在 f127 的行业口径里，宁可说没取到
            return None
        try:
            resp = astock.em_get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": f"{1 if prefix == 'sh' else 0}.{code}", "fields": "f57,f58,f127"},
                timeout=10,
            )
            data = resp.json().get("data") or {}
        except Exception:  # noqa: BLE001 — 行业只是补充维度，取不到不影响主指标
            return None
        value = str(data.get("f127") or "").strip()
        return value or None

    return cache_runtime.get(
        f"xray:industry:{code}", fetch, valid=lambda v: v is not None,
        ttl=24 * 3600, decorate=False,
    )


# ——— 通用小件 ———


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _r(value: Any, digits: int = 6) -> float | None:
    out = _finite(value)
    return round(out, digits) if out is not None else None


def _downside_deviation(returns: pd.Series) -> float | None:
    """只对负收益算标准差（下行波动）。没有负收益时返回 None，不当成 0。"""
    if returns.empty:
        return None
    downside = returns[returns < 0]
    if downside.empty:
        return None
    return float(np.sqrt((downside ** 2).mean()))


# ——— 体检 ———


def compute_xray(closes: dict[str, pd.Series], values: dict[str, float]) -> dict[str, Any]:
    """纯计算：市值加权组合的风险指标。取数在 ``build_xray`` 里。

    ``closes`` 每只一条日期索引的收盘序列；``values`` 是每只的当前市值（同一币种）。
    数据不够的、权重为 0 的进 ``skipped``，权重在幸存者之间重新归一，并写进 ``notes``。
    """
    gaps: list[dict[str, str]] = []
    notes: list[str] = []
    usable: dict[str, pd.Series] = {}
    for code, series in closes.items():
        if series is None or len(series) == 0:
            gaps.append({"code": code, "reason": "没有取到日线收盘价"})
            continue
        if len(series) < MIN_HISTORY_DAYS:
            gaps.append({"code": code, "reason": f"只有 {len(series)} 根收盘价（至少 {MIN_HISTORY_DAYS} 根）"})
            continue
        if values.get(code, 0) <= 0:
            gaps.append({"code": code, "reason": "没有市值（行情没取到），没进入权重"})
            continue
        usable[code] = series

    if not usable:
        return {"ok": False, "error": "没有一只持仓有足够的日线价格，算不了体检", "gaps": gaps}

    frame = pd.DataFrame(usable).dropna(axis=1, how="all")
    total_value = sum(values[code] for code in frame.columns)
    weights = np.array([values[code] / total_value for code in frame.columns], dtype=float)

    aligned = frame.dropna(axis=0, how="any")
    if len(aligned) < MIN_HISTORY_DAYS:
        notes.append(f"几只有共同价格的交易日只有 {len(aligned)} 天，相关性按这些天算")
    if len(aligned) < 2:
        return {"ok": False, "error": "共同交易日不足 2 天，算不了组合收益", "gaps": gaps}

    returns = aligned.pct_change(fill_method=None).dropna(how="any")
    if returns.empty:
        return {"ok": False, "error": "没有可用的收益观测", "gaps": gaps}

    port = pd.Series(returns.to_numpy(dtype=float) @ weights, index=returns.index)
    symbols = [str(c) for c in frame.columns]

    result: dict[str, Any] = {
        "ok": True,
        "inputs": {
            "symbols": symbols,
            "weights": {sym: _r(weights[i], 8) for i, sym in enumerate(symbols)},
            "values": {sym: _r(values[sym], 2) for sym in symbols},
            "total_value": _r(total_value, 2),
            "aligned_days": int(len(aligned)),
            "first_date": _day(aligned.index[0]),
            "last_date": _day(aligned.index[-1]),
            "window_days": XRAY_KLINE_COUNT,
        },
        "concentration": _concentration(weights),
        "volatility": _volatility(port),
        "drawdown": _drawdown(port),
        "tail_risk": _tail_risk(port),
        "diversification": _diversification(returns, weights, port),
        "correlation": _correlation(returns, weights, port),
        "risk_contribution": _risk_contribution(returns, weights),
        "asset_vol_annualized": {
            sym: _r(float(returns[sym].std(ddof=1)) * math.sqrt(PERIODS_PER_YEAR)) for sym in symbols
        },
        "gaps": gaps,
        "notes": notes,
    }
    return result


def _concentration(w: np.ndarray) -> dict[str, Any]:
    hhi = float(np.sum(w ** 2))
    order = np.argsort(w)[::-1]
    return {
        "hhi": _r(hhi),
        "effective_n": _r(1.0 / hhi) if hhi > 0 else None,
        "top1_weight": _r(float(w[order[0]])) if len(order) else None,
        "top3_weight": _r(float(w[order[:3]].sum())) if len(order) else None,
    }


def _volatility(port: pd.Series) -> dict[str, Any]:
    vol = float(port.std(ddof=1)) if len(port) > 1 else None
    downside = _downside_deviation(port)
    return {
        "daily_vol": _r(vol),
        "annualized_vol": _r(vol * math.sqrt(PERIODS_PER_YEAR)) if vol is not None else None,
        "downside_deviation_annualized": (
            _r(downside * math.sqrt(PERIODS_PER_YEAR)) if downside is not None else None
        ),
    }


def _drawdown(port: pd.Series) -> dict[str, Any]:
    if port.empty:
        return {"max_drawdown": None, "max_drawdown_start": None, "max_drawdown_trough": None}
    equity = (1.0 + port).cumprod()
    # 起点净值恒为 1：第一笔收益之前就已经是历史高点，否则首日下跌会被漏掉
    peak = equity.cummax().clip(lower=1.0)
    dd = (equity - peak) / peak
    trough = dd.idxmin()
    before_trough = equity.loc[:trough]
    start = port.index[0] if float(before_trough.max()) < 1.0 else before_trough.idxmax()
    return {
        "max_drawdown": _r(float(dd.loc[trough])),
        "max_drawdown_start": _day(start),
        "max_drawdown_trough": _day(trough),
    }


def _tail_risk(port: pd.Series) -> dict[str, Any]:
    """历史模拟法：直接取经验分布的 95 / 99 分位，不做参数假设。"""
    losses = -port.to_numpy(dtype=float)
    out: dict[str, Any] = {}
    for level in (0.95, 0.99):
        var = float(np.quantile(losses, level))
        tail = losses[losses >= var]
        key = str(int(round(level * 100)))
        out[f"var_{key}"] = _r(var)
        out[f"expected_shortfall_{key}"] = _r(float(tail.mean())) if len(tail) else None
    out["method"] = "历史模拟（经验分布分位，无参数假设）"
    return out


def _diversification(returns: pd.DataFrame, w: np.ndarray, port: pd.Series) -> dict[str, Any]:
    if returns.shape[1] < 2:
        return {"diversification_ratio": None, "note": "只有一只，谈不上分散"}
    asset_vols = returns.std(ddof=1).to_numpy(dtype=float)
    port_vol = float(port.std(ddof=1)) if len(port) > 1 else 0.0
    if port_vol <= 0 or not math.isfinite(port_vol):
        return {"diversification_ratio": None, "note": "组合波动为 0，比率没有意义"}
    return {"diversification_ratio": _r(float(np.dot(w, asset_vols) / port_vol))}


def _correlation(returns: pd.DataFrame, w: np.ndarray, port: pd.Series) -> dict[str, Any]:
    if returns.shape[1] < 2:
        return {"avg_pairwise_abs": None, "max_pair": None, "beta_to_equal_weight": None,
                "note": "只有一只，谈不上相关性"}
    corr = returns.corr().to_numpy(dtype=float)
    n = corr.shape[0]
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    avg = float(np.mean([abs(corr[i, j]) for i, j in pairs])) if pairs else None
    max_pair = None
    if pairs:
        i, j = max(pairs, key=lambda pair: abs(corr[pair[0], pair[1]]))
        max_pair = {"symbols": [str(returns.columns[i]), str(returns.columns[j])], "corr": _r(float(corr[i, j]))}
    market = returns.mean(axis=1)
    market_var = float(market.var(ddof=1)) if len(market) > 1 else 0.0
    beta = _r(float(port.cov(market) / market_var)) if market_var > 0 and math.isfinite(market_var) else None
    return {
        "avg_pairwise_abs": _r(avg),
        "max_pair": max_pair,
        "beta_to_equal_weight": beta,
        "correlation_matrix": {
            "symbols": [str(s) for s in returns.columns],
            "values": [[_r(float(v)) for v in row] for row in corr],
        },
    }


def _risk_contribution(returns: pd.DataFrame, w: np.ndarray) -> dict[str, Any]:
    """每只对组合方差的贡献：w_i·(Σw)_i / (wᵀΣw)。

    贡献占比大于权重 = 这只比它的钱更「吵」；小于权重 = 起了缓冲作用（长仓时不会为负）。
    """
    cov = returns.cov(ddof=1).to_numpy(dtype=float)
    port_var = float(w @ cov @ w)
    if not math.isfinite(port_var) or port_var <= 0:
        return {"shares": None, "note": "组合方差为 0，算不了风险贡献"}
    marginal = cov @ w
    return {
        "shares": {
            str(sym): _r(float(w[i] * marginal[i] / port_var))
            for i, sym in enumerate(returns.columns)
        },
    }


def _industry_exposure(codes: list[str], values: dict[str, float]) -> dict[str, Any]:
    """按行业汇总权重；认不出行业的单列一组，不并进「其它」当成同一类。"""
    buckets: dict[str, float] = {}
    unknown: list[str] = []
    total = sum(values.get(code, 0) for code in codes) or 1.0
    for code in codes:
        industry = _industry(code)
        if industry is None:
            unknown.append(code)
            continue
        buckets[industry] = buckets.get(industry, 0.0) + values.get(code, 0.0)
    rows = sorted(
        ({"industry": k, "weight": _r(v / total)} for k, v in buckets.items()),
        key=lambda row: -(row["weight"] or 0),
    )
    return {"rows": rows, "unknown": unknown}


# ——— 回放 ———


def _anchor(full: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> tuple[tuple[pd.Timestamp, float, pd.Timestamp, float] | None, str | None]:
    """一只在这段时期的起点价和终点价；取不到就给原因（进缺口）。

    起点取起点当天或之前最后一个收盘价（当天停牌就用停牌前的）。起点之前没有价格的
    **进缺口**，不拿后来的第一个价格往前补——补出来的那几天是编的「持平」，
    组合起点却仍算在起点，覆盖率也会把它算进来。
    """
    before = full.loc[:start]
    if before.empty:
        first = f"（最早的价格在 {_day(full.index[0])}）" if len(full) else ""
        return None, f"这段时期开始时还没有价格{first}，可能当时还没上市，没算进组合"
    last = full.loc[:end]
    if last.empty:
        return None, "这段时期结束时还没有价格"
    begin_date, begin_px = before.index[-1], float(before.iloc[-1])
    end_date, end_px = last.index[-1], float(last.iloc[-1])
    if end_date < end - pd.Timedelta(days=EDGE_DAYS):
        return None, f"最后一个价格在 {_day(end_date)}，到这段时期结束都没有新价格（可能停牌或退市），涨跌算不准"
    if begin_px <= 0 or end_px <= 0:
        return None, "价格不是正数，算不了涨跌"
    return (begin_date, begin_px, end_date, end_px), None


def _path_stats(path: pd.Series) -> dict[str, Any]:
    """一条从 1 起步的净值路径 → 期间最大回撤与最差单日。"""
    if path.empty:
        return {"max_drawdown": None, "max_drawdown_peak": None, "max_drawdown_trough": None,
                "worst_day": None, "worst_day_date": None}
    dd = path / path.cummax() - 1
    trough = dd.idxmin()
    peak = path.loc[:trough].idxmax()
    daily = path.pct_change().dropna()
    worst = daily.idxmin() if len(daily) else None
    return {
        "max_drawdown": _r(float(dd.min())),
        "max_drawdown_peak": _day(peak),
        "max_drawdown_trough": _day(trough),
        "worst_day": _r(float(daily.min())) if worst is not None else None,
        "worst_day_date": _day(worst) if worst is not None else None,
    }


def _replay_one(scenario: dict[str, str], positions: list[dict], series: dict[str, pd.Series],
                fetch_gaps: dict[str, str], bench: pd.Series | None, bench_error: str | None) -> tuple[dict, dict | None]:
    start, end = pd.Timestamp(scenario["start"]), pd.Timestamp(scenario["end"])
    total = sum(p["value"] for p in positions) or 1.0
    gaps: list[dict[str, str]] = []
    anchors: dict[str, tuple[pd.Timestamp, float, pd.Timestamp, float]] = {}
    for p in positions:
        code = p["code"]
        if code in fetch_gaps:
            gaps.append({"code": code, "name": p["name"], "reason": f"历史价格没取到：{fetch_gaps[code]}"})
            continue
        full = series.get(code)
        if full is None or full.empty:
            gaps.append({"code": code, "name": p["name"], "reason": "没有这只的历史日线"})
            continue
        anchor, why = _anchor(full, start, end)
        if anchor is None:
            gaps.append({"code": code, "name": p["name"], "reason": why or "算不了"})
            continue
        anchors[code] = anchor

    out: dict[str, Any] = {
        "id": scenario["id"], "label": scenario["label"],
        "start": scenario["start"], "end": scenario["end"],
        "benchmark": None, "benchmark_error": bench_error,
        "portfolio": None, "portfolio_note": None, "holdings": [], "gaps": gaps,
        "coverage": None, "positions_included": len(anchors),
    }

    path_bench: pd.Series | None = None
    if bench is not None:
        bench_anchor, why = _anchor(bench, start, end)
        if bench_anchor is None:
            out["benchmark_error"] = f"对照指数{why}"
        else:
            b_date, b_px, e_date, e_px = bench_anchor
            segment = bench.loc[b_date:end]
            path_bench = segment / b_px
            out["benchmark"] = {
                "return": _r(e_px / b_px - 1), "first_date": _day(b_date),
                "last_date": _day(e_date), **_path_stats(path_bench),
            }

    if not anchors:
        out["portfolio_note"] = "这段时期没有一只持仓有可用的价格"
        return out, None

    included_value = sum(p["value"] for p in positions if p["code"] in anchors)
    coverage = included_value / total
    out["coverage"] = _r(coverage, 4)
    weights = {p["code"]: p["value"] / included_value for p in positions if p["code"] in anchors}

    # 共同日历：对照指数与有价格的持仓在这段时期的交易日 ∪ {起点}；停牌那几天按停牌前收盘价计。
    # 带上指数的日历，几只同一天都停牌时那一天才不会从日历里消失。
    bench_days = list(bench.loc[start:end].index) if bench is not None else []
    days = sorted({start, *bench_days, *[d for code in anchors for d in series[code].loc[start:end].index]})
    cal = pd.DatetimeIndex(days)

    normalized: dict[str, pd.Series] = {}
    rows: list[dict[str, Any]] = []
    for p in positions:
        code = p["code"]
        if code not in anchors:
            continue
        begin_date, begin_px, end_date, end_px = anchors[code]
        full = series[code]
        own = set(full.loc[start:end].index)
        path = full.reindex(full.index.union(cal)).ffill().reindex(cal)
        path.iloc[0] = begin_px
        normalized[code] = path / begin_px
        halted = sum(1 for d in cal if d >= begin_date and d not in own)
        ret = end_px / begin_px - 1
        rows.append({
            "code": code, "name": p["name"], "weight": _r(weights[code]), "return": _r(ret),
            "contribution": _r(weights[code] * ret),
            "first_date": _day(begin_date), "last_date": _day(end_date),
            "halted_days": halted,
        })
    out["holdings"] = sorted(rows, key=lambda r: r["contribution"] or 0)

    path = sum(weights[code] * normalized[code] for code in normalized)
    if coverage < MIN_COVERAGE:
        out["portfolio_note"] = (
            f"这段时期有价格的持仓只占这一组市值的 {coverage:.0%}（不到一半），"
            "组合数字没有代表性，不给，只列各只的涨跌"
        )
        return out, {"dates": [_day(d) for d in cal], "portfolio": None,
                     "benchmark": _aligned(path_bench, cal)}

    ret = sum(weights[code] * (anchors[code][3] / anchors[code][1] - 1) for code in anchors)
    out["portfolio"] = {
        "return": _r(ret), "value_change": _r(included_value * ret, 2),
        "included_value": _r(included_value, 2),
        "first_date": _day(cal[0]), "last_date": _day(cal[-1]), "days": len(cal),
        **_path_stats(path),
    }
    if coverage < 1:
        out["portfolio_note"] = (
            f"权重只在这段时期有价格的 {len(anchors)} 只之间分配（占这一组市值的 {coverage:.0%}）"
        )
    return out, {
        "dates": [_day(d) for d in cal],
        "portfolio": [_r(float(x)) for x in path],
        "benchmark": _aligned(path_bench, cal),
    }


def _aligned(path_bench: pd.Series | None, cal: pd.DatetimeIndex) -> list[float | None] | None:
    if path_bench is None:
        return None
    aligned = path_bench.reindex(path_bench.index.union(cal)).ffill().reindex(cal)
    return [_r(float(x)) if pd.notna(x) else None for x in aligned]


def _benchmark(start: str, end: str) -> tuple[pd.Series | None, str | None]:
    try:
        rows = astock.index_daily_em(BENCHMARK["secid"], 3000)
    except Exception as exc:  # noqa: BLE001 — 对照指数取不到就只报组合，不整段失败
        return None, f"{BENCHMARK['label']}没取到：{exc}"
    series = _series(rows)
    if series.empty:
        return None, f"{BENCHMARK['label']}没取到"
    return series, None


# ——— 入口 ———


def _positions(user_id: int | None) -> list[dict]:
    """持仓 + 实时行情 → [{code, name, shares, value}]；取不到价格的不算市值（value=0）。"""
    holdings = pf._load(user_id).get("holdings", [])
    if not holdings:
        return []
    codes = [h["code"] for h in holdings]
    try:
        quotes = astock.tencent_quote(codes)
    except Exception:  # noqa: BLE001 — 行情取不到时回放仍可用（历史价格另取），体检则进缺口
        quotes = {}
    out = []
    for h in holdings:
        q = quotes.get(h["code"], {})
        price = _finite(q.get("price")) or 0.0
        out.append({
            "code": h["code"],
            "name": q.get("name") or h["code"],
            "shares": h.get("shares"),
            "value": price * float(h.get("shares") or 0),
        })
    return out


def build_xray(user_id: int | None = None, force: bool = False) -> dict[str, Any]:
    positions = _positions(user_id)
    if not positions:
        return {"ok": False, "error": "还没有持仓，先记一笔再体检", "gaps": []}

    values = {p["code"]: p["value"] for p in positions}
    no_price = [p for p in positions if p["value"] <= 0]
    closes: dict[str, pd.Series] = {}
    gaps: list[dict[str, str]] = [
        {"code": p["code"], "name": p["name"], "reason": "没有市价，没算进权重"} for p in no_price
    ]
    for p in positions:
        if p["value"] <= 0:
            continue
        series = _series(_kline(p["code"], XRAY_KLINE_COUNT))
        if series.empty:
            gaps.append({"code": p["code"], "name": p["name"], "reason": "没取到日线收盘价"})
            continue
        closes[p["code"]] = series

    names = {p["code"]: p["name"] for p in positions}
    report = compute_xray(closes, values)
    # 展示用：代码之外再给名称，界面上只写代码看不懂是哪只
    if report.get("ok"):
        report["inputs"]["names"] = {c: names.get(c, c) for c in report["inputs"]["symbols"]}
    # compute_xray 只看到能进权重的那些列，没市价 / 没日线的要在外面补齐
    merged = gaps + list(report.get("gaps", []))
    report["gaps"] = [{**g, "name": g.get("name") or names.get(g["code"], "")} for g in merged]
    report["as_of"] = _day(max((s.index[-1] for s in closes.values()), default="")) if closes else ""
    report["source"] = "腾讯前复权日线（收盘）"
    report["notes"] = [
        "按当前持仓市值加权，用的是滚动窗口内的历史收益，不含任何买卖判断",
        "只给数字，不出综合分；集中度、波动、回撤这些线自己设阈值看",
        *report.get("notes", []),
    ]
    if report.get("ok"):
        exposure = _industry_exposure([s for s in report["inputs"]["symbols"]], values)
        report["industry_exposure"] = exposure
        if exposure["unknown"]:
            unknown = "、".join(f"{c} {names.get(c, '')}".strip() for c in exposure["unknown"])
            report["notes"].append(f"行业没取到：{unknown}（不算进行业暴露，也没当成「其它」）")
    return report


def build_replay(user_id: int | None = None, force: bool = False) -> dict[str, Any]:
    positions = _positions(user_id)
    priced = [p for p in positions if p["value"] > 0]
    if not priced:
        return {"ok": False, "error": "还没有带市价的持仓，回放不了", "scenarios": []}

    lo = (min(pd.Timestamp(s["start"]) for s in SCENARIOS) - pd.Timedelta(days=LEAD_DAYS)).date().isoformat()
    series: dict[str, pd.Series] = {}
    fetch_gaps: dict[str, str] = {}
    for p in priced:
        rows = _full_history(p["code"])
        if not rows:
            fetch_gaps[p["code"]] = "没有取到历史日线"
            continue
        full = _series(rows)
        # 只要回放用得到的那一段，避免每次切片都扫全历史
        series[p["code"]] = full.loc[lo:]

    bench, bench_error = _benchmark(lo, SCENARIOS[-1]["end"])

    scenarios = []
    paths: dict[str, Any] = {}
    for scenario in SCENARIOS:
        one, path = _replay_one(scenario, priced, series, fetch_gaps, bench, bench_error)
        scenarios.append(one)
        if path:
            paths[scenario["id"]] = path

    return {
        "ok": True,
        "total_value": _r(sum(p["value"] for p in priced), 2),
        "positions_input": len(priced),
        "benchmark_label": BENCHMARK["label"],
        "scenarios": scenarios,
        "paths": paths,
        "notes": [
            "按现在的持仓比例回看这几段历史行情，不是你当时的真实持仓，也不预测下一次会怎样",
            f"时期的起止取{BENCHMARK['label']}这段时间收盘价的最高点和最低点",
            PRICE_NOTE,
            f"有价格的持仓不到这一组市值的 {MIN_COVERAGE:.0%} 时不给组合数字，只列各只涨跌",
        ],
        "min_coverage": MIN_COVERAGE,
    }
