"""择时 + 大类资产配置（资管层聚合引擎）。

研究设计：research/timing_asset_allocation_technical_design.md
- 择时引擎：宏观总分 × 流动性得分 × 市场确认 → 5 档风险等级 + 风险预算倍率 + 现金底仓；
  四条门控规则（市场门控 / 流动性门控 / 双弱制动 / 一致性升级）对档位 clamp。
- 配置引擎：风险等级锚定组合 + 三资产 AssetScore 主动偏离（tanh 带限）→ 股/债/商品/现金
  目标权重（合计 100%）；资产分缺失或上游大面积缺源时回退锚点组合。

数据全部复用既有模块（market / bonds / oil / gold_score / astock），本文件只做合成计算，
不新增数据源外呼（行业涨跌家数取自宏观快照，避免重复请求）。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import astock
import ai_insight
import bonds
import gold_score
import market
import oil
import sector_scores
import cache_runtime

_BEIJING = timezone(timedelta(hours=8))

_SCHEMA_VERSION = 1
_MODEL_VERSION = "timing_alloc v1.0"
_STATE_FILE = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "allocation_state.json",
)
_SNAPSHOT_FILE = os.path.join(os.path.dirname(_STATE_FILE), "allocation_snapshot.json")
_STATE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _as_points(hist) -> list[dict]:
    return [p for p in (hist or []) if isinstance(p, dict)
            and isinstance(p.get("v"), (int, float)) and p.get("date")]


def _pct_rank_of(points: list[dict], window: int = 250, min_n: int = 60) -> float | None:
    """当前值在过去 window 点中的百分位（0-100）。样本不足返回 None，不臆测。"""
    vals = [p["v"] for p in points]
    if len(vals) < max(min_n, 2):
        return None
    sample = vals[-window:]
    cur = vals[-1]
    return round(sum(1 for v in sample if v <= cur) / len(sample) * 100.0, 1)


def _safe_pct(points: list[dict], window: int = 250) -> float:
    """分位，缺样本给 50（中性），供直接当分数用的场合。"""
    p = _pct_rank_of(points, window=window)
    return 50.0 if p is None else p


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _ma_series(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1: i + 1]) / n
    return out


def _ranks_from_values(points: list[dict], window: int = 250, min_periods: int = 60,
                       invert: bool = False) -> list[dict]:
    """逐点扩展分位（无前视）：每期分位取截至当日 window 窗口内的样本。"""
    clean = _as_points(points)
    out = []
    vals = [(-p["v"] if invert else p["v"]) for p in clean]
    for i, p in enumerate(clean):
        sample = vals[max(0, i - window + 1): i + 1]
        if len(sample) >= min_periods:
            out.append({"date": str(p["date"]), "v": round(
                sum(1 for v in sample if v <= vals[i]) / len(sample) * 100.0, 1)})
    return out


def _combine(parts: list[tuple[str, float, float, list[dict]]], weights: dict[str, float],
             scale: float, replay_today: str | None = None) -> tuple[float | None, list[dict], list[dict]]:
    """按权重合成子成分 → (总分, parts分解, 逐日回放)。

    parts 元素：(名称, 当前分, 当前原始读数文本, 逐日分位序列)；缺成分（None）按已覆盖
    权重归一，覆盖 <50% 返回 None。回放 = 当期同权重对逐日分位加权。

    replay_today：给定日期时，把每个「当期有分但 hist 缺当日值」的成分的当期分注入
    回放尾点——否则样本积累期的成分（如上涨家数占比只有数日观测）在 replay 里整体丢权，
    导致 hist 尾点与当期分脱节（同一时刻差出数分）。
    """
    num = den = 0.0
    out_parts = []
    hists: list[tuple[dict[str, float], float]] = []
    today_scores: dict[str, float] = {}
    for name, score, text, hist in parts:
        w = weights.get(name, 0.0)
        if score is None or w <= 0:
            out_parts.append({"name": name, "weight": w, "score": None,
                              "value": text, "contribution": None})
            continue
        num += score * w
        den += w
        out_parts.append({"name": name, "weight": w, "score": round(score, 1),
                          "value": text, "contribution": round((score - 50.0) / 100.0 * w, 2)})
        today_scores[name] = round(score, 1)
        if hist:
            hists.append(({str(p["date"]): p["v"] for p in hist}, w))
    total_w = sum(weights.values())
    # scale：围绕 50 的放大倍数（1=普通加权均值；2=以 50 为中性放大偏离，用于择时合成）
    avg = num / den if den > 0 else 50.0
    score = round(_clamp(50.0 + (avg - 50.0) * scale, 0, 100), 1) if den >= 0.5 * total_w and den > 0 else None
    # 回放日期 = 成分 hist 的并集；某成分当天缺值时按已覆盖权重归一，
    # 样本积累期的成分（如上涨家数只有数日观测）不会把曲线起点拖到自己的首日
    all_dates: set[str] = set()
    for m, _w in hists:
        all_dates |= set(m)
    replay = []
    for d in sorted(all_dates):
        vals = [(m[d], w) for m, w in hists if d in m]
        den_d = sum(w for _v, w in vals)
        if den_d <= 0:
            continue
        replay.append({"date": d, "v": round(_clamp(
            50.0 + (sum(v * w for v, w in vals) / den_d - 50.0) * scale, 0, 100), 1)})
    # 尾点对齐：把当期有分但 hist 缺当日值的成分（含完全无 hist 的）以当期分注入今天，
    # 保证 replay 最新一点与当期分同口径（否则样本积累期成分在 replay 里丢权，尾点失真）
    if replay_today and today_scores:
        wnum = sum(today_scores[n] * weights.get(n, 0.0) for n in today_scores)
        wden = sum(weights.get(n, 0.0) for n in today_scores)
        avg_t = wnum / wden if wden > 0 else 50.0
        today_v = round(_clamp(50.0 + (avg_t - 50.0) * scale, 0, 100), 1)
        replay = [p for p in replay if p["date"] != replay_today]
        replay.append({"date": replay_today, "v": today_v})
    return score, out_parts, replay


def _drivers(parts: list[dict], top: int = 2) -> list[dict]:
    return [{"name": p["name"], "contribution": p["contribution"]}
            for p in sorted((p for p in parts if p["contribution"] is not None),
                            key=lambda p: -abs(p["contribution"]))[:top]]


def _zh_parts(parts: list[dict]) -> list[dict]:
    """资产因子 parts 的 name 转中文（保留原 key 便于审计）。"""
    out = []
    for p in parts:
        q = dict(p)
        q["key"] = p.get("name")
        q["name"] = _FACTOR_NAMES.get(p.get("name"), p.get("name"))
        out.append(q)
    return out


def _zh_drivers(parts: list[dict], top: int = 3) -> list[dict]:
    """资产 drivers 的 name 转中文（与 parts 同名映射）。"""
    return [{"name": _FACTOR_NAMES.get(d["name"], d["name"]), "contribution": d["contribution"]}
            for d in _drivers(parts, top)]


# ---------------------------------------------------------------------------
# 择时引擎
# ---------------------------------------------------------------------------

_MC_WEIGHTS = {"trend": 45.0, "breadth": 25.0, "risk_pressure": 20.0, "crowding": 10.0}
_TIMING_WEIGHTS = {"macro": 40.0, "liquidity": 35.0, "market_confirm": 25.0}
_TIMING_SCALE = 1.6  # 加权均值围绕 50 的放大倍数（与债市分品种评分同惯例），门控再对档位 clamp

# 指数日K进程级缓存：全文件共用，避免趋势/风险压力/相关性三处重复外呼（push2his 偶发断连）
_IDX_CACHE: dict[str, tuple[float, list[dict]]] = {}
_IDX_LOCK = threading.Lock()
_IDX_TTL = 3600.0


def _index_points(secid: str, days: int = 320) -> list[dict]:
    now = time.time()
    with _IDX_LOCK:
        hit = _IDX_CACHE.get(secid)
        if hit and now - hit[0] < _IDX_TTL:
            return hit[1][-days:]
    rows = []
    for _ in range(2):  # 东财断连时重试一次（腾讯降级在内）
        rows = astock.index_daily_em(secid, days=days)
        if rows:
            break
        time.sleep(1.0)
    pts = _kline_points(rows, days)
    if pts:
        with _IDX_LOCK:
            _IDX_CACHE[secid] = (now, pts)
    return pts

_RISK_LEVELS = [
    {"key": "strong_risk_off", "min": -1, "max": 25, "label": "强偏空", "multiplier": 0.60,
     "cash_floor": 0.30, "action": "明显降仓", "action_key": "reduce_risk_hard"},
    {"key": "risk_off", "min": 25, "max": 40, "label": "偏空", "multiplier": 0.80,
     "cash_floor": 0.20, "action": "降低风险", "action_key": "reduce_risk"},
    {"key": "neutral", "min": 40, "max": 60, "label": "中性", "multiplier": 1.00,
     "cash_floor": 0.10, "action": "维持基准", "action_key": "hold"},
    {"key": "risk_on", "min": 60, "max": 75, "label": "偏多", "multiplier": 1.15,
     "cash_floor": 0.05, "action": "增加风险", "action_key": "increase_risk"},
    {"key": "strong_risk_on", "min": 75, "max": 101, "label": "强偏多", "multiplier": 1.25,
     "cash_floor": 0.03, "action": "高风险预算", "action_key": "increase_risk_hard"},
]

# 风险等级 → 锚定组合（股/债/商/现金），设计文档 §4.2
_ANCHOR_WEIGHTS = {
    "strong_risk_off": {"equity": 20, "bond": 45, "commodity": 5, "cash": 30},
    "risk_off": {"equity": 30, "bond": 42, "commodity": 8, "cash": 20},
    "neutral": {"equity": 45, "bond": 35, "commodity": 10, "cash": 10},
    "risk_on": {"equity": 55, "bond": 30, "commodity": 10, "cash": 5},
    "strong_risk_on": {"equity": 65, "bond": 25, "commodity": 7, "cash": 3},
}

_DELTA_BANDS = {"equity": 10.0, "bond": 10.0, "commodity": 5.0}


def _kline_points(rows: list[dict], days: int) -> list[dict]:
    return [{"date": r["date"], "v": float(r["close"])} for r in (rows or [])
            if r.get("date") and isinstance(r.get("close"), (int, float))][-days:]


def _trend_component(macro: dict, macro_date: str) -> tuple[float | None, str, list[dict]]:
    """价格趋势：全指与沪深300 各自 1M 动量 40% + MA20 位置 20% + MA60 位置 20% + 3M 动量 20%，两指数取均值。"""
    sub_scores: dict[str, float] = {}
    sub_hists: dict[str, dict[str, float]] = {}
    texts = []
    for key, secid in (("csi_all", "1.000985"), ("hs300", "1.000300")):
        pts = _index_points(secid)
        if len(pts) < 70:
            continue
        dates = [p["date"] for p in pts]
        vals = [p["v"] for p in pts]
        ma20 = _ma_series(vals, 20)
        ma60 = _ma_series(vals, 60)
        per_date = []
        for i in range(len(vals)):
            s = None
            if i >= 60:
                # 各分量 clamp 到 0-100 后按权重（40/20/20/20，合计 100）归一 → 总分 0-100
                s = (40.0 * _clamp((vals[i] / vals[i - 21] - 1.0) * 800.0 + 50.0, 0, 100)
                     + 20.0 * _clamp((vals[i] / ma20[i] - 1.0) * 2000.0 + 50.0, 0, 100)
                     + 20.0 * _clamp((vals[i] / ma60[i] - 1.0) * 800.0 + 50.0, 0, 100)
                     + 20.0 * _clamp((vals[i] / vals[i - 63] - 1.0) * 300.0 + 50.0, 0, 100)) / 100.0
            per_date.append((dates[i], s, vals[i], ma20[i], ma60[i]))
        sub_hists[key] = {d: round(s, 1) for d, s, _c, _m1, _m2 in per_date if s is not None}
        last = per_date[-1]
        sub_scores[key] = round(last[1], 1)
        chg1m = (last[2] / per_date[-22][2] - 1.0) * 100 if len(per_date) > 22 else None
        texts.append(f"{'中证全指' if key == 'csi_all' else '沪深300'} "
                     f"1M {chg1m:+.1f}% · 收盘/MA20 {(last[2] / last[3] - 1) * 100:+.1f}%"
                     if chg1m is not None and last[3] else key)
    if not sub_scores:
        return None, "指数日K不可用", []
    # 回放取两指数公共日期的均值
    dates = set.intersection(*(set(m) for m in sub_hists.values())) if len(sub_hists) > 1 else set(next(iter(sub_hists.values())))
    hist = [{"date": d, "v": round(sum(m[d] for m in sub_hists.values()) / len(sub_hists), 1)}
            for d in sorted(dates)]
    score = round(sum(sub_scores.values()) / len(sub_scores), 1)
    return score, "；".join(texts), hist[-260:]


def _market_confirm(macro: dict, macro_date: str, curve: dict, pos: dict,
                    sector: dict, liq_leverage: dict | None) -> dict:
    """市场确认 = 0.45×趋势 + 0.25×广度 + 0.20×(100−风险压力) + 0.10×拥挤度修正。"""
    cn = macro.get("cn") or {}

    trend_score, trend_text, trend_hist = _trend_component(macro, macro_date)

    breadth_sub = []
    up_pts = _as_points((cn.get("market_breadth") or {}).get("hist"))
    if (cn.get("market_breadth") or {}).get("value") is not None:
        breadth_sub.append(("market_breadth", cn["market_breadth"]["value"]))
        if len(up_pts) >= 2:
            up_pts = up_pts + [{"date": str(cn["market_breadth"].get("date") or macro_date)[:10],
                                "v": cn["market_breadth"]["value"]}]
    else:
        breadth_sub.append(("market_breadth", None))
    nh = cn.get("new_high_breadth") or {}
    nh_pts = _as_points(nh.get("hist"))
    breadth_sub.append(("new_high_breadth", nh.get("value")))
    if len(nh_pts) >= 2:
        nh_pts = nh_pts + [{"date": str(nh.get("date") or macro_date)[:10], "v": nh["value"]}]
    # 广度子分：指标本身即 0-100 占比 → 用各自历史分位统一口径（当日值追加进序列再算分位）
    b_parts = [
        ("上涨家数占比",
         None if breadth_sub[0][1] is None else _safe_pct(up_pts[-250:] if len(up_pts) >= 60 else []),
         f"{breadth_sub[0][1]:.0f}%" if breadth_sub[0][1] is not None else "—",
         _ranks_from_values(up_pts)),
        ("20日新高占比",
         None if nh.get("value") is None else _safe_pct(nh_pts[-250:] if len(nh_pts) >= 60 else []),
         f"{nh.get('value'):.0f}%" if nh.get("value") is not None else "—",
         _ranks_from_values(nh_pts)),
    ]
    # 当日占比本身也参与打分（分位 70% + 当日值 30%），避免历史分位滞后
    for i, (name, sc, _t, _h) in enumerate(b_parts):
        if sc is not None:
            cur = breadth_sub[0][1] if name == "上涨家数占比" else nh.get("value")
            b_parts[i] = (name, round(sc * 0.7 + float(cur) * 0.3, 1), _t, _h)
    breadth_score, breadth_parts, breadth_hist = _combine(
        b_parts, {"上涨家数占比": 60.0, "20日新高占比": 40.0}, 1.0)

    # —— 风险压力（高=压力大）——
    idx_pts = _index_points("1.000985")
    vols: list[dict] = []
    if len(idx_pts) >= 60:
        for i in range(20, len(idx_pts)):
            rets = [math.log(idx_pts[j]["v"] / idx_pts[j - 1]["v"]) for j in range(i - 19, i + 1)]
            vol = math.sqrt(sum(r * r for r in rets) / len(rets) * 252.0) * 100.0
            vols.append({"date": idx_pts[i]["date"], "v": round(vol, 2)})
    vol_pct = _pct_rank_of(vols) if vols else None
    credit_pts = _as_points(((curve.get("credit") or {}).get("AAA-3年")) or [])
    credit_pct = _pct_rank_of(credit_pts)
    lev = None
    if liq_leverage and liq_leverage.get("value") is not None:
        lev = float(liq_leverage["value"])
    r_parts = [
        ("指数波动率", vol_pct if vol_pct is not None else None,
         (f"{vols[-1]['v']:.1f}%年化（分位{vol_pct:.0f}%）" if vols and vol_pct is not None
          else f"{vols[-1]['v']:.1f}%年化" if vols else "—"), _ranks_from_values(vols)),
        ("信用利差", credit_pct if credit_pct is not None else None,
         (f"{credit_pts[-1]['v']:.0f}bp（分位{credit_pct:.0f}%）" if credit_pts and credit_pct is not None
          else f"{credit_pts[-1]['v']:.0f}bp" if credit_pts else "—"), _ranks_from_values(credit_pts)),
        ("两融杠杆温度", lev, f"{lev:.0f}/100" if lev is not None else "—", []),
    ]
    rp_score, rp_parts, rp_hist = _combine(r_parts, {"指数波动率": 40.0, "信用利差": 35.0, "两融杠杆温度": 25.0}, 1.0)

    # —— 拥挤度（高=拥挤）——
    t_futures_pcts = [c.get("oi_pct_1y") for c in (pos.get("contracts") or [])
                      if isinstance(c.get("oi_pct_1y"), (int, float))]
    tf = round(sum(t_futures_pcts) / len(t_futures_pcts) * 100.0, 1) if t_futures_pcts else None
    # 逐日回放：各品种持仓量序列先各自分位化（无前视），再按日取均值，与当前值同口径
    tf_rank_maps = []
    for c in (pos.get("contracts") or []):
        oi_pts = _as_points(c.get("oi_hist"))
        if len(oi_pts) >= 60:
            tf_rank_maps.append({str(p["date"]): p["v"] for p in _ranks_from_values(oi_pts)})
    common = set.intersection(*(set(m) for m in tf_rank_maps)) if tf_rank_maps else set()
    tf_hist = [{"date": d, "v": round(sum(m[d] for m in tf_rank_maps) / len(tf_rank_maps), 1)}
               for d in sorted(common)] if tf_rank_maps else []
    inds = [i for i in (sector.get("industries") or []) if isinstance(i, dict)]
    cr = [i.get("crowding", {}).get("risk") for i in inds
          if isinstance(i.get("crowding"), dict) and isinstance(i.get("crowding").get("risk"), (int, float))]
    secc = round(sum(cr) / len(cr), 1) if cr else None
    c_parts = [
        ("国债期货持仓", tf, f"{tf:.0f}/100" if tf is not None else "—", _ranks_from_values(tf_hist)),
        ("行业拥挤度", secc, f"{secc:.0f}/100" if secc is not None else "—", []),
    ]
    crowding_score, _c_parts_out, _c_hist = _combine(c_parts, {"国债期货持仓": 50.0, "行业拥挤度": 50.0}, 1.0)

    # 拥挤度修正分：>85 进入饱和区扣 5-10 分，其余 50-100 线性（低拥挤略加分）
    if crowding_score is None:
        crowding_adj = None
    else:
        crowding_adj = round(_clamp(50.0 + (crowding_score - 50.0) * 0.6, 0, 100), 1)
        if crowding_score > 85:
            crowding_adj = round(crowding_adj - (5.0 + (crowding_score - 85.0) * 0.3), 1)

    mc_parts = [
        ("trend", trend_score, trend_text, trend_hist),
        ("breadth", breadth_score,
         "；".join(f"{p['name']} {p['value']}" for p in breadth_parts if p["value"] not in (None, "—")), breadth_hist),
        ("risk_pressure", (100 - rp_score) if rp_score is not None else None,
         "；".join(f"{p['name']} {p['value']}" for p in rp_parts if p["value"] not in (None, "—")), rp_hist),
        ("crowding", crowding_adj,
         (f"拥挤度 {crowding_score:.0f}/100" if crowding_score is not None else "拥挤度数据不足")
         + ("（饱和区扣分）" if crowding_score and crowding_score > 85 else ""),
         []),
    ]
    # crowding 的当期分注入回放尾点（它没有逐日 hist）
    today = str(datetime.now(_BEIJING).strftime("%Y-%m-%d"))
    score, parts, hist = _combine(mc_parts, _MC_WEIGHTS, 1.0, replay_today=today)

    # 波动率/信用利差当日值留给失效条件生成
    return {
        "score": score, "parts": parts, "hist": hist[-260:], "drivers": _drivers(parts),
        "risk_pressure_score": rp_score,
        "vol_pct": vol_pct, "credit_pct": credit_pct,
        "crowding_score": crowding_score, "breadth_score": breadth_score, "trend_score": trend_score,
        "desc": "趋势 45% + 广度 25% + 风险压力(反向) 20% + 拥挤度修正 10%；价格未确认时压制风险等级（市场门控）",
    }


def _regime_of(score: float) -> dict:
    for lv in _RISK_LEVELS:
        if lv["min"] <= score < lv["max"]:
            return lv
    return _RISK_LEVELS[-1] if score >= 75 else _RISK_LEVELS[0]


def _expand_monthly(hist: list[dict], daily_dates: list[str]) -> list[dict]:
    """月度序列（如宏观总分）前向填充到日度日期轴：每日用「上一个完整月」的值。

    宏观 hist 的当月值是随发布持续更新的，直接填当月会在月内引入前视；
    改用滞后一月（t 月值只填 t+1 月的日度点），回放线无前视，代价是宏观信号
    滞后约一个月——对月频先验（择时权重 40% 的中期锚）可接受。
    """
    if not hist or not daily_dates:
        return []
    by_month = {str(p["date"])[:7]: p["v"] for p in hist}
    months = sorted(by_month)
    # prev_month[m] = m 前一个月的值；对 hist 最后一个月的「下一个月」（通常是当前月）
    # 也补上映射，否则最新一段日度点会整体缺失
    def _next_month(m: str) -> str:
        y, mm = int(m[:4]), int(m[5:7])
        return f"{y + mm // 12:04d}-{mm % 12 + 1:02d}"

    prev_month: dict[str, float] = {}
    last = None
    for m in months:
        if last is not None:
            prev_month[m] = last
        last = by_month[m]
    if months:
        prev_month[_next_month(months[-1])] = by_month[months[-1]]
    out = []
    for d in daily_dates:
        v = prev_month.get(str(d)[:7])
        if v is not None:
            out.append({"date": str(d)[:10], "v": v})
    return out


def _timing_engine(macro_score: float | None, liq_score: float | None, mc: dict,
                   macro_hist: list[dict], liq_hist: list[dict]) -> dict:
    # 择时分逐日回放：以市场确认的逐日 hist 为日期轴，宏观（月度）前向填充、流动性按日对齐
    axis = [str(p["date"])[:10] for p in (mc.get("hist") or [])]
    macro_daily = _expand_monthly(macro_hist, axis) if axis else []
    parts_in = [
        ("macro", macro_score, "宏观总分（模块加权，回测权重）", macro_daily),
        ("liquidity", liq_score, "中美流动性·国内综合（资金压力/政策/杠杆温度加权）", liq_hist),
        ("market_confirm", mc.get("score"), "价格与仓位确认", mc.get("hist") or []),
    ]
    score, parts, hist = _combine(parts_in, _TIMING_WEIGHTS, _TIMING_SCALE,
                                   replay_today=(mc.get("hist") or [{}])[-1].get("date"))

    # —— 门控（对档位 clamp，不改变分数本身）——
    gates = []
    level = _regime_of(score) if score is not None else None
    idx = _RISK_LEVELS.index(level) if level else 2
    if mc.get("score") is not None and mc["score"] < 35 and idx > 2:
        gates.append({"rule": "market_gate", "desc": "市场确认分 <35，风险等级最高只能到中性",
                      "capped_to": "neutral"})
        idx = min(idx, 2)
    if liq_score is not None and liq_score < 30 and idx > 1:
        gates.append({"rule": "liquidity_gate", "desc": "流动性得分 <30，风险等级最高只能到偏空",
                      "capped_to": "risk_off"})
        idx = min(idx, 1)
    if (macro_score is not None and macro_score < 35 and liq_score is not None and liq_score < 35):
        floor = 0 if (mc.get("score") is not None and mc["score"] < 30) else 1
        if idx > floor:
            gates.append({"rule": "double_weak_brake",
                          "desc": "宏观与流动性双弱（均<35），至少降至" + ("强偏空" if floor == 0 else "偏空"),
                          "capped_to": _RISK_LEVELS[floor]["key"]})
            idx = min(idx, floor)
    if (macro_score is not None and liq_score is not None and mc.get("score") is not None
            and macro_score > 65 and liq_score > 65 and mc["score"] > 65 and idx < 3):
        gates.append({"rule": "consistency_upgrade", "desc": "三证据一致 >65，至少升至偏多",
                      "raised_to": "risk_on"})
        idx = max(idx, 3)
    level = _RISK_LEVELS[idx]

    # —— 失效条件（≤3 条，贴近当前读数）——
    invalidation = []
    if mc.get("score") is not None:
        invalidation.append(f"市场确认分跌破 35（当前 {mc['score']:.0f}）→ 风险等级上限压至中性")
    if mc.get("credit_pct") is not None:
        invalidation.append(f"AAA-3Y 信用利差分位升破 80（当前 {mc['credit_pct']:.0f}%）→ 风险压力显著上行")
    if liq_score is not None:
        invalidation.append(f"流动性得分跌破 30（当前 {liq_score:.0f}）→ 风险等级上限压至偏空")

    text = (f"当前市场环境：{level['label']}。建议风险预算为中性组合的 {level['multiplier']:.2f} 倍，"
            f"现金底仓 {level['cash_floor'] * 100:.0f}%。")
    if score is not None:
        text = text[:-1] + f"（择时分 {score:.0f}/100）。"
    return {
        "score": score, "regime": level["key"], "regime_label": level["label"],
        "risk_budget_multiplier": level["multiplier"], "cash_floor": level["cash_floor"],
        "recommended_action": level["action_key"], "recommended_action_label": level["action"],
        "gates": gates, "invalidation": invalidation[:3], "text": text,
        "parts": parts, "hist": hist, "drivers": _drivers(parts, top=3),
    }


# ---------------------------------------------------------------------------
# 配置引擎
# ---------------------------------------------------------------------------

_ASSET_NAMES = {"equity": "股票", "bond": "债券", "commodity": "商品", "cash": "现金"}
# 资产因子 key → 中文（前端/AI 工具的解释层展示用）
_FACTOR_NAMES = {
    "macro_fit": "宏观适配", "liquidity": "流动性", "valuation": "估值/Carry",
    "trend": "价格趋势", "diversification": "分散化", "carry": "Carry 静态收益",
}
_ASSET_SCORE_WEIGHTS = {
    "equity": {"macro_fit": 35.0, "liquidity": 20.0, "valuation": 20.0, "trend": 20.0, "diversification": 5.0},
    "bond": {"macro_fit": 35.0, "liquidity": 10.0, "carry": 30.0, "trend": 15.0, "diversification": 10.0},
    "commodity": {"macro_fit": 35.0, "liquidity": 10.0, "valuation": 25.0, "trend": 20.0, "diversification": 10.0},
}


def _bond_bond_side(segments: dict) -> tuple[float | None, str, list[dict]]:
    rows = [r for r in (segments.get("rows") or [])
            if r.get("segment") != "杠杆套息(回购+短券)" and isinstance(r.get("score"), (int, float))]
    if not rows:
        return None, "债市分品种评分不可用", []
    score = round(_clamp(50.0 + sum(r["score"] for r in rows) / len(rows) * 25.0, 0, 100), 1)
    best = max(rows, key=lambda r: r["score"])
    text = f"五品种均值 {score:.0f}/100，相对占优：{best['segment']}"
    hist_maps = {r["segment"]: {str(p.get("date")): p.get("v") for p in (r.get("hist") or [])
                                if isinstance(p, dict) and isinstance(p.get("v"), (int, float))}
                 for r in rows}
    dates = set.intersection(*(set(m) for m in hist_maps.values())) if hist_maps else set()
    hist = [{"date": d, "v": round(_clamp(50.0 + sum(m[d] for m in hist_maps.values()) / len(hist_maps) * 25.0, 0, 100), 1)}
            for d in sorted(dates)]
    return score, text, hist


def _carry_score(calc: dict) -> tuple[float | None, str]:
    """Carry 静态锚 → 0-100：组合锚（1Y+5Y carry 均值）映射，[0,60]bp → [30,75]。"""
    rows = {r["tenor"]: r for r in (calc.get("rows") or []) if isinstance(r, dict)}
    carries = [rows[t]["total_static_bp_3m"] for t in ("1年", "5年") if t in rows
               and isinstance(rows[t].get("total_static_bp_3m"), (int, float))]
    if not carries:
        return None, "carry 计算层不可用"
    avg = sum(carries) / len(carries)
    score = round(_clamp(30.0 + (avg / 60.0) * 45.0, 0, 100), 1)
    return score, f"1Y/5Y 三个月静态收益均值 {avg:+.0f}bp"


def _asset_scores(macro: dict, macro_date: str, liq_score: float | None, mc: dict,
                  segments: dict, calc: dict, oil_data: dict, gold_data: dict,
                  corr_summary: dict) -> dict:
    cn = macro.get("cn") or {}
    mods = {m.get("name"): m for m in (macro.get("modules") or []) if isinstance(m, dict)}
    liq_text = f"{liq_score:.0f}/100" if liq_score is not None else "—"

    # —— 股票 ——
    climate = mods.get("国内增长与景气") or {}
    climate_score = climate.get("score")
    climate_fit = None
    if climate_score is not None and macro.get("composite"):
        # 景气低分（经济弱）→ 宽松预期 → 对股票偏利多：取 100-分（与宏观总分同向处理）
        climate_fit = round(100.0 - climate_score, 1)
    pe = cn.get("index_pe_ttm") or {}
    pe_pct = _pct_rank_of(_as_points(pe.get("hist")))
    erp = cn.get("equity_risk_premium") or {}
    erp_pct = _pct_rank_of(_as_points(erp.get("hist")))
    val_scores = [100 - p for p in (pe_pct, erp_pct) if p is not None]
    val_score = round(sum(val_scores) / len(val_scores), 1) if val_scores else None
    stock_corr = corr_summary.get("stock_bond_corr_60d")

    def _divers_score(corr) -> float | None:
        if corr is None:
            return None
        # 低/负相关 → 债券分散价值高；corr ∈ [-0.3, +0.5] → [75, 25]
        return round(_clamp(75.0 - (corr + 0.3) / 0.8 * 50.0, 0, 100), 1)

    eq_parts = [
        ("macro_fit", climate_fit, f"增长景气适配（景气分 {climate_score or '—'}，反向）", []),
        ("liquidity", liq_score, f"流动性 {liq_text}", []),
        ("valuation", val_score,
         f"沪深300 PE 分位 {pe_pct or '—'}%、ERP 分位 {erp_pct or '—'}%（低=便宜=加分）", []),
        ("trend", mc.get("trend_score"), "指数趋势（全指+沪深300）", []),
        ("diversification", _divers_score(stock_corr),
         f"股债相关性 60 日 {stock_corr:+.2f}" if stock_corr is not None else "股债相关性缺样本", []),
    ]
    eq_score, eq_parts_out, _ = _combine(eq_parts, _ASSET_SCORE_WEIGHTS["equity"], 1.0)

    # —— 债券 ——
    bond_fit, bond_fit_text, bond_fit_hist = _bond_bond_side(segments)
    carry_score, carry_text = _carry_score(calc)
    bd_parts = [
        ("macro_fit", bond_fit, bond_fit_text, bond_fit_hist),
        ("liquidity", liq_score, f"流动性 {liq_text}", []),
        ("carry", carry_score, carry_text, []),
        ("trend", mc.get("trend_score"), "价格趋势（股债同源，弱势市场债相对占优由八状态体现）", []),
        ("diversification", _divers_score(stock_corr),
         f"股债相关性 60 日 {stock_corr:+.2f}" if stock_corr is not None else "股债相关性缺样本", []),
    ]
    bd_score, bd_parts_out, _ = _combine(bd_parts, _ASSET_SCORE_WEIGHTS["bond"], 1.0)

    # —— 商品：油 + 金 ——
    oil_s = oil_data.get("oil_score")
    gold_s = gold_data.get("gold_score")
    cmd_s = round((oil_s + gold_s) / 2.0, 1) if oil_s is not None and gold_s is not None else (oil_s or gold_s)
    usd_dim = ((gold_data.get("dimensions") or {}).get("机会成本与美元") or {}).get("score")
    if usd_dim is not None and cmd_s is not None:
        cmd_s = round(cmd_s * 0.85 + (100.0 - usd_dim) * 0.15, 1)  # 美元走弱 → 商品计价加分
    oil_trend = ((oil_data.get("dimensions") or {}).get("趋势确认") or {}).get("score")
    gold_trend = ((gold_data.get("dimensions") or {}).get("趋势确认") or {}).get("score")
    tr_vals = [v for v in (oil_trend, gold_trend) if v is not None]
    cmd_trend = round(sum(tr_vals) / len(tr_vals), 1) if tr_vals else None
    cmd_parts = [
        ("macro_fit", cmd_s, f"油 {oil_s or '—'}/金 {gold_s or '—'} 多维评分均值（含美元计价修正）", []),
        ("liquidity", liq_score, f"流动性 {liq_text}", []),
        ("valuation", cmd_s, "油/金各自的期限结构与仓位定价已含在多维评分内", []),
        ("trend", cmd_trend, f"油趋势 {oil_trend or '—'} / 金趋势 {gold_trend or '—'}", []),
        ("diversification", _divers_score(corr_summary.get("stock_cmd_corr_60d")),
         "与股票低相关（商品自带通胀对冲）", []),
    ]
    cmd_score, cmd_parts_out, _ = _combine(cmd_parts, _ASSET_SCORE_WEIGHTS["commodity"], 1.0)

    return {
        "equity": {"score": eq_score, "parts": _zh_parts(eq_parts_out), "drivers": _zh_drivers(eq_parts_out)},
        "bond": {"score": bd_score, "parts": _zh_parts(bd_parts_out), "drivers": _zh_drivers(bd_parts_out)},
        "commodity": {"score": cmd_score, "parts": _zh_parts(cmd_parts_out), "drivers": _zh_drivers(cmd_parts_out)},
    }


def _correlation_summary() -> dict:
    """四资产日频序列的波动率与相关性摘要（公共窗口，V1 不进优化器，仅解释层）。"""
    def _rets(points: list[dict]) -> dict[str, float]:
        out = {}
        for i in range(1, len(points)):
            if points[i]["v"] and points[i - 1]["v"]:
                out[str(points[i]["date"])] = math.log(points[i]["v"] / points[i - 1]["v"])
        return out

    stock = _rets(_index_points("1.000985"))
    bond_idx = (bonds.get_index().get("series") or [])
    bond = _rets([{"date": p.get("date"), "v": p.get("v")} for p in _as_points(bond_idx)])
    oil_pts = _rets([{"date": p.get("date"), "v": p.get("v")} for p in
                     _as_points((oil.brent_daily_history(320).get("points")) or [])])
    gold_pts = _rets([{"date": p.get("date"), "v": p.get("v")} for p in
                      _as_points((gold_score.au0_daily_history(320).get("points")) or [])])

    def _corr(a: dict[str, float], b: dict[str, float], n: int = 60) -> float | None:
        dates = sorted(set(a) & set(b))[-n:]
        if len(dates) < 30:
            return None
        xs = [a[d] for d in dates]
        ys = [b[d] for d in dates]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        vy = math.sqrt(sum((y - my) ** 2 for y in ys))
        return round(cov / (vx * vy), 2) if vx > 0 and vy > 0 else None

    def _vol_series(r: dict[str, float]) -> list[dict]:
        ds = sorted(r)
        out = []
        for i in range(20, len(ds) + 1):
            win = [r[d] for d in ds[i - 20:i]]
            out.append({"date": ds[i - 1], "v": round(math.sqrt(sum(x * x for x in win) / 20 * 252) * 100, 1)})
        return out

    out = {"window": "60/120 日公共样本，20 日年化波动率"}
    sb = _corr(stock, bond)
    out["stock_bond_corr_60d"] = sb
    out["stock_bond_corr_120d"] = _corr(stock, bond, 120)
    sc = _corr(stock, oil_pts)
    out["stock_cmd_corr_60d"] = sc if sc is not None else _corr(stock, gold_pts)
    out["stock_cmd_basis"] = "油（缺则金）"
    vols = {}
    for name, r in (("股票", stock), ("债券", bond), ("商品油", oil_pts), ("商品金", gold_pts)):
        series = _vol_series(r)
        if series:
            pct = _pct_rank_of(series)
            vols[name] = {"vol_20d_ann": series[-1]["v"], "pct_1y": pct}
    out["vols"] = vols
    return out


def _read_state() -> dict | None:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("target_weights"):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_state(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)
    except OSError:
        pass


def _resolve_weights(anchor: dict, scores: dict, cash_floor: float,
                     bond_cap: float = 65.0) -> tuple[dict, bool, str]:
    """锚点 + tanh 主动偏离 → 目标权重。缺失资产分回退锚点；现金吸收残差。"""
    usable = {k: v["score"] for k, v in scores.items() if v.get("score") is not None}
    if len(usable) < 3:
        return (dict(anchor), False,
                "资产评分覆盖不足，回退风险等级锚定组合（" + "、".join(
                    f"{_ASSET_NAMES[k]}缺失" for k in ("equity", "bond", "commodity") if k not in usable) + "）")
    w = dict(anchor)
    deltas = {}
    for k in ("equity", "bond", "commodity"):
        deltas[k] = round(_DELTA_BANDS[k] * math.tanh((usable[k] - 50.0) / 20.0), 1)
        w[k] = anchor[k] + deltas[k]
    w["cash"] = 100.0 - (w["equity"] + w["bond"] + w["commodity"])
    # 约束：现金 ≥ 底仓；债券上限（宽松货币环境下债券锚点本身可能偏高，防单边挤占）
    if w["cash"] < cash_floor * 100.0:
        w["cash"] = round(cash_floor * 100.0, 1)
        w["equity"] = 100.0 - w["cash"] - w["bond"] - w["commodity"]
    if w["bond"] > bond_cap:
        w["cash"] = round(w["cash"] + w["bond"] - bond_cap, 1)
        w["bond"] = bond_cap
    total = w["equity"] + w["bond"] + w["commodity"] + w["cash"]
    if abs(total - 100.0) > 0.5:
        w["cash"] = round(w["cash"] + (100.0 - total), 1)
    note = "主动偏离 = 带限 tanh(资产分)，资金守恒（现金吸收残差），现金 ≥ 择时底仓"
    return {k: round(v, 1) for k, v in w.items()}, True, note


def _explain_assets(scores: dict, timing: dict, corr: dict) -> dict[str, dict]:
    sb = corr.get("stock_bond_corr_60d")
    corr_state = "股债相关性转正，债券分散价值下降" if (sb or 0) > 0.2 else (
        "股债低相关/负相关，债券分散价值正常" if sb is not None else "相关性样本不足")

    def top_side(parts, side):
        rows = [p for p in parts if p.get("contribution") is not None
                and (p["contribution"] > 0 if side > 0 else p["contribution"] < 0)]
        rows.sort(key=lambda p: -abs(p["contribution"]))
        return [_FACTOR_NAMES.get(p.get("name"), p.get("name")) for p in rows[:2]]

    out = {}
    for key, zh in (("equity", "股票"), ("bond", "债券"), ("commodity", "商品"), ("cash", "现金")):
        if key == "cash":
            out[key] = {
                "support": ["流动性缓冲", "择时现金底仓约束"],
                "constraint": ["风险偏好上升时机会成本高"],
                "meaning": f"降至择时底仓 {timing['cash_floor'] * 100:.0f}%",
            }
            continue
        parts = scores[key]["parts"]
        support = top_side(parts, 1) or ["无明显正贡献"]
        constraint = top_side(parts, -1) or ["无明显负贡献"]
        if key == "bond":
            constraint = constraint + [corr_state]
        out[key] = {"support": support, "constraint": constraint[:2],
                    "meaning": f"资产分 {scores[key]['score']:.0f}/100，权重相对锚点按带限偏离调整"}
    return out


# ---------------------------------------------------------------------------
# payload 组装
# ---------------------------------------------------------------------------


def _payload() -> dict:
    macro = market.get_macro() or {}
    liq = market.get_liquidity() or {}
    curve = bonds.get_curve() or {}
    segments = bonds.get_segments() or {}
    calc = bonds.get_calc() or {}
    pos = bonds.get_positioning() or {}
    sector = sector_scores.get_sector_scores() or {}
    oil_data = oil.get_oil_score() or {}
    gold_data = gold_score.get_gold_score() or {}

    comp = macro.get("composite") or {}
    macro_score = comp.get("score")
    macro_hist = _as_points(comp.get("hist"))
    macro_date = str(macro_hist[-1]["date"])[:10] if macro_hist else datetime.now(_BEIJING).strftime("%Y-%m-%d")
    liq_comp = liq.get("cn_composite") or {}
    liq_score = liq_comp.get("score")
    liq_hist = _as_points(liq_comp.get("hist"))
    liq_date = str(liq_hist[-1]["date"])[:10] if liq_hist else macro_date
    liq_leverage = (liq.get("cn_indices") or {}).get("leverage") or None

    mc = _market_confirm(macro, macro_date, curve, pos, sector, liq_leverage)
    timing = _timing_engine(macro_score, liq_score, mc, macro_hist, liq_hist)
    corr = _correlation_summary()
    scores = _asset_scores(macro, macro_date, liq_score, mc, segments, calc,
                           oil_data, gold_data, corr)

    anchor = _ANCHOR_WEIGHTS[timing["regime"]]
    base = _ANCHOR_WEIGHTS["neutral"]
    target, resolved, resolve_note = _resolve_weights(anchor, scores, timing["cash_floor"])

    prev = _read_state()
    last_weights = (prev or {}).get("target_weights")
    regime_changed = bool(prev and prev.get("regime") != timing["regime"])
    rows = []
    max_abs_delta = 0.0
    for key in ("equity", "bond", "commodity", "cash"):
        vs_last = round(target[key] - last_weights[key], 1) if last_weights else None
        vs_base = round(target[key] - base[key], 1)
        if vs_last is not None:
            max_abs_delta = max(max_abs_delta, abs(vs_last))
        rows.append({
            "asset": key, "name": _ASSET_NAMES[key],
            "anchor": anchor[key], "target": target[key],
            "last": last_weights.get(key) if last_weights else None,
            "vs_last": vs_last, "vs_base": vs_base,
            "suggestion": ("加仓" if (vs_last if vs_last is not None else vs_base) > 0.5 else
                           "减仓" if (vs_last if vs_last is not None else vs_base) < -0.5 else "维持"),
        })
    rebalance = bool(regime_changed or max_abs_delta >= 3.0)

    # 现金收益锚
    shibor = bonds.get_shibor() or {}
    sh_series = (shibor.get("series") or {}).get("1Y") or []
    sh_last = sh_series[-1].get("v") if sh_series else None

    explanation = _explain_assets(scores, timing, corr)
    for r in rows:
        r.update(explanation.get(r["asset"]) or {})

    as_of = max(d for d in (macro_date, liq_date) if d)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "model_version": _MODEL_VERSION,
        "as_of": as_of,
        "timing": timing,
        "evidence": {
            "macro": {"score": macro_score, "state": comp.get("state"),
                      "date": macro_date, "parts": comp.get("parts"),
                      "hist": macro_hist, "source": "宏观总分（模块加权，回测权重）"},
            "liquidity": {"score": liq_score, "state": liq_comp.get("state"),
                          "date": liq_date, "parts": liq_comp.get("parts"),
                          "hist": liq_hist, "source": "中美流动性·国内综合"},
            "market_confirm": mc,
        },
        "allocation": {
            "regime": timing["regime"], "regime_label": timing["regime_label"],
            "anchor": anchor, "base_weights": base, "target_weights": target,
            "last_weights": last_weights, "last_as_of": (prev or {}).get("as_of"),
            "regime_changed": regime_changed, "rebalance_trigger": rebalance,
            "rows": rows, "resolved": resolved, "resolve_note": resolve_note,
            "asset_scores": scores, "correlation": corr,
            "cash_yield_note": (f"Shibor 1Y {sh_last:.2f}%（现金收益锚）" if sh_last is not None
                                else "Shibor 序列不可用"),
        },
        "text": timing["text"],
        "method": (
            "择时 = 0.40×宏观总分 + 0.35×流动性 + 0.25×市场确认（围绕 50 放大 1.6 倍，门控另对档位 clamp），叠加四条门控"
            "（市场确认<35 封顶中性、流动性<30 封顶偏空、双弱制动、一致升级）；"
            "配置 = 风险等级锚点组合 ± 带限 tanh 主动偏离（股债 ±10pct、商品 ±5pct），现金吸收残差且不低于底仓。"
            "商品评分来自油价/黄金多维评分；债券评分来自债市八状态分品种映射 + carry 静态锚；"
            "相关性/波动为解释层摘要，V1 未进入数值优化（设计文档 §4.4 降级条款）。"
        ),
        "notes": [
            "「较当前」基准 = 上一次建议的目标权重（首次运行等于较中性基准）。",
            "权重为模型研究输出，不构成投资建议；调仓阈值 |Δ|≥3pct 或风险等级跨档。",
        ],
        "updated": datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M"),
    }
    _write_state({"as_of": as_of, "regime": timing["regime"],
                  "target_weights": target, "updated_at": time.time()})
    return payload


def get_timing_allocation(force: bool = False) -> dict:
    return cache_runtime.get(
        "timing:allocation", _payload,
        valid=lambda v: bool(v.get("timing", {}).get("regime")),
        ttl=3600,
        warm=_load_snapshot,
        save=_save_snapshot,
        force=force,
    )


# ---------------------------------------------------------------------------
# AI 解读：择时分的通俗解释（复用 pulse 的 LLM 配置发现；写回缓存快照）
# ---------------------------------------------------------------------------

_INSIGHT_MAX_CHARS = 120  # 每段字数上限（prompt 要求 ≤80，截断兜底）


def _fmt_part_line(name: str, weight: float, p: dict) -> str:
    score = p.get("score")
    score_txt = f"{score:.0f}/100" if isinstance(score, (int, float)) else "缺失"
    value = str(p.get("value") or "").strip()
    if len(value) > 80:
        value = value[:80] + "…"
    return f"- {name}（权重{weight:.0f}%）：{score_txt}，{value or '无读数'}"


async def _build_insight(force: bool = False) -> dict[str, str] | str:
    """调 LLM 按宏观/流动性/市场确认三层各写一段总结性判断；失败返回空串。"""
    payload = get_timing_allocation(force=force)
    timing = payload.get("timing") or {}
    if timing.get("score") is None:
        return ""
    from pulse.pulse_insight import chat_json

    ev = payload.get("evidence") or {}
    # 宏观/流动性 parts 的 value 字段是 None（读数在各自页面的模块里）——注入时补上
    # score 与方向语义，否则 LLM 只见贡献不见读数，只能瞎猜状态
    liq_raw = (market.get_liquidity() or {}).get("cn_composite") or {}

    def _ev_lines(key: str, default_name: str) -> list[str]:
        blk = ev.get(key) or {}
        lines = []
        if blk.get("score") is not None:
            state = f"{blk.get('state') or ''}" if key != "market_confirm" else ""
            state_txt = f"（{state}）" if state else ""
            lines.append(f"{default_name}总分 {blk['score']:.0f}/100{state_txt}，"
                         f"日期 {blk.get('date') or '—'}")
        # 流动性：用原始 composite parts（score 已归一为越高越利多）；
        # 债市状态是 favorable=low 反向指标，注入时直接标明真实方向（债强/债弱），
        # 历史 LLM 误读都源于没给方向
        src = (liq_raw.get("parts") if key == "liquidity" else None) or []
        bond_idx = ((market.get_liquidity() or {}).get("cn_indices") or {}).get("bond") or {}
        bond_pct = bond_idx.get("value") if isinstance(bond_idx.get("value"), (int, float)) else None
        for p, raw in zip(blk.get("parts") or [], src):
            if not (isinstance(p, dict) and p.get("contribution") is not None):
                continue
            if p.get("value"):
                val = str(p["value"])
            elif raw and isinstance(raw.get("score"), (int, float)):
                val = f"{raw['score']:.0f}/100"
            else:
                val = "—"
            note = ""
            if p.get("name") == "债市状态" and bond_pct is not None:
                direction = "债市偏强（债券价格上涨）" if bond_pct >= 50 else "债市偏弱（债券价格下跌）"
                note = (f"［实际方向：{direction}；中债国债净价20日收益分位 {bond_pct:.0f}%。"
                        "此指标计入流动性分时取反向（债强→小幅拖累），正文说债市必须与实际方向一致，"
                        "禁止说反；对权益资金的含义不做推断（不写分流/未分流）］")
            lines.append(f"  · {p.get('name')}：{val}（贡献 {p['contribution']:+.2f}）{note}")
        return lines

    # 宏观模块读数摘要（名字→方向→分），一段一行
    macro_mods = {m.get("name"): m for m in (macro.get("modules") or [])
                  if isinstance(m, dict)} if isinstance(macro := (market.get_macro() or {}), dict) else {}
    macro_lines = []
    for p in (ev.get("macro") or {}).get("parts") or []:
        mod = macro_mods.get(p.get("name")) or {}
        dr = {"inverse": "对收益反向（分高=利空，已取反计入）", "direct": ""}.get(p.get("direction"), "")
        if isinstance(mod.get("desc"), str) and mod["desc"]:
            dr = (dr + "：" + mod["desc"]) if dr else mod["desc"]
        macro_lines.append(f"  · {p['name']}：分 {p.get('score')}，贡献 {p['contribution']:+.2f}"
                           + (f"（{dr}）" if dr else ""))

    gates = "; ".join(g.get("desc") or g.get("rule") for g in timing.get("gates") or [])
    prompt = (
        "你是资产管理公司的首席策略官，为择时结论写三段解读。\n"
        f"模型当前：择时分 {timing['score']:.0f}、风险等级「{timing.get('regime_label')}」"
        f"（合成 = 40%×宏观 + 35%×流动性 + 25%×市场确认）。\n\n"
        "三层证据读数如下，仅供你推理——页面已展示全部分数与贡献，正文禁止复述：\n"
        "【宏观（中期先验）】\n" + "\n".join(_ev_lines("macro", "宏观")) + "\n"
        + "\n".join(macro_lines) + "\n"
        "【流动性（资金面）】\n" + "\n".join(_ev_lines("liquidity", "流动性")) + "\n"
        "【市场确认（价格与仓位验证）】\n" + "\n".join(_ev_lines("market_confirm", "市场确认")) + "\n"
        "口径（方向语义，引用读数前必须按此换算，禁止照抄分位当方向）：\n"
        "- 市场确认：趋势=中证全指与沪深300的1/3个月动量和均线位置；广度=上涨家数与20日新高占比；"
        "风险压力子项的 score 已反向（高=压力小）；「波动率 26%年化（分位86%）」指波动水平绝对值中等、"
        "但处近一年高位，读分位定方向、读水平值定幅度；信用利差同理。拥挤度=国债期货持仓与行业拥挤（过高扣分）。\n"
        "- 流动性子项「score」已统一为越高越利多；注意三个易错方向：债市状态子项注入行已标注实际方向"
        "（债强/债弱），说债市方向必须与标注一致、禁止说反；该子项对权益资金的含义不做推断（不写分流/未分流）；"
        "两融杠杆温度低=杠杆资金未入场、拖累流动性分（分位 40-60 属中性，禁止说成「偏高」，>80 才是过热）；"
        "银行间资金压力/政策基准的分数已反向（高=宽松）。\n"
        "- 宏观模块「对收益反向」= 景气分低反而利多权益（复苏交易逻辑），其正贡献来自低分。\n"
        "- 所有「分位」是历史百分位不是水平值：低分位=处于近一年低位。引用任何子项前先核对其 score "
        "与贡献的方向是否支持你要下的判断，两者矛盾时以原始读数文本为准。\n"
        + (f"门控（对档位的额外约束）：{gates}\n" if gates else "")
        + "\n输出宏观/流动性/市场确认各一段，每段 1-2 句、80 字以内，是该维度的总结性观点与判断：\n"
        "- 先给判断（这个维度当前什么状态、对风险偏好偏多还是偏空、强度如何），再给实质依据（谁在驱动、结构性弱点或背离在哪）\n"
        "- 有洞见：写读数背后的含义和三层之间的逻辑关系（价格是否确认宏观、资金面与走势是否背离等），不罗列子项\n"
        "- 实描、高信息密度：禁过渡语与评语气；分数/贡献/百分比一律不复述，最多引用一个能强化判断的关键数字\n"
        "- 语言规范：陈述句 + 常用金融词汇（驱动/背离/拖累/收敛/扩张/确认/透支等）。禁比喻和套话，"
        "包括但不限于「成色」「根基」「托底式弱顺风」「默许而非加持」「最扎实一环」「外强内弱」这类修辞化表述——"
        "与其相对的判断必须直接写出因果（如「改善依赖外需，内需政策未发力」）\n"
        "- 专业不说黑话；不预测未来、不给个股建议、不两面下注\n"
        '只返回 JSON：{"macro": "...", "liquidity": "...", "market_confirm": "..."}，不要解释或代码块标记。'
    )
    parsed = await chat_json(prompt)
    out = {k: str(parsed.get(k)).strip()[:_INSIGHT_MAX_CHARS]
           for k in ("macro", "liquidity", "market_confirm")
           if isinstance(parsed.get(k), str) and parsed.get(k).strip()}
    return out if len(out) == 3 else ""


async def get_ai_insight(force: bool = False) -> dict[str, str] | str:
    """AI 解读（macro/liquidity/market_confirm 三段）：手动刷新（force）直接重建；
    否则返回缓存快照里已存的解读。

    数据重建后（ai_insight_at 早于 payload 的 updated）旧解读描述的已不是当前
    读数，非 force 请求也会重生成一次。LLM 失败时保留旧解读，不让页面文字消失。"""

    def _valid(v) -> bool:
        return isinstance(v, dict) and all(v.get(k) for k in
                                           ("macro", "liquidity", "market_confirm"))

    return await ai_insight.cached_insight(
        cache_key="timing:allocation",
        build=lambda: _build_insight(force=False),
        valid=_valid,
        writable=lambda snap: bool(snap.get("timing", {}).get("regime")),
        save=_save_snapshot,
        force=force,
    )


def _load_snapshot() -> dict | None:
    try:
        with open(_SNAPSHOT_FILE, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if value.get("schema_version") == _SCHEMA_VERSION and value.get("timing", {}).get("regime") else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _save_snapshot(value: dict) -> None:
    os.makedirs(os.path.dirname(_SNAPSHOT_FILE), exist_ok=True)
    temp = _SNAPSHOT_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
    os.replace(temp, _SNAPSHOT_FILE)
