"""新端点统一入口 —— 把 astock_new 的 40 个上游端点暴露成 HTTP /api/extra 与 MCP 工具。

设计取舍：一个参数表 + 一个调度函数，而不是 40 个路由/工具。理由是两个消费方
（HTTP 与 MCP）需要的是同一份「端点名 + 参数签名 + 结果序列化」，逐端点复制一遍
只会让两边漂移；签名由 inspect 从函数本身读，新增上游端点只改 ENDPOINTS 一处。

调用约定：
- 参数一律从字符串进（HTTP query / MCP JSON），按函数签名的注解与默认值强制转换；
- 认不出的参数名直接报错，不静默丢弃（静默丢弃 = 用户以为筛了条件，其实没有）；
- 返回统一是 {endpoint, as_of, kind, ...}，DataFrame 转成 columns/rows 并保留 attrs
  （上游把 coverage / source_url / fallback_reason 这类元信息挂在 attrs 里，丢了就
  没法判断结果是不是完整）；
- ValueError → 参数错（4xx），RuntimeError → 上游不可用或格式漂移（502）。
"""

from __future__ import annotations

import inspect
import math
import time
from datetime import date, datetime
from typing import Any

import pandas as pd

import astock_new as upstream
import gstock_new as overseas

# 端点名 → 中文说明。键必须存在于 astock_new（下面 _check() 会在导入时校验）。
ENDPOINTS: dict[str, str] = {
    # 行情 / 逐笔 / 盘后包
    "tencent_kline": "腾讯 K 线（日/周/月前后复权 + 1~60 分钟；科创板成交量是股、其余是手）",
    "tencent_ticks": "腾讯当日逐笔成交（约 3 秒一笔快照，只有最近一个交易日）",
    "tdx_daily_package": "通达信官网盘后包：某交易日沪深京全市场日线（含成交额）",
    "bse_quote_backup": "北交所官网当前行情快照（只给当前交易日，不回填历史）",
    # 研报 / 资讯
    "sina_research_reports": "新浪研报列表（东财之外的第二来源）",
    "wallstreetcn_lives": "华尔街见闻 7×24 快讯",
    "cctv_news": "新闻联播条目与文字稿",
    # 资金面
    "margin_trading_backup": "沪深交易所官方两融数据（东财之外的官方备胎）",
    "etf_shares": "上交所 / 深交所 ETF 份额",
    # 基础数据 / 舆情
    "st_stock_list": "沪深京 ST / *ST 名单（东财不可达时退 baostock，只覆盖沪深）",
    "sse_e_interaction": "上证e互动问答（补上巨潮互动易不覆盖的沪市）",
    "trading_calendar": "深交所交易日历（整月）",
    # 指数
    "index_constituents": "中证 / 国证指数成分股",
    "index_weights": "中证 / 国证指数权重（百分数）",
    "index_valuation": "中证指数近期 PE 与股息率（不含 PB）",
    # 宏观与利率
    "chinabond_yield_curve": "中债收益率曲线",
    "repo_fixing_rates": "回购定盘利率 FR / FDR",
    "lpr_history": "LPR 全历史",
    "macro_calendar": "全球宏观日历（重要性 1–4）",
    # 期货与大宗
    "futures_daily": "五家期货交易所官方日行情",
    "options_daily": "商品与股指期权日行情",
    "futures_position_rank": "期货持仓排名（上期所 / 上期能源 / 郑商所 / 中金所）",
    "futures_realtime": "新浪实时期货（含大商所品种）",
    "futures_kline": "新浪期货日 K（主力连续或具体合约，含大商所历史）",
    "a50_futures": "富时中国 A50 期货",
    "sge_spot": "上海金交所现货",
    # 事件驱动
    "earnings_forecast": "业绩预告",
    "institution_survey": "机构调研",
    "holder_trades": "股东 / 高管增减持",
    "share_buyback": "股份回购",
    "equity_pledge": "股权质押",
    "ipo_calendar": "新股申购日历",
    # 可转债
    "convertible_bonds": "可转债条款、转股价值与溢价率",
}

# global-stock-data v2.1–v2.3 新增的海外层。单独一张表，因为它们的可用性完全不同：
# BLS / 财政部 / 纽约联储可达，Yahoo 会 429，中金所之类直连不通。混在一起会让
# 「整层没接」与「单个源波动」分不清。
OVERSEAS_ENDPOINTS: dict[str, str] = {
    # 全球利率 / 汇率 / 央行
    "policy_rates": "主要经济体政策利率（BIS 汇总）",
    "ecb_series": "欧央行数据序列（汇率 / 利率等）",
    "ecb_fx": "欧央行官方汇率",
    "ecb_key_rates": "欧央行关键利率",
    "ecb_yield_curve": "欧元区收益率曲线（AAA 等）",
    "nyfed_rates": "纽约联储利率（SOFR 等）",
    "fed_data": "美联储 H 系列发布数据",
    "fiscal_data": "美国财政部 FiscalData（债务等）",
    # 行情
    "stock_kline_yahoo": "Yahoo 全历史 K 线（亚太 / 欧洲 / 美股，交易所当地日历合并周月季线）",
    # 波动率 / 做空 / 持仓
    "vol_index_history": "CBOE 波动率指数历史（VIX / VIX9D / VIX3M / VVIX / SKEW / OVX / GVZ 等）",
    "vix_term_structure": "VIX 期限结构（contango / 倒挂与长短端差）",
    "short_interest_all": "FINRA 全美做空余额（某一期全表）",
    "short_interest": "单只股票最近 N 期做空余额",
    "short_interest_ranking": "做空余额排行（回补天数 / 空头股数 / 环比）",
    "insider_trades": "SEC Form 4 内部人交易明细",
    "holdings_13f": "SEC 13F 机构持仓",
    # 台股 / 能源
    "tw_market": "台湾证交所 / 柜买中心行情",
    "tw_stock": "台股单票行情与基本面",
    "energy_prices": "EIA 能源价格序列",
    "energy_inventories": "EIA 能源库存",
    # 美国宏观
    "bls_series": "BLS 任意序列号（免 key，日限额 25 次）",
    "us_macro": "美国宏观 23 指标一次取齐（CPI / 失业 / 非农 / JOLTS / PPI / ECI 等）",
    "us_macro_latest": "美国宏观各指标最新一期与环比 / 同比 + 下次发布日期",
    "bls_release_calendar": "BLS 发布日程",
    "us_jobless_claims": "劳工部每周初请 / 续请失业金",
}

ENDPOINTS.update(OVERSEAS_ENDPOINTS)
_ASTOCK_NAMES = [n for n in ENDPOINTS if n not in OVERSEAS_ENDPOINTS]
_OWNER: dict[str, Any] = dict.fromkeys(_ASTOCK_NAMES, upstream) | dict.fromkeys(OVERSEAS_ENDPOINTS, overseas)

# 上游有几个参数没写类型注解（或者默认值是 None），靠签名推不出来。这里显式补齐：
# 少一处就让「2026」变字符串传进去，端点在内部比较时静默出错。
_PARAM_TYPES: dict[str, dict[str, type]] = {
    "trading_calendar": {"year": int, "month": int},
}

# 默认 10 分钟；BLS 类单独放宽到 1 小时——免 key 版每天只有 25 次请求、
# 每次 25 个序列、10 年，超了会被封；上游自己的 _bls_cache 也是 1 小时，
# 这一层对上是为了不让同一参数在 10 分钟后又被重新拉一次。
_CACHE_SECONDS = 600
_CACHE_TTL = {"us_macro": 3600, "us_macro_latest": 3600, "bls_series": 3600,
              "bls_release_calendar": 3600}
_cache: dict[tuple, tuple[float, Any]] = {}
_CACHEABLE = {"st_stock_list", "tdx_daily_package", "convertible_bonds", "lpr_history", "etf_shares",
              "us_macro", "us_macro_latest", "bls_series", "bls_release_calendar",
              "us_jobless_claims", "fiscal_data", "fed_data", "short_interest_all"}


def _check() -> None:
    missing = [n for n, mod in _OWNER.items() if not callable(getattr(mod, n, None))]
    if missing:
        raise RuntimeError(f"ENDPOINTS 里有未实现的端点: {', '.join(missing)}")


_check()


def _params_of(name: str) -> dict[str, Any]:
    """端点参数表：{名: {type, required, default, description}}，供 /api/extra 自描述。"""
    sig = inspect.signature(getattr(_OWNER[name], name))
    out: dict[str, Any] = {}
    for pname, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        default = None if p.default is p.empty else p.default
        ann = p.annotation
        override = _PARAM_TYPES.get(name, {}).get(pname)
        kind = "string"
        if override is bool:
            kind = "boolean"
        elif override is int:
            kind = "integer"
        elif override is float:
            kind = "number"
        elif isinstance(default, bool):
            kind = "boolean"
        elif isinstance(default, int):
            kind = "integer"
        elif isinstance(default, float):
            kind = "number"
        elif isinstance(default, (list, tuple)):
            kind = "array"
        elif ann is not p.empty and getattr(ann, "__name__", "") in ("int", "float", "bool"):
            kind = {"int": "integer", "float": "number", "bool": "boolean"}[ann.__name__]
        out[pname] = {
            "type": kind,
            "required": p.default is p.empty,
            "default": default,
            "description": "",
        }
    return out


def describe() -> list[dict[str, Any]]:
    return [
        {"endpoint": name, "description": desc, "params": _params_of(name)}
        for name, desc in ENDPOINTS.items()
    ]


def _coerce(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """按签名把字符串参数转成目标类型；未知参数直接报错（静默丢弃会让用户以为筛了条件）。"""
    sig = inspect.signature(getattr(_OWNER[name], name))
    allowed = {p for p in sig.parameters
               if p != "self" and sig.parameters[p].kind not in (
                   inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)}
    unknown = [k for k in raw if k not in allowed]
    if unknown:
        raise ValueError(f"{name} 不认识参数 {', '.join(unknown)}；可用参数: {', '.join(sorted(allowed))}")

    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or value == "":
            continue
        p = sig.parameters[key]
        default = None if p.default is p.empty else p.default
        override = _PARAM_TYPES.get(name, {}).get(key)
        target = override or (type(default) if default is not None else None)
        if isinstance(value, (list, tuple, dict)):
            kwargs[key] = value
        elif target is bool:
            if isinstance(value, str) and value.lower() in ("0", "false", "no"):
                kwargs[key] = False
            elif isinstance(value, str) and value.lower() in ("1", "true", "yes"):
                kwargs[key] = True
            else:
                raise ValueError(f"{name}.{key} 需要布尔值（true/false）")
        elif target is int:
            try:
                kwargs[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name}.{key} 需要整数，收到 {value!r}") from exc
        elif target is float:
            try:
                kwargs[key] = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name}.{key} 需要数字，收到 {value!r}") from exc
        elif target in (list, tuple):
            kwargs[key] = [v.strip() for v in str(value).split(",") if v.strip()]
        else:
            kwargs[key] = value

    missing = [p for p in allowed
               if sig.parameters[p].default is inspect.Parameter.empty and p not in kwargs]
    if missing:
        raise ValueError(f"{name} 缺少必填参数: {', '.join(missing)}")
    return kwargs


def _num(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if not isinstance(value, float | int | str | bool | type(None)):
        item = getattr(value, "item", None)
        if callable(item):  # numpy 标量
            try:
                return item()
            except Exception:  # noqa: BLE001
                return str(value)
    return value


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, pd.DataFrame):
        return {
            "kind": "table",
            "columns": [str(c) for c in obj.columns],
            "rows": [{str(k): _num(v) for k, v in row.items()}
                     for row in obj.to_dict("records")],
            "attrs": {str(k): _jsonable(v) for k, v in (obj.attrs or {}).items()},
        }
    if isinstance(obj, list):
        return {"kind": "list", "rows": [_jsonable(x) for x in obj]}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_jsonable(x) for x in obj]
    return _num(obj)


def call(name: str, raw_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """调一个端点，返回可 JSON 序列化的结果。"""
    if name not in ENDPOINTS:
        raise ValueError(f"未知端点 {name}；可用: {', '.join(sorted(ENDPOINTS))}")
    kwargs = _coerce(name, raw_params or {})

    key = (name, tuple(sorted((k, str(v)) for k, v in kwargs.items())))
    ttl = _CACHE_TTL.get(name, _CACHE_SECONDS)
    if name in _CACHEABLE:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return {**hit[1], "cached": True}

    fn = getattr(_OWNER[name], name)
    result = _jsonable(fn(**kwargs))
    payload = {
        "endpoint": name,
        "description": ENDPOINTS[name],
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "params": kwargs,
        "result": result,
    }
    if name in _CACHEABLE:
        _cache[key] = (time.time(), payload)
        # 缓存只装整表结果，条数很有限；超了就按时间清最旧的一半
        if len(_cache) > 32:
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:16]:
                _cache.pop(k, None)
    return payload
