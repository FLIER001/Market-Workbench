"""PFS V3.0 的公开数据初筛实现。

严格按 Manager-First 边界处理：只把现任管理团队任期覆盖的产品收益用于初筛；
持仓 Alpha、因子归因、真实资金流、策略容量和平台趋势缺失时使用 50 分中性先验，
并通过 Confidence 收缩，绝不把替代指标包装成已验证 Skill。
"""

from __future__ import annotations

import bisect
import html
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

import fund
import cache_runtime

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
_CACHE_FILE = os.path.join(DATA_DIR, "fund_pfs.json")
_DB_FILE = os.path.join(DATA_DIR, "fund_pfs.sqlite")
_SCHEMA_VERSION = 2
_TTL = 24 * 3600
_DETAIL_LIMIT = 40
_F10_LOCK = threading.Lock()
_NAV_LOCK = threading.Lock()  # AKShare净值接口内部py_mini_racer不是线程安全的
_F10_LAST_REQUEST = 0.0

_RETURN_FIELDS = ("近6月", "近1年", "近2年", "近3年")
_ACTIVE_TYPES = ("股票型", "股票型-普通", "混合型-偏股", "混合型-灵活", "混合型-平衡")
_MISSING_EVIDENCE = [
    "季度持仓归因、选股 Alpha 与交易 Alpha",
    "多因子 Alpha、显著性、Bootstrap 与真实 Skill Probability",
    "日频基金净申赎与完整持仓拥挤",
    "基于 ADV/冲击成本的策略容量与可变现天数",
    "投研团队、平台稳定性与流程一致性的事件证据",
]


def _num(value) -> float | None:
    try:
        if value is None:
            return None
        number = float(str(value).replace(",", "").replace("%", ""))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    text = str(value or "").strip()
    return "" if text in {"nan", "NaN", "<NA>", "None"} else text


def _clip(value: float, low: float = 0, high: float = 100) -> float:
    return min(high, max(low, value))


def _weighted(parts: list[tuple[float | None, float]], prior: float = 50) -> float:
    """缺失项保留原权重并落到中性先验，避免只按幸存指标重归一化。"""
    return sum((prior if value is None else value) * weight for value, weight in parts)


def _mean(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _strategy(fund_type: str) -> str:
    if fund_type in {"股票型", "股票型-普通"}:
        return "主动股票"
    return {
        "混合型-偏股": "偏股混合",
        "混合型-灵活": "灵活配置",
        "混合型-平衡": "平衡混合",
    }.get(fund_type, fund_type or "未分类")


def _manager_names(value: str) -> list[str]:
    return [name for name in re.split(r"[,，、/\s]+", value) if name]


def _base_product_name(name: str) -> str:
    return re.sub(r"(?:\s|[-_/])?(?:人民币)?[A-HI-Z](?:类)?$", "", name, flags=re.I)


def _tenure_score(days: int | None) -> float | None:
    if days is None:
        return None
    anchors = [(0, 20), (548, 55), (1095, 70), (1825, 82), (3650, 95)]
    for (d0, s0), (d1, s1) in zip(anchors, anchors[1:]):
        if days <= d1:
            return s0 + (s1 - s0) * (days - d0) / (d1 - d0)
    return 98


def _inverse_rank(value: float | None, peers: list[float]) -> float | None:
    if value is None or len(peers) < 3:
        return None
    values = sorted(peers)
    left, right = bisect.bisect_left(values, value), bisect.bisect_right(values, value)
    return 100 - (left + right) / 2 / len(values) * 100


def _rank(value: float | None, peers: list[float]) -> float | None:
    inverse = _inverse_rank(value, peers)
    return None if inverse is None else 100 - inverse


def _curve(value: float | None, points: list[tuple[float, float]]) -> float | None:
    if value is None:
        return None
    if value <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return points[-1][1]


def _read_f10(url: str) -> str:
    global _F10_LAST_REQUEST
    with _F10_LOCK:
        wait = 0.18 - (time.monotonic() - _F10_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"})
        for attempt in range(2):
            try:
                raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
                _F10_LAST_REQUEST = time.monotonic()
                return raw
            except Exception as error:
                if getattr(error, "code", None) != 514 or attempt:
                    raise
                time.sleep(1)
        return ""


def _add_peer_percentiles(rows: list[dict]) -> None:
    for field in _RETURN_FIELDS:
        groups: dict[str, list[float]] = {}
        for row in rows:
            value = row.get(field)
            if value is not None:
                groups.setdefault(row["fund_type"], []).append(value)
        for values in groups.values():
            values.sort()
        for row in rows:
            value = row.get(field)
            values = groups.get(row["fund_type"], [])
            if value is None or len(values) < 3:
                score = None
            else:
                left, right = bisect.bisect_left(values, value), bisect.bisect_right(values, value)
                score = (left + right) / 2 / len(values) * 100
            row.setdefault("return_percentiles", {})[field] = round(score, 1) if score is not None else None


def _rating_score(row: dict) -> float | None:
    stars = [_num(row.get(key)) for key in ("上海证券", "招商证券", "济安金信", "晨星评级")]
    return _mean([star * 20 if star is not None else None for star in stars])


def _bulk_universe() -> tuple[list[dict], dict]:
    import akshare as ak

    rank_rows = fund._cached("fund_rank_table", 3600, fund._rank_table)
    ratings = fund._cached("pfs_rating_all", 6 * 3600, ak.fund_rating_all)
    managers = fund._cached("pfs_managers", 24 * 3600, ak.fund_manager_em)
    purchases = fund._cached("pfs_purchase", 6 * 3600, ak.fund_purchase_em)

    rating_map = {}
    for _, item in ratings.iterrows():
        code = _text(item.get("代码"))
        if re.fullmatch(r"\d{6}", code):
            rating_map[code] = item.to_dict()

    purchase_map = {}
    for _, item in purchases.iterrows():
        code = _text(item.get("基金代码"))
        if re.fullmatch(r"\d{6}", code):
            purchase_map[code] = item.to_dict()

    manager_stats: dict[str, dict] = {}
    code_managers: dict[str, list[str]] = {}
    for _, item in managers.iterrows():
        name = _text(item.get("姓名"))
        code = _text(item.get("现任基金代码"))
        if not name or not re.fullmatch(r"\d{6}", code):
            continue
        code_managers.setdefault(code, []).append(name)
        stat = manager_stats.setdefault(name, {"career_days": None, "aum": None, "funds": set()})
        stat["career_days"] = max(stat["career_days"] or 0, int(_num(item.get("累计从业时间")) or 0)) or None
        stat["aum"] = max(stat["aum"] or 0, _num(item.get("现任基金资产总规模")) or 0) or None
        stat["funds"].add(_base_product_name(_text(item.get("现任基金"))))

    rows = []
    for rank in rank_rows:
        code = rank["code"]
        rating = rating_map.get(code, {})
        purchase = purchase_map.get(code, {})
        fund_type = _text(rating.get("类型")) or _text(purchase.get("基金类型"))
        if fund_type not in _ACTIVE_TYPES:
            continue
        names = list(dict.fromkeys(_manager_names(_text(rating.get("基金经理"))) + code_managers.get(code, [])))
        stats = [manager_stats[name] for name in names if name in manager_stats]
        career_days = min((stat["career_days"] for stat in stats if stat["career_days"]), default=None)
        manager_aum = max((stat["aum"] for stat in stats if stat["aum"]), default=None)
        fund_count = max((len(stat["funds"]) for stat in stats), default=None)
        row = {
            **{field: rank.get(field) for field in _RETURN_FIELDS},
            "code": code,
            "name": rank["name"],
            "data_date": rank.get("date"),
            "fund_type": fund_type,
            "strategy": _strategy(fund_type),
            "manager": "、".join(names),
            "manager_names": names,
            "manager_count": len(names),
            "platform": _text(rating.get("基金公司")),
            "manager_career_days": career_days,
            "manager_aum": manager_aum,
            "manager_fund_count": fund_count,
            "purchase_status": _text(purchase.get("申购状态")),
            "redemption_status": _text(purchase.get("赎回状态")),
            "fee_pct": _num(purchase.get("手续费")),
            "rating_score": _rating_score(rating),
        }
        rows.append(row)
    _add_peer_percentiles(rows)
    return rows, {
        "rank_count": len(rank_rows),
        "rating_count": len(rating_map),
        "manager_count": len(manager_stats),
        "purchase_count": len(purchase_map),
    }


def _manager_assignment(code: str) -> dict:
    def _fetch():
        page = _read_f10(f"https://fundf10.eastmoney.com/jjjl_{code}.html")
        found = re.findall(
            r"manager/(\d+)\.html[^>]*>([^<]+)</a></p><p><strong>上任日期：</strong>(\d{4}-\d{2}-\d{2})",
            page,
        )
        assignments = [{"manager_id": item[0], "name": item[1].strip(), "start_date": item[2]} for item in found]
        if not assignments:
            return {"assignments": [], "team_start_date": None, "team_tenure_days": None}
        team_start = max(item["start_date"] for item in assignments)
        days = (datetime.now(BEIJING).date() - date.fromisoformat(team_start)).days
        return {"assignments": assignments, "team_start_date": team_start, "team_tenure_days": days}

    return fund._cached(f"pfs_assignment_{code}", 24 * 3600, _fetch)


def _risk_analysis(code: str) -> dict:
    import akshare as ak

    def _fetch():
        for attempt in range(2):
            try:
                frame = ak.fund_individual_analysis_xq(symbol=code, timeout=10)
                break
            except Exception:
                if attempt:
                    raise
                time.sleep(0.5)
        rows = {}
        for _, item in frame.iterrows():
            period = _text(item.get("周期"))
            rows[period] = {
                "risk_return_peer": _num(item.get("较同类风险收益比")),
                "resilience_peer": _num(item.get("较同类抗风险波动")),
                "volatility": _num(item.get("年化波动率")),
                "sharpe": _num(item.get("年化夏普比率")),
                "max_drawdown": _num(item.get("最大回撤")),
            }
        return rows

    return fund._cached(f"pfs_risk_{code}", 6 * 3600, _fetch)


def _annual_fees(code: str) -> dict:
    import akshare as ak

    def _fetch():
        for attempt in range(2):
            try:
                frame = ak.fund_individual_detail_info_xq(symbol=code, timeout=10)
                break
            except Exception:
                if attempt:
                    raise
                time.sleep(0.5)
        fees = {}
        for _, item in frame.iterrows():
            if _text(item.get("费用类型")) != "其他费用":
                continue
            label, value = _text(item.get("条件或名称")), _num(item.get("费用"))
            if label and value is not None:
                fees[label] = value
        return {"items": fees, "total": round(sum(fees.values()), 3) if fees else None}

    return fund._cached(f"pfs_fees_{code}", 24 * 3600, _fetch)


def _f10_rows(code: str, table_type: str) -> list[list[str]]:
    def _fetch():
        raw = _read_f10(f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type={table_type}&code={code}")
        rows = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
            cells = [html.unescape(re.sub(r"<[^>]+>|\s+", "", cell)) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if cells and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
                rows.append(cells)
        return rows

    return fund._cached(f"pfs_f10_{table_type}_{code}", 24 * 3600, _fetch)


def _scale_history(code: str) -> list[dict]:
    return [
        {
            "date": cells[0], "subscriptions": _num(cells[1]), "redemptions": _num(cells[2]),
            "ending_shares": _num(cells[3]), "net_assets": _num(cells[4]), "aum_change_pct": _num(cells[5]),
        }
        for cells in _f10_rows(code, "gmbd") if len(cells) >= 6
    ]


def _holder_history(code: str) -> list[dict]:
    return [
        {
            "date": cells[0], "institution_pct": _num(cells[1]), "individual_pct": _num(cells[2]),
            "internal_pct": _num(cells[3]), "total_shares": _num(cells[4]),
        }
        for cells in _f10_rows(code, "cyrjg") if len(cells) >= 5
    ]


def _nav_features(rows: list[dict], start_date: str | None) -> dict:
    rows = [row for row in rows if row.get("date") and row.get("nav") and (not start_date or row["date"] >= start_date)]
    if len(rows) < 30:
        return {"n": len(rows)}
    equity, returns = [1.0], []
    for previous, current in zip(rows, rows[1:]):
        change = _num(current.get("day_pct"))
        daily = change / 100 if change is not None else current["nav"] / previous["nav"] - 1
        if math.isfinite(daily) and daily > -1:
            returns.append(daily)
            equity.append(equity[-1] * (1 + daily))
    if len(returns) < 29:
        return {"n": len(returns) + 1}
    calendar_days = max(1, (date.fromisoformat(rows[-1]["date"]) - date.fromisoformat(rows[0]["date"])).days)
    ann_return = (equity[-1] ** (365 / calendar_days) - 1) * 100
    mean = sum(returns) / len(returns)
    volatility = math.sqrt(sum((item - mean) ** 2 for item in returns) / len(returns)) * math.sqrt(252)
    downside = math.sqrt(sum(min(item, 0) ** 2 for item in returns) / len(returns)) * math.sqrt(252)
    sortino = (mean * 252 - 0.015) / downside if downside else None
    tail_n = max(1, math.ceil(len(returns) * 0.05))
    cvar95 = sum(sorted(returns)[:tail_n]) / tail_n * 100
    peak_value = equity[0]
    peak_index = trough_index = 0
    max_drawdown = 0.0
    for index, value in enumerate(equity):
        if value > peak_value:
            peak_value, peak_index = value, index
        drawdown = value / peak_value - 1
        if drawdown < max_drawdown:
            max_drawdown, trough_index = drawdown, index
            drawdown_peak = peak_index
    recovery_index = next((index for index in range(trough_index + 1, len(equity)) if equity[index] >= equity[drawdown_peak]), None) if max_drawdown < 0 else 0
    recovery_days = (
        (date.fromisoformat(rows[recovery_index]["date"]) - date.fromisoformat(rows[trough_index]["date"])).days
        if recovery_index is not None else None
    )
    rolling = {}
    for label, window in (("12m", 252), ("24m", 504), ("36m", 756)):
        values = [equity[index] / equity[index - window] - 1 for index in range(window, len(equity))]
        rolling[f"rolling_{label}_positive_ratio"] = round(sum(value > 0 for value in values) / len(values) * 100, 1) if values else None
        rolling[f"rolling_{label}_median_return"] = round(sorted(values)[len(values) // 2] * 100, 2) if values else None
    return {
        "n": len(equity), "start_date": rows[0]["date"], "end_date": rows[-1]["date"],
        "ann_return": round(ann_return, 2), "volatility": round(volatility * 100, 2),
        "sortino": round(sortino, 2) if sortino is not None else None,
        "max_drawdown": round(max_drawdown * 100, 2), "cvar95": round(cvar95, 2),
        "recovery_days": recovery_days, "unrecovered": recovery_index is None,
        **rolling,
    }


def _scale_features(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest, previous = rows[0], rows[1] if len(rows) > 1 else {}
    net_flow = (
        latest["subscriptions"] - latest["redemptions"]
        if latest.get("subscriptions") is not None and latest.get("redemptions") is not None else None
    )
    opening_shares = latest.get("ending_shares") - net_flow if latest.get("ending_shares") is not None and net_flow is not None else None
    flow_rate = net_flow / opening_shares * 100 if opening_shares and net_flow is not None else None
    redemption_rate = latest.get("redemptions") / opening_shares * 100 if opening_shares and latest.get("redemptions") is not None else None
    aum_growth = latest.get("aum_change_pct")
    if aum_growth is None and latest.get("net_assets") is not None and previous.get("net_assets"):
        aum_growth = (latest["net_assets"] / previous["net_assets"] - 1) * 100
    flow_score = _weighted([
        (_curve(flow_rate, [(-50, 10), (-20, 25), (-10, 45), (0, 75), (10, 90), (25, 70), (50, 25), (100, 5)]), 0.70),
        (_curve(redemption_rate, [(0, 95), (10, 85), (25, 65), (50, 35), (100, 5)]), 0.30),
    ]) if flow_rate is not None or redemption_rate is not None else None
    return {
        "date": latest.get("date"), "net_assets": latest.get("net_assets"), "ending_shares": latest.get("ending_shares"),
        "net_share_flow": round(net_flow, 2) if net_flow is not None else None,
        "net_share_flow_rate": round(flow_rate, 2) if flow_rate is not None else None,
        "redemption_rate": round(redemption_rate, 2) if redemption_rate is not None else None,
        "aum_growth_1q": round(aum_growth, 2) if aum_growth is not None else None,
        "quarterly_flow_score": round(flow_score, 1) if flow_score is not None else None,
    }


def _holder_features(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest, previous = rows[0], rows[1] if len(rows) > 1 else {}
    current, prior = latest.get("institution_pct"), previous.get("institution_pct")
    return {
        "date": latest.get("date"), "institution_pct": current,
        "individual_pct": latest.get("individual_pct"), "internal_pct": latest.get("internal_pct"),
        "institution_change": round(current - prior, 2) if current is not None and prior is not None else None,
    }


def _store_observations(rows: list[dict], details: dict[str, dict]) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    seen_at = datetime.now(BEIJING).isoformat(timespec="seconds")
    observations = []
    for row in rows:
        code, detail = row["code"], details.get(row["code"], {})
        for metric, items, source in (
            ("nav", (detail.get("nav") or {}).get("rows") or [], "东方财富单位净值走势"),
            ("scale", (detail.get("scale") or {}).get("rows") or [], "东方财富F10规模变动"),
            ("holders", (detail.get("holders") or {}).get("rows") or [], "东方财富F10持有人结构"),
        ):
            for item in items:
                observations.append((code, metric, item.get("date") or "", None, seen_at, source, json.dumps(item, ensure_ascii=False, sort_keys=True), 0))
        snapshot = {
            "manager_aum": row.get("manager_aum"), "manager_fund_count": row.get("manager_fund_count"),
            "strategy": row.get("strategy"), "manager": row.get("manager"),
        }
        observations.append((code, "manager_load", seen_at[:10], seen_at, seen_at, "PFS构建快照", json.dumps(snapshot, ensure_ascii=False, sort_keys=True), 1))
        feature_snapshot = {
            "nav": (detail.get("nav") or {}).get("features") or {},
            "scale": (detail.get("scale") or {}).get("features") or {},
            "holders": (detail.get("holders") or {}).get("features") or {},
        }
        observations.append((code, "feature_snapshot", seen_at[:10], seen_at, seen_at, "PFS构建快照", json.dumps(feature_snapshot, ensure_ascii=False, sort_keys=True), 1))
    with sqlite3.connect(_DB_FILE) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                fund_code TEXT NOT NULL, metric TEXT NOT NULL, effective_date TEXT NOT NULL,
                published_at TEXT, first_seen_at TEXT NOT NULL, source TEXT NOT NULL,
                payload TEXT NOT NULL, pit_usable INTEGER NOT NULL,
                PRIMARY KEY (fund_code, metric, effective_date, payload)
            )
        """)
        connection.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?)", observations)
        count, usable = connection.execute("SELECT COUNT(*), COALESCE(SUM(pit_usable), 0) FROM observations").fetchone()
    return {"path": _DB_FILE, "observation_count": count, "pit_usable_count": usable, "historical_publication_dates": "missing"}


def _stored_series(code: str, metric: str, max_age_days: int, descending: bool = False) -> list[dict]:
    try:
        with sqlite3.connect(_DB_FILE) as connection:
            stored = connection.execute(
                "SELECT effective_date, first_seen_at, payload FROM observations WHERE fund_code=? AND metric=? ORDER BY effective_date, first_seen_at",
                (code, metric),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    latest_by_date = {effective_date: json.loads(payload) for effective_date, _, payload in stored}
    if not latest_by_date:
        return []
    latest = max(latest_by_date)
    if (datetime.now(BEIJING).date() - date.fromisoformat(latest)).days > max_age_days:
        return []
    return [latest_by_date[key] for key in sorted(latest_by_date, reverse=descending)]


def _detail(code: str, previous: dict | None = None) -> dict:
    detail = {"assignment": (previous or {}).get("assignment") or {}, "risk": {}, "fees": {}, "nav": {}, "scale": {}, "holders": {}, "errors": []}
    try:
        if not detail["assignment"].get("team_tenure_days"):
            detail["assignment"] = _manager_assignment(code)
    except Exception as error:  # source gaps are data, not fabricated values
        detail["errors"].append(f"经理任期：{error}")
    try:
        detail["risk"] = _risk_analysis(code)
    except Exception as error:
        detail["errors"].append(f"风险分析：{error}")
    try:
        detail["fees"] = _annual_fees(code)
    except Exception as error:
        detail["errors"].append(f"年度费用：{error}")
    try:
        nav_rows = _stored_series(code, "nav", 7)
        if not nav_rows:
            with _NAV_LOCK:
                nav_rows = fund.nav_history(code, limit=4000)["rows"]
        detail["nav"] = {"rows": nav_rows, "features": _nav_features(nav_rows, detail["assignment"].get("team_start_date"))}
    except Exception as error:
        detail["errors"].append(f"任期净值：{error}")
    try:
        scale_rows = _stored_series(code, "scale", 100, descending=True) or _scale_history(code)
        detail["scale"] = {"rows": scale_rows, "features": _scale_features(scale_rows)}
    except Exception as error:
        detail["errors"].append(f"季度规模：{error}")
    try:
        holder_rows = _stored_series(code, "holders", 260, descending=True) or _holder_history(code)
        detail["holders"] = {"rows": holder_rows, "features": _holder_features(holder_rows)}
    except Exception as error:
        detail["errors"].append(f"持有人结构：{error}")
    return detail


def _add_detail_scores(rows: list[dict], details: dict[str, dict]) -> None:
    for fund_type in {row["fund_type"] for row in rows}:
        peers = [row for row in rows if row["fund_type"] == fund_type]
        features = [(details.get(row["code"], {}).get("nav") or {}).get("features") or {} for row in peers]
        for row, feature in zip(peers, features):
            risk_scores = [
                _inverse_rank(abs(feature.get("max_drawdown")) if feature.get("max_drawdown") is not None else None,
                              [abs(item["max_drawdown"]) for item in features if item.get("max_drawdown") is not None]),
                _inverse_rank(feature.get("volatility"), [item["volatility"] for item in features if item.get("volatility") is not None]),
                _rank(feature.get("sortino"), [item["sortino"] for item in features if item.get("sortino") is not None]),
                _rank(feature.get("cvar95"), [item["cvar95"] for item in features if item.get("cvar95") is not None]),
            ]
            rolling_scores = [
                _rank(feature.get(field), [item[field] for item in features if item.get(field) is not None])
                for field in ("rolling_12m_positive_ratio", "rolling_24m_positive_ratio", "rolling_36m_positive_ratio")
            ]
            detail = details.setdefault(row["code"], {})
            detail["nav_risk_score"] = _mean(risk_scores)
            detail["rolling_persistence_score"] = _mean(rolling_scores)


def _aligned_periods(tenure_days: int | None) -> list[str]:
    if tenure_days is None:
        return []
    return [
        field for field, minimum in (("近6月", 183), ("近1年", 365), ("近2年", 730), ("近3年", 1095))
        if tenure_days >= minimum
    ]


def _confidence(tenure_days: int | None, manager_count: int, aligned: list[str], detail_coverage: float) -> tuple[float, dict]:
    tenure = (_tenure_score(tenure_days) or 20) / 100
    cycle = {0: 0.25, 1: 0.45, 2: 0.60, 3: 0.72, 4: 0.82}[len(aligned)]
    attribution = 0.90 if manager_count == 1 else 0.65 if manager_count == 2 else 0.45
    data = 0.48 + 0.30 * detail_coverage
    components = {
        "tenure": round(tenure, 2),
        "cycle": cycle,
        "attribution": attribution,
        "data": data,
        "platform": 0.50,
        "strategy": 0.55,
    }
    score = 0.25 * tenure + 0.20 * cycle + 0.20 * attribution + 0.15 * data + 0.10 * 0.50 + 0.10 * 0.55
    return round(score, 2), components


def _score_candidate(row: dict, detail: dict, peer_aum: list[float], peer_load: list[float], peer_fee: list[float], peer_annual_fee: list[float] | None = None) -> dict:
    assignment = detail.get("assignment") or {}
    tenure_days = assignment.get("team_tenure_days")
    aligned = _aligned_periods(tenure_days)
    aligned_scores = [row["return_percentiles"].get(field) for field in aligned]
    aligned_score = _mean(aligned_scores)

    risk_period = "近3年" if tenure_days and tenure_days >= 1095 else "近1年"
    risk = (detail.get("risk") or {}).get(risk_period) or {}
    risk_score = _mean([risk.get("risk_return_peer"), risk.get("resilience_peer"), detail.get("nav_risk_score")])
    attribution = 90 if row["manager_count"] == 1 else 65 if row["manager_count"] == 2 else 45
    manager_score = _weighted([
        (_tenure_score(tenure_days), 0.25), (attribution, 0.25), (aligned_score, 0.35), (risk_score, 0.15),
    ])
    if aligned_scores:
        clean = [value for value in aligned_scores if value is not None]
        spread = math.sqrt(sum((value - sum(clean) / len(clean)) ** 2 for value in clean) / len(clean)) if clean else 0
        persistence = _clip((sum(clean) / len(clean)) - 0.25 * spread) if clean else None
    else:
        persistence = None
    rolling_persistence = detail.get("rolling_persistence_score")
    evidence_score = _mean([persistence, rolling_persistence])
    verified_skill = _weighted([(aligned_score, 0.40), (evidence_score, 0.30), (risk_score, 0.30)])

    annual_fee = (detail.get("fees") or {}).get("total")
    fee_score = _inverse_rank(annual_fee, peer_annual_fee or [])
    if fee_score is None:
        fee_score = _inverse_rank(row.get("fee_pct"), peer_fee)
    status_score = 85 if "开放" in row.get("purchase_status", "") and "开放" in row.get("redemption_status", "") else 55
    implementation = _weighted([(fee_score, 0.70), (status_score, 0.30)])
    quality_components = {
        "manager": round(manager_score, 1),
        "process": 50.0,
        "verified_skill": round(verified_skill, 1),
        "platform": 50.0,
        "implementation": round(implementation, 1),
    }
    quality = _weighted([
        (manager_score, 0.35), (None, 0.25), (verified_skill, 0.20), (None, 0.10), (implementation, 0.10),
    ])

    capacity = _inverse_rank(row.get("manager_aum"), peer_aum)
    bandwidth = _inverse_rank(row.get("manager_fund_count"), peer_load)
    flow_score = ((detail.get("scale") or {}).get("features") or {}).get("quarterly_flow_score")
    potential_components = {
        "evidence": round(evidence_score if evidence_score is not None else 50, 1),
        "capacity": round(capacity if capacity is not None else 50, 1),
        "bandwidth": round(bandwidth if bandwidth is not None else 50, 1),
        "flow": round(flow_score if flow_score is not None else 50, 1),
        "platform_trend": 50.0,
        "implementation": round(implementation, 1),
    }
    potential = _weighted([
        (evidence_score, 0.25), (capacity, 0.25), (bandwidth, 0.15), (flow_score, 0.15), (None, 0.10), (implementation, 0.10),
    ])
    observed = [
        bool(assignment), bool(detail.get("risk")), bool((detail.get("nav") or {}).get("features")),
        bool((detail.get("scale") or {}).get("features")), bool((detail.get("holders") or {}).get("features")),
    ]
    confidence, confidence_components = _confidence(
        tenure_days, row["manager_count"], aligned, sum(observed) / len(observed),
    )

    gate_failures, review_reasons, risk_notes = [], [], []
    if not row.get("manager") or tenure_days is None:
        gate_failures.append("现任经理或本产品任期无法确认")
    if not aligned_scores or aligned_score is None:
        gate_failures.append("缺少现任团队任期可覆盖的净值证据")
    if "暂停赎回" in row.get("redemption_status", ""):
        gate_failures.append("产品当前暂停赎回")
    if tenure_days is not None and tenure_days < 548:
        review_reasons.append("现任管理团队不足18个月")
    if row["manager_count"] > 1:
        review_reasons.append("多人共管且公开数据无法确认决策责任")
    if "暂停申购" in row.get("purchase_status", ""):
        review_reasons.append("产品当前暂停申购")
    p1, p3 = row["return_percentiles"].get("近1年"), row["return_percentiles"].get("近3年")
    if p1 is not None and p3 is not None and p3 >= 70 and p1 < 35:
        risk_notes.append("近一年明显弱于三年证据：需区分风格逆风与能力衰减")
    if row.get("manager_fund_count") and row["manager_fund_count"] >= 10:
        risk_notes.append("现管产品较多，需核查管理带宽与同策略份额重复")
    scale_metrics = ((detail.get("scale") or {}).get("features") or {})
    if (scale_metrics.get("net_share_flow_rate") or 0) >= 30:
        risk_notes.append("最近季度净份额流入超过30%，需核查规模扩张与拥挤")
    if (scale_metrics.get("net_share_flow_rate") or 0) <= -20:
        risk_notes.append("最近季度净份额流出超过20%，需核查持续赎回压力")

    penalty = 0.0
    raw = 0.75 * quality + 0.25 * potential
    final = 50 + confidence * (raw - 50) - penalty
    if gate_failures:
        tier = "exclude"
    elif review_reasons and quality >= 68:
        tier = "review"
    elif final >= 80 and quality >= 80 and confidence >= 0.80:
        tier = "core_buy"
    elif final >= 74 and quality >= 75 and potential >= 80 and confidence >= 0.65:
        tier = "potential_buy"
    elif final >= 68 or (raw >= 68 and confidence < 0.80):
        # V3.0 将“Confidence 偏低”单列为 Watch；Raw 已达观察线但被证据收缩
        # 压到 68 以下时不直接冒充买入，也不与低质量 Gate Exclude 混为一类。
        tier = "watch"
    else:
        tier = "exclude"

    candidate_type = "稳健观察"
    if tenure_days and 548 <= tenure_days < 1095:
        candidate_type = "新锐经理型"
    elif p1 is not None and p3 is not None and p3 >= 70 and p1 < 35:
        candidate_type = "暂时逆风待核型"
    elif quality >= 75 and capacity is not None and capacity >= 70:
        candidate_type = "已验证、未拥挤初筛型"

    why_good = []
    if aligned_score is not None:
        why_good.append(f"现任团队任期可覆盖窗口的同类收益分位为 {aligned_score:.0f}%")
    if risk_score is not None:
        why_good.append(f"{risk_period}同类风险收益与抗波动综合分位为 {risk_score:.0f}%")
    if tenure_days is not None:
        why_good.append(f"当前管理团队已连续管理 {tenure_days / 365:.1f} 年")
    why_potential = []
    if capacity is not None and capacity >= 60:
        why_potential.append(f"经理现管总规模处于同策略较低侧，规模压力初筛分位 {capacity:.0f}%")
    if bandwidth is not None and bandwidth >= 60:
        why_potential.append(f"现管产品数较同类更精简，带宽初筛分位 {bandwidth:.0f}%")
    if evidence_score is not None and evidence_score >= 65:
        why_potential.append(f"任期内滚动与同类排名一致性得分 {evidence_score:.0f}")
    if flow_score is not None and flow_score >= 65:
        why_potential.append(f"最近季度真实份额流与赎回压力得分 {flow_score:.0f}")
    breaks = [
        "核心经理或当前决策团队发生实质变化",
        "规模与产品数量快速扩张并侵蚀可实现性",
        "风险收益和同类排名持续恶化且无法由风格解释",
        "后续持仓归因显示 Alpha 主要来自一次性行业暴露",
    ]
    return {
        **row,
        "annual_fee_pct": annual_fee,
        "annual_fee_items": (detail.get("fees") or {}).get("items") or {},
        "team_start_date": assignment.get("team_start_date"),
        "team_tenure_days": tenure_days,
        "manager_assignments": assignment.get("assignments") or [],
        "risk_period": risk_period,
        "risk_metrics": risk,
        "nav_metrics": ((detail.get("nav") or {}).get("features") or {}),
        "scale_metrics": scale_metrics,
        "holder_metrics": ((detail.get("holders") or {}).get("features") or {}),
        "quality_score": round(quality, 1),
        "potential_score": round(potential, 1),
        "raw_score": round(raw, 1),
        "confidence": confidence,
        "risk_penalty": penalty,
        "final_score": round(final, 1),
        "quality_components": quality_components,
        "potential_components": potential_components,
        "confidence_components": confidence_components,
        "tier": tier,
        "candidate_type": candidate_type,
        "gate_pass": not gate_failures,
        "gate_failures": gate_failures,
        "review_reasons": review_reasons,
        "risk_notes": risk_notes,
        "why_good": why_good or ["当前公开数据不足以形成优质判断"],
        "why_potential": why_potential or ["当前公开数据不足以形成潜力判断"],
        "breaks_thesis": breaks,
        "detail_errors": detail.get("errors") or [],
        "data_coverage": round(sum([
            tenure_days is not None, aligned_score is not None, risk_score is not None,
            row.get("fee_pct") is not None, row.get("manager_aum") is not None,
            row.get("manager_fund_count") is not None, bool((detail.get("nav") or {}).get("features")),
            bool(scale_metrics), bool((detail.get("holders") or {}).get("features")),
        ]) / 9 * 100),
    }


def _preselect(rows: list[dict]) -> list[dict]:
    for row in rows:
        pcts = row["return_percentiles"]
        evidence = _weighted([(pcts.get("近1年"), 0.30), (pcts.get("近2年"), 0.30), (pcts.get("近3年"), 0.40)])
        row["pre_score"] = 0.75 * evidence + 0.15 * (row.get("rating_score") or 50) + 0.10 * (_tenure_score(row.get("manager_career_days")) or 50)
    # A/C/E 等份额先合并成一个研究单元，避免同一经理同一策略挤占深筛名额。
    product_rows = []
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["strategy"], row["manager"], _base_product_name(row["name"])), []).append(row)
    for variants in grouped.values():
        representative = max(variants, key=lambda row: row["pre_score"])
        representative["share_classes"] = [
            {"code": item["code"], "name": item["name"], "purchase_fee_pct": item.get("fee_pct")}
            for item in variants
        ]
        product_rows.append(representative)

    selected = []
    for strategy in sorted({row["strategy"] for row in product_rows}):
        peers = [row for row in product_rows if row["strategy"] == strategy]
        selected.extend(sorted(peers, key=lambda row: row["pre_score"], reverse=True)[:10])
    unique = {row["code"]: row for row in selected}
    return sorted(unique.values(), key=lambda row: row["pre_score"], reverse=True)[:_DETAIL_LIMIT]


def _build(previous: dict | None = None) -> dict:
    universe, source_counts = _bulk_universe()
    selected = _preselect(universe)
    old = {}
    for row in (previous or {}).get("rows", []):
        if row.get("team_tenure_days"):
            old[row["code"]] = {"assignment": {
                "team_start_date": row.get("team_start_date"),
                "team_tenure_days": row.get("team_tenure_days"),
                "assignments": row.get("manager_assignments") or [],
            }}
    details = {}
    # 天天基金有明确频控；4 并发即可在十几秒完成，又不会把缺失误判成真实 Gate 失败。
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_detail, row["code"], old.get(row["code"])): row["code"] for row in selected}
        for future in as_completed(futures):
            code = futures[future]
            try:
                details[code] = future.result()
            except Exception as error:
                details[code] = {"assignment": {}, "risk": {}, "errors": [str(error)]}

    _add_detail_scores(selected, details)
    pit_store = _store_observations(selected, details)

    annual_fees_by_type: dict[str, list[float]] = {}
    for row in selected:
        fee = (details.get(row["code"], {}).get("fees") or {}).get("total")
        if fee is not None:
            annual_fees_by_type.setdefault(row["fund_type"], []).append(fee)
    scored = []
    for row in selected:
        peers = [item for item in universe if item["fund_type"] == row["fund_type"]]
        scored.append(_score_candidate(
            row,
            details.get(row["code"], {}),
            [item["manager_aum"] for item in peers if item.get("manager_aum") is not None],
            [item["manager_fund_count"] for item in peers if item.get("manager_fund_count") is not None],
            [item["fee_pct"] for item in peers if item.get("fee_pct") is not None],
            annual_fees_by_type.get(row["fund_type"], []),
        ))
    scored.sort(key=lambda row: row["final_score"], reverse=True)
    counts = {tier: sum(row["tier"] == tier for row in scored) for tier in ("core_buy", "potential_buy", "watch", "review", "exclude")}
    generated = datetime.now(BEIJING)
    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": generated.strftime("%Y-%m-%d %H:%M"),
        "as_of": max((row.get("data_date") or "" for row in scored), default=generated.date().isoformat()),
        "stale": False,
        "universe_count": len(universe),
        "candidate_count": len(scored),
        "tier_counts": counts,
        "pit_store": pit_store,
        "rows": scored,
        "methodology": {
            "name": "PFS V3.0 · Manager-First 公开数据初筛层",
            "formula": "Raw=0.75Q+0.25P；Final=50+C×(Raw−50)−RiskPenalty",
            "quality_weights": {"manager": 35, "process": 25, "verified_skill": 20, "platform": 10, "implementation": 10},
            "potential_weights": {"evidence": 25, "capacity": 25, "bandwidth": 15, "flow": 15, "platform_trend": 10, "implementation": 10},
            "direct_coverage_pct": 44,
            "proxy_coverage_pct": 28,
            "missing_coverage_pct": 28,
            "limitations": _MISSING_EVIDENCE,
            "definitions": [
                "只评价主动股票、偏股混合、灵活配置和平衡混合；指数/ETF、债券与FOF不跨策略排名。",
                "Universe 按公开基金代码/份额统计；深筛前合并同基金的 A/C/E 等重复份额，保留净值证据更完整的代表份额。",
                "产品收益仅在现任团队上任日期覆盖的窗口内参与经理初筛，前任经理历史不归给现任经理。",
                "任期风险与滚动持续性由日净值复算；季度Flow使用真实申购/赎回份额，不解释为日频资金净流入。",
                "历史回填尚无原始公告发布时间，SQLite中标为pit_usable=0；从本次运行开始的构建快照才可用于未来Point-in-Time验证。",
                "Verified Skill 当前仅为同类净值与风险证据初筛，不等于因子 Alpha、持仓 Alpha 或真实 Skill Probability。",
                "Capacity 当前仅用经理现管总规模的同策略相对位置作压力初筛，不等于策略真实容量。",
                "缺失模块固定使用50分中性先验，并由 Confidence 将极端分数向50收缩。",
                "未经 Point-in-Time Walk-Forward 回测和概率校准，不把 PFS 输出称为 Alpha 或自动交易信号。",
            ],
            "sources": [
                {"label": "东方财富基金排行/经理/申赎", "scope": "净值收益、经理职业记录、现管规模与产品数、交易状态", "status": "fresh"},
                {"label": "天天基金基金经理页", "scope": "现任经理本产品上任日期", "status": "fresh"},
                {"label": "雪球基金公开接口（经 AKShare）", "scope": "同类风险收益、抗波动、波动率、夏普与最大回撤", "status": "partial"},
                {"label": "东方财富F10规模与持有人结构", "scope": "季度申购/赎回份额、净资产、机构/个人/内部持有比例", "status": "fresh"},
                {"label": "PFS V3.0 用户方案", "scope": "门槛、Q/P/C/Penalty 公式与分层规则", "status": "methodology"},
            ],
            "source_counts": source_counts,
        },
    }


def _load_cache() -> dict | None:
    try:
        with open(_CACHE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if data.get("schema_version") == _SCHEMA_VERSION and data.get("rows") else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _save_cache(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = _CACHE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temp, _CACHE_FILE)


def get_pfs(force: bool = False) -> dict:
    return cache_runtime.get(
        "fund_pfs:v2", lambda: _build(_load_cache()),
        valid=lambda value: bool(value.get("rows")),
        ttl=_TTL, warm=_load_cache, save=_save_cache, force=force,
    )


def start_scheduler() -> None:
    """工作日 23:30 后执行一次 PFS 日更；已有快照时全程后台刷新。"""
    def loop():
        refreshed_day = ""
        while True:
            time.sleep(60)
            now = datetime.now(BEIJING)
            day = now.date().isoformat()
            if now.weekday() < 5 and (now.hour, now.minute) >= (23, 30) and refreshed_day != day:
                get_pfs(force=True)
                refreshed_day = day
    threading.Thread(target=loop, daemon=True, name="fund-pfs-scheduler").start()


def query_pfs(strategy: str = "", tier: str = "", pool: str = "", keyword: str = "", limit: int = 100, force: bool = False) -> dict:
    data = get_pfs(force)
    rows = data["rows"]
    if strategy:
        rows = [row for row in rows if row["strategy"] == strategy]
    if tier:
        rows = [row for row in rows if row["tier"] == tier]
    if pool == "mature":
        rows = [row for row in rows if (row.get("team_tenure_days") or 0) >= 1095]
    elif pool == "emerging":
        rows = [row for row in rows if 548 <= (row.get("team_tenure_days") or 0) < 1095]
    if keyword:
        query = keyword.strip().lower()
        rows = [row for row in rows if query in row["name"].lower() or row["code"].startswith(query) or query in row["manager"].lower()]
    return {**data, "matched_count": len(rows), "rows": rows[:max(1, min(limit, 200))]}
