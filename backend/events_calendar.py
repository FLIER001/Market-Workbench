"""自选与持仓的事件日历（A 股）。

把接下来一段时间要发生的事排成一条时间轴，每条写明来源：

- 定期报告预约披露日（东财 `RPT_PUBLIC_BS_APPOIN`）：已经实际披露的不再列；改过期的标出改期次数。
- 限售解禁（东财 `RPT_LIFT_STAGE`）：只列已经排定的批次，写解禁股数与占总股本比例。
- 除权除息（东财 `RPT_SHAREBONUS_DET`）：只列已经定下除权除息日的；方案已公告但还没定日子的这一版不列。

边界：
- 只做沪深 A 股。港股、美股、北交所、场外基金不在这些来源里，单独写进 gaps，不当成「没有事件」。
- 某个来源取不到的，逐条写进 gaps；不把「没取到」显示成「没有安排」。
- 代码数量上限 30 只，超出的先按持仓优先，其余写进 gaps。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

import astock
import cache_runtime
from abnormal import board_of

BEIJING = timezone(timedelta(hours=8))
MAX_CODES = 30
ALLOWED_DAYS = (7, 30, 90)
CACHE_TTL = 6 * 3600
WORKERS = 6

PERIOD_NAMES = {"03-31": "一季报", "06-30": "中报", "09-30": "三季报", "12-31": "年报"}


def _r(value: Any, digits: int = 4) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return round(out, digits) if out == out and abs(out) != float("inf") else None


def _day(value: Any) -> str | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _period_label(period: str | None) -> str:
    if not period:
        return "定期报告"
    return f"{period[:4]} 年{PERIOD_NAMES.get(period[5:], '定期报告')}"


def _rows(report: str, code: str, *, sort: str, filters: str = "") -> list[dict]:
    """一份报表的原始行（整份历史，翻两页够用），失败返回空列表。"""
    def fetch() -> list[dict]:
        try:
            from holder_increase import _fetch_pages
            return _fetch_pages(report, f'(SECURITY_CODE="{code}"){filters}',
                                sort_columns=sort, max_pages=2)
        except Exception:  # noqa: BLE001 — 单个来源失败只影响这一类事件
            return []

    return cache_runtime.get(
        f"calendar:{report}:{code}:{filters}",
        fetch,
        valid=lambda v: isinstance(v, list),
        ttl=CACHE_TTL, decorate=False,
    ) or []


def _appointments(code: str, start: date, end: date) -> list[dict]:
    out = []
    for row in _rows("RPT_PUBLIC_BS_APPOIN", code, sort="REPORT_DATE"):
        if _day(row.get("ACTUAL_PUBLISH_DATE")):
            continue                                  # 已经披露过的不算「接下来要发生」
        appointed = _day(row.get("APPOINT_PUBLISH_DATE"))
        if not appointed:
            continue
        day = date.fromisoformat(appointed)
        if not (start <= day <= end):
            continue
        changes = [d for d in (_day(row.get(f)) for f in
                               ("FIRST_CHANGE_DATE", "SECOND_CHANGE_DATE", "THIRD_CHANGE_DATE")) if d]
        period = _day(row.get("REPORT_DATE"))
        detail = _period_label(period)
        first = _day(row.get("FIRST_APPOINT_DATE"))
        if changes:
            detail += f"，改期 {len(changes)} 次（最近 {changes[-1]}）"
        if first and first != appointed:
            detail += f"，首次预约 {first}"
        out.append({"kind": "预约披露", "date": appointed, "detail": detail,
                    "source": "东财·预约披露时间"})
    return out


def _lockups(code: str, start: date, end: date) -> list[dict]:
    out = []
    for row in _rows("RPT_LIFT_STAGE", code, sort="FREE_DATE"):
        day = _day(row.get("FREE_DATE"))
        if not day:
            continue
        when = date.fromisoformat(day)
        if not (start <= when <= end):
            continue
        parts = []
        shares = _r(row.get("CURRENT_FREE_SHARES"), 2)
        ratio = _r(row.get("FREE_RATIO"), 6)
        if shares is not None:
            parts.append(f"解禁 {shares:,.2f} 万股")
        if ratio is not None:
            parts.append(f"占总股本 {ratio * 100:.2f}%")
        kind = str(row.get("FREE_SHARES_TYPE") or "").strip()
        if kind:
            parts.append(kind)
        out.append({"kind": "限售解禁", "date": day, "detail": "，".join(parts) or "解禁",
                    "source": "东财·限售解禁"})
    return out


def _dividends(code: str, start: date, end: date) -> list[dict]:
    out = []
    for row in _rows("RPT_SHAREBONUS_DET", code, sort="EX_DIVIDEND_DATE"):
        day = _day(row.get("EX_DIVIDEND_DATE"))
        if not day:
            continue
        when = date.fromisoformat(day)
        if not (start <= when <= end):
            continue
        detail = str(row.get("IMPL_PLAN_PROFILE") or "").strip()
        record = _day(row.get("EQUITY_RECORD_DATE"))
        if record:
            detail = f"{detail}，股权登记日 {record}" if detail else f"股权登记日 {record}"
        out.append({"kind": "除权除息", "date": day, "detail": detail or "分红方案",
                    "source": "东财·分红送转"})
    return out


def classify(codes: list[str]) -> tuple[list[str], list[str]]:
    """把传进来的代码分成「能算的沪深 A 股」和「说明理由就不算的」。

    板块判定复用 abnormal.board_of：astock.get_prefix 把 9 开头一律当沪市（那是 B 股），
    920xxx 北交所会被它错判成沪市主板。
    """
    usable, skipped = [], []
    for code in codes:
        code = str(code).strip()
        if not (len(code) == 6 and code.isdigit()):
            skipped.append(f"{code}：港股 / 美股 / 基金代码，这套事件来源只覆盖 A 股")
            continue
        if code.startswith(("5", "15", "16", "18", "11", "12")):
            skipped.append(f"{code}：场内基金 / 可转债，没有这类事件")
            continue
        board = board_of(code)
        if board is None:
            skipped.append(f"{code}：B 股或无法识别的代码，没查")
            continue
        if board == "bse":
            skipped.append(f"{code}：北交所，预约披露与解禁来源不覆盖，没查")
            continue
        usable.append(code)
    return usable, skipped


def build_calendar(codes: list[str], days: int = 30) -> dict[str, Any]:
    today = datetime.now(BEIJING).date()
    window_days = days if days in ALLOWED_DAYS else 30
    start, end = today, today + timedelta(days=window_days)

    usable, skipped = classify(list(dict.fromkeys(codes)))
    gaps: list[str] = list(skipped)
    if len(usable) > MAX_CODES:
        gaps.append(f"只查了前 {MAX_CODES} 只 A 股，另有 {len(usable) - MAX_CODES} 只没查")
        usable = usable[:MAX_CODES]

    if not usable:
        return {"as_of": today.isoformat(), "window": {"start": start.isoformat(), "end": end.isoformat(),
                                                      "days": window_days},
                "items": [], "codes": 0, "gaps": gaps, "sources": []}

    try:
        quotes = astock.tencent_quote(usable)
    except Exception:  # noqa: BLE001 — 名称只用于展示
        quotes = {}

    def job(code: str) -> tuple[str, list[dict], list[str]]:
        items, errors = [], []
        for label, fn in (("预约披露", _appointments), ("限售解禁", _lockups), ("除权除息", _dividends)):
            try:
                items.extend(fn(code, start, end))
            except Exception as exc:  # noqa: BLE001 — 单类失败单独报告，不连累其它
                errors.append(f"{code} {label}：{exc}")
        return code, items, errors

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(job, usable))

    items: list[dict] = []
    for code, found, errors in results:
        name = (quotes.get(code) or {}).get("name") or code
        for item in found:
            items.append({"code": code, "name": name, **item})
        gaps.extend(errors)

    # 各来源已按窗口过滤过；这里是唯一出口，再兜一次，任何越界日期都不该出现在日历上
    items = [item for item in items if start.isoformat() <= item["date"] <= end.isoformat()]
    items.sort(key=lambda item: (item["date"], item["code"]))
    return {
        "as_of": today.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": window_days},
        "items": items,
        "codes": len(usable),
        "gaps": gaps,
        "sources": ["东财·预约披露时间", "东财·限售解禁", "东财·分红送转"],
    }
