"""基金数据层 —— 中国公募基金的搜索、实时估值、净值走势、指标与筛选。

数据来源：东财/天天基金（akshare + efinance），全部只读、无状态。
设计（吸收高星项目调研结论，docs/research/2026-08-05-基金模块开源项目调研.md）：

- 实时估值走 efinance.get_realtime_increase_rate（天天基金盘中估算，按基金代码
  精准查询；非交易时段返回「最新净值」）。估值是基于上季重仓股的推算值，
  与真实净值有偏差，接口里 estimate 与 nav 始终分开给（对标 x2rr/funds 的
  「估值 vs 净值」双线处理）。
- 名单搜索走 akshare.fund_name_em（全量 2.7w 只，含拼音），本地内存缓存 24h，
  支持代码/简称/拼音模糊匹配。
- 历史净值走 akshare.fund_open_fund_info_em（单位净值走势），指标（年化、
  最大回撤、波动率、夏普）由净值序列自算——不依赖第三方评分接口。
- 筛选走 akshare.fund_open_fund_rank_em（全量业绩排行，含近1周~成立来），
  支持类型过滤、4433 法则、业绩区间过滤与多列排序。
- 所有外部调用都过模块级 TTL 缓存：东财有 1s 限流，缓存也是防封手段。
"""

from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_CACHE: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()
BEIJING = timezone(timedelta(hours=8))


def _cached(key: str, ttl: float, fetch):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    data = fetch()
    with _LOCK:
        _CACHE[key] = (time.time(), data)
    return data


def _num(v) -> Optional[float]:
    """东财字段常见 '--'/None/字符串，统一转 float 或 None。"""
    try:
        if v is None:
            return None
        f = float(str(v).replace(",", "").replace("%", ""))
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 基金名单与搜索
# ---------------------------------------------------------------------------

def _name_index() -> list[dict]:
    """全量基金名单（代码/简称/拼音/类型），缓存 24h。"""
    import akshare as ak

    df = ak.fund_name_em()
    out = []
    for _, r in df.iterrows():
        code = str(r.get("基金代码", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        out.append({
            "code": code,
            "name": str(r.get("基金简称", "")).strip(),
            "type": str(r.get("基金类型", "")).strip(),
            "pinyin": str(r.get("拼音缩写", "")).strip(),
            "pinyin_full": str(r.get("拼音全称", "")).strip(),
        })
    return out


def search_funds(query: str, limit: int = 20) -> list[dict]:
    """模糊搜索：代码前缀 > 简称包含 > 拼音（缩写/全拼）。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    idx = _cached("fund_name_index", 24 * 3600, _name_index)
    exact, by_code, by_name, by_pinyin = [], [], [], []
    for f in idx:
        code, name = f["code"], f["name"].lower()
        if code == q:
            exact.append(f)
        elif code.startswith(q):
            by_code.append(f)
        elif q in name:
            by_name.append(f)
        elif q in f["pinyin"].lower() or q in f["pinyin_full"].lower():
            by_pinyin.append(f)
        if len(exact) + len(by_code) + len(by_name) + len(by_pinyin) > 400:
            break  # 遍历短路：拼音全匹配扫全表太贵，命中足够就停
    out = []
    seen = set()
    for group in (exact, by_code, by_name, by_pinyin):
        for f in group:
            if f["code"] in seen:
                continue
            seen.add(f["code"])
            out.append({"code": f["code"], "name": f["name"], "type": f["type"]})
            if len(out) >= limit:
                return out
    return out


def fund_meta(code: str) -> Optional[dict]:
    """按代码从名单取名称/类型（搜索缓存同一份，零额外请求）。"""
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    idx = _cached("fund_name_index", 24 * 3600, _name_index)
    for f in idx:
        if f["code"] == code:
            return {"code": code, "name": f["name"], "type": f["type"]}
    return None


# ---------------------------------------------------------------------------
# 实时估值 / 最新净值
# ---------------------------------------------------------------------------

def realtime_estimates(codes: list[str]) -> dict[str, dict]:
    """批量盘中估值 + 最新公布净值。缓存 60s（交易时段天天基金估值约分钟级更新）。

    返回 {code: {name, estimate_pct, estimate_time, nav, nav_date}}。
    非交易时段 / 估算未出时 estimate_* 为 None，nav 为最近公布净值。
    """
    codes = list(dict.fromkeys(c.strip() for c in codes if re.fullmatch(r"\d{6}", (c or "").strip())))
    if not codes:
        return {}
    key = "rt_est_" + ",".join(sorted(set(codes)))

    def _fetch():
        # 主源：efinance 天天基金盘中估值。2026 年起东财已下线对外盘中估值
        # （fundgz / FundMNFInfo / FundGuZhi 全时段返回空），主源失效时回退到
        # 自研估算：上季十大重仓 × 实时股价加权推算（下方 _self_estimate）。
        base: dict[str, dict] = {}
        try:
            import efinance as ef

            df = ef.fund.get_realtime_increase_rate(fund_codes=codes)
            for _, r in df.iterrows():
                c = str(r.get("基金代码", "")).strip()
                base[c] = {
                    "name": str(r.get("基金名称", "")).strip(),
                    "estimate_pct": _num(r.get("估算涨跌幅")),
                    "estimate_time": str(r.get("估算时间") or "") or None,
                    "nav": _num(r.get("最新净值")),
                    "nav_date": str(r.get("最新净值公开日期") or "") or None,
                }
        except Exception:
            base = {}
        # efinance 失效（估值全 None）时逐只自研估算补齐
        if not any(v.get("estimate_pct") is not None for v in base.values()):
            est = _self_estimate(codes)
            for c, e in est.items():
                if c in base:
                    base[c]["estimate_pct"] = e["estimate_pct"]
                    base[c]["estimate_time"] = e["estimate_time"]
                    if e.get("source"):
                        base[c]["estimate_source"] = e["source"]
                    if e.get("estimate_stale"):
                        base[c]["estimate_stale"] = True
                    if e.get("proxy"):
                        base[c]["estimate_proxy"] = e["proxy"]
        # 天天基金约晚 20:00 公布当日净值。最近三期净值使用短缓存，既能及时识别
        # 当日是否已更新，也能给列表补出今日/上一净值日的确认收益。
        with ThreadPoolExecutor(max_workers=min(4, len(codes))) as pool:
            returns = dict(zip(codes, pool.map(_recent_return_fields, codes)))
        for c, r in returns.items():
            if not r:
                continue
            q = base.setdefault(c, {
                "name": c, "estimate_pct": None, "estimate_time": None,
                "nav": None, "nav_date": None,
            })
            # 最新净值统一使用天天基金最近确认的单位净值；日收益同源。
            q.update({k: v for k, v in r.items()
                      if k in {"nav", "nav_date"} or k.startswith(("today_", "yesterday_"))})
        return base

    return _cached(key, 60, _fetch)


def _recent_nav_rows(code: str) -> list[dict]:
    """最近三期确认净值（倒序）；短缓存确保晚间公布后几分钟内可见。"""
    def _fetch():
        import json
        import urllib.parse
        import urllib.request

        query = urllib.parse.urlencode({"fundCode": code, "pageIndex": 1, "pageSize": 3})
        req = urllib.request.Request(
            f"https://api.fund.eastmoney.com/f10/lsjz?{query}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"},
        )
        payload = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        return [
            {"date": str(r.get("FSRQ") or "")[:10], "nav": _num(r.get("DWJZ")), "day_pct": _num(r.get("JZZZL"))}
            for r in ((payload.get("Data") or {}).get("LSJZList") or [])
            if r.get("FSRQ") and _num(r.get("DWJZ")) is not None
        ]

    return _cached(f"recent_nav_{code}", 5 * 60, _fetch)


def _recent_return_fields(code: str, today: str | None = None) -> dict:
    """提供最近确认的单位净值及今日/上一净值日收益。"""
    try:
        rows = _recent_nav_rows(code)
    except Exception:
        return {}
    if not rows:
        return {}
    today = today or datetime.now(BEIJING).strftime("%Y-%m-%d")

    def fields(row_index: int | None, prefix: str) -> dict:
        if row_index is None:
            return {f"{prefix}_return_pct": None, f"{prefix}_return_date": None,
                    f"{prefix}_return_per_share": None, f"{prefix}_return_base_per_share": None}
        row = rows[row_index]
        pct = row.get("day_pct")
        prior_nav = rows[row_index + 1].get("nav") if row_index + 1 < len(rows) else None
        # 收益金额用净值差，避免两位小数的日增长率在大额持仓上放大舍入误差。
        per_share = row.get("nav") - prior_nav if row.get("nav") is not None and prior_nav is not None else None
        return {f"{prefix}_return_pct": pct, f"{prefix}_return_date": row["date"],
                f"{prefix}_return_per_share": per_share, f"{prefix}_return_base_per_share": prior_nav}

    today_index = next((i for i, row in enumerate(rows) if row["date"] == today), None)
    previous_index = next((i for i, row in enumerate(rows) if row["date"] < today), None)
    return {
        "nav": rows[0]["nav"], "nav_date": rows[0]["date"],
        **fields(today_index, "today"),
        **fields(previous_index, "yesterday"),
    }


# ---------------------------------------------------------------------------
# 自研盘中估值（东财下线官方估值后的后备）：上季十大重仓 × 实时股价加权
# ---------------------------------------------------------------------------

def _stock_mkt_prefix(code: str) -> str:
    """6 位股票代码 -> 腾讯行情 sh/sz 前缀。"""
    return ("sh" if code[0] in "689" or code.startswith("5") else "sz") + code


def _realtime_stock_pct(codes: list[str]) -> dict[str, float]:
    """批量取 A股实时涨跌幅（腾讯行情），{6位代码: 涨跌幅%}。缓存 20s。"""
    codes = [c for c in dict.fromkeys(codes) if re.fullmatch(r"\d{6}", c)]
    if not codes:
        return {}

    def _fetch():
        import urllib.request

        q = ",".join(_stock_mkt_prefix(c) for c in codes)
        raw = urllib.request.urlopen(f"https://qt.gtimg.cn/q={q}", timeout=8).read().decode("gbk", "ignore")
        out = {}
        for seg in raw.strip().split(";"):
            if "~" not in seg:
                continue
            p = seg.split("~")
            if len(p) > 32 and p[0]:
                m = re.search(r"(\d{6})", p[0])
                pct = _num(p[32])
                if m and pct is not None:
                    out[m.group(1)] = pct
        return out

    return _cached("rt_stock_" + ",".join(sorted(codes)), 20, _fetch)


def _top_holdings(code: str) -> list[tuple[str, float]]:
    """基金最新一期十大重仓股 [(标的, 占净值%)]，缓存 6h。债基/货基返回 []。

    标的两种形态：
    - A股：6 位代码字符串（"600519"），走腾讯行情；
    - 海外股（港/美/韩）：("116","02419") 元组——东财 secid 前缀 + 代码，走东财行情。
    QDII/沪港深基金的十大重仓会混排多市场股票，统一在这里解析。
    """
    def _fetch():
        import urllib.request

        url = f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10&year=&month="
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
        try:
            t = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        except Exception:
            return None  # 限流(514)/网络异常：不写缓存，下次重试（区别于"真无持仓"的 []）
        out = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
            # A股链接形如 //quote.eastmoney.com/unify/r/0.300613（0=深 1=沪）
            m = re.search(r"unify/r/\d\.(\d{6})", row)
            # 海外链接形如 unify/r/116.02419（港）/105.KLAC（美）/177.005930（韩）
            g = re.search(r"unify/r/(1(?:0[567]|1[67])|177)\.([A-Z0-9]{1,12})", row)
            pcts = re.findall(r"([\d.]+)%", row)
            if not pcts:
                continue
            if m:
                out.append((m.group(1), float(pcts[0])))
            elif g:
                out.append(((g.group(1), g.group(2)), float(pcts[0])))
        return out[:10]

    cache_key = f"top_hold_{code}"
    with _LOCK:
        hit = _CACHE.get(cache_key)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
    data = _fetch()
    if data is None:
        return []  # 异常不缓存，本次按无持仓跳过
    with _LOCK:
        _CACHE[cache_key] = (time.time(), data)
    return data


def _self_estimate(codes: list[str]) -> dict[str, dict]:
    """逐只估算今日净值涨跌幅。三条路径按基金类型自动选择：

    - 港股 / QDII 指数型：跟踪标的映射到港/美/韩实时指数或海外 ETF 代理，
      标 source=global_index。闭市市场沿用最近涨跌幅（东财延时行情），并标
      estimate_stale=True（那是「隔夜/最近一场」的涨跌，非今日盘中）。
    - 指数型 / ETF联接（A股）：用「跟踪标的指数」的实时涨跌幅（×0.95），
      标 source=index。对无公开实时行情的中证 9 字头指数，用同标的头部场内 ETF 代理。
    - 主动股基/偏股混合（含沪港深、QDII 主动基）：按上季十大重仓 × 个股实时
      涨跌幅加权，标 source=self。持仓按市场分流：A股走腾讯，港/美/韩股东财。

    纯债/货基/同业存单（无股票持仓）返回空，前端显示「—」。
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    out: dict[str, dict] = {}
    # 先两条指数路径（命中即采用）：港股/QDII 优先于 A股指数
    active: list[str] = []
    for c in codes:
        ge = _global_index_estimate(c)
        if ge is not None:
            out[c] = ge
            continue
        ie = _index_estimate(c)
        if ie is not None:
            out[c] = ie
        else:
            active.append(c)
    # 主动基走重仓加权（A股 + 港/美/韩混合持仓分别取价后合并）
    if active:
        holdings = {c: _top_holdings(c) for c in active}
        a_codes = sorted({sc for hs in holdings.values() for sc, w in hs
                          if not isinstance(sc, tuple)})
        ovl_secs = sorted({sc for hs in holdings.values() for sc, w in hs
                           if isinstance(sc, tuple)})
        pcts = _realtime_stock_pct(a_codes)
        pcts.update(_overseas_pct(ovl_secs))
        for c in active:
            hs = holdings.get(c) or []
            if not hs:
                continue
            coverage = sum(w for _, w in hs)  # 十大重仓合计占净值%
            if coverage < 15:
                # 覆盖太低（债券/货基/打新基的股票零头，或披露不全），加权无意义，不给伪精确估值
                continue
            est = sum((pcts.get(sc) or 0.0) * w for sc, w in hs) / 100.0  # 重仓贡献的净值涨跌%
            out[c] = {"estimate_pct": round(est, 2), "estimate_time": now, "source": "self"}
    return out


# ---------------------------------------------------------------------------
# 指数 / 场内ETF 行情兜底：指数型/ETF联接基金用「跟踪标的」的实时涨跌幅估算。
# 联接/指数基金业绩基准 = 指数收益率 × 95%，指数行情本身即最佳估算源——
# 比重仓加权更准（重仓披露滞后且只覆盖前十大）。
# ---------------------------------------------------------------------------

# 跟踪标的指数名 -> 行情代理。
#   ("idx", 腾讯指数代码)      : 直接取指数实时涨跌幅（000 系 sh/399 系 sz）
#   ("etf", 场内ETF代码)       : 该指数无公开实时行情（多为中证 9 字头），用跟踪同一
#                                指数的头部场内 ETF 实时涨跌幅代理（同标的 ETF 涨跌幅 ≈ 指数）
# 命中关键词按子串匹配跟踪标的指数名。
_INDEX_PROXY: list[tuple[str, tuple[str, str]]] = [
    ("红利低波", ("etf", "512890")),       # 中证红利低波动指数（无公开行情）→ 红利低波ETF华泰柏瑞
    ("半导体产业", ("etf", "512480")),     # 中证半导体产业指数 → 半导体ETF国联安
    ("中药", ("etf", "560080")),           # 中证中药指数 → 中药ETF汇添富
    ("科创板50", ("idx", "sh000688")),     # 上证科创板50成份指数
    ("科创50", ("idx", "sh000688")),
    ("有色金属", ("idx", "sh000819")),     # 有色金属指数
    ("军工", ("idx", "sz399967")),         # 中证军工指数
    ("大宗商品", ("idx", "sh000979")),     # 中证大宗商品股票指数
    ("上游资源", ("etf", "510410")),       # 中证上游资源产业指数 → 资源ETF博时
]


def _tracking_index(code: str) -> Optional[str]:
    """基金 F10 概况页的「跟踪标的」指数名；非指数型基金返回 None。缓存 24h。"""
    def _fetch():
        import urllib.request

        url = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"})
        t = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        m = re.search(r"跟踪标的</th>\s*<td[^>]*>(.*?)</td>", t, re.S)
        name = re.sub(r"<[^>]+>|\s+", "", m.group(1)) if m else ""
        return None if (not name or "无跟踪标的" in name) else name

    return _cached(f"track_idx_{code}", 24 * 3600, _fetch)


def _proxy_pct(kind: str, code: str) -> Optional[float]:
    """取一个行情代理（指数或场内ETF）的实时涨跌幅。指数/ETF 均走腾讯行情。"""
    symbol = code if kind == "idx" else (("sh" if code[0] in "56" or code.startswith(("51", "58")) else "sz") + code)
    def _fetch():
        import urllib.request

        raw = urllib.request.urlopen(f"https://qt.gtimg.cn/q={symbol}", timeout=8).read().decode("gbk", "ignore")
        p = raw.split("~")
        return _num(p[32]) if len(p) > 32 else None

    return _cached(f"proxy_{symbol}", 20, _fetch)


# ---------------------------------------------------------------------------
# 港股 / QDII 估值：跟踪标的 → 海外实时指数/ETF 代理 + 海外个股行情。
# 行情走 gstock._push2_stock_get（push2 优先、push2delay 降级），与全球指数同源。
# 闭市市场沿用最近涨跌幅并标 estimate_stale（QDII 净值按当地收盘确认，
# 白天看到的「估值」本就是隔夜的，标注后不与 A股盘中估值混淆）。
# ---------------------------------------------------------------------------

# 跟踪标的指数名关键词 → (gstock 指数 key, 东财 secid)。
# 用 gstock._INDICES 里现成的市场时段表判断开闭市；9 字头无行情的用海外 ETF 代理。
_GLOBAL_INDEX_PROXY: list[tuple[str, str, str]] = [
    # 美股指数（gstock key → _MARKET_HOURS 判开闭市）
    ("纳斯达克100",   "ndx",    "100.NDX"),     # 建信/广发纳斯达克100(QDII)
    ("纳斯达克",      "ndx",    "100.NDX"),
    ("标普",          "spx",    "100.SPX"),     # 标普500/标普消费（无专属行情，用标普500代理）
    # 港股指数
    ("恒生科技",      "hstech", "124.HSTECH"),  # 恒生科技
    ("恒生中国企业",  "hsi",    "100.HSCEI"),   # 国企指数
    ("恒生",          "hsi",    "100.HSI"),     # 恒生指数
    ("港股通",        "hsi",    "100.HSI"),     # 中证港股通综合/非银等 → 恒指代理
    ("沪港深",        "hsi",    "100.HSI"),
    # 海外行业指数 → 海外 ETF 代理（无指数公开行情时）
    ("海外互联网",    "ndx",    "107.KWEB"),    # 中证海外互联网 → KWEB（美交易时段）
    ("中国互联网",    "ndx",    "107.KWEB"),
    ("海外科技",      "ndx",    "105.QQQ"),     # 海外科技（无专属指数行情）→ 纳指ETF
]

# 非交易时段的界定：这些市场不开盘时，估值就是「最近一场」的隔夜数。


def _overseas_pct(tickers: list[tuple[str, str]]) -> dict:
    """批量取海外标的实时涨跌幅 {(mkt, code): pct}。入参是 _top_holdings 解析出的
    (东财secid前缀, 代码) 元组；逐只 stock/get（自带 10^f59 缩放，批量 ulist 不返回
    f59 不可用）；20s 缓存。"""
    out: dict = {}
    import gstock
    for tk in dict.fromkeys(tickers):
        mkt, code = tk
        secid = f"{mkt}.{code}"
        def _fetch(s=secid):
            d = gstock._push2_stock_get(s, gstock._QUOTE_FIELDS)
            return gstock._quote_from(d or {}).get("change_pct")
        pct = _cached(f"ovl_{secid}", 20, _fetch)
        if pct is not None:
            out[tk] = pct
    return out


def _global_index_estimate(code: str) -> Optional[dict]:
    """港股/QDII 指数型：按跟踪标的对应的海外实时指数估算（×0.95）。"""
    idx_name = _tracking_index(code)
    if not idx_name:
        return None
    for kw, gkey, secid in _GLOBAL_INDEX_PROXY:
        if kw not in idx_name:
            continue
        pct = _overseas_pct([tuple(secid.split(".", 1))]).get(tuple(secid.split(".", 1)))
        if pct is None:
            return None
        import gstock
        open_now = gstock._is_market_open(gkey)
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        return {"estimate_pct": round(pct * 0.95, 2), "estimate_time": now,
                "source": "global_index", "proxy": idx_name,
                "estimate_stale": not open_now}
    return None


def _index_estimate(code: str) -> Optional[dict]:
    """指数型/ETF联接：按跟踪标的的实时涨跌幅估算今日净值涨跌（≈ 指数 × 0.95）。"""
    idx_name = _tracking_index(code)
    if not idx_name:
        return None
    for kw, (kind, proxy) in _INDEX_PROXY:
        if kw in idx_name:
            pct = _proxy_pct(kind, proxy)
            if pct is None:
                return None
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
            return {"estimate_pct": round(pct * 0.95, 2), "estimate_time": now,
                    "source": "index", "proxy": idx_name}
    return None


# ---------------------------------------------------------------------------
# 历史净值与业绩指标
# ---------------------------------------------------------------------------

def nav_history(code: str, limit: int = 250) -> dict:
    """单位净值走势（东财天天基金）。返回升序 rows + 区间统计。"""
    code = (code or "").strip()

    def _fetch():
        import akshare as ak

        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        rows = []
        for _, r in df.iterrows():
            nav = _num(r.get("单位净值"))
            if nav is None:
                continue
            rows.append({
                "date": str(r.get("净值日期", ""))[:10],
                "nav": nav,
                "acc_nav": _num(r.get("累计净值")),
                "day_pct": _num(r.get("日增长率")),
            })
        rows.sort(key=lambda x: x["date"])
        return rows

    rows = _cached(f"nav_hist_{code}", 6 * 3600, _fetch)
    rows = rows[-limit:] if limit else rows
    return {"code": code, "rows": rows, "count": len(rows)}


def _ann_return(equity: list[float], periods_per_year: int = 252) -> Optional[float]:
    if len(equity) < 2 or equity[0] <= 0:
        return None
    total = equity[-1] / equity[0]
    years = (len(equity) - 1) / periods_per_year
    if years <= 0:
        return None
    return (total ** (1 / years) - 1) * 100


def _max_drawdown(equity: list[float]) -> Optional[float]:
    if not equity:
        return None
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd * 100


def fund_metrics(code: str, risk_free: float = 0.015) -> dict:
    """由近一年净值序列自算业绩指标：年化收益/最大回撤/年化波动率/夏普。"""
    hist = nav_history(code, limit=250)
    navs = [r["nav"] for r in hist["rows"] if r["nav"] and r["nav"] > 0]
    if len(navs) < 30:
        return {"code": code, "ann_return": None, "max_drawdown": None,
                "volatility": None, "sharpe": None, "n": len(navs)}
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    vol = math.sqrt(var) * math.sqrt(252)
    sharpe = ((mean * 252) - risk_free) / vol if vol > 0 else None
    return {
        "code": code,
        "ann_return": round(_ann_return(navs) or 0.0, 2),
        "max_drawdown": round(_max_drawdown(navs) or 0.0, 2),
        "volatility": round(vol * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "n": len(navs),
    }


# ---------------------------------------------------------------------------
# 档案与持仓明细
# ---------------------------------------------------------------------------

def _attach_change_pct(holdings: list[dict]) -> list[dict]:
    """给十大重仓补当日涨跌幅（change_pct）。A股走腾讯行情、海外（港/美/韩）走东财，
    与盘中估值同源（20s 缓存）。失败/无行情时 change_pct=None，前端显示「—」。"""
    if not holdings:
        return holdings
    a_codes = [h["stock_code"] for h in holdings if re.fullmatch(r"\d{6}", h.get("stock_code") or "")]
    pcts: dict = {}
    try:
        pcts = _realtime_stock_pct(a_codes)
    except Exception:
        pcts = {}
    # 海外标的：fund_portfolio_hold_em 的「股票代码」对港/美股可能是非 6 位代码，尽量解析
    # （港股 5 位、美股字母）。这里只对能映射到东财 secid 的补价；解析不了的留 None。
    out = []
    for h in holdings:
        sc = h.get("stock_code") or ""
        cp = pcts.get(sc) if re.fullmatch(r"\d{6}", sc) else None
        out.append({**h, "change_pct": cp})
    return out


def fund_profile(code: str) -> dict:
    """基金档案：基本信息 + 最新十大重仓 + 业绩指标。"""
    code = (code or "").strip()
    meta = fund_meta(code) or {"code": code, "name": code, "type": ""}
    out = {**meta}

    def _holdings():
        import akshare as ak

        df = ak.fund_portfolio_hold_em(symbol=code, date=str(__import__("datetime").datetime.now().year))
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "stock_code": str(r.get("股票代码", "")).strip(),
                "stock_name": str(r.get("股票名称", "")).strip(),
                "weight": _num(r.get("占净值比例")),
                "quarter": str(r.get("季度", "")).strip(),
            })
        return rows

    try:
        hs = _cached(f"fund_hold_{code}", 24 * 3600, _holdings)
        # 只保留最近一个季度
        if hs:
            latest_q = hs[0]["quarter"]
            holdings = [h for h in hs if h["quarter"] == latest_q][:10]
            out["holdings"] = _attach_change_pct(holdings)
            out["holdings_quarter"] = latest_q
        else:
            out["holdings"], out["holdings_quarter"] = [], None
    except Exception:
        out["holdings"], out["holdings_quarter"] = [], None

    try:
        out["metrics"] = fund_metrics(code)
    except Exception:
        out["metrics"] = None
    return out


# ---------------------------------------------------------------------------
# 业绩排行与筛选（含 4433 法则）
# ---------------------------------------------------------------------------

_RANK_COLS = ["近1周", "近1月", "近3月", "近6月", "近1年", "近2年", "近3年", "今年来", "成立来"]


def _rank_table() -> list[dict]:
    import akshare as ak

    df = ak.fund_open_fund_rank_em(symbol="全部")
    out = []
    for _, r in df.iterrows():
        code = str(r.get("基金代码", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        row = {
            "code": code,
            "name": str(r.get("基金简称", "")).strip(),
            "date": str(r.get("日期", ""))[:10],
            "nav": _num(r.get("单位净值")),
            "day_pct": _num(r.get("日增长率")),
        }
        for c in _RANK_COLS:
            row[c] = _num(r.get(c))
        out.append(row)
    return out


def screen_funds(fund_type: str = "", r4433: bool = False,
                 sort_by: str = "近1年", order: str = "desc",
                 min_y1: Optional[float] = None, min_m6: Optional[float] = None,
                 min_y3: Optional[float] = None,
                 keyword: str = "", limit: int = 100) -> dict:
    """全市场基金业绩筛选。

    - fund_type：类型包含匹配（如 "股票"/"混合"/"债券"/"指数"/"QDII"/"货币"）
    - r4433：4433 法则——近1年 前1/4，近2/3/5年及今年来 前1/3（此处数据含
      近1/2/3年与今年来，缺 5 年列则自动跳过该条件）
    - min_y1/min_m6/min_y3：近1年/近6月/近3年收益率下限（%）
    """
    table = _cached("fund_rank_table", 3600, _rank_table)
    rows = table
    if fund_type:
        idx = _cached("fund_name_index", 24 * 3600, _name_index)
        type_map = {f["code"]: f["type"] for f in idx}
        rows = [r for r in rows if fund_type in type_map.get(r["code"], "")]
    if keyword:
        kw = keyword.strip().lower()
        rows = [r for r in rows if kw in r["name"].lower() or r["code"].startswith(kw)]
    if min_y1 is not None:
        rows = [r for r in rows if (r.get("近1年") or -1e9) >= min_y1]
    if min_m6 is not None:
        rows = [r for r in rows if (r.get("近6月") or -1e9) >= min_m6]
    if min_y3 is not None:
        rows = [r for r in rows if (r.get("近3年") or -1e9) >= min_y3]

    total_all = len(table)
    if r4433:
        # 每个排名维度按百分位过滤：前 25% / 前 33%
        def _pct_filter(rs, col, top_pct):
            vals = sorted((r[col] for r in rs if r.get(col) is not None), reverse=True)
            if not vals:
                return rs
            k = max(1, int(len(vals) * top_pct))
            threshold = vals[min(k - 1, len(vals) - 1)]
            return [r for r in rs if r.get(col) is not None and r[col] >= threshold]

        rows = _pct_filter(rows, "近1年", 0.25)
        for col in ("近2年", "近3年", "今年来"):
            rows = _pct_filter(rows, col, 1 / 3)

    reverse = order != "asc"
    rows.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by) or 0.0), reverse=reverse)
    return {"total_all": total_all, "total_matched": len(rows),
            "rows": rows[: max(1, min(limit, 500))], "sort_by": sort_by}
