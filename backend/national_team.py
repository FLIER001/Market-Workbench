"""国家队披露持仓研究：显式导入、保守时点、原文/改进双口径。

不推断未披露持仓，不将 ETF 申赎归因国家队，不声称复现研报收益。
"""
from __future__ import annotations

from datetime import date
from typing import Literal
import hashlib
import json

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Holding(Record):
    holder: str = Field(min_length=1, max_length=160)
    # 由来源文件核验身份；组别用于独立敏感性分析，不自动扩展主体名单。
    platform: Literal["汇金", "证金", "外管", "社保", "集成电路", "国新", "诚通"]
    shares: float = Field(ge=0, le=1e18)


class Snapshot(Record):
    period: date
    disclosed: date
    source: str = Field(min_length=1, max_length=1000)
    float_shares: float = Field(ge=1e-8, le=1e18)
    holdings: list[Holding] = Field(max_length=100)
    complete: bool  # 完整前十大非限售股东表，未出现仍不等于零持仓

    @model_validator(mode="after")
    def check(self):
        if self.disclosed < self.period:
            raise ValueError("披露日不得早于报告期")
        names = [h.holder for h in self.holdings]
        if len(names) != len(set(names)):
            raise ValueError("同一快照股东重复，禁止重复合计")
        return self


class Price(Record):
    day: date
    close: float = Field(ge=1e-8, le=1e18)
    adjusted_close: float = Field(ge=1e-8, le=1e18)


class Stock(Record):
    code: str = Field(pattern=r"^[0-9]{6}$")
    name: str = Field(min_length=1, max_length=80)
    industry: str = Field(min_length=1, max_length=80)
    eligible: bool  # 用户核验信号日股票池/ST状态，不使用今天的名单回填历史
    snapshots: list[Snapshot] = Field(max_length=100)
    prices: list[Price] = Field(max_length=1000)

    @model_validator(mode="after")
    def unique(self):
        keys = [(s.period, s.disclosed) for s in self.snapshots]
        if len(keys) != len(set(keys)) or len(self.prices) != len({p.day for p in self.prices}):
            raise ValueError("重复快照版本或价格日期")
        return self


class MarketPoint(Record):
    day: date
    available: date
    holding_value: float = Field(ge=0, le=1e18)
    csi800_float_value: float = Field(ge=1e-8, le=1e18)

    @model_validator(mode="after")
    def check(self):
        if self.available < self.day:
            raise ValueError("市场指标可用日不得早于观察日")
        return self


class NationalTeamRequest(Record):
    as_of: date
    previous_signal: date
    mode: Literal["paper", "improved"] = "improved"
    top_n: int = Field(default=30, ge=1, le=100)
    source: str = Field(min_length=1, max_length=1000)
    universe: str = Field(min_length=1, max_length=200)
    platforms: list[Literal["汇金", "证金", "外管", "社保", "集成电路", "国新", "诚通"]] = Field(default=["汇金", "证金"], min_length=1, max_length=7)
    # 同一主体范围、同一来源冻结版本；历史每一天只能用当时已披露持仓。
    market: list[MarketPoint] = Field(default=[], max_length=5000)
    stocks: list[Stock] = Field(default=[], max_length=2000)

    @model_validator(mode="after")
    def check(self):
        if self.previous_signal >= self.as_of:
            raise ValueError("上次信号日必须早于本次信号日")
        if len(self.stocks) != len({s.code for s in self.stocks}):
            raise ValueError("股票代码重复")
        if len(self.market) != len({m.day for m in self.market}):
            raise ValueError("市场指标日期重复")
        if len(self.platforms) != len(set(self.platforms)):
            raise ValueError("主体范围重复")
        return self


def regime(req: NationalTeamRequest) -> dict:
    points = sorted((p for p in req.market if p.day <= req.as_of), key=lambda p: p.day)
    # 市场收盘值当日可用，但其持仓输入必须已披露。不能删掉迟到行再拼接成日频。
    points = points[-818:]
    if len(points) < 818 or points[-1].day != req.as_of:
        return {"state": "unknown", "slope": None, "reason": "需截至信号日的818条日频市场指标（756日窗口＋63日斜率）"}
    if any(p.available > p.day for p in points):
        return {"state": "unknown", "slope": None, "reason": "市场序列包含事后才可得的数据；请用各日当时已公开的持仓重建"}
    if any((b.day-a.day).days > 15 for a, b in zip(points, points[1:])):
        return {"state": "unknown", "slope": None, "reason": "市场日频序列存在超过15天的断档"}
    ratio = pd.Series([p.holding_value / p.csi800_float_value for p in points])
    window = ratio.rolling(756, min_periods=756)
    z = ((ratio-window.median()) / window.std(ddof=0)).iloc[-63:]
    if not np.isfinite(z).all():
        return {"state": "unknown", "slope": None, "reason": "市场指标无足够变异，无法识别状态"}
    slope = float(np.polyfit(np.arange(63), z, 1)[0])
    return {"state": "inflow" if slope > 0 else "other", "slope": slope,
            "reason": "持仓市值相对强度的趋势代理，不是实际资金净流入"}


def _snapshot(stock: Stock, cutoff: date):
    # 只有日期，没有发布时间：当日披露推迟到下一交易日信号可用。
    visible = [s for s in stock.snapshots if s.disclosed < cutoff]
    return max(visible, key=lambda s: (s.period, s.disclosed), default=None)


def evaluate(req: NationalTeamRequest) -> dict:
    state = regime(req)
    rows, excluded = [], []
    for stock in req.stocks:
        reason = None
        current, old = _snapshot(stock, req.as_of), _snapshot(stock, req.previous_signal)
        prices = {p.day: p for p in stock.prices if p.day <= req.as_of}
        history = sorted(prices.values(), key=lambda p: p.day)[-252:]
        if not stock.eligible:
            reason = "不满足信号日股票池/ST条件"
        elif current is None or not current.complete:
            reason = "缺少已披露完整持仓快照"
        elif (req.as_of-current.period).days > 180:
            reason = "持仓报告期距信号日超过180天"
        elif sum(h.shares for h in current.holdings if h.platform in req.platforms) <= 0:
            reason = "选定主体未披露正持仓（不视为零持仓）"
        elif req.as_of not in prices or len(history) < 252:
            reason = "缺少信号日价格或252条历史收盘价"
        elif (history[-1].day-history[0].day).days > 430:
            reason = "252条价格跨期过长，可能长期停牌或缺数"
        if reason:
            excluded.append({"code": stock.code, "reason": reason})
            continue
        shares = sum(h.shares for h in current.holdings if h.platform in req.platforms)
        price = prices[req.as_of]
        value = shares * price.close
        gap = max(getattr(p, "close" if req.mode == "paper" else "adjusted_close") for p in history) / getattr(price, "close" if req.mode == "paper" else "adjusted_close") - 1
        change = None
        # 离开榜单/新进入榜单的旧持仓未知，不把缺失当0；相同报告期的修订不作加仓。
        if old and old.complete and old.period < current.period and (req.previous_signal-old.period).days <= 180:
            old_shares = sum(h.shares for h in old.holdings if h.platform in req.platforms)
            if old_shares > 0:
                if req.mode == "improved":
                    change = shares/current.float_shares - old_shares/old.float_shares
                elif req.previous_signal in prices:
                    change = value - old_shares*prices[req.previous_signal].close
        rows.append({"code": stock.code, "name": stock.name, "industry": stock.industry,
                     "period": current.period.isoformat(), "disclosed": current.disclosed.isoformat(),
                     "source": current.source, "shares": shares, "holding_value": value,
                     "change": change, "gap": gap, "score": None, "selected": False})
    eligible = [r for r in rows if state["state"] == "other" or (state["state"] == "inflow" and r["change"] is not None)]
    if eligible:
        frame = pd.DataFrame(eligible)
        # 等值平均秩，分值只表示本次共同样本中的相对位置。
        gap_rank = frame.gap.rank(method="average", pct=True)
        other_rank = (frame.change if state["state"] == "inflow" else -frame.holding_value).rank(method="average", pct=True)
        for row, score in zip(eligible, 50*(gap_rank+other_rank)):
            row["score"] = float(score)
    rows.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1), r["code"]))
    # 数量不足时不给“优选组合”；不把小样本强行凑成完整投资组合。
    ready = len(eligible) >= max(2, req.top_n)
    if ready:
        for row in rows[:req.top_n]:
            row["selected"] = True
    digest = hashlib.sha256(json.dumps(req.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"version": "national-team-v1", "as_of": req.as_of.isoformat(), "mode": req.mode,
            "source": req.source, "universe": req.universe, "platforms": req.platforms,
            "input_sha256": digest, "regime": state, "status": "research_only" if ready else "insufficient_data",
            "counts": {"input": len(req.stocks), "observable": len(rows), "rankable": len(eligible), "excluded": len(excluded)},
            "rows": rows, "excluded": excluded, "portfolio": [{"code": r["code"], "weight": 1/req.top_n} for r in rows if r["selected"]],
            "warnings": ["导入源及主体身份尚需人工核验；本结果未经收益回测，不是买入建议。",
                         "国家队持仓为披露可见下界；未出现不代表清仓。ETF渠道未纳入。",
                         "股价距高点不代表合理估值或预期收益；持股占比变化也受股本变化影响。",
                         "同一主体范围和历史可得数据必须贯穿市场状态与股票因子；交易应晚于信号日。",
                         "252/756交易日、总体标准差和同日披露延后是本实现约定，不宣称精确复现原文。"]}


def template() -> dict:
    return {"as_of": "2026-10-23", "previous_signal": "2026-08-31", "mode": "improved", "top_n": 30,
            "source": "请填写真实数据来源、导出版本及复权口径", "universe": "请填写信号日股票池与历史成分来源",
            "platforms": ["汇金", "证金"], "market": [], "stocks": []}


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description="国家队因子：导入JSON，输出可审计研究结果")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(NationalTeamRequest.model_validate_json(args.input.read_text()))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
