"""申万一级行业综合观察评分。

行业分类与历史月报来自申万宏源研究，当前状态由申万成分分类叠加
a-stock-data 腾讯个股行情按申万方法聚合，并统一在申万 2021 版一级行业
（31 个行业）内计算。申万日频接口只作聚合失败时的备用源：

- 估值赔率：最新 PE/PB 在本行业近 60 个月历史中的分位；
- 盈利景气代理：最新指数点位 / PE 得到的隐含盈利因子，其 3 月、12 月变化；
- 交易确认：最近约 30 个交易日内的换手率、成交额占比分位，经非线性拥挤修正；
- 集中风险：日频换手率与成交额占比同时处于近期高位时，只作扣分项。

评分用于把不同量纲压到同一观察尺度。盈利景气是市场口径代理，并非行业实际
营收或利润统计。
"""

from __future__ import annotations

import json
import io
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import requests
import urllib3

import astock
import cache_runtime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
_PRIMARY_CACHE_FILE = os.path.join(DATA_DIR, "sector_scores.json")
_FALLBACK_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".cache", "sector_scores.json")
_PRIMARY_SNAPSHOT_FILE = os.path.join(DATA_DIR, "sector_score_snapshots.json")
_FALLBACK_SNAPSHOT_FILE = os.path.join(
    os.path.dirname(__file__),
    ".cache",
    "sector_score_snapshots.json",
)
_PRIMARY_CLASSIFICATION_FILE = os.path.join(DATA_DIR, "sw2021_stock_classification.json")
_FALLBACK_CLASSIFICATION_FILE = os.path.join(
    os.path.dirname(__file__),
    ".cache",
    "sw2021_stock_classification.json",
)
_PRIMARY_AGGREGATE_FILE = os.path.join(DATA_DIR, "sector_score_aggregate_snapshots.json")
_FALLBACK_AGGREGATE_FILE = os.path.join(
    os.path.dirname(__file__),
    ".cache",
    "sector_score_aggregate_snapshots.json",
)
_TTL = 60 * 60
_INTRADAY_TTL = 15 * 60
_SCHEMA_VERSION = 6
_MAX_MONTHS = 60
_CLASSIFICATION_START = date(2021, 7, 31)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
}
_DATES_URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/week_month_datetime/"
_REPORT_URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_reports/"
_DAILY_URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/"
_CLASSIFICATION_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"

# 申万 2021 行业分类文件用六位分类代码；前两位映射到一级行业指数。
_SW_LEVEL1 = {
    "11": ("801010", "农林牧渔"),
    "22": ("801030", "基础化工"),
    "23": ("801040", "钢铁"),
    "24": ("801050", "有色金属"),
    "27": ("801080", "电子"),
    "28": ("801880", "汽车"),
    "33": ("801110", "家用电器"),
    "34": ("801120", "食品饮料"),
    "35": ("801130", "纺织服饰"),
    "36": ("801140", "轻工制造"),
    "37": ("801150", "医药生物"),
    "41": ("801160", "公用事业"),
    "42": ("801170", "交通运输"),
    "43": ("801180", "房地产"),
    "45": ("801200", "商贸零售"),
    "46": ("801210", "社会服务"),
    "48": ("801780", "银行"),
    "49": ("801790", "非银金融"),
    "51": ("801230", "综合"),
    "61": ("801710", "建筑材料"),
    "62": ("801720", "建筑装饰"),
    "63": ("801730", "电力设备"),
    "64": ("801890", "机械设备"),
    "65": ("801740", "国防军工"),
    "71": ("801750", "计算机"),
    "72": ("801760", "传媒"),
    "73": ("801770", "通信"),
    "74": ("801950", "煤炭"),
    "75": ("801960", "石油石化"),
    "76": ("801970", "环保"),
    "77": ("801980", "美容护理"),
}


def _finite(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], current: float | None) -> float | None:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    if current is None or len(clean) < 12:
        return None
    return round(sum(value <= current for value in clean) / len(clean) * 100, 1)


def _relative_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return round(max(-200.0, min(200.0, (current / previous - 1) * 100)), 1)


def _rank(rows: list[dict], field: str) -> dict[str, float]:
    pairs = sorted(
        ((row["code"], row[field]) for row in rows if row.get(field) is not None),
        key=lambda item: item[1],
    )
    if not pairs:
        return {}
    if len(pairs) == 1:
        return {pairs[0][0]: 50.0}
    out = {}
    index = 0
    while index < len(pairs):
        end = index + 1
        while end < len(pairs) and pairs[end][1] == pairs[index][1]:
            end += 1
        average_rank = (index + end - 1) / 2
        score = round(average_rank / (len(pairs) - 1) * 100, 1)
        for offset in range(index, end):
            out[pairs[offset][0]] = score
        index = end
    return out


def _weighted(parts: list[tuple[float | None, float]]) -> float | None:
    usable = [(value, weight) for value, weight in parts if value is not None]
    if not usable:
        return None
    total = sum(weight for _, weight in usable)
    return sum(value * weight for value, weight in usable) / total


def _activity_confirmation(level: float | None) -> float | None:
    """交易活跃度在约60分最有确认意义；过低无共识，过高转为拥挤。"""
    if level is None:
        return None
    return level / 60 * 100 if level <= 60 else (100 - level) / 40 * 100


def _request_json(url: str, params: dict) -> dict:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.get(
                url,
                params=params,
                headers=_HEADERS,
                verify=False,
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("code")) != "200":
                raise RuntimeError(payload.get("message") or "申万接口返回异常")
            return payload
        except Exception as error:
            last_error = error
            if attempt == 0:
                time.sleep(0.25)
    raise RuntimeError(f"申万数据请求失败：{last_error}") from last_error


def _latest_json(paths: tuple[str, ...], schema_version: int) -> dict | None:
    candidates = []
    for path in dict.fromkeys(paths):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("schema_version") == schema_version:
                candidates.append(payload)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: str(item.get("fetched_at") or item.get("updated_at") or ""),
    )


def _fetch_sw_classification() -> dict:
    """下载申万 2021 个股分类文件，并保留当前沪深 A 股的一级行业映射。"""
    import pandas as pd

    response = requests.get(
        _CLASSIFICATION_URL,
        headers={**_HEADERS, "Referer": "https://www.swsresearch.com/"},
        verify=False,
        timeout=30,
    )
    response.raise_for_status()
    frame = pd.read_excel(io.BytesIO(response.content))
    required = {"股票代码", "计入日期", "行业代码", "更新日期"}
    if not required.issubset(frame.columns):
        raise ValueError("申万行业分类文件字段不完整")

    frame["股票代码"] = (
        frame["股票代码"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )
    frame["计入日期"] = pd.to_datetime(frame["计入日期"], errors="coerce")
    frame["更新日期"] = pd.to_datetime(frame["更新日期"], errors="coerce")
    today = datetime.now(BEIJING).date()
    frame = (
        frame[frame["计入日期"].dt.date <= today]
        .sort_values("计入日期")
        .drop_duplicates("股票代码", keep="last")
    )
    frame["一级前缀"] = frame["行业代码"].astype(str).str.zfill(6).str[:2]

    members: dict[str, list[str]] = {code: [] for code, _ in _SW_LEVEL1.values()}
    for stock_code, level1_prefix in zip(frame["股票代码"], frame["一级前缀"]):
        stock_code = str(stock_code)
        level1 = _SW_LEVEL1.get(str(level1_prefix))
        # 申万 A 股行业指数不含北交所；沪深 A 股代码仅保留 0/3/6 开头。
        if level1 and stock_code.startswith(("0", "3", "6")):
            members[level1[0]].append(stock_code)

    if len([codes for codes in members.values() if len(codes) >= 5]) != 31:
        raise ValueError("申万一级行业成分映射不完整")
    source_as_of = frame["更新日期"].max()
    return {
        "schema_version": 1,
        "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        "source_as_of": source_as_of.strftime("%Y-%m-%d %H:%M") if pd.notna(source_as_of) else None,
        "members": {code: sorted(set(codes)) for code, codes in members.items()},
    }


def _load_sw_classification() -> tuple[dict, str | None]:
    cached = _latest_json(
        (_PRIMARY_CLASSIFICATION_FILE, _FALLBACK_CLASSIFICATION_FILE),
        schema_version=1,
    )
    today = datetime.now(BEIJING).date().isoformat()
    if cached and str(cached.get("fetched_at") or "").startswith(today):
        return cached, None
    try:
        payload = _fetch_sw_classification()
        _write_json_with_fallback(
            payload,
            _PRIMARY_CLASSIFICATION_FILE,
            _FALLBACK_CLASSIFICATION_FILE,
        )
        return payload, None
    except Exception as error:
        if cached:
            return cached, str(error)
        raise


def _batched_tencent_quotes(codes: list[str], batch_size: int = 60) -> dict[str, dict]:
    quotes: dict[str, dict] = {}
    for offset in range(0, len(codes), batch_size):
        batch = codes[offset:offset + batch_size]
        quotes.update(astock.tencent_quote(batch))
    return quotes


def _positive_harmonic(rows: list[dict], field: str) -> float | None:
    usable = [
        row for row in rows
        if (row.get(field) or 0) > 0 and (row.get("mcap_yi") or 0) > 0
    ]
    denominator = sum(row["mcap_yi"] / row[field] for row in usable)
    if not usable or denominator <= 0:
        return None
    return sum(row["mcap_yi"] for row in usable) / denominator


def _aggregate_tencent_snapshot(classification: dict) -> tuple[str, dict[str, dict], dict]:
    """按申万流通市值加权方法聚合腾讯个股行情。"""
    members: dict[str, list[str]] = classification["members"]
    codes = sorted({code for industry_codes in members.values() for code in industry_codes})
    quotes = _batched_tencent_quotes(codes)
    quote_dates = [
        quote.get("quote_date")
        for quote in quotes.values()
        if quote.get("quote_date")
    ]
    if not quote_dates:
        raise RuntimeError("腾讯个股行情未返回有效日期")
    quote_date = max(set(quote_dates), key=quote_dates.count)
    quote_times = [
        quote.get("quote_time")
        for quote in quotes.values()
        if quote.get("quote_date") == quote_date and quote.get("quote_time")
    ]
    quote_time = max(quote_times) if quote_times else None

    eligible_quotes = {
        code: quote
        for code, quote in quotes.items()
        if quote.get("quote_date") == quote_date
        and (quote.get("price") or 0) > 0
        and (quote.get("last_close") or 0) > 0
        and (quote.get("mcap_yi") or 0) > 0
        and not str(quote.get("name") or "").startswith("退市")
    }
    total_amount = sum(
        max(0.0, quote.get("amount_wan") or 0)
        for quote in eligible_quotes.values()
    )
    rows: dict[str, dict] = {}
    total_members = 0
    total_quoted = 0
    industry_coverage: dict[str, dict] = {}

    for level_prefix, (industry_code, industry_name) in _SW_LEVEL1.items():
        del level_prefix
        industry_members = members.get(industry_code, [])
        industry_quotes = [
            eligible_quotes[code]
            for code in industry_members
            if code in eligible_quotes
        ]
        total_members += len(industry_members)
        total_quoted += len(industry_quotes)
        if len(industry_quotes) < 5:
            continue

        def float_cap(quote: dict) -> float:
            current = quote.get("float_mcap_yi") or 0
            total = quote.get("mcap_yi") or 0
            # 个别代码变更期间腾讯会短暂交换总/流通市值；流通市值不得高于总市值。
            return min(current, total) if current > 0 and total > 0 else max(current, total)

        current_float_cap = sum(float_cap(quote) for quote in industry_quotes)
        previous_float_cap = sum(
            float_cap(quote) / quote["price"] * quote["last_close"]
            for quote in industry_quotes
        )
        industry_amount = sum(
            max(0.0, quote.get("amount_wan") or 0)
            for quote in industry_quotes
        )
        # 用流通市值加权个股换手率；按股数相加会让低价股因拆股单位不同而获得过高权重。
        turnover_rate = (
            sum(
                max(0.0, quote.get("turnover_pct") or 0)
                * float_cap(quote)
                for quote in industry_quotes
            )
            / current_float_cap
            if current_float_cap > 0
            else None
        )
        pe = _positive_harmonic(industry_quotes, "pe_static")
        pb = _positive_harmonic(industry_quotes, "pb")
        return_pct = (
            (current_float_cap / previous_float_cap - 1) * 100
            if previous_float_cap > 0
            else None
        )
        rows[industry_code] = {
            "code": industry_code,
            "name": industry_name,
            # 用流通市值作连续尺度；景气代理据此与申万历史流通市值/PE 对比。
            "close": current_float_cap,
            "return_pct": return_pct,
            "turnover_rate": turnover_rate,
            "pe": pe,
            "pb": pb,
            "turnover_share": industry_amount / total_amount * 100 if total_amount > 0 else None,
            "float_market_cap": current_float_cap,
            "dividend_yield": None,
            "component_count": len(industry_members),
            "quoted_component_count": len(industry_quotes),
        }
        industry_coverage[industry_code] = {
            "members": len(industry_members),
            "quoted": len(industry_quotes),
            "coverage_pct": round(len(industry_quotes) / len(industry_members) * 100, 1),
        }

    if len(rows) < 25:
        raise RuntimeError(f"腾讯聚合行业不完整：仅 {len(rows)} 个行业")
    coverage_pct = total_quoted / total_members * 100 if total_members else 0
    if coverage_pct < 90:
        raise RuntimeError(f"腾讯成分股行情覆盖不足：{coverage_pct:.1f}%")
    return quote_date, rows, {
        "source": "tencent_constituent_aggregate",
        "source_label": "申万成分分类 × 腾讯个股行情聚合",
        "quote_time": quote_time,
        "classification_as_of": classification.get("source_as_of"),
        "component_count": total_members,
        "quoted_component_count": total_quoted,
        "coverage_pct": round(coverage_pct, 1),
        "industry_coverage": industry_coverage,
        "method": "成分股涨幅按申万流通市值方法聚合；PE/PB以最近申万官方值为锚并随聚合市值涨幅滚动",
    }


def _anchor_aggregate_scale(
    rows: dict[str, dict],
    anchor_rows: dict[str, dict],
) -> dict[str, dict]:
    """把腾讯原始流通市值锚定到最近申万指数尺度，保持跨期可比。"""
    anchored = {}
    for code, row in rows.items():
        anchor = anchor_rows.get(code)
        change = (row.get("return_pct") or 0) / 100
        anchor_close = anchor.get("close") if anchor else None
        anchor_float_cap = anchor.get("float_market_cap") if anchor else None
        anchored[code] = {
            **row,
            "raw_constituent_pe": row.get("pe"),
            "raw_constituent_pb": row.get("pb"),
            "close": (
                anchor_close * (1 + change)
                if anchor_close is not None and anchor_close > 0
                else row.get("close")
            ),
            "float_market_cap": (
                anchor_float_cap * (1 + change)
                if anchor_float_cap is not None and anchor_float_cap > 0
                else row.get("float_market_cap")
            ),
            "pe": (
                anchor["pe"] * (1 + change)
                if anchor and (anchor.get("pe") or 0) > 0
                else row.get("pe")
            ),
            "pb": (
                anchor["pb"] * (1 + change)
                if anchor and (anchor.get("pb") or 0) > 0
                else row.get("pb")
            ),
        }
    return anchored


def _row_from_sw(item: dict) -> dict:
    return {
        "code": str(item.get("swindexcode") or "").strip(),
        "name": str(item.get("swindexname") or "").strip(),
        "close": _finite(item.get("closeindex")),
        "return_pct": _finite(item.get("markup")),
        "turnover_rate": _finite(item.get("turnoverrate")),
        "pe": _finite(item.get("pe")),
        "pb": _finite(item.get("pb")),
        "turnover_share": _finite(item.get("bargainsumrate")),
        "float_market_cap": _finite(item.get("negotiablessharesum1")),
        "dividend_yield": _finite(item.get("dp")),
    }


def _snapshot_from_results(results: list[dict]) -> tuple[str, dict[str, dict]]:
    rows = [_row_from_sw(item) for item in results]
    rows = [row for row in rows if row["code"] and row["name"]]
    if len(rows) < 25:
        raise ValueError(f"申万一级行业快照不完整：仅 {len(rows)} 个行业")
    raw_day = str(results[0].get("bargaindate") or "")[:10]
    datetime.strptime(raw_day, "%Y-%m-%d")
    return raw_day, {row["code"]: row for row in rows}


def _fetch_month_dates() -> list[date]:
    payload = _request_json(_DATES_URL, {"type": "MONTH"})
    dates = []
    for item in payload.get("data") or []:
        try:
            day = datetime.strptime(str(item.get("bargaindate"))[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if day >= _CLASSIFICATION_START:
            dates.append(day)
    return sorted(set(dates))[-_MAX_MONTHS:]


def _fetch_month(day: date) -> tuple[str, dict[str, dict]]:
    payload = _request_json(
        _REPORT_URL,
        {
            "page": "1",
            "page_size": "50",
            "index_type": "一级行业",
            "bargaindate": day.isoformat(),
            "type": "MONTH",
            "swindexcode": "all",
        },
    )
    data = payload.get("data") or {}
    return _snapshot_from_results(data.get("results") or [])


def _fetch_daily_window() -> list[tuple[str, dict[str, dict]]]:
    """获取最近约 30 个交易日的申万一级行业日频快照。"""
    end = datetime.now(BEIJING).date()
    start = end - timedelta(days=45)
    payload = _request_json(
        _DAILY_URL,
        {
            "page": "1",
            "page_size": "2000",
            "index_type": "一级行业",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "type": "DAY",
            "swindexcode": "all",
        },
    )
    grouped: dict[str, list[dict]] = {}
    for item in (payload.get("data") or {}).get("results") or []:
        day = str(item.get("bargaindate") or "")[:10]
        if day:
            grouped.setdefault(day, []).append(item)
    snapshots = []
    for day, results in grouped.items():
        try:
            snapshots.append(_snapshot_from_results(results))
        except (ValueError, TypeError):
            continue
    return sorted(snapshots)


def _closest_history(
    history: list[tuple[date, dict]],
    target: date,
    max_gap_days: int,
) -> dict | None:
    if not history:
        return None
    day, row = min(history, key=lambda item: abs((item[0] - target).days))
    return row if abs((day - target).days) <= max_gap_days else None


def build_scores_from_snapshots(
    snapshots: list[tuple[str, dict[str, dict]]],
    daily_snapshots: list[tuple[str, dict[str, dict]]] | None = None,
) -> dict:
    """以月频历史为锚、最新日频数据为当前值计算评分。"""
    monthly = sorted(
        (datetime.strptime(day, "%Y-%m-%d").date(), rows)
        for day, rows in snapshots
    )
    daily = sorted(
        (datetime.strptime(day, "%Y-%m-%d").date(), rows)
        for day, rows in (daily_snapshots or [])
    )
    if len(monthly) < 12:
        raise ValueError("申万历史月度快照不足 12 期")
    current_frequency = "daily" if daily else "monthly"
    current_date, current_rows = daily[-1] if daily else monthly[-1]

    raw: list[dict] = []
    for code, current in current_rows.items():
        monthly_history = [(day, rows[code]) for day, rows in monthly if code in rows]
        activity_history = (
            [(day, rows[code]) for day, rows in daily if code in rows]
            if daily
            else monthly_history
        )
        pe_history = [
            row["pe"] for _, row in monthly_history
            if row.get("pe") is not None and row["pe"] > 0
        ]
        pb_history = [
            row["pb"] for _, row in monthly_history
            if row.get("pb") is not None and row["pb"] > 0
        ]
        turnover_history = [
            row["turnover_rate"] for _, row in activity_history
            if row.get("turnover_rate") is not None and row["turnover_rate"] >= 0
        ]
        share_history = [
            row["turnover_share"] for _, row in activity_history
            if row.get("turnover_share") is not None and row["turnover_share"] >= 0
        ]
        lag_history = [(day, row) for day, row in monthly_history if day < current_date]
        previous_3m = _closest_history(
            lag_history,
            current_date - timedelta(days=92),
            max_gap_days=46,
        )
        previous_year = _closest_history(
            lag_history,
            current_date - timedelta(days=365),
            max_gap_days=48,
        )

        pe = current.get("pe")
        pb = current.get("pb")
        pe_percentile = _percentile(pe_history, pe if pe and pe > 0 else None)
        pb_percentile = _percentile(pb_history, pb if pb and pb > 0 else None)
        valuation_score = _weighted([
            (100 - pe_percentile if pe_percentile is not None else None, 0.7),
            (100 - pb_percentile if pb_percentile is not None else None, 0.3),
        ])

        def earnings_proxy(row: dict | None) -> float | None:
            if not row:
                return None
            scale = row.get("close")
            row_pe = row.get("pe")
            if scale is None or row_pe is None or row_pe <= 0:
                return None
            return scale / row_pe

        current_earnings = earnings_proxy(current)
        raw.append({
            **current,
            "valuation_score": round(valuation_score, 1) if valuation_score is not None else None,
            "pe_percentile": pe_percentile,
            "pb_percentile": pb_percentile,
            "earnings_3m": _relative_change(current_earnings, earnings_proxy(previous_3m)),
            "earnings_yoy": _relative_change(current_earnings, earnings_proxy(previous_year)),
            "turnover_rate_percentile": _percentile(
                turnover_history,
                current.get("turnover_rate"),
            ),
            "turnover_share_percentile": _percentile(
                share_history,
                current.get("turnover_share"),
            ),
            "monthly_sample_count": len(monthly_history),
            "activity_sample_count": len(activity_history),
        })

    earnings_3m_rank = _rank(raw, "earnings_3m")
    earnings_yoy_rank = _rank(raw, "earnings_yoy")
    rows = []
    for row in raw:
        code = row["code"]
        prosperity = _weighted([
            (earnings_yoy_rank.get(code), 0.7),
            (earnings_3m_rank.get(code), 0.3),
        ])
        activity_level = _weighted([
            (row.get("turnover_rate_percentile"), 0.5),
            (row.get("turnover_share_percentile"), 0.5),
        ])
        attention = _activity_confirmation(activity_level)
        crowding = activity_level
        base = _weighted([
            (row.get("valuation_score"), 0.3),
            (prosperity, 0.4),
            (attention, 0.3),
        ])
        penalty = max(0.0, ((crowding or 0) - 80))
        score = (
            max(0.0, min(100.0, (base or 0) - penalty))
            if base is not None
            else None
        )
        if crowding is not None and crowding >= 80:
            phase = "集中风险"
        elif score is not None and score >= 70:
            phase = "综合占优"
        elif (
            row.get("valuation_score") is not None
            and row["valuation_score"] >= 65
            and (prosperity or 0) < 50
        ):
            phase = "赔率观察"
        elif score is not None and score < 40:
            phase = "相对偏弱"
        else:
            phase = "中性观察"

        missing = []
        if row.get("valuation_score") is None:
            missing.append("估值")
        if prosperity is None:
            missing.append("盈利景气")
        if attention is None:
            missing.append("交易确认")
        rows.append({
            "code": code,
            "name": row["name"],
            "score": round(score, 1) if score is not None else None,
            "phase": phase,
            "latest_return": row.get("return_pct"),
            "valuation": {
                "score": row.get("valuation_score"),
                "pe": row.get("pe"),
                "pe_percentile": row.get("pe_percentile"),
                "pb": row.get("pb"),
                "pb_percentile": row.get("pb_percentile"),
                "history_samples": row["monthly_sample_count"],
            },
            "prosperity": {
                "score": round(prosperity, 1) if prosperity is not None else None,
                "earnings_3m": row.get("earnings_3m"),
                "earnings_yoy": row.get("earnings_yoy"),
            },
            "attention": {
                "score": round(attention, 1) if attention is not None else None,
                "turnover_rate": row.get("turnover_rate"),
                "turnover_rate_percentile": row.get("turnover_rate_percentile"),
                "turnover_share": row.get("turnover_share"),
                "turnover_share_percentile": row.get("turnover_share_percentile"),
                "activity_level": round(activity_level, 1) if activity_level is not None else None,
                "daily_history_samples": row["activity_sample_count"],
            },
            "crowding": {
                "risk": round(crowding, 1) if crowding is not None else None,
                "penalty": round(penalty, 1),
            },
            "data_quality": {
                "history_samples": row["monthly_sample_count"],
                "missing": missing,
            },
        })

    rows.sort(key=lambda item: (item["score"] is None, -(item["score"] or 0), item["name"]))
    return {
        "schema_version": _SCHEMA_VERSION,
        "as_of": current_date.isoformat(),
        "current_frequency": current_frequency,
        "monthly_as_of": monthly[-1][0].isoformat(),
        "daily_history_samples": len(daily),
        "history_start": monthly[0][0].isoformat(),
        "history_samples": len(monthly),
        "industries": rows,
        "methodology": {
            "classification": "申万一级行业（2021版，31个行业）",
            "frequency": "当前值由申万成分分类叠加腾讯个股行情聚合；历史估值与景气锚使用申万月报；申万日频仅作备用",
            "weights": {"valuation": 30, "prosperity": 40, "attention": 30},
            "penalty": "拥挤风险超过 80 分后，每超 1 分扣 1 分，最多扣 20 分",
            "definitions": [
                "估值赔率：最近申万官方行业 PE/PB 按腾讯成分股流通市值涨幅滚动，并对比近 60 个月申万历史；PE 分位占 70%，PB 分位占 30%。",
                "盈利景气代理：用行业指数点位 / PE 构造隐含盈利因子，分别与约 3 个月前、12 个月前的申万月报锚点比较并横向排名。",
                "交易确认：换手率与行业成交额占比历史分位各半；活跃度约60分时确认度最高，过低表示共识不足，过高转为拥挤而不再正向加分。",
                "拥挤风险：与交易确认共享原始数据但采用单调风险刻度，超过80分后每超1分扣1分。",
            ],
            "sources": [
                {
                    "label": "申万行业分类标准 2021 版说明",
                    "url": "https://wxweb.swsresearch.com/swsreport/2021_08/328340.pdf",
                },
                {
                    "label": "申万宏源研究·指数分析",
                    "url": "https://www.swsresearch.com/institute_sw/allIndex/analysisIndex",
                },
                {
                    "label": "腾讯财经个股行情（经 a-stock-data 聚合）",
                    "url": "https://stockapp.finance.qq.com/",
                },
                {"label": "用户附件：A股ETF轮动识别方法论", "url": None},
                {
                    "label": "Moskowitz & Grinblatt (1999), Do Industries Explain Momentum?",
                    "url": "https://doi.org/10.1111/0022-1082.00146",
                },
                {
                    "label": "Da, Engelberg & Gao (2011), In Search of Attention",
                    "url": "https://doi.org/10.1111/j.1540-6261.2011.01679.x",
                },
                {
                    "label": "Kinlaw, Kritzman & Turkington (2019), Crowded Trades",
                    "url": "https://doi.org/10.3905/jpm.2019.45.5.046",
                },
            ],
        },
    }


def _build() -> dict:
    dates = _fetch_month_dates()
    if len(dates) < 12:
        raise RuntimeError(f"申万可用月报不足：仅 {len(dates)} 期")

    wanted = {day.isoformat() for day in dates}
    snapshot_map = {
        day: rows
        for day, rows in _load_snapshot_cache().items()
        if day in wanted
    }
    missing_dates = [day for day in dates if day.isoformat() not in snapshot_map]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_month, day): day for day in missing_dates}
        for future in as_completed(futures):
            try:
                day, rows = future.result()
                snapshot_map[day] = rows
            except Exception:
                continue
    snapshots = sorted(snapshot_map.items())
    if len(snapshots) < 24:
        raise RuntimeError(f"申万历史快照获取不足：仅成功 {len(snapshots)} 期")
    _save_snapshot_cache(snapshot_map)

    sws_daily_snapshots = []
    daily_error = None
    try:
        sws_daily_snapshots = _fetch_daily_window()
        if len(sws_daily_snapshots) < 12:
            daily_error = f"申万备用日频样本不足：仅 {len(sws_daily_snapshots)} 个交易日"
            sws_daily_snapshots = []
    except Exception as error:
        daily_error = str(error)

    classification_error = None
    aggregate_error = None
    aggregate_day = None
    aggregate_rows = None
    aggregate_meta = None
    try:
        classification, classification_error = _load_sw_classification()
        aggregate_day, aggregate_rows, aggregate_meta = _aggregate_tencent_snapshot(classification)
        anchor_day, anchor_rows = (
            sws_daily_snapshots[-1]
            if sws_daily_snapshots
            else snapshots[-1]
        )
        aggregate_rows = _anchor_aggregate_scale(aggregate_rows, anchor_rows)
        aggregate_meta["official_anchor_as_of"] = anchor_day
        aggregate_meta["method"] += f"；指数连续尺度锚定申万 {anchor_day}"
    except Exception as error:
        aggregate_error = str(error)

    # 申万日频只负责为交易确认指标提供历史启动窗口；腾讯聚合快照优先覆盖同日数据。
    daily_map = {day: rows for day, rows in sws_daily_snapshots}
    aggregate_cache = _load_aggregate_snapshots()
    if aggregate_day and aggregate_rows and aggregate_meta:
        aggregate_cache[aggregate_day] = {
            "rows": aggregate_rows,
            "meta": aggregate_meta,
        }
        _save_aggregate_snapshots(aggregate_cache)
    for day, item in aggregate_cache.items():
        rows = item.get("rows")
        if isinstance(rows, dict):
            daily_map[day] = rows
    daily_snapshots = sorted(daily_map.items())

    data = build_scores_from_snapshots(snapshots, daily_snapshots)
    using_aggregate = bool(
        aggregate_day
        and aggregate_meta
        and data["as_of"] == aggregate_day
    )
    if using_aggregate:
        data.update({
            "current_source": aggregate_meta["source"],
            "current_source_label": aggregate_meta["source_label"],
            "quote_time": aggregate_meta.get("quote_time"),
            "classification_as_of": aggregate_meta.get("classification_as_of"),
            "official_anchor_as_of": aggregate_meta.get("official_anchor_as_of"),
            "component_count": aggregate_meta.get("component_count"),
            "quoted_component_count": aggregate_meta.get("quoted_component_count"),
            "coverage_pct": aggregate_meta.get("coverage_pct"),
            "calculation_method": aggregate_meta.get("method"),
            "is_intraday": (
                aggregate_day == datetime.now(BEIJING).date().isoformat()
                and bool(aggregate_meta.get("quote_time"))
                and aggregate_meta["quote_time"] < "15:05:00"
            ),
        })
    elif data["current_frequency"] == "daily":
        data.update({
            "current_source": "sws_daily_fallback",
            "current_source_label": "申万日频备用源",
            "quote_time": None,
            "classification_as_of": None,
            "official_anchor_as_of": data["as_of"],
            "component_count": None,
            "quoted_component_count": None,
            "coverage_pct": None,
            "calculation_method": "申万宏源研究指数分析日频",
            "is_intraday": False,
        })
    else:
        data.update({
            "current_source": "sws_monthly_fallback",
            "current_source_label": "申万月报备用源",
            "quote_time": None,
            "classification_as_of": None,
            "official_anchor_as_of": data["as_of"],
            "component_count": None,
            "quoted_component_count": None,
            "coverage_pct": None,
            "calculation_method": "申万宏源研究指数分析月报",
            "is_intraday": False,
        })
    data["history_requested"] = len(dates)
    data["history_partial"] = len(snapshots) < len(dates)
    data["daily_error"] = daily_error
    data["classification_error"] = classification_error
    data["aggregate_error"] = aggregate_error
    data["generated_at"] = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
    data["stale"] = False
    return data


def _load_snapshot_cache() -> dict[str, dict[str, dict]]:
    merged: dict[str, dict[str, dict]] = {}
    for path in dict.fromkeys((_PRIMARY_SNAPSHOT_FILE, _FALLBACK_SNAPSHOT_FILE)):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("schema_version") != 1:
                continue
            for day, rows in (payload.get("snapshots") or {}).items():
                if isinstance(rows, dict):
                    merged[day] = rows
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return merged


def _write_json_with_fallback(data: dict, primary: str, fallback: str) -> None:
    last_error: OSError | None = None
    for path in dict.fromkeys((primary, fallback)):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, path)
            return
        except OSError as error:
            last_error = error
    raise last_error or OSError("缓存写入失败")


def _save_snapshot_cache(snapshots: dict[str, dict[str, dict]]) -> None:
    _write_json_with_fallback(
        {
            "schema_version": 1,
            "updated_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
            "snapshots": snapshots,
        },
        _PRIMARY_SNAPSHOT_FILE,
        _FALLBACK_SNAPSHOT_FILE,
    )


def _load_aggregate_snapshots() -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for path in dict.fromkeys((_PRIMARY_AGGREGATE_FILE, _FALLBACK_AGGREGATE_FILE)):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("schema_version") != 1:
                continue
            for day, item in (payload.get("snapshots") or {}).items():
                if isinstance(item, dict) and isinstance(item.get("rows"), dict):
                    merged[day] = item
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return merged


def _save_aggregate_snapshots(snapshots: dict[str, dict]) -> None:
    cutoff = datetime.now(BEIJING).date() - timedelta(days=120)
    retained = {
        day: item
        for day, item in snapshots.items()
        if datetime.strptime(day, "%Y-%m-%d").date() >= cutoff
    }
    _write_json_with_fallback(
        {
            "schema_version": 1,
            "updated_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
            "snapshots": retained,
        },
        _PRIMARY_AGGREGATE_FILE,
        _FALLBACK_AGGREGATE_FILE,
    )


def _load_cache() -> dict | None:
    candidates = []
    for path in dict.fromkeys((_PRIMARY_CACHE_FILE, _FALLBACK_CACHE_FILE)):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("schema_version") == _SCHEMA_VERSION:
                candidates.append(data)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: str(item.get("generated_at") or ""))


def _save_cache(data: dict) -> None:
    _write_json_with_fallback(data, _PRIMARY_CACHE_FILE, _FALLBACK_CACHE_FILE)


def _cache_ttl(data: dict) -> int:
    now = datetime.now(BEIJING)
    if (
        data.get("current_source") == "tencent_constituent_aggregate"
        and data.get("as_of") == now.date().isoformat()
        and now.weekday() < 5
        and (now.hour, now.minute) >= (9, 15)
        and (now.hour, now.minute) <= (15, 30)
    ):
        return _INTRADAY_TTL
    return _TTL


def get_cached_sector_scores() -> dict | None:
    """立即返回最近一次成功结果，不触发任何外部数据读取。"""
    return _load_cache()


def get_sector_scores(force: bool = False) -> dict:
    cached = _load_cache()
    return cache_runtime.get(
        "sector_scores:v6", _build,
        valid=lambda value: bool(value.get("industries")),
        ttl=_cache_ttl(cached or {}), warm=_load_cache, save=_save_cache, force=force,
    )
