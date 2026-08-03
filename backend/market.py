"""市场总览数据层 —— 市场情绪 + 板块资金流（板块/大盘级公开数据，不涉个股推荐）。

省流量：全站共享一份缓存（TTL 默认 5 分钟），多个用户/多次打开只抓一次；
盘中 5 分钟刷新足够，非交易时段数据本就不变。数据源全免费、无 key。
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

import astock
import gstock

BEIJING = timezone(timedelta(hours=8))
_CACHE: dict = {}
_TTL = 300  # 5 分钟；全站共享，省数据源压力


def _cached(key: str, fn, valid=bool):
    """TTL 缓存。数据源故障的空结果不缓存（valid 判否），下次请求直接重试。

    入口见 get_liquidity 的 last-good 语义（失败回退旧值）；本函数语义保持"空结果不缓存"，
    供 overview/emotion 等其他调用方继续按原约定使用。
    """
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    val = fn()
    if valid(val):
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
    return d if isinstance(d, dict) and (d.get("cn") or d.get("us")) else None


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


def _layered_get(key: str, build, valid, warm=None):
    """last-good 语义：新鲜值 TTL 内直接返回；过期则重建；

    重建失败/结果无效时，回退最近一次成功的 last-good 并标注 stale；
    故障期按 next_retry 退避，不每请求重建。首次冷启动用 warm()（磁盘快照）兜底。
    本轮部分源故障时，由 build() 内部通过 last_good(key) 逐段合并。
    """
    now = time.time()
    e = _LAYERED.setdefault(key, {})
    fresh = e.get("fresh")
    if fresh and now - fresh[0] < _TTL:
        return fresh[1]
    # 无 last-good 时先装磁盘快照：即使即将重建，也让 build 失败路径立即可回退
    if not e.get("last_good") and warm:
        warm_val = warm()
        if warm_val is not None:
            e["last_good"] = (now, warm_val)
    if now < e.get("next_retry", 0):
        lg = e.get("last_good")
        if lg:
            return _with_stale(lg[1], lg[0])
        # 无 last-good、无快照：放行至 build（有机会拿到部分/全部数据）
    try:
        val = build()
    except Exception:
        val = None
    if val is not None and valid(val):
        e["fresh"] = (now, val)
        e["last_good"] = (now, val)
        e["next_retry"] = 0
        _FAILED_STREAK.pop(key, None)
        return val
    _FAILED_STREAK[key] = _FAILED_STREAK.get(key, 0) + 1
    e["next_retry"] = now + _RETRY_BACKOFF[min(_FAILED_STREAK[key], len(_RETRY_BACKOFF)) - 1]
    lg = e.get("last_good")
    if lg:
        return _with_stale(lg[1], lg[0])
    return val if isinstance(val, dict) else {}


def _merge(prev, cur, key: str):
    """本轮新值优先，缺失时回退 last-good 里的同名字段（dict 逐 key / 整段回退）。"""
    if isinstance(prev, dict) and isinstance(cur, dict):
        out = dict(prev)
        out.update({k: v for k, v in cur.items() if v not in (None, {}, [])})
        return out
    return cur if cur not in (None, {}, []) else prev


def _last_good(key: str):
    e = _LAYERED.get(key) or {}
    lg = e.get("last_good")
    return lg[1] if lg else None


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


def _sub_cached(key: str, fn, ttl: float = _SUB_TTL, valid=bool):
    """单源小缓存：TTL 内直接返回；过期先同步拉一次；失败回 last-good + 后台重试。

    用于利率/债券等低频序列——多数请求 0 外呼，源偶发故障也不丢指标。
    """
    now = time.time()
    hit = _SUB.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    if val is not None and valid(val):
        _SUB[key] = (now, val)
        _SUB_LAST[key] = (now, val)
        return val
    last = _SUB_LAST.get(key)
    if last and now - last[0] < _SUB_STALE_TTL:
        _kick_bg(f"sub:{key}", lambda: _sub_refresh(key, fn, ttl, valid))
        return last[1]
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
    def build():
        return {
            "sentiment": _sentiment(),
            "sectors": _sectors(),
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
    return _cached("overview", build, valid=lambda v: bool(v.get("sentiment") or v.get("sectors")))


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
    """短线情绪（含缓存，5 分钟）。"""
    return _cached("emotion", _emotion)


def get_turnover_top() -> dict:
    """全市场成交额榜 Top20（客观公开榜单，含缓存 5 分钟）。"""
    def build():
        return {
            "stocks": astock.market_turnover_rank(20),
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
    return _cached("turnover_top", build, valid=lambda v: bool(v.get("stocks")))


def get_global_indices() -> list[dict]:
    """全球指数快照（美股 / 港股，含缓存 5 分钟）。空结果不缓存。"""
    return _cached("global_indices", gstock.global_indices, valid=bool)



# ---------------------------------------------------------------------------
# 资金供给 · 独立页面数据层（国内 + 国外美国，含历史趋势 + 美联储利率）
# ---------------------------------------------------------------------------

# 美债利率 / 美联储 / 利差 FRED 序列定义：key → (series_id, 单位, 中文标签)
# FRED CSV 用 curl 拉取（本环境 requests 直连/代理均超时，但 curl 稳定）。
_FRED_SERIES = {
    "effr":        ("EFFR",     "%",  "有效联邦基金利率 EFFR"),
    "dgs10":       ("DGS10",    "%",  "美债 10 年期收益率"),
    "dgs2":        ("DGS2",     "%",  "美债 2 年期收益率"),
    "dgs3m":       ("DGS3MO",   "%",  "美债 3 个月收益率"),
    "t10y3m":      ("T10Y3M",   "%",  "10Y − 3M 利差"),
    "t10y2y":      ("T10Y2Y",   "%",  "10Y − 2Y 利差"),
    "fed_target_u": ("DFEDTARU","%",  "美联储利率上限"),
    "fed_target_l": ("DFEDTARL","%",  "美联储利率下限"),
    "sofr":        ("SOFR",     "%",  "SOFR 担保隔夜融资利率"),
    "rrp":         ("RRPONTSYD","十亿$","隔夜逆回购 ON RRP"),   # FRED 原始单位=十亿美元
    "dgs30":       ("DGS30",    "%",  "美债 30 年期收益率"),
    "walcl":       ("WALCL",    "亿$",  "美联储总资产"),            # FRED 原始单位=百万美元
    # ↓ 国外综合指数专用（不在单卡区展示，直接供合成）
    "ig_oas":      ("BAMLC0A0CM",  "%", "ICE BofA 投资级公司债 OAS"),
    "hy_oas":      ("BAMLH0A0HYM2","%", "ICE BofA 高收益债 OAS"),
    "vix":         ("VIXCLS",      "",  "VIX 波动率指数"),
    "iorb":        ("IORB",        "%", "准备金余额利率 IORB"),
    "reserves":    ("WTREGEN",     "十亿$","银行准备金余额"),      # FRED 原始单位=百万美元
    "term_premium": ("THREEFYTP10","%", "10Y 国债期限溢价（Kim-Wright）"),
    "dxy":         ("DTWEXBGS",    "",  "广义美元指数"),
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


def _fred_series_cached(series_id: str, limit: int) -> list[tuple[str, float]]:
    key = f"fred:{series_id}:{limit}"
    now = time.time()
    hit = _SUB.get(key)
    if hit and now - hit[0] < _FRED_SERIES_TTL:
        return hit[1]
    rows = _fred_csv(series_id, limit)
    if rows:
        _SUB[key] = (now, rows)
        _SUB_LAST[key] = (now, rows)
        snap = _load_fred_snapshot()
        snap[key] = {"ts": now, "rows": [[d, v] for d, v in rows]}
        _save_json(_FRED_SNAPSHOT, snap)
        return rows
    # 失败：内存 last-good → 磁盘快照
    last = _SUB_LAST.get(key)
    if last:
        _kick_bg(f"sub:{key}", lambda: _fred_refresh(key, series_id, limit))
        return last[1]
    snap_hit = _load_fred_snapshot().get(key)
    if snap_hit and snap_hit.get("rows"):
        rows = [(str(d), float(v)) for d, v in snap_hit["rows"]]
        _SUB_LAST[key] = (float(snap_hit.get("ts") or now), rows)
        return rows
    return []


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


def _pct_of(series: list[float], current: float) -> float:
    return _pct_rank(series, current)


def _mk_us_index(label: str, comps: list[dict], weights: list[float],
                 interpretation: str, desc: str, date: str,
                 hist_from: list[dict], favorable: str = "low") -> dict:
    """国外综合指数组装：分位加权合成 0-100。favorable=low 表示分低有利，high 反之。"""
    value = round(sum(c["pct"] * w for c, w in zip(comps, weights)), 1)
    return {
        "value": value,
        "label": label,
        "favorable": favorable,
        "desc": desc,
        "date": date,
        "hist": hist_from,
        "interpretation": interpretation,
        "components": comps,
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

    成功拉取即落盘快照；数据源断连时回退快照，避免杠杆情绪指数的子指标图表空窗。
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
            return last[1]
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
    }
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
        return last[1]
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
        if len(hist) > 1:
            fresh_ok = True
        elif len(snapshot.get(secid, {}).get("hist", [])) > 1:
            # 历史源断连：回退本地快照（最近一次成功抓取的 30 日序列）
            hist = list(snapshot[secid]["hist"])
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
            out[secid] = {"name": name, "hist": hist, "latest": hist[-1]}
    # 日期对齐：个别指数还没推当日点时，用其最新值补齐到最新交易日，保证三指数合计可算
    latest_date = max((f["latest"]["date"] for f in out.values()), default=None)
    if latest_date:
        for f in out.values():
            if f["latest"]["date"] < latest_date:
                f["hist"].append({"date": latest_date, "v": f["latest"]["v"]})
                f["latest"] = f["hist"][-1]
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

    # 反推各档区间概率：P(>X) 的差分
    # 按 strike 从高到低，相邻两档 P 之差即为落在该档的概率
    bands = []
    for i in range(len(strikes) - 1):
        higher = strikes[i]      # 更高利率档
        lower = strikes[i + 1]   # 更低利率档
        band_prob = round(lower["prob"] - higher["prob"], 1)
        bands.append({
            "label": f"> {higher['strike']:.2f}%",
            "prob": higher["prob"],
        })
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


# 美联储加息概率的"最后已知良好值"持久化文件：Kalshi 挂了时兜底，避免页面空白。
_FED_ODDS_LAST = os.path.join(os.path.expanduser("~"), ".vibe-research", "fed_odds_last.json")


def _fed_odds_with_fallback() -> dict:
    """Kalshi 实时数据优先；取不到时回退到磁盘上的最近一次成功结果，并标注 stale。"""
    fresh = _kalshi_fed_odds()
    if fresh.get("strikes"):
        # 成功：写盘留作下次兜底
        try:
            os.makedirs(os.path.dirname(_FED_ODDS_LAST), exist_ok=True)
            with open(_FED_ODDS_LAST, "w", encoding="utf-8") as f:
                json.dump({**fresh, "fetched_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")}, f, ensure_ascii=False)
        except Exception:
            pass
        fresh["stale"] = False
        return fresh
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


def _zscore(series: list[float], current: float) -> float:
    """z-score → 0-100 分位（越高=越紧/越热）。"""
    if len(series) < 20:
        return 50.0
    mean = sum(series) / len(series)
    std = max((sum((v - mean) ** 2 for v in series) / len(series)) ** 0.5, 1e-9)
    z = (current - mean) / std
    # z-score → 0-100（正态 CDF 近似）
    pct = 50 + 50 * (1 if z > 0 else -1) * min(abs(z) / 3.0, 1.0) ** 0.7
    return round(max(0, min(100, pct)), 1)


def _pct_rank(series: list[float], current: float) -> float:
    """近 N 日分位（0-100，越高=越紧/越热）。"""
    if not series:
        return 50.0
    below = sum(1 for v in series if v <= current)
    return round(below / len(series) * 100, 1)


def _cn_short_liquidity_index() -> dict:
    """短期流动性指数：SHIBOR O/N + 1W + FR001 + FR007 等权，近 6 个月窗口，越高=资金越紧。

    带单指数缓存：源故障时回退最近一次成功值，页面不空窗。
    """
    return _sub_cached("cn_idx:short_liquidity", _cn_short_liquidity_index_fetch, ttl=1800)


def _cn_short_liquidity_index_fetch() -> dict:
    try:
        ak = astock._akshare()
        end = datetime.now(BEIJING).strftime("%Y%m%d")
        start = (datetime.now(BEIJING) - timedelta(days=200)).strftime("%Y%m%d")
        df_shibor = ak.macro_china_shibor_all().tail(120)
        df_repo = ak.repo_rate_hist(start_date=start, end_date=end).tail(120)
    except Exception:
        return {}

    if df_shibor.empty or df_repo.empty:
        return {}

    # 最新值
    latest_shibor = df_shibor.iloc[-1]
    latest_repo = df_repo.iloc[-1]

    # 各子指标近 250 日分位
    on_pct = _pct_rank(df_shibor["O/N-定价"].dropna().tolist(), latest_shibor["O/N-定价"])
    w1_pct = _pct_rank(df_shibor["1W-定价"].dropna().tolist(), latest_shibor["1W-定价"])
    fr001_pct = _pct_rank(df_repo["FR001"].dropna().tolist(), latest_repo["FR001"])
    fr007_pct = _pct_rank(df_repo["FR007"].dropna().tolist(), latest_repo["FR007"])

    composite = round((on_pct + w1_pct + fr001_pct + fr007_pct) / 4, 1)

    # 历史序列：各子指标分位平均
    hist_map = {}
    for _, row in df_shibor.iterrows():
        d = str(row["日期"])
        if d not in hist_map:
            hist_map[d] = {}
        hist_map[d]["on"] = row.get("O/N-定价")
        hist_map[d]["w1"] = row.get("1W-定价")
    for _, row in df_repo.iterrows():
        d = str(row["date"])
        if d not in hist_map:
            hist_map[d] = {}
        hist_map[d]["fr001"] = row.get("FR001")
        hist_map[d]["fr007"] = row.get("FR007")

    # 对每天都有 4 个子指标的日期，计算当日分位（用全部历史数据）
    all_on = df_shibor["O/N-定价"].dropna().tolist()
    all_w1 = df_shibor["1W-定价"].dropna().tolist()
    all_fr001 = df_repo["FR001"].dropna().tolist()
    all_fr007 = df_repo["FR007"].dropna().tolist()

    hist = []
    for d in sorted(hist_map.keys())[-120:]:  # 近 6 个月交易日
        vals = hist_map[d]
        if all(k in vals and vals[k] is not None for k in ("on", "w1", "fr001", "fr007")):
            pct = (_pct_rank(all_on, vals["on"]) + _pct_rank(all_w1, vals["w1"]) +
                   _pct_rank(all_fr001, vals["fr001"]) + _pct_rank(all_fr007, vals["fr007"])) / 4
            hist.append({"date": d, "v": round(pct, 1)})

    # 子指标原始值历史序列（前端展开画趋势图）
    def _raw_hist(series: dict, days: int = 120) -> list[dict]:
        pts = [(str(d), v) for d, v in series.items() if v is not None]
        return [{"date": d, "v": round(float(v), 4)} for d, v in pts[-days:]]

    shibor_on_map = {str(r["日期"]): r.get("O/N-定价") for _, r in df_shibor.iterrows()}
    shibor_w1_map = {str(r["日期"]): r.get("1W-定价") for _, r in df_shibor.iterrows()}
    fr001_map = {str(r["date"]): r.get("FR001") for _, r in df_repo.iterrows()}
    fr007_map = {str(r["date"]): r.get("FR007") for _, r in df_repo.iterrows()}

    return {
        "value": composite,
        "label": "短期流动性",
        "favorable": "low",
        "desc": f"SHIBOR O/N {latest_shibor['O/N-定价']:.2f}% · 1W {latest_shibor['1W-定价']:.2f}% · FR001 {latest_repo['FR001']:.2f}% · FR007 {latest_repo['FR007']:.2f}%",
        "date": str(latest_shibor["日期"]),
        "hist": hist,
        "interpretation": "越高=资金面越紧（越低越有利；<30 宽松，>70 偏紧）",
        "components": [
            {"label": "SHIBOR O/N", "value": f"{latest_shibor['O/N-定价']:.2f}%", "pct": round(on_pct, 1),
             "hist": _raw_hist(shibor_on_map)},
            {"label": "SHIBOR 1W", "value": f"{latest_shibor['1W-定价']:.2f}%", "pct": round(w1_pct, 1),
             "hist": _raw_hist(shibor_w1_map)},
            {"label": "FR001", "value": f"{latest_repo['FR001']:.2f}%", "pct": round(fr001_pct, 1),
             "hist": _raw_hist(fr001_map)},
            {"label": "FR007", "value": f"{latest_repo['FR007']:.2f}%", "pct": round(fr007_pct, 1),
             "hist": _raw_hist(fr007_map)},
        ],
    }


def _cn_policy_rate_index() -> dict:
    """政策利率指数：LPR 1Y + 5Y 近 3 年分位与历史，越高=资金成本越高。

    月度披露：12 小时缓存，源故障回退 last-good。
    """
    return _sub_cached("cn_idx:policy_rate", _cn_policy_rate_index_fetch, ttl=12 * 3600)


def _cn_policy_rate_index_fetch() -> dict:
    try:
        ak = astock._akshare()
        df = ak.macro_china_lpr().tail(42)  # 近 3.5 年月度（LPR 月度披露，留冗余覆盖 3 年）
    except Exception:
        return {}
    if df.empty:
        return {}

    latest = df.iloc[-1]
    lpr1y_pct = _pct_rank(df["LPR1Y"].dropna().tolist(), latest["LPR1Y"])
    lpr5y_pct = _pct_rank(df["LPR5Y"].dropna().tolist(), latest["LPR5Y"])
    composite = round((lpr1y_pct + lpr5y_pct) / 2, 1)

    hist = []
    for _, row in df.iterrows():
        d = str(row["TRADE_DATE"])
        l1, l5 = row.get("LPR1Y"), row.get("LPR5Y")
        if l1 is not None and l5 is not None:
            pct = (_pct_rank(df["LPR1Y"].dropna().tolist(), l1) +
                   _pct_rank(df["LPR5Y"].dropna().tolist(), l5)) / 2
            hist.append({"date": d, "v": round(pct, 1)})

    return {
        "value": composite,
        "label": "政策利率",
        "favorable": "low",
        "desc": f"LPR 1Y {latest['LPR1Y']:.2f}% · 5Y {latest['LPR5Y']:.2f}%",
        "date": str(latest["TRADE_DATE"]),
        "hist": hist,  # 近 3 年月度
        "interpretation": "越高=贷款成本越高（越低越有利；近 3 年分位，<30 宽松，>70 偏高）",
        "components": [
            {"label": "LPR 1Y", "value": f"{latest['LPR1Y']:.2f}%", "pct": round(lpr1y_pct, 1),
             "hist": [{"date": str(r["TRADE_DATE"]), "v": round(float(r["LPR1Y"]), 2)}
                      for _, r in df.iterrows() if r.get("LPR1Y") is not None]},
            {"label": "LPR 5Y", "value": f"{latest['LPR5Y']:.2f}%", "pct": round(lpr5y_pct, 1),
             "hist": [{"date": str(r["TRADE_DATE"]), "v": round(float(r["LPR5Y"]), 2)}
                      for _, r in df.iterrows() if r.get("LPR5Y") is not None]},
        ],
    }


def _cn_bond_index() -> dict:
    """债市景气指数：中债国债总净价指数近 3 年分位与历史，越高=债市越强。

    日频：6 小时缓存，源故障回退 last-good。
    """
    return _sub_cached("cn_idx:bond", _cn_bond_index_fetch, ttl=6 * 3600)


def _cn_bond_index_fetch() -> dict:
    try:
        ak = astock._akshare()
        df = ak.bond_treasury_index_cbond().tail(750)  # 近 3 年交易日
    except Exception:
        return {}
    if df.empty:
        return {}

    latest = df.iloc[-1]
    vals = df["value"].tolist()
    composite = _pct_rank(vals, latest["value"])

    hist = [{"date": str(r["date"]), "v": round(_pct_rank(vals, r["value"]), 1)} for _, r in df.iterrows()]

    # 近 20 日涨跌幅
    if len(vals) >= 21:
        chg_20d = round((vals[-1] - vals[-21]) / vals[-21] * 100, 2)
    else:
        chg_20d = None

    return {
        "value": composite,
        "label": "债市景气",
        "favorable": "low",  # 股债跷跷板：债市弱=分低=对股市有利
        "desc": f"中债国债总净价 {latest['value']:.2f}" + (f" · 近 20 日 {chg_20d:+.2f}%" if chg_20d is not None else ""),
        "date": str(latest["date"]),
        "hist": hist,
        "interpretation": "越高=债市走牛（股债跷跷板，越高对股市越不利；>70 偏强）",
        "components": [
            {"label": "中债国债总净价", "value": f"{latest['value']:.2f}", "pct": round(composite, 1),
             "hist": [{"date": str(r["date"]), "v": round(float(r["value"]), 2)} for _, r in df.iterrows()]},
        ],
    }


def _cn_leverage_index(cn_margin: dict) -> dict:
    """杠杆情绪指数：两融余额分位 + 融资净买入近 5 日均值分位，越高=杠杆情绪越热。"""
    rzrqye_hist = cn_margin.get("rzrqye_hist", [])
    rzjme_hist = cn_margin.get("rzjme_hist", [])
    if not rzrqye_hist:
        return {}

    # 近 6 个月窗口（数据层已拉 130 条，快照兜底时可能只有 20-25 条，按可得窗口算）
    window = rzrqye_hist[-120:]
    vals = [h["v"] for h in window]
    balance_pct = _pct_rank(vals, vals[-1])

    # 融资净买入近 5 日均值
    if len(rzjme_hist) >= 5:
        recent5 = [h["v"] for h in rzjme_hist[-5:]]
        jme_mean = sum(recent5) / len(recent5)
        all_jme = [h["v"] for h in rzjme_hist[-120:]]
        jme_pct = _pct_rank(all_jme, jme_mean)
    else:
        jme_mean = 0.0
        jme_pct = 50.0

    composite = round(balance_pct * 0.6 + jme_pct * 0.4, 1)

    # 历史合成序列（6 个月窗口）
    hist = []
    all_jme = [h["v"] for h in rzjme_hist[-120:]]
    start = len(rzrqye_hist) - len(window)
    for i, h in enumerate(window, start=start):
        b_pct = _pct_rank(vals, h["v"])
        if i >= 4 and i < len(rzjme_hist):
            recent = [x["v"] for x in rzjme_hist[max(0, i-4):i+1]]
            j_pct = _pct_rank(all_jme, sum(recent) / len(recent))
        else:
            j_pct = 50.0
        hist.append({"date": h["date"], "v": round(b_pct * 0.6 + j_pct * 0.4, 1)})

    return {
        "value": composite,
        "label": "杠杆情绪",
        "favorable": "high",
        "desc": f"两融 {vals[-1]:.0f} 亿（分位 {balance_pct:.0f}）· 近5日净买入均值 {jme_mean:.1f} 亿",
        "date": rzrqye_hist[-1]["date"],
        "hist": hist,
        "interpretation": "越高=杠杆资金越热（越高越有利；>70 亢奋，<30 冰点）",
        "components": [
            {"label": "两融余额（权重 60%）", "value": f"{vals[-1]:.0f} 亿", "pct": round(balance_pct, 1),
             "hist": [{"date": h["date"], "v": round(float(h["v"]), 1)} for h in rzrqye_hist[-120:]]},
            {"label": "融资净买入近5日均值（权重 40%）", "value": f"{jme_mean:+.1f} 亿", "pct": round(jme_pct, 1),
             "hist": [{"date": h["date"], "v": round(float(h["v"]), 1)} for h in rzjme_hist[-120:]]},
        ],
    }


def _total_flow_hist(flows: dict, days: int = 30) -> list[dict]:
    """三指数主力净流入按日期加总的序列；最后一点用当日实时合计替换（T 日各指数 hist 尚是昨日）。"""
    dates = sorted({h["date"] for f in flows.values() for h in f.get("hist", [])})
    total = sum(f["latest"]["v"] for f in flows.values()) if flows else 0.0
    pts = []
    for d in dates[-days:]:
        s = 0.0
        for f in flows.values():
            hit = next((h["v"] for h in f.get("hist", []) if h["date"] == d), None)
            if hit is None:
                s = None  # 某指数当日缺数据则该日不完整，跳过
                break
            s += hit
        if s is not None:
            pts.append({"date": d, "v": round(s, 1)})
    if pts:
        pts[-1]["v"] = round(total, 1)  # 当日实时合计覆盖末点
    return pts


def _cn_momentum_index(cn_data: dict) -> dict:
    """主力动量指数：融资净买入近 5 日均值分位(60%) + 当日主力净流入分位(40%)，越高=增量资金越积极。

    当日主力净流入是盘中实时值（东财 push2delay），融资净买入是 T+1。
    """
    rzjme_hist = cn_data.get("rzjme_hist", [])
    if len(rzjme_hist) < 5:
        return {}

    flows = cn_data.get("index_flows", {})
    total = sum(f["latest"]["v"] for f in flows.values()) if flows else 0

    all_jme = [h["v"] for h in rzjme_hist[-120:]]
    # 近 5 日均值
    recent5 = [h["v"] for h in rzjme_hist[-5:]]
    mean5 = sum(recent5) / len(recent5)
    jme_pct = _pct_rank(all_jme, mean5)

    # 当日主力净流入分位（用上证指数近 20 日 hist 做基准）
    flow_pct = 50.0
    sh_hist = flows.get("1.000001", {}).get("hist", [])
    if len(sh_hist) >= 3:
        sh_vals = [h["v"] for h in sh_hist]
        # 用三指数合计替代单指数，更全
        flow_pct = _pct_rank(sh_vals, total / 3)  # 除以 3 近似到单指数量级
    composite = round(jme_pct * 0.6 + flow_pct * 0.4, 1)

    hist = []
    for i, h in enumerate(rzjme_hist[-120:], start=len(rzjme_hist) - min(len(rzjme_hist), 120)):
        if i >= 4:
            win = [x["v"] for x in rzjme_hist[max(0, i-4):i+1]]
            m = sum(win) / len(win)
            hist.append({"date": h["date"], "v": round(_pct_rank(all_jme, m), 1)})
    # 追加当日实时值（如果有主力净流入数据）
    if flows:
        from datetime import date as _date
        today_str = _date.today().isoformat()
        if not hist or hist[-1]["date"] != today_str:
            hist.append({"date": today_str, "v": composite})  # composite 已反向

    return {
        "value": composite,
        "label": "主力动量",
        "favorable": "high",
        "desc": f"当日三指数主力净流入 {total:+.1f} 亿 · 融资净买入近5日均值 {mean5:+.1f} 亿",
        "date": rzjme_hist[-1]["date"],
        "hist": hist,
        "interpretation": "越高=增量资金越积极（越高越有利；>70 流入强，<30 大幅流出）",
        "components": [
            {"label": "融资净买入近5日均值（权重 60%）", "value": f"{mean5:+.1f} 亿", "pct": round(jme_pct, 1),
             "hist": [
                 {"date": h["date"], "v": round(
                     sum(x["v"] for x in rzjme_hist[max(0, i - 4):i + 1]) /
                     len(rzjme_hist[max(0, i - 4):i + 1]), 1)}
                 for i, h in enumerate(rzjme_hist)
             ][-120:]},
            {"label": "当日主力净流入（权重 40%）", "value": f"{total:+.1f} 亿", "pct": round(flow_pct, 1),
             "hist": _total_flow_hist(flows)},
        ],
    }


# ---------------------------------------------------------------------------
# 国外（美国）综合指数 —— 分位加权合成 0-100，越高=越紧/越热
# 调研依据：芝加哥联储 NFCI（风险/信用/杠杆三分法）、高盛 FCI（政策利率/长端利率/
# 信用利差/汇率/权益五因子）、纽约联储 10Y-3M 利差衰退预测（Estrella-Mishkin）、
# 纽联储 2026 SOMA 报告（EFFR-IORB 利差=准备金充裕度关键指标）、ICE BofA HY OAS。
# ---------------------------------------------------------------------------

_US_INDEX_WIN = 250  # 国外指数统一窗口：近 250 个交易日（≈1 年），日频数据

def _us_credit_stress_index(us: dict) -> dict:
    """信用压力指数：HY OAS(60%) + IG OAS(40%) 近一年分位，越高=信用越紧。

    HY OAS 是 1997 年以来历次衰退/股灾的领先信号（纽联储与多篇研报），
    IG OAS 代表投资级融资成本。ICE 自 2026-04 起仅向 FRED 提供近 3 年数据，窗口内无碍。
    """
    hy = us.get("hy_oas", {}).get("hist") or []
    ig = us.get("ig_oas", {}).get("hist") or []
    if len(hy) < 60 or len(ig) < 60:
        return {}
    win = _US_INDEX_WIN
    hyv = [p["v"] for p in hy][-win:]
    igv = [p["v"] for p in ig][-win:]
    hy_pct = _pct_of(hyv, hyv[-1])
    ig_pct = _pct_of(igv, igv[-1])

    ig_map = {p["date"]: p["v"] for p in ig}
    hist = []
    for p in hy[-win:]:
        if p["date"] in ig_map:
            hist.append({"date": p["date"], "v": round(
                _pct_of(hyv, p["v"]) * 0.6 + _pct_of(igv, ig_map[p["date"]]) * 0.4, 1)})

    return _mk_us_index(
        "信用压力",
        [{"label": "高收益债 OAS（权重 60%）", "value": f"{hyv[-1]:.2f}%", "pct": round(hy_pct, 1), "hist": hy[-win:]},
         {"label": "投资级债 OAS（权重 40%）", "value": f"{igv[-1]:.2f}%", "pct": round(ig_pct, 1), "hist": ig[-win:]}],
        [0.6, 0.4],
        "越高=信用利差越宽、融资越紧（越低越有利；<30 宽松，>70 紧张）",
        f"HY OAS {hyv[-1]:.2f}% · IG OAS {igv[-1]:.2f}%",
        hy[-1]["date"], hist)


def _us_curve_index(us: dict) -> dict:
    """曲线倒挂指数：10Y−3M(70%) + 10Y−2Y(30%)，越倒挂分越高。

    10Y−3M 是纽约联储衰退概率模型的唯一输入（Estrella-Mishkin，2-6 个季度
    前瞻最强），10Y−2Y 为市场更常用的辅助口径。取分位后正向化（倒挂=紧）。
    """
    s1 = us.get("t10y3m", {}).get("hist") or []
    s2 = us.get("t10y2y", {}).get("hist") or []
    if len(s1) < 60 or len(s2) < 60:
        return {}
    win = _US_INDEX_WIN
    v1 = [-p["v"] for p in s1][-win:]  # 倒挂越深=分高=不利
    v2 = [-p["v"] for p in s2][-win:]
    p1 = _pct_of(v1, v1[-1])
    p2 = _pct_of(v2, v2[-1])

    m2 = {p["date"]: -p["v"] for p in s2}
    hist = []
    for i, p in enumerate(s1[-win:]):
        if p["date"] in m2:
            hist.append({"date": p["date"], "v": round(
                _pct_of(v1, -p["v"]) * 0.7 + _pct_of(v2, m2[p["date"]]) * 0.3, 1)})

    return _mk_us_index(
        "曲线倒挂",
        [{"label": "10Y − 3M 利差（权重 70%，倒挂为负）", "value": f"{s1[-1]['v']:+.2f}%", "pct": round(p1, 1), "hist": s1[-win:]},
         {"label": "10Y − 2Y 利差（权重 30%）", "value": f"{s2[-1]['v']:+.2f}%", "pct": round(p2, 1), "hist": s2[-win:]}],
        [0.7, 0.3],
        "越高=倒挂越深（越低越有利；纽联储衰退预测口径，<30 正常陡峭，>70 深度倒挂=衰退预警）",
        f"10Y−3M {s1[-1]['v']:+.2f}% · 10Y−2Y {s2[-1]['v']:+.2f}%",
        s1[-1]["date"], hist)


def _us_funding_stress_index(us: dict) -> dict:
    """短端资金压力：SOFR−EFFR 利差(50%) + EFFR−IORB 利差(50%)，越高=融资越紧。

    纽联储 2026 SOMA 年报明确：EFFR-IORB 走窄是准备金从充裕转向充足的关键信号；
    SOFR-EFFR 是回购/无担保市场分化的标准压力计（2019-09 回购危机即此组合飙升）。
    """
    sofr = us.get("sofr", {}).get("hist") or []
    effr = us.get("effr", {}).get("hist") or []
    iorb = us.get("iorb", {}).get("hist") or []
    if len(sofr) < 60 or len(effr) < 60 or len(iorb) < 60:
        return {}
    win = _US_INDEX_WIN
    spread1 = _spread_hist([(p["date"], p["v"]) for p in sofr], [(p["date"], p["v"]) for p in effr])
    spread2 = _spread_hist([(p["date"], p["v"]) for p in effr], [(p["date"], p["v"]) for p in iorb])
    if len(spread1) < 60 or len(spread2) < 60:
        return {}
    h1 = [{"date": d, "v": round(v * 100, 1)} for d, v in spread1][-win:]   # % → bp
    h2 = [{"date": d, "v": round(v * 100, 1)} for d, v in spread2][-win:]
    v1 = [p["v"] for p in h1]
    v2 = [p["v"] for p in h2]
    p1 = _pct_of(v1, v1[-1])
    p2 = _pct_of(v2, v2[-1])

    m2 = {p["date"]: p["v"] for p in h2}
    hist = []
    for p in h1:
        if p["date"] in m2:
            hist.append({"date": p["date"], "v": round(
                _pct_of(v1, p["v"]) * 0.5 + _pct_of(v2, m2[p["date"]]) * 0.5, 1)})

    return _mk_us_index(
        "短端资金压力",
        [{"label": "SOFR − EFFR（权重 50%）", "value": f"{v1[-1]:+.1f} bp", "pct": round(p1, 1), "hist": h1},
         {"label": "EFFR − IORB（权重 50%）", "value": f"{v2[-1]:+.1f} bp", "pct": round(p2, 1), "hist": h2}],
        [0.5, 0.5],
        "越高=回购/无担保融资越紧（越低越有利；纽联储准备金监控口径，<30 宽松，>70 偏紧）",
        f"SOFR−EFFR {v1[-1]:+.1f}bp · EFFR−IORB {v2[-1]:+.1f}bp",
        h1[-1]["date"], hist)


def _us_qt_index(us: dict) -> dict:
    """缩表进程指数：总资产(60%) + 准备金(40%) 20 日变化率，越高=抽水越快（越不利）。

    总资产/准备金下行 = QT 在抽水；上行 = 储备管理购买（RMP）在补水。
    用变化率而非水位：水位受趋势增长影响，变化率才对应"当下紧缩/投放力度"。
    """
    bal = us.get("walcl", {}).get("hist") or []
    res = us.get("reserves", {}).get("hist") or []
    if len(bal) < 40 or len(res) < 40:
        return {}
    win = 60  # 变化率序列较短，窗口取 60 个交易日
    lag = 20

    def _chg_hist(h):
        pts = [{"date": h[i]["date"], "v": round(h[i]["v"] - h[i - lag]["v"], 3)}
               for i in range(lag, len(h))]
        return pts[-win:]

    h1 = _chg_hist(bal)
    h2 = _chg_hist(res)
    if len(h1) < 30 or len(h2) < 30:
        return {}
    v1 = [-p["v"] for p in h1]  # 资产减少=抽水=分高=不利
    v2 = [-p["v"] for p in h2]
    p1 = _pct_of(v1, v1[-1])
    p2 = _pct_of(v2, v2[-1])

    m2 = {p["date"]: p["v"] for p in h2}
    hist = []
    for i, p in enumerate(h1):
        if p["date"] in m2:
            hist.append({"date": p["date"], "v": round(
                _pct_of(v1, -p["v"]) * 0.6 + _pct_of(v2, -m2[p["date"]]) * 0.4, 1)})

    return _mk_us_index(
        "缩表进程",
        [{"label": "美联储总资产 20 日变动（权重 60%）", "value": f"{h1[-1]['v']:+.0f} 亿$", "pct": round(p1, 1), "hist": h1},
         {"label": "银行准备金 20 日变动（权重 40%）", "value": f"{h2[-1]['v'] / 10:+.0f} 亿$", "pct": round(p2, 1), "hist": h2}],
        [0.6, 0.4],
        "越高=缩表/抽水越快（越低越有利；2025-12 起储备管理购买 RMP 转向补水）",
        f"总资产 20 日 {h1[-1]['v']:+.0f} 亿$ · 准备金 20 日 {h2[-1]['v'] / 10:+.0f} 亿$",
        h1[-1]["date"], hist)


def _us_risk_appetite_index(us: dict) -> dict:
    """风险偏好指数：VIX(50%) + 美元指数 20 日变动(30%) + 期限溢价(20%)，越高=避险/收紧。

    VIX 是 NFCI 风险子指数的核心；美元走强与期限溢价走高都是高盛 FCI 里的收紧因子。
    """
    vix = us.get("vix", {}).get("hist") or []
    dxy = us.get("dxy", {}).get("hist") or []
    tp = us.get("term_premium", {}).get("hist") or []
    if len(vix) < 60 or len(dxy) < 60 or len(tp) < 60:
        return {}
    win = _US_INDEX_WIN
    lag = 20
    dxy_chg = [{"date": dxy[i]["date"], "v": round(dxy[i]["v"] - dxy[i - lag]["v"], 2)}
               for i in range(lag, len(dxy))][-win:]
    vv = [p["v"] for p in vix][-win:]
    vd = [p["v"] for p in dxy_chg]
    vt = [p["v"] for p in tp][-win:]
    p1 = _pct_of(vv, vv[-1])   # VIX 高=避险=分高=不利
    p2 = _pct_of(vd, vd[-1])   # 美元走强=收紧=分高=不利
    p3 = _pct_of(vt, vt[-1])   # 期限溢价高=收紧=分高=不利

    md = {p["date"]: p["v"] for p in dxy_chg}
    mt = {p["date"]: p["v"] for p in tp}
    hist = []
    for p in vix[-win:]:
        if p["date"] in md and p["date"] in mt:
            hist.append({"date": p["date"], "v": round(
                _pct_of(vv, p["v"]) * 0.5 + _pct_of(vd, md[p["date"]]) * 0.3 +
                _pct_of(vt, mt[p["date"]]) * 0.2, 1)})

    return _mk_us_index(
        "风险偏好",
        [{"label": "VIX 波动率（权重 50%）", "value": f"{vv[-1]:.1f}", "pct": round(p1, 1), "hist": vix[-win:]},
         {"label": "美元指数 20 日变动（权重 30%）", "value": f"{vd[-1]:+.2f}", "pct": round(p2, 1), "hist": dxy_chg},
         {"label": "10Y 期限溢价（权重 20%）", "value": f"{vt[-1]:.2f}%", "pct": round(p3, 1), "hist": tp[-win:]}],
        [0.5, 0.3, 0.2],
        "越高=市场越避险、美元越强（越低越有利；<30 乐观，>70 避险）",
        f"VIX {vv[-1]:.1f} · 美元指数 20 日 {vd[-1]:+.2f} · 期限溢价 {vt[-1]:.2f}%",
        vix[-1]["date"], hist)


def get_liquidity() -> dict:
    """资金供给指标汇总（独立页面，含历史趋势 + 美联储利率；缓存 5 分钟）。

    last-good 语义：任一源故障只影响该层，整页/各指数回退最近一次成功值（stale 标注），
    指标不再随源波动"时有时无"；冷启动从磁盘快照恢复。
    """
    return _layered_get("liquidity", _liquidity_build,
                        valid=lambda v: bool(v.get("cn") or v.get("us")),
                        warm=_load_liquidity_snapshot)


def _liquidity_build() -> dict:
    def build():
        # --- 国内 ---
        cn = _cn_margin_full()
        flows = _cn_index_flows()
        if flows:
            cn["index_flows"] = flows
            # 全市场合计
            total = sum(f["latest"]["v"] for f in flows.values())
            cn["total_main_net_yi"] = round(total, 1)

        # --- 国外（美国）：FRED 序列（小并发池拉取，比串行快 ~3 倍；3 workers 避免触发限流） ---
        us: dict = {}

        def _fetch_fred(item):
            key, (sid, unit, label) = item
            hist = _fred_series_cached(sid, 500)  # 综合指数分位需要 ~250 交易日 + 缓冲；带 TTL/磁盘兜底
            if not hist:
                return key, None
            latest_val = hist[-1][1]
            if unit == "亿$":
                latest_val = round(latest_val / 100, 1)
                hist = [(d, round(v / 100, 1)) for d, v in hist]
            prev_val = hist[-2][1] if len(hist) > 1 else None
            return key, {
                "label": label,
                "unit": unit,
                "value": round(latest_val, 3),
                "date": hist[-1][0],
                "chg": round(latest_val - prev_val, 3) if prev_val is not None else None,
                "hist": [{"date": d, "v": round(v, 3)} for d, v in hist],
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
            idx = _sub_cached(f"cn_idx:{key}", fn, ttl=300, valid=lambda v: bool(v))
            if idx:
                cn_indices[key] = idx

        # --- 国外（美国）综合指数 ---
        us_indices = {}
        for key, fn in [("credit_stress", _us_credit_stress_index),
                        ("curve", _us_curve_index),
                        ("funding_stress", _us_funding_stress_index),
                        ("qt", _us_qt_index),
                        ("risk_appetite", _us_risk_appetite_index)]:
            idx = _sub_cached(f"us_idx:{key}", lambda fn=fn: _empty_to_none(fn(us)),
                              ttl=300, valid=lambda v: bool(v))
            if idx:
                us_indices[key] = idx

        # --- 美联储加息概率（Kalshi 市场，挂了回退到最近一次的缓存值） ---
        fed_odds = _fed_odds_with_fallback()

        result = {
            "cn": cn,
            "cn_indices": cn_indices,
            "us": us,
            "us_indices": us_indices,
            "fed_odds": fed_odds,
            "updated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        }
        return result
    val = build()
    if val.get("cn") or val.get("us"):
        # 分段合并 last-good：本轮挂掉的段回退最近成功值，不再整段消失；
        # 合并结果同时作为新的 last-good 落盘
        prev = _last_good("liquidity")
        if isinstance(prev, dict):
            for field in ("cn", "cn_indices", "us", "us_indices", "fed_odds"):
                val[field] = _merge(prev.get(field), val.get(field), field)
            val.pop("stale", None)
            val.pop("stale_since", None)
        _save_json(_LIQUIDITY_SNAPSHOT, val)
    return val
