"""申万二级行业指标与一级映射（申万 2021 版，131 个二级行业）。

数据全部来自申万宏源研究指数分析官方接口（与一级行业评分同一来源）：

- 当前值：最近一个交易日的二级行业日频快照（涨跌幅、换手率、成交额占比、
  PE/PB、股息率、流通市值）；
- 历史锚：最近约 60 个月二级行业月报，用于 PE/PB/换手率/成交占比分位；
- 一级映射：二级行业指数代码（801xx0）前两位对应一级行业六位分类前缀，
  复用 sector_scores 的申万 2021 映射表，比代码区间启发式更严格。

与一级评分页不同，这里不另造一套综合评分权重，只提供同口径指标和
131 个二级行业内的横向排名百分位，供一级行业行内展开查看。
"""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import requests
import urllib3
import cache_runtime

from sector_scores import (
    _HEADERS,
    _activity_confirmation,
    _percentile,
    _relative_change,
    _request_json,
    _row_from_sw,
    _SW_LEVEL1,
    _weighted,
    _write_json_with_fallback,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
_PRIMARY_CACHE_FILE = os.path.join(DATA_DIR, "sw_level2_scores.json")
_FALLBACK_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".cache", "sw_level2_scores.json")
_PRIMARY_SNAPSHOT_FILE = os.path.join(DATA_DIR, "sw_level2_snapshots.json")
_FALLBACK_SNAPSHOT_FILE = os.path.join(
    os.path.dirname(__file__),
    ".cache",
    "sw_level2_snapshots.json",
)
_TTL = 60 * 60
_INTRADAY_TTL = 60 * 60
_SCHEMA_VERSION = 2
_SNAPSHOT_SCHEMA_VERSION = 1
_MAX_MONTHS = 60
_MIN_MONTHLY_ROWS = 100  # 二级行业月报正常为 131 行，允许少量新设/缺失
_CLASSIFICATION_START = date(2021, 7, 31)

_DATES_URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/week_month_datetime/"
_REPORT_URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_reports/"
_DAILY_URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/"
_INDEX_TYPE = "二级行业"



# 二级指数代码前 5 位段 → 一级行业指数代码（申万 2021 版，131 个二级行业）。
# 多数段与一级指数代码前 5 位一致（80101→801010 农林牧渔）；跨段归属的
# 例外已按申万官方分类核对：机械设备二级在 80107 段（一级指数 801890）、
# 汽车零部件/乘用车等在 80109 段（一级 801880）、计算机二级在 80104x 之外的
# 80104 段与钢铁共用规则不适用，直接列段。数据来自申万指数分析接口全量
# 二级行业清单逐段核对。
_SW_LEVEL1_SEGMENT = {
    "80101": "801010",  # 农林牧渔
    "80103": "801030",  # 基础化工
    "80104": "801040",  # 钢铁
    "80105": "801050",  # 有色金属
    "80107": "801890",  # 机械设备（通用/专用/轨交/工程机械/自动化设备）
    "80108": "801080",  # 电子
    "80109": "801880",  # 汽车（零部件/乘用车/商用车/汽车服务）
    "80110": "801750",  # 计算机（计算机设备/IT服务/软件开发；通信设备按申万归计算机指数段）
    "80111": "801110",  # 家用电器
    "80112": "801120",  # 食品饮料
    "80113": "801130",  # 纺织服饰
    "80114": "801140",  # 轻工制造
    "80115": "801150",  # 医药生物
    "80116": "801160",  # 公用事业
    "80117": "801170",  # 交通运输（物流/铁路公路）
    "80118": "801180",  # 房地产
    "80119": "801790",  # 非银金融（证券/保险/多元金融）
    "80120": "801200",  # 商贸零售
    "80121": "801210",  # 社会服务
    "80122": "801770",  # 通信（通信服务）
    "80123": "801230",  # 综合
    "80171": "801710",  # 建筑材料
    "80172": "801720",  # 建筑装饰
    "80173": "801730",  # 电力设备
    "80174": "801740",  # 国防军工
    "80176": "801760",  # 传媒
    "80178": "801780",  # 银行
    "80188": "801880",  # 汽车（摩托车及其他）
    "80195": "801950",  # 煤炭
    "80196": "801960",  # 石油石化
    "80197": "801970",  # 环保
    "80198": "801980",  # 美容护理
    "80199": "801170",  # 交通运输（航空机场/航运港口）；旅游及景区/教育属社会服务
}


def _level1_code(level2_code: str, name: str = "") -> str | None:
    """二级行业指数代码 → 一级行业指数代码（段映射 + 少量跨段例外）。"""
    # 80199 段跨三个一级行业：航空机场/航运港口归交通运输，旅游及景区/教育
    # 归社会服务，电视广播归传媒。
    if level2_code.startswith("80199"):
        if name in ("旅游及景区", "教育"):
            return "801210"
        if name.startswith("电视广播"):
            return "801760"
        return "801170"
    if level2_code == "801102" or name == "通信设备":
        return "801770"
    return _SW_LEVEL1_SEGMENT.get(level2_code[:5])


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
            "page_size": "200",
            "index_type": _INDEX_TYPE,
            "bargaindate": day.isoformat(),
            "type": "MONTH",
            "swindexcode": "all",
        },
    )
    rows = [
        row
        for row in (_row_from_sw(item) for item in (payload.get("data") or {}).get("results") or [])
        if row["code"] and row["name"]
    ]
    if len(rows) < _MIN_MONTHLY_ROWS:
        raise ValueError(f"申万二级行业 {day} 月报不完整：仅 {len(rows)} 行")
    return day.isoformat(), {row["code"]: row for row in rows}


def _fetch_daily_window(days: int = 45) -> list[tuple[str, dict[str, dict]]]:
    """最近约 30 个交易日的申万二级行业日频快照。"""
    end = datetime.now(BEIJING).date()
    start = end - timedelta(days=days)
    payload = _request_json(
        _DAILY_URL,
        {
            "page": "1",
            "page_size": "5000",
            "index_type": _INDEX_TYPE,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "type": "DAY",
            "swindexcode": "all",
        },
    )
    grouped: dict[str, dict[str, dict]] = {}
    for item in (payload.get("data") or {}).get("results") or []:
        day = str(item.get("bargaindate") or "")[:10]
        row = _row_from_sw(item)
        if day and row["code"] and row["name"]:
            grouped.setdefault(day, {})[row["code"]] = row
    snapshots = [
        (day, rows)
        for day, rows in grouped.items()
        if len(rows) >= _MIN_MONTHLY_ROWS
    ]
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


def _rank_by(rows: list[dict], field: str) -> dict[str, float]:
    """131 个二级行业内按指标升序排名，映射到 0-100 百分位。"""
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
        score = round(((index + end - 1) / 2) / (len(pairs) - 1) * 100, 1)
        for offset in range(index, end):
            out[pairs[offset][0]] = score
        index = end
    return out


def build_level2_payload(
    monthly_snapshots: list[tuple[str, dict[str, dict]]],
    daily_snapshots: list[tuple[str, dict[str, dict]]],
) -> dict:
    """二级行业五指标评分，口径与一级行业评分完全一致。

    估值赔率（PE 分位 70% + PB 分位 30%，反向）、盈利景气（隐含盈利因子
    较 12 个月前 70% + 较 3 个月前 30%，131 个二级行业横向排名）、
    交易确认（换手率与成交占比分位各半，经非线性拥挤修正）、
    拥挤风险（超过 80 后每分扣 1 分）。
    """
    monthly = sorted(
        (datetime.strptime(day, "%Y-%m-%d").date(), rows)
        for day, rows in monthly_snapshots
    )
    daily = sorted(
        (datetime.strptime(day, "%Y-%m-%d").date(), rows)
        for day, rows in daily_snapshots
    )
    if len(monthly) < 12:
        raise ValueError("申万二级行业历史月度快照不足 12 期")
    if not daily:
        raise ValueError("申万二级行业日频快照缺失")
    current_date, current_rows = daily[-1]

    raw: list[dict] = []
    for code, current in current_rows.items():
        level1 = _level1_code(code, current["name"])
        if not level1:
            continue
        monthly_history = [(day, rows[code]) for day, rows in monthly if code in rows]
        activity_history = [(day, rows[code]) for day, rows in daily if code in rows]
        pe_history = [
            row["pe"] for _, row in monthly_history
            if row.get("pe") is not None and 0 < row["pe"] < 500
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
        previous_3m = _closest_history(lag_history, current_date - timedelta(days=92), 46)
        previous_year = _closest_history(lag_history, current_date - timedelta(days=365), 48)

        def earnings_proxy(row: dict | None) -> float | None:
            if not row:
                return None
            scale = row.get("close")
            row_pe = row.get("pe")
            if scale is None or row_pe is None or row_pe <= 0 or row_pe >= 500:
                return None
            return scale / row_pe

        current_earnings = earnings_proxy(current)
        pe = current.get("pe")
        pb = current.get("pb")
        pe_percentile = _percentile(pe_history, pe if pe and 0 < pe < 500 else None)
        pb_percentile = _percentile(pb_history, pb if pb and pb > 0 else None)
        valuation_score = _weighted([
            (100 - pe_percentile if pe_percentile is not None else None, 0.7),
            (100 - pb_percentile if pb_percentile is not None else None, 0.3),
        ])
        raw.append({
            "code": code,
            "name": current["name"],
            "level1_code": level1,
            "latest_return": current.get("return_pct"),
            "pe": pe if pe is not None and 0 < pe < 500 else None,
            "pb": pb,
            "dividend_yield": current.get("dividend_yield"),
            "float_market_cap": current.get("float_market_cap"),
            "turnover_rate": current.get("turnover_rate"),
            "turnover_share": current.get("turnover_share"),
            "valuation_score": round(valuation_score, 1) if valuation_score is not None else None,
            "pe_percentile": pe_percentile,
            "pb_percentile": pb_percentile,
            "earnings_3m": _relative_change(current_earnings, earnings_proxy(previous_3m)),
            "earnings_yoy": _relative_change(current_earnings, earnings_proxy(previous_year)),
            "turnover_rate_percentile": _percentile(turnover_history, current.get("turnover_rate")),
            "turnover_share_percentile": _percentile(share_history, current.get("turnover_share")),
            "monthly_sample_count": len(monthly_history),
            "activity_sample_count": len(activity_history),
        })

    earnings_3m_rank = _rank_by(raw, "earnings_3m")
    earnings_yoy_rank = _rank_by(raw, "earnings_yoy")
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
            "level1_code": row["level1_code"],
            "score": round(score, 1) if score is not None else None,
            "phase": phase,
            "latest_return": row.get("latest_return"),
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

    rows.sort(key=lambda item: (item["level1_code"], -(item["score"] or 0), item["name"]))
    level1_map = {code: name for code, name in _SW_LEVEL1.values()}
    return {
        "schema_version": _SCHEMA_VERSION,
        "as_of": current_date.isoformat(),
        "monthly_as_of": monthly[-1][0].isoformat(),
        "history_start": monthly[0][0].isoformat(),
        "history_samples": len(monthly),
        "daily_history_samples": len(daily),
        "industry_count": len(rows),
        "level1_names": level1_map,
        "industries": rows,
        "methodology": {
            "classification": "申万二级行业（2021版，131个行业）；二级指数代码段映射申万一级行业",
            "frequency": "当前值取申万二级行业最近交易日日频；历史分位锚取申万月报",
            "weights": {"valuation": 30, "prosperity": 40, "attention": 30},
            "penalty": "拥挤风险超过 80 分后，每超 1 分扣 1 分，最多扣 20 分",
            "definitions": [
                "与一级行业评分同一公式：估值赔率 PE 分位 70% + PB 分位 30%（反向）；盈利景气较12个月前 70% + 较3个月前 30%；交易确认由换手率和成交额占比历史分位各半形成，并在约60分时达到最高确认度。",
                "PE 超过 500 或亏损导致的极端值不计入估值与盈利景气。",
                "拥挤风险：换手率与成交占比历史分位各半，超过80分后每超1分综合扣1分。",
                "二级行业排名与评分只在 131 个二级行业之间比较，与一级行业评分不直接可比。",
            ],
            "sources": [
                {
                    "label": "申万宏源研究·指数分析（二级行业日频/月报）",
                    "url": "https://www.swsresearch.com/institute_sw/allIndex/analysisIndex",
                },
                {
                    "label": "申万行业分类标准 2021 版说明",
                    "url": "https://wxweb.swsresearch.com/swsreport/2021_08/328340.pdf",
                },
            ],
        },
    }


def _load_snapshot_cache() -> dict[str, dict[str, dict]]:
    merged: dict[str, dict[str, dict]] = {}
    for path in dict.fromkeys((_PRIMARY_SNAPSHOT_FILE, _FALLBACK_SNAPSHOT_FILE)):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("schema_version") != _SNAPSHOT_SCHEMA_VERSION:
                continue
            for day, rows in (payload.get("snapshots") or {}).items():
                if isinstance(rows, dict):
                    merged[day] = rows
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return merged


def _save_snapshot_cache(snapshots: dict[str, dict[str, dict]]) -> None:
    _write_json_with_fallback(
        {
            "schema_version": _SNAPSHOT_SCHEMA_VERSION,
            "updated_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
            "snapshots": snapshots,
        },
        _PRIMARY_SNAPSHOT_FILE,
        _FALLBACK_SNAPSHOT_FILE,
    )


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
    monthly = sorted(snapshot_map.items())
    if len(monthly) < 24:
        raise RuntimeError(f"申万二级行业历史快照获取不足：仅成功 {len(monthly)} 期")
    _save_snapshot_cache(snapshot_map)

    daily = _fetch_daily_window()
    if len(daily) < 12:
        raise RuntimeError(f"申万二级行业日频样本不足：仅 {len(daily)} 个交易日")

    data = build_level2_payload(monthly, daily)
    data["history_requested"] = len(dates)
    data["history_partial"] = len(monthly) < len(dates)
    now = datetime.now(BEIJING)
    data["is_intraday"] = (
        data["as_of"] == now.date().isoformat()
        and now.weekday() < 5
        and (now.hour, now.minute) < (15, 5)
    )
    data["generated_at"] = now.strftime("%Y-%m-%d %H:%M")
    data["stale"] = False
    return data


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
        data.get("as_of") == now.date().isoformat()
        and now.weekday() < 5
        and (now.hour, now.minute) >= (9, 15)
        and (now.hour, now.minute) <= (15, 30)
    ):
        return _INTRADAY_TTL
    return _TTL


def get_cached_level2_scores() -> dict | None:
    """最近一次成功结果，不触发任何外部数据读取。"""
    return _load_cache()


def get_level2_scores(force: bool = False) -> dict:
    cached = _load_cache()
    return cache_runtime.get(
        "sw_level2_scores:v2", _build,
        valid=lambda value: bool(value.get("industries")),
        ttl=_cache_ttl(cached or {}), warm=_load_cache, save=_save_cache, force=force,
    )
