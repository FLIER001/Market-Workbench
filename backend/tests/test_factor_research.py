"""因子研究口径回归：用合成数据核算，不访问网络。"""
import json

import numpy as np
import pandas as pd
import pytest

from test_factors import _install_panel, _make_panel

import factor_backtest
import factors


def test_interval_keeps_warmup_and_forward_labels(monkeypatch):
    panel = _make_panel(seed=42)
    _install_panel(monkeypatch, panel)
    dates = sorted(panel["date"].unique())
    result = factors.evaluate("mom120", start=dates[150], end=dates[155])
    assert result["start"] == dates[150]
    assert result["end"] == dates[155]
    assert result["n_days"] == 6
    assert result["ic_decay"][20]["days"] == 6
    assert result["sample"]["input_rows"] == 6 * 60
    assert result["sample"]["used_rows"] == 6 * 60
    assert result["sample"]["loss_ratio"] == 0
    assert set(result["quantile_returns_by_horizon"]) == {1, 5, 10, 20}
    json.dumps(result, allow_nan=False)


def test_only_requested_factor_is_computed(monkeypatch):
    panel = _make_panel(n_days=80)
    def unwanted(_df):
        raise AssertionError("动量检验不应计算市场相关性或加载指数")
    monkeypatch.setattr(factors, "_index_ret_aligned", unwanted)
    result = factors.compute_factors(panel, ["mom20"])
    assert "mom20" in result
    base = factors.compute_factors(panel, [])
    assert not set(factors.FACTOR_META) & set(base.columns)


def test_full_factor_bank_still_has_row_aligned_series(monkeypatch):
    panel = _make_panel(n_days=260)
    dates = sorted(panel["date"].unique())
    market = pd.Series(np.sin(np.arange(len(dates))) / 100, index=dates)
    monkeypatch.setattr(factors, "_index_ret_aligned", lambda df: df["date"].map(market))
    frame = factors.compute_factors(panel)
    assert len(frame) == len(panel)
    assert set(factors.FACTOR_META) <= set(frame.columns)
    for name in factors.FACTOR_META:
        assert np.any(pd.Series(frame[name]).notna().to_numpy()), name


def test_forward_horizon_uses_calendar_not_next_available_bar():
    panel = _make_panel(n_codes=3, n_days=25)
    dates = sorted(panel["date"].unique())
    code = "600000"
    panel = pd.DataFrame(panel.loc[~((panel["code"] == code) & (panel["date"] == dates[10]))])
    frame = factors.compute_factors(panel, [])
    assert pd.isna(frame.loc[(dates[9], code), "ret_fwd_1"])
    close = panel.set_index(["date", "code"])["close"]
    assert frame.loc[(dates[9], code), "ret_fwd_5"] == pytest.approx(
        close.loc[(dates[14], code)] / close.loc[(dates[9], code)] - 1)


def test_spearman_ranks_only_paired_finite_samples():
    a = np.array([1, 2, 3, 4, 5, 6.0])
    b = np.array([3, np.nan, 1, np.inf, 2, 4.0])
    # 配对秩 [1,2,3,4] 与 [3,1,2,4]，Σd²=6，ρ=1-6*6/(4*(4²-1))。
    assert factors._safe_spearman(a, b) == pytest.approx(0.4)


def test_ic_requires_thirty_paired_samples(monkeypatch):
    panel = _make_panel()
    _install_panel(monkeypatch, panel)
    frame = factors.compute_factors(panel, ["mom20"])
    mask = frame.index.get_level_values("code") >= "600020"
    frame.loc[mask, "ret_fwd_1"] = np.nan
    monkeypatch.setattr(factors, "_factor_frame", lambda *_args: frame)
    with pytest.raises(ValueError, match="有效横截面天数不足"):
        factors.evaluate("mom20", min_days_listed=1)


def test_quantile_turnover_is_new_members_over_current_members():
    codes = [f"{i:06}" for i in range(30)]
    index = pd.MultiIndex.from_product([["2024-01-02", "2024-01-03"], codes], names=["date", "code"])
    values = list(range(30))
    changed = values.copy()
    changed[5], changed[6] = changed[6], changed[5]
    frame = pd.DataFrame({"f": values + changed, "ret_fwd_1": np.arange(60) / 1000}, index=index)
    _returns, turnover, _corr = factors._quantile_stats(frame, "f")
    assert turnover["Q1"] == pytest.approx(1 / 6)
    assert turnover["Q2"] == pytest.approx(1 / 6)
    assert turnover["Q5"] == 0
    assert factors._quantile_bins(pd.Series([1] * 30)) is None
    # 连续分组不能形成时，返回 null，禁止制造代码排序带来的伪价差。
    frame["f"] = 1
    assert factors._quantile_vs_benchmark(frame, "f") == (None, None)


@pytest.mark.parametrize("cost", [0, 1, 1.5, 2, 3])
def test_selected_cost_equals_exact_stress_path(monkeypatch, cost):
    panel = _make_panel(close_fn=lambda i, t: 10 * (1 + 0.001 * i * t))
    _install_panel(monkeypatch, panel)
    monkeypatch.setattr(factor_backtest, "_save_run", lambda _: None)
    result = factor_backtest.run_backtest("mom60", cost=cost, min_days_listed=1)
    assert result["metrics"] == result["cost_stress"][f"{cost:g}x"]
    assert 0 < result["metrics"]["ann_turnover"] < 100


def test_open_rebalance_does_not_use_same_day_close():
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    prices = pd.DataFrame(10.0, index=dates, columns=["a", "b"])
    signals = {dates[0]: (2, ["a", "b"]), dates[1]: (2, ["a", "b"])}
    def run(close):
        return factor_backtest._run_once(signals, close, prices, prices + 1, prices - 1, dates, {"a", "b"}, 0)
    baseline = run(prices)
    intraday_spike = prices.copy()
    intraday_spike.loc[dates[1], "a"] = 100
    changed = run(intraday_spike)
    # 同日盘中价格变动不应影响开盘下单，次日回归原价后净值必须完全回归。
    assert changed["nav"][-1] == baseline["nav"][-1] == 1
    assert changed["metrics"]["ann_turnover"] == pytest.approx(
        baseline["metrics"]["ann_turnover"] / np.mean(changed["nav"]), abs=0.01)


def test_yearly_return_includes_first_day_and_compounds():
    dates = ["2023-12-28", "2023-12-29", "2024-01-02", "2024-01-03"]
    opens = pd.DataFrame(10.0, index=dates, columns=["a"])
    close = pd.DataFrame([10, 11, 12, 13], index=dates, columns=["a"])
    result = factor_backtest._run_once({dates[0]: (1, ["a"])}, close, opens, opens + 1, opens - 1, dates, {"a"}, 0)
    yearly = result["yearly"]
    assert yearly["2024"] == pytest.approx(13 / 11 - 1, abs=1e-4)
    assert (1 + yearly["2023"]) * (1 + yearly["2024"]) - 1 == pytest.approx(result["metrics"]["total_return"], abs=1e-4)


@pytest.mark.parametrize("start,end", [("2024-02-30", None), ("20240102", None), ("2024-12-31", "2024-01-02")])
def test_invalid_window_is_rejected_before_loading(monkeypatch, start, end):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("非法参数不应加载全市场数据")
    monkeypatch.setattr(factors.factor_data, "load_panel", forbidden)
    for run in (factors.evaluate, factor_backtest.run_backtest):
        with pytest.raises(ValueError):
            run("mom60", start=start, end=end)
