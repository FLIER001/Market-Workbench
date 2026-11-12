"""规则核对候选池：在一个范围里按用户设的条件逐只核对，只排除、不排名。

和「标的筛选」页的自然语言筛选不同，这里没有 AI：同一组条件、同一份行情，跑两次结果一样。

范围：
- 自选 / 持仓（由前端把代码传进来）；
- 中证指数官网的成分股（沪深300 / 中证500 / 中证1000 / 中证A500 / 上证50 / 科创50），
  成分取 akshare 转接的中证官网文件，缓存 24 小时，并显示文件日期。

条件（都按区间给 min / max，可只给一边）：
市盈率 TTM、市净率、总市值（亿）、当日换手率（%），外加两条开关：
排除 ST / *ST / 退市整理、排除当日没有成交（停牌）。

边界：
- 每只、每条都给出「满足 / 不满足 / 缺数据」和实际值；来源没给的值算缺数据，不当成 0。
- 亏损公司的市盈率是负数，照实显示，不参与「>= 0」这类条件时按原值比。
- 分「全部满足 / 有缺数据 / 有不满足」三组，各组按代码排序，不排名、不打分。
- 开盘前几乎所有股票都还没有成交，「停牌」这一条记为缺数据。
- 基金、债券、港股、美股不在这个范围内，传进来会进 gaps。
- 没有「当日成交额」这一条：腾讯行情快照不带成交额字段（只有换手率），
  为此单开一次逐只取数不划算，等有批量成交额来源再加。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import astock
import cache_runtime

#: 一次最多核对这么多只，避免整页拖死
MAX_CODES = 2000
#: 腾讯行情接口一次取这么多只，分批并发
QUOTE_BATCH = 120
QUOTE_WORKERS = 6
CONSTITUENT_TTL = 24 * 3600

#: 可选范围（前端按这个列表出选项）
UNIVERSES: dict[str, dict[str, str]] = {
    "hs300": {"label": "沪深300", "symbol": "000300"},
    "csi500": {"label": "中证500", "symbol": "000905"},
    "csi1000": {"label": "中证1000", "symbol": "000852"},
    "csi_a500": {"label": "中证A500", "symbol": "000510"},
    "sse50": {"label": "上证50", "symbol": "000016"},
    "star50": {"label": "科创50", "symbol": "000688"},
}

#: 数值条件：键 → (中文名, 单位, 取值字段)
NUMERIC_FIELDS: dict[str, tuple[str, str, str]] = {
    "pe_ttm": ("市盈率 TTM", "倍", "pe_ttm"),
    "pb": ("市净率", "倍", "pb"),
    "mcap_yi": ("总市值", "亿", "mcap_yi"),
    "turnover_pct": ("当日换手率", "%", "turnover_pct"),
}

PASS, FAIL, MISSING = "pass", "fail", "missing"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _is_fund_or_bond(code: str) -> bool:
    return code.startswith(("5", "15", "16", "18", "11", "12"))


def is_st(name: str) -> bool:
    """ST / *ST / 退市整理期：名称里带 ST 或「退」。"""
    upper = (name or "").upper().replace(" ", "")
    return "ST" in upper or "退" in name


def constituents(universe: str) -> dict[str, Any]:
    """指数成分股（中证官网文件），缓存 24 小时；取不到返回 error，不静默成空。"""
    spec = UNIVERSES.get(universe)
    if not spec:
        raise ValueError(f"未知的范围：{universe}")

    def fetch() -> dict[str, Any]:
        import akshare as ak

        frame = ak.index_stock_cons_csindex(symbol=spec["symbol"])
        rows = []
        for _, row in frame.iterrows():
            code = str(row.get("成分券代码") or "").strip()
            if len(code) == 6 and code.isdigit():
                rows.append({"code": code, "name": str(row.get("成分券名称") or "").strip()})
        as_of = ""
        if len(frame) > 0:
            as_of = str(frame.iloc[0].get("日期") or "")[:10]
        return {"label": spec["label"], "as_of": as_of, "rows": rows}

    value = cache_runtime.get(
        f"pool:constituents:{universe}",
        fetch,
        valid=lambda v: isinstance(v, dict) and bool(v.get("rows")),
        ttl=CONSTITUENT_TTL,
        decorate=False,
    )
    if not value or not value.get("rows"):
        raise RuntimeError(f"{spec['label']}的成分股没取到（中证官网文件不可用或缓存为空）")
    return value


def _quotes(codes: list[str]) -> dict[str, dict]:
    """分批取腾讯行情快照；单批失败只丢那一批（对应代码会进缺数据）。"""
    batches = [codes[i:i + QUOTE_BATCH] for i in range(0, len(codes), QUOTE_BATCH)]
    out: dict[str, dict] = {}

    def one(batch: list[str]) -> dict[str, dict]:
        try:
            return astock.tencent_quote(batch)
        except Exception:  # noqa: BLE001 — 一批失败不影响其它批
            return {}

    with ThreadPoolExecutor(max_workers=QUOTE_WORKERS) as pool:
        for part in pool.map(one, batches):
            out.update(part)
    return out


def _check_range(value: float | None, low: float | None, high: float | None) -> tuple[str, str | None]:
    """一条区间条件 → (状态, 不满足的原因)。值缺失一律 missing，不当成 0 也不当成满足。"""
    if value is None:
        return MISSING, "来源没有这个值"
    if low is not None and value < low:
        return FAIL, f"低于下限 {low:g}"
    if high is not None and value > high:
        return FAIL, f"高于上限 {high:g}"
    return PASS, None


def evaluate(code: str, name: str, quote: dict | None, conditions: dict[str, Any]) -> dict[str, Any]:
    """单只核对。返回每条的 status / value / 限值，以及总体判定（有任一 missing 且无 fail → missing）。"""
    checks: list[dict[str, Any]] = []
    verdict = PASS

    for key, (label, unit, field) in NUMERIC_FIELDS.items():
        cond = conditions.get(key) or {}
        low = _num(cond.get("min"))
        high = _num(cond.get("max"))
        if low is None and high is None:
            continue                       # 这一条用户没设，不出现在结果里
        raw = _num((quote or {}).get(field))
        # 腾讯行情对缺值一律填 0：PE / PB / 市值不可能正好是 0，一律按缺数据算。
        # 换手率例外——停牌当天真的是 0，这是值不是缺值。价格缺失时整只都不可信。
        if field != "turnover_pct" and raw == 0:
            raw = None
        if (quote or {}).get("price") in (None, 0):
            raw = None
        status, why = _check_range(raw, low, high)
        checks.append({"key": key, "label": label, "unit": unit, "value": raw,
                       "min": low, "max": high, "status": status, "reason": why})
        if status == FAIL:
            verdict = FAIL
        elif status == MISSING and verdict == PASS:
            verdict = MISSING

    if conditions.get("exclude_st"):
        named = bool(name) and is_st(name)
        status = PASS if not named else FAIL
        checks.append({"key": "exclude_st", "label": "排除 ST / 退市整理", "unit": "",
                       "value": name, "min": None, "max": None, "status": status,
                       "reason": None if status == PASS else f"名称含 ST / 退：{name}"})
        if status == FAIL:
            verdict = FAIL

    if conditions.get("exclude_halted"):
        price = _num((quote or {}).get("price"))
        if price is None:
            status, why = MISSING, "没有行情，判断不了是否停牌（开盘前普遍如此）"
        elif price <= 0:
            status, why = FAIL, "没有成交价，疑似停牌"
        else:
            status, why = PASS, None
        checks.append({"key": "exclude_halted", "label": "排除当日无成交", "unit": "",
                       "value": price, "min": None, "max": None, "status": status, "reason": why})
        if status == FAIL:
            verdict = FAIL
        elif status == MISSING and verdict == PASS:
            verdict = MISSING

    return {
        "code": code, "name": name or (quote or {}).get("name") or code,
        "price": _num((quote or {}).get("price")),
        "change_pct": _num((quote or {}).get("change_pct")),
        "verdict": verdict, "checks": checks,
        "failed": [c["label"] for c in checks if c["status"] == FAIL],
        "missing": [c["label"] for c in checks if c["status"] == MISSING],
    }


def build_pool(universe: str, codes: list[str] | None, conditions: dict[str, Any]) -> dict[str, Any]:
    """核对一批标的（或一个指数成分池）并分组。"""
    universe = universe or "watch"
    gaps: list[str] = []
    scope_label = "自选 / 持仓"

    if universe == "watch":
        raw = [str(c).strip() for c in (codes or [])]
        usable, skipped = [], []
        for code in dict.fromkeys(raw):
            if not (len(code) == 6 and code.isdigit()):
                skipped.append(code)
                continue
            if _is_fund_or_bond(code):
                skipped.append(code)
                continue
            usable.append(code)
        if skipped:
            gaps.append(f"基金 / 债券 / 港股美股等 {len(skipped)} 只在自选里，不在这个范围（{', '.join(skipped[:8])}）")
        members = [{"code": c, "name": ""} for c in usable]
    else:
        info = constituents(universe)
        scope_label = info["label"]
        members = info["rows"]
        if info.get("as_of"):
            gaps.append(f"成分股取中证官网文件，文件日期 {info['as_of']}")

    if len(members) > MAX_CODES:
        gaps.append(f"只核对了前 {MAX_CODES} 只，另有 {len(members) - MAX_CODES} 只没核对")
        members = members[:MAX_CODES]
    if not members:
        return {"universe": universe, "scope_label": scope_label, "rows": [],
                "groups": {"pass": [], "missing": [], "fail": []}, "counts": {"pass": 0, "missing": 0, "fail": 0},
                "conditions": conditions, "gaps": gaps or ["这个范围里没有可核对的 A 股"],
                "as_of": None, "source": "腾讯行情快照"}

    quotes = _quotes([m["code"] for m in members])
    rows = [evaluate(m["code"], m.get("name") or "", quotes.get(m["code"]), conditions) for m in members]
    rows.sort(key=lambda row: row["code"])

    groups = {
        "pass": [r["code"] for r in rows if r["verdict"] == PASS],
        "missing": [r["code"] for r in rows if r["verdict"] == MISSING],
        "fail": [r["code"] for r in rows if r["verdict"] == FAIL],
    }
    if any(r["verdict"] == MISSING for r in rows):
        gaps.append("有缺数据的标的单独列一组：来源没给值不算满足，也不算不满足")
    quote_times: list[str] = [str(q["quote_date"]) for q in quotes.values() if q.get("quote_date")]
    return {
        "universe": universe,
        "scope_label": scope_label,
        "rows": rows,
        "groups": groups,
        "counts": {k: len(v) for k, v in groups.items()},
        "conditions": conditions,
        "gaps": gaps,
        "as_of": max(quote_times) if quote_times else None,
        "source": "腾讯行情快照（PE/PB/市值/换手）",
        "note": "只排除、不排名；全部满足只说明过了你设的这几条，不代表其它任何判断",
    }
