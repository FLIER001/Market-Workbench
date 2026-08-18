"""因子公式引擎：解析/执行/安全 + 自定义因子端到端（合成数据，不打真 HTTP）。"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from test_factors import _install_panel, _make_panel  # noqa: E402


def test_compile_and_reject():
    import factor_expr

    ok = factor_expr.compile_expr("-close/ts_delta(close,5)")
    assert ok[0] == "bin"
    factor_expr.compile_expr("cs_rank(ts_mean(amount,20)/ts_mean(amount,120))")
    factor_expr.compile_expr("close[1]")
    factor_expr.compile_expr("ts_corr(ret,volume,10)")

    for bad, why in [
        ("import os", "import"),
        ("__import__('os')", "dunder"),
        ("close+unknown", "未知字段"),
        ("ts_mean(close)", "缺窗口"),
        ("ts_mean(close,999)", "窗口超限"),
        ("close[0]", "滞后 0"),
        ("close]", "多余符号"),
        ("eval('1')", "eval"),
        ("", "空"),
        ("close+" * 100 + "close", "过长或节点过多"),
    ]:
        try:
            factor_expr.compile_expr(bad)
            raise AssertionError(f"应当被拒绝：{bad}")
        except ValueError:
            pass


def test_expr_matches_builtin_rev5(monkeypatch):
    """公式版 5 日反转与内置 rev5 的横截面秩应一致（口径对齐验证）。"""
    import factors
    import factor_expr

    panel = _make_panel(seed=11)
    _install_panel(monkeypatch, panel)

    fields = factor_expr.build_fields(panel)
    ast = factor_expr.compile_expr("-(close/close[5]-1)")
    fw = factor_expr.evaluate(ast, fields).stack()

    built = factors.compute_factors(panel, ["rev5"])
    joined = pd.concat([fw.rename("expr"), built["rev5"]], axis=1).dropna()
    # rev5 内置 = -(close/close.shift(5)-1)，两者应完全一致（浮点容差内）
    assert np.allclose(joined["expr"], joined["rev5"], atol=1e-9)


def test_expanded_fields_and_ops(monkeypatch):
    """扩充字段集（K 线形态/相对昨收/多周期动量/量比）与新算子可执行且量纲合理。"""
    import factor_expr

    panel = _make_panel(seed=5)
    _install_panel(monkeypatch, panel)
    fields = factor_expr.build_fields(panel)

    # ret20 与手写 close/close[20]-1 一致
    ast = factor_expr.compile_expr("close/close[20]-1")
    out = factor_expr.evaluate(ast, fields)
    joined = pd.concat([out.stack(), fields["ret20"].stack()], axis=1).dropna()
    assert np.allclose(joined.iloc[:, 0], joined.iloc[:, 1], atol=1e-9)

    # klen = (high-low)/close，合成面板 high=close*1.01, low=close*0.99 → 恒 0.02
    v = fields["klen"].stack().dropna()
    assert np.allclose(v, 0.02, atol=1e-9)

    for e in ["cs_rank(ts_rank(close,20))", "-cs_zscore(ts_skew(ret,60))",
              "ts_kurt(ret,60)", "ts_cov(ret,volume,20)", "ts_rsv(close,20)",
              "cs_rank(kmid*vol_ratio5)", "cs_rank(ts_corr(close,volume,60))"]:
        out = factor_expr.evaluate(factor_expr.compile_expr(e), fields)
        assert out.notna().sum().sum() > 0


def test_cs_rank_is_cross_sectional(monkeypatch):
    """cs_rank 按日横截面排名：每天最大值为股票数 N。"""
    import factor_expr

    panel = _make_panel(seed=3)
    fields = factor_expr.build_fields(panel)
    ast = factor_expr.compile_expr("cs_rank(close)")
    out = factor_expr.evaluate(ast, fields)
    # 用每天有效股票数校验 max rank（面板有 NaN，dropna 后逐行看）
    for date, row in out.iterrows():
        s = row.dropna()
        if len(s) > 1:
            assert s.max() == len(s)


def test_custom_factor_end_to_end(monkeypatch, tmp_path):
    """保存自定义因子 → evaluate/backtest 用 custom: 前缀跑通。"""
    import factor_backtest
    import factor_data
    import factor_expr
    import factors

    import os
    monkeypatch.setattr(factor_expr, "_CUSTOM_FILE", str(tmp_path / "custom.json"))
    monkeypatch.setattr(factor_data, "DATA_DIR", str(tmp_path), raising=False)

    factor_expr.save_custom("lovol", "低波动", "-cs_zscore(ts_std(ret,20))")
    panel = _make_panel(seed=5)
    _install_panel(monkeypatch, panel)

    ev = factors.evaluate("custom:lovol", min_days_listed=1)
    assert ev["factor_name"] == "低波动"
    assert ev["n_days"] > 0
    # 覆盖保存（同 id）
    factor_expr.save_custom("lovol", "低波动v2", "-cs_zscore(ts_std(ret,60))")
    assert len(factor_expr.list_custom()) == 1
    factor_expr.delete_custom("lovol")
    assert factor_expr.list_custom() == []
