"""黄金环境监测 V3（方法见 research/gold_methodology_v3.md）。

5 维 8 指标，总分 100：
  G01 美国10年期TIPS实际收益率        美国财政部（日；FRED兜底/校验）
  G02 广义贸易加权美元指数            FRED DTWEXBGS（日）
  G03 ETF相对动量模型的流量残差      WGC 100+基金（周，吨）
  G04 COMEX管理基金净多头动量         CFTC Disaggregated COT 官方年报（周）
  G05 OFR金融压力（剔除安全资产）      OFR 官方 fsi.csv（日）
  G06 全球已报告央行滚动储备变动      IMF IFS（月，吨）
  G07 上海—伦敦黄金溢价              WGC/ICE LBMA AM 与 SGE PM（日）
  G08 黄金中期风险调整动量           WGC/ICE LBMA PM USD/oz（日）

单项得分 = 原始信号历史分位 × 100（G06 用 2010 年以来月度序列，其余 5 年日/周频）；
G04 带拥挤度上限修正；G05 带美元+实际利率同升状态修正；核心指标无效时暂停新总分；固定权重，不重归一化。
"""
from __future__ import annotations

import csv
import hashlib
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

import ai_insight
import cache_runtime
import market

BEIJING = timezone(timedelta(hours=8))

_SNAPSHOT = os.path.join(
    os.environ.get("MW_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".market-workbench"),
    "gold_score_snapshot.json")
_CB_GOLD_VINTAGE_DIR = os.path.join(os.path.dirname(_SNAPSHOT), "gold_cb_vintages")
_ETF_VINTAGE_DIR = os.path.join(os.path.dirname(_SNAPSHOT), "gold_etf_vintages")
_WGC_REFERENCE_SNAPSHOT = os.path.join(os.path.dirname(_SNAPSHOT), "gold_wgc_reference_v3.json")

_GOLD_SERIES_TTL = 6 * 3600          # 外部日/周频序列 6h 内 0 外呼

# payload 结构版本。只用于「关键字段能力探测」的降级判断，
# 不再作为快照可用性的硬门槛（见 _load_gold_snapshot）。
_SCHEMA_VERSION = 4
MODEL_VERSION = "gold-environment-3.0"
_RECORD_DIR = os.path.join(os.path.dirname(_SNAPSHOT), "gold_records", MODEL_VERSION)
_INPUT_DIR = os.path.join(os.path.dirname(_SNAPSHOT), "gold_inputs")
_GOLD_BUILD_LOCK = threading.RLock()
# 历史序列下发点数上限。这些序列只喂前端 Sparkline，渲染宽度数百 px，
# 500 点/指标纯属过采样：抽稀到 200 点后 payload 约 180KB → 60KB。
# 而前端持久化层按 UTF-16 计费（180KB ≈ 360KB 配额占用），
# payload 体积直接决定它能否写进 localStorage 配额。
_HIST_MAX_POINTS = 200
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
    "g03_etf":       ("ETF流量残差", "投资资金与仓位", 0.15, "{:.1f} 吨"),
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
_USDCNY_META: dict = {}
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
            # 新浪日K当日 bar 盘后才生成：盘中用 nf_AU0 实时价补当日点，保证走势含最新日期
            try:
                live = _parse_nf_au0(_fetch_sina_cn(""), _au0_prev_settlement())
                today = datetime.now(BEIJING).strftime("%Y-%m-%d")
                if live and live.get("date") == today and live.get("price") \
                        and (not points or points[-1]["date"] < today):
                    points.append({"date": today, "v": round(live["price"], 2)})
            except Exception:  # noqa: BLE001 — 实时补点失败不影响历史序列
                pass
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
            _USDCNY_META['fetched_at'] = datetime.now(BEIJING).isoformat(timespec='seconds')
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
    """PAXG/USDT交易所代理：7×24 实时行情 + 当日分时。

    数据源：Binance 公共行情镜像（data-api.binance.vision）。每枚 PAXG 锚定 1
    盎司伦敦金权益；交易所USDT报价不是伦敦现货定盘，失败回退
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
                "name": "PAXG/USDT · Binance交易所代理",
                'quote_currency': 'USDT',
                'fx_as_of': _USDCNY_META.get('fetched_at'),
                'fx_time_basis': 'fetched_at',
                'fx_stale': bool(_USDCNY_CACHE.get('usdcny') and now - _USDCNY_CACHE['usdcny'][0] > _USDCNY_TTL),
                'conversion_assumption': 'USDT≈USD；使用同一汇率折算，不代表人民币实际收益',
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
        "usable": bool(rows) and not observation_lag,
        "fetch_status": "fallback" if stale else "ok",
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
        return _parse_ofr_ex_safe(raw)[-2400:]
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
                if v == v and float(v) > 0][-2400:]
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
    equal = sum(1 for v in xs if v == current)
    return (below + 0.5 * equal) / len(xs)


def _score_history(rows: list[tuple[str, float]], reference=None, *, expanding=False) -> list[tuple[str, float]]:
    """Same calendar-window midrank for current and historical observations.

    Requires three years of signal observations and minimum density. These are
    revised-source observation scores, NOT historical release-time predictions.
    ``reference`` is retained for older callers; it never selects a prefix.
    """
    out = []
    left = 0
    for i, (day, value) in enumerate(rows):
        end = date.fromisoformat(day[:10] if len(day) >= 10 else day + "-01")
        try:
            cutoff = end.replace(year=end.year - 5).isoformat()
        except ValueError:  # leap day
            cutoff = end.replace(year=end.year - 5, day=28).isoformat()
        if not expanding:
            while left < i and rows[left][0] < cutoff:
                left += 1
        window = rows[left:i + 1]
        first = date.fromisoformat(window[0][0][:10] if len(window[0][0]) >= 10 else window[0][0] + "-01")
        min_points = 36 if len(day) == 7 else 104 if (end - first).days / len(window) > 3 else 500
        if (end - first).days >= 1095 and len(window) >= min_points:
            out.append((day, _pct_rank([v for _, v in window], value) * 100))
    return out


def _crowding_cap(score: float, level_pct: float, rising: bool) -> float:
    if level_pct >= 0.95:
        return min(score, 50)
    if level_pct >= 0.90:
        return min(score, 65)
    if level_pct >= 0.80:
        return min(score, 80)
    return score


def _dimension_score_history(parts, score_histories, weights):
    """Compose release-date scores only. Observation months are not timestamps."""
    if any(len(d) != 10 for rows in score_histories.values() for d, _ in rows):
        raise ValueError('history requires available dates, not observation months')
    return _compose_gold_history([(k, weights.get(k, 0)) for k, _ in parts], score_histories)


def _append_current_score(hist: list[dict], current_date: str, score: float) -> None:
    """把当前实时分作为独立时点放入趋势尾（wall date），不覆盖最近观测日的历史值。"""
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


def _thin(points: list[dict], limit: int = _HIST_MAX_POINTS) -> list[dict]:
    """点数超过 limit 时均匀抽稀，保留首尾与时点跨度。

    这些序列仅供前端 Sparkline 作图，像素宽度远小于点数，抽稀不损失可读性；
    但能把 payload 压掉约 2/3 —— 前端持久化层按 UTF-16 计费，体积直接
    关系到 localStorage 配额能否写下（写不下就退回「每次打开都冷启动」）。
    """
    n = len(points)
    if n <= limit or limit < 2:
        return points
    step = n / limit
    out = [points[min(int(i * step), n - 1)] for i in range(limit)]
    out[-1] = points[-1]        # 末点代表「最新」，必须保留
    return out


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
        "hist": _thin([{"date": d, "v": round(v, 3)} for d, v in hist[-500:]]),
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
    data = market._layered_get(
        "gold_score_v3", lambda: market._run_source_refresh(_build, force),
        valid=lambda v: v.get('model_version') == MODEL_VERSION or bool(v.get('indicators')),
        warm=_load_gold_snapshot,
        ttl=3600,
        force=force,
    )
    if data.get('model_version') != MODEL_VERSION:
        data = {**data, 'gold_score': None, 'signal': '旧版历史参考', 'score_status': 'legacy',
                'validation_status': 'unverified', 'contributions': [], 'hist': [],
                'last_complete_score': {'gold_score': data.get('gold_score'), 'score_as_of': data.get('updated'),
                                        'model_version': data.get('model_version') or 'legacy'}}
    elif data.get('score_status') == 'complete':
        # A cached score must expire even if its background rebuild fails.
        today = datetime.now(BEIJING).date()
        expired = [s for s in data.get('source_status', []) if s.get('active') and
                   (_period_age_days(s.get('latest_period'), today) or 0) > (s.get('max_age_days') or 0)]
        if expired:
            expired_keys = {s['key'] for s in expired}
            indicators = [{**i, 'data_status': 'stale', 'effective_weight': 0}
                          if expired_keys.intersection(i.get('sources', [])) else i for i in data['indicators']]
            data = {**data, 'last_complete_score': {k: data.get(k) for k in ('gold_score','score_as_of','model_version','input_id')},
                    'gold_score': None, 'score_status': 'insufficient', 'signal': '数据不足', 'contributions': [],
                    'indicators': indicators, 'coverage': round(sum(i.get('effective_weight', 0) for i in indicators)*100),
                    'driver_agreement': None, 'data_quality_status': 'insufficient', 'stale': True,
                    'data_quality': '缓存观测已超期，暂停当前总分'}
    return data


# ---------------------------------------------------------------------------
# AI 解读：黄金评分的通俗解释（复用择时配置的 LLM 发现；写回评分快照）
# ---------------------------------------------------------------------------

_INSIGHT_MAX_CHARS = 120  # 每段字数上限（prompt 要求 ≤80，截断兜底）


async def _build_insight(force: bool = False) -> dict[str, str] | str:
    """调 LLM 按机会成本/资金仓位/综合形势三段解读当前黄金评分；失败返回空串。"""
    payload = get_gold_score(force=force)
    if payload.get("gold_score") is None:
        return ""
    from pulse.pulse_insight import chat_json

    def _dim_lines() -> list[str]:
        lines = []
        for name, d in (payload.get("dimensions") or {}).items():
            if not isinstance(d, dict) or d.get("score") is None:
                continue
            lines.append(f"  · {name}（维度权重{d['weight'] * 100:.0f}%）：分 {d['score']:.0f}/100")
        return lines

    def _ind_lines() -> list[str]:
        lines = []
        for i in payload.get("indicators") or []:
            if not isinstance(i, dict) or i.get("score") is None:
                continue
            stale_tag = ""
            if i.get("effective_weight") is not None and i["effective_weight"] < i["weight"]:
                stale_tag = "，数据滞后半权"
            lines.append(
                f"  · {i['label']}（{i['dimension']}，权重{i['weight'] * 100:.0f}%{stale_tag}）："
                f"原始水平 {i.get('value_text') or '—'}，计分信号 {i.get('signal')}，细项 {i.get('details')}，分 {i['score']:.0f}/100，观测日 {i.get('date') or '—'}"
            )
        return lines

    stale = [s["label"] for s in payload.get("source_status") or []
             if isinstance(s, dict) and s.get('active') and not s.get('usable', s.get('status') == 'fresh')]
    prompt = (
        "你是资产管理公司的贵金属策略分析师，为黄金多维评分写三段解读。\n"
        f"模型当前：总分 {payload['gold_score']:.0f}/100、信号「{payload.get('signal')}」、"
        f"驱动一致性 {payload.get('driver_agreement')}%，未经预测校准（{payload.get('mode')}）。\n\n"
        "五维读数如下，仅供你推理——页面已展示全部分数，正文禁止复述：\n"
        "【维度】\n" + "\n".join(_dim_lines()) + "\n"
        "【指标】\n" + "\n".join(_ind_lines()) + "\n"
        "口径（方向语义，引用读数前先核对，禁止照抄读数当方向）：\n"
        "- score是相对历史的环境分，不是上涨概率。低COMEX动量分不代表净仓位水平低；用details中的水平分位判断。\n"
        "- ETF是相对动量回归的流量残差，不是调查预期差或总持仓。央行是已报告储备存量差。\n"
        "- 固定先验权重，未经收益预测验证。不能给买卖建议或将分位当成信号正负。\n"
        + (f"数据状态：{'、'.join(stale)} 滞后或缺失，置信度已降。\n" if stale else "")
        + "\n输出三段，每段 1-2 句、80 字以内：\n"
        "- opportunity_cost：机会成本与美元维度当前状态（实际利率与美元对黄金的压制或支撑、强度如何）\n"
        "- flows_positioning：投资资金与仓位（ETF 流量、COMEX 仓位反映的资金态度，与趋势是否背离）\n"
        "- overall：综合形势（五维合起来看黄金处于什么环境、最大的支撑与拖累分别是什么、信号可信度）\n"
        "- 实描、高信息密度：先判断后依据，写读数背后的含义与维度间的逻辑关系，不罗列子项；"
        "分数/贡献/百分比一律不复述，最多引用一个能强化判断的关键数字；禁过渡语、比喻和两面下注\n"
        "- 语言规范：陈述句 + 常用金融词汇（压制/支撑/背离/透支/确认等）；不预测未来、不给交易指令\n"
        '只返回 JSON：{"opportunity_cost": "...", "flows_positioning": "...", "overall": "..."}，'
        "不要解释或代码块标记。"
    )
    parsed = await chat_json(prompt)
    out = {k: str(parsed.get(k)).strip()[:_INSIGHT_MAX_CHARS]
           for k in ("opportunity_cost", "flows_positioning", "overall")
           if isinstance(parsed.get(k), str) and parsed.get(k).strip()}
    return out if len(out) == 3 else ""


def _valid_insight(v) -> bool:
    return isinstance(v, dict) and all(
        isinstance(v.get(k), str) and v.get(k) for k in
        ("opportunity_cost", "flows_positioning", "overall"))


async def get_ai_insight(force: bool = False) -> dict[str, str] | str:
    """AI 解读三段：手动刷新（force）直接重建；否则返回缓存里已存的解读。

    评分重建后（ai_insight_at 早于评分的 updated）旧解读描述的已不是当前
    读数，非 force 请求也会重生成一次。LLM 失败时保留旧解读，不让页面文字消失。"""
    current = get_gold_score()
    if current.get('score_status') != 'complete':
        return ''
    state_id = current.get('state_id')
    snap = cache_runtime.peek('gold_score_v3') or {}
    if snap.get('ai_input_id') != state_id:
        # Do not fall back to text about a different version, state or input.
        cache_runtime.update_value('gold_score_v3', lambda s: {**s, 'ai_insight': '', 'ai_input_id': state_id})
    result = await ai_insight.cached_insight(
        cache_key="gold_score_v3",
        build=lambda: _build_insight(force=False),
        valid=_valid_insight,
        writable=lambda snap: bool(snap.get("indicators")),
        save=lambda snap: market._save_json(_SNAPSHOT, snap),
        force=force,
    )
    latest = get_gold_score()
    return result if latest.get('state_id') == state_id and latest.get('score_status') == 'complete' else ''


# 快照兜底所需的最小字段集：只要这些在，就够渲染出一版可用的页面。
_SNAPSHOT_REQUIRED = ("indicators", "dimensions", "source_status")


def _load_gold_snapshot():
    """冷启动兜底：关键字段齐全就先用起来，不要求 schema_version 严格相等。

    旧写法用 `schema_version == 3` 做相等判断，等于把「快照可用」绑死在 payload
    结构不变上：任何一次结构演进都会让整张快照被判废，cache_runtime 随即走
    「无可用值 → 同步重建」分支，进程重启后首个请求要干等一次全量冷建
    （实测 50s，前端整段显示「首次计算中」）。统一走 cache_runtime.usable_snapshot
    的能力探测，结构演进只降级为兜底骨架（打 degraded 标记）而非作废。
    """
    snapshot = market._load_json(_SNAPSHOT)
    if isinstance(snapshot, dict) and snapshot.get('model_version') == MODEL_VERSION:
        return snapshot  # an all-missing state is a valid cached response too
    return cache_runtime.usable_snapshot(snapshot, _SNAPSHOT_REQUIRED, schema_version=_SCHEMA_VERSION)


def _proxy_available_day(key: str, period: str) -> str:
    """Conservative calendar proxy ONLY for revised-source research.

    Not an actual release calendar: holidays, publication disruptions and data
    revisions are unknown. Real published records use first-seen UTC timestamps.
    Dates refer to the first subsequent LBMA PM session we allow for research.
    """
    if len(period) == 7:
        y, m = map(int, period.split('-'))
        next_month = date(y + (m == 12), m % 12 + 1, 1)
        return (next_month + timedelta(days=100)).isoformat()
    d = date.fromisoformat(period)
    if key == 'g02_dollar':
        d += timedelta(days=8 - d.weekday())  # following Tuesday, after H.10
    elif key == 'g04_comex':
        d += timedelta(days=(4 - d.weekday()) % 7 + 1)  # after Friday 15:30 ET
    elif key == 'g03_etf':
        d += timedelta(days=7)  # unknown historic release: explicitly lagged proxy
    else:
        for _ in range(3 if key == 'g05_stress' else 1):
            d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
    return d.isoformat()


def _compose_gold_history(parts, histories, *, until=None):
    """Fixed weights; require every part and expire inputs by their own cadence."""
    if not parts or any(not histories.get(k) for k, _ in parts):
        return []
    days = {k: [d for d, _ in histories[k]] for k, _ in parts}
    start = max(days[k][0] for k, _ in parts)
    end = until or max(days[k][-1] for k, _ in parts)
    if start > end:
        return []
    cur = date.fromisoformat(start)
    result = []
    while cur.isoformat() <= end:
        day = cur.isoformat()
        got = []
        for key, weight in parts:
            j = bisect_right(days[key], day) - 1
            # Upper bounds on research carry-forward, not probabilistic penalties.
            max_age = 100 if key == 'g06_cb' else 14 if key in ('g02_dollar', 'g03_etf', 'g04_comex') else 7
            if j < 0 or (cur - date.fromisoformat(days[key][j])).days > max_age:
                break
            got.append(histories[key][j][1] * weight)
        if len(got) == len(parts):
            result.append({'date': day, 'v': round(sum(got) / sum(w for _, w in parts), 3)})
        cur += timedelta(days=1)
    return result


def calculate_gold(inputs: dict, source_status=None, now: datetime | None = None) -> dict:
    """Pure model calculation: no network, persistence, or production cache writes."""
    now = now or datetime.now(BEIJING)
    if now.tzinfo is None:
        raise ValueError('now must have a timezone')
    stamp = now.isoformat(timespec='seconds')
    today = now.date()
    def rows(name):
        return sorted({str(d): float(v) for d, v in inputs.get(name, [])
                       if isinstance(v, (float, int)) and math.isfinite(v)
                       and str(d) <= today.isoformat()}.items())
    real, dollar, etf, cot, stress, cb, fx = [rows(k) for k in ('real', 'dollar', 'etf', 'cot', 'stress', 'cb', 'fx')]
    reference = inputs.get('reference') or {}
    prices = {k: sorted((str(d), float(v)) for d, v in reference.get(k, [])
                       if v and v > 0 and str(d) <= today.isoformat())
              for k in ('lbma_am_usd', 'lbma_pm_usd', 'sge_pm_cny')}
    momentum_raw = _risk_adj_momentum_series(prices['lbma_pm_usd'])
    momentum = [(d, v) for (d, _), v in zip(momentum_raw, _ema([v for _, v in momentum_raw], 3))]
    indicators, histories = [], {}
    observation_histories = {}
    def add(key, raw, signal, note='', *, expanding=False, cap=None, details=None, availability=None):
        if not signal:
            return
        scored = _score_history(signal, expanding=expanding)
        if cap:
            scored = [(d, cap(d, s)) for d, s in scored]
        score = scored[-1][1] if scored and scored[-1][0] == signal[-1][0] else None
        ind = _mk_indicator(key, raw, score, signal[-1][1], note)
        ind['score'] = round(score, 3) if score is not None else None
        ind['signal_date'] = signal[-1][0]
        ind['signal_hist'] = _thin([{'date': d, 'v': round(v, 6)} for d, v in signal[-500:]])
        ind['score_hist'] = _thin([{'date': d, 'v': round(v, 3)} for d, v in scored[-500:]])
        ind['details'] = details or {}
        ind['history_kind'] = 'revised_observation'
        ind['available_at'] = stamp  # conservative first seen at this build; persisted input is immutable
        ind['availability_basis'] = 'first_seen'
        observation_histories[key] = scored
        # Proxy dates are never labelled as actual historical availability.
        dated = [(availability.get(d, _proxy_available_day(key, d)) if availability else _proxy_available_day(key, d), s) for d,s in scored]
        # A delayed dependency may make multiple observations available together.
        histories[key] = sorted({d:s for d,s in dated if d <= today.isoformat()}.items())
        indicators.append(ind)
    for key, data, log_change in [('g01_real_rate', real, False), ('g02_dollar', dollar, True)]:
        if len(data) > 60:
            def change(i, n):
                return math.log(data[i][1] / data[i-n][1]) if log_change else data[i][1] - data[i-n][1]
            values = [-(0.6 * change(i, 20) + 0.4 * change(i, 60)) for i in range(60, len(data))]
            signal = list(zip([d for d, _ in data[60:]], _ema(values, 3)))
            add(key, data, signal, '20/60日变化经3日EMA；分位表示相对历史位置',
                details={'change_20': change(len(data)-1, 20) * (100 if not log_change else 100),
                         'change_60': change(len(data)-1, 60) * 100,
                         'change_unit': 'bp' if not log_change else '对数%'
                         })
    residuals = _etf_surprise_signal(etf, momentum)
    if residuals:
        last = residuals[-1]
        add('g03_etf', etf, [(r['date'], r['smoothed']) for r in residuals],
            '相对价格动量回归的流量残差，非调查预期差；4周EMA',
            details={k: round(last[k], 6) for k in ('flow', 'expected', 'residual', 'smoothed', 'beta')})
    if len(cot) > 4:
        levels = dict(_score_history(cot))
        signals = [(cot[i][0], cot[i][1]-cot[i-4][1]) for i in range(4, len(cot))]
        add('g04_comex', cot, signals, '净多/OI的4周变化；仓位水平与拥挤度独立展示',
            cap=lambda d, s: _crowding_cap(s, levels.get(d, 50)/100, True),
            details={'level_percentile': levels.get(cot[-1][0]), 'change_4w_pp': signals[-1][1]})
    if len(stress) > 20 and len(real) > 20 and len(dollar) > 20:
        values = [0.7*(stress[i][1]-stress[i-20][1])+0.3*stress[i][1] for i in range(20, len(stress))]
        sig = list(zip([d for d, _ in stress[20:]], _ema(values, 3)))
        rd, dd = [d for d, _ in real], [d for d, _ in dollar]
        stress_availability = {}
        def stress_cap(day, score):
            # State inputs are matched at the observation date, never today's state.
            ri, di = bisect_right(rd, day)-1, bisect_right(dd, day)-1
            stress_availability[day] = max(_proxy_available_day('g05_stress',day),
                _proxy_available_day('g01_real_rate',real[ri][0]) if ri>=0 else day,
                _proxy_available_day('g02_dollar',dollar[di][0]) if di>=0 else day)
            return min(score, 50) if ri >= 20 and di >= 20 and real[ri][1] > real[ri-20][1] and dollar[di][1] > dollar[di-20][1] else score
        add('g05_stress', stress, sig, 'OFR剔除安全资产；美元与实际利率同升时封顶50（经验规则）', cap=stress_cap, availability=stress_availability)
    if len(cb) >= 13:
        by_month = dict(cb)
        rolling = [(d, v-by_month[f'{int(d[:4])-1:04d}{d[4:]}']) for d, v in cb
                   if d >= '2010-01' and f'{int(d[:4])-1:04d}{d[4:]}' in by_month]
        add('g06_cb', rolling, rolling, '已报告储备存量12月差；不等于同期全部市场净购买量', expanding=True)
    fx_days, fx_map, am = [d for d, _ in fx], dict(fx), dict(prices['lbma_am_usd'])
    premium = []
    for day, cny in prices['sge_pm_cny']:
        j = bisect_right(fx_days, day)-1
        # Never backfill before first FX, nor carry a stale FX more than 5 days.
        if j >= 0 and day in am and (date.fromisoformat(day)-date.fromisoformat(fx_days[j])).days <= 5:
            premium.append((day, cny*31.1034768/fx_map[fx_days[j]]-am[day]))
    if len(premium) > 20:
        from statistics import median
        sig = []
        for i in range(19, len(premium)):
            cutoff = (date.fromisoformat(premium[i][0])-timedelta(days=1096)).isoformat()
            window = [v for d, v in premium[:i+1] if d >= cutoff]
            sig.append((premium[i][0], sum(v for _, v in premium[i-19:i+1])/20-median(window)))
        sig = [(d, v) for (d, _), v in zip(sig, _ema([v for _, v in sig], 3))]
        add('g07_sglb', premium, sig, 'SHAUPM折USD − LBMA AM；中间价口径，非可交易套利价差')
    add('g08_momentum', momentum, momentum, '60/120日收益÷各期年化波动，0.6/0.4混合、3日EMA',
        details={'return_60': prices['lbma_pm_usd'][-1][1]/prices['lbma_pm_usd'][-61][1]-1,
                 'return_120': prices['lbma_pm_usd'][-1][1]/prices['lbma_pm_usd'][-121][1]-1}
        if len(prices['lbma_pm_usd']) > 120 else {})
    source_map = {s['key']: dict(s) for s in source_status or []}
    active = {**_INDICATOR_SOURCES, 'g01_real_rate': tuple(inputs.get('real_sources') or ('gold:fred:DFII10',))}
    active['g05_stress'] = ('gold:ofr', 'gold:fred:DTWEXBGS', *active['g01_real_rate'])
    valid = []
    for ind in indicators:
        key = ind['key']
        states = [source_map.get(s, {'usable': False, 'status': 'missing'}) for s in active[key]]
        age_limit = 100 if key == 'g06_cb' else 14 if key in ('g02_dollar','g04_comex') else 10 if key == 'g03_etf' else 7
        timely = (_period_age_days(ind['signal_date'], today) or 0) <= age_limit
        usable = all(s.get('usable', s.get('status') == 'fresh') for s in states) and timely
        ind['data_status'] = 'insufficient' if ind['score'] is None else 'fresh' if usable else 'stale'
        ind['sources'] = list(active[key])
        ind['effective_weight'] = ind['weight'] if usable and ind['score'] is not None else 0
        if ind['effective_weight']:
            valid.append(ind)
    complete = len(valid) == len(_META)
    total = round(sum(i['score']*i['weight'] for i in valid), 3) if complete else None
    contribution = sorted([{'key': i['key'], 'label': i['label'], 'points': round((i['score']-50)*i['weight'], 3)}
                           for i in valid], key=lambda x: x['points'], reverse=True) if complete else []
    dims = {}
    for name, parts in _DIM_PARTS.items():
        got = [i for i in valid if i['key'] in dict(parts)]
        score = sum(i['score']*i['weight'] for i in got)/_DIM_WEIGHT[name] if len(got) == len(parts) else None
        dims[name] = {'score': round(score, 3) if score is not None else None, 'weight': _DIM_WEIGHT[name],
                      'hist': _thin(_compose_gold_history([(k, _META[k][2]) for k, _ in parts], histories, until=today.isoformat())[-366:])}
    agreement = None
    if complete:
        direction = 1 if total >= 65 else -1 if total < 45 else 0
        agreement = round(sum(i['weight'] for i in valid if (i['score'] >= 60 if direction > 0 else i['score'] <= 40 if direction < 0 else 40 < i['score'] < 60))*100)
    active_keys = {s for keys in active.values() for s in keys}
    for key, s in source_map.items():
        s['active'] = key in active_keys
    problems = [f"{i['label']}：{i['data_status']}" for i in indicators if i['data_status'] != 'fresh']
    problems += [_META[k][0]+'：缺失' for k in _META if k not in {i['key'] for i in indicators}]
    all_hist = _compose_gold_history([(k, v[2]) for k,v in _META.items()], histories, until=today.isoformat())
    return {'schema_version': _SCHEMA_VERSION, 'model_version': MODEL_VERSION, 'date': today.isoformat(),
            'gold_score': total, 'score_status': 'complete' if complete else 'insufficient',
            'score_as_of': stamp if complete else None, 'history_kind': 'reconstructed_proxy',
            'hist': _thin(all_hist[-366:]), 'recorded_hist': [],
            'signal': _signal_label(total).replace('强利多','明显偏强').replace('强利空','明显偏弱').replace('利多','偏强').replace('利空','偏弱') if complete else '数据不足',
            'confidence': '未经预测校准', 'driver_agreement': agreement,
            'validation_status': 'unverified', 'data_quality_status': 'complete' if complete else 'insufficient',
            'coverage': round(sum(i['weight'] for i in valid)*100), 'mode': '五维八指标 · 固定先验权重',
            'dimensions': dims, 'indicators': indicators, 'contributions': contribution,
            'top_positive_drivers': [c['label'] for c in contribution if c['points'] > 0][:3],
            'top_negative_drivers': [c['label'] for c in reversed(contribution) if c['points'] < 0][:3],
            'data_quality': '；'.join(problems) or '全部核心输入有效', 'source_status': list(source_map.values()),
            'stale': not complete, 'updated': stamp,
            'benchmark': {'usd': _thin([{'date':d,'v':v} for d,v in prices['lbma_pm_usd'][-260:]]),
                          'cny': _thin([{'date':d,'v':v} for d,v in prices['sge_pm_cny'][-260:]])},
            # Full-resolution internal data is removed before the public response.
            '_research_histories': histories, '_observation_histories': observation_histories}


def _gather_gold_inputs():
    _SOURCE_STATUS.clear()
    fred_real = _fred_series('DFII10', 2400)
    treasury = _treasury_real_yield_10y()
    ts = _SOURCE_STATUS.get('gold:treasury:DFII10', {})
    if treasury and ts.get('usable', ts.get('status') == 'fresh') and (not fred_real or treasury[-1][0] >= fred_real[-1][0]):
        real = sorted({**dict(fred_real), **dict(treasury)}.items())
        real_sources = ['gold:treasury:DFII10']
    else:
        real, real_sources = fred_real, ['gold:fred:DFII10']
    return {'real': real, 'real_sources': real_sources,
            'dollar': _fred_series('DTWEXBGS', 2400, ttl=_h10_cache_ttl()),
            'etf': _etf_holdings(), 'cot': _comex_money_net(), 'stress': _ofr_fsi(),
            'cb': _cb_gold_monthly_reported(), 'reference': _wgc_reference_prices(), 'fx': _usd_cny()}


def _hash_gold(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def get_validation_summary():
    data = market._load_json(os.path.join(os.path.dirname(_SNAPSHOT), 'gold_validation.json'))
    if not isinstance(data, dict) or data.get('model_version') != MODEL_VERSION:
        return {'model_version': MODEL_VERSION, 'status': 'not_run', 'independent_oos': False,
                'conclusion': '当前运行目录尚无此版本的验证报告；原始发布版本不足，预测能力尚未证实。'}
    return {k: data.get(k) for k in ('model_version','status','independent_oos','evaluated_at','sample_count','conclusion','source_as_of','source_hash','horizons')}


RISK_MODEL_VERSION = 'gold-risk-prior-1.0'
RISK_DECAYS = (.94, .97, .99)


def calculate_gold_risk(prices, now=None):
    """Frozen variance ensemble. Fractions throughout; never a directional signal."""
    now = now or datetime.now(BEIJING)
    clean = {}
    for day, value in prices:
        try:
            day, value = date.fromisoformat(str(day)).isoformat(), float(value)
        except (ValueError, TypeError):
            continue
        if day <= now.date().isoformat() and math.isfinite(value) and value > 0:
            clean[day] = value
    rows = sorted(clean.items())
    payload = {'model_version':RISK_MODEL_VERSION, 'status':'insufficient',
        'research_status':'research_only', 'independent_oos':False,
        'benchmark':'LBMA PM USD/oz', 'horizon_days':20,
        'observed_at':rows[-1][0] if rows else None, 'calculated_at':now.isoformat(timespec='seconds'),
        'annualized_volatility':None, 'horizon_volatility':None,
        'trailing20_volatility':None, 'trailing60_volatility':None,
        'components':[], 'decays':list(RISK_DECAYS),
        'reason':'至少需要121个有效LBMA PM交易日价格；不使用代理金价补足。'}
    if len(rows) < 121:
        return payload
    if (now.date()-date.fromisoformat(rows[-1][0])).days > 7:
        return {**payload,'status':'stale','reason':'LBMA PM观测已超过7天，暂停生成新风险预测。'}
    returns = [math.log(b[1]/a[1]) for a,b in zip(rows,rows[1:])]
    components = []
    for decay in RISK_DECAYS:
        numerator = denominator = 0.
        for r in returns:
            numerator = decay*numerator+r*r
            denominator = decay*denominator+1
        components.append({'decay':decay,'annualized_volatility':math.sqrt(252*numerator/denominator)})
    annualized = math.sqrt(sum(c['annualized_volatility']**2 for c in components)/len(components))
    from statistics import stdev
    return {**payload,'status':'ready','reason':None,
        'input_id':_hash_gold(rows), 'sample_count':len(rows), 'components':components,
        'annualized_volatility':annualized, 'horizon_volatility':annualized*math.sqrt(20/252),
        'trailing20_volatility':stdev(returns[-20:])*math.sqrt(252),
        'trailing60_volatility':stdev(returns[-60:])*math.sqrt(252)}


def _cached_reference_prices():
    """Shared read-only cache for the two independent gold forecast panels."""
    key = 'gold:wgc_reference_v3'
    hit = market._SUB.get(key) or market._SUB_LAST.get(key)
    snapshot = market._load_json(_WGC_REFERENCE_SNAPSHOT) or {}
    if snapshot.get('data') and (not hit or float(snapshot.get('ts') or 0)>hit[0]):
        hit = (float(snapshot.get('ts') or 0), snapshot['data'])
    prices = (hit[1].get('lbma_pm_usd') or []) if hit else []
    fetched = datetime.fromtimestamp(hit[0],BEIJING).isoformat(timespec='seconds') if hit else None
    return prices, fetched


def get_risk_forecast():
    """Read the same reference-price cache as gold; no HTTP-time research or fetch."""
    prices, fetched = _cached_reference_prices()
    payload = calculate_gold_risk(prices)
    payload['fetched_at'] = fetched
    summary = market._load_json(os.path.join(os.path.dirname(__file__),'data','gold_risk_validation.json'))
    payload['validation'] = summary if isinstance(summary,dict) and summary.get('model_version')==RISK_MODEL_VERSION else None
    return payload


def get_direction_forecast():
    from pathlib import Path
    import gold_direction
    prices, fetched = _cached_reference_prices()
    payload = gold_direction.forecast(prices)
    payload['fetched_at'] = fetched
    payload['benchmark'] = 'LBMA PM USD/oz'
    summary = market._load_json(os.path.join(os.path.dirname(__file__),'data','gold_direction_validation.json'))
    payload['validation'] = summary if isinstance(summary,dict) and summary.get('model_version')==gold_direction.MODEL_VERSION else None
    payload['recorded_at'] = None
    payload['record_error'] = None
    if payload['status'] == 'ready':
        # First-seen predictions are kept, never backdated or overwritten on refresh.
        folder = Path(_SNAPSHOT).parent/'gold_direction_records'/gold_direction.MODEL_VERSION
        path = folder/(payload['source_hash']+'.json')
        try:
            folder.mkdir(parents=True,exist_ok=True)
            record = {'published_at':payload['created_at'],'forecast':payload,
                      'prices':[(d,v) for d,v in prices if d<=payload['observed_at']]}
            try:
                with path.open('x',encoding='utf-8') as output:
                    json.dump(record,output,ensure_ascii=False,allow_nan=False)
            except FileExistsError:
                record = json.loads(path.read_text())
            payload['recorded_at'] = record['published_at']
        except (OSError,ValueError,KeyError):
            payload['record_error'] = '本次预测未能留存，暂不可用于后续真实记录检验。'
    return payload


def _persist_gold(payload, inputs):
    """Immutable input revisions + first-published states; identical builds dedupe."""
    old = market._load_json(_SNAPSHOT) or {}
    input_id = _hash_gold(inputs)
    payload['input_id'] = input_id
    input_file = os.path.join(_INPUT_DIR, input_id+'.json')
    if not os.path.exists(input_file):
        market._save_json(input_file, {'input_id': input_id, 'first_seen_at': payload['updated'], 'inputs': inputs})
    else:
        first = (market._load_json(input_file) or {}).get('first_seen_at')
        if first:
            for ind in payload['indicators']:
                ind['available_at'] = first
    if (market._load_json(input_file) or {}).get('input_id') != input_id:
        raise OSError('gold input revision was not persisted')
    prior = old.get('last_complete_score')
    if old.get('model_version') == MODEL_VERSION and old.get('score_status') == 'complete':
        prior = {k: old.get(k) for k in ('gold_score','score_as_of','model_version','input_id')}
    payload['last_complete_score'] = prior
    payload['change_vs_previous'] = None
    if (payload['score_status'] == old.get('score_status') == 'complete'
            and old.get('model_version') == MODEL_VERSION and old.get('input_id') != input_id):
        previous = {c['key']: c['points'] for c in old.get('contributions', [])}
        if len(previous) == len(_META):
            payload['change_vs_previous'] = {
                'as_of': old.get('score_as_of'), 'total_points': round(payload['gold_score']-old['gold_score'], 3),
                'contributions': [{'key':c['key'],'label':c['label'],'points':round(c['points']-previous[c['key']],3)}
                                  for c in payload['contributions']]}
    fingerprint = _hash_gold([MODEL_VERSION, input_id, payload['score_status'], payload['coverage'],
                              [(i['key'], i['data_status'], i['score']) for i in payload['indicators']]])
    payload['state_id'] = fingerprint
    record_file = os.path.join(_RECORD_DIR, fingerprint+'.json')
    if not os.path.exists(record_file):
        market._save_json(record_file, {'published_at': payload['updated'], 'state_id': fingerprint,
            'input_id': input_id, 'model_version': MODEL_VERSION, 'gold_score': payload['gold_score'],
            'score_status': payload['score_status'], 'coverage': payload['coverage'],
            'source_status': payload['source_status'],
            'indicator_scores': {i['key']: i['score'] for i in payload['indicators']}})
    record = market._load_json(record_file) or {}
    if record.get('state_id') != fingerprint:
        raise OSError('gold published record was not persisted')
    if payload['score_status'] == 'complete':
        payload['score_as_of'] = record.get('published_at', payload['score_as_of'])
    from pathlib import Path
    records = [market._load_json(str(p)) for p in Path(_RECORD_DIR).glob('*.json')]
    payload['recorded_hist'] = _thin([{'date': r['published_at'], 'v':r['gold_score']} for r in sorted(
        [r for r in records if r and r.get('gold_score') is not None], key=lambda r:r['published_at'])][-500:])
    payload.pop('_research_histories', None)
    payload.pop('_observation_histories', None)
    market._save_json(_SNAPSHOT, payload)
    if (market._load_json(_SNAPSHOT) or {}).get('state_id') != fingerprint:
        raise OSError('gold snapshot was not persisted')
    return payload


def _build() -> dict:
    with _GOLD_BUILD_LOCK:
        inputs = _gather_gold_inputs()
        payload = calculate_gold(inputs, list(_SOURCE_STATUS.values()))
        return _persist_gold(payload, inputs)
