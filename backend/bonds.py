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

import json
import hashlib
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import astock
import cache_runtime
from bond_math import TENOR_YEARS, finite, scenario_rows
from bisect import bisect_left

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get("MW_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".market-workbench")


def _stamp(value: dict) -> dict:
    if value:
        value = {**value, "schema_version": 2, "generated_at": datetime.now(BEIJING).isoformat()}
    return value


def _snapshot_time(name: str) -> float:
    value = _load_snapshot(name) or {}
    try:
        return datetime.fromisoformat(value.get("generated_at") or "").timestamp()
    except ValueError:
        return 0.


def _midrank(values: list[float], current: float) -> float:
    values = [v for v in values if finite(v)]
    if len(values) < 2 or max(values) == min(values):
        return .5
    return (sum(v < current for v in values) + (sum(v == current for v in values) - 1) / 2) / (len(values) - 1)


def _source_meta(value: dict) -> dict:
    return {k: value.get(k) for k in ("generated_at", "cache_state", "cached_at", "refresh_error", "legacy_snapshot")}


def _part_source(key: str) -> str:
    return {"macro": "宏观数据层", "policy": "宏观政策/融资数据层", "funding": "Shibor/FDR007/存单与政策锚（代理）",
            "supply_demand": "新增专项债近3月月均发行（部分供给代理）", "positioning": "新浪国债期货主力连续（AKShare）",
            "global": "中美10年国债利差"}.get(key, "中债收益率曲线/财富指数")


def _part_url(key: str) -> str:
    return {"macro": "https://data.stats.gov.cn/", "policy": "https://www.pbc.gov.cn/",
            "funding": "https://www.chinamoney.com.cn/chinese/bkfrr/", "supply_demand": "https://www.mof.gov.cn/",
            "positioning": "https://www.cffex.com.cn/"}.get(key, "https://yield.chinabond.com.cn/")


def _status(date: str | None, frequency: str, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(BEIJING)
    try:
        if frequency != "daily":
            observed = datetime.strptime(str(date)[:7], "%Y-%m")
            lag = (now.year - observed.year) * 12 + now.month - observed.month
            return ("fresh" if 0 <= lag <= (4 if frequency == "quarterly" else 2) else "stale", "按月/季度发布节奏")
        observed = datetime.strptime(str(date)[:10], "%Y-%m-%d").date()
        # Prefer a local exchange calendar; never claim weekday fallback knows holidays.
        calendar_path = os.path.join(DATA_DIR, "factor", "calendar.json")
        try:
            with open(calendar_path, encoding="utf-8") as f:
                days = json.load(f).get("dates", [])
        except (OSError, ValueError, AttributeError):
            days = []
        today = now.date()
        if days and max(days) >= today.isoformat():
            lag = sum(observed.isoformat() < d < today.isoformat() for d in days)
            basis = "本地交易日历；当日收盘数据允许次日到达"
        else:
            lag = sum((observed + timedelta(days=i)).weekday() < 5 for i in range(1, max(0, (today - observed).days)))
            basis = "工作日近似（节假日日历不可用）"
        return ("fresh" if observed <= today and lag == 0 else "stale", basis)
    except (TypeError, ValueError):
        return "unknown", "观察日期未知"


def _segment_hist(parts: list[dict], dates: list[str]) -> list[dict]:
    expanded = [(p, sorted(((_available_date(x), x["v"]) for x in p["hist"]
                            if _available_date(x) and finite(x.get("v"))))) for p in parts]
    total = sum(p["weight"] for p in parts)
    out = []
    for d in dates:
        num = den = 0.
        for p, hist in expanded:
            ds = [x[0] for x in hist]
            idx = bisect_left(ds, d)
            if idx >= len(ds) or ds[idx] != d:
                idx -= 1
            if idx >= 0 and (datetime.fromisoformat(d) - datetime.fromisoformat(ds[idx])).days <= p["max_age_days"]:
                num += hist[idx][1] * p["weight"]
                den += p["weight"]
        if total and den / total >= .6:
            out.append({"date": d, "v": round(num / den, 3)})
    return out


def _snapshot_path(name: str) -> str:
    return os.path.join(DATA_DIR, f"bonds_{name}.json")


def _load_snapshot(name: str) -> dict | None:
    try:
        with open(_snapshot_path(name), encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            return None
        if name == "dashboard" and value.get("schema_version") != 2:
            return None
        if not value.get("generated_at"):
            value["legacy_snapshot"] = True
        return value
    except (OSError, json.JSONDecodeError):
        return None


def _save_snapshot(name: str, value: dict) -> None:
    """快照落盘是 best-effort：失败（含个别 payload 不 JSON 可序列化）只意味着
    下次重启需重拉，不能让数据请求本身失败。"""
    try:
        path = _snapshot_path(name)
        os.makedirs(DATA_DIR, exist_ok=True)
        temp = path + f".{threading.get_ident()}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temp, path)
        if name == "dashboard" and value.get("input_fingerprint") and value.get("input_vintage"):
            folder = os.path.join(DATA_DIR, "bonds_vintages")
            os.makedirs(folder, exist_ok=True)
            vintage_path = os.path.join(folder, f"{value['generated_at'][:10]}-{value['input_fingerprint']}.json")
            # Immutable input vintages begin now; they do not retroactively turn
            # historical reconstructed data into first-release observations.
            vintage_temp = vintage_path + f".{threading.get_ident()}.tmp"
            try:
                with open(vintage_temp, "w", encoding="utf-8") as handle:
                    json.dump({"generated_at": value["generated_at"], "input_fingerprint": value["input_fingerprint"],
                               "inputs": value["input_vintage"]}, handle, ensure_ascii=False, allow_nan=False)
                os.link(vintage_temp, vintage_path)
            except FileExistsError:
                pass
            finally:
                if os.path.exists(vintage_temp):
                    os.unlink(vintage_temp)
    except (OSError, TypeError, ValueError):
        pass

# 曲线关键期限（年 → DataFrame 列名）
TENORS: list[tuple[str, str]] = [
    ("3月", "3月"), ("6月", "6月"), ("1年", "1年"), ("2年", "2年"), ("3年", "3年"),
    ("5年", "5年"), ("7年", "7年"), ("10年", "10年"), ("30年", "30年"),
]

# 期限展示顺序（前端利差矩阵行序）
TENOR_ORDER = ["3月", "6月", "1年", "2年", "3年", "5年", "7年", "10年", "30年"]


def _tail(points: list[dict], n: int = 780) -> list[dict]:
    """截尾到 n 点（780 日 ≈ 3 年交易日；月度指标由调用方自行截 36）。"""
    return points[-n:] if len(points) > n else points


def _call_with_timeout(fn, *args, timeout: float = 30.0, **kwargs):
    """上游调用加超时：akshare 底层 requests 不设超时，一次卡死会同时卡住
    cache_runtime 冷启动 owner 和所有等待者。daemon 线程 + join 封顶，超时抛
    TimeoutError；挂死的线程随进程退出回收，不再累积阻塞。"""
    result: dict = {}

    def _run():
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 原样转交调用方的 except 分支
            result["error"] = exc

    name = getattr(fn, "__name__", "upstream")
    worker = threading.Thread(target=_run, daemon=True, name=f"upstream:{name}")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"{name} timed out after {timeout:.0f}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _fetch_yield_frame(days: int):
    """近 days 日的三条中债曲线（国债 / AAA 中短票 / AAA 商行债），失败返回 None。

    接口对超过约 1 年的窗口直接返回空，按 365 日分段拉取后拼接；
    各段并发请求并带超时（单段卡住最多耗一个超时周期，不再串行放大）。
    """
    import concurrent.futures

    ak = astock._akshare()
    end = datetime.now(BEIJING)
    segments: list[tuple[str, str]] = []
    remaining = days
    seg_end = end
    while remaining > 0:
        span = min(remaining, 365)
        seg_start = seg_end - timedelta(days=span)
        segments.append((seg_start.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d")))
        remaining -= span
        seg_end = seg_start

    def _pull(span: tuple[str, str]):
        try:
            return _call_with_timeout(ak.bond_china_yield, timeout=30.0,
                                      start_date=span[0], end_date=span[1])
        except Exception:  # noqa: BLE001 - 单段失败跳过，其余段照常拼接
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(segments))) as pool:
        frames = [df for df in pool.map(_pull, segments)
                  if df is not None and not df.empty]
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
            if finite(v):  # finite-value boundary
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
    curve = [{"tenor": label, "value": pts[-1]["v"], "date": pts[-1]["date"]} for label, pts in gov.items() if pts[-1]["date"] == last_date]

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
              if label in _TENOR_YEARS}

    return {
        "date": last_date,
        "curve": curve,
        "yields": yields,
        "spreads": spreads,
        "credit": credit,
        "source": "中债收益率曲线（AKShare 转接 chinabond）",
    }


def get_curve(force: bool = False) -> dict:
    return cache_runtime.get(
        "bonds:curve", lambda: _stamp(_curve_payload()), valid=bool, ttl=6 * 3600, force=force,
        warm=lambda: _load_snapshot("curve"), warm_time=lambda: _snapshot_time("curve"),
        save=lambda value: _save_snapshot("curve", value),
    )


# ——— 小型计算层：carry / roll / forward / breakeven（框架 §7.4 / §12.3）———
# 全部由当期曲线直接推导，确定性公式、无拟合参数；输入是曲线关键期限的收益率，
# 输出每个期限的持有期静态收益指标。只算不加判断。

# 关键期限（年），与 TENORS 对齐；O/N 资金成本取 Shibor 隔夜近似。
_TENOR_YEARS = TENOR_YEARS


def _calc_payload(curve: dict | None = None, funding: dict | None = None) -> dict:
    curve = get_curve() if curve is None else curve
    funding = get_shibor() if funding is None else funding
    pts = (curve or {}).get("curve") or []
    if len(pts) < 3:
        return {}
    on = (((funding or {}).get("series") or {}).get("O/N") or [{}])[-1].get("v")
    # A proxy cannot finance a different-date scenario without disclosure.
    aligned = bool(curve.get("date") and curve.get("date") == funding.get("date"))
    cost = on if aligned and finite(on) else None
    return {"date": curve.get("date"), "funding_cost": cost,
            "funding_observed": on, "funding_date": funding.get("date"),
            "funding_status": "proxy" if cost is not None else "missing_or_date_mismatch",
            "horizon_years": .25, "rows": scenario_rows(pts, cost),
            "method": "标准化假想平价债：本金100、半年付息、尾期按实际年数计息；3个月后按静态曲线估值。票息/到期本金不再投资。Carry为收益率不变的持有收益，Roll为静态曲线带来的额外估值收益。净收益扣初始资本的简单融资利息，未计杠杆、折扣、税费与交易成本；不是具体可交易券的报价。",
            "source": "中债曲线；Shibor O/N仅作同日无担保融资代理"}


def get_calc(force: bool = False) -> dict:
    return get_dashboard(force).get("calc") or {}


# ——— 资金面（Shibor，银行间资金价格的市场观测）———

_SHIBOR_TENORS = ["O/N", "1W", "1M", "3M", "6M", "1Y"]


def _shibor_payload() -> dict:
    """Shibor 定价序列（全国银行间同业拆借中心口径），近 90 日。"""
    ak = astock._akshare()
    df = _call_with_timeout(ak.macro_china_shibor_all)
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
            if finite(v):
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
    return cache_runtime.get(
        "bonds:shibor", lambda: _stamp(_shibor_payload()), valid=bool, ttl=6 * 3600, force=force,
        warm=lambda: _load_snapshot("shibor"), warm_time=lambda: _snapshot_time("shibor"),
        save=lambda value: _save_snapshot("shibor", value),
    )


# ——— 政策利率锚（OMO 7 天逆回购 → LPR 的官方利率体系）———

_POLICY_DESC = {
    "OMO_7D": "7 天逆回购操作利率（当前主要政策利率）",
    "LPR_1Y": "1 年期 LPR（贷款市场报价利率）",
    "LPR_5Y": "5 年期以上 LPR（房贷等长期贷款定价基准）",
}


def _policy_payload() -> dict:
    """LPR 历史 + 政策利率锚的当期值与最近一次变动。"""
    ak = astock._akshare()
    lpr = _call_with_timeout(ak.macro_china_lpr)
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
        "omo_status": "当前OMO未在此接口自动核验；历史手工政策锚只作待核验代理",
        "lpr_1y": hist_1y, "lpr_5y": hist_5y,
        "source": "LPR（全国银行间同业拆借中心，AKShare 转接）",
    }


def get_policy(force: bool = False) -> dict:
    return cache_runtime.get(
        "bonds:policy", lambda: _stamp(_policy_payload()), valid=bool, ttl=24 * 3600, force=force,
        warm=lambda: _load_snapshot("policy"), warm_time=lambda: _snapshot_time("policy"),
        save=lambda value: _save_snapshot("policy", value),
    )


# ——— 中债新综合指数（债市总回报的市场观测）———

def _index_payload() -> dict:
    """中债-新综合指数近 250 日净值序列。"""
    ak = astock._akshare()
    df = _call_with_timeout(ak.bond_new_composite_index_cbond)
    if df is None or df.empty or "date" not in df.columns:
        return {}
    pts = []
    for _, row in df.iterrows():
        try:
            v = float(row["value"])
        except (TypeError, ValueError):
            continue
        if finite(v):
            pts.append({"date": str(row["date"])[:10], "v": round(v, 4)})
    if not pts:
        return {}
    return {
        "date": pts[-1]["date"],
        "series": _tail(pts),
        "source": "中债-新综合财富指数（总值，AKShare 转接）",
    }


def get_index(force: bool = False) -> dict:
    return cache_runtime.get(
        "bonds:index", lambda: _stamp(_index_payload()), valid=bool, ttl=6 * 3600, force=force,
        warm=lambda: _load_snapshot("index"), warm_time=lambda: _snapshot_time("index"),
        save=lambda value: _save_snapshot("index", value),
    )


def _prior_inputs_payload() -> dict:
    """Separate cache so candidate inputs never delay the existing dashboard."""
    from concurrent.futures import ThreadPoolExecutor
    from bond_priors import INDEX_JOBS
    previous = cache_runtime.peek("bonds:prior_inputs:v1") or {}
    prior = previous.get("series") or {}

    def fetch(job):
        key, (indicator, period) = job
        try:
            frame = _call_with_timeout(astock._akshare().bond_new_composite_index_cbond,
                                       indicator=indicator, period=period, timeout=25)
            points = sorted(({"date": str(r["date"])[:10], "v": float(r["value"])}
                             for _, r in frame.iterrows() if finite(float(r["value"]))), key=lambda p: p["date"])
            if not points:
                raise ValueError("来源没有有效观测")
            return key, {"points": points[-1600:], "date": points[-1]["date"],
                         "source": f"中债新综合指数/{period}/{indicator}（AKShare）",
                         "fetched_at": datetime.now(BEIJING).isoformat(), "error": None}
        except Exception as exc:
            # Retain real prior dates on partial failure, never stamp old points
            # as a successful new fetch. Only dependent models pause.
            return key, {**prior.get(key, {}), "error": str(exc)}

    with ThreadPoolExecutor(max_workers=5) as pool:
        result = dict(pool.map(fetch, INDEX_JOBS.items()))
    return _stamp({"series": result, "date": max((v.get("date", "") for v in result.values()), default=""),
                   "source": "中债新综合指数期限分段与指数属性"})


def get_priors(force: bool = False) -> dict:
    import bond_priors
    curve = get_curve(force=force) or {}
    old_inputs = cache_runtime.peek("bonds:prior_inputs:v1") or {}
    retry_partial = any(v.get("error") for v in (old_inputs.get("series") or {}).values())
    auxiliary = cache_runtime.get("bonds:prior_inputs:v1", _prior_inputs_payload,
        ttl=300 if retry_partial else 6*3600,
        valid=lambda v: any(b.get("points") for b in (v.get("series") or {}).values()), force=force,
        warm=lambda: _load_snapshot("prior_inputs"), warm_time=lambda: _snapshot_time("prior_inputs"),
        save=lambda v: _save_snapshot("prior_inputs", v)) or {}
    result = bond_priors.forecast(curve, auxiliary)
    result["cache_state"] = "refreshing" if any(v.get("cache_state") == "refreshing" for v in (curve, auxiliary)) else "fresh"
    if result.get("input_fingerprint"):
        try:
            folder = os.path.join(DATA_DIR, "bonds_priors")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{result['generated_at'][:10]}-{result['input_fingerprint']}.json")
            if not os.path.exists(path):
                temp = path + f".{threading.get_ident()}.tmp"
                try:
                    with open(temp, "w", encoding="utf-8") as f:
                        json.dump({"forecast": result, "curve": curve, "index_sources": auxiliary}, f,
                                  ensure_ascii=False, allow_nan=False)
                    os.link(temp, path)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
        except (OSError, ValueError):
            pass
    return result


# ——— 全球对照（中美利差）———

def _global_payload() -> dict:
    """中美国债收益率对照与 10Y 利差，近 250 日。"""
    ak = astock._akshare()
    df = _call_with_timeout(ak.bond_zh_us_rate)
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
        if not (finite(cn) and finite(us)):
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
    return cache_runtime.get(
        "bonds:global", lambda: _stamp(_global_payload()), valid=bool, ttl=6 * 3600, force=force,
        warm=lambda: _load_snapshot("global"), warm_time=lambda: _snapshot_time("global"),
        save=lambda value: _save_snapshot("global", value),
    )


# ——— 页面聚合 ———

def _slim_curve(curve: dict) -> dict:
    """overview 用的曲线瘦身版：整条当期曲线 + 各序列只留近 120 点。

    完整 3 年序列只服务框架回算（get_framework 内部直取 get_curve），
    不随 overview 下发（裸 payload 会到 600KB+）。
    """
    if not curve:
        return {}
    return {
        **_source_meta(curve),
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
        **_source_meta(funding),
        "date": funding.get("date"),
        "series": {k: v[-120:] for k, v in (funding.get("series") or {}).items()},
        "source": funding.get("source"),
    }


def _slim_positioning(pos: dict) -> dict:
    if not pos:
        return {}
    return {
        **_source_meta(pos),
        "date": pos.get("date"),
        "contracts": [
            {**{k: v for k, v in c.items() if k != "oi_hist"},
             "oi_hist": (c.get("oi_hist") or [])[-120:]}
            for c in (pos.get("contracts") or [])
        ],
        "source": pos.get("source"),
        "method": pos.get("method"),
    }


def _overview_payload(force: bool = False) -> dict:
    """债市页一次性聚合：曲线 / 资金 / 政策锚 / 指数 / 全球对照 / 计算层 / 量仓。

    每个子块独立失败降级（空 dict），不互相阻塞；序列裁到近 120 点控体积，
    完整 3 年序列由 /bonds/framework 与 /bonds/segments 端点按需提供。
    """
    import concurrent.futures
    getters = (get_curve, get_shibor, get_policy, get_index, get_global, get_positioning)
    def pull(getter):
        try:
            return getter(force=force) or {}
        except Exception as exc:
            return {"cache_state": "error", "refresh_error": str(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        curve, shibor, policy, index, globe, positioning = pool.map(pull, getters)
    calc = _calc_payload(curve, shibor)
    blocks = (curve, shibor, policy, index, globe, calc, positioning)
    states = [block.get("cache_state") for block in blocks if isinstance(block, dict)]
    cache_state = next((state for state in ("refreshing", "error", "stale") if state in states), "fresh")
    errors = list(dict.fromkeys(
        str(block.get("refresh_error")) for block in blocks
        if isinstance(block, dict) and block.get("refresh_error")
    ))
    cached_at = [block.get("cached_at") for block in blocks if isinstance(block, dict) and block.get("cached_at")]
    return {
        "_inputs": {"curve": curve, "funding": shibor, "policy": policy, "index": index, "global": globe, "positioning": positioning},
        "curve": _slim_curve(curve),
        "funding": _slim_funding(shibor),
        "policy": policy,
        "index": {**_source_meta(index), "date": index.get("date"), "series": (index.get("series") or [])[-120:],
                  "source": index.get("source")} if index else {},
        "global": {**_source_meta(globe), "date": globe.get("date"), "series": (globe.get("series") or [])[-120:],
                   "spread": (globe.get("spread") or [])[-120:], "source": globe.get("source")} if globe else {},
        "calc": calc,
        "positioning": _slim_positioning(positioning),
        "cache_state": cache_state,
        "cached_at": min(cached_at) if cached_at else None,
        "refresh_error": "；".join(errors) or None,
    }


def get_overview(force: bool = False) -> dict:
    return get_dashboard(force)


# ——— 宏观长序列补齐（供框架 3 年回算；与 market.get_macro 的短序列独立）———
# 数据缺口：market 层部分指标（社融存量同比、DR007-OMO 利差）只有当期几个月到 140 日，
# 不够 3 年回算。这里用可回溯的公开源重建：
# - 社融存量同比：官方存量表（当期 7 个月）作锚 + shrzgm 月增量（2015 起）回推存量，算同比；
# - 资金利率-政策锚利差：东财银行间拆借隔夜（2004 起）× OMO 7D 利率时间表。

_OMO_SCHEDULE = [  # 7 天逆回购操作利率（生效日 → 利率%）
    # Historical reference only; current validity is not automatically verified.
    # Its derived spread is displayed as an unverified proxy, not a scored policy anchor.
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
    """社融存量同比（3 年目标）：三层来源取最优。

    1. mofcom shrzgm 136 月增量 + 官方存量锚回推（主路径，mofcom 常挂）；
    2. 本地累积快照（官方每月 7 个月窗口滚动留存，levels/growth 逐月变长）；
    3. 两者合并取最长。官方公布月优先用官方同比（回推两端误差 ~1pp）。
    """
    try:
        import macro_fetch
        official = macro_fetch.social_financing_stock() or {}
    except Exception:  # noqa: BLE001
        official = {}
    if not official.get("stock_level_hist"):
        return {}
    # 累积快照先滚动并入官方最新窗口（即使 mofcom 挂也能攒数据）
    accum = _merge_accum(official)

    flows: dict[str, float] = {}
    try:
        df = astock._akshare().macro_china_shrzgm()
    except Exception:  # noqa: BLE001
        df = None
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            m = str(r["月份"])
            # '202604' → '2026-04'（补零，与官方存量表日期对齐）
            d = f"{m[:4]}-{m[4:6].zfill(2)}" if len(m) == 6 else m
            try:
                flows[d] = float(r["社会融资规模增量"])
            except (TypeError, ValueError):
                continue

    anchor_date = max(str(p["date"]) for p in official["stock_level_hist"])

    # 路径 A：mofcom 增量回推存量
    levels: dict[str, float] = {str(p["date"]): p["v"] for p in official["stock_level_hist"]}
    if flows:
        months = sorted({*flows.keys(), *levels.keys()})
        months = [m for m in months if m <= anchor_date]
        for i in range(len(months) - 2, -1, -1):
            m = months[i]
            if m in levels:
                continue
            nxt = months[i + 1]
            base = levels.get(nxt)
            if base is None or nxt not in flows:
                continue
            levels[m] = round(base - flows[nxt] / 10000.0, 4)  # 增量亿元 → 万亿元

    # 路径 B：累积快照的 levels（mofcom 挂时的存量来源）——与 A 合并取并集
    accum_levels = {str(k): v for k, v in (accum.get("levels") or {}).items()
                    if isinstance(v, (int, float)) and str(k) <= anchor_date}
    merged_levels = dict(accum_levels)
    merged_levels.update({k: v for k, v in levels.items() if v})
    # 官方月覆盖累积快照（官方即最新修正）
    for p in official["stock_level_hist"]:
        merged_levels[str(p["date"])] = p["v"]

    # 同比：官方 > 累积 growth > 由合并存量计算（存量非正视为脏数据跳过）
    official_growth = {str(p["date"]): p["v"] for p in official.get("hist") or []}
    accum_growth = {str(k): v for k, v in (accum.get("growth") or {}).items()
                    if isinstance(v, (int, float))}
    hist = []
    for m in sorted(merged_levels):
        if merged_levels[m] <= 0:
            continue
        year_ago = f"{int(m[:4]) - 1}-{m[5:]}"
        if year_ago not in merged_levels or merged_levels[year_ago] <= 0:
            continue
        if m in official_growth:
            v = official_growth[m]
        elif m in accum_growth:
            v = accum_growth[m]
        else:
            v = round((merged_levels[m] / merged_levels[year_ago] - 1) * 100, 2)
        hist.append({"date": m, "v": v})
    hist = hist[-36:]
    if len(hist) < 8:  # 分位下限同口径；序列会随累积逐月变长
        return {}
    # Official-overwritten values cannot independently validate reconstruction.
    src = "人民银行存量表" + (" + mofcom 增量回推" if flows else " + 本地累积快照")
    return {
        "date": hist[-1]["date"],
        "hist": hist,
        "value": hist[-1]["v"],
        "validated": None, "quality": "derived_unvalidated",
        "official_hist": [{"date": k, "v": v} for k, v in sorted(official_growth.items())],
        "source": src,
    }


def get_afre_stock(force: bool = False) -> dict:
    """社融存量同比（3 年目标）：官方增量表 + mofcom 长增量 双源 + 本地累积快照。

    三层降级（finflux 项目经验：mofcom 需 legacy TLS 且常挂）：
    1. mofcom shrzgm 136 月增量（挂时自动跳过）回推存量同比；
    2. 官方月度快照累积文件 bonds_afre_accum.json——每月抓到的官方存量/同比
       逐月并入（官方表只给最新 7 个月，滚动留存后序列自然变长）；
    3. 仅当期官方 7 个月（不够分位下限则 policy 状态如实少一个 part）。
    """
    return cache_runtime.get(
        "bonds:afre", lambda: _stamp(_afre_stock_payload()),
        valid=lambda v: bool(v.get("hist")),
        ttl=24 * 3600, force=force,
        warm=lambda: _load_snapshot("afre"), warm_time=lambda: _snapshot_time("afre"),
        save=lambda value: _save_snapshot("afre", value),
    )


def _merge_accum(official: dict) -> dict:
    """把官方最新 7 个月的存量/同比并入本地累积文件，返回累积后的完整序列。

    累积文件结构：{"levels": {date: 万亿}, "growth": {date: %}}。
    官方每月覆盖 7 个月窗口，滚动合并后日期单调去重（新值覆盖旧值）。
    """
    accum_path = _snapshot_path("afre_accum")
    accum: dict = {"levels": {}, "growth": {}}
    try:
        with open(accum_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict) and isinstance(loaded.get("levels"), dict) and isinstance(loaded.get("growth"), dict):
            accum = loaded
    except (OSError, ValueError):
        pass
    changed = False
    for pt in official.get("stock_level_hist") or []:
        d = str(pt.get("date"))
        if d and pt.get("v") is not None and accum["levels"].get(d) != pt["v"]:
            accum["levels"][d] = pt["v"]
            changed = True
    for pt in official.get("hist") or []:
        d = str(pt.get("date"))
        if d and pt.get("v") is not None and accum["growth"].get(d) != pt["v"]:
            accum["growth"][d] = pt["v"]
            changed = True
    if changed:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            temp = accum_path + ".tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(accum, fh, ensure_ascii=False)
            os.replace(temp, accum_path)  # 原子写：崩溃不留半文件
        except OSError:
            pass
    return accum


def _ib_overnight_payload() -> dict:
    """银行间隔夜利率 3 年序列（东财中国银行同业拆借市场·隔夜），减 OMO 得资金-政策锚利差。"""
    ak = astock._akshare()
    df = _call_with_timeout(ak.rate_interbank, market="中国银行同业拆借市场",
                            symbol="Shibor人民币", indicator="隔夜")
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
        if d < cutoff or not finite(v):
            continue
        omo = _omo_on(d)
        hist.append({"date": d, "v": round(v, 4), "spread_bp": round((v - omo) * 100, 1) if omo else None})
    hist = sorted({p["date"]: p for p in hist}.values(), key=lambda p: p["date"])
    if len(hist) < 100:
        return {}
    return {
        "date": hist[-1]["date"],
        "series": _tail(hist),
        "source": "东财·银行间同业拆借隔夜 × 人民银行 OMO 7D",
    }


def get_ib_overnight(force: bool = False) -> dict:
    return cache_runtime.get(
        "bonds:ib_on", lambda: _stamp(_ib_overnight_payload()),
        valid=lambda v: bool(v.get("series")), ttl=6 * 3600, force=force,
        warm=lambda: _load_snapshot("ib_on"), warm_time=lambda: _snapshot_time("ib_on"),
        save=lambda value: _save_snapshot("ib_on", value),
    )


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
    vals = [p["v"] for p in (hist or [])[-n:] if finite(p.get("v"))]
    if len(vals) < 8:
        return None
    cur = vals[-1]
    return _midrank(vals, cur)


def _score_from_pct(pct: float | None, direction: str) -> float | None:
    """分位 → [-2,+2] 状态分。direction=down 表示该指标越低越利好债券。"""
    if pct is None:
        return None
    centered = pct - 0.5  # [-0.5, +0.5]
    s = centered * 4.0
    return _clamp(s if direction == "up" else -s)


def _pct_at(hist: list, idx: int, n: int = 60) -> float | None:
    """hist[idx] 在其前 n 期窗口内的分位（0-1）。历史回算用：禁止用未来数据。"""
    vals = [p["v"] for p in (hist or [])[: idx + 1][-n:] if finite(p.get("v"))]
    if len(vals) < 8:
        return None
    cur = vals[-1]
    return _midrank(vals, cur)


def _score_at_vals(values: list[float], idx: int, direction: str, n: int = 60) -> float | None:
    """滚动分位映射（回算热路径：直接吃值序列，禁止用未来数据）。"""
    window = values[: idx + 1][-n:]
    if len(window) < 8:
        return None
    cur = window[-1]
    return _score_from_pct(_midrank(window, cur), direction)


def _available_date(point: dict) -> str | None:
    raw = point.get("release_at") or point.get("available_at")
    if raw:
        return str(raw)[:10]
    date = str(point.get("date") or "")
    # Month-end is ONLY a reconstruction convention, never a publication date.
    if len(date) == 7:
        import calendar
        try:
            year, month = map(int, date.split("-"))
            return f"{date}-{calendar.monthrange(year, month)[1]:02d}"
        except ValueError:
            return None
    return date[:10] if len(date) >= 10 else None


def _state_hist(parts: list[dict], max_points: int = 740, dates: list[str] | None = None) -> list[dict]:
    expanded = []
    for p in parts:
        h = sorted(((_available_date(pt), pt["v"]) for pt in p.get("hist") or []
                    if _available_date(pt) and finite(pt.get("v"))), key=lambda pair: pair[0])
        if h:
            expanded.append((p, [d for d, _ in h], [v for _, v in h]))
    axis = dates if dates is not None else sorted({d for _, ds, _ in expanded for d in ds})
    out = []
    for d in axis:
        num = den = 0.
        for p, ds, vs in expanded:
            idx = bisect_left(ds, d)
            if idx >= len(ds) or ds[idx] != d:
                idx -= 1
            if idx < 0:
                continue
            max_age = p.get("max_age_days", 150 if len(str((p.get("hist") or [{}])[0].get("date", ""))) == 7 else 10)
            if (datetime.fromisoformat(d) - datetime.fromisoformat(ds[idx])).days > max_age:
                continue
            score = _score_at_vals(vs, idx, p.get("_direction", "up"), int(p.get("_window") or 60))
            if score is not None:
                w = p.get("weight", 1.)
                num += score * w
                den += w
        if den:
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
        hist = [dict(p) for p in (ind.get("hist") or [])]
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": direction})

    add("pmi", "制造业 PMI（历史相对位置）", "down", 1.2)
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
        "meaning": "增长/通胀/信用相对历史位置；水平弱不等于边际走弱或低于预期",
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
        hist = [dict(p) for p in (ind.get("hist") or [])]
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": direction})

    # Financing demand has no universal bullish direction; observe it without a directional score.
    add("fiscal_revenue_expenditure", "广义财政支出同比（两本账；非财政收支差额）", "down", 0.8)
    # Supply is counted once in the dedicated supply module.
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "policy", "name": "政策与融资", "score": round(score, 2),
        "meaning": "财政支出与融资观察；融资量上升的债价方向需区分供给与需求",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _funding_state(cn: dict, ib: dict | None = None) -> dict:
    """Funding：资金价格相对政策锚的偏离（越便宜越利好债券）。

    - 隔夜-OMO 利差：本层 get_ib_overnight 重建的 3 年日频序列（东财拆借隔夜 × OMO 时间表）；
    - 存单-政策利率利差：market 宏观层 ncd_aaa_spread（短序列，仅当期分位参与）。
    """
    parts: list[dict] = []
    ib = (get_ib_overnight() or {}) if ib is None else ib
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
        ("dr007_policy_spread", "FDR007 - OMO 7D 利差（bp，上午定盘）", 0.6, "down"),
        ("ncd_aaa_spread", "AAA 存单1Y - OMO 7D（期限不同的负债成本代理）", 1.0, "down"),
    ):
        ind = cn.get(key)
        if not ind:
            continue
        pct = _pct(ind.get("hist"))
        s = _score_from_pct(pct, direction)
        if s is None:
            continue
        hist = [dict(p) for p in (ind.get("hist") or [])]
        parts.append({"key": key, "label": label, "pct": round(pct, 2), "score": round(s, 2),
                      "weight": weight, "hist": hist, "_direction": direction})
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "funding", "name": "资金面", "score": round(score, 2),
        "meaning": "资金价格与政策锚的相对位置；期限/主体不同的代理不能等同实际回购融资",
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
    hist = [dict(p) for p in (ind.get("hist") or [])]
    parts = [{"key": "special_bond_issuance", "label": "专项债发行（亿元）",
              "pct": round(pct, 2), "score": round(s, 2), "weight": 1.0, "hist": hist,
              "_direction": "down"}]
    return {
        "key": "supply_demand", "name": "政府债供给观察", "score": round(s, 2),
        "meaning": "仅观察新增专项债发行节奏；非全市场净供给，机构需求尚未接入",
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
    # 中债指数动量：债价趋势确认（滚动 20 日涨跌幅，60 日窗口分位——与其他 part 同口径）
    series = (index.get("series") or []) if index else []
    if len(series) >= 60:
        vals = [p["v"] for p in series]
        # 动量序列：滚动 20 日涨跌幅（可回算全长度）
        mom_hist = [{"date": series[i]["date"], "v": round((vals[i] / vals[i - 20] - 1) * 100, 3)}
                    for i in range(20, len(series))]
        pct = _pct(mom_hist)  # 与其他 part 统一用 60 期窗口
        s = _score_from_pct(pct, "up")
        if s is not None:
            parts.append({"key": "index_mom_20d", "label": "中债指数 20 日动量（%）",
                          "pct": round(pct, 2), "score": round(s, 2), "weight": 1.0,
                          "hist": mom_hist, "_direction": "up"})
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "curve_tp", "name": "曲线与动量观察", "score": round(score, 2),
        "meaning": "期限利差与财富指数动量；斜率不等于期限溢价，补偿与趋势需分别观察",
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
    ind = cn.get("credit_spread_aaa") if not credit.get("AAA-3年") else None
    if ind:
        pct = _pct(ind.get("hist"))
        s = _score_from_pct(pct, "up")
        if s is not None:
            hist = [dict(p) for p in (ind.get("hist") or [])]
            parts.append({"key": "credit_spread_aaa", "label": "宏观层 AAA 信用利差（bp）",
                          "pct": round(pct, 2), "score": round(s, 2), "weight": 1.0,
                          "hist": hist, "_direction": "up"})
    if not parts:
        return {}
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "credit", "name": "信用利差", "score": round(score, 2),
        "meaning": "AAA利差位置；高利差既可能是补偿，也可能反映信用或流动性风险",
        "hist": _state_hist(parts),
        "parts": parts,
    }


# ——— 仓位与拥挤度数据层（国债期货量仓，框架 §9.2 Crowding）———
# 中金所国债期货主力连续（TS/TF/T/TL）：持仓量 + 成交量 + 收盘价，日频。
# 持仓量分位 = 杠杆/拥挤代理；价量同向 + 高持仓 = 趋势拥挤。

_FUT_SYMBOLS = [("TS0", "TS 2年"), ("TF0", "TF 5年"), ("T0", "T 10年"), ("TL0", "TL 30年")]


def _positioning_payload() -> dict:
    """四品种国债期货主力：最新持仓/成交/收盘 + 各自近一年分位。

    四个品种并发拉取（每路带超时），单品种失败跳过。"""
    ak = astock._akshare()
    import concurrent.futures
    from datetime import date
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=1200)).strftime("%Y%m%d")

    def _pull(sym: str):
        try:
            return _call_with_timeout(ak.futures_main_sina, timeout=30.0,
                                      symbol=sym, start_date=start, end_date=end)
        except Exception:  # noqa: BLE001 - 单品种失败跳过
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_FUT_SYMBOLS)) as pool:
        pulled = dict(zip((sym for sym, _ in _FUT_SYMBOLS), pool.map(_pull, (s for s, _ in _FUT_SYMBOLS))))

    out = []
    for sym, label in _FUT_SYMBOLS:
        df = pulled.get(sym)
        if df is None or len(df) < 60 or "持仓量" not in df.columns:
            continue
        clean = []
        for _, row in df.iterrows():
            try:
                oi, vol, close = (float(row[k]) for k in ("持仓量", "成交量", "收盘价"))
            except (ValueError, TypeError):
                continue
            if all(finite(v) and v >= 0 for v in (oi, vol, close)):
                clean.append((str(row["日期"])[:10], oi, vol, close))
        clean = sorted({r[0]: r for r in clean}.values())
        if len(clean) < 60:
            continue
        dates, oi, vol, close = map(list, zip(*clean))
        def pct_rank(vals):
            return _midrank(vals[-250:], vals[-1])
        oi_hist = [{"date": d, "v": v} for d, v in zip(dates, oi)][-780:]
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
        "method": "主力连续量仓热度，近250期中位秩；未区分多空与套保，换月和市场扩容会影响比较，不能直接推断方向。",
    }


def get_positioning(force: bool = False) -> dict:
    return cache_runtime.get(
        "bonds:positioning", lambda: _stamp(_positioning_payload()), valid=bool, ttl=6 * 3600, force=force,
        warm=lambda: _load_snapshot("positioning"), warm_time=lambda: _snapshot_time("positioning"),
        save=lambda value: _save_snapshot("positioning", value),
    )


def _positioning_state(d: dict | None = None) -> dict:
    """Positioning：国债期货持仓分位（持仓越高=拥挤越高，利空债券）。

    当期分与历史回算统一用滚动 250 期（≈1 年交易日）持仓分位，
    与 _positioning_payload 的 oi_pct_1y 同口径，保证 hist 尾端 ≈ score。
    """
    d = (get_positioning() or {}) if d is None else d
    contracts = d.get("contracts") or []
    parts = []
    for c in contracts:
        if c.get("oi_pct_1y") is None:
            continue
        # 拥挤度分数：持仓分位越高越利空债券 → 反向
        s = _clamp((c["oi_pct_1y"] - 0.5) * 4.0)
        parts.append({"key": c["symbol"], "label": f"{c['label']}主力持仓分位（近 1 年）",
                      "pct": c["oi_pct_1y"], "score": round(s, 2), "weight": 1.0,
                      "hist": c.get("oi_hist") or [], "_direction": "up",
                      "_window": 250})
    if not parts:
        return {
            "key": "positioning", "name": "期货量仓热度", "score": None,
            "meaning": "期货量仓数据源暂不可用，本状态留空",
            "parts": [],
        }
    tw = sum(p["weight"] for p in parts)
    score = _clamp(sum(p["score"] * p["weight"] for p in parts) / tw)
    return {
        "key": "positioning", "name": "期货量仓热度", "score": round(score, 2),
        "meaning": "国债期货参与热度（高持仓不等于净多头或看空；主力换月与市场扩容会影响分位）",
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
        "key": "global", "name": "中美利率对照", "score": round(s, 2),
        "meaning": "中美10Y利差的相对位置；包含中国自身利率，不是外生冲击或汇率预测",
        "hist": _state_hist(parts),
        "parts": parts,
    }


def _framework_payload(base: dict | None = None, macro_data: dict | None = None, ib: dict | None = None) -> dict:
    base = base or {"curve": get_curve(), "index": get_index(), "global": get_global(), "positioning": get_positioning()}
    if macro_data is None:
        try:
            macro_data = market.get_macro() or {}
        except Exception:
            macro_data = {}
    cn = macro_data.get("cn") or {}
    states = [_macro_state(cn), _policy_state(cn), _funding_state(cn, ib), _supply_state(cn),
              _curve_tp_state(base.get("curve") or {}, base.get("index") or {}),
              _credit_state(base.get("curve") or {}, cn), _positioning_state(base.get("positioning") or {}),
              _global_state(base.get("global") or {})]
    module_names = [("macro", "宏观与通胀"), ("policy", "政策与融资"), ("funding", "资金面"),
                    ("supply_demand", "政府债供给观察"), ("curve_tp", "曲线与动量观察"),
                    ("credit", "AAA信用利差"), ("positioning", "期货量仓热度"), ("global", "中美利率对照")]
    enriched = []
    for raw, (key, name) in zip(states, module_names):
        state = raw or {"key": key, "name": name, "score": None, "parts": [], "meaning": "数据不足，未生成实验分数"}
        if key == "policy":
            for indicator_key in ("social_financing_stock",):
                ind = cn.get(indicator_key) or {}
                if ind:
                    state["parts"].append({"key": indicator_key, "label": "社融存量同比（仅观察，方向不预设）",
                                           "score": None, "pct": _pct(ind.get("hist")), "weight": 0,
                                           "hist": ind.get("hist") or []})
        for part in state.get("parts") or []:
            h = part.get("hist") or []
            indicator = cn.get(part["key"]) or {}
            meta = indicator.get("meta") or {}
            frequency = meta.get("frequency") or ("quarterly" if part["key"] == "gdp" else "monthly" if h and len(str(h[-1].get("date", ""))) == 7 else "daily")
            observation_date = h[-1].get("date") if h else indicator.get("date")
            status, calendar_basis = _status(observation_date, frequency)
            if meta.get("status") in ("fallback", "stale"):
                status = meta["status"]
            if indicator:
                owner = indicator
            elif part["key"] == "index_mom_20d":
                owner = base.get("index") or {}
            else:
                owner_key = {"curve_tp": "curve", "credit": "curve", "positioning": "positioning", "global": "global"}.get(key, "")
                owner = base.get(owner_key) or {}
            if status == "fresh" and (owner.get("refresh_error") or owner.get("cache_state") == "error"):
                status = "fallback"
            part.update({"value": h[-1].get("v") if h else indicator.get("value"),
                         "change": h[-1]["v"] - h[-2]["v"] if len(h) > 1 else None,
                         "date": h[-1].get("date") if h else None,
                         "release_at": meta.get("release_at"), "frequency": frequency,
                         "sample_size": min(len(h), part.get("_window", 60)),
                         "window": part.get("_window", 60), "window_start": h[-min(len(h), part.get("_window", 60))].get("date") if h else None,
                         "source": indicator.get("source") or _part_source(key),
                         "source_url": meta.get("source_url") or indicator.get("source_url") or _part_url(key),
                         "quality": meta.get("quality") or ("proxy" if key in ("funding", "supply_demand", "positioning", "global") else "derived"),
                         "status": status, "calendar_basis": calendar_basis,
                         "history_kind": "retrospective_reconstruction"})
            if part["key"] == "ib_overnight_omo":
                part["quality"] = "proxy_unverified_policy"
                part["source"] = "隔夜拆借 × 手工OMO参考表（最后维护2025-05-08，当前锚未自动核验）"
                part["status"] = "unverified"
            part["hist"] = h[-60:]
            part.pop("_direction", None)
            part.pop("_window", None)
        usable = [p for p in state["parts"] if p.get("status") == "fresh" and finite(p.get("score")) and p.get("weight", 0) > 0]
        denominator = sum(p.get("weight", 0) for p in usable)
        state["score"] = round(sum(p["score"] * p["weight"] for p in usable) / denominator, 2) if denominator else None
        state["updated_parts"] = len(usable)
        state["missing_parts"] = [p["label"] for p in state["parts"] if p.get("status") != "fresh"]
        # Historical charts are rebuilt after frequency-aware as-of alignment,
        # but unknown publication vintages remain explicitly retrospective.
        state["history_kind"] = "retrospective_reconstruction"
        state["experimental"] = True
        enriched.append(state)
    return {"date": (base.get("curve") or {}).get("date"), "states": enriched,
            "coverage": round(sum(s.get("score") is not None for s in enriched) / 8 * 100),
            "notes": ["实验分数未经样本外校准，不是方向、概率或收益预测。", "历史为事后重建，月度发布时点未知；不得用作无前视回测。", "政府债供给仅含专项债代理，机构需求尚未接入。"],
            "method": "各频率独立标明窗口；并列中位秩映射[-2,2]。方向及权重仅为研究假设，估值/热度/基本面不能等同债价方向。"}


def get_framework(force: bool = False) -> dict:
    return get_dashboard(force).get("framework") or {}


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
    "融资套息观察（未加杠杆）": {"macro": 0.2, "policy": 0.5, "funding": 2.0, "supply_demand": 0.3,
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
    "长债(5-10Y)": "宏观预期反转（PMI/社融连续2月改善（预期差数据未接入））或期限溢价快速抬升",
    "超长债(20Y+)": "保险配置力量减弱、超长供给放量，或 TL价量与基差同时恶化（OI不单独识别方向）",
    "信用债(AAA)": "信用利差压缩至历史低位后遇流动性冲击（赎回-抛售螺旋），或资金面收敛叠加杠杆去化",
    "融资套息观察（未加杠杆）": "资金价格波动加大（隔夜利率脉冲）或同日可执行融资后持有收益转负；回购成本未接入时仅作情景",
}


def _segments_payload(fw: dict | None = None, calc: dict | None = None) -> dict:
    """分品种评分：状态驱动分 + carry/roll 静态锚 + 失效条件 + 逐期回算趋势。"""
    fw = (get_framework() or {}) if fw is None else fw
    states = {s["key"]: s for s in fw.get("states", []) if s.get("score") is not None}
    calc = (get_calc() or {}) if calc is None else calc
    rows_by_tenor = {r["tenor"]: r for r in calc.get("rows", [])}

    # 各状态历史得分的公共日期轴（取全状态日期并集，用于品种分回算；近 3 年）
    all_dates: set[str] = set()
    for s in fw.get("states", []):
        for pt in s.get("hist") or []:
            if pt.get("date"):
                all_dates.add(str(pt["date"]))
    dates = sorted(all_dates)[-740:]

    historical_states = {s["key"]: s for s in fw.get("states", [])}
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
        # Missing states never imply full confidence.
        # 驱动分：[-2,+2]；再列出该品种贡献最大/最小的状态
        score = _clamp(contrib / tw) if tw else None  # uncalibrated prior only
        sorted_states = sorted(
            [(k, states[k]["score"] * w / tw) for k, w in weights.items() if k in states],
            key=lambda t: t[1], reverse=True,
        )
        drivers = [
            {"state": states[k]["name"], "contribution": round(v, 2),
             "weight": weights[k]}
            for k, v in sorted_states
        ]
        # Use a real daily axis and as-of lookup for monthly modules; never
        # overwrite a historical point with a differently composed current score.
        daily_dates = [d for d in dates if len(d) == 10]
        history_parts = [{"hist": historical_states.get(k, {}).get("hist") or [], "weight": w,
                          "_direction": "up", "_window": 1, "max_age_days": 150 if k in ("macro", "policy", "supply_demand") else 10}
                         for k, w in weights.items()]
        # State scores are already standardized: aggregate as-of scores directly.
        hist = _segment_hist(history_parts, daily_dates)
        row: dict = {
            "segment": seg,
            "score": round(score, 2) if score is not None and tw / sum(weights.values()) >= .6 else None,
            "coverage": round(tw / sum(weights.values()) * 100),
            "experimental": True, "drivers": drivers, "history_kind": "retrospective_reconstruction",
            "hist": hist,
            "invalidation": _SEGMENT_INVALIDATION[seg],
        }
        anchor = _SEGMENT_ANCHOR.get(seg)
        if anchor and anchor in rows_by_tenor:
            r = rows_by_tenor[anchor]
            row["anchor_tenor"] = anchor
            row["carry_roll_bp_3m"] = r["total_static_bp_3m"]
            row["breakeven_bp_3m"] = r["breakeven_bp_3m"]
        if seg == "融资套息观察（未加杠杆）":
            # 套息空间 = 1Y 国债 - Shibor O/N（正 = 有套息空间）
            y1 = rows_by_tenor.get("1年")
            cost = calc.get("funding_cost")
            if y1 and cost is not None:
                row["carry_roll_bp_3m"] = y1["total_static_bp_3m"]
                row["anchor_tenor"] = "1年-资金成本"
                row["breakeven_bp_3m"] = None
        rows.append(row)

    # 展示顺序：按评分排序的五个债券品种在前，杠杆套息固定殿后（它是策略组合不是单一品种）
    levered = [r for r in rows if r["segment"] == "融资套息观察（未加杠杆）"]
    rows = [r for r in rows if r["segment"] != "融资套息观察（未加杠杆）"]
    # Fixed maturity order; heuristic scores do not rank investment returns.
    rows.extend(levered)
    return {
        "date": fw.get("date"),
        "rows": rows,
        "method": (
            "品种分 = Σ(八状态分 × 品种特定权重) / Σ可用权重（覆盖至少60%）（框架 §11.2 多期限权重先验："
            "未校准假设；热度和利差位置不代表收益率方向）。"
            "carry/roll 为该品种锚定期限的 3 个月静态收益（计算层）。"
        ),
        "notes": [
            "权重先验来自研究框架 §11.2 表格（1-3 个月持有期口径），未经样本外校准，只供实验观察，不用于投资排序。",
            "评分是状态读数的品种映射，不是买卖建议；每个品种附失效条件（框架 §0.2 原则三）。",
        ],
    }


def get_segments(force: bool = False) -> dict:
    return get_dashboard(force).get("segments") or {}


def _fingerprint(value) -> str:
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items() if k not in ("cache_state", "cached_at", "data_as_of", "refresh_error", "ai_insight")}
        if isinstance(obj, list):
            return [clean(x) for x in obj]
        return obj
    return hashlib.sha256(json.dumps(clean(value), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]


def _curve_comparisons(curve: dict) -> list[dict]:
    yields = curve.get("yields") or {}
    maps = {k: {p["date"]: p["v"] for p in points} for k, points in yields.items() if points}
    dates = sorted(set.intersection(*(set(m) for m in maps.values()))) if maps else []
    return [{"label": label, "date": dates[-1 - lag],
             "points": [{"tenor": k, "years": TENOR_YEARS[k], "value": m[dates[-1 - lag]]} for k, m in maps.items()]}
            for lag, label in ((0, "当期"), (5, "5个观测交易日前"), (20, "20个观测交易日前")) if len(dates) > lag]


def _load_validation() -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "research", "output", "bonds_validation", "result.json")
    try:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
        # Per-origin predictions live in the research artifact, not every page response.
        return {k: v for k, v in report.items() if k != "predictions"}
    except (OSError, ValueError):
        return {"status": "not_run", "conclusion": "预测能力未验证；历史时点数据与成本输入待核验", "results": [], "limitations": []}


def _dashboard_payload(force: bool = False) -> dict:
    base = _overview_payload(force=force)
    deadline = time.time() + 45
    while force and base.get("cache_state") == "refreshing" and time.time() < deadline:
        time.sleep(1)
        base = _overview_payload(force=False)
    inputs = base.pop("_inputs")
    try:
        macro_data = market.get_macro() or {}
    except Exception:
        macro_data = {}
    ib = get_ib_overnight() or {}
    fingerprint = _fingerprint({"model_version": 2, "inputs": inputs, "macro": macro_data.get("cn"), "ib": ib, "source_health": {k: [v.get("cache_state"), v.get("refresh_error")] for k, v in inputs.items()}})
    sources = []
    for key, block in inputs.items():
        frequency = "monthly" if key == "policy" else "daily"
        status, basis = _status(block.get("date"), frequency)
        if not block.get("date"):
            status = "missing"
        elif not block.get("generated_at"):
            status = "time_unknown" if status == "fresh" else status
        if status == "fresh" and (block.get("refresh_error") or block.get("cache_state") == "error"):
            status = "fallback"
        sources.append({"key": key, "date": block.get("date"), "fetched_at": block.get("generated_at"),
                        "status": status, "calendar_basis": basis, "source": block.get("source"),
                        "source_url": _part_url("funding" if key == "funding" else "policy" if key == "policy" else "positioning" if key == "positioning" else "curve"),
                        "quality": "proxy" if key in ("funding", "positioning", "global") else "direct",
                        "error": block.get("refresh_error")})
    fw = _framework_payload(inputs, macro_data, ib)
    calc = _calc_payload(inputs["curve"], inputs["funding"])
    seg = _segments_payload(fw, calc)
    generated_at = datetime.now(BEIJING).isoformat()
    for block in (fw, calc, seg):
        block.update({"input_fingerprint": fingerprint, "generated_at": generated_at})
    base.update({"schema_version": 2, "generated_at": generated_at, "input_fingerprint": fingerprint,
                 "framework": fw, "calc": calc, "segments": seg, "sources": sources,
                 "updated_sources": sum(s["status"] == "fresh" for s in sources),
                 "total_sources": len(sources), "curve_comparisons": _curve_comparisons(inputs["curve"]),
                 "validation": _load_validation(), "prediction_status": "unvalidated",
                 "history_kind": "retrospective_reconstruction"})
    used_keys = {p["key"] for state in fw["states"] for p in state["parts"]}
    base["input_vintage"] = {"raw_sources": inputs, "macro": {k: v for k, v in (macro_data.get("cn") or {}).items() if k in used_keys},
                              "ib_overnight": ib, "model_version": 2}
    base["dependency_versions"] = {f"bonds:{'shibor' if k == 'funding' else k}": _fingerprint(v) for k, v in inputs.items()}
    base["dependency_versions"]["bonds:ib_on"] = _fingerprint(ib)
    base["dependency_versions"]["macro"] = _fingerprint(macro_data)
    base["dependency_health"] = {f"bonds:{'shibor' if k == 'funding' else k}": [v.get("cache_state"), v.get("refresh_error")]
                                 for k, v in inputs.items()}
    base["dependency_health"]["bonds:ib_on"] = [ib.get("cache_state"), ib.get("refresh_error")]
    base["dependency_health"]["macro"] = [macro_data.get("cache_state"), macro_data.get("refresh_error")]
    old = cache_runtime.peek("bonds:dashboard:v2") or {}
    if (old.get("ai_insight") or {}).get("input_fingerprint") == fingerprint:
        base["ai_insight"] = old["ai_insight"]
    return base


def get_dashboard(force: bool = False) -> dict:
    previous = cache_runtime.peek("bonds:dashboard:v2") or {}
    getters = {"bonds:curve": get_curve, "bonds:shibor": get_shibor, "bonds:policy": get_policy,
               "bonds:index": get_index, "bonds:global": get_global, "bonds:positioning": get_positioning,
               "bonds:ib_on": get_ib_overnight, "macro": market.get_macro}
    live = {}
    if previous:
        for key in previous.get("dependency_versions", {}):
            try:
                live[key] = getters[key]() or {}
            except Exception:
                live[key] = {"cache_state": "error", "refresh_error": "source unavailable"}
    changed = bool(previous and str(previous.get("generated_at", ""))[:10] != datetime.now(BEIJING).date().isoformat()) or any(_fingerprint(value) != previous["dependency_versions"].get(key) or
                  [value.get("cache_state"), value.get("refresh_error")] != previous.get("dependency_health", {}).get(key)
                  for key, value in live.items())
    value = cache_runtime.get("bonds:dashboard:v2", lambda: _dashboard_payload(force=force),
                             valid=lambda v: any((v.get(k) or {}).get(field) for k, field in
                                                 (("curve", "curve"), ("funding", "series"), ("policy", "anchors"),
                                                  ("index", "series"), ("global", "series"), ("positioning", "contracts"))),
                             ttl=3600, force=force or changed,
                             warm=lambda: _load_snapshot("dashboard"), warm_time=lambda: _snapshot_time("dashboard"),
                             save=lambda value: _save_snapshot("dashboard", value))
    # Root cache freshness is separate from a child-source refresh. Keeping the
    # polling state visible lets the frontend follow through the next generation.
    if value.get("cache_state") == "fresh" and any(s.get("cache_state") == "refreshing" for s in (value.get(k) or {} for k in ("curve", "funding", "positioning"))):
        value["cache_state"] = "refreshing"
    # Large immutable inputs are persisted for research, not sent on every page request.
    return {k: v for k, v in value.items() if k not in ("input_vintage", "dependency_versions", "dependency_health")}


# ---------------------------------------------------------------------------
# AI 解读：分品种优先顺序 + 各品种简要解析（复用择时配置的 LLM 发现；写回快照）
# ---------------------------------------------------------------------------

async def _build_insight(payload: dict | None = None) -> dict | str:
    from pulse.pulse_insight import chat_json
    payload = get_dashboard() if payload is None else payload
    rows = payload.get("segments", {}).get("rows") or []
    if not rows:
        return ""
    context = {"snapshot_id": payload.get("input_fingerprint"), "generated_at": payload.get("generated_at"),
               "sources": payload.get("sources"), "segments": rows,
               "states": [{"name": s["name"], "meaning": s["meaning"],
                           "parts": [{k: v for k, v in p.items() if k != "hist"} for p in s["parts"]]}
                          for s in payload.get("framework", {}).get("states", [])]}
    prompt = ("为债市研究工作台写数据解读。只解释给定快照里的原值、变化、静态收益、风险与缺口。"
              "实验分数未经验证，禁止根据分数给出偏多/偏空或品种投资优先顺序。"
              "持仓量没有净多空方向，期限利差不是期限溢价；Shibor是无担保融资代理。"
              "数据过期/日期不齐必须点明；没有汇率、预期值或机构持仓数据，不得补写相关结论。"
              "ranking字段写一段事实摘要（不作排序），各品种只解释收益拆解、久期风险与缺失，"
              "每段不超过100字，避免复述实验分数。返回JSON：{\"ranking\":\"摘要\",\"segments\":{\"品种全名\":\"解读\"}}。\n"
              + json.dumps(context, ensure_ascii=False))
    parsed = await chat_json(prompt)
    names = {r["segment"] for r in rows}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("ranking"), str):
        return ""
    texts = parsed.get("segments")
    if not isinstance(texts, dict) or not all(isinstance(texts.get(n), str) and texts[n].strip() for n in names):
        return ""
    return {"ranking": parsed["ranking"].strip()[:180],
            "segments": {n: texts[n].strip()[:180] for n in names},
            "input_fingerprint": payload.get("input_fingerprint"), "generated_at": datetime.now(BEIJING).isoformat()}


def _valid_insight(value) -> bool:
    return isinstance(value, dict) and bool(value.get("ranking")) and bool(value.get("segments"))


async def get_ai_insight(force: bool = False) -> dict | str:
    payload = get_dashboard()
    fingerprint = payload.get("input_fingerprint")
    old = payload.get("ai_insight") or {}
    if not force and _valid_insight(old) and old.get("input_fingerprint") == fingerprint:
        return old
    fresh = await _build_insight(payload)
    if not fresh:
        return old if old.get("input_fingerprint") == fingerprint else ""
    def merge(current):
        if isinstance(current, dict) and current.get("input_fingerprint") == fingerprint:
            current["ai_insight"] = fresh
            _save_snapshot("dashboard", current)
        return current
    cache_runtime.update_value("bonds:dashboard:v2", merge)
    return fresh
