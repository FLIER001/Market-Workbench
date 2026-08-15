"""油价多维评分系统（框架 V1.0，research/oil_price_analysis_framework_v1.0.html）。

框架主线：边际物理稀缺 → 预期库存 → 风险溢价。5 维 8 指标，总分 100（高分=对油价偏多）：
  P01 商业原油库存季调偏离      EIA 周度 WCESTUS1（周）
  P02 原油需求天数              EIA 周度 W_EPC0_VSD_NUS_DAYS（周）
  P03 美国原油产量              EIA 周度 WCRFPUS2（周）
  P04 炼厂原油投入              EIA 周度 WCRRIUS2（周）
  P05 WTI 管理基金净多头        CFTC Disaggregated COT（周）
  P06 地缘风险指数              GPR 官方日度 GPRD（日）
  P07 美元指数                  腾讯 hf_DINIW（日）
  P08 布伦特动量                新浪全球期货日K OIL（日）

工程口径与黄金评分一致：原始信号 → 5 年历史分位 × 100 → EMA 平滑；
P05 带拥挤度上限修正（框架 §14：极端净多+基本面转弱→下行脆弱）。
价格/期限结构层（Brent-WTI、SC 基差、裂解代理）单独成块呈现客观数据，不混入评分权重。
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import subprocess
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import cache_runtime

BEIJING = timezone(timedelta(hours=8))

_SNAPSHOT_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".vibe-research")
_SNAPSHOT = os.path.join(_SNAPSHOT_DIR, "oil_score_snapshot.json")

# EIA v2 / bulk：
# v2 API 无 key 时可用 DEMO_KEY，但限流严格（OVER_RATE_LIMIT 后需长时间退避）。
# 首选 v2 单请求；限流时落盘失败，后台按退避重试。用户可申请 key 后配 VR_EIA_API_KEY。
_EIA_KEY = os.environ.get("VR_EIA_API_KEY") or "DEMO_KEY"
_EIA_SNDW = "https://api.eia.gov/v2/petroleum/sum/sndw/data/"
_CFTC_ZIP = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
_GPR_XLS = "https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls"
# 新浪全球期货日 K（同源黄金 AU0 用的 InnerFutures 接口家族，外盘是 GlobalFutures）
_SINA_GLOBAL_DAILY = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
    "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}"
)
_SINA_CN_FUT_DAILY = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
    "InnerFuturesNewService.getDailyKLine?symbol={sym}"
)

_META = {
    "p01_stocks":  ("美国商业原油库存季调偏离", "边际物理稀缺", 0.22, "{:+.0f} 千桶"),
    "p02_dos":     ("原油需求天数", "边际物理稀缺", 0.08, "{:.1f} 天"),
    "p03_prod":    ("美国原油产量", "供给弹性", 0.13, "{:,.0f} 千桶/日"),
    "p04_runs":    ("炼厂原油投入", "炼化与产品需求", 0.12, "{:,.0f} 千桶/日"),
    "p05_cot":     ("WTI管理基金净多头", "仓位与拥挤度", 0.10, "{:+.1f}% OI"),
    "p06_gpr":     ("地缘风险指数", "风险溢价", 0.10, "{:.0f}"),
    "p07_usd":     ("美元指数", "计价与机会成本", 0.10, "{:.2f}"),
    "p08_mom":     ("布伦特风险调整动量", "趋势确认", 0.15, "{:+.2f}"),
}
_DIM_PARTS = {
    "边际物理稀缺":   [("p01_stocks", 0.733), ("p02_dos", 0.267)],
    "供给弹性":       [("p03_prod", 1.0)],
    "炼化与产品需求": [("p04_runs", 1.0)],
    "风险溢价":       [("p06_gpr", 1.0)],
    "仓位与拥挤度":   [("p05_cot", 1.0)],
    "计价与机会成本": [("p07_usd", 1.0)],
    "趋势确认":       [("p08_mom", 1.0)],
}
_DIM_WEIGHT = {"边际物理稀缺": 0.30, "供给弹性": 0.13, "炼化与产品需求": 0.12,
               "风险溢价": 0.10, "仓位与拥挤度": 0.10, "计价与机会成本": 0.10,
               "趋势确认": 0.15}
_DIM_DISPLAY = [  # 前端展示顺序：主线（稀缺→供给→需求）→ 定价环境 → 溢价/确认
    {"name": "边际物理稀缺", "note": "商业库存季调偏离 + 需求天数"},
    {"name": "供给弹性", "note": "美国产量（页岩响应代理）"},
    {"name": "炼化与产品需求", "note": "炼厂原油投入"},
    {"name": "计价与机会成本", "note": "美元指数"},
    {"name": "风险溢价", "note": "GPR 地缘风险指数"},
    {"name": "仓位与拥挤度", "note": "WTI 管理基金净多头"},
    {"name": "趋势确认", "note": "布伦特动量"},
]

_SOURCE_LABELS = {
    "oil:eia": "EIA 周度石油数据（DEMO_KEY）",
    "oil:cot": "CFTC WTI 原金持仓",
    "oil:gpr": "GPR 地缘风险指数",
    "oil:quotes": "腾讯/新浪外盘行情",
    "oil:series": "新浪全球期货日K",
    "oil:usdidx": "美元指数（frankfurter 合成）",
}
_SOURCE_STATUS: dict[str, dict] = {}

_series_lock = threading.Lock()
_SERIES_MEM: dict[str, tuple[float, list[tuple[str, float]]]] = {}
_SERIES_TTL = 6 * 3600


def _curl(url: str, timeout: int = 20, headers: dict | None = None) -> bytes:
    cmd = ["curl", "-L", "-s", "--max-time", str(timeout)]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    try:
        r = subprocess.run(cmd + [url], capture_output=True, timeout=timeout + 5)
        return r.stdout if r.returncode == 0 else b""
    except Exception:  # noqa: BLE001
        return b""


def _save_bytes(path: str, raw: bytes) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _save_json(path: str, obj) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _series_cached(key: str, fn, ttl: float = _SERIES_TTL):
    """内存 TTL + 快照磁盘兜底（语义对齐 gold_score._series_cached）。

    EIA 周度返回 dict[str, list]，其余是 list[tuple]；统一按值序列化、原样还原。
    """
    now = time.time()
    with _series_lock:
        hit = _SERIES_MEM.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        rows = fn()
    except Exception:  # noqa: BLE001
        rows = []
    path = os.path.join(_SNAPSHOT_DIR, f"oil_series_{key.replace(':', '_')}.json")
    if rows:
        with _series_lock:
            _SERIES_MEM[key] = (now, rows)
        _save_json(path, {"ts": now, "value": rows})
        return rows
    snap = _load_json(path)
    if snap and snap.get("value"):
        return snap["value"]
    return []


# ---------------------------------------------------------------------------
# EIA 周度：库存 / 需求天数 / 产量 / 炼厂投入（单接口单请求，DEMO_KEY 限流下最省调用）
# ---------------------------------------------------------------------------

_EIA_SERIES = {
    "WCESTUS1": "p_stocks",          # 商业库存（不含 SPR）
    "W_EPC0_VSD_NUS_DAYS": "p_dos",  # 需求天数
    "WCRFPUS2": "p_prod",            # 美国产量
    "WCRRIUS2": "p_runs",            # 炼厂净投入
}
_EIA_BULK_URL = "https://api.eia.gov/bulk/PET.zip"
_EIA_BULK_ZIP = os.path.join(_SNAPSHOT_DIR, "eia_pet_bulk.zip")


def _parse_eia_bulk() -> dict[str, list[tuple[str, float]]]:
    """EIA Petroleum Bulk（57MB zip，无需 key、无限流）：全量历史到上周。

    行级 JSON：series_id=PET.WCESTUS1.W 等；data 为 [YYYYMMDD, value] 降序列表。
    每次重解新 zip 前必须清缓存，否则进程内复用旧值、周度更新失效。
    """
    import zipfile as _zf
    _BULK_CACHE.clear()
    with _zf.ZipFile(_EIA_BULK_ZIP) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            import io as _io
            for line in _io.TextIOWrapper(f, encoding="utf-8"):
                sid = line[:64].split('"')[3] if '"' in line[:64] else ""
                if sid in _EIA_BULK_IDS:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    key = _EIA_BULK_IDS[sid]
                    points = {}
                    for period, value in rec.get("data", []):
                        d = f"{str(period)[:4]}-{str(period)[4:6]}-{str(period)[6:8]}"
                        try:
                            v = float(value)
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(v):
                            points[d] = v
                    _BULK_CACHE[key] = sorted(points.items())[-260:]
                if len(_BULK_CACHE) == len(_EIA_BULK_IDS):
                    break
    return dict(_BULK_CACHE)


_EIA_BULK_IDS = {
    "PET.WCESTUS1.W": "p_stocks",
    "PET.W_EPC0_VSD_NUS_DAYS.W": "p_dos",
    "PET.WCRFPUS2.W": "p_prod",
    "PET.WCRRIUS2.W": "p_runs",
    "PET.WCRSTUS1.W": "p_total",
}
_BULK_CACHE: dict[str, list[tuple[str, float]]] = {}


def _parse_eia(raw: bytes) -> dict[str, list[tuple[str, float]]]:
    try:
        rows = json.loads(raw.decode("utf-8", "ignore"))["response"]["data"]
    except (ValueError, KeyError, TypeError):
        return {}
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        series, period, value = r.get("series"), r.get("period"), r.get("value")
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if not series or not period or not math.isfinite(v):
            continue
        if series in _EIA_SERIES:
            out.setdefault(_EIA_SERIES[series], {})[period] = v
        elif series == "WCRSTUS1":  # 总库存，减商业库存得 SPR
            out.setdefault("total", {})[period] = v
    result: dict[str, list[tuple[str, float]]] = {}
    for name, points in out.items():
        if name == "total":
            continue
        result[name] = sorted(points.items())
    if "total" in out and result.get("p_stocks"):
        comm = dict(result["p_stocks"])
        spr = {d: tot - comm[d] for d, tot in out["total"].items() if d in comm}
        result["spr"] = sorted(spr.items())
    return result


def _eia_weekly() -> dict[str, list[tuple[str, float]]]:
    """EIA 周度：bulk zip（无限流、全历史）为常规源，v2 API 为增量刷新兜底。"""
    # bulk 快照缺失或超过 7 天时先重下（57MB，几分钟一次可接受）
    need_bulk = (not os.path.exists(_EIA_BULK_ZIP)
                 or time.time() - os.path.getmtime(_EIA_BULK_ZIP) > 7 * 86400)
    if need_bulk:
        raw = _curl(_EIA_BULK_URL, timeout=300)
        if raw and raw[:2] == b"PK":
            _save_bytes(_EIA_BULK_ZIP, raw)
    if os.path.exists(_EIA_BULK_ZIP):
        parsed = _parse_eia_bulk()
        if parsed.get("p_stocks"):
            stocks = dict(parsed["p_stocks"])
            total = dict(parsed.get("p_total") or [])
            parsed["spr"] = sorted({d: tot - stocks[d] for d, tot in total.items()
                                    if d in stocks}.items())
            parsed.pop("p_total", None)
            return {k: v for k, v in parsed.items() if k != "p_total"}

    # 兜底：v2 单请求（DEMO_KEY 限流常见，间隔重试）
    start = (datetime.now(BEIJING) - timedelta(days=260 * 7)).strftime("%Y-%m-%d")
    url = (_EIA_SNDW + f"?api_key={_EIA_KEY}&frequency=weekly&data%5B0%5D=value"
           + "&facets%5Bduoarea%5D%5B%5D=NUS&facets%5Bproduct%5D%5B%5D=EPC0"
           + f"&start={start}&length=5000")
    last_err = "bulk 不可用"
    for attempt in range(3):
        raw = _curl(url, timeout=45)
        if raw:
            text = raw.decode("utf-8", "ignore")
            if "OVER_RATE_LIMIT" not in text and "API_KEY" not in text:
                parsed = _parse_eia(raw)
                if parsed.get("p_stocks"):
                    return parsed
                last_err = "EIA 周度数据为空"
            else:
                last_err = text[:120]
        else:
            last_err = "EIA 无返回"
        if attempt < 2:
            time.sleep(65)
    raise ValueError(f"EIA 拉取失败：{last_err}")


_EIA_CACHE_TTL = 2 * 3600
_eia_lock = threading.Lock()
_EIA_MEM: tuple[float, dict] | None = None


def _eia_series(name: str) -> list[tuple[str, float]]:
    global _EIA_MEM
    with _eia_lock:
        if _EIA_MEM and time.time() - _EIA_MEM[0] < _EIA_CACHE_TTL:
            return _EIA_MEM[1].get(name) or []
    got = _series_cached("oil:eia", _eia_weekly, ttl=_EIA_CACHE_TTL)
    with _eia_lock:
        _EIA_MEM = (time.time(), got)
    return got.get(name) or []


# ---------------------------------------------------------------------------
# CFTC WTI 管理基金净多头 %OI（复用黄金 COT 的官方年度 zip 通道）
# ---------------------------------------------------------------------------


def _cftc_wti_net() -> list[tuple[str, float]]:
    def fetch():
        year = datetime.now(BEIJING).year
        points: dict[str, float] = {}
        for y in range(year - 5, year + 1):
            raw = _curl(_CFTC_ZIP.format(year=y), timeout=60,
                        headers={"User-Agent": "Mozilla/5.0"})
            if not raw or raw[:2] != b"PK":
                continue
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
                text = zf.read(zf.namelist()[0]).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                continue
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("Market_and_Exchange_Names", "").strip() != \
                        "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE":
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
    return _series_cached("cot", fetch, ttl=24 * 3600)


# ---------------------------------------------------------------------------
# GPR 日度（官方 .xls，pandas+xlrd 解析，venv 已具备）
# ---------------------------------------------------------------------------


def _gpr_daily() -> list[tuple[str, float]]:
    def fetch():
        import pandas as pd
        raw = _curl(_GPR_XLS, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
        if not raw or raw[:4] != b"\xd0\xcf\x11\xe0":
            raise ValueError("GPR xls 下载失败")
        df = pd.read_excel(io.BytesIO(raw), usecols=["date", "GPRD"])
        rows = []
        for d, v in zip(df["date"], df["GPRD"]):
            try:
                day = str(pd.Timestamp(d).date())
                value = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                rows.append((day, value))
        if len(rows) < 500:
            raise ValueError("GPR 样本异常")
        return rows
    return _series_cached("gpr", fetch, ttl=24 * 3600)


# ---------------------------------------------------------------------------
# 行情：腾讯 hf_ 实时（Brent/WTI/NG）+ 新浪日 K（Brent/WTI/SC）
# ---------------------------------------------------------------------------

_SPOT_TTL = 20
_SPOT_CACHE: dict[str, tuple[float, dict]] = {}
_SPOT_LOCK = threading.Lock()


def _parse_hf_quotes(raw: str) -> dict[str, dict]:
    """腾讯/新浪 hf_ 前缀外盘行情（与 gold_score._parse_hf_quotes 同字段口径）。"""
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


def oil_spot() -> dict:
    """Brent / WTI / 天然气实时行情快照（腾讯 hf_ 20 秒档）。"""
    now = time.time()
    with _SPOT_LOCK:
        hit = _SPOT_CACHE.get("spot")
        if hit and now - hit[0] < _SPOT_TTL:
            return hit[1]
        try:
            import astock
            quotes = _parse_hf_quotes(astock._fetch_gtimg(["hf_OIL", "hf_CL", "hf_NG"]))
            if quotes:
                payload = {
                    "brent": quotes.get("OIL"),
                    "wti": quotes.get("CL"),
                    "ng": quotes.get("NG"),
                    "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
                }
                _SPOT_CACHE["spot"] = (now, payload)
                return payload
        except Exception:  # noqa: BLE001
            pass
        if hit:
            return dict(hit[1], stale=True)
        return {"brent": None, "wti": None, "ng": None, "fetched_at": None, "stale": True}


_BRENT_HIST_TTL = 3600  # 日K盘中仅收盘价变动，1 小时足够
_BRENT_HIST_CACHE: tuple[float, dict] | None = None
_BRENT_HIST_LOCK = threading.Lock()


def brent_daily_history(days: int = 400) -> dict:
    """布伦特连续（OIL）日K收盘序列，供评分卡旁的油价近 1 年走势。

    与黄金页 au0_daily_history 同语义：失败回退最近一次成功结果（stale 标记）。
    """
    global _BRENT_HIST_CACHE
    now = time.time()
    with _BRENT_HIST_LOCK:
        if _BRENT_HIST_CACHE and now - _BRENT_HIST_CACHE[0] < _BRENT_HIST_TTL:
            return {**_BRENT_HIST_CACHE[1], "points": _BRENT_HIST_CACHE[1]["points"][-days:]}
    points = _daily_kline("OIL")  # 复用 _KLINE_CACHE，这里只包装语义与兜底
    if points:
        payload = {
            "symbol": "OIL",
            "points": points,
            "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
        with _BRENT_HIST_LOCK:
            _BRENT_HIST_CACHE = (now, payload)
        return {**payload, "points": points[-days:]}
    with _BRENT_HIST_LOCK:
        if _BRENT_HIST_CACHE:
            stale = dict(_BRENT_HIST_CACHE[1])
            stale["points"] = stale["points"][-days:]
            stale["stale"] = True
            return stale
    return {"symbol": "OIL", "points": [], "fetched_at": None, "stale": True}


_KLINE_TTL = 3600
_KLINE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_KLINE_LOCK = threading.Lock()


def _parse_daily_kline(raw: str) -> list[dict]:
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        rows = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    out = []
    for r in rows:
        d = str(r.get("d") or r.get("date") or "")[:10]
        try:
            c = float(r.get("c") or r.get("close"))
        except (TypeError, ValueError):
            continue
        if d and math.isfinite(c) and c > 0:
            out.append({"date": d, "v": round(c, 3)})
    return out


def _daily_kline(sym: str, cn: bool = False) -> list[dict]:
    now = time.time()
    cache_key = f"k:{sym}"
    with _KLINE_LOCK:
        hit = _KLINE_CACHE.get(cache_key)
        if hit and now - hit[0] < _KLINE_TTL:
            return hit[1]
    url = (_SINA_CN_FUT_DAILY if cn else _SINA_GLOBAL_DAILY).format(sym=sym)
    raw = _curl(url, timeout=15, headers={"Referer": "https://finance.sina.com.cn/futures/"})
    raw = raw.decode("gbk", "ignore")
    points = _parse_daily_kline(raw)
    if points:
        with _KLINE_LOCK:
            _KLINE_CACHE[cache_key] = (now, points)
    return points


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------


def _pct_rank(series: list[float], current: float, min_n: int = 20) -> float:
    xs = [v for v in series if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(xs) < min_n:
        return 0.5
    return sum(1 for v in xs if v < current) / len(xs)


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _crowding_cap(score: float, level_pct: float) -> float:
    if level_pct >= 0.95:
        return min(score, 50)
    if level_pct >= 0.90:
        return min(score, 65)
    if level_pct >= 0.80:
        return min(score, 80)
    return score


def _mk_indicator(key, hist, score, signal_val, note="", fmt_extra=None):
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
        "note": note, **(fmt_extra or {}),
    }


def _risk_adj_momentum_series(hist: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """逐日 0.6×60日 + 0.4×120日 风险调整动量（口径与黄金 G08 一致）。"""
    out = []
    for end in range(121, len(hist) + 1):
        seg = hist[end - 121:end]
        vals = [v for _, v in seg]
        rets = [math.log(b / a) for a, b in zip(vals, vals[1:]) if a > 0 and b > 0]
        if len(rets) < 10:
            continue
        mean = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
        if vol < 1e-9:
            continue
        m60 = _single_momentum(vals, 60)
        m120 = _single_momentum(vals, 120)
        if m60 is not None and m120 is not None:
            out.append((hist[end - 1][0], 0.6 * m60 + 0.4 * m120))
    return out


def _single_momentum(vals: list[float], n: int) -> float | None:
    if len(vals) < n + 1:
        return None
    window = vals[-(n + 1):]
    rets = [math.log(b / a) for a, b in zip(window, window[1:]) if a > 0 and b > 0]
    if len(rets) < max(10, n - 5):
        return None
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
    if vol < 1e-9:
        return None
    return (window[-1] / window[0] - 1) / vol


def _weekly_score_history(points: list[tuple[str, float]]) -> list[dict]:
    """周频得分趋势：每周保留最后一点（前端近 1 年走势）。"""
    weekly: dict[tuple[int, int], dict] = {}
    for d, v in points:
        try:
            iso = date.fromisoformat(d[:10]).isocalendar()[:2]
        except ValueError:
            continue
        weekly[iso] = {"date": d, "v": round(v, 1)}
    return list(weekly.values())[-52:]


def _build() -> dict:
    current_date = datetime.now(BEIJING).strftime("%Y-%m-%d")
    stocks = _eia_series("p_stocks")
    dos = _eia_series("p_dos")
    prod = _eia_series("p_prod")
    runs = _eia_series("p_runs")
    spr = _eia_series("spr")
    cot = _cftc_wti_net()
    gpr = _gpr_daily()
    brent = [(p["date"], p["v"]) for p in _daily_kline("OIL")]
    wti_k = [(p["date"], p["v"]) for p in _daily_kline("CL")]
    spot = oil_spot()

    indicators: list[dict] = []
    scored: dict[str, dict] = {}
    notes: list[str] = []

    # P01 库存季调偏离：相对 5 年同周中位数的偏离（框架 §3.3 InventoryZ）
    if len(stocks) >= 60:
        sig = []
        for i, (d, v) in enumerate(stocks):
            # 仅用 90 天前的历史做季节基准，避免近端重复计入
            history = [stocks[j][1] for j in range(i)
                       if _week_dist(stocks[j][0], d) <= 1
                       and stocks[j][0] <= _shift_day(d, -90)]
            if len(history) < 12:
                continue
            med = sorted(history)[len(history) // 2]
            sig.append((d, -(v - med)))  # 库存低于季节性 → 利多
        sig = [(d, v) for d, v in sig if math.isfinite(v)]
        if len(sig) > 40:
            smoothed = list(zip([d for d, _ in sig], _ema([v for _, v in sig], 3)))
            latest = smoothed[-1]
            score = _pct_rank([v for _, v in smoothed[-260:]], latest[1]) * 100
            ind = _mk_indicator("p01_stocks", stocks[-260:], score, latest[1],
                                "EIA 周度；正值=库存低于 5 年同周中位数（偏多）")
            # 展示原始库存水平 + 季调偏离两个口径
            ind["value"] = stocks[-1][1]
            ind["value_text"] = f"{stocks[-1][1]:,.0f} 千桶（{latest[1]:+,.0f} 季调）"
            indicators.append(ind)
            scored["p01_stocks"] = {**ind, "_hist_sig": smoothed}
        else:
            notes.append("库存季调样本不足")
    else:
        notes.append("EIA 库存缺失")

    # P02 需求天数：水平信号，天数低=缓冲薄=利多
    if len(dos) >= 40:
        vals = [v for _, v in dos[-260:]]
        score = _pct_rank([-v for v in vals], -dos[-1][1]) * 100
        ind = _mk_indicator("p02_dos", dos[-260:], score, dos[-1][1],
                            "EIA Days of Supply（不含 SPR）；天数越低缓冲越薄")
        indicators.append(ind)
        # 维度趋势信号与卡片口径一致：水平取反（天数低=利多）
        scored["p02_dos"] = {**ind, "_hist_sig": [(d, -v) for d, v in dos]}
    else:
        notes.append("需求天数缺失")

    # P03 美国产量：4 周变化取反（产量下降=利多）
    if len(prod) >= 8:
        sig = [(prod[i][0], -(prod[i][1] - prod[i - 4][1])) for i in range(4, len(prod))]
        smoothed = list(zip([d for d, _ in sig], _ema([v for _, v in sig], 3)))
        latest = smoothed[-1]
        score = _pct_rank([v for _, v in smoothed[-260:]], latest[1]) * 100
        ind = _mk_indicator("p03_prod", prod[-260:], score, latest[1],
                            "EIA 周度产量；信号为 4 周变化取反（页岩供给响应代理）")
        ind["value"] = prod[-1][1]
        ind["value_text"] = f"{prod[-1][1]:,.0f} 千桶/日（4周 {-latest[1]:+,.0f}）"
        indicators.append(ind)
        scored["p03_prod"] = {**ind, "_hist_sig": smoothed}
    else:
        notes.append("美国产量缺失")

    # P04 炼厂投入：4 周变化（开工意愿上升=原油需求偏强）
    if len(runs) >= 8:
        sig = [(runs[i][0], runs[i][1] - runs[i - 4][1]) for i in range(4, len(runs))]
        smoothed = list(zip([d for d, _ in sig], _ema([v for _, v in sig], 3)))
        latest = smoothed[-1]
        score = _pct_rank([v for _, v in smoothed[-260:]], latest[1]) * 100
        ind = _mk_indicator("p04_runs", runs[-260:], score, latest[1],
                            "EIA Refiner Net Input；开工上升=原油直接需求偏强")
        ind["value"] = runs[-1][1]
        ind["value_text"] = f"{runs[-1][1]:,.0f} 千桶/日（4周 {latest[1]:+,.0f}）"
        indicators.append(ind)
        scored["p04_runs"] = {**ind, "_hist_sig": smoothed}
    else:
        notes.append("炼厂投入缺失")

    # P05 CFTC WTI 管理基金净多头 4 周变化，带拥挤度上限
    if len(cot) > 8:
        sig = [cot[i][1] - cot[i - 4][1] for i in range(4, len(cot))]
        base = _pct_rank(sig[-260:], sig[-1]) * 100
        level_pct = _pct_rank([v for _, v in cot[-260:]], cot[-1][1])
        capped = _crowding_cap(base, level_pct)
        crowd_note = ""
        if capped < base:
            crowd_note = f"净多头处于历史 {level_pct * 100:.0f}% 分位，触发拥挤度上限 {capped:.0f}"
        ind = _mk_indicator("p05_cot", cot[-260:], capped, sig[-1],
                            crowd_note or "CFTC WTI-PHYSICAL 管理基金净多头/未平仓合约")
        indicators.append(ind)
        # 维度趋势信号与卡片口径一致：4 周变化（同方向）
        scored["p05_cot"] = {**ind, "_hist_sig": list(zip([d for d, _ in cot[4:]], sig))}
    else:
        notes.append("CFTC 持仓缺失")

    # P06 GPR：30 日均值（月度化事件强度）
    if len(gpr) >= 60:
        sig = []
        for i in range(30, len(gpr)):
            avg = sum(v for _, v in gpr[i - 29:i + 1]) / 30
            sig.append((gpr[i][0], avg))
        latest = sig[-1]
        score = _pct_rank([v for _, v in sig[-260:]], latest[1]) * 100
        ind = _mk_indicator("p06_gpr", gpr[-260:], score, latest[1],
                            "官方 GPR 日度指数 30 日均值；风险溢价代理，仅在物理层同向时可信")
        indicators.append(ind)
        scored["p06_gpr"] = {**ind, "_hist_sig": sig}
    else:
        notes.append("GPR 缺失")

    # P07 美元指数：60 日收益率取反
    usd = _usd_index_series()
    if len(usd) > 60:
        sig = [(usd[i][0], -(math.log(usd[i][1] / usd[i - 60][1]))) for i in range(60, len(usd))
               if usd[i - 60][1] > 0]
        latest = sig[-1]
        score = _pct_rank([v for _, v in sig[-260:]], latest[1]) * 100
        ind = _mk_indicator("p07_usd", usd[-260:], score, latest[1],
                            "美元指数 60 日收益率取反；框架提醒强内生性，只作计价维度")
        indicators.append(ind)
        scored["p07_usd"] = {**ind, "_hist_sig": sig}
    else:
        notes.append("美元指数缺失")

    # P08 布伦特风险调整动量
    if len(brent) > 121:
        mom = _risk_adj_momentum_series(brent)
        smoothed = list(zip([d for d, _ in mom], _ema([v for _, v in mom], 3)))
        latest = smoothed[-1]
        score = _pct_rank([v for _, v in smoothed[-260:]], latest[1]) * 100
        ind = _mk_indicator("p08_mom", brent[-260:], score, latest[1],
                            "布伦特连续 60/120 日风险调整动量（新浪日K），3 日 EMA")
        # 展示价格水平 + 动量信号两个口径
        ind["value"] = brent[-1][1]
        ind["value_text"] = f"{brent[-1][1]:.2f} USD（动量 {latest[1]:+.2f}）"
        indicators.append(ind)
        scored["p08_mom"] = {**ind, "_hist_sig": smoothed}
    else:
        notes.append("布伦特日K样本不足")

    # 数据源状态
    _SOURCE_STATUS.clear()
    def _mark(key: str, rows, max_age_days: int):
        latest = rows[-1][0] if rows else None
        try:
            age = (datetime.now(BEIJING).date() - date.fromisoformat(latest[:10])).days \
                if latest else None
        except ValueError:
            age = None
        status = "missing" if not rows else \
            "stale" if age is not None and age > max_age_days else "fresh"
        _SOURCE_STATUS[key] = {
            "key": key, "label": _SOURCE_LABELS.get(key, key), "status": status,
            "latest_period": latest, "age_days": age, "max_age_days": max_age_days,
        }
    _mark("oil:eia", stocks, 21)       # 周频，节假日顺延
    _mark("oil:cot", cot, 21)
    _mark("oil:gpr", gpr, 14)
    _mark("oil:series", brent, 10)
    _mark("oil:usdidx", usd, 7)
    if spot.get("stale"):
        _SOURCE_STATUS["oil:quotes"] = {"key": "oil:quotes",
                                        "label": _SOURCE_LABELS["oil:quotes"],
                                        "status": "stale", "latest_period": None,
                                        "age_days": None, "max_age_days": None}

    stale_keys = {s["key"] for s in _SOURCE_STATUS.values() if s["status"] == "stale"}
    for ind in indicators:
        key_sources = {"p01_stocks": ("oil:eia",), "p02_dos": ("oil:eia",),
                       "p03_prod": ("oil:eia",), "p04_runs": ("oil:eia",),
                       "p05_cot": ("oil:cot",), "p06_gpr": ("oil:gpr",),
                       "p07_usd": ("oil:usdidx",), "p08_mom": ("oil:series",)}
        src = key_sources.get(ind["key"], ())
        factor = 0.5 if any(k in stale_keys for k in src) else 1.0
        ind["effective_weight"] = round(ind["weight"] * factor, 4)
        ind["data_status"] = "stale" if factor < 1 else "fresh"

    # 总分（缺失剔除、陈旧半权、归一化）
    got = [i for i in indicators if i["score"] is not None]
    wsum = sum(i["effective_weight"] for i in got)
    total = round(sum(i["score"] * i["effective_weight"] for i in got) / wsum, 1) if wsum > 0 else None

    # 维度得分 + 趋势（趋势必须是 0-100 得分口径：逐日把信号转分位，再按权重合成）
    dims: dict[str, dict] = {}
    for name, parts in _DIM_PARTS.items():
        avail = [(k, w) for k, w in parts if k in scored]
        if not avail:
            continue
        w = sum(pw for _, pw in avail)
        score = round(sum(scored[k]["score"] * pw for k, pw in avail) / w, 1)
        dim_hist = _weekly_score_history(
            _merge_dim_score_signals(
                [(scored[k].get("_hist_sig") or [(p["date"], p["v"]) for p in scored[k]["hist"]], pw)
                 for k, pw in avail]))
        dims[name] = {"score": score, "weight": _DIM_WEIGHT[name],
                      "effective_weight": round(w, 4), "hist": dim_hist}

    # 总分趋势（与总合同口径：各指标信号转分位后按有效权重合成）
    total_hist = _weekly_score_history(
        _merge_dim_score_signals(
            [(scored[k].get("_hist_sig") or [(p["date"], p["v"]) for p in scored[k]["hist"]],
              scored[k].get("effective_weight", scored[k]["weight"]))
             for k in scored]))
    if total is not None and total_hist:
        if total_hist[-1]["date"] == current_date:
            total_hist[-1] = {"date": current_date, "v": total}
        else:
            total_hist.append({"date": current_date, "v": total})

    pos = [i["label"] for i in sorted(got, key=lambda x: -x["score"]) if i["score"] >= 60][:3]
    neg = [i["label"] for i in sorted(got, key=lambda x: x["score"]) if i["score"] <= 40][:3]

    confidence = "低"
    if total is not None and dims:
        direction = "up" if total >= 65 else "down" if total <= 44 else None
        if direction:
            agree_w = sum(d["effective_weight"] for d in dims.values()
                          if (direction == "up" and d["score"] >= 60)
                          or (direction == "down" and d["score"] <= 40))
            ratio = agree_w / (sum(d["effective_weight"] for d in dims.values()) or 1)
            confidence = "高" if ratio >= 0.75 else "中" if ratio >= 0.5 else "低"
        else:
            confidence = "中"
    stale_sources = [s for s in _SOURCE_STATUS.values() if s["status"] != "fresh"]
    if stale_sources:
        confidence = "低"

    # 结构层数据（客观呈现，不入评分）
    structure = _build_structure(brent, wti_k, spot, spr, dos)

    payload = {
        "schema_version": 1,
        "date": current_date,
        "oil_score": total,
        "hist": total_hist,
        "signal": _signal_label(total) if total is not None else None,
        "confidence": confidence,
        "mode": f"7维{len(got)}指标（框架 V1.0）" if len(got) == 8 else f"7维体系·{len(got)}指标可用（框架 V1.0）",
        "coverage": round(wsum * 100),
        "dimensions": dims,
        "dimension_order": _DIM_DISPLAY,
        "indicators": indicators,
        "top_positive_drivers": pos,
        "top_negative_drivers": neg,
        "structure": structure,
        "data_quality": "；".join(
            [f"{s['label']}滞后" for s in stale_sources if s["status"] == "stale"]
            + [f"{s['label']}缺失" for s in stale_sources if s["status"] == "missing"]
            + notes) or "正常",
        "source_status": list(_SOURCE_STATUS.values()),
        "stale": bool(stale_sources),
        "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
    }
    for ind in indicators:  # 内部字段不落盘
        ind.pop("_hist_sig", None)
    if indicators:
        _save_json(_SNAPSHOT, payload)
    return payload


def _week_dist(d1: str, d2: str) -> int:
    """两个日期的 ISO 周序号差（近似同周判断）。"""
    try:
        w1 = date.fromisoformat(d1[:10]).isocalendar()[1]
        w2 = date.fromisoformat(d2[:10]).isocalendar()[1]
        return min(abs(w1 - w2), 52 - abs(w1 - w2))
    except ValueError:
        return 99


def _shift_day(d: str, days: int) -> str:
    try:
        return (date.fromisoformat(d[:10]) + timedelta(days=days)).isoformat()
    except ValueError:
        return d


def _merge_dim_score_signals(pairs: list[tuple[list[tuple[str, float]], float]]) -> list[tuple[str, float]]:
    """多指标信号按日期转分位得分后加权合成（0-100 口径，与卡片得分一致）。

    每个指标用自身信号序列的滚动窗口分位；然后按维度/总分权重合成到共同日期轴。
    """
    if not pairs:
        return []
    per_series: list[tuple[dict[str, float], float]] = []  # (date→score, weight)
    for series, weight in pairs:
        if not series:
            continue
        vals = [v for _, v in series]
        dates = [d for d, _ in series]
        # 滚动分位：截至当日的 260 点窗口（与卡片 _pct_rank 口径一致）
        score_map: dict[str, float] = {}
        for i, d in enumerate(dates):
            window = vals[max(0, i - 260):i + 1]
            if len(window) < 20:
                score_map[d] = 50.0
            else:
                score_map[d] = sum(1 for x in window if x < vals[i]) / len(window) * 100
        per_series.append((score_map, weight))
    if not per_series:
        return []
    dates = sorted({d for sm, _ in per_series for d in sm})
    out = []
    for d in dates:
        num = den = 0.0
        for score_map, weight in per_series:
            v = _last_value_on_or_before(score_map, d)
            if v is not None:
                num += v * weight
                den += weight
        if den > 0:
            out.append((d, num / den))
    return out


def _last_value_on_or_before(score_map: dict[str, float], d: str) -> float | None:
    """score_map 字典无序，线性取 <= d 的最大日期值（系列点数少，可接受）。"""
    best_key, best_v = None, None
    for k, v in score_map.items():
        if k <= d and (best_key is None or k > best_key):
            best_key, best_v = k, v
    return best_v


def _usd_index_series() -> list[tuple[str, float]]:
    """美元指数日 K：frankfurter 官方汇率按 ICE 权重合成（EUR57.6/JPY13.6/GBP11.9/CAD9.1/SEK4.2/CHF3.6）。

    权重与 ICE DXY 一致，合成序列与 DXY 高度同趋势，仅作计价维度信号。
    """
    def fetch():
        start = (datetime.now(BEIJING) - timedelta(days=1400)).strftime("%Y-%m-%d")
        raw = _curl(f"https://api.frankfurter.dev/v1/{start}..?base=USD"
                    "&symbols=EUR,JPY,GBP,CAD,SEK,CHF", timeout=30)
        try:
            rates = json.loads(raw.decode("utf-8", "ignore"))["rates"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("美元指数合成失败")
        rows = []
        for day in sorted(rates):
            r = rates[day]
            try:
                usd_per = {"EUR": 1 / r["EUR"], "JPY": r["JPY"], "GBP": r["GBP"],
                           "CAD": r["CAD"], "SEK": r["SEK"], "CHF": r["CHF"]}
                idx = 50.14348112 * math.prod(v ** w for v, w in zip(
                    (usd_per[k] for k in ("EUR", "JPY", "GBP", "CAD", "SEK", "CHF")),
                    (0.576, 0.136, 0.119, 0.091, 0.042, 0.036)))
            except (KeyError, TypeError, ZeroDivisionError):
                continue
            if math.isfinite(idx) and idx > 0:
                rows.append((day, round(idx, 4)))
        if len(rows) < 100:
            raise ValueError("美元指数样本异常")
        return rows
    return _series_cached("usdidx", fetch, ttl=24 * 3600)


def _build_structure(brent, wti_k, spot, spr, dos) -> dict:
    """价格结构层：基准价差 + 供需锚，全部客观数据。

    Brent/WTI 日 K 交易时区不同（Brent 收盘晚于 WTI），直接按日期对齐会漏掉
    Brent 独有的交易日；改为各取最近 15 个交易日后按「末位对齐」配对（两侧
    点数一致，比严格同日交集多保留约 1/5 的样本，且价差只用于形态观察）。
    """
    sc = [(p["date"], p["v"]) for p in _daily_kline("SC0", cn=True)]
    n = min(15, len(brent), len(wti_k))
    spread_bw = []
    if n > 0:
        b_tail = brent[-n:]
        w_tail = wti_k[-n:]
        spread_bw = [(b_tail[i][0], round(b_tail[i][1] - w_tail[i][1], 2))
                     for i in range(n)
                     if math.isfinite(b_tail[i][1] - w_tail[i][1])]
    # SC 为 CNY/桶、Brent 为 USD/桶 → 名义价差无意义，改为 SC/Brent 比率锚。
    # SC 是夜盘+日盘（T+1 凌晨收盘），与 Brent 天然错一日，同样用末位对齐。
    fx = _fetch_fx()
    ratio_sc = []
    if fx:
        m = min(15, len(sc), len(brent))
        if m > 0:
            s_tail = sc[-m:]
            b_tail = brent[-m:]
            ratio_sc = [(s_tail[i][0], round(s_tail[i][1] / (b_tail[i][1] * fx), 4))
                        for i in range(m)]
    return {
        "brent_wti": [{"date": d, "v": v} for d, v in spread_bw[-40:]],
        "sc_brent_ratio": [{"date": d, "v": v} for d, v in ratio_sc[-40:]],
        "spr": [{"date": d, "v": round(v, 0)} for d, v in (spr or [])[-60:]],
        "days_of_supply": [{"date": d, "v": v} for d, v in (dos or [])[-60:]],
        "usdcny": fx,
        "note": "Brent-WTI 反映美湾物流与品质错配；SC/Brent 比率（汇率折算后）反映亚太采购相对强弱；两图按交易日末位对齐",
    }


def _fetch_fx() -> float | None:
    try:
        raw = subprocess.run(
            ["curl", "-L", "-s", "--max-time", "8", "-H",
             "Referer: https://finance.sina.com.cn",
             "https://hq.sinajs.cn/list=fx_susdcny"],
            capture_output=True, timeout=12).stdout.decode("gbk", "ignore")
        return float(raw.split('"')[1].split(",")[8])
    except Exception:  # noqa: BLE001
        return None


def _signal_label(total: float) -> str:
    if total >= 80: return "强利多"
    if total >= 65: return "利多"
    if total >= 45: return "中性"
    if total >= 20: return "利空"
    return "强利空"


def _warm():
    d = _load_json(_SNAPSHOT)
    return d if isinstance(d, dict) and d.get("schema_version") == 1 and d.get("indicators") else None


def get_oil_score(force: bool = False) -> dict:
    return cache_runtime.get(
        "oil_score_v1", _build, valid=lambda v: bool(v.get("indicators")),
        warm=_warm, ttl=3600, force=force,
        save=lambda v: None,  # _build 内已落盘
    )
