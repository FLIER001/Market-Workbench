import pytest

import fund
import fund_portfolio as fpf


def test_recent_returns_wait_for_today_and_keep_previous_nav_day(monkeypatch):
    rows = [
        {"date": "2026-08-10", "nav": 2.985, "day_pct": 2.16},
        {"date": "2026-08-07", "nav": 2.922, "day_pct": -0.17},
        {"date": "2026-08-06", "nav": 2.927, "day_pct": -0.68},
    ]
    monkeypatch.setattr(fund, "_recent_nav_rows", lambda _code: rows)

    before_update = fund._recent_return_fields("110022", "2026-08-11")
    assert before_update["nav"] == 2.985
    assert before_update["nav_date"] == "2026-08-10"
    assert before_update["today_return_pct"] is None
    assert before_update["yesterday_return_pct"] == 2.16
    assert before_update["yesterday_return_date"] == "2026-08-10"
    assert before_update["yesterday_return_per_share"] == pytest.approx(2.985 - 2.922)
    assert before_update["yesterday_return_base_per_share"] == 2.922

    rows.insert(0, {"date": "2026-08-11", "nav": 3.0, "day_pct": 0.5})
    after_update = fund._recent_return_fields("110022", "2026-08-11")
    assert after_update["today_return_pct"] == 0.5
    assert after_update["today_return_per_share"] == pytest.approx(3.0 - 2.985)
    assert after_update["yesterday_return_pct"] == 2.16


def test_fund_portfolio_returns_amounts(monkeypatch, tmp_path):
    monkeypatch.setattr(fpf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(fpf, "FPF_FILE", str(tmp_path / "fund_portfolio.json"))
    monkeypatch.setattr(fpf, "_estimate_is_current", lambda: True)
    fpf._save({"holdings": [
        {"code": "110022", "shares": 1000, "cost": 2.5},
        {"code": "000001", "shares": 500, "cost": 1.0},
    ], "closed": []})
    monkeypatch.setattr(fund, "realtime_estimates", lambda _codes: {
        "110022": {
            "name": "已确认基金", "nav": 3.0, "nav_date": "2026-08-11",
            "estimate_pct": 5.0, "estimate_time": None,
            "today_return_pct": 1.0, "today_return_date": "2026-08-11", "today_return_per_share": 0.03,
            "today_return_base_per_share": 2.97,
            "yesterday_return_pct": 2.0, "yesterday_return_date": "2026-08-10", "yesterday_return_per_share": 0.058,
            "yesterday_return_base_per_share": 2.9,
        },
        "000001": {
            "name": "估算基金", "nav": 2.0, "nav_date": "2026-08-10",
            "estimate_pct": 2.0, "estimate_time": None,
            "today_return_pct": None, "today_return_date": None, "today_return_per_share": None,
            "today_return_base_per_share": None,
            "yesterday_return_pct": None, "yesterday_return_date": None, "yesterday_return_per_share": None,
            "yesterday_return_base_per_share": None,
        },
    })

    data = fpf.get_portfolio()
    holding = data["holdings"][0]
    assert holding["today_return_amount"] == 30.0
    assert holding["yesterday_return_amount"] == 58.0
    assert data["totals"]["today_pnl"] == 50.0  # 实际 30 优先，另一只用估算 20 补位
    assert data["totals"]["today_pnl_pct"] == 1.26
    assert data["totals"]["yesterday_pnl"] == 58.0
    assert data["totals"]["yesterday_pnl_pct"] == 2.0
