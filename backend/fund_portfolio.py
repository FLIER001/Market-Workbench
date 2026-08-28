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


def _year_start_nav(code: str, year: int) -> float | None:
    """本年度首个净值公布日的单位净值（历史定值，取一次落账本即可）。"""
    try:
        rows = fund.nav_history(code, limit=260).get("rows", [])
    except Exception:
        return None
    return next((r["nav"] for r in rows if str(r.get("date", "")).startswith(str(year))), None)

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


def _snap_estimates(user_id: int | None = None) -> dict:
    """从当前响应缓存里抠出「行情部分」（每只基金的净值/估值/日收益），供写操作
    秒回时复用。缓存不存在时返回空 dict——新增持仓的净值/估值列显示空，后台
    预热或下次刷新补上，不阻塞写操作。"""
    with _LOCK:
        snap = _RESP_CACHE.get(_cache_key(user_id))
    if not snap:
        return {}
    out: dict = {}
    for row in snap.get("holdings", []):
        code = row.get("code")
        if not code:
            continue
        out[code] = {k: row.get(k) for k in (
            "name", "nav", "nav_date", "estimate_pct", "estimate_time",
            "estimate_source", "estimate_stale", "estimate_proxy",
            "today_return_pct", "today_return_date", "today_return_per_share",
            "today_return_base_per_share", "yesterday_return_pct",
            "yesterday_return_date", "yesterday_return_per_share",
            "yesterday_return_base_per_share")}
    for c in snap.get("closed", []):
        code = c.get("code")
        if not code or code in out:
            continue
        # 快照里清仓记录只存了涨跌幅：按 卖出净值 × (1 + pct) 反推现净值，
        # 写操作秒回时「清仓后」列不闪空（两位小数往返无损）。
        nav = c.get("nav") or 0.0
        pct = c.get("post_close_pct")
        out[code] = {"name": c.get("name"),
                     "nav": nav * (1 + pct / 100) if nav and pct is not None else None}
    return out


def _respond(user_id: int | None = None) -> dict:
    """写操作统一出口：用旧快照行情拼新账本，秒回；旧快照缺失时（服务器重启后
    第一次写）才走真重算，保住正确性。"""
    est = _snap_estimates(user_id)
    if est or not _load(user_id).get("holdings"):
        return get_portfolio(reuse_estimates=est, user_id=user_id)
    return get_portfolio(user_id=user_id)


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


def _clean_bought_date(date: str | None) -> str | None:
    """买入日期规整为 YYYY-MM-DD；不合法返回 None（等价于未填写）。"""
    date = (date or "").strip()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None
    return date


def add_holding(code: str, shares: float, cost: float, bought_date: str | None = None,
                user_id: int | None = None) -> dict:
    """加一笔基金持仓（份额 + 单位成本净值）；同代码按加权平均合并。

    bought_date 记录最早一笔的买入日期，用于本年盈亏分段：
    年前买入按年初净值作基准、年内买入按成本净值作基准。未填（旧账本）按年前买入处理。
    """
    bd = _clean_bought_date(bought_date)
    with _LOCK:
        d = _load(user_id)
        for h in d["holdings"]:
            if h["code"] == code:
                total = h["shares"] + shares
                h["cost"] = round((h["shares"] * h["cost"] + shares * cost) / total, 4) if total else cost
                h["shares"] = total
                old_bd = h.get("bought_date")
                if bd and old_bd and bd < old_bd:
                    h["bought_date"] = bd
                break
        else:
            entry = {"code": code, "shares": shares, "cost": cost}
            if bd:
                entry["bought_date"] = bd
            d["holdings"].append(entry)
        _save(d, user_id)
    _invalidate(user_id)
    return _respond(user_id)


def update_holding(code: str, shares: float, cost: float, bought_date: str | None = None,
                   user_id: int | None = None) -> dict:
    """直接改一笔基金持仓的份额 / 成本净值 / 买入日期（录错更正用，覆盖而非合并）。

    bought_date 传 None 表示保持原值不变（区别于空串 = 清除日期）。
    """
    with _LOCK:
        d = _load(user_id)
        for h in d["holdings"]:
            if h["code"] == code:
                h["shares"] = shares
                h["cost"] = cost
                if bought_date is not None:
                    bd = _clean_bought_date(bought_date)
                    if bd:
                        h["bought_date"] = bd
                    else:
                        h.pop("bought_date", None)
                break
        else:
            raise ValueError(f"持仓中没有 {code}，无法修改")
        _save(d, user_id)
    _invalidate(user_id)
    return _respond(user_id)


def remove_holding(code: str, user_id: int | None = None) -> dict:
    with _LOCK:
        d = _load(user_id)
        d["holdings"] = [h for h in d["holdings"] if h["code"] != code]
        _save(d, user_id)
    _invalidate(user_id)
    return _respond(user_id)


def close_position(code: str, date: str, nav: float, shares: float, cost: float | None = None,
                   user_id: int | None = None) -> dict:
    """记一笔已卖出：按卖出净值算已实现盈亏，存 closed 并扣减当前份额。"""
    # 先在锁外取基金名（fund_meta 冷缓存要拉全量名单 ~5s，别堵账本锁）：
    # 快照缓存里有名字就直接用（零网络）；没有再走 fund_meta（它有自己的进程级缓存）。
    snap_est = _snap_estimates(user_id)
    cached_name = (snap_est.get(code) or {}).get("name")
    name = cached_name or code
    if not cached_name:
        try:
            meta = fund.fund_meta(code)
            if meta:
                name = meta["name"]
        except Exception:
            pass
    with _LOCK:
        d = _load(user_id)
        holding = next((h for h in d["holdings"] if h["code"] == code), None)
        if cost is None:
            if holding is None:
                raise ValueError("当前持仓中没有该基金，请补充买入成本净值")
            cost = holding["cost"]
        pnl = (nav - cost) * shares
        d.setdefault("closed", [])
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
    return _respond(user_id)


def remove_closed(index: int, user_id: int | None = None) -> dict:
    with _LOCK:
        d = _load(user_id)
        cl = d.get("closed", [])
        if 0 <= index < len(cl):
            cl.pop(index)
            _save(d, user_id)
    _invalidate(user_id)
    return _respond(user_id)


def replace_ledger(d: dict, user_id: int | None = None) -> None:
    """用校验过的基金账本整体替换当前账本（数据导入用）。结构异常抛 ValueError。"""
    if not isinstance(d, dict) or not isinstance(d.get("holdings"), list):
        raise ValueError("基金持仓账本格式不对（缺 holdings 列表）")
    for h in d["holdings"]:
        if not isinstance(h, dict):
            raise ValueError("持仓条目必须是对象")
        code = h.get("code")
        if not isinstance(code, str) or not (6 <= len(code) <= 12):
            raise ValueError(f"基金代码不合法: {code!r}")
        for field in ("shares", "cost"):
            v = h.get(field)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{code} 的 {field} 必须是数字")
        if h.get("shares") <= 0:
            raise ValueError(f"{code} 的份额必须大于 0")
    if not isinstance(d.get("closed", []), list):
        raise ValueError("已卖出记录必须是列表")
    with _LOCK:
        _save(d, user_id)
    _invalidate(user_id)


def get_portfolio(bypass: bool = False, user_id: int | None = None,
                  reuse_estimates: dict | None = None) -> dict:
    """读基金持仓 + 最新净值/盘中估值，算浮盈与最近两个确认净值日收益。

    默认走进程内响应缓存（后台预热/入场补刷写入），秒回；bypass=True 真重算。
    reuse_estimates：写操作（加/删/卖出）秒回时传入上次快照的行情，跳过网络
    拉取直接拼新账本——账本变了但行情没变，盈亏数字与旧快照一致即可接受，
    新行情由下一次后台预热/刷新补上。
    """
    if not bypass and reuse_estimates is None:
        with _LOCK:
            if _cache_key(user_id) in _RESP_CACHE:
                return _RESP_CACHE[_cache_key(user_id)]
    with _LOCK:
        d = _load(user_id)
    hs = d.get("holdings", [])
    # 行情源：已清仓记录也要最新净值（算「清仓后」），未持仓的代码一并拉
    codes = list(dict.fromkeys([h["code"] for h in hs] + [c["code"] for c in d.get("closed", [])]))
    est: dict = {}
    if reuse_estimates is not None:
        est = reuse_estimates
    elif codes:
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
            "bought_date": h.get("bought_date"),
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
    # 卖出后至今涨跌幅 = 最新净值 / 卖出净值 - 1；最新净值取不到时留 None，前端显示占位
    closed = [
        {**c, "post_close_pct": (
            round((est.get(c["code"], {}).get("nav", 0.0) - c["nav"]) / c["nav"] * 100, 2)
            if c.get("nav") and (est.get(c["code"], {}) or {}).get("nav") else None
        )}
        for c in closed
    ]
    # 本年盈亏（YTD）分段口径（同场内证券）：年前买入按年初净值 → 最新净值，
    # 年内买入按成本净值 → 最新净值；并入本年已卖出的实现盈亏。
    # 年初净值是历史定值，只在需要时拉一次并落账本（ytd_open），之后直接重放。
    year_now = datetime.now(BEIJING).year
    ytd_pnl = 0.0
    ytd_base = 0.0
    has_ytd = False
    if hs:
        stored = d.get("ytd_open") if d.get("ytd_year") == year_now else None
        stored = dict(stored or {})
        missing = [
            h["code"] for h in hs
            if (h.get("bought_date") or "0000")[:4] < str(year_now) and h["code"] not in stored
        ]
        if missing:
            fetched = {}
            for c in missing:
                v = _year_start_nav(c, year_now)
                if v:
                    fetched[c] = v
            stored.update(fetched)
            with _LOCK:
                d2 = _load(user_id)
                if year_now != d2.get("ytd_year"):
                    d2["ytd_year"] = year_now
                    d2["ytd_open"] = {}
                d2.setdefault("ytd_open", {}).update(fetched)
                _save(d2, user_id)
        for h in hs:
            nav_now = (est.get(h["code"], {}) or {}).get("nav") or 0.0
            if not nav_now:
                continue
            if (h.get("bought_date") or "0000")[:4] < str(year_now):
                year_start = stored.get(h["code"])
                if year_start:
                    ytd_pnl += (nav_now - year_start) * h["shares"]
                    ytd_base += year_start * h["shares"]
                    has_ytd = True
            else:
                ytd_pnl += (nav_now - h["cost"]) * h["shares"]
                ytd_base += h["cost"] * h["shares"]
                has_ytd = True
    if closed:
        for c in closed:
            if str(c.get("date", ""))[:4] == str(year_now):
                ytd_pnl += c.get("pnl", 0.0)
                ytd_base += c.get("cost", 0.0) * c.get("shares", 0.0)
                has_ytd = True
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
        "ytd_pnl": round(ytd_pnl, 2) if has_ytd else None,
        "ytd_pnl_pct": round(ytd_pnl / ytd_base * 100, 2) if (has_ytd and ytd_base) else None,
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
