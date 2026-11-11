"""个股事件回看与财报前瞻（A 股个股）。

两件事都只描述过去，不预测下一次、不给买卖提示：

- ``build_event_study``：选一类事件，把这只股票历史上每一次排出来，看事件前 5 个交易日到后 20 个交易日、
  相对沪深300的累计超额收益（每个交易日这只股票的涨跌减去指数当天的涨跌，再累加），
  按「事件前 5 日 / 第 0–1 日 / 第 0–5 日 / 第 0–20 日」四档给平均、中位数和上涨占比。
- ``build_earnings_preview``：下一份定期报告的预约披露日、机构一致预期（本财年与下一财年）、
  过去几次发布后第一个交易日的涨跌（含大盘）、以及最近一次实际净利润落在业绩预告区间的哪一边。

边界（沿用上游 Vibe-Research 的口径）：
- 事件少于 8 次只列逐次结果，不给平均值——几次的平均说明不了什么。
- 窗口里有停牌的照列，但不进平均（复牌那天的涨跌会被算进窗口）。
- 最近的还没满 20 个交易日的，只进已经有的那几档。
- 同一天公告的几条算一次；两次相隔太近、窗口重叠的标出来。
- 涨跌用前复权日线，沪深300是价格指数、不含分红。
- 没有日期的记录数出来写进缺口，不当成 0。

数据：事件明细走东财数据中心（业绩预告 / 股东增减持 / 龙虎榜 / 限售解禁 / 机构调研），
价格走腾讯前复权日线（复用因子数据层的分页取数），基准走东财沪深300指数日线。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone, timedelta
from typing import Any

import pandas as pd

import astock
import cache_runtime
import factor_data
import holder_increase

BEIJING = timezone(timedelta(hours=8))
BENCHMARK = {"secid": "1.000300", "label": "沪深300指数"}
#: 窗口：事件前 PRE 个交易日到后 POST 个交易日（第 0 天是事件日）
PRE = 5
POST = 20
#: 少于这么多次就不给平均值
MIN_EVENTS = 8
HORIZONS: tuple[tuple[str, int, int, str], ...] = (
    ("pre", -PRE, -1, "事件前 5 日"),
    ("d0_1", 0, 1, "第 0–1 日"),
    ("d0_5", 0, 5, "第 0–5 日"),
    ("d0_20", 0, POST, "第 0–20 日"),
)
#: 取价格时在最早的事件前多取这么多天（要凑够事件前 6 个交易日，长假也够）
LEAD_DAYS = 30
#: 事件明细翻页上限（每页 500 条）
MAX_PAGES = 8

FORECAST_UP = ("预增", "略增", "扭亏", "减亏")
FORECAST_DOWN = ("预减", "略减", "首亏", "增亏")
NET_PROFIT = "归属于上市公司股东的净利润"
PERIOD_NAMES = {"03-31": "一季报", "06-30": "中报", "09-30": "三季报", "12-31": "年报"}
#: 限售解禁按占流通盘比例分两组的线（%）
LOCKUP_LARGE_PCT = 1.0

KINDS: dict[str, dict[str, str]] = {
    "earnings_forecast": {"label": "业绩预告", "report": "RPT_PUBLIC_OP_NEWPREDICT", "date_field": "NOTICE_DATE"},
    "holder_increase": {"label": "重要股东增持", "report": "RPT_SHARE_HOLDER_INCREASE", "date_field": "NOTICE_DATE"},
    "holder_decrease": {"label": "重要股东减持", "report": "RPT_SHARE_HOLDER_INCREASE", "date_field": "NOTICE_DATE"},
    "dragon_tiger": {"label": "龙虎榜上榜", "report": "RPT_DAILYBILLBOARD_DETAILSNEW", "date_field": "TRADE_DATE"},
    "lockup": {"label": "限售解禁", "report": "RPT_LIFT_STAGE", "date_field": "FREE_DATE"},
    "survey": {"label": "机构调研", "report": "RPT_ORG_SURVEYNEW", "date_field": "NOTICE_DATE"},
}


# ——— 小件 ———


def _r(value: Any, digits: int = 6) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return round(out, digits) if math.isfinite(out) else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _day(value: Any) -> str | None:
    """东财给的日期是 '2024-07-13 00:00:00'；取不到合法日期返回 None。"""
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _today() -> date:
    return datetime.now(BEIJING).date()


def _period_label(report_date: str | None) -> str:
    if not report_date:
        return "业绩预告"
    return f"{report_date[:4]} 年{PERIOD_NAMES.get(report_date[5:], '定期报告')}"


# ——— 取数 ———


def _series(rows: list[dict], field: str = "close") -> pd.Series:
    by: dict[pd.Timestamp, float] = {}
    for row in rows or []:
        try:
            stamp = pd.Timestamp(str(row.get("date") or "")[:10])
            value = float(row.get(field))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            by[stamp] = value
    if not by:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([]))
    return pd.Series(by, dtype="float64").sort_index()


def _klines(code: str) -> pd.Series:
    """上市以来全部前复权日线，缓存 6 小时（事件回看要往前翻很多年）。"""
    rows = cache_runtime.get(
        f"events:kline:{code}",
        lambda: factor_data.fetch_stock_history(code),
        valid=lambda v: isinstance(v, list) and bool(v),
        ttl=6 * 3600, decorate=False,
    ) or []
    return _series(rows)


def _benchmark() -> pd.Series:
    rows = cache_runtime.get(
        f"events:benchmark:{BENCHMARK['secid']}",
        lambda: astock.index_daily_em(BENCHMARK["secid"], 3000),
        valid=lambda v: isinstance(v, list) and bool(v),
        ttl=6 * 3600, decorate=False,
    ) or []
    return _series(rows)


def _event_rows(code: str, kind: str) -> list[dict]:
    """东财数据中心的事件明细（整份历史，翻页到取完）。"""
    spec = KINDS[kind]
    filters = [f'(SECURITY_CODE="{code}")']
    if kind in ("holder_increase", "holder_decrease"):
        filters.append('(DIRECTION="增持")' if kind == "holder_increase" else '(DIRECTION="减持")')

    rows = cache_runtime.get(
        f"events:{kind}:{code}",
        lambda: holder_increase._fetch_pages(
            spec["report"], "".join(filters), sort_columns=spec["date_field"], max_pages=MAX_PAGES),
        valid=lambda v: isinstance(v, list),
        ttl=6 * 3600, decorate=False,
    )
    return rows or []


# ——— 事件归一化 ———


def _by_day(rows: list[dict], field: str) -> tuple[dict[str, list[dict]], int]:
    grouped: dict[str, list[dict]] = {}
    bad = 0
    for row in rows:
        day = _day(row.get(field))
        if day is None:
            bad += 1
            continue
        grouped.setdefault(day, []).append(row)
    return grouped, bad


def _range_text(low: Any, high: Any, unit: str = "") -> str:
    lo, hi = _num(low), _num(high)
    if lo is None and hi is None:
        return ""
    if lo is None or hi is None or lo == hi:
        return f"{(lo if hi is None else hi):,.2f}{unit}"
    return f"{lo:,.2f}–{hi:,.2f}{unit}"


def _amount_text(low: Any, high: Any) -> str:
    """金额字段是元：量大就换成亿元 / 万元，不把 690,000,000.00 元原样抬上去。"""
    lo, hi = _num(low), _num(high)
    magnitudes = [abs(v) for v in (lo, hi) if isinstance(v, float)]
    if not magnitudes:
        return ""
    scale, unit = (1e8, " 亿元") if max(magnitudes) >= 1e8 else (1e4, " 万元")
    if lo is None or hi is None or lo == hi:
        return f"{(lo if hi is None else hi) / scale:,.2f}{unit}"
    return f"{lo / scale:,.2f}–{hi / scale:,.2f}{unit}"


def _forecast_events(rows: list[dict]) -> tuple[list[dict], int]:
    """业绩预告 → 每个公告日一次事件。"""
    grouped, bad = _by_day(rows, "NOTICE_DATE")
    out = []
    for day, items in grouped.items():
        net = [row for row in items if str(row.get("PREDICT_FINANCE") or "") == NET_PROFIT]
        main = net or items
        types = sorted({str(row.get("PREDICT_TYPE") or "").strip() for row in main} - {""})
        directions = {"up" if t in FORECAST_UP else "down" if t in FORECAST_DOWN else "other" for t in types}
        period = _day(main[0].get("REPORT_DATE"))
        detail = "、".join(types) or "类型没写"
        finance = str(main[0].get("PREDICT_FINANCE") or "")
        if finance.startswith("每股"):
            amount = _range_text(main[0].get("PREDICT_AMT_LOWER"), main[0].get("PREDICT_AMT_UPPER"), " 元")
        else:
            amount = _amount_text(main[0].get("PREDICT_AMT_LOWER"), main[0].get("PREDICT_AMT_UPPER"))
        if amount:
            short = "归母净利润" if finance == NET_PROFIT else finance.replace("扣除非经常性损益后的净利润", "扣非净利润")
            detail += f"，{short} {amount}"
        out.append({
            "date": day, "label": f"{_period_label(period)}预告", "detail": detail,
            "direction": directions.pop() if len(directions) == 1 else "other",
        })
    return out, bad


def _holder_events(rows: list[dict], kind: str) -> tuple[list[dict], int]:
    """股东增减持 → 每个公告日一次事件（同一天公告的几位股东合并）。"""
    grouped, bad = _by_day(rows, "NOTICE_DATE")
    label = KINDS[kind]["label"]
    out = []
    for day, items in grouped.items():
        holders = list(dict.fromkeys(str(row.get("HOLDER_NAME") or "").strip() for row in items if row.get("HOLDER_NAME")))
        who = holders[0][:24] if len(holders) == 1 else f"{len(holders) or len(items)} 位股东"
        shares = [_num(row.get("CHANGE_NUM")) for row in items]
        parts = [who]
        if shares and all(v is not None for v in shares):
            parts.append(f"合计 {abs(sum(shares)):,.2f} 万股")
        rates = sorted({str(row.get("CHANGE_RATE") or "").strip() for row in items} - {""})
        if rates:
            parts.append("占比 " + "、".join(rates[:3]) + "%")
        out.append({"date": day, "label": label, "detail": "，".join(parts), "direction": None})
    return out, bad


def _dragon_events(rows: list[dict]) -> tuple[list[dict], int]:
    grouped, bad = _by_day(rows, "TRADE_DATE")
    out = []
    for day, items in grouped.items():
        parts = []
        for row in items[:3]:
            reason = str(row.get("EXPLAIN") or "").strip()[:40]
            amount = _num(row.get("BILLBOARD_DEAL_AMT"))
            if amount is not None:
                reason += f"，成交 {amount / 1e8:,.2f} 亿元" if reason else f"成交 {amount / 1e8:,.2f} 亿元"
            if reason:
                parts.append(reason)
        out.append({"date": day, "label": "龙虎榜上榜", "detail": "；".join(parts) or "上榜", "direction": None})
    return out, bad


def _lockup_events(rows: list[dict]) -> tuple[list[dict], int, list[dict]]:
    """限售解禁 → 每个解禁日一次事件；还没到的单独列出来（只列，不算涨跌）。"""
    today = _today()
    grouped, bad = _by_day(rows, "FREE_DATE")
    past: list[dict] = []
    upcoming: list[dict] = []
    for day, items in grouped.items():
        shares = [_num(row.get("CURRENT_FREE_SHARES")) for row in items]
        # FREE_RATIO 是占总股本的小数（0.0045 = 0.45%），不是百分数
        ratios = [_num(row.get("FREE_RATIO")) for row in items]
        kinds = sorted({str(row.get("FREE_SHARES_TYPE") or "").strip() for row in items} - {""})
        parts = []
        if shares and all(v is not None for v in shares):
            parts.append(f"解禁 {abs(sum(shares)):,.2f} 万股")
        ratio_pct = None
        if ratios and all(v is not None for v in ratios):
            ratio_pct = abs(sum(ratios)) * 100
            parts.append(f"占总股本 {ratio_pct:.2f}%")
        if kinds:
            parts.append("、".join(kinds[:2]))
        event = {
            "date": day, "label": "限售解禁", "detail": "，".join(parts),
            "direction": "other" if ratio_pct is None else ("large" if ratio_pct >= LOCKUP_LARGE_PCT else "small"),
        }
        if date.fromisoformat(day) > today:
            upcoming.append({"date": day, "label": "限售解禁", "detail": "，".join(parts)})
        else:
            past.append(event)
    upcoming.sort(key=lambda e: e["date"])
    return past, bad, upcoming


def _survey_events(rows: list[dict]) -> tuple[list[dict], int]:
    """机构调研 → 按公告日算（接待日通常早几天，那几天外界还不知道）。"""
    grouped, bad = _by_day(rows, "NOTICE_DATE")
    out = []
    for day, items in grouped.items():
        row = items[0]
        seen = _day(row.get("RECEIVE_START_DATE"))
        who = str(row.get("RECEIVE_OBJECT") or "").strip()[:24]
        parts = [f"接待日 {seen}" if seen else "", who, f"当天 {len(items)} 条记录" if len(items) > 1 else ""]
        out.append({"date": day, "label": "机构调研", "detail": "，".join(p for p in parts if p) or "调研",
                    "direction": None})
    return out, bad


def _normalize(kind: str, rows: list[dict]) -> tuple[list[dict], int, list[dict]]:
    if kind == "earnings_forecast":
        events, bad = _forecast_events(rows)
        return events, bad, []
    if kind in ("holder_increase", "holder_decrease"):
        events, bad = _holder_events(rows, kind)
        return events, bad, []
    if kind == "dragon_tiger":
        events, bad = _dragon_events(rows)
        return events, bad, []
    if kind == "lockup":
        events, bad, upcoming = _lockup_events(rows)
        return events, bad, upcoming
    events, bad = _survey_events(rows)
    return events, bad, []


# ——— 超额收益 ———


def _window(day0: pd.Timestamp, cal: pd.DatetimeIndex, stock: pd.Series,
            index: pd.Series) -> tuple[dict[str, Any] | None, str | None]:
    """一次事件的逐日超额收益。``cal`` 是对照指数的交易日。算不了就给原因（进缺口）。"""
    k = int(cal.searchsorted(day0))          # 第 0 天：事件日当天或之后的第一个交易日
    if k >= len(cal):
        return None, "事件之后还没有交易日"
    base = k - PRE - 1
    if base < 0:
        return None, f"对照指数在事件前不够 {PRE + 1} 个交易日"
    days = cal[base:min(k + POST, len(cal) - 1) + 1]
    if stock.loc[:days[0]].empty:
        return None, "事件前第 6 个交易日还没有价格，可能当时还没上市"
    aligned = stock.reindex(stock.index.union(days)).ffill().reindex(days)
    own = set(stock.index)
    halted = sum(1 for d in days if d not in own)
    abnormal = (aligned.pct_change() - index.reindex(days).pct_change()).iloc[1:]
    offsets = list(range(-PRE, -PRE + len(abnormal)))
    excess = dict(zip(offsets, abnormal.tolist()))
    cumulative = dict(zip(offsets, abnormal.cumsum().tolist()))

    def span(start: int, end: int) -> float | None:
        return _r(sum(excess[o] for o in range(start, end + 1))) if end in excess else None

    return {
        "day0": cal[k].date().isoformat(),
        "halted_days": halted,
        "days_after": len(abnormal) - PRE - 1,
        **{key: span(a, b) for key, a, b, _ in HORIZONS},
        "path": [_r(cumulative[o]) for o in offsets],
    }, None


def _stats(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": len(values),
        "mean": _r(sum(values) / len(values)),
        "median": _r(median),
        "positive_share": _r(sum(1 for v in values if v > 0) / len(values), 4),
    }


def _summary(events: list[dict[str, Any]], groups: dict[str, str]) -> list[dict[str, Any]]:
    """每组每档：停牌的不算、没满那一档的不算；少于 MIN_EVENTS 次不给平均。"""
    out = []
    for key, label in groups.items():
        members = [e for e in events if key == "all" or e.get("direction") == key]
        if key != "all" and not members:
            continue
        clean = [e for e in members if e["halted_days"] == 0]
        horizons = {}
        for hkey, _, _, _ in HORIZONS:
            values = [e[hkey] for e in clean if e[hkey] is not None]
            horizons[hkey] = (_stats(values) if len(values) >= MIN_EVENTS
                              else {"n": len(values), "mean": None, "median": None, "positive_share": None})
        most = max((h["n"] for h in horizons.values()), default=0)
        note = None
        if most < MIN_EVENTS:
            note = f"能算的只有 {most} 次（不到 {MIN_EVENTS} 次），不给平均，只看逐次结果"
        elif len(clean) < len(members):
            note = f"窗口里有停牌的 {len(members) - len(clean)} 次没算进来"
        out.append({"key": key, "label": label, "events": len(members), "horizons": horizons, "note": note})
    return out


def _mean_path(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """窗口完整、没停牌的事件的平均 / 中位累计超额收益路径。"""
    full = [e["path"] for e in events if e["halted_days"] == 0 and len(e["path"]) == PRE + 1 + POST]
    if len(full) < MIN_EVENTS:
        return None
    columns = list(zip(*full))
    medians = []
    for column in columns:
        ordered = sorted(column)
        mid = len(ordered) // 2
        medians.append(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2)
    return {
        "offsets": list(range(-PRE, POST + 1)),
        "n": len(full),
        "mean": [_r(sum(c) / len(c)) for c in columns],
        "median": [_r(m) for m in medians],
    }


def _groups_for(kind: str) -> dict[str, str]:
    base = {"all": "全部"}
    if kind == "earnings_forecast":
        base["up"] = f"预告向好（{'、'.join(FORECAST_UP)}）"
        base["down"] = f"预告向差（{'、'.join(FORECAST_DOWN)}）"
    elif kind == "lockup":
        base["large"] = f"较大解禁（占总股本 ≥ {LOCKUP_LARGE_PCT:g}%）"
        base["small"] = f"较小解禁（不到 {LOCKUP_LARGE_PCT:g}%）"
    return base


def _aligned_calendar(stock: pd.Series, bench: pd.Series) -> pd.DatetimeIndex:
    """交易日历截到这只股票最后一个有价格的日子。

    不截的话，今天指数已有这根 K、个股收盘后才更新，今天会被当成停牌，
    超额收益变成「−指数涨跌」。
    """
    cal = pd.DatetimeIndex(bench.index)
    if len(stock) and (cal > stock.index[-1]).any():
        cal = cal[cal <= stock.index[-1]]
    return cal


def build_event_study(code: str, kind: str) -> dict[str, Any]:
    if kind not in KINDS:
        return {"ok": False, "error": f"事件类型只认 {'、'.join(KINDS)}"}
    if not (len(code) == 6 and code.isdigit()) or astock.get_prefix(code) == "bj":
        return {"ok": False, "error": "事件回看只做沪深个股；北交所日线取不到"}

    spec = KINDS[kind]
    rows = _event_rows(code, kind)
    raw_events, bad, upcoming = _normalize(kind, rows)
    raw_events.sort(key=lambda e: e["date"])

    result: dict[str, Any] = {
        "ok": True, "code": code, "kind": kind, "kind_label": spec["label"],
        "benchmark_label": BENCHMARK["label"],
        "window": {"pre": PRE, "post": POST}, "min_events": MIN_EVENTS,
        "horizons": [{"key": k, "from": a, "to": b, "label": lab} for k, a, b, lab in HORIZONS],
        "groups": _groups_for(kind), "events": [], "gaps": [], "summary": [], "notes": [],
        "sources": ["东方财富数据中心 · " + spec["label"], "腾讯前复权日线", "东方财富·沪深300指数日线"],
    }
    if kind == "lockup":
        result["upcoming"] = upcoming
    if bad:
        result["gaps"].append({"date": None, "label": spec["label"],
                               "reason": f"有 {bad} 条没有日期，没算进来"})

    if not raw_events:
        result["notes"] = [
            f"这只股票没查到{spec['label']}" + ("（只算有日期的）" if bad else "")
            + ("；只有已经排期、还没到的" if upcoming else "")
        ]
        return result

    stock = _klines(code)
    bench = _benchmark()
    if stock.empty:
        return {"ok": False, "error": "这只股票的日线没取到"}
    if bench.empty:
        return {"ok": False, "error": f"{BENCHMARK['label']}没取到，算不了超额收益"}
    cal = _aligned_calendar(stock, bench)

    computed: dict[str, dict[str, Any]] = {}     # 按第 0 天合并：同一天落在同一交易日的算一次
    for event in raw_events:
        window, why = _window(pd.Timestamp(event["date"]), cal, stock, bench)
        if window is None:
            result["gaps"].append({"date": event["date"], "label": event["label"], "reason": why})
            continue
        previous = computed.get(window["day0"])
        if previous:
            previous["label"] = f"{previous['label']}；{event['label']}"
            previous["detail"] = f"{previous['detail']}；{event['detail']}"
            if previous["direction"] != event["direction"]:
                previous["direction"] = "other"
            continue
        computed[window["day0"]] = {**event, **window}

    events = sorted(computed.values(), key=lambda e: e["day0"])
    # 两次事件的窗口（前 5 到后 20 个交易日）共用交易日时标出来：同一段行情会被算进两次
    for previous, current in zip(events, events[1:]):
        gap = int(cal.searchsorted(pd.Timestamp(current["day0"]))) - int(cal.searchsorted(pd.Timestamp(previous["day0"])))
        if gap <= PRE + POST:
            current["overlaps_previous"] = True

    result["events"] = events
    result["summary"] = _summary(events, result["groups"])
    path = _mean_path(events)
    if path:
        result["mean_path"] = path
    if len(events) != len(raw_events):
        overlap = sum(1 for e in events if e.get("overlaps_previous"))
        if overlap:
            result["gaps"].append({"date": None, "label": spec["label"],
                                   "reason": f"有 {overlap} 次与上一次相隔不到 25 个交易日，窗口重叠，同一段行情会被算进两次"})
    result["notes"] = [
        f"窗口是事件前 {PRE} 个交易日到后 {POST} 个交易日，超额收益 = 个股当日涨跌 − {BENCHMARK['label']}当日涨跌，再累加",
        f"少于 {MIN_EVENTS} 次不给平均；窗口里有停牌的不进平均",
        "只描述这只股票过去的情况，不预测这一次，也不构成买卖提示",
    ]
    return result


# ——— 财报前瞻 ———


def _appointments(code: str) -> list[dict]:
    rows = cache_runtime.get(
        f"events:appoint:{code}",
        lambda: holder_increase._fetch_pages(
            "RPT_PUBLIC_BS_APPOIN", f'(SECURITY_CODE="{code}")',
            sort_columns="REPORT_DATE", max_pages=2),
        valid=lambda v: isinstance(v, list),
        ttl=6 * 3600, decorate=False,
    )
    return rows or []


def _reports(code: str) -> list[dict]:
    rows = cache_runtime.get(
        f"events:reports:{code}",
        lambda: holder_increase._fetch_pages(
            "RPT_LICO_FN_CPD", f'(SECURITY_CODE="{code}")',
            sort_columns="REPORTDATE", max_pages=2),
        valid=lambda v: isinstance(v, list),
        ttl=6 * 3600, decorate=False,
    )
    return rows or []


def _consensus(code: str) -> list[dict]:
    rows = cache_runtime.get(
        f"events:consensus:{code}",
        lambda: astock.profit_forecast(code),
        valid=lambda v: isinstance(v, list),
        ttl=6 * 3600, decorate=False,
    )
    return rows or []


def build_earnings_preview(code: str) -> dict[str, Any]:
    if not (len(code) == 6 and code.isdigit()) or astock.get_prefix(code) == "bj":
        return {"ok": False, "error": "财报前瞻只做沪深个股；北交所日线取不到"}

    today = _today()
    result: dict[str, Any] = {
        "ok": True, "code": code, "gaps": [], "notes": [],
        "sources": ["东方财富·预约披露时间", "东方财富·业绩报表", "东方财富·盈利预测（机构一致预期）",
                    "腾讯前复权日线", "东方财富·沪深300指数日线"],
    }

    appointments = _appointments(code)
    upcoming: list[dict] = []
    published: list[dict] = []
    for row in appointments:
        period = _day(row.get("REPORT_DATE"))
        actual = _day(row.get("ACTUAL_PUBLISH_DATE"))
        appointed = _day(row.get("APPOINT_PUBLISH_DATE"))
        if actual:
            published.append({"period": period, "date": actual})
        elif appointed and date.fromisoformat(appointed) >= today:
            changes = [d for d in (_day(row.get(f)) for f in ("FIRST_CHANGE_DATE", "SECOND_CHANGE_DATE", "THIRD_CHANGE_DATE")) if d]
            upcoming.append({
                "period": period, "period_label": _period_label(period),
                "date": appointed, "first_appointed": _day(row.get("FIRST_APPOINT_DATE")),
                "rescheduled": changes, "days_left": (date.fromisoformat(appointed) - today).days,
            })
    upcoming.sort(key=lambda item: item["date"])
    result["next_report"] = upcoming[0] if upcoming else None
    if not appointments:
        result["gaps"].append({"date": None, "label": "预约披露", "reason": "没查到预约披露时间"})
    elif not upcoming:
        result["gaps"].append({"date": None, "label": "预约披露",
                               "reason": "交易所还没公布下一期的预约披露日，这里只有已披露的历史"})

    consensus = []
    for row in _consensus(code):
        consensus.append({
            "year": str(row.get("年度") or ""),
            "analysts": row.get("预测机构数"),
            "low": _num(row.get("最小值")),
            "mean": _num(row.get("均值")),
            "high": _num(row.get("最大值")),
            "unit": "元/股（每股收益）",
        })
    result["consensus"] = consensus
    if not consensus:
        result["gaps"].append({"date": None, "label": "一致预期", "reason": "没有机构盈利预测"})

    # 业绩报表按报告期索引：实际 EPS / 营收 / 归母净利润
    reports = {}
    for row in _reports(code):
        period = _day(row.get("REPORTDATE"))
        if period:
            reports[period] = row

    stock = _klines(code)
    bench = _benchmark()
    history: list[dict] = []
    if published and (stock.empty or bench.empty):
        result["gaps"].append({"date": None, "label": "发布后涨跌", "reason": "日线没取到，算不了发布后第一个交易日的涨跌"})
    else:
        cal = _aligned_calendar(stock, bench)
        stock_pct = stock.pct_change()
        bench_pct = bench.pct_change()
        for item in sorted(published, key=lambda i: i["date"], reverse=True)[:8]:
            entry: dict[str, Any] = {
                "period": item["period"], "period_label": _period_label(item["period"]),
                "publish_date": item["date"],
            }
            row = reports.get(item["period"] or "")
            if row:
                entry["eps"] = _num(row.get("BASIC_EPS"))
                entry["net_profit_yi"] = (
                    _r(_num(row.get("PARENT_NETPROFIT")) / 1e8, 4)
                    if _num(row.get("PARENT_NETPROFIT")) is not None else None
                )
                entry["revenue_yi"] = (
                    _r(_num(row.get("TOTAL_OPERATE_INCOME")) / 1e8, 4)
                    if _num(row.get("TOTAL_OPERATE_INCOME")) is not None else None
                )
            else:
                entry["eps"] = entry["net_profit_yi"] = entry["revenue_yi"] = None
                result["gaps"].append({"date": item["date"], "label": _period_label(item["period"]),
                                       "reason": "业绩报表里没有这一期，实际值没取到"})
            # 定期报告的正式披露日按盘前口径：反应日 = 当天或之后第一个交易日
            k = int(cal.searchsorted(pd.Timestamp(item["date"])))
            if 0 < k < len(cal):
                day0 = cal[k]
                entry["reaction_day"] = day0.date().isoformat()
                entry["reaction"] = _r(float(stock_pct.get(day0, float("nan"))))
                entry["market"] = _r(float(bench_pct.get(day0, float("nan"))))
                if entry["reaction"] is not None and entry["market"] is not None:
                    entry["excess"] = _r(entry["reaction"] - entry["market"])
            else:
                entry["reaction_day"] = None
                entry["reaction"] = entry["market"] = entry["excess"] = None
                if k >= len(cal):
                    entry["pending"] = True
            history.append(entry)
    # 同一天发布几份（年报和一季报常常同一天）→ 反应日只有一个，合并成一条，不重复计数
    merged: dict[str, dict[str, Any]] = {}
    for entry in history:
        key = entry.get("reaction_day") or f"{entry['period']}-{entry['publish_date']}"
        previous = merged.get(key)
        if previous is None:
            merged[key] = entry
            continue
        previous["period_label"] = f"{previous['period_label']}、{entry['period_label']}"
        previous["same_day_releases"] = (previous.get("same_day_releases") or 1) + 1
    result["history"] = list(merged.values())

    # 最近一期实际净利润落在业绩预告区间的哪一边
    surprise = None
    latest = next((h for h in history if h.get("net_profit_yi") is not None), None)
    if latest:
        forecasts = _event_rows(code, "earnings_forecast")
        band = [
            row for row in forecasts
            if _day(row.get("REPORT_DATE")) == latest["period"]
            and str(row.get("PREDICT_FINANCE") or "") == NET_PROFIT
        ]
        if band:
            low = _num(band[0].get("PREDICT_AMT_LOWER"))
            high = _num(band[0].get("PREDICT_AMT_UPPER"))
            actual = latest["net_profit_yi"] * 1e8 if latest["net_profit_yi"] is not None else None
            if low is not None and high is not None and actual is not None:
                position = "低于区间下限" if actual < low else "高于区间上限" if actual > high else "落在区间内"
                surprise = {
                    "period_label": latest["period_label"],
                    "forecast_low_yi": _r(low / 1e8, 4), "forecast_high_yi": _r(high / 1e8, 4),
                    "actual_yi": latest["net_profit_yi"], "position": position,
                    "forecast_type": str(band[0].get("PREDICT_TYPE") or ""),
                }
        if surprise is None:
            result["gaps"].append({"date": None, "label": "预告与实际对照",
                                   "reason": "最近一期没有归母净利润的业绩预告区间，没做对照"})
    result["surprise"] = surprise

    result["notes"] = [
        "发布后涨跌 = 反应日收盘 ÷ 前一交易日收盘 − 1，含当天大盘涨跌；excess 另减了沪深300当日涨跌",
        "一致预期取机构最新盈利预测的均值与区间，不是公司指引",
        "只描述过去与已排期的事，不预测下一次，不构成买卖提示",
    ]
    return result


def kinds_catalog() -> list[dict[str, str]]:
    return [{"key": key, "label": spec["label"]} for key, spec in KINDS.items()]
