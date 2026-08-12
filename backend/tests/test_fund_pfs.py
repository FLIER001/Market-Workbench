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
    details = {"000001": {
        "nav": {"rows": [{"date": "2026-08-10", "nav": 1.2}]},
        "scale": {"rows": [{"date": "2026-06-30", "net_assets": 10}]},
        "holders": {"rows": []},
    }}
    result = fund_pfs._store_observations(rows, details)
    assert result["observation_count"] == 4
    assert result["pit_usable_count"] == 2
    assert result["historical_publication_dates"] == "missing"
    assert fund_pfs._stored_series("000001", "nav", 7)[0]["nav"] == 1.2
