"""基金持仓账本 —— 用户自己录入的基金持仓 + 实时估值/最新净值叠加浮动盈亏。

与股票持仓（portfolio.py）同构：存本地用户数据目录（VR_DATA_DIR 或
~/.vibe-research/fund_portfolio.json），不上传、不进仓库。区别在行情源：
基金看「最新公布净值」，交易时段叠加天天基金盘中估值（推算值，与净值分列）。

- 市值/盈亏按最新公布净值计算（确定值）；
- 当日估值收益 = 估算涨跌幅 × 昨日净值市值（交易时段参考，x2rr/funds 同款口径）；
- 加仓按金额+确认份额录入（基金按份额确认），同代码按加权平均成本合并；
- 卖出记已实现盈亏并存 closed 列表。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone, timedelta

import fund

CACHE_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
FPF_FILE = os.path.join(CACHE_DIR, "fund_portfolio.json")
BEIJING = timezone(timedelta(hours=8))
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")


def _estimate_is_current() -> bool:
    """盘中估值只在当日开盘后可作为今日收益补位，避免凌晨沿用上一交易日估值。"""
    now = datetime.now(BEIJING)
    return now.weekday() < 5 and now.hour * 60 + now.minute >= 9 * 60 + 15


def _load() -> dict:
    try:
        with open(FPF_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"holdings": [], "closed": [], "last_refresh": None}


def _save(d: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = FPF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, FPF_FILE)


def add_holding(code: str, shares: float, cost: float) -> dict:
    """加一笔基金持仓（份额 + 单位成本净值）；同代码按加权平均合并。"""
    with _LOCK:
        d = _load()
        for h in d["holdings"]:
            if h["code"] == code:
                total = h["shares"] + shares
                h["cost"] = round((h["shares"] * h["cost"] + shares * cost) / total, 4) if total else cost
                h["shares"] = total
                break
        else:
            d["holdings"].append({"code": code, "shares": shares, "cost": cost})
        _save(d)
    return get_portfolio()


def remove_holding(code: str) -> dict:
    with _LOCK:
        d = _load()
        d["holdings"] = [h for h in d["holdings"] if h["code"] != code]
        _save(d)
    return get_portfolio()


def close_position(code: str, date: str, nav: float, shares: float, cost: float | None = None) -> dict:
    """记一笔已卖出：按卖出净值算已实现盈亏，存 closed 并扣减当前份额。"""
    with _LOCK:
        d = _load()
        holding = next((h for h in d["holdings"] if h["code"] == code), None)
        if cost is None:
            if holding is None:
                raise ValueError("当前持仓中没有该基金，请补充买入成本净值")
            cost = holding["cost"]
        pnl = (nav - cost) * shares
        d.setdefault("closed", [])
        name = code
        try:
            meta = fund.fund_meta(code)
            if meta:
                name = meta["name"]
        except Exception:
            pass
        d["closed"].append({
            "code": code, "name": name, "date": date, "nav": nav,
            "shares": shares, "cost": cost, "pnl": round(pnl, 2),
            "pnl_pct": round((nav - cost) / cost * 100, 2) if cost else 0.0,
        })
        if holding is not None:
            remain = holding["shares"] - shares
            if remain > 1e-9:
                holding["shares"] = remain
            else:
                d["holdings"] = [h for h in d["holdings"] if h["code"] != code]
        _save(d)
    return get_portfolio()


def remove_closed(index: int) -> dict:
    with _LOCK:
        d = _load()
        cl = d.get("closed", [])
        if 0 <= index < len(cl):
            cl.pop(index)
            _save(d)
    return get_portfolio()


def get_portfolio() -> dict:
    """读基金持仓 + 最新净值/盘中估值，算浮盈与最近两个确认净值日收益。"""
    with _LOCK:
        d = _load()
    hs = d.get("holdings", [])
    codes = [h["code"] for h in hs]
    est: dict = {}
    if codes:
        try:
            est = fund.realtime_estimates(codes)
        except Exception:
            est = {}
    rows, tmv, tcost, tday, ttoday, ttoday_base, tyesterday, tyesterday_base = [], 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    has_estimate = False
    today_actual_count = today_estimate_count = yesterday_count = 0
    estimate_is_current = _estimate_is_current()
    for h in hs:
        q = est.get(h["code"], {})
        meta_name = q.get("name") or h["code"]
        nav = q.get("nav") or 0.0
        mv = nav * h["shares"]
        cv = h["cost"] * h["shares"]
        pnl = mv - cv
        est_pct = q.get("estimate_pct")
        day_pnl = mv * est_pct / 100 if (est_pct is not None and mv) else None
        today_pnl = q.get("today_return_per_share")
        today_base = q.get("today_return_base_per_share")
        yesterday_pnl = q.get("yesterday_return_per_share")
        yesterday_base = q.get("yesterday_return_base_per_share")
        # ponytail: 账本没有逐日份额，按当前份额回算；有交易流水时再按确认日份额重建。
        today_pnl = today_pnl * h["shares"] if today_pnl is not None else None
        today_base = today_base * h["shares"] if today_base is not None else None
        yesterday_pnl = yesterday_pnl * h["shares"] if yesterday_pnl is not None else None
        yesterday_base = yesterday_base * h["shares"] if yesterday_base is not None else None
        if today_pnl is not None:
            ttoday += today_pnl
            if today_base is not None:
                ttoday_base += today_base
            today_actual_count += 1
        elif day_pnl is not None and estimate_is_current:
            ttoday += day_pnl
            ttoday_base += mv
            today_estimate_count += 1
        if yesterday_pnl is not None:
            tyesterday += yesterday_pnl
            if yesterday_base is not None:
                tyesterday_base += yesterday_base
            yesterday_count += 1
        if day_pnl is not None:
            tday += day_pnl
            has_estimate = True
        rows.append({
            "code": h["code"], "name": meta_name,
            "nav": nav, "nav_date": q.get("nav_date"),
            "estimate_pct": est_pct, "estimate_time": q.get("estimate_time"),
            "estimate_source": q.get("estimate_source"),
            "estimate_stale": bool(q.get("estimate_stale")),
            "estimate_proxy": q.get("estimate_proxy"),
            "shares": h["shares"], "cost": h["cost"],
            "market_value": round(mv, 2), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / cv * 100, 2) if cv else 0.0,
            "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
            "today_return_amount": round(today_pnl, 2) if today_pnl is not None else None,
            "today_return_pct": q.get("today_return_pct"),
            "today_return_date": q.get("today_return_date"),
            "yesterday_return_amount": round(yesterday_pnl, 2) if yesterday_pnl is not None else None,
            "yesterday_return_pct": q.get("yesterday_return_pct"),
            "yesterday_return_date": q.get("yesterday_return_date"),
        })
        tmv += mv
        tcost += cv
    total_pnl = tmv - tcost
    closed = d.get("closed", [])
    return {
        "holdings": rows,
        "totals": {
            "market_value": round(tmv, 2), "cost": round(tcost, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / tcost * 100, 2) if tcost else 0.0,
            "day_estimate_pnl": round(tday, 2) if has_estimate else None,
            "today_pnl": round(ttoday, 2) if today_actual_count + today_estimate_count else None,
            "today_pnl_pct": round(ttoday / ttoday_base * 100, 2) if ttoday_base else None,
            "yesterday_pnl": round(tyesterday, 2) if yesterday_count else None,
            "yesterday_pnl_pct": round(tyesterday / tyesterday_base * 100, 2) if tyesterday_base else None,
        },
        "closed": closed,
        "realized_pnl": round(sum(c.get("pnl", 0) for c in closed), 2),
        "updated": _now(),
        "last_refresh": d.get("last_refresh"),
    }
