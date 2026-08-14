"""市场总览数据层 —— 市场情绪 + 板块资金流（板块/大盘级公开数据，不涉个股推荐）。

省流量：全站共享一份缓存（TTL 默认 5 分钟），多个用户/多次打开只抓一次；
盘中 5 分钟刷新足够，非交易时段数据本就不变。数据源全免费、无 key。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

import astock
import gstock
import macro_fetch
import cache_runtime

BEIJING = timezone(timedelta(hours=8))
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_INFLIGHT: set[str] = set()
_TTL = 300  # 5 分钟；全站共享，省数据源压力


def _cached(key: str, fn, valid=bool, ttl: int | None = None):
    """TTL 缓存。数据源故障的空结果不缓存（valid 判否），下次请求直接重试。

    入口见 get_liquidity 的 last-good 语义（失败回退旧值）；本函数语义保持"空结果不缓存"，
    供 overview/emotion 等其他调用方继续按原约定使用。
    """
    now = time.time()
    limit = ttl if ttl is not None else _TTL
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < limit:
            return hit[1]
        if hit:
            if key not in _CACHE_INFLIGHT:
                _CACHE_INFLIGHT.add(key)

                def refresh():
                    try:
                        value = fn()
                        if valid(value):
                            with _CACHE_LOCK:
                                _CACHE[key] = (time.time(), value)
                    finally:
                        with _CACHE_LOCK:
                            _CACHE_INFLIGHT.discard(key)

                threading.Thread(target=refresh, daemon=True, name=f"market-cache:{key}").start()
            return hit[1]
    val = fn()
    if valid(val):
        with _CACHE_LOCK:
            _CACHE[key] = (now, val)
    return val


# ---------------------------------------------------------------------------
# 分层缓存：成功结果作为 last-good 长期保留，源故障/断连时回退，指标不再"时有时无"
# ---------------------------------------------------------------------------

# 冷启动兜底：服务重启后若源仍不可用，从磁盘快照恢复整页 last-good，页面直接可渲染
_LIQUIDITY_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "liquidity_snapshot.json")


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_json(path: str, obj) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _load_liquidity_snapshot():
    d = _load_json(_LIQUIDITY_SNAPSHOT)
    if not isinstance(d, dict) or not (d.get("cn") or d.get("us")):
        return None
    try:
        ts = os.path.getmtime(_LIQUIDITY_SNAPSHOT)
    except OSError:
        ts = time.time()
    return ts, d


def _with_stale(data, fetched_at: float | None):
    """给 payload 顶层注入 stale 标记（浅拷贝，不污染缓存里的原件）。"""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if fetched_at is None:
        out.pop("stale", None)
        out.pop("stale_since", None)
    else:
        out["stale"] = True
        out["stale_since"] = datetime.fromtimestamp(fetched_at, BEIJING).strftime("%Y-%m-%d %H:%M")
    return out


# last-good 分层缓存：entry = {"fresh": (ts, val), "last_good": (ts, val), "next_retry": ts}
_LAYERED: dict = {}
# 单源小缓存（利率/债券指数等）：value → (ts, val)
_SUB: dict = {}
_SUB_LAST: dict = {}
_FAILED_STREAK: dict = {}
_REFRESH_THREADS: set = set()
# 全页失败重试退避：40s → 80s → 160s …封顶 10 分钟，避免故障时每秒全量重建打爆上游
_RETRY_BACKOFF = (40, 80, 160, 320, 600)
_SUB_TTL = 6 * 3600   # 利率/债券等低频序列：6 小时内直接用缓存，0 外呼
_SUB_STALE_TTL = 900  # 过期后源故障：继续用 last-good 最多 15 分钟，同时后台重试
_SOURCE_REFRESH = threading.local()


def source_refresh_forced() -> bool:
    return bool(getattr(_SOURCE_REFRESH, "force", False))


def _run_source_refresh(fn, force: bool):
    previous = source_refresh_forced()
    _SOURCE_REFRESH.force = force
    try:
        return fn()
    finally:
        _SOURCE_REFRESH.force = previous


def _layered_get(key: str, build, valid, warm=None, ttl=_TTL, force: bool = False):
    """Compatibility wrapper for the shared stale-while-revalidate cache."""
    return cache_runtime.get(key, build, valid=valid, warm=warm, ttl=ttl, force=force)


def _merge(prev, cur, key: str):
    """本轮新值优先，缺失时回退 last-good 里的同名字段（dict 逐 key / 整段回退）。"""
    if isinstance(prev, dict) and isinstance(cur, dict):
        out = dict(prev)
        out.update({k: v for k, v in cur.items() if v not in (None, {}, [])})
        return out
    return cur if cur not in (None, {}, []) else prev


def _last_good(key: str):
    return cache_runtime.peek(key)


def _kick_bg(key: str, fn) -> None:
    """后台重试（不阻塞当前请求）：stale 缓存续命期间异步刷新。"""
    import threading
    if key in _REFRESH_THREADS:
        return
    _REFRESH_THREADS.add(key)

    def _run():
        try:
            fn()
        finally:
            _REFRESH_THREADS.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def _status_copy(data, *, stale: bool, fetched_at: float | None = None,
                 reason: str | None = None):
    """Copy one source/index payload and attach truthful fallback metadata."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    out["stale"] = stale
    if fetched_at is not None:
        out["fetched_at"] = datetime.fromtimestamp(fetched_at, BEIJING).strftime("%Y-%m-%d %H:%M")
    if reason:
        out["fallback_reason"] = reason
    elif not stale:
        out.pop("fallback_reason", None)
    return out


def _sub_cached(key: str, fn, ttl: float = _SUB_TTL, valid=bool,
                mark_stale: bool = False):
    """单源小缓存：TTL 内直接返回；过期先同步拉一次；失败回 last-good + 后台重试。

    用于利率/债券等低频序列——多数请求 0 外呼，源偶发故障也不丢指标。
    """
    now = time.time()
    hit = _SUB.get(key)
    if hit and not source_refresh_forced() and now - hit[0] < ttl:
        return (_status_copy(hit[1], stale=bool(hit[1].get("stale")), fetched_at=hit[0])
                if mark_stale else hit[1])
    try:
        val = fn()
    except Exception:
        val = None
    if val is not None and valid(val):
        if mark_stale:
            val = _status_copy(val, stale=bool(val.get("stale")), fetched_at=now)
        _SUB[key] = (now, val)
        _SUB_LAST[key] = (now, val)
        return val
    last = _SUB_LAST.get(key)
    if last and now - last[0] < _SUB_STALE_TTL:
        _kick_bg(f"sub:{key}", lambda: _sub_refresh(key, fn, ttl, valid))
        return (_status_copy(last[1], stale=True, fetched_at=last[0],
                             reason="数据源刷新失败，使用最近成功值")
                if mark_stale else last[1])
    return val


def _sub_refresh(key: str, fn, ttl: float, valid) -> None:
    try:
        val = fn()
    except Exception:
        val = None
    if val is not None and valid(val):
        now = time.time()
        _SUB[key] = (now, val)
        _SUB_LAST[key] = (now, val)


def _empty_to_none(v):
    """{} / [] / None → None，便于分层缓存判断「该源这轮彻底失败」。"""
    return None if v in ({}, [], None) else v


# 主力资金流历史快照：push2his 时通时断，成功时落盘，断连时回退快照，避免页面图表数据时有时无
_FLOWS_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "index_flows_snapshot.json")


def _load_flows_snapshot() -> dict:
    try:
        with open(_FLOWS_SNAPSHOT, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and all(isinstance(v.get("hist"), list) for v in d.values()):
            return d
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return {}


def _save_flows_snapshot(flows: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_FLOWS_SNAPSHOT), exist_ok=True)
        tmp = _FLOWS_SNAPSHOT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(flows, f, ensure_ascii=False)
        os.replace(tmp, _FLOWS_SNAPSHOT)
    except OSError:
        pass


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
    return {
        "sentiment": _cached("market_sentiment", _sentiment, valid=bool, ttl=60),
        "sectors": _cached("sector_flows", _sectors, valid=bool, ttl=300),
        "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
    }


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
    """短线情绪（含缓存，2 分钟）。"""
    return _cached("emotion", _emotion, ttl=120)


def get_turnover_top() -> dict:
    """全市场成交额榜 Top20（客观公开榜单，含缓存 5 分钟）。"""
    def build():
        return {
            "stocks": astock.market_turnover_rank(20),
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
    return _cached("turnover_top", build, valid=lambda v: bool(v.get("stocks")), ttl=60)


def get_global_indices() -> list[dict]:
    """全球指数快照（美股 / 港股，含缓存 5 分钟）。空结果不缓存。"""
    return _cached("global_indices", gstock.global_indices, valid=bool, ttl=60)


def get_global_indices_for(keys: list[str]) -> list[dict]:
    """指定市场的全球指数快照（缓存 5 分钟，按 keys 组合缓存）。

    供「市场全景」增量刷新：只请求已开盘的市场，闭市市场由前端本地缓存合并。
    未知 key 直接忽略；全部未知时返回空列表（不缓存）。
    """
    valid_keys = sorted({k for k in keys if k in gstock.INDEX_KEYS})
    if not valid_keys:
        return []
    return _cached("global_indices:" + ",".join(valid_keys),
                   lambda: gstock.global_indices(valid_keys), valid=bool, ttl=60)



# ---------------------------------------------------------------------------
# 资金供给 · 独立页面数据层（国内 + 国外美国，含历史趋势 + 美联储利率）
# ---------------------------------------------------------------------------

# 美债利率 / 美联储 / 利差 FRED 序列定义：key → (series_id, 单位, 中文标签)
# FRED CSV 用 curl 拉取（本环境 requests 直连/代理均超时，但 curl 稳定）。
_FRED_SERIES = {
    # key: (FRED id, display unit, label, raw-value multiplier, frequency)
    "effr":         ("EFFR",       "%",    "有效联邦基金利率 EFFR", 1.0, "日频"),
    "dgs10":        ("DGS10",      "%",    "美债 10 年期收益率", 1.0, "日频"),
    "dgs2":         ("DGS2",       "%",    "美债 2 年期收益率", 1.0, "日频"),
    "dgs3m":        ("DGS3MO",     "%",    "美债 3 个月收益率", 1.0, "日频"),
    "t10y3m":       ("T10Y3M",     "%",    "10Y − 3M 利差", 1.0, "日频"),
    "t10y2y":       ("T10Y2Y",     "%",    "10Y − 2Y 利差", 1.0, "日频"),
    "fed_target_u": ("DFEDTARU",   "%",    "美联储目标利率上限", 1.0, "日频"),
    "fed_target_l": ("DFEDTARL",   "%",    "美联储目标利率下限", 1.0, "日频"),
    "sofr":         ("SOFR",       "%",    "SOFR 担保隔夜融资利率", 1.0, "日频"),
    "tgcr":         ("TGCRRATE",   "%",    "三方一般抵押品回购利率 TGCR", 1.0, "日频"),
    "rrp":          ("RRPONTSYD",  "十亿$", "隔夜逆回购 ON RRP", 1.0, "日频"),
    "dgs30":        ("DGS30",      "%",    "美债 30 年期收益率", 1.0, "日频"),
    # H.4.1 原始单位均为百万美元；换算为亿美元展示和计算。
    "walcl":        ("WALCL",      "亿$",   "美联储总资产", 0.01, "周频"),
    "reserves":     ("WRESBAL",    "亿$",   "银行准备金余额", 0.01, "周频"),
    "tga":          ("WTREGEN",    "亿$",   "美国财政部一般账户 TGA", 0.01, "周频"),
    # 国外综合指数专用。
    "ig_oas":       ("BAMLC0A0CM", "%",    "ICE BofA 投资级公司债 OAS", 1.0, "日频"),
    "hy_oas":       ("BAMLH0A0HYM2","%",   "ICE BofA 高收益债 OAS", 1.0, "日频"),
    "vix":          ("VIXCLS",     "",     "VIX 波动率指数", 1.0, "日频"),
    "iorb":         ("IORB",       "%",    "准备金余额利率 IORB", 1.0, "日频"),
    "term_premium": ("THREEFYTP10","%",    "10Y 期限溢价（Kim-Wright）", 1.0, "日频"),
    "dxy":          ("DTWEXBGS",   "",     "美联储广义美元指数", 1.0, "日频"),
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


# FRED 序列磁盘兜底：重启后 / 连续故障期，用最近一次成功的序列保证国外单卡与指数不断档
_FRED_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "fred_series_snapshot.json")
_FRED_SERIES_TTL = 6 * 3600  # FRED 日频更新，6 小时内直接用缓存，0 外呼


def _load_fred_snapshot() -> dict:
    d = _load_json(_FRED_SNAPSHOT)
    return d if isinstance(d, dict) else {}


def _fred_series_cached(series_id: str, limit: int) -> tuple[list[tuple[str, float]], dict]:
    key = f"fred:{series_id}:{limit}"
    now = time.time()
    hit = _SUB.get(key)
    if hit and now - hit[0] < _FRED_SERIES_TTL:
        return hit[1], {"stale": False, "fetched_at": hit[0]}
    rows = _fred_csv(series_id, limit)
    if rows:
        _SUB[key] = (now, rows)
        _SUB_LAST[key] = (now, rows)
        snap = _load_fred_snapshot()
        snap[key] = {"ts": now, "rows": [[d, v] for d, v in rows]}
        _save_json(_FRED_SNAPSHOT, snap)
        return rows, {"stale": False, "fetched_at": now}
    # 失败：内存 last-good → 磁盘快照
    last = _SUB_LAST.get(key)
    if last:
        _kick_bg(f"sub:{key}", lambda: _fred_refresh(key, series_id, limit))
        return last[1], {"stale": True, "fetched_at": last[0],
                         "fallback_reason": "FRED刷新失败，使用内存最近值"}
    snap_hit = _load_fred_snapshot().get(key)
    if snap_hit and snap_hit.get("rows"):
        rows = [(str(d), float(v)) for d, v in snap_hit["rows"]]
        _SUB_LAST[key] = (float(snap_hit.get("ts") or now), rows)
        ts = float(snap_hit.get("ts") or now)
        return rows, {"stale": True, "fetched_at": ts,
                      "fallback_reason": "FRED刷新失败，使用磁盘快照"}
    return [], {"stale": True, "fetched_at": None, "fallback_reason": "FRED无可用数据"}


def _fred_refresh(key: str, series_id: str, limit: int) -> None:
    rows = _fred_csv(series_id, limit)
    if rows:
        now = time.time()
        _SUB[key] = (now, rows)
        _SUB_LAST[key] = (now, rows)
        snap = _load_fred_snapshot()
        snap[key] = {"ts": now, "rows": [[d, v] for d, v in rows]}
        _save_json(_FRED_SNAPSHOT, snap)


def _fred_latest(series_id: str) -> tuple[str, float] | None:
    d = _fred_csv(series_id, 1)
    return d[-1] if d else None


def _series_map(data: list[tuple[str, float]]) -> dict[str, float]:
    return {d: v for d, v in data}


def _spread_hist(a: list[tuple[str, float]], b: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """两条序列按日期对齐求 a−b（bp 场景外单位一致即原单位差）。"""
    bm = _series_map(b)
    return [(d, va - bm[d]) for d, va in a if d in bm]


def _mk_us_index(label: str, kind: str, comps: list[dict], interpretation: str,
                 desc: str, date: str, hist: list[dict], source_items: list[dict]) -> dict:
    """Build a descriptive U.S. state index with point-in-time history and source status."""
    fetched = [x.get("fetched_at") for x in source_items if x.get("fetched_at")]
    reasons = [x.get("fallback_reason") for x in source_items if x.get("fallback_reason")]
    return {
        "value": hist[-1]["v"], "label": label, "kind": kind, "favorable": "low", "desc": desc,
        "date": date, "hist": hist, "interpretation": interpretation,
        "components": comps, "source": "FRED", "frequency": "日频/周频",
        "coverage": 1.0, "stale": any(bool(x.get("stale")) for x in source_items),
        "fetched_at": min(fetched) if fetched else None,
        **({"fallback_reason": "；".join(dict.fromkeys(reasons))} if reasons else {}),
    }


# 两融历史快照：东财数据中心偶发断连时回退最近一次成功抓取，保证指数子指标图表不断数据
_MARGIN_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "margin_hist_snapshot.json")


def _load_margin_snapshot() -> list:
    try:
        with open(_MARGIN_SNAPSHOT, encoding="utf-8") as f:
            rows = json.load(f)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save_margin_snapshot(rows: list) -> None:
    try:
        os.makedirs(os.path.dirname(_MARGIN_SNAPSHOT), exist_ok=True)
        tmp = _MARGIN_SNAPSHOT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, _MARGIN_SNAPSHOT)
    except OSError:
        pass


def _cn_margin_full() -> dict:
    """国内杠杆资金：全市场两融（东财 RPTA_RZRQ_LSHJ 历史汇总，T+1 披露，取近 60 日趋势）。

    成功拉取即落盘快照；数据源断连时回退快照，避免杠杆温度指数的子指标图表空窗。
    T+1 披露：当日已成功就当天不再重复请求（0 外呼），源故障时回退最近成功日的值。
    """
    now = time.time()
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    hit = _SUB.get("cn_margin")
    if hit and datetime.fromtimestamp(hit[0], BEIJING).strftime("%Y-%m-%d") == today:
        return hit[1]
    rows = astock.eastmoney_datacenter(
        "RPTA_RZRQ_LSHJ", page_size=130, sort_columns="dim_date", sort_types="-1")
    rows_from_fresh = bool(rows)
    if rows:
        _save_margin_snapshot(rows)
    else:
        last = _SUB_LAST.get("cn_margin")
        if last:  # 源故障：回退内存里最近成功日的值，并后台重试
            _kick_bg("sub:cn_margin", _margin_refresh)
            return _status_copy(last[1], stale=True, fetched_at=last[0],
                                reason="两融源刷新失败，使用内存最近值")
        rows = _load_margin_snapshot()
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
    result = {
        "date": str(latest.get("DIM_DATE", ""))[:10],
        "rzye_yi": round(rzye / 1e8, 0),
        "rzye_chg_yi": round((rzye - f(prev, "RZYE")) / 1e8, 1) if prev else None,
        "rzrqye_yi": round(rzrqye / 1e8, 0),
        "rzrqye_chg_yi": round((rzrqye - f(prev, "RZRQYE")) / 1e8, 1) if prev else None,
        "rzjme_yi": round(f(latest, "RZJME") / 1e8, 1),
        "rzrqye_hist": rzrqye_hist,
        "rzjme_hist": rzjme_hist,
        "source": "东方财富·交易所两融汇总",
        "frequency": "T+1交易日",
        "source_date": str(latest.get("DIM_DATE", ""))[:10],
        "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M") if rows_from_fresh else (
            datetime.fromtimestamp(os.path.getmtime(_MARGIN_SNAPSHOT), BEIJING).strftime("%Y-%m-%d %H:%M")
            if os.path.exists(_MARGIN_SNAPSHOT) else None),
        "stale": not rows_from_fresh,
    }
    if not rows_from_fresh:
        result["fallback_reason"] = "两融源刷新失败，使用磁盘快照"
    if rows_from_fresh:
        _SUB["cn_margin"] = (now, result)
        _SUB_LAST["cn_margin"] = (now, result)
    return result


def _margin_refresh() -> None:
    rows = astock.eastmoney_datacenter(
        "RPTA_RZRQ_LSHJ", page_size=130, sort_columns="dim_date", sort_types="-1")
    if rows:
        _save_margin_snapshot(rows)
        _SUB.pop("cn_margin", None)  # 下次请求重走主路径并重建缓存


def _cn_index_flows() -> dict:
    """国内主力资金：上证 / 深成 / 创业板 主力净流入，近 30 日历史 + 当日实时。

    历史走 push2his 域名的 fflow/kline/get（klt=101，可拿 30 个交易日；该域名偶发断连，带重试）；
    当日盘中/收盘的最新值用 push2delay 覆盖末点（push2delay 同路径只回最新 1 天，不能作历史源）。
    60 秒小缓存：盘中每请求 6 次外呼 ×3 指数会触发限流，1 分钟粒度对流水中继足够。
    """
    now = time.time()
    hit = _SUB.get("index_flows")
    if hit and now - hit[0] < 60:
        return hit[1]
    out = _cn_index_flows_fetch()
    if out:
        _SUB["index_flows"] = (now, out)
        _SUB_LAST["index_flows"] = (now, out)
        return out
    # 彻底失败：回退内存 last-good，并后台重试
    last = _SUB_LAST.get("index_flows")
    if last:
        _kick_bg("sub:index_flows", _flows_refresh)
        return {k: _status_copy(v, stale=True, fetched_at=last[0],
                                reason="指数大单流向刷新失败，使用内存最近值")
                for k, v in last[1].items()}
    return out


def _flows_refresh() -> None:
    out = _cn_index_flows_fetch()
    if out:
        now = time.time()
        _SUB["index_flows"] = (now, out)
        _SUB_LAST["index_flows"] = (now, out)


def _cn_index_flows_fetch() -> dict:
    indices = [("1.000001", "上证指数"), ("0.399001", "深证成指"), ("0.399006", "创业板指")]
    headers = {"User-Agent": astock.UA, "Referer": "https://quote.eastmoney.com/"}
    params = {"fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55", "klt": "101", "lmt": "30"}

    def _parse(payload: dict) -> list[dict]:
        hist = []
        for line in ((payload.get("data") or {}).get("klines")) or []:
            p = str(line).split(",")
            if len(p) < 2:
                continue
            try:
                hist.append({"date": p[0], "v": round(float(p[1]) / 1e8, 1)})
            except (TypeError, ValueError):
                continue
        return hist

    # push2his 域名对当前网络时通时断（直连/代理都可能被拒），带重试，尽力而为
    import time as _time

    def _fetch_hist(secid: str, retries: int = 2) -> list[dict]:
        for attempt in range(1 + retries):
            try:
                r = astock.em_get("https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get",
                                  params={**params, "secid": secid}, headers=headers, timeout=10).json()
                hist = _parse(r)
                if hist:
                    return hist
            except Exception:
                pass
            if attempt < retries:
                _time.sleep(0.8 * (attempt + 1))
        return []

    snapshot = _load_flows_snapshot()
    out = {}
    fresh_ok = False
    for secid, name in indices:
        hist: list[dict] = _fetch_hist(secid)
        used_snapshot = False
        if len(hist) > 1:
            fresh_ok = True
        elif len(snapshot.get(secid, {}).get("hist", [])) > 1:
            # 历史源断连：回退本地快照（最近一次成功抓取的 30 日序列）
            hist = list(snapshot[secid]["hist"])
            used_snapshot = True
        try:  # 当日实时覆盖末点（历史源收盘后可能滞后一日；push2delay 稳定）
            r = astock.em_get("https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
                              params={**params, "secid": secid}, headers=headers, timeout=10).json()
            live = _parse(r)
            if live:
                if hist and hist[-1]["date"] == live[-1]["date"]:
                    hist[-1] = live[-1]
                else:
                    hist.append(live[-1])
        except Exception:
            pass
        if hist:
            out[secid] = {
                "name": name, "hist": hist, "latest": hist[-1],
                "source": "东方财富·指数大单流向", "frequency": "盘中/交易日",
                "source_date": hist[-1]["date"],
                "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
                "stale": used_snapshot,
                **({"fallback_reason": "历史接口不可用，历史序列来自磁盘快照"} if used_snapshot else {}),
            }
    if fresh_ok:
        _save_flows_snapshot(out)
    return out


def _kalshi_fed_odds() -> dict:
    """Kalshi 最近一期 FOMC 利率市场：各档目标区间概率（美分报价 ≈ 概率）。"""
    hit = _SUB.get("kalshi_fed_odds")
    if hit and time.time() - hit[0] < 3600:  # 盘中报价 1 小时粒度足够
        return hit[1]
    fresh = _kalshi_fed_odds_fetch()
    if fresh:
        _SUB["kalshi_fed_odds"] = (time.time(), fresh)
    return fresh


def _kalshi_fed_odds_fetch() -> dict:
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


# 美联储目标利率阈值概率的 last-good 文件：Kalshi 挂了时兜底，避免页面空白。
_FED_ODDS_LAST = os.path.join(os.path.expanduser("~"), ".vibe-research", "fed_odds_last.json")


def _fed_odds_with_fallback() -> dict:
    """Kalshi 实时数据优先；取不到时回退到磁盘上的最近一次成功结果，并标注 stale。"""
    fresh = _kalshi_fed_odds()
    if fresh.get("strikes"):
        fetched_at = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
        result = {**fresh, "fetched_at": fetched_at, "stale": False,
                  "source": "Kalshi", "frequency": "小时级"}
        try:
            os.makedirs(os.path.dirname(_FED_ODDS_LAST), exist_ok=True)
            with open(_FED_ODDS_LAST, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception:
            pass
        return result
    # 失败：读盘的最后良好值
    try:
        with open(_FED_ODDS_LAST, encoding="utf-8") as f:
            last = json.load(f)
        if last.get("strikes"):
            last["stale"] = True
            return last
    except Exception:
        pass
    return {}


def _pct_rank(series: list[float], current: float) -> float:
    """Empirical percentile, including the current observation."""
    if not series:
        return 50.0
    below = sum(1 for v in series if v <= current)
    return round(below / len(series) * 100, 1)


def _rolling_rank(points: list[dict], window: int, *, invert: bool = False,
                  min_periods: int = 20) -> list[dict]:
    """Point-in-time percentiles; every date only sees observations available then."""
    clean = [p for p in points if isinstance(p.get("v"), (int, float)) and p.get("date")]
    out = []
    values = [(-p["v"] if invert else p["v"]) for p in clean]
    for i, p in enumerate(clean):
        sample = values[max(0, i - window + 1):i + 1]
        if len(sample) >= min_periods:
            out.append({"date": str(p["date"]), "v": _pct_rank(sample, values[i])})
    return out


def _combine_ranked(weighted: list[tuple[list[dict], float]]) -> list[dict]:
    """Combine already point-in-time-ranked series on their real common dates."""
    if not weighted:
        return []
    maps = [({p["date"]: p["v"] for p in hist}, weight) for hist, weight in weighted]
    dates = set(maps[0][0])
    for m, _ in maps[1:]:
        dates &= set(m)
    total_weight = sum(w for _, w in maps)
    if not dates or total_weight <= 0:
        return []
    return [{"date": d, "v": round(sum(m[d] * w for m, w in maps) / total_weight, 1)}
            for d in sorted(dates)]


def _raw_points(df, date_key: str, value_key: str, *, scale: float = 1.0,
                digits: int = 4) -> list[dict]:
    out = []
    for _, row in df.iterrows():
        try:
            value = float(row[value_key]) * scale
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"date": str(row[date_key])[:10], "v": round(value, digits)})
    return out


_PBC_OMO_INDEX = "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/index.html"
# Official rate-change dates. The live PBOC announcement below supplies the current value.
_OMO_7D_HISTORY = (
    ("2023-01-01", 2.00),
    ("2023-06-13", 1.90),
    ("2023-08-15", 1.80),
    ("2024-07-22", 1.70),
    ("2024-09-27", 1.50),
    ("2025-05-08", 1.40),
)


def _cn_policy_anchor_fetch() -> dict:
    """Read the latest official 7-day reverse-repo rate from the PBOC notice."""
    import html as _html
    import re as _re
    import requests as _requests
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Market-Workbench/1.1)"}
    try:
        index = _requests.get(_PBC_OMO_INDEX, headers=headers, timeout=15)
        index.raise_for_status()
        index.encoding = "utf-8"
        hit = _re.search(r'href="([^"]+/20\d{15,}/index\.html)"', index.text)
        if not hit:
            return {}
        url = "https://www.pbc.gov.cn" + hit.group(1)
        page = _requests.get(url, headers=headers, timeout=15)
        page.raise_for_status()
        page.encoding = "utf-8"
        desc_hit = _re.search(r'<meta\s+name="Description"\s+content="([^"]+)"', page.text, _re.I)
        desc = _html.unescape(desc_hit.group(1)) if desc_hit else ""
        rate_hit = _re.search(r"期限操作利率.*?7天([0-9.]+)%", desc)
        date_hit = _re.search(r"/(20\d{6})\d+/index\.html", url)
        if not rate_hit or not date_hit:
            return {}
        date = f"{date_hit.group(1)[:4]}-{date_hit.group(1)[4:6]}-{date_hit.group(1)[6:8]}"
        return {"rate": float(rate_hit.group(1)), "date": date, "source_url": url,
                "source": "中国人民银行·公开市场业务交易公告", "frequency": "交易日"}
    except Exception:
        return {}


def _cn_policy_anchor() -> dict:
    fallback_date, fallback_rate = _OMO_7D_HISTORY[-1]
    fallback = {"rate": fallback_rate, "date": fallback_date,
                "source": "中国人民银行·政策利率变更表", "frequency": "事件驱动",
                "fallback_reason": "人民银行公告刷新失败，使用最近已核实政策利率"}
    return _sub_cached("cn_policy_anchor", _cn_policy_anchor_fetch, ttl=12 * 3600,
                       valid=lambda v: isinstance(v, dict) and v.get("rate") is not None,
                       mark_stale=True) or _status_copy(fallback, stale=True)


def _omo_rate_on(date: str, anchor: dict) -> float:
    schedule = list(_OMO_7D_HISTORY)
    if anchor.get("date") and anchor.get("rate") is not None:
        schedule.append((anchor["date"], float(anchor["rate"])))
    eligible = [rate for start, rate in sorted(set(schedule)) if start <= date]
    return eligible[-1] if eligible else schedule[0][1]


def _cn_short_liquidity_index() -> dict:
    return _sub_cached("cn_idx:short_liquidity", _cn_short_liquidity_index_fetch,
                       ttl=1800, mark_stale=True)


def _cn_short_liquidity_index_fetch() -> dict:
    try:
        ak = astock._akshare()
        fr = ak.repo_rate_query(symbol="回购定盘利率").tail(180)
        fdr = ak.repo_rate_query(symbol="银银间回购定盘利率").tail(180)
    except Exception:
        return {}
    if fr.empty or fdr.empty:
        return {}
    rows = fr.merge(fdr, on="date", how="inner").tail(130)
    anchor = _cn_policy_anchor()
    policy_spread, nonbank_spread = [], []
    for _, row in rows.iterrows():
        date = str(row["date"])
        try:
            fdr007, fr007 = float(row["FDR007"]), float(row["FR007"])
        except (TypeError, ValueError):
            continue
        policy_spread.append({"date": date, "v": round((fdr007 - _omo_rate_on(date, anchor)) * 100, 1)})
        nonbank_spread.append({"date": date, "v": round((fr007 - fdr007) * 100, 1)})
    r1 = _rolling_rank(policy_spread, 120)
    r2 = _rolling_rank(nonbank_spread, 120)
    hist = _combine_ranked([(r1, 0.5), (r2, 0.5)])
    if not hist:
        return {}
    common_date = hist[-1]["date"]
    pmap1, pmap2 = ({p["date"]: p["v"] for p in x} for x in (r1, r2))
    raw1, raw2 = ({p["date"]: p["v"] for p in x} for x in (policy_spread, nonbank_spread))
    return {
        "value": hist[-1]["v"], "label": "银行间资金压力", "kind": "stress", "favorable": "low",
        "desc": f"FDR007−7天OMO {raw1[common_date]:+.1f}bp · FR007−FDR007 {raw2[common_date]:+.1f}bp",
        "date": common_date,
        "hist": hist,
        "interpretation": "FR/FDR为定盘利率代理，非DR/R成交加权利率",
        "source": "中国货币网·FR/FDR；中国人民银行·7天逆回购",
        "frequency": "交易日", "coverage": 1.0,
        "stale": bool(anchor.get("stale")), "fetched_at": anchor.get("fetched_at"),
        "fallback_reason": anchor.get("fallback_reason"),
        "components": [
            {"label": "FDR007−7天OMO（银银资金/政策锚代理）", "value": f"{raw1[common_date]:+.1f} bp",
             "pct": pmap1[common_date], "date": common_date, "hist": policy_spread[-120:]},
            {"label": "FR007−FDR007（非银分层代理）", "value": f"{raw2[common_date]:+.1f} bp",
             "pct": pmap2[common_date], "date": common_date, "hist": nonbank_spread[-120:]},
        ],
    }


def _cn_policy_rate_index() -> dict:
    return _sub_cached("cn_idx:policy_rate", _cn_policy_rate_index_fetch,
                       ttl=12 * 3600, mark_stale=True)


def _cn_policy_rate_index_fetch() -> dict:
    try:
        ak = astock._akshare()
        df = ak.macro_china_lpr().tail(42)  # 近 3.5 年月度（LPR 月度披露，留冗余覆盖 3 年）
    except Exception:
        return {}
    if df.empty:
        return {}
    anchor = _cn_policy_anchor()
    l1 = _raw_points(df, "TRADE_DATE", "LPR1Y", digits=2)
    l5 = _raw_points(df, "TRADE_DATE", "LPR5Y", digits=2)
    omo = [{"date": p["date"], "v": _omo_rate_on(p["date"], anchor)} for p in l1]
    ranks = [_rolling_rank(x, 42, min_periods=12) for x in (omo, l1, l5)]
    hist = _combine_ranked([(ranks[0], 0.5), (ranks[1], 0.25), (ranks[2], 0.25)])
    if not hist:
        return {}
    date = hist[-1]["date"]
    pmaps = [{p["date"]: p["v"] for p in h} for h in ranks]
    rawmaps = [{p["date"]: p["v"] for p in h} for h in (omo, l1, l5)]
    return {
        "value": hist[-1]["v"], "label": "政策与贷款基准", "kind": "state", "favorable": "low",
        "desc": f"7天OMO {rawmaps[0][date]:.2f}% · LPR 1Y {rawmaps[1][date]:.2f}% · 5Y {rawmaps[2][date]:.2f}%",
        "date": date, "hist": hist,
        "interpretation": "7天OMO权重50%，LPR 1Y/5Y各25%",
        "source": "中国人民银行；全国银行间同业拆借中心", "frequency": "政策事件/月频",
        "coverage": 1.0, "stale": bool(anchor.get("stale")), "fetched_at": anchor.get("fetched_at"),
        "fallback_reason": anchor.get("fallback_reason"),
        "components": [
            {"label": "7天逆回购政策利率（权重50%）", "value": f"{rawmaps[0][date]:.2f}%",
             "pct": pmaps[0][date], "date": anchor.get("date", date), "hist": omo},
            {"label": "LPR 1Y（权重25%）", "value": f"{rawmaps[1][date]:.2f}%",
             "pct": pmaps[1][date], "date": date, "hist": l1},
            {"label": "LPR 5Y（权重25%）", "value": f"{rawmaps[2][date]:.2f}%",
             "pct": pmaps[2][date], "date": date, "hist": l5},
        ],
    }


def _cn_bond_index() -> dict:
    return _sub_cached("cn_idx:bond", _cn_bond_index_fetch,
                       ttl=6 * 3600, mark_stale=True)


def _cn_bond_index_fetch() -> dict:
    try:
        ak = astock._akshare()
        df = ak.bond_treasury_index_cbond().tail(750)  # 近 3 年交易日
    except Exception:
        return {}
    if df.empty:
        return {}

    raw = _raw_points(df, "date", "value", digits=3)
    returns = [{"date": raw[i]["date"], "v": round((raw[i]["v"] / raw[i - 20]["v"] - 1) * 100, 3)}
               for i in range(20, len(raw)) if raw[i - 20]["v"]]
    hist = _rolling_rank(returns, 250, min_periods=60)
    if not hist:
        return {}
    date = hist[-1]["date"]
    latest_return = next(p["v"] for p in reversed(returns) if p["date"] == date)
    return {
        "value": hist[-1]["v"], "label": "债市状态", "kind": "state", "favorable": "low",
        "desc": f"中债国债总净价20日收益 {latest_return:+.2f}%",
        "date": date, "hist": hist,
        "interpretation": "20日收益率的250日滚动分位",
        "source": "中债国债总净价指数（AKShare转接）", "frequency": "交易日",
        "coverage": 1.0, "stale": False,
        "components": [
            {"label": "中债国债总净价20日收益率", "value": f"{latest_return:+.2f}%",
             "pct": hist[-1]["v"], "date": date, "hist": returns[-250:]},
        ],
    }


def _cn_leverage_index(cn_margin: dict) -> dict:
    """Leverage temperature based on stationary changes, not the trending balance level."""
    rzrqye_hist = cn_margin.get("rzrqye_hist", [])
    rzjme_hist = cn_margin.get("rzjme_hist", [])
    if not rzrqye_hist:
        return {}

    jmap = {p["date"]: p["v"] for p in rzjme_hist}
    balances = [p for p in rzrqye_hist if p["date"] in jmap and p["v"]]
    bal_chg, netbuy_ratio = [], []
    for i in range(20, len(balances)):
        cur = balances[i]
        if not balances[i - 20]["v"]:
            continue
        date = cur["date"]
        prior_dates = [p["date"] for p in balances[max(0, i - 4):i + 1]]
        buys = [jmap[d] for d in prior_dates if d in jmap]
        if not buys:
            continue
        bal_chg.append({"date": date, "v": round((cur["v"] / balances[i - 20]["v"] - 1) * 100, 3)})
        netbuy_ratio.append({"date": date, "v": round(sum(buys) / len(buys) / cur["v"] * 10000, 3)})
    r1, r2 = (_rolling_rank(x, 120, min_periods=30) for x in (bal_chg, netbuy_ratio))
    hist = _combine_ranked([(r1, 0.5), (r2, 0.5)])
    if not hist:
        return {}
    date = hist[-1]["date"]
    ranks = [{p["date"]: p["v"] for p in x} for x in (r1, r2)]
    raws = [{p["date"]: p["v"] for p in x} for x in (bal_chg, netbuy_ratio)]
    return {
        "value": hist[-1]["v"], "label": "杠杆温度", "kind": "state", "favorable": "high",
        "desc": f"两融余额20日 {raws[0][date]:+.2f}% · 5日净买入/余额 {raws[1][date]:+.1f}bp",
        "date": date, "hist": hist,
        "interpretation": "余额20日变化、净买入/余额各50%",
        "source": cn_margin.get("source", "东方财富·交易所两融汇总"), "frequency": "T+1交易日",
        "coverage": 1.0, "stale": bool(cn_margin.get("stale")),
        "fetched_at": cn_margin.get("fetched_at"), "fallback_reason": cn_margin.get("fallback_reason"),
        "components": [
            {"label": "两融余额20日变化（权重50%）", "value": f"{raws[0][date]:+.2f}%",
             "pct": ranks[0][date], "date": date, "hist": bal_chg[-120:]},
            {"label": "融资净买入5日均值/两融余额（权重50%）", "value": f"{raws[1][date]:+.1f} bp",
             "pct": ranks[1][date], "date": date, "hist": netbuy_ratio[-120:]},
        ],
    }


def _cn_momentum_index(cn_data: dict) -> dict:
    """Low-confidence flow observation; each index is ranked against its own history."""
    flows = cn_data.get("index_flows", {})
    ranked = []
    components = []
    for flow in flows.values():
        raw = flow.get("hist") or []
        rank = _rolling_rank(raw, 30, min_periods=10)
        if not rank:
            continue
        ranked.append((rank, 1.0))
        latest_date = rank[-1]["date"]
        raw_map = {p["date"]: p["v"] for p in raw}
        components.append({"label": flow.get("name", "指数") + "大单流向",
                           "value": f"{raw_map[latest_date]:+.1f} 亿", "pct": rank[-1]["v"],
                           "date": latest_date, "hist": raw[-30:]})
    hist = _combine_ranked(ranked)
    if not hist:
        return {}
    date = hist[-1]["date"]
    stale = any(bool(f.get("stale")) for f in flows.values())
    return {
        "value": hist[-1]["v"], "label": "大单流向（辅助）", "kind": "auxiliary", "favorable": "high",
        "desc": "上证、深成、创业板分别按自身30日分位后等权观察",
        "date": date, "hist": hist,
        "interpretation": "供应商口径且指数成分重叠，仅作辅助观察",
        "source": "东方财富·指数大单流向", "frequency": "盘中/交易日", "coverage": len(components) / 3,
        "stale": stale, "components": components,
    }


# ---------------------------------------------------------------------------
# 国外（美国）综合指数 —— 分位加权合成 0-100，越高=越紧/越热
# 调研依据：芝加哥联储 NFCI（风险/信用/杠杆三分法）、高盛 FCI（政策利率/长端利率/
# 信用利差/汇率/权益五因子）、纽约联储 10Y-3M 利差衰退预测（Estrella-Mishkin）、
# 纽联储 2026 SOMA 报告（EFFR-IORB 利差=准备金充裕度关键指标）、ICE BofA HY OAS。
# ---------------------------------------------------------------------------

_US_INDEX_WIN = 250  # 国外指数统一窗口：近 250 个交易日（≈1 年），日频数据

def _us_credit_stress_index(us: dict) -> dict:
    """HY/IG credit-spread pressure, equally weighted after rolling ranks."""
    hy = us.get("hy_oas", {}).get("hist") or []
    ig = us.get("ig_oas", {}).get("hist") or []
    if len(hy) < 60 or len(ig) < 60:
        return {}
    rh, ri = _rolling_rank(hy, _US_INDEX_WIN), _rolling_rank(ig, _US_INDEX_WIN)
    hist = _combine_ranked([(rh, 0.5), (ri, 0.5)])
    if not hist:
        return {}
    date = hist[-1]["date"]
    raw_h, raw_i = ({p["date"]: p["v"] for p in x} for x in (hy, ig))
    rank_h, rank_i = ({p["date"]: p["v"] for p in x} for x in (rh, ri))
    return _mk_us_index(
        "信用压力", "stress",
        [{"label": "高收益债 OAS（权重50%）", "value": f"{raw_h[date]:.2f}%",
          "pct": rank_h[date], "date": date, "hist": hy[-_US_INDEX_WIN:]},
         {"label": "投资级债 OAS（权重50%）", "value": f"{raw_i[date]:.2f}%",
          "pct": rank_i[date], "date": date, "hist": ig[-_US_INDEX_WIN:]}],
        "OAS越宽，压力越高",
        f"HY OAS {raw_h[date]:.2f}% · IG OAS {raw_i[date]:.2f}%",
        date, hist, [us["hy_oas"], us["ig_oas"]])


def _us_curve_index(us: dict) -> dict:
    """Long-horizon recession warning; kept separate from current funding stress."""
    s1 = us.get("t10y3m", {}).get("hist") or []
    s2 = us.get("t10y2y", {}).get("hist") or []
    if len(s1) < 60 or len(s2) < 60:
        return {}
    r1 = _rolling_rank(s1, _US_INDEX_WIN, invert=True)
    r2 = _rolling_rank(s2, _US_INDEX_WIN, invert=True)
    hist = _combine_ranked([(r1, 0.7), (r2, 0.3)])
    if not hist:
        return {}
    date = hist[-1]["date"]
    raw1, raw2 = ({p["date"]: p["v"] for p in x} for x in (s1, s2))
    rank1, rank2 = ({p["date"]: p["v"] for p in x} for x in (r1, r2))
    return _mk_us_index(
        "收益率曲线预警", "warning",
        [{"label": "10Y−3M（权重70%，负值为倒挂）", "value": f"{raw1[date]:+.2f}%",
          "pct": rank1[date], "date": date, "hist": s1[-_US_INDEX_WIN:]},
         {"label": "10Y−2Y（权重30%）", "value": f"{raw2[date]:+.2f}%",
          "pct": rank2[date], "date": date, "hist": s2[-_US_INDEX_WIN:]}],
        "中长期预警，非即时信号",
        f"10Y−3M {raw1[date]:+.2f}% · 10Y−2Y {raw2[date]:+.2f}%",
        date, hist, [us["t10y3m"], us["t10y2y"]])


def _us_funding_stress_index(us: dict) -> dict:
    """Three overnight rates relative to IORB, following New York Fed monitoring."""
    sofr = us.get("sofr", {}).get("hist") or []
    tgcr = us.get("tgcr", {}).get("hist") or []
    effr = us.get("effr", {}).get("hist") or []
    iorb = us.get("iorb", {}).get("hist") or []
    if min(map(len, (sofr, tgcr, effr, iorb))) < 60:
        return {}
    base = [(p["date"], p["v"]) for p in iorb]
    spreads = []
    for source in (sofr, tgcr, effr):
        raw = _spread_hist([(p["date"], p["v"]) for p in source], base)
        spreads.append([{"date": d, "v": round(v * 100, 1)} for d, v in raw])
    ranks = [_rolling_rank(x, _US_INDEX_WIN) for x in spreads]
    hist = _combine_ranked([(x, 1.0) for x in ranks])
    if not hist:
        return {}
    date = hist[-1]["date"]
    raws = [{p["date"]: p["v"] for p in x} for x in spreads]
    ranked = [{p["date"]: p["v"] for p in x} for x in ranks]
    labels = ("SOFR−IORB", "TGCR−IORB", "EFFR−IORB")
    comps = [{"label": f"{label}（等权）", "value": f"{raws[i][date]:+.1f} bp",
              "pct": ranked[i][date], "date": date, "hist": spreads[i][-_US_INDEX_WIN:]}
             for i, label in enumerate(labels)]
    return _mk_us_index(
        "短端资金压力", "stress", comps,
        "相对IORB越高，压力越大",
        " · ".join(f"{labels[i]} {raws[i][date]:+.1f}bp" for i in range(3)),
        date, hist, [us[k] for k in ("sofr", "tgcr", "effr", "iorb")])


def _asof_values(points: list[dict], dates: list[str]) -> dict[str, float]:
    """Latest available value on or before each requested date."""
    ordered = sorted(points, key=lambda p: p["date"])
    out, i, latest = {}, 0, None
    for date in sorted(dates):
        while i < len(ordered) and ordered[i]["date"] <= date:
            latest = ordered[i]["v"]
            i += 1
        if latest is not None:
            out[date] = latest
    return out


def _us_qt_index(us: dict) -> dict:
    """System-liquidity pressure: reserves + ON RRP, with TGA as a separate drain."""
    res = us.get("reserves", {}).get("hist") or []
    rrp = us.get("rrp", {}).get("hist") or []
    tga = us.get("tga", {}).get("hist") or []
    if min(map(len, (res, rrp, tga))) < 20:
        return {}
    dates = [p["date"] for p in res]
    rrp_map = _asof_values(rrp, dates)
    tga_map = {p["date"]: p["v"] for p in tga}
    system = [{"date": p["date"], "v": round(p["v"] + rrp_map[p["date"]] * 10, 1)}
              for p in res if p["date"] in rrp_map]
    tga_weekly = [{"date": d, "v": tga_map[d]} for d in dates if d in tga_map]
    lag = 4  # weekly observations: roughly four weeks, not twenty days
    sys_chg = [{"date": system[i]["date"], "v": round(system[i]["v"] - system[i - lag]["v"], 1)}
               for i in range(lag, len(system))]
    tga_chg = [{"date": tga_weekly[i]["date"], "v": round(tga_weekly[i]["v"] - tga_weekly[i - lag]["v"], 1)}
               for i in range(lag, len(tga_weekly))]
    r1 = _rolling_rank(sys_chg, 60, invert=True, min_periods=20)
    r2 = _rolling_rank(tga_chg, 60, min_periods=20)
    hist = _combine_ranked([(r1, 0.5), (r2, 0.5)])
    if not hist:
        return {}
    date = hist[-1]["date"]
    raw1, raw2 = ({p["date"]: p["v"] for p in x} for x in (sys_chg, tga_chg))
    rank1, rank2 = ({p["date"]: p["v"] for p in x} for x in (r1, r2))
    return _mk_us_index(
        "系统流动性压力", "stress",
        [{"label": "准备金+ON RRP四周变化（权重50%，下降为压力）",
          "value": f"{raw1[date]:+.0f} 亿$", "pct": rank1[date], "date": date, "hist": sys_chg[-60:]},
         {"label": "TGA四周变化（权重50%，上升为抽水）",
          "value": f"{raw2[date]:+.0f} 亿$", "pct": rank2[date], "date": date, "hist": tga_chg[-60:]}],
        "系统流动性=准备金+ON RRP；TGA上升为抽水",
        f"系统流动性四周 {raw1[date]:+.0f} 亿$ · TGA四周 {raw2[date]:+.0f} 亿$",
        date, hist, [us["reserves"], us["rrp"], us["tga"]])


def _us_risk_appetite_index(us: dict) -> dict:
    """Market-stress block; aligned to the latest date shared by every component."""
    vix = us.get("vix", {}).get("hist") or []
    dxy = us.get("dxy", {}).get("hist") or []
    tp = us.get("term_premium", {}).get("hist") or []
    if len(vix) < 60 or len(dxy) < 60 or len(tp) < 60:
        return {}
    lag = 20
    dxy_chg = [{"date": dxy[i]["date"], "v": round(dxy[i]["v"] - dxy[i - lag]["v"], 2)}
               for i in range(lag, len(dxy))]
    raws = (vix, dxy_chg, tp)
    ranks = [_rolling_rank(x, _US_INDEX_WIN) for x in raws]
    hist = _combine_ranked([(x, 1.0) for x in ranks])
    if not hist:
        return {}
    date = hist[-1]["date"]
    rawmaps = [{p["date"]: p["v"] for p in x} for x in raws]
    rankmaps = [{p["date"]: p["v"] for p in x} for x in ranks]
    return _mk_us_index(
        "市场压力", "stress",
        [{"label": "VIX（等权）", "value": f"{rawmaps[0][date]:.1f}",
          "pct": rankmaps[0][date], "date": date, "hist": vix[-_US_INDEX_WIN:]},
         {"label": "美联储广义美元指数20日变动（等权）", "value": f"{rawmaps[1][date]:+.2f}",
          "pct": rankmaps[1][date], "date": date, "hist": dxy_chg[-_US_INDEX_WIN:]},
         {"label": "10Y期限溢价（等权）", "value": f"{rawmaps[2][date]:.2f}%",
          "pct": rankmaps[2][date], "date": date, "hist": tp[-_US_INDEX_WIN:]}],
        "VIX、广义美元变动、期限溢价等权",
        f"VIX {rawmaps[0][date]:.1f} · 广义美元20日 {rawmaps[1][date]:+.2f} · 期限溢价 {rawmaps[2][date]:.2f}%",
        date, hist, [us["vix"], us["dxy"], us["term_premium"]])


# ---------------------------------------------------------------------------
# 资金面综合得分（两张卡片）—— 把上面的单指数加权合成为「宽松—友好度」单一分。
# 输入均为各自指数的点时滚动分位（无前视），方向按 favorable 归一（越高越利多）：
#   资金偏松/杠杆偏热/大单流入 = 利多（favorable=high 直接用分位），
#   各类压力/预警 = 利空项（favorable=low 取 100-分位）。
# 历史合成同样只用各指数点时分位序列的公共日期，不重算单指数。
# 依据：芝加哥联储 NFCI（信用/杠杆/风险加权合成）、高盛 FCI（政策利率/长端/信用/
# 汇率/权益权重打分）、国内「货币绝对水平管方向、边际资金管节奏」的实践经验。
# 权重未经收益回测校准，是状态仪表而非交易信号。
# ---------------------------------------------------------------------------

_LIQUIDITY_COMPOSITE_SCHEMA = 1

_CN_LIQUIDITY_COMPOSITE: dict[str, float] = {
    # 单指数 label（cn_indices 的键值）→ 权重（合计 100）
    "银行间资金压力": 25.0,   # 短端资金价格，货币条件的方向锚（反向：压力=利空）
    "政策与贷款基准": 20.0,   # 政策与 LPR 绝对水平，中周期资金成本锚（反向）
    "债市状态": 10.0,         # 债市走强=资金充裕的旁证（反向：分位高=债强=宽松）
    "杠杆温度": 30.0,         # 边际资金入场节奏，对 A 股资金面最直接（正向）
    "大单流向（辅助）": 15.0,  # 供应商口径观察项，降权保留（正向）
}

_US_LIQUIDITY_COMPOSITE: dict[str, float] = {
    "短端资金压力": 25.0,     # SOFR/TGCR/EFFR−IORB，货币市场即时压力（反向）
    "信用压力": 20.0,         # HY/IG OAS，信用条件（反向）
    "系统流动性压力": 20.0,   # 准备金+ON RRP / TGA，量化水位（反向）
    "市场压力": 20.0,         # VIX/美元/期限溢价，风险偏好通道（反向）
    "收益率曲线预警": 15.0,   # 中长期衰退预警，慢变量降权（反向）
}


def _composite_state(score: float) -> str:
    """总分状态标签：偏多/中性偏多/中性/中性偏空/偏空（同宏观面口径）。"""
    if score >= 65:
        return "偏多"
    if score >= 55:
        return "中性偏多"
    if score > 45:
        return "中性"
    if score > 35:
        return "中性偏空"
    return "偏空"


def _liquidity_composite(indices: dict, schema: dict[str, float], label: str, desc: str) -> dict | None:
    """把单指数按权重合成 0-100 宽松—友好度总分；缺失按已覆盖权重归一（<50% 不输出）。

    schema 键为单指数 label（cn_indices/us_indices 里各成员的中文标签）。
    """
    by_label = {idx.get("label"): idx for idx in indices.values() if isinstance(idx, dict)}
    parts = []
    num = 50.0
    covered = 0.0
    for name, weight in schema.items():
        idx = by_label.get(name) or {}
        value = idx.get("value")
        if not isinstance(value, (int, float)):
            parts.append({"name": name, "weight": weight, "score": None, "contribution": None})
            continue
        # 压力/预警类（favorable=low）取 100-分位，归一成越高越利多
        score = 100.0 - value if idx.get("favorable", "high") == "low" else float(value)
        num += (score - 50.0) / 100.0 * weight
        covered += weight
        parts.append({"name": name, "weight": weight, "score": round(score, 1),
                      "contribution": round((score - 50.0) / 100.0 * weight, 1)})
    total_w = sum(schema.values())
    if covered < 0.5 * total_w:
        return None
    score = round(num / total_w * 100.0, 1)
    contribs = [p for p in parts if p["contribution"] is not None]
    fetched = [idx.get("fetched_at") for idx in indices.values() if idx.get("fetched_at")]
    return {
        "schema": _LIQUIDITY_COMPOSITE_SCHEMA,
        "label": label,
        "score": score,
        "state": _composite_state(score),
        "coverage": round(covered / total_w * 100.0, 1),
        # 主要驱动：贡献绝对值最大的两个成员
        "drivers": [p["name"] for p in sorted(contribs, key=lambda p: -abs(p["contribution"]))[:2]],
        "parts": parts,
        "desc": desc,
        "stale": any(bool(idx.get("stale")) for idx in indices.values()),
        "fetched_at": min(fetched) if fetched else None,
    }


def _liquidity_composite_hist(indices: dict, schema: dict[str, float], limit: int = 120) -> list[dict]:
    """逐日回放总分：透视各指数点时分位历史，同一天按已覆盖权重归一合成。"""
    per_name: dict[str, dict[str, float]] = {}
    for idx in indices.values():
        if not isinstance(idx, dict) or idx.get("label") not in schema:
            continue
        invert = idx.get("favorable", "high") == "low"
        per_name[idx["label"]] = {p["date"]: (100.0 - p["v"] if invert else p["v"])
                                  for p in (idx.get("hist") or [])
                                  if isinstance(p.get("v"), (int, float))}
    if not per_name:
        return []
    dates = sorted(set().union(*(set(m) for m in per_name.values())))
    out = []
    for date in dates[-limit:]:
        num, covered = 50.0, 0.0
        for name, weight in schema.items():
            value = per_name.get(name, {}).get(date)
            if value is None:
                continue
            num += (value - 50.0) / 100.0 * weight
            covered += weight
        if covered < 0.5 * sum(schema.values()):
            continue
        out.append({"date": date, "v": round(num / sum(schema.values()) * 100.0, 1)})
    return out


def _cn_liquidity_composite_full(cn_indices: dict) -> dict | None:
    comp = _liquidity_composite(
        cn_indices, _CN_LIQUIDITY_COMPOSITE, "国内资金面",
        "货币条件锚定方向、边际资金决定节奏：银行间资金价格与政策利率定基调，"
        "杠杆温度与大单流向定节奏；总分 0-100，越高越宽松友好（权重为经验先验，"
        "未经收益回测，作状态仪表而非交易信号）")
    if comp:
        comp["hist"] = _liquidity_composite_hist(cn_indices, _CN_LIQUIDITY_COMPOSITE)
    return comp


def _us_liquidity_composite_full(us_indices: dict) -> dict | None:
    comp = _liquidity_composite(
        us_indices, _US_LIQUIDITY_COMPOSITE, "美国金融条件",
        "对风险资产友好的资金面状态（参照 NFCI/FCI 的加权分位合成）：短端资金、"
        "信用利差、系统流动性（准备金/ON RRP/TGA）、市场压力与曲线预警；"
        "总分 0-100，越高越宽松友好（权重为经验先验，未经收益回测）")
    if comp:
        comp["hist"] = _liquidity_composite_hist(us_indices, _US_LIQUIDITY_COMPOSITE)
    return comp


def get_liquidity(force: bool = False) -> dict:
    """资金供给指标汇总（独立页面，含历史趋势 + 美联储利率；缓存 5 分钟）。

    last-good 语义：任一源故障只影响该层，整页/各指数回退最近一次成功值（stale 标注），
    指标不再随源波动"时有时无"；冷启动从磁盘快照恢复。
    """
    return _layered_get("liquidity", lambda: _run_source_refresh(_liquidity_build, force),
                        valid=lambda v: bool(v.get("cn") or v.get("us")),
                        warm=_load_liquidity_snapshot, ttl=300, force=force)


def _merge_liquidity_group(prev: dict | None, cur: dict | None, label: str) -> dict:
    """Keep missing last-good members, but mark every carried member as stale."""
    out = dict(cur or {})
    for key, value in (prev or {}).items():
        if key not in out and isinstance(value, dict):
            out[key] = _status_copy(value, stale=True,
                                    reason=f"{label}本轮缺失，使用整页最近快照")
    return out


def _liquidity_freshness(payload: dict) -> dict:
    stale = []

    def add(label: str, item: dict | None):
        if isinstance(item, dict) and item.get("stale"):
            stale.append({"label": label, "date": item.get("date") or item.get("source_date"),
                          "fetched_at": item.get("fetched_at"),
                          "reason": item.get("fallback_reason") or "数据源回退"})

    add("国内两融", payload.get("cn"))
    for group in (payload.get("cn_indices") or {}, payload.get("us") or {}, payload.get("us_indices") or {}):
        for item in group.values():
            if item.get("kind") != "auxiliary":
                add(item.get("label", "指标"), item)
    add("目标利率分布", payload.get("fed_odds"))
    return {"stale": bool(stale), "stale_count": len(stale), "stale_sources": stale}


def _liquidity_build() -> dict:
    def build():
        # --- 国内 ---
        cn = _cn_margin_full()
        flows = _cn_index_flows()
        if flows:
            cn["index_flows"] = flows

        # --- 国外（美国）：FRED 序列（小并发池拉取，比串行快 ~3 倍；3 workers 避免触发限流） ---
        us: dict = {}

        def _fetch_fred(item):
            key, (sid, unit, label, scale, frequency) = item
            hist, status = _fred_series_cached(sid, 500)
            if not hist:
                return key, None
            hist = [(d, round(v * scale, 3)) for d, v in hist]
            latest_val = hist[-1][1]
            prev_val = hist[-2][1] if len(hist) > 1 else None
            fetched_at = status.get("fetched_at")
            return key, {
                "label": label, "unit": unit, "value": round(latest_val, 3), "date": hist[-1][0],
                "chg": round(latest_val - prev_val, 3) if prev_val is not None else None,
                "hist": [{"date": d, "v": round(v, 3)} for d, v in hist],
                "source": f"FRED:{sid}", "frequency": frequency, "source_date": hist[-1][0],
                "fetched_at": (datetime.fromtimestamp(fetched_at, BEIJING).strftime("%Y-%m-%d %H:%M")
                               if isinstance(fetched_at, (int, float)) else None),
                "stale": bool(status.get("stale")),
                **({"fallback_reason": status["fallback_reason"]} if status.get("fallback_reason") else {}),
            }

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            for key, val in pool.map(_fetch_fred, _FRED_SERIES.items()):
                if val is not None:
                    us[key] = val

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
        # 依赖 cn 数据的指数：同样分段缓存，其输入（两融/主力资金）故障时指数回退最近成功值
        for key, fn in [("leverage", lambda: _empty_to_none(_cn_leverage_index(cn))),
                        ("momentum", lambda: _empty_to_none(_cn_momentum_index(cn)))]:
            idx = _sub_cached(f"cn_idx:{key}", fn, ttl=300, valid=lambda v: bool(v), mark_stale=True)
            if idx:
                cn_indices[key] = idx

        # --- 国外（美国）综合指数 ---
        us_indices = {}
        for key, fn in [("credit_stress", _us_credit_stress_index),
                        ("curve", _us_curve_index),
                        ("funding_stress", _us_funding_stress_index),
                        ("system_liquidity", _us_qt_index),
                        ("risk_appetite", _us_risk_appetite_index)]:
            idx = _sub_cached(f"us_idx:{key}", lambda fn=fn: _empty_to_none(fn(us)),
                              ttl=300, valid=lambda v: bool(v), mark_stale=True)
            if idx:
                us_indices[key] = idx

        # --- 资金面综合得分（两张卡片；指数层缺失成员按权重归一） ---
        cn_composite = _cn_liquidity_composite_full(cn_indices)
        us_composite = _us_liquidity_composite_full(us_indices)

        # --- 美联储目标利率阈值概率（Kalshi 市场，失败时回退最近成功值） ---
        fed_odds = _fed_odds_with_fallback()

        assembled_at = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
        return {
            "cn": cn,
            "cn_indices": cn_indices,
            "cn_composite": cn_composite,
            "us": us,
            "us_indices": us_indices,
            "us_composite": us_composite,
            "fed_odds": fed_odds,
            "updated": assembled_at,
            "assembled_at": assembled_at,
        }
    val = build()
    if val.get("cn") or val.get("us"):
        # 分段合并 last-good：本轮挂掉的段回退最近成功值，不再整段消失；
        # 合并结果同时作为新的 last-good 落盘
        prev = _last_good("liquidity")
        if isinstance(prev, dict):
            for field in ("cn_indices", "us", "us_indices"):
                val[field] = _merge_liquidity_group(prev.get(field), val.get(field), field)
            if val.get("cn") and prev.get("cn"):
                val["cn"]["index_flows"] = _merge_liquidity_group(
                    prev["cn"].get("index_flows"), val["cn"].get("index_flows"), "index_flows")
            if not val.get("cn") and prev.get("cn"):
                val["cn"] = _status_copy(prev["cn"], stale=True,
                                         reason="国内资金源本轮缺失，使用整页最近快照")
            if not val.get("fed_odds") and prev.get("fed_odds"):
                val["fed_odds"] = _status_copy(prev["fed_odds"], stale=True,
                                                reason="目标利率市场本轮缺失，使用整页最近快照")
        # 综合得分随指数层一起回退：本轮缺失时沿用最近成功值（快照已随指数落盘）
        if not val.get("cn_composite"):
            prev_comp = (prev or {}).get("cn_composite") if isinstance(prev, dict) else None
            if prev_comp:
                val["cn_composite"] = _status_copy(
                    prev_comp, stale=True, reason="国内综合得分本轮缺失，使用最近快照")
        if not val.get("us_composite"):
            prev_comp = (prev or {}).get("us_composite") if isinstance(prev, dict) else None
            if prev_comp:
                val["us_composite"] = _status_copy(
                    prev_comp, stale=True, reason="美国综合得分本轮缺失，使用最近快照")
        freshness = _liquidity_freshness(val)
        val["freshness"] = freshness
        val["stale"] = freshness["stale"]
        stale_times = [x.get("fetched_at") for x in freshness["stale_sources"] if x.get("fetched_at")]
        if stale_times:
            val["stale_since"] = min(stale_times)
        else:
            val.pop("stale_since", None)
        _save_json(_LIQUIDITY_SNAPSHOT, val)
    return val


# ---------------------------------------------------------------------------
# 宏观面 —— 国内重要宏观经济指标（GDP/CPI/PPI/PMI/M2/工业增加值/进出口等）
# ---------------------------------------------------------------------------

_MACRO_TTL = 1800  # 聚合层 30 分钟检查；月/季底层源另有 12 小时缓存
_MACRO_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "macro_snapshot.json")
_MACRO_CLIMATE_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "macro_climate_hist.json")
_MACRO_EPS_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "macro_eps_consensus.json")


def _load_macro_snapshot():
    d = _load_json(_MACRO_SNAPSHOT)
    return d if isinstance(d, dict) and (d.get("cn") or d.get("groups")) else None


def _load_climate_hist() -> list:
    d = _load_json(_MACRO_CLIMATE_SNAPSHOT)
    return d if isinstance(d, list) else []


def _save_climate_hist(modules: list) -> None:
    """追加一条景气快照（同数据月份去重覆盖），保留最近 24 条。"""
    cur = next((m for m in modules or [] if m.get("name") == _CLIMATE_MODULE_NAME), None)
    if not cur or cur.get("score") is None:
        return
    subs = cur.get("submodules") or []
    cur_date = ""
    for s in subs:
        for u in s.get("used") or []:
            d = u.get("date") or ""
            if d > cur_date:
                cur_date = d
    hist = [r for r in _load_climate_hist()
            if r.get("schema") == _CLIMATE_SCHEMA and r.get("date") != cur_date]
    hist.append({"schema": _CLIMATE_SCHEMA, "date": cur_date, "score": cur["score"],
                 "subs": {s["name"]: s.get("score") for s in subs}})
    _save_json(_MACRO_CLIMATE_SNAPSHOT, hist[-24:])


def _append_observation_history(card: dict, previous: dict | None, limit: int = 120) -> dict:
    """给只发布当前值的卡片续接本地观测历史；同观察期覆盖、不重复。"""
    points = {str(p.get("date", "")): p.get("v") for p in (previous or {}).get("hist", [])
              if isinstance(p, dict) and isinstance(p.get("v"), (int, float))}
    if card.get("date") and isinstance(card.get("value"), (int, float)):
        points[str(card["date"])] = card["value"]
    hist = [{"date": d, "v": v} for d, v in sorted(points.items())][-limit:]
    card["hist"] = hist
    card["prev"] = hist[-2]["v"] if len(hist) > 1 else None
    return card


def _market_breadth_card(previous: dict | None = None) -> dict | None:
    """全A上涨家数占比；历史由每日宏观快照按交易日续接。"""
    s = _sentiment()
    total = sum(s.get(k, 0) for k in ("up", "down", "flat"))
    if not total:
        return None
    card = {
        "label": "全A上涨家数占比", "value": round(s["up"] / total * 100.0, 2),
        "forecast": None, "prev": None, "date": str(s.get("date", ""))[:10], "hist": [],
        "unit": "%", "source": "乐咕乐股·全市场涨跌家数",
        "source_url": "https://legulegu.com/stockdata/market-activity",
        "scope": "全A股（上涨/下跌/平盘家数）",
        "sample_size": total,
    }
    return _append_observation_history(card, previous)


def _eps_consensus(rows: list[dict]) -> dict[str, dict]:
    """东财盈利预测行 → {股票: {year, eps}}，只取最近一个预测年度。"""
    out = {}
    for row in rows:
        candidates = []
        for i in range(1, 5):
            try:
                if str(row.get(f"YEAR_MARK{i}", "")).upper() == "E":
                    candidates.append((int(row[f"YEAR{i}"]), float(row[f"EPS{i}"])))
            except (TypeError, ValueError, KeyError):
                continue
        code = str(row.get("SECURITY_CODE", ""))
        if code and candidates:
            year, eps = min(candidates)
            out[code] = {"year": year, "eps": eps}
    return out


def _eps_revision_card(current: dict[str, dict], snapshot: dict | None, today: str) -> tuple[dict | None, dict]:
    """共同样本、同目标年度的EPS净上调扩散；首个交易日只建立基准。"""
    previous = (snapshot or {}).get("values") or {}
    hist = list((snapshot or {}).get("hist") or [])
    card = None
    if previous and (snapshot or {}).get("date") != today:
        common = [code for code in current if code in previous
                  and current[code].get("year") == previous[code].get("year")]
        if len(common) >= 50:
            up = sum(current[c]["eps"] > previous[c]["eps"] + 1e-6 for c in common)
            down = sum(current[c]["eps"] < previous[c]["eps"] - 1e-6 for c in common)
            value = round((up - down) / len(common) * 100.0, 2)
            hist = [p for p in hist if p.get("date") != today]
            hist.append({"date": today, "v": value})
            hist = hist[-120:]
    if hist:
        card = {
            "label": "一致预期EPS净上调扩散", "value": hist[-1]["v"], "forecast": None,
            "prev": hist[-2]["v"] if len(hist) > 1 else None,
            "date": hist[-1]["date"], "hist": hist, "unit": "%",
            "source": "东方财富·盈利预测(高覆盖500股)",
            "source_url": "https://data.eastmoney.com/report/profitforecast.jshtml",
            "scope": "东财盈利预测覆盖度最高的500只A股；共同样本且目标年度相同",
            "sample_size": len(current),
        }
    return card, {"date": today, "values": current, "hist": hist}


def _eps_revision_breadth() -> dict | None:
    rows = astock.eastmoney_datacenter(
        "RPT_WEB_RESPREDICT", page_size=500,
        sort_columns="RATING_ORG_NUM", sort_types="-1")
    current = _eps_consensus(rows)
    if not current:
        return None
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    card, snapshot = _eps_revision_card(current, _load_json(_MACRO_EPS_SNAPSHOT), today)
    _save_json(_MACRO_EPS_SNAPSHOT, snapshot)
    return card


def _latest_report_period() -> str:
    """最近已完整披露的业绩报告期（YYYYMMDD）。"""
    now = datetime.now(BEIJING)
    y, m = now.year, now.month
    if m >= 11:
        return f"{y}0930"
    if m >= 9:
        return f"{y}0630"
    if m >= 5:
        return f"{y}0331"
    return f"{y - 1}0930"


def _profit_breadth_card(previous: dict | None = None) -> dict | None:
    """全A盈利广度：报告期内净利润同比为正的公司占比。季度更新。"""
    period = _latest_report_period()
    rows = astock.yjbb_snapshot(period)
    if not rows:
        return None
    total = len(rows)
    if total < 100:
        return None
    positive = sum(1 for v in rows.values() if (v.get("profit_yoy") or 0) > 0)
    value = round(positive / total * 100.0, 2)
    card = {
        "label": "全A盈利广度（净利同比正）", "value": value,
        "forecast": None, "prev": None,
        "date": f"{period[:4]}-{period[4:6]}-{period[6:]}", "hist": [],
        "unit": "%", "source": "东方财富·业绩报表",
        "source_url": "https://data.eastmoney.com/bbsj/yjbb.html",
        "scope": f"全A股 {period[:4]}年{period[4:6]}月报告期",
        "sample_size": total,
    }
    return _append_observation_history(card, previous)


def _index_valuation_cards() -> dict:
    """沪深300 PE-TTM + 股权风险溢价（一次拉取，两项指标）。日频。"""
    os.environ["TQDM_DISABLE"] = "1"
    try:
        ak = astock._akshare()
        df = ak.stock_index_pe_lg(symbol="沪深300")
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    pe_pts = []
    for d, v in zip(df["日期"], df["滚动市盈率"]):
        try:
            pe = float(v)
        except (TypeError, ValueError):
            continue
        if pe != pe or pe <= 0:
            continue
        pe_pts.append((str(d)[:10], round(pe, 2)))
    if len(pe_pts) < 250:
        return {}
    pe_recent = pe_pts[-1250:]
    pe_hist = [{"date": d, "v": v} for d, v in pe_recent[-250:]]
    out = {
        "index_pe_ttm": {
            "label": "沪深300 PE-TTM", "value": pe_recent[-1][1],
            "forecast": None,
            "prev": pe_recent[-2][1] if len(pe_recent) > 1 else None,
            "date": pe_recent[-1][0], "hist": pe_hist,
            "unit": "", "source": "乐咕乐股·指数市盈率",
            "source_url": "https://legulegu.com/stockdata/sz50-ttm-lyr",
            "scope": "沪深300 滚动市盈率(TTM)",
        }
    }
    # ERP = 盈利收益率(1/PE×100) − 10Y国债收益率
    try:
        start = (datetime.now(BEIJING) - timedelta(days=365 * 5)).strftime("%Y%m%d")
        bdf = ak.bond_zh_us_rate(start_date=start)
        bond_pts: dict[str, float] = {}
        for d, v in zip(bdf["日期"], bdf["中国国债收益率10年"]):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv == fv:
                bond_pts[str(d)[:10]] = fv
    except Exception:
        bond_pts = {}
    if bond_pts:
        erp_pts = [(d, round(100.0 / pe - bond_pts[d], 2))
                   for d, pe in pe_recent if d in bond_pts and pe > 0 and bond_pts[d] > 0]
        if len(erp_pts) >= 250:
            erp_hist = [{"date": d, "v": v} for d, v in erp_pts[-250:]]
            out["equity_risk_premium"] = {
                "label": "沪深300 股权风险溢价", "value": erp_pts[-1][1],
                "forecast": None,
                "prev": erp_pts[-2][1] if len(erp_pts) > 1 else None,
                "date": erp_pts[-1][0], "hist": erp_hist,
                "unit": "%", "source": "乐咕乐股PE + 东财10Y国债",
                "source_url": "https://data.eastmoney.com/cjsj/zmgzsyl.html",
                "scope": "沪深300盈利收益率(1/PE) − 10年期国债收益率",
            }
    return out


def _new_high_breadth_card() -> dict | None:
    """全A新高占比：20日新高 / (20日新高 + 20日新低)。日频。"""
    try:
        ak = astock._akshare()
        df = ak.stock_a_high_low_statistics(symbol="all")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    pts = []
    for _, row in df.iterrows():
        d = str(row.get("date", ""))[:10]
        try:
            hi = float(row.get("high20"))
            lo = float(row.get("low20"))
        except (TypeError, ValueError):
            continue
        if hi != hi or lo != lo:
            continue
        denom = hi + lo
        if denom > 0:
            pts.append((d, round(hi / denom * 100.0, 2)))
    if len(pts) < 20:
        return None
    hist = [{"date": d, "v": v} for d, v in pts[-250:]]
    return {
        "label": "全A新高占比(20日)", "value": pts[-1][1],
        "forecast": None,
        "prev": pts[-2][1] if len(pts) > 1 else None,
        "date": pts[-1][0], "hist": hist,
        "unit": "%", "source": "乐咕乐股·新高新低统计",
        "source_url": "https://legulegu.com/stockdata/highlowstatistics",
        "scope": "20日新高 / (20日新高 + 20日新低)",
    }


_MACRO_DAILY = {"dr007_policy_spread", "ncd_aaa_spread", "credit_spread_aaa",
                "mkt_margin_balance", "mkt_margin_netbuy",
                "market_breadth", "eps_revision_breadth", "usdcnh",
                "index_pe_ttm", "equity_risk_premium", "new_high_breadth"}
_MACRO_QUARTERLY = {"gdp", "bank_survey", "profit_breadth"}
_MACRO_PROXY = {"copper_oil_ratio", "resale_house_breadth", "usdcnh", "policy_execution"}
_MACRO_SOURCE_URLS = {
    **{k: "https://www.pbc.gov.cn/" for k in (
        "m1", "m2", "social_financing", "social_financing_stock", "private_credit_growth",
        "household_ml_loan", "corp_ml_loan", "bill_financing", "fiscal_deposit", "nonbank_deposit")},
    **{k: "https://data.stats.gov.cn/" for k in (
        "m1_m2_spread", "price_spread", "industrial_momentum", "services_momentum",
        "order_inventory_spread")},
    **{k: "https://data.eastmoney.com/" for k in (
        "mkt_margin_balance", "mkt_margin_netbuy", "mkt_main_inflow")},
    "policy_execution": "https://www.mof.gov.cn/",
    "profit_breadth": "https://data.eastmoney.com/bbsj/yjbb.html",
    "index_pe_ttm": "https://legulegu.com/stockdata/sz50-ttm-lyr",
    "equity_risk_premium": "https://data.eastmoney.com/cjsj/zmgzsyl.html",
    "new_high_breadth": "https://legulegu.com/stockdata/highlowstatistics",
}


def _indicator_is_stale(date_str: str, frequency: str, now: datetime) -> bool:
    try:
        if frequency == "daily" and len(date_str) >= 10:
            observed = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=BEIJING)
            return (now - observed).days > 7
        observed = datetime.strptime(date_str[:7], "%Y-%m").replace(tzinfo=BEIJING)
        lag = (now.year - observed.year) * 12 + now.month - observed.month
        return lag > (4 if frequency == "quarterly" else 2)
    except (ValueError, TypeError):
        return True


def _annotate_macro_indicators(indicators: dict, fetched_keys: set[str], fetched_at: str,
                               fallback_fetched_at: str | None = None) -> None:
    """补齐每张卡的观察期、抓取时间、状态、频率、质量、范围和来源链接。"""
    now = datetime.now(BEIJING)
    for key, card in indicators.items():
        if not isinstance(card, dict):
            continue
        old_meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
        frequency = "daily" if key in _MACRO_DAILY else "quarterly" if key in _MACRO_QUARTERLY else "monthly"
        source = str(card.get("source", ""))
        quality = "proxy" if key in _MACRO_PROXY or "代理" in source or "近似" in source else (
            "derived" if source.startswith("派生") else "direct")
        if key not in fetched_keys:
            status = "fallback"
        else:
            status = "stale" if _indicator_is_stale(str(card.get("date", "")), frequency, now) else "fresh"
        source_url = (_MACRO_SOURCE_URLS.get(key) or card.get("source_url") or card.get("page_url") or card.get("xlsx_url")
                      or card.get("pdf_url") or old_meta.get("source_url"))
        if not source_url:
            source_url = next((url for marker, url in (
                ("统计局", "https://data.stats.gov.cn/"),
                ("人民银行", "https://www.pbc.gov.cn/"),
                ("财政部", "https://www.mof.gov.cn/"),
                ("东方财富", "https://data.eastmoney.com/"),
                ("FRED", "https://fred.stlouisfed.org/"),
                ("CPB", "https://www.cpb.nl/en/world-trade-monitor"),
                ("S&P Global", "https://www.spglobal.com/marketintelligence/en/mi/products/pmi.html"),
                ("外汇交易中心", "https://www.chinamoney.com.cn/"),
            ) if marker in source), None)
        card["meta"] = {
            "observation_period": card.get("date"),
            "release_at": card.get("release_at"),
            "fetched_at": fetched_at if key in fetched_keys else (old_meta.get("fetched_at") or fallback_fetched_at),
            "status": status,
            "frequency": frequency,
            "quality": quality,
            "scope": card.get("scope") or card.get("label"),
            "source_url": source_url,
            "owner_module": _MACRO_INDICATOR_OWNER.get(key),
            "derived_from": _MACRO_DERIVED_FROM.get(key, []),
        }


# akshare 的 investing 接口统一返回（商品/日期/今值/预测值/前值）
def _inv_hist(fn, label: str, limit: int = 60) -> dict | None:
    """拉一条 investing 格式宏观指标，解析为前端友好的卡片数据。"""
    try:
        ak = astock._akshare()
        df = fn().tail(limit)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    # 过滤今值为 NaN 的行（未发布月份）
    df = df.dropna(subset=["今值"])
    if df.empty:
        return None
    latest = df.iloc[-1]
    val = float(latest["今值"])
    forecast = latest.get("预测值")
    prev = latest.get("前值")
    hist = [{"date": str(r["日期"])[:10], "v": round(float(r["今值"]), 3)}
            for _, r in df.iterrows() if r.get("今值") is not None]
    return {
        "label": label,
        "value": round(val, 2),
        "forecast": round(float(forecast), 2) if forecast is not None and str(forecast) != "nan" else None,
        "prev": round(float(prev), 2) if prev is not None and str(prev) != "nan" else None,
        "date": str(latest["日期"])[:10],
        "hist": hist,
    }


# ---------------------------------------------------------------------------
# 东财 datacenter 官方宏观回退源（替代已停更的 investing.com 系列）
# ---------------------------------------------------------------------------

def _em_series(report: str, label: str, value_key: str, limit: int = 60,
               date_fmt: str = "month") -> dict | None:
    """从东财 datacenter 宏观报表拉一条时间序列，对齐 investing 卡片结构。

    report:     RPT_ECONOMY_* 报表名
    value_key:  取值字段（同比/指数列）
    date_fmt:   'month' 用 REPORT_DATE 的 YYYY-MM；'quarter' 取季末月
    """
    try:
        rows = astock.eastmoney_datacenter(
            report, page_size=limit, sort_columns="REPORT_DATE", sort_types="-1")
    except Exception:
        return None
    if not rows:
        return None
    pts = []
    for r in rows:
        rd = str(r.get("REPORT_DATE") or "")[:10]
        if not rd:
            continue
        d = rd[:7]  # YYYY-MM
        try:
            v = float(r.get(value_key))
        except (TypeError, ValueError):
            continue
        pts.append((d, v))
    if not pts:
        return None
    # 升序去重
    pts = sorted({d: v for d, v in pts}.items())
    hist = [{"date": d, "v": round(v, 3)} for d, v in pts[-limit:]]
    last = hist[-1]["v"]
    prev = hist[-2]["v"] if len(hist) > 1 else None
    return {
        "label": label,
        "value": round(last, 2),
        "forecast": None,
        "prev": round(prev, 2) if prev is not None else None,
        "date": hist[-1]["date"],
        "hist": hist,
        "source": "东方财富·官方宏观",
    }


def _em_trade_balance(limit: int = 60) -> dict | None:
    """贸易差额 = 出口 - 进口（亿美元）。东财海关 EXIT_BASE/IMPORT_BASE 单位为千美元，除以 1e6 转亿美元。"""
    try:
        rows = astock.eastmoney_datacenter(
            "RPT_ECONOMY_CUSTOMS", page_size=limit,
            sort_columns="REPORT_DATE", sort_types="-1")
    except Exception:
        return None
    if not rows:
        return None
    pts = []
    for r in rows:
        rd = str(r.get("REPORT_DATE") or "")[:10]
        if not rd:
            continue
        try:
            ex = float(r.get("EXIT_BASE"))
            im = float(r.get("IMPORT_BASE"))
        except (TypeError, ValueError):
            continue
        pts.append((rd[:7], (ex - im) / 1e6))  # 千美元→亿美元
    if not pts:
        return None
    pts = sorted({d: v for d, v in pts}.items())
    hist = [{"date": d, "v": round(v, 1)} for d, v in pts[-limit:]]
    last = hist[-1]["v"]
    prev = hist[-2]["v"] if len(hist) > 1 else None
    return {
        "label": "贸易差额", "value": round(last, 1),
        "forecast": None, "prev": round(prev, 1) if prev is not None else None,
        "date": hist[-1]["date"], "hist": hist,
        "unit": "亿美元", "source": "东方财富·海关",
    }


# 货币供应/社融等 NBS 格式接口，单独处理
def _m2_yoy() -> dict | None:
    """M2 同比（百分比），用 macro_china_money_supply 的同比增长列。"""
    try:
        ak = astock._akshare()
        df = ak.macro_china_money_supply()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    # 该接口最新在最前，取前 36 个月
    df = df.head(36).copy()
    df = df.sort_values("月份")
    latest = df.iloc[-1]
    month = str(latest["月份"])
    # "2026年06月份" → "2026-06"
    try:
        m = month.replace("年", "-").replace("月份", "").replace("月", "")
        date_str = m
    except Exception:
        date_str = month
    val = float(latest["货币和准货币(M2)-同比增长"])
    hist = [{"date": str(r["月份"]).replace("年", "-").replace("月份", "").replace("月", ""),
             "v": round(float(r["货币和准货币(M2)-同比增长"]), 2)}
            for _, r in df.iterrows()]
    return {
        "label": "M2 同比",
        "value": round(val, 2),
        "forecast": None,
        "prev": round(float(df.iloc[-2]["货币和准货币(M2)-同比增长"]), 2) if len(df) > 1 else None,
        "date": date_str,
        "hist": hist,
        "unit": "%",
        "source": "人民银行·货币供应量",
    }


def _m1_yoy() -> dict | None:
    """M1 同比（百分比）。"""
    try:
        ak = astock._akshare()
        df = ak.macro_china_money_supply()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.head(36).copy().sort_values("月份")
    latest = df.iloc[-1]
    month = str(latest["月份"])
    try:
        m = month.replace("年", "-").replace("月份", "").replace("月", "")
        date_str = m
    except Exception:
        date_str = month
    val = float(latest["货币(M1)-同比增长"])
    hist = [{"date": str(r["月份"]).replace("年", "-").replace("月份", "").replace("月", ""),
             "v": round(float(r["货币(M1)-同比增长"]), 2)}
            for _, r in df.iterrows()]
    return {
        "label": "M1 同比",
        "value": round(val, 2),
        "forecast": None,
        "prev": round(float(df.iloc[-2]["货币(M1)-同比增长"]), 2) if len(df) > 1 else None,
        "date": date_str,
        "hist": hist,
        "unit": "%",
        "source": "人民银行·货币供应量",
    }


def _social_financing() -> dict | None:
    """社会融资规模增量（亿元/月）。"""
    try:
        ak = astock._akshare()
        df = ak.macro_china_shrzgm()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.tail(24).copy().sort_values("月份")
    latest = df.iloc[-1]
    month = str(latest["月份"])
    # "201501" → "2015-01"
    try:
        date_str = f"{month[:4]}-{month[4:]}" if len(month) == 6 else month
    except Exception:
        date_str = month
    val = float(latest["社会融资规模增量"])
    hist = [{"date": f"{str(r['月份'])[:4]}-{str(r['月份'])[4:]}" if len(str(r["月份"])) == 6 else str(r["月份"]),
             "v": round(float(r["社会融资规模增量"]), 1)}
            for _, r in df.iterrows()]
    return {
        "label": "社融增量",
        "value": round(val, 0),
        "forecast": None,
        "prev": round(float(df.iloc[-2]["社会融资规模增量"]), 0) if len(df) > 1 else None,
        "date": date_str,
        "hist": hist,
        "unit": "亿元",
        "source": "人民银行·社会融资规模增量",
    }


# ---------------------------------------------------------------------------
# 宏观面 8 模块得分体系
# 八模块按五类因果层组织；每模块 = 指标方向调整后历史分位加权，0-100。
# 方向 direction: "up"=指标上升利多 / "down"=上升利空（分位取反）。
# ---------------------------------------------------------------------------

# 国内增长与景气模块：4 子模块结构（research/宏观-景气模块_V1.0.md §1-2）。
# 子模块权重为 0-100 百分点；指标键、方向、子模块内相对权重。
# spec 末位 "exp"=按自身历史分位评分（经营预期，§8）；score="cli"=CLI 方向评分。
_CLIMATE_MODULE_NAME = "国内增长与景气"
_CLIMATE_SCHEMA = 2
_MACRO_CLIMATE: list[tuple[str, float, list[tuple[str, str, float, str]]]] = [
    ("国内官方", 45.0, [
        ("pmi_headline", "up", 8.0, "level"),
        ("pmi_new_orders", "up", 0.0, "level"),  # 仅展示；已进入订单库存差
        ("pmi_production", "up", 6.0, "level"),
        ("pmi_new_export_orders", "up", 5.0, "level"),
        ("pmi_expectation", "up", 5.0, "exp"),
        ("non_man_pmi", "up", 9.0, "level"),
    ]),
    ("国内市场化", 20.0, [
        ("cx_pmi", "up", 12.0, "level"),
        ("caixin_services_pmi", "up", 8.0, "level"),
    ]),
    ("国内实际活动", 15.0, [
        ("industrial_momentum", "up", 7.0, "level"),
        ("services_momentum", "up", 4.0, "level"),
        ("order_inventory_spread", "up", 4.0, "level"),
    ]),
    ("增长结果确认", 20.0, [
        ("gdp", "up", 7.0, "level"),
        ("industrial_revenue", "up", 7.0, "level"),
        ("fai_equipment", "up", 6.0, "level"),
    ]),
]

_MACRO_MODULES: list[tuple[str, str, str, list[tuple[str, str, float]]]] = [
    ("全球外部", "Globe", "外需 / 汇率 / 全球增长代理", [
        ("world_trade_yoy_3mma", "up", 1.5),
        ("copper_oil_ratio", "up", 1.0),
        ("usdcnh", "down", 1.2),
        ("exports", "up", 1.3),
        ("imports", "up", 0.8),
    ]),
    (_CLIMATE_MODULE_NAME, "Factory", "领先景气 / 实际活动 / 增长结果确认", _MACRO_CLIMATE),
    ("价格与工业利润", "Gauge", "物价 / 利润率 / 工业利润 / 库存周期", [
        ("cpi", "up", 1.0),
        ("core_cpi", "up", 1.2),
        ("ppi", "up", 1.3),
        ("industrial_profit", "up", 1.5),
        ("industrial_inventory", "up", 0.8),
        ("price_spread", "down", 1.0),
    ]),
    ("信用周期", "Coins", "信用活化 / 私人信用 / 分部门贷款 / 贷款需求", [
        ("m1", "up", 0.0),  # 仅展示；已进入M1-M2
        ("m2", "up", 0.0),
        ("m1_m2_spread", "up", 1.3),
        ("social_financing_stock", "up", 0.0),  # 仅展示；私人信用由其分解得到
        ("private_credit_growth", "up", 1.8),
        ("household_ml_loan", "up", 1.2),
        ("corp_ml_loan", "up", 1.2),
        ("bill_financing", "down", 0.8),
        ("bank_survey", "up", 1.0),
    ]),
    ("货币与金融条件", "Activity", "资金价格 / 存单 / 信用利差 / 非银流动性", [
        ("nonbank_deposit", "up", 0.7),
        ("fiscal_deposit", "down", 0.6),
        ("dr007_policy_spread", "down", 1.1),
        ("ncd_aaa_spread", "down", 1.1),
        ("credit_spread_aaa", "down", 1.1),
    ]),
    ("财政地产", "Landmark", "财政收支 / 专项债 / 地产销售资金 / 政策执行", [
        ("fiscal_expenditure", "up", 0.0),  # 仅展示；已包含于两本账广义支出
        ("fiscal_revenue_expenditure", "up", 1.3),
        ("special_bond_issuance", "up", 1.2),
        ("property_sales_area", "up", 1.3),
        ("property_funds", "up", 1.0),
        ("property_loans", "up", 0.9),
        ("resale_house_breadth", "up", 0.8),
        ("policy_execution", "up", 0.0),  # 仅展示；由本模块分项合成
    ]),
    ("盈利预期", "TrendingUp", "EPS修正 / 盈利广度 / 估值分位 / 股权风险溢价", [
        ("eps_revision_breadth", "up", 1.0),
        ("profit_breadth", "up", 0.8),
        ("index_pe_ttm", "down", 0.8),
        ("equity_risk_premium", "up", 0.8),
    ]),
    ("市场确认", "Zap", "市场宽度与趋势确认 · 不将杠杆资金流机械视为利多", [
        ("market_breadth", "up", 1.0),
        ("new_high_breadth", "up", 0.8),
    ]),
]

_MACRO_CLUSTERS = [
    {"name": "外部约束", "desc": "全球增长、外需与汇率约束", "modules": ["全球外部"]},
    {"name": "国内周期", "desc": "领先景气 → 实际增长 → 价格与工业利润", "modules": [_CLIMATE_MODULE_NAME, "价格与工业利润"]},
    {"name": "政策与融资", "desc": "信用传导、金融条件与财政地产", "modules": ["信用周期", "货币与金融条件", "财政地产"]},
    {"name": "盈利传导", "desc": "宏观变化进入盈利预期", "modules": ["盈利预期"]},
    {"name": "市场验证", "desc": "市场价格只做确认，不反推宏观基本面", "modules": ["市场确认"]},
]


def _macro_indicator_owners() -> dict[str, str]:
    owners: dict[str, str] = {}
    for name, _, _, specs in _MACRO_MODULES:
        keys = ([key for _, _, sub_specs in specs for key, _, weight, _ in sub_specs if weight > 0]
                if name == _CLIMATE_MODULE_NAME else [key for key, _, weight in specs if weight > 0])
        for key in keys:
            if key in owners:
                raise ValueError(f"宏观指标重复计分: {key} -> {owners[key]} / {name}")
            owners[key] = name
    return owners


_MACRO_INDICATOR_OWNER = _macro_indicator_owners()
_MACRO_DERIVED_FROM = {
    "world_trade_yoy_3mma": ["world_trade_volume"],
    "m1_m2_spread": ["m1", "m2"],
    "price_spread": ["pmi_input_price", "pmi_output_price"],
    "private_credit_growth": ["social_financing_stock", "government_bond_stock"],
    "household_ml_loan": ["credit_by_sector"],
    "corp_ml_loan": ["credit_by_sector"],
    "bill_financing": ["credit_by_sector"],
    "fiscal_deposit": ["credit_by_sector"],
    "nonbank_deposit": ["credit_by_sector"],
    "industrial_momentum": ["industrial"],
    "services_momentum": ["services_production"],
    "order_inventory_spread": ["pmi_new_orders", "pmi_finished_inventory"],
    "policy_execution": ["fiscal_revenue_expenditure", "special_bond_issuance", "corp_ml_loan"],
    "mkt_margin_balance": ["margin_balance"],
}


# ---------------------------------------------------------------------------
# 国内增长与景气评分引擎（research/宏观-景气模块_V1.0.md）
# ---------------------------------------------------------------------------

def _freshness_factor(date_str: str, as_of: str | None = None) -> float:
    """数据滞后衰减（§11）：当月1.0 / 1月0.9 / 2月0.75 / ≥3月0.5。"""
    if not date_str:
        return 0.5
    try:
        now = datetime.strptime(as_of[:7], "%Y-%m") if as_of else datetime.now(BEIJING)
        parts = date_str.split("-")
        y, m = int(parts[0]), int(parts[1])
        lag = (now.year - y) * 12 + (now.month - m)
    except (ValueError, IndexError):
        return 0.5
    if lag <= 0:
        return 1.0
    if lag == 1:
        return 0.9
    if lag == 2:
        return 0.75
    return 0.5


def _hist_vals(ind_card: dict) -> list[float]:
    return [p["v"] for p in (ind_card.get("hist") or []) if isinstance(p.get("v"), (int, float))]


def _level_pct(hist: list[float]) -> float:
    """历史分位（§4 Level，hist≥4 用分位，否则中性 50）。"""
    if len(hist) < 4:
        return 50.0
    return _pct_rank(hist, hist[-1])


def _momentum_pct(hist: list[float]) -> float:
    """动量分位（§4 Momentum）：原始动量 = 0.6×ΔX_t + 0.4×ΔMA3_t，对全历史取分位。"""
    n = len(hist)
    if n < 8:
        return 50.0
    ma3 = [sum(hist[i - 2:i + 1]) / 3.0 for i in range(2, n)]
    moms = []
    for i in range(3, n):
        dx = hist[i] - hist[i - 1]
        dma3 = ma3[i - 2] - ma3[i - 3]
        moms.append(0.6 * dx + 0.4 * dma3)
    if not moms:
        return 50.0
    return _pct_rank(moms, moms[-1])


def _surprise_pct(ind_card: dict, hist: list[float]) -> float | None:
    """预期差分位（§4 Surprise）：actual−forecast，锚定 hist 波动尺度。无预期返回 None。"""
    fc = ind_card.get("forecast")
    val = ind_card.get("value")
    if fc is None or val is None or len(hist) < 4:
        return None
    mean = sum(hist) / len(hist)
    std = max((sum((v - mean) ** 2 for v in hist) / len(hist)) ** 0.5, 1e-9)
    z = (val - fc) / std
    return round(max(0.0, min(100.0, 50.0 + 50.0 * (1 if z > 0 else -1) * min(abs(z) / 3.0, 1.0) ** 0.7)), 1)


def _climate_ind_score(key: str, direction: str, base_w: float, score_type: str,
                       ind: dict, as_of: str | None = None) -> dict | None:
    """单指标景气评分。返回 {key,direction,weight,pct,freshness} 或 None（缺数据跳过）。"""
    card = ind.get(key)
    if not card or card.get("value") is None:
        return None
    hist = _hist_vals(card)
    if score_type == "exp":
        # 生产经营预期：仅按自身历史分位（§8），不叠加动量/预期差。
        pct = _level_pct(hist)
    elif score_type == "cli":
        # OECD CLI：0.3×Level + 0.5×Momentum + 0.2×Acceleration（§8）。
        n = len(hist)
        if n >= 6:
            mom_series = [hist[i] - hist[i - 1] for i in range(1, n)]
            acc_series = [mom_series[i] - mom_series[i - 1] for i in range(1, len(mom_series))]
            acc_pct = _pct_rank(acc_series, acc_series[-1]) if acc_series else 50.0
            pct = 0.3 * _level_pct(hist) + 0.5 * _momentum_pct(hist) + 0.2 * acc_pct
        else:
            pct = _level_pct(hist)
    else:
        # 标准 PMI 类：0.5×Level + 0.3×Momentum + 0.2×Surprise；无预期→0.6L+0.4M（§4）。
        lvl = _level_pct(hist)
        mom = _momentum_pct(hist)
        sur = _surprise_pct(card, hist)
        pct = (0.5 * lvl + 0.3 * mom + 0.2 * sur) if sur is not None else (0.6 * lvl + 0.4 * mom)
    if direction == "down":
        pct = 100.0 - pct
    freshness = _freshness_factor(card.get("date", ""), as_of)
    return {"key": key, "direction": direction, "weight": base_w,
            "pct": round(pct, 1), "freshness": freshness, "date": card.get("date", "")}


_MIN_MODULE_COVERAGE = 0.5


def _quality_factor(card: dict) -> float:
    meta = card.get("meta") or {}
    quality = {"direct": 1.0, "derived": 0.9, "proxy": 0.7}.get(meta.get("quality"), 1.0)
    if meta.get("status") == "fallback":
        quality *= 0.6
    return quality


def _climate_state(score: float) -> str:
    if score >= 70:
        return "强扩张"
    if score >= 60:
        return "扩张"
    if score >= 50:
        return "弱扩张"
    if score >= 40:
        return "弱收缩"
    if score >= 30:
        return "收缩"
    return "深度收缩"


def _climate_module_score(ind: dict, as_of: str | None = None) -> dict | None:
    """景气总分：名义权重固定；缺失按中性50，滞后信号向50衰减，并披露覆盖/置信度。"""
    submods = []
    total = 0.0
    total_coverage = 0.0
    total_confidence = 0.0
    all_used = []
    for sub_name, sub_w, specs in _MACRO_CLIMATE:
        nominal = sum(bw for _, _, bw, _ in specs)
        num = 50.0 * nominal
        covered = 0.0
        confidence = 0.0
        used = []
        keys = []
        for key, direction, bw, st in specs:
            keys.append(key)
            if bw <= 0:
                continue
            r = _climate_ind_score(key, direction, bw, st, ind, as_of)
            if r is None:
                continue
            # 先给每项中性50，再按时效将有效信号偏离中性的部分加回来。
            num += (r["pct"] - 50.0) * bw * r["freshness"]
            covered += bw
            confidence += bw * r["freshness"] * _quality_factor(ind[key])
            used.append(r)
            all_used.append(r)
        coverage = covered / nominal if nominal else 0.0
        confidence_pct = confidence / nominal if nominal else 0.0
        sub_score = round(num / nominal, 1) if coverage >= _MIN_MODULE_COVERAGE else None
        submods.append({"name": sub_name, "weight": sub_w, "score": sub_score,
                        "coverage": round(coverage * 100.0, 1),
                        "confidence": round(confidence_pct * 100.0, 1),
                        "indicators": [k for k in keys if k in ind], "used": used})
        total += (sub_score if sub_score is not None else 50.0) * sub_w
        total_coverage += coverage * sub_w
        total_confidence += confidence_pct * sub_w
    coverage = total_coverage / 100.0
    score = round(total / 100.0, 1) if coverage >= _MIN_MODULE_COVERAGE else None
    return {"score": score, "coverage": round(coverage * 100.0, 1),
            "confidence": round(total_confidence, 1),
            "submodules": submods, "used": all_used,
            "indicators": [k for _, _, specs in _MACRO_CLIMATE for k, _, _, _ in specs if k in ind]}


def _climate_current_date(climate: dict) -> str:
    """景气当前数据月份（所有指标 date 取最大）。"""
    d = ""
    for s in climate.get("submodules") or []:
        for u in s.get("used") or []:
            if (u.get("date") or "") > d:
                d = u["date"]
    return d


def _climate_hist_mom(climate: dict) -> float | None:
    """景气 MoM：当前总分 − 最近一次数据月份不同的历史快照总分。"""
    if not climate or climate.get("score") is None:
        return None
    cur_date = _climate_current_date(climate)
    hist = [r for r in _load_climate_hist() if r.get("schema") == _CLIMATE_SCHEMA]
    base = None
    for rec in reversed(hist):
        if rec.get("date") and rec["date"] != cur_date:
            base = rec
            break
    if not base or base.get("score") is None:
        return None
    return round(climate["score"] - base["score"], 1)


def _indicators_as_of(ind: dict, as_of: str) -> dict:
    """截取到指定月份的指标卡，用于无未来数据的历史得分回放。"""
    out = {}
    for key, card in ind.items():
        if not isinstance(card, dict):
            continue
        hist = [p for p in card.get("hist") or []
                if isinstance(p, dict) and isinstance(p.get("v"), (int, float))
                and str(p.get("date", ""))[:7] <= as_of]
        if not hist:
            continue
        snap = dict(card)
        snap.update({
            "hist": hist,
            "value": hist[-1]["v"],
            "prev": hist[-2]["v"] if len(hist) > 1 else None,
            "date": hist[-1]["date"],
            # 历史预期差只有当前卡同月才可用，禁止把当前预期带回过去。
            "forecast": card.get("forecast") if str(card.get("date", ""))[:7] == as_of else None,
        })
        out[key] = snap
    return out


def _module_score_history(ind: dict, months: int = 36) -> dict[str, list[dict]]:
    """按月回放各模块得分，返回最近 ``months`` 个月。"""
    dates = sorted({str(p.get("date", ""))[:7]
                    for card in ind.values() if isinstance(card, dict)
                    for p in card.get("hist") or []
                    if len(str(p.get("date", ""))) >= 7})[-months:]
    out: dict[str, list[dict]] = {}
    for date in dates:
        for mod in _module_scores(_indicators_as_of(ind, date), as_of=date):
            if mod.get("score") is not None:
                out.setdefault(mod["name"], []).append({"date": date, "v": mod["score"]})
    return out


def _module_scores(ind: dict, as_of: str | None = None) -> list[dict]:
    """计算各模块得分：模块内指标方向调整后历史分位（0-100）加权平均。

    国内增长与景气模块走专用引擎（4 子模块 + Level/Momentum/Surprise + 滞后衰减 +
    状态/动量/四象限）；其余模块沿用分位加权。分位用指标自身 hist 全序列；
    方向 down 取 100-分位。hist 过短（<4 点）降级为 prev 对比方向分（改善65/恶化35）。
    """
    # 国内增长与景气：先算当前分，再对照同口径历史快照得到 MoM 动量与四象限。
    climate_cur = _climate_module_score(ind, as_of)
    mom = _climate_hist_mom(climate_cur) if as_of is None else None

    out = []
    for name, icon, desc, specs in _MACRO_MODULES:
        if name == _CLIMATE_MODULE_NAME:
            if not climate_cur:
                continue
            score = climate_cur["score"]
            state = _climate_state(score) if score is not None else None
            direction_lbl = None
            if score is not None and mom is not None:
                direction_lbl = "改善" if mom > 0.3 else ("恶化" if mom < -0.3 else "持平")
            level_high = score is not None and score >= 50.0
            mom_up = (mom is not None and mom >= 0)
            quadrant = None
            if mom is not None:
                quadrant = ("扩张" if level_high else "复苏") if mom_up else ("放缓" if level_high else "衰退")
            out.append({
                "name": name, "icon": icon, "desc": desc,
                "score": score, "weight_pct": None,
                "coverage": climate_cur["coverage"], "confidence": climate_cur["confidence"],
                "indicators": climate_cur["indicators"],
                "used": climate_cur["used"],
                "submodules": climate_cur["submodules"],
                "state": state, "mom": mom, "direction": direction_lbl, "quadrant": quadrant,
            })
            continue
        total_w = sum(w for _, _, w in specs if w > 0)
        num = 50.0 * total_w
        covered_w = 0.0
        confidence_w = 0.0
        used = []
        for key, direction, w in specs:
            if w <= 0:
                continue
            ind_card = ind.get(key)
            if not ind_card or ind_card.get("value") is None:
                continue
            hist = ind_card.get("hist") or []
            vals = [p["v"] for p in hist if isinstance(p.get("v"), (int, float))]
            if len(vals) >= 4:
                pct = _pct_rank(vals, vals[-1])
            elif ind_card.get("prev") is not None:
                pct = 65.0 if ind_card["value"] > ind_card["prev"] else 35.0
            elif len(vals) >= 1:
                # 有当前值但历史不足（新指标上线初期），按中性 50 参与计分，避免拉低覆盖率。
                pct = 50.0
            else:
                continue
            if direction == "down":
                pct = 100.0 - pct
            freshness = _freshness_factor(ind_card.get("date", ""), as_of)
            num += (pct - 50.0) * w * freshness
            covered_w += w
            confidence_w += w * freshness * _quality_factor(ind_card)
            used.append({"key": key, "direction": direction, "weight": w,
                         "pct": round(pct, 1), "freshness": freshness,
                         "date": ind_card.get("date", "")})
        coverage = covered_w / total_w if total_w else 0.0
        confidence = confidence_w / total_w if total_w else 0.0
        score = round(num / total_w, 1) if coverage >= _MIN_MODULE_COVERAGE else None
        out.append({
            "name": name, "icon": icon, "desc": desc,
            "score": score, "weight_pct": None,
            "coverage": round(coverage * 100.0, 1),
            "confidence": round(confidence * 100.0, 1),
            "indicators": [k for k, _, _ in specs if k in ind],
            "used": used,
        })
    if as_of is None:
        histories = _module_score_history(ind)
        for mod in out:
            mod["hist"] = histories.get(mod["name"], [])
    return out


# ---------------------------------------------------------------------------
# 宏观总分（Macro Composite）：8 模块 → 单一 0-100 分
# 权重与方向来自 2021-12 ~ 2026-07 逐月回放回测（研究文档见
# research/A股宏观面总分模块_回测与权重设计.md）：
#   有回测历史的模块按 IC3m（对中证全A未来3月收益）定权：
#     财政地产 +0.52 / 国内增长与景气 -0.47 / 全球外部 +0.30 / 价格与工业利润 +0.03
#   增长景气模块对收益为反向（景气越热未来越弱），合成时取 100-分。
#   信用周期 / 货币与金融条件快照历史不足（2026 起源），无法回测，给小额
#   先验权重（10 / 5），方向按利多为正；显著为噪声的价格模块给 10。
# 复合分回测（重叠样本，块自助 p≈0.4，t≈2.5-3.5）：IC3m≈0.74（全A）、
# 0.63（沪深300），前1/3 组 3 月胜率 0.91 vs 后1/3 组 0.18。
# ---------------------------------------------------------------------------

_MACRO_COMPOSITE_SCHEMA = 1
_MACRO_COMPOSITE: dict[str, tuple[float, float]] = {
    "财政地产": (30.0, 1.0),
    "国内增长与景气": (25.0, -1.0),
    "全球外部": (20.0, 1.0),
    "价格与工业利润": (10.0, 1.0),
    "信用周期": (10.0, 1.0),
    "货币与金融条件": (5.0, 1.0),
}


def _macro_composite(modules: list[dict]) -> dict | None:
    """把 8 模块得分按回测权重合成总分；贡献 = (分-50)×权重（反向模块再取反）。"""
    by_name = {m["name"]: m for m in modules}
    parts = []
    num = 50.0
    covered = 0.0
    for name, (weight, sign) in _MACRO_COMPOSITE.items():
        mod = by_name.get(name)
        if not mod or mod.get("score") is None:
            parts.append({"name": name, "weight": weight, "direction": "inverse" if sign < 0 else "direct",
                          "score": None, "contribution": None})
            continue
        score = mod["score"]
        adj = 100.0 - score if sign < 0 else score
        num += (adj - 50.0) / 100.0 * weight
        covered += weight
        parts.append({"name": name, "weight": weight, "direction": "inverse" if sign < 0 else "direct",
                      "score": score, "contribution": round((adj - 50.0) / 100.0 * weight, 1)})
    total_w = sum(w for w, _s in _MACRO_COMPOSITE.values())
    if covered < 0.5 * total_w:
        return None
    score = round(num / total_w * 100.0, 1)
    contribs = [abs(p["contribution"]) for p in parts if p["contribution"] is not None]
    return {
        "schema": _MACRO_COMPOSITE_SCHEMA,
        "score": score,
        "state": _composite_state(score),
        "coverage": round(covered / total_w * 100.0, 1),
        # 主要驱动：贡献绝对值最大的两个模块
        "drivers": [p["name"] for p in
                    sorted((p for p in parts if p["contribution"] is not None),
                           key=lambda p: -abs(p["contribution"]))[:2]],
        "parts": parts,
        "desc": ("模块加权总分（0-100，越高越利多）；权重与方向由 2021-2026 逐月回放回测确定，"
                 "增长景气模块对收益为反向，合成时取 100-分"),
    }


def _composite_history(ind: dict, months: int = 36) -> list[dict]:
    """按月回放复合总分：直接透视模块得分历史，不重算模块。"""
    per_module = _module_score_history(ind, months=months)
    by_date: dict[str, dict[str, float]] = {}
    for name, pts in per_module.items():
        if name not in _MACRO_COMPOSITE:
            continue
        for pt in pts:
            by_date.setdefault(pt["date"], {})[name] = pt["v"]
    out: list[dict] = []
    for date in sorted(by_date):
        mods = [{"name": n, "score": s} for n, s in by_date[date].items()]
        comp = _macro_composite(mods)
        if comp and comp.get("score") is not None:
            out.append({"date": date, "v": comp["score"]})
    return out


def _month_end_closes(df, limit: int = 36) -> list[dict]:
    """指数日K → 月末收盘点列（按月升序；乱序输入按日期排序后取每月最后交易日）。"""
    if df is None or getattr(df, "empty", True) or "date" not in getattr(df, "columns", []):
        return []
    closes: dict[str, float] = {}
    for _, r in df.sort_values("date").iterrows():
        try:
            closes[str(r["date"])[:7]] = float(r["close"])
        except (TypeError, ValueError):
            continue
    return [{"date": d, "v": round(v, 1)} for d, v in sorted(closes.items())][-limit:]


def _composite_benchmark(limit: int = 36) -> dict | None:
    """总分对照基准：中证全A（000985）月末收盘（回测同源基准）。

    独立 6h 缓存、失败返回 None——基准缺席只影响对照图，不影响总分。"""
    def build():
        try:
            ak = astock._akshare()
            df = ak.stock_zh_index_daily_tx(symbol="sh000985")
        except Exception:
            return None
        hist = _month_end_closes(df, limit=limit)
        return {"label": "中证全A", "hist": hist} if len(hist) >= 12 else None
    return cache_runtime.get("macro_composite_benchmark", build,
                             valid=lambda v: v is not None,
                             ttl=6 * 3600, decorate=False)


def _macro_composite_full(ind: dict) -> dict | None:
    """当前总分 + 逐月历史 + 全A对照基准，顶层输出结构。"""
    mods = _module_scores(ind)
    comp = _macro_composite(mods)
    if not comp:
        return None
    comp["hist"] = _composite_history(ind)
    bench = _composite_benchmark()
    if bench:
        comp["benchmark"] = bench
    return comp


def _add_derived(ind: dict) -> None:
    """由已有指标纯计算派生新指标，就地并入 ind dict。

    - m1_m2_spread: M1-M2 剪刀差（信用活化）
    - price_spread: PMI 购进-出厂价格差（利润率压力，差值扩大=压利润）
    - private_credit_growth: 从社融/政府债存量及同比反推私人信用存量同比
    - household_ml_loan / corp_ml_loan / bill_financing: 余额转近3月月均增量
    """

    def _align_diff(a_key: str, b_key: str, label: str, unit: str = "%", a_sub=None, b_sub=None):
        a = ind.get(a_key)
        b = ind.get(b_key)
        if not a or not b:
            return None
        ah = a.get(a_sub) if a_sub else a.get("hist")
        bh = b.get(b_sub) if b_sub else b.get("hist")
        if not ah or not bh:
            return None
        bm = {p["date"]: p["v"] for p in bh}
        pts = []
        for p in ah:
            if p["date"] in bm:
                pts.append((p["date"], round(p["v"] - bm[p["date"]], 2)))
        if not pts:
            return None
        pts.sort()
        hist = [{"date": d, "v": v} for d, v in pts]
        return {
            "label": label, "value": hist[-1]["v"], "forecast": None,
            "prev": hist[-2]["v"] if len(hist) > 1 else None,
            "date": hist[-1]["date"], "hist": hist, "unit": unit, "source": "派生计算",
        }

    # M1-M2 剪刀差
    d = _align_diff("m1", "m2", "M1-M2 剪刀差")
    if d:
        ind["m1_m2_spread"] = d

    # PMI 购进-出厂价格差（正值扩大=成本压力，对利润不利；取购进−出厂）
    inp = ind.get("pmi_input_price")
    outp = ind.get("pmi_output_price")
    if inp and outp:
        om = {p["date"]: p["v"] for p in outp.get("hist", [])}
        pts = []
        for p in inp.get("hist", []):
            if p["date"] in om:
                pts.append((p["date"], round(p["v"] - om[p["date"]], 2)))
        if pts:
            pts.sort()
            hist = [{"date": d_, "v": v} for d_, v in pts]
            ind["price_spread"] = {
                "label": "PMI 购进-出厂价差", "value": hist[-1]["v"], "forecast": None,
                "prev": hist[-2]["v"] if len(hist) > 1 else None,
                "date": hist[-1]["date"], "hist": hist, "unit": "pt", "source": "派生计算",
            }

    # 私人信用存量同比：私人信用=社融存量−政府债券存量；分别由当期存量和同比反推上年存量。
    sfs = ind.get("social_financing_stock")
    if sfs and sfs.get("stock_level_hist") and sfs.get("gov_bond_level_hist"):
        total_level = {p["date"]: p["v"] for p in sfs["stock_level_hist"]}
        total_growth = {p["date"]: p["v"] for p in sfs.get("hist", [])}
        gov_level = {p["date"]: p["v"] for p in sfs["gov_bond_level_hist"]}
        gov_growth = {p["date"]: p["v"] for p in sfs.get("gov_bond_growth_hist", [])}
        pts = []
        for date in sorted(set(total_level) & set(total_growth) & set(gov_level) & set(gov_growth)):
            private_now = total_level[date] - gov_level[date]
            total_prev = total_level[date] / (1.0 + total_growth[date] / 100.0)
            gov_prev = gov_level[date] / (1.0 + gov_growth[date] / 100.0)
            private_prev = total_prev - gov_prev
            if private_now > 0 and private_prev > 0:
                pts.append((date, round((private_now / private_prev - 1.0) * 100.0, 2)))
        if pts:
            pts.sort()
            hist = [{"date": d_, "v": v} for d_, v in pts]
            ind["private_credit_growth"] = {
                "label": "私人信用存量同比", "value": hist[-1]["v"], "forecast": None,
                "prev": hist[-2]["v"] if len(hist) > 1 else None,
                "date": hist[-1]["date"], "hist": hist, "unit": "%",
                "source": "派生·(社融存量−政府债存量)同比",
            }

    def _balance_to_flow(hist: list[dict], label: str) -> dict | None:
        """余额转月度增量，再做近3月月均，消除非平稳水平值。"""
        raw = [p for p in hist if isinstance(p.get("v"), (int, float))]
        changes = [(raw[i]["date"], raw[i]["v"] - raw[i - 1]["v"]) for i in range(1, len(raw))]
        smooth = [(changes[i][0], round(sum(v for _, v in changes[i - 2:i + 1]) / 3.0, 2))
                  for i in range(2, len(changes))]
        pts = smooth or [(d, round(v, 2)) for d, v in changes]
        if not pts:
            return None
        out_hist = [{"date": d, "v": v} for d, v in pts]
        return {
            "label": label, "value": out_hist[-1]["v"], "forecast": None,
            "prev": out_hist[-2]["v"] if len(out_hist) > 1 else None,
            "date": out_hist[-1]["date"], "hist": out_hist, "unit": "亿元",
            "source": "派生·人民银行贷款余额月差(近3月均值)",
        }

    # 信贷收支子指标：只把增量/动量送入评分，原始余额仍保留在 credit_by_sector 详情中。
    cbs = ind.get("credit_by_sector")
    if cbs:
        for src_key, new_key, label in [
            ("corp_ml_loan_hist", "corp_ml_loan", "企事业中长期贷款近3月月均增量"),
            ("bill_financing_hist", "bill_financing", "票据融资近3月月均增量"),
            ("fiscal_deposit_hist", "fiscal_deposit", "财政性存款近3月月均增量"),
            ("nonbank_deposit_hist", "nonbank_deposit", "非银存款近3月月均增量"),
        ]:
            card = _balance_to_flow(cbs.get(src_key) or [], label)
            if card:
                ind[new_key] = card
        household = _balance_to_flow(cbs.get("hist") or [], "住户中长期贷款近3月月均增量")
        if household:
            ind["household_ml_loan"] = household

    # ---- 景气模块 V1.0 派生指标 ----

    def _momentum_derived(src_key: str, new_key: str, label: str):
        """实际活动动量 = 当月同比 − 过去12个月均值（景气动量，§6）。"""
        c = ind.get(src_key)
        if not c:
            return
        source_hist = [p for p in c.get("hist", []) if isinstance(p.get("v"), (int, float))]
        vals = [p["v"] for p in source_hist]
        if len(vals) < 13:
            return
        hist = [{"date": source_hist[i]["date"],
                 "v": round(vals[i] - sum(vals[i - 12:i]) / 12.0, 2)}
                for i in range(12, len(vals))]
        ind[new_key] = {
            "label": label, "value": hist[-1]["v"], "forecast": None,
            "prev": hist[-2]["v"] if len(hist) > 1 else None,
            "date": hist[-1]["date"] if hist else c.get("date", ""),
            "hist": hist, "unit": "pt",
            "source": f"派生·{c.get('label', src_key)}同比−近12月均值",
        }

    _momentum_derived("industrial", "industrial_momentum", "工业增加值动量")
    _momentum_derived("services_production", "services_momentum", "服务业生产指数动量")

    # 订单库存差 = PMI 新订单 − PMI 产成品库存（库存周期领先，§5）
    s = _align_diff("pmi_new_orders", "pmi_finished_inventory",
                    "PMI 新订单−产成品库存", unit="pt")
    if s:
        s["source"] = "派生·PMI新订单−产成品库存"
        ind["order_inventory_spread"] = s

    # CPB 世界贸易量 3MMA 同比（平滑，§8；数据为量指数，需先算同比再 3 月平滑）
    wt = ind.get("world_trade_volume")
    if wt:
        pts = [(p["date"], p["v"]) for p in wt.get("hist", []) if isinstance(p.get("v"), (int, float))]
        yoy = []
        for i, (d, v) in enumerate(pts):
            if i >= 12 and pts[i - 12][1]:
                yoy.append((d, round((v / pts[i - 12][1] - 1.0) * 100.0, 2)))
        sma = []
        for i in range(2, len(yoy)):
            sma.append((yoy[i][0], round((yoy[i][1] + yoy[i - 1][1] + yoy[i - 2][1]) / 3.0, 2)))
        if sma:
            ind["world_trade_yoy_3mma"] = {
                "label": "CPB 世界贸易量 3MMA 同比", "value": sma[-1][1], "forecast": None,
                "prev": sma[-2][1] if len(sma) > 1 else None,
                "date": sma[-1][0],
                "hist": [{"date": d, "v": v} for d, v in sma],
                "unit": "%", "source": "派生·CPB贸易量同比3月平滑",
            }


def _macro_build(force_sources: bool = False) -> dict:
    def build():
        ak = astock._akshare()
        from concurrent.futures import ThreadPoolExecutor
        previous = _last_good("macro") or _load_macro_snapshot() or {}
        previous_cn = previous.get("cn") if isinstance(previous, dict) else {}
        previous_cn = previous_cn if isinstance(previous_cn, dict) else {}

        # 官方宏观指标：优先东财 datacenter（2026 最新），investing.com 已停更仅作兜底。
        # 每项 (key, investing_fn, label, 东财回退_fn)。东财成功则用东财，否则回退 investing。
        em_specs = [
            ("gdp", ak.macro_china_gdp_yearly, "GDP 不变价同比",
             lambda: _em_series("RPT_ECONOMY_GDP", "GDP 不变价同比", "SUM_SAME")),
            ("cpi", ak.macro_china_cpi_yearly, "CPI 同比",
             lambda: _em_series("RPT_ECONOMY_CPI", "CPI 同比", "NATIONAL_SAME")),
            ("ppi", ak.macro_china_ppi_yearly, "PPI 同比",
             lambda: _em_series("RPT_ECONOMY_PPI", "PPI 同比", "BASE_SAME")),
            ("pmi", ak.macro_china_pmi_yearly, "制造业 PMI",
             lambda: _em_series("RPT_ECONOMY_PMI", "制造业 PMI", "MAKE_INDEX")),
            ("non_man_pmi", ak.macro_china_non_man_pmi, "非制造业 PMI",
             lambda: _em_series("RPT_ECONOMY_PMI", "非制造业 PMI", "NMAKE_INDEX")),
            ("industrial", ak.macro_china_industrial_production_yoy, "工业增加值同比",
             lambda: _em_series("RPT_ECONOMY_INDUS_GROW", "工业增加值同比", "BASE_SAME")),
            ("exports", ak.macro_china_exports_yoy, "出口同比",
             lambda: _em_series("RPT_ECONOMY_CUSTOMS", "出口同比", "EXIT_ACCUMULATE_SAME")),
            ("imports", ak.macro_china_imports_yoy, "进口同比",
             lambda: _em_series("RPT_ECONOMY_CUSTOMS", "进口同比", "IMPORT_ACCUMULATE_SAME")),
            ("trade_balance", ak.macro_china_trade_balance, "贸易差额",
             _em_trade_balance),
        ]
        indicators: dict = {}

        def _resolve(spec):
            key, inv_fn, label, em_fn = spec
            val = None
            try:
                val = em_fn()
            except Exception:
                val = None
            if val is None:  # 东财失败才回退 investing（可能陈旧）
                val = _inv_hist(inv_fn, label)
                if val is not None:
                    val.setdefault("source", "investing.com(更新滞后)")
            return key, val

        with ThreadPoolExecutor(max_workers=5) as pool:
            for key, val in pool.map(_resolve, em_specs):
                if val is not None:
                    indicators[key] = val

        # NBS 格式指标（单独处理）
        for key, fn in [("m2", _m2_yoy), ("m1", _m1_yoy), ("social_financing", _social_financing)]:
            val = fn()
            if val is not None:
                indicators[key] = val

        # ---- 扩展指标：macro_fetch 爬取的官方统计（PMI 分项/核心 CPI/工业利润/
        #      房地产/设备投资/财政/社融存量/信贷收支/银行家问卷/世界贸易量/专项债） ----
        try:
            ext = macro_fetch.fetch_all(force=force_sources)
        except Exception:
            ext = {}
        # 把扩展指标并入 indicators（键名统一，供分组引用）
        for k, v in ext.items():
            if isinstance(v, dict) and v.get("label"):
                indicators[k] = v
        # 财新制造业 PMI：macro_fetch 键名映射为分组引用的 cx_pmi
        if isinstance(ext.get("caixin_manufacturing_pmi"), dict):
            indicators["cx_pmi"] = ext["caixin_manufacturing_pmi"]

        if not indicators:
            return {}

        # 派生指标（依赖已有指标的纯计算）
        _add_derived(indicators)

        # 只有当前观测的发布项，用上一份快照续接可比历史。
        if indicators.get("fiscal_revenue_expenditure"):
            _append_observation_history(
                indicators["fiscal_revenue_expenditure"], previous_cn.get("fiscal_revenue_expenditure"))

        # 市场观察层：两融变化、全A宽度和一致预期EPS修正。
        try:
            cn_liq = _cn_margin_full()
            if cn_liq:
                margin_hist = cn_liq.get("rzrqye_hist") or []
                if margin_hist:
                    margin_momentum = [
                        {"date": margin_hist[i]["date"],
                         "v": round((margin_hist[i]["v"] / margin_hist[i - 20]["v"] - 1.0) * 100.0, 2)}
                        for i in range(20, len(margin_hist)) if margin_hist[i - 20]["v"]
                    ]
                    if not margin_momentum:
                        margin_momentum = margin_hist
                    indicators["mkt_margin_balance"] = {
                        "label": "两融余额20日变化", "value": margin_momentum[-1]["v"],
                        "forecast": None, "prev": margin_momentum[-2]["v"] if len(margin_momentum) > 1 else None,
                        "date": margin_momentum[-1]["date"], "hist": margin_momentum,
                        "unit": "%", "source": "派生·全市场两融余额20日变化",
                    }
                netbuy_hist = cn_liq.get("rzjme_hist") or []
                if netbuy_hist:
                    indicators["mkt_margin_netbuy"] = {
                        "label": "融资净买入", "value": cn_liq.get("rzjme_yi"),
                        "forecast": None, "prev": netbuy_hist[-2]["v"] if len(netbuy_hist) > 1 else None,
                        "date": cn_liq.get("date", ""), "hist": netbuy_hist,
                        "unit": "亿", "source": "资金面·融资",
                    }
        except Exception:
            pass

        try:
            breadth = _market_breadth_card(previous_cn.get("market_breadth"))
            if breadth:
                indicators["market_breadth"] = breadth
        except Exception:
            pass
        try:
            eps_revision = _eps_revision_breadth()
            if eps_revision:
                indicators["eps_revision_breadth"] = eps_revision
        except Exception:
            pass

        # 盈利与估值层：盈利广度、PE-TTM、ERP
        try:
            profit = _profit_breadth_card(previous_cn.get("profit_breadth"))
            if profit:
                indicators["profit_breadth"] = profit
        except Exception:
            pass
        try:
            val_cards = _index_valuation_cards()
            if val_cards:
                indicators.update(val_cards)
        except Exception:
            pass

        # 市场确认层：新高占比
        try:
            nh = _new_high_breadth_card()
            if nh:
                indicators["new_high_breadth"] = nh
        except Exception:
            pass

        # 按八层框架分组，前端按组渲染
        groups = {
            "增长": ["gdp", "industrial", "industrial_revenue", "fai_equipment"],
            "物价": ["cpi", "core_cpi", "ppi"],
            "景气": ["pmi_headline", "pmi_new_orders", "pmi_production", "pmi_new_export_orders",
                     "pmi_finished_inventory", "non_man_pmi", "cx_pmi", "caixin_services_pmi",
                     "pmi_expectation"],
            "价格价差": ["pmi_input_price", "pmi_output_price", "price_spread"],
            "信用": ["m2", "m1", "m1_m2_spread", "social_financing", "social_financing_stock",
                     "private_credit_growth", "household_ml_loan", "corp_ml_loan", "bill_financing"],
            "货币流动性": ["fiscal_deposit", "nonbank_deposit", "bank_survey",
                           "dr007_policy_spread", "ncd_aaa_spread", "credit_spread_aaa"],
            "财政地产": ["fiscal_expenditure", "fiscal_revenue_expenditure", "special_bond_issuance",
                        "property_sales_area", "property_funds", "property_loans",
                        "resale_house_breadth", "policy_execution"],
            "盈利": ["industrial_profit", "industrial_inventory", "eps_revision_breadth",
                     "profit_breadth", "index_pe_ttm", "equity_risk_premium"],
            "市场确认": ["mkt_margin_balance", "mkt_margin_netbuy", "market_breadth",
                        "new_high_breadth"],
            "外贸全球": ["exports", "imports", "trade_balance", "world_trade_volume", "world_trade_yoy_3mma",
                        "copper_oil_ratio", "usdcnh"],
        }

        return {
            "cn": indicators,
            "groups": {g: [k for k in keys if k in indicators] for g, keys in groups.items()},
            "clusters": _MACRO_CLUSTERS,
            "_fetched_keys": list(indicators),
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }

    val = build()
    if val and (val.get("cn") or val.get("groups")):
        fetched_keys = set(val.pop("_fetched_keys", []))
        prev = _last_good("macro")
        fallback_fetched_at = prev.get("updated") if isinstance(prev, dict) else None
        if isinstance(prev, dict):
            # cn 指标做 last-good 合并（单源故障回退旧值）；
            # groups 由当轮指标重建，不合并，避免旧分组键残留
            val["cn"] = _merge(prev.get("cn"), val.get("cn"), "cn")
            # 旧快照可能仍含已废弃的大单流向加总，或旧版两融绝对水平；
            # 未在本轮按新口径重算时不允许悄悄续接。
            val["cn"].pop("mkt_main_inflow", None)
            for key in ("mkt_margin_balance", "mkt_margin_netbuy",
                        "profit_breadth", "index_pe_ttm",
                        "equity_risk_premium", "new_high_breadth"):
                if key not in fetched_keys:
                    val["cn"].pop(key, None)
            val["groups"] = {g: [k for k in keys if k in val["cn"]]
                             for g, keys in val["groups"].items()}
            val.pop("stale", None)
            val.pop("stale_since", None)
        # 口径已被准确私人信用存量同比替代，禁止 last-good 把旧近似键续命。
        val["cn"].pop("private_credit_pulse", None)
        _annotate_macro_indicators(val["cn"], fetched_keys, val["updated"], fallback_fetched_at)
        val["modules"] = _module_scores(val["cn"])
        val["composite"] = _macro_composite_full(val["cn"])
        _save_climate_hist(val["modules"])
        _save_json(_MACRO_SNAPSHOT, val)
    return val


def get_macro(force: bool = False) -> dict:
    """国内重要宏观经济指标汇总（GDP/CPI/PPI/PMI/M2/工业增加值/进出口/贸易差额/社融）。

    last-good 语义同 get_liquidity：源故障回退最近成功值并标注 stale；
    月度/季度数据 1 小时刷新足够。冷启动从磁盘快照恢复。
    """
    return _layered_get("macro", lambda: _macro_build(force_sources=force),
                        valid=lambda v: bool(v.get("cn") or v.get("groups")),
                        warm=_load_macro_snapshot, ttl=_MACRO_TTL, force=force)
