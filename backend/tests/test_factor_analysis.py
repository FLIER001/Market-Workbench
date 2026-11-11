"""因子研究执行层（backend/factor_analysis.py）口径回归：合成数据，不联网、不读真实数据集。

覆盖：输入指纹与变更拒绝、定义/配置校验、阶段标签截断（开发期标签不得进入验证/留出）、
按周期的前瞻对齐、共同样本重算、因子值日度截面秩相关、缺失/同值/常数因子的 null 证据与审计、
冻结公式独立于共享因子目录、遗留 __tmp_ 可执行但目录不可见、取消语义、快照回测不复用旧工件。
"""
import json

import numpy as np
import pandas as pd
import pytest

from test_factors import _install_panel, _make_panel

import factor_analysis
import factor_backtest
import factor_data
import factor_expr
import factors


def _install_inputs(monkeypatch, tmp_path, available=False):
    """把输入指纹指向临时目录：不哈希真实数据集，也不依赖本机是否已构建数据。"""
    for module, attr in ((factor_data, "_BARS_FILE"), (factor_data, "_INSTRUMENTS_FILE"),
                         (factor_data, "_CALENDAR_FILE"), (factor_data, "_CATALOG_FILE"),
                         (factor_pit_module(), "_FUND_FILE")):
        path = tmp_path / f"{attr.strip('_').lower()}.bin"
        monkeypatch.setattr(module, attr, str(path), raising=False)
        if available and attr != "_FUND_FILE":
            path.write_text('{"dates": []}', encoding="utf-8")
    return tmp_path


def factor_pit_module():
    import factor_pit

    return factor_pit


def _phase_config(dates, n_days=220, min_days_listed=0):
    return {
        "horizon": 5,
        "min_days_listed": min_days_listed,
        "development": {"start": dates[100], "end": dates[200]},
        "validation": {"start": dates[201], "end": dates[210]},
        "holdout": {"start": dates[211], "end": dates[min(n_days - 1, 219)]},
    }


def _setup(monkeypatch, tmp_path, n_codes=60, n_days=220, seed=7):
    _install_inputs(monkeypatch, tmp_path, available=True)
    panel = _make_panel(n_codes=n_codes, n_days=n_days, seed=seed)
    _install_panel(monkeypatch, panel)
    dates = sorted(panel["date"].unique())
    return panel, dates


def _definition(expr=None, factor=None, direction="high"):
    definition = {"direction": direction}
    if expr is not None:
        definition["expr"] = expr
    if factor is not None:
        definition["factor"] = factor
    return definition


# ---------------------------------------------------------------------------
# 指纹与校验
# ---------------------------------------------------------------------------

def test_input_manifest_hashes_files_and_code_by_stat(monkeypatch, tmp_path):
    _install_inputs(monkeypatch, tmp_path)
    bars = tmp_path / "bars.csv.gz"
    bars.write_bytes(b"one")
    monkeypatch.setattr(factor_data, "_BARS_FILE", str(bars))

    first = factor_analysis.input_manifest()
    assert first["files"]["bars"] == {"present": True, "bytes": 3,
                                      "sha256": first["files"]["bars"]["sha256"]}
    assert first["files"]["calendar"] == {"present": False}
    assert set(first["code"]) == set(factor_analysis._CODE_FILES)
    assert len(first["id"]) == 16
    # 文件未变 → 走 stat 键缓存，同一对象（不重复读盘）
    assert factor_analysis.input_manifest()["files"]["bars"] == first["files"]["bars"]

    factor_analysis._HASH_CACHE.clear()
    bars.write_bytes(b"two-longer")
    second = factor_analysis.input_manifest()
    assert second["id"] != first["id"]
    assert second["files"]["bars"]["bytes"] == 10


def test_execute_rejects_changed_input_fingerprint(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    manifest = factor_analysis.input_manifest()

    factor_analysis._HASH_CACHE.clear()
    (tmp_path / "bars_file.bin").write_bytes(b"rebuilt")  # 模拟数据重建
    with pytest.raises(factor_analysis.InputChangedError):
        factor_analysis.execute([_definition(factor="mom20")], config, "development", manifest)


@pytest.mark.parametrize("config,message", [
    ({"horizon": 3}, "主检验周期"),
    ({"horizon": 5, "min_days_listed": "60"}, "整数"),
    ({"horizon": 5, "min_days_listed": 99999}, "0-5000"),
])
def test_validate_config_rejects_bad_horizon_and_listing(config, message):
    base = {"horizon": 5, "min_days_listed": 0,
            "development": {"start": "2020-01-01", "end": "2020-06-30"},
            "validation": {"start": "2020-07-01", "end": "2020-12-31"},
            "holdout": {"start": "2021-01-01", "end": "2021-06-30"}}
    with pytest.raises(ValueError, match=message):
        factor_analysis.validate_config({**base, **config})


def test_validate_config_requires_ordered_non_overlapping_periods():
    base = {"horizon": 1, "min_days_listed": 60,
            "development": {"start": "2020-01-01", "end": "2020-06-30"},
            "validation": {"start": "2020-07-01", "end": "2020-12-31"},
            "holdout": {"start": "2021-01-01", "end": "2021-06-30"}}
    assert factor_analysis.validate_config(base) == base

    overlap = {**base, "validation": {"start": "2020-06-30", "end": "2020-12-31"}}
    with pytest.raises(ValueError, match="不重叠"):
        factor_analysis.validate_config(overlap)
    swapped = {**base, "development": {"start": "2020-06-30", "end": "2020-01-01"}}
    with pytest.raises(ValueError, match="晚于"):
        factor_analysis.validate_config(swapped)
    bad_date = {**base, "holdout": {"start": "2021-02-30", "end": "2021-06-30"}}
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        factor_analysis.validate_config(bad_date)
    # 反序期间（留出期早于验证期）也必须被拒
    reversed_order = {**base, "holdout": {"start": "2020-01-01", "end": "2020-02-01"}}
    with pytest.raises(ValueError, match="不重叠"):
        factor_analysis.validate_config(reversed_order)


def test_validate_definition_requires_exactly_one_source():
    builtin = factor_analysis.validate_definition(_definition(factor="mom20"))
    assert builtin == {"factor": "mom20", "direction": "high"}
    expr = factor_analysis.validate_definition(_definition(expr="-close/ts_delta(close,5)"))
    assert expr == {"expr": "-close/ts_delta(close,5)", "direction": "high"}
    assert "factor" not in expr  # Definition 里只出现用到的那个键

    for bad, message in [
        (_definition(factor="mom20", expr="close"), "只能包含"),
        ({"direction": "high"}, "只能包含"),
        (_definition(factor="mom20", direction="up"), "方向"),
        (_definition(factor="nope"), "未知因子"),
        (_definition(expr="close+bogus"), "未知字段"),
        (_definition(expr="import os"), "import"),
        (_definition(expr="1+2"), "常数"),  # 纯标量表达式静态即被拒
        (_definition(expr="abs(5)"), "常数"),
    ]:
        with pytest.raises(ValueError, match=message):
            factor_analysis.validate_definition(bad)


def test_execute_scalar_only_expression_is_rejected_before_loading(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def forbidden():
        raise AssertionError("非法定义不应加载全市场面板")

    monkeypatch.setattr(factor_data, "load_panel", lambda *_a, **_k: forbidden())
    config = {"horizon": 5, "min_days_listed": 0,
              "development": {"start": "2024-01-01", "end": "2024-06-30"},
              "validation": {"start": "2024-07-01", "end": "2024-12-31"},
              "holdout": {"start": "2025-01-01", "end": "2025-06-30"}}
    for expr in ("2+3", "log(1)", "1/0"):
        with pytest.raises(ValueError):
            factor_analysis.execute([_definition(expr=expr)], config, "development", None)


# ---------------------------------------------------------------------------
# 阶段标签截断与周期对齐
# ---------------------------------------------------------------------------

def test_development_forward_labels_never_reach_validation(monkeypatch, tmp_path):
    """阶段截止日之后的前瞻标签必须置空：开发期不得消费验证期的价格。"""
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    result = factor_analysis.execute([_definition(factor="mom20")], config, "development", None)

    assert (result["start"], result["end"]) == (dates[100], dates[200])
    stats = result["native"][0]["by_horizon"]
    for horizon in (1, 5, 10, 20):
        series = stats[str(horizon)]["ic_series"]["dates"]
        # 标签结束日 ≤ 阶段结束日 → 最后一个可用信号日 = 结束日往前 h 个交易日
        assert series[-1] == dates[200 - horizon]
        assert all(d <= dates[200] for d in series)
        # 100 个信号日窗口里，最后 h 天没有区内标签
        assert stats[str(horizon)]["n_days"] == 101 - horizon
        assert stats[str(horizon)]["sample"]["skipped_days"] == horizon
    # 一期样本审计只统计区间内（含被截断的）行
    assert stats["1"]["sample"]["input_rows"] == 101 * 60
    assert stats["1"]["sample"]["used_rows"] == 100 * 60
    assert stats["20"]["sample"]["used_rows"] == 81 * 60
    json.dumps(result, allow_nan=False)


def test_labels_are_aligned_by_horizon_not_reused(monkeypatch, tmp_path):
    """各周期用各自的 ret_fwd_h：h 越大，末段可用天数越少，不得共用一日标签。"""
    panel, dates = _setup(monkeypatch, tmp_path, n_days=240)
    config = _phase_config(dates, n_days=240)
    result = factor_analysis.execute([_definition(factor="mom20")], config, "development", None)
    by_horizon = result["native"][0]["by_horizon"]
    counts = [by_horizon[str(h)]["n_days"] for h in (1, 5, 10, 20)]
    assert counts == sorted(counts, reverse=True)
    assert len(set(counts)) == 4
    # 用面板手工核验一次 h=20 的标签语义：T 日标签 = close(T+20)/close(T)-1
    frame = factors.factor_frame(panel, {"factor": "mom20"})
    close = panel.pivot(index="date", columns="code", values="close").sort_index()
    expected = close.shift(-20) / close - 1.0
    got = frame["ret_fwd_20"].unstack("code")
    aligned = got.reindex(expected.index)
    assert np.allclose(aligned.to_numpy(), expected.to_numpy(), equal_nan=True)


def test_validation_phase_uses_only_its_own_window(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    result = factor_analysis.execute([_definition(factor="mom20")], config, "validation", None)
    assert (result["phase"], result["start"], result["end"]) == ("validation", dates[201], dates[210])
    for horizon in (1, 5, 10, 20):
        series = result["native"][0]["by_horizon"][str(horizon)]["ic_series"]["dates"]
        assert all(dates[201] <= d <= dates[210] for d in series)


# ---------------------------------------------------------------------------
# 方向、比较与相关
# ---------------------------------------------------------------------------

def test_direction_is_applied_once_before_statistics(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    high = factor_analysis.execute([_definition(expr="cs_rank(close)", direction="high")],
                                   config, "development", None)
    flipped_expr = factor_analysis.execute([_definition(expr="-cs_rank(close)", direction="high")],
                                            config, "development", None)
    flipped_dir = factor_analysis.execute([_definition(expr="cs_rank(close)", direction="low")],
                                          config, "development", None)

    a = high["native"][0]["by_horizon"]["5"]
    b = flipped_expr["native"][0]["by_horizon"]["5"]
    c = flipped_dir["native"][0]["by_horizon"]["5"]
    # 方向只取一次负号：显式负号公式 == 低值优先，不能双重取反
    assert b["ic"]["rank_ic_mean"] == pytest.approx(-a["ic"]["rank_ic_mean"], abs=1e-12)
    assert c["ic"]["rank_ic_mean"] == pytest.approx(b["ic"]["rank_ic_mean"], abs=1e-12)
    assert c["long_short_bp"] == pytest.approx(-a["long_short_bp"], abs=1e-9)
    assert a["ic_series"]["values"] != b["ic_series"]["values"]


def test_single_definition_has_no_common_or_correlation(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    result = factor_analysis.execute([_definition(factor="mom20")], config, "development", None)
    assert len(result["native"]) == 1
    assert result["common"] is None and result["correlation"] is None
    assert set(result["native"][0]["by_horizon"]) == {"1", "5", "10", "20"}
    assert result["horizons"] == [1, 5, 10, 20]
    assert result["input_manifest"]["id"]


def test_compare_recomputes_common_sample(monkeypatch, tmp_path):
    """共同样本必须在联合样本上重算，不能拼接各自的标量摘要。"""
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    defs = [_definition(factor="mom20"), _definition(factor="rev5")]
    result = factor_analysis.execute(defs, config, "development", None)

    assert [d["definition"]["factor"] for d in result["native"]] == ["mom20", "rev5"]
    common = result["common"]["by_horizon"]
    assert set(common) == {"1", "5", "10", "20"}
    for horizon, block in common.items():
        assert len(block["results"]) == 2
        # 联合样本行数 ≤ 任一单因子样本行数，且共同样本 n_days ≤ 各自 native
        for index, stats in enumerate(block["results"]):
            assert stats["sample"]["input_rows"] == block["paired_rows"]
            assert block["paired_rows"] <= result["native"][index]["by_horizon"][horizon]["sample"]["paired_rows"]
            assert stats["n_days"] <= result["native"][index]["by_horizon"][horizon]["n_days"]
    assert all(block["paired_rows"] > 0 for block in common.values())
    json.dumps(result, allow_nan=False)


def test_correlation_is_daily_cross_sectional_factor_value_rank(monkeypatch, tmp_path):
    """相关性是因子值的日度截面秩相关，不是 IC 序列相关。"""
    panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    defs = [_definition(factor="mom20"), _definition(expr="cs_rank(close)"), _definition(factor="mom60")]
    result = factor_analysis.execute(defs, config, "development", None)
    corr = result["correlation"]
    assert corr["n_days"] > 0 and corr["sample_rows"] > 0

    # 手工按日截面秩相关复算（同一个联合样本口径）
    columns = []
    for definition in defs:
        column = factors.factor_column(definition)
        frame = factors.factor_frame(panel, definition)
        sign = 1.0 if definition["direction"] == "high" else -1.0
        columns.append((frame[column] * sign).rename(column))
    joint = pd.concat([
        c[(c.index.get_level_values("date") >= dates[100])
          & (c.index.get_level_values("date") <= dates[200])].dropna()
        for c in columns], axis=1, join="inner").dropna()
    daily = [day.rank().corr(method="pearson") for _d, day in joint.groupby(level="date") if len(day) >= 3]
    for i in range(3):
        for j in range(3):
            expected = float(np.mean([m.iloc[i, j] for m in daily]))
            assert corr["matrix"][i][j] == pytest.approx(expected, abs=1e-9)
    # 同一面板的 IC 序列相关是另一统计量，不能拿它冒充因子值相关。
    series = [entry["by_horizon"]["1"]["ic_series"]["values"] for entry in result["native"]]
    assert corr["matrix"][0][1] != pytest.approx(np.corrcoef(series[0], series[1])[0, 1], abs=1e-4)
    # 矩阵对称、对角为 1
    for i in range(3):
        assert corr["matrix"][i][i] == pytest.approx(1.0, abs=1e-9)
        for j in range(3):
            assert corr["matrix"][i][j] == pytest.approx(corr["matrix"][j][i], abs=1e-9)


def test_correlation_matches_known_rank_relationship(monkeypatch, tmp_path):
    """单调变换不改变秩 → 相关为 1；完美反向 → 相关为 -1。"""
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    same = factor_analysis.execute(
        [_definition(expr="cs_rank(close)"), _definition(expr="cs_rank(open)")],
        config, "development", None)
    assert same["correlation"]["matrix"][0][1] > 0.99

    opposite = factor_analysis.execute(
        [_definition(expr="close"), _definition(expr="-close")],
        config, "development", None)
    assert opposite["correlation"]["matrix"][0][1] == pytest.approx(-1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# null 证据与审计
# ---------------------------------------------------------------------------

def _blank_stats(stats):
    return (stats["n_days"] == 0 and stats["ic"] == {"ic_mean": None, "rank_ic_mean": None,
                                                     "rank_ic_ir": None, "rank_ic_positive_ratio": None}
            and stats["ic_series"] == {"dates": [], "values": []}
            and all(v is None for v in stats["quantile_returns"].values())
            and all(v is None for v in stats["quantile_turnover"].values())
            and stats["long_short_bp"] is None and stats["rank_autocorr"] is None
            and stats["by_year"] == {})


def test_all_missing_factor_returns_null_evidence_with_audit(monkeypatch, tmp_path):
    """因子全缺失（窗口超出数据长度）：证据全 null + 审计说明，不伪造 0 分。"""
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    result = factor_analysis.execute([_definition(expr="ts_mean(close,500)")], config, "development", None)
    for horizon in ("1", "5", "10", "20"):
        stats = result["native"][0]["by_horizon"][horizon]
        assert _blank_stats(stats), horizon
        assert stats["sample"]["factor_rows"] == 0
        assert stats["sample"]["paired_rows"] == 0
        assert stats["sample"]["loss_ratio"] == 1.0
        assert stats["sample"]["input_rows"] == 101 * 60
        assert stats["sample"]["skipped_days"] == 101
    assert json.dumps(result, allow_nan=False)


def test_constant_and_tied_factor_returns_null_ic(monkeypatch, tmp_path):
    """常数因子（覆盖完好但没有区分度）必须 null 且审计显示非覆盖问题。"""
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    result = factor_analysis.execute([_definition(expr="cs_rank(close)-cs_rank(close)+1")],
                                     config, "development", None)
    for horizon in ("1", "5", "10", "20"):
        stats = result["native"][0]["by_horizon"][horizon]
        assert stats["ic"]["rank_ic_mean"] is None and stats["ic"]["ic_mean"] is None
        assert stats["ic"]["rank_ic_positive_ratio"] is None
        assert stats["ic_series"] == {"dates": [], "values": []}
        assert stats["n_days"] == 0
        assert stats["sample"]["factor_rows"] > 0  # 有值：不是缺失，而是无区分度
        assert stats["sample"]["paired_rows"] > 0
        assert all(v is None for v in stats["quantile_returns"].values())
        assert stats["long_short_bp"] is None
    assert json.dumps(result, allow_nan=False)


def test_pairs_missing_labels_shrink_sample_not_scores(monkeypatch, tmp_path):
    """停牌造成的配对缺口只影响样本计数与 n_days，不得变成 0 分。"""
    panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    # 让一半股票在区间内缺失标签端点（删掉它们最后 20 天行情）
    half = sorted(panel["code"].unique())[:30]
    panel = panel.loc[~((panel["code"].isin(half)) & (panel["date"] > dates[180]))]
    _install_panel(monkeypatch, panel)
    result = factor_analysis.execute([_definition(factor="mom20")], config, "development", None)
    stats = result["native"][0]["by_horizon"]["20"]
    assert stats["ic"]["rank_ic_mean"] is not None  # 仍有 30 条以上配对
    assert stats["avg_coverage"] < 60
    assert stats["n_days"] == 81  # 仍有 30 条有效配对，恰好达到横截面下限
    assert stats["sample"]["used_rows"] < stats["sample"]["eligible_rows"]


def test_symbols_are_audit_aligned_with_legacy_input_rows(monkeypatch, tmp_path):
    """sample.input_rows 是阶段内（上市过滤前）行数，eligible_rows 是过滤后行数。"""
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates, min_days_listed=120)
    result = factor_analysis.execute([_definition(factor="mom20")], config, "development", None)
    sample = result["native"][0]["by_horizon"]["5"]["sample"]
    assert sample["input_rows"] == 101 * 60
    assert sample["eligible_rows"] < sample["input_rows"]
    assert sample["factor_rows"] <= sample["eligible_rows"]
    assert 0 <= sample["loss_ratio"] <= 1
    json.dumps(result, allow_nan=False)


# ---------------------------------------------------------------------------
# 冻结公式独立性与遗留临时因子
# ---------------------------------------------------------------------------

def test_frozen_expr_runs_without_touching_shared_factor_directory(monkeypatch, tmp_path):
    """公式快照直接在沙箱解析器执行：不读也不写共享自定义因子目录。"""
    _install_inputs(monkeypatch, tmp_path, available=True)
    monkeypatch.setattr(factor_expr, "_CUSTOM_FILE", str(tmp_path / "custom_factors.json"))
    panel = _make_panel(seed=3)
    _install_panel(monkeypatch, panel)
    dates = sorted(panel["date"].unique())
    config = _phase_config(dates)

    def forbidden():
        raise AssertionError("公式执行不得读取共享自定义因子目录")

    monkeypatch.setattr(factor_expr, "list_custom", forbidden)
    definition = _definition(expr="-close/ts_delta(close,5)")
    first = factor_analysis.execute([definition], config, "development", None)
    assert not (tmp_path / "custom_factors.json").exists()  # 不产生临时保存

    # 目录出现无关内容后再跑一次：结果完全一致（同公式同名，独立于目录状态）
    factor_expr.save_custom("other", "别的因子", "cs_rank(close)")
    second = factor_analysis.execute([definition], config, "development", None)
    assert (second["native"][0]["by_horizon"]["5"]["ic"]["rank_ic_mean"]
            == first["native"][0]["by_horizon"]["5"]["ic"]["rank_ic_mean"])
    assert second["native"][0]["definition"] == first["native"][0]["definition"]
    # 同名公式在同一研究内列名一致，便于比对
    assert factors.factor_column(definition) == factors.factor_column(dict(definition))


def test_legacy_temporary_factor_is_executable_but_hidden(monkeypatch, tmp_path):
    """遗留 __tmp_ 因子：目录里不可见，但按 id 仍可执行（原「找不到」根因）。"""
    _install_inputs(monkeypatch, tmp_path, available=True)
    monkeypatch.setattr(factor_expr, "_CUSTOM_FILE", str(tmp_path / "custom_factors.json"))
    panel = _make_panel(seed=3)
    _install_panel(monkeypatch, panel)
    dates = sorted(panel["date"].unique())
    config = _phase_config(dates)

    factor_expr.save_custom("__tmp_42", "临时试跑", "-cs_zscore(ts_std(ret,20))")
    assert factor_expr.list_custom() == []  # 目录不可见
    saved = factor_expr.lookup_custom("__tmp_42")
    assert saved is not None and saved["name"] == "临时试跑"
    assert factors.factor_name_of("custom:__tmp_42") == "临时试跑"

    result = factor_analysis.execute([_definition(factor="custom:__tmp_42")], config, "development", None)
    assert result["native"][0]["by_horizon"]["5"]["n_days"] > 0
    # 遗留路径（factors.evaluate）也必须能跑通
    assert factors.evaluate("custom:__tmp_42", min_days_listed=1)["n_days"] > 0
    # 删除后按 id 查找失败，给出可读错误
    factor_expr.delete_custom("__tmp_42")
    with pytest.raises(ValueError, match="不存在"):
        factor_analysis.validate_definition(_definition(factor="custom:__tmp_42"))


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------

def test_cancelled_execution_raises_and_returns_no_evidence(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)

    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(factor_analysis.CancelledError):
        factor_analysis.execute([_definition(factor="mom20")], config, "development", None,
                                cancelled=cancelled)
    # 取消检查必须发生在工作单元之间（不是只在入口）
    assert calls["n"] > 3

    def never():
        return False

    result = factor_analysis.execute([_definition(factor="mom20")], config, "development", None,
                                    cancelled=never)
    assert result["native"][0]["by_horizon"]["1"]["n_days"] > 0


def test_cancelled_backtest_raises(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    with pytest.raises(factor_analysis.CancelledError):
        factor_analysis.backtest(_definition(factor="mom20"), config, "development",
                                 {"top_n": 10}, None, cancelled=lambda: True)


# ---------------------------------------------------------------------------
# 快照回测
# ---------------------------------------------------------------------------

def test_snapshot_backtest_reuses_simulator_without_writing_artifacts(monkeypatch, tmp_path):
    panel, dates = _setup(monkeypatch, tmp_path, n_days=240)
    config = _phase_config(dates, n_days=240)
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(factor_backtest, "RUNS_DIR", str(runs_dir))

    def forbidden(_payload):
        raise AssertionError("快照回测不得写共享私有工件")

    monkeypatch.setattr(factor_backtest, "_save_run", forbidden)
    # 快照路径必须复用已算好的因子帧：不得再次加载面板
    monkeypatch.setattr(factor_data, "load_panel", lambda *_a, **_k: panel)

    result = factor_analysis.backtest(_definition(factor="mom20"), config, "development",
                                      {"top_n": 10, "freq": "monthly", "cost": 1.0}, None)
    assert not runs_dir.exists()
    assert result["params"]["start"] == dates[100] and result["params"]["end"] == dates[200]
    assert result["params"]["top_n"] == 10 and result["params"]["freq"] == "monthly"
    assert result["factor_name"] == factors.FACTOR_META["mom20"]
    assert result["definition"] == {"factor": "mom20", "direction": "high"}
    assert result["timing_note"].startswith("T 日收盘")
    # 主结果精确对应用户选择的成本倍数（沿用模拟器的压力表语义）
    assert result["metrics"] == result["cost_stress"]["1x"]
    assert set(result["cost_stress"]) == {"0x", "1x", "2x", "3x"}
    json.dumps(result, allow_nan=False)


def test_snapshot_backtest_expression_and_direction(monkeypatch, tmp_path):
    """公式定义 + 低值优先：方向只应用一次，组合排名与统计同向。"""
    panel, dates = _setup(monkeypatch, tmp_path, n_days=240, seed=5)
    config = _phase_config(dates, n_days=240)
    monkeypatch.setattr(factor_backtest, "_save_run", lambda _payload: None)

    up = factor_analysis.backtest(_definition(expr="cs_rank(close)", direction="high"),
                                  config, "development", {"top_n": 5, "cost": 0.0}, None)
    down = factor_analysis.backtest(_definition(expr="cs_rank(close)", direction="low"),
                                    config, "development", {"top_n": 5, "cost": 0.0}, None)
    assert up["params"]["cost_multiplier"] == 0.0
    assert up["metrics"]["n_days"] == down["metrics"]["n_days"] > 0
    equivalent = factor_analysis.backtest(_definition(expr="-cs_rank(close)", direction="high"),
                                          config, "development", {"top_n": 5, "cost": 0.0}, None)
    assert down["nav"] == equivalent["nav"]
    assert up["nav"] != down["nav"]  # 随机面板不预设哪个方向更赚钱


@pytest.mark.parametrize("params,message", [
    ({"top_n": 0}, "持仓数"),
    ({"top_n": 501}, "持仓数"),
    ({"freq": "daily"}, "weekly/monthly"),
    ({"cost": 3.5}, "成本倍数"),
    ({"top_pct": 1.5}, "持仓比例"),
])
def test_backtest_params_are_validated_before_loading(monkeypatch, tmp_path, params, message):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)

    def forbidden(*_a, **_k):
        raise AssertionError("非法参数不应加载全市场数据")

    monkeypatch.setattr(factor_data, "load_panel", forbidden)
    with pytest.raises(ValueError, match=message):
        factor_analysis.backtest(_definition(factor="mom20"), config, "development", params, None)


def test_backtest_top_pct_uses_ratio_path(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path, n_days=240)
    config = _phase_config(dates, n_days=240)
    monkeypatch.setattr(factor_backtest, "_save_run", lambda _payload: None)
    result = factor_analysis.backtest(_definition(factor="mom20"), config, "development",
                                      {"top_pct": 0.2, "freq": "weekly"}, None)
    assert result["params"]["top_pct"] == 0.2
    assert result["metrics"]["n_days"] > 0


def test_phase_name_is_validated(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    with pytest.raises(ValueError, match="development/validation/holdout"):
        factor_analysis.execute([_definition(factor="mom20")], config, "training", None)
    with pytest.raises(ValueError, match="development/validation/holdout"):
        factor_analysis.backtest(_definition(factor="mom20"), config, "training", None, None)


def test_definitions_limit_and_ordering(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    too_many = [_definition(factor="mom20") for _ in range(5)]
    with pytest.raises(ValueError, match="最多比较"):
        factor_analysis.execute(too_many, config, "development", None)
    with pytest.raises(ValueError, match="至少需要"):
        factor_analysis.execute([], config, "development", None)


@pytest.mark.parametrize("definition", [
    {"factor": "mom20", "expr": "", "direction": "high"},
    {"expr": ["close"], "direction": "high"},
    {"factor": 20, "direction": "high"},
    {"factor": "", "direction": "high"},
    {"expr": "ts_mean(1,5)+close", "direction": "high"},
    {"expr": "ts_corr(close,1,5)", "direction": "high"},
    {"expr": "close[-1]", "direction": "high"},
    {"expr": "close[501]", "direction": "high"},
    {"expr": "close.__class__", "direction": "high"},
    {"expr": "open('file')", "direction": "high"},
    {"expr": "(" * 145 + "close" + ")" * 145, "direction": "high"},
])
def test_invalid_definition_is_rejected_without_loading(monkeypatch, definition):
    monkeypatch.setattr(factor_data, "load_panel", lambda: pytest.fail("must validate before loading"))
    with pytest.raises(ValueError):
        factor_analysis.execute([definition], {}, "development", None)


def test_definition_and_config_normalize_without_mutating(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    definition = _definition(expr=" close[1.0] ")
    assert factor_analysis.validate_definition(definition) == _definition(expr="close[1.0]")
    assert definition["expr"] == " close[1.0] "
    config = _phase_config(dates)
    config["horizon"], config["min_days_listed"] = 5.0, 60.0
    normalized = factor_analysis.validate_config(config)
    assert type(normalized["horizon"]) is int and type(normalized["min_days_listed"]) is int
    assert type(config["min_days_listed"]) is float
    with pytest.raises(ValueError, match="主检验周期"):
        factor_analysis.validate_config({**config, "horizon": True})
    with pytest.raises(ValueError, match="不支持"):
        factor_analysis.validate_config({**config, "universe": "imaginary"})


def test_manifest_creation_without_data_but_execution_requires_inputs(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    (tmp_path / "bars_file.bin").unlink()
    manifest = factor_analysis.input_manifest()
    assert manifest["files"]["bars"] == {"present": False}
    assert all(isinstance(value, str) for value in manifest["code"].values())
    monkeypatch.setattr(factor_data, "load_panel", lambda: pytest.fail("missing input must fail first"))
    for operation in ("execute", "backtest"):
        with pytest.raises(ValueError, match="未构建完整"):
            if operation == "execute":
                factor_analysis.execute([_definition(factor="mom20")], _phase_config(dates), "development", manifest)
            else:
                factor_analysis.backtest(_definition(factor="mom20"), _phase_config(dates), "development", {}, manifest)
    assert str(tmp_path) not in json.dumps(manifest)


def test_fingerprint_streaming_cache_and_same_stat_replacement(monkeypatch, tmp_path):
    import builtins
    import os

    path = tmp_path / "large.bin"
    content = b"a" * (2 * (1 << 20) + 7)
    path.write_bytes(content)
    original = builtins.open
    reads = []

    class Reader:
        def __enter__(self):
            self.file = original(path, "rb")
            return self

        def __exit__(self, *args):
            self.file.close()

        def read(self, size):
            assert 0 < size <= 1 << 20
            reads.append(size)
            return self.file.read(size)

    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: Reader() if p == str(path) else original(p, *a, **k))
    first = factor_analysis._fingerprint(str(path))
    assert len(reads) == 4
    assert factor_analysis._fingerprint(str(path)) == first and len(reads) == 4
    first["sha256"] = "mutated"
    assert factor_analysis._fingerprint(str(path))["sha256"] != "mutated"
    old_stat = path.stat()
    replacement = tmp_path / "new.bin"
    replacement.write_bytes(b"b" * len(content))
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    replacement.replace(path)
    second = factor_analysis._fingerprint(str(path))
    assert second["bytes"] == first["bytes"] and second["sha256"] != first["sha256"]
    assert len(reads) == 8


@pytest.mark.parametrize("operation", ["execute", "backtest"])
def test_input_changes_during_final_work_are_rejected(monkeypatch, tmp_path, operation):
    _panel, dates = _setup(monkeypatch, tmp_path)
    manifest = factor_analysis.input_manifest()
    if operation == "execute":
        original = factor_analysis._horizon_stats

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            (tmp_path / "bars_file.bin").write_bytes(b"changed during statistics")
            return result

        monkeypatch.setattr(factor_analysis, "_horizon_stats", changed)
        with pytest.raises(factor_analysis.InputChangedError):
            factor_analysis.execute([_definition(factor="mom20")], _phase_config(dates), "development", manifest)
    else:
        original = factor_backtest._run_once

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            (tmp_path / "bars_file.bin").write_bytes(b"changed during simulator")
            return result

        monkeypatch.setattr(factor_backtest, "_run_once", changed)
        with pytest.raises(factor_analysis.InputChangedError):
            factor_analysis.backtest(_definition(factor="mom20"), _phase_config(dates), "development", {}, manifest)


def test_snapshot_backtest_cancels_during_daily_simulation(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    active = False
    checks = 0
    original = factor_backtest._run_once

    def simulate(*args, **kwargs):
        nonlocal active
        active = True
        return original(*args, **kwargs)

    def cancelled():
        nonlocal checks
        if active:
            checks += 1
        return checks >= 5

    monkeypatch.setattr(factor_backtest, "_run_once", simulate)
    monkeypatch.setattr(factor_backtest, "_save_run", lambda _: pytest.fail("snapshot must not save"))
    with pytest.raises(factor_analysis.CancelledError):
        factor_analysis.backtest(_definition(factor="mom20"), _phase_config(dates), "development", {},
                                 factor_analysis.input_manifest(), cancelled=cancelled)
    assert checks == 5


def test_expression_materializes_only_referenced_price_and_finance_fields(monkeypatch):
    panel = _make_panel(n_days=30)
    ast = factor_expr.compile_expr("vwap0+ret5")
    fields = factor_expr.build_fields(panel, factor_expr.field_dependencies(ast))
    assert set(fields) == {"vwap0", "vwap", "amount", "volume", "close", "ret5"}
    observed = []
    original = factor_expr.build_fields

    def build(panel, names):
        fields = original(panel, names)
        observed.append(set(fields))
        return fields

    def finance(dates, names):
        assert names == {"fn_eps"}
        prices = panel.pivot(index="date", columns="code", values="close").reindex(dates)
        return {"fn_eps": prices * 2}

    monkeypatch.setattr(factor_expr, "build_fields", build)
    monkeypatch.setattr(factors, "_fund_wide_fields", finance)
    frame = factors.factor_frame(panel, _definition(expr="fn_eps/close"))
    assert observed == [{"close"}]
    assert np.allclose(frame[factors.factor_column(_definition(expr="fn_eps/close"))], 2)
    observed.clear()
    monkeypatch.setattr(factors, "_fund_wide_fields", lambda *args: pytest.fail("price-only formula must not read finance"))
    factors.factor_frame(panel, _definition(expr="close"))
    assert observed == [{"close"}]


def test_hidden_legacy_records_survive_catalog_edits_and_snapshot_deletion(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(factor_expr, "_CUSTOM_FILE", str(tmp_path / "custom.json"))
    factor_expr.save_custom("__tmp_a", "临时", "close")
    factor_expr.save_custom("visible", "可见", "open")
    factor_expr.save_custom("__tmp_b", "另一个临时", "high")
    factor_expr.delete_custom("visible")
    assert factor_expr.list_custom() == []
    assert factor_expr.lookup_custom("__tmp_a") and factor_expr.lookup_custom("__tmp_b")
    snapshot = factor_analysis.validate_definition(_definition(factor="custom:__tmp_a", direction="low"))
    assert snapshot == _definition(expr="close", direction="low")
    factor_expr.delete_custom("__tmp_a")
    result = factor_analysis.execute([snapshot], _phase_config(dates), "development", None)
    assert result["native"][0]["by_horizon"]["1"]["n_days"] > 0
    assert factor_expr.lookup_custom("__tmp_b")


@pytest.mark.parametrize("phase", ["development", "validation", "holdout"])
def test_every_phase_and_horizon_masks_exact_label_end(monkeypatch, tmp_path, phase):
    _panel, dates = _setup(monkeypatch, tmp_path, n_days=220)
    config = {"horizon": 20, "min_days_listed": 0,
              "development": {"start": dates[50], "end": dates[99]},
              "validation": {"start": dates[100], "end": dates[149]},
              "holdout": {"start": dates[150], "end": dates[199]}}
    result = factor_analysis.execute([_definition(expr="ts_mean(close,40)")], config, phase, None)
    end_pos = dates.index(config[phase]["end"])
    for horizon in factor_analysis.HORIZONS:
        stats = result["native"][0]["by_horizon"][str(horizon)]
        assert stats["n_days"] == 50 - horizon
        assert stats["ic_series"]["dates"][-1] == dates[end_pos - horizon]
        assert stats["sample"]["factor_rows"] == 50 * 60  # 完整历史预热保留


def test_no_label_or_no_eligible_rows_yields_null_evidence(monkeypatch, tmp_path):
    _panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    config["development"] = {"start": dates[200], "end": dates[200]}
    for minimum in (0, 5000):
        result = factor_analysis.execute([_definition(expr="close/0"), _definition(expr="close")],
                                         {**config, "min_days_listed": minimum}, "development", None)
        for item in result["native"]:
            for stats in item["by_horizon"].values():
                assert _blank_stats(stats)
                assert stats["sample"]["skipped_days"] == 1
        for block in result["common"]["by_horizon"].values():
            assert block["paired_rows"] == 0
        assert result["correlation"]["n_days"] == 0
        assert result["correlation"]["matrix"] == [[None, None], [None, None]]
        json.dumps(result, allow_nan=False)


def test_common_sample_is_recomputed_on_exact_stock_date_pairs_for_all_horizons(monkeypatch, tmp_path):
    panel, dates = _setup(monkeypatch, tmp_path, n_days=65)
    config = {"horizon": 10, "min_days_listed": 0,
              "development": {"start": dates[10], "end": dates[50]},
              "validation": {"start": dates[51], "end": dates[55]},
              "holdout": {"start": dates[56], "end": dates[64]}}
    definitions = [_definition(factor="mom20"), _definition(factor="rev5"), _definition(factor="mom60", direction="low")]
    base = factors.compute_factors(panel, [])
    rng = np.random.default_rng(101)
    frames = {}
    for i, definition in enumerate(definitions):
        name = definition["factor"]
        frame = base.copy()
        frame[name] = rng.normal(size=len(frame))
        frame.loc[frame.index.get_level_values("code").isin([f"{600000 + j:06d}" for j in range(i * 5, i * 5 + 5)]), name] = np.nan
        frames[name] = frame
    loads = 0

    def load():
        nonlocal loads
        loads += 1
        return panel

    monkeypatch.setattr(factor_data, "load_panel", load)
    monkeypatch.setattr(factors, "factor_frame", lambda panel, definition: frames[definition["factor"]].copy())
    result = factor_analysis.execute(definitions, config, "development", None)
    assert loads == 1
    for horizon in factor_analysis.HORIZONS:
        ret = f"ret_fwd_{horizon}"
        cols = [(frames[d["factor"]][d["factor"]] * (-1 if d["direction"] == "low" else 1)).rename(f"f{i}")
                for i, d in enumerate(definitions)]
        joint = pd.concat(cols + [base[ret]], axis=1).dropna()
        index_dates = joint.index.get_level_values("date")
        joint = joint[(index_dates >= dates[10]) & (index_dates <= dates[50 - horizon])]
        block = result["common"]["by_horizon"][str(horizon)]
        assert block["paired_rows"] == (41 - horizon) * 45 == len(joint)
        for i, stats in enumerate(block["results"]):
            daily_ic = [np.corrcoef(day[f"f{i}"].rank(), day[ret].rank())[0, 1] for _, day in joint.groupby(level="date")]
            assert stats["ic"]["rank_ic_mean"] == pytest.approx(np.mean(daily_ic))
            assert stats["sample"]["used_rows"] == len(joint)
            returns = {f"Q{q}": [] for q in range(1, 6)}
            for _, day in joint.groupby(level="date"):
                bins = pd.Series(pd.qcut(day[f"f{i}"], 5, labels=False), index=day.index) + 1
                for q in range(1, 6):
                    returns[f"Q{q}"].append(day.loc[bins == q, ret].mean() * 1e4)
            for q in returns:
                assert stats["quantile_returns"][q] == pytest.approx(np.mean(returns[q]))
            assert result["native"][i]["by_horizon"][str(horizon)]["sample"]["paired_rows"] == (41 - horizon) * 55
    assert result["correlation"]["sample_rows"] == 41 * 45
    json.dumps(result, allow_nan=False)


def test_known_calendar_does_not_skip_a_market_wide_bar_gap(monkeypatch, tmp_path):
    panel, dates = _setup(monkeypatch, tmp_path, n_days=65)
    monkeypatch.setattr(factor_data, "load_calendar", lambda: dates)
    panel = pd.DataFrame(panel.loc[panel["date"] != dates[40]])
    _install_panel(monkeypatch, panel)
    frame = factors.compute_factors(panel, [])
    for horizon in factor_analysis.HORIZONS:
        assert pd.Series(frame.xs(dates[40 - horizon], level="date")[f"ret_fwd_{horizon}"]).isna().all()
    fields = factor_expr.build_fields(panel, ["close"])
    lag = factor_expr.evaluate(factor_expr.compile_expr("close[1]"), fields)
    assert lag.loc[dates[41]].isna().all()  # 昨日缺行不能偷换成前一根可用行情
    config = {"horizon": 10, "min_days_listed": 0,
              "development": {"start": dates[10], "end": dates[50]},
              "validation": {"start": dates[51], "end": dates[55]},
              "holdout": {"start": dates[56], "end": dates[64]}}
    result = factor_analysis.execute([_definition(expr="close")], config, "development", None)
    for horizon, n_days in ((1, 38), (5, 34), (10, 29), (20, 20)):
        stats = result["native"][0]["by_horizon"][str(horizon)]
        assert stats["n_days"] == n_days
        last_signal = 50 - horizon
        assert stats["ic_series"]["dates"][-1] == dates[last_signal if last_signal != 40 else 39]


def test_backtest_calendar_gap_cannot_delay_signal_to_a_later_open(monkeypatch, tmp_path):
    panel, dates = _setup(monkeypatch, tmp_path)
    config = _phase_config(dates)
    monkeypatch.setattr(factor_data, "load_calendar", lambda: dates)
    window = dates[100:201]
    signal = factor_backtest._rebalance_dates(window, "weekly")[0]
    missing = dates[dates.index(signal) + 1]
    panel = pd.DataFrame(panel.loc[panel["date"] != missing])
    _install_panel(monkeypatch, panel)
    original = factor_backtest._run_once
    paths = 0

    def simulate(candidates, close, opens, high, low, window_dates, *args, **kwargs):
        nonlocal paths
        paths += 1
        assert missing in candidates and missing in window_dates
        assert opens.loc[missing].isna().all()
        assert dates[dates.index(missing) + 1] not in candidates
        return original(candidates, close, opens, high, low, window_dates, *args, **kwargs)

    monkeypatch.setattr(factor_backtest, "_run_once", simulate)
    result = factor_analysis.backtest(_definition(expr="close"), config, "development",
                                     {"freq": "weekly", "cost": 0.0}, None)
    assert paths == 4
    assert result["nav"]["dates"] == window
    assert result["metrics"]["n_days"] == 101
    json.dumps(result, allow_nan=False)
