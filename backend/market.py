"""市场总览数据层 —— 市场情绪 + 板块资金流（板块/大盘级公开数据，不涉个股推荐）。

省流量：全站共享一份缓存（TTL 默认 5 分钟），多个用户/多次打开只抓一次；
盘中 5 分钟刷新足够，非交易时段数据本就不变。数据源全免费、无 key。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

import astock
import gstock

BEIJING = timezone(timedelta(hours=8))
_CACHE: dict = {}
_TTL = 300  # 5 分钟；全站共享，省数据源压力


def _cached(key: str, fn, valid=bool):
    """TTL 缓存。数据源故障的空结果不缓存（valid 判否），下次请求直接重试。"""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    val = fn()
    if valid(val):
        _CACHE[key] = (now, val)
    return val


def _num(v) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _sentiment() -> dict:
    """市场情绪：涨跌家数/涨停跌停/活跃度 + 大盘宽度、题材投机（客观数据机械分档）。"""
    try:
        # akshare 惰性导入（同 astock 模式）：未装时降级返回空，不挡整个服务启动
        df = astock._akshare().stock_market_activity_legu()
        d = {row["item"]: row["value"] for _, row in df.iterrows()}
    except Exception:
        return {}
    up, down, flat = _num(d.get("上涨")), _num(d.get("下跌")), _num(d.get("平盘"))
    zt, zt_real = _num(d.get("涨停")), _num(d.get("真实涨停"))
    dt, dt_real = _num(d.get("跌停")), _num(d.get("真实跌停"))
    r = up / max(down, 1)
    if up < 600:
        breadth = "冰点"
    elif r < 0.7:
        breadth = "偏弱"
    elif r < 1.2:
        breadth = "中性"
    elif r < 2.5:
        breadth = "偏强"
    else:
        breadth = "普涨"
    speculation = "亢奋" if zt_real >= 100 else "活跃" if zt_real >= 60 else "普通" if zt_real >= 30 else "冰点"
    return {
        "up": up, "down": down, "flat": flat,
        "zt": zt, "zt_real": zt_real, "dt": dt, "dt_real": dt_real,
        "active": str(d.get("活跃度", "")),
        "breadth": breadth, "speculation": speculation,
        "date": str(d.get("统计日期", "")),
    }


def _sectors() -> list[dict]:
    """行业资金流（按净额降序）。不含领涨股等个股字段。"""
    try:
        f = astock._akshare().stock_fund_flow_industry(symbol="即时")
        f = f.sort_values("净额", ascending=False)
    except Exception:
        return []
    out = []
    for _, row in f.iterrows():
        out.append({
            "name": str(row["行业"]),
            "pct": round(float(row.get("行业-涨跌幅", 0) or 0), 2),
            "net": round(float(row.get("净额", 0) or 0), 2),
            "inflow": round(float(row.get("流入资金", 0) or 0), 2),
            "outflow": round(float(row.get("流出资金", 0) or 0), 2),
            "firms": _num(row.get("公司家数")),
        })
    return out


def get_overview() -> dict:
    """市场情绪 + 板块资金（含缓存）。资金轮动由前端从 sectors 头尾取。"""
    def build():
        return {
            "sentiment": _sentiment(),
            "sectors": _sectors(),
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
    return _cached("overview", build, valid=lambda v: bool(v.get("sentiment") or v.get("sectors")))


def _emotion() -> dict:
    """短线情绪（聚合口径，**零个股名**）：连板梯队 / 最高连板 / 炸板率 / 封板率 / 晋级率 / 涨跌停家数。

    数据源＝东财涨停板四池（push2ex）。只把池子聚合成计数与比率，
    **不输出任何个股 code/name**——守产品「零标的」红线（个股清单是甩名单，不做）。
    """
    # 定位最近交易日：从今天往前回溯，第一日有涨停池即取（非交易日/盘前返空则继续回溯）。
    today = datetime.now(BEIJING).date()
    resolved, zt = "", []
    for back in range(8):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        zt = astock.em_zt_topic_pool("getTopicZTPool", d, "fbt:asc")
        if zt:
            resolved = d
            break
    if not resolved:
        return {}

    zb = astock.em_zt_topic_pool("getTopicZBPool", resolved, "fbt:asc")    # 炸板池
    dt = astock.em_zt_topic_pool("getTopicDTPool", resolved, "fund:asc")   # 跌停池
    yzt = astock.em_zt_topic_pool("getYesterdayZTPool", resolved, "zs:desc")  # 昨涨停池

    boards = [_num(p.get("lbc")) or 1 for p in zt]      # 每只连板数（缺省按 1 板）
    lianban = [b for b in boards if b >= 2]             # 2 板及以上（连板）
    # 连板梯队：2/3/4/5+ 各多少家（5 代表 5 板及以上），只保留有家数的档
    tiers = Counter(min(b, 5) for b in lianban)
    ladder = [{"boards": b, "count": tiers[b], "plus": b >= 5} for b in sorted(tiers)]

    # 连板股清单（2 板+，客观公开榜单数据；按连板数、成交额降序）。
    # 产品定位调整（2026-07-05）：从「零标的」→「展示客观榜单但不推荐/不预测/不评分」。
    lianban_stocks = sorted(
        ({
            "code": str(p.get("c", "")), "name": p.get("n", ""),
            "boards": _num(p.get("lbc")) or 1,
            "price": round((astock._numf(p.get("p")) or 0) / 1000, 2),
            "pct": round(astock._numf(p.get("zdp")) or 0, 2),
            "amount": astock._numf(p.get("amount")),      # 成交额,元（'-' 占位归一为 None，防排序对 str 取负崩溃）
            "float_cap": astock._numf(p.get("ltsz")),     # 流通市值,元
            "industry": p.get("hybk", ""),  # 概念/行业
        } for p in zt if (_num(p.get("lbc")) or 1) >= 2),
        key=lambda x: (-x["boards"], -(x["amount"] or 0)),
    )

    zt_count, zb_count, yzt_count = len(zt), len(zb), len(yzt)
    attempts = zt_count + zb_count                       # 尝试涨停 = 封住 + 炸板
    seal_rate = round(zt_count / attempts, 3) if attempts else None      # 封板率
    break_rate = round(zb_count / attempts, 3) if attempts else None     # 炸板率
    # 晋级率＝今日 2 板+（＝昨涨停今又停）÷ 昨日涨停家数
    promotion_rate = round(len(lianban) / yzt_count, 3) if yzt_count else None

    return {
        "date": f"{resolved[:4]}-{resolved[4:6]}-{resolved[6:]}",
        "zt_count": zt_count,
        "dt_count": len(dt),
        "zb_count": zb_count,
        "max_boards": max(boards) if boards else 0,
        "lianban_count": len(lianban),
        "ladder": ladder,
        "lianban_stocks": lianban_stocks,
        "seal_rate": seal_rate,
        "break_rate": break_rate,
        "promotion_rate": promotion_rate,
        "yzt_count": yzt_count,
    }


def get_short_term_emotion() -> dict:
    """短线情绪（含缓存，5 分钟）。"""
    return _cached("emotion", _emotion)


def get_turnover_top() -> dict:
    """全市场成交额榜 Top20（客观公开榜单，含缓存 5 分钟）。"""
    def build():
        return {
            "stocks": astock.market_turnover_rank(20),
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
    return _cached("turnover_top", build, valid=lambda v: bool(v.get("stocks")))


def get_global_indices() -> list[dict]:
    """全球指数快照（美股 / 港股，含缓存 5 分钟）。空结果不缓存。"""
    return _cached("global_indices", gstock.global_indices, valid=bool)



# ---------------------------------------------------------------------------
# 资金供给 · 独立页面数据层（国内 + 国外美国，含历史趋势 + 美联储利率）
# ---------------------------------------------------------------------------

# 美债利率 / 美联储 / 利差 FRED 序列定义：key → (series_id, 单位, 中文标签)
# FRED CSV 用 curl 拉取（本环境 requests 直连/代理均超时，但 curl 稳定）。
_FRED_SERIES = {
    "effr":        ("EFFR",     "%",  "有效联邦基金利率 EFFR"),
    "dgs10":       ("DGS10",    "%",  "美债 10 年期收益率"),
    "dgs2":        ("DGS2",     "%",  "美债 2 年期收益率"),
    "dgs3m":       ("DGS3MO",   "%",  "美债 3 个月收益率"),
    "t10y3m":      ("T10Y3M",   "%",  "10Y − 3M 利差"),
    "t10y2y":      ("T10Y2Y",   "%",  "10Y − 2Y 利差"),
    "fed_target_u": ("DFEDTARU","%",  "美联储利率上限"),
    "fed_target_l": ("DFEDTARL","%",  "美联储利率下限"),
    "sofr":        ("SOFR",     "%",  "SOFR 担保隔夜融资利率"),
    "rrp":         ("RRPONTSYD","十亿$","隔夜逆回购 ON RRP"),   # FRED 原始单位=十亿美元
    "dgs30":       ("DGS30",    "%",  "美债 30 年期收益率"),
    "walcl":       ("WALCL",    "亿$",  "美联储总资产"),            # FRED 原始单位=百万美元
}


def _fred_csv(series_id: str, limit: int = 260) -> list[tuple[str, float]]:
    """拉 FRED CSV 历史序列（curl 子进程；返回最近 limit 个有效点）。

    FRED 在连续快速请求时偶发返回 HTML 页面而非 CSV——用正则白名单过滤，
    非 CSV 响应直接返回空，不抛异常。
    """
    import csv as _csv
    import io as _io
    import re as _re
    import subprocess as _sp
    import time as _time
    try:
        r = _sp.run(
            ["curl", "-L", "-s", "--max-time", "15",
             f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return []
    if r.returncode != 0 or not r.stdout:
        return []
    # CSV 第一行应是 "observation_date,<SERIES>" 或日期格式；HTML 直接跳过
    if not _re.match(r'^observation_date,', r.stdout) and not _re.match(r'^\d{4}-\d{2}-\d{2}', r.stdout):
        return []
    data: list[tuple[str, float]] = []
    for row in _csv.reader(_io.StringIO(r.stdout)):
        if len(row) < 2:
            continue
        date_str, val_str = row[0].strip(), row[1].strip()
        if not _re.match(r'^\d{4}-\d{2}-\d{2}', date_str) or val_str in ("", "."):
            continue
        try:
            data.append((date_str, float(val_str)))
        except ValueError:
            continue
    _time.sleep(0.15)  # FRED 连续请求间留小间隔，降低被限流概率
    return data[-limit:] if len(data) > limit else data


def _fred_latest(series_id: str) -> tuple[str, float] | None:
    d = _fred_csv(series_id, 1)
    return d[-1] if d else None


def _cn_margin_full() -> dict:
    """国内杠杆资金：全市场两融（东财 RPTA_RZRQ_LSHJ 历史汇总，T+1 披露，取近 20 日趋势）。"""
    rows = astock.eastmoney_datacenter(
        "RPTA_RZRQ_LSHJ", page_size=25, sort_columns="dim_date", sort_types="-1")
    if not rows:
        return {}
    rows = list(reversed(rows))  # 升序，方便画趋势

    def f(row, key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    latest, prev = rows[-1], rows[-2] if len(rows) > 1 else {}
    rzye = f(latest, "RZYE")
    rzrqye = f(latest, "RZRQYE")
    # 历史序列（date, value）
    rzrqye_hist = [{"date": str(r.get("DIM_DATE", ""))[:10], "v": round(f(r, "RZRQYE") / 1e8, 0)} for r in rows]
    rzjme_hist = [{"date": str(r.get("DIM_DATE", ""))[:10], "v": round(f(r, "RZJME") / 1e8, 1)} for r in rows]
    return {
        "date": str(latest.get("DIM_DATE", ""))[:10],
        "rzye_yi": round(rzye / 1e8, 0),
        "rzye_chg_yi": round((rzye - f(prev, "RZYE")) / 1e8, 1) if prev else None,
        "rzrqye_yi": round(rzrqye / 1e8, 0),
        "rzrqye_chg_yi": round((rzrqye - f(prev, "RZRQYE")) / 1e8, 1) if prev else None,
        "rzjme_yi": round(f(latest, "RZJME") / 1e8, 1),
        "rzrqye_hist": rzrqye_hist,
        "rzjme_hist": rzjme_hist,
    }


def _cn_index_flows() -> dict:
    """国内主力资金：上证 / 深成 / 创业板 当日实时主力净流入（东财 push2delay，盘中实时）。"""
    indices = [("1.000001", "上证指数"), ("0.399001", "深证成指"), ("0.399006", "创业板指")]
    out = {}
    for secid, name in indices:
        try:
            r = astock.em_get(
                "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
                params={"secid": secid, "fields1": "f1,f2,f3,f7",
                        "fields2": "f51,f52,f53,f54,f55", "klt": "101", "lmt": "30"},
                headers={"User-Agent": astock.UA, "Referer": "https://quote.eastmoney.com/"},
                timeout=10).json()
        except Exception:
            continue
        klines = ((r.get("data") or {}).get("klines")) or []
        hist = []
        for line in klines:
            p = str(line).split(",")
            if len(p) < 2:
                continue
            try:
                hist.append({"date": p[0], "v": round(float(p[1]) / 1e8, 1)})
            except (TypeError, ValueError):
                continue
        if hist:
            out[secid] = {"name": name, "hist": hist, "latest": hist[-1]}
    return out


def _kalshi_fed_odds() -> dict:
    """Kalshi 最近一期 FOMC 利率市场：各档目标区间概率（美分报价 ≈ 概率）。"""
    import subprocess as _sp
    try:
        r = _sp.run(
            ["curl", "-L", "-s", "--max-time", "15",
             "https://api.elections.kalshi.com/trade-api/v2/events?limit=5&status=open&series_ticker=KXFED"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0 or not r.stdout:
            return {}
        import json as _json
        events = _json.loads(r.stdout).get("events", [])
        if not events:
            return {}
        # 按 strike_date 找最近一期
        events.sort(key=lambda e: e.get("strike_date", ""))
        nearest = events[0]
        event_ticker = nearest["event_ticker"]
        meeting_date = nearest.get("sub_title", "").replace("On ", "")
    except Exception:
        return {}

    try:
        r = _sp.run(
            ["curl", "-L", "-s", "--max-time", "15",
             f"https://api.elections.kalshi.com/trade-api/v2/markets?limit=30&status=open&series_ticker=KXFED&event_ticker={event_ticker}"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0 or not r.stdout:
            return {}
        import json as _json
        markets = _json.loads(r.stdout).get("markets", [])
    except Exception:
        return {}

    # 各档 strike 的 "upper bound > X%" 概率（美分报价 0..100 ≈ 概率%）
    strikes = []
    for m in sorted(markets, key=lambda m: m.get("floor_strike", 0), reverse=True):
        strike = m.get("floor_strike")
        price = m.get("last_price_dollars")
        if strike is not None and price is not None:
            strikes.append({"strike": float(strike), "prob": round(float(price) * 100, 1)})
    if not strikes:
        return {}

    # 反推各档区间概率：P(>X) 的差分
    # 按 strike 从高到低，相邻两档 P 之差即为落在该档的概率
    bands = []
    for i in range(len(strikes) - 1):
        higher = strikes[i]      # 更高利率档
        lower = strikes[i + 1]   # 更低利率档
        band_prob = round(lower["prob"] - higher["prob"], 1)
        bands.append({
            "label": f"> {higher['strike']:.2f}%",
            "prob": higher["prob"],
        })
    # 最常见的区间：找概率跃迁最大的那档
    # 即 P(>X) 从 ~99 骤降到 ~1 的那个 X
    mid_label = ""
    for i in range(len(strikes) - 1):
        if strikes[i + 1]["prob"] > 50 > strikes[i]["prob"]:
            mid_label = f"{strikes[i]['strike']:.2f}%"
            break

    return {
        "event": nearest.get("title", ""),
        "meeting": meeting_date,
        "strikes": strikes,
        "likely_upper": mid_label,
    }


def _zscore(series: list[float], current: float) -> float:
    """z-score → 0-100 分位（越高=越紧/越热）。"""
    if len(series) < 20:
        return 50.0
    mean = sum(series) / len(series)
    std = max((sum((v - mean) ** 2 for v in series) / len(series)) ** 0.5, 1e-9)
    z = (current - mean) / std
    # z-score → 0-100（正态 CDF 近似）
    pct = 50 + 50 * (1 if z > 0 else -1) * min(abs(z) / 3.0, 1.0) ** 0.7
    return round(max(0, min(100, pct)), 1)


def _pct_rank(series: list[float], current: float) -> float:
    """近 N 日分位（0-100，越高=越紧/越热）。"""
    if not series:
        return 50.0
    below = sum(1 for v in series if v <= current)
    return round(below / len(series) * 100, 1)


def _cn_short_liquidity_index() -> dict:
    """短期流动性指数：SHIBOR O/N + 1W + FR001 + FR007 等权，越高=资金越紧。"""
    try:
        ak = astock._akshare()
        df_shibor = ak.macro_china_shibor_all().tail(260)
        df_repo = ak.repo_rate_hist(start_date="20250801", end_date="20260730").tail(260)
    except Exception:
        return {}

    if df_shibor.empty or df_repo.empty:
        return {}

    # 最新值
    latest_shibor = df_shibor.iloc[-1]
    latest_repo = df_repo.iloc[-1]

    # 各子指标近 250 日分位
    on_pct = _pct_rank(df_shibor["O/N-定价"].dropna().tolist(), latest_shibor["O/N-定价"])
    w1_pct = _pct_rank(df_shibor["1W-定价"].dropna().tolist(), latest_shibor["1W-定价"])
    fr001_pct = _pct_rank(df_repo["FR001"].dropna().tolist(), latest_repo["FR001"])
    fr007_pct = _pct_rank(df_repo["FR007"].dropna().tolist(), latest_repo["FR007"])

    composite = round((on_pct + w1_pct + fr001_pct + fr007_pct) / 4, 1)

    # 历史序列：各子指标分位平均
    hist_map = {}
    for _, row in df_shibor.iterrows():
        d = str(row["日期"])
        if d not in hist_map:
            hist_map[d] = {}
        hist_map[d]["on"] = row.get("O/N-定价")
        hist_map[d]["w1"] = row.get("1W-定价")
    for _, row in df_repo.iterrows():
        d = str(row["date"])
        if d not in hist_map:
            hist_map[d] = {}
        hist_map[d]["fr001"] = row.get("FR001")
        hist_map[d]["fr007"] = row.get("FR007")

    # 对每天都有 4 个子指标的日期，计算当日分位（用全部历史数据）
    all_on = df_shibor["O/N-定价"].dropna().tolist()
    all_w1 = df_shibor["1W-定价"].dropna().tolist()
    all_fr001 = df_repo["FR001"].dropna().tolist()
    all_fr007 = df_repo["FR007"].dropna().tolist()

    hist = []
    for d in sorted(hist_map.keys())[-60:]:  # 近 60 个交易日
        vals = hist_map[d]
        if all(k in vals and vals[k] is not None for k in ("on", "w1", "fr001", "fr007")):
            pct = (_pct_rank(all_on, vals["on"]) + _pct_rank(all_w1, vals["w1"]) +
                   _pct_rank(all_fr001, vals["fr001"]) + _pct_rank(all_fr007, vals["fr007"])) / 4
            hist.append({"date": d, "v": round(pct, 1)})

    return {
        "value": composite,
        "label": "短期流动性",
        "desc": f"SHIBOR O/N {latest_shibor['O/N-定价']:.2f}% · 1W {latest_shibor['1W-定价']:.2f}% · FR001 {latest_repo['FR001']:.2f}% · FR007 {latest_repo['FR007']:.2f}%",
        "date": str(latest_shibor["日期"]),
        "hist": hist,
        "interpretation": "越高=资金面越紧（>70 偏紧，<30 宽松）",
    }


def _cn_policy_rate_index() -> dict:
    """政策利率指数：LPR 1Y + 5Y 近 3 年分位，越高=资金成本越高。"""
    try:
        ak = astock._akshare()
        df = ak.macro_china_lpr().tail(780)  # 近 3 年月度
    except Exception:
        return {}
    if df.empty:
        return {}

    latest = df.iloc[-1]
    lpr1y_pct = _pct_rank(df["LPR1Y"].dropna().tolist(), latest["LPR1Y"])
    lpr5y_pct = _pct_rank(df["LPR5Y"].dropna().tolist(), latest["LPR5Y"])
    composite = round((lpr1y_pct + lpr5y_pct) / 2, 1)

    hist = []
    for _, row in df.iterrows():
        d = str(row["TRADE_DATE"])
        l1, l5 = row.get("LPR1Y"), row.get("LPR5Y")
        if l1 is not None and l5 is not None:
            pct = (_pct_rank(df["LPR1Y"].dropna().tolist(), l1) +
                   _pct_rank(df["LPR5Y"].dropna().tolist(), l5)) / 2
            hist.append({"date": d, "v": round(pct, 1)})

    return {
        "value": composite,
        "label": "政策利率",
        "desc": f"LPR 1Y {latest['LPR1Y']:.2f}% · 5Y {latest['LPR5Y']:.2f}%",
        "date": str(latest["TRADE_DATE"]),
        "hist": hist[-24:],  # 近 2 年月度
        "interpretation": "越高=贷款成本越高（>70 偏紧，<30 宽松）",
    }


def _cn_bond_index() -> dict:
    """债市景气指数：中债国债总净价指数近 250 日分位，越高=债市越强。"""
    try:
        ak = astock._akshare()
        df = ak.bond_treasury_index_cbond().tail(260)
    except Exception:
        return {}
    if df.empty:
        return {}

    latest = df.iloc[-1]
    vals = df["value"].tolist()
    composite = _pct_rank(vals, latest["value"])

    hist = [{"date": str(r["date"]), "v": round(_pct_rank(vals, r["value"]), 1)} for _, r in df.iterrows()]

    # 近 20 日涨跌幅
    if len(vals) >= 21:
        chg_20d = round((vals[-1] - vals[-21]) / vals[-21] * 100, 2)
    else:
        chg_20d = None

    return {
        "value": composite,
        "label": "债市景气",
        "desc": f"中债国债总净价 {latest['value']:.2f}" + (f" · 近 20 日 {chg_20d:+.2f}%" if chg_20d is not None else ""),
        "date": str(latest["date"]),
        "hist": hist,
        "interpretation": "越高=债市走牛（股债跷跷板，>70 偏强）",
    }


def _cn_leverage_index(cn_margin: dict) -> dict:
    """杠杆情绪指数：两融余额分位 + 融资净买入近 5 日均值分位，越高=杠杆情绪越热。"""
    rzrqye_hist = cn_margin.get("rzrqye_hist", [])
    rzjme_hist = cn_margin.get("rzjme_hist", [])
    if not rzrqye_hist:
        return {}

    vals = [h["v"] for h in rzrqye_hist]
    balance_pct = _pct_rank(vals, vals[-1])

    # 融资净买入近 5 日均值
    if len(rzjme_hist) >= 5:
        recent5 = [h["v"] for h in rzjme_hist[-5:]]
        jme_mean = sum(recent5) / len(recent5)
        all_jme = [h["v"] for h in rzjme_hist]
        jme_pct = _pct_rank(all_jme, jme_mean)
    else:
        jme_pct = 50.0

    composite = round(balance_pct * 0.6 + jme_pct * 0.4, 1)

    # 历史合成序列
    hist = []
    all_jme = [h["v"] for h in rzjme_hist]
    for i, h in enumerate(rzrqye_hist[-25:], start=len(rzrqye_hist) - 25):
        b_pct = _pct_rank(vals, h["v"])
        if i >= 4 and i < len(rzjme_hist):
            recent = [x["v"] for x in rzjme_hist[max(0, i-4):i+1]]
            j_pct = _pct_rank(all_jme, sum(recent) / len(recent))
        else:
            j_pct = 50.0
        hist.append({"date": h["date"], "v": round(b_pct * 0.6 + j_pct * 0.4, 1)})

    return {
        "value": composite,
        "label": "杠杆情绪",
        "desc": f"两融 {vals[-1]:.0f} 亿（分位 {balance_pct:.0f}）· 近5日净买入均值 {jme_mean:.1f} 亿",
        "date": rzrqye_hist[-1]["date"],
        "hist": hist,
        "interpretation": "越高=杠杆资金越热（>70 亢奋，<30 冰点）",
    }


def _cn_momentum_index(cn_data: dict) -> dict:
    """主力动量指数：融资净买入近 5 日均值分位 + 当日主力净流入，越高=增量资金越积极。"""
    rzjme_hist = cn_data.get("rzjme_hist", [])
    if len(rzjme_hist) < 5:
        return {}

    flows = cn_data.get("index_flows", {})
    total = sum(f["latest"]["v"] for f in flows.values()) if flows else 0

    all_jme = [h["v"] for h in rzjme_hist]
    # 近 5 日均值
    recent5 = [h["v"] for h in rzjme_hist[-5:]]
    mean5 = sum(recent5) / len(recent5)
    composite = _pct_rank(all_jme, mean5)

    hist = []
    for i, h in enumerate(rzjme_hist):
        if i >= 4:
            window = [x["v"] for x in rzjme_hist[max(0, i-4):i+1]]
            m = sum(window) / len(window)
            hist.append({"date": h["date"], "v": round(_pct_rank(all_jme, m), 1)})

    return {
        "value": composite,
        "label": "主力动量",
        "desc": f"当日三指数主力净流入 {total:+.1f} 亿 · 融资净买入近5日均值 {mean5:+.1f} 亿",
        "date": rzjme_hist[-1]["date"],
        "hist": hist,
        "interpretation": "越高=增量资金越积极（>70 流入强，<30 大幅流出）",
    }


def get_liquidity() -> dict:
    """资金供给指标汇总（独立页面，含历史趋势 + 美联储利率；缓存 5 分钟）。"""
    def build():
        # --- 国内 ---
        cn = _cn_margin_full()
        flows = _cn_index_flows()
        if flows:
            cn["index_flows"] = flows
            # 全市场合计
            total = sum(f["latest"]["v"] for f in flows.values())
            cn["total_main_net_yi"] = round(total, 1)

        # --- 国外（美国）：FRED 序列 ---
        us: dict = {}
        for key, (sid, unit, label) in _FRED_SERIES.items():
            hist = _fred_csv(sid, 260)
            if not hist:
                continue
            latest_val = hist[-1][1]
            if unit == "亿$":
                latest_val = round(latest_val / 100, 1)
                hist = [(d, round(v / 100, 1)) for d, v in hist]
            prev_val = hist[-2][1] if len(hist) > 1 else None
            us[key] = {
                "label": label,
                "unit": unit,
                "value": round(latest_val, 3),
                "date": hist[-1][0],
                "chg": round(latest_val - prev_val, 3) if prev_val is not None else None,
                "hist": [{"date": d, "v": round(v, 3)} for d, v in hist],
            }

        # --- 国内综合指数 ---
        cn_indices = {}
        for key, fn in [("short_liquidity", _cn_short_liquidity_index),
                        ("policy_rate", _cn_policy_rate_index),
                        ("bond", _cn_bond_index)]:
            try:
                idx = fn()
                if idx:
                    cn_indices[key] = idx
            except Exception:
                continue
        # 依赖 cn 数据的指数
        lev = _cn_leverage_index(cn)
        if lev:
            cn_indices["leverage"] = lev
        mom = _cn_momentum_index(cn)
        if mom:
            cn_indices["momentum"] = mom

        # --- 美联储加息概率（Kalshi 市场） ---
        fed_odds = _kalshi_fed_odds()

        return {
            "cn": cn,
            "cn_indices": cn_indices,
            "us": us,
            "fed_odds": fed_odds,
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
    return _cached("liquidity", build, valid=lambda v: bool(v.get("cn") or v.get("us")))