"""黄金价格多维评分系统（方案 V2.1，沿用 research/黄金价格多维评分系统技术方案_V2.0.md）。

5 维 8 指标，总分 100：
  G01 美国10年期TIPS实际收益率        美国财政部（日；FRED兜底/校验）
  G02 广义贸易加权美元指数            FRED DTWEXBGS（日）
  G03 全球实物黄金ETF超预期流量      WGC 100+基金（周，吨）
  G04 COMEX管理基金净多头动量         CFTC Disaggregated COT 官方年报（周）
  G05 OFR金融压力（剔除安全资产）      OFR 官方 fsi.csv（日）
  G06 全球已报告央行滚动储备变动      IMF IFS（月，吨）
  G07 上海—伦敦黄金溢价              WGC/ICE LBMA AM 与 SGE PM（日）
  G08 黄金中期风险调整动量           WGC/ICE LBMA PM USD/oz（日）

单项得分 = 原始信号历史分位 × 100（G06 用 2010 年以来月度序列，其余 5 年日/周频）；
G04 带拥挤度上限修正；G05 带美元+实际利率同升状态修正；缺失指标权重归一化。
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import subprocess
import threading
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

import market

BEIJING = timezone(timedelta(hours=8))

_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "gold_score_snapshot.json")
_CB_GOLD_VINTAGE_DIR = os.path.join(os.path.dirname(_SNAPSHOT), "gold_cb_vintages")
_ETF_VINTAGE_DIR = os.path.join(os.path.dirname(_SNAPSHOT), "gold_etf_vintages")
_WGC_REFERENCE_SNAPSHOT = os.path.join(os.path.dirname(_SNAPSHOT), "gold_wgc_reference_v3.json")

_GOLD_SERIES_TTL = 6 * 3600          # 外部日/周频序列 6h 内 0 外呼
_TREASURY_REAL_YIELD = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
    "?data=daily_treasury_real_yield_curve&field_tdr_date_value={year}"
)
_CFTC_ZIP = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
_OFR_CSV = "https://www.financialresearch.gov/financial-stress-index/data/fsi.csv"
_WGC_ETF_HOLDINGS = "https://fsapi.gold.org/api/v11/charts/etfv2/revised/holdings-chart2"
_WGC_REFERENCE = "https://fsapi.gold.org/api/goldprice/v13/chart/main"
_IMF_GOLD_CSV = (
    "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/IL/latest/"
    "G001.RGV_REVS.FTO.M"
)
_FINE_TROY_OZ_PER_TONNE = 32150.746568627
_SOURCE_STATUS: dict[str, dict] = {}
_SOURCE_LABELS = {
    "gold:treasury:DFII10": "美国财政部10年期实际利率",
    "gold:fred:DFII10": "美国10年期实际利率",
    "gold:fred:DTWEXBGS": "广义贸易加权美元指数",
    "gold:etf_global": "WGC全球黄金ETF持仓",
    "gold:cot": "CFTC COMEX仓位",
    "gold:ofr": "OFR金融压力（剔除安全资产）",
    "gold:cb_monthly_reported": "IMF央行黄金储备",
    "gold:wgc_reference_v3": "WGC/ICE黄金参考价",
    "gold:fx": "美元兑人民币汇率",
}
_SOURCE_MAX_AGE_DAYS = {
    "gold:treasury:DFII10": 7,
    "gold:fred:DFII10": 7,
    "gold:fred:DTWEXBGS": 14,  # 日频观测、H.10 周度发布
    "gold:etf_global": 10,
    "gold:cot": 14,
    "gold:ofr": 7,
    "gold:cb_monthly_reported": 100,
    "gold:wgc_reference_v3": 7,
    "gold:fx": 5,
}

# 方案完整版权重（第 1/3 节）
_META = {
    "g01_real_rate": ("美国10年期实际利率", "机会成本与美元", 0.25, "{:.2f}%"),
    "g02_dollar":    ("广义贸易加权美元指数", "机会成本与美元", 0.15, "{:.1f}"),
    "g03_etf":       ("全球实物黄金ETF持仓", "投资资金与仓位", 0.15, "{:.1f} 吨"),
    "g04_comex":     ("COMEX管理基金净多头", "投资资金与仓位", 0.10, "{:+.1f}% OI"),
    "g05_stress":    ("OFR金融压力（剔除安全资产）", "金融风险与避险", 0.10, "{:+.2f}"),
    "g06_cb":        ("全球已报告央行滚动储备变动", "结构性需求", 0.10, "{:+.0f} 吨/12月"),
    "g07_sglb":      ("上海—伦敦黄金溢价", "结构性需求", 0.05, "{:+.1f} USD/oz"),
    "g08_momentum":  ("黄金风险调整动量", "趋势确认", 0.10, "{:+.2f}"),
}
# 维度内权重（方案第 7 节）
_DIM_PARTS = {
    "机会成本与美元": [("g01_real_rate", 0.625), ("g02_dollar", 0.375)],
    "投资资金与仓位": [("g03_etf", 0.60), ("g04_comex", 0.40)],
    "金融风险与避险": [("g05_stress", 1.0)],
    "结构性需求":     [("g06_cb", 0.667), ("g07_sglb", 0.333)],
    "趋势确认":       [("g08_momentum", 1.0)],
}
_DIM_WEIGHT = {"机会成本与美元": 0.40, "投资资金与仓位": 0.25, "金融风险与避险": 0.10,
               "结构性需求": 0.15, "趋势确认": 0.10}
_INDICATOR_SOURCES = {
    "g01_real_rate": ("gold:treasury:DFII10",),
    "g02_dollar": ("gold:fred:DTWEXBGS",),
    "g03_etf": ("gold:etf_global", "gold:wgc_reference_v3"),
    "g04_comex": ("gold:cot",),
    "g05_stress": ("gold:ofr",),
    "g06_cb": ("gold:cb_monthly_reported",),
    "g07_sglb": ("gold:wgc_reference_v3", "gold:fx"),
    "g08_momentum": ("gold:wgc_reference_v3",),
}


def _curl(url: str, timeout: int = 20, accept: str = "") -> bytes:
    try:
        cmd = ["curl", "-L", "-s", "--max-time", str(timeout)]
        if accept:
            cmd += ["-H", f"Accept: {accept}"]
        r = subprocess.run(cmd + [url], capture_output=True, timeout=timeout + 5)
        return r.stdout if r.returncode == 0 else b""
    except Exception:
        return b""


# ---------------------------------------------------------------------------
# 实时金价（腾讯财经 hf_ 前缀迷你行情，仅标准库、不封 IP）
# ---------------------------------------------------------------------------

_GOLD_SPOT_URL = "https://qt.gtimg.cn/q=hf_XAU,hf_GC"
_GOLD_SPOT_TTL = 20  # 秒；伦敦金近全天交易，20 秒档足够接近实时
_GOLD_SPOT_CACHE: dict[str, tuple[float, dict]] = {}
_GOLD_SPOT_LOCK = threading.Lock()

_CN_GOLD_URL = "https://hq.sinajs.cn/list=gds_AU9999,gds_AUTD,nf_AU0"
_AU0_DAILY_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
    "InnerFuturesNewService.getDailyKLine?symbol=AU0"
)
_CN_GOLD_TTL = 20  # 秒
_CN_GOLD_CACHE: dict[str, tuple[float, dict]] = {}
_CN_GOLD_LOCK = threading.Lock()

# PAXG-USD 暗盘现货（Binance 公共行情镜像 data-api.binance.vision，7×24）。
# 腾讯 hf_XAU / 纽约 GC 有盘前盘后空档；PAXG 每枚锚定 1 盎司伦敦金 Good Delivery，
# 7×24 交易、暗盘时段同样出价，补齐夜间/周末空档。
_PAXG_SYMBOL = "PAXGUSDT"
_BINANCE_DATA_API = "https://data-api.binance.vision"
_PAXG_TTL = 20  # 秒；7×24 交易，与现货节奏对齐
_PAXG_CACHE: dict[str, tuple[float, dict]] = {}
_PAXG_CHART_TTL = 60  # 分钟线没有必要跟随 20 秒 ticker 重拉全天数据
_PAXG_CHART_CACHE: dict[str, tuple[float, dict]] = {}
_PAXG_LOCK = threading.Lock()

# PAXG → 国内金价近似折算：1 枚 PAXG = 1 金衡盎司 = 31.1034768 克，USDCNY 取新浪在岸
_TROY_OZ_GRAMS = 31.1034768
_USDCNY_URL = "https://hq.sinajs.cn/list=fx_susdcny"
_USDCNY_TTL = 600  # 秒；汇率日内波动远小于金价，10 分钟足够
_USDCNY_CACHE: dict[str, tuple[float, float]] = {}
_USDCNY_LOCK = threading.Lock()


def _parse_hf_quotes(raw: str) -> dict[str, dict]:
    """解析腾讯 hf_ 前缀行情（逗号分隔，非 A 股 `~` 分隔）。

    字段：0=现价 1=涨跌幅% 2=现价/昨收 3=今开 4=最高 5=最低 6=行情时间(北京时间)
          7=昨收 8=昨结算 … 12=日期 13=名称。只取语义明确且经实测的字段。
    """
    out: dict[str, dict] = {}
    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        f = line.split('"')[1].split(",")
        if len(f) < 14:
            continue

        def num(i: int) -> float | None:
            try:
                v = float(f[i])
                return v if math.isfinite(v) else None
            except (ValueError, IndexError):
                return None

        price = num(0)
        if price is None or price <= 0:
            continue
        out[key] = {
            "name": f[13].strip(),
            "price": price,
            "change_pct": num(1),
            "prev_close": num(7),
            "high": num(4),
            "low": num(5),
            "time": f[6].strip(),
            "date": f[12].strip(),
        }
    return out


def gold_spot() -> dict:
    """伦敦金 / 纽约金实时行情快照；失败回退最近一次成功结果（stale 标记）。"""
    now = time.time()
    with _GOLD_SPOT_LOCK:
        hit = _GOLD_SPOT_CACHE.get("spot")
        if hit and now - hit[0] < _GOLD_SPOT_TTL:
            return hit[1]
        try:
            import astock
            quotes = _parse_hf_quotes(astock._fetch_gtimg(["hf_XAU", "hf_GC"]))
            if quotes:
                payload = {
                    "xau": quotes.get("XAU"),
                    "gc": quotes.get("GC"),
                    "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
                }
                _GOLD_SPOT_CACHE["spot"] = (now, payload)
                return payload
        except Exception:  # noqa: BLE001 — 网络/解析异常统一回退
            pass
        if hit:
            payload = dict(hit[1])
            payload["stale"] = True
            return payload
        return {"xau": None, "gc": None, "fetched_at": None, "stale": True}


def _fetch_sina_cn(raw: str) -> str:
    """新浪国内期货/现货行情：需带 Referer，返回 GBK 文本。"""
    import subprocess as _sp
    cmd = ["curl", "-L", "-s", "--max-time", "10",
           "-H", "Referer: https://finance.sina.com.cn", _CN_GOLD_URL]
    r = _sp.run(cmd, capture_output=True, timeout=15)
    if r.returncode != 0:
        return ""
    return r.stdout.decode("gbk", "ignore")


def _au0_prev_settlement() -> float | None:
    """沪金主力前一交易日结算价（新浪期货日 K，s 字段）。失败返回 None。"""
    import subprocess as _sp
    cmd = ["curl", "-L", "-s", "--max-time", "10",
           "-H", "Referer: https://finance.sina.com.cn/futures/", _AU0_DAILY_URL]
    try:
        r = _sp.run(cmd, capture_output=True, timeout=15)
        if r.returncode != 0:
            return None
        raw = r.stdout.decode("gbk", "ignore")
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return None
        rows = json.loads(raw[start:end + 1])
        if not rows:
            return None
        v = float(rows[-1].get("s"))
        return v if math.isfinite(v) and v > 0 else None
    except Exception:  # noqa: BLE001 — 昨结缺失时仅影响涨跌幅
        return None


def _parse_cn_gold(raw: str) -> dict[str, dict]:
    """解析新浪 gds_ 行情（沪金99 / 黄金延期，CNY/克）。

    字段：0=现价 1=涨跌额 2=昨结 3=今开 4=最高 5=最低 6=时间 7=昨收
          8=买价 9=卖价 10=买量 11=卖量 12=日期 13=名称
    """
    out: dict[str, dict] = {}
    for line in raw.strip().split("\n"):
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        f = line.split('"')[1].split(",")
        if len(f) < 14:
            continue

        def num(i: int) -> float | None:
            try:
                v = float(f[i])
                return v if math.isfinite(v) else None
            except (ValueError, IndexError):
                return None

        price = num(0)
        if price is None or price <= 0:
            continue
        prev_close = num(7) or num(2)
        change = None
        if prev_close:
            change = price - prev_close
        out[key] = {
            "name": f[13].strip(),
            "price": price,
            "prev_close": prev_close,
            "change": round(change, 2) if change is not None else None,
            "change_pct": round(change / prev_close * 100, 2) if change is not None and prev_close else None,
            "open": num(3),
            "high": num(4),
            "low": num(5),
            "time": f[6].strip(),
            "date": f[12].strip(),
        }
    return out


def _parse_nf_au0(raw: str, prev_settle: float | None) -> dict | None:
    """解析新浪 nf_AU0 期货行情（黄金连续，CNY/克）。

    字段：0=名称 2=现价 5=买价 6=卖价 7=结算价 10=最高 11=最低
          17=日期 27=持仓量 28=昨结算价
    """
    for line in raw.strip().split("\n"):
        if "nf_AU0" not in line or '"' not in line:
            continue
        f = line.split('"')[1].split(",")
        if len(f) < 28:
            return None

        def num(i: int) -> float | None:
            try:
                v = float(f[i])
                return v if math.isfinite(v) else None
            except (ValueError, IndexError):
                return None

        price = num(2)
        if price is None or price <= 0:
            return None
        prev_close = num(28) or prev_settle
        change = None
        if prev_close:
            change = price - prev_close
        return {
            "name": f[0].strip(),
            "price": price,
            "prev_close": prev_close,
            "change": round(change, 2) if change is not None else None,
            "change_pct": round(change / prev_close * 100, 2) if change is not None and prev_close else None,
            "open": None,
            "high": num(10),
            "low": num(11),
            "time": None,
            "date": f[17].strip(),
        }
    return None


def cn_gold_spot() -> dict:
    """国内金价：沪金主力（AU0）、沪金99（AU9999）与黄金延期（AUTD），CNY/克。失败回退最近一次成功。"""
    now = time.time()
    with _CN_GOLD_LOCK:
        hit = _CN_GOLD_CACHE.get("cn")
        if hit and now - hit[0] < _CN_GOLD_TTL:
            return hit[1]
        try:
            raw = _fetch_sina_cn("")
            quotes = _parse_cn_gold(raw)
            quotes["AU0"] = _parse_nf_au0(raw, _au0_prev_settlement())
            if quotes:
                payload = {
                    "au0": quotes.get("AU0"),
                    "au9999": quotes.get("AU9999"),
                    "autd": quotes.get("AUTD"),
                    "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
                }
                _CN_GOLD_CACHE["cn"] = (now, payload)
                return payload
        except Exception:  # noqa: BLE001 — 网络/解析异常统一回退
            pass
        if hit:
            payload = dict(hit[1])
            payload["stale"] = True
            return payload
        return {"au0": None, "au9999": None, "autd": None, "fetched_at": None, "stale": True}

_AU0_HIST_TTL = 3600  # 秒；日K盘中仅收盘价变动，1 小时足够
_AU0_HIST_CACHE: dict[str, tuple[float, dict]] = {}
_AU0_HIST_LOCK = threading.Lock()


def au0_daily_history(days: int = 400) -> dict:
    """沪金主力（AU0）日K收盘序列，供评分卡旁的国内金价近1年走势。

    近一年走势与前端对齐用收盘价（c 字段），区别于涨跌幅基准的结算价（s）。
    """
    now = time.time()
    with _AU0_HIST_LOCK:
        hit = _AU0_HIST_CACHE.get("hist")
        if hit and now - hit[0] < _AU0_HIST_TTL:
            points = hit[1]["points"][-days:]
            return {**hit[1], "points": points}
        try:
            import subprocess as _sp
            cmd = ["curl", "-L", "-s", "--max-time", "10",
                   "-H", "Referer: https://finance.sina.com.cn/futures/", _AU0_DAILY_URL]
            r = _sp.run(cmd, capture_output=True, timeout=15)
            raw = r.stdout.decode("gbk", "ignore")
            start, end = raw.find("["), raw.rfind("]")
            if start < 0 or end <= start:
                raise ValueError("AU0 日K无返回")
            rows = json.loads(raw[start:end + 1])
            points = []
            for row in rows:
                d, c = str(row.get("d", ""))[:10], row.get("c")
                try:
                    close = float(c)
                except (TypeError, ValueError):
                    continue
                if d and math.isfinite(close) and close > 0:
                    points.append({"date": d, "v": round(close, 2)})
            if not points:
                raise ValueError("AU0 日K为空")
            payload = {
                "symbol": "AU0",
                "points": points,
                "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
            }
            _AU0_HIST_CACHE["hist"] = (now, payload)
            return {**payload, "points": points[-days:]}
        except Exception:  # noqa: BLE001 — 失败回退最近一次成功
            if hit:
                payload = dict(hit[1])
                payload["points"] = payload["points"][-days:]
                payload["stale"] = True
                return payload
            return {"symbol": "AU0", "points": [], "fetched_at": None, "stale": True}


def _binance_get(path: str, timeout: int = 12) -> bytes:
    """Binance 公共行情镜像（data-api.binance.vision）GET。仅标准库。"""
    cmd = ["curl", "-L", "-s", "--max-time", str(timeout),
           "-H", "Accept: application/json", f"{_BINANCE_DATA_API}{path}"]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    return r.stdout if r.returncode == 0 and r.stdout else b""


def _bj_midnight_ms() -> int:
    """当日北京 00:00 对应的 UTC 毫秒时间戳（Binance klines 以 UTC ms 计）。"""
    now = datetime.now(BEIJING)
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(mid.timestamp() * 1000)


def _parse_binance_klines(raw: bytes) -> list[dict]:
    """解析 Binance klines 数组 → [{time, price, volume, mod}]（北京时间时钟分）。"""
    try:
        rows = json.loads(raw.decode("utf-8", "ignore"))
    except (ValueError, json.JSONDecodeError):
        return []
    points: list[dict] = []
    for k in rows:
        # [openTime, o, h, l, close, volume, closeTime, qvol, trades, ...]
        if not isinstance(k, list) or len(k) < 6:
            continue
        try:
            ot = int(k[0])
            close = float(k[4])
            vol = float(k[5])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        bj = datetime.fromtimestamp(ot / 1000, BEIJING)
        points.append({
            "time": bj.strftime("%H:%M"),
            "price": close,
            "volume": vol,
            "mod": bj.hour * 60 + bj.minute,
            "ot": ot,
        })
    return points


def usdcny_rate() -> float | None:
    """在岸 USDCNY 汇率（新浪 fx_s，日更新鲜、无鉴权）。失败回退最近一次成功缓存。

    字段：1=买价 2=卖价 3=昨收 5=开盘 6=最高 7=最低 8=最新/收盘。
    """
    now = time.time()
    with _USDCNY_LOCK:
        hit = _USDCNY_CACHE.get("usdcny")
        if hit and now - hit[0] < _USDCNY_TTL:
            return hit[1]
        try:
            cmd = ["curl", "-L", "-s", "--max-time", "8",
                   "-H", "Referer: https://finance.sina.com.cn", _USDCNY_URL]
            r = subprocess.run(cmd, capture_output=True, timeout=12)
            f = r.stdout.decode("gbk", "ignore").split('"')[1].split(",")
            rate = float(f[8])
            if not math.isfinite(rate) or not 5 <= rate <= 9:
                raise ValueError(f"USDCNY 异常：{rate}")
            _USDCNY_CACHE["usdcny"] = (now, rate)
            return rate
        except Exception:  # noqa: BLE001 — 汇率缺失只降级折算字段，不影响 USD 行情
            return hit[1] if hit else None


def _paxg_to_cny_gram(usd_per_oz: float | None, fx: float | None) -> float | None:
    """USD/盎司 → CNY/克。缺汇率或价格异常时返回 None。"""
    if usd_per_oz is None or fx is None or not usd_per_oz > 0 or not fx > 0:
        return None
    v = usd_per_oz / _TROY_OZ_GRAMS * fx
    return round(v, 2) if math.isfinite(v) else None


def paxg_usd_spot() -> dict:
    """PAXG-USD 暗盘现货：7×24 实时行情 + 当日分时。

    数据源：Binance 公共行情镜像（data-api.binance.vision）。每枚 PAXG 锚定 1
    盎司伦敦金 Good Delivery，与现货黄金同口径、暗盘时段也连续出价。失败回退
    最近一次成功结果（stale 标记）。
    """
    now = time.time()
    with _PAXG_LOCK:
        hit = _PAXG_CACHE.get("paxg")
        if hit and now - hit[0] < _PAXG_TTL:
           return hit[1]
        try:
            mid_ms = _bj_midnight_ms()
            today = datetime.now(BEIJING).strftime("%Y-%m-%d")
            chart_hit = _PAXG_CHART_CACHE.get("minute")
            chart = chart_hit[1] if chart_hit and now - chart_hit[0] < _PAXG_CHART_TTL and chart_hit[1]["date"] == today else None
            if chart is None:
                # 昨日收盘：北京午夜前最后一根 1m k 线的 close（当日分时基准）
                yk = _parse_binance_klines(_binance_get(
                    f"/api/v3/klines?symbol={_PAXG_SYMBOL}&interval=1m&limit=1&endTime={mid_ms - 1}"))
                prev_close = yk[-1]["price"] if yk else None
                # 当日分时：自北京 00:00 起 1m k 线，按 UTC ms 游标分页取全量
                points: list[dict] = []
                cursor = mid_ms
                now_ms = int(datetime.now(BEIJING).timestamp() * 1000)
                for _ in range(3):  # 3×1000 分钟 ≈ 50h，足够覆盖单日
                    page = _parse_binance_klines(_binance_get(
                        f"/api/v3/klines?symbol={_PAXG_SYMBOL}&interval=1m&limit=1000&startTime={cursor}"))
                    if not page:
                        break
                    points.extend(page)
                    if len(page) < 1000:
                        break
                    cursor = page[-1]["ot"] + 60_000
                    if cursor > now_ms:
                        break
                if points:
                    chart = {"date": today, "prev_close": prev_close, "points": points}
                    _PAXG_CHART_CACHE["minute"] = (now, chart)
                elif chart_hit and chart_hit[1]["date"] == today:
                    chart = chart_hit[1]
            prev_close = chart.get("prev_close") if chart else None
            points = list(chart.get("points") or []) if chart else []
            # 实时最新价：24h ticker lastPrice 优先，落回分时末点
            tk_raw = _binance_get(f"/api/v3/ticker/24hr?symbol={_PAXG_SYMBOL}")
            tk = json.loads(tk_raw.decode("utf-8", "ignore")) if tk_raw else {}
            last = points[-1] if points else None
            last_price = float(tk["lastPrice"]) if tk.get("lastPrice") else (last["price"] if last else None)
            if last_price is None:
                raise ValueError("无实时价格")
            if prev_close is None and tk.get("prevClosePrice"):
                prev_close = float(tk["prevClosePrice"])
            change = round(last_price - prev_close, 2) if prev_close else None
            change_pct = round(change / prev_close * 100, 2) if change is not None and prev_close else None
            # 用 ticker 最新价覆盖分时末点，保证图尾与卡片一致
            if last and points:
                points[-1] = {**last, "price": last_price}
            # 国内金价近似折算（CNY/克）：PAXG 锚定 1 金衡盎司，× USDCNY ÷ 盎司克重。
            # 分时保持 USD 原值 + 附 usdcny，由前端折算；汇率缺失时 USD 图不受影响。
            fx = usdcny_rate()
            open_usd = float(tk["openPrice"]) if tk.get("openPrice") else None
            high_usd = float(tk["highPrice"]) if tk.get("highPrice") else None
            low_usd = float(tk["lowPrice"]) if tk.get("lowPrice") else None
            cny_price = _paxg_to_cny_gram(last_price, fx)
            cny_prev = _paxg_to_cny_gram(prev_close, fx)
            chart_points = [{"time": p["time"], "price": p["price"], "volume": p["volume"]}
                           for p in points]
            payload = {
                "name": "PAXG-USD（现货黄金暗盘）",
                "price": last_price,
                "prev_close": prev_close,
                "change": change,
                "change_pct": change_pct,
                "open": open_usd,
                "high": high_usd,
                "low": low_usd,
                "volume": float(tk["volume"]) if tk.get("volume") else None,
                "time": last["time"] if last else datetime.now(BEIJING).strftime("%H:%M"),
                "date": today,
                "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
                "usdcny": fx,
                "cny": {
                    "price": cny_price,
                    "prev_close": cny_prev,
                    "change": round(cny_price - cny_prev, 2)
                        if cny_price is not None and cny_prev is not None else None,
                    "open": _paxg_to_cny_gram(open_usd, fx),
                    "high": _paxg_to_cny_gram(high_usd, fx),
                    "low": _paxg_to_cny_gram(low_usd, fx),
                } if fx else None,
               "minute": {
                   "date": today,
                   "prev_close": prev_close or 0.0,
                   "points": chart_points,
                    # 7×24：x 轴固定覆盖当日 00:00–24:00（北京时钟分钟）
                    "market_minutes": [[0, 1440]],
               },
            }
            _PAXG_CACHE["paxg"] = (now, payload)
            return payload
        except Exception:  # noqa: BLE001 — 网络/解析异常统一回退
            pass
        if hit:
            payload = dict(hit[1])
            payload["stale"] = True
            return payload
        return {"name": None, "price": None, "fetched_at": None, "minute": None, "stale": True}


# ---------------------------------------------------------------------------
# 序列拉取：内存 TTL + 磁盘兜底（与 market._fred_series_cached 同语义）
# ---------------------------------------------------------------------------

def _period_age_days(period: str | None, today: date | None = None) -> int | None:
    if not period:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}", period):
            year, month = map(int, period.split("-"))
            next_month = date(year + (month == 12), month % 12 + 1, 1)
            observed = next_month - timedelta(days=1)
        else:
            observed = date.fromisoformat(period[:10])
    except ValueError:
        return None
    return max(0, ((today or datetime.now(BEIJING).date()) - observed).days)


def _source_status(key: str, stale: bool, fetched_at: float | None,
                   rows: list[tuple[str, float]] | None = None,
                   today: date | None = None) -> None:
    latest_period = rows[-1][0] if rows else None
    age_days = _period_age_days(latest_period, today)
    max_age_days = _SOURCE_MAX_AGE_DAYS.get(key)
    observation_lag = age_days is not None and max_age_days is not None and age_days > max_age_days
    status = "missing" if not rows else "stale" if stale or observation_lag else "fresh"
    _SOURCE_STATUS[key] = {
        "key": key,
        "label": _SOURCE_LABELS.get(key, key),
        "status": status,
        "stale_reason": "fallback" if stale else "observation_lag" if observation_lag else None,
        "fetched_at": datetime.fromtimestamp(fetched_at, BEIJING).strftime("%Y-%m-%d %H:%M")
        if fetched_at else None,
        "latest_period": latest_period,
        "age_days": age_days,
        "max_age_days": max_age_days,
    }


def _series_cached(key: str, fn, ttl: float = _GOLD_SERIES_TTL) -> list[tuple[str, float]]:
    now = time.time()
    hit = market._SUB.get(key)
    if hit and not market.source_refresh_forced() and now - hit[0] < ttl:
        _source_status(key, False, hit[0], hit[1])
        return hit[1]
    try:
        rows = fn()
    except Exception:
        rows = []
    if rows:
        market._SUB[key] = (now, rows)
        market._SUB_LAST[key] = (now, rows)
        snap = market._load_fred_snapshot()
        snap[key] = {"ts": now, "rows": [[d, v] for d, v in rows]}
        market._save_json(market._FRED_SNAPSHOT, snap)
        _source_status(key, False, now, rows)
        return rows
    last = market._SUB_LAST.get(key)
    if last:
        _source_status(key, True, last[0], last[1])
        return last[1]
    snap_hit = market._load_fred_snapshot().get(key)
    if snap_hit and snap_hit.get("rows"):
        rows = [(str(d), float(v)) for d, v in snap_hit["rows"]]
        market._SUB_LAST[key] = (float(snap_hit.get("ts") or now), rows)
        _source_status(key, True, float(snap_hit.get("ts") or now), rows)
        return rows
    _source_status(key, False, None)
    return []


def _fred_series(series_id: str, limit: int, ttl: float = _GOLD_SERIES_TTL) -> list[tuple[str, float]]:
    return _series_cached(
        f"gold:fred:{series_id}", lambda: market._fred_csv(series_id, limit), ttl=ttl
    )


def _h10_cache_ttl(now: datetime | None = None) -> float:
    """周一 16:15 ET 发布后立即使发布前缓存失效，其余时间沿用 6 小时 TTL。"""
    current = now or datetime.now(ZoneInfo("America/New_York"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("America/New_York"))
    monday = current - timedelta(days=current.weekday())
    release = monday.replace(hour=16, minute=15, second=0, microsecond=0)
    if current < release:
        release -= timedelta(days=7)
    return min(_GOLD_SERIES_TTL, max(60.0, (current - release).total_seconds()))


def _parse_treasury_real_yield(raw: bytes) -> list[tuple[str, float]]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []
    ns = {"d": "http://schemas.microsoft.com/ado/2007/08/dataservices"}
    rows = []
    for props in root.iter():
        if not props.tag.endswith("properties"):
            continue
        day = props.find("d:NEW_DATE", ns)
        value = props.find("d:TC_10YEAR", ns)
        try:
            rows.append((day.text[:10], float(value.text)))
        except (AttributeError, TypeError, ValueError):
            continue
    return sorted(set(rows))


def _treasury_real_yield_10y() -> list[tuple[str, float]]:
    year = datetime.now(BEIJING).year
    return _series_cached(
        "gold:treasury:DFII10",
        lambda: _parse_treasury_real_yield(_curl(_TREASURY_REAL_YIELD.format(year=year))),
        ttl=3600,
    )


def _parse_wgc_etf_holdings(raw: str) -> list[tuple[str, float]]:
    """解析 WGC 周度地区持仓并汇总为全球实物黄金 ETF 吨数。"""
    try:
        data = json.loads(raw)["chartData"]["data"]["Weekly"]["tonnes"]["set"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    out: list[tuple[str, float]] = []
    for row in data:
        try:
            total = sum(float(v) for v in row[1:5] if v is not None)
            date = datetime.fromtimestamp(float(row[0]) / 1000, timezone.utc).strftime("%Y-%m-%d")
        except (IndexError, TypeError, ValueError, OSError):
            continue
        if total > 0:
            out.append((date, total))
    return out


def _etf_holdings() -> list[tuple[str, float]]:
    """G03：WGC 全球100多只实物黄金 ETF 周度总持仓，吨。"""
    def fetch():
        rows = _parse_wgc_etf_holdings(_curl(_WGC_ETF_HOLDINGS, timeout=30).decode("utf-8", "ignore"))
        if rows:
            fetched = datetime.now(BEIJING)
            market._save_json(
                os.path.join(_ETF_VINTAGE_DIR, f"{fetched:%Y-%m-%d}_{rows[-1][0]}.json"),
                {
                    "source": _WGC_ETF_HOLDINGS,
                    "series": "Weekly.tonnes.global",
                    "unit": "tonnes",
                    "fetched_at": fetched.isoformat(),
                    "latest_period": rows[-1][0],
                    "rows": [[d, round(v, 6)] for d, v in rows],
                },
            )
        return rows
    return _series_cached("gold:etf_global", fetch, ttl=24 * 3600)


def _parse_ofr_ex_safe(raw: str) -> list[tuple[str, float]]:
    """剔除含 Gold/USD 的 Safe assets，保留信用/估值/融资/波动率贡献。"""
    out = []
    for row in csv.DictReader(io.StringIO(raw)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.get("Date", "")):
            continue
        try:
            value = sum(float(row[key]) for key in ("Credit", "Equity valuation", "Funding", "Volatility"))
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append((row["Date"], value))
    return out


def _ofr_fsi() -> list[tuple[str, float]]:
    """G05：OFR官方日频压力贡献之和，剔除含黄金价格的安全资产类别。"""
    def fetch():
        raw = _curl(_OFR_CSV).decode("utf-8", "ignore")
        if not raw.startswith("Date,"):
            return []
        return _parse_ofr_ex_safe(raw)[-1300:]
    return _series_cached("gold:ofr", fetch)


def _parse_wgc_reference(raw: str) -> dict[str, list[tuple[str, float]]]:
    """解析 Goldhub 官方接口中的 ICE LBMA AM/PM 与 SGE PM 参考价。"""
    try:
        chart = json.loads(raw)["chartData"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}
    out: dict[str, list[tuple[str, float]]] = {}
    for key in ("lbma_am_usd", "lbma_pm_usd", "sge_pm_cny"):
        points: dict[str, float] = {}
        for row in chart.get(key) or []:
            try:
                d = datetime.fromtimestamp(float(row[0]) / 1000, timezone.utc).strftime("%Y-%m-%d")
                value = float(row[1])
            except (IndexError, TypeError, ValueError, OSError):
                continue
            if math.isfinite(value) and value > 0:
                points[d] = value
        out[key] = sorted(points.items())
    return out


def _wgc_reference_prices() -> dict[str, list[tuple[str, float]]]:
    """近8年真实 LBMA/SHAUPM 日频价；失败只回退同源快照，不使用代理。"""
    key = "gold:wgc_reference_v3"
    now = time.time()
    hit = market._SUB.get(key)
    if hit and now - hit[0] < _GOLD_SERIES_TTL:
        rows = hit[1]
        latest = rows.get("lbma_pm_usd") or []
        _source_status(key, False, hit[0], latest)
        return rows

    today = datetime.now(BEIJING).date()
    # 额外 3 年只用于 ETF 滚动回归预热；正式评分仍使用最近 5 年。
    cutoff = date(today.year - 8, today.month, min(today.day, 28))
    merged = {k: {} for k in ("lbma_am_usd", "lbma_pm_usd", "sge_pm_cny")}
    for year in range(cutoff.year, today.year + 1):
        start = max(cutoff, date(year, 1, 1))
        end = min(today, date(year, 12, 31))
        raw = _curl(f"{_WGC_REFERENCE}?startDate={start}&endDate={end}", timeout=30)
        parsed = _parse_wgc_reference(raw.decode("utf-8", "ignore"))
        for series, points in parsed.items():
            merged[series].update(points)
    rows = {series: sorted(points.items()) for series, points in merged.items()}
    if all(len(rows[series]) >= 600 for series in rows):
        market._SUB[key] = (now, rows)
        market._SUB_LAST[key] = (now, rows)
        market._save_json(_WGC_REFERENCE_SNAPSHOT, {"ts": now, "data": rows})
        _source_status(key, False, now, rows["lbma_pm_usd"])
        return rows

    last = market._SUB_LAST.get(key)
    if last:
        latest = last[1].get("lbma_pm_usd") or []
        _source_status(key, True, last[0], latest)
        return last[1]
    snap = market._load_json(_WGC_REFERENCE_SNAPSHOT)
    if isinstance(snap, dict) and isinstance(snap.get("data"), dict):
        restored = {
            series: [(str(d), float(v)) for d, v in points]
            for series, points in snap["data"].items()
        }
        fetched_at = float(snap.get("ts") or now)
        market._SUB_LAST[key] = (fetched_at, restored)
        _source_status(key, True, fetched_at, restored.get("lbma_pm_usd") or [])
        return restored
    _source_status(key, False, None)
    return {}


def _usd_cny() -> list[tuple[str, float]]:
    """中行美元兑人民币中间价（×100 折算）。"""
    def fetch():
        import akshare as ak
        fx = ak.currency_boc_safe()
        return [(str(d)[:10], float(v) / 100) for d, v in zip(fx["日期"], fx["美元"])
                if v == v and float(v) > 0][-1300:]
    return _series_cached("gold:fx", fetch)


def _comex_money_net() -> list[tuple[str, float]]:
    """G04：COMEX 黄金管理基金净多头占总未平仓合约比例（%，周频）。

    CFTC Disaggregated COT 官方年度 zip（f_year.txt），Market=GOLD -
    COMMODITY EXCHANGE INC.；列 M_Money_Positions_Long/Short_All、Open_Interest_All。
    """
    def fetch():
        import zipfile
        year = datetime.now(BEIJING).year
        points: dict[str, float] = {}
        for y in range(year - 5, year + 1):  # 近 5 年 + 当年
            raw = _curl(_CFTC_ZIP.format(year=y), timeout=40)
            if not raw or raw[:2] != b"PK":
                continue
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
                name = zf.namelist()[0]
                text = zf.read(name).decode("utf-8", "ignore")
            except Exception:
                continue
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("Market_and_Exchange_Names", "").strip() != "GOLD - COMMODITY EXCHANGE INC.":
                    continue
                try:
                    d = row["Report_Date_as_YYYY-MM-DD"].strip()[:10]
                    oi = float(row["Open_Interest_All"])
                    long_ = float(row["M_Money_Positions_Long_All"])
                    short_ = float(row["M_Money_Positions_Short_All"])
                    if oi > 0:
                        points[d] = (long_ - short_) / oi * 100
                except (KeyError, TypeError, ValueError):
                    continue
        return sorted(points.items())
    return _series_cached("gold:cot", fetch, ttl=24 * 3600)  # 周频数据，24h 刷一次足够


def _parse_imf_gold_csv(raw: str) -> list[tuple[str, float]]:
    """解析 IMF IL 全球黄金储备实物量，并将金衡盎司换算为吨。"""
    points: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        if (row.get("COUNTRY"), row.get("INDICATOR"), row.get("UNIT"), row.get("FREQUENCY")) != \
                ("G001", "RGV_REVS", "FTO", "M"):
            continue
        period = row.get("TIME_PERIOD", "")
        if not re.fullmatch(r"\d{4}-M\d{2}", period):
            continue
        try:
            ounces = float(row["OBS_VALUE"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(ounces) and ounces > 0:
            points[period.replace("-M", "-")] = ounces / _FINE_TROY_OZ_PER_TONNE
    return sorted(points.items())


def _cb_gold_monthly_reported() -> list[tuple[str, float]]:
    """IMF IFS 全球已报告官方黄金储备，月度存量（吨，通常滞后约两个月）。"""
    def fetch():
        rows = _parse_imf_gold_csv(
            _curl(_IMF_GOLD_CSV, timeout=30, accept="text/csv").decode("utf-8", "ignore")
        )
        if rows:
            fetched = datetime.now(BEIJING)
            market._save_json(
                os.path.join(_CB_GOLD_VINTAGE_DIR, f"{fetched:%Y-%m-%d}_{rows[-1][0]}.json"),
                {
                    "source": _IMF_GOLD_CSV,
                    "series": "G001.RGV_REVS.FTO.M",
                    "unit": "tonnes",
                    "fetched_at": fetched.isoformat(),
                    "latest_period": rows[-1][0],
                    "rows": [[d, round(v, 6)] for d, v in rows],
                },
            )
        return rows
    return _series_cached("gold:cb_monthly_reported", fetch, ttl=24 * 3600)


# ---------------------------------------------------------------------------
# 信号与评分
# ---------------------------------------------------------------------------

def _pct_rank(series: list[float], current: float) -> float:
    """历史分位 0-1。样本太少给 0.5（中性），避免早期乱打分。

    日/周频 5 年约 250-1250 点；季度序列 2010 年以来约 60 点，阈值取 20。"""
    xs = [v for v in series if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(xs) < 20:
        return 0.5
    below = sum(1 for v in xs if v < current)
    return below / len(xs)


def _score_history(rows: list[tuple[str, float]], reference: list[float] | None = None) -> list[tuple[str, float]]:
    """逐点扩展窗口分位：每个历史时点只用截至当日的数据，避免前视偏差。

    reference 仅约束参照窗口的截止范围（如近 5 年），窗口左端仍随时间推进；
    最后一点的分位与当前实盘评分一致。"""
    values = [v for _, v in rows]
    ref = reference or values
    ref_limit = len(ref)
    out: list[tuple[str, float]] = []
    for i, (d, v) in enumerate(rows):
        end = min(i + 1, ref_limit)
        out.append((d, _pct_rank(values[:end], v) * 100))
    return out


def _crowding_cap(score: float, level_pct: float, rising: bool) -> float:
    if level_pct >= 0.95:
        return min(score, 50)
    if level_pct >= 0.90:
        return min(score, 65)
    if level_pct >= 0.80:
        return min(score, 80)
    return score


def _dimension_score_history(parts: list[tuple[str, float]], score_histories: dict[str, list[tuple[str, float]]],
                             weights: dict[str, float]) -> list[dict]:
    """不同频率指标按截至当日最近得分合成，近一年每周保留最后一点。"""
    series = {key: rows for key, _ in parts if (rows := score_histories.get(key))}
    if not series:
        return []
    latest = max(rows[-1][0] for rows in series.values())
    latest_day = latest[:10] if len(latest) >= 10 else f"{latest}-01"
    cutoff = (date.fromisoformat(latest_day) - timedelta(days=365)).isoformat()
    event_dates = sorted({d for rows in series.values() for d, _ in rows if d >= cutoff})
    weekly: dict[tuple[int, int], dict] = {}
    for day in event_dates:
        got = []
        for key, rows in series.items():
            i = bisect_right([d for d, _ in rows], day) - 1
            if i >= 0 and weights.get(key, 0) > 0:
                got.append((rows[i][1], weights[key]))
        if not got:
            continue
        score = sum(value * weight for value, weight in got) / sum(weight for _, weight in got)
        parsed = date.fromisoformat(day[:10] if len(day) >= 10 else f"{day}-01")
        weekly[parsed.isocalendar()[:2]] = {"date": day, "v": round(score, 1)}
    return list(weekly.values())


def _append_current_score(hist: list[dict], current_date: str, score: float) -> None:
    """把当前计算值作为独立时点放入趋势，避免覆盖最近观测日。

    周末/节假日计算的分数落到最近已有观测日，避免历史里出现非交易日时点。"""
    point = {"date": current_date, "v": score}
    if hist and hist[-1]["date"] == current_date:
        hist[-1] = point
    else:
        hist.append(point)


def _risk_adj_momentum(hist: list[tuple[str, float]], n: int) -> float | None:
    """n 日收益率 ÷ n 日年化波动率。"""
    if len(hist) < n + 1:
        return None
    window = [v for _, v in hist[-(n + 1):]]
    rets = [math.log(b / a) for a, b in zip(window, window[1:]) if a > 0 and b > 0]
    if len(rets) < max(10, n - 5):
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol = math.sqrt(var) * math.sqrt(252)
    if vol < 1e-9:
        return None
    return (window[-1] / window[0] - 1) / vol


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _risk_adj_momentum_series(hist: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """逐日计算 G08，确保最后一个信号与卡片显示日期一致。"""
    out = []
    for end in range(121, len(hist) + 1):
        segment = hist[end - 121:end]
        m60 = _risk_adj_momentum(segment, 60)
        m120 = _risk_adj_momentum(segment, 120)
        if m60 is not None and m120 is not None:
            out.append((hist[end - 1][0], 0.6 * m60 + 0.4 * m120))
    return out


def _rolling_residuals(pairs: list[tuple[str, float, float]], window: int = 156,
                       min_obs: int = 104) -> list[dict]:
    """只用 t-1 及以前的一元滚动回归，提取价格动量无法解释的 ETF 流量。"""
    out = []
    for i, (day, flow, momentum) in enumerate(pairs):
        hist = pairs[max(0, i - window):i]
        if len(hist) < min_obs:
            continue
        xs = [row[2] for row in hist]
        ys = [row[1] for row in hist]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        beta = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom if denom > 1e-12 else 0.0
        expected = mean_y + beta * (momentum - mean_x)
        out.append({"date": day, "residual": flow - expected, "flow": flow,
                    "expected": expected, "beta": beta})
    smoothed = _ema([row["residual"] for row in out], 4)
    for row, value in zip(out, smoothed):
        row["smoothed"] = value
    return out


def _etf_surprise_signal(etf: list[tuple[str, float]],
                         price_momentum: list[tuple[str, float]]) -> list[dict]:
    raw = [
        (etf[i][0], 0.7 * (etf[i][1] / etf[i - 4][1] - 1)
         + 0.3 * (etf[i][1] / etf[i - 12][1] - 1))
        for i in range(12, len(etf))
        if etf[i - 4][1] > 0 and etf[i - 12][1] > 0
    ]
    pairs = []
    j = 0
    current_momentum = None
    for day, flow in raw:
        while j < len(price_momentum) and price_momentum[j][0] <= day:
            current_momentum = price_momentum[j][1]
            j += 1
        if current_momentum is not None:
            pairs.append((day, flow, current_momentum))
    return _rolling_residuals(pairs)


def _mk_indicator(key, hist, score, signal_val, note="", unit_hist=""):
    label, dim, weight, fmt = _META[key]
    latest = hist[-1] if hist else (None, None)
    prev = hist[-2][1] if len(hist) > 1 else None
    chg = round(latest[1] - prev, 3) if latest[1] is not None and prev is not None else None
    return {
        "key": key, "label": label, "dimension": dim, "weight": weight,
        "value": round(latest[1], 3) if latest[1] is not None else None,
        "value_text": fmt.format(latest[1]) if latest[1] is not None else None,
        "chg": chg, "date": latest[0],
        "score": round(score, 1) if score is not None else None,
        "signal": round(signal_val, 4) if signal_val is not None else None,
        "hist": [{"date": d, "v": round(v, 3)} for d, v in hist[-500:]],
        "note": note,
    }


def _signal_label(total: float) -> str:
    if total >= 80: return "强利多"
    if total >= 65: return "利多"
    if total >= 45: return "中性"
    if total >= 20: return "利空"
    return "强利空"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def get_gold_score(force: bool = False) -> dict:
    return market._layered_get(
        "gold_score_v3", lambda: market._run_source_refresh(_build, force),
        valid=lambda v: bool(v.get("indicators")),
        warm=_load_gold_snapshot,
        ttl=3600,
        force=force,
    )


def _load_gold_snapshot():
    d = market._load_json(_SNAPSHOT)
    return d if isinstance(d, dict) and d.get("schema_version") == 3 and d.get("indicators") else None


def _build() -> dict:
    wall_date = datetime.now(BEIJING).strftime("%Y-%m-%d")
    _SOURCE_STATUS.clear()
    fred_real = _fred_series("DFII10", 1300)             # G01 长历史/兜底/交叉校验
    treasury_real = _treasury_real_yield_10y()            # G01 财政部当年日度主源
    treasury_fresh = _SOURCE_STATUS.get("gold:treasury:DFII10", {}).get("status") == "fresh"
    use_treasury = bool(treasury_fresh and treasury_real
                        and (not fred_real or treasury_real[-1][0] >= fred_real[-1][0]))
    if use_treasury:
        real = sorted({**dict(fred_real), **dict(treasury_real)}.items())
        real_sources = ("gold:treasury:DFII10",)
    else:
        real = fred_real
        real_sources = ("gold:fred:DFII10",)
    dxy = _fred_series("DTWEXBGS", 1300, ttl=_h10_cache_ttl())  # G02 H.10 周度发布
    etf = _etf_holdings()                                # G03 全球实物黄金ETF周度持仓
    cot = _comex_money_net()                             # G04 管理基金净多头%OI
    ofr = _ofr_fsi()                                     # G05 OFR ex-safe
    cb_m = _cb_gold_monthly_reported()                   # G06 月度已报告储备
    reference = _wgc_reference_prices()                  # G07/G08 WGC/ICE真实参考价
    lbma_am = reference.get("lbma_am_usd") or []         # G07 LBMA AM
    lbma_pm = reference.get("lbma_pm_usd") or []         # G08 LBMA PM
    sge_pm = reference.get("sge_pm_cny") or []           # G07 SHAUPM
    fx = _usd_cny()                                      # G07 汇率
    momentum_raw = _risk_adj_momentum_series(lbma_pm) if len(lbma_pm) > 120 else []
    momentum_signal = list(zip(
        [d for d, _ in momentum_raw], _ema([v for _, v in momentum_raw], 3)
    )) if momentum_raw else []

    indicators: list[dict] = []
    notes: list[str] = []
    scored_keys: dict[str, dict] = {}

    # G01：-(0.6×Δ20 + 0.4×Δ60)，实际利率下降利多
    if len(real) > 60:
        raw_sig = [-(0.6 * (real[i][1] - real[i - 20][1]) + 0.4 * (real[i][1] - real[i - 60][1]))
                   for i in range(60, len(real))]
        sig = _ema(raw_sig, 3)
        common = set(dict(fred_real)) & set(dict(treasury_real))
        crosscheck = (abs(dict(fred_real)[max(common)] - dict(treasury_real)[max(common)])) if common else None
        note = ("美国财政部日度实际收益率主源，FRED DFII10用于历史与故障兜底"
                + (f"；最新同日差 {crosscheck:.2f} 个百分点" if crosscheck is not None else ""))
        ind = _mk_indicator("g01_real_rate", real, _pct_rank(sig, sig[-1]) * 100, sig[-1], note)
        ind["_score_hist"] = _score_history(list(zip([d for d, _ in real[60:]], sig)))
        indicators.append(ind); scored_keys["g01_real_rate"] = ind
    else:
        notes.append("实际利率缺失")

    # G02：-(0.6×ln20 + 0.4×ln60)，美元贬值利多
    if len(dxy) > 60:
        raw_sig = [-(0.6 * math.log(dxy[i][1] / dxy[i - 20][1]) + 0.4 * math.log(dxy[i][1] / dxy[i - 60][1]))
                   for i in range(60, len(dxy)) if dxy[i - 20][1] > 0 and dxy[i - 60][1] > 0]
        sig = _ema(raw_sig, 3)
        ind = _mk_indicator("g02_dollar", dxy, _pct_rank(sig, sig[-1]) * 100, sig[-1])
        signal_dates = [dxy[i][0] for i in range(60, len(dxy))
                        if dxy[i - 20][1] > 0 and dxy[i - 60][1] > 0]
        ind["_score_hist"] = _score_history(list(zip(signal_dates, sig)))
        indicators.append(ind); scored_keys["g02_dollar"] = ind
    else:
        notes.append("美元指数缺失")

    # G03：方案B——ETF 4/12周流量剔除价格动量可解释部分，残差做4周EMA。
    etf_surprise = _etf_surprise_signal(etf, momentum_signal) if len(etf) > 260 and momentum_signal else []
    if etf_surprise:
        latest = etf_surprise[-1]
        score_hist = [row["smoothed"] for row in etf_surprise[-260:]]
        note = (f"超预期流量={latest['smoothed']:+.4f}；原始4/12周流量={latest['flow']:+.4f}，"
                f"价格动量解释值={latest['expected']:+.4f}；滚动回归β={latest['beta']:+.3f}")
        ind = _mk_indicator("g03_etf", etf[-260:],
                            _pct_rank(score_hist, latest["smoothed"]) * 100,
                            latest["smoothed"], note)
        ind.update({"raw_signal": round(latest["flow"], 4),
                    "expected_signal": round(latest["expected"], 4),
                    "model_beta": round(latest["beta"], 4)})
        ind["_score_hist"] = _score_history(
            [(row["date"], row["smoothed"]) for row in etf_surprise[-260:]], score_hist)
        indicators.append(ind); scored_keys["g03_etf"] = ind
    else:
        notes.append("ETF超预期流量样本不足")

    # G04：净多头%OI 的 4 周变化，带拥挤度上限修正
    if len(cot) > 8:
        sig = [cot[i][1] - cot[i - 4][1] for i in range(4, len(cot))]
        base = _pct_rank(sig[-260:], sig[-1]) * 100
        # 拥挤度：当前净多头%OI 的 5 年历史分位
        level_pct = _pct_rank([v for _, v in cot[-260:]], cot[-1][1])
        rising = sig[-1] > 0
        capped = _crowding_cap(base, level_pct, rising)
        crowd_note = ""
        if capped < base:
            crowd_note = f"仓位处于历史 {level_pct * 100:.0f}% 分位，触发拥挤度上限 {capped:.0f}"
        ind = _mk_indicator("g04_comex", cot, capped, sig[-1],
                            crowd_note or "CFTC Disaggregated COT 管理基金净多头/未平仓合约")
        level_values = [v for _, v in cot]
        ind["_score_hist"] = [
            (cot[i + 4][0], _crowding_cap(_pct_rank(sig[:i + 1], value) * 100,
                                          _pct_rank(level_values[:i + 5], cot[i + 4][1]), value > 0))
            for i, value in enumerate(sig)
        ]
        indicators.append(ind); scored_keys["g04_comex"] = ind
    else:
        notes.append("COMEX 仓位缺失")

    # G05：0.7×20日变化 + 0.3×当前水平，状态修正：美元与实际利率同升时上限 50
    if len(ofr) > 60:
        raw_sig = [0.7 * (ofr[i][1] - ofr[i - 20][1]) + 0.3 * ofr[i][1]
                   for i in range(60, len(ofr))]
        sig = _ema(raw_sig, 3)
        base = _pct_rank(sig, sig[-1]) * 100
        score = base
        state_note = "OFR信用、股票估值、融资、波动率四类贡献之和；已剔除含黄金价格的安全资产类"
        if len(real) > 20 and len(dxy) > 20:
            dxy_up = dxy[-1][1] > dxy[-21][1]
            real_up = real[-1][1] > real[-21][1]
            if dxy_up and real_up:
                score = min(base, 50)
                if score < base:
                    state_note = "美元与实际利率 20 日同升，金融压力信号受限（≤50）"
        ind = _mk_indicator("g05_stress", ofr, score, sig[-1], state_note)
        real_dates, dxy_dates = [d for d, _ in real], [d for d, _ in dxy]
        stress_hist = []
        sig_dates = [d for d, _ in ofr[60:]]
        for i, (day, value) in enumerate(zip(sig_dates, sig)):
            # 逐点扩展窗口分位：只用截至当日的信号，避免前视
            score_at = _pct_rank(sig[:i + 1], value) * 100
            ri, di = bisect_right(real_dates, day) - 1, bisect_right(dxy_dates, day) - 1
            if ri >= 20 and di >= 20 and real[ri][1] > real[ri - 20][1] and dxy[di][1] > dxy[di - 20][1]:
                score_at = min(score_at, 50)
            stress_hist.append((day, score_at))
        ind["_score_hist"] = stress_hist
        indicators.append(ind); scored_keys["g05_stress"] = ind
    else:
        notes.append("OFR 金融压力缺失")

    # G06：IMF 全球月度储备存量的滚动 12 月变化，按 2010 年以来历史分位计分。
    if len(cb_m) >= 13:
        rolling = [(cb_m[i][0], cb_m[i][1] - cb_m[i - 12][1]) for i in range(12, len(cb_m))]
        rolling = [(d, v) for d, v in rolling if d >= "2010-01"]
        latest_month = cb_m[-1][1] - cb_m[-2][1]
        ind = _mk_indicator(
            "g06_cb", rolling, _pct_rank([v for _, v in rolling], rolling[-1][1]) * 100,
            rolling[-1][1],
            f"IMF IFS 月度已报告口径，通常滞后约2个月；最新月 {latest_month:+.1f} 吨；"
            "不含尚未披露交易，历史迟报修订按抓取日留存版本",
        )
        ind["_score_hist"] = _score_history(rolling)
        indicators.append(ind); scored_keys["g06_cb"] = ind
    else:
        notes.append("央行储备月度数据缺失")

    # G07：SHAUPM折 USD/oz − LBMA AM，20日均值对3年滚动中位数的偏离。
    if len(sge_pm) > 130 and len(lbma_am) > 130 and fx:
        fx_map = dict(fx)
        # 汇率序列起点晚于 SGE 时，用最近已知汇率前向补齐，避免溢价序列缺日
        # （20日均值窗口被拉稀为 25-34 个日历日的混合窗口）。
        fx_days = sorted(fx_map)
        first_fx = fx_days[0]
        for d, _ in sge_pm:
            if d < first_fx and d not in fx_map:
                fx_map[d] = fx_map[first_fx]
            elif d not in fx_map:
                i = bisect_right(fx_days, d) - 1
                if i >= 0:
                    fx_map[d] = fx_map[fx_days[i]]
        lbma_map = dict(lbma_am)
        prem: list[tuple[str, float]] = []
        for d, cny_g in sge_pm:
            rate = fx_map.get(d)
            lon = lbma_map.get(d)
            if rate and lon:
                prem.append((d, cny_g * 31.1034768 / rate - lon))
        if len(prem) > 130:
            raw_sig = []
            for i in range(20, len(prem)):
                avg20 = sum(v for _, v in prem[i - 19:i + 1]) / 20
                win = [v for _, v in prem[max(0, i - 750):i + 1]]
                med = sorted(win)[len(win) // 2]
                raw_sig.append(avg20 - med)
            sig = _ema(raw_sig, 3)
            prem_latest = prem[-1][1]
            ind = _mk_indicator("g07_sglb", prem, _pct_rank(sig[-1260:], sig[-1]) * 100, sig[-1],
                                "WGC方法：SGE SHAUPM折USD − ICE LBMA Gold Price AM；方向性理论价差")
            ind["_score_hist"] = _score_history(
                list(zip([d for d, _ in prem[20:]], sig)), sig[-1260:])
            ind["value"] = round(prem_latest, 2)
            ind["value_text"] = f"{prem_latest:+.1f} USD/oz"
            indicators.append(ind); scored_keys["g07_sglb"] = ind
        else:
            notes.append("上海—伦敦溢价样本不足")
    else:
        notes.append("上海金/伦敦金溢价缺失")

    # G08：逐日 0.6×60日 + 0.4×120日风险调整动量，再做3日EMA。
    if momentum_signal:
        sig = momentum_signal
        if sig:
            score_hist = [v for _, v in sig[-1260:]]
            ind = _mk_indicator("g08_momentum", lbma_pm,
                                _pct_rank(score_hist, sig[-1][1]) * 100, sig[-1][1],
                                "WGC/ICE LBMA Gold Price PM；60/120日风险调整动量，3日EMA")
            ind["_score_hist"] = _score_history(sig, score_hist)
            indicators.append(ind); scored_keys["g08_momentum"] = ind
        else:
            notes.append("动量信号不足")
    else:
        notes.append("伦敦金价序列缺失")

    # 抓取成功但观测期超出来源发布节奏时，指标半权并降低置信度。
    indicator_sources = {**_INDICATOR_SOURCES, "g01_real_rate": real_sources}
    for ind in indicators:
        statuses = [_SOURCE_STATUS.get(key, {}).get("status") for key in indicator_sources[ind["key"]]]
        factor = 0.5 if "stale" in statuses else 1.0
        ind["effective_weight"] = round(ind["weight"] * factor, 4)
        ind["data_status"] = "stale" if factor < 1 else "fresh"

    # 总分：缺失指标剔除、陈旧指标半权，其余权重归一化。
    scored = [i for i in indicators if i["score"] is not None]
    wsum = sum(i["effective_weight"] for i in scored)
    total = round(sum(i["score"] * i["effective_weight"] for i in scored) / wsum, 1) if wsum > 0 else None

    # 维度得分
    score_histories = {ind["key"]: ind.pop("_score_hist", []) for ind in indicators}
    total_hist = _dimension_score_history(
        [(ind["key"], ind["effective_weight"]) for ind in scored], score_histories,
        {ind["key"]: ind["effective_weight"] for ind in scored},
    )
    # 观测日 = 已有趋势的最近观测日（周末/节假日计算沿用上一观测日，不制造非交易日时点）
    current_date = total_hist[-1]["date"] if total_hist else wall_date
    if total is not None:
        _append_current_score(total_hist, current_date, total)
    dims: dict[str, dict] = {}
    for name, parts in _DIM_PARTS.items():
        got = [(scored_keys[k]["score"], scored_keys[k]["effective_weight"])
               for k, _ in parts if k in scored_keys]
        if not got:
            continue
        w = sum(w for _, w in got)
        score = round(sum(s * part_w for s, part_w in got) / w, 1)
        hist = _dimension_score_history(parts, score_histories,
                                        {k: scored_keys[k]["effective_weight"] for k, _ in parts if k in scored_keys})
        _append_current_score(hist, hist[-1]["date"] if hist else wall_date, score)
        dims[name] = {"score": score, "weight": _DIM_WEIGHT[name],
                      "effective_weight": round(w, 4), "hist": hist}

    # 主要贡献
    pos = [i["label"] for i in sorted(scored, key=lambda x: -x["score"]) if i["score"] >= 60][:3]
    neg = [i["label"] for i in sorted(scored, key=lambda x: x["score"]) if i["score"] <= 40][:3]

    # 方向一致性 → 置信度（方案第 9 节）
    confidence = "低"
    if total is not None and dims:
        direction = "up" if total >= 65 else "down" if total <= 44 else None
        if direction:
            agree_w = 0.0
            for name, d in dims.items():
                if direction == "up" and d["score"] >= 60:
                    agree_w += d["effective_weight"]
                elif direction == "down" and d["score"] <= 40:
                    agree_w += d["effective_weight"]
            total_w = sum(d["effective_weight"] for d in dims.values()) or 1
            ratio = agree_w / total_w
            confidence = "高" if ratio >= 0.75 else "中" if ratio >= 0.5 else "低"
        else:
            confidence = "中"

    source_status = list(_SOURCE_STATUS.values())
    stale_sources = [s for s in source_status if s["status"] == "stale"]
    missing_sources = [s for s in source_status if s["status"] == "missing"]
    if stale_sources or missing_sources:
        confidence = "低"
    quality = []
    if stale_sources:
        quality.append("陈旧来源：" + "、".join(s["label"] for s in stale_sources))
    if missing_sources:
        quality.append("缺失来源：" + "、".join(s["label"] for s in missing_sources))
    quality.extend(notes)
    stale_times = [s["fetched_at"] for s in stale_sources if s.get("fetched_at")]
    payload = {
        "schema_version": 3,
        "date": current_date,
        "gold_score": total,
        "hist": total_hist,
        "signal": _signal_label(total) if total is not None else None,
        "confidence": confidence,
        "mode": f"5维{len(scored)}指标（方案 V2.1）",
        "coverage": round(wsum * 100),
        "dimensions": dims,
        "indicators": indicators,
        "top_positive_drivers": pos,
        "top_negative_drivers": neg,
        "data_quality": "；".join(quality) if quality else "正常",
        "source_status": source_status,
        "stale": bool(stale_sources),
        "stale_since": min(stale_times) if stale_times else None,
        "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
    }
    if indicators:
        market._save_json(_SNAPSHOT, payload)
    return payload
