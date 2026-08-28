import pytest
from datetime import date, timedelta

import fund_pfs


def _row():
    return {
        "code": "000001", "name": "测试主动基金", "fund_type": "股票型-普通", "strategy": "主动股票",
        "data_date": "2026-08-10", "manager": "测试经理", "manager_names": ["测试经理"], "manager_count": 1,
        "platform": "测试基金", "manager_career_days": 3000, "manager_aum": 30.0, "manager_fund_count": 3,
        "purchase_status": "开放申购", "redemption_status": "开放赎回", "fee_pct": 0.15, "rating_score": 80,
        "近6月": 10.0, "近1年": 20.0, "近2年": 40.0, "近3年": 60.0,
        "return_percentiles": {"近6月": 80.0, "近1年": 85.0, "近2年": 90.0, "近3年": 95.0},
    }


def _detail(days=1500):
    return {
        "assignment": {"team_start_date": "2022-07-01", "team_tenure_days": days, "assignments": []},
        "risk": {"近3年": {"risk_return_peer": 88.0, "resilience_peer": 82.0, "max_drawdown": 18.0, "sharpe": 1.2}},
        "errors": [],
    }


def test_score_uses_neutral_prior_and_confidence_formula():
    result = fund_pfs._score_candidate(_row(), _detail(), [20, 30, 50, 100], [2, 3, 5, 8], [0, 0.08, 0.15, 0.3])

    assert result["quality_components"]["process"] == 50
    assert result["quality_components"]["platform"] == 50
    assert result["potential_components"]["flow"] == 50
    assert result["potential_components"]["platform_trend"] == 50
    assert result["final_score"] == pytest.approx(
        50 + result["confidence"] * (result["raw_score"] - 50) - result["risk_penalty"], abs=0.1
    )
    assert result["gate_pass"] is True


def test_tier_thresholds_are_reachable_and_ordered():
    """分层阈值必须落在模型数学上限内：中性先验(process/platform=50)下
    quality 上限约 81、final 上限约 79 —— core_buy 阈值超过它就永远空缺。"""
    row = {
        **_row(),
        "近6月": 99.0, "近1年": 99.0, "近2年": 99.0, "近3年": 99.0,
        "return_percentiles": {"近6月": 99.0, "近1年": 99.0, "近2年": 99.0, "近3年": 99.0},
        "manager_career_days": 5000, "manager_aum": 5.0, "manager_fund_count": 1, "fee_pct": 0.0,
    }
    detail = {
        **_detail(days=4000),
        "fees": {"total": 0.1, "items": {}},
        "nav": {"features": {}}, "scale": {"features": {}}, "holders": {"features": {}},
        "nav_risk_score": 99.0, "rolling_persistence_score": 99.0,
    }
    best = fund_pfs._score_candidate(
        row, detail, [5.0, 100.0, 200.0, 300.0], [1, 5, 10, 20], [0.0, 0.5, 1.0], [0.1, 0.5, 1.0, 1.5],
    )
    # 满分候选必须能进 core_buy —— 锁住阈值不越过数学上限
    assert best["tier"] == "core_buy", f"best case got {best['tier']}: final={best['final_score']} q={best['quality_score']} c={best['confidence']}"
    # 中游偏上候选应落 watch，不得冒充买入（分位 55-65、费用中游）
    mid = fund_pfs._score_candidate(
        {**_row(), "return_percentiles": {"近6月": 58.0, "近1年": 62.0, "近2年": 65.0, "近3年": 60.0}},
        {**_detail(), "nav_risk_score": 55.0, "rolling_persistence_score": 55.0},
        [20, 30, 50, 100], [2, 3, 5, 8], [0, 0.08, 0.15, 0.3],
    )
    assert mid["tier"] == "watch"
    # 证据收缩后低分候选必须排除
    low = fund_pfs._score_candidate(
        {**_row(), "return_percentiles": {"近6月": 20.0, "近1年": 25.0, "近2年": 30.0, "近3年": 35.0}},
        _detail(days=400),
        [20, 30, 50], [2, 3, 5], [0, 0.15, 0.3],
    )
    assert low["tier"] == "exclude"


def test_short_tenure_excludes_predecessor_risk_period():
    """任期不足 1 年：雪球「近1年」风险指标窗口含前任管理期，不得参与评分。"""
    detail = {
        **_detail(days=204),
        "risk": {"近1年": {"risk_return_peer": 99.0, "resilience_peer": 99.0, "sharpe": 2.0, "max_drawdown": -5.0}},
    }
    result = fund_pfs._score_candidate(_row(), detail, [20, 30, 50], [2, 3, 5], [0, 0.15, 0.3])
    assert result["risk_period"] is None
    # 近1年雪球分位 99 不应进入评分：nav_risk_score 缺失时 risk_score 应为 None
    assert result["risk_metrics"] == {}
    assert "risk_score" not in result  # risk_score 是中间量，验证其未污染输出
    # 任期风险字段 why_good 里不得引用雪球近1年口径
    assert all("近1年" not in item for item in result["why_good"])


def test_manager_evidence_does_not_use_period_before_current_team():
    result = fund_pfs._score_candidate(_row(), _detail(days=800), [20, 30, 50], [2, 3, 5], [0, 0.15, 0.3])

    # 800 天只允许近6月/近1年/近2年；近3年（包含前任）不得进入当前团队证据。
    assert "85" in result["why_good"][0]  # (80 + 85 + 90) / 3


def test_missing_current_assignment_fails_gate():
    result = fund_pfs._score_candidate(_row(), {"assignment": {}, "risk": {}, "errors": []}, [20, 30, 50], [2, 3, 5], [0, 0.15, 0.3])

    assert result["gate_pass"] is False
    assert result["tier"] == "exclude"
    assert "现任经理或本产品任期无法确认" in result["gate_failures"]


def test_nav_and_quarterly_flow_features_are_computed_from_raw_series():
    start = date(2023, 1, 1)
    rows = []
    nav = 1.0
    for index in range(800):
        change = 0.10 if index < 500 else -0.05
        nav *= 1 + change / 100
        rows.append({"date": (start + timedelta(days=index)).isoformat(), "nav": nav, "day_pct": change})
    nav_features = fund_pfs._nav_features(rows, "2023-01-01")
    assert nav_features["n"] == 800
    assert nav_features["rolling_12m_positive_ratio"] is not None
    assert nav_features["max_drawdown"] < 0
    assert nav_features["cvar95"] == pytest.approx(-0.05)

    scale = fund_pfs._scale_features([
        {"date": "2026-06-30", "subscriptions": 20.0, "redemptions": 10.0, "ending_shares": 110.0, "net_assets": 150.0},
        {"date": "2026-03-31", "subscriptions": 0.0, "redemptions": 0.0, "ending_shares": 100.0, "net_assets": 120.0},
    ])
    assert scale["net_share_flow"] == 10.0
    assert scale["net_share_flow_rate"] == 10.0
    assert scale["aum_growth_1q"] == 25.0
    assert scale["quarterly_flow_score"] > 70


def test_point_in_time_store_marks_backfill_and_live_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(fund_pfs, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fund_pfs, "_DB_FILE", str(tmp_path / "pfs.sqlite"))
    rows = [{"code": "000001", "manager_aum": 10, "manager_fund_count": 2, "strategy": "主动股票", "manager": "测试经理"}]
    # nav 日期取 3 天前：_stored_series 的 max_age_days=7 用「距今天数」判断新鲜度，
    # 硬编码日期会在若干天后悄然让本测试变红。
    nav_date = (date.today() - timedelta(days=3)).isoformat()
    details = {"000001": {
        "nav": {"rows": [{"date": nav_date, "nav": 1.2}]},
        "scale": {"rows": [{"date": "2026-06-30", "net_assets": 10}]},
        "holders": {"rows": []},
    }}
    result = fund_pfs._store_observations(rows, details)
    assert result["observation_count"] == 4
    assert result["pit_usable_count"] == 2
    assert result["historical_publication_dates"] == "missing"
    assert fund_pfs._stored_series("000001", "nav", 7)[0]["nav"] == 1.2
