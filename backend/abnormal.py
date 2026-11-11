"""异动接近度：A 股离交易所「异常波动 / 严重异常波动」标准还有多远。

纯规则计算：不判断方向、不预测、不给买卖建议。达到标准时交易所会公告，
这里只按公开规则推算，一律以交易所公告为准。

规则出处 —— 2026 年修订版交易规则（2026-04-24 发布、2026-07-06 起施行）：
  上交所《交易规则》5.4.2 / 5.4.3 / 5.4.5（主板）、6.10 / 6.11 / 6.12（科创板）；
  深交所《交易规则》5.4.1 / 5.4.3 / 5.4.4 / 5.4.5（主板、创业板）；
  北交所《交易规则》5.4.2 – 5.4.5。

- 累计偏离值 =（期末收盘 / 期初前收盘 − 1）× 100% −（基准指数期末收盘 / 期初前收盘 − 1）× 100%。
  是**区间涨幅之差**，不是每天的偏离值相加。期间有除权除息的按前复权价算。
- 「连续 N 个交易日内」：截至当天、长度 1 到 N 天的任一区间达到即算。
- 达到后交易所公告，指标从公告的次一交易日起重新计算；停牌的从复牌之日起重新计算。
- 没有涨跌幅限制的交易日不纳入：沪深是新股上市后前 5 个交易日。
- 2026-07-06 起主板 ST 股涨跌幅改为 10%，异常波动标准与主板其他股票相同。

没算的（如实写进 gaps，不当成 0）：
- 沪深主板「连续 3 日日均换手率是前 5 日的 30 倍、且累计换手率达 20%」这一条：腾讯日 K 没有换手率。
- 北交所：腾讯 K 线不支持，北证50 也取不到。
- 基金、债券、B 股、港股、美股：不适用这套股票标准或基准不同。
"""

from __future__ import annotations

import time
from typing import Any

import astock
import cache_runtime

#: 一次最多算这么多只，避免自选很长时把整页拖住
MAX_CODES = 60
#: 取多少根日 K：30 日窗口 + 指望「上次达到后重新计算」需要的余量
STOCK_BARS = 80
INDEX_BARS = 100
#: 盘中日 K 会变，缓存最多用 10 分钟
CACHE_TTL = 600

#: 2026 年修订版规则。count = 10 个交易日内第几次同向异常波动构成严重异常波动；
#: sev10 / sev30 = [向上, 向下] 的累计偏离标准（%）。
BOARD_RULES: dict[str, dict[str, Any]] = {
    "sh_main": {"label": "沪市主板", "index_secid": "1.000002", "index_label": "上证A股指数",
                "abn": 20, "count": 4, "sev10": (100, -50), "sev30": (200, -70), "no_limit_days": 5},
    "star": {"label": "科创板", "index_secid": "1.000688", "index_label": "科创50",
             "abn": 30, "count": 3, "sev10": (100, -50), "sev30": (200, -70), "no_limit_days": 5},
    "sz_main": {"label": "深市主板", "index_secid": "0.399107", "index_label": "深证A股指数",
                "abn": 20, "count": 4, "sev10": (100, -50), "sev30": (200, -70), "no_limit_days": 5},
    "chinext": {"label": "创业板", "index_secid": "0.399102", "index_label": "创业板综合指数",
                "abn": 30, "count": 3, "sev10": (100, -50), "sev30": (200, -70), "no_limit_days": 5},
    "bse": {"label": "北交所", "index_secid": "0.899050", "index_label": "北证50",
            "abn": 40, "count": 3, "sev10": (150, -60), "sev30": (300, -75), "no_limit_days": 1},
}

RULE_SOURCE = "上交所 / 深交所 / 北交所《交易规则》（2026 年修订，2026-07-06 起施行）"


def board_of(code: str) -> str | None:
    """6 位代码 → 板块；基金、债券、B 股返回 None。"""
    if not (len(code) == 6 and code.isdigit()):
        return None
    if code.startswith("60") or code.startswith("601") or code.startswith("603") or code.startswith("605"):
        return "sh_main"
    if code[:2] in {"68", "69"}:
        return "star"
    if code.startswith("00"):
        return "sz_main"
    if code.startswith("30"):
        return "chinext"
    if code[:2] in {"92", "43", "83", "87", "88"}:
        return "bse"
    return None


def is_fund_or_bond(code: str) -> bool:
    """场内基金（5xx / 15x / 16x / 18x）与可转债（11x / 12x）。"""
    return code.startswith(("5", "15", "16", "18", "11", "12"))


def _r4(value: float) -> float:
    return round(value * 1e4) / 1e4


def _bars(rows: list[dict]) -> list[dict]:
    """[{date, close}] 升序；非正收盘价丢掉。"""
    out = []
    for row in rows or []:
        date = str(row.get("date") or "")[:10]
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if len(date) == 10 and close > 0:
            out.append({"date": date, "close": close})
    out.sort(key=lambda bar: bar["date"])
    return out


def proximity(stock: list[dict], index: list[dict], rule: dict[str, Any],
              *, listed_in_range: bool = False) -> dict[str, Any]:
    """按交易所规则逐日推算到最新一天的接近程度。

    ``stock`` / ``index`` 是前复权日收盘（升序）。返回里：
    ``abnormal`` / ``severe10`` / ``severe30`` 三条标准各自的最大向上、最大向下累计偏离，
    ``same_dir`` 是最近 10 个交易日里已有几次同向异常波动，``level`` 是 0–1+ 的接近度
    （1 = 已达到），``recent`` 是最近 30 个交易日里按规则算出达到过的标准。
    """
    if not stock or not index:
        return {"error": "日 K 不够，算不了"}

    idx_dates = [bar["date"] for bar in index]
    idx_pos = {date: i for i, date in enumerate(idx_dates)}
    notes: list[str] = []

    # rows[k] 对应 stock[k+1]：这一天的当日偏离所需的收盘 / 前收，以及指数同期两个收盘
    rows: list[dict[str, Any] | None] = []
    idx_missing = 0
    for j in range(1, len(stock)):
        cur, prev = stock[j], stock[j - 1]
        pos = idx_pos.get(cur["date"])
        if pos is None or pos == 0 or prev["close"] <= 0 or cur["close"] <= 0:
            if pos is None and cur["date"] >= (idx_dates[0] if idx_dates else ""):
                idx_missing += 1
            rows.append(None)
            continue
        # 上一根 K 线比上一个指数交易日还晚 ⇒ 指数缺了那天，这一天的指数涨跌跨了两天，对不上
        if prev["date"] > idx_dates[pos - 1]:
            rows.append(None)
            continue
        # 上一根 K 线比上一个指数交易日早 ⇒ 中间停过牌，复牌日起重新计算
        resumed = idx_dates[pos - 1] != prev["date"]
        rows.append({
            "date": cur["date"], "close": cur["close"], "prev": prev["close"],
            "idx": index[pos]["close"], "idx_prev": index[pos - 1]["close"],
            "resumed": resumed,
        })

    if idx_missing:
        notes.append(f"{rule['index_label']}缺 {idx_missing} 个交易日的数据，缺的那天和紧接着的一天不算")

    first_start = max(0, rule["no_limit_days"] - 1) if listed_in_range else 0
    if listed_in_range:
        notes.append(f"上市不久，上市后前 {rule['no_limit_days']} 个交易日没有涨跌幅限制，不算")

    def cumulative(a: int, t: int) -> float:
        start, end = rows[a], rows[t]
        assert start is not None and end is not None
        return _r4(
            (end["close"] / start["prev"] - 1) * 100
            - (end["idx"] / start["idx_prev"] - 1) * 100
        )

    def scan(start: int, t: int) -> tuple[float | None, float | None]:
        up = down = None
        for a in range(start, t + 1):
            if rows[a] is None or rows[t] is None:
                continue
            value = cumulative(a, t)
            if value > 0 and (up is None or value > up):
                up = value
            if value < 0 and (down is None or value < down):
                down = value
        return up, down

    abn_start = sev_start = first_start
    abn_hits: list[dict[str, Any]] = []
    triggers: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None

    for t, row in enumerate(rows):
        if row is None:
            abn_start = max(abn_start, t + 1)
            sev_start = max(sev_start, t + 1)
            continue
        if t < first_start:
            continue
        if row["resumed"]:
            abn_start = max(abn_start, t)
            sev_start = max(sev_start, t)

        up3, down3 = scan(max(abn_start, t - 2), t)
        hit_up = up3 is not None and up3 >= rule["abn"]
        hit_down = down3 is not None and down3 <= -rule["abn"]
        abn_dir = None
        if hit_up and hit_down:
            abn_dir = "up" if up3 / rule["abn"] >= -down3 / rule["abn"] else "down"
        elif hit_up:
            abn_dir = "up"
        elif hit_down:
            abn_dir = "down"

        lo = max(sev_start, t - 9)
        same = {
            "up": sum(1 for hit in abn_hits if hit["t"] >= lo and hit["dir"] == "up"),
            "down": sum(1 for hit in abn_hits if hit["t"] >= lo and hit["dir"] == "down"),
        }
        up10, down10 = scan(lo, t)
        up30, down30 = scan(max(sev_start, t - 29), t)
        s10_dir = "up" if up10 is not None and up10 >= rule["sev10"][0] else (
            "down" if down10 is not None and down10 <= rule["sev10"][1] else None)
        s30_dir = "up" if up30 is not None and up30 >= rule["sev30"][0] else (
            "down" if down30 is not None and down30 <= rule["sev30"][1] else None)
        count_dir = abn_dir if abn_dir and same[abn_dir] + 1 >= rule["count"] else None

        last = {
            "t": t,
            "since": max(abn_start, t - 2),
            "abnormal": {"days": 3, "up": up3, "down": down3,
                         "limit": [rule["abn"], -rule["abn"]], "reached": abn_dir},
            "severe10": {"days": 10, "up": up10, "down": down10,
                         "limit": list(rule["sev10"]), "reached": s10_dir},
            "severe30": {"days": 30, "up": up30, "down": down30,
                         "limit": list(rule["sev30"]), "reached": s30_dir},
            "same": same,
        }

        if abn_dir:
            value = up3 if abn_dir == "up" else down3
            triggers.append({"t": t, "date": row["date"], "kind": "abnormal", "dir": abn_dir,
                             "why": f"3 个交易日内累计偏离 {_sign(value)}%（标准 ±{rule['abn']}%）"})
            abn_hits.append({"t": t, "dir": abn_dir})
            abn_start = t + 1

        why: list[str] = []
        if count_dir:
            why.append(f"10 个交易日内第 {rule['count']} 次{'向上' if count_dir == 'up' else '向下'}异常波动")
        if s10_dir:
            why.append(f"10 个交易日内累计偏离 {_sign(up10 if s10_dir == 'up' else down10)}%"
                       f"（标准 {_sign(rule['sev10'][0])}% / {rule['sev10'][1]}%）")
        if s30_dir:
            why.append(f"30 个交易日内累计偏离 {_sign(up30 if s30_dir == 'up' else down30)}%"
                       f"（标准 {_sign(rule['sev30'][0])}% / {rule['sev30'][1]}%）")
        if why:
            triggers.append({"t": t, "date": row["date"], "kind": "severe",
                             "dir": count_dir or s10_dir or s30_dir, "why": "；".join(why)})
            sev_start = t + 1

    if last is None:
        if listed_in_range and len(rows) <= first_start:
            return {"error": f"上市后前 {rule['no_limit_days']} 个交易日不纳入计算"}
        return {"error": "能算的交易日不够"}

    usable = [k for k, row in enumerate(rows) if row is not None and k >= first_start]
    if len(usable) < 30:
        notes.append(f"能算的只有 {len(usable)} 个交易日，30 日那条按已有的天数算")

    recent_from = last["t"] - 29
    return {
        "as_of": rows[last["t"]]["date"],
        "since": rows[last["since"]]["date"],
        "abnormal": last["abnormal"],
        "severe10": last["severe10"],
        "severe30": last["severe30"],
        "same_dir": {**last["same"], "need": rule["count"]},
        "recent": [{k: v for k, v in item.items() if k != "t"}
                   for item in triggers if item["t"] >= recent_from],
        "level": _r4(max(_crit_ratio(last["abnormal"]), _crit_ratio(last["severe10"]),
                         _crit_ratio(last["severe30"]))),
        "notes": notes,
    }


def _crit_ratio(crit: dict[str, Any]) -> float:
    up = crit["up"] / crit["limit"][0] if crit.get("up") is not None else 0.0
    down = crit["down"] / crit["limit"][1] if crit.get("down") is not None else 0.0
    return max(up, down)


def _sign(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{'+' if value > 0 else ''}{value:.2f}"


# ——— 取数 ———


def _stock_bars(code: str) -> list[dict]:
    def fetch() -> list[dict]:
        # 腾讯 K 线连续快取会被 WAF 挡掉一两根（实测连跑 4 只后开始空回），隔一口气重试一次
        for attempt in range(2):
            rows = astock.tencent_kline(code, count=STOCK_BARS)
            if rows:
                return rows
            if attempt == 0:
                time.sleep(0.4)
        return []

    return _bars(cache_runtime.get(
        f"abnormal:stock:{code}",
        fetch,
        valid=lambda v: isinstance(v, list) and bool(v),
        ttl=CACHE_TTL, decorate=False,
    ) or [])


def _index_bars(rule: dict[str, Any]) -> list[dict]:
    return _bars(cache_runtime.get(
        f"abnormal:index:{rule['index_secid']}",
        lambda: astock.index_daily_em(rule["index_secid"], INDEX_BARS),
        valid=lambda v: isinstance(v, list) and bool(v),
        ttl=CACHE_TTL, decorate=False,
    ) or [])


def for_codes(codes: list[str]) -> dict[str, Any]:
    """自选一批代码 → [{code, name, board, result, skipped}]。非 A 股 / 基金 / 北交所进 gaps。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for code in codes:
        code = str(code).strip()
        if code and code not in seen:
            seen.add(code)
            ordered.append(code)

    gaps: list[str] = []
    rows: list[dict[str, Any]] = []
    others = [c for c in ordered if not (len(c) == 6 and c.isdigit())]
    funds = [c for c in ordered if len(c) == 6 and c.isdigit() and is_fund_or_bond(c)]
    bse = [c for c in ordered if len(c) == 6 and c.isdigit() and not is_fund_or_bond(c) and board_of(c) == "bse"]
    unknown = [c for c in ordered if len(c) == 6 and c.isdigit() and not is_fund_or_bond(c)
               and board_of(c) not in BOARD_RULES]
    stocks = [c for c in ordered if len(c) == 6 and c.isdigit() and not is_fund_or_bond(c)
              and board_of(c) in BOARD_RULES and board_of(c) != "bse"]

    if others:
        gaps.append(f"港股 / 美股 / 无法识别的代码 {len(others)} 只：不适用 A 股的异常波动标准")
    if funds:
        gaps.append(f"基金 / 债券 {len(funds)} 只：不适用股票的异常波动标准，没算")
    if bse:
        gaps.append(f"北交所 {len(bse)} 只：腾讯 K 线不支持北交所、北证50 也取不到，没算（标准是 3 日 ±40%）")
    if unknown:
        gaps.append(f"{'、'.join(unknown[:8])}{' 等' if len(unknown) > 8 else ''}：B 股或无法识别的代码，没算")
    if len(stocks) > MAX_CODES:
        gaps.append(f"只算了前 {MAX_CODES} 只 A 股，另有 {len(stocks) - MAX_CODES} 只没算")
        stocks = stocks[:MAX_CODES]
    if any(board_of(c) in ("sh_main", "sz_main") for c in stocks):
        gaps.append("沪深主板「连续 3 日日均换手率是前 5 日的 30 倍且累计换手率达 20%」这一条没算：日 K 里没有换手率")

    try:
        quotes = astock.tencent_quote(stocks) if stocks else {}
    except Exception:  # noqa: BLE001 — 名称只是展示用，取不到不影响规则计算
        quotes = {}

    as_of = None
    intraday = False
    for code in stocks:
        board = board_of(code)
        rule = BOARD_RULES[board]
        name = (quotes.get(code) or {}).get("name") or ""
        stock = _stock_bars(code)
        if not stock:
            rows.append({"code": code, "name": name, "board": board, "board_label": rule["label"],
                         "index_label": rule["index_label"], "result": None,
                         "skipped": "没取到日 K"})
            continue
        index = _index_bars(rule)
        if not index:
            rows.append({"code": code, "name": name, "board": board, "board_label": rule["label"],
                         "index_label": rule["index_label"], "result": None,
                         "skipped": f"{rule['index_label']}没取到，算不了基准"})
            continue
        # 第一根就是上市首日的判据：这根 K 线离自选取数窗口的起点很近且前面没有更早的价格，
        # 只按「最新一天 - 根数」判断会误伤老股票，所以只在明确少于 rule 所需天数时启用
        listed_in_range = len(stock) < (rule["no_limit_days"] + 30) and stock[0]["date"] == index[0]["date"]
        result = proximity(stock, index, rule, listed_in_range=listed_in_range)
        if "error" in result:
            rows.append({"code": code, "name": name, "board": board, "board_label": rule["label"],
                         "index_label": rule["index_label"], "result": None, "skipped": result["error"]})
            continue
        rows.append({"code": code, "name": name, "board": board, "board_label": rule["label"],
                     "index_label": rule["index_label"], "result": result, "skipped": None})
        as_of = max(as_of or "", result["as_of"])
        quote = quotes.get(code) or {}
        if quote.get("quote_date") == result["as_of"] and quote.get("price") is not None:
            # 日 K 最后一根是今天、当天还没收盘：按最新价估算
            intraday = True

    return {
        "rows": rows,
        "rules": [{"board": key, "label": rule["label"], "index": rule["index_label"],
                   "abn": rule["abn"], "count": rule["count"],
                   "sev10": list(rule["sev10"]), "sev30": list(rule["sev30"])}
                  for key, rule in BOARD_RULES.items() if key != "bse"],
        "as_of": as_of,
        "intraday": intraday,
        "gaps": gaps,
        "source": "腾讯前复权日 K（个股）+ 东财指数日 K（基准）",
        "rule_source": RULE_SOURCE,
    }
