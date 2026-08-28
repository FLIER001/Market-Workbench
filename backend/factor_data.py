"""因子实验室数据层：全 A 股票池 + 日线面板 + 交易日历 + catalog。

数据边界（探索级，必须随结果展示）：
- 日线为腾讯前复权价（复权因子未单独保留），成交额为原始披露值；
- 股票池为构建时点的存量上市股票 → 幸存者偏差（无退市股历史）；
- 无 point-in-time ST/停牌/涨跌停历史，ST 按构建时名称快照剔除（静态偏差）；
- 上市日期用首根 K 线日期代理。

存储：~/.vibe-research/factor/{bars.csv.gz, instruments.csv.gz, catalog.json}
# ponytail: CSV.gz + pandas，~1400 万行读约 10s；行数或并发用户上来再换 Parquet/DuckDB
# ponytail: 整包重建（无增量日更），日常重建约 20-40 分钟；要日更时再加追加模式
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import astock

DATA_DIR = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "factor",
)
_BARS_FILE = os.path.join(DATA_DIR, "bars.csv.gz")
_INSTRUMENTS_FILE = os.path.join(DATA_DIR, "instruments.csv.gz")
_CALENDAR_FILE = os.path.join(DATA_DIR, "calendar.json")
_CATALOG_FILE = os.path.join(DATA_DIR, "catalog.json")

# 进度状态（模块级，/api/factor/data-status 轮询读取）
_STATE_LOCK = threading.Lock()
_STATE: dict = {"building": False, "pre_acquired": False, "fetched": 0, "total": 0, "failed": 0, "started_at": None, "error": None, "done_at": None}

_dates_cache: list[str] | None = None


def data_version() -> str:
    """当前可用数据的版本号 = catalog 构建完成时间；无数据返回空串。"""
    catalog = load_catalog()
    return catalog.get("built_at", "") if catalog else ""


def load_catalog() -> dict | None:
    try:
        with open(_CATALOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def build_state() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


# ---------------------------------------------------------------------------
# 股票池
# ---------------------------------------------------------------------------

def fetch_instruments() -> list[dict]:
    """全市场 A 股列表（东财 push2 clist 分页）：code/name/流通市值。

    剔除北交所（f13=0 沪/1 深，北交所不在沪深市场过滤内）与当前名称含 ST 的股票。
    """
    merged: dict[str, dict] = {}
    page = 1
    while page <= 20:
        payload = None
        for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
            try:
                response = astock.em_get(
                    f"https://{host}/api/qt/clist/get",
                    params={
                        "pn": str(page), "pz": "1000", "po": "1", "np": "1",
                        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
                        "fid": "f21", "fields": "f12,f13,f14,f21",
                        # 沪深 A 股（剔除退市/停牌隐藏），不含北交所
                        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                    },
                    headers={"User-Agent": astock.UA},
                    timeout=15,
                )
                payload = response.json()
                if payload.get("data") is not None:
                    break
                payload = None
            except Exception:  # noqa: BLE001 — 换备用域名重试
                payload = None
        data = (payload or {}).get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        for row in diff:
            code = str(row.get("f12") or "")
            if len(code) != 6 or not code.isdigit():
                continue
            name = str(row.get("f14") or code)
            if "ST" in name.upper() or "退" in name:
                continue
            merged[code] = {
                "code": code,
                "name": name,
                "float_mcap": astock._number(row.get("f21")) or 0.0,
            }
        total = data.get("total") or 0
        if page * 1000 >= total:
            break
        page += 1
    return sorted(merged.values(), key=lambda item: -item["float_mcap"])


# ---------------------------------------------------------------------------
# 日线拉取（腾讯前复权，全量历史分页拼接）
# ---------------------------------------------------------------------------

def fetch_stock_history(code: str) -> list[dict]:
    """单股全部前复权日线。腾讯单次最多 800 根，倒序分页拉到头后拼接去重。"""
    all_rows: dict[str, dict] = {}
    end_date = ""  # 空 = 从最新开始
    for _ in range(12):  # 上限 ~9600 根 ≈ 39 年，足够全 A
        count = 800
        query_end = f",{end_date}" if end_date else ",,"
        rows = _tencent_kline_paged(code, count, query_end)
        if not rows:
            break
        first_date = rows[0]["date"]
        if first_date in all_rows:
            break
        for row in rows:
            all_rows.setdefault(row["date"], row)
        if len(rows) < count:
            break  # 已到上市头
        end_date = first_date
    return [all_rows[d] for d in sorted(all_rows)]


def _tencent_kline_paged(code: str, count: int, end_suffix: str) -> list[dict]:
    """带结束日期的腾讯日线查询（astock.tencent_kline 不支持 end，这里单独实现）。"""
    import urllib.parse
    import urllib.request

    symbol = f"{astock.get_prefix(code)}{code}"
    param = f"{symbol},day,,{end_suffix.lstrip(',')},{count},qfq" if end_suffix.strip(",") else f"{symbol},day,,,{count},qfq"
    query = urllib.parse.urlencode({"param": param})
    request = urllib.request.Request(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{query}",
        headers={"User-Agent": astock.UA, "Referer": "https://gu.qq.com/"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = astock._parse_tencent_kline(payload, symbol, "day")
        if rows:
            return rows
    except Exception:  # noqa: BLE001
        pass
    # 备用域名（proxy.finance.qq.com，2026-08 起 web.ifzq 被 WAF 拦时的实际主力）
    try:
        import requests as _req

        session = _req.Session()
        session.trust_env = False
        resp = session.get(
            astock._TENCENT_KLINE_FALLBACK,
            params={"_var": "k", "param": param, "r": "0.1"},
            headers={"User-Agent": astock.UA, "Referer": "https://gu.qq.com/"},
            timeout=12,
        )
        text = resp.text
        payload = json.loads(text[text.find("={") + 1:])
        return astock._parse_tencent_kline(payload, symbol, "day")
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

def build_dataset(max_workers: int = 8) -> dict:
    """同步构建全量数据集（调用方在后台线程跑，状态写 _STATE）。

    app 层 POST /api/factor/build 可能已先置位 building（TOCTOU 防双跑占位）——
    此时直接走自己的流程，不要被 already_building 挡回来。
    """
    with _STATE_LOCK:
        if _STATE["building"] and not _STATE.get("pre_acquired"):
            return {"already_building": True}
        _STATE.update(building=True, pre_acquired=False, fetched=0, failed=0,
                      error=None, started_at=_now(), done_at=None, total=0)

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        instruments = fetch_instruments()
        if not instruments:
            raise RuntimeError("股票池拉取为空（东财 clist 不可达）")
        with _STATE_LOCK:
            _STATE["total"] = len(instruments)

        frames = []
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_stock_history, it["code"]): it for it in instruments}
            for future in as_completed(futures):
                it = futures[future]
                try:
                    rows = future.result()
                except Exception:  # noqa: BLE001 — 单股失败不阻塞全局
                    rows = []
                done += 1
                if not rows:
                    with _STATE_LOCK:
                        _STATE["failed"] += 1
                        _STATE["fetched"] = done
                    continue
                frames.append(pd.DataFrame({
                    "code": it["code"],
                    "date": [r["date"] for r in rows],
                    "open": [r["open"] for r in rows],
                    "close": [r["close"] for r in rows],
                    "high": [r["high"] for r in rows],
                    "low": [r["low"] for r in rows],
                    "volume": [r["volume"] for r in rows],
                    "amount": [r["amount"] or 0.0 for r in rows],
                }))
                with _STATE_LOCK:
                    _STATE["fetched"] = done

        if not frames:
            raise RuntimeError("所有个股日线拉取失败")

        panel = pd.concat(frames, ignore_index=True)
        # 腾讯 qfq 深历史在分红密集处会把价格折算到 0 附近甚至负数（前复权基准漂移），
        # 这些价格不可用且会产生 ±1000% 的假收益：丢弃 close <= 0.1 元的行，并剔除因此残缺的股票。
        panel = panel[panel["close"] > 0.1]
        panel = panel[(panel["high"] >= panel["low"]) & (panel["close"] <= panel["high"] * 1.1)
                      & (panel["close"] >= panel["low"] * 0.9)]
        valid_counts = panel.groupby("code")["date"].count()
        keep_codes = set(valid_counts[valid_counts >= 500].index)  # 至少 ~2 年有效数据
        panel = panel[panel["code"].isin(keep_codes)]
        panel.sort_values(["code", "date"], inplace=True)

        # 交易日历 + 指数收盘（贝塔/特质波动的市场基准）：沪深300，全量历史
        index_rows = astock.index_daily_em("1.000300", days=8000)
        calendar = [r["date"] for r in index_rows]

        # 首根 K 线日期 = 上市日代理
        first_bar = panel.groupby("code")["date"].min()

        built_at = _now()
        catalog = {
            "built_at": built_at,
            "stocks": int(panel["code"].nunique()),
            "rows": int(len(panel)),
            "date_min": str(panel["date"].min()),
            "date_max": str(panel["date"].max()),
            "calendar_days": len(calendar),
            "st_excluded": "按构建时名称快照（静态偏差）",
            "biases": BIAS_LABELS,
        }

        _atomic_write_csv(panel, _BARS_FILE)
        inst_df = pd.DataFrame(instruments)
        inst_df["list_date"] = inst_df["code"].map(first_bar).fillna("")
        _atomic_write_csv(inst_df, _INSTRUMENTS_FILE)
        _atomic_write_json({"dates": calendar,
                            "index_close": {r["date"]: r["close"] for r in index_rows},
                            "built_at": built_at}, _CALENDAR_FILE)
        _atomic_write_json(catalog, _CATALOG_FILE)

        with _STATE_LOCK:
            _STATE.update(building=False, done_at=built_at)
        return catalog
    except Exception as exc:  # noqa: BLE001 — 状态机必须收敛
        with _STATE_LOCK:
            _STATE.update(building=False, error=str(exc), done_at=_now())
        raise


BIAS_LABELS = [
    "survivorship_bias: 股票池为构建时点存量上市股票，无退市股历史",
    "qfq_price: 前复权价格，复权因子未单独保留",
    "no_pit_status: 无 point-in-time ST/停牌/涨跌停历史",
    "list_date_proxy: 上市日期用首根 K 线日期代理",
]


def _atomic_write_csv(df: pd.DataFrame, path: str) -> None:
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False, compression="gzip")
    os.replace(tmp, path)


def _atomic_write_json(obj: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 读取（不常驻内存：每次按需读 CSV.gz 的日期子集，用完交给 GC）
# ---------------------------------------------------------------------------

def load_panel(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """日线面板（date,code,open,close,high,low,volume,amount）。

    内存友好：不常驻全量（全 A 全历史 float64 约 2GB），每次读 CSV.gz 过滤后返回，
    调用方用完即弃。注意 pandas 读 CSV.gz 无谓词下推——start/end 只影响返回行数，
    峰值仍是全量（~2GB 瞬时）；要降峰值需换 Parquet/DuckDB（见 ponytail 注）。
    # ponytail: CSV.gz 全量读入后过滤；内存或并发吃紧时上 Parquet 谓词下推
    """
    if not os.path.exists(_BARS_FILE):
        raise FileNotFoundError("因子数据未构建：先调 POST /api/factor/build")
    df = pd.read_csv(_BARS_FILE, dtype={"code": str})
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    return df


def load_instruments() -> pd.DataFrame:
    if not os.path.exists(_INSTRUMENTS_FILE):
        raise FileNotFoundError("因子数据未构建")
    return pd.read_csv(_INSTRUMENTS_FILE, dtype={"code": str})


def load_calendar() -> list[str]:
    try:
        with open(_CALENDAR_FILE, encoding="utf-8") as f:
            return json.load(f).get("dates", [])
    except (OSError, ValueError):
        return []


def load_index_close() -> pd.Series:
    """市场基准（沪深300）收盘序列，date→close。贝塔/特质波动因子用。"""
    try:
        with open(_CALENDAR_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return pd.Series(data.get("index_close", {}), dtype=float).sort_index()
    except (OSError, ValueError):
        return pd.Series(dtype=float)


def lab_status() -> dict:
    catalog = load_catalog()
    state = build_state()
    return {
        "has_data": catalog is not None,
        "catalog": catalog,
        "biases": BIAS_LABELS,
        "building": state["building"],
        "progress": {
            "fetched": state["fetched"], "total": state["total"], "failed": state["failed"],
            "started_at": state["started_at"], "done_at": state["done_at"], "error": state["error"],
        },
    }
