"""持仓择时信号 —— 按 research/A股优质个股中短期择时策略.md 的规则实现。

设计：事件式信号，不是"位置式"。信号只在规则触发当天产生，并带有效期；
同一事件的信号强度随时间衰减，过期后回到「观望」。不会因为"价格仍在 20 日线上方"
就一直显示加仓，也不会因为"跌得深"就提高减仓强度——执行纪律由用户自己定，
本模块只报告"规则在何时触发、现在处于什么阶段、规则建议是什么"。

事件（收盘确认，盘中触及仅预警）：
- 启动（加仓）：收盘向上穿越 20日均线×1.01（前一日未站上），有效期 10 个交易日。
- 突破确认（加仓）：收盘向上穿越过去 50 日最高收盘×1.01。
- 跌破（减仓）：收盘跌破 20日均线×0.99；量能未转弱时按文档「可观察 1 个交易日」降级为待确认。
- 突破失败（减仓）：向上突破后 10 个交易日内收盘跌破突破位×0.99。

强度（0-3）＝ 事件基础档 + 方向一致的确认条件数（20日均线方向、5/20 日量能），
再乘以时间衰减（触发当日 1.0，随后每个交易日 -0.1）。同一信号不会随价格下跌加深
而变强——这正是与"跌了就减、涨了就加"的本质区别。

相对强弱（个股对行业指数）需要外部行业序列，文档未给可复算阈值，本实现不含，
不凭空捏造，详情中如实标注「需人工确认」。

本模块只做信号计算，不写持仓文件；输出为规则化技术指标提示，非投资建议。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import astock

MA_WIN = 20          # 趋势均线窗口
BREAK_WIN = 50       # 突破窗口（历史最高收盘，不含当日）
BAND = 0.01          # 1% 过滤带
VOL_SHORT = 5        # 短量窗口
VOL_LONG = 20        # 长量窗口
CONFIRM_DAYS = 10    # 信号有效期 / 突破确认窗口（交易日）
EXPIRED_DAYS = 20    # 超过 20 个交易日未确认的启动信号不再提示（≈ 2 倍有效期）
MIN_ROWS = 30        # 最少数据量：低于此无法判断 20 日均线趋势
KLINE_COUNT = 70     # 拉取 K 线根数（需 ≥ 50 日窗口 + 20 日量均线）
CACHE_TTL = 1800     # 结果缓存 30 分钟，与持仓刷新节奏一致
DECAY_PER_DAY = 0.1  # 同一事件信号每个交易日衰减 10%

_cache: dict[str, tuple[float, dict]] = {}


def _ma(vals: list[float], n: int, i: int) -> float | None:
    """截至下标 i（含）的 n 日均值；数据不足返回 None。"""
    if i + 1 < n:
        return None
    return sum(vals[i + 1 - n:i + 1]) / n


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def _age(i: int, idx: int) -> int:
    """事件距今的交易日数（触发当日为 0）。"""
    return max(0, i - idx)


def _decayed(strength: float, age: int) -> int:
    return max(0, min(3, round(strength * max(0.0, 1 - DECAY_PER_DAY * age))))


def _strength(base: int, trend_ok: bool | None, vol_ok: bool | None) -> int:
    s = base + (1 if trend_ok else 0) + (1 if vol_ok else 0)
    if base >= 1 and vol_ok is False:  # 量能背离：降一档（文档「可观察 1 个交易日」）
        s -= 1
    return max(1, min(3, s))


def _find_cross(closes: list[float], i: int, lookback: int) -> dict | None:
    """最近一次「收盘向上穿越 20日均线×1.01」的启动点（前一日尚未站上）。"""
    for j in range(i, max(1, i - lookback) - 1, -1):
        m_j = _ma(closes, MA_WIN, j)
        m_j1 = _ma(closes, MA_WIN, j - 1)
        if m_j is None or m_j1 is None:
            continue
        if closes[j] > m_j * (1 + BAND) and closes[j - 1] <= m_j1 * (1 + BAND):
            return {"idx": j}
    return None


def _find_break(closes: list[float], i: int, lookback: int) -> dict | None:
    """最近一次「收盘向上突破过去 50 日最高收盘×1.01」的位置与突破位。"""
    for j in range(i, max(1, i - lookback) - 1, -1):
        win = closes[max(0, j - BREAK_WIN):j]
        if win and closes[j] > max(win) * (1 + BAND):
            return {"idx": j, "price": max(win)}
    return None


def _find_downcross(closes: list[float], i: int, lookback: int) -> dict | None:
    """最近一次「收盘向下跌破 20日均线×0.99」的位置（前一日仍在带上方）。"""
    for j in range(i, max(1, i - lookback) - 1, -1):
        m_j = _ma(closes, MA_WIN, j)
        m_j1 = _ma(closes, MA_WIN, j - 1)
        if m_j is None or m_j1 is None:
            continue
        if closes[j] < m_j * (1 - BAND) and closes[j - 1] >= m_j1 * (1 - BAND):
            return {"idx": j}
    return None


def compute_signal(code: str, rows: list[dict]) -> dict:
    """rows：前复权日 K [{date, close, volume, ...}]，按日期升序。"""
    out = {
        "code": code,
        "signal": None,          # add / reduce / watch / None
        "signal_label": "数据不足",
        "strength": 0,
        "strength_label": "—",
        "action": "无可执行信号（历史 K 线不足）",
        "details": [],
        "as_of": None,
        "pending": False,
        "rule": "20日均线启动 + 50日突破确认 + 1%过滤带 + 5/20日量能（文档：中短期择时策略）",
        "since": None,           # 当前信号事件的触发日期
        "age_days": 0,           # 距触发日的交易日数（当日为 0）
    }
    rows = [r for r in rows if r.get("close") and r.get("volume") is not None]
    if len(rows) < MIN_ROWS:
        return out

    closes = [float(r["close"]) for r in rows]
    vols = [float(r.get("volume") or 0) for r in rows]
    n = len(closes)
    i = n - 1
    close = closes[i]
    ma20 = _ma(closes, MA_WIN, i)
    ma20_5ago = _ma(closes, MA_WIN, i - 5)   # 5 个交易日前的 20 日均线
    ma_up = ma20 > ma20_5ago if (ma20 is not None and ma20_5ago is not None) else None
    vs = _ma(vols, VOL_SHORT, i)
    vl = _ma(vols, VOL_LONG, i)
    vol_up = vs > vl if (vs is not None and vl is not None) else None
    # 突破位：今日之前 50 日（不足则全部历史）的最高收盘
    hist = closes[max(0, i - BREAK_WIN):i]
    hi50 = max(hist) if hist else None
    above_ma = ma20 is not None and close > ma20 * (1 + BAND)
    below_ma = ma20 is not None and close < ma20 * (1 - BAND)

    # 事件侦测（事件 = 穿越发生的那一天，不是"价格处于某侧"的状态）
    cross = _find_cross(closes, i, EXPIRED_DAYS)          # 最近一次上穿 20 日线
    brk = _find_break(closes, i, EXPIRED_DAYS)            # 最近一次上破 50 日高点
    down = _find_downcross(closes, i, EXPIRED_DAYS)       # 最近一次跌破 20 日线
    if brk:
        brk["failed"] = close < brk["price"] * (1 - BAND) and _age(i, brk["idx"]) <= CONFIRM_DAYS

    # 若跌破发生在最近一次上穿/上破之后（或同日），则多头事件已被空头事件取代
    if down and (not cross or down["idx"] >= cross["idx"]) and (not brk or down["idx"] >= brk["idx"]):
        cross = None
        brk = None
    # 若上穿发生在最近一次跌破之后，则空头事件已失效（重新站回，不按下跌处理）
    if cross and down and cross["idx"] > down["idx"]:
        down = None

    pos_vs_ma = (close / ma20 - 1) * 100 if ma20 else 0.0
    out["as_of"] = rows[i].get("date")
    lt = time.localtime()
    out["pending"] = bool(out["as_of"] == time.strftime("%Y-%m-%d", lt) and 9 <= lt.tm_hour < 15)

    details = [
        f"收盘 {_fmt(close)}，20日均线 {_fmt(ma20)}（{'高于' if pos_vs_ma >= 0 else '低于'} {abs(pos_vs_ma):.1f}%）",
        f"20日均线方向：{'向上' if ma_up else '向下/走平'}（5 个交易日前 {_fmt(ma20_5ago)}）" if ma_up is not None else "20日均线方向：数据不足",
        f"量能：5日均量/20日均量 = {vs / vl:.2f}（{'放大' if vol_up else '未放大'}）" if vol_up is not None else "量能：数据不足",
        f"50日突破位 {_fmt(hi50)}（前复权最高收盘，不含当日）" if hi50 else "50日突破位：数据不足",
    ]
    details.append("相对强弱（个股 vs 行业指数）：本信号未含，按文档需人工确认")

    # ---- 事件裁决：跌破 > 突破 > 启动（同为最新事件时，风险优先）----
    if brk and brk.get("failed"):
        d = rows[brk["idx"]]["date"]
        out.update(signal="reduce", signal_label="减仓", since=d, age_days=_age(i, brk["idx"]))
        out["action"] = (f"突破失败：{d} 突破 {_fmt(brk['price'])} 后 10 日内收盘跌破突破位 1%，"
                         "按文档下一交易日退出")
        out["strength"] = 2 if vol_up is False else 1
        details.insert(0, f"事件：{d} 突破 {_fmt(brk['price'])} 后未能维持，收盘 {_fmt(close)} 低于突破位×0.99")
    elif down:
        d = rows[down["idx"]]["date"]
        age = _age(i, down["idx"])
        fresh = age == 0
        if vol_up is False:
            label, act = "减仓", "跌破 20 日均线 1% 且量能转弱，按文档下一交易日退出"
        else:
            label, act = "减仓（待确认）", "跌破 20 日均线 1%，但量能未转弱：按文档可观察 1 个交易日，次日未收复再退出"
        out.update(signal="reduce", signal_label=label, since=d, age_days=age)
        out["action"] = f"{'信号触发：' if fresh else '信号维持：'}{d} {act}"
        raw = _strength(2 if vol_up is False else 1, ma_up is False, vol_up is False)
        out["strength"] = raw if fresh else _decayed(raw, age)
        details.insert(0, f"事件：{d} 收盘跌破 20 日均线 1%（{_fmt(closes[down['idx']])} < {_fmt(_ma(closes, MA_WIN, down['idx']))}×0.99）")
    elif brk:
        d = rows[brk["idx"]]["date"]
        age = _age(i, brk["idx"])
        if age == 0:
            out.update(signal="add", signal_label="加仓", since=d, age_days=0,
                       action="信号触发：收盘有效突破 50 日高点 1%，按文档可加至计划仓位（下一交易日执行）")
            out["strength"] = _strength(2, ma_up, vol_up)
            details.insert(0, f"事件：收盘 {_fmt(close)} 高于 50 日突破位 {_fmt(hi50)} 超 1%")
        elif above_ma:
            out.update(signal="add", signal_label="持有", since=d, age_days=age,
                       action=f"{d} 突破 {_fmt(brk['price'])} 后维持中：趋势延续，按文档让趋势决定持有期，不因上涨追高")
            out["strength"] = _decayed(1 + (1 if ma_up else 0) + (1 if vol_up else 0), age)
            details.insert(0, f"事件：{d} 突破 {_fmt(brk['price'])}，此后 {age} 个交易日未跌破突破位与 20 日线")
        else:
            out.update(signal="watch", signal_label="观望", since=d, age_days=age,
                       action=f"{d} 突破后已回到 20 日线 ±1% 过滤带内：按文档过滤带内不操作，等待方向")
            details.insert(0, f"事件：{d} 突破 {_fmt(brk['price'])}，当前位于过滤带内")
    elif cross:
        d = rows[cross["idx"]]["date"]
        age = _age(i, cross["idx"])
        if age == 0:
            out.update(signal="add", signal_label="加仓", since=d, age_days=0,
                       action="信号触发：收盘站上 20 日均线 1%（启动），按文档先建计划仓位的 50%（下一交易日执行）")
            out["strength"] = _strength(1, ma_up, vol_up)
            details.insert(0, f"事件：收盘 {_fmt(close)} 站上 20 日均线 {_fmt(ma20)} 超 1%，前一日尚未站上")
        elif age <= CONFIRM_DAYS and above_ma:
            out.update(signal="add", signal_label="持有待确认", since=d, age_days=age,
                       action=f"{d} 启动后第 {age} 个交易日（10 日窗口内）：突破 50 日高点 {_fmt(hi50 or 0)}×1.01 可加至计划仓位；窗口内不追高")
            out["strength"] = _decayed(_strength(1, ma_up, vol_up), age)
            details.insert(0, f"事件：{d} 启动（站上 20 日线 1%），等待 50 日突破确认")
        else:
            out.update(signal="watch", signal_label="观望", since=d, age_days=age,
                       action=f"{d} 启动信号已过 10 日有效期仍未确认突破：按文档不再加仓，不追高、不因下跌补仓")
            details.insert(0, f"事件：{d} 启动，{age} 个交易日未形成 50 日突破，信号失效")
    elif below_ma:
        out.update(signal="watch", signal_label="观望",
                   action="处于 20 日均线下方但无新触发事件（跌破信号已过期）：按文档不因下跌补仓，只有重新形成完整买入信号才考虑进入")
    elif above_ma:
        out.update(signal="watch", signal_label="观望",
                   action="位于 20 日均线上方但无新启动/突破事件：按文档不追高、不因上涨加仓")
    else:
        out.update(signal="watch", signal_label="观望",
                   action=f"处于 20 日均线 ±1% 过滤带内（{abs(pos_vs_ma):.1f}%）：按文档过滤带内不操作")

    out["strength_label"] = "★" * out["strength"] if out["strength"] else "—"
    out["details"] = details
    return out


def _load_rows(code: str) -> list[dict]:
    try:
        data = astock.chart_kline(code, period="day", count=KLINE_COUNT)
        return data.get("rows") or []
    except Exception:  # noqa: BLE001 — 单票行情失败不拖垮整组信号
        return []


def get_timing_signals(codes: list[str]) -> dict[str, dict]:
    """批量取信号：按代码去重、30 分钟缓存、并发拉 K 线。失败标的返回占位信号。"""
    now = time.time()
    result: dict[str, dict] = {}
    todo: list[str] = []
    for c in dict.fromkeys(codes):  # 保序去重
        hit = _cache.get(c)
        if hit and now - hit[0] < CACHE_TTL:
            result[c] = hit[1]
        else:
            todo.append(c)
    if todo:
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            fetched = list(pool.map(_load_rows, todo))
        for c, rows in zip(todo, fetched):
            try:
                sig = compute_signal(c, rows)
            except Exception:  # noqa: BLE001 — 计算异常按无信号降级
                sig = {"code": c, "signal": None, "signal_label": "计算失败", "strength": 0,
                       "strength_label": "—", "action": "信号计算异常", "details": [], "as_of": None,
                       "pending": False, "rule": ""}
            _cache[c] = (now, sig)
            result[c] = sig
    return result
