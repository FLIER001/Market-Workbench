"""A股板块双评分系统（强度分 + 机会分）。

基于《A股板块评分系统技术方案_防追涨优化版_V1.1》落地：
- 板块强度分：相对趋势 / 扩散改善 / 交易确认 / 趋势稳定性 / 龙头集中度
- 板块机会分：基本面景气 / 盈利预期 / 估值匹配 / 价格位置 / 拥挤程度 / 催化
- 硬约束：机会分<40 不建议参与；强度分<40 只进左侧观察；拥挤惩罚>=15 标记"强势但不追"

数据源：
- 成分股：backend/data/plate_constituents.json（人工维护 + sectorResearch 代表企业）
- 行情：astock.tencent_quote 批量实时 + astock.tencent_kline 日K
- 全A基准：中证全指（东财 1.000985）
- 基本面：东财业绩报表（akshare stock_yjbb_em）
- 盈利预测：同花顺一致预期（akshare stock_profit_forecast_ths），缺失时并入基本面
"""

from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import astock
import cache_runtime

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
_CONSTITUENTS_FILE = os.path.join(os.path.dirname(__file__), "data", "plate_constituents.json")
_PRIMARY_CACHE_FILE = os.path.join(DATA_DIR, "plate_scores.json")
_FALLBACK_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".cache", "plate_scores.json")
_PRIMARY_SNAPSHOT_FILE = os.path.join(DATA_DIR, "plate_score_snapshots.json")
_FALLBACK_SNAPSHOT_FILE = os.path.join(os.path.dirname(__file__), ".cache", "plate_score_snapshots.json")
_STOCK_FACTOR_CACHE_FILE = os.path.join(DATA_DIR, "plate_stock_factors.json")
_BOARD_CACHE_FILE = os.path.join(DATA_DIR, "plate_board_constituents.json")
_TTL = 60 * 60
_INTRADAY_TTL = 15 * 60
_SCHEMA_VERSION = 2
_KLINE_COUNT = 260  # 约 1 年日K，覆盖位置/拥挤的时间序列分位
_KLINE_WORKERS = 8  # 腾讯K线并发上限（实测 8 并发稳定）
_FACTOR_WORKERS = 16  # 估值分位/盈利预测抓取并发（每日缓存，仅首次全量慢）
_MIN_CONSTITUENTS = 15
_MAX_CONSTITUENTS = 300  # 每个板块最多保留 300 只最相关成分（人工核心股始终保留，补充股按流通市值降序保留，不足不补）
_EFFECTIVE_STOCK_CAP = 0.07  # 同时满足单股≤8%、前五大≤35%
_DATE_WEIGHT_COVERAGE = 0.80  # 交易日保留门槛：当日有权重覆盖的成分比例
_BOARD_CACHE_SCHEMA = 2  # 版本2：缓存东财板块全量成分；300只上限在 _enrich_boards 取用时按流通市值降序截断

# 东财公开板块仅用于补足人工核心成分；人工清单始终优先保留。
_EM_BOARD_CODES = {
    "BK001": ["BK0917"], "BK002": ["BK1134"], "BK003": ["BK0800"],
    "BK004": ["BK1104", "BK0696"], "BK005": ["BK1135", "BK0579"],
    "BK006": ["BK1646"], "BK007": ["BK1090", "BK1184"], "BK008": ["BK1004"],
    "BK009": ["BK1166"], "BK010": ["BK0963", "BK0921"], "BK011": ["BK0490"],
    "BK012": ["BK0802", "BK0920", "BK1528"], "BK013": ["BK0900"],
    "BK014": ["BK0574"], "BK015": ["BK0588"], "BK016": ["BK0595", "BK1314", "BK1313"],
    "BK017": ["BK0989"], "BK018": ["BK1647", "BK0918", "BK0581", "BK1309", "BK1311"],
    "BK019": ["BK1106"], "BK020": ["BK0668"], "BK021": ["BK0899"],
    "BK022": ["BK0548", "BK0693", "BK1712"], "BK023": ["BK0896"],
    "BK024": ["BK0680", "BK1239", "BK1244"], "BK025": ["BK0485", "BK0927"],
    "BK026": ["BK1115", "BK1646", "BK0900"], "BK027": ["BK0547"],
    "BK028": ["BK0578", "BK0695"], "BK029": ["BK1346", "BK1345", "BK1344", "BK1343"],
    "BK030": ["BK1641", "BK1139", "BK0683"],
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _finite(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], current: float | None) -> float | None:
    """当前值在历史序列中的分位（0-100）。"""
    clean = [v for v in values if v is not None and math.isfinite(v)]
    if current is None or len(clean) < 12:
        return None
    below = sum(1 for v in clean if v < current)
    return round(below / len(clean) * 100, 1)


def _weighted(parts: list[tuple[float | None, float]]) -> float | None:
    """加权平均，跳过 None。权重自动归一化。"""
    valid = [(v, w) for v, w in parts if v is not None]
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    return sum(v * w for v, w in valid) / total_w


def _cross_section_rank(rows: list[dict], field: str) -> dict[str, float]:
    """横截面排名分位（0-100），值越大分位越高。"""
    valid = [(r["board_code"], r[field]) for r in rows if r.get(field) is not None]
    if len(valid) < 3:
        return {}
    sorted_vals = sorted(v for _, v in valid)
    n = len(sorted_vals)
    out = {}
    for code, val in valid:
        rank = sum(1 for v in sorted_vals if v < val)
        out[code] = round(rank / (n - 1) * 100, 1) if n > 1 else 50.0
    return out


def _cross_pct(value: float | None, peers: list[float]) -> float | None:
    """value 在 peers 横截面中的分位（0-100，越高越靠前）。"""
    if value is None:
        return None
    vals = [v for v in peers if v is not None]
    if len(vals) < 3:
        return None
    below = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    return round((below + 0.5 * equal) / len(vals) * 100, 1)


def _median(values: list[float]) -> float | None:
    clean = sorted(v for v in values if v is not None and math.isfinite(v))
    if not clean:
        return None
    n = len(clean)
    mid = n // 2
    return clean[mid] if n % 2 == 1 else (clean[mid - 1] + clean[mid]) / 2


def _load_constituents() -> list[dict]:
    with open(_CONSTITUENTS_FILE, encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("boards", [])


def _capped_weights(raw_weights: dict[str, float], cap: float = _EFFECTIVE_STOCK_CAP) -> dict[str, float]:
    """按原始权重比例迭代分配，直至所有成分均不超过 cap。"""
    positive = {code: value for code, value in raw_weights.items() if value > 0}
    if len(positive) * cap < 1 - 1e-9:
        return {}
    remaining = set(positive)
    result: dict[str, float] = {}
    mass = 1.0
    while remaining:
        raw_total = sum(positive[code] for code in remaining)
        proposed = {code: mass * positive[code] / raw_total for code in remaining}
        breached = {code for code, weight in proposed.items() if weight > cap + 1e-12}
        if not breached:
            result.update(proposed)
            break
        for code in breached:
            result[code] = cap
        mass -= cap * len(breached)
        remaining -= breached
    return result


def _enrich_boards(boards: list[dict]) -> list[dict]:
    """保留人工核心股，并用东财板块成分股补足至每板块最多 300 只（按流通市值降序，不足不补）。"""
    cached = {}
    cache_fresh = False
    try:
        with open(_BOARD_CACHE_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
        cached = payload.get("boards") or {}
        updated = datetime.fromisoformat(payload.get("updated_at"))
        cache_fresh = (
            payload.get("schema") == _BOARD_CACHE_SCHEMA
            and datetime.now(BEIJING) - updated.astimezone(BEIJING) < timedelta(days=7)
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        pass

    enriched = []
    refreshed = {}
    for board in boards:
        constituents = [dict(item) for item in board.get("constituents") or []]
        seen = {item["code"] for item in constituents}
        source_error = None
        candidates = cached.get(board["board_code"], []) if cache_fresh else []
        if not candidates:
            try:
                candidates = astock.concept_constituents_em(_EM_BOARD_CODES.get(board["board_code"], []))
            except Exception as error:
                candidates = cached.get(board["board_code"], [])
                source_error = str(error)
        refreshed[board["board_code"]] = candidates
        # 补充股预算：总数不超过 _MAX_CONSTITUENTS（人工核心股始终保留，不受上限约束）
        budget = max(_MAX_CONSTITUENTS - len(constituents), 0)
        for item in candidates:
            if budget <= 0:
                break
            if item["code"] in seen:
                continue
            constituents.append({
                "code": item["code"],
                "name": item["name"],
                "membership_type": "supplement",
                "business_relevance": 0.75,
                "source": item["source"],
            })
            seen.add(item["code"])
            budget -= 1
        enriched.append({
            **board,
            "constituents": constituents,
            "configured_constituent_count": len(board.get("constituents") or []),
            "constituent_source_error": source_error,
            "constituent_source_stale": bool(source_error and candidates),
        })
    if refreshed and (not cache_fresh or refreshed != cached):
        try:
            os.makedirs(os.path.dirname(_BOARD_CACHE_FILE), exist_ok=True)
            tmp = _BOARD_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema": _BOARD_CACHE_SCHEMA,
                        "updated_at": datetime.now(BEIJING).isoformat(),
                        "boards": refreshed,
                    },
                    handle,
                    ensure_ascii=False,
                )
            os.replace(tmp, _BOARD_CACHE_FILE)
        except OSError:
            pass
    return enriched


def _load_stock_factor_cache() -> dict:
    try:
        with open(_STOCK_FACTOR_CACHE_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload.get("stocks") or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_stock_factor_cache(stocks: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STOCK_FACTOR_CACHE_FILE), exist_ok=True)
        tmp = _STOCK_FACTOR_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated_at": datetime.now(BEIJING).isoformat(), "stocks": stocks}, handle, ensure_ascii=False)
        os.replace(tmp, _STOCK_FACTOR_CACHE_FILE)
    except OSError:
        pass


def _fetch_stock_factors(codes: list[str]) -> dict[str, dict]:
    """每日缓存真实历史估值分位与一致预期修正；失败时保留旧值并标 stale。"""
    today = datetime.now(BEIJING).date().isoformat()
    cache = _load_stock_factor_cache()
    missing = [code for code in codes if (cache.get(code) or {}).get("fetched_on") != today]

    def fetch(code: str) -> tuple[str, dict]:
        valuation = astock.valuation_percentile(code)
        forecast = astock.profit_forecast_revision_em(code)
        metrics = valuation.get("metrics") or {}
        return code, {
            "fetched_on": today,
            "valuation_period": valuation.get("period"),
            "pe_percentile": (metrics.get("pe_ttm") or {}).get("percentile"),
            "pb_percentile": (metrics.get("pb") or {}).get("percentile"),
            "forecast": forecast,
            "stale": False,
        }

    with ThreadPoolExecutor(max_workers=_FACTOR_WORKERS) as executor:
        futures = {executor.submit(fetch, code): code for code in missing}
        for future in as_completed(futures):
            code = futures[future]
            try:
                _, cache[code] = future.result()
            except Exception:
                if code in cache:
                    cache[code] = {**cache[code], "stale": True}
    _save_stock_factor_cache(cache)
    return {code: cache.get(code, {}) for code in codes}


def _completed_financial_period(now: datetime) -> str:
    """选择全市场已经完成法定披露的最近可比报告期。"""
    year = now.year
    if (now.month, now.day) >= (10, 31):
        return f"{year}0930"
    if (now.month, now.day) >= (8, 31):
        return f"{year}0630"
    if (now.month, now.day) >= (4, 30):
        return f"{year}0331"
    return f"{year - 1}1231"


# ---------------------------------------------------------------------------
# 板块指数构建
# ---------------------------------------------------------------------------

def _build_board_index(
    constituents: list[dict],
    klines: dict[str, list[dict]],
    quotes: dict[str, dict],
) -> dict | None:
    """构建板块综合指数（0.6×限权市值 + 0.4×等权）。

    返回 {dates: [...], cap_index: [...], equal_index: [...], blended: [...],
           weights: {code: w}, turnover: [...], amount: [...]}
    """
    if not constituents:
        return None

    # 1. 计算权重：relevance × sqrt(float_mcap)，并要求完整历史窗口
    raw_weights = {}
    weight_inputs = {}
    for c in constituents:
        code = c["code"]
        quote = quotes.get(code)
        kl = klines.get(code, [])
        if not quote or len(kl) < 121:
            continue
        mcap = _finite(quote.get("float_mcap_yi"))
        price = _finite(quote.get("price"))
        if mcap is None or mcap <= 0 or price is None or price <= 0:
            continue
        relevance = _finite(c.get("business_relevance")) or 1.0
        raw_weights[code] = relevance * math.sqrt(mcap)
        weight_inputs[code] = {
            "relevance": relevance,
            "float_shares_yi": mcap / price,
        }

    if len(raw_weights) < _MIN_CONSTITUENTS:
        return None

    weights = _capped_weights(raw_weights)
    if not weights:
        return None

    # 2. 确定交易日：全量成分无法要求“每只股票当日都有行情”，
    #    改为按当日权重覆盖率筛选（缺数据的个股权重当日自动忽略）。
    total_weight = sum(weights.values())
    date_weight: dict[str, float] = {}
    for code, weight in weights.items():
        for row in klines.get(code, []):
            date_weight[row["date"]] = date_weight.get(row["date"], 0.0) + weight
    sorted_dates = [
        day for day in sorted(date_weight)
        if date_weight[day] >= total_weight * _DATE_WEIGHT_COVERAGE
    ]
    if len(sorted_dates) < 121:
        return None

    # 3. 预建 date -> (index, row) 索引，供净值/成交额/估值三处复用
    kline_idx = {code: {r["date"]: (i, r) for i, r in enumerate(klines.get(code, []))} for code in weights}

    return {
        "dates": sorted_dates,
        "weights": weights,
        "weight_inputs": weight_inputs,
        "kline_idx": kline_idx,
        "effective_constituent_count": len(weights),
        "max_weight_pct": round(max(weights.values()) * 100, 2),
        "top5_weight_pct": round(sum(sorted(weights.values(), reverse=True)[:5]) * 100, 2),
    }


def _derive_daily_series(
    weights: dict[str, float],
    klines: dict[str, list[dict]],
    sorted_dates: list[str],
    kline_idx: dict[str, dict[str, tuple[int, dict]]],
    weight_inputs: dict[str, dict] | None = None,
) -> dict:
    """由前一日价格形成的限权权重派生指数、成交额与换手率序列。"""
    cap_ret_series: list[float] = []
    eq_ret_series: list[float] = []
    blended_series: list[float] = []
    amount_series: list[float] = []
    turnover_series: list[float | None] = []

    for date in sorted_dates:
        day_weights = weights
        if weight_inputs:
            raw = {}
            for code, item in weight_inputs.items():
                hit = kline_idx.get(code, {}).get(date)
                if not hit or hit[0] == 0:
                    continue
                previous_close = klines[code][hit[0] - 1]["close"]
                if previous_close > 0:
                    raw[code] = item["relevance"] * math.sqrt(item["float_shares_yi"] * previous_close)
            day_weights = _capped_weights(raw) or weights
        cap_ret = 0.0
        eq_ret = 0.0
        day_amount = 0.0
        day_turnover = 0.0
        turnover_weight = 0.0
        valid_count = 0
        for code, w in day_weights.items():
            hit = kline_idx.get(code, {}).get(date)
            if not hit:
                continue
            idx, row = hit
            if idx == 0:
                continue
            prev_close = klines[code][idx - 1]["close"]
            if prev_close <= 0:
                continue
            ret = (row["close"] - prev_close) / prev_close
            cap_ret += w * ret
            eq_ret += ret
            valid_count += 1
            amount = _finite(row.get("amount"))
            day_amount += amount if amount is not None else (_finite(row.get("volume")) or 0.0) * row["close"] * 100
            turnover = _finite(row.get("turnover_pct"))
            if turnover is not None:
                day_turnover += w * turnover
                turnover_weight += w
        if valid_count > 0:
            eq_ret /= valid_count
        cap_ret_series.append(cap_ret)
        eq_ret_series.append(eq_ret)
        blended_series.append(0.6 * cap_ret + 0.4 * eq_ret)
        amount_series.append(day_amount)
        turnover_series.append(day_turnover / turnover_weight if turnover_weight > 0 else None)

    cap_nav = [1.0]
    eq_nav = [1.0]
    blend_nav = [1.0]
    for i in range(1, len(blended_series)):
        cap_nav.append(cap_nav[-1] * (1 + cap_ret_series[i]))
        eq_nav.append(eq_nav[-1] * (1 + eq_ret_series[i]))
        blend_nav.append(blend_nav[-1] * (1 + blended_series[i]))

    return {
        "cap_nav": cap_nav,
        "equal_nav": eq_nav,
        "blended_nav": blend_nav,
        "amount": amount_series,
        "turnover": turnover_series,
    }


# ---------------------------------------------------------------------------
# 板块强度分（100分）
# ---------------------------------------------------------------------------

def _calc_strength(board_index: dict, klines: dict[str, list[dict]], benchmark: list[dict], cross: dict[str, list[float]]) -> dict:
    """强度分五维度；全部使用可观察量，不把静态权重冒充收益贡献。"""
    del cross
    dates = board_index["dates"]
    derived = board_index.get("derived") or _derive_daily_series(
        board_index["weights"], klines, dates, board_index["kline_idx"], board_index.get("weight_inputs")
    )
    board_index["derived"] = derived
    board_index["amount"] = derived["amount"]  # 供主流程复用，避免重复计算
    board_index["turnover"] = derived["turnover"]
    nav = derived["blended_nav"]
    eq_nav = derived["equal_nav"]
    cap_nav = derived["cap_nav"]
    n = len(dates)
    if n < 121:
        return {"score": None, "detail": {}}

    # 基准对齐
    bench_map = {r["date"]: r["close"] for r in benchmark}
    bench_closes = [bench_map.get(d) for d in dates]

    # --- 1. 相对趋势（25分）---
    def _window_return(series, w):
        if len(series) < w + 1:
            return None
        return (series[-1] - series[-1 - w]) / series[-1 - w]

    r20 = _window_return(nav, 20)
    r60 = _window_return(nav, 60) if n >= 61 else None
    bench20 = _window_return(bench_closes, 20) if all(b is not None for b in bench_closes[-21:]) else None
    bench60 = _window_return(bench_closes, 60) if all(b is not None for b in bench_closes[-61:]) else None

    er20 = (r20 - bench20) if r20 is not None and bench20 is not None else None
    er60 = (r60 - bench60) if r60 is not None and bench60 is not None else None

    # 相对趋势分：超额收益映射到 0-100（±10% 为极值）
    def _er_score(er):
        if er is None:
            return None
        return max(0.0, min(100.0, 50 + er * 500))

    relative_trend = _weighted([(_er_score(er20), 0.6), (_er_score(er60), 0.4)])

    # --- 2. 扩散改善（30分）：覆盖率变化/收益中位数/等权限权差/新高新低 ---
    def _breadth_at(offset: int) -> tuple[float | None, float | None, float | None]:
        target = dates[-1 - offset]
        above = []
        returns20 = []
        high_low = []
        for code in board_index["weights"]:
            hit = board_index["kline_idx"].get(code, {}).get(target)
            if not hit or hit[0] < 59:
                continue
            idx, row = hit
            history = klines[code]
            ma20 = sum(item["close"] for item in history[idx - 19:idx + 1]) / 20
            above.append(row["close"] > ma20)
            returns20.append(row["close"] / history[idx - 20]["close"] - 1)
            window60 = [item["close"] for item in history[idx - 59:idx + 1]]
            high_low.append(1 if row["close"] >= max(window60) else -1 if row["close"] <= min(window60) else 0)
        return (
            sum(above) / len(above) if above else None,
            _median(returns20),
            sum(high_low) / len(high_low) if high_low else None,
        )

    b0, median_r20, hl0 = _breadth_at(0)
    b5, _, hl5 = _breadth_at(5)
    b20, _, _ = _breadth_at(20)
    breadth_change = 0.6 * (b0 - b5) + 0.4 * (b0 - b20) if None not in (b0, b5, b20) else None
    coverage_score = max(0.0, min(100.0, 50 + breadth_change * 250)) if breadth_change is not None else None
    median_return_score = max(0.0, min(100.0, 50 + median_r20 * 500)) if median_r20 is not None else None
    eq_r20 = _window_return(eq_nav, 20)
    cap_r20 = _window_return(cap_nav, 20)
    equal_cap_spread = (eq_r20 - cap_r20) if eq_r20 is not None and cap_r20 is not None else None
    spread_score = max(0.0, min(100.0, 50 + equal_cap_spread * 1000)) if equal_cap_spread is not None else None
    high_low_score = max(0.0, min(100.0, 50 + (hl0 - hl5) * 250)) if None not in (hl0, hl5) else None
    breadth_impulse = _weighted([
        (coverage_score, 10), (median_return_score, 8), (spread_score, 7), (high_low_score, 5),
    ])

    # --- 3. 交易确认（20分）：板块成交额占全市场成交额的变化 ---
    amounts = board_index["amount"]
    benchmark_amount = {row["date"]: _finite(row.get("amount")) for row in benchmark}
    turnover_shares = [
        amount / benchmark_amount[day]
        if benchmark_amount.get(day) and benchmark_amount[day] > 0 else None
        for day, amount in zip(dates, amounts)
    ]
    clean_shares = [value for value in turnover_shares if value is not None]
    flow_confirmation = None
    if len(clean_shares) >= 60:
        ma5 = sum(clean_shares[-5:]) / 5
        ma20 = sum(clean_shares[-20:]) / 20
        if ma20 > 0:
            flow_impulse = (ma5 - ma20) / ma20
            impulse_score = max(0.0, min(100.0, 50 + flow_impulse * 500))
            pctile = _percentile(clean_shares, clean_shares[-1])
            if pctile is not None:
                level_score = (
                    pctile / 30 * 40 if pctile < 30
                    else 40 + (pctile - 30) / 50 * 40 if pctile < 80
                    else 80 + (pctile - 80) / 15 * 10 if pctile < 95
                    else 90 - (pctile - 95) * 2
                )
                flow_confirmation = 0.7 * impulse_score + 0.3 * level_score
                if r20 is not None and r20 < 0:
                    flow_confirmation -= min(30.0, abs(r20) * 300)
                flow_confirmation = max(0.0, min(100.0, flow_confirmation))

    # --- 4. 趋势稳定性（15分）：收益风险比 + 回撤 + 上涨日 + 路径效率 ---
    rets = []
    for i in range(max(1, n - 20), n):
        if nav[i - 1] > 0:
            rets.append((nav[i] - nav[i - 1]) / nav[i - 1])
    trend_quality = None
    if len(rets) >= 10:
        mean_ret = sum(rets) / len(rets)
        var = sum((r - mean_ret) ** 2 for r in rets) / len(rets)
        vol = math.sqrt(var)
        window = nav[-21:]
        peak = window[0]
        max_drawdown = 0.0
        for value in window:
            peak = max(peak, value)
            max_drawdown = min(max_drawdown, value / peak - 1)
        path_total = sum(abs(value) for value in rets)
        risk_adjusted = max(0.0, min(100.0, 50 + (mean_ret / vol) * 25)) if vol > 0 else None
        drawdown_score = max(0.0, min(100.0, 100 + max_drawdown * 500))
        up_score = sum(value > 0 for value in rets) / len(rets) * 100
        efficiency = abs((r20 or 0)) / path_total if path_total > 0 else 0.0
        efficiency_score = min(100.0, efficiency * 100) if (r20 or 0) >= 0 else 0.0
        trend_quality = _weighted([
            (risk_adjusted, 0.4), (drawdown_score, 0.3), (up_score, 0.2), (efficiency_score, 0.1),
        ])

    # --- 5. 龙头集中度（10分，反向）：前三大绝对收益贡献率 ---
    contributions = []
    for code, weight in board_index["weights"].items():
        history = klines.get(code, [])
        if len(history) >= 21 and history[-21]["close"] > 0:
            contribution = abs(weight * (history[-1]["close"] / history[-21]["close"] - 1))
            contributions.append(contribution)
    total_contribution = sum(contributions)
    top3_contribution = sum(sorted(contributions, reverse=True)[:3]) / total_contribution if total_contribution > 0 else None
    leader_concentration = None
    if top3_contribution is not None:
        leader_concentration = (
            100.0 if top3_contribution <= 0.35
            else 0.0 if top3_contribution >= 0.65
            else (0.65 - top3_contribution) / 0.30 * 100
        )

    # --- 合成 ---
    strength = _weighted([
        (relative_trend, 0.25),
        (breadth_impulse, 0.30),
        (flow_confirmation, 0.20),
        (trend_quality, 0.15),
        (leader_concentration, 0.10),
    ])

    return {
        "score": round(strength, 1) if strength is not None else None,
        "detail": {
            "relative_trend": round(relative_trend, 1) if relative_trend is not None else None,
            "breadth_impulse": round(breadth_impulse, 1) if breadth_impulse is not None else None,
            "flow_confirmation": round(flow_confirmation, 1) if flow_confirmation is not None else None,
            "trend_quality": round(trend_quality, 1) if trend_quality is not None else None,
            "leader_concentration": round(leader_concentration, 1) if leader_concentration is not None else None,
            "er20": round(er20 * 100, 2) if er20 is not None else None,
            "er60": round(er60 * 100, 2) if er60 is not None else None,
            "ma20_coverage": round(b0 * 100, 1) if b0 is not None else None,
            "ma20_coverage_change": round(breadth_change * 100, 1) if breadth_change is not None else None,
            "top3_return_contribution": round(top3_contribution * 100, 1) if top3_contribution is not None else None,
        },
    }


# ---------------------------------------------------------------------------
# 板块机会分（100分）
# ---------------------------------------------------------------------------

def _calc_opportunity(
    board_index: dict,
    klines: dict[str, list[dict]],
    quotes: dict[str, dict],
    yjbb: dict[str, dict],
    stock_factors: dict[str, dict],
    benchmark: list[dict],
) -> dict:
    """机会分：财务、盈利修正、真实估值历史分位、时间序列位置与拥挤。"""
    del quotes
    dates = board_index["dates"]
    derived = board_index.get("derived") or _derive_daily_series(
        board_index["weights"], klines, dates, board_index["kline_idx"], board_index.get("weight_inputs")
    )
    board_index["derived"] = derived
    nav = derived["blended_nav"]
    n = len(nav)
    if n < 121:
        return {"score": None, "detail": {}}

    codes = list(board_index["weights"].keys())

    # --- 1. 基本面景气（30分）---
    profit_yoys: dict[str, float] = {}
    revenue_yoys: dict[str, float] = {}
    for c in codes:
        snap = yjbb.get(c)
        if snap:
            py = _finite(snap.get("profit_yoy"))
            ry = _finite(snap.get("revenue_yoy"))
            if py is not None:
                profit_yoys[c] = max(-100.0, min(200.0, py))
            if ry is not None:
                revenue_yoys[c] = max(-100.0, min(200.0, ry))

    fundamental = None
    def _fundamental_component(values: dict[str, float]) -> float | None:
        if not values:
            return None
        total_weight = sum(board_index["weights"].get(code, 0.0) for code in values)
        weighted_mean = sum(board_index["weights"].get(code, 0.0) * value for code, value in values.items()) / total_weight
        median = _median(list(values.values()))
        positive = sum(value > 0 for value in values.values()) / len(values) * 100
        score = lambda value: max(0.0, min(100.0, 50 + value * 0.5))
        return _weighted([(score(weighted_mean), 0.4), (score(median), 0.4), (positive, 0.2)])

    financial_coverage = len(profit_yoys) / len(codes) if codes else 0.0
    if financial_coverage >= 0.60:
        fundamental = _weighted([
            (_fundamental_component(profit_yoys), 0.65),
            (_fundamental_component(revenue_yoys), 0.35),
        ])

    # --- 2. 盈利预期变化（20分）：下一预测年度 EPS 相对上月修正 ---
    revisions = {
        code: _finite(((stock_factors.get(code) or {}).get("forecast") or {}).get("revision_pct"))
        for code in codes
    }
    revisions = {code: value for code, value in revisions.items() if value is not None}
    forecast_coverage = len(revisions) / len(codes) if codes else 0.0
    earnings_revision = None
    if forecast_coverage >= 0.40:
        median_revision = _median(list(revisions.values()))
        positive_revision = sum(value > 0 for value in revisions.values()) / len(revisions) * 100
        revision_score = max(0.0, min(100.0, 50 + (median_revision or 0) * 2.5))
        earnings_revision = _weighted([(revision_score, 0.5), (positive_revision, 0.5)])

    # --- 3. 估值匹配度（15分）：真实近五年 PE/PB 历史分位 ---
    stock_scores = {}
    for code in codes:
        factor = stock_factors.get(code) or {}
        pe_pct = _finite(factor.get("pe_percentile"))
        pb_pct = _finite(factor.get("pb_percentile"))
        score = _weighted([
            (100 - pe_pct if pe_pct is not None else None, 0.7),
            (100 - pb_pct if pb_pct is not None else None, 0.3),
        ])
        if score is not None:
            stock_scores[code] = score
    valuation_coverage = len(stock_scores) / len(codes) if codes else 0.0
    valuation_match = None
    if valuation_coverage >= 0.60:
        weight_sum = sum(board_index["weights"].get(code, 0.0) for code in stock_scores)
        valuation_match = sum(board_index["weights"].get(code, 0.0) * value for code, value in stock_scores.items()) / weight_sum

    # --- 4. 价格位置（20分）：全部使用板块自身时间序列分位 ---
    r20_series = [nav[i] / nav[i - 20] - 1 for i in range(20, n)]
    r60_series = [nav[i] / nav[i - 60] - 1 for i in range(60, n)]
    dist_ma60_series = [nav[i] / (sum(nav[i - 59:i + 1]) / 60) - 1 for i in range(59, n)]
    high_closeness_series = [nav[i] / max(nav[i - 119:i + 1]) for i in range(119, n)]
    position_risk = _weighted([
        (_percentile(r20_series, r20_series[-1]), 0.35),
        (_percentile(r60_series, r60_series[-1]), 0.25),
        (_percentile(dist_ma60_series, dist_ma60_series[-1]), 0.20),
        (_percentile(high_closeness_series, high_closeness_series[-1]), 0.20),
    ])
    position_score = 100 - position_risk if position_risk is not None else None

    # --- 5. 拥挤程度（15分）：换手率与成交额占比的历史分位 ---
    turnover_history = [value for value in derived["turnover"] if value is not None]
    benchmark_amount = {row["date"]: _finite(row.get("amount")) for row in benchmark}
    turnover_share_history = [
        amount / benchmark_amount[day]
        for day, amount in zip(dates, derived["amount"])
        if benchmark_amount.get(day) and benchmark_amount[day] > 0
    ]
    crowding_risk = _weighted([
        (_percentile(turnover_history, turnover_history[-1]) if turnover_history else None, 0.5),
        (_percentile(turnover_share_history, turnover_share_history[-1]) if turnover_share_history else None, 0.5),
    ])
    crowding_score = None
    if crowding_risk is not None:
        crowding_score = 100 - crowding_risk

    # 催化未接入前不再填固定中性值；其 5% 明确转给价格位置。
    catalyst = None
    required = [fundamental, earnings_revision, valuation_match, position_score, crowding_score]
    opportunity = (
        0.30 * fundamental + 0.20 * earnings_revision + 0.15 * valuation_match
        + 0.20 * position_score + 0.15 * crowding_score
        if all(value is not None for value in required) else None
    )

    return {
        "score": round(opportunity, 1) if opportunity is not None else None,
        "detail": {
            "fundamental": round(fundamental, 1) if fundamental is not None else None,
            "earnings_revision": round(earnings_revision, 1) if earnings_revision is not None else None,
            "valuation_match": round(valuation_match, 1) if valuation_match is not None else None,
            "position_score": round(position_score, 1) if position_score is not None else None,
            "crowding_score": round(crowding_score, 1) if crowding_score is not None else None,
            "catalyst": None,
        },
        "coverage": {
            "financial": round(financial_coverage * 100, 1),
            "forecast": round(forecast_coverage * 100, 1),
            "valuation": round(valuation_coverage * 100, 1),
            "stale_factor_count": sum(bool((stock_factors.get(code) or {}).get("stale")) for code in codes),
        },
    }


# ---------------------------------------------------------------------------
# 状态信号
# ---------------------------------------------------------------------------

def _classify_state(
    strength: float | None,
    opportunity: float | None,
    crowding_penalty: float = 0.0,
    strong_line: float = 70.0,
    weak_line: float = 50.0,
    opp_line: float = 65.0,
) -> str:
    """强度-机会二维矩阵状态（分位驱动）。

    strong_line / weak_line / opp_line 由主流程按 30 板块横截面分位给出：
    强度高/低、机会高低是"在 30 家里排前列/靠后"的相对概念，随数据自适应，
    避免横截面化后固定 70/65 阈值导致的象限空心化。
    """
    if strength is None or opportunity is None:
        return "数据不足"
    # 拥挤惩罚只在真正跨过强势线时才使用“强势”标签。
    if crowding_penalty >= 15:
        if strength >= strong_line:
            return "强势但不追"
        return "中性观察" if strength >= weak_line and opportunity >= weak_line else "弱势回避"
    if strength >= strong_line and opportunity >= opp_line:
        return "主线可参与"
    if strength >= strong_line and opportunity >= weak_line:
        return "强势观察"
    if strength >= strong_line:
        return "强势但不追"
    if strength >= weak_line and opportunity >= opp_line:
        return "低位启动候选"
    if strength < weak_line and opportunity >= opp_line:
        return "左侧观察"
    if strength < weak_line and opportunity >= weak_line:
        return "中性观察"
    return "弱势回避"


def _pct_line(values: list[float], pct: float, floor: float, cap: float) -> float:
    """横截面分位阈值：取 values 的 pct 分位，夹在 [floor, cap] 之间防极端。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return cap
    k = (len(vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    frac = k - lo
    line = vals[lo] + (vals[hi] - vals[lo]) * frac
    return max(floor, min(cap, line))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _fetch_all_klines(codes: list[str]) -> dict[str, list[dict]]:
    """并发拉取所有成分股日K。"""
    out = {}
    with ThreadPoolExecutor(max_workers=_KLINE_WORKERS) as executor:
        futures = {
            executor.submit(astock.tencent_kline, code, "day", _KLINE_COUNT): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                rows = future.result()
                if rows:
                    out[code] = rows
            except Exception:
                continue
    return out


def _fetch_quotes(codes: list[str]) -> dict[str, dict]:
    quotes = {}
    for start in range(0, len(codes), 60):
        quotes.update(astock.tencent_quote(codes[start:start + 60]))
    return quotes


def _build() -> dict:
    now = datetime.now(BEIJING)
    boards = _enrich_boards(_load_constituents())
    if not boards:
        raise RuntimeError("板块成分股数据为空")

    # 1. 收集所有成分股代码（去重）
    all_codes = list({c["code"] for b in boards for c in b["constituents"]})

    # 2. 批量拉取实时行情
    quotes = _fetch_quotes(all_codes)

    # 3. 并发拉取日K
    klines = _fetch_all_klines(all_codes)

    # 4. 拉取中证全指基准
    benchmark = astock.csi_all_share_daily(_KLINE_COUNT)

    # 5. 使用全市场已完成披露的同一报告期；不跨期静默补值
    financial_period = _completed_financial_period(now)
    yjbb = astock.yjbb_snapshot(financial_period)
    stock_factors = _fetch_stock_factors(all_codes)

    # 6. 第一遍：构建各板块指数
    built: list[tuple[dict, dict]] = []  # (board, board_index)
    for board in boards:
        board_index = _build_board_index(board["constituents"], klines, quotes)
        if not board_index:
            continue
        derived = board_index.get("derived") or _derive_daily_series(
            board_index["weights"], klines, board_index["dates"], board_index["kline_idx"], board_index.get("weight_inputs")
        )
        board_index["derived"] = derived
        board_index["amount"] = derived["amount"]
        board_index["turnover"] = derived["turnover"]
        built.append((board, board_index))

    # 7. 第二遍：逐板块打分
    rows = []
    built_map = {b["board_code"]: bi for b, bi in built}
    for board in boards:
        board_index = built_map.get(board["board_code"])
        if not board_index:
            rows.append({
                "board_code": board["board_code"],
                "board_name": board["board_name"],
                "board_group": board.get("board_group", ""),
                "sector_key": board.get("sector_key"),
                "strength": {"score": None, "detail": {}},
                "opportunity": {"score": None, "detail": {}},
                "priority": None,
                "state": "数据不足",
                "signal": None,
                "constituent_count": len(board["constituents"]),
                "effective_constituent_count": 0,
                "confidence": "低",
                "data_quality": {
                    "flags": ["LOW_COVERAGE"],
                    "configured_count": board.get("configured_constituent_count"),
                    "source_count": len(board["constituents"]),
                },
                "error": "指数构建失败（有效成分少于15只或历史不足120日）",
            })
            continue

        strength = _calc_strength(board_index, klines, benchmark, {})
        opportunity = _calc_opportunity(board_index, klines, quotes, yjbb, stock_factors, benchmark)

        s_score = strength["score"]
        o_score = opportunity["score"]

        # 综合优先级：先扣拥挤，再按数据置信度调整
        raw_priority = None
        if s_score is not None and o_score is not None:
            raw_priority = round(0.55 * o_score + 0.45 * s_score, 1)

        # 拥挤惩罚（用机会分的拥挤维度反向）
        crowding_penalty = 0.0
        crowd_detail = opportunity["detail"].get("crowding_score")
        if crowd_detail is not None and crowd_detail < 50:
            crowding_penalty = round((50 - crowd_detail) * 0.5, 1)

        coverage = opportunity.get("coverage") or {}
        flags = []
        effective_count = board_index.get("effective_constituent_count", 0)
        if effective_count < _MIN_CONSTITUENTS or coverage.get("financial", 0) < 60 or coverage.get("forecast", 0) < 40 or coverage.get("valuation", 0) < 60:
            flags.append("LOW_COVERAGE")
        if coverage.get("stale_factor_count", 0) > 0:
            flags.append("STALE")
        if len(board_index["dates"]) < 120:
            flags.append("SHORT_HISTORY")
        if flags or raw_priority is None:
            confidence = "低"
            priority = None
        elif coverage.get("financial", 0) >= 80 and coverage.get("forecast", 0) >= 60 and coverage.get("valuation", 0) >= 80:
            confidence = "高"
            priority = round(max(0.0, raw_priority - crowding_penalty), 1)
        else:
            confidence = "中"
            priority = round(max(0.0, raw_priority - crowding_penalty) * 0.85, 1)

        rows.append({
            "board_code": board["board_code"],
            "board_name": board["board_name"],
            "board_group": board.get("board_group", ""),
            "sector_key": board.get("sector_key"),
            "strength": strength,
            "opportunity": opportunity,
            "priority": priority,
            "raw_priority": raw_priority,
            "state": None,  # 占位，分位阈值确定后统一分类
            "signal": None,
            "constituent_count": len(board["constituents"]),
            "effective_constituent_count": effective_count,
            "crowding_penalty": crowding_penalty,
            "confidence": confidence,
            "data_quality": {
                "flags": flags,
                "configured_count": board.get("configured_constituent_count"),
                "source_count": len(board["constituents"]),
                "effective_count": effective_count,
                "history_days": len(board_index["dates"]),
                "max_weight_pct": board_index.get("max_weight_pct"),
                "top5_weight_pct": board_index.get("top5_weight_pct"),
                **coverage,
            },
        })

    # 第三遍：按全体横截面分位定强弱阈值，统一分类 + 打信号
    s_all = [r["strength"]["score"] for r in rows if r["strength"]["score"] is not None]
    o_all = [r["opportunity"]["score"] for r in rows if r["opportunity"]["score"] is not None]
    strong_line = _pct_line(s_all, 0.70, floor=55.0, cap=75.0)  # 前30%算强
    weak_line = _pct_line(s_all, 0.40, floor=45.0, cap=55.0)     # 后40%算弱
    opp_line = _pct_line(o_all, 0.65, floor=55.0, cap=70.0)      # 前35%算有机会

    for r in rows:
        if r["state"] is not None:  # 数据不足的占位行
            continue
        s_score = r["strength"]["score"]
        o_score = r["opportunity"]["score"]
        if r.get("confidence") == "低":
            r["state"] = "数据不足"
            r["priority"] = None
            continue
        r["state"] = _classify_state(s_score, o_score, r["crowding_penalty"], strong_line, weak_line, opp_line)
        if s_score is None or o_score is None:
            continue
        r["signal"] = {
            "主线可参与": "MAIN_TREND_CONFIRMED",
            "低位启动候选": "ROTATION_START",
            "左侧观察": "LEFT_SIDE_WATCH",
            "强势但不追": "STRONG_BUT_OVERHEATED",
            "弱势回避": "TREND_EXIT",
        }.get(r["state"])
        if r["state"] in {"强势但不追", "弱势回避"} or o_score < 40:
            r["priority"] = None

    # 按优先级排序
    rows.sort(key=lambda r: (r["priority"] is None, -(r["priority"] or 0)))

    # 排名
    for i, row in enumerate(rows):
        row["rank"] = i + 1

    quote_dates = [item.get("quote_date") for item in quotes.values() if item.get("quote_date")]
    quote_date = max(set(quote_dates), key=quote_dates.count) if quote_dates else None
    benchmark_date = benchmark[-1]["date"] if benchmark else None
    board_dates = [index["dates"][-1] for _, index in built if index.get("dates")]
    date_candidates = [value for value in [quote_date, benchmark_date, *board_dates] if value]
    as_of = min(date_candidates) if date_candidates else now.date().isoformat()
    quote_times = [item.get("quote_time") for item in quotes.values() if item.get("quote_date") == as_of and item.get("quote_time")]
    quote_time = max(quote_times) if quote_times else None
    is_intraday = bool(
        as_of == now.date().isoformat() and quote_time and quote_time < "15:05:00"
        and now.weekday() < 5 and (now.hour, now.minute) >= (9, 15)
    )

    return {
        "schema_version": _SCHEMA_VERSION,
        "as_of": as_of,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "is_intraday": is_intraday,
        "quote_time": quote_time,
        "financial_period": financial_period,
        "board_count": len(rows),
        "boards": rows,
        "thresholds": {"strong": round(strong_line, 1), "weak": round(weak_line, 1), "opportunity": round(opp_line, 1)},
        "methodology": {
            "framework": "板块强度分 + 板块机会分（防追涨双评分）",
            "strength_weights": {"relative_trend": 25, "breadth_impulse": 30, "flow_confirmation": 20, "trend_quality": 15, "leader_concentration": 10},
            "opportunity_weights": {"fundamental": 30, "earnings_revision": 20, "valuation_match": 15, "position_score": 20, "crowding_score": 15, "catalyst": 0},
            "hard_constraints": [
                "机会分<40：不进入建议参与名单",
                "强度分<40：只能进入左侧观察",
                "拥挤惩罚≥15：标记为强势但不追",
            ],
            "definitions": [
                "强度分：确认趋势和市场共识，不直接表示买入价值",
                "机会分：判断当前价格位置下的未来收益风险比",
                "相对趋势：板块综合指数 20/60 日相对中证全指超额收益",
                "板块权重：人工核心股加东财板块补充股，前一日流通市值形成权重；单股有效上限7%，同时满足单股不高于8%和前五大不高于35%。",
                "扩散改善：MA20覆盖变化、收益中位数、等权限权差和新高新低改善。",
                "交易确认：板块真实成交额占中证全指成交额的MA5/MA20变化，不解释为资金净流入。",
                "估值匹配：成分股真实近五年PE/PB历史分位；盈利修正取下一预测年度EPS相对上月变化。",
                "价格位置与拥挤程度：均使用板块自身时间序列历史分位；催化未取得结构化数据前不计分。",
                "成分范围：人工核心股全部保留，东财概念/行业板块成分按流通市值降序补足，每板块最多300只最相关成分（不足300只不补）；交易日按当日权重覆盖率≥80%保留。",
            ],
            "sources": [
                {"label": "腾讯财经个股行情与K线", "url": "https://stockapp.finance.qq.com/"},
                {"label": "东方财富中证全指日K", "url": "https://quote.eastmoney.com/"},
                {"label": "东方财富业绩报表", "url": "https://data.eastmoney.com/"},
                {"label": "东方财富F10盈利预测", "url": "https://emweb.securities.eastmoney.com/"},
                {"label": "百度股市通历史估值（经AKShare）", "url": "https://gushitong.baidu.com/"},
                {"label": "用户附件：A股板块评分系统技术方案_防追涨优化版_V1.1", "url": None},
            ],
        },
    }


def _load_cache() -> dict | None:
    for path in dict.fromkeys((_PRIMARY_CACHE_FILE, _FALLBACK_CACHE_FILE)):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("schema_version") == _SCHEMA_VERSION and data.get("boards"):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return None


def _save_cache(data: dict) -> None:
    for path in dict.fromkeys((_PRIMARY_CACHE_FILE, _FALLBACK_CACHE_FILE)):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            return
        except OSError:
            continue


def _cache_ttl(data: dict) -> int:
    now = datetime.now(BEIJING)
    if (
        data.get("as_of") == now.date().isoformat()
        and now.weekday() < 5
        and (now.hour, now.minute) >= (9, 15)
        and (now.hour, now.minute) <= (15, 30)
    ):
        return _INTRADAY_TTL
    return _TTL


def get_cached_plate_scores() -> dict | None:
    """立即返回最近一次成功结果，不触发任何外部数据读取。"""
    return _load_cache()


def get_plate_scores(force: bool = False) -> dict:
    cached = _load_cache()
    return cache_runtime.get(
        "plate_scores:v5", _build,
        valid=lambda value: bool(value.get("boards")),
        ttl=_cache_ttl(cached or {}), warm=_load_cache, save=_save_cache, force=force,
    )
