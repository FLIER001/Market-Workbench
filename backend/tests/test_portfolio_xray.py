"""持仓体检与历史回放的纯计算口径：权重归一、最小历史过滤、缺口不进权重、
回撤起点、尾部风险分位、风险贡献合计为 1、回放的停牌与上市前缺口、覆盖率不足不给组合数字。
"""
import numpy as np
import pandas as pd
import pytest

import portfolio_xray as px


def _series(values, start="2024-01-01", name="close"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx, dtype="float64", name=name)


def _walk(steps, seed, sigma=0.01, start=100.0):
    rng = np.random.default_rng(seed)
    return _series(start * np.cumprod(1 + rng.normal(0, sigma, steps)))


# ——— 体检 ———


def test_weights_normalized_and_risk_contribution_sums_to_one():
    a, b = _walk(200, 1), _walk(200, 2)
    report = px.compute_xray({"a": a, "b": b}, {"a": 600.0, "b": 400.0})

    assert report["ok"] is True
    assert report["inputs"]["weights"] == {"a": 0.6, "b": 0.4}
    shares = report["risk_contribution"]["shares"]
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-6)


def test_short_history_is_skipped_not_zero_weighted_silently():
    long_series, short_series = _walk(200, 3), _walk(20, 4)
    report = px.compute_xray({"a": long_series, "b": short_series}, {"a": 500.0, "b": 500.0})

    assert report["ok"] is True
    assert report["inputs"]["symbols"] == ["a"]
    assert report["inputs"]["weights"] == {"a": 1.0}
    assert [gap["code"] for gap in report["gaps"]] == ["b"]


def test_no_price_holding_is_a_gap():
    report = px.compute_xray({"a": _walk(200, 5)}, {"a": 0.0})
    assert report["ok"] is False
    assert report["gaps"][0]["reason"].startswith("没有市值")


def test_max_drawdown_counts_the_first_day_drop():
    # 先跌 50% 再缓慢涨回：起点净值 1 必须先被当成历史高点，否则首日下跌会被漏掉
    closes = _series([100.0, 50.0, *[50.0 + i for i in range(70)]])
    report = px.compute_xray({"a": closes}, {"a": 1.0})
    assert report["drawdown"]["max_drawdown"] == pytest.approx(-0.5, abs=1e-6)


def test_single_asset_has_no_diversification_number():
    report = px.compute_xray({"a": _walk(120, 6)}, {"a": 1.0})
    assert report["diversification"]["diversification_ratio"] is None
    assert report["correlation"]["avg_pairwise_abs"] is None
    assert report["tail_risk"]["var_95"] is not None


def test_tail_risk_is_the_empirical_quantile():
    # 100 天里 10 天亏 10%：95% 分位必然落在 −10% 那一档，ES 也等于 −10%
    rets = [-0.10] * 10 + [0.001] * 90
    closes = _series(100 * np.cumprod([1 + r for r in rets]))
    report = px.compute_xray({"a": closes}, {"a": 1.0})
    assert report["tail_risk"]["var_95"] == pytest.approx(0.10, abs=1e-6)
    assert report["tail_risk"]["expected_shortfall_95"] == pytest.approx(0.10, abs=1e-6)


def test_empty_input_reports_error_not_crash():
    report = px.compute_xray({}, {})
    assert report["ok"] is False
    assert "算不了" in report["error"]


# ——— 回放 ———


def _bars(start, periods, value=100.0, step=1.0):
    idx = pd.bdate_range(start, periods=periods)
    vals = [value + i * step for i in range(periods)]
    return pd.Series(vals, index=idx, dtype="float64")


def test_anchor_rejects_holding_that_delisted_mid_window():
    # 最后一个价格离区间结束太远 → 涨跌算不准，进缺口
    full = _bars("2015-01-05", 120)
    anchor, why = px._anchor(full, pd.Timestamp("2015-06-01"), pd.Timestamp("2015-08-26"))
    assert anchor is None
    assert "没有新价格" in why


def test_anchor_rejects_holding_listed_after_window_start():
    full = _bars("2015-07-10", 60)
    anchor, why = px._anchor(full, pd.Timestamp("2015-06-08"), pd.Timestamp("2015-08-26"))
    assert anchor is None
    assert "还没有价格" in why


def test_anchor_uses_last_close_before_start_when_suspended():
    # 起点附近停牌：用停牌前最后一个收盘价，只要后面还复牌就不算缺口
    full = _bars("2015-01-05", 200)
    full = full.drop(full.index[100:120])
    anchor, why = px._anchor(full, pd.Timestamp("2015-06-01"), pd.Timestamp("2015-09-30"))
    assert why is None
    assert anchor is not None
    assert anchor[0] <= pd.Timestamp("2015-06-01")


def test_replay_refuses_portfolio_number_below_half_coverage():
    scenario = {"id": "t", "label": "测试窗口", "start": "2018-01-24", "end": "2019-01-03"}
    covered = _bars("2017-06-01", 460)
    positions = [
        {"code": "600000", "name": "有价格", "value": 400.0},
        {"code": "600001", "name": "上市太晚", "value": 600.0},
    ]
    series = {"600000": covered}
    out, path = px._replay_one(scenario, positions, series, {}, None, None)

    assert out["portfolio"] is None
    assert "不到一半" in out["portfolio_note"]
    assert out["coverage"] == pytest.approx(0.4)
    assert path is not None and path["portfolio"] is None


def test_replay_weights_only_survivors_and_reports_gap():
    scenario = {"id": "t", "label": "测试窗口", "start": "2018-01-24", "end": "2019-01-03"}
    covered = _bars("2017-06-01", 460)
    positions = [
        {"code": "600000", "name": "有价格", "value": 800.0},
        {"code": "600001", "name": "没取到", "value": 200.0},
    ]
    out, _ = px._replay_one(scenario, positions, {"600000": covered}, {"600001": "没有取到历史日线"}, None, None)

    assert out["portfolio"] is not None
    assert out["holdings"][0]["weight"] == pytest.approx(1.0)
    assert out["gaps"][0]["code"] == "600001"
    assert "占这一组市值的 80%" in out["portfolio_note"]


def test_replay_without_any_price_says_so():
    scenario = {"id": "t", "label": "测试窗口", "start": "2018-01-24", "end": "2019-01-03"}
    positions = [{"code": "600001", "name": "没取到", "value": 100.0}]
    out, _ = px._replay_one(scenario, positions, {}, {"600001": "没有取到历史日线"}, None, None)
    assert out["portfolio_note"] == "这段时期没有一只持仓有可用的价格"


def test_path_stats_finds_worst_day_and_drawdown():
    path = pd.Series([1.0, 1.1, 0.9, 1.0], index=pd.bdate_range("2020-01-01", periods=4))
    stats = px._path_stats(path)
    assert stats["max_drawdown"] == pytest.approx(0.9 / 1.1 - 1, abs=1e-6)
    assert stats["worst_day"] == pytest.approx(0.9 / 1.1 - 1, abs=1e-6)
