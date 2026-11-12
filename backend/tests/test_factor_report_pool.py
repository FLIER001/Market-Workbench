"""回测报表与规则候选池的口径：月度收益、回撤段、夏普区间可复现、分段、
区间条件的三态判定、ST / 停牌、基金与非 A 股排除、成分池取不到时不静默成空。
"""
import numpy as np
import pandas as pd
import pytest

import candidate_pool as cp
import factor_report as fr


def _dates(n, start="2023-01-02"):
    return [str(d.date()) for d in pd.bdate_range(start, periods=n)]


# ——— 报表层 ———


def test_monthly_returns_uses_month_end_and_starts_from_one():
    dates = ["2023-01-03", "2023-01-31", "2023-02-15", "2023-02-28"]
    nav = [1.0, 1.10, 1.05, 1.21]
    out = fr.monthly_returns(dates, nav)
    assert out["2023-01"] == pytest.approx(0.10, abs=1e-6)
    # 二月以一月末净值为基准：1.21 / 1.10 − 1 = 10%
    assert out["2023-02"] == pytest.approx(0.10, abs=1e-6)


def test_drawdown_episodes_finds_peak_trough_recovery():
    dates = _dates(6)
    nav = [1.0, 1.2, 0.9, 0.8, 1.25, 1.3]
    episodes = fr.drawdown_episodes(dates, nav)
    assert len(episodes) == 1
    worst = episodes[0]
    assert worst["peak"] == dates[1] and worst["trough"] == dates[3]
    assert worst["recovered"] == dates[4]
    assert worst["depth"] == pytest.approx(0.8 / 1.2 - 1, abs=1e-4)   # 落库保留 4 位小数


def test_drawdown_marks_unrecovered_episode():
    dates = _dates(4)
    episodes = fr.drawdown_episodes(dates, [1.0, 1.2, 1.0, 0.95])
    assert episodes[0]["recovered"] is None
    assert "没涨回前高" in episodes[0]["note"]


def test_sharpe_ci_is_reproducible_and_brackets_the_point_estimate():
    rng = np.random.default_rng(11)
    nav = [float(x) for x in 100 * np.cumprod(1 + rng.normal(0.0006, 0.011, 500))]
    first = fr.sharpe_ci(nav)
    second = fr.sharpe_ci(nav)
    assert first == second                      # 固定种子：同输入同输出
    assert first["low"] <= first["sharpe"] <= first["high"]
    assert first["iters"] == fr.BOOTSTRAP_ITERS


def test_sharpe_ci_refuses_thin_sample():
    assert fr.sharpe_ci([1.0, 1.01, 1.02])["low"] is None
    assert "不足" in fr.sharpe_ci([1.0, 1.01, 1.02])["note"]


def test_segments_split_the_window_and_report_each_part():
    # 净值序列以 1.0 为初始本金（回测的 nav_list 就是这种口径）
    rng = np.random.default_rng(5)
    nav = [float(x) for x in np.cumprod(1 + rng.normal(0.0004, 0.01, 400))]
    parts = fr.segments(_dates(400), nav, parts=2)
    assert len(parts) == 2
    assert parts[0]["start"] == _dates(400)[0]
    assert parts[1]["end"] == _dates(400)[-1]
    # 第二段以第一段末尾净值为基准，两段复利应能接回整段（第一段基准是 1.0）
    chain = (1 + parts[0]["return"]) * (1 + parts[1]["return"]) - 1
    assert chain == pytest.approx(nav[-1] - 1, abs=1e-3)


def test_build_returns_all_four_blocks_and_handles_bad_input():
    rng = np.random.default_rng(7)
    nav = [float(x) for x in np.cumprod(1 + rng.normal(0.0003, 0.01, 300))]
    out = fr.build(_dates(300), nav, nav)
    assert set(out) >= {"monthly_returns", "drawdowns", "sharpe_ci", "segments"}
    assert fr.build([], [], None)["monthly_returns"] == {}


# ——— 规则候选池 ———

QUOTE = {"price": 10.0, "change_pct": 1.2, "pe_ttm": 18.0, "pb": 2.1, "mcap_yi": 320.0,
         "turnover_pct": 3.5}


def test_range_checks_three_states():
    row = cp.evaluate("600519", "贵州茅台", QUOTE, {"pe_ttm": {"min": 10, "max": 20}})
    assert row["verdict"] == cp.PASS
    checks = {c["key"]: c for c in row["checks"]}
    assert checks["pe_ttm"]["status"] == cp.PASS
    assert checks["pe_ttm"]["value"] == 18.0

    high = cp.evaluate("600519", "贵州茅台", QUOTE, {"pe_ttm": {"max": 15}})
    assert high["verdict"] == cp.FAIL
    assert "高于上限" in high["checks"][0]["reason"]

    missing = cp.evaluate("600519", "贵州茅台", {**QUOTE, "pe_ttm": 0}, {"pe_ttm": {"max": 15}})
    assert missing["verdict"] == cp.MISSING, "0 是腾讯的缺值填充，不当成真实市盈率"


def test_negative_pe_is_compared_as_is():
    # 亏损公司 PE 为负：设「PE ≥ 0」时它会不满足，而不是被当成缺数据
    row = cp.evaluate("000001", "平安银行", {**QUOTE, "pe_ttm": -12.0}, {"pe_ttm": {"min": 0}})
    assert row["verdict"] == cp.FAIL
    assert row["checks"][0]["value"] == -12.0


def test_st_and_halted_switches():
    st = cp.evaluate("000002", "*ST泛海", QUOTE, {"exclude_st": True})
    assert st["verdict"] == cp.FAIL

    halted = cp.evaluate("000003", "某股", {**QUOTE, "price": 0}, {"exclude_halted": True})
    assert halted["verdict"] == cp.FAIL

    no_quote = cp.evaluate("000004", "某股", None, {"exclude_halted": True})
    assert no_quote["verdict"] == cp.MISSING
    assert "开盘前" in no_quote["checks"][0]["reason"]


def test_fail_beats_missing_in_overall_verdict():
    row = cp.evaluate("000005", "某股", {**QUOTE, "pb": None}, {"pe_ttm": {"max": 5}, "pb": {"max": 3}})
    assert row["verdict"] == cp.FAIL


def test_pool_groups_and_sorts_without_ranking(monkeypatch):
    monkeypatch.setattr(cp, "_quotes", lambda codes: {
        "600000": {**QUOTE, "name": "浦发银行"},
        "600001": {**QUOTE, "name": "某ST股", "pe_ttm": 50.0},
        "600002": {**QUOTE, "name": "缺值股", "pb": None},
    })
    out = cp.build_pool("watch", ["600001", "600000", "600002"], {"pe_ttm": {"max": 30}, "pb": {"max": 5}})
    assert out["counts"] == {"pass": 1, "missing": 1, "fail": 1}
    assert [r["code"] for r in out["rows"]] == ["600000", "600001", "600002"]   # 按代码排
    assert out["groups"]["pass"] == ["600000"]


def test_pool_excludes_funds_and_non_a_shares(monkeypatch):
    monkeypatch.setattr(cp, "_quotes", lambda codes: dict.fromkeys(codes, QUOTE))
    out = cp.build_pool("watch", ["600000", "510300", "00700"], {})
    assert [r["code"] for r in out["rows"]] == ["600000"]
    assert any("不在这个范围" in gap for gap in out["gaps"])


def test_pool_without_conditions_marks_everything_pass(monkeypatch):
    monkeypatch.setattr(cp, "_quotes", lambda codes: dict.fromkeys(codes, QUOTE))
    out = cp.build_pool("watch", ["600000"], {})
    assert out["rows"][0]["verdict"] == cp.PASS
    assert out["rows"][0]["checks"] == []


def test_pool_constituent_failure_is_not_silent(monkeypatch):
    def boom(universe):
        raise RuntimeError(f"{universe} 的成分股没取到")

    monkeypatch.setattr(cp, "constituents", boom)
    with pytest.raises(RuntimeError):
        cp.build_pool("hs300", [], {})
