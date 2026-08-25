"""数据源健康检查层 —— 一次轻量探活 + 各页面缓存/快照状态汇总。

两层信息，全部只读、不触发重建：
  1. upstreams：对关键上游发一次极小请求（timeout 8s、单请求单响应），
     判断「现在能不能拿到数据」；
  2. datasets：读各模块最近一次成功结果的时间戳（cache_runtime 条目 /
     落盘快照），判断「页面现在渲染的数据有多旧」。

探活带 60s 节流：连点刷新按钮不会打爆上游（它们大多有风控/限流）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import astock
import cache_runtime
import gold_score
import market

BEIJING = timezone(timedelta(hours=8))
_PROBE_THROTTLE = 60  # 秒；两次探活的最小间隔

_probe_lock = threading.Lock()
_last_probe: dict[str, dict] = {}
_last_probe_at = 0.0


def _now_str() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 探活：每个源一个最小请求（与正式抓取同一入口，测的才是真实链路）
# ---------------------------------------------------------------------------

def _probe_tencent() -> str | None:
    """腾讯行情（指数实时，仅标准库）。"""
    rows = astock.index_quote()
    return None if rows else "无行情返回"


def _probe_sina() -> str | None:
    """新浪期货日 K（黄金 AU0 / 油价 OIL 共用的结算价源）。"""
    try:
        value = astock._futures_prev_settlement("AU0")
    except Exception as e:  # noqa: BLE001
        return f"异常：{e}"
    return None if value else "无数据返回"


def _probe_em_push2() -> str | None:
    """东财 push2 系（全球指数 / 分钟线；记忆里对部分 IP 有 TCP 断连风控）。

    与 astock._TRENDS_HOSTS 同一主机序列：主站被风控时降级主机仍可用，
    全挂才算这个源挂了。
    """
    import requests

    s = requests.Session()
    s.trust_env = False  # push2 走系统代理必挂，直连才是真实链路（对齐 astock.em_index_minutes）
    last_error = "全部主机无数据"
    for host in ("push2his.eastmoney.com", "push2.eastmoney.com", "push2delay.eastmoney.com"):
        try:
            r = s.get(
                f"https://{host}/api/qt/stock/trends2/get",
                params={"secid": "1.000001", "fields1": "f1", "fields2": "f51,f53", "ndays": "2", "iscr": "0"},
                headers={"User-Agent": astock.UA, "Referer": "https://quote.eastmoney.com/"},
                timeout=8,
            )
            data = r.json().get("data") or {}
            if data.get("trends"):
                return None
            last_error = f"{host} 返回体无数据"
        except Exception as e:  # noqa: BLE001
            last_error = f"{host} 连接失败：{type(e).__name__}"
    return last_error


def _probe_em_datacenter() -> str | None:
    """东财数据中心（融资融券/解禁/龙虎榜/大宗/股东户数/分红共用）。"""
    rows = astock.eastmoney_datacenter("RPTA_WEB_RZRQ_GGMX", columns="ALL", filter_str="(scode=\"600519\")", page_size=1)
    return None if rows else "无数据返回"


def _probe_em_search() -> str | None:
    """东财搜索 suggest（个股搜索框；走 em_get 含直连/代理自适应）。"""
    try:
        r = astock.em_get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={"input": "600519", "type": 14, "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 5},
            headers={"User-Agent": astock.UA}, timeout=8,
        )
        rows = (r.json().get("QuotationCodeTable") or {}).get("Data") or []
        return None if rows else "无数据返回"
    except Exception as e:  # noqa: BLE001
        return f"连接失败：{type(e).__name__}"


def _probe_sws() -> str | None:
    """申万研究（行业评分月报 / 二级行业日频）。"""
    import requests

    try:
        r = requests.get(
            "https://www.swsresearch.com/institute-sw/api/index_analysis/week_month_datetime/",
            params={"page": "1", "pagesize": "1"},
            headers={"User-Agent": astock.UA, "Referer": "https://www.swsresearch.com/"},
            timeout=8, verify=False,
        )
        return None if str(r.json().get("code")) == "200" else f"接口码 {r.json().get('code')}"
    except Exception as e:  # noqa: BLE001
        return f"连接失败：{type(e).__name__}"


def _probe_fred() -> str | None:
    """FRED CSV（黄金/宏观的多条底层序列）。"""
    rows = market._fred_csv("DGS10", 1)
    return None if rows else "CSV 无数据返回"


def _probe_chinabond() -> str | None:
    """中债收益率曲线（AKShare 转接；探 3 日窗口最小请求）。"""
    try:
        ak = astock._akshare()
        end = datetime.now(BEIJING)
        start = end - timedelta(days=3)
        df = ak.bond_china_yield(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        return None if df is not None and not df.empty else "无数据返回"
    except Exception as e:  # noqa: BLE001
        return f"异常：{type(e).__name__}"


def _probe_frankfurter() -> str | None:
    """frankfurter 汇率（油价页美元指数合成的源）。"""
    import requests

    try:
        r = requests.get("https://api.frankfurter.dev/v1/latest?base=USD", timeout=8)
        return None if r.json().get("rates") else "返回体无汇率"
    except Exception as e:  # noqa: BLE001
        return f"连接失败：{type(e).__name__}"


def _probe_eia() -> str | None:
    """EIA v2 API（油价页周度数据兜底源；DEMO_KEY 限流严格）。"""
    import requests

    key = __import__("os").environ.get("VR_EIA_API_KEY") or "DEMO_KEY"
    try:
        r = requests.get(
            "https://api.eia.gov/v2/petroleum/sum/sndw/data/",
            params={"api_key": key, "frequency": "weekly", "data[0]": "value",
                    "facets[product][]": "WCESTUS1", "length": "1"},
            timeout=8,
        )
        body = r.json()
        return None if body.get("response", {}).get("data") else "无数据返回（可能是 DEMO_KEY 限流）"
    except Exception as e:  # noqa: BLE001
        return f"连接失败：{type(e).__name__}"


def _probe_polymarket() -> str | None:
    """Polymarket Gamma API（全球预期页主源之一）。"""
    import requests

    try:
        r = requests.get("https://gamma-api.polymarket.com/events", params={"limit": 1}, timeout=8)
        return None if isinstance(r.json(), list) and r.json() else "返回体为空"
    except Exception as e:  # noqa: BLE001
        return f"连接失败：{type(e).__name__}"


def _probe_binance() -> str | None:
    """Binance 公共行情镜像（PAXG 主源；挂了黄金页国内金价折算退化）。"""
    try:
        raw = gold_score._binance_get("/api/v3/ticker/price?symbol=PAXGUSDT", timeout=8)
        if not raw:
            return "无数据返回"
        import json as _json

        price = _json.loads(raw).get("price")
        return None if price else "返回体无价格"
    except Exception as e:  # noqa: BLE001
        return f"异常：{type(e).__name__}"


def _probe_kalshi() -> str | None:
    """Kalshi 交易 API（全球预期页双源之一；挂了只剩 Polymarket）。"""
    import subprocess as _sp

    try:
        r = _sp.run(
            ["curl", "-L", "-s", "--max-time", "8",
             "https://api.elections.kalshi.com/trade-api/v2/events?limit=1&status=open&series_ticker=KXFED"],
            capture_output=True, text=True, timeout=12,
        )
        if r.returncode != 0 or not r.stdout:
            return "无数据返回"
        import json as _json

        return None if _json.loads(r.stdout).get("events") else "返回体无事件"
    except Exception as e:  # noqa: BLE001
        return f"异常：{type(e).__name__}"


# 探活清单：key / 展示名 / 所属页面 / 探活函数
_PROBES: list[tuple[str, str, str, object]] = [
    ("tencent",   "腾讯行情",        "行情 / 指数 / 自选 / 持仓", _probe_tencent),
    ("sina",      "新浪财经",        "黄金 / 油价 / 资金流兜底", _probe_sina),
    ("em_push2",  "东财 push2 系",   "全球指数 / 分时 / 资金流", _probe_em_push2),
    ("em_dc",     "东财数据中心",    "两融 / 解禁 / 龙虎榜 / 大宗", _probe_em_datacenter),
    ("em_search", "东财搜索",        "个股搜索框", _probe_em_search),
    ("sws",       "申万研究",        "行业评分 / 二级行业", _probe_sws),
    ("fred",      "FRED",           "黄金 / 宏观 / 资金面", _probe_fred),
    ("chinabond", "中债曲线(AKShare)", "债市页", _probe_chinabond),
    ("frankfurter", "frankfurter 汇率", "油价页美元指数", _probe_frankfurter),
    ("eia",       "EIA 石油数据",    "油价页周度数据", _probe_eia),
    ("polymarket", "Polymarket",     "全球预期页", _probe_polymarket),
    ("binance",   "Binance 镜像",    "黄金页 PAXG 折算", _probe_binance),
    ("kalshi",    "Kalshi",         "全球预期页", _probe_kalshi),
]


def probe_upstreams(force: bool = False, only: list[str] | None = None) -> dict[str, dict]:
    """并发探活上游。60s 节流（force 或 only 指定时才重探对应源）。

    only = [key...] 时只探指定源并合并进既有结果——单源按钮的按需探活，
    其余源沿用上次结果，不打全量请求。
    """
    global _last_probe_at
    with _probe_lock:
        if only:
            targets = [p for p in _PROBES if p[0] in set(only)]
            if not targets:
                return dict(_last_probe)
        else:
            if not force and _last_probe_at and time.time() - _last_probe_at < _PROBE_THROTTLE:
                return dict(_last_probe)
            _last_probe_at = time.time()
            targets = list(_PROBES)

    from concurrent.futures import ThreadPoolExecutor

    def run(item: tuple[str, str, str, object]) -> tuple[str, dict]:
        key, name, pages, fn = item
        started = time.time()
        try:
            error = fn()
        except Exception as e:  # noqa: BLE001 — 单源失败不拖垮整页
            error = f"异常：{e}"
        return key, {
            "key": key, "name": name, "pages": pages,
            "status": "ok" if error is None else "fail",
            "error": error,
            "latency_ms": int((time.time() - started) * 1000),
            "probed_at": _now_str(),
        }

    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        results = dict(ex.map(run, targets))

    with _probe_lock:
        if only:
            _last_probe.update(results)
            return dict(_last_probe)
        _last_probe.clear()
        _last_probe.update(results)
        return dict(results)


# ---------------------------------------------------------------------------
# 各页面数据集状态：读缓存条目 / 落盘快照的时间戳，不触发任何外呼
# ---------------------------------------------------------------------------

# cache_runtime key → (展示名, 所属页面)
_RUNTIME_KEYS: list[tuple[str, str]] = [
    ("market_sentiment", "市场情绪"),
    ("sector_flows", "板块资金流"),
    ("liquidity", "流动性综合"),
    ("macro", "宏观指标"),
    ("bonds:curve", "中债曲线"),
    ("bonds:overview", "债市总览"),
    ("bonds:framework", "八状态框架"),
    ("bonds:segments", "分品种评分"),
    ("bonds:positioning", "仓位拥挤度"),
    ("gold_score_v3", "黄金评分"),
    ("oil_score_v1", "油价评分"),
    ("timing:allocation", "择时配置"),
    ("sector_scores:v6", "行业评分"),
    ("sw_level2_scores:v2", "申万二级行业"),
    ("plate_scores:v5", "板块双评分"),
    ("rss_radar:v2", "资讯雷达"),
    ("fund_pfs:v2", "基金 PFS"),
]

_PAGE_OF_KEY = {
    "market_sentiment": "市场全景", "sector_flows": "市场全景", "liquidity": "资金面",
    "macro": "宏观面", "bonds:curve": "债市", "bonds:overview": "债市",
    "bonds:framework": "债市", "bonds:segments": "债市", "bonds:positioning": "债市",
    "gold_score_v3": "黄金", "oil_score_v1": "油价", "timing:allocation": "择时配置",
    "sector_scores:v6": "行业研究", "sw_level2_scores:v2": "行业研究",
    "plate_scores:v5": "行业研究", "rss_radar:v2": "资讯", "fund_pfs:v2": "标的筛选",
}

# 各模块快照/缓存文件 → (模块 id, 展示名)；mtime 即最近一次成功构建
_SNAPSHOT_KEYS = [
    ("pulse", "全球预期"),
    ("factor_data", "因子研究"),
]


def _snapshot_state(module: str) -> dict | None:
    """pulse / factor_data 的快照状态（文件 mtime + 简单内容摘要）。"""
    import os

    base = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
    if module == "pulse":
        path = os.path.join(base, "pulse", "pulse_snapshot.json")
        try:
            return {
                "key": "pulse", "name": "全球预期", "page": "全球预期",
                "cached_at": datetime.fromtimestamp(os.path.getmtime(path), BEIJING).isoformat(timespec="seconds"),
                "cache_state": "fresh", "refresh_error": None,
            }
        except OSError:
            return None
    if module == "factor_data":
        import factor_data

        catalog = factor_data.load_catalog()
        if not catalog:
            return None
        return {
            "key": "factor_data", "name": "因子数据集", "page": "因子研究",
            "cached_at": catalog.get("built_at"),
            "cache_state": "fresh", "refresh_error": None,
            "detail": f"{catalog.get('n_codes', '?')} 标的 × {catalog.get('n_days', '?')} 交易日",
        }
    return None


def collect_datasets() -> list[dict]:
    """汇总各页面数据集的最近成功时间与缓存状态（零外呼）。"""
    out: list[dict] = []
    for key, name in _RUNTIME_KEYS:
        value = cache_runtime.peek(key)
        entry = cache_runtime._entries.get(key)  # noqa: SLF001 — 同仓模块间只读访问
        if value is None and entry is None:
            continue
        state = "fresh"
        error = None
        if entry is not None:
            error = entry.error
            state = "error" if entry.error else "refreshing" if entry.refreshing else "fresh"
        cached_at = None
        if entry is not None and entry.cached_at:
            cached_at = datetime.fromtimestamp(entry.cached_at, BEIJING).isoformat(timespec="seconds")
        out.append({
            "key": key, "name": name,
            "page": _PAGE_OF_KEY.get(key, name),
            "cached_at": cached_at, "cache_state": state, "refresh_error": error,
        })
    for module, _name in _SNAPSHOT_KEYS:
        state = _snapshot_state(module)
        if state:
            out.append(state)
    out.sort(key=lambda d: d.get("name") or "")
    return out


def build_report(force: bool = False, only: list[str] | None = None) -> dict:
    """健康报告：探活结果 + 数据集状态 + 汇总（version 由 app 层补充）。

    only = [key...] 只重探指定上游（单源按钮），其余沿用上次结果。
    """
    upstreams = probe_upstreams(force=force, only=only)
    datasets = collect_datasets()
    failed = [u["name"] for u in upstreams.values() if u["status"] != "ok"]
    errored = [d["name"] for d in datasets if d.get("cache_state") == "error"]
    return {
        "checked_at": _now_str(),
        "upstreams": list(upstreams.values()),
        "datasets": datasets,
        "summary": {
            "upstream_total": len(upstreams),
            "upstream_failed": failed,
            "dataset_error": errored,
            "all_ok": not failed and not errored,
        },
    }
