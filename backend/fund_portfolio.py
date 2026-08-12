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
import time
from datetime import datetime, timezone, timedelta

import fund

CACHE_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
FPF_FILE = os.path.join(CACHE_DIR, "fund_portfolio.json")
BEIJING = timezone(timedelta(hours=8))
_LOCK = threading.Lock()

# 进程内「刷新好的基金持仓响应」缓存：后台预热 / 点进页面强制重算后写入，
# GET /api/fund-portfolio 直接返回它，用户看到的就是上次刷新算好的收益。
_RESP_CACHE: dict[str, dict] = {}


def _cache_key(user_id: int | None) -> str:
    return str(user_id) if user_id is not None else "legacy"


def _path(user_id: int | None) -> str:
    return FPF_FILE if user_id is None else os.path.join(CACHE_DIR, f"fund_portfolio.user-{user_id}.json")


def write_response_cache(snap: dict, user_id: int | None = None) -> None:
    with _LOCK:
        _RESP_CACHE[_cache_key(user_id)] = snap


def _invalidate(user_id: int | None = None) -> None:
    """账本结构变化（加/删/卖出）后，下次 GET 不再沿用旧缓存。"""
    with _LOCK:
        if user_id is None:
            _RESP_CACHE.clear()
        else:
            _RESP_CACHE.pop(_cache_key(user_id), None)


def _now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")


def _estimate_is_current() -> bool:
    """盘中估值只在当日开盘后可作为今日收益补位，避免凌晨沿用上一交易日估值。"""
    now = datetime.now(BEIJING)
    return now.weekday() < 5 and now.hour * 60 + now.minute >= 9 * 60 + 15


def _load(user_id: int | None = None) -> dict:
    try:
        with open(_path(user_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"holdings": [], "closed": [], "last_refresh": None}


def _save(d: dict, user_id: int | None = None) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _path(user_id)
    if os.path.exists(path):
        try:
            import shutil
            shutil.copy2(path, path + ".backup")
        except OSError:
            pass
    d["version"] = int(d.get("version", 0)) + 1
    d["ledger_updated_at"] = _now()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


def add_holding(code: str, shares: float, cost: float, user_id: int | None = None) -> dict:
    """加一笔基金持仓（份额 + 单位成本净值）；同代码按加权平均合并。"""
    with _LOCK:
        d = _load(user_id)
        for h in d["holdings"]:
            if h["code"] == code:
                total = h["shares"] + shares
                h["cost"] = round((h["shares"] * h["cost"] + shares * cost) / total, 4) if total else cost
                h["shares"] = total
                break
        else:
            d["holdings"].append({"code": code, "shares": shares, "cost": cost})
        _save(d, user_id)
    _invalidate(user_id)
    return get_portfolio(user_id=user_id)


def remove_holding(code: str, user_id: int | None = None) -> dict:
    with _LOCK:
        d = _load(user_id)
        d["holdings"] = [h for h in d["holdings"] if h["code"] != code]
        _save(d, user_id)
    _invalidate(user_id)
    return get_portfolio(user_id=user_id)


def close_position(code: str, date: str, nav: float, shares: float, cost: float | None = None,
                   user_id: int | None = None) -> dict:
    """记一笔已卖出：按卖出净值算已实现盈亏，存 closed 并扣减当前份额。"""
    with _LOCK:
        d = _load(user_id)
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
        _save(d, user_id)
    _invalidate(user_id)
    return get_portfolio(user_id=user_id)


def remove_closed(index: int, user_id: int | None = None) -> dict:
    with _LOCK:
        d = _load(user_id)
        cl = d.get("closed", [])
        if 0 <= index < len(cl):
            cl.pop(index)
            _save(d, user_id)
    _invalidate(user_id)
    return get_portfolio(user_id=user_id)


def get_portfolio(bypass: bool = False, user_id: int | None = None) -> dict:
    """读基金持仓 + 最新净值/盘中估值，算浮盈与最近两个确认净值日收益。

    默认走进程内响应缓存（后台预热/入场补刷写入），秒回；bypass=True 真重算。
    """
    if not bypass:
        with _LOCK:
            if _cache_key(user_id) in _RESP_CACHE:
                return _RESP_CACHE[_cache_key(user_id)]
    with _LOCK:
        d = _load(user_id)
    hs = d.get("holdings", [])
    codes = [h["code"] for h in hs]
    est: dict = {}
    if codes:
        try:
            est = fund.realtime_estimates(codes, bypass=bypass)
        except TypeError:
            # 测试/旧桩不带 bypass 参数
            try:
                est = fund.realtime_estimates(codes)
            except Exception:
                est = {}
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
    result = {
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
        "estimate_as_of": max((str(h.get("estimate_time") or "") for h in rows), default=None),
        "nav_as_of": max((str(h.get("nav_date") or "") for h in rows), default=None),
        "ledger_updated_at": d.get("ledger_updated_at"),
        "version": d.get("version", 0),
    }
    write_response_cache(result, user_id)
    return result


def _refresh_mode() -> str | None:
    """基金后台预热窗口（工作日）：
    - 交易时段 9:15-11:30 / 13:00-15:00：盘中估值有效，刷缓存供秒开；
    - 净值更新时段 19:00-23:00：当日确认净值陆续公布，刷新今日收益。
    下午 15:00-19:00 估值/净值都不变，跳过不刷。
    """
    now = datetime.now(BEIJING)
    if now.weekday() >= 5:
        return None
    mins = now.hour * 60 + now.minute
    trading = (9 * 60 + 15 <= mins <= 11 * 60 + 30) or (13 * 60 <= mins <= 15 * 60)
    nav_update = 19 * 60 <= mins <= 23 * 60 + 30
    return "estimate" if trading else "nav" if nav_update else None


def _is_refresh_hours() -> bool:
    return _refresh_mode() is not None


def _refresh_snapshot(user_id: int | None = None) -> None:
    """后台定时任务：绕过短缓存重算持仓收益并落时间戳——用户点进来直接看刷新好的缓存。

    先失效 fund 层的行情/估值短缓存（rt_est_/rt_stock_/proxy_/ovl_），否则 60s 内的
    旧估值会被原样返回，「预热」形同虚设。算完写回基金持仓响应缓存，GET 即刻可用。
    """
    fund.invalidate("rt_est_", "rt_stock_", "proxy_", "ovl_")
    snap = get_portfolio(bypass=True, user_id=user_id)
    write_response_cache(snap, user_id)
    with _LOCK:
        d = _load(user_id)
        d["last_refresh"] = _now()
        _save(d, user_id)


def start_scheduler(interval: int = 120, user_ids=lambda: ()) -> None:
    """盘中每 2 分钟刷新估值；19:00-23:00 每 30 分钟追踪确认净值。"""
    def loop():
        last_nav = 0.0
        while True:
            time.sleep(interval)
            mode = _refresh_mode()
            if mode is None or (mode == "nav" and time.time() - last_nav < 1800):
                continue
            try:
                for user_id in user_ids():
                    _refresh_snapshot(user_id)
                if mode == "nav":
                    last_nav = time.time()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()


def legacy_status(user_id: int) -> dict:
    legacy, target = _load(None), _load(user_id)
    return {"available": bool(legacy.get("holdings") or legacy.get("closed")),
            "target_empty": not bool(target.get("holdings") or target.get("closed")),
            "holdings": len(legacy.get("holdings") or []), "closed": len(legacy.get("closed") or [])}


def import_legacy(user_id: int) -> dict:
    import shutil
    status = legacy_status(user_id)
    if not status["available"]:
        raise ValueError("没有可导入的旧基金持仓")
    if not status["target_empty"]:
        raise ValueError("当前账号已有基金持仓，不能覆盖")
    legacy = _load(None)
    shutil.copy2(FPF_FILE, FPF_FILE + f".pre-user-import-{datetime.now(BEIJING):%Y%m%d-%H%M%S}.backup")
    _save(legacy, user_id)
    _invalidate(user_id)
    return get_portfolio(user_id=user_id)
