"""AI 工具层 —— 把后端已有的全部客观数据能力暴露成 function-calling 工具。

设计原则：
- **只给客观数据**：每个工具返回的都是公开可查的事实（行情/财报/资金/公告/板块），
  不含任何评分、排名倾向、买卖建议或预测。结论一律由用户自己配置的模型给出。
- **裁剪后再喂**：原始接口动辄上百条（资金流 120 天、互动易 30 条），直接塞进上下文
  会把 token 烧光且淹没重点。每个工具在这里做「取最近 N 条 + 关键字段 + 汇总统计」，
  让模型拿到的是能直接推理的密度，而不是原始转储。
- **失败不抛**：任何异常都转成 {"error": ...} 回喂给模型，让它换个工具继续，不中断对话循环。

chat.py / mcp_server.py 共用本模块，新增工具只需改这里一处。
"""

from __future__ import annotations

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import astock
import gstock
import market
import newsradar
import cache_runtime
import bonds
import gold_score
import sector_scores
import sw_level2_scores

# ——— schema 简写：让 20+ 个工具定义保持一屏可读 ———

_CODE = {"code": {"type": "string", "description": "6 位 A 股代码，如 600519"}}


def _t(name: str, desc: str, props: dict | None = None, required: list[str] | None = None,
       example: str = "") -> dict:
    if example:
        desc = f"{desc}\n\n示例：{example}"
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []},
        },
    }


def _pick(rows: list[dict], keys: tuple[str, ...] | None, limit: int) -> list[dict]:
    """取前 limit 条（控 token）；keys 为 None 时保留全部字段，只截条数。"""
    head = (rows or [])[:limit]
    if keys is None:
        return [r for r in head if isinstance(r, dict)]
    return [{k: r.get(k) for k in keys} for r in head if isinstance(r, dict)]


TOOLS: list[dict] = [
    # —— 行情与估值 ——
    _t("query_quote", "查 A 股实时行情：现价/涨跌/PE/PB/市值/换手/涨跌停。可批量。",
       {"codes": {"type": "array", "items": {"type": "string"}, "description": "6 位代码列表，如 ['600519','000858']"}},
       ["codes"],
       "查贵州茅台和五粮液现在多少钱 → query_quote(codes=['600519','000858'])"),
    _t("query_valuation", "查单只个股的完整估值：行情 + 机构一致预期 EPS + 前向 PE / PEG / PE 消化年数。",
       _CODE, ["code"],
       "宁德时代按明年盈利算贵不贵 → query_valuation(code='300750')"),
    _t("query_valuation_percentile",
       "查个股 PE-TTM / PB 的历史估值分位：当前值 + 近五年 20/50/80 分位带 + 当前所处百分位。判断估值贵贱先用这个。",
       _CODE, ["code"],
       "长江电力现在的估值在历史上算什么水平 → query_valuation_percentile(code='600900')"),
    _t("query_kline",
       "查个股 K 线并附区间统计（起止价、区间涨跌幅、最高/最低、振幅）。判断价格位置与趋势用。",
       {**_CODE,
        "period": {"type": "string", "enum": ["day", "week", "month"], "description": "周期，默认 day"},
        "count": {"type": "integer", "description": "取最近多少根，默认 60，最大 250"}},
       ["code"],
       "看中远海控最近半年周线走势 → query_kline(code='601919', period='week', count=26)"),

    # —— 基本面 ——
    _t("query_financials",
       "查个股最新报告期财务关键指标：营收/净利及同比、ROE、毛利率、净利率、每股经营现金流、EPS。",
       _CODE, ["code"],
       "爱美客最新一季报赚了多少、毛利率多高 → query_financials(code='300896')"),
    _t("query_company_info", "查公司基本概况：所属行业、总股本/流通股、上市日期等。", _CODE, ["code"],
       "这家公司是做什么的、盘子多大 → query_company_info(code='002594')"),
    _t("query_reports", "查个股近期研报列表（标题/机构/评级/日期）。", _CODE, ["code"],
       "卖方最近怎么评论恒瑞医药 → query_reports(code='600276')"),
    _t("query_news", "查个股近期新闻（标题/时间/来源）。", _CODE, ["code"],
       "三一重工最近有什么动静 → query_news(code='600031')"),

    # —— 资金面与筹码 ——
    _t("query_fund_flow",
       "查个股资金流向：最近若干日主力/超大单/大单/中单/小单净流入，并附近 5 日、20 日累计主力净额。",
       {**_CODE, "days": {"type": "integer", "description": "明细返回最近多少日，默认 10，最大 60"}},
       ["code"],
       "紫金矿业的钱最近在流入还是流出 → query_fund_flow(code='601899')"),
    _t("query_margin", "查个股融资融券：融资余额、融资买入/偿还、融券余额趋势（最近若干期）。", _CODE, ["code"],
       "杠杆资金对中信证券的态度 → query_margin(code='600030')"),
    _t("query_holders", "查个股股东户数变化（户数增减 = 筹码集中或分散的直接证据）。", _CODE, ["code"],
       "筹码在向少数人集中还是扩散 → query_holders(code='000858')"),
    _t("query_block_trade", "查个股大宗交易记录：成交价、折溢价率、成交量、买卖营业部。", _CODE, ["code"],
       "最近谁在大宗折价甩卖 → query_block_trade(code='601012')"),
    _t("query_dragon_tiger", "查个股龙虎榜：近 30 日上榜记录、最近一次买卖席位 TOP5、机构专用席位净买额。", _CODE, ["code"],
       "涨停是游资拉的还是有机构参与 → query_dragon_tiger(code='002104')"),
    _t("query_dividend", "查个股历史分红方案：每股派息、股息率、除权除息日、分红进度。", _CODE, ["code"],
       "工商银行历年分红与股息率 → query_dividend(code='601398')"),

    # —— 事件与风险 ——
    _t("query_announcements", "查个股近期公告（标题/日期/类型）。查风险与重大事项先用这个。", _CODE, ["code"],
       "最近有没有减持/并购/业绩预告 → query_announcements(code='601899')"),
    _t("query_lockup", "查个股限售解禁：历史解禁记录 + 未来 90 天待解禁事件（日期/类型/股数/占比）。", _CODE, ["code"],
       "未来三个月有多少限售股要解禁 → query_lockup(code='688981')"),
    _t("query_investor_qa", "查个股投资者互动易问答（公司对投资者提问的官方回复，常含经营细节）。", _CODE, ["code"],
       "公司在互动易上怎么回应订单问题 → query_investor_qa(code='002460')"),

    # —— 行业与板块 ——
    _t("query_concepts", "查个股所属板块与概念归属，以及当下被市场归到哪些热门概念在炒。", _CODE, ["code"],
       "这家公司蹭上了哪些概念 → query_concepts(code='000977')"),
    _t("query_industry_comparison", "查全市场行业板块横向对比：各行业涨跌幅、成交额、领涨股。看板块强弱用。",
       {"top_n": {"type": "integer", "description": "返回前 N 个行业，默认 20"}},
       example="今天哪些行业领涨、哪些垫底 → query_industry_comparison(top_n=10)"),
    _t("query_industry_reports", "按关键词查行业研报（非个股），了解卖方对某赛道的最新覆盖。",
       {"keywords": {"type": "array", "items": {"type": "string"}, "description": "行业关键词，如 ['光模块','算力']"},
        "days": {"type": "integer", "description": "回溯天数，默认 90"}},
       example="卖方怎么看 AI 算力赛道 → query_industry_reports(keywords=['算力','光模块'])"),

    # —— 市场层 ——
    _t("query_market",
       "查大盘与市场情绪。scope: indices=A股指数 / global=全球指数 / emotion=短线情绪(连板梯队/封板率) / turnover=全市场成交额 TOP20 / overview=大盘总览(指数+情绪+板块资金流)。",
       {"scope": {"type": "string", "enum": ["indices", "global", "emotion", "turnover", "overview"],
                  "description": "要查的范围，默认 overview"}},
       example="今天市场整体怎么样 → query_market(scope='overview')"),
    _t("query_news_radar",
       "查资讯雷达：12 条赛道的行业资讯聚合（非个股新闻，看产业面动态用）。可传 track 只看某条赛道（如「半导体」「AI」）。",
       {"track": {"type": "string", "description": "赛道名关键词，留空看全部"},
        "per_track": {"type": "integer", "description": "每条赛道取最新几条，默认 5"}},
       example="半导体产业最近有什么动态 → query_news_radar(track='半导体')"),
    _t("search_public_news",
       "联网搜索公开资料，用于核验已经识别出的重大事件。仅当现有公告/新闻指向重大变化且资料不足时调用一次，不要用于普通事件或批量扫标的。",
       {"query": {"type": "string", "description": "精确查询词，包含公司/板块名和重大事件关键词"},
        "count": {"type": "integer", "description": "返回条数，默认 5，最大 8"}},
       ["query"],
       "核实「某公司获得大额订单」传闻 → search_public_news(query='公司名 大额订单')"),

    # —— 海外 ——
    _t("query_global_stock",
       "查美股 / 港股 / 韩股个股：行情 + 关键财务指标（韩股仅行情）。美股用字母代码(AAPL)，港股用数字(00700)，韩股 6 位数字加 .KS(005930.KS)。",
       {"symbol": {"type": "string", "description": "美股字母代码 / 港股代码 / 韩股 XXXXXX.KS"}},
       ["symbol"],
       "英伟达现在的情况 → query_global_stock(symbol='NVDA')"),
    _t("query_hk_cashflow",
       "查港股现金流量表：经营/投资/筹资活动现金流净额、现金及等价物净增加、期初/期末现金，多期、附同比。仅港股，代码用数字如 00700。",
       {"symbol": {"type": "string", "description": "港股代码，如 00700"}},
       ["symbol"],
       "腾讯经营现金流趋势 → query_hk_cashflow(symbol='00700')"),

    # —— 市场研判层（各页面的合成分与分解，客观数据非建议） ——
    _t("query_macro_composite",
       "查宏观总分：8 模块合成分 + 各模块得分/权重/贡献分解 + 近 3 年总分走势。看整体宏观环境冷热用。",
       example="当前宏观环境综合处于什么状态、由哪些模块驱动 → query_macro_composite()"),
    _t("query_liquidity_composite",
       "查中美流动性综合得分：合成分/状态 + 各分项（银行间资金压力/政策利率/债市/杠杆温度等）得分与贡献。看资金面松紧用。",
       example="现在流动性是松是紧、结构上谁在拖累 → query_liquidity_composite()"),
    _t("query_sector_scores",
       "查行业评分（申万一级）：各行业综合得分/所处阶段/估值分位/动量等，历史回测口径。看行业景气与轮动位置用。",
       {"top_n": {"type": "integer", "description": "返回前 N 个行业，默认 15"}},
       example="哪些行业评分处于景气高位、哪些在低位 → query_sector_scores(top_n=15)"),
    _t("query_gold_score",
       "查黄金多维评分：总分/信号 + 五维（机会成本/资金仓位/避险/结构性需求/趋势确认）+ 正负贡献因子。",
       example="黄金现在什么评分、哪些因子在拖累 → query_gold_score()"),
    _t("query_bonds_curve",
       "查中债国债收益率曲线（3M-30Y 关键期限）+ 期限利差（10Y-1Y 等）+ AAA 信用利差的历史序列。",
       example="当前收益率曲线形态、期限利差在什么位置 → query_bonds_curve()"),
    _t("query_bonds_overview",
       "查债市全景快照：收益率曲线与关键期限日变动 + Shibor 资金利率（O/N-1Y 各期限）+ LPR 政策利率锚 "
       "+ 中债新综合指数 + 中美 10Y 利差。问「债市现在什么状态」先用这个。",
       example="债市整体什么状态、资金面紧不紧 → query_bonds_overview()"),
    _t("query_bonds_framework",
       "查中国债市研究框架八状态仪表盘：宏观与通胀/政策与融资/资金面/供需与机构行为/曲线与风险补偿/信用利差/"
       "仓位与拥挤度/海外与汇率，各状态 [-2,+2] 评分（正分=对债券价格有利）并附所用指标与分位。"
       "研究框架见站内 china_bond_research_framework：宏观状态→政策反应→资金与融资→供需与机构行为→曲线与溢价。",
       example="债市框架八个状态各自什么读数、哪些偏多哪些偏空 → query_bonds_framework()"),
    _t("query_bonds_calc",
       "查债市计算层：各关键期限（3M-30Y）3 个月持有期的 carry（票息-资金成本）/ roll（骑乘）/ 静态合计 / "
       "盈亏平衡收益率上行幅度，单位 bp，由当期中债曲线确定性推导。",
       example="现在哪个期限的 carry+roll 最厚、能扛多少收益率上行 → query_bonds_calc()"),
    _t("query_bonds_positioning",
       "查债市仓位与拥挤度：国债期货四品种（TS/TF/T/TL）主力持仓量、成交量及各自近一年分位。"
       "持仓高分位 + 价格高位 = 久期拥挤的常用代理（框架 §9.2）。",
       example="国债期货持仓是不是处于高位、久期交易挤不拥挤 → query_bonds_positioning()"),
    _t("query_bonds_segments",
       "查债市分品种评分：短债(1-3Y)/中短债(3-5Y)/长债(5-10Y)/超长债(20Y+)/信用债(AAA)/杠杆套息 六个品种，"
       "按框架 §11.2 多期限权重先验对八状态加权得出 [-2,+2] 相对分，附前三大驱动、carry+roll 静态锚与各自失效条件。"
       "问「该配短债还是长债、信用还是利率」用这个。",
       example="现在短端和长端哪个更值得配、信用债评分如何 → query_bonds_segments()"),
]

TOOL_NAMES = [t["function"]["name"] for t in TOOLS]


# ——— 各工具的执行实现（裁剪逻辑集中在这里） ———

_TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _kline_tencent(code: str, period: str, n: int) -> list[dict]:
    """腾讯前复权 K 线（备用源）。

    mootdx 走 TCP 7709，在部分网络下连不通（实测本机返回空）；东财 push2his 的 kline 路径
    也可能被拦。腾讯 HTTP 接口实测不封 IP（项目数据源分层里的首选行情源），拿它兜底。
    返回字段顺序：日期, 开, 收, 高, 低, 成交量。
    """
    import requests

    prefix = astock.get_prefix(code)
    sym = f"{prefix}{code}"
    r = requests.get(_TENCENT_KLINE, params={"param": f"{sym},{period},,,{n},qfq"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
    d = (r.json().get("data") or {}).get(sym) or {}
    raw = d.get("qfq" + period) or d.get(period) or []
    out = []
    for it in raw:
        if not isinstance(it, list) or len(it) < 6:
            continue
        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        out.append({"date": it[0], "open": _f(it[1]), "close": _f(it[2]),
                    "high": _f(it[3]), "low": _f(it[4]), "volume": _f(it[5])})
    return out


def _kline(args: dict):
    period = str(args.get("period") or "day")
    if period not in ("day", "week", "month"):
        period = "day"
    cat = {"day": 4, "week": 5, "month": 6}[period]
    n = max(5, min(int(args.get("count") or 60), 250))
    code = str(args["code"])
    # 腾讯优先：HTTP、实测不封 IP、亚秒级返回；mootdx 走 TCP 7709，连不通时要等十几秒超时
    # （实测本机就是这种情况），放在后面当备份而不是主路径。
    try:
        rows = _kline_tencent(code, period, n)
    except Exception:  # noqa: BLE001 — 网络问题转备用源
        rows = []
    if not rows:
        try:
            rows = astock.kline(code, category=cat, offset=n)
        except Exception:  # noqa: BLE001
            rows = []
    if not rows:
        return {"error": "K 线数据源当前不可达（mootdx 与备用源均无返回）"}
    closes = [r.get("close") for r in rows if isinstance(r.get("close"), (int, float))]
    highs = [r.get("high") for r in rows if isinstance(r.get("high"), (int, float))]
    lows = [r.get("low") for r in rows if isinstance(r.get("low"), (int, float))]
    stat = {}
    if closes:
        first, last = closes[0], closes[-1]
        stat = {
            "bars": len(rows), "first_close": first, "last_close": last,
            "change_pct": round((last - first) / first * 100, 2) if first else None,
            "highest": max(highs) if highs else None, "lowest": min(lows) if lows else None,
        }
        if stat["highest"] and stat["lowest"] and stat["lowest"]:
            stat["amplitude_pct"] = round((stat["highest"] - stat["lowest"]) / stat["lowest"] * 100, 2)
            stat["drawdown_from_high_pct"] = round((last - stat["highest"]) / stat["highest"] * 100, 2)
    # 明细只回最近 30 根，避免长周期请求把上下文撑爆
    detail = _pick(rows[-30:], ("date", "open", "close", "high", "low", "volume"), 30)
    return {"summary": stat, "recent": detail}


_FFLOW_DELAY = "https://push2delay.eastmoney.com/api/qt/stock/fflow/daykline/get"

# 主源（东财 push2his）断连时的历史序列快照：成功时落盘、失败时回补，
# 避免第三方源再挂时近 5/20/60 日累计整体缺失（同 market.py 指数流向的思路）。
_FFLOW_SNAPSHOT = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "stock_fflow_snapshot.json")


def _fflow_load_snapshot() -> dict:
    try:
        with open(_FFLOW_SNAPSHOT, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and all(isinstance(v, list) for v in d.values()):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _fflow_save_snapshot(code: str, rows: list[dict]) -> None:
    snap = _fflow_load_snapshot()
    snap[code] = rows[-120:]
    try:
        os.makedirs(os.path.dirname(_FFLOW_SNAPSHOT), exist_ok=True)
        tmp = _FFLOW_SNAPSHOT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, _FFLOW_SNAPSHOT)
    except OSError:
        pass


def _fund_flow_sina(code: str) -> list[dict]:
    """历史资金流（第二源：新浪）。

    东财 push2his 在部分网络下持续断连（TCP 通、应用层直接掐），push2delay 只有当天 1 条；
    新浪这条线路能拿约 60 个交易日的历史。字段口径不同：r0=超大单 r1=大单 r2=中单 r3=小单，
    主力 = r0_net + r1_net（与东财口径一致）。
    """
    import requests

    daima = ("sh" if code.startswith("6") else "sz") + code
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "MoneyFlow.ssl_qsfx_lscjfb")
    d = requests.get(url, params={"page": 1, "num": 60, "sort": "opendate", "asc": 0,
                                  "daima": daima},
                     headers={"User-Agent": astock.UA,
                              "Referer": "https://finance.sina.com.cn/"}, timeout=12).json()
    out = []
    for r in d or []:
        try:
            out.append({
                "date": r["opendate"],
                "main_net": float(r["r0_net"]) + float(r["r1_net"]),
                "super_net": float(r["r0_net"]), "large_net": float(r["r1_net"]),
                "mid_net": float(r["r2_net"]), "small_net": float(r["r3_net"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    out.reverse()  # 接口按日期倒序，翻成正序对齐东财
    return out


def _fund_flow_today(code: str) -> list[dict]:
    """当日资金流（东财延迟行情线路）。

    push2delay 稳定可达，代价是只给当天一条、拿不到历史。放在新浪之后，
    专门用来覆盖第三方源尚未更新的当日末点。
    """
    import requests

    secid = f"{1 if code.startswith('6') else 0}.{code}"
    params = {"secid": secid, "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
              "lmt": "120", "klt": "101"}
    headers = {"User-Agent": astock.UA, "Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    d = requests.get(_FFLOW_DELAY, params=params, headers=headers, timeout=12).json()
    out = []
    for line in (d.get("data") or {}).get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return 0.0
        out.append({"date": p[0], "main_net": _f(p[1]), "small_net": _f(p[2]),
                    "mid_net": _f(p[3]), "large_net": _f(p[4]), "super_net": _f(p[5])})
    return out


def _fund_flow(args: dict):
    code = str(args["code"])
    rows = astock.stock_fund_flow_120d(code)
    source = "东方财富"
    if rows:
        _fflow_save_snapshot(code, rows)
    else:
        # 第二源：新浪历史 60 日；末点用东财延迟线路补当天（新浪当日晚上才更新）
        try:
            rows = _fund_flow_sina(code)
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            source = "新浪财经"
            try:
                today = _fund_flow_today(code)
                if today and today[-1]["date"] > rows[-1]["date"]:
                    rows.append(today[-1])
            except Exception:  # noqa: BLE001
                pass
        # 第三层兜底：东财延迟线路（仅当天）+ 本地快照历史拼接
        if not rows:
            try:
                rows = _fund_flow_today(code)
            except Exception:  # noqa: BLE001
                rows = []
            cached = _fflow_load_snapshot().get(code, [])
            if rows and cached and cached[-1]["date"] <= rows[-1]["date"]:
                rows = [r for r in cached if r["date"] < rows[-1]["date"]] + rows
                source = "东方财富(延迟)+本地缓存"
            elif rows:
                source = "东方财富(延迟)"
            elif cached:
                rows = cached  # 全部外源不可达，至少给上次成功的历史
                source = "本地缓存"
    # 只要有 ≥1 天新数据就续写快照：主源长期断连时靠新浪/延迟线路把历史攒起来
    if rows:
        cached = _fflow_load_snapshot().get(code, [])
        if not cached or rows[-1]["date"] > cached[-1]["date"]:
            _fflow_save_snapshot(code, rows)
    if not rows:
        return {"error": "无资金流数据（东财/新浪均不可达且无本地缓存）"}
    days = max(1, min(int(args.get("days") or 10), 60))
    tail = rows[-days:]
    def _sum(n: int) -> float:
        return round(sum(r.get("main_net", 0) for r in rows[-n:]) / 1e8, 3)
    return {
        "unit": "元（汇总项单位：亿元）", "source": source,
        "main_net_5d_yi": _sum(5), "main_net_20d_yi": _sum(20), "main_net_60d_yi": _sum(60),
        "recent": _pick(tail, ("date", "main_net", "super_net", "large_net", "mid_net", "small_net"), days),
    }


def _concepts(args: dict):
    code = str(args["code"])
    blocks = astock.concept_blocks(code)
    try:
        hot = astock.hot_concepts(code)
    except Exception:  # noqa: BLE001 — 热门概念是加分项，挂了不该拖垮板块归属
        hot = []
    return {
        "total_blocks": blocks.get("total", 0),
        "blocks": _pick(blocks.get("boards", []), ("name", "change_pct", "lead_stock"), 30),
        "hot_concepts": _pick(hot, ("concept", "hit"), 15),
    }


def _company_info(args: dict):
    """公司概况。akshare 的东财概况接口时好时坏，挂了就用腾讯行情 + 板块归属拼一份降级版，
    保证这个工具任何时候都能给出「这家公司是干什么的、多大体量」，而不是一个报错。"""
    code = str(args["code"])
    try:
        info = astock.individual_info(code)
        if info:
            return info
    except Exception:  # noqa: BLE001 — 上游接口不稳，转降级源
        pass
    q = (astock.tencent_quote([code]) or {}).get(code) or {}
    if not q:
        return {"error": "公司概况数据源当前不可达"}
    industry = ""
    try:
        boards = (astock.concept_blocks(code).get("boards") or [])
        industry = boards[0].get("name", "") if boards else ""
    except Exception:  # noqa: BLE001 — 行业是加分项，拿不到不影响主体
        pass
    return {
        "name": q.get("name"), "code": code, "industry_or_board": industry,
        "total_mcap_yi": q.get("mcap_yi"), "float_mcap_yi": q.get("float_mcap_yi"),
        "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
        "note": "概况接口暂不可用，以上为行情源降级数据（市值单位：亿元）",
    }


def _investor_qa(args: dict):
    """互动易：公司回复常有整段公文，截断后再喂，否则十几条就能吃掉整个上下文。"""
    rows = astock.investor_qa(str(args["code"]))
    out = []
    for r in _pick(rows, None, 12):
        q, a = (r.get("question") or ""), (r.get("answer") or "")
        out.append({
            "ask_time": r.get("ask_time"),
            "question": q[:200],
            "answer": a[:400] if a else "（未回复）",
        })
    return out


def _market(args: dict):
    scope = str(args.get("scope") or "overview")
    if scope == "indices":
        return astock.index_quote()
    if scope == "global":
        return market.get_global_indices()
    if scope == "emotion":
        d = market.get_short_term_emotion() or {}
        return {k: d.get(k) for k in ("tiers", "limitUp", "limitDown", "brokenRate", "promoteRate", "updated") if k in d} or d
    if scope == "turnover":
        d = market.get_turnover_top() or {}
        # 字段名必须与 astock.market_turnover_rank() 的实际返回一致：
        # price / pct / amount / mcap / float_cap / industry。此前写的是
        # turnover / changePct，这两个键根本不存在，_pick 全部取到 None——
        # 返回的每条只剩 name 和 code，其余字段一片空白（#28）。
        return {
            "stocks": _pick(
                d.get("stocks", []),
                ("name", "code", "price", "pct", "amount", "mcap", "float_cap", "industry"),
                20,
            ),
            "updated": d.get("updated"),
        }
    return market.get_overview()


def _radar(args: dict):
    """资讯雷达：数据按 12 条赛道分组，这里摊平成一张扁平清单（每条带赛道名）方便模型阅读。
    可传 track 只看某条赛道；每赛道取最新若干条，避免 12×几十条把上下文吃光。"""
    d = newsradar.get_radar(force=False) or {}
    want = str(args.get("track") or "").strip()
    per = max(1, min(int(args.get("per_track") or 5), 20))
    out, total = [], 0
    for ind in d.get("industries") or []:
        name = ind.get("name", "")
        items = ind.get("items") or []
        total += len(items)
        if want and want not in name:
            continue
        for it in items[:per]:
            out.append({"track": name, "title": it.get("title"),
                        "time": it.get("time"), "source": it.get("source")})
    return {"generated_at": d.get("generated_at"), "total_cached": total,
            "tracks": [i.get("name") for i in (d.get("industries") or [])], "items": out}


def public_news_search(query: str, count: int = 5) -> dict:
    """Bing RSS 公开网页搜索：固定目标域名、只取摘要，不抓任意结果页正文。"""
    q = re.sub(r"\s+", " ", str(query or "")).strip()[:160]
    if not q:
        return {"query": "", "results": []}
    limit = max(1, min(int(count or 5), 8))
    def build():
        import requests
        response = requests.get(
            "https://www.bing.com/search",
            params={"q": q, "format": "rss", "setlang": "zh-hans"},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Market-Workbench/1.1; +https://github.com/FLIER001/Market-Workbench)",
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            timeout=15,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content[:1_000_000])
        results = []
        for item in root.findall(".//item"):
            title = html.unescape(re.sub(r"<[^>]+>", "", item.findtext("title") or "")).strip()
            link = html.unescape((item.findtext("link") or "").strip())
            parsed = urlparse(link)
            if not title or parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            snippet = html.unescape(re.sub(r"<[^>]+>", "", item.findtext("description") or ""))
            results.append({
                "title": title[:240], "url": link,
                "snippet": re.sub(r"\s+", " ", snippet).strip()[:500],
                "published": (item.findtext("pubDate") or "").strip(),
                "source": parsed.netloc.lower().removeprefix("www."),
            })
            if len(results) >= limit:
                break
        return {"query": q, "results": results}

    value = cache_runtime.get(
        f"public_news:{q.lower()}:{limit}", build,
        valid=lambda data: isinstance(data.get("results"), list),
        ttl=1800, decorate=False,
    )
    if "results" not in value:
        raise RuntimeError(value.get("refresh_error") or "公开资料搜索暂不可用")
    return value


def _public_news_search(args: dict):
    return public_news_search(str(args.get("query") or ""), int(args.get("count") or 5))


# name -> 执行函数。绝大多数是「调后端函数 + 裁剪」，复杂的抽成上面的私有函数。
def _macro_composite(args: dict):
    """宏观总分：合成分 + 模块贡献分解 + 近 3 年走势（hist 只留 date/v，指标明细不进上下文）。"""
    d = market.get_macro() or {}
    comp = d.get("composite") or {}
    hist = [(p.get("date"), p.get("v")) for p in (comp.get("hist") or []) if isinstance(p, dict)]
    modules = [
        {k: m.get(k) for k in ("name", "score", "desc", "coverage", "confidence")}
        for m in (d.get("modules") or []) if isinstance(m, dict)
    ]
    out = {
        "as_of": d.get("updated"),
        "score": comp.get("score"), "state": comp.get("state"),
        "drivers": comp.get("drivers"),
        "parts": comp.get("parts"),
        "modules": modules,
        "hist_recent": hist[-40:],
    }
    return out or {"error": "宏观数据暂不可用"}


def _liquidity_composite(args: dict):
    """中美流动性综合得分：合成分/状态 + 分项贡献 + 近期走势。"""
    d = market.get_liquidity() or {}
    out = {"as_of": d.get("assembled_at") or d.get("updated")}
    for side, key in (("cn", "cn_composite"), ("us", "us_composite")):
        comp = d.get(key) or {}
        if comp:
            hist = [(p.get("date"), p.get("v")) for p in (comp.get("hist") or []) if isinstance(p, dict)]
            out[side] = {
                "score": comp.get("score"), "state": comp.get("state"),
                "desc": comp.get("desc"), "parts": comp.get("parts"),
                "hist_recent": hist[-40:],
            }
    if len(out) <= 1:
        return {"error": "流动性数据暂不可用"}
    return out


def _sector_scores(args: dict) -> dict:
    """行业评分：每行只留得分/阶段/估值分位等关键字段。"""
    d = sector_scores.get_sector_scores() or {}
    industries = d.get("industries") or []
    top_n = max(1, min(int(args.get("top_n") or 15), 31))
    def _row(ind: dict) -> dict:
        val = ind.get("valuation") or {}
        return {
            "name": ind.get("name"), "score": ind.get("score"), "phase": ind.get("phase"),
            "latest_return": ind.get("latest_return"),
            "pe_pct": val.get("pe_pct"), "pe": val.get("pe"),
        }
    rows = [_row(i) for i in industries if isinstance(i, dict)]
    return {
        "as_of": d.get("as_of"), "source": d.get("current_source_label"),
        "industries": rows[:top_n],
    } or {"error": "行业评分暂不可用"}


def _gold_score(args: dict):
    """黄金评分：总分/信号 + 五维得分 + 正负贡献因子各取前 5。"""
    d = gold_score.get_gold_score() or {}
    if not d.get("gold_score"):
        return {"error": "黄金评分暂不可用"}
    def _drv(items):
        return [{k: x.get(k) for k in ("name", "score", "weight", "desc")} for x in (items or [])[:5] if isinstance(x, dict)]
    return {
        "date": d.get("date"), "score": d.get("gold_score"),
        "signal": d.get("signal"), "confidence": d.get("confidence"),
        "coverage": d.get("coverage"),
        "top_positive_drivers": _drv(d.get("top_positive_drivers")),
        "top_negative_drivers": _drv(d.get("top_negative_drivers")),
    }


def _bonds_curve(args: dict):
    """债市曲线：当期整条曲线 + 各利差只留最新值与近 40 点走势。"""
    d = bonds.get_curve() or {}
    if not d.get("curve"):
        return {"error": "债市数据暂不可用"}
    def _tail(series: dict) -> dict:
        return {k: v[-40:] for k, v in (series or {}).items()}
    return {
        "date": d.get("date"), "curve": d.get("curve"),
        "spreads": _tail(d.get("spreads")), "credit": _tail(d.get("credit")),
        "source": d.get("source"),
    }


def _bonds_framework(args: dict):
    """八状态仪表盘：模型侧裁掉 hist 趋势（页面专用），只留分位/权重/单项分。"""
    d = bonds.get_framework() or {}
    states = d.get("states") or []
    if not states:
        return {"error": "债市框架暂不可用"}
    slim = []
    for s in states:
        parts = [{k: p.get(k) for k in ("key", "label", "pct", "score", "weight")}
                 for p in (s.get("parts") or [])]
        slim.append({k: s.get(k) for k in ("key", "name", "score", "meaning")} | {"parts": parts})
    return {
        "date": d.get("date"), "states": slim, "coverage": d.get("coverage"),
        "method": d.get("method"),
    }


def _bonds_overview(args: dict):
    """债市全景：各子块只留最新值 + 日/月变动，序列只带迷你走势，控 token。"""
    d = bonds.get_overview() or {}

    def _last(series):
        pts = series or []
        return pts[-1]["v"] if pts else None

    def _chg(series, days=1):
        pts = series or []
        return round((pts[-1]["v"] - pts[-1 - days]["v"]) * 100, 1) if len(pts) > days else None

    def _mini(series, n=30):
        pts = (series or [])[-n:]
        return pts if len(pts) > 1 else None

    out: dict = {}
    curve = d.get("curve") or {}
    if curve.get("curve"):
        yields = curve.get("yields") or {}
        out["curve"] = {
            "date": curve.get("date"),
            "points": curve["curve"],
            "daily_chg_bp": {k: _chg(v) for k, v in yields.items() if _chg(v) is not None},
            "spreads_bp": {k: _last(v) for k, v in (curve.get("spreads") or {}).items()},
            "credit_spreads_bp": {k: _last(v) for k, v in (curve.get("credit") or {}).items()},
        }
    funding = d.get("funding") or {}
    if funding.get("series"):
        out["funding"] = {
            "date": funding.get("date"),
            "shibor": {k: {"value": _last(v), "chg_bp": _chg(v)}
                       for k, v in funding["series"].items()},
        }
    policy = d.get("policy") or {}
    if policy.get("anchors"):
        out["policy"] = {"date": policy.get("date"), "anchors": policy["anchors"]}
    index = d.get("index") or {}
    if index.get("series"):
        out["index"] = {"date": index.get("date"), "value": _last(index["series"]),
                        "trend": _mini(index["series"], 60)}
    globe = d.get("global") or {}
    if globe.get("series"):
        out["global"] = {"date": globe.get("date"),
                         "cn_10y": globe["series"][-1]["cn"],
                         "us_10y": globe["series"][-1]["us"],
                         "spread_bp": _last(globe.get("spread")),
                         "spread_trend": _mini(globe.get("spread"), 60)}
    return out or {"error": "债市数据暂不可用"}


_HANDLERS = {
    "query_quote": lambda a: astock.tencent_quote([str(c) for c in a.get("codes", [])]),
    "query_valuation": lambda a: astock.full_valuation(str(a["code"])),
    "query_valuation_percentile": lambda a: astock.valuation_percentile(str(a["code"])),
    "query_kline": _kline,
    "query_financials": lambda a: astock.financials(str(a["code"])),
    "query_company_info": _company_info,
    "query_reports": lambda a: _pick(astock.eastmoney_reports(str(a["code"]), max_pages=1),
                                     ("title", "publishDate", "orgSName", "emRatingName"), 15),
    "query_news": lambda a: _pick(astock.stock_news(str(a["code"]), limit=15),
                                  ("新闻标题", "发布时间", "文章来源"), 15),
    "query_fund_flow": _fund_flow,
    "query_margin": lambda a: _pick(astock.margin_trading(str(a["code"])),
                                    ("date", "rzye", "rzmre", "rzche", "rqye", "rzrqye"), 15),
    "query_holders": lambda a: _pick(astock.holder_num_change(str(a["code"])), None, 10),
    "query_block_trade": lambda a: _pick(astock.block_trade(str(a["code"])), None, 15),
    "query_dragon_tiger": lambda a: astock.dragon_tiger_board(str(a["code"])),
    "query_dividend": lambda a: _pick(astock.dividend_history(str(a["code"])), None, 12),
    "query_announcements": lambda a: _pick(astock.announcements(str(a["code"])), ("title", "date", "type"), 15),
    "query_lockup": lambda a: astock.lockup_expiry(str(a["code"])),
    "query_investor_qa": _investor_qa,
    "query_concepts": _concepts,
    "query_industry_comparison": lambda a: astock.industry_comparison(top_n=max(5, min(int(a.get("top_n") or 20), 50))),
    "query_industry_reports": lambda a: _pick(
        astock.eastmoney_industry_reports(keywords=a.get("keywords"), days=int(a.get("days") or 90), max_pages=1),
        ("title", "publishDate", "orgSName", "industryName"), 20),
    "query_market": _market,
    "query_news_radar": _radar,
    "search_public_news": _public_news_search,
    "query_global_stock": lambda a: gstock.us_hk_stock(str(a.get("symbol", ""))) or {"error": "未找到该美股/港股/韩股代码"},
    "query_hk_cashflow": lambda a: gstock.hk_cashflow(str(a.get("symbol", ""))) or {"error": "未找到该港股现金流（仅港股支持）"},
    "query_macro_composite": _macro_composite,
    "query_liquidity_composite": _liquidity_composite,
    "query_sector_scores": _sector_scores,
    "query_gold_score": _gold_score,
    "query_bonds_curve": _bonds_curve,
    "query_bonds_overview": _bonds_overview,
    "query_bonds_framework": _bonds_framework,
    "query_bonds_calc": lambda a: bonds.get_calc() or {"error": "债市计算层暂不可用"},
    "query_bonds_positioning": lambda a: bonds.get_positioning() or {"error": "国债期货量仓暂不可用"},
    "query_bonds_segments": lambda a: bonds.get_segments() or {"error": "分品种评分暂不可用"},
}


def exec_tool(name: str, args: dict):
    """执行工具，返回可序列化结果（失败返回 error 字段，不抛）。"""
    fn = _HANDLERS.get(name)
    if fn is None:
        return {"error": f"未知工具 {name}"}
    try:
        return fn(args or {})
    except astock.DependencyMissing as e:
        return {"error": str(e)}
    except KeyError as e:
        return {"error": f"{name} 缺少必填参数 {e}"}
    except Exception as e:  # noqa: BLE001 — 工具错误回喂给模型，不中断循环
        return {"error": f"{name} 执行失败：{e}"}
