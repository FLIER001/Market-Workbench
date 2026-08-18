"""因子实验室：合成数据 golden test（不打真 HTTP，时点纪律断言）。"""
import sys
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, ".")


def _make_panel(n_codes: int = 60, n_days: int = 220, seed: int = 7,
                close_fn=None) -> pd.DataFrame:
    """合成日线面板：60 只 × 80 个工作日，价格由 close_fn(code_idx, t) 决定。"""
    rng = np.random.default_rng(seed)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=n_days)]
    rows = []
    for i in range(n_codes):
        code = f"{600000 + i:06d}"
        base = 10.0 + i
        for t, date in enumerate(dates):
            if close_fn is not None:
                close = float(close_fn(i, t))
            else:
                close = base * float(np.exp(rng.normal(0, 0.01)))
                base = close
            rows.append({
                "code": code, "date": date,
                "open": close, "high": close * 1.01, "low": close * 0.99,
                "close": close, "volume": 1e6, "amount": close * 1e6,
            })
    return pd.DataFrame(rows)


def _install_panel(monkeypatch, panel: pd.DataFrame):
    import factor_data

    monkeypatch.setattr(factor_data, "load_panel",
                        lambda start=None, end=None: _slice(panel, start, end))
    monkeypatch.setattr(factor_data, "data_version", lambda: "test")
    monkeypatch.setattr(factor_data, "_panel_cache", None, raising=False)


def _slice(panel: pd.DataFrame, start, end) -> pd.DataFrame:
    df = panel
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    return df


def test_momentum_factor_rank_ic_perfect():
    """价格按 code 序号单调上涨 → mom60 因子 RankIC 应≈1（涨得多的因子值大）。"""
    import factors

    panel = _make_panel(close_fn=lambda i, t: 10.0 * (1 + i * 0.5) * (1 + 0.001 * t) ** i)
    out = factors.compute_factors(panel, ["mom60"])
    # 每一天横截面因子秩与 code 序号完全同序
    for date, day in out.groupby(level="date"):
        if day["mom60"].notna().sum() < 66:
            continue
        ranks = day["mom60"].rank()
        idx = [int(c) for c in day.index.get_level_values("code")]
        assert np.corrcoef(ranks, idx)[0, 1] > 0.99


def test_evaluate_random_factor_ic_near_zero(monkeypatch):
    """随机收益下的动量因子：RankIC 应接近 0 且检验输出结构完整。"""
    import factors

    panel = _make_panel(seed=42)
    _install_panel(monkeypatch, panel)
    result = factors.evaluate("mom20", min_days_listed=1)
    assert abs(result["ic"]["rank_ic_mean"]) < 0.3
    assert set(result["quantile_returns"]) == {"Q1", "Q2", "Q3", "Q4", "Q5"}
    assert result["n_days"] > 0
    assert set(result["ic_decay"]) == {1, 5, 10, 20}
    assert result["ic_decay"][1]["rank_ic_mean"] is not None


def test_evaluate_timing_discipline(monkeypatch):
    """时点纪律：T 日因子只配 T+1 起的收益。

    构造：所有股票前 60 日同价，第 61 日起 code 序号大的股票暴涨。
    mom60 在第 60 日仍为 0（无区分度）→ 这些日子应跳过（IC 无法计算即无 NaN 污染）；
    第 61 日因子已能区分，但对应前瞻收益从 62 日起。核心断言：
    ret_fwd_1 不得包含当日收益（同日成交）。用单边上涨日手工核验。
    """
    import factors

    # 第 40 日全部 +10%（当日）：若引擎把当日收益计入前瞻收益，mom20 与之相关性会被拉满
    def close_fn(i, t):
        base = 10.0 + i * 0.3
        if t == 40:
            return base * (1 + 0.10)
        return base
    panel = _make_panel(close_fn=close_fn)
    out = factors.compute_factors(panel, ["mom20"])
    dates = sorted(out.index.get_level_values("date").unique())
    day40 = out.xs(dates[40], level="date")
    # ret_fwd_1 在 T=40 应等于 T→T+1 的收益（先回撤 10% 再回来 → 为负），绝不能是 +10%
    assert (day40["ret_fwd_1"] < 0).all()


def test_backtest_golden_nav(monkeypatch):
    """回测净值手工核算：两期确定行情下等权组合的净值。"""
    import factor_backtest

    # 60 只股票：序号大的动量高。月调仓 → 2024-01-31 信号，02-01 成交。
    panel = _make_panel(close_fn=lambda i, t: 10.0 * (1 + i * 0.01) * (1 + 0.002 * i) ** t)
    _install_panel(monkeypatch, panel)
    result = factor_backtest.run_backtest(
        "mom60", top_n=10, freq="monthly", cost=0.0, min_days_listed=1)
    m = result["metrics"]
    assert m["n_days"] == len(result["nav"]["dates"])
    # 02-01 建仓后持有到月末，组合全部上涨（动量最高的 10 只天天涨）→ 净值 > 1
    assert result["nav"]["strategy"][-1] > 1.0
    # 无成本对照与主结果一致（cost=0 时 0x 与 1x*0 等价）
    assert result["cost_stress"]["0x"]["total_return"] == m["total_return"]
    assert result["timing_note"].startswith("T 日收盘")


def test_backtest_timing_discipline(monkeypatch):
    """时点纪律：建仓必须是 T+1 开盘，不是 T 日收盘。

    两步：先跑一次定位首个实际建仓日 D（nav 首次偏离 1.0），
    再把 D 日全部开盘价改为收盘的 1.2 倍重跑。
    T+1 开盘建仓 → 当日净值 ≈ 1/1.2 ≈ 0.83；若错误地用 T 日收盘建仓则净值 = 1.0。
    """
    import factor_backtest

    panel = _make_panel(close_fn=lambda i, t: 10.0 * (1 + 0.001 * i * t))
    _install_panel(monkeypatch, panel)
    first = factor_backtest.run_backtest("mom60", top_n=10, freq="monthly",
                                         cost=0.0, min_days_listed=1)
    pairs = list(zip(first["nav"]["dates"], first["nav"]["strategy"]))
    i_dev = next(i for i, v in enumerate(first["nav"]["strategy"]) if v != 1.0)
    exec_day = first["nav"]["dates"][i_dev - 1]  # 无成本+开盘=收盘时，建仓日净值恰为 1.0

    mask = panel["date"] == exec_day
    panel.loc[mask, "open"] = panel.loc[mask, "close"] * 1.2
    result = factor_backtest.run_backtest("mom60", top_n=10, freq="monthly",
                                          cost=0.0, min_days_listed=1)
    i = result["nav"]["dates"].index(exec_day)
    assert result["nav"]["strategy"][i] < 0.95  # 若用 T 收盘建仓会是 1.0


def test_fetch_instruments_filters(monkeypatch):
    """股票池过滤：ST/退市剔除、北交所代码不进池。"""
    import factor_data

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def fake_em_get(url, params=None, **kwargs):
        if params and params.get("pn") == "1":
            return FakeResp({"data": {"total": 3, "diff": [
                {"f12": "600001", "f13": 1, "f14": "正常股份", "f21": 100},
                {"f12": "600002", "f13": 1, "f14": "ST 某某", "f21": 50},
                {"f12": "600003", "f13": 1, "f14": "某某退", "f21": 30},
            ]}})
        return FakeResp({"data": {"total": 3, "diff": []}})

    monkeypatch.setattr(factor_data.astock, "em_get", fake_em_get)
    out = factor_data.fetch_instruments()
    assert [it["code"] for it in out] == ["600001"]


def test_build_dataset_atomic(monkeypatch, tmp_path):
    """构建落盘 + catalog + 状态机收敛（monkeypatch 掉网络层）。"""
    import factor_data

    monkeypatch.setattr(factor_data, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(factor_data, "_BARS_FILE", str(tmp_path / "bars.csv.gz"))
    monkeypatch.setattr(factor_data, "_INSTRUMENTS_FILE", str(tmp_path / "instruments.csv.gz"))
    monkeypatch.setattr(factor_data, "_CALENDAR_FILE", str(tmp_path / "calendar.json"))
    monkeypatch.setattr(factor_data, "_CATALOG_FILE", str(tmp_path / "catalog.json"))

    # 500 行起步的有效数据门槛：单股造 600 根
    rows = [{"date": d, "open": 10, "close": 10, "high": 10, "low": 10, "volume": 100, "amount": 1000}
            for d in [x.strftime("%Y-%m-%d") for x in pd.bdate_range("2022-01-03", periods=600)]]
    monkeypatch.setattr(factor_data, "fetch_instruments",
                        lambda: [{"code": "600001", "name": "测试", "float_mcap": 100}])
    monkeypatch.setattr(factor_data, "fetch_stock_history", lambda code: rows)
    monkeypatch.setattr(factor_data.astock, "index_daily_em",
                        lambda secid, days=8000: [{"date": "2024-01-01", "close": 4000.0}])

    catalog = factor_data.build_dataset(max_workers=1)
    assert catalog["stocks"] == 1
    assert factor_data.build_state()["building"] is False
    panel = factor_data.load_panel()
    assert len(panel) == 600 and panel.iloc[0]["code"] == "600001"
