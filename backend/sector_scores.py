"""申万一级行业综合观察评分。

全部指标来自申万宏源研究“指数分析”，并统一在申万 2021 版一级行业
（31 个行业）内计算。最新交易日负责当前状态，月报负责长期锚：

- 估值赔率：最新 PE/PB 在本行业近 60 个月历史中的分位；
- 盈利景气代理：最新指数点位 / PE 得到的隐含盈利因子，其 3 月、12 月变化；
- 资本活跃度：最近约 30 个交易日内的换手率、成交额占比分位；
- 集中风险：日频换手率与成交额占比同时处于近期高位时，只作扣分项。

评分用于把不同量纲压到同一观察尺度。盈利景气是市场口径代理，并非行业实际
营收或利润统计。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import requests
import urllib3

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
_TTL = 60 * 60
_LOCK = threading.Lock()
_SCHEMA_VERSION = 4
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
    return {
        code: round(index / (len(pairs) - 1) * 100, 1)
        for index, (code, _) in enumerate(pairs)
    }


def _weighted(parts: list[tuple[float | None, float]]) -> float | None:
    usable = [(value, weight) for value, weight in parts if value is not None]
    if not usable:
        return None
    total = sum(weight for _, weight in usable)
    return sum(value * weight for value, weight in usable) / total


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
            close, row_pe = row.get("close"), row.get("pe")
            if close is None or row_pe is None or row_pe <= 0:
                return None
            return close / row_pe

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
    turnover_share_rank = _rank(raw, "turnover_share")

    rows = []
    for row in raw:
        code = row["code"]
        prosperity = _weighted([
            (earnings_yoy_rank.get(code), 0.7),
            (earnings_3m_rank.get(code), 0.3),
        ])
        attention = _weighted([
            (row.get("turnover_rate_percentile"), 0.45),
            (row.get("turnover_share_percentile"), 0.35),
            (turnover_share_rank.get(code), 0.2),
        ])
        crowding = _weighted([
            (row.get("turnover_rate_percentile"), 0.5),
            (row.get("turnover_share_percentile"), 0.5),
        ])
        base = _weighted([
            (row.get("valuation_score"), 0.3),
            (prosperity, 0.4),
            (attention, 0.3),
        ])
        penalty = max(0.0, ((crowding or 0) - 80) * 0.5)
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
            missing.append("资本活跃")
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
            "frequency": "当前值取最新完整交易日；历史估值与景气锚使用月报；资本活跃使用近 45 天日频数据",
            "weights": {"valuation": 30, "prosperity": 40, "attention": 30},
            "penalty": "集中风险超过 80 分后，每超 1 分扣 0.5 分",
            "definitions": [
                "估值赔率：最新交易日 PE/PB 对比近 60 个月历史；PE 分位占 70%，PB 分位占 30%。",
                "盈利景气代理：用最新交易日指数点位 / PE 构造隐含盈利因子，对比 3 月、12 月前月报并横向排名。",
                "资本活跃度：日换手率历史分位占 45%，日成交额占比历史分位占 35%，当日成交额占比行业排名占 20%。",
                "集中风险：日换手率与成交额占比同时处于近期高位时扣分，不把高热度直接当作优势。",
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

    daily_snapshots = []
    daily_error = None
    try:
        daily_snapshots = _fetch_daily_window()
        if len(daily_snapshots) < 12:
            daily_error = f"日频样本不足：仅 {len(daily_snapshots)} 个交易日"
            daily_snapshots = []
    except Exception as error:
        daily_error = str(error)

    data = build_scores_from_snapshots(snapshots, daily_snapshots)
    data["history_requested"] = len(dates)
    data["history_partial"] = len(snapshots) < len(dates)
    data["daily_error"] = daily_error
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


def get_sector_scores(force: bool = False) -> dict:
    with _LOCK:
        cached = _load_cache()
        if not force and cached:
            generated = cached.get("generated_at")
            try:
                generated_at = datetime.strptime(
                    generated,
                    "%Y-%m-%d %H:%M",
                ).replace(tzinfo=BEIJING)
                age = time.time() - generated_at.timestamp()
            except (TypeError, ValueError):
                age = _TTL + 1
            if age < _TTL:
                return cached
        try:
            data = _build()
            _save_cache(data)
            return data
        except Exception as error:
            if cached:
                return {**cached, "stale": True, "refresh_error": str(error)}
            raise
