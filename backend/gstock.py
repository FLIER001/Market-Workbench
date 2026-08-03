"""美股 / 港股数据层 —— 移植自 global-stock-data（美港股全栈工具包）。

只并入「域内(东财)」的合规子集：全球指数 + 美港股行情 + 关键财务指标。
用途＝A 股「看隔夜外围脸色」+ 个股页支持美港股代码。

工程要点：
- 东财调用全部复用 `astock.em_get`（直连优先、避开用户 Clash 代理挂国内站）+
  `astock.eastmoney_datacenter`（datacenter 三表/指标已封装）。
- push2 stock/get 直连偶发掉连 → **push2 优先、失败降级 push2delay**（延时行情，研究场景足够），
  latch 到可用主机整进程复用（同成交额榜的做法）。
- Yahoo / SEC 等国外源不并入（需科学上网、且非必要）。

合规：只做客观数据整理，不预置标的、不推荐、不预测。
"""

from __future__ import annotations

import astock
import time
from datetime import datetime, timezone, timedelta

# ---- 各市场本地交易时段（用于"闭市就不请求，直接用上次收盘价"）----
# 每市场: (UTC 偏移小时, 交易时段列表[(start_h,start_m,end_h,end_m)], 周末是否闭市)
_MARKET_HOURS = {
    "spx":     (-4, [(9,30,16,0)], True),   # 美东 9:30-16:00
    "ndx":     (-4, [(9,30,16,0)], True),
    "hsi":     (8,  [(9,30,12,0),(13,0,16,0)], True),  # 香港
    "hstech":  (8,  [(9,30,12,0),(13,0,16,0)], True),
    "n225":    (9,  [(9,0,11,30),(12,30,15,0)], True), # 东京
    "ks11":    (9,  [(9,0,15,30)], True),   # 首尔
    "twii":    (8,  [(9,0,13,30)], True),   # 台北
    "aord":    (10, [(10,0,16,0)], True),   # 悉尼
    "set":     (7,  [(10,0,12,30),(14,30,16,30)], True), # 曼谷
    "jkse":    (7,  [(9,0,12,0),(13,30,15,15)], True),  # 雅加达
    "klse":    (8,  [(9,0,12,30),(14,30,17,0)], True),  # 吉隆坡
    "vnindex": (7,  [(9,0,11,30),(13,0,15,0)], True),   # 胡志明
    "gdaxi":   (2,  [(9,0,17,30)], True),   # 法兰克福
    "ftse":    (1,  [(8,0,16,30)], True),   # 伦敦
    "fchi":    (2,  [(9,0,17,30)], True),   # 巴黎
    "aex":     (2,  [(9,0,17,30)], True),   # 阿姆斯特丹
    "ssmi":    (2,  [(9,0,17,30)], True),   # 苏黎世
    "ibex":    (2,  [(9,0,17,30)], True),   # 马德里
    "sensex":  (5.5,[(9,15,15,30)], True),  # 孟买
}

def _is_market_open(key: str) -> bool:
    """该市场此刻是否在交易时段内（按其本地时间判断）。配置缺失时保守返回 True（照常请求）。"""
    cfg = _MARKET_HOURS.get(key)
    if not cfg:
        return True
    offset, sessions, weekend_closed = cfg
    local_now = datetime.now(timezone.utc) + timedelta(hours=offset)
    if weekend_closed and local_now.weekday() >= 5:  # 周六日闭市
        return False
    hm = local_now.hour * 60 + local_now.minute
    for (sh, sm, eh, em) in sessions:
        if sh * 60 + sm <= hm <= eh * 60 + em:
            return True
    return False

# 每个指数最近一次成功取到的报价（闭市时直接回用，不再请求东财）
_LAST_QUOTES: dict = {}
_LAST_QUOTES_TS: dict = {}
_LAST_QUOTES_TTL = 25 * 3600  # 最长保留 25 小时，避免隔夜陈旧

_UA_H = {"User-Agent": astock.UA}
_GS_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")
_gs_host = [0]  # 当前可用主机下标；首次 push2 掉连后 latch 到 push2delay

# 全球指数（东财 push2 secid）—— A 股看隔夜外围脸色 + 主要经济体指数，均已实测。
_INDICES = (
    # 美股（weight = 该市场总市值近似，单位万亿美元；同一指数群内按代表市值分配）
    {"key": "spx", "name": "标普500", "secid": "100.SPX", "region": "美股", "weight": 40.0},
    {"key": "ndx", "name": "纳斯达克", "secid": "100.NDX", "region": "美股", "weight": 25.0},
    # 港股
    {"key": "hsi", "name": "恒生指数", "secid": "100.HSI", "region": "港股", "weight": 4.5},
    {"key": "hstech", "name": "恒生科技", "secid": "124.HSTECH", "region": "港股", "weight": 1.5},
    # 亚太
    {"key": "n225", "name": "日经225", "secid": "100.N225", "region": "亚太", "weight": 6.0},
    {"key": "ks11", "name": "韩国KOSPI", "secid": "100.KS11", "region": "亚太", "weight": 1.8},
    {"key": "twii", "name": "台湾加权", "secid": "100.TWII", "region": "亚太", "weight": 2.0},
    {"key": "aord", "name": "澳大利亚普通股", "secid": "100.AORD", "region": "亚太", "weight": 1.7},
    {"key": "set", "name": "泰国SET", "secid": "100.SET", "region": "亚太", "weight": 0.5},
    {"key": "jkse", "name": "印尼雅加达综合", "secid": "100.JKSE", "region": "亚太", "weight": 0.6},
    {"key": "klse", "name": "马来西亚KLCI", "secid": "100.KLSE", "region": "亚太", "weight": 0.4},
    {"key": "vnindex", "name": "越南胡志明", "secid": "100.VNINDEX", "region": "亚太", "weight": 0.2},
    # 欧洲
    {"key": "gdaxi", "name": "德国DAX30", "secid": "100.GDAXI", "region": "欧洲", "weight": 2.0},
    {"key": "ftse", "name": "英国富时100", "secid": "100.FTSE", "region": "欧洲", "weight": 3.0},
    {"key": "fchi", "name": "法国CAC40", "secid": "100.FCHI", "region": "欧洲", "weight": 2.5},
    {"key": "aex", "name": "荷兰AEX", "secid": "100.AEX", "region": "欧洲", "weight": 0.9},
    {"key": "ssmi", "name": "瑞士SMI", "secid": "100.SSMI", "region": "欧洲", "weight": 1.8},
    {"key": "ibex", "name": "西班牙IBEX35", "secid": "100.IBEX", "region": "欧洲", "weight": 0.7},
    # 南亚
    {"key": "sensex", "name": "印度SENSEX", "secid": "100.SENSEX", "region": "南亚", "weight": 4.5},
)

# 中国视角的交易时段分组：日盘 = 亚太/港股/南亚（北京时间白天交易）；
# 夜盘 = 美股/欧洲（北京时间夜间/凌晨交易）。
_DAY_REGIONS = {"港股", "亚太", "南亚"}
_NIGHT_REGIONS = {"美股", "欧洲"}


def session_of(region: str) -> str:
    if region in _NIGHT_REGIONS:
        return "夜盘"
    return "日盘"


def weighted_session_change(indices: list[dict]) -> dict:
    """按市值权重计算日盘/夜盘的加权综合涨跌幅。缺数据的指数自动跳过并归一化。"""
    out = {}
    for session, regions in (("日盘", _DAY_REGIONS), ("夜盘", _NIGHT_REGIONS)):
        num = 0.0
        den = 0.0
        for it in indices:
            if it["region"] not in regions:
                continue
            chg = it.get("change_pct")
            w = it.get("weight")
            if chg is None or not isinstance(chg, (int, float)) or not w:
                continue
            num += chg * w
            den += w
        out[session] = round(num / den, 2) if den > 0 else None
    return out

# 搜索返回的 MktNum → (secucode 后缀, 市场名)
_MKT = {105: (".O", "NASDAQ"), 106: (".N", "NYSE"), 107: (".O", "US"), 116: (".HK", "HK"),
        177: (".KS", "KR")}  # 177=韩股（Kospi/Kosdaq，含三星/SK海力士等半导体龙头）；东财仅行情、无 F10 财务

_QUOTE_FIELDS = "f43,f44,f45,f46,f48,f57,f58,f59,f60,f116,f170"


def _push2_stock_get(secid: str, fields: str) -> dict | None:
    """东财 push2 stock/get：push2 优先、失败降级 push2delay；latch 可用主机。空数据返回 None。"""
    params = {"secid": secid, "fields": fields}
    for i in range(_gs_host[0], len(_GS_HOSTS)):
        try:
            r = astock.em_get(f"https://{_GS_HOSTS[i]}/api/qt/stock/get",
                              params=params, headers=_UA_H, timeout=10)
            d = r.json().get("data")
        except Exception:
            continue
        if d:
            _gs_host[0] = i
            return d
    return None


def _price(d: dict, key: str):
    """f43 等价格字段：除以 10^f59 还原。'-' / None → None。"""
    v = d.get(key)
    if not isinstance(v, (int, float)):
        return None
    dec = d.get("f59")
    if not isinstance(dec, int):  # 注意：不能用 `or 2`——韩元等 f59=0 会被误判成 2，价格被多除 100 倍
        dec = 2
    return round(v / (10 ** dec), dec)


def _quote_from(d: dict) -> dict:
    chg = d.get("f170")
    return {
        "code": d.get("f57"), "name": d.get("f58"),
        "price": _price(d, "f43"), "open": _price(d, "f46"),
        "high": _price(d, "f44"), "low": _price(d, "f45"),
        "prev_close": _price(d, "f60"),
        "amount": d.get("f48") if isinstance(d.get("f48"), (int, float)) else None,
        "mcap": d.get("f116") if isinstance(d.get("f116"), (int, float)) and d.get("f116") else None,
        "change_pct": round(chg / 100, 2) if isinstance(chg, (int, float)) else None,
    }


def global_indices() -> list[dict]:
    """全球指数快照。开市的市场实时请求；闭市的直接回用上次收盘价（标记 closed），不再请求。"""
    now = time.time()
    out = []
    for idx in _INDICES:
        key = idx["key"]
        open_now = _is_market_open(key)
        cached = _LAST_QUOTES.get(key)
        fresh_enough = cached is not None and (now - _LAST_QUOTES_TS.get(key, 0)) < _LAST_QUOTES_TTL

        # 闭市且有可用缓存：直接回用，不发请求
        if not open_now and fresh_enough:
            item = dict(cached)
            item["closed"] = True
            out.append(item)
            continue

        # 开市（或无缓存兜底）：实时请求
        d = _push2_stock_get(idx["secid"], "f43,f57,f58,f59,f60,f170")
        if not d:
            # 请求失败但有缓存：退回缓存（保持页面不空）
            if fresh_enough:
                item = dict(cached)
                item["closed"] = not open_now
                out.append(item)
            continue
        chg = d.get("f170")
        item = {
            "key": key, "name": idx["name"], "region": idx["region"],
            "price": _price(d, "f43"),
            "change_pct": round(chg / 100, 2) if isinstance(chg, (int, float)) else None,
            "weight": idx["weight"],
            "session": session_of(idx["region"]),
            "closed": not open_now,
        }
        # 取到有效价才更新缓存（避免把失败的 None 写进去）
        if item["price"] is not None:
            _LAST_QUOTES[key] = item
            _LAST_QUOTES_TS[key] = now
        out.append(item)
    return out


def _search(q: str) -> dict | None:
    """东财搜索一次：市场过滤 + **精确代码匹配优先**，退而取第一条。

    只按 MktNum 过滤挑不出正股——东财搜 AAPL 会混入 AAPL22(票据)/AAPB(2倍做多ETF)，
    搜 BABA 混入 05593(窝轮)，且 SecurityType 分不开(正股与 ETF 同为 Type7、正股港股与窝轮同为 Type6)。
    正股的 Code 恰好等于查询词，故精确匹配 Code==q 最稳；无精确匹配(名称查询)才退回第一条。
    """
    url = "https://searchapi.eastmoney.com/api/suggest/get"
    params = {"input": q, "type": 14,
              "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 10}
    try:
        r = astock.em_get(url, params=params, headers=_UA_H, timeout=10)
        rows = (r.json().get("QuotationCodeTable") or {}).get("Data") or []
    except Exception:
        return None
    matches = []
    for s in rows:
        try:
            mkt = int(s.get("MktNum"))
        except (TypeError, ValueError):
            continue
        if mkt in _MKT:
            matches.append((mkt, s))
    if not matches:
        return None
    mkt, s = next(((m, x) for m, x in matches if str(x.get("Code", "")).upper() == q), matches[0])
    suffix, market = _MKT[mkt]
    code = s.get("Code", "")
    return {"code": code, "name": s.get("Name", ""), "secid_prefix": mkt,
            "secucode": f"{code}{suffix}", "market": market}


def resolve_symbol(query: str) -> dict | None:
    """代码/名称 → {code, name, secid_prefix, secucode, market}。认美股/港股/韩股。
    数字型港股短代码（如 `700`）补零到 5 位再试一次（东财按 `00700` 收）。
    韩股用国际后缀 `.KS`/`.KQ`/`.KR`（如三星 `005930.KS`）——韩股代码与 A 股同为 6 位数字，
    需显式后缀区分，否则前端会按 A 股处理、后端也搜不到韩股。"""
    q = query.strip().upper()
    if not q:
        return None
    for suf in (".KS", ".KQ", ".KR"):  # 剥掉韩股后缀，按裸代码搜（东财 177=韩股）
        if q.endswith(suf):
            q = q[: -len(suf)]
            break
    hit = _search(q)
    if hit is None and q.isdigit() and len(q) < 5:
        hit = _search(q.zfill(5))
    return hit


def _key_metrics(secucode: str) -> dict | None:
    """东财 GMAININDICATOR 最新一期关键财务指标（美股/港股中文字段）。"""
    market = "HK" if secucode.endswith(".HK") else "US"
    rows = astock.eastmoney_datacenter(
        f"RPT_{market}F10_FN_GMAININDICATOR",
        filter_str=f'(SECUCODE="{secucode}")',
        page_size=1, sort_columns="REPORT_DATE", sort_types="-1")
    if not rows:
        return None
    m = rows[0]
    return {
        "report_date": str(m.get("REPORT_DATE") or "")[:10],
        "revenue": m.get("OPERATE_INCOME"),
        "revenue_yoy": m.get("OPERATE_INCOME_YOY"),
        "net_profit": m.get("PARENT_HOLDER_NETPROFIT") or m.get("HOLDER_PROFIT"),
        "eps": m.get("BASIC_EPS"),
        "roe": m.get("ROE_AVG"),
        "gross_margin": m.get("GROSS_PROFIT_RATIO"),
        "net_margin": m.get("NET_PROFIT_RATIO"),
        "debt_ratio": m.get("DEBT_ASSET_RATIO"),
    }


def us_hk_stock(query: str) -> dict:
    """个股聚合（美/港）：解析代码 → 行情 + 关键财务指标。查不到返回 {}。"""
    info = resolve_symbol(query)
    if not info:
        return {}
    d = _push2_stock_get(f"{info['secid_prefix']}.{info['code']}", _QUOTE_FIELDS)
    quote = _quote_from(d or {})  # 行情临时取不到也返回完整 null 形状，契合 GlobalQuote 类型
    return {
        "code": info["code"],
        "name": info["name"] or quote.get("name") or info["code"],
        "market": info["market"],
        "quote": quote,
        "metrics": _key_metrics(info["secucode"]) if info["market"] != "KR" else None,  # 韩股东财无 F10 财务
    }


# 港股现金流量表汇总科目：东财 RPT_HKSK_FN_CASHFLOW 的 STD_ITEM_CODE → 中文标签。
# 用稳定数字码作 key（不用东财中文 ITEM_NAME，避开其编码/措辞差异）；实测每期返回这 8 行汇总。
_HK_CF_ITEMS = {
    "003999": "经营活动现金流净额",
    "005999": "投资活动现金流净额",
    "007999": "筹资活动现金流净额",
    "006999": "汇率变动前现金净额",
    "011997": "汇率变动等其他影响",
    "010999": "现金及等价物净增加",
    "011001": "期初现金及等价物",
    "011999": "期末现金及等价物",
}
_HK_CF_ORDER = ("003999", "005999", "007999", "006999", "011997", "010999", "011001", "011999")


def hk_cashflow(query: str, periods: int = 8) -> dict:
    """港股现金流量表（东财 datacenter RPT_HKSK_FN_CASHFLOW，与已接入 GMAININDICATOR 同为东财域内源）。

    按 REPORT_DATE 分组还原每期汇总（经营 / 投资 / 筹资 / 净增加 / 期初期末），返回最近 `periods` 期。
    金额为原生币种（见 `currency`，港股多为人民币或港元），季度为 YTD 累计、附同比。
    非港股（美/韩股，其现金流走 F10/SK 或无）或查不到 → 返回 {}。
    """
    info = resolve_symbol(query)
    if not info or not info["secucode"].endswith(".HK"):
        return {}
    # ⚠️ 该端点是**按科目逐行**返回的，一期就有几十行（实测腾讯 00700 最多 52 行/期、
    # 工行 01398 38 行/期）。只按 SECUCODE 取 300 行，最新 8 期根本装不下——
    # 最旧的那期会被截断成残缺科目，而且不报错。所以在**服务端**就按需要的科目码过滤：
    # 实测同样 300 行，覆盖期数从 13 期升到 39 期，请求量反而更小。
    item_filter = "(STD_ITEM_CODE in (" + ",".join(f'"{c}"' for c in _HK_CF_ORDER) + "))"
    rows = astock.eastmoney_datacenter(
        "RPT_HKSK_FN_CASHFLOW",
        filter_str=f'(SECUCODE="{info["secucode"]}"){item_filter}',
        page_size=300, sort_columns="REPORT_DATE", sort_types="-1")
    if not rows:
        return {}
    by_period: dict[str, dict] = {}
    for r in rows:
        rd = str(r.get("REPORT_DATE") or "")[:10]
        code = str(r.get("STD_ITEM_CODE") or "")
        if not rd or code not in _HK_CF_ITEMS:
            continue
        p = by_period.setdefault(rd, {
            "report_date": rd, "report": r.get("REPORT"),
            "currency": r.get("CURRENCY"), "account_standard": r.get("ACCOUNT_STANDARD"),
            "items": {},
        })
        amt, yoy = r.get("AMOUNT"), r.get("YOY_RATIO")
        p["items"][_HK_CF_ITEMS[code]] = {
            "amount": amt if isinstance(amt, (int, float)) else None,
            "yoy": yoy if isinstance(yoy, (int, float)) else None,
        }
    if not by_period:
        return {}
    periods_out = sorted(by_period.values(), key=lambda x: x["report_date"], reverse=True)[:periods]
    return {
        "code": info["code"], "name": info["name"], "market": "HK",
        "currency": periods_out[0].get("currency"),
        "item_order": [_HK_CF_ITEMS[c] for c in _HK_CF_ORDER],
        "periods": periods_out,
    }
