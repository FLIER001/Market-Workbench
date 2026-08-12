"""A股全栈数据层 —— 移植自 a-stock-data 工具包（五层数据源，自包含）。

分级依赖：
  - 行情（腾讯）        : 仅需标准库 urllib —— 永远可用
  - 研报（东财）+ PDF   : 仅需 requests —— 轻量必装
  - 一致预期/新闻/公告  : akshare（惰性导入，缺失时优雅报错）
  - K线/财务/F10        : mootdx（惰性导入，缺失时优雅报错）

合规：本模块只按用户传入的代码返回客观数据，不预置任何标的、不排名、不建议。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_QUOTE_CACHE: dict[tuple[str, ...], tuple[float, dict[str, dict]]] = {}
_QUOTE_LOCK = threading.Lock()
_INDEX_CACHE: tuple[float, list[dict]] | None = None


def _number(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def get_prefix(code: str) -> str:
    """6 位代码 → 交易所前缀。5 开头是沪市基金/ETF（51/56/58 等），深市基金 15/16 开头走默认 sz。"""
    if code.startswith(("6", "9", "5")):
        return "sh"
    if code.startswith("8"):
        return "bj"
    return "sz"


class DependencyMissing(RuntimeError):
    """惰性依赖未安装时抛出，前端据此提示 pip install。"""


# ---------------------------------------------------------------------------
# Layer 1 · 行情（腾讯财经，仅标准库，不封 IP）
# ---------------------------------------------------------------------------

def _fetch_gtimg(prefixed_codes: list[str]) -> str:
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed_codes)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("gbk")


def _parse_gtimg(data: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]

        def num(i: int) -> float:
            try:
                return float(vals[i]) if vals[i] else 0.0
            except (ValueError, IndexError):
                return 0.0

        quote_stamp = vals[30].strip() if len(vals) > 30 else ""
        quote_date = (
            f"{quote_stamp[:4]}-{quote_stamp[4:6]}-{quote_stamp[6:8]}"
            if len(quote_stamp) >= 8 and quote_stamp[:8].isdigit()
            else ""
        )
        quote_time = (
            f"{quote_stamp[8:10]}:{quote_stamp[10:12]}:{quote_stamp[12:14]}"
            if len(quote_stamp) >= 14 and quote_stamp[:14].isdigit()
            else ""
        )
        result[code] = {
            "name": vals[1],
            "price": num(3),
            "last_close": num(4),
            "open": num(5),
            "quote_date": quote_date,
            "quote_time": quote_time,
            "change_amt": num(31),
            "change_pct": num(32),
            "high": num(33),
            "low": num(34),
            "volume_lot": num(36),
            "amount_wan": num(37),
            "turnover_pct": num(38),
            "pe_ttm": num(39),
            "amplitude_pct": num(43),
            # 腾讯字段 44=流通市值、45=总市值；单位均为亿元。
            "float_mcap_yi": num(44),
            "mcap_yi": num(45),
            "pb": num(46),
            "limit_up": num(47),
            "limit_down": num(48),
            "vol_ratio": num(49),
            "pe_static": num(52),
        }
    return result


def tencent_quote(codes: list[str]) -> dict[str, dict]:
    """批量个股实时行情：现价 / 涨跌 / PE / PB / 市值 / 换手 / 涨跌停。"""
    key = tuple(sorted(set(codes)))
    now = time.time()
    with _QUOTE_LOCK:
        hit = _QUOTE_CACHE.get(key)
        if hit and now - hit[0] < 3:
            return hit[1]
        prefixed = [f"{get_prefix(c)}{c}" for c in key]
        result = _parse_gtimg(_fetch_gtimg(prefixed))
        if result:
            _QUOTE_CACHE[key] = (time.time(), result)
        return result


# A股大盘指数（前缀规则与个股不同，固定带前缀代码）
A_INDICES = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688", "bj899050"]

# 大盘指数卡 name → 腾讯完整代码（点击展开当日分时用；指数须用完整带前缀代码，
# 裸 6 位走 get_prefix 会把 000300 误判成 sz399300、sh000688 拼错前缀）
A_INDEX_CODES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "沪深300": "sh000300",
    "科创50": "sh000688",
    "北证50": "bj899050",
}


def a_index_em_secid(prefixed_code: str) -> str | None:
    """腾讯带前缀代码（sh000001/sz399006/bj899050）→ 东财指数 secid。"""
    if len(prefixed_code) != 8 or not prefixed_code[:2].isalpha():
        return None
    p, num = prefixed_code[:2], prefixed_code[2:]
    if p == "sh":
        return f"1.{num}"
    if p in ("sz", "bj"):
        return f"0.{num}"
    return None


def index_quote() -> list[dict]:
    """A股大盘指数实时行情（上证/深证成指/创业板指/沪深300）。"""
    global _INDEX_CACHE
    now = time.time()
    with _QUOTE_LOCK:
        if _INDEX_CACHE and now - _INDEX_CACHE[0] < 5:
            return _INDEX_CACHE[1]
        parsed = _parse_gtimg(_fetch_gtimg(A_INDICES))
        out = []
        for full in A_INDICES:
            q = parsed.get(full[2:])
            if q:
                out.append({"name": q["name"], "price": q["price"], "change_pct": q["change_pct"], "change_amt": q["change_amt"]})
        if out:
            _INDEX_CACHE = (time.time(), out)
        return out


# ---------------------------------------------------------------------------
# Layer 2 · 研报（东财 reportapi，仅 requests）
# ---------------------------------------------------------------------------

_REPORT_API = "https://reportapi.eastmoney.com/report/list"
_PDF_TPL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"


def _report_session():
    import requests  # 轻依赖，随后端一起装

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
    return s


def eastmoney_reports(code: str, max_pages: int = 3) -> list[dict]:
    """按个股代码拉研报列表（qType=0）。"""
    session = _report_session()
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": code, "rcode": "",
            "p": str(page), "pageNum": str(page), "pageNumber": str(page),
        }
        r = session.get(_REPORT_API, params=params, timeout=30)
        d = r.json()
        rows = d.get("data") or []
        if not rows:
            break
        out.extend(rows)
        if page >= (d.get("TotalPage", 1) or 1):
            break
        time.sleep(0.3)
    return out


def eastmoney_industry_reports(keywords: list[str] | None = None, days: int = 90, max_pages: int = 3) -> list[dict]:
    """按行业拉研报（qType=1）——适合产业链 / 主题级检索。keywords 在标题上过滤。"""
    from datetime import date, timedelta

    session = _report_session()
    end = date.today()
    begin = end - timedelta(days=days)
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": begin.isoformat(), "endTime": end.isoformat(),
            "pageNo": str(page), "fields": "", "qType": "1",
            "orgCode": "", "code": "", "rcode": "",
        }
        r = session.get(_REPORT_API, params=params, timeout=30)
        rows = r.json().get("data") or []
        if not rows:
            break
        out.extend(rows)
        time.sleep(0.3)
    if keywords:
        out = [r for r in out if any(k in r.get("title", "") for k in keywords)]
    return out


def pdf_url(info_code: str) -> str:
    return _PDF_TPL.format(info_code=info_code)


# ---------------------------------------------------------------------------
# Layer 3/4/5 · akshare 惰性封装（一致预期 / 新闻 / 公告 / 基本面）
# ---------------------------------------------------------------------------

def _akshare():
    try:
        import akshare as ak
        return ak
    except ImportError as e:
        raise DependencyMissing("akshare 未安装：pip install akshare") from e


def profit_forecast(code: str) -> list[dict]:
    """机构一致预期 EPS（同花顺）。"""
    ak = _akshare()
    df = ak.stock_profit_forecast_ths(symbol=code, indicator="预测年报每股收益")
    return df.to_dict("records") if df is not None and not df.empty else []


def stock_news(code: str, limit: int = 20) -> list[dict]:
    """个股新闻（东财）。"""
    ak = _akshare()
    df = ak.stock_news_em(symbol=code)
    return df.head(limit).to_dict("records") if df is not None and not df.empty else []


def individual_info(code: str) -> dict:
    """个股基本面（东财）：行业 / 总股本 / 上市时间等。"""
    ak = _akshare()
    df = ak.stock_individual_info_em(symbol=code)
    if df is None or df.empty:
        return {}
    return {str(row["item"]): row["value"] for _, row in df.iterrows()}


def disclosure(code: str) -> list[dict]:
    """巨潮公告全文列表（akshare cninfo，本环境不稳，保留作备用）。"""
    ak = _akshare()
    market = "沪市" if code.startswith("6") else ("北交所" if code.startswith("8") else "深市")
    df = ak.stock_zh_a_disclosure_report_cninfo(symbol=code, market=market)
    return df.head(30).to_dict("records") if df is not None and not df.empty else []


def announcements(code: str, limit: int = 15) -> list[dict]:
    """个股近期公告（东财公开接口，仅 requests，稳定）。返回 日期/标题/类型/详情链接。"""
    import requests

    r = requests.get(
        "https://np-anotice-stock.eastmoney.com/api/security/ann",
        params={"sr": -1, "page_size": limit, "page_index": 1, "ann_type": "A",
                "client_source": "web", "stock_list": code, "f_node": 0, "s_node": 0},
        headers={"User-Agent": UA}, timeout=20,
    )
    lst = (r.json().get("data") or {}).get("list") or []
    out = []
    for a in lst:
        cols = [c.get("column_name") for c in (a.get("columns") or []) if c.get("column_name")]
        art = a.get("art_code", "")
        out.append({
            "date": (a.get("notice_date", "") or "")[:10],
            "title": a.get("title", ""),
            "type": cols[0] if cols else "",
            "url": f"https://data.eastmoney.com/notices/detail/{code}/{art}.html" if art else "",
        })
    return out


# ---------------------------------------------------------------------------
# mootdx 惰性封装（K线 / 财务 / F10）
# ---------------------------------------------------------------------------

def _mootdx_client():
    try:
        from mootdx.quotes import Quotes
        return Quotes.factory(market="std")
    except ImportError as e:
        raise DependencyMissing("mootdx 未安装：pip install mootdx") from e


def kline(code: str, category: int = 4, offset: int = 60) -> list[dict]:
    """K线：category 4=日 5=周 6=月 11=60分钟。"""
    client = _mootdx_client()
    df = client.bars(symbol=code, category=category, offset=offset)
    return df.to_dict("records") if df is not None and not df.empty else []


_TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _parse_tencent_kline(payload: dict, symbol: str, period: str) -> list[dict]:
    data = (payload.get("data") or {}).get(symbol) or {}
    raw = data.get(f"qfq{period}") or data.get(period) or []
    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            turnover = item[7] if len(item) > 7 and not isinstance(item[7], dict) else None
            amount_wan = item[8] if len(item) > 8 and not isinstance(item[8], dict) else None
            rows.append({
                "date": str(item[0])[:10],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]),
                "turnover_pct": float(turnover) if turnover not in (None, "") else None,
                # 腾讯个股 K 线第 9 列为成交额（万元）。统一换算为元，避免用前复权价反推成交额。
                "amount": float(amount_wan) * 10_000 if amount_wan not in (None, "") else None,
            })
        except (TypeError, ValueError):
            continue
    return rows


# 腾讯 K 线备用域名：主域名 web.ifzq.gtimg.cn 2026-08 起被腾讯 WAF 拦（HTTPS 501 挑战页），
# 同接口在 proxy.finance.qq.com 下仍可用（akshare stock_zh_a_hist_tx 走的就是这条）。
_TENCENT_KLINE_FALLBACK = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def tencent_kline(code: str, period: str = "day", count: int = 250) -> list[dict]:
    """腾讯前复权 K 线；HTTP、零鉴权，作为页面图表主源。

    主源 web.ifzq.gtimg.cn 被 WAF 拦后自动降级到 proxy.finance.qq.com。
    """
    if period not in {"day", "week", "month"}:
        raise ValueError("period 必须是 day/week/month")
    symbol = f"{get_prefix(code)}{code}"
    query = urllib.parse.urlencode({"param": f"{symbol},{period},,,{count},qfq"})
    request = urllib.request.Request(
        f"{_TENCENT_KLINE}?{query}",
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = _parse_tencent_kline(payload, symbol, period)
        if rows:
            return rows
    except Exception:  # noqa: BLE001 — 主源被 WAF 拦/超时，走备用域名
        pass

    # 备用：proxy.finance.qq.com（直连、忽略系统代理，避免科学上网代理挂掉国内站）
    try:
        import requests as _req

        session = _req.Session()
        session.trust_env = False
        params = {
            "_var": "kline_dayqfq",
            "param": f"{symbol},{period},,,{count},qfq",
            "r": "0.12345",
        }
        resp = session.get(
            _TENCENT_KLINE_FALLBACK,
            params=params,
            headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
            timeout=12,
        )
        text = resp.text
        payload = json.loads(text[text.find("={") + 1:])
        return _parse_tencent_kline(payload, symbol, period)
    except Exception:  # noqa: BLE001 — 备用也失败则返回空，交由上层降级
        return []


def minute_kline(code: str) -> dict:
    """分时图（当日分钟级）：腾讯 web.ifzq.gtimg.cn minute/query 接口，零鉴权。"""
    symbol = code if code[:2].isalpha() else f"{get_prefix(code)}{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = (payload.get("data") or {}).get(symbol, {}).get("data", {})
    rows = data.get("data", [])
    date = data.get("date", "")
    # 昨收
    qt = (payload.get("data") or {}).get(symbol, {}).get("qt", {})
    qt_data = qt.get(symbol, []) if isinstance(qt, dict) else []
    prev_close = 0.0
    if isinstance(qt_data, list) and len(qt_data) > 4:
        try:
            prev_close = float(qt_data[4])
        except (ValueError, IndexError):
            pass

    # 欧美指数腾讯只给当前 tick（无当日序列）：用东财 push2his 分钟历史补全
    if len(rows) < 2:
        secid = next((sec for sym, sec in GLOBAL_INDEX_MINUTE_SRC.values() if sym == symbol), None)
        if secid:
            em = em_index_minutes(secid)
            if em is not None:
                return em

    # 港股收盘 16:00；A 股收盘 15:00。腾讯港股数据含 16:00 后的竞价/延时尾盘，
    # 截到 16:00 足够。A 股 15:00 后无交易数据（腾讯不返回），不截断。
    # 用代码前缀区分：hk 港股截到 1600，其余（A 股）截到 1500。
    is_hk = symbol.lower().startswith("hk")
    cutoff = "1600" if is_hk else "1500"
    points = []
    for line in rows:
        parts = str(line).split(" ")
        if len(parts) < 2:
            continue
        try:
            t = parts[0]
            # 只保留盘中交易时段，过滤收盘后竞价/延时重复数据
            if t < "0930" or t > cutoff:
                continue
            points.append({
                "time": t,
                "price": float(parts[1]),
                "volume": int(parts[2]) if len(parts) > 2 else 0,
            })
        except (ValueError, IndexError):
            continue
    return {"date": date, "prev_close": prev_close, "points": points}


# 全球指数 key → (腾讯分钟 symbol, 东财 secid)。港股/亚太/南亚腾讯全量分钟优先；
# 欧美腾讯只有当前 tick、亚太部分腾讯不认，这两类直接用东财 push2his 分钟序列。
# key 与 gstock._INDICES 对齐，东财 secid 取自其配置。
# 全球指数 key → (腾讯分钟 symbol, 东财 secid)。港股/亚太/南亚腾讯全量分钟优先；
# 欧美/其余腾讯只有当前 tick或不认，用东财 push2his 分钟序列。
# 东财 trends2 时间戳 = 各市场开盘对应的北京时间（日经东京 09:00=北京 08:00，
# DAX 法兰克福 09:00=北京 15:00 夏令，标普美东 09:30=北京 21:30 夏令），与指数卡
# 同源，因此分时图最新点与卡片点位一致。
GLOBAL_INDEX_MINUTE_SRC: dict[str, tuple[str | None, str]] = {
    # 美股（腾讯单点 → 东财）
    "spx": ("usINX", "100.SPX"), "ndx": ("usNDX", "100.NDX"),
    # 港股（腾讯全量分钟）
    "hsi": ("hkHSI", "100.HSI"), "hstech": ("hkHSTECH", "124.HSTECH"),
    # 亚太（腾讯 hk 前缀不认 → 东财）
    "n225": (None, "100.N225"), "ks11": (None, "100.KS11"), "twii": (None, "100.TWII"),
    "aord": (None, "100.AORD"), "set": (None, "100.SET"), "jkse": (None, "100.JKSE"),
    "klse": (None, "100.KLSE"), "vnindex": (None, "100.VNINDEX"),
    # 欧洲（腾讯单点 → 东财）
    "gdaxi": ("euGDAXI", "100.GDAXI"), "ftse": ("euFTSE", "100.FTSE"),
    "fchi": ("euFCHI", "100.FCHI"), "aex": ("euAEX", "100.AEX"),
    "ssmi": ("euSSMI", "100.SSMI"), "ibex": ("euIBEX", "100.IBEX"),
    # 南亚（→ 东财）
    "sensex": (None, "100.SENSEX"),
}


# trends2 可用主机 latch：push2his 优先，直连偶发被掐时降级 push2delay（延时分钟，
# 对"当日走势图"足够）。不走 em_get——其直连探测一旦锁死直连会把降级主机也拖挂。
_TRENDS_HOSTS = ("push2his.eastmoney.com", "push2.eastmoney.com", "push2delay.eastmoney.com")
_trends_host = [0]


def em_index_minutes(secid: str, ndays: int = 1) -> dict | None:
    """东财 push2his trends2：全球指数分钟序列（收盘价口径，无成交量）。

    ndays=1 当日；>1 用于闭市时回退取上一交易日。失败/空返回 None。
    成交量统一置 0——欧美/亚太指数无口径一致的成交量，图表对全零量自动退化为纯走势线。
    """
    import requests as _req

    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11",
        "fields2": "f51,f53,f58", "ndays": max(ndays, 2), "iscr": 0,
    }
    # 强制直连：push2his/push2 对系统代理会 RemoteDisconnected，走代理反而全挂
    s = _req.Session()
    s.trust_env = False
    d: dict = {}
    for i in range(_trends_host[0], len(_TRENDS_HOSTS)):
        try:
            r = s.get(f"https://{_TRENDS_HOSTS[i]}/api/qt/stock/trends2/get",
                      params=params, headers={"User-Agent": UA}, timeout=10)
            data = r.json().get("data") or {}
            if data.get("trends"):
                _trends_host[0] = i  # latch 到首个可用主机，整进程复用
                d = data
                break
        except Exception:  # noqa: BLE001 — 换下一个主机
            continue
    if not d:
        return None
    try:
        trends = d.get("trends") or []
        # 美股 21:30 开盘（北京夜间）跨自然日：ndays=1 开盘前只给上一交易日，
        # 用 ndays=2 拿两天，只保留最新交易日；prev_close 取该日首点（昨收基准）。
        prev = d.get("preClose")
        points = []
        raw: list[tuple[str, str, float]] = []
        for line in trends:
            parts = str(line).split(",")
            if len(parts) < 2:
                continue
            try:
                day = parts[0].split(" ")[0]
                hhmm = parts[0].split(" ")[-1].replace(":", "")
                raw.append((day, hhmm, float(parts[1])))
            except (ValueError, IndexError):
                continue
        if not raw:
            return None
        latest_day = raw[-1][0]
        day_points = [(hm, pr) for dy, hm, pr in raw if dy == latest_day]
        # 隔夜市场（如美股 21:30→次日04:00 北京时间）跨自然日：
        # latest_day 只保留了午夜后的数据，丢失了当晚 21:30-23:59 的走势。
        # 检测跨日会话：如果首条数据在晚间（>=18:00）且跨了自然日，
        # 则保留从首条起的所有点（即完整交易会话）。
        first_hm = raw[0][1]
        first_hour = int(first_hm[:2])
        if first_hour >= 18 and len({dy for dy, _, _ in raw}) > 1:
            # 隔夜会话：保留从首个晚间点开始的所有数据
            day_points = [(hm, pr) for _, hm, pr in raw]
            # prev_close = 会话开始前最后一个收盘价（即 raw 之前的数据）
            # trends2 的 preClose 即为该会话的昨收
        elif day_points and day_points[0][1] != raw[0][1]:  # 普通跨日：首点前还有前一交易日数据
            prev_close_val = raw[-len(day_points) - 1][2] if len(raw) > len(day_points) else prev
            if isinstance(prev_close_val, (int, float)):
                prev = prev_close_val
        points = [{"time": hm, "price": pr, "volume": 0} for hm, pr in day_points]
        day = str(d.get("prePriceDate") or (str(trends[0]).split(" ")[0] if trends else "")).replace("-", "")
        if day_points:
            day = latest_day.replace("-", "")
        return {
            "date": day,
            "prev_close": float(prev) if isinstance(prev, (int, float)) else 0.0,
            "points": points,
        }
    except Exception:  # noqa: BLE001 — 数据格式异常时返回 None，让上层按"无分时数据"处理
        return None


def em_index_minutes_latest(secid: str, market_key: str | None = None) -> dict | None:
    """闭市市场的"上一交易日"分时：取东财日 K 最后两收价，合成一条两端点走势线。

    用于 trends2 对闭市市场返回空时兜底——图表显示为一条直线，但价格/涨跌幅/
    昨收基准正确，并带上该 K 线日期供界面标注「上一交易日」。失败返回 None。

    market_key 传入全球指数 key（如 'gdaxi'）时，用该市场北京时间交易时段
    生成端点时间戳；否则按 A 股 09:30-15:00 兜底。
    """
    import requests as _req

    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": 101, "fqt": 0, "end": "20500101", "lmt": 2,
    }
    s = _req.Session()
    s.trust_env = False
    for host in _TRENDS_HOSTS:
        try:
            r = s.get(f"https://{host}/api/qt/stock/kline/get",
                      params=params, headers={"User-Agent": UA}, timeout=10)
            d = r.json().get("data") or {}
            klines = d.get("klines") or []
            if not klines:
                continue
            last = str(klines[-1]).split(",")          # [date, open, close, high, low, ...]
            day = last[0].replace("-", "")
            close = float(last[2])
            prev_close = float(str(klines[-2]).split(",")[2]) if len(klines) > 1 else float(last[1])

            # 端点时间戳：有 market_key 时用该市场北京时间开/收盘，否则 A 股
            if market_key:
                try:
                    import gstock as _gs
                    mm = _gs.market_minutes_bj(market_key)
                    if mm:
                        # 取首段开盘和末段收盘（处理跨午夜）
                        open_mod, _ = mm[0]
                        _, close_mod = mm[-1]
                        open_t = f"{open_mod // 60:02d}{open_mod % 60:02d}"
                        # 跨午夜：close_mod 可能 < open_mod，取模到 [0,1440)
                        close_wrapped = close_mod % (24 * 60)
                        close_t = f"{close_wrapped // 60:02d}{close_wrapped % 60:02d}"
                    else:
                        open_t, close_t = "0930", "1500"
                except Exception:
                    open_t, close_t = "0930", "1500"
            else:
                open_t, close_t = "0930", "1500"

            return {
                "date": day,
                "prev_close": prev_close,
                "points": [
                    {"time": open_t, "price": close, "volume": 0},
                    {"time": close_t, "price": close, "volume": 0},
                ],
            }
        except Exception:  # noqa: BLE001 — 换下一个主机
            continue
    return None


def chart_kline(code: str, period: str = "day", count: int = 250) -> dict:
    """供前端绘图的标准 OHLCV：腾讯前复权主源，mootdx 不复权备用。"""
    rows: list[dict] = []
    source = "腾讯财经"
    adjustment = "前复权"
    try:
        rows = tencent_kline(code, period=period, count=count)
    except Exception:  # noqa: BLE001 — 主源失败后按既定顺序降级
        rows = []

    if not rows:
        category = {"day": 4, "week": 5, "month": 6}[period]
        raw = kline(code, category=category, offset=count)
        source = "通达信"
        adjustment = "不复权"
        for item in raw:
            raw_date = item.get("date") or item.get("datetime")
            if hasattr(raw_date, "strftime"):
                raw_date = raw_date.strftime("%Y-%m-%d")
            try:
                rows.append({
                    "date": str(raw_date)[:10],
                    "open": float(item["open"]),
                    "close": float(item["close"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "volume": float(item.get("volume") or item.get("vol") or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue

    rows = sorted(
        {row["date"]: row for row in rows if row.get("date")}.values(),
        key=lambda row: row["date"],
    )[-count:]
    return {
        "code": code,
        "period": period,
        "adjustment": adjustment,
        "source": source,
        "as_of": rows[-1]["date"] if rows else None,
        "rows": rows,
    }


def finance(code: str) -> dict:
    """季报财务快照（37 字段）。"""
    client = _mootdx_client()
    df = client.finance(symbol=code)
    if df is None or (hasattr(df, "empty") and df.empty):
        return {}
    return df.to_dict("records")[0] if hasattr(df, "to_dict") else dict(df)


# ---------------------------------------------------------------------------
# 估值计算
# ---------------------------------------------------------------------------

def calc_peg(pe: float, cagr: float) -> float:
    if cagr <= 0:
        return float("inf")
    return pe / (cagr * 100)


def pe_digestion(current_pe: float, cagr: float, target_pe: float = 30) -> float:
    if current_pe <= target_pe:
        return 0.0
    if cagr <= 0:
        return float("inf")
    return math.log(current_pe / target_pe) / math.log(1 + cagr)


def financials(code: str) -> dict:
    """财务关键指标（同花顺财务摘要，最新报告期）—— 干净可靠的营收/净利/ROE/毛利率等。

    注：mootdx finance() 的营收/净利数值不可靠(实测放大数倍)，故财务摘要走此源。
    """
    ak = _akshare()
    df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
    if df is None or df.empty:
        return {}
    row = df.iloc[-1].to_dict()  # 最新报告期（按报告期升序，取末行）

    def g(k):
        v = row.get(k)
        return None if v in (False, "false", "", None) else v

    return {
        "period": g("报告期"),
        "revenue": g("营业总收入"), "revenue_yoy": g("营业总收入同比增长率"),
        "net_profit": g("净利润"), "net_profit_yoy": g("净利润同比增长率"),
        "eps": g("基本每股收益"), "bvps": g("每股净资产"),
        "roe": g("净资产收益率"), "gross_margin": g("销售毛利率"), "net_margin": g("销售净利率"),
        "op_cf_ps": g("每股经营现金流"),
    }


def valuation_percentile(code: str, period: str = "近五年") -> dict:
    """历史估值分位（百度股市通）：PE-TTM / PB 的当前值 + 历史 20/50/80 分位带 + 所处分位。

    只表达"处于历史什么位置"，不划买卖线（理杏仁式中立呈现）。
    """
    ak = _akshare()

    def _q(vals: list, p: float) -> float:
        if not vals:
            return 0.0
        idx = p * (len(vals) - 1)
        lo = int(idx)
        if lo + 1 >= len(vals):
            return vals[-1]
        frac = idx - lo
        return vals[lo] * (1 - frac) + vals[lo + 1] * frac

    metrics = {}
    for key, ind in (("pe_ttm", "市盈率(TTM)"), ("pb", "市净率")):
        try:
            df = ak.stock_zh_valuation_baidu(symbol=code, indicator=ind, period=period)
            raw = df.iloc[:, 1].dropna().astype(float).tolist()
            if not raw:
                continue
            cur = float(raw[-1])
            s = sorted(raw)
            below = sum(1 for x in s if x < cur)
            metrics[key] = {
                "current": round(cur, 2),
                "percentile": round(below / max(len(s) - 1, 1) * 100, 1),
                "min": round(s[0], 2), "max": round(s[-1], 2),
                "p20": round(_q(s, 0.2), 2), "p50": round(_q(s, 0.5), 2), "p80": round(_q(s, 0.8), 2),
                "n": len(s),
            }
        except Exception:
            continue
    return {"period": "近5年", "metrics": metrics}


def full_valuation(code: str) -> dict:
    """单票完整估值：腾讯行情 + 一致预期 EPS + 前向PE/PEG/消化年数。"""
    quotes = tencent_quote([code])
    q = quotes.get(code)
    if not q:
        raise ValueError(f"未取到 {code} 的行情")

    price = q["price"]
    out = {
        "name": q["name"], "code": code, "price": price,
        "mcap_yi": q["mcap_yi"], "pe_ttm": q["pe_ttm"], "pb": q["pb"],
        "eps_26e": None, "eps_27e": None, "pe_26e": None,
        "cagr_pct": None, "peg": None, "digest_years": None, "analyst_count": 0,
    }

    try:
        rows = profit_forecast(code)
    except DependencyMissing:
        out["forecast_note"] = "一致预期需安装 akshare"
        return out

    def _eps(row: dict):
        # 同花顺对覆盖不全的股票会缺「均值」或给 '-' 占位，硬取会让整只票的估值接口 502
        try:
            return float(str(row.get("均值", "")).replace(",", ""))
        except ValueError:
            return None

    eps_26 = eps_27 = None
    for row in rows:
        y = str(row.get("年度", ""))
        if "2026" in y:
            eps_26 = _eps(row)
            try:
                out["analyst_count"] = int(float(row.get("预测机构数") or 0))
            except (TypeError, ValueError):
                pass
        elif "2027" in y:
            eps_27 = _eps(row)

    out["eps_26e"], out["eps_27e"] = eps_26, eps_27
    if eps_26 and eps_26 > 0:
        pe_26e = price / eps_26
        out["pe_26e"] = round(pe_26e, 1)
        if eps_27:
            cagr = eps_27 / eps_26 - 1
            out["cagr_pct"] = round(cagr * 100, 0)
            peg = calc_peg(pe_26e, cagr)
            out["peg"] = round(peg, 2) if peg != float("inf") else None
            dig = pe_digestion(pe_26e, cagr)
            out["digest_years"] = round(dig, 1) if dig != float("inf") else None
    return out


# ===========================================================================
# Layer 3/4/10 · 资金面 / 筹码 / 信号（东财数据中心，移植自 a-stock-data v3.3）
#
# 合规：以下端点全部按【用户传入的单个代码】返回该股的客观公开数据（龙虎榜记录、
# 融资融券、大宗交易、股东户数、分红、资金流、解禁、板块归属、投资者问答），
# 不预置标的、不做主观评分、不给买卖建议。
# 定位调整（2026-07-05）：涨停池 / 全市场成交额榜等【客观公开榜单】现已用于产品 UI
# （每日复盘的连板股 + 成交额 TOP20）——如实展示公开榜单≠荐股，只要不附推荐/评分/预测。
# 仍不做：主观评分排名、买卖点位、涨跌预测；龙虎榜个股名单/强势股/人气榜等带隐性倾向的甩单暂不进 UI。
# ===========================================================================

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_MIN_INTERVAL = 1.0          # 两次东财请求最小间隔（秒），内置防封节流
_em_last_call = [0.0]
_EM_SESSIONS: dict = {}         # {direct(bool): requests.Session}

# 数据层连接模式：国内财经站（东财/腾讯/新浪）本应「直连」——很多用户开着 Clash/V2Ray
# 科学上网，系统代理会把东财这类国内站路由挂掉（典型：push2.eastmoney.com 的 CONNECT 被掐）。
# 默认 auto：先试直连、失败再降级走系统代理；探测一次后固定，避免每次都重试。
# 只有少数「必须靠代理才能出网」的环境需要 VR_DATA_PROXY=1 强制走代理。
# 注意：这只影响数据层；AI 层（可能要调国外模型）仍走各自的系统代理，不受影响。
_em_mode = ["proxy" if os.environ.get("VR_DATA_PROXY", "").strip().lower() in ("1", "true", "yes") else "auto"]


def _em_session(direct: bool):
    """东财专用会话。direct=True → `trust_env=False` 忽略 HTTP(S)_PROXY 环境变量、直连。

    直连会话不重试（探测要快，失败即降级）；代理会话保留瞬态错误退避重试。惰性构建、复用。
    """
    if direct in _EM_SESSIONS:
        return _EM_SESSIONS[direct]
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    s.trust_env = not direct     # 直连会话不读环境里的代理配置
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry = Retry(total=0) if direct else Retry(
            total=3, connect=3, backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    except Exception:
        pass  # 老版本 urllib3 缺参数时降级为无重试
    _EM_SESSIONS[direct] = s
    return s


def em_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 15):
    """东财统一请求入口：串行限流 + **直连优先、失败降级系统代理**（避免科学上网代理挂掉国内站）。

    第一次请求探测：先直连（短超时、不重试），成功即固定走直连；失败则降级走系统代理并固定。
    探测结果整个进程复用，避免每次重试。`VR_DATA_PROXY=1` 可跳过探测、强制走代理。
    """
    wait = _EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        mode = _em_mode[0]
        if mode != "auto":
            return _em_session(mode == "direct").get(url, params=params, headers=headers, timeout=timeout)
        # auto：先直连，成功固定 direct；直连失败再走系统代理、成功固定 proxy。
        try:
            r = _em_session(True).get(url, params=params, headers=headers, timeout=min(timeout, 8))
            _em_mode[0] = "direct"
            return r
        except Exception:
            r = _em_session(False).get(url, params=params, headers=headers, timeout=timeout)
            _em_mode[0] = "proxy"
            return r
    finally:
        _em_last_call[0] = time.time()


# ---------------------------------------------------------------------------
# 打板层 · 涨停/炸板/跌停/昨涨停 原始池（东财 push2ex，走 em_get 限流）
# ⚠️ 合规：原始池含个股 code/name —— 仅供 market.py 聚合成【不含个股名】的短线情绪指标。
#    切勿把原始池直接接成 API/UI（会甩个股名单、破产品「零标的」红线）。
# ---------------------------------------------------------------------------
_ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"


def em_zt_topic_pool(endpoint: str, date: str, sort: str = "fbt:asc") -> list[dict]:
    """东财涨停板行情中心原始池（push2ex）。
    endpoint: getTopicZTPool(涨停) / getTopicZBPool(炸板) / getTopicDTPool(跌停) / getYesterdayZTPool(昨涨停)
    date: YYYYMMDD 交易日。非交易日 / 参数错 → []。
    池内每项字段含 lbc(连板数) / zbc(炸板次数) / hybk(行业) 等。"""
    url = f"https://push2ex.eastmoney.com/{endpoint}"
    params = {"ut": _ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0,
              "pagesize": 10000, "sort": sort, "date": date}
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = em_get(url, params=params, headers=headers, timeout=10)
        return (r.json().get("data") or {}).get("pool") or []
    except Exception:
        return []


def _numf(v):
    """东财数值字段可能是 '-'（停牌/无数据）→ 归一成 float 或 None。"""
    return v if isinstance(v, (int, float)) else None


def market_turnover_rank(n: int = 20) -> list[dict]:
    """全市场成交额榜（沪深京 A 股按成交额降序 TopN）。

    东财行情中心 clist。**push2(实时) 不可达时降级 push2delay(延迟行情，日榜场景足够)**。
    返回每只: code / name / price / pct / amount(成交额,元) / mcap(总市值,元) /
    float_cap(流通市值,元) / industry。
    ⚠️ 这是客观公开榜单数据（东财/同花顺同款），产品侧只做客观展示——非推荐、非预测、不评分。
    """
    params = {"pn": 1, "pz": n, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f6",
              "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
              "fields": "f12,f14,f2,f3,f6,f20,f21,f100"}
    diff: list[dict] = []
    for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
        try:
            r = em_get(f"https://{host}/api/qt/clist/get", params=params,
                       headers={"User-Agent": UA}, timeout=12)
            diff = (r.json().get("data") or {}).get("diff") or []
            if diff:
                break
        except Exception:
            continue
    return [{
        "code": str(d.get("f12", "")), "name": d.get("f14", ""),
        "price": _numf(d.get("f2")), "pct": _numf(d.get("f3")),
        "amount": _numf(d.get("f6")), "mcap": _numf(d.get("f20")),
        "float_cap": _numf(d.get("f21")), "industry": d.get("f100", "") or "",
    } for d in diff]


def eastmoney_datacenter(report_name: str, columns: str = "ALL", filter_str: str = "",
                         page_size: int = 50, sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    """东财数据中心统一查询 —— 龙虎榜/解禁/融资融券/大宗交易/股东户数/分红 共用（已内置限流）。"""
    params = {
        "reportName": report_name, "columns": columns, "filter": filter_str,
        "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types, "source": "WEB", "client": "WEB",
    }
    try:
        d = em_get(_DATACENTER_URL, params=params, timeout=15).json()
    except Exception:
        return []
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


def margin_trading(code: str, page_size: int = 30) -> list[dict]:
    """融资融券明细（日级）：融资余额 / 融资买入 / 融券余额 / 两融合计。"""
    data = eastmoney_datacenter(
        "RPTA_WEB_RZRQ_GGMX", filter_str=f'(SCODE="{code}")',
        page_size=page_size, sort_columns="DATE", sort_types="-1")
    return [{
        "date": str(r.get("DATE", ""))[:10],
        "rzye": r.get("RZYE", 0), "rzmre": r.get("RZMRE", 0), "rzche": r.get("RZCHE", 0),
        "rqye": r.get("RQYE", 0), "rqmcl": r.get("RQMCL", 0),
        "rzrqye": r.get("RZRQYE", 0),
    } for r in data]


def block_trade(code: str, page_size: int = 20) -> list[dict]:
    """大宗交易：成交价 / 折溢价率 / 量 / 买卖方营业部。"""
    data = eastmoney_datacenter(
        "RPT_DATA_BLOCKTRADE", filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size, sort_columns="TRADE_DATE", sort_types="-1")
    rows = []
    for r in data:
        close = r.get("CLOSE_PRICE") or 0
        deal = r.get("DEAL_PRICE") or 0
        rows.append({
            "date": str(r.get("TRADE_DATE", ""))[:10],
            "price": deal, "close": close,
            "premium_pct": round((deal / close - 1) * 100, 2) if close else 0,
            "vol": r.get("DEAL_VOLUME", 0), "amount": r.get("DEAL_AMT", 0),
            "buyer": r.get("BUYER_NAME", ""), "seller": r.get("SELLER_NAME", ""),
        })
    return rows


def holder_num_change(code: str, page_size: int = 10) -> list[dict]:
    """股东户数变化（季度级）：户数 / 环比 / 户均持股。持续减少 = 筹码集中。"""
    data = eastmoney_datacenter(
        "RPT_HOLDERNUMLATEST", filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size, sort_columns="END_DATE", sort_types="-1")
    return [{
        "date": str(r.get("END_DATE", ""))[:10],
        "holder_num": r.get("HOLDER_NUM", 0),
        "change_ratio": r.get("HOLDER_NUM_RATIO", 0),
        "avg_shares": r.get("AVG_FREE_SHARES", 0),
    } for r in data]


def dividend_history(code: str, page_size: int = 20) -> list[dict]:
    """分红送转历史：每股派息（税前）/ 每10股转增 / 每10股送股 / 进度。"""
    data = eastmoney_datacenter(
        "RPT_SHAREBONUS_DET", filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size, sort_columns="EX_DIVIDEND_DATE", sort_types="-1")
    return [{
        "date": str(r.get("EX_DIVIDEND_DATE", ""))[:10],
        "bonus_rmb": r.get("PRETAX_BONUS_RMB", 0),
        "transfer_ratio": r.get("TRANSFER_RATIO", 0),
        "bonus_ratio": r.get("BONUS_RATIO", 0),
        "plan": r.get("ASSIGN_PROGRESS", ""),
    } for r in data]


def stock_fund_flow_120d(code: str) -> list[dict]:
    """个股资金流（日级，最近 120 交易日）：主力 / 小单 / 中单 / 大单 / 超大单净流入（元）。"""
    market_code = 1 if code.startswith("6") else 0
    params = {
        "secid": f"{market_code}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": "120",
    }
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Origin": "https://quote.eastmoney.com"}
    try:
        d = em_get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                   params=params, headers=headers, timeout=15).json()
    except Exception:
        return []
    rows = []
    for line in d.get("data", {}).get("klines", []):
        p = line.split(",")
        if len(p) >= 6:
            def _f(x):
                try:
                    return float(x) if x not in ("-", "") else 0.0
                except ValueError:
                    return 0.0
            rows.append({
                "date": p[0], "main_net": _f(p[1]), "small_net": _f(p[2]),
                "mid_net": _f(p[3]), "large_net": _f(p[4]), "super_net": _f(p[5]),
            })
    return rows


def dragon_tiger_board(code: str, trade_date: str | None = None, look_back: int = 30) -> dict:
    """龙虎榜：该股近期上榜记录 + 最近一次买卖席位 TOP5 + 机构专用席位净买。"""
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)).strftime("%Y-%m-%d")
    records = []
    data = eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f'(TRADE_DATE>=\'{start}\')(TRADE_DATE<=\'{trade_date}\')(SECURITY_CODE="{code}")',
        page_size=50, sort_columns="TRADE_DATE", sort_types="-1")
    for r in data:
        records.append({
            "date": str(r.get("TRADE_DATE", ""))[:10],
            "reason": r.get("EXPLANATION", ""),
            "net_buy": round((r.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),  # 万元
            "turnover": round(float(r.get("TURNOVERRATE") or 0), 2),
        })

    seats = {"buy": [], "sell": []}
    institution = {"buy_amt": 0.0, "sell_amt": 0.0, "net_amt": 0.0}
    if records:
        latest = records[0]["date"]
        buy_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            filter_str=f'(TRADE_DATE=\'{latest}\')(SECURITY_CODE="{code}")',
            page_size=10, sort_columns="BUY", sort_types="-1")
        sell_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSSELL",
            filter_str=f'(TRADE_DATE=\'{latest}\')(SECURITY_CODE="{code}")',
            page_size=10, sort_columns="SELL", sort_types="-1")
        for r in buy_data[:5]:
            seats["buy"].append({"name": r.get("OPERATEDEPT_NAME", ""),
                                 "buy_amt": round((r.get("BUY") or 0) / 10000, 1),
                                 "sell_amt": round((r.get("SELL") or 0) / 10000, 1),
                                 "net": round((r.get("NET") or 0) / 10000, 1)})
        for r in sell_data[:5]:
            seats["sell"].append({"name": r.get("OPERATEDEPT_NAME", ""),
                                  "buy_amt": round((r.get("BUY") or 0) / 10000, 1),
                                  "sell_amt": round((r.get("SELL") or 0) / 10000, 1),
                                  "net": round((r.get("NET") or 0) / 10000, 1)})
        for detail, side in ((buy_data, "buy"), (sell_data, "sell")):
            for r in detail:
                if str(r.get("OPERATEDEPT_CODE", "")) == "0":  # 机构专用席位
                    amt = (r.get("BUY") or 0) if side == "buy" else (r.get("SELL") or 0)
                    institution[f"{side}_amt"] += amt
        institution["buy_amt"] = round(institution["buy_amt"] / 10000, 1)
        institution["sell_amt"] = round(institution["sell_amt"] / 10000, 1)
        institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)
    return {"records": records, "seats": seats, "institution": institution}


def lockup_expiry(code: str, trade_date: str | None = None, forward_days: int = 90) -> dict:
    """限售解禁日历：历史解禁记录 + 未来 N 天待解禁事件。

    字段随东财 2026 改列名同步（a-stock-data §3.6）：旧 LIMITED_STOCK_TYPE/FREE_SHARES_NUM
    已废、致 type/shares 恒空 → 改 FREE_SHARES_TYPE/FREE_SHARES，并补 able_shares（实际可流通股数）。
    """
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    history = [{
        "date": str(r.get("FREE_DATE", ""))[:10], "type": r.get("FREE_SHARES_TYPE", ""),
        "shares": r.get("FREE_SHARES", 0), "able_shares": r.get("ABLE_FREE_SHARES", 0),
        "ratio": r.get("FREE_RATIO", 0),
    } for r in eastmoney_datacenter(
        "RPT_LIFT_STAGE", filter_str=f'(SECURITY_CODE="{code}")',
        page_size=15, sort_columns="FREE_DATE", sort_types="-1")]

    end = (datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=forward_days)).strftime("%Y-%m-%d")
    upcoming = [{
        "date": str(r.get("FREE_DATE", ""))[:10], "type": r.get("FREE_SHARES_TYPE", ""),
        "shares": r.get("FREE_SHARES", 0), "able_shares": r.get("ABLE_FREE_SHARES", 0),
        "ratio": r.get("FREE_RATIO", 0),
    } for r in eastmoney_datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{trade_date}\')(FREE_DATE<=\'{end}\')',
        page_size=20, sort_columns="FREE_DATE", sort_types="1")]
    return {"history": history, "upcoming": upcoming}


def concept_blocks(code: str) -> dict:
    """个股所属板块/概念归属（东财 slist，行业/概念/地域混合，板块名自解释）。"""
    market_code = 1 if code.startswith("6") else 0
    params = {"fltt": "2", "invt": "2", "secid": f"{market_code}.{code}",
              "spt": "3", "pi": "0", "pz": "200", "po": "1", "fields": "f12,f14,f3,f128"}
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        d = em_get("https://push2.eastmoney.com/api/qt/slist/get", params=params, headers=headers, timeout=15).json()
    except Exception:
        return {"total": 0, "boards": [], "concept_tags": []}
    diff = (d.get("data") or {}).get("diff") or {}
    items = diff.values() if isinstance(diff, dict) else diff
    boards = [{"name": it.get("f14", ""), "code": it.get("f12", ""),
               "change_pct": it.get("f3", ""), "lead_stock": it.get("f128", "")} for it in items]
    return {"total": len(boards), "boards": boards, "concept_tags": [b["name"] for b in boards]}


def hot_concepts(code: str) -> list[dict]:
    """个股当下被市场归到哪些概念在炒（东财热门概念命中，按热度降序）。"""
    import requests

    try:
        prefix = "SH" if code.startswith("6") else "SZ"
        r = requests.post(
            "https://emappdata.eastmoney.com/stockrank/getHotStockRankList",
            json={"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38", "srcSecurityCode": prefix + code},
            headers={"User-Agent": UA}, timeout=10)
        data = r.json().get("data") or []
    except Exception:
        return []
    return [{"concept": x.get("conceptName"), "bk": x.get("conceptId"), "hit": x.get("hitCount")} for x in data]


def investor_qa(code: str, page_size: int = 30) -> list[dict]:
    """互动易问答（巨潮）：投资者提问 + 公司回复（answer=None 表示未回复）。"""
    import requests

    try:
        r1 = requests.post("https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
                           data={"keyWord": code}, headers={"User-Agent": UA}, timeout=10)
        d1 = r1.json().get("data") or []
        if not d1:
            return []
        org_id = d1[0].get("secid")
        params = {"_t": 1, "stockcode": code, "orgId": org_id, "pageSize": page_size,
                  "pageNum": 1, "keyWord": "", "startDay": "", "endDay": ""}
        rows = requests.post("https://irm.cninfo.com.cn/newircs/company/question",
                             params=params, headers={"User-Agent": UA}, timeout=10).json().get("rows") or []
    except Exception:
        return []
    out = []
    for it in rows:
        ts = it.get("pubDate")
        out.append({
            "company": it.get("companyShortName"),
            "question": it.get("mainContent"), "answer": it.get("attachedContent"),
            "answerer": it.get("attachedAuthor"),
            "ask_time": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M") if ts else "",
        })
    return out


def industry_comparison(top_n: int = 20) -> dict:
    """全行业涨跌幅排名（东财行业板块，~100 个行业）：板块级涨跌 / 涨跌家数 / 领涨。"""
    params = {"pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
              "fid": "f3",  # fid=f3 + po=1：按涨跌幅降序，否则 top/bottom 切片非涨幅序（a-stock-data §3.7）
              "fs": "m:90+t:2", "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207"}
    try:
        d = em_get("https://push2.eastmoney.com/api/qt/clist/get",
                   params=params, headers={"User-Agent": UA}, timeout=15).json()
    except Exception:
        return {"top": [], "bottom": [], "total": 0}
    items = d.get("data", {}).get("diff", [])
    if isinstance(items, dict):
        items = list(items.values())
    if not items:
        return {"top": [], "bottom": [], "total": 0}
    rows = [{
        "rank": i + 1, "name": it.get("f14", ""), "change_pct": it.get("f3", 0),
        "code": it.get("f12", ""), "up_count": it.get("f104", 0), "down_count": it.get("f105", 0),
    } for i, it in enumerate(items)]
    return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows)}


# ---------------------------------------------------------------------------
# 指数日K（东财）+ 业绩报表快照 —— 板块评分数据层
# ---------------------------------------------------------------------------

_CSI_ALL_SHARE_SECID = "1.000985"  # 中证全指（板块评分的全A基准）


def index_daily_em(secid: str, days: int = 260) -> list[dict]:
    """东财指数日K线（无鉴权 HTTP）：中证全指/沪深300/上证指数等。

    东财 push2his 被掐时降级腾讯指数日K（proxy.finance.qq.com）。
    """
    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "0", "beg": "0", "end": "20500101",
        "lmt": str(days), "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    try:
        d = em_get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params=params, headers={"User-Agent": UA}, timeout=15,
        ).json()
    except Exception:
        d = {}
    klines = (d.get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            rows.append({
                "date": parts[0][:10],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]) if len(parts) > 6 else 0.0,
            })
        except (TypeError, ValueError):
            continue
    if rows:
        return rows
    return _index_daily_tx(secid, days)


def _index_daily_tx(secid: str, days: int = 260) -> list[dict]:
    """腾讯指数日K（东财 push2his 备用）。secid '1.000985' → 'sh000985'。"""
    try:
        market, code = secid.split(".", 1)
        symbol = ("sh" if market == "1" else "sz") + code
        import requests as _req

        session = _req.Session()
        session.trust_env = False
        resp = session.get(
            _TENCENT_KLINE_FALLBACK,
            params={"_var": "k", "param": f"{symbol},day,,,{days},", "r": "0.1"},
            headers={"User-Agent": UA}, timeout=12,
        )
        text = resp.text
        payload = json.loads(text[text.find("={") + 1:])
        data = (payload.get("data") or {}).get(symbol) or {}
        raw = data.get("day") or data.get("qfqday") or []
        rows = []
        for item in raw:
            if not isinstance(item, list) or len(item) < 6:
                continue
            try:
                # 腾讯指数日K第7列是 {} 占位，成交额在第9列；个股前复权第7列即成交额。
                amount_raw = item[8] if len(item) > 8 else (item[6] if len(item) > 6 else 0.0)
                if isinstance(amount_raw, dict):
                    amount_raw = 0.0
                rows.append({
                    "date": str(item[0])[:10],
                    "open": float(item[1]),
                    "close": float(item[2]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                    "volume": float(item[5]),
                    "amount": float(amount_raw),
                })
            except (TypeError, ValueError):
                continue
        return rows
    except Exception:  # noqa: BLE001
        return []


def csi_all_share_daily(days: int = 260) -> list[dict]:
    """中证全指日K线（板块评分的全A基准）。"""
    return index_daily_em(_CSI_ALL_SHARE_SECID, days)


def yjbb_snapshot(date_str: str) -> dict[str, dict]:
    """东财业绩报表快照：指定报告期全 A 股的营收/净利同比。

    返回 {code: {revenue_yoy, profit_yoy, roe, gross_margin}}。
    date_str 格式 '20260331'。
    """
    try:
        ak = _akshare()
        df = ak.stock_yjbb_em(date=date_str)
    except Exception:
        return {}
    if df is None or (hasattr(df, "empty") and df.empty):
        return {}
    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        code = str(row.get("股票代码", "")).zfill(6)
        if not code or len(code) != 6:
            continue

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        out[code] = {
            "revenue_yoy": _num(row.get("营业总收入-同比增长")),
            "profit_yoy": _num(row.get("净利润-同比增长")),
            "roe": _num(row.get("净资产收益率")),
            "gross_margin": _num(row.get("销售毛利率")),
        }
    return out


def concept_constituents_em(board_codes: list[str], limit: int | None = None) -> list[dict]:
    """东财概念/行业板块成分股，按流通市值降序合并去重。

    默认分页拉取板块全部成分股；传入 limit 时每个板块最多保留 limit 只。
    """
    merged: dict[str, dict] = {}
    for board_code in board_codes:
        page = 1
        fetched = 0
        while page <= 50:
            payload = None
            for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
                try:
                    response = em_get(
                        f"https://{host}/api/qt/clist/get",
                        params={
                            "pn": str(page), "pz": "1000", "po": "1", "np": "1",
                            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
                            "fid": "f21", "fields": "f12,f14,f21",
                            "fs": f"b:{board_code}+f:!50",
                        },
                        headers={"User-Agent": UA},
                        timeout=15,
                    )
                    payload = response.json()
                    if payload.get("data") is not None:
                        break
                    payload = None
                except Exception:
                    payload = None
            data = (payload or {}).get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                break
            for row in diff:
                code = str(row.get("f12") or "")
                if len(code) != 6 or not code.isdigit():
                    continue
                item = {
                    "code": code,
                    "name": str(row.get("f14") or code),
                    "float_mcap": _number(row.get("f21")) or 0.0,
                    "source": f"eastmoney:{board_code}",
                }
                previous = merged.get(code)
                if previous is None or item["float_mcap"] > previous["float_mcap"]:
                    merged[code] = item
            fetched += len(diff)
            total = data.get("total") or 0
            if limit is not None and fetched >= limit:
                break
            if fetched >= total:
                break
            page += 1
    return sorted(merged.values(), key=lambda item: -item["float_mcap"])


def profit_forecast_revision_em(code: str) -> dict:
    """东财 F10 下一预测年度 EPS 相对上月的一致预期修正。"""
    import requests

    symbol = f"{get_prefix(code).upper()}{code}"
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax",
        params={"code": symbol},
        headers={"User-Agent": UA, "Referer": "https://emweb.securities.eastmoney.com/"},
        timeout=15,
    )
    rows = response.json().get("yctj_list") or []
    forecasts = sorted(
        (row for row in rows if str(row.get("YEAR_MARK") or "").upper() == "E"),
        key=lambda row: int(row.get("YEAR") or 9999),
    )
    if not forecasts:
        return {}
    row = forecasts[0]
    current = _number(row.get("EPS"))
    previous = _number(row.get("EPS_LASTMONTHS"))
    revision = (
        (current / previous - 1) * 100
        if current is not None and previous not in (None, 0)
        else None
    )
    return {
        "year": int(row.get("YEAR") or 0) or None,
        "eps": current,
        "eps_last_month": previous,
        "revision_pct": round(max(-100.0, min(100.0, revision)), 2) if revision is not None else None,
    }
