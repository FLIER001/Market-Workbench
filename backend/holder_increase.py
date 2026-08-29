"""事件分析 · 高管/管理层/股东增持数据层。

两个东财数据中心数据集互补，覆盖「高管、管理层、股东」增持：
- RPT_EXECUTIVE_HOLD_DETAILS 高管持股变动明细：董监高本人及亲属的日级变动，
  含职务（POSITION_NAME）与变动人和董监高的关系（PERSON_DSE_RELATION）。
  无方向字段，用 END_HOLD_NUM > BEGIN_HOLD_NUM 判增持。
- RPT_SHARE_HOLDER_INCREASE 股东增减持（filter DIRECTION="增持"）：股东/大股东
  增持，含公告日、增持区间（START_DATE~END_DATE）、持股比例（HOLD_RATIO）。
  数据集只含已完成的交易记录，没有"增持计划未结束"字段。

时间窗口（与页面语义一致）：
- 1d  近1日 / 7d 过去7日 / 30d 过去30日：按【增持开始日】（披露区间起点）落入窗口；
  无区间的高管单笔变动用其首次披露日（变动日）。进展类披露（如触及1%刻度）
  不会把早已开始的增持重新拉进短窗口。
- all 全部（进行中）：最全口径——回看窗口内所有有增持记录的股票默认入选
  （交易数据无法证明"已结束"），只有出现明确结束信号才剔除：累计增持金额
  已达成计划下限、已过计划期限且期限后无增持、或近期披露实施完毕。计划来自
  个股公告标题+正文解析（最近活跃优先，数量封顶），解析不到就不做排除。

缓存：原始记录+计划一份 last-good（TTL 30 分钟，快照落盘；计划缓存随原始层
一起在后台重拉时刷新），窗口聚合每请求即时计算（纯内存、毫秒级），评分在
同一窗口内按股票聚合。只呈现公开披露事实与透明评分，不做任何买卖建议。
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import astock
import cache_runtime

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
SNAPSHOT = "holder_increase_raw.json"
RAW_KEY = "holder_increase:raw"

WINDOWS = {"1d": 1, "7d": 7, "30d": 30}  # all = 进行中，不按天数
PAGE_SIZE = 500
LOOKBACK_DAYS = 35        # 覆盖 30 日窗口留余量
MAX_PAGES_EXEC = 24       # 高管明细量大（全市场日级变动），翻页封顶防拖死
MAX_PAGES_HOLDER = 8
MAX_PLAN_FETCH = 40       # 单次重建最多解析 N 只候选股的公告计划（按最近活跃优先）

# 身份分层权重（评分之身份分，0-40）。实控人/董事长信号最强，亲属弱于本人。
TIER_WEIGHT = {"chairman": 40, "exec": 30, "big_holder": 25, "relative": 20, "holder": 15}
BIG_HOLDER_RATIO = 5.0    # HOLD_RATIO ≥ 5% 视作大股东

ANN_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
ANN_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"


def _snapshot_path() -> str:
    return os.path.join(DATA_DIR, SNAPSHOT)


def _load_snapshot() -> dict | None:
    try:
        with open(_snapshot_path(), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _save_snapshot(value: dict) -> None:
    """快照落盘 best-effort：失败只意味着重启后需重拉，不让请求失败。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = _snapshot_path()
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temp, path)
    except (OSError, TypeError, ValueError):
        pass


def _now_stamp() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _d(v) -> str:
    return str(v or "")[:10]


# ---------------------------------------------------------------------------
# 拉取（em_get 自带限流/重试/直连降级；数据集量大需翻页）
# ---------------------------------------------------------------------------

def _fetch_pages(report_name: str, filter_str: str, *, sort_columns: str = "",
                 max_pages: int) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        data = astock.eastmoney_datacenter(
            report_name, filter_str=filter_str, page_size=PAGE_SIZE,
            sort_columns=sort_columns, page_number=page)
        if not data:
            break
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            break
    return rows


def _identity_from_position(position: str, relation: str) -> tuple[str, str]:
    """高管明细 → (tier, 身份文案)。职务里含董事长/实际控制人归最高档（副董事长除外）。"""
    pos = (position or "").strip()
    relation = (relation or "").strip()
    if pos and ("实际控制人" in pos or ("董事长" in pos and "副董事长" not in pos)):
        return "chairman", pos
    if relation and relation != "本人":
        return "relative", f"{pos or '董监高'}({relation})" if pos else f"董监高({relation})"
    if pos:
        return "exec", pos
    return "exec", "董监高"


def _fetch_exec_increase(today: date) -> list[dict]:
    """高管持股变动明细 → 增持记录（END > BEGIN）。CHANGE_DATE 不可排序，客户端排序。"""
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    raw = _fetch_pages(
        "RPT_EXECUTIVE_HOLD_DETAILS", f"(CHANGE_DATE>='{cutoff}')",
        max_pages=MAX_PAGES_EXEC)
    rows: list[dict] = []
    for r in raw:
        begin, end = _f(r.get("BEGIN_HOLD_NUM")), _f(r.get("END_HOLD_NUM"))
        if begin is None or end is None or end <= begin:
            continue
        shares = end - begin
        price = _f(r.get("AVERAGE_PRICE")) or 0
        amount = abs(_f(r.get("CHANGE_AMOUNT")) or 0) or shares * price
        tier, identity = _identity_from_position(
            str(r.get("POSITION_NAME") or ""), str(r.get("PERSON_DSE_RELATION") or ""))
        rows.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME") or ""),
            "person": str(r.get("PERSON_NAME") or "未知"),
            "tier": tier, "identity": identity,
            "amount": round(amount, 0),
            "shares": round(shares, 0),
            "price": price or None,
            "activity_date": _d(r.get("CHANGE_DATE")),
            "notice_date": "",
            "start_date": None, "end_date": None,
            "ratio_pct": _f(r.get("CHANGE_RATIO")) or 0,
            "reason": str(r.get("CHANGE_REASON") or ""),
            "market": "", "ongoing": False, "source": "exec",
        })
    rows.sort(key=lambda x: x["activity_date"], reverse=True)
    return rows


def _fetch_holder_increase(today: date) -> list[dict]:
    """股东增减持（DIRECTION=增持）：近 35 天公告 + 区间未结束的全部计划。"""
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent = _fetch_pages(
        "RPT_SHARE_HOLDER_INCREASE",
        f'(DIRECTION="增持")(NOTICE_DATE>=\'{cutoff}\')',
        sort_columns="NOTICE_DATE", max_pages=MAX_PAGES_HOLDER)
    # 区间未结束的计划公告日可能早于回看窗口，单独拉一次
    ongoing = _fetch_pages(
        "RPT_SHARE_HOLDER_INCREASE",
        f'(DIRECTION="增持")(END_DATE>=\'{today.isoformat()}\')',
        sort_columns="NOTICE_DATE", max_pages=MAX_PAGES_HOLDER)
    merged: dict[tuple, dict] = {}
    for r in recent + ongoing:
        key = (r.get("SECURITY_CODE"), r.get("HOLDER_NAME"),
               _d(r.get("TRADE_DATE")), _f(r.get("CHANGE_NUM")))
        merged.setdefault(key, r)
    rows: list[dict] = []
    for r in merged.values():
        shares_wan = _f(r.get("CHANGE_NUM")) or 0          # 万股
        shares = shares_wan * 1e4
        price = _f(r.get("TRADE_AVERAGE_PRICE")) or _f(r.get("REAL_PRICE")) or _f(r.get("CLOSE_PRICE")) or 0
        hold_ratio = _f(r.get("HOLD_RATIO")) or 0
        start, end = _d(r.get("START_DATE")), _d(r.get("END_DATE"))
        notice = _d(r.get("NOTICE_DATE"))
        end_date = end or None
        is_ongoing = bool(end_date and end_date >= today.isoformat())
        rows.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "person": str(r.get("HOLDER_NAME") or "未知"),
            "tier": "big_holder" if hold_ratio >= BIG_HOLDER_RATIO else "holder",
            "identity": f"大股东(持股{hold_ratio:.1f}%)" if hold_ratio >= BIG_HOLDER_RATIO else "股东",
            "amount": round(shares * price, 0),
            "shares": round(shares, 0),
            "price": price or None,
            "activity_date": notice or _d(r.get("TRADE_DATE")),
            "notice_date": notice,
            "start_date": start or None, "end_date": end_date,
            "ratio_pct": _f(r.get("CHANGE_RATE")) or _f(r.get("CHANGE_FREE_RATIO")) or 0,
            "reason": str(r.get("MARKET") or ""),
            "market": str(r.get("MARKET") or ""), "ongoing": is_ongoing, "source": "holder",
        })
    rows.sort(key=lambda x: x["activity_date"], reverse=True)
    return rows


def _dedup(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for r in rows:
        if not r["code"] or not r["activity_date"]:
            continue
        key = (r["code"], r["person"], r["activity_date"], r["shares"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# 增持计划解析（公告标题筛选 + 正文提取计划金额下限/期限；best-effort）
# ---------------------------------------------------------------------------

def _add_months(iso_date: str, months: int) -> str | None:
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return None
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def _parse_plan_amount(text: str) -> tuple[float, str] | None:
    """正文里"不低于/不少于 X 亿元(万元)" → (元, 展示文案)。只要下限，上限不作达成依据。"""
    if not text:
        return None
    m = re.search(r"(?:不低于|不少于)\s*(?:人民币)?\s*([\d,，]+(?:\.\d+)?)\s*(亿|万)\s*元", text)
    if not m:
        return None
    value = float(m.group(1).replace(",", "").replace("，", ""))
    unit = 1e8 if m.group(2) == "亿" else 1e4
    return value * unit, f"≥{m.group(1)}{m.group(2)}元"


def _parse_plan_deadline(text: str, notice_date: str) -> str | None:
    if text:
        m = (re.search(r"(?:至|到|截至|截止(?:于)?)[^。；\n]{0,10}?(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:前|止)?", text)
             or re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:前|止)", text))
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                pass
        m = re.search(r"(\d+)\s*个月", text)
        if m and notice_date:
            return _add_months(notice_date, int(m.group(1)))
    return None


def _is_plan_title(title: str) -> bool:
    return bool(re.search(r"增持", title or "")) and bool(re.search(r"计划|拟", title or "")) \
        and not re.search(r"进展|完成|完毕|结果", title or "")


def _is_done_title(title: str) -> bool:
    return bool(re.search(r"增持", title or "")) and bool(re.search(r"实施完毕|实施完成|完成实施", title or ""))


def _fetch_plan(code: str, today: date) -> dict | None:
    """个股公告 → 增持计划信息。解析不到返回 None（不做排除）。"""
    import requests

    r = requests.get(
        ANN_LIST_URL,
        params={"sr": -1, "page_size": 120, "page_index": 1, "ann_type": "A",
                "client_source": "web", "stock_list": code, "f_node": 0, "s_node": 0},
        headers={"User-Agent": astock.UA}, timeout=15)
    anns = (r.json().get("data") or {}).get("list") or []
    recent_done = any(
        _is_done_title(a.get("title") or "")
        and _d(a.get("notice_date")) >= (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        for a in anns)
    spec = next((a for a in anns if _is_plan_title(a.get("title") or "")), None)
    if not spec and not recent_done:
        return None
    plan: dict = {"title": "", "notice_date": "", "amount": None, "amount_label": "",
                  "deadline": None, "done": recent_done}
    if spec:
        plan["title"] = spec.get("title") or ""
        plan["notice_date"] = _d(spec.get("notice_date"))
        try:
            body_r = requests.get(
                ANN_CONTENT_URL,
                params={"art_code": spec.get("art_code"), "client_source": "web", "page_index": 1},
                headers={"User-Agent": astock.UA}, timeout=15)
            body = ((body_r.json().get("data") or {}).get("notice_content") or "")[:8000]
        except Exception:  # noqa: BLE001 - 正文拿不到就只剩标题信息，不阻塞
            body = ""
        parsed = _parse_plan_amount(body)
        if parsed:
            plan["amount"], plan["amount_label"] = parsed
        plan["deadline"] = _parse_plan_deadline(body, plan["notice_date"])
    return plan


def _plan_candidates(records: list[dict], today: date) -> set[str]:
    """「全部 · 进行中」候选 = 最全口径：回看窗口内所有有增持记录的股票
    （含披露区间未结束的）。交易数据无法证明"已结束"，因此默认全部入选，
    只有出现明确结束信号（计划达成/过期无增持/披露实施完毕）才由
    _plan_ended 剔除；解析不到计划的按仍在增持处理。"""
    return {r["code"] for r in records}


def _plan_ended(plan: dict | None, cum_amount: float, last_buy: str, today: date) -> bool:
    """计划口径判断增持是否已结束。只有仍适用（近期发布/未到期）的计划才参与判定。"""
    if not plan:
        return False
    today_iso = today.isoformat()
    if plan.get("done"):
        return True
    deadline = plan.get("deadline")
    if deadline and today_iso > deadline and last_buy <= deadline:
        return True   # 已过期限且期限后再无增持
    amount = plan.get("amount")
    if amount and cum_amount >= amount:
        return True   # 累计金额已达成计划下限
    return False


# ---------------------------------------------------------------------------
# 窗口聚合 + 评分（每请求即时计算，纯内存）
# ---------------------------------------------------------------------------

def _amount_score(total: float) -> int:
    if total >= 1e8:
        return 25
    if total >= 3e7:
        return 20
    if total >= 1e7:
        return 15
    if total >= 3e6:
        return 10
    if total >= 1e6:
        return 6
    return 3 if total > 0 else 0


def _ratio_score(max_ratio: float) -> int:
    # ratio_pct 为百分比：≥0.5% 满档，向下递减（档位按真实披露粒度校准）
    if max_ratio >= 0.5:
        return 15
    if max_ratio >= 0.2:
        return 11
    if max_ratio >= 0.1:
        return 8
    if max_ratio >= 0.05:
        return 5
    return 2 if max_ratio > 0 else 0


def _score(rows: list[dict], today: date) -> tuple[int, dict]:
    identity = max(TIER_WEIGHT[r["tier"]] for r in rows)
    amount = _amount_score(sum(r["amount"] or 0 for r in rows))
    ratio = _ratio_score(max(r["ratio_pct"] or 0 for r in rows))
    people = len({r["person"] for r in rows})
    count = 10 if len(rows) >= 3 or people >= 2 else 7 if len(rows) == 2 else 4
    if any(r["ongoing"] for r in rows):
        recency = 10
    else:
        latest = max(r["activity_date"] for r in rows)
        days = (today - date.fromisoformat(latest)).days
        recency = 8 if days <= 1 else 6 if days <= 3 else 3 if days <= 7 else 1
    score = identity + amount + ratio + count + recency
    return score, {"identity": identity, "amount": amount, "ratio": ratio,
                   "count": count, "recency": recency}


def _grade(score: int) -> str:
    return "strong" if score >= 70 else "watch" if score >= 55 else "normal"


def _anchor_date(r: dict) -> str:
    """窗口筛选锚点：增持开始日（区间起点）优先，无区间则用首次披露日。"""
    return r["start_date"] or r["activity_date"]


def _period_text(rows: list[dict]) -> str:
    """区间展示取披露的 START~END。数据集混着两种行：批次行（触刻度披露的
    实际买入区间）与累计行（整轮增持一条，起点≈计划/窗口起点、金额累计）。
    跨年区间带全年份，避免"12-09 ~ 08-22"式的年份歧义。"""
    starts = [r["start_date"] for r in rows if r["start_date"]]
    ends = [r["end_date"] for r in rows if r["end_date"]]
    fmt = lambda d: d.replace("-", "/")  # noqa: E731
    if starts or ends:
        lo = min(starts) if starts else None
        hi = max(ends) if ends else None
        if lo and hi:
            if lo[:4] != hi[:4]:
                return f"{fmt(lo)} ~ {fmt(hi)}"
            return f"{fmt(lo)[5:]} ~ {fmt(hi)[5:]}" if lo != hi else fmt(lo)[5:]
        return fmt(lo or hi)[5:] if (lo or hi)[:4] == str(datetime.now(BEIJING).year) else fmt(lo or hi)
    return max(r["activity_date"] for r in rows)[5:].replace("-", "/")


def _is_cumulative(rows: list[dict]) -> bool:
    """区间跨度 > 45 天视为累计披露行（整轮增持一条），而非单批买入。"""
    for r in rows:
        if r["start_date"] and r["end_date"]:
            try:
                span = (date.fromisoformat(r["end_date"]) - date.fromisoformat(r["start_date"])).days
            except ValueError:
                continue
            if span > 45:
                return True
    return False


def _aggregate(records: list[dict], window: str, plans: dict | None = None) -> list[dict]:
    today = datetime.now(BEIJING).date()
    plans = plans or {}
    if window == "all":
        codes = _plan_candidates(records, today)
        picked: set[str] = set()
        for code in codes:
            rows_of_code = [r for r in records if r["code"] == code]
            plan = plans.get(code)
            if _plan_active(plan, today):
                notice = (plan.get("notice_date") or "") if plan else ""
                cum = sum(r["amount"] or 0 for r in rows_of_code
                          if not notice or r["activity_date"] >= notice)
                last_buy = max(r["activity_date"] for r in rows_of_code)
                if _plan_ended(plan, cum, last_buy, today):
                    continue
            picked.add(code)
        group_rows = [r for r in records if r["code"] in picked]
    else:
        # 窗口锚点 = 增持开始日（披露区间的起点）；无区间（高管单笔变动）时用首次披露日。
        # 进展类公告（如触及1%刻度）不会把早已开始的增持重新拉进短窗口。
        cutoff = (today - timedelta(days=WINDOWS[window])).isoformat()
        group_rows = [r for r in records if _anchor_date(r) >= cutoff]
    groups: dict[str, list[dict]] = {}
    for r in group_rows:
        if not r["code"]:
            continue
        groups.setdefault(r["code"], []).append(r)
    out: list[dict] = []
    for code, rows in groups.items():
        rows.sort(key=lambda x: x["activity_date"], reverse=True)
        best = min(rows, key=lambda x: -TIER_WEIGHT[x["tier"]])
        score, breakdown = _score(rows, today)
        out.append({
            "code": code, "name": rows[0]["name"] or code,
            "score": score, "grade": _grade(score), "breakdown": breakdown,
            "tier": best["tier"], "identity": best["identity"],
            "people": len({r["person"] for r in rows}), "count": len(rows),
            "total_amount": sum(r["amount"] or 0 for r in rows),
            "latest_date": rows[0]["activity_date"],
            "period": _period_text(rows),
            "cumulative": _is_cumulative(rows),
            "ongoing": any(r["ongoing"] for r in rows),
            "plan": plans.get(code),
            "records": rows[:12],
        })
    out.sort(key=lambda x: x["latest_date"], reverse=True)  # 先按新近度，稳定排序保证同分内日期优先
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def _plan_active(plan: dict | None, today: date) -> bool:
    """计划信息是否仍适用：近期披露的实施完毕、未到期的期限、或近期发布的计划。
    太老且已到期的计划不参与排除（可能存在未抓到的新计划，持续增持代理为准）。"""
    if not plan:
        return False
    if plan.get("done"):
        return True
    deadline = plan.get("deadline")
    if deadline and deadline >= today.isoformat():
        return True
    notice = plan.get("notice_date") or ""
    return bool(notice) and notice >= (today - timedelta(days=200)).isoformat()


def _build_raw() -> dict:
    today = datetime.now(BEIJING).date()
    records = _dedup(_fetch_exec_increase(today) + _fetch_holder_increase(today))
    if not records:
        raise ValueError("高管/股东增持数据为空")
    payload: dict = {"records": records, "updated": _now_stamp(),
                     "source": "eastmoney", "plans": {}}
    candidates = _plan_candidates(records, today)
    # 计划解析量有限，优先覆盖最近仍有增持的股票
    ordered = sorted(
        candidates,
        key=lambda c: max((r["activity_date"] for r in records if r["code"] == c), default=""),
        reverse=True)
    plans: dict[str, dict] = {}
    for code in ordered[:MAX_PLAN_FETCH]:
        try:
            plan = _fetch_plan(code, today)
        except Exception:  # noqa: BLE001 - 单只公告失败不影响整体
            plan = None
        if plan:
            plans[code] = plan
        time.sleep(0.3)
    payload["plans"] = plans
    return payload


def _valid_raw(value) -> bool:
    return isinstance(value, dict) and bool(value.get("records"))


def get_holder_increase(window: str, force: bool = False) -> dict:
    """窗口聚合结果。原始层走 cache_runtime last-good；force 令后台重拉，先返回旧值。"""
    raw = cache_runtime.get(
        RAW_KEY, _build_raw, valid=_valid_raw, ttl=1800,
        warm=_load_snapshot, save=_save_snapshot, force=force)
    records = raw.get("records") or []
    return {
        "window": window, "updated": raw.get("updated"), "source": raw.get("source", "eastmoney"),
        "cache_state": raw.get("cache_state"), "cached_at": raw.get("cached_at"),
        "data_as_of": raw.get("data_as_of"), "refresh_error": raw.get("refresh_error"),
        "total_records": len(records),
        "rows": _aggregate(records, window, raw.get("plans")),
    }
