"""持仓数据层 —— 用户自己录入的持仓 + 实时行情叠加浮动盈亏。

合规：持仓是用户主动录入的自己的标的（存本地 ~/.vibe-research/portfolio.json，
不上传、不进仓库），不预置任何标的、不含 _SEED 兜底、不做推荐。
盈亏红涨绿跌（A股口径）。含每半小时后台定时刷新 + 手动刷新。

存储位置：默认用户目录 ~/.vibe-research/（可用 VR_DATA_DIR 覆盖）——
放仓库外，重新下载/覆盖项目文件夹不会丢数据（issue #12）。
≤v0.1.1 存在 backend/.cache/ 仓库内，首次启动自动迁移（复制，旧文件保留作备份）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import astock

HERE = os.path.dirname(os.path.abspath(__file__))
_OLD_PF_FILE = os.path.join(HERE, ".cache", "portfolio.json")  # ≤v0.1.1 旧位置
# CACHE_DIR 名字保留（测试/外部按此名 monkeypatch），实际已是用户数据目录
CACHE_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
PF_FILE = os.path.join(CACHE_DIR, "portfolio.json")
BEIJING = timezone(timedelta(hours=8))
_LOCK = threading.Lock()

# 进程内「刷新好的持仓响应」缓存：后台预热 / 点进页面强制重算后写入，
# GET /api/portfolio 直接返回它，用户看到的就是上次刷新算好的收益。
# 账本结构变化（加/删/清仓）时主动失效，保证 GET 不返回过期结构。
_RESP_CACHE: dict[str, dict] = {}


def _cache_key(user_id: int | None) -> str:
    return str(user_id) if user_id is not None else "legacy"


def _path(user_id: int | None) -> str:
    return PF_FILE if user_id is None else os.path.join(CACHE_DIR, f"portfolio.user-{user_id}.json")


def _invalidate(user_id: int | None = None) -> None:
    with _LOCK:
        if user_id is None:
            _RESP_CACHE.clear()
        else:
            _RESP_CACHE.pop(_cache_key(user_id), None)


def _migrate_legacy() -> None:
    """旧版持仓在仓库内 .cache/ 里，重下载项目会丢；迁到用户目录（新位置已有则不动）。"""
    try:
        if not os.path.exists(PF_FILE) and os.path.exists(_OLD_PF_FILE):
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = PF_FILE + ".migrate.tmp"
            shutil.copy2(_OLD_PF_FILE, tmp)
            os.replace(tmp, PF_FILE)  # 原子落位：复制中断不会留半截 portfolio.json 挡住下次重试
    except OSError as e:
        # 迁移失败不阻塞启动，但要出声——旧数据原样保留在 _OLD_PF_FILE，可手工复制
        print(f"[vibe-research] 持仓数据迁移失败（旧数据仍在 {_OLD_PF_FILE}）: {e}", file=sys.stderr)


_migrate_legacy()


def _now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")


def _load(user_id: int | None = None) -> dict:
    try:
        with open(_path(user_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"holdings": [], "last_refresh": None}


def _save(d: dict, user_id: int | None = None) -> None:
    # 先写临时文件再原子改名：并发读若撞上写中途的半截 JSON，会被 _load 静默当成空持仓
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _path(user_id)
    if os.path.exists(path):
        try:
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
    """加一笔持仓；同代码则按加权平均成本合并（加仓）。

    bought_date 记录最早一笔的买入日期，用于本年盈亏分段：
    年前买入按年初价作基准、年内买入按成本价作基准。未填（旧账本）按年前买入处理。
    """
    bd = _clean_bought_date(bought_date)
    with _LOCK:
        d = _load(user_id)
        for h in d["holdings"]:
            if h["code"] == code:
                total = h["shares"] + shares
                # 4 位小数：ETF/基金成本常见 3-4 位（issue #13），2-3 位会让市值/盈亏对不上账
                h["cost"] = round((h["shares"] * h["cost"] + shares * cost) / total, 4) if total else cost
                h["shares"] = total
                # 合并只取双方都有日期时的更早者；旧记录无日期则保持无日期（按年前买入计）
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
    return get_portfolio(user_id=user_id)


def update_holding(code: str, shares: float, cost: float, bought_date: str | None = None,
                   user_id: int | None = None) -> dict:
    """直接改一笔持仓的数量 / 成本价 / 买入日期（录错更正用，覆盖而非合并）。

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
    return get_portfolio(user_id=user_id)


def remove_holding(code: str, user_id: int | None = None) -> dict:
    with _LOCK:
        d = _load(user_id)
        d["holdings"] = [h for h in d["holdings"] if h["code"] != code]
        _save(d, user_id)
    _invalidate(user_id)
    return get_portfolio(user_id=user_id)


def close_position(code: str, date: str, price: float, shares: float, cost: float | None = None,
                   user_id: int | None = None) -> dict:
    """记一笔已清仓：算已实现盈亏，存入 closed 列表，并同步从当前持仓扣减股数。

    cost 不传时自动取该代码当前持仓的加权成本——买入成本在添加持仓时已录入，
    清仓不该要求重填。持仓里没有该代码时必须显式给成本（会拿到明确的错误而不是按 0 算）。
    """
    with _LOCK:
        d = _load(user_id)
        holding = next((h for h in d["holdings"] if h["code"] == code), None)
        if cost is None:
            if holding is None:
                raise ValueError("当前持仓中没有该代码，请补充买入成本")
            cost = holding["cost"]
        pnl = (price - cost) * shares
        d.setdefault("closed", [])
        try:
            name = astock.tencent_quote([code]).get(code, {}).get("name", code)
        except Exception:
            name = code
        d["closed"].append({
            "code": code, "name": name, "date": date, "price": price,
            "shares": shares, "cost": cost, "pnl": round(pnl, 2),
            "pnl_pct": round((price - cost) / cost * 100, 2) if cost else 0.0,
        })
        if holding is not None:
            remain = holding["shares"] - shares
            if remain > 1e-9:
                holding["shares"] = remain
            else:  # 全部卖出（或微小浮点误差）→ 从当前持仓移除
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


def replace_ledger(d: dict, user_id: int | None = None) -> None:
    """用校验过的账本整体替换当前账本（数据导入用）。结构异常抛 ValueError。"""
    if not isinstance(d, dict) or not isinstance(d.get("holdings"), list):
        raise ValueError("证券持仓账本格式不对（缺 holdings 列表）")
    for h in d["holdings"]:
        if not isinstance(h, dict):
            raise ValueError("持仓条目必须是对象")
        code = h.get("code")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            raise ValueError(f"持仓代码不合法: {code!r}")
        for field in ("shares", "cost"):
            v = h.get(field)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{code} 的 {field} 必须是数字")
        if h.get("shares") <= 0:
            raise ValueError(f"{code} 的股数必须大于 0")
    if not isinstance(d.get("closed", []), list):
        raise ValueError("已清仓记录必须是列表")
    with _LOCK:
        _save(d, user_id)
    _invalidate(user_id)


def get_portfolio(fresh: bool = False, user_id: int | None = None, refresh_bases: bool = False) -> dict:
    """读持仓 + 实时行情，算每笔与汇总的市值/浮动盈亏。"""
    cache_key = _cache_key(user_id)
    if not fresh:
        with _LOCK:
            if cache_key in _RESP_CACHE:
                return _RESP_CACHE[cache_key]
    with _LOCK:
        d = _load(user_id)
    hs = d.get("holdings", [])
    closed = d.get("closed", [])
    codes = list(dict.fromkeys([h["code"] for h in hs] + [c["code"] for c in closed]))
    try:
        quotes = astock.tencent_quote(codes) if codes else {}
    except Exception:
        quotes = {}
    rows, tmv, tcost, tday, tday_base = [], 0.0, 0.0, 0.0, 0.0
    if hs:
        for h in hs:
            q = quotes.get(h["code"], {})
            price = q.get("price", 0.0)
            mv = price * h["shares"]
            cv = h["cost"] * h["shares"]
            pnl = mv - cv
            # 当日盈亏 = (现价 - 昨收) × 股数；昨收缺失（行情异常/新股）时按 0
            last_close = q.get("last_close", 0.0)
            day_pnl = (price - last_close) * h["shares"] if last_close else 0.0
            day_base = last_close * h["shares"]  # 昨收市值，作当日盈亏比例的基数
            rows.append({
                "code": h["code"], "name": q.get("name", h["code"]),
                "price": price, "shares": h["shares"], "cost": h["cost"],
                "bought_date": h.get("bought_date"),
                "market_value": round(mv, 2), "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / cv * 100, 2) if cv else 0.0,
                "day_pnl": round(day_pnl, 2),
                "day_pnl_pct": round(day_pnl / day_base * 100, 2) if day_base else 0.0,
            })
            tmv += mv
            tcost += cv
            tday += day_pnl
            tday_base += day_base
    total_pnl = tmv - tcost
    # 本年盈亏（YTD）分段口径：年前买入的份额按「本年度首个交易日收盘价 → 现价」计，
    # 年内买入的份额按「成本价 → 现价」计（没持有过的涨幅不该算进来）；
    # 再并入本年度已清仓的实现盈亏。买入日期取最早一笔（加仓合并的近似）。
    year_now = datetime.now(BEIJING).year
    ytd_pnl = 0.0
    ytd_base = 0.0
    year_open_by_code: dict[str, float | None] = {}
    if hs:
        # 年前买入（含未填日期的旧记录）：需要年初基准价
        needs_year_open = [
            h for h in hs
            if (h.get("bought_date") or "0000")[:4] < str(year_now)
        ]
        if refresh_bases and needs_year_open:
            # 重算路径（后台预热 / 点进页面补刷）：拉 K 线重算 YTD 并落账本，
            # GET 缓存路径直接用落账本的年初基准重放，毫秒级返回。
            for h in needs_year_open:
                try:
                    klines = astock.tencent_kline(h["code"], period="day", count=260)
                except Exception:
                    klines = []
                year_open = next(
                    (k.get("close") for k in klines if str(k.get("date", "")).startswith(str(year_now))),
                    None,
                )
                year_open_by_code[h["code"]] = year_open
            with _LOCK:
                d2 = _load(user_id)
                if year_now != d2.get("ytd_year"):
                    d2["ytd_year"] = year_now
                    d2["ytd_open"] = {}
                d2.setdefault("ytd_open", {}).update(
                    {c: v for c, v in year_open_by_code.items() if v}
                )
                d2["ytd_refresh_date"] = datetime.now(BEIJING).date().isoformat()
                _save(d2, user_id)
        elif not refresh_bases and needs_year_open:
            # 普通 GET（响应缓存恰好未命中，如服务重启后首次）：沿用账本里的年初基准，
            # 不为单只股票的 YTD 逐只拉 260 根 K 线（持仓稍多就拖垮首屏）。
            stored = d.get("ytd_open") if d.get("ytd_year") == year_now else None
            for h in needs_year_open:
                year_open_by_code[h["code"]] = (stored or {}).get(h["code"])
        for h in hs:
            q = quotes.get(h["code"], {})
            price = q.get("price", 0.0)
            if not price:
                continue
            if (h.get("bought_date") or "0000")[:4] < str(year_now):
                year_open = year_open_by_code.get(h["code"])
                if year_open:
                    ytd_pnl += (price - year_open) * h["shares"]
                    ytd_base += year_open * h["shares"]
            else:
                # 年内买入（或日期异常）：按成本计，买入即起点
                ytd_pnl += (price - h["cost"]) * h["shares"]
                ytd_base += h["cost"] * h["shares"]
    if closed:
        # 本年已清仓的实现盈亏并入本年盈亏；年前买入的清仓记录只有成本价，
        # 其实现盈亏含年前涨幅部分，按账本粒度无法拆分，整笔计入（近似）。
        for c in closed:
            if str(c.get("date", ""))[:4] == str(year_now):
                ytd_pnl += c.get("pnl", 0.0)
                ytd_base += c.get("cost", 0.0) * c.get("shares", 0.0)
    if closed:
        # 清仓后至今涨跌幅 = 现价 / 清仓价 - 1；现价取不到时留 None，前端显示占位
        closed = [
            {**c, "post_close_pct": (
                round((quotes[c["code"]]["price"] - c["price"]) / c["price"] * 100, 2)
                if c.get("price") and quotes.get(c["code"], {}).get("price") else None
            )}
            for c in closed
        ]
    result = {
        "holdings": rows,
        "totals": {
            "market_value": round(tmv, 2), "cost": round(tcost, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / tcost * 100, 2) if tcost else 0.0,
            "day_pnl": round(tday, 2),
            "day_pnl_pct": round(tday / tday_base * 100, 2) if tday_base else 0.0,
        },
        "closed": closed,
        "realized_pnl": round(sum(c.get("pnl", 0) for c in closed), 2),
        "ytd_pnl": round(ytd_pnl, 2),
        "ytd_pnl_pct": round(ytd_pnl / ytd_base * 100, 2) if ytd_base else 0.0,
        "updated": _now(),
        "last_refresh": d.get("last_refresh"),
        "quote_as_of": max((q.get("quote_date", "") + " " + q.get("quote_time", "")
                            for q in quotes.values() if q.get("quote_date")), default=None),
        "ledger_updated_at": d.get("ledger_updated_at"),
        "version": d.get("version", 0),
    }
    with _LOCK:
        _RESP_CACHE[cache_key] = result
    return result


def _is_trading_hours() -> bool:
    """与前端 isTradingHours 同口径：工作日 9:15-11:30 / 13:00-15:00。"""
    now = datetime.now(BEIJING)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 15 <= mins <= 11 * 60 + 30) or (13 * 60 <= mins <= 15 * 60)


def _is_postclose_window() -> bool:
    now = datetime.now(BEIJING)
    return now.weekday() < 5 and 15 * 60 + 10 <= now.hour * 60 + now.minute <= 15 * 60 + 20


def _refresh_snapshot(user_id: int | None = None) -> None:
    """后台定时任务：真重算持仓收益（行情 + YTD 基准）并写响应缓存 + 时间戳。

    之后用户点进持仓页，GET 直接返回这份算好的缓存——「刷新好后缓存的收益」。
    """
    current = datetime.now(BEIJING)
    d = _load(user_id)
    year_now = current.year
    stored = d.get("ytd_open") if d.get("ytd_year") == year_now else None
    # 年前买入的持仓缺年初基准（新录入/换年）→ 立即补拉，不等收盘后窗口
    missing_base = any(
        (h.get("bought_date") or "0000")[:4] < str(year_now) and h["code"] not in (stored or {})
        for h in d.get("holdings", [])
    )
    refresh_bases = missing_base or (
        current.hour * 60 + current.minute >= 15 * 60 + 10 and d.get("ytd_refresh_date") != current.date().isoformat()
    )
    get_portfolio(fresh=True, user_id=user_id, refresh_bases=refresh_bases)
    with _LOCK:
        d = _load(user_id)
        d["last_refresh"] = _now()
        _save(d, user_id)


def start_scheduler(interval: int = 300, user_ids=lambda: ()) -> None:
    """每半小时后台刷新一次持仓数据（daemon 线程；仅在 A 股交易时段执行）。"""
    def loop():
        while True:
            time.sleep(interval)
            if not (_is_trading_hours() or _is_postclose_window()):
                continue
            try:
                for user_id in user_ids():
                    _refresh_snapshot(user_id)
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()


def legacy_status(user_id: int) -> dict:
    legacy = _load(None)
    target = _load(user_id)
    return {"available": bool(legacy.get("holdings") or legacy.get("closed")),
            "target_empty": not bool(target.get("holdings") or target.get("closed")),
            "holdings": len(legacy.get("holdings") or []), "closed": len(legacy.get("closed") or [])}


def import_legacy(user_id: int) -> dict:
    status = legacy_status(user_id)
    if not status["available"]:
        raise ValueError("没有可导入的旧证券持仓")
    if not status["target_empty"]:
        raise ValueError("当前账号已有证券持仓，不能覆盖")
    legacy = _load(None)
    os.makedirs(CACHE_DIR, exist_ok=True)
    shutil.copy2(PF_FILE, PF_FILE + f".pre-user-import-{datetime.now(BEIJING):%Y%m%d-%H%M%S}.backup")
    _save(legacy, user_id)
    _invalidate(user_id)
    return get_portfolio(user_id=user_id)
