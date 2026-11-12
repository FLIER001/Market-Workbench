"""美股 / 港股「新层」数据层 —— 移植自 global-stock-data v2.3.0（不并入既有 gstock.py）。

覆盖 v2.1–v2.3 新增：全球利率·汇率·央行（美联储 / 欧央行 / 纽约联储 / 财政部）、
VIX 期限结构与波动率指数、FINRA 做空余额、SEC Form 4 / 13F、台股、EIA 能源、
美国宏观（BLS 免 key 接口与每周失业金），以及 v2.1.0 修好的 Yahoo 全历史 K 线。

为什么单独一个模块而不是并进 gstock.py：gstock.py 的定位是「东财域内子集」（文件头
写明 Yahoo / SEC 等国外源不并入，免得海外源连不上时把整页拖死）。这里全是海外源，
可用性差异很大，单独成层后 extra_api 可以按端点报错，不影响原有的美港股行情/财务。

上游代码逐字保留，只去掉 SKILL.md 的代码围栏、删掉块尾的用法演示（那些会在 import
时真的执行），并统一头部 import。网络可达性见 docs/upstream-sync/2026-11-11-上游对账.md。

来源：https://github.com/simonlin1212/global-stock-data （Apache-2.0）。
"""
# ruff: noqa
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportOptionalMemberAccess=false, reportOptionalOperand=false, reportOptionalSubscript=false
# pyright: reportOptionalIterable=false, reportIndexIssue=false, reportReturnType=false
# pyright: reportPossiblyUnboundVariable=false, reportOperatorIssue=false, reportMissingImports=false
# 逐字移植的上游代码，类型标注按「第三方代码」对待：改了就无法与上游逐行对账。
from __future__ import annotations

import calendar
import copy
import csv
import io
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date as _date_cls
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

try:  # pandas 只在部分端点用；缺了也不该让整层 import 失败
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

# ---- BLS（美国宏观）常量：上游块被单独抽出来，这里集中定义 ----
_BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
_BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
_BLS_MAX_SERIES, _BLS_MAX_YEARS, _BLS_DAILY = 25, 10, 25    # 免 key 版官方上限，2026-11-02 实测一致
_BLS_ID = re.compile(r"[A-Z0-9_#\-]{2,45}")                  # 官方：大写字母、数字，可含 _ - #
_BLS_CITE = ("Source: U.S. Bureau of Labor Statistics, retrieved {d}. BLS.gov cannot vouch for the data "
             "or analyses derived from these data after the data have been retrieved from BLS.gov.")
_BLS_TTL = 3600
_bls_cache = {}                                              # 同一请求 1 小时内复用：免 key 版每天只有 25 次

import requests

_yahoo_session = None

def get_yahoo_session() -> requests.Session:
    """获取带 crumb 的 Yahoo Finance session（自动缓存）"""
    global _yahoo_session
    if _yahoo_session and hasattr(_yahoo_session, '_crumb'):
        return _yahoo_session
    
    s = requests.Session()
    s.headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    
    # Step 1: 获取 cookie
    s.get('https://fc.yahoo.com', timeout=10)
    
    # Step 2: 获取 crumb
    r = s.get('https://query2.finance.yahoo.com/v1/test/getcrumb', timeout=10)
    r.raise_for_status()
    s._crumb = r.text
    
    _yahoo_session = s
    return s

def yahoo_quote_summary(symbol: str, modules: list[str]) -> dict:
    """Yahoo quoteSummary 统一查询"""
    s = get_yahoo_session()
    r = s.get(f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}', params={
        'modules': ','.join(modules),
        'crumb': s._crumb,
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get('quoteSummary', {}).get('result', [{}])
    return results[0] if results else {}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

def eastmoney_datacenter(report_name: str, columns: str = "ALL",
                          filter_str: str = "", page_size: int = 50,
                          sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    """东财数据中心统一查询"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = requests.get(DATACENTER_URL, params=params, headers={"User-Agent": UA}, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []

import requests, re, time, threading

# SEC / BLS 要求程序在 User-Agent 里留可联系的真实姓名与邮箱（否则 403 /
# Undeclared Automated Tool）。产品侧用环境变量注入，不把联系方式写进仓库。
SEC_CONTACT = os.environ.get("MW_SEC_CONTACT") or "your-name your-email@example.com"

class DataNotAvailable(RuntimeError):
    """该日/该标的确实没有数据（如非交易日、文件尚未发布）——可安全回退到下一个候选日。

    与配置错误、网络错误区分开：后者必须立刻抛给调用方，
    否则「SEC_CONTACT 没配」会被日期回退循环吞掉，最后伪装成「没找到数据」。
    """

class _RateLimiter:
    """线程安全的最小间隔节流器（用锁，避免并发下被击穿）"""

    def __init__(self, max_per_sec: float):
        self._interval = 1.0 / float(max_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

_LIMITS = {
    "sec.gov": _RateLimiter(8),
    "finra.org": _RateLimiter(4),
    "cboe.com": _RateLimiter(4),
    "nasdaq.com": _RateLimiter(2),
    "bls.gov": _RateLimiter(2),       # BLS 官方上限 50 次 / 10 秒；免 key 版另有每天 25 次（见 Layer 16）
    "doleta.gov": _RateLimiter(2),
    "_default": _RateLimiter(5),
}

def _limiter_for(url: str) -> _RateLimiter:
    for host, lim in _LIMITS.items():
        if host != "_default" and host in url:
            return lim
    return _LIMITS["_default"]

def _is_object_missing(resp) -> bool:
    """
    正向识别「资源确实不存在」。

    ⚠️ SEC Archives 与 FINRA CDN 都托管在 S3 上，而 S3 在调用方没有
    ListBucket 权限时，对**不存在的对象**返回 `403 AccessDenied`（XML）
    而不是 404 NoSuchKey。实测 2026-07-24 两个源行为一致。

    真正的拒绝长得完全不同（SEC 的 UA 未声明返回 ~4.8KB HTML 页面），
    所以这里按 Content-Type + XML 错误码正向判定，
    而不是用「排除法」——否则限流/封禁会被伪装成「没数据」。
    """
    if resp.status_code == 404:
        return True
    if resp.status_code != 403:
        return False
    ctype = (resp.headers.get("Content-Type") or "").lower()
    head = (resp.text or "")[:500]
    return "xml" in ctype and "<Code>AccessDenied</Code>" in head

def official_get(url: str, params: dict = None, headers: dict = None,
                 timeout: int = 30, as_json: bool = False, raw: bool = False,
                 data: dict = None, json_body: dict = None):
    """
    V2.0 官方源统一出口：自动节流 + UA 处理 + 友好错误。
    as_json=True 返回 dict，raw=True 返回 bytes（XML 不声明字符集时用），否则返回 str。
    data / json_body：给了任一个就改发 POST（表单 / JSON），V2.3 新增。

    异常语义：资源不存在 → DataNotAvailable（调用方可回退到下一个候选日）；
             配置/限流/网络 → RuntimeError（必须冒泡）。
    """
    if "sec.gov" in url or "bls.gov" in url:
        if "your-email@example.com" in SEC_CONTACT:
            raise RuntimeError(
                "请先把 SEC_CONTACT 改成你的真实姓名与邮箱 —— SEC 与 BLS 都要求程序在 "
                "User-Agent 里留联系方式：SEC 不留返回 Undeclared Automated Tool 错误，BLS 网站直接 403。")
        h = {"User-Agent": SEC_CONTACT, "Accept-Encoding": "gzip, deflate"}
    else:
        h = {"User-Agent": UA}
    h.update(headers or {})

    _limiter_for(url).wait()
    try:
        if data is not None or json_body is not None:
            r = requests.post(url, params=params, data=data, json=json_body, headers=h, timeout=timeout)
        else:
            r = requests.get(url, params=params, headers=h, timeout=timeout)
        r.raise_for_status()
    except requests.HTTPError as e:
        resp = e.response
        code = resp.status_code
        low = (resp.text or "")[:4000].lower()
        # ① 正向识别：资源确实不存在（404，或 S3 风格的 403 AccessDenied）
        if _is_object_missing(resp):
            raise DataNotAvailable(
                f"HTTP {code} {url[:80]} — 资源不存在（该日无数据/尚未发布）") from e
        # ② SEC 的 UA 未声明（返回 HTML 页，含 Undeclared Automated Tool）
        if code == 403 and "undeclared" in low:
            raise RuntimeError(
                f"SEC 拒绝请求：User-Agent 未被识别为已声明。"
                f"当前 SEC_CONTACT={SEC_CONTACT!r}，"
                f"格式应为 'Company Name AdminContact@domain.com'") from e
        # ②b BLS 拦截不带联系方式的程序，也会临时封请求过快的地址
        if code == 403 and "bls.gov" in url:
            raise RuntimeError(
                f"BLS 拒绝请求（HTTP 403）：请确认 SEC_CONTACT 里是真实邮箱（当前 {SEC_CONTACT!r}）；"
                f"也可能是请求太频繁被临时封，稍后再试") from e
        # ③ 其余一律视为真错误，必须冒泡（限流/封禁/权限/接口变更）
        body = re.sub(r"\s+", " ", re.sub(r"(?s)<(script|style).*?</\1>|<[^>]+>", " ", resp.text or "")).strip()
        hint = {400: "参数错误：" + (body[:200] or "（官方未给原因）"),
                403: "被拒绝：限流、封禁或权限问题（已排除「资源不存在」）",
                404: "端点不存在：接口可能已变更",
                429: "请求过快：已内置节流，若仍触发请调低 _LIMITS"}.get(code, "")
        raise RuntimeError(f"HTTP {code} {url[:80]} — {hint}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"请求失败 {url[:80]} — {type(e).__name__}: {e}") from e
    return r.json() if as_json else (r.content if raw else r.text)

def assert_us_ticker(ticker: str) -> str:
    """Layer 6.1 / 9 / 10 / 11 仅支持美股；传入港股代码时给出明确提示"""
    t = str(ticker).upper()
    if t.endswith(".HK") or (t.isdigit() and len(t) in (4, 5)):
        raise ValueError(f"'{ticker}' 看起来是港股代码；该层仅支持美股。"
                         f"港股请用 Layer 1-5 的港股端点。")
    if not t.replace(".", "").replace("-", "").isalnum():
        raise ValueError(f"无效的 ticker: '{ticker}'")
    return t

import requests, re

def us_stock_quote_sina(ticker: str) -> dict:
    """
    新浪美股行情 — 36字段
    ticker: 纯字母，如 "AAPL", "TSLA", "BABA"
    """
    url = f"https://hq.sinajs.cn/list=gb_{ticker.lower()}"
    r = requests.get(url, headers={
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": UA,
    }, timeout=10)
    r.encoding = "gbk"
    text = r.text
    
    m = re.search(r'"(.+)"', text)
    if not m:
        return {}
    
    fields = m.group(1).split(",")
    if len(fields) < 30:
        return {}
    
    return {
        "name": fields[0],           # 中文名
        "price": float(fields[1]),    # 最新价
        "change_pct": float(fields[2]),  # 涨跌幅 %
        "timestamp": fields[3],       # 时间
        "prev_close": float(fields[26]),  # 昨收
        "open": float(fields[5]),     # 开盘
        "high": float(fields[6]),     # 最高
        "low": float(fields[7]),      # 最低
        "volume": float(fields[10]) if fields[10] else 0,  # 成交量
        "high_52w": float(fields[8]) if fields[8] else 0,  # 52周最高
        "low_52w": float(fields[9]) if fields[9] else 0,   # 52周最低
        "market_cap": float(fields[12]) if fields[12] else 0,  # 市值
        "eps": float(fields[13]) if fields[13] else 0,  # EPS
        "pe": float(fields[14]) if fields[14] else 0,   # PE
    }

def us_stock_quote_tencent(ticker: str) -> dict:
    """
    腾讯美股行情 — 71字段
    ticker: 纯字母，如 "AAPL"
    """
    url = f"https://qt.gtimg.cn/q=us{ticker.upper()}"
    r = requests.get(url, timeout=10)
    r.encoding = "gbk"
    text = r.text
    
    m = re.search(r'"(.+)"', text)
    if not m:
        return {}
    
    fields = m.group(1).split("~")
    if len(fields) < 52:   # 需读到 fields[51](PB)，美股正常返回 71 个
        return {}
    
    # ⚠️ 下标以实测为准，勿照抄港股那套（两市布局不同，见本节末「腾讯行情字段对照表」）
    return {
        "name": fields[1],           # 中文名
        "name_en": fields[46],       # 英文名，如 "Apple Inc."
        "price": float(fields[3]) if fields[3] else 0,
        "prev_close": float(fields[4]) if fields[4] else 0,
        "open": float(fields[5]) if fields[5] else 0,
        "volume": int(float(fields[6])) if fields[6] else 0,
        "high": float(fields[33]) if fields[33] else 0,
        "low": float(fields[34]) if fields[34] else 0,
        "high_52w": float(fields[48]) if fields[48] else 0,
        "low_52w": float(fields[49]) if fields[49] else 0,
        "change_pct": float(fields[32]) if fields[32] else 0,
        "float_market_cap": float(fields[44]) if fields[44] else 0,  # 流通市值，亿美元
        "market_cap": float(fields[45]) if fields[45] else 0,        # 总市值，亿美元
        "eps": float(fields[47]) if fields[47] else 0,
        "pe": float(fields[39]) if fields[39] else 0,
        "pb": float(fields[51]) if fields[51] else 0,
        "currency": fields[35],      # "USD"
        "timestamp": fields[30],
    }

def hk_stock_quote_tencent(code: str) -> dict:
    """
    腾讯港股行情 — 78字段（最全）
    code: 五位数字，如 "00700", "09988"
    """
    url = f"https://qt.gtimg.cn/q=r_hk{code}"
    r = requests.get(url, timeout=10)
    r.encoding = "gbk"
    text = r.text
    
    m = re.search(r'"(.+)"', text)
    if not m:
        return {}
    
    fields = m.group(1).split("~")
    if len(fields) < 76:   # 需读到 fields[75](币种)，港股正常返回 78 个
        return {}
    
    # ⚠️ 下标以实测为准，与美股那套不同（见本节末「腾讯行情字段对照表」）
    return {
        "name": fields[1],           # 中文名
        "code": fields[2],           # 五位代码，如 "00700"（旧版误当英文名）
        "name_en": fields[46],       # 英文名，如 "TENCENT"
        "price": float(fields[3]) if fields[3] else 0,
        "prev_close": float(fields[4]) if fields[4] else 0,
        "open": float(fields[5]) if fields[5] else 0,
        "high": float(fields[33]) if fields[33] else 0,
        "low": float(fields[34]) if fields[34] else 0,
        "volume": int(float(fields[6])) if fields[6] else 0,  # 成交量(股)
        "amount": float(fields[37]) if fields[37] else 0,     # 成交额
        "change_pct": float(fields[32]) if fields[32] else 0,
        "pe": float(fields[39]) if fields[39] else 0,
        "pb": float(fields[58]) if fields[58] else 0,
        "high_52w": float(fields[48]) if fields[48] else 0,
        "low_52w": float(fields[49]) if fields[49] else 0,
        "float_market_cap": float(fields[44]) if fields[44] else 0,  # 流通市值，亿港元
        "market_cap": float(fields[45]) if fields[45] else 0,        # 总市值，亿港元
        "currency": fields[75],      # "HKD"
        "timestamp": fields[30],
    }

def hk_stock_quote_sina(code: str) -> dict:
    """
    新浪港股行情 — 25字段
    code: 五位数字，如 "00700"
    """
    url = f"https://hq.sinajs.cn/list=rt_hk{code}"
    r = requests.get(url, headers={
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": UA,
    }, timeout=10)
    r.encoding = "gbk"
    text = r.text
    
    m = re.search(r'"(.+)"', text)
    if not m:
        return {}
    
    fields = m.group(1).split(",")
    if len(fields) < 15:
        return {}
    
    return {
        "name_en": fields[0],
        "name": fields[1],           # 中文名
        "open": float(fields[2]) if fields[2] else 0,
        "prev_close": float(fields[3]) if fields[3] else 0,
        "high": float(fields[4]) if fields[4] else 0,
        "low": float(fields[5]) if fields[5] else 0,
        "price": float(fields[6]) if fields[6] else 0,
        "change": float(fields[7]) if fields[7] else 0,
        "change_pct": float(fields[8]) if fields[8] else 0,
        "volume": float(fields[12]) if fields[12] else 0,
        "amount": float(fields[11]) if fields[11] else 0,
    }

def stock_quote_eastmoney(ticker_or_code: str, secid_prefix: int = 105) -> dict:
    """
    东财 push2 实时行情 — 美股+港股统一接口
    美股: stock_quote_eastmoney("AAPL", 105)  # NASDAQ
          stock_quote_eastmoney("BABA", 106)  # NYSE
    港股: stock_quote_eastmoney("00700", 116)
    返回: 最新价/开高低收/成交量/成交额/换手率/涨跌幅/中文名
    
    secid_prefix 说明: 105=NASDAQ, 106=NYSE, 107=US_ETF, 116=港股
    如不确定前缀，先调 stock_search() 获取 mkt_num
    """
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": f"{secid_prefix}.{ticker_or_code}",
        "fields": "f43,f44,f45,f46,f47,f48,f55,f57,f58,f59,f60,f170",
    }
    r = requests.get(url, params=params, timeout=10)
    d = r.json().get("data")
    if not d:
        return {}
    
    # f59 = 小数位数, 价格字段需除以 10^f59 还原真实值
    dec = d.get("f59", 3)
    divisor = 10 ** dec
    
    def _p(key):
        v = d.get(key)
        if v is None or v == "-":
            return None
        return round(v / divisor, dec)
    
    return {
        "code": d.get("f57"),           # 股票代码
        "name": d.get("f58"),           # 中文名
        "price": _p("f43"),             # 最新价
        "high": _p("f44"),              # 最高
        "low": _p("f45"),               # 最低
        "open": _p("f46"),              # 开盘
        "volume": d.get("f47"),         # 成交量(股)
        "amount": d.get("f48"),         # 成交额
        "turnover_rate": d.get("f55"),  # 换手率(%)
        "prev_close": _p("f60"),        # 昨收
        "change_pct": round(d["f170"] / 100, 2) if d.get("f170") is not None else None,  # 涨跌幅(%)
    }

def us_stock_kline_sina(ticker: str, num: int = 120) -> list[dict]:
    """
    新浪美股日K — 可回溯到1984年
    ticker: 如 "AAPL"
    返回: [{date, open, high, low, close, volume}, ...]
    """
    url = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK"
    params = {"symbol": ticker.upper(), "num": num}
    r = requests.get(url, params=params, headers={"Referer": "https://finance.sina.com.cn/"}, timeout=15)
    text = r.text
    
    # 解析 JSONP: var=([{...},...])
    import json
    m = re.search(r'\((\[.+\])\)', text)
    if not m:
        return []
    
    try:
        items = json.loads(m.group(1))
    except ValueError as exc:
        raise RuntimeError(f"CBOE 合约数组无法解析: {exc}") from exc
    result = []
    for item in items:
        result.append({
            "date": item.get("d"),
            "open": float(item.get("o", 0)),
            "high": float(item.get("h", 0)),
            "low": float(item.get("l", 0)),
            "close": float(item.get("c", 0)),
            "volume": int(item.get("v", 0)),
        })
    return result

_YAHOO_INTRADAY = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "4h"}

def _yahoo_exchange_tz(meta: dict, intraday: bool):
    """K 线日期按交易所当地时间算，与使用者电脑的时区无关。"""
    from datetime import timedelta, timezone
    try:
        from zoneinfo import ZoneInfo          # Python 3.9+；Windows 需另装 tzdata
        return ZoneInfo(meta["exchangeTimezoneName"]), 0
    except Exception:
        # 退回固定偏移：gmtoffset 是「当前」偏移，跨夏令时会差 1 小时。
        # 日/周/月线的时间戳落在当地 00:00–16:00 之间，再加 3 小时可吸收这 1 小时误差。
        tz = timezone(timedelta(seconds=meta.get("gmtoffset") or 0))
        return tz, (0 if intraday else 3 * 3600)

def _yahoo_range_start(range_: str):
    """range_ → 起始日（UTC 日期），供周/月/季线换算成起止日期用"""
    import calendar
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    if range_ == "ytd":
        return today.replace(month=1, day=1)
    m = re.fullmatch(r"(\d+)(d|mo|y)", str(range_))
    if not m:
        raise ValueError(f"range_ 取 1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max，收到 {range_!r}")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return today - timedelta(days=n)
    k = today.year * 12 + today.month - 1 - n * (12 if unit == "y" else 1)
    y, m = k // 12, k % 12 + 1
    return today.replace(year=y, month=m, day=min(today.day, calendar.monthrange(y, m)[1]))

def stock_kline_yahoo(symbol: str, interval: str = "1d", range_: str = "6mo",
                      start: str = None, end: str = None) -> list[dict]:
    """
    Yahoo Finance chart API — 美股/港股及其他市场通用，零crumb
    symbol: "AAPL" (美股) / "0700.HK" (港股) / 其他市场后缀见「市场代码规则」
    interval: "1d", "1wk", "1mo", "3mo", "1m", "2m", "5m", "15m", "30m", "90m", "1h", "4h"
    range_: "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"
    start / end: "YYYY-MM-DD"（按交易所当地日期），给了任一个就按起止日期取（忽略 range_）；
                 只给 end = 从最早一根取到 end。日线、分钟线含 start、end 当天
    返回: [{date, open, high, low, close, volume, adj_close?}, ...]；区间内没有交易返回 []

    周/月/季线由日线按交易所当地日历合并（日期标签是周期第一天：周一 / 每月 1 日 / 每季第一天，
    与 Yahoo 原生标签一致）。每一根都是完整周期：start 所在周期从第一天算起，end 所在周期算到最后一天，
    还没走完的当期只到最新交易日。range_ 同理：会补齐第一个周期（如 2y 月线 = 25 根）。
    不直接用 Yahoo 原生周/月/季线，是因为它在节假日开头的那一周会拿上一周的收盘价当开盘价（实测东京、
    香港、伦敦），按 start/end 截取时周线会多算或少算一天、季线分组跟着起始日漂移，末尾还会多一根只含最新一天的 bar。

    ⚠️ Yahoo 的 range=max 会无视 interval，按标的年限退成月线或季线
    （实测 AAPL 返回 169 根季线、0700.HK 270 根月线），所以 "max" 在这里改用起止时间请求，
    日线可拿到上市以来全部（AAPL 1980 起 11,549 根）。分钟线 Yahoo 只给最近 60 天（1h / 4h 为 730 天；
    1m 为 30 天且一次最多取约 7 天；2m 实测只有最近约 30 个交易日，更早的返回 []），不支持 "max"；
    在这个窗口内可以用 start/end。start 落在窗口第一天时，只返回窗口内的那一段。
    """
    import time
    from datetime import datetime, timedelta, timezone
    intraday = interval in _YAHOO_INTRADAY

    def _day(s, name):
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 要写成 YYYY-MM-DD，收到 {s!r}") from None

    d0 = _day(start, "start") if start else None
    d1 = _day(end, "end") if end else None
    if d0 and d1 and d0 > d1:
        raise ValueError(f"start {start} 晚于 end {end}")
    # 当地日期最多比 UTC 日期早一天（新西兰开盘时 UTC 还是前一天），超过一天才算未来
    if d0 and d0.timestamp() - 86400 > time.time():
        raise ValueError(f"start {start} 在未来")

    if interval in ("1wk", "1mo", "3mo"):
        def _first(d):          # d 所在周期的第一天
            if interval == "1wk":
                return d - timedelta(days=d.weekday())
            return d.replace(month=(d.month - 1) // (3 if interval == "3mo" else 1)
                             * (3 if interval == "3mo" else 1) + 1, day=1)

        def _next(d):           # 下一个周期的第一天
            if interval == "1wk":
                return d + timedelta(days=7)
            k = d.year * 12 + d.month - 1 + (3 if interval == "3mo" else 1)
            return d.replace(year=k // 12, month=k % 12 + 1, day=1)

        if not (d0 or d1) and range_ != "max":
            d0 = datetime.combine(_yahoo_range_start(range_), datetime.min.time(), timezone.utc)
        s0 = _first(d0.date()).isoformat() if d0 else None
        s1 = (_next(_first(d1.date())) - timedelta(days=1)).isoformat() if d1 else None
        daily = stock_kline_yahoo(symbol, "1d", range_="max", start=s0, end=s1)
        bars = {}
        for x in daily:
            k = _first(datetime.strptime(x["date"], "%Y-%m-%d").date()).isoformat()
            b = bars.get(k)
            if b is None:
                bars[k] = dict(x, date=k)
                continue
            b["high"] = max(v for v in (b["high"], x["high"]) if v is not None) \
                if (b["high"] is not None or x["high"] is not None) else None
            b["low"] = min(v for v in (b["low"], x["low"]) if v is not None) \
                if (b["low"] is not None or x["low"] is not None) else None
            b["close"] = x["close"]
            b["volume"] += x["volume"]
            if "adj_close" in x:
                b["adj_close"] = x["adj_close"]
        return list(bars.values())

    lo = d0.strftime("%Y-%m-%d") if d0 else None
    hi = d1.strftime("%Y-%m-%d") if d1 else None
    params = {"interval": interval, "events": "div,split"}
    if d0 or d1:
        # 起点往前放宽：亚太的时间戳落在 UTC 前一天（东京 00:00Z、悉尼 23:00Z、新西兰最早 21:00Z），
        # 分钟线只放宽 14 小时，免得把 60 天窗口的第一天挤出去。终点：日线放宽 2 天；分钟线放宽 30 小时——
        # 期货（CL=F 等）按纽约时间交易到当地午夜，即 UTC 次日 04:00–05:00；不放宽 2 天是给 1m 的 8 天上限留余量。
        # 精确范围由下面按交易所当地日期再筛
        pad = timedelta(hours=14) if intraday else timedelta(days=1)
        params["period1"] = int((d0 - pad).timestamp()) if d0 else -2208988800  # 1900-01-01
        if intraday:            # 放宽的部分不能超出 Yahoo 的分钟线窗口，否则整个请求被拒
            days = {"1m": 30, "60m": 730, "1h": 730, "4h": 730}.get(interval, 60)
            edge = int(time.time()) - days * 86400 + 600
            for d, name, v in ((d0, "start", start), (d1, "end", end)):
                if d and d.timestamp() + 86400 <= edge:
                    raise ValueError(f"{interval} 线 Yahoo 只给最近 {days} 天，{name} {v} 超出范围")
            params["period1"] = max(params["period1"], edge)
        tail = timedelta(hours=30) if intraday else timedelta(days=2)
        params["period2"] = min(int((d1 + tail).timestamp()) if d1 else 2**31, int(time.time()))
        if params["period1"] >= params["period2"]:
            return []           # start 是今天或明天（按 UTC），还没有成交
        if interval == "1m" and params["period2"] - params["period1"] > 8 * 86400:
            raise ValueError("1m 线 Yahoo 一次最多取约 7 天，请同时给 start 和 end，跨度不超过 7 天")
    elif range_ == "max":
        if intraday:
            raise ValueError("分钟/小时线没有全部历史（Yahoo 分钟线只给最近 60 天，1m 30 天，1h / 4h 730 天），请用 range_ 或 start/end")
        params["period1"], params["period2"] = -2208988800, int(time.time())
    else:
        params["range"] = range_

    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params=params, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    if r.status_code == 404:
        raise ValueError(f"Yahoo 找不到 {symbol!r}（代码不对或已退市）")
    if r.status_code in (400, 422):        # 参数不被接受（如分钟线超出 60 天窗口）
        try:
            desc = ((r.json().get("chart") or {}).get("error") or {}).get("description")
        except ValueError:
            desc = None
        if desc and desc.startswith("Data doesn't exist"):
            return []           # 区间早于上市或晚于最新交易日，和节假日一样视为「没有交易」
        raise ValueError(f"Yahoo 拒绝该请求：{desc or r.text[:200]}")
    r.raise_for_status()

    chart = ((r.json().get("chart") or {}).get("result") or [None])[0] or {}
    meta = chart.get("meta") or {}
    timestamps = chart.get("timestamp") or []
    indicators = chart.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    adj = ((indicators.get("adjclose") or [{}])[0]).get("adjclose")
    tz, nudge = _yahoo_exchange_tz(meta, intraday)
    live_ts = meta.get("regularMarketTime")
    fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"

    def _local(ts):
        # 最新一根的时间戳是真实成交时间，用当前偏移换算本来就准，不加修正量
        return datetime.fromtimestamp(ts + (0 if ts == live_ts else nudge), tz)

    def _v(key, i):
        x = (quote.get(key) or [None] * len(timestamps))[i]
        # Yahoo 内部按单精度存价格（7786.36 会变成 7786.3599…），保留 7 位有效数字去掉噪声；
        # 不能一律只留 2 位小数——港股仙股、汇率有 3 位以上小数
        return float(f"{x:.7g}") if x is not None else None

    result = []
    for i, ts in enumerate(timestamps):
        close = _v("close", i)
        if close is None:          # 停牌/缺失的 bar 直接跳过，不用 0 冒充价格
            continue
        row = {
            "date": _local(ts).strftime(fmt),
            "open": _v("open", i), "high": _v("high", i), "low": _v("low", i),
            "close": close,
            "volume": int((quote.get("volume") or [0] * len(timestamps))[i] or 0),
        }
        if adj and adj[i] is not None:
            row["adj_close"] = float(f"{adj[i]:.7g}")    # 含分红再投资的复权收盘
        if (hi and row["date"][:10] > hi) or (lo and row["date"][:10] < lo):
            continue
        if result and result[-1]["date"] == row["date"]:
            result[-1] = row       # 盘中最后一根偶尔与上一根同日，保留较新的
        else:
            result.append(row)
    return result

def _ema(values: list[float], period: int) -> list[float]:
    """EMA 指数移动平均（内部辅助）"""
    result = [values[0]]
    k = 2 / (period + 1)
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def calc_ma(klines: list[dict], periods: list[int] = None) -> list[dict]:
    """
    移动平均线 MA + EMA
    klines: K线数据 [{date, open, high, low, close, volume}, ...]
    periods: 周期列表，默认 [5, 10, 20, 60]
    返回: [{date, close, ma5, ma10, ma20, ma60, ema12, ema26}, ...]
    """
    if periods is None:
        periods = [5, 10, 20, 60]
    closes = [k["close"] for k in klines]
    
    # EMA 12/26（MACD 常用）
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    
    result = []
    for i, k in enumerate(klines):
        row = {"date": k["date"], "close": k["close"]}
        for p in periods:
            if i >= p - 1:
                row[f"ma{p}"] = round(sum(closes[i - p + 1:i + 1]) / p, 4)
            else:
                row[f"ma{p}"] = None
        row["ema12"] = round(ema12[i], 4)
        row["ema26"] = round(ema26[i], 4)
        result.append(row)
    return result

def calc_macd(klines: list[dict], fast: int = 12, slow: int = 26,
              signal: int = 9) -> list[dict]:
    """
    MACD (Moving Average Convergence Divergence)
    klines: K线数据
    fast/slow/signal: 快线/慢线/信号线周期（默认 12/26/9）
    返回: [{date, close, dif, dea, macd_hist}, ...]
    
    dif = EMA(fast) - EMA(slow)        金叉/死叉看 dif 穿越 dea
    dea = EMA(signal) of dif           信号线
    macd_hist = (dif - dea) * 2        柱状图（红涨绿跌）
    """
    closes = [k["close"] for k in klines]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    
    dif = [round(f - s, 4) for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    
    result = []
    for i, k in enumerate(klines):
        result.append({
            "date": k["date"],
            "close": k["close"],
            "dif": round(dif[i], 4),
            "dea": round(dea[i], 4),
            "macd_hist": round((dif[i] - dea[i]) * 2, 4),
        })
    return result

def calc_rsi(klines: list[dict],
             periods: list[int] = None) -> list[dict]:
    """
    RSI (Relative Strength Index)
    klines: K线数据
    periods: 周期列表（默认 [6, 12, 24]）
    返回: [{date, close, rsi6, rsi12, rsi24}, ...]
    
    RSI > 70 超买区（可能回调）
    RSI < 30 超卖区（可能反弹）
    """
    if periods is None:
        periods = [6, 12, 24]
    closes = [k["close"] for k in klines]
    
    # 涨跌额序列
    changes = [0.0] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    
    result = []
    for i, k in enumerate(klines):
        row = {"date": k["date"], "close": k["close"]}
        for p in periods:
            if i < p:
                row[f"rsi{p}"] = None
                continue
            avg_gain = sum(gains[i - p + 1:i + 1]) / p
            avg_loss = sum(losses[i - p + 1:i + 1]) / p
            if avg_loss == 0:
                row[f"rsi{p}"] = 100.0
            else:
                rs = avg_gain / avg_loss
                row[f"rsi{p}"] = round(100 - 100 / (1 + rs), 2)
        result.append(row)
    return result

def calc_kdj(klines: list[dict], n: int = 9,
             m1: int = 3, m2: int = 3) -> list[dict]:
    """
    KDJ 随机指标
    klines: K线数据
    n: RSV 周期（默认9）
    m1/m2: K/D 平滑系数（默认3/3）
    返回: [{date, close, k, d, j}, ...]
    
    K/D > 80 超买，K/D < 20 超卖
    J > 100 或 J < 0 为极端信号
    金叉: K 上穿 D；死叉: K 下穿 D
    """
    k_val, d_val = 50.0, 50.0
    result = []
    
    for i, kline in enumerate(klines):
        if i < n - 1:
            result.append({"date": kline["date"], "close": kline["close"],
                           "k": None, "d": None, "j": None})
            continue
        
        window = klines[i - n + 1:i + 1]
        high_n = max(w["high"] for w in window)
        low_n = min(w["low"] for w in window)
        
        rsv = (kline["close"] - low_n) / (high_n - low_n) * 100 if high_n != low_n else 50.0
        k_val = (1 / m1) * rsv + (1 - 1 / m1) * k_val
        d_val = (1 / m2) * k_val + (1 - 1 / m2) * d_val
        j_val = 3 * k_val - 2 * d_val
        
        result.append({
            "date": kline["date"],
            "close": kline["close"],
            "k": round(k_val, 2),
            "d": round(d_val, 2),
            "j": round(j_val, 2),
        })
    return result

def calc_boll(klines: list[dict], period: int = 20,
              num_std: float = 2.0) -> list[dict]:
    """
    布林带 (Bollinger Bands)
    klines: K线数据
    period: 中轨 MA 周期（默认20）
    num_std: 标准差倍数（默认2）
    返回: [{date, close, upper, middle, lower, bandwidth}, ...]
    
    价格触及 upper → 可能超买
    价格触及 lower → 可能超卖
    bandwidth 收窄 → 即将变盘
    """
    closes = [k["close"] for k in klines]
    result = []
    
    for i, k in enumerate(klines):
        if i < period - 1:
            result.append({"date": k["date"], "close": k["close"],
                           "upper": None, "middle": None, "lower": None,
                           "bandwidth": None})
            continue
        
        window = closes[i - period + 1:i + 1]
        ma = sum(window) / period
        std = (sum((x - ma) ** 2 for x in window) / period) ** 0.5
        upper = ma + num_std * std
        lower = ma - num_std * std
        
        result.append({
            "date": k["date"],
            "close": k["close"],
            "upper": round(upper, 4),
            "middle": round(ma, 4),
            "lower": round(lower, 4),
            "bandwidth": round((upper - lower) / ma * 100, 2) if ma else None,
        })
    return result

def financial_statements_eastmoney(secucode: str, statement: str = "balance",
                                     page_size: int = 200) -> list[dict]:
    """
    东财 datacenter 财报三表
    secucode: "AAPL.O" (NASDAQ) / "BABA.N" (NYSE) / "00700.HK" (港股)
    statement: "balance" / "income" / "cashflow"
    返回: [{ITEM_NAME, AMOUNT, YOY_RATIO, REPORT, REPORT_DATE, ...}, ...]
    
    注意: 数据按科目行展开，每行一个科目（如"流动资产合计"、"营业收入"等），
    同一期报告有多行。用 REPORT_DATE 分组可还原整张报表。
    """
    # 报表名映射（注意命名不统一：balance/income 用 F10，cashflow 用 SK）
    report_map = {
        "balance": {"us": "RPT_USF10_FN_BALANCE", "hk": "RPT_HKF10_FN_BALANCE"},
        "income":  {"us": "RPT_USF10_FN_INCOME",  "hk": "RPT_HKF10_FN_INCOME"},
        "cashflow": {"us": "RPT_USSK_FN_CASHFLOW", "hk": "RPT_HKSK_FN_CASHFLOW"},
    }
    
    market = "hk" if secucode.endswith(".HK") else "us"
    report_name = report_map[statement][market]
    
    return eastmoney_datacenter(
        report_name=report_name,
        filter_str=f'(SECUCODE="{secucode}")',
        page_size=page_size,
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )

def key_indicators_eastmoney(secucode: str, page_size: int = 4) -> list[dict]:
    """
    东财 GMAININDICATOR 关键财务指标（中文）
    secucode: "AAPL.O" (NASDAQ) / "BABA.N" (NYSE) / "00700.HK" (港股)
    page_size: 返回最近几期报告（默认4期=一年）
    返回: [{REPORT_DATE, OPERATE_INCOME, BASIC_EPS, ROE_AVG, ROA, ...}, ...]
    
    美股核心字段(49): OPERATE_INCOME(营收), GROSS_PROFIT(毛利), GROSS_PROFIT_RATIO(毛利率%),
      PARENT_HOLDER_NETPROFIT(归母净利), NET_PROFIT_RATIO(净利率%), BASIC_EPS, DILUTED_EPS,
      ROE_AVG(平均ROE%), ROA(%), CURRENT_RATIO(流动比率), DEBT_ASSET_RATIO(资产负债率%),
      OPERATE_INCOME_YOY(营收同比%), BASIC_EPS_YOY(EPS同比%)
    
    港股额外字段(75): BPS(每股净资产), ROIC(投入资本回报率), EQUITY_RATIO(产权比率),
      HOLDER_PROFIT(股东应占溢利), OCF_SALES(经营现金流/营收%), DPS_HKD(每股股息),
      DIVI_RATIO(股息率%), PER_NETCASH_OPERATE(每股经营现金流)
    """
    market = "hk" if secucode.endswith(".HK") else "us"
    report_name = f"RPT_{'HK' if market == 'hk' else 'US'}F10_FN_GMAININDICATOR"
    
    return eastmoney_datacenter(
        report_name=report_name,
        filter_str=f'(SECUCODE="{secucode}")',
        page_size=page_size,
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )

def key_statistics(symbol: str) -> dict:
    """
    Yahoo 关键财务指标
    symbol: "AAPL" (美股) 或 "0700.HK" (港股)
    返回: PE/PB/EV/EBITDA/利润率/目标价/ROE/Beta 等
    """
    data = yahoo_quote_summary(symbol, ["financialData", "defaultKeyStatistics", "summaryDetail"])
    
    fd = data.get("financialData", {})
    ks = data.get("defaultKeyStatistics", {})
    sd = data.get("summaryDetail", {})
    
    def _val(d, key):
        v = d.get(key, {})
        return v.get("raw") if isinstance(v, dict) else v
    
    return {
        # 价格相关
        "current_price": _val(fd, "currentPrice"),
        "target_high": _val(fd, "targetHighPrice"),
        "target_low": _val(fd, "targetLowPrice"),
        "target_mean": _val(fd, "targetMeanPrice"),
        "recommendation": fd.get("recommendationKey"),  # buy/hold/sell
        
        # 估值指标
        "trailing_pe": _val(sd, "trailingPE"),
        "forward_pe": _val(ks, "forwardPE"),
        "peg_ratio": _val(ks, "pegRatio"),
        "price_to_book": _val(ks, "priceToBook"),
        "enterprise_value": _val(ks, "enterpriseValue"),
        "ev_to_ebitda": _val(ks, "enterpriseToEbitda"),
        "ev_to_revenue": _val(ks, "enterpriseToRevenue"),
        
        # 盈利能力
        "profit_margin": _val(ks, "profitMargins"),
        "operating_margin": _val(fd, "operatingMargins"),
        "gross_margin": _val(fd, "grossMargins"),
        "return_on_equity": _val(fd, "returnOnEquity"),
        "return_on_assets": _val(fd, "returnOnAssets"),
        
        # 成长性
        "earnings_growth": _val(fd, "earningsGrowth"),
        "revenue_growth": _val(fd, "revenueGrowth"),
        
        # 风险
        "beta": _val(ks, "beta"),
        "short_ratio": _val(ks, "shortRatio"),
        
        # 股息
        "dividend_yield": _val(sd, "dividendYield"),
        "payout_ratio": _val(ks, "payoutRatio"),
        
        # 规模
        "market_cap": _val(sd, "marketCap"),
        "total_revenue": _val(fd, "totalRevenue"),
        "total_cash": _val(fd, "totalCash"),
        "total_debt": _val(fd, "totalDebt"),
    }

def analyst_estimates(symbol: str) -> dict:
    """
    Yahoo 分析师预期 — EPS预测/评级趋势/升降级历史
    symbol: "AAPL" 或 "0700.HK"
    """
    data = yahoo_quote_summary(symbol, [
        "earningsTrend", "recommendationTrend", "upgradeDowngradeHistory",
        "earnings", "earningsHistory",
    ])
    
    # EPS 趋势
    et = data.get("earningsTrend", {}).get("trend", [])
    eps_trend = []
    for t in et:
        eps_trend.append({
            "period": t.get("period"),
            "end_date": t.get("endDate"),
            "eps_estimate": t.get("earningsEstimate", {}).get("avg", {}).get("raw"),
            "eps_high": t.get("earningsEstimate", {}).get("high", {}).get("raw"),
            "eps_low": t.get("earningsEstimate", {}).get("low", {}).get("raw"),
            "revenue_estimate": t.get("revenueEstimate", {}).get("avg", {}).get("raw"),
            "num_analysts": t.get("earningsEstimate", {}).get("numberOfAnalysts", {}).get("raw"),
        })
    
    # 评级趋势 (最近4个月)
    rt = data.get("recommendationTrend", {}).get("trend", [])
    rating_trend = []
    for r_ in rt:
        rating_trend.append({
            "period": r_.get("period"),
            "strong_buy": r_.get("strongBuy"),
            "buy": r_.get("buy"),
            "hold": r_.get("hold"),
            "sell": r_.get("sell"),
            "strong_sell": r_.get("strongSell"),
        })
    
    # 升降级历史 (最近20条)
    udh = data.get("upgradeDowngradeHistory", {}).get("history", [])[:20]
    upgrades = []
    for u in udh:
        upgrades.append({
            "date": u.get("epochGradeDate"),
            "firm": u.get("firm"),
            "to_grade": u.get("toGrade"),
            "from_grade": u.get("fromGrade"),
            "action": u.get("action"),  # up/down/main/init
        })
    
    return {
        "eps_trend": eps_trend,
        "rating_trend": rating_trend,
        "upgrade_downgrade": upgrades,
    }

def institutional_holders(symbol: str) -> dict:
    """
    Yahoo 机构持仓 — 前10大机构 + 内部人持股比例
    symbol: "AAPL" 或 "0700.HK"
    """
    data = yahoo_quote_summary(symbol, ["institutionOwnership", "majorHoldersBreakdown"])
    
    # 持股比例总览
    mhb = data.get("majorHoldersBreakdown", {})
    def _val(d, key):
        v = d.get(key, {})
        return v.get("raw") if isinstance(v, dict) else v
    
    overview = {
        "insiders_pct": _val(mhb, "insidersPercentHeld"),
        "institutions_pct": _val(mhb, "institutionsPercentHeld"),
        "institutions_float_pct": _val(mhb, "institutionsFloatPercentHeld"),
        "institutions_count": _val(mhb, "institutionsCount"),
    }
    
    # 前10大机构
    io = data.get("institutionOwnership", {}).get("ownershipList", [])
    top_holders = []
    for h in io[:10]:
        top_holders.append({
            "name": h.get("organization"),
            "shares": _val(h, "position"),
            "value": _val(h, "value"),
            "pct_held": _val(h, "pctHeld"),
            "report_date": h.get("reportDate", {}).get("fmt") if isinstance(h.get("reportDate"), dict) else None,
        })
    
    return {"overview": overview, "top_holders": top_holders}

def financial_statements_yahoo(symbol: str,
                                 quarterly: bool = False) -> dict:
    """
    Yahoo 财报三表 — 结构化完整报表
    symbol: "AAPL" 或 "0700.HK"
    quarterly: False=年度, True=季度
    返回: {"income": [...], "balance": [...], "cashflow": [...]}
    """
    suffix = "Quarterly" if quarterly else ""
    data = yahoo_quote_summary(symbol, [
        f"incomeStatementHistory{suffix}",
        f"balanceSheetHistory{suffix}",
        f"cashflowStatementHistory{suffix}",
    ])
    
    def _extract(statements):
        result = []
        for stmt in statements:
            row = {}
            for k, v in stmt.items():
                if isinstance(v, dict) and "raw" in v:
                    row[k] = v["raw"]
                elif isinstance(v, dict) and "fmt" in v:
                    row[k] = v["fmt"]
                else:
                    row[k] = v
            result.append(row)
        return result
    
    income_key = f"incomeStatementHistory{suffix}"
    balance_key = f"balanceSheetHistory{suffix}"
    cashflow_key = f"cashflowStatementHistory{suffix}"
    
    return {
        "income": _extract(data.get(income_key, {}).get("incomeStatementHistory", [])),
        "balance": _extract(data.get(balance_key, {}).get("balanceSheetStatements", [])),
        "cashflow": _extract(data.get(cashflow_key, {}).get("cashflowStatements", [])),
    }

def fund_flow_daily(ticker_or_code: str, secid_prefix: int = 105,
                      limit: int = 100) -> list[dict]:
    """
    东财 push2his 日级资金流 — 主力/大单/中单/小单净流入
    美股: fund_flow_daily("AAPL", 105)  # NASDAQ
          fund_flow_daily("BABA", 106)  # NYSE
    港股: fund_flow_daily("00700", 116)
    返回: [{date, main_net, big_net, mid_net, small_net, main_pct, ...}, ...]
    """
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": f"{secid_prefix}.{ticker_or_code}",
        "klt": 101,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "lmt": limit,
    }
    r = requests.get(url, params=params, timeout=15)
    d = r.json()
    data = d.get("data")
    if not data or not data.get("klines"):
        return []
    
    result = []
    for line in data["klines"]:
        parts = line.split(",")
        # f51=日期, f52=主力净流入, f53=小单净流入, f54=中单净流入, f55=大单净流入, f56=超大单净流入
        result.append({
            "date": parts[0],
            "main_net": float(parts[1]),       # 主力净流入（元）
            "small_net": float(parts[2]),       # 小单净流入
            "mid_net": float(parts[3]),         # 中单净流入
            "big_net": float(parts[4]),         # 大单净流入
            "super_big_net": float(parts[5]),   # 超大单净流入
            "main_pct": float(parts[6]) if len(parts) > 6 and parts[6] else 0,  # 主力净占比%
        })
    return result

import re

from datetime import datetime, timezone, timedelta

CBOE_BASE = "https://cdn.cboe.com/api/global/delayed_quotes"

_OSI = re.compile(r"^(?P<root>[A-Z][A-Z0-9]*)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
                  r"(?P<cp>[CP])(?P<strike>\d{8})$")

def parse_osi(symbol: str) -> dict:
    """解析 OCC 合约代码 → {expiry, type, strike}；无法解析返回 {}"""
    m = _OSI.match(symbol)
    if not m:
        return {}
    g = m.groupdict()
    return {"expiry": f"20{g['y']}-{g['m']}-{g['d']}",
            "type": "call" if g["cp"] == "C" else "put",
            "strike": int(g["strike"]) / 1000.0}

def options_chain_cboe(ticker: str) -> dict:
    """
    CBOE 官方延时期权全链（仅美股）。
    返回 {"ticker","timestamp","spot","contracts":[{symbol,expiry,type,strike,bid,ask,
          volume,open_interest,iv,delta,gamma,vega,theta,rho,last_trade_price}]}
    """
    ticker = assert_us_ticker(ticker)
    raw = official_get(f"{CBOE_BASE}/options/{ticker}.json", as_json=True)
    data = raw.get("data") or {}
    contracts = []
    for o in data.get("options") or []:
        meta = parse_osi(o.get("option", ""))
        if not meta:
            continue
        contracts.append({
            "symbol": o["option"], **meta,
            "bid": o.get("bid"), "ask": o.get("ask"),
            "volume": o.get("volume") or 0,
            "open_interest": o.get("open_interest") or 0,
            "iv": o.get("iv"), "delta": o.get("delta"), "gamma": o.get("gamma"),
            "vega": o.get("vega"), "theta": o.get("theta"), "rho": o.get("rho"),
            "last_trade_price": o.get("last_trade_price"),
        })
    if not contracts:
        raise DataNotAvailable(f"{ticker} 未返回任何期权合约 —— 该标的可能无期权，"
                               f"或不在 CBOE 覆盖范围（CBOE 仅覆盖美股）")
    return {"ticker": ticker, "timestamp": raw.get("timestamp"),
            "spot": data.get("current_price"), "contracts": contracts}

try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:      # Windows 上 zoneinfo 可能缺 tzdata
    _ET_TZ = None

def _et_today() -> str:
    """
    美东今日 YYYY-MM-DD，用于 0DTE 判定。

    ⚠️ 必须区分 EDT(UTC-4) 与 EST(UTC-5)：硬编码 UTC-4 会让冬令时
    UTC 04:00–05:00 这一小时算成次日，导致 0DTE 选错到期日。
    """
    now = datetime.now(timezone.utc)
    if _ET_TZ is not None:
        return now.astimezone(_ET_TZ).strftime("%Y-%m-%d")
    # 无 tzdata 时的回退：按美国 DST 规则（3月第2个周日 ~ 11月第1个周日）自算
    y = now.year
    # 美国 DST 在**当地时间 2:00** 切换，换算成 UTC：
    #   开始 = 3月第2个周日 02:00 EST = 07:00 UTC
    #   结束 = 11月第1个周日 02:00 EDT = 06:00 UTC
    # 用 00:00 UTC 当切换点会在切换日凌晨那几小时取错偏移。
    mar8 = datetime(y, 3, 8, tzinfo=timezone.utc)
    dst_start = (mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
                 ).replace(hour=7)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = (nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
               ).replace(hour=6)
    offset = 4 if dst_start <= now < dst_end else 5
    return (now - timedelta(hours=offset)).strftime("%Y-%m-%d")

def filter_expiry(chain: dict, expiry: str = None, dte_max: int = None) -> list[dict]:
    """按到期日筛选。expiry='0DTE' 取当日到期；dte_max 取 N 天内到期"""
    cs = chain["contracts"]
    if expiry == "0DTE":
        return [c for c in cs if c["expiry"] == _et_today()]
    if expiry:
        return [c for c in cs if c["expiry"] == expiry]
    if dte_max is not None:
        today = datetime.strptime(_et_today(), "%Y-%m-%d")
        return [c for c in cs
                if 0 <= (datetime.strptime(c["expiry"], "%Y-%m-%d") - today).days <= dte_max]
    return cs

def unusual_activity(contracts: list[dict], min_volume: int = 500,
                     vol_oi_min: float = 1.0) -> list[dict]:
    """
    异动合约识别：成交量 >= min_volume 且 volume/open_interest >= vol_oi_min。
    vol/OI > 1 = 当日成交超过存量持仓 = 新建仓，是 options flow 的核心信号。
    """
    out = []
    for c in contracts:
        vol, oi = c["volume"], c["open_interest"]
        if vol < min_volume:
            continue
        ratio = vol / oi if oi > 0 else float("inf")
        if ratio >= vol_oi_min:
            out.append({**c, "vol_oi_ratio": round(ratio, 2) if oi > 0 else None})
    return sorted(out, key=lambda x: -x["volume"])

def chain_summary(contracts: list[dict]) -> dict:
    """链级聚合：put/call 量比与持仓比、成交量加权 IV、净 delta 敞口"""
    calls = [c for c in contracts if c["type"] == "call"]
    puts = [c for c in contracts if c["type"] == "put"]
    cv, pv = sum(c["volume"] for c in calls), sum(c["volume"] for c in puts)
    coi, poi = sum(c["open_interest"] for c in calls), sum(c["open_interest"] for c in puts)
    traded = [c for c in contracts if c["volume"] > 0 and c.get("iv")]
    tot_v = sum(c["volume"] for c in traded)
    vwiv = sum(c["iv"] * c["volume"] for c in traded) / tot_v if tot_v else None
    net_delta = sum((c.get("delta") or 0) * c["volume"] * 100 for c in contracts)
    return {"call_volume": cv, "put_volume": pv,
            "put_call_volume_ratio": round(pv / cv, 3) if cv else None,
            "call_oi": coi, "put_oi": poi,
            "put_call_oi_ratio": round(poi / coi, 3) if coi else None,
            "volume_weighted_iv": round(vwiv, 4) if vwiv else None,
            "net_delta_exposure_shares": round(net_delta),
            "contracts_total": len(contracts),
            "contracts_traded": len([c for c in contracts if c["volume"] > 0])}

def cboe_quote(ticker: str) -> dict:
    """CBOE 个股快照（含现价，可与期权链配合定 ATM）"""
    return official_get(f"{CBOE_BASE}/quotes/{assert_us_ticker(ticker)}.json",
                        as_json=True)["data"]

def options_chain(symbol: str, expiration: int = None) -> dict:
    """
    Yahoo 期权链 — calls + puts 完整数据（仅美股）
    symbol: "AAPL", "TSLA" 等美股 ticker
    ⚠️ 港股(如0700.HK)期权不在Yahoo覆盖范围，调用会返回空列表
    expiration: Unix timestamp (不传则返回最近到期日 + 所有到期日列表)
    返回: {"expiration_dates": [...], "calls": [...], "puts": [...]}
    """
    s = get_yahoo_session()
    params = {"crumb": s._crumb}
    if expiration:
        params["date"] = expiration
    
    r = s.get(f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}",
              params=params, timeout=15)
    r.raise_for_status()
    
    oc = r.json().get("optionChain", {}).get("result", [{}])[0]
    
    exp_dates = oc.get("expirationDates", [])
    options = oc.get("options", [{}])[0] if oc.get("options") else {}
    
    def _parse_options(opts):
        result = []
        for o in opts:
            def _val(key):
                v = o.get(key, {})
                return v.get("raw") if isinstance(v, dict) else v
            result.append({
                "strike": _val("strike"),
                "last_price": _val("lastPrice"),
                "bid": _val("bid"),
                "ask": _val("ask"),
                "volume": _val("volume"),
                "open_interest": _val("openInterest"),
                "implied_volatility": _val("impliedVolatility"),
                "in_the_money": o.get("inTheMoney"),
                "expiration": o.get("expiration", {}).get("fmt") if isinstance(o.get("expiration"), dict) else None,
                "contract_symbol": o.get("contractSymbol"),
            })
        return result
    
    return {
        "expiration_dates": exp_dates,  # Unix timestamps, 可依次传入获取各期
        "calls": _parse_options(options.get("calls", [])),
        "puts": _parse_options(options.get("puts", [])),
        "underlying_price": oc.get("quote", {}).get("regularMarketPrice"),
    }

import csv, io, re

from datetime import datetime

_YMD = re.compile(r"\d{4}-\d{2}-\d{2}")

_VOL_DAYS = {"VIX1D": 1, "VIX9D": 9, "VIX": 30, "VIX3M": 93, "VIX6M": 183, "VIX1Y": 365}

def vol_index_history(index: str = "VIX", start: str = None, end: str = None) -> list:
    """
    CBOE 波动率指数日线历史。
    index: VIX / VIX1D / VIX9D / VIX3M / VIX6M / VIX1Y / VVIX / SKEW / OVX / GVZ / VXN / RVX …
    start/end: YYYY-MM-DD（含）
    返回 [{date, open, high, low, close}]，旧的在前；
    VVIX / SKEW / OVX / GVZ 的文件只有收盘值，open/high/low 为 None
    代码不存在 → DataNotAvailable
    """
    idx = index.strip().upper() if isinstance(index, str) else ""
    if not re.fullmatch(r"[A-Z0-9]{2,8}", idx):
        raise ValueError(f"无效的指数代码: {index!r}")
    start, end = _check_range(start, end, _YMD)
    raw = official_get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{idx}_History.csv")
    reader = csv.reader(io.StringIO(raw))
    head = [h.strip().upper() for h in next(reader, [])]
    if not head or head[0] != "DATE":
        raise RuntimeError(f"CBOE {idx} 历史文件格式变了：{head[:5]}")
    ci = head.index("CLOSE") if "CLOSE" in head else (head.index(idx) if idx in head else len(head) - 1)
    oi, hi, li = (head.index(k) if k in head else None for k in ("OPEN", "HIGH", "LOW"))

    def f(p, i):
        if i is None or i >= len(p) or p[i].strip() == "":
            return None
        try:
            return float(p[i])
        except ValueError:
            return None

    out = []
    for p in reader:
        if not p or not p[0].strip():
            continue
        try:
            d = datetime.strptime(p[0].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if (start and d < start) or (end and d > end):
            continue
        c = f(p, ci)
        if c is None:
            continue
        out.append({"date": d, "open": f(p, oi), "high": f(p, hi), "low": f(p, li), "close": c})
    if not out:
        raise DataNotAvailable(f"CBOE {idx} 在所选区间没有数据")
    return out

def vix_term_structure(indices=("VIX9D", "VIX", "VIX3M", "VIX6M", "VIX1Y")) -> dict:
    """
    VIX 期限结构（各期限指数的最新收盘）。
    shape: contango = 期限越长越高（常态）；backwardation = 短端高于长端（恐慌时）；mixed = 其他
    返回 {"date","dates_aligned","points":[{index,days,date,close}],"shape","front_back_spread"}
    dates_aligned=False 表示各指数最新日期不一致（CBOE 各文件更新时间不同），比较时留意
    """
    if isinstance(indices, str):
        indices = [indices]
    names = []
    for ix in indices:
        ix = str(ix).strip().upper()
        if ix not in _VOL_DAYS:
            raise ValueError(f"{ix} 不是期限类指数，可选 {list(_VOL_DAYS)}")
        if ix not in names:
            names.append(ix)
    if len(names) < 2:
        raise ValueError("indices 至少给两个不同的期限")
    pts = []
    for ix in names:            # 先检查完参数再下载
        last = vol_index_history(ix)[-1]
        pts.append({"index": ix, "days": _VOL_DAYS[ix], "date": last["date"], "close": last["close"]})
    pts.sort(key=lambda x: x["days"])
    closes = [p["close"] for p in pts]
    if all(a < b for a, b in zip(closes, closes[1:])):
        shape = "contango"
    elif all(a > b for a, b in zip(closes, closes[1:])):
        shape = "backwardation"
    else:
        shape = "mixed"
    dates = {p["date"] for p in pts}
    return {"date": max(dates), "dates_aligned": len(dates) == 1, "points": pts, "shape": shape,
            "front_back_spread": round(closes[-1] - closes[0], 2)}

SEC_HEADERS = {"User-Agent": "SimonLin global-stock-data/1.0 (contact@example.com)"}

def sec_filings(cik: str, form_type: str = None) -> dict:
    """
    SEC EDGAR Filing 列表
    cik: CIK号（10位补零），如 "0000320193" (Apple)
         可通过 ticker_to_cik() 从 ticker 转换
    form_type: 筛选类型，如 "10-K", "10-Q", "8-K"（不传返回全部）
    返回: {"company_name": ..., "filings": [{form, date, accession_number, primary_document}, ...]}
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    r = requests.get(url, headers=SEC_HEADERS, timeout=15)
    r.raise_for_status()
    
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    
    filings = []
    for i in range(len(forms)):
        if form_type and forms[i] != form_type:
            continue
        filings.append({
            "form": forms[i],
            "date": dates[i],
            "accession_number": accessions[i],
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            "description": descriptions[i] if i < len(descriptions) else "",
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accessions[i].replace('-', '')}/{primary_docs[i]}" if i < len(primary_docs) and primary_docs[i] else "",
        })
    
    return {
        "company_name": data.get("name"),
        "cik": cik,
        "ticker": data.get("tickers", [""])[0] if data.get("tickers") else "",
        "filings": filings[:50],  # 最近50条
    }

def sec_xbrl_facts(cik: str, metrics: list[str] = None) -> dict:
    """
    SEC EDGAR XBRL 结构化财务数据
    cik: CIK号（10位补零）
    metrics: 要提取的指标名，如 ["RevenueFromContractWithCustomerExcludingAssessedTax",
             "NetIncomeLoss", "EarningsPerShareDiluted"]
             不传则返回所有可用指标名列表
    
    返回: {"company": ..., "metrics": {"Revenue": [{end, val, form, filed}, ...], ...}}
    """
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    r = requests.get(url, headers=SEC_HEADERS, timeout=15)
    r.raise_for_status()
    
    facts = r.json()
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    
    # 如果不传 metrics，返回所有可用指标
    if not metrics:
        available = []
        for k, v in us_gaap.items():
            label = v.get("label", k)
            units = list(v.get("units", {}).keys())
            available.append({"name": k, "label": label, "units": units})
        return {
            "company": facts.get("entityName"),
            "total_metrics": len(available),
            "available_metrics": available,
        }
    
    # 提取指定指标
    result = {}
    for metric_name in metrics:
        metric = us_gaap.get(metric_name, {})
        if not metric:
            result[metric_name] = []
            continue
        
        # 自动选择单位（USD 或 USD/shares）
        units = metric.get("units", {})
        unit_key = "USD" if "USD" in units else list(units.keys())[0] if units else None
        if not unit_key:
            result[metric_name] = []
            continue
        
        entries = units[unit_key]
        # 只取 10-K 和 10-Q
        filtered = [e for e in entries if e.get("form") in ("10-K", "10-Q")]
        result[metric_name] = [{
            "end": e.get("end"),
            "val": e.get("val"),
            "form": e.get("form"),
            "filed": e.get("filed"),
            "fy": e.get("fy"),
            "fp": e.get("fp"),
        } for e in filtered[-20:]]  # 最近20条
    
    return {
        "company": facts.get("entityName"),
        "metrics": result,
    }

def stock_search(keyword: str, count: int = 10) -> list[dict]:
    """
    东财股票搜索 — 支持中英文，返回代码+市场+中文名
    keyword: "AAPL" / "苹果" / "Tencent" / "00700" / "特斯拉"
    返回: [{code, name, mkt_num, market_name, security_type}, ...]
    
    mkt_num 即 push2/push2his 的 secid 前缀:
    105=NASDAQ, 106=NYSE, 107=美股ETF, 116=港股
    """
    url = "https://searchapi.eastmoney.com/api/suggest/get"
    params = {
        "input": keyword,
        "type": 14,  # 14=全球市场
        "token": "D43BF722C8E33BDC906FB84D85E326E8",
        "count": count,
    }
    r = requests.get(url, params=params, timeout=10)
    d = r.json()
    
    suggestions = d.get("QuotationCodeTable", {}).get("Data", [])
    result = []
    for s in suggestions:
        mkt = s.get("MktNum", "")
        # 只保留美股和港股
        if str(mkt) not in ("105", "106", "107", "116"):
            continue
        
        market_map = {"105": "NASDAQ", "106": "NYSE", "107": "US_OTHER", "116": "HK"}
        result.append({
            "code": s.get("Code"),
            "name": s.get("Name"),
            "mkt_num": int(mkt),
            "market_name": market_map.get(str(mkt), str(mkt)),
            "security_type": s.get("SecurityTypeName"),
        })
    return result

def stock_news(keyword: str, count: int = 10) -> list[dict]:
    """
    Yahoo Finance 新闻搜索
    keyword: 股票代码或关键词，如 "AAPL", "Tesla", "0700.HK"
    返回: [{title, publisher, link, publish_time, thumbnail}, ...]
    注意: 需要先获取 Yahoo cookie 才能调用，否则返回 400
    """
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    s.get("https://fc.yahoo.com", timeout=10)  # 获取 cookie
    
    url = "https://query2.finance.yahoo.com/v1/finance/search"
    params = {"q": keyword, "quotesCount": 0, "newsCount": count}
    r = s.get(url, params=params, timeout=10)
    r.raise_for_status()
    
    news = r.json().get("news", [])
    result = []
    for n in news:
        result.append({
            "title": n.get("title"),
            "publisher": n.get("publisher"),
            "link": n.get("link"),
            "publish_time": n.get("providerPublishTime"),
            "thumbnail": n.get("thumbnail", {}).get("resolutions", [{}])[0].get("url") if n.get("thumbnail") else None,
        })
    return result

_cik_cache = None

def ticker_to_cik(ticker: str) -> dict:
    """
    SEC EDGAR ticker → CIK 映射
    ticker: 如 "AAPL", "TSLA", "MSFT"
    返回: {"ticker": "AAPL", "cik": "0000320193", "company": "Apple Inc."}
    
    首次调用下载完整映射表(~10KB JSON, 10000+公司)并缓存。
    """
    global _cik_cache
    if not _cik_cache:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers=SEC_HEADERS, timeout=15)
        r.raise_for_status()
        _cik_cache = r.json()
    
    ticker_upper = ticker.upper()
    for _, v in _cik_cache.items():
        if v.get("ticker") == ticker_upper:
            cik_str = str(v["cik_str"]).zfill(10)
            return {
                "ticker": ticker_upper,
                "cik": cik_str,
                "company": v.get("title"),
            }
    return {}

def market_stock_list(market: str = "us_nasdaq", sort_field: str = "f3",
                       sort_desc: bool = True, page: int = 1,
                       page_size: int = 20) -> dict:
    """
    东财 push2 全市场股票列表 — 涨跌幅/成交量/成交额排名
    market: "us_nasdaq" (m:105), "us_nyse" (m:106), "hk" (m:116)
    sort_field: 排序字段
      f3=涨跌幅, f5=成交量, f6=成交额, f2=最新价, f7=振幅, f15=最高, f16=最低
    sort_desc: True=降序(默认), False=升序
    page/page_size: 分页（默认第1页，每页20条）
    返回: {"total": 5925, "stocks": [{code, name, price, change_pct, volume, ...}, ...]}
    
    典型用途:
    - 今日涨幅 TOP 20: market_stock_list("us_nasdaq", "f3", True)
    - 今日跌幅 TOP 20: market_stock_list("us_nasdaq", "f3", False)
    - 成交量 TOP 20: market_stock_list("hk", "f5", True)
    - 遍历全市场: 循环 page=1..N, 每页100条做筛选
    """
    market_map = {"us_nasdaq": "m:105", "us_nyse": "m:106", "us_etf": "m:107", "hk": "m:116"}
    fs = market_map.get(market, market)
    
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "fs": fs,
        "fields": "f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18",
        "pn": page,
        "pz": page_size,
        "fid": sort_field,
        "po": 1 if sort_desc else 0,
    }
    r = requests.get(url, params=params, timeout=15)
    d = r.json()
    data = d.get("data", {})
    
    total = data.get("total", 0)
    diff = data.get("diff", [])
    # 东财 push2 的 diff 有时是 list、有时是按序号为键的 dict（如 {"0":{...},"1":{...}}）。
    # 直接 for item in diff 遇到 dict 会拿到字符串键 → AttributeError，统一成列表。
    if isinstance(diff, dict):
        diff = list(diff.values())
    
    stocks = []
    for item in diff:
        stocks.append({
            "code": item.get("f12"),         # 股票代码
            "name": item.get("f14"),         # 中文名
            "price": item.get("f2"),         # 最新价(原始值, 需÷10^小数位)
            "change_pct": round(item["f3"] / 100, 2) if item.get("f3") is not None else None,  # 涨跌幅(%)
            "change_amount": item.get("f4"), # 涨跌额(原始值)
            "volume": item.get("f5"),        # 成交量(股)
            "amount": item.get("f6"),        # 成交额
            "amplitude": round(item["f7"] / 100, 2) if item.get("f7") is not None else None,  # 振幅(%)
            "high": item.get("f15"),         # 最高(原始值)
            "low": item.get("f16"),          # 最低(原始值)
            "open": item.get("f17"),         # 开盘(原始值)
            "prev_close": item.get("f18"),   # 昨收(原始值)
        })
    
    return {"total": total, "stocks": stocks}

from datetime import datetime, timedelta

def _recent_weekdays(days_back: int = 7) -> list[str]:
    d, out = datetime.now(), []
    while len(out) < days_back:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out

def short_volume_all(date: str = None, market: str = "CNMS") -> dict:
    """
    FINRA 全市场每日空头成交量。
    market: CNMS(合并全市场) / FNSQ(Nasdaq) / FNYX(NYSE) / FNRA(TRF)
    date: YYYYMMDD；不传则自动回退找最近有数据的交易日
    返回 {"date","market","count","data":{SYMBOL:{short,short_exempt,total,ratio}}}
    """
    for d in ([date] if date else _recent_weekdays(7)):
        try:
            raw = official_get(
                f"https://cdn.finra.org/equity/regsho/daily/{market}shvol{d}.txt")
        except DataNotAvailable:
            continue   # 该日无文件（非交易日/尚未发布），回退下一日
        # 其余异常（网络/限流/配置）直接抛出，不伪装成「没数据」
        rows = {}
        for line in raw.splitlines()[1:]:
            p = line.split("|")
            if len(p) < 5 or not p[1]:
                continue
            try:
                sv, se, tv = float(p[2]), float(p[3]), float(p[4])
            except ValueError:
                continue
            rows[p[1]] = {"short": sv, "short_exempt": se, "total": tv,
                          "ratio": round(sv / tv, 4) if tv else None}
        if rows:
            return {"date": d, "market": market, "count": len(rows), "data": rows}
    # 抛 DataNotAvailable 而非 RuntimeError：指定日期无数据时，
    # 调用方（如 short_volume_symbol 的多日循环）要能捕获并跳过这一天
    raise DataNotAvailable(f"未找到 {market} "
                           f"{'该日' if date else '近 7 个工作日'}的 Reg SHO 数据")

def short_volume_symbol(symbol: str, days: int = 5, market: str = "CNMS") -> list[dict]:
    """单只股票近 N 个交易日的空头成交占比时间序列"""
    out = []
    for d in _recent_weekdays(days * 2):
        if len(out) >= days:
            break
        try:
            snap = short_volume_all(date=d, market=market)
        except DataNotAvailable:
            continue
        rec = snap["data"].get(symbol.upper())
        if rec:
            out.append({"date": d, **rec})
    return out

def short_volume_ranking(snapshot: dict, min_total: float = 1_000_000,
                         top: int = 20) -> list[dict]:
    """空头占比排行（先按最小成交量过滤，避免小票噪音）"""
    rows = [{"symbol": s, **v} for s, v in snapshot["data"].items()
            if v["total"] >= min_total and v["ratio"] is not None]
    return sorted(rows, key=lambda x: -x["ratio"])[:top]

import calendar

from datetime import date as _date, datetime, timedelta

def _int_in(v, name: str, lo: int, hi: int) -> int:
    """整数参数检查：不是整数或越界 → ValueError（10.3 / 10.4 也用）"""
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} 要是 {lo}–{hi} 的整数，收到 {v!r}") from None
    if isinstance(v, bool) or not lo <= n <= hi or (isinstance(v, float) and v != n):
        raise ValueError(f"{name} 要是 {lo}–{hi} 的整数，收到 {v!r}")
    return n

def _si_anchor_dates(n: int) -> list:
    """最近 n 个结算锚点（每月 15 日、月末），新的在前"""
    t = _date.today()
    out, y, m = [], t.year, t.month
    while len(out) < n:
        for day in (calendar.monthrange(y, m)[1], 15):
            d = _date(y, m, day)
            if d <= t:
                out.append(d)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[:n]

def _si_parse(raw: str, d: str, only: str = None) -> dict:
    lines = raw.splitlines()
    if not lines:
        raise DataNotAvailable(f"FINRA 做空余额 {d} 文件为空")
    head = [h.strip().strip('"') for h in lines[0].split("|")]
    idx = {h: i for i, h in enumerate(head)}
    need = ("symbolCode", "currentShortPositionQuantity", "settlementDate")
    if any(k not in idx for k in need):
        raise RuntimeError(f"FINRA 做空余额 {d} 表头变了：{head[:6]}…")

    def col(p, k):
        i = idx.get(k)
        return p[i].strip().strip('"') if i is not None and i < len(p) else ""

    def num(s):
        try:
            return float(s) if s != "" else None
        except ValueError:
            return None

    rows, settle = {}, ""
    for line in lines[1:]:
        if only and only not in line:             # 只查一只时先粗筛，省去逐行拆分
            continue
        p = line.split("|")
        sym = col(p, "symbolCode")
        if only and sym != only:
            continue
        if not sym or sym in rows:                 # 实测同一文件无重复代码；万一有，保留第一条
            continue
        cur = num(col(p, "currentShortPositionQuantity"))
        if cur is None:
            continue
        settle = settle or col(p, "settlementDate")
        rows[sym] = {
            "name": col(p, "issueName"),
            "market": col(p, "marketClassCode"),   # 实测 7 种：NYSE / NNM（纳斯达克全球市场）/ SC（纳斯达克资本市场）/ ARCA / BZX / AMEX / OTC
            "short_interest": cur,
            "previous": num(col(p, "previousShortPositionQuantity")),
            "change": num(col(p, "changePreviousNumber")),
            "change_pct": num(col(p, "changePercent")),
            "avg_daily_volume": num(col(p, "averageDailyVolumeQuantity")),
            "days_to_cover": num(col(p, "daysToCoverQuantity")),
            "split": col(p, "stockSplitFlag") == "S",
            "revised": col(p, "revisionFlag") != "",
        }
    if not rows and not only:
        raise DataNotAvailable(f"FINRA 做空余额 {d} 文件没有可解析的行")
    return {"settlement_date": settle or f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "count": len(rows), "data": rows}

def _si_fetch(d: str, only: str = None) -> dict:
    raw = official_get(f"https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d}.csv")
    return _si_parse(raw, d, only)

def _si_resolve(anchor, only: str = None) -> dict:
    """从锚点日往前试 3 个工作日，找到实际结算日的文件（如 2024-01-15 马丁路德金日 → 01-12）"""
    d, tried = anchor, 0
    while tried < 3:
        if d.weekday() < 5:
            tried += 1
            try:
                return _si_fetch(d.strftime("%Y%m%d"), only)
            except DataNotAvailable:
                pass
        d -= timedelta(days=1)
    raise DataNotAvailable(f"{anchor} 前一周内没有 FINRA 做空余额文件（可能还没发布）")

def short_interest_all(date: str = None) -> dict:
    """
    FINRA 全美做空余额（未平仓空头存量），每月两期，覆盖交易所上市 + OTC。
    date: 结算日 YYYYMMDD；不传则自动找最近已发布的一期（结算后约 8 个工作日才发布）
    data 的键是 FINRA 写法：去掉点和横线（BRK.B → BRKB，优先股如 BFHPRA）
    返回 {"settlement_date","count","data":{SYMBOL:{name,market,short_interest,previous,
          change,change_pct,avg_daily_volume,days_to_cover,split,revised}}}
    """
    if date:
        try:
            datetime.strptime(date, "%Y%m%d")
            ok = len(date) == 8
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ValueError(f"date 要写成 YYYYMMDD 且是真实日期，如 20260915，收到 {date!r}")
        return _si_fetch(date)
    for anchor in _si_anchor_dates(3):
        try:
            return _si_resolve(anchor)
        except DataNotAvailable:
            continue
    raise DataNotAvailable("最近三个结算期都没找到 FINRA 做空余额文件")

def short_interest(symbol: str, periods: int = 1) -> list:
    """
    单只股票最近 N 期做空余额（新的在前）。每期要下载一个约 2.7MB 的全市场文件。
    symbol: BRK.B / BRK-B 都行（FINRA 文件里写成 BRKB）；优先股请按 FINRA 写法传，如 BAC-PL 写成 BACPRL
    periods: 1–24
    文件里查不到这个代码 → DataNotAvailable
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"symbol 要是股票代码字符串，收到 {symbol!r}")
    sym = assert_us_ticker(symbol.strip())
    code = sym.replace(".", "").replace("-", "")
    n = _int_in(periods, "periods", 1, 24)
    out = []
    for anchor in _si_anchor_dates(n + 2):
        if len(out) >= n:
            break
        try:
            snap = _si_resolve(anchor, only=code)
        except DataNotAvailable:
            continue            # 最新一期常常还没发布，跳过
        rec = snap["data"].get(code)
        if rec:
            out.append({"settlement_date": snap["settlement_date"], "symbol": sym, **rec})
    if not out:
        raise DataNotAvailable(f"FINRA 做空余额文件里没有 {code}（优先股要按 FINRA 写法传，如 BAC-PL 写成 BACPRL）")
    return out

def short_interest_ranking(snapshot: dict, by: str = "days_to_cover",
                           min_avg_volume: float = 500_000, top: int = 20,
                           include_otc: bool = False) -> list:
    """
    做空余额排行。by: days_to_cover（回补天数）/ short_interest（空头股数）/ change_pct（较上期变化%）
    min_avg_volume: 日均成交量下限，过滤成交稀少、回补天数畸高的小票
    include_otc: 默认去掉 OTC 场外股票（含早年文件里的 OTCBB；外国公司的场外代码常有极端值，会占满榜单）
    """
    if by not in ("days_to_cover", "short_interest", "change_pct"):
        raise ValueError("by 取 days_to_cover / short_interest / change_pct")
    top = _int_in(top, "top", 1, 100000)
    if not isinstance(snapshot, dict) or "data" not in snapshot:
        raise ValueError("snapshot 要传 short_interest_all() 的返回值")
    rows = [{"symbol": s, **v} for s, v in snapshot["data"].items()
            if (include_otc or not str(v["market"] or "").upper().startswith("OTC"))
            and (v["avg_daily_volume"] or 0) >= min_avg_volume and v[by] is not None]
    return sorted(rows, key=lambda x: -x[by])[:top]

_FORM_LABEL = {"4": "内部人交易", "8-K": "重大事件", "13F-HR": "机构持仓",
               "144": "限售股拟出售", "10-K": "年报", "10-Q": "季报",
               "SC 13D": "举牌(主动)", "SC 13G": "举牌(被动)", "S-1": "IPO注册"}

def daily_filings(date: str = None, forms: list[str] = None) -> dict:
    """
    EDGAR 每日申报流。date=YYYYMMDD，不传自动回退找最近有数据的日子。
    forms: 只保留这些表单类型，如 ["4","8-K","13F-HR"]；None=全部
    返回 {"date","total","by_form":{...},"filings":[{form,form_label,company,cik,date,url}]}
    """
    for d in ([date] if date else _recent_weekdays(7)):
        dt = datetime.strptime(d, "%Y%m%d")
        url = (f"https://www.sec.gov/Archives/edgar/daily-index/"
               f"{dt.year}/QTR{(dt.month - 1) // 3 + 1}/form.{d}.idx")
        try:
            raw = official_get(url)
        except DataNotAvailable:
            continue   # 该日无索引文件，回退下一日
        # 配置错误（SEC_CONTACT 未改）与网络错误在此直接抛出——
        # 否则会被 7 次循环吞掉，最终误报成「未找到 EDGAR 每日索引」
        lines = raw.splitlines()
        start = next((i + 1 for i, L in enumerate(lines) if L.startswith("---")), 11)
        filings, by_form = [], {}
        for L in lines[start:]:
            if len(L) < 98:
                continue
            form, company = L[:12].strip(), L[12:74].strip()
            cik, filed, path = L[74:86].strip(), L[86:98].strip(), L[98:].strip()
            if not form:
                continue
            by_form[form] = by_form.get(form, 0) + 1
            if forms and form not in forms:
                continue
            filings.append({"form": form, "form_label": _FORM_LABEL.get(form, ""),
                            "company": company, "cik": cik, "date": filed,
                            "url": f"https://www.sec.gov/Archives/{path}" if path else None})
        if by_form:
            return {"date": d, "total": sum(by_form.values()),
                    "by_form": dict(sorted(by_form.items(), key=lambda x: -x[1])),
                    "filings": filings}
    raise DataNotAvailable("未找到近 7 个工作日的 EDGAR 每日索引")

def fulltext_search(query: str, forms: str = None, date_from: str = None,
                    date_to: str = None, limit: int = 20) -> dict:
    """
    query: 加引号为精确短语，如 '"HBM4"'
    forms: "8-K" / "10-K" 等；date_from/to: YYYY-MM-DD
    """
    p = {"q": query, "from": 0, "size": limit}
    if forms:
        p["forms"] = forms
    if date_from:
        p["dateRange"], p["startdt"] = "custom", date_from
    if date_to:
        p["dateRange"], p["enddt"] = "custom", date_to
    j = official_get("https://efts.sec.gov/LATEST/search-index", params=p, as_json=True)
    hits = (j.get("hits") or {}).get("hits") or []
    return {"total": ((j.get("hits") or {}).get("total") or {}).get("value", 0),
            "results": [{"form": (h.get("_source") or {}).get("root_form"),
                         "company": ((h.get("_source") or {}).get("display_names") or [None])[0],
                         "filed": (h.get("_source") or {}).get("file_date"),
                         "id": h.get("_id")} for h in hits]}

import xml.etree.ElementTree as ET

from datetime import date as _date, timedelta

_sec_tickers = None

def _sec_cik(ticker: str = None, cik=None) -> str:
    """ticker 或 cik → 10 位 CIK 字符串"""
    global _sec_tickers
    if cik is not None and ticker is not None:
        raise ValueError("ticker 和 cik 只给一个")
    if cik is not None:
        s = str(cik).strip()
        if not s.isdigit() or len(s) > 10:
            raise ValueError(f"无效的 CIK: {cik!r}")
        return s.zfill(10)
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("ticker 和 cik 至少给一个")
    t = assert_us_ticker(ticker).replace(".", "-")      # SEC 代码表写 BRK-B
    if _sec_tickers is None:
        _sec_tickers = official_get("https://www.sec.gov/files/company_tickers.json", as_json=True)
    for v in _sec_tickers.values():
        if str(v.get("ticker", "")).upper() == t:
            return str(v["cik_str"]).zfill(10)
    raise ValueError(f"SEC 代码表里没有 {t}（只收录在 SEC 申报的公司）")

def _sec_rows(rec: dict) -> list:
    n = len(rec.get("form") or [])
    return [{k: (rec.get(k) or [None] * n)[i] for k in
             ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")}
            for i in range(n)]

def _sec_recent(cik10: str, since: str = None, until: str = None, stop=None) -> tuple:
    """
    公司名 + 申报列表（新的在前）。
    SEC 主文件只含最近一年或最近 1000 份（取多者）；大机构一年就有上万份，更早的在分页文件里。
    since（YYYY-MM-DD）早于主文件覆盖范围时，再读申报日期与 [since, until] 有交集的分页。
    stop：可选判断函数，已读到的行满足它就不再往更早的分页读（分页从新到旧读）。
    """
    j = official_get(f"https://data.sec.gov/submissions/CIK{cik10}.json", as_json=True)
    f = j.get("filings") or {}
    rows = _sec_rows(f.get("recent") or {})
    oldest = min((r["filingDate"] for r in rows if r["filingDate"]), default="9999")
    if since and since < oldest and not (stop and stop(rows)):
        # 分页标注的 filingTo 偶尔比实际最新一行早一两天，放宽 7 天再筛
        lo = (_date.fromisoformat(since) - timedelta(days=7)).isoformat()
        for pg in sorted(f.get("files") or [], key=lambda x: x.get("filingTo") or "", reverse=True):
            if (pg.get("filingTo") or "9999") >= lo and (not until or (pg.get("filingFrom") or "") <= until):
                rows += _sec_rows(official_get(f"https://data.sec.gov/submissions/{pg['name']}", as_json=True))
                if stop and stop(rows):
                    break
        rows.sort(key=lambda r: r["filingDate"] or "", reverse=True)
    return j.get("name"), rows

def _xml(text):
    """解析 XML 并去掉命名空间前缀（13F 信息表带命名空间，Form 4 不带）。
    传 bytes：SEC 的 XML 不声明字符集，按文本解码会把非英文名字解成乱码"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError(f"SEC XML 解析失败：{e}") from None
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    return root

def _xv(el, path: str):
    """取 <x><value>…</value></x> 或 <x>…</x> 的文本，没有返回 None"""
    if el is None:
        return None
    n = el.find(path)
    if n is None:
        return None
    v = n.find("value")
    s = ((v.text if v is not None else n.text) or "").strip()
    return s or None

def _xf(el, path: str):
    s = _xv(el, path)
    try:
        return float(s.replace(",", "")) if s is not None else None
    except ValueError:
        return None

def _xb(el, path: str):
    """布尔字段；元素不存在返回 None（旧版表格没有这一栏 ≠ 否）"""
    s = _xv(el, path)
    return None if s is None else s.lower() in ("1", "true")

_F4_CODES = {
    "P": "公开市场买入", "S": "公开市场卖出", "A": "公司授予", "M": "期权行权转股",
    "F": "缴税扣股", "G": "赠与", "D": "卖回给公司", "C": "衍生品转换", "X": "行权（价内）",
    "O": "行权（价外）", "J": "其他", "W": "继承", "I": "自主交易", "E": "衍生品到期作废",
    "H": "衍生品到期（多头）", "K": "互换", "L": "小额买入", "U": "要约收购", "V": "自愿提前申报",
    "Z": "存取有表决权信托",
}

def insider_trades(ticker: str = None, cik=None, days: int = 90, max_filings: int = 30,
                   codes=None) -> dict:
    """
    SEC Form 4 内部人交易明细（董事、高管、10% 大股东）。
    ticker 或 cik 二选一；days: 回看天数（按申报日，1–3650）。超出 SEC 主文件（约最近一年）时，会读完窗口内的全部历史分页
    （为了数准 form4_in_window，与 max_filings 无关），大机构回看几年可能要一两分钟；
    max_filings: 最多解析多少份 Form 4（1–200，每份一次请求，从新到旧）
    codes: 只保留这些交易代码，如 ("P","S") 或 "PS"；None = 全部（代码含义见 _F4_CODES）
    返回 {"cik","company","days","form4_in_window","filings_scanned","trades":[…],"summary":{…}}
    trades 每条：filed 申报日 / date 交易日 / owner / role / code / code_label / acquired_disposed（A 增 D 减）/
               shares / price / value / owned_after 交易后持股 / direct 是否直接持有 /
               filing_10b5_1 / url
    filing_10b5_1：这份申报勾了 10b5-1 计划选框（True/False）。选框是整份申报的，表示其中有交易按事先登记的
               计划进行，不一定是这一行（同一份里的行权、扣税行也会带上 True），具体哪笔看申报脚注；
               SEC 2023 年起启用的新版表格才有这一栏（实测 AAPL 从 2023-03 开始出现），更早为 None。
    一份申报有多个申报人时，owner 用 " / " 连接，role 为各人身份合并。
    form4_in_window / filings_scanned 也计入该 CIK 作为「申报人」的 Form 4（如伯克希尔买别家股票），这些份不产出交易行。
    只读 Form 4 的非衍生品表（股票本身的买卖、授予、行权转股、扣税）；期权等衍生品表和修订版 4/A 不读。
    summary 只统计公开市场买入 P / 卖出 S——这两类最能反映内部人自己掏钱的判断。
    """
    days = _int_in(days, "days", 1, 3650)
    max_filings = _int_in(max_filings, "max_filings", 1, 200)
    if codes is not None:
        if not isinstance(codes, (str, list, tuple, set, frozenset)):
            raise ValueError(f'codes 要写成 "PS" 或 ("P", "S") 这样，收到 {codes!r}')
        codes = {str(c).strip().upper() for c in codes}
        bad = sorted(codes - set(_F4_CODES))
        if not codes or bad:
            raise ValueError(f"codes 里有无效的交易代码 {bad}，可选 {sorted(_F4_CODES)}")
    cik10 = _sec_cik(ticker, cik)
    since = (_date.today() - timedelta(days=days)).isoformat()
    name, recent = _sec_recent(cik10, since)
    f4 = [r for r in recent if r["form"] == "4" and (r["filingDate"] or "") >= since]
    trades, company, scanned = [], name, 0
    for r in f4[:max_filings]:
        acc = r["accessionNumber"]
        doc = (r["primaryDocument"] or "").rsplit("/", 1)[-1]   # 去掉 xslF345X05/ 渲染前缀，取原始 XML
        if not acc or not doc.lower().endswith(".xml"):
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc.replace('-', '')}/{doc}"
        root = _xml(official_get(url, raw=True))
        scanned += 1
        issuer_cik = (root.findtext("issuer/issuerCik") or "").strip()
        if not issuer_cik.isdigit() or int(issuer_cik) != int(cik10):
            continue    # 该 CIK 在这份 Form 4 里是「申报人」而非「发行人」（如伯克希尔买别家股票）
        company = (root.findtext("issuer/issuerName") or company or "").strip()
        owners, roles = [], set()
        for o in root.findall("reportingOwner"):
            owners.append((o.findtext("reportingOwnerId/rptOwnerName") or "").strip())
            rel = o.find("reportingOwnerRelationship")
            if _xb(rel, "isDirector"):
                roles.add("董事")
            if _xb(rel, "isOfficer"):
                roles.add(_xv(rel, "officerTitle") or "高管")
            if _xb(rel, "isTenPercentOwner"):
                roles.add("10%大股东")
            if _xb(rel, "isOther"):
                roles.add(_xv(rel, "otherText") or "其他")
        plan = _xb(root, "aff10b5One")
        for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
            code = (t.findtext("transactionCoding/transactionCode") or "").strip()
            if codes and code not in codes:
                continue
            shares = _xf(t, "transactionAmounts/transactionShares")
            price = _xf(t, "transactionAmounts/transactionPricePerShare")
            trades.append({
                "filed": r["filingDate"], "accession": acc,
                "owner": " / ".join(o for o in owners if o), "role": "、".join(sorted(roles)),
                "date": _xv(t, "transactionDate"), "security": _xv(t, "securityTitle"),
                "code": code, "code_label": _F4_CODES.get(code, ""),
                "acquired_disposed": _xv(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                "shares": shares, "price": price,
                "value": round(shares * price, 2) if shares is not None and price is not None else None,
                "owned_after": _xf(t, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                "direct": _xv(t, "ownershipNature/directOrIndirectOwnership") == "D",
                "filing_10b5_1": plan, "url": url,
            })
    summ = {}
    for k, c in (("buy", "P"), ("sell", "S")):
        rows = [t for t in trades if t["code"] == c]
        summ[k + "_trades"] = len(rows)
        summ[k + "_shares"] = sum(t["shares"] or 0 for t in rows)
        summ[k + "_value"] = round(sum(t["value"] or 0 for t in rows), 2)
    return {"cik": cik10, "company": company, "days": days, "form4_in_window": len(f4),
            "filings_scanned": scanned, "trades": trades, "summary": summ}

import re

from datetime import date as _date, timedelta

def holdings_13f(cik, period: str = None, top: int = None) -> dict:
    """
    SEC 13F-HR 机构持仓明细。
    cik: 机构 CIK（如伯克希尔 1067983；不知道 CIK 可用 10.2 全文检索或 SEC 网站查）
    period: 报告期 YYYY-MM-DD（季末日，如 2026-06-30），不传取报告期最新的一季。
            不传时若最近一年没有 13F-HR，会往更早的分页翻，翻到第一份为止；从没交过 13F 的大公司要翻完全部分页
            （实测 JPMorgan Chase Financial 54 页、约 1 分钟）
    top: 只返回市值前 N 条；None = 全部
    2013 年二季度以前的 13F 是纯文本、没有 XML 持仓表 → DataNotAvailable
    返回 {"cik","manager","period","filed","accession","rows","positions","total_value",
          "reported_total","holdings":[{name,class,cusip,put_call,value,shares,share_type,weight}]}
    同一证券按 (cusip, put_call) 合并；value 单位统一为美元（2023-01-03 前申报的原文单位是千美元，已乘 1000）。
    reported_total 是封面页申报的合计，可与 total_value 对账（不一致说明有行没读到）。
    只读原始 13F-HR，修订版 13F-HR/A 不读。
    """
    if period is not None:
        try:
            pd = _date.fromisoformat(period) if isinstance(period, str) and len(period) == 10 else None
        except ValueError:
            pd = None
        if pd is None or period[5:] not in ("03-31", "06-30", "09-30", "12-31"):
            raise ValueError(f"period 要写成季末日 YYYY-MM-DD（03-31 / 06-30 / 09-30 / 12-31），收到 {period!r}")
        if pd > _date.today():
            raise ValueError(f"period {period} 还没到")
    if top is not None:
        top = _int_in(top, "top", 1, 100000)
    if cik is None:
        raise ValueError("cik 必填（如伯克希尔 1067983）")
    cik10 = _sec_cik(cik=cik)
    if period:      # 原始申报在季末后 45 天内，晚交的也不会晚到一年半以后
        name, recent = _sec_recent(cik10, period, (pd + timedelta(days=548)).isoformat())
    else:           # 主文件里没有 13F-HR（可能很久没交了）就往更早的分页翻，翻到第一份为止
        name, recent = _sec_recent(cik10, "1993-01-01",
                                   stop=lambda rs: any(x["form"] == "13F-HR" for x in rs))
    cands = [r for r in recent if r["form"] == "13F-HR"]
    if period:
        cands = [r for r in cands if r["reportDate"] == period]
    if not cands:
        raise DataNotAvailable(f"CIK {cik10} 没有{'报告期 ' + period + ' 的' if period else ''}13F-HR")
    # 按报告期取最新（晚交的旧季度不会被当成最新）；同一报告期取最晚申报的那份
    r = max(cands, key=lambda x: (x["reportDate"] or "", x["filingDate"] or ""))
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{r['accessionNumber'].replace('-', '')}"
    items = ((official_get(base + "/index.json", as_json=True).get("directory") or {})
             .get("item") or [])
    xmls = [i["name"] for i in items if str(i.get("name", "")).lower().endswith(".xml")
            and i["name"].lower() != "primary_doc.xml"]
    if not xmls:
        raise DataNotAvailable(f"{base} 没有 XML 持仓表（2013 年二季度以前的 13F 是纯文本格式，本工具不解析）")
    rows = None
    for x in xmls:                                  # 信息表文件名各家不同，找含 infoTable 的那个
        infos = _xml(official_get(f"{base}/{x}", raw=True)).findall(".//infoTable")
        if infos:
            rows = infos
            break
    if rows is None:
        raise RuntimeError(f"{base} 里没找到持仓明细表（infoTable），格式可能变了")
    mult = 1000.0 if (r["filingDate"] or "") < "2023-01-03" else 1.0
    agg = {}
    for it in rows:
        key = ((it.findtext("cusip") or "").strip(), (it.findtext("putCall") or "").strip())
        v = (_xf(it, "value") or 0.0) * mult
        sh = _xf(it, "shrsOrPrnAmt/sshPrnamt") or 0.0
        a = agg.get(key)
        if a is None:
            agg[key] = {"name": (it.findtext("nameOfIssuer") or "").strip(),
                        "class": (it.findtext("titleOfClass") or "").strip(),
                        "cusip": key[0], "put_call": key[1] or None,
                        "value": v, "shares": sh,
                        "share_type": (it.findtext("shrsOrPrnAmt/sshPrnamtType") or "").strip()}
        else:
            a["value"] += v
            a["shares"] += sh
    hold = sorted(agg.values(), key=lambda x: -x["value"])
    total = sum(h["value"] for h in hold)
    reported = None
    try:
        rv = _xml(official_get(f"{base}/primary_doc.xml", raw=True)).findtext(".//tableValueTotal")
        reported = float(rv) * mult if rv and rv.strip() else None
    except DataNotAvailable:
        pass
    for h in hold:
        h["weight"] = round(h["value"] / total, 4) if total else None
    return {"cik": cik10, "manager": name, "period": r["reportDate"], "filed": r["filingDate"],
            "accession": r["accessionNumber"], "rows": len(rows), "positions": len(hold),
            "total_value": total, "reported_total": reported,
            "holdings": hold[:top] if top else hold}

XBRL_TAGS = {
    "营业收入": "Revenues",
    "营业收入(合同)": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "净利润": "NetIncomeLoss",
    "研发费用": "ResearchAndDevelopmentExpense",
    "毛利": "GrossProfit",
    "经营利润": "OperatingIncomeLoss",
    "总资产": "Assets",
    "股东权益": "StockholdersEquity",
    "现金及等价物": "CashAndCashEquivalentsAtCarryingValue",
    "经营现金流": "NetCashProvidedByUsedInOperatingActivities",
    "资本开支": "PaymentsToAcquirePropertyPlantAndEquipment",
    "长期负债": "LongTermDebtNoncurrent",
    "稀释EPS": "EarningsPerShareDiluted",
}

_INSTANT_TAGS = {
    "Assets",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "LongTermDebtNoncurrent",
}

def _frame_period(year: int, quarter, instant: bool) -> str:
    """时点概念没有纯年度周期，年度请求落到 Q4I。"""
    if instant:
        return f"CY{year}Q{quarter}I" if quarter else f"CY{year}Q4I"
    return f"CY{year}Q{quarter}" if quarter else f"CY{year}"

def market_frame(tag: str, year: int, quarter: int = None, unit: str = "USD",
                 instant=None) -> dict:
    """
    全市场横截面。tag 可用 XBRL_TAGS 的中文键或原始 XBRL 标签。
    quarter: 1-4 季度；None 为年度
    instant: 是否为时点(资产负债表)概念。None=自动判定。

    自动判定逻辑：先按 _INSTANT_TAGS 猜一种周期形式，404 再换另一种重试。
    这样**任意原始 XBRL 标签**（Liabilities / InventoryNet / AssetsCurrent …）
    都能正确取到数，而不必把所有时点概念都枚举进 _INSTANT_TAGS。
    已知类型时显式传 instant=True/False 可省掉一次探测请求。
    """
    tag = XBRL_TAGS.get(tag, tag)
    guess = (tag in _INSTANT_TAGS) if instant is None else instant
    attempts = [guess] if instant is not None else [guess, not guess]

    last_err = None
    for is_instant in attempts:
        period = _frame_period(year, quarter, is_instant)
        try:
            j = official_get(
                f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json",
                timeout=45, as_json=True)
        except DataNotAvailable as e:      # 周期形式不对时 SEC 返回 404
            last_err = e
            continue
        rows = [{"cik": d.get("cik"), "entity": d.get("entityName"),
                 "value": d.get("val"), "end": d.get("end")} for d in j.get("data", [])]
        return {"tag": tag, "period": period, "unit": unit,
                "instant": is_instant, "count": len(rows), "data": rows}
    raise last_err or RuntimeError("SEC XBRL 尝试了两种口径都没有返回数据")

def frame_ranking(frame: dict, top: int = 20, ascending: bool = False) -> list[dict]:
    return sorted(frame["data"], key=lambda x: x["value"], reverse=not ascending)[:top]

def frame_screen(frame: dict, min_value: float = None,
                 max_value: float = None) -> list[dict]:
    """按数值区间筛选全市场公司"""
    out = frame["data"]
    if min_value is not None:
        out = [r for r in out if r["value"] >= min_value]
    if max_value is not None:
        out = [r for r in out if r["value"] <= max_value]
    return out

import csv, io

def treasury_yield_curve(year: int = None) -> list[dict]:
    """美国国债收益率曲线（每日，1M~30Y）。政府数据，S 级。返回 [0] 为最新一日"""
    year = year or datetime.now().year
    url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
           f"&field_tdr_date_value={year}&page&_format=csv")
    return list(csv.DictReader(io.StringIO(official_get(url))))

def cftc_cot(limit: int = 20, market_contains: str = None) -> list[dict]:
    """CFTC 持仓报告(COT)。政府数据，S 级"""
    q = {"$limit": limit, "$order": "report_date_as_yyyy_mm_dd DESC"}
    if market_contains:
        q["$where"] = f"upper(contract_market_name) like upper('%{market_contains}%')"
    return official_get("https://publicreporting.cftc.gov/resource/6dca-aqww.json",
                        params=q, as_json=True)

def earnings_calendar(date: str = None) -> dict:
    """Nasdaq 财报日历。date=YYYY-MM-DD，不传取今天。
    ⚠️ D 级：Nasdaq 条款禁止自动抓取，仅供个人学习研究，见「数据源合规分级」"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    j = official_get("https://api.nasdaq.com/api/calendar/earnings",
                     params={"date": date}, headers={"Accept": "application/json"},
                     as_json=True)
    rows = ((j.get("data") or {}).get("rows")) or []
    return {"date": date, "count": len(rows),
            "rows": [{"symbol": r.get("symbol"), "name": r.get("name"),
                      "time": r.get("time"), "eps_forecast": r.get("epsForecast"),
                      "market_cap": r.get("marketCap")} for r in rows]}

import csv, io, re

from datetime import date as _date, timedelta

def _num(x):
    """SDMX / 美联储 CSV 的空值有 ''、'NaN'、'ND'（No Data）几种写法，统一成 None"""
    if x is None:
        return None
    x = str(x).strip()
    if x in ("", "NaN", "ND", "NA", "null"):
        return None
    try:
        return float(x)
    except ValueError:
        return None

_PERIOD_D = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")

_PERIOD = re.compile(r"\d{4}(-\d{2}(-\d{2})?|-Q[1-4])?")

_YMD_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")

_FMT = {_PERIOD_D: "YYYY / YYYY-MM / YYYY-MM-DD", _YMD_ONLY: "YYYY-MM-DD",
        _PERIOD: "YYYY / YYYY-MM / YYYY-MM-DD（季度数据也认 YYYY-Q1）"}

def _check_range(start, end, pattern=_PERIOD):
    """
    校验 start / end，返回规整后的 (start, end)。可传字符串或 datetime.date。
    粒度可以不同（start="2024-12-20", end="2024" 合法），先后只比两者共同的前缀；
    季度先换成月份再比（start 的 Q3 按 7 月、end 的 Q3 按 9 月）。
    """
    out = []
    for name, v in (("start", start), ("end", end)):
        if isinstance(v, _date):                 # datetime 也是 date 的子类
            v = v.isoformat()[:10]
        if v is not None:
            if not (isinstance(v, str) and pattern.fullmatch(v)):
                raise ValueError(f"{name} 要写成 {_FMT.get(pattern, 'YYYY-MM-DD')}，收到 {v!r}")
            try:
                if len(v) == 10:
                    _date.fromisoformat(v)
                elif len(v) == 7 and "Q" not in v and not 1 <= int(v[5:]) <= 12:
                    raise ValueError
            except ValueError:
                raise ValueError(f"{name} 不是真实存在的日期：{v!r}") from None
        out.append(v)
    s, e = out
    if s and e:
        def _key(v, last):
            if "Q" not in v:
                return v
            q = int(v[-1])
            return f"{v[:4]}-{3 * q if last else 3 * q - 2:02d}"
        ks, ke = _key(s, False), _key(e, True)
        k = min(len(ks), len(ke))
        if ks[:k] > ke[:k]:
            raise ValueError(f"start {s} 晚于 end {e}")
    return s, e

def _period_params(last, start, end):
    """SDMX 接口：给了 start/end 就按区间取，否则取每个序列最近 last 个观测"""
    start, end = _check_range(start, end)
    if start or end:
        p = {}
        if start:
            p["startPeriod"] = start
        if end:
            p["endPeriod"] = end
        return p
    if int(last) < 1:
        raise ValueError("last 必须 ≥ 1")
    return {"lastNObservations": int(last)}

def _sdmx_csv(url: str, params: dict) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(official_get(url, params=params))))
    if not rows:     # 代码不存在时 BIS / ECB 都回 404（已转成 DataNotAvailable）；这里兜底 200 + 只有表头
        raise DataNotAvailable(f"{url[:90]} — 该代码或区间没有数据")
    return rows

_SDMX_PART = re.compile(r"^[A-Za-z0-9_.+\-]*$")

def policy_rates(areas=None, last: int = 1, start: str = None, end: str = None) -> list[dict]:
    """
    全球央行政策利率（BIS 国际清算银行，S 级，转载须注明来源 BIS）。

    areas: ISO 两位代码，如 "US" 或 ["US", "JP", "CN", "XM"]（XM = 欧元区）；
           None = 全部（2026-10 实测返回 48 个经济体，其中 38 个仍在更新；德法意等欧元区
           成员国的本国利率停在 1998 年、阿根廷停在 2025-07，欧元区统一看 XM；
           克罗地亚 2023 年改用欧元后 BIS 只剩空值，不返回）
    last : 每个经济体取最近 N 个观测（日频，含周末）；与 start/end（YYYY-MM-DD）二选一
    返回 [{area, name, date, rate, source}]，按经济体、日期升序

    ⚠️ 各国更新有滞后：实测 2026-10-11 澳洲仍是 09-24 的 4.35，而澳洲央行 09-30 起已是 4.60。
       一定要看 date，需要当天准确值请查该国央行。
    """
    if isinstance(areas, str):
        areas = [areas]
    for a in areas or []:
        if not (isinstance(a, str) and len(a) == 2 and a.isascii() and a.isalpha()):
            raise ValueError(f"经济体代码应为两位字母，如 'US' / 'XM'，收到 {a!r}")
    key = "D." + "+".join(a.upper() for a in (areas or []))
    params = _period_params(last, start, end)
    if "lastNObservations" in params:      # 部分国家周末/假日记为空值（菲律宾），多取几天再截
        params["lastNObservations"] += 10
    rows = _sdmx_csv(f"https://stats.bis.org/api/v1/data/WS_CBPOL/{key}/all",
                     dict(params, format="csv"))
    by_area = {}
    for r in rows:
        rate = _num(r.get("OBS_VALUE"))
        if rate is None:
            continue
        parts = (r.get("TITLE") or "").split(" - ")   # "Central bank policy rates - Japan - Daily - …"
        by_area.setdefault(r["REF_AREA"], []).append({
            "area": r["REF_AREA"], "name": parts[1].strip() if len(parts) > 2 else r["REF_AREA"],
            "date": r["TIME_PERIOD"], "rate": rate, "source": (r.get("SOURCE_REF") or "").strip()})
    out = []
    for a in sorted(by_area):
        obs = sorted(by_area[a], key=lambda x: x["date"])
        out.extend(obs if (start or end) else obs[-int(last):])
    if not out:
        raise DataNotAvailable(f"BIS 政策利率：{areas or '全部'} 在该区间没有数值")
    return out

def ecb_series(flow: str, key: str, last: int = 1, start: str = None,
               end: str = None) -> list[dict]:
    """
    ECB 通用取数（S 级：须注明 "Source: ECB statistics." 且不得改动数据）。
    flow / key 在 data.ecb.europa.eu 上查，如 flow="EXR", key="D.USD.EUR.SP00.A"。
    返回 [{key, date, value, title}]，按序列、日期升序
    """
    if not (flow.isalnum() and _SDMX_PART.match(key)):
        raise ValueError(f"非法的 ECB 数据集或序列代码：{flow!r} / {key!r}")
    rows = _sdmx_csv(f"https://data-api.ecb.europa.eu/service/data/{flow}/{key}",
                     dict(_period_params(last, start, end), format="csvdata"))
    return [{"key": r["KEY"], "date": r["TIME_PERIOD"], "value": _num(r.get("OBS_VALUE")),
             "title": r.get("TITLE")} for r in rows]

_ECB_FX_DEFAULT = ["USD", "JPY", "GBP", "CHF", "CNY", "HKD", "AUD", "CAD", "SGD", "KRW",
                   "INR", "SEK", "NOK", "DKK", "NZD", "BRL", "MXN", "ZAR", "TRY", "PLN"]

def ecb_fx(currencies=None, last: int = 1, start: str = None, end: str = None) -> list[dict]:
    """
    欧元参考汇率：1 欧元 = rate 单位外币（每个工作日 16:00 CET 前后公布）。
    currencies: "USD" 或 ["USD", "JPY"]；None = 20 种常用货币
    返回 [{currency, date, rate}]
    """
    if isinstance(currencies, str):
        currencies = [currencies]
    cur = [c.upper() for c in (currencies or _ECB_FX_DEFAULT)]
    if not all(len(c) == 3 and c.isascii() and c.isalpha() for c in cur):
        raise ValueError(f"币种应为三位字母代码，收到 {currencies!r}")
    rows = ecb_series("EXR", f"D.{'+'.join(cur)}.EUR.SP00.A", last, start, end)
    return [{"currency": r["key"].split(".")[2], "date": r["date"], "rate": r["value"]}
            for r in rows if r["value"] is not None]

def _pivot_by_date(rows: list[dict], name_of) -> list[dict]:
    table = {}
    for r in rows:
        if r["value"] is not None:
            table.setdefault(r["date"], {"date": r["date"]})[name_of(r["key"])] = r["value"]
    return [table[d] for d in sorted(table)]

def ecb_key_rates(last: int = 1, start: str = None, end: str = None) -> list[dict]:
    """
    ECB 三大政策利率（%）：deposit_facility 存款便利（现行主导利率）、
    main_refinancing 主要再融资、marginal_lending 边际贷款。

    ⚠️ 这组数据只在调息那天有记录，不是每天一条：每行 = 从 date 起生效的三个利率。
    last=N 是最近 N 次调整（只给 end 时是 end 之前的最近 N 次）；给 start 时，第一行是
    start 当天仍在生效的那次调整（所以它的 date 可能早于 start），之后是区间内的每次调整。
    返回日期升序
    """
    names = {"DFR": "deposit_facility", "MRR_FR": "main_refinancing", "MLFR": "marginal_lending"}
    url = "https://data-api.ecb.europa.eu/service/data/FM/B.U2.EUR.4F.KR.DFR+MRR_FR+MLFR.LEV"
    start, end = _check_range(start, end, _PERIOD_D)
    if int(last) < 1:
        raise ValueError("last 必须 ≥ 1")

    def _get(params):
        try:
            return _sdmx_csv(url, dict(params, format="csvdata"))
        except DataNotAvailable:
            return []

    if start:
        first = start + ("-01-01" if len(start) == 4 else "-01" if len(start) == 7 else "")
        before = (_date.fromisoformat(first) - timedelta(days=1)).isoformat()
        rows = _get({"endPeriod": before, "lastNObservations": 1})   # start 前各自最后一次
        rows += _get(dict({"startPeriod": start}, **({"endPeriod": end} if end else {})))
    else:
        # 多取几条：三个利率不一定同一天调，合并后最早几行可能缺项，裁掉
        rows = _get(dict({"lastNObservations": int(last) + 3}, **({"endPeriod": end} if end else {})))
    # 三个利率不一定同一天调（如 2019-09-18 只调了存款便利），按日期合并后向前填充
    by_date = {}
    for r in rows:
        v = _num(r.get("OBS_VALUE"))
        if v is not None:
            by_date.setdefault(r["TIME_PERIOD"], {})[names[r["KEY"].split(".")[6]]] = v
    out, cur = [], {}
    for d in sorted(by_date):
        cur.update(by_date[d])
        if len(cur) == len(names):         # 三项齐了才输出
            out.append(dict({"date": d}, **cur))
    if start:     # 从 start 当天仍在生效的那次开始（start 当天正好调息，就从这次开始）
        pre = [i for i, r in enumerate(out) if r["date"] <= first]
        out = out[pre[-1]:] if pre else out
    else:
        out = out[-int(last):]
    if not out:
        raise DataNotAvailable("ECB 关键利率：所选区间之前和之内都没有记录")
    return out

def ecb_yield_curve(curve: str = "AAA", tenors=("3M", "1Y", "2Y", "5Y", "10Y", "30Y"),
                    last: int = 1, start: str = None, end: str = None) -> list[dict]:
    """
    欧元区国债即期收益率曲线（%）。
    curve="AAA" 只含 AAA 评级国家（ECB 主口径）；curve="all" 含全部欧元区国家（收益率更高）
    tenors: 期限，如 "3M" "6M" "1Y" … "30Y"
    返回 [{date, "3M": …, "10Y": …}]，日期升序
    """
    code = {"AAA": "G_N_A", "ALL": "G_N_C"}.get(curve.upper())
    if not code:
        raise ValueError('curve 只能是 "AAA" 或 "all"')
    if isinstance(tenors, str):
        tenors = [tenors]
    tenors = [str(t).upper() for t in tenors]
    if not all(re.fullmatch(r"\d{1,2}[MY]", t) for t in tenors):
        raise ValueError(f"期限格式应为 3M / 10Y 这类，收到 {tenors!r}")
    key = f"B.U2.EUR.4F.{code}.SV_C_YM." + "+".join(f"SR_{t}" for t in tenors)
    rows = ecb_series("YC", key, last, start, end)
    pivot = _pivot_by_date(rows, lambda k: k.split(".")[-1][3:])
    return [dict({"date": p["date"]}, **{t: p[t] for t in tenors if t in p}) for p in pivot]

_NYFED_RATES = {"sofr": "secured", "tgcr": "secured", "bgcr": "secured", "sofrai": "secured",
                "effr": "unsecured", "obfr": "unsecured"}

def nyfed_rates(rate: str = "sofr", last: int = 5, start: str = None,
                end: str = None) -> list[dict]:
    """
    纽约联储参考利率（S 级；对外展示或分发须附纽约联储规定的声明，见合规分级）。
    rate: sofr 担保隔夜融资利率 / effr 有效联邦基金利率 / obfr 隔夜银行融资利率 /
          tgcr、bgcr 三方 / 广义回购利率 / sofrai SOFR 30·90·180 天平均与指数
    last 取最近 N 个工作日（1–999）；或给 start/end（YYYY-MM-DD）。返回原始字段，按日期升序：
      effectiveDate, percentRate, percentPercentile1/25/75/99, volumeInBillions
      （effr 另有 targetRateFrom/To 目标区间；sofrai 为 average30day/90day/180day, index）
    """
    kind = _NYFED_RATES.get(str(rate).lower())
    if not kind:
        raise ValueError(f"rate 只能是 {sorted(_NYFED_RATES)}，收到 {rate!r}")
    base = f"https://markets.newyorkfed.org/api/rates/{kind}/{rate.lower()}"
    start, end = _check_range(start, end, _YMD_ONLY)
    if start or end:
        params = {"startDate": start or "2000-01-01", "endDate": end or _date.today().isoformat()}
        j = official_get(f"{base}/search.json", params=params, as_json=True)
    else:
        if not 1 <= int(last) <= 999:
            raise ValueError("last 范围 1–999（纽约联储上限；更长请用 start/end）")
        j = official_get(f"{base}/last/{int(last)}.json", as_json=True)
    rows = j.get("refRates") or []
    if not rows:
        raise DataNotAvailable(f"纽约联储 {rate} 在该区间没有数据（SOFR 2018-04-02 起）")
    return sorted(rows, key=lambda r: r["effectiveDate"])

_FED_PACKAGES = {
    "H10": "60f32914ab61dfab590e0e470153e3ae",   # 美元对 23 种货币，日频，每周一发布上一周
    "H6": "798e2796917702a5f8423426ba7e6b42",    # M1 / M2 及其组成，月频
}

def fed_data(release: str = "H10", last: int = 10) -> dict:
    """
    美联储理事会公开数据（S 级，公有领域，须注明来源 Federal Reserve Board）。
    release: "H10" 汇率 / "H6" 货币存量；last: 最近 N 个观测（1–5000）
    返回 {"release", "series": {代码: {name, unit, multiplier, currency, quote?}},
          "rows": [{date, 代码: 数值或 None}]}，日期升序

    ⚠️ H10 的报价方向不统一：澳元、欧元、新西兰元、英镑是「1 单位外币 = 多少美元」
       （代码含 $US，quote 字段已标出），其余是「1 美元 = 多少外币」。
    ⚠️ H6 数值要乘 multiplier 才是美元（多数是 1e9，即十亿美元）。
    """
    rel = str(release).upper().replace(".", "")
    if rel not in _FED_PACKAGES:
        raise ValueError(f"release 只能是 {list(_FED_PACKAGES)}，收到 {release!r}")
    if not 1 <= int(last) <= 5000:
        raise ValueError("last 范围 1–5000（超过 5000 时美联储返回空文件）")
    text = official_get("https://www.federalreserve.gov/datadownload/Output.aspx", params={
        "rel": rel, "series": _FED_PACKAGES[rel], "lastobs": str(int(last)), "from": "", "to": "",
        "filetype": "csv", "label": "include", "layout": "seriescolumn", "type": "package"})
    lines = list(csv.reader(io.StringIO(text)))
    head = {r[0].strip().rstrip(":"): r[1:] for r in lines if r and not r[0][:1].isdigit()}
    if "Time Period" not in head:
        raise RuntimeError("美联储下载接口没有返回 CSV（地址规则可能已变，请到官网数据下载页核对）")
    codes = head["Time Period"]
    series = {}
    for i, c in enumerate(codes):
        meta = {"name": head.get("Series Description", [""] * len(codes))[i],
                "unit": head.get("Unit", [""] * len(codes))[i],
                "multiplier": _num(head.get("Multiplier", [""] * len(codes))[i]),
                "currency": head.get("Currency", [""] * len(codes))[i]}
        if rel == "H10":
            meta["quote"] = ("USD per 1 " + meta["currency"]) if "$US" in c \
                else (meta["currency"] + " per 1 USD")
        series[c] = meta
    rows = [dict({"date": r[0]}, **{c: _num(v) for c, v in zip(codes, r[1:])})
            for r in lines if r and r[0][:1].isdigit()]
    if not rows:
        raise DataNotAvailable(f"美联储 {rel} 没有返回数据行")
    return {"release": rel, "series": series, "rows": rows}

_FISCAL = {
    # 名称: (路径, 默认排序, 默认字段；None = 全部字段)
    "debt": ("/v2/accounting/od/debt_to_penny", "-record_date",
             "record_date,tot_pub_debt_out_amt,debt_held_public_amt,intragov_hold_amt"),
    "avg_rates": ("/v2/accounting/od/avg_interest_rates", "-record_date",
                  "record_date,security_type_desc,security_desc,avg_interest_rate_amt"),
    "auctions": ("/v1/accounting/od/auctions_query", "-auction_date",
                 "auction_date,issue_date,maturity_date,cusip,security_type,security_term,"
                 "reopening,offering_amt,high_yield,high_investment_rate,high_discnt_rate,int_rate,"
                 "bid_to_cover_ratio,total_tendered,total_accepted"),
    "upcoming": ("/v1/accounting/od/upcoming_auctions", "auction_date", None),
    "fx": ("/v1/accounting/od/rates_of_exchange", "-record_date",
           "record_date,country_currency_desc,exchange_rate,effective_date"),
}

def fiscal_data(dataset: str = "debt", filters: str = None, limit: int = 20,
                fields: str = None) -> list[dict]:
    """
    美国财政部 FiscalData 开放接口（S 级，官方明示无需注册或 token）。
    dataset:
      "debt"      每日联邦债务总额（tot_pub_debt_out_amt，美元）及公众 / 政府内部持有
      "avg_rates" 各类国债的月度平均利率（%）
      "auctions"  国债拍卖：中标利率、投标倍数。中长期国债（Note/Bond）看 high_yield；
                  短期国库券（Bill）看 high_investment_rate（等价收益率）和 high_discnt_rate
                  （贴现率），它的 high_yield 恒为 None。已公告未拍卖的结果字段全为 None
      "upcoming"  未来拍卖日程（自动只保留今天及以后）
      "fx"        财政部季度记账汇率（政府会计用，1 美元 = 多少外币，不是市场价）
    filters: 官方过滤语法，如 "security_type:eq:Note" 或
             "country_currency_desc:in:(Japan-Yen,Euro Zone-Euro)"；多个条件用逗号连接
    limit  : 返回条数（1–10000）；fields: 逗号分隔的字段名，覆盖默认字段
    返回 [dict]；数值保持官方的字符串格式（自己 float()），官方写 "null" 的转成 None
    """
    if dataset not in _FISCAL:
        raise ValueError(f"dataset 只能是 {list(_FISCAL)}，收到 {dataset!r}")
    path, sort, default_fields = _FISCAL[dataset]
    if not 1 <= int(limit) <= 10000:
        raise ValueError("limit 范围 1–10000")
    flt = [filters] if filters else []
    if dataset == "upcoming":
        flt.append(f"auction_date:gte:{_date.today().isoformat()}")
    params = {"sort": sort, "page[size]": int(limit)}
    if flt:
        params["filter"] = ",".join(flt)
    if fields or default_fields:
        params["fields"] = fields or default_fields
    j = official_get("https://api.fiscaldata.treasury.gov/services/api/fiscal_service" + path,
                     params=params, as_json=True)
    rows = j.get("data") or []
    if not rows:
        raise DataNotAvailable(f"FiscalData {dataset} 没有符合条件的数据")
    return [{k: (None if v == "null" else v) for k, v in r.items()} for r in rows]

import re, time

_TW = {
    "twse": {"base": "https://openapi.twse.com.tw/v1",
             "quotes": "/exchangeReport/STOCK_DAY_ALL", "valuation": "/exchangeReport/BWIBBU_ALL",
             "revenue": "/opendata/t187ap05_L", "profile": "/opendata/t187ap03_L",
             "margin": "/exchangeReport/MI_MARGN", "exdividend": "/exchangeReport/TWT48U_ALL",
             "dividend": "/opendata/t187ap45_L"},
    "tpex": {"base": "https://www.tpex.org.tw/openapi/v1",
             "quotes": "/tpex_mainboard_daily_close_quotes",
             "valuation": "/tpex_mainboard_peratio_analysis",
             "revenue": "/mopsfin_t187ap05_O", "profile": "/mopsfin_t187ap03_O",
             "margin": "/tpex_mainboard_margin_balance", "exdividend": "/tpex_exright_prepost"},
    # 柜买的股利分派表 mopsfin_t187ap39_O 实测停在 2021 年（2026-10-11），不收
}

_TW_QUOTE = {"code": ("Code", "SecuritiesCompanyCode"), "name": ("Name", "CompanyName"),
             "open": ("OpeningPrice", "Open"), "high": ("HighestPrice", "High"),
             "low": ("LowestPrice", "Low"), "close": ("ClosingPrice", "Close"),
             "change": ("Change", "Change"), "volume": ("TradeVolume", "TradingShares"),
             "turnover": ("TradeValue", "TransactionAmount"), "trades": ("Transaction", "TransactionNumber")}

_TW_VAL = {"code": ("Code", "SecuritiesCompanyCode"), "name": ("Name", "CompanyName"),
           "pe": ("PEratio", "PriceEarningRatio"), "pb": ("PBratio", "PriceBookRatio"),
           "dividend_yield": ("DividendYield", "YieldRatio")}

_TW_CODE_KEYS = ("Code", "SecuritiesCompanyCode", "公司代號", "股票代號")

_tw_cache = {}

def _roc_date(s):
    """民国日期 1151008 → 2026-10-08；11509 → 2026-09；其他原样返回"""
    s = (s or "").strip()
    if s.isdigit() and len(s) in (7, 5):
        y = int(s[:3]) + 1911
        return f"{y}-{s[3:5]}-{s[5:]}" if len(s) == 7 else f"{y}-{s[3:5]}"
    return s or None

def _tw_num(s):
    s = (s or "").strip().replace(",", "")
    if s in ("", "-", "--", "N/A", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None

def _tw_rows(market: str, dataset: str) -> list:
    if market not in _TW:
        raise ValueError("market 取 twse（上市）或 tpex（上柜）")
    if dataset not in _TW[market] or dataset == "base":
        raise ValueError(f"{market} 的 dataset 可选 {[k for k in _TW[market] if k != 'base']}")
    url = _TW[market]["base"] + _TW[market][dataset]
    hit = _tw_cache.get(url)
    if hit and time.time() - hit[0] < 600:          # 全市场快照一天一更，10 分钟内复用
        return hit[1]
    for attempt in range(2):
        try:
            rows = official_get(url, timeout=60, as_json=True)
            break
        except RuntimeError as e:
            if attempt or "请求失败" not in str(e):  # 只对连接中断类错误重试一次
                raise
    if not isinstance(rows, list) or not rows:
        raise DataNotAvailable(f"{market} {dataset} 返回空")
    _tw_cache[url] = (time.time(), rows)
    return rows

def tw_market(dataset: str = "quotes", market: str = "twse", code: str = None) -> list:
    """
    台湾证交所（twse 上市）/ 柜买中心（tpex 上柜）全市场最新一日数据。只有最新一期，没有历史。
    dataset: quotes 行情 / valuation 本益比·殖利率·股价净值比 / revenue 月营收 /
             profile 公司基本资料 / margin 融资融券 / exdividend 除权除息预告 /
             dividend 董事会股利决议（仅 twse）
    code: 只要某一只（如 "2330"）；None = 全市场
    quotes、valuation 统一成英文字段并转成数字（date 转成公历）；当天没有收盘价的 close / change 为 None
    （官方文件偶有成交量不为 0 但价格栏为空的行，照原样保留）；
    其余保留官方原字段（多为中文）、值为字符串，日期为民国纪年（可用 _roc_date 转换）。
    """
    c = None if code is None else str(code).strip().upper()
    if c == "":
        raise ValueError("code 不能为空；要全市场请不传 code")
    rows = _tw_rows(market, dataset)
    k = 0 if market == "twse" else 1
    if c is not None:
        rows = [r for r in rows if any(str(r.get(x, "")).strip().upper() == c for x in _TW_CODE_KEYS)]
    if dataset not in ("quotes", "valuation"):
        return [dict(r) for r in rows]          # 复制一份，调用方改动不会污染 10 分钟缓存
    spec = _TW_QUOTE if dataset == "quotes" else _TW_VAL
    out = []
    for r in rows:
        o = {"date": _roc_date(r.get("Date")), "market": market}
        for nk, src in spec.items():
            v = r.get(src[k])
            o[nk] = (v or "").strip() if nk in ("code", "name") else _tw_num(v)
        if dataset == "quotes" and o["close"] is None:
            o["change"] = None                  # 没有收盘价：官方涨跌栏写 0，不代表持平
        out.append(o)
    return out

def tw_stock(code: str) -> dict:
    """
    单只台股最新行情 + 估值（自动判断上市 / 上柜）。code: 2330 / 2330.TW / 6488.TWO
    估值字段为 None 表示官方当天没给值（如亏损股没有本益比）
    """
    c = str(code).strip().upper() if isinstance(code, (str, int)) and not isinstance(code, bool) else ""
    for suf in (".TWO", ".TW"):
        if c.endswith(suf):
            c = c[:-len(suf)]
            break
    if not re.fullmatch(r"[0-9][0-9A-Z]{3,5}", c):
        raise ValueError(f"无效的台股代码: {code!r}（例：2330、6488）")
    for m in ("twse", "tpex"):
        q = tw_market("quotes", m, c)
        if q:
            v = tw_market("valuation", m, c)
            out = dict(q[0])
            for kk in ("pe", "pb", "dividend_yield"):
                out[kk] = v[0][kk] if v else None
            out["valuation_date"] = v[0]["date"] if v else None
            return out
    raise DataNotAvailable(f"上市、上柜当日行情里都没有 {c}（可能停牌、已下市或代码有误）")

import csv, io, json, re

from datetime import datetime, timedelta

_EIA_PRICE = {
    "WTI": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=D",
    "BRENT": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RBRTE&f=D",
    "HENRY_HUB": "https://www.eia.gov/dnav/ng/hist/rngwhhdD.htm",   # 天然气走 LeafHandler 会 404
}

_EIA_ROW = re.compile(r"<td class='B6'>(.*?)</td>(.*?)</tr>", re.S)

_EIA_CELL = re.compile(r"<td class='B3'>(.*?)</td>", re.S)

_EIA_WEEK = re.compile(r"(\d{4})\s+([A-Z][a-z]{2})-\s*(\d{1,2})")

def _eia_num(s):
    s = re.sub(r"<[^>]+>|&nbsp;", "", s or "").strip().replace(",", "")
    try:
        return float(s) if s not in ("", "-", "--", "NA", "W") else None
    except ValueError:
        return None

def energy_prices(series: str = "WTI", start: str = None, end: str = None) -> list:
    """
    EIA 能源现货日价（数据由 EIA 转载自 Refinitiv，B 级，见上方合规说明）。
    series: WTI / BRENT（美元/桶）/ HENRY_HUB（美元/百万英热）
    start/end: YYYY-MM-DD（含）
    返回 [{date, price}]，旧的在前（2020-04-20 WTI 为 -36.98，负价是真实值）
    """
    s = series.strip().upper() if isinstance(series, str) else ""
    if s not in _EIA_PRICE:
        raise ValueError(f"series 可选 {list(_EIA_PRICE)}")
    start, end = _check_range(start, end, _YMD)
    html = official_get(_EIA_PRICE[s])
    out = []
    for label, cells in _EIA_ROW.findall(html):
        m = _EIA_WEEK.search(label.replace("&nbsp;", " "))
        if not m:
            continue
        d0 = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%Y %b %d").date()
        monday = d0 - timedelta(days=d0.weekday())      # 首周可能从周四开始，按周一对齐 5 个格子
        for i, v in enumerate(_EIA_CELL.findall(cells)[:5]):
            p = _eia_num(v)
            if p is None:
                continue
            d = (monday + timedelta(days=i)).isoformat()
            if (start and d < start) or (end and d > end):
                continue
            out.append({"date": d, "price": p})
    if not out:
        raise DataNotAvailable(f"EIA {s} 在所选区间没有数据")
    return out

def _eia_week(h: str):
    try:
        return datetime.strptime(h.strip(), "%m/%d/%y").strftime("%Y-%m-%d")
    except ValueError:
        return None

def energy_inventories() -> dict:
    """
    EIA 最新一周库存。只有最新一周和对比值，没有历史。
    petroleum: 每行 {item, value, week_ago, change, change_pct, year_ago, change_vs_year_ago, pct_vs_year_ago}，
               单位百万桶（周度石油状况报告 WPSR 表 1）
    natural_gas: 每行 {region, value, week_ago, year_ago, net_change, five_year_avg,
                 pct_vs_year_ago, pct_vs_five_year_avg}，单位十亿立方英尺 Bcf（周度天然气储量报告 WNGSR）
    """
    rd = list(csv.reader(io.StringIO(
        official_get("https://ir.eia.gov/wpsr/table1.csv").lstrip("﻿"))))
    if not rd or len(rd[0]) < 8 or rd[0][0].strip() != "STUB_1":
        raise RuntimeError(f"EIA WPSR table1.csv 格式变了：{rd[0][:4] if rd else '空'}")
    head = rd[0]
    pet = []
    for p in rd[1:]:
        if not p or p[0].strip() == "STUB_1":      # 文件后半段是另一张表（供需，千桶/日），不读
            break
        v = [_eia_num(x) for x in p[1:8]] + [None] * 7
        pet.append({"item": p[0].strip(), "value": v[0], "week_ago": v[1], "change": v[2],
                    "change_pct": v[3], "year_ago": v[4], "change_vs_year_ago": v[5],
                    "pct_vs_year_ago": v[6]})
    if not pet:
        raise DataNotAvailable("EIA WPSR table1.csv 没有库存行")
    try:
        g = json.loads(official_get("https://ir.eia.gov/ngs/wngsr.json").lstrip("﻿"))
    except ValueError as exc:
        raise RuntimeError(f"EIA wngsr.json 无法解析: {exc}") from exc
    gas = []
    for se in g.get("series") or []:
        data = {d[0]: d[1] for d in (se.get("data") or []) if isinstance(d, list) and len(d) == 2}
        calc = se.get("calculated") or {}
        gas.append({"region": se.get("name"), "value": data.get(g.get("current_week")),
                    "week_ago": data.get(g.get("week_ago")), "year_ago": data.get(g.get("year_ago")),
                    "net_change": calc.get("net_change"), "five_year_avg": calc.get("5yr-avg"),
                    "pct_vs_year_ago": calc.get("pct-change_yrago"),
                    "pct_vs_five_year_avg": calc.get("pct-chg_5yr-avg")})
    return {"petroleum": {"week": _eia_week(head[1]), "week_ago": _eia_week(head[2]),
                          "year_ago": _eia_week(head[5]), "unit": "million barrels", "rows": pet},
            "natural_gas": {"week": g.get("current_week"), "release_date": (g.get("release_date") or "")[:11],
                            "unit": "bcf", "rows": gas}}

import copy, re, time

import xml.etree.ElementTree as ET

from datetime import date as _date, datetime, timedelta

_BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"

_BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"

_BLS_ID = re.compile(r"[A-Z0-9_#\-]{2,45}")

_BLS_CITE = ("Source: U.S. Bureau of Labor Statistics, retrieved {d}. BLS.gov cannot vouch for the data "
             "or analyses derived from these data after the data have been retrieved from BLS.gov.")

_BLS_TTL = 3600

_bls_cache = {}

def _fixed_get(url: str, **kw):
    """本层的地址都是固定的：404 说明地址变了，不是「这一年没数据」，所以改报 RuntimeError"""
    try:
        return official_get(url, **kw)
    except DataNotAvailable as e:
        raise RuntimeError(f"{url} 返回「不存在」，官方可能换了地址：{e}") from e

def _bls_date(year, period):
    """M01–M12 → 2026-09；Q01–Q04 → 2026-Q3；S01/S02 → 2026-H1；A01 → 2026。
    年均值（M13 / Q05 / S03）返回 None；认不出的写法报 RuntimeError，不悄悄丢掉"""
    p, y = str(period or ""), str(year or "")
    if p in ("M13", "Q05", "S03"):
        return None
    n = int(p[1:]) if re.fullmatch(r"\d{4}", y) and re.fullmatch(r"[MQSA]\d{2}", p) else 0
    if p[:1] == "M" and 1 <= n <= 12:
        return f"{y}-{n:02d}"
    if p[:1] == "Q" and 1 <= n <= 4:
        return f"{y}-Q{n}"
    if p[:1] == "S" and n in (1, 2):
        return f"{y}-H{n}"
    if p == "A01" and n:
        return y
    raise RuntimeError(f"BLS 返回了认不出的期次（year={year!r}, period={period!r}），接口格式可能变了")

def _bls_value(v, where: str):
    """BLS 的数：只有 "-"（这一期没有数，如 2025 年 10 月停摆）算 None；别的写法说明格式变了，不能当成 None"""
    t = str(v if v is not None else "").strip()
    if t == "-":
        return None
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        raise RuntimeError(f"BLS 返回了读不懂的数 {v!r}（{where}），接口格式可能变了")
    return float(t)

def _bls_parse(series_json) -> list:
    rows, seen = [], set()
    sid = series_json.get("seriesID")
    for d in series_json["data"]:
        if not isinstance(d, dict):
            raise RuntimeError(f"BLS 返回的 {sid} 数据格式变了")
        dt = _bls_date(d.get("year"), d.get("period"))
        if dt is None:
            continue
        if dt in seen:                         # 同一期出现两次：不知道该信哪一个
            raise RuntimeError(f"BLS 返回的 {sid} 里 {dt} 出现了两次")
        seen.add(dt)
        notes = [f for f in (d.get("footnotes") or []) if isinstance(f, dict) and (f.get("code") or f.get("text"))]
        rows.append({"date": dt, "value": _bls_value(d.get("value"), f"{sid} {dt}"),
                     "preliminary": any(f.get("code") in ("P", "I", "U") for f in notes),
                     "footnotes": [(f"{f['code']}: " if f.get("code") else "") + (f.get("text") or "")
                                   for f in notes]})
    return rows

def _bls_request(ids: list, y0: int, y1: int):
    """一次 POST（≤25 个序列、≤10 年）。返回 (结果, 是否来自缓存)"""
    key = (tuple(ids), y0, y1)
    hit = _bls_cache.get(key)
    if hit and time.time() - hit[0] < _BLS_TTL:
        return copy.deepcopy(hit[1]), True
    j = _fixed_get(_BLS_API, json_body={"seriesid": list(ids), "startyear": str(y0), "endyear": str(y1)},
                     as_json=True, timeout=60)
    status = j.get("status")
    msgs = [str(m).strip() for m in (j.get("message") or [])]
    if status == "REQUEST_FAILED_INVALID_PARAMETERS":
        raise ValueError("BLS 拒绝参数：" + "；".join(msgs))
    if status != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS 返回 {status}：{'；'.join(msgs) or '没有说明'}"
                           f"（免 key 版每天 25 次，用完要等第二天）")
    if any("reduced to the system-allowed limit" in m for m in msgs):
        raise RuntimeError("BLS 截短了这次请求，免 key 版上限可能变了，请核对官方 FAQ：" + "；".join(msgs))
    out = {"series": {}, "missing": [], "no_data": {}, "notes": [], "retrieved": _date.today().isoformat()}
    for m in msgs:
        bad = re.fullmatch(r"(?:Series does not exist|Invalid Series) for Series (\S+?)[.!]?", m)
        gap = re.fullmatch(r"No Data Available for Series (\S+) Year: (\d{4})", m)
        if bad:
            out["missing"].append(bad.group(1).upper())
        elif gap:
            out["no_data"].setdefault(gap.group(1).upper(), []).append(int(gap.group(2)))
        elif not m.startswith("Unable to get Catalog Data"):   # v1 不提供序列说明，JOLTS 等会带这句，无害
            out["notes"].append(m)
    res = j.get("Results")
    lst = res.get("series") if isinstance(res, dict) else None
    if not isinstance(lst, list) or not all(isinstance(s, dict) and isinstance(s.get("data"), list) for s in lst):
        raise RuntimeError("BLS 说成功，但返回里没有 Results.series 或格式变了；这次不缓存，请稍后重试")
    got = [str(s.get("seriesID", "")).upper() for s in lst]
    if sorted(got) != sorted(ids):             # 必须正好是请求的那几个、各一次：漏回、多回、重复都不能当成正常结果，也不缓存
        lost = [i for i in ids if i not in got]
        extra = sorted({g for g in got if g not in ids or got.count(g) > 1})
        raise RuntimeError(f"BLS 返回的序列和请求的对不上（漏回 {lost}，多出或重复 {extra}）；这次不缓存，请稍后重试")
    for sid, s in zip(got, lst):
        out["series"][sid] = _bls_parse(s)
    for i in ids:
        if not out["series"][i] and i not in out["missing"] and i not in out["no_data"]:
            out["notes"].append(f"{i} 在 {y0}–{y1} 年没有返回数据，BLS 也没说明原因")
    _bls_cache[key] = (time.time(), out)
    return copy.deepcopy(out), False

def _year_spans(years: list) -> str:
    ys, spans = sorted(set(years)), []
    for y in ys:
        if spans and y == spans[-1][1] + 1:
            spans[-1][1] = y
        else:
            spans.append([y, y])
    return "、".join(str(a) if a == b else f"{a}–{b}" for a, b in spans)

def bls_series(series_ids, start_year: int = None, end_year: int = None, max_queries: int = 5) -> dict:
    """
    BLS 公共 API v1（免注册、免 key，S 级；对外展示须注明来源与取数日期，照抄返回的 citation）。
    series_ids : 一个序列号或列表，如 "CUUR0000SA0" 或 ["CUSR0000SA0", "LNS14000000"]（序列号在 data.bls.gov 的 Data Finder 查）
    start_year / end_year : 年份（含），1913 起；不传取最近 3 年（今年和前两年）
    max_queries : 这次最多发几次请求（1–25）。按 25 个序列 × 10 年拆分后超过它，就在联网前报 ValueError
    返回 {"series": {序列号: [{date, value, preliminary, footnotes}]（日期升序）},
          "notes": [BLS 的其他提示], "queries": 实际联网次数（缓存命中不算）, "retrieved": 取数日期, "citation": 引用声明}
      date：月度 2026-09 / 季度 2026-Q3 / 半年 2026-H1 / 年度 2026（年均值行不返回，v1 本来也不给）
      value：None = BLS 这一期写的是 "-"（如 2025 年 10 月政府停摆）；有的序列那一期整行没有，rows 里就没有这一期
      "retrieved" 是实际取数那天（命中缓存时沿用当时的日期）
      preliminary：带 P（初值）或 I / U（链式 CPI 的初值 / 中间值）脚注。False 不代表以后不改，见下方「修订」
    """
    if isinstance(series_ids, str):
        series_ids = [series_ids]
    if not isinstance(series_ids, (list, tuple)) or not series_ids:
        raise ValueError("series_ids 给一个序列号或序列号列表")
    ids = []
    for s in series_ids:
        sid = s.strip().upper() if isinstance(s, str) else ""
        if not _BLS_ID.fullmatch(sid):
            raise ValueError(f"无效的 BLS 序列号：{s!r}")
        if sid not in ids:
            ids.append(sid)
    this_year = _date.today().year
    y1 = this_year if end_year is None else _int_in(end_year, "end_year", 1913, this_year)
    y0 = max(1913, y1 - 2) if start_year is None else _int_in(start_year, "start_year", 1913, this_year)
    if y0 > y1:
        raise ValueError(f"start_year {y0} 晚于 end_year {y1}")
    mq = _int_in(max_queries, "max_queries", 1, _BLS_DAILY)
    chunks = [ids[i:i + _BLS_MAX_SERIES] for i in range(0, len(ids), _BLS_MAX_SERIES)]
    spans = [(y, min(y + _BLS_MAX_YEARS - 1, y1)) for y in range(y0, y1 + 1, _BLS_MAX_YEARS)]
    need = len(chunks) * len(spans)
    if need > mq:
        tip = (f"确定要取就把 max_queries 调到 {need}" if need <= _BLS_DAILY
               else "超过每天 25 次，一天内取不完，请减少序列或缩短年份")
        raise ValueError(f"这次要发 {need} 次请求（{len(ids)} 个序列 × {y1 - y0 + 1} 年，每次最多 "
                         f"{_BLS_MAX_SERIES} 个序列、{_BLS_MAX_YEARS} 年），超过 max_queries={mq}；"
                         f"免 key 版每天只有 25 次，{tip}")
    series, no_data, notes, sent, got = {sid: [] for sid in ids}, {}, [], 0, []
    for ch in chunks:
        for a, b in spans:
            out, cached = _bls_request(ch, a, b)
            sent += 0 if cached else 1
            if out["missing"]:           # 序列号写错：第一次请求就能发现，不再往下发
                raise DataNotAvailable(f"BLS 没有这些序列：{out['missing']}（序列号可在 data.bls.gov 的 Data Finder 查）")
            for sid, ys in out["no_data"].items():
                no_data.setdefault(sid, []).extend(ys)
            notes += [m for m in out["notes"] if m not in notes]
            for sid, rows in out["series"].items():
                if sid in series:
                    series[sid] += rows
            got.append(out["retrieved"])
    for sid in series:
        series[sid].sort(key=lambda r: r["date"])
    if not any(series.values()):
        raise DataNotAvailable(f"{ids} 在 {y0}–{y1} 年没有数据")
    notes += [f"{sid} 没有 {_year_spans(ys)} 年的数据" for sid, ys in no_data.items()]
    d = min(got)                                 # 实际取数那天（缓存跨过午夜也不改）
    return {"series": series, "notes": notes, "queries": sent, "retrieved": d, "citation": _BLS_CITE.format(d=d)}

_US_MACRO = {
    # 名称: (序列号, 说明, 单位, 是否季调, 频率, 发布名称（bls_release_calendar 用）, 类型)
    # 类型 index / money：算变化率；level：只算差值（非农看新增人数）；rate：本身是百分比，只算百分点差
    "cpi": ("CUUR0000SA0", "CPI-U 总指数", "index 1982-84=100", False, "M", "Consumer Price Index", "index"),
    "cpi_sa": ("CUSR0000SA0", "CPI-U 总指数（季调）", "index 1982-84=100", True, "M", "Consumer Price Index", "index"),
    "core_cpi": ("CUUR0000SA0L1E", "核心 CPI（不含食品和能源）", "index 1982-84=100", False, "M", "Consumer Price Index", "index"),
    "core_cpi_sa": ("CUSR0000SA0L1E", "核心 CPI（季调）", "index 1982-84=100", True, "M", "Consumer Price Index", "index"),
    "shelter_cpi_sa": ("CUSR0000SAH1", "CPI 住房分项（季调）", "index 1982-84=100", True, "M", "Consumer Price Index", "index"),
    "cpi_w": ("CWUR0000SA0", "CPI-W 城市工薪和文职人员", "index 1982-84=100", False, "M", "Consumer Price Index", "index"),
    "chained_cpi": ("SUUR0000SA0", "链式 CPI（C-CPI-U）", "index 1999-12=100", False, "M", "Consumer Price Index", "index"),
    "unemployment_rate": ("LNS14000000", "失业率", "percent", True, "M", "Employment Situation", "rate"),
    "u6": ("LNS13327709", "U-6 广义失业率", "percent", True, "M", "Employment Situation", "rate"),
    "participation_rate": ("LNS11300000", "劳动参与率", "percent", True, "M", "Employment Situation", "rate"),
    "employment_population": ("LNS12300000", "就业人口比", "percent", True, "M", "Employment Situation", "rate"),
    "payrolls": ("CES0000000001", "非农就业人数", "thousands of persons", True, "M", "Employment Situation", "level"),
    "avg_hourly_earnings": ("CES0500000003", "私营部门平均时薪", "USD per hour", True, "M", "Employment Situation", "money"),
    "avg_weekly_hours": ("CES0500000002", "私营部门平均周工时", "hours", True, "M", "Employment Situation", "level"),
    "ppi": ("WPSFD4", "PPI 最终需求（季调）", "index 2009-11=100", True, "M", "Producer Price Index", "index"),
    "ppi_nsa": ("WPUFD4", "PPI 最终需求", "index 2009-11=100", False, "M", "Producer Price Index", "index"),
    "core_ppi": ("WPSFD49104", "PPI 最终需求不含食品和能源（季调）", "index 2010-04=100", True, "M", "Producer Price Index", "index"),
    "core_ppi_nsa": ("WPUFD49104", "PPI 最终需求不含食品和能源", "index 2010-04=100", False, "M", "Producer Price Index", "index"),
    "jolts_openings": ("JTS000000000000000JOL", "JOLTS 职位空缺", "thousands", True, "M",
                       "Job Openings and Labor Turnover Survey", "level"),
    "jolts_quits_rate": ("JTS000000000000000QUR", "JOLTS 主动离职率", "percent", True, "M",
                         "Job Openings and Labor Turnover Survey", "rate"),
    "import_prices": ("EIUIR", "进口价格指数", "index 2000=100", False, "M", "U.S. Import and Export Price Indexes", "index"),
    "eci_yoy": ("CIU1010000000000A", "雇佣成本指数 ECI 总薪酬同比", "percent (12-month change)", False, "Q",
                "Employment Cost Index", "rate"),
    "productivity": ("PRS85006092", "非农企业劳动生产率（较上季，季调年化）", "percent (annual rate)", True, "Q",
                     "Productivity and Costs", "rate"),
}

def _macro_names(indicators) -> list:
    if indicators is None:
        return list(_US_MACRO)
    names = [indicators] if isinstance(indicators, str) else indicators
    if not isinstance(names, (list, tuple)) or not names:
        raise ValueError(f"indicators 给名称或名称列表，可选 {list(_US_MACRO)}")
    bad = [n for n in names if n not in _US_MACRO]
    if bad:
        raise ValueError(f"没有这些指标：{bad}；可选 {list(_US_MACRO)}（其他序列用 bls_series）")
    return list(dict.fromkeys(names))

def us_macro(indicators=None, start_year: int = None, end_year: int = None, max_queries: int = 5) -> dict:
    """
    美国常用宏观指标（BLS，S 级）。indicators：名称或列表，None = 全部 23 个（一次请求就够）。
    start_year / end_year / max_queries 同 bls_series（不传取最近 3 年；1913 年起，但多数指标起点更晚）
    返回 {"indicators": {名称: {series_id, name, unit, seasonally_adjusted, frequency, release, rows}},
          "notes", "queries", "retrieved", "citation"}；rows 同 bls_series
    """
    names = _macro_names(indicators)
    r = bls_series([_US_MACRO[n][0] for n in names], start_year, end_year, max_queries)
    out = {}
    for n in names:
        sid, label, unit, sa, freq, release, _kind = _US_MACRO[n]
        out[n] = {"series_id": sid, "name": label, "unit": unit, "seasonally_adjusted": sa,
                  "frequency": "monthly" if freq == "M" else "quarterly", "release": release,
                  "rows": r["series"][sid]}
    return {"indicators": out, "notes": r["notes"], "queries": r["queries"],
            "retrieved": r["retrieved"], "citation": r["citation"]}

def _shift(period: str, k: int) -> str:
    """期次往前推 k 期：2026-09 → 2026-08；2026-Q1 推 1 期 → 2025-Q4"""
    y = int(period[:4])
    if "-Q" in period:
        n = y * 4 + int(period[-1]) - 1 - k
        return f"{n // 4}-Q{n % 4 + 1}"
    n = y * 12 + int(period[5:7]) - 1 - k
    return f"{n // 12}-{n % 12 + 1:02d}"

def us_macro_latest(indicators=None, next_release: bool = True) -> dict:
    """
    各指标最新一期 + 上一期 + 去年同期，变化在本地算（v1 不给变化率）。默认一次 BLS 请求 + 读一次日程。
    返回 {"rows": [{indicator, name, unit, seasonally_adjusted, date, value, preliminary,
                    prev_date, prev, change, change_pct, year_ago, yoy_change, yoy_pct, release, next_release}],
          "notes", "queries", "retrieved", "citation"}
      change / yoy_change：差值（失业率是百分点，非农是千人）；change_pct / yoy_pct：变化率 %，
      只对指数和金额类给（失业率这类本身是百分比的给 None）。按 BLS 的做法用公布值直接算，未四舍五入
      上一期、去年同期按期次找，不按位置；那一期没有数（2025 年 10 月停摆）就是 None
      next_release：下一次发布 {date, time_et}（按美东日期，当天的也算），日程里没有就是 None
    """
    names = _macro_names(indicators)
    m = us_macro(names)
    upcoming, notes = [], list(m["notes"])
    if next_release:
        try:
            upcoming = bls_release_calendar(start=_et_today())     # Layer 6 的美东日期，分得清冬夏令时
        except RuntimeError as e:              # 日程取不到不影响数值（DataNotAvailable 也是 RuntimeError）
            notes.append(f"发布日程没取到，next_release 都是 None：{e}")
    rows = []
    for n in names:
        it, kind = m["indicators"][n], _US_MACRO[n][6]
        vals = {r["date"]: r for r in it["rows"] if r["value"] is not None}
        row = {"indicator": n, "name": it["name"], "unit": it["unit"],
               "seasonally_adjusted": it["seasonally_adjusted"], "release": it["release"],
               "next_release": next(({"date": e["date"], "time_et": e["time_et"]} for e in upcoming
                                     if e["release"] == it["release"]), None)}
        if not vals:                           # 字段照样给全，缺的都是 None
            rows.append(dict(row, **dict.fromkeys(("date", "value", "preliminary", "prev_date", "prev", "change",
                                                   "change_pct", "year_ago", "yoy_change", "yoy_pct"))))
            continue
        last = max(vals)
        cur = vals[last]["value"]
        prev_d = _shift(last, 1)
        prev = vals.get(prev_d, {}).get("value")
        ya = vals.get(_shift(last, 4 if "-Q" in last else 12), {}).get("value")

        def _chg(a):
            return None if a is None else round(cur - a, 6)

        def _pct(a):
            return None if a is None or kind not in ("index", "money") or not a else (cur / a - 1) * 100

        row.update({"date": last, "value": cur, "preliminary": vals[last]["preliminary"],
                    "prev_date": prev_d, "prev": prev, "change": _chg(prev), "change_pct": _pct(prev),
                    "year_ago": ya, "yoy_change": _chg(ya), "yoy_pct": _pct(ya)})
        rows.append(row)
    return {"rows": rows, "notes": notes, "queries": m["queries"],
            "retrieved": m["retrieved"], "citation": m["citation"]}

def bls_release_calendar(release: str = None, start=None, end=None) -> list:
    """
    BLS 官方发布日程（官网 iCalendar 文件，S 级；不占 API 次数，但要配好 SEC_CONTACT）。
    release : 按名称筛，不分大小写。和某个发布名称完全一致就只取它（"Employment Situation" 不带上
              "Employment Situation of Veterans"），否则按包含筛（"price" 取全部带 Price 的发布）。
              us_macro 返回的 release 字段可以直接传
    start / end : YYYY-MM-DD（含），按美东日期
    返回 [{date, time_et, release}]，按时间升序。只有 BLS 当前公布的那段日程；只有发布时间，不写这次发的是哪个月的数
    """
    if release is not None and not (isinstance(release, str) and release.strip()):
        raise ValueError("release 给发布名称的一部分，如 'Consumer Price Index'")
    start, end = _check_range(start, end, _YMD_ONLY)
    text = re.sub(r"\r?\n[ \t]", "", _fixed_get(_BLS_ICS))        # iCalendar 折行：以空格开头的行接到上一行
    body = text.strip("\ufeff \t\r\n")
    lines = [ln.strip() for ln in body.splitlines()]
    evs = re.findall(r"^BEGIN:VEVENT\r?$(.*?)^END:VEVENT\r?$", body, re.S | re.M)
    # 整份文件要完整：首尾是 VCALENDAR，事件开闭配对，否则可能是被截断的残件（读出来会少几条，不能当成「没有发布」）
    if not (body.startswith("BEGIN:VCALENDAR") and body.endswith("END:VCALENDAR") and evs
            and lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == len(evs)):
        raise RuntimeError(f"BLS 日程不是完整的日历文件（开头：{body[:60]!r}，结尾：{body[-40:]!r}），请核对 {_BLS_ICS}")
    out = []
    for ev in evs:
        t = re.search(r"^DTSTART([^:\r\n]*):(\d{8}T\d{6})\r?$", ev, re.M)
        s = re.search(r"^SUMMARY[^:\r\n]*:(.*?)\r?$", ev, re.M)
        try:
            when = datetime.strptime(t.group(2), "%Y%m%dT%H%M%S") if t else None
        except ValueError:                        # 位数对、日期或时间不存在（如 2 月 30 日、25:61）
            when = None
        if not (when and s and s.group(1).strip()):   # 现在 313 个事件都带合法时间和名称；缺了说明格式变了，不能悄悄少几条
            raise RuntimeError(f"BLS 日程里有一条读不懂的事件（{ev.strip()[:120]!r}），请核对 {_BLS_ICS}")
        if "TZID=US-Eastern" not in t.group(1):
            raise RuntimeError(f"BLS 日程的时区写法变了（{t.group(1)!r}），请核对 {_BLS_ICS}")
        name = s.group(1).strip().replace("\\,", ",").replace("\\;", ";")
        out.append({"date": when.strftime("%Y-%m-%d"), "time_et": when.strftime("%H:%M"), "release": name})
    if release:                                   # 先在整份日程里定筛法，再筛日期
        want = release.strip().lower()
        exact = any(r["release"].lower() == want for r in out)
        out = [r for r in out if (r["release"].lower() == want if exact else want in r["release"].lower())]
    out = [r for r in out if not ((start and r["date"] < start) or (end and r["date"] > end))]
    if not out:
        raise DataNotAvailable(f"BLS 日程里没有符合条件的发布（release={release!r}, {start}–{end}）")
    return sorted(out, key=lambda r: (r["date"], r["time_et"]))

_DOL_CLAIMS = "https://oui.doleta.gov/unemploy/wkclaims/report.asp"

_DOL_FIELDS = (("initial_nsa", "InitialClaims/NSA"), ("initial_sa", "InitialClaims/SA"),
               ("initial_sa_4wk", "InitialClaims/SA4WK"), ("continued_nsa", "ContinuedClaims/NSA"),
               ("continued_sa", "ContinuedClaims/SA"), ("continued_sa_4wk", "ContinuedClaims/SA4WK"),
               ("iur_nsa", "IUR/NSA"), ("iur_sa", "IUR/SA"), ("covered_employment", "CoveredEmployment"))

def _dol_num(s, where: str):
    t = (s or "").replace("\xa0", " ").strip()                       # 空格子是 &#160;，数字带千分位
    if not t:
        return None
    if not re.fullmatch(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?", t):    # 先按完整写法校验再去逗号；只有空格子算没有数
        raise RuntimeError(f"劳工部申领文件里有读不懂的数 {s!r}（{where}），格式可能变了")
    t = t.replace(",", "")
    return float(t) if "." in t else int(t)

def us_jobless_claims(start_year: int = None, end_year: int = None) -> list:
    """
    美国每周失业金申领，全美合计（劳工部 ETA，S 级，注明来源 U.S. Department of Labor）。1967 年起。
    start_year / end_year : 年份（含），不传取今年和去年
    返回 [{week_ended, initial_nsa, initial_sa, initial_sa_4wk, continued_nsa, continued_sa, continued_sa_4wk,
           iur_nsa, iur_sa, covered_employment}]，按周升序；week_ended 是那一周的周六
      initial：这一周新提交的初次申请；continued：这一周仍在领取的周数（又叫 insured unemployment），比初请晚一周公布
      iur：保险失业率 %（续请 ÷ 失业保险覆盖的就业人数）；没有的格子是 None（1967 年的四周均值、保险失业率等）
    还没有数的周不返回（官方文件里未来的周只有季节因子）
    """
    this_year = _date.today().year
    y1 = this_year if end_year is None else _int_in(end_year, "end_year", 1967, this_year)
    y0 = max(1967, y1 - 1) if start_year is None else _int_in(start_year, "start_year", 1967, this_year)
    if y0 > y1:
        raise ValueError(f"start_year {y0} 晚于 end_year {y1}")
    raw = _fixed_get(_DOL_CLAIMS, data={"level": "us", "strtdate": str(y0), "enddate": str(y1),
                                          "filetype": "xml"}, raw=True, timeout=90)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:      # 2026-11-02 实测：对方数据库连不上时返回 200 和一句 "Unable to connect!…"
        head = re.sub(r"\s+", " ", raw[:150].decode("utf-8", "replace")).strip()
        raise RuntimeError(f"劳工部没有返回 XML（返回开头：{head!r}）：可能是对方数据库临时故障（稍后再试），"
                           "也可能是表单规则变了，请到 oui.doleta.gov/unemploy/claims.asp 核对") from None
    if root.tag != "r539cyNational":
        raise RuntimeError(f"劳工部申领文件格式变了（根节点 {root.tag!r}）")
    out = []
    for w in root.findall("week"):
        raw_wk = w.findtext("weekEnded")
        try:
            wk = datetime.strptime((raw_wk or "").strip(), "%m/%d/%Y").date().isoformat()
        except ValueError:
            raise RuntimeError(f"劳工部申领文件里有读不懂的日期 {raw_wk!r}，格式可能变了") from None
        row = {"week_ended": wk}
        row.update({k: _dol_num(w.findtext(path), f"{wk} {path}") for k, path in _DOL_FIELDS})
        if all(row[k] is None for k, _ in _DOL_FIELDS):
            continue
        out.append(row)
    if not out:
        raise DataNotAvailable(f"劳工部 {y0}–{y1} 年没有申领数据")
    return sorted(out, key=lambda r: r["week_ended"])
