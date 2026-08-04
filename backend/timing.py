"""持仓择时信号 —— 按 research/A股优质个股中短期择时策略.md 的规则实现。

规则（收盘确认，盘中触及仅预警）：
- 启动：收盘价 > 20日均线 × 1.01，且 20日均线向上（高于 5 个交易日前）→ 加仓
- 确认突破：收盘价 > 过去 50 日最高收盘（不含当日）× 1.01 → 加仓
- 跌破：收盘价 < 20日均线 × 0.99 → 减仓（跌破近期突破位同样归并提示）
- 其余：观望（过滤带内不操作、不追高、不因下跌补仓）

强度（0-3）＝ 基础档 + 方向一致的确认条件数（20日均线方向、5/20 日量能）。
量能不能确认方向时降 1 档（文档：量能尚未转弱可观察 1 个交易日）。
相对强弱（个股对行业指数）需要外部行业序列，本实现不含；文档未给可复算阈值，
不凭空捏造，文案中如实标注「需人工确认」。

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

_cache: dict[str, tuple[float, dict]] = {}


def _ma(vals: list[float], n: int, i: int) -> float | None:
    """截至下标 i（含）的 n 日均值；数据不足返回 None。"""
    if i + 1 < n:
        return None
    return sum(vals[i + 1 - n:i + 1]) / n


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def _strength(base: int, trend_ok: bool | None, vol_ok: bool | None) -> int:
    s = base + (1 if trend_ok else 0) + (1 if vol_ok else 0)
    if base >= 1 and vol_ok is False:  # 量能背离：降一档（文档「可观察 1 个交易日」）
        s -= 1
    return max(1, min(3, s))


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
    ma20_prev = _ma(closes, MA_WIN, i - 1)   # 昨日 20 日均线
    ma20_5ago = _ma(closes, MA_WIN, i - 5)   # 5 个交易日前的 20 日均线
    ma_up = ma20 > ma20_5ago if (ma20 is not None and ma20_5ago is not None) else None
    vs = _ma(vols, VOL_SHORT, i)
    vl = _ma(vols, VOL_LONG, i)
    vol_up = vs > vl if (vs is not None and vl is not None) else None
    above_prev = ma20_prev is not None and closes[i - 1] > ma20_prev * (1 + BAND)
    # 突破位：今日之前 50 日（不足则全部历史）的最高收盘
    hist = closes[max(0, i - BREAK_WIN):i]
    hi50 = max(hist) if hist else None
    broken_today = hi50 is not None and close > hi50 * (1 + BAND)

    # 近一次向上突破 50 日高点的位置与价位（10 个交易日窗口内视为近期突破）
    recent_break = None
    for j in range(i - 1, max(0, i - CONFIRM_DAYS) - 1, -1):
        win = closes[max(0, j - BREAK_WIN):j]
        if win and closes[j] > max(win) * (1 + BAND):
            recent_break = {"idx": j, "date": rows[j]["date"], "price": max(win)}
            break
    break_below = (
        recent_break is not None
        and close < recent_break["price"] * (1 - BAND)
        and (i - recent_break["idx"]) <= CONFIRM_DAYS
    )
    # 最近一次「站上 20 日线 1%」的启动点（用于启动信号有效期与提示）
    recent_cross = None
    for j in range(i, max(1, i - EXPIRED_DAYS) - 1, -1):
        m_j = _ma(closes, MA_WIN, j)
        m_j1 = _ma(closes, MA_WIN, j - 1)
        if m_j is not None and m_j1 is not None and closes[j] > m_j * (1 + BAND) and closes[j - 1] <= m_j1 * (1 + BAND):
            recent_cross = {"idx": j, "date": rows[j]["date"]}
            break

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
    if recent_break:
        details.append(f"近期突破：{recent_break['date']} 突破 {_fmt(recent_break['price'])}")
    details.append("相对强弱（个股 vs 行业指数）：本信号未含，按文档需人工确认")

    if close > (ma20 or 0) * (1 + BAND):
        if broken_today:
            out.update(signal="add", signal_label="加仓",
                       action="突破确认：收盘有效突破 50 日高点，按文档可加至计划仓位（下一交易日执行）")
            s_base = 2
            details.insert(0, f"突破确认：收盘高于 50 日突破位 {_fmt(hi50)} 超 1%")
        elif recent_break and close > recent_break["price"]:
            out.update(signal="add", signal_label="持有",
                       action=f"突破维持中（{recent_break['date']} 突破 {_fmt(recent_break['price'])}），趋势延续，按文档让趋势决定持有期")
            s_base = 1
        elif not above_prev and recent_cross and recent_cross["idx"] == i:
            out.update(signal="add", signal_label="加仓",
                       action="启动信号：收盘站上 20 日均线 1%，按文档先建计划仓位的 50%（下一交易日执行）")
            s_base = 1
        elif recent_cross and (i - recent_cross["idx"]) <= CONFIRM_DAYS:
            out.update(signal="add", signal_label="持有待确认",
                       action=f"{recent_cross['date']} 启动后 10 日窗口内：突破 50 日高点 {_fmt(hi50 or 0)}×1.01 可加至计划仓位")
            s_base = 1
        else:
            out.update(signal="watch", signal_label="观望",
                       action="位于 20 日均线上方但无新启动/突破信号，按文档不追高、不因上涨加仓")
            s_base = 0
        out["strength"] = _strength(s_base, ma_up, vol_up) if s_base else (1 if (ma_up or vol_up) else 0)
    elif close < (ma20 or 0) * (1 - BAND):
        if break_below:
            out.update(signal="reduce", signal_label="减仓",
                       action=f"突破失败：收盘跌破 {recent_break['date']} 突破位 {_fmt(recent_break['price'])} 的 1% 过滤带，按文档退出（下一交易日执行）")
        elif vol_up is False:
            out.update(signal="reduce", signal_label="减仓",
                       action="趋势终止：收盘跌破 20 日均线 1% 且量能转弱，按文档退出（下一交易日执行）")
        else:
            out.update(signal="reduce", signal_label="减仓（待确认）",
                       action="收盘跌破 20 日均线 1%，但量能未转弱：按文档可观察 1 个交易日，次日未收复再退出")
        out["strength"] = _strength(2 if vol_up is False else 1, ma_up is False, vol_up is False)
    else:
        out.update(signal="watch", signal_label="观望",
                   action=f"处于 20 日均线 ±1% 过滤带内（{abs(pos_vs_ma):.1f}%），按文档过滤带内不操作")

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
