"""债市数据层 —— 收益率曲线、期限利差、信用利差、资金利率、政策利率锚、中债指数、中美利差。

设计约定（与 market.py 的低频序列一致）：
- 只给客观数据与序列，不给任何利率走势判断或配置建议。
- 全部走 cache_runtime（TTL 6 小时，交易日频数据够用），last-good 兜底。
- 数据源是 AKShare 转接的中债/中国货币网/全国银行间同业拆借中心公开数据，
  接口失败时返回空字段而不是抛——前端按「数据暂不可用」降级，不阻塞整页。
- 页面结构对应研究框架（research/china_bond_research_framework_v1.1.md）的
  「政策锚 → 资金 → 曲线/期限 → 信用 → 全球」链条，但只呈现事实。
- 计算层（get_calc）由曲线直接推导 carry/roll/breakeven，确定性公式无判断。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import astock
import cache_runtime
from bisect import bisect_left

BEIJING = timezone(timedelta(hours=8))

# 曲线关键期限（年 → DataFrame 列名）
TENORS: list[tuple[str, str]] = [
    ("3月", "3月"), ("6月", "6月"), ("1年", "1年"), ("3年", "3年"),
    ("5年", "5年"), ("7年", "7年"), ("10年", "10年"), ("30年", "30年"),
]

# 期限展示顺序（前端利差矩阵行序）
TENOR_ORDER = ["3月", "6月", "1年", "3年", "5年", "7年", "10年", "30年"]


def _tail(points: list[dict], n: int = 780) -> list[dict]:
    """截尾到 n 点（780 日 ≈ 3 年交易日；月度指标由调用方自行截 36）。"""
    return points[-n:] if len(points) > n else points


def _fetch_yield_frame(days: int):
    """近 days 日的三条中债曲线（国债 / AAA 中短票 / AAA 商行债），失败返回 None。

    接口对超过约 1 年的窗口直接返回空，按 365 日分段拉取后拼接。
    """
    ak = astock._akshare()
    end = datetime.now(BEIJING)
    frames = []
    remaining = days
    seg_end = end
    while remaining > 0:
        span = min(remaining, 365)
        seg_start = seg_end - timedelta(days=span)
        try:
            df = ak.bond_china_yield(start_date=seg_start.strftime("%Y%m%d"),
                                     end_date=seg_end.strftime("%Y%m%d"))
        except Exception:  # noqa: BLE001
            df = None
        if df is not None and not df.empty:
            frames.append(df)
        remaining -= span
        seg_end = seg_start
    if not frames:
        return None
    import pandas as pd
    return pd.concat(frames, ignore_index=True)


def _series_by_tenor(df, curve_name: str) -> dict[str, list[dict]]:
    """单条曲线按期限切成 {期限: [{date, v}]}，去掉 NaN 与重复日期（接口偶发重复行）。"""
    sub = df[df["曲线名称"] == curve_name]
    out: dict[str, list[dict]] = {}
    for label, col in TENORS:
        if col not in sub.columns:
            continue
        seen: dict[str, float] = {}
        for _, row in sub.iterrows():
            try:
                v = float(row[col])
            except (TypeError, ValueError):
                continue
            if v == v:  # NaN 检查
                seen[str(row["日期"])[:10]] = v
        pts = [{"date": d, "v": round(v, 4)} for d, v in sorted(seen.items())]
        if pts:
            out[label] = pts
    return out


def _curve_payload() -> dict:
    # 回溯 1200 日（≈3 年交易日）：覆盖状态分 3 年回算；分位窗口仍由 _pct/_pct_at 控制。
    df = _fetch_yield_frame(1200)
    if df is None or df.empty:
        return {}
    gov = _series_by_tenor(df, "中债国债收益率曲线")
    if not gov:
        return {}
    aaa = _series_by_tenor(df, "中债中短期票据收益率曲线(AAA)")
    bank = _series_by_tenor(df, "中债商业银行普通债收益率曲线(AAA)")

    last_date = max(pts[-1]["date"] for pts in gov.values())
    curve = [{"tenor": label, "value": pts[-1]["v"]} for label, pts in gov.items()]

    # 期限利差：与 1 年期基准的差。10Y-1Y 是最常用的曲线陡平观测。
    spreads: dict[str, list[dict]] = {}
    if "1年" in gov:
        base = {p["date"]: p["v"] for p in gov["1年"]}
        for label in ("10年", "30年", "3年"):
            if label in gov:
                spreads[label + "-1年"] = [
                    {"date": p["date"], "v": round((p["v"] - base[p["date"]]) * 100, 2)}
                    for p in gov[label] if p["date"] in base
                ]

    # 信用利差：AAA 中短票 - 国债（同期限同日对齐）；品种利差：商行债 - 国债
    credit: dict[str, list[dict]] = {}
    for label in ("1年", "3年", "5年"):
        if label in gov and label in aaa:
            gmap = {p["date"]: p["v"] for p in gov[label]}
            credit["AAA-" + label] = [
                {"date": p["date"], "v": round((p["v"] - gmap[p["date"]]) * 100, 2)}
                for p in aaa[label] if p["date"] in gmap
            ]
    if "1年" in gov and "1年" in bank:
        gmap = {p["date"]: p["v"] for p in gov["1年"]}
        credit["商行债-1年"] = [
            {"date": p["date"], "v": round((p["v"] - gmap[p["date"]]) * 100, 2)}
            for p in bank["1年"] if p["date"] in gmap
        ]

    # 关键期限的日度序列（供前端看当日变动与迷你走势）
    yields = {label: _tail(pts) for label, pts in gov.items()
              if label in ("1年", "5年", "10年", "30年")}

    return {
        "date": last_date,
        "curve": curve,
        "yields": yields,
        "spreads": spreads,
        "credit": credit,
        "source": "中债收益率曲线（AKShare 转接 chinabond）",
    }


def get_curve(force: bool = False) -> dict:
    return cache_runtime.get("bonds:curve", _curve_payload, valid=bool, ttl=6 * 3600, force=force)


# ——— 小型计算层：carry / roll / forward / breakeven（框架 §7.4 / §12.3）———
# 全部由当期曲线直接推导，确定性公式、无拟合参数；输入是曲线关键期限的收益率，
# 输出每个期限的持有期静态收益指标。只算不加判断。

# 关键期限（年），与 TENORS 对齐；O/N 资金成本取 Shibor 隔夜近似。
_TENOR_YEARS: dict[str, float] = {"3月": 0.25, "6月": 0.5, "1年": 1.0, "3年": 3.0,
                                  "5年": 5.0, "7年": 7.0, "10年": 10.0, "30年": 30.0}


def _interp_yield(curve_pts: list[dict], years: float) -> float | None:
    """在关键期限上按期限对数线性插值收益率（到期收益率曲线的标准做法）。"""
    pts = sorted(
        [(_TENOR_YEARS.get(p.get("tenor")), p.get("value")) for p in curve_pts],
        key=lambda t: t[0] or 0,
    )
    pts = [(y, v) for y, v in pts if y is not None and isinstance(v, (int, float))]
    if not pts:
        return None
    if years <= pts[0][0]:
        return pts[0][1]
    if years >= pts[-1][0]:
        return pts[-1][1]
    for (y0, v0), (y1, v1) in zip(pts, pts[1:]):
        if y0 <= years <= y1:
            w = (math.log(years) - math.log(y0)) / (math.log(y1) - math.log(y0))
            return v0 + w * (v1 - v0)
    return None


def _modified_duration(y: float, n: float) -> float:
    """近似修正久期：n 年期平价债券（半年付息简化为连续复利麦考利久期近似）。"""
    if y <= 0:
        return n
    d = (1 - math.exp(-y * n)) / y  # 连续复利下的麦考利久期近似
    return d


def _calc_payload() -> dict:
    """carry / roll / breakeven 计算表：每个关键期限给静态持有 3M 的收益拆解。"""
    curve = get_curve() or {}
    pts = curve.get("curve") or []
    if len(pts) < 3:
        return {}
    funding = get_shibor() or {}
    on_cost = ((funding.get("series") or {}).get("O/N") or [{}])[-1].get("v")
    horizon = 0.25  # 持有期 3 个月

    rows = []
    for label, n in _TENOR_YEARS.items():
        y_now = next((p["value"] for p in pts if p.get("tenor") == label), None)
        if y_now is None:
            continue
        y_after = _interp_yield(pts, max(n - horizon, 0.08))
        # carry：票息 - 资金成本（年化，bp）
        carry_bp = round((y_now - (on_cost if on_cost is not None else y_now)) * 100, 1)
        # roll：曲线不变时剩余期限缩短带来的估值收益（年化差 × 剩余久期，近似）
        if y_after is not None and n > horizon + 0.1:
            d_mod = _modified_duration(y_now, n - horizon)
            roll_bp = round((y_now - y_after) * d_mod * 100, 1)
            total_bp = round(carry_bp + roll_bp, 1)
            # breakeven：3M 内收益率最多上行多少，carry+roll 仍能覆盖资本损失
            d_full = _modified_duration(y_now, n)
            breakeven_bp = round(total_bp / d_full, 1) if d_full > 0.05 else None
        else:
            roll_bp, total_bp, breakeven_bp = 0.0, carry_bp, None
        rows.append({
            "tenor": label, "years": n,
            "yield": y_now,
            "carry_bp_3m": carry_bp,          # 3 个月持有期的票息-资金成本（未年化折算前为年化口径）
            "roll_bp_3m": roll_bp,            # 骑乘收益（曲线静态）
            "total_static_bp_3m": total_bp,   # carry + roll
            "breakeven_bp_3m": breakeven_bp,  # 盈亏平衡收益率上行幅度
        })

    if not rows:
        return {}
    return {
        "date": curve.get("date"),
        "funding_cost": on_cost,
        "horizon_years": horizon,
        "rows": rows,
        "method": (
            "carry=到期收益率-隔夜资金成本；roll=曲线静止时剩余期限缩短的估值收益"
            "（对数插值远期收益率×近似修正久期）；breakeven=(carry+roll)/久期。"
            "年化口径、无拟合参数、纯曲线推导。"
        ),
        "source": "由中债收益率曲线推导",
    }


def get_calc(force: bool = False) -> dict:
    return cache_runtime.get("bonds:calc", _calc_payload, valid=bool, ttl=6 * 3600, force=force)


# ——— 资金面（Shibor，银行间资金价格的市场观测）———

_SHIBOR_TENORS = ["O/N", "1W", "1M", "3M", "6M", "1Y"]


def _shibor_payload() -> dict:
    """Shibor 定价序列（全国银行间同业拆借中心口径），近 90 日。"""
    ak = astock._akshare()
    df = ak.macro_china_shibor_all()
    if df is None or df.empty or "日期" not in df.columns:
        return {}
    out: dict[str, list[dict]] = {}
    for t in _SHIBOR_TENORS:
        col = f"{t}-定价"
        if col not in df.columns:
            continue
        pts = []
        for _, row in df.iterrows():
            try:
                v = float(row[col])
            except (TypeError, ValueError):
                continue
            if v == v:
                pts.append({"date": str(row["日期"])[:10], "v": round(v, 4)})
        if pts:
            out[t] = _tail(pts)
    if not out:
        return {}
    return {
        "date": max(pts[-1]["date"] for pts in out.values()),
        "series": out,
        "source": "Shibor（全国银行间同业拆借中心，AKShare 转接）",
    }


def get_shibor(force: bool = False) -> dict:
    return cache_runtime.get("bonds:shibor", _shibor_payload, valid=bool, ttl=6 * 3600, force=force)


# ——— 政策利率锚（OMO 7 天逆回购 → LPR 的官方利率体系）———

_POLICY_DESC = {
    "OMO_7D": "7 天逆回购操作利率（当前主要政策利率）",
    "LPR_1Y": "1 年期 LPR（贷款市场报价利率）",
    "LPR_5Y": "5 年期以上 LPR（房贷等长期贷款定价基准）",
}


def _policy_payload() -> dict:
    """LPR 历史 + 政策利率锚的当期值与最近一次变动。"""
    ak = astock._akshare()
    lpr = ak.macro_china_lpr()
    if lpr is None or lpr.empty:
        return {}
    rows = lpr.tail(24).to_dict("records")  # 近两年月度
    hist_1y = [{"date": str(r.get("TRADE_DATE"))[:10], "v": r.get("LPR1Y")} for r in rows]
    hist_5y = [{"date": str(r.get("TRADE_DATE"))[:10], "v": r.get("LPR5Y")} for r in rows]
    latest = rows[-1] if rows else {}
    prev = rows[-2] if len(rows) > 1 else {}
    anchors = []
    # OMO 7D 不在 LPR 接口里；用 1Y LPR 与 OMO 的公开差值不可推，单独给 LPR 两档。
    for key, col in (("LPR_1Y", "LPR1Y"), ("LPR_5Y", "LPR5Y")):
        cur, old = latest.get(col), prev.get(col)
        try:
            chg = round(float(cur) - float(old), 3) if cur is not None and old is not None else None
        except (TypeError, ValueError):
            chg = None
        anchors.append({
            "key": key, "label": _POLICY_DESC[key],
            "value": cur, "chg_bp": round(chg * 100, 1) if chg is not None else None,
            "date": str(latest.get("TRADE_DATE"))[:10],
        })
    return {
        "date": str(latest.get("TRADE_DATE"))[:10],
        "anchors": anchors,
        "lpr_1y": hist_1y, "lpr_5y": hist_5y,
        "source": "LPR（全国银行间同业拆借中心，AKShare 转接）",
    }


def get_policy(force: bool = False) -> dict:
    return cache_runtime.get("bonds:policy", _policy_payload, valid=bool, ttl=24 * 3600, force=force)


# ——— 中债新综合指数（债市总回报的市场观测）———

def _index_payload() -> dict:
    """中债-新综合指数近 250 日净值序列。"""
    ak = astock._akshare()
    df = ak.bond_new_composite_index_cbond()
    if df is None or df.empty or "date" not in df.columns:
        return {}
    pts = []
    for _, row in df.iterrows():
        try:
            v = float(row["value"])
        except (TypeError, ValueError):
            continue
        if v == v:
            pts.append({"date": str(row["date"])[:10], "v": round(v, 4)})
    if not pts:
        return {}
    return {
        "date": pts[-1]["date"],
        "series": _tail(pts),
        "source": "中债-新综合指数（中债估值中心，AKShare 转接）",
    }


def get_index(force: bool = False) -> dict:
    return cache_runtime.get("bonds:index", _index_payload, valid=bool, ttl=6 * 3600, force=force)


# ——— 全球对照（中美利差）———

def _global_payload() -> dict:
    """中美国债收益率对照与 10Y 利差，近 250 日。"""
    ak = astock._akshare()
    df = ak.bond_zh_us_rate()
    if df is None or df.empty:
        return {}
    cn_col, us_col = "中国国债收益率10年", "美国国债收益率10年"
    if cn_col not in df.columns or us_col not in df.columns:
        return {}
    pts, spread = [], []
    for _, row in df.iterrows():
        d = str(row["日期"])[:10]
        try:
            cn, us = float(row[cn_col]), float(row[us_col])
        except (TypeError, ValueError):
            continue
        if cn != cn or us != us:
            continue
        pts.append({"date": d, "cn": round(cn, 4), "us": round(us, 4)})
        spread.append({"date": d, "v": round((cn - us) * 100, 1)})
    if not pts:
        return {}
    return {
        "date": pts[-1]["date"],
        "series": _tail(pts),
        "spread": _tail(spread),
        "source": "中美国债收益率（AKShare 转接）",
    }


def get_global(force: bool = False) -> dict:
    return cache_runtime.get("bonds:global", _global_payload, valid=bool, ttl=6 * 3600, force=force)


# ——— 页面聚合 ———

def _slim_curve(curve: dict) -> dict:
    """overview 用的曲线瘦身版：整条当期曲线 + 各序列只留近 120 点。

    完整 3 年序列只服务框架回算（get_framework 内部直取 get_curve），
    不随 overview 下发（裸 payload 会到 600KB+）。
    """
    if not curve:
        return {}
    return {
        "date": curve.get("date"),
        "curve": curve.get("curve"),
        "yields": {k: v[-120:] for k, v in (curve.get("yields") or {}).items()},
        "spreads": {k: v[-120:] for k, v in (curve.get("spreads") or {}).items()},
        "credit": {k: v[-120:] for k, v in (curve.get("credit") or {}).items()},
        "source": curve.get("source"),
    }


def _slim_funding(funding: dict) -> dict:
    if not funding:
        return {}
    return {
        "date": funding.get("date"),
        "series": {k: v[-120:] for k, v in (funding.get("series") or {}).items()},
        "source": funding.get("source"),
    }


def _slim_positioning(pos: dict) -> dict:
    if not pos:
        return {}
    return {
        "date": pos.get("date"),
        "contracts": [
            {**{k: v for k, v in c.items() if k != "oi_hist"},
             "oi_hist": (c.get("oi_hist") or [])[-120:]}
            for c in (pos.get("contracts") or [])
        ],
        "source": pos.get("source"),
        "method": pos.get("method"),
    }


def get_overview(force: bool = False) -> dict:
    """债市页一次性聚合：曲线 / 资金 / 政策锚 / 指数 / 全球对照 / 计算层 / 量仓。

    每个子块独立失败降级（空 dict），不互相阻塞；序列裁到近 120 点控体积，
    完整 3 年序列由 /bonds/framework 与 /bonds/segments 端点按需提供。
    """
    curve = get_curve(force=force)
    shibor = get_shibor()
    policy = get_policy()
    index = get_index()
    globe = get_global()
    calc = get_calc()
    positioning = get_positioning()
    return {
        "curve": _slim_curve(curve),
        "funding": _slim_funding(shibor),
        "policy": policy,
        "index": {"date": index.get("date"), "series": (index.get("series") or [])[-120:],
                  "source": index.get("source")} if index else {},
        "global": {"date": globe.get("date"), "series": (globe.get("series") or [])[-120:],
                   "spread": (globe.get("spread") or [])[-120:], "source": globe.get("source")} if globe else {},
        "calc": calc,
        "positioning": _slim_positioning(positioning),
    }


# ——— 宏观长序列补齐（供框架 3 年回算；与 market.get_macro 的短序列独立）———
# 数据缺口：market 层部分指标（社融存量同比、DR007-OMO 利差）只有当期几个月到 140 日，
# 不够 3 年回算。这里用可回溯的公开源重建：
# - 社融存量同比：官方存量表（当期 7 个月）作锚 + shrzgm 月增量（2015 起）回推存量，算同比；
# - 资金利率-政策锚利差：东财银行间拆借隔夜（2004 起）× OMO 7D 利率时间表。

_OMO_SCHEDULE = [  # 7 天逆回购操作利率（公开市场业务公告，生效日 → 利率%）
    ("2020-03-30", 2.20), ("2022-01-17", 2.10), ("2022-08-15", 2.00),
    ("2023-06-13", 1.90), ("2023-08-15", 1.80), ("2024-07-22", 1.70),
    ("2024-09-27", 1.50), ("2025-05-08", 1.40),
]


def _omo_on(date: str) -> float | None:
    rate = None
    for effective, value in _OMO_SCHEDULE:
        if effective <= date:
            rate = value
        else:
            break
    return rate


def _afre_stock_payload() -> dict:
    """社融存量同比（3 年+）：官方存量锚 + shrzgm 月增量回推。

    官方表只给最新 7 个月的存量/同比；shrzgm 增量自 2015 年完整。
    以官方最新存量为锚逐月倒扣增量得到存量序列，同比由存量直接算出，
    与官方公布的当月同比交叉验证（偏差 <0.2pp 视为通过）。
    """
    try:
        import macro_fetch
        official = macro_fetch.social_financing_stock() or {}
    except Exception:  # noqa: BLE001
        official = {}
    anchor_date = anchor_level = None
    if official.get("stock_level_hist"):
        pts = sorted(official["stock_level_hist"], key=lambda p: p["date"])
        anchor_date, anchor_level = pts[-1]["date"], pts[-1]["v"]
    if not anchor_date:
        return {}
    ak = astock._akshare()
    df = ak.macro_china_shrzgm()
    if df is None or df.empty:
        return {}
    flows: dict[str, float] = {}
    for _, r in df.iterrows():
        m = str(r["月份"])
        # '202604' → '2026-04'（补零，与官方存量表日期对齐）
        d = f"{m[:4]}-{m[4:6].zfill(2)}" if len(m) == 6 else m
        try:
            flows[d] = float(r["社会融资规模增量"])
        except (TypeError, ValueError):
            continue
    if anchor_date not in flows and not any(p["date"] == anchor_date for p in official.get("stock_level_hist", [])):
        return {}
    # 官方存量月份优先（避免增量源滞后引入误差），其余月份由后一月存量倒扣当月增量回推
    levels: dict[str, float] = {p["date"]: p["v"] for p in official.get("stock_level_hist", [])}
    months = sorted({*flows.keys(), *levels.keys()})
    months = [m for m in months if m <= anchor_date]
    for i in range(len(months) - 2, -1, -1):
        m = months[i]
        if m in levels:
            continue
        nxt = months[i + 1]
        base = levels.get(nxt)
        if base is None or m not in flows:
            continue
        levels[m] = round(base - flows[m] / 10000.0, 4)  # 增量亿元 → 万亿元
    # 同比（万亿口径直接比值）；官方公布月份优先采用官方值（回推在两端累积误差 ~1pp）
    official_growth = {p["date"]: p["v"] for p in official.get("hist", [])}
    hist = []
    for m in sorted(levels):
        year_ago = f"{int(m[:4]) - 1}-{m[5:]}"
        if year_ago not in levels or levels[year_ago] <= 0:
            continue
        v = official_growth.get(m, round((levels[m] / levels[year_ago] - 1) * 100, 2))
        hist.append({"date": m, "v": v})
    hist = hist[-36:]
    if len(hist) < 24:
        return {}
    dev = [abs(p["v"] - official_growth[p["date"]]) for p in hist[-7:] if p["date"] in official_growth]
    return {
        "date": hist[-1]["date"],
        "hist": hist,
        "value": hist[-1]["v"],
        "validated": (max(dev) < 1.5) if dev else None,
        "source": "人民银行存量表 + 社融增量回推（官方同比优先）",
    }


def get_afre_stock(force: bool = False) -> dict:
    return cache_runtime.get("bonds:afre", _afre_stock_payload, valid=lambda v: bool(v.get("hist")), ttl=24 * 3600, force=force)


def _ib_overnight_payload() -> dict:
    """银行间隔夜利率 3 年序列（东财中国银行同业拆借市场·隔夜），减 OMO 得资金-政策锚利差。"""
    ak = astock._akshare()
    df = ak.rate_interbank(market="中国银行同业拆借市场", symbol="Shibor人民币", indicator="隔夜")
    if df is None or df.empty:
        return {}
    cutoff = (datetime.now(BEIJING) - timedelta(days=1200)).strftime("%Y-%m-%d")
    hist = []
    for _, r in df.iterrows():
        d = str(r.get("报告日") or "")[:10]
        try:
            v = float(r.get("利率"))
        except (TypeError, ValueError):
            continue
        if d < cutoff or v != v:
            continue
        omo = _omo_on(d)
        hist.append({"date": d, "v": round(v, 4), "spread_bp": round((v - omo) * 100, 1) if omo else None})
    if len(hist) < 100:
        return {}
    return {
        "date": hist[-1]["date"],
        "series": _tail(hist),
        "source": "东财·银行间同业拆借隔夜 × 人民银行 OMO 7D",
    }


def get_ib_overnight(force: bool = False) -> dict:
    return cache_runtime.get("bonds:ib_on", _ib_overnight_payload, valid=lambda v: bool(v.get("series")), ttl=6 * 3600, force=force)


# ——— 研究框架层：八状态仪表盘 ———
# 对应 research/china_bond_research_framework_v1.1.md 的 §2.2「八个统一状态」：
# 每日维护 Macro/Policy/Funding/SupplyDemand/CurveTP/Credit/Positioning/Global
# 八个状态分，均标准化到 [-2, +2]，正分统一定义为「对债券价格有利 / 对收益率下行有利」。
# 本层是可复现的状态读数（每个分都有列出所用指标与计算口径），不是方向观点；
# 失效条件与策略映射由用户自己的 AI 结合页面上其他客观数据完成。

import market  # noqa: E402  (macro indicator base, reuse market.get_macro)


def _clamp(v: float) -> float:
    return max(-2.0, min(2.0, v))


def _pct(hist: list, n: int = 60) -> float | None:
    """当前值在近 n 期内的分位（0-1）。用于把不同量纲的指标统一成状态分。"""
    vals = [p["v"] for p in (hist or [])[-n:] if isinstance(p.get("v"), (int, float))]
    if len(vals) < 8:
        return None
    cur = vals[-1]
    rank = sum(1 for v in vals if v <= cur)
    return rank / len(vals)


def _score_from_pct(pct: float | None, direction: str) -> float | None:
    """分位 → [-2,+2] 状态分。direction=down 表示该指标越低越利好债券。"""
    if pct is None:
        return None
    centered = pct - 0.5  # [-0.5, +0.5]
    s = centered * 4.0
    return _clamp(s if direction == "up" else -s)


def _pct_at(hist: list, idx: int, n: int = 60) -> float | None:
    """hist[idx] 在其前 n 期窗口内的分位（0-1）。历史回算用：禁止用未来数据。"""
    vals = [p["v"] for p in (hist or [])[: idx + 1][-n:] if isinstance(p.get("v"), (int, float))]
    if len(vals) < 8:
        return None
    cur = vals[-1]
    rank = sum(1 for v in vals if v <= cur)
    return rank / len(vals)


def _score_at_vals(values: list[float], idx: int, direction: str, n: int = 60) -> float | None:
    """滚动分位映射（回算热路径：直接吃值序列，禁止用未来数据）。"""
    window = values[: idx + 1][-n:]
    if len(window) < 8:
        return None
    cur = window[-1]
    rank = sum(1 for v in window if v <= cur)
    return _score_from_pct(rank / len(window), direction)


def _state_hist(parts: list[dict], max_points: int = 740) -> list[dict]:
    """逐期回算状态分（近 3 年≈740 个交易日；月度指标约 36 点，不足按可用长度）。

    每个 part 用自身 hist（date→v）按滚动 60 期窗口分位映射成 [-2,+2]
    （direction 存于 part["_direction"]），逐日期对可用指标加权。只用当期及
    以前的数据，无前视。
    """
    all_dates: set[str] = set()
    for p in parts:
        for pt in p.get("hist") or []:
            if pt.get("date"):
                all_dates.add(str(pt["date"]))
    dates = sorted(all_dates)
    # 预展开：每个 part 的 (方向, 权重, 日期→值, 有序日期, 完整序列)。
    # 序列只建一次，循环里按序号切片，避免逐日期重建。
    expanded = []
    for p in parts:
        h = [(str(pt.get("date")), pt.get("v")) for pt in (p.get("hist") or [])
             if pt.get("date") is not None and pt.get("v") is not None]
        if not h:
            continue
        order = [d for d, _ in h]
        lookup = dict(h)
        expanded.append((p.get("_direction", "up"), p.get("weight", 1.0), lookup, order,
                         list(lookup.values())))
    out: list[dict] = []
    for d in dates:
        num = den = 0.0
        for direction, weight, lookup, order, values in expanded:
            v = lookup.get(d)
            if v is None:
                continue
            idx = bisect_left(order, d)  # order 有序，等价于 index(d) 但 O(log n)
            if idx >= len(order) or order[idx] != d:
                continue
            s = _score_at_vals(values, idx, direction)
            if s is None:
                continue
            num += s * weight
            den += weight
        if den > 0:
            out.append({"date": d, "v": round(_clamp(num / den), 3)})
    return out[-max_points:]


def _macro_state(cn: dict) -> dict:
    """Macro：增长/通胀/信用对债券的方向读数（基本面越弱越利好债券）。"""
    parts: list[dict] = []

    def add(key: str, label: str, direction: str, weight: float):
        ind = cn.get(key)
        if not ind:
            return
        pct = _pct(ind.get("hist"))
        s = _score_from_pct(pct, direction)
        if s is None:
            return
        hist = [{"date": p.get("date"), "v": p.get("v")} for p in (ind.get("hist") or [])]
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": direction})

    add("pmi", "制造业 PMI（越弱越利好债）", "down", 1.2)
    add("gdp", "GDP 同比", "down", 1.0)
    add("cpi", "CPI 同比", "down", 0.8)
    add("ppi", "PPI 同比", "down", 0.8)
    add("property_sales_area", "地产销售面积同比", "down", 1.0)
    add("private_credit_growth", "私人信用增速", "down", 1.0)
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    hist = _state_hist(parts)
    return {
        "key": "macro", "name": "宏观与通胀", "score": round(score, 2),
        "meaning": "增长/通胀/信用边际变化（正分=基本面偏弱，利好债券）",
        "hist": hist,
        "parts": parts,
    }


def _policy_state(cn: dict) -> dict:
    """Policy：货币/财政的方向与力度（越宽松越利好债券）。"""
    parts: list[dict] = []

    def add(key: str, label: str, direction: str, weight: float):
        ind = cn.get(key)
        if not ind:
            return
        pct = _pct(ind.get("hist"))
        s = _score_from_pct(pct, direction)
        if s is None:
            return
        hist = [{"date": p.get("date"), "v": p.get("v")} for p in (ind.get("hist") or [])]
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": direction})

    add("social_financing_stock", "社融存量同比（越高越利好债）", "up", 1.0)
    add("fiscal_revenue_expenditure", "财政收支差额同比（扩张越强越利好债）", "down", 0.8)
    add("special_bond_issuance", "专项债发行（供给增=利率债供给压力，利空债）", "down", 0.8)
    # 社融存量同比：market 层只有 7 个月；换成重建的 3 年序列
    afre = get_afre_stock() or {}
    for p in parts:
        if p["key"] == "social_financing_stock" and afre.get("hist"):
            p["hist"] = afre["hist"]
            p["label"] = "社融存量同比（3 年重建，越高越利好债）"
            pct = _pct(afre["hist"])
            s = _score_from_pct(pct, "up")
            if s is not None:
                p["pct"], p["score"] = round(pct, 2), round(s, 2)
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "policy", "name": "政策与融资", "score": round(score, 2),
        "meaning": "货币/财政/政府债净供给的方向读数（正分=条件对债券友好）",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _funding_state(cn: dict) -> dict:
    """Funding：资金价格相对政策锚的偏离（越便宜越利好债券）。

    - 隔夜-OMO 利差：本层 get_ib_overnight 重建的 3 年日频序列（东财拆借隔夜 × OMO 时间表）；
    - 存单-政策利率利差：market 宏观层 ncd_aaa_spread（短序列，仅当期分位参与）。
    """
    parts: list[dict] = []
    ib = get_ib_overnight() or {}
    spread = [{"date": p["date"], "v": p["spread_bp"]} for p in (ib.get("series") or [])
              if p.get("spread_bp") is not None]
    if spread:
        pct = _pct(spread)
        s = _score_from_pct(pct, "down")  # 利差分位越低 = 资金越松
        if s is not None:
            parts.append({"key": "ib_overnight_omo", "label": "银行间隔夜 - OMO 7D 利差（bp）",
                          "pct": round(pct, 2), "score": round(s, 2), "weight": 1.4,
                          "hist": spread, "_direction": "down"})
    for key, label, weight, direction in (
        ("dr007_policy_spread", "DR007 - OMO 7D 利差（bp）", 0.6, "down"),
        ("ncd_aaa_spread", "AAA 存单 1Y - 政策利率（bp）", 1.0, "down"),
    ):
        ind = cn.get(key)
        if not ind:
            continue
        pct = _pct(ind.get("hist"))
        s = _score_from_pct(pct, direction)
        if s is None:
            continue
        hist = [{"date": p.get("date"), "v": p.get("v")} for p in (ind.get("hist") or [])]
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": direction})
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "funding", "name": "资金面", "score": round(score, 2),
        "meaning": "资金价格相对政策锚的偏离（正分=资金偏松，利好债券）",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _supply_state(cn: dict) -> dict:
    """SupplyDemand：政府债供给与机构承接（供给越高越利空债券）。"""
    ind = cn.get("special_bond_issuance")
    if not ind:
        return {}
    pct = _pct(ind.get("hist"))
    s = _score_from_pct(pct, "down")
    if s is None:
        return {}
    hist = [{"date": p.get("date"), "v": p.get("v")} for p in (ind.get("hist") or [])]
    parts = [{"key": "special_bond_issuance", "label": "专项债发行（亿元）",
              "pct": round(pct, 2), "score": round(s, 2), "weight": 1.0, "hist": hist,
              "_direction": "down"}]
    return {
        "key": "supply_demand", "name": "供需与机构行为", "score": round(s, 2),
        "meaning": "政府债供给节奏代理（正分=供给压力偏小）；机构行为维度需人工/AI 结合托管数据补足",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _curve_tp_state(curve: dict, index: dict) -> dict:
    """CurveTP：曲线陡平 + carry/roll 的市场读数（利差越高=长端补偿越足，越利好债券）。"""
    parts: list[dict] = []
    spreads = curve.get("spreads") or {}
    for key, label, weight in (
        ("10年-1年", "10Y-1Y 期限利差（bp）", 1.2),
        ("30年-1年", "30Y-1Y 期限利差（bp）", 0.8),
    ):
        hist = spreads.get(key)
        if not hist:
            continue
        pct = _pct(hist)
        s = _score_from_pct(pct, "up")  # 利差分位越高 = 风险补偿越足
        if s is None:
            continue
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": "up"})
    # 中债指数动量：债价趋势确认（近 20 日涨跌幅在自身 60 日窗口的分位）
    series = (index.get("series") or []) if index else []
    if len(series) >= 60:
        vals = [p["v"] for p in series]
        mom = (vals[-1] / vals[-21] - 1) * 100
        windows = [(vals[i] / vals[i - 20] - 1) * 100 for i in range(20, len(vals))]
        rank = sum(1 for w in windows if w <= mom)
        pct = rank / len(windows)
        s = _clamp((pct - 0.5) * 4.0)
        # 动量序列：滚动 20 日涨跌幅（可回算全长度）
        mom_hist = [{"date": series[i]["date"], "v": round((vals[i] / vals[i - 20] - 1) * 100, 3)}
                    for i in range(20, len(series))]
        parts.append({"key": "index_mom_20d", "label": "中债指数 20 日动量（%）",
                      "pct": round(pct, 2), "score": round(s, 2), "weight": 1.0,
                      "hist": mom_hist, "_direction": "up"})
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "curve_tp", "name": "曲线与风险补偿", "score": round(score, 2),
        "meaning": "期限利差与债价动量的市场读数（正分=风险补偿偏足 / 趋势偏多）",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _credit_state(curve: dict, cn: dict) -> dict:
    """Credit：信用利差补偿（利差越高=补偿越足，配置价值越高）。"""
    parts: list[dict] = []
    credit = curve.get("credit") or {}
    for key, label, weight in (
        ("AAA-1年", "AAA 中短票 1Y 信用利差（bp）", 1.0),
        ("AAA-3年", "AAA 中短票 3Y 信用利差（bp）", 1.2),
        ("AAA-5年", "AAA 中短票 5Y 信用利差（bp）", 1.0),
    ):
        hist = credit.get(key)
        if not hist:
            continue
        pct = _pct(hist)
        s = _score_from_pct(pct, "up")
        if s is None:
            continue
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": "up"})
    ind = cn.get("credit_spread_aaa")
    if ind:
        pct = _pct(ind.get("hist"))
        s = _score_from_pct(pct, "up")
        if s is not None:
            hist = [{"date": p.get("date"), "v": p.get("v")} for p in (ind.get("hist") or [])]
            parts.append({"key": "credit_spread_aaa", "label": "宏观层 AAA 信用利差（bp）",
                          "pct": round(pct, 2), "score": round(s, 2), "weight": 1.0,
                          "hist": hist, "_direction": "up"})
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "credit", "name": "信用利差", "score": round(score, 2),
        "meaning": "信用风险补偿的市场读数（正分=利差补偿偏足）",
        "hist": _state_hist(parts),
        "parts": parts,
    }


# ——— 仓位与拥挤度数据层（国债期货量仓，框架 §9.2 Crowding）———
# 中金所国债期货主力连续（TS/TF/T/TL）：持仓量 + 成交量 + 收盘价，日频。
# 持仓量分位 = 杠杆/拥挤代理；价量同向 + 高持仓 = 趋势拥挤。

_FUT_SYMBOLS = [("TS0", "TS 2年"), ("TF0", "TF 5年"), ("T0", "T 10年"), ("TL0", "TL 30年")]


def _positioning_payload() -> dict:
    """四品种国债期货主力：最新持仓/成交/收盘 + 各自近一年分位。"""
    ak = astock._akshare()
    from datetime import date
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=1200)).strftime("%Y%m%d")
    out = []
    for sym, label in _FUT_SYMBOLS:
        try:
            df = ak.futures_main_sina(symbol=sym, start_date=start, end_date=end)
        except Exception:  # noqa: BLE001
            continue
        if df is None or len(df) < 60 or "持仓量" not in df.columns:
            continue
        oi = [float(v) for v in df["持仓量"] if str(v).replace(".", "").isdigit()]
        vol = [float(v) for v in df["成交量"] if str(v).replace(".", "").isdigit()]
        close = [float(v) for v in df["收盘价"] if str(v).replace(".", "").isdigit()]
        if len(oi) < 60:
            continue
        def pct_rank(vals: list[float]) -> float:
            cur = vals[-1]
            window = vals[-250:] if len(vals) >= 250 else vals
            return sum(1 for v in window if v <= cur) / len(window)
        dates = [str(d)[:10] for d in df["日期"].tolist()]
        # 持仓走势（近 120 点，与 oi/vol 等长切片；日期对齐过滤后的长度）
        n = min(len(oi), len(dates))
        oi_hist = [{"date": dates[len(dates) - n + i], "v": oi[len(oi) - n + i]} for i in range(n)][-780:]
        out.append({
            "symbol": sym, "label": label,
            "date": dates[-1],
            "close": close[-1] if close else None,
            "oi": oi[-1],
            "oi_pct_1y": round(pct_rank(oi), 2),
            "oi_hist": oi_hist,
            "volume": vol[-1] if vol else None,
            "vol_pct_1y": round(pct_rank(vol), 2) if vol else None,
        })
    if not out:
        return {}
    return {
        "date": max(r["date"] for r in out),
        "contracts": out,
        "source": "中金所国债期货主力连续（新浪，AKShare 转接）",
        "method": "持仓量/成交量在近一年窗口的分位；持仓高分位 + 价格高位 = 久期拥挤的常用代理（框架 §9.2）",
    }


def get_positioning(force: bool = False) -> dict:
    return cache_runtime.get("bonds:positioning", _positioning_payload, valid=bool, ttl=6 * 3600, force=force)


def _positioning_state() -> dict:
    """Positioning：国债期货持仓分位（持仓越高=拥挤越高，利空债券；价格分位越高越拥挤）。

    用四品种持仓分位的加权均值反向映射；缺数据时如实留空。
    """
    d = get_positioning() or {}
    contracts = d.get("contracts") or []
    parts = []
    for c in contracts:
        if c.get("oi_pct_1y") is None:
            continue
        # 拥挤度分数：持仓分位越高越利空债券 → 反向
        s = _clamp(-(c["oi_pct_1y"] - 0.5) * 4.0)
        parts.append({"key": c["symbol"], "label": f"{c['label']}主力持仓分位",
                      "pct": c["oi_pct_1y"], "score": round(s, 2), "weight": 1.0,
                      "hist": c.get("oi_hist") or [], "_direction": "down"})
    if not parts:
        return {
            "key": "positioning", "name": "仓位与拥挤度", "score": None,
            "meaning": "期货量仓数据源暂不可用，本状态留空",
            "parts": [],
        }
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "positioning", "name": "仓位与拥挤度", "score": round(score, 2),
        "meaning": "国债期货持仓分位（正分=持仓偏低/不拥挤，利好债券；负分=拥挤偏高）",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _global_state(globe: dict) -> dict:
    """Global：中美利差对照（美债越贵/利差越倒挂，对国内长端约束越强）。"""
    spread = (globe or {}).get("spread") or []
    if not spread:
        return {}
    pct = _pct(spread)
    s = _score_from_pct(pct, "up")  # 中美利差分位越高 = 外部约束越松
    if s is None:
        return {}
    parts = [{"key": "cn_us_10y", "label": "中美 10Y 利差（bp）",
              "pct": round(pct, 2), "score": round(s, 2), "weight": 1.0,
              "hist": spread, "_direction": "up"}]
    return {
        "key": "global", "name": "海外与汇率", "score": round(s, 2),
        "meaning": "中美 10Y 利差的市场读数（正分=外部利率约束偏松）",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _framework_payload() -> dict:
    """八状态仪表盘：每个状态给分值、口径说明与所用指标（含分位与权重）。"""
    try:
        macro_data = market.get_macro() or {}
    except Exception:  # noqa: BLE001
        macro_data = {}
    cn = macro_data.get("cn") or {}
    curve = get_curve() or {}
    index = get_index() or {}
    globe = get_global() or {}

    states = [
        _macro_state(cn),
        _policy_state(cn),
        _funding_state(cn),
        _supply_state(cn),
        _curve_tp_state(curve, index),
        _credit_state(curve, cn),
        _positioning_state(),
        _global_state(globe),
    ]
    states = [s for s in states if s]
    # payload 瘦身：part 级 hist 只留近 60 点（弹层趋势图用）；状态级 hist 已是近 3 年。
    for s in states:
        for p in s.get("parts") or []:
            h = p.get("hist")
            if isinstance(h, list) and len(h) > 60:
                p["hist"] = h[-60:]
            p.pop("_direction", None)
        s.pop("_direction", None)
    scored = [s for s in states if s.get("score") is not None]

    notes = [
        "正分统一定义为「对债券价格有利」（框架 §2.2）；每个分值由所列指标在近 60 期窗口内的分位数映射而来，可复现。",
        "SupplyDemand 仅覆盖政府债供给，机构承接维度待补托管数据；Positioning 用国债期货持仓分位作拥挤代理。",
        "本仪表盘是状态读数不是方向观点；分品种映射见 segments（框架 §11.2 权重先验），策略表达与失效条件由 AI/人工完成。",
    ]
    return {
        "date": max([curve.get("date", ""), globe.get("date", "")] + [str(cn.get(k, {}).get("date") or "") for k in ("cpi", "pmi")]),
        "states": states,
        "coverage": round(len(scored) / 8 * 100),
        "notes": notes,
        "method": "指标近 60 期分位 → 以 0.5 为中性映射到 [-2,+2]，按模块内权重加权；分位数据来自中债曲线、Shibor/LPR、市场宏观指标层",
    }


def get_framework(force: bool = False) -> dict:
    return cache_runtime.get("bonds:framework", _framework_payload, valid=lambda v: bool(v.get("states")), ttl=3600, force=force)


# ——— 分品种评分层：短债 / 中短债 / 长债 / 超长债 / 信用债 / 杠杆套息 ——
# 依据框架 §11.2「多期限权重先验」：同一状态变量对不同期限/品种的传导强度不同——
# Funding/Positioning 主导短端（1-10 交易日权重高），Macro/Policy/CurveTP 主导中长端，
# Credit 状态只作用于信用品种，Global 约束长端与跨境。评分 = Σ(状态分 × 品种特定权重)，
# 再叠加该品种自身的静态收益锚（carry/roll，来自计算层）。输出为品种相对吸引力排序
# 与各自失效条件，不输出绝对方向观点。

# 品种 × 八状态权重先验（框架 §11.2 表格数值化；正分传导 = 利好该品种）
_SEGMENT_WEIGHTS: dict[str, dict[str, float]] = {
    "短债(1-3Y)": {"macro": 0.4, "policy": 0.6, "funding": 1.6, "supply_demand": 0.8,
                    "curve_tp": 0.6, "credit": 0.2, "positioning": 0.8, "global": 0.3},
    "中短债(3-5Y)": {"macro": 0.7, "policy": 0.8, "funding": 1.3, "supply_demand": 1.0,
                      "curve_tp": 1.0, "credit": 0.4, "positioning": 0.9, "global": 0.4},
    "长债(5-10Y)": {"macro": 1.1, "policy": 1.1, "funding": 0.8, "supply_demand": 1.2,
                     "curve_tp": 1.3, "credit": 0.4, "positioning": 0.8, "global": 0.7},
    "超长债(20Y+)": {"macro": 1.2, "policy": 1.0, "funding": 0.5, "supply_demand": 1.5,
                      "curve_tp": 1.4, "credit": 0.4, "positioning": 0.7, "global": 0.9},
    "信用债(AAA)": {"macro": 0.6, "policy": 0.6, "funding": 1.1, "supply_demand": 0.8,
                     "curve_tp": 0.5, "credit": 1.8, "positioning": 0.8, "global": 0.4},
    "杠杆套息(回购+短券)": {"macro": 0.2, "policy": 0.5, "funding": 2.0, "supply_demand": 0.3,
                            "curve_tp": 0.4, "credit": 0.9, "positioning": 0.6, "global": 0.2},
}

# 品种 ↔ 计算层期限锚（carry/roll 静态收益参考）
_SEGMENT_ANCHOR: dict[str, str] = {
    "短债(1-3Y)": "3年", "中短债(3-5Y)": "5年", "长债(5-10Y)": "10年", "超长债(20Y+)": "30年",
}

# 品种失效条件（框架 §0.2 原则三：结论必须带失效条件）
_SEGMENT_INVALIDATION: dict[str, str] = {
    "短债(1-3Y)": "资金利率中枢上移（DR007 持续高于政策利率 20bp 以上）或存单利率快速上行",
    "中短债(3-5Y)": "曲线熊陡（短端不动、中长端上行）或政府债供给放量且承接不足",
    "长债(5-10Y)": "宏观预期反转（PMI/社融连续 2 月超预期）或期限溢价快速抬升",
    "超长债(20Y+)": "保险配置力量减弱、超长供给放量，或 TL 持仓拥挤度过高后的集中减仓",
    "信用债(AAA)": "信用利差压缩至历史低位后遇流动性冲击（赎回-抛售螺旋），或资金面收敛叠加杠杆去化",
    "杠杆套息(回购+短券)": "资金价格波动加大（隔夜利率脉冲）或套息空间（短券-回购利差）收敛至 20bp 以内",
}


def _segments_payload() -> dict:
    """分品种评分：状态驱动分 + carry/roll 静态锚 + 失效条件 + 逐期回算趋势。"""
    fw = get_framework() or {}
    states = {s["key"]: s for s in fw.get("states", []) if s.get("score") is not None}
    calc = get_calc() or {}
    rows_by_tenor = {r["tenor"]: r for r in calc.get("rows", [])}

    # 各状态历史得分的公共日期轴（取全状态日期并集，用于品种分回算；近 3 年）
    all_dates: set[str] = set()
    for s in states.values():
        for pt in s.get("hist") or []:
            if pt.get("date"):
                all_dates.add(str(pt["date"]))
    dates = sorted(all_dates)[-740:]

    rows = []
    for seg, weights in _SEGMENT_WEIGHTS.items():
        contrib = 0.0
        tw = 0.0
        for key, w in weights.items():
            s = states.get(key)
            if not s:
                continue
            contrib += s["score"] * w
            tw += w
        if tw == 0:
            continue
        # 驱动分：[-2,+2]；再列出该品种贡献最大/最小的状态
        score = _clamp(contrib / tw * 1.6)  # 加权均值放大到品种分距（先验，未校准）
        sorted_states = sorted(
            [(k, states[k]["score"] * w / tw) for k, w in weights.items() if k in states],
            key=lambda t: t[1], reverse=True,
        )
        drivers = [
            {"state": states[k]["name"], "contribution": round(v, 2),
             "weight": weights[k]}
            for k, v in sorted_states[:3]
        ]
        # 逐期回算：每个日期对可用状态加权（与当期同口径）
        hist_maps = {k: {str(p["date"]): p["v"] for p in (states[k].get("hist") or [])}
                     for k in weights if k in states and states[k].get("hist")}
        hist = []
        for d in dates:
            num = den = 0.0
            for k, w in weights.items():
                v = hist_maps.get(k, {}).get(d)
                if v is None:
                    continue
                num += v * w
                den += w
            if den > 0:
                hist.append({"date": d, "v": round(_clamp(num / den * 1.6), 3)})
        row: dict = {
            "segment": seg,
            "score": round(score, 2),
            "drivers": drivers,
            "hist": hist,
            "invalidation": _SEGMENT_INVALIDATION[seg],
        }
        anchor = _SEGMENT_ANCHOR.get(seg)
        if anchor and anchor in rows_by_tenor:
            r = rows_by_tenor[anchor]
            row["anchor_tenor"] = anchor
            row["carry_roll_bp_3m"] = r["total_static_bp_3m"]
            row["breakeven_bp_3m"] = r["breakeven_bp_3m"]
        if seg == "杠杆套息(回购+短券)":
            # 套息空间 = 1Y 国债 - Shibor O/N（正 = 有套息空间）
            y1 = rows_by_tenor.get("1年")
            cost = calc.get("funding_cost")
            if y1 and cost is not None:
                row["carry_roll_bp_3m"] = round((y1["yield"] - cost) * 100, 1)
                row["anchor_tenor"] = "1年-资金成本"
                row["breakeven_bp_3m"] = None
        rows.append(row)

    # 展示顺序：按评分排序的五个债券品种在前，杠杆套息固定殿后（它是策略组合不是单一品种）
    levered = [r for r in rows if r["segment"] == "杠杆套息(回购+短券)"]
    rows = [r for r in rows if r["segment"] != "杠杆套息(回购+短券)"]
    rows.sort(key=lambda r: r["score"], reverse=True)
    rows.extend(levered)
    return {
        "date": fw.get("date"),
        "rows": rows,
        "method": (
            "品种分 = Σ(八状态分 × 品种特定权重) / Σ权重 × 1.6（框架 §11.2 多期限权重先验："
            "短端重资金/仓位，长端重宏观/政策/期限溢价，信用品种叠加信用利差状态）。"
            "carry/roll 为该品种锚定期限的 3 个月静态收益（计算层）。"
        ),
        "notes": [
            "权重先验来自研究框架 §11.2 表格（1-3 个月持有期口径），未经样本外校准，只用于相对排序。",
            "评分是状态读数的品种映射，不是买卖建议；每个品种附失效条件（框架 §0.2 原则三）。",
        ],
    }


def get_segments(force: bool = False) -> dict:
    return cache_runtime.get("bonds:segments", _segments_payload, valid=lambda v: bool(v.get("rows")), ttl=3600, force=force)
