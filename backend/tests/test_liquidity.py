"""资金面框架回归测试：口径、无前视、频率和回退状态均离线验证。"""
from datetime import date, timedelta

import market


def _points(n, start=date(2025, 1, 1), step=1.0, base=0.0, days=1):
    return [{"date": (start + timedelta(days=i * days)).isoformat(), "v": base + i * step}
            for i in range(n)]


def _source(points, fetched="2026-08-11 09:00"):
    return {"hist": points, "stale": False, "fetched_at": fetched}


def test_fred_reserves_and_tga_are_distinct_and_scaled_correctly():
    reserves = market._FRED_SERIES["reserves"]
    tga = market._FRED_SERIES["tga"]

    assert reserves[:4] == ("WRESBAL", "亿$", "银行准备金余额", 0.01)
    assert tga[:4] == ("WTREGEN", "亿$", "美国财政部一般账户 TGA", 0.01)
    assert reserves[0] != tga[0]


def test_rolling_rank_never_changes_past_scores_when_future_arrives():
    past = _points(30)
    past_scores = market._rolling_rank(past, 20, min_periods=5)
    extended_scores = market._rolling_rank(past + [
        {"date": "2025-02-01", "v": 1_000_000.0},
        {"date": "2025-02-02", "v": -1_000_000.0},
    ], 20, min_periods=5)

    assert extended_scores[:len(past_scores)] == past_scores


def test_leverage_uses_changes_and_balance_normalized_flow_not_levels():
    balances = _points(90, base=10_000.0, step=5.0)
    netbuy = [{"date": p["date"], "v": 10.0 + i % 7} for i, p in enumerate(balances)]
    idx = market._cn_leverage_index({
        "rzrqye_hist": balances, "rzjme_hist": netbuy, "stale": False,
        "source": "test", "fetched_at": "2026-08-11 09:00",
    })

    assert idx["kind"] == "state"
    assert idx["favorable"] == "high"
    assert idx["label"] == "杠杆温度"
    assert "20日变化" in idx["components"][0]["label"]
    assert "/两融余额" in idx["components"][1]["label"]
    assert all(c["label"] != "两融余额" for c in idx["components"])


def test_index_flow_is_auxiliary_and_does_not_sum_overlapping_indices():
    hist = _points(20, base=-50.0, step=5.0)
    flows = {
        key: {"name": name, "hist": hist, "latest": hist[-1], "stale": False}
        for key, name in (("1.000001", "上证指数"), ("0.399001", "深证成指"),
                          ("0.399006", "创业板指"))
    }
    idx = market._cn_momentum_index({"index_flows": flows})

    assert idx["kind"] == "auxiliary"
    assert idx["favorable"] == "high"
    assert idx["date"] == hist[-1]["date"]
    assert len(idx["components"]) == 3
    assert idx["interpretation"] == "供应商口径且指数成分重叠，仅作辅助观察"


def test_system_liquidity_uses_four_week_change_and_tga_as_separate_drain():
    reserves = _points(40, step=20.0, base=30_000.0, days=7)
    rrp = _points(280, step=-0.02, base=50.0, days=1)  # FRED display unit is $bn
    tga = _points(40, step=10.0, base=7_000.0, days=7)
    us = {"reserves": _source(reserves), "rrp": _source(rrp), "tga": _source(tga)}

    idx = market._us_qt_index(us)

    assert idx["kind"] == "stress"
    assert idx["favorable"] == "low"
    assert idx["label"] == "系统流动性压力"
    assert all("四周" in c["label"] for c in idx["components"])
    assert any("TGA" in c["label"] for c in idx["components"])
    assert all("20日" not in c["label"] for c in idx["components"])


def test_carried_sources_are_marked_stale_and_reported():
    prev = {"old": {"label": "旧值", "date": "2026-08-01", "stale": False}}
    merged = market._merge_liquidity_group(prev, {}, "test")
    payload = {
        "cn": {"index_flows": {"flow": {"name": "上证指数", "stale": True}}},
        "cn_indices": {**merged, "flow": {"label": "大单流向（辅助）", "kind": "auxiliary", "stale": True}},
        "us": {}, "us_indices": {},
    }
    freshness = market._liquidity_freshness(payload)

    assert merged["old"]["stale"] is True
    assert freshness["stale"] is True
    assert freshness["stale_count"] == 1
    assert freshness["stale_sources"][0]["label"] == "旧值"


def test_macro_score_does_not_treat_margin_or_vendor_flow_as_monotonic_bullish():
    market_inputs = {key for name, _, _, specs in market._MACRO_MODULES if name == "市场确认"
                     for key, _, _ in specs}

    assert market_inputs == {"market_breadth", "new_high_breadth"}
    assert not hasattr(market, "_total_flow_hist")
