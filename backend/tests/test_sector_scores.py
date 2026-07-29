"""申万一级行业评分纯逻辑与 API 契约测试。"""

from datetime import date

import sector_scores


def _row(code, name, close, pe, pb, turnover, share, return_pct=1.0):
    return {
        "code": code,
        "name": name,
        "close": close,
        "return_pct": return_pct,
        "turnover_rate": turnover,
        "pe": pe,
        "pb": pb,
        "turnover_share": share,
        "float_market_cap": 1000,
        "dividend_yield": 2,
    }


def test_build_scores_uses_sw_monthly_history():
    snapshots = []
    for index in range(13):
        year = 2023 + index // 12
        month = index % 12 + 1
        day = date(year, month, 28).isoformat()
        snapshots.append((
            day,
            {
                "801010": _row(
                    "801010",
                    "农林牧渔",
                    1000 + index * 30,
                    30 - index,
                    3 - index * 0.05,
                    20 + index * 2,
                    1 + index * 0.05,
                ),
                "801030": _row(
                    "801030",
                    "基础化工",
                    1200 + index * 5,
                    15 + index * 0.3,
                    1.5 + index * 0.03,
                    35 - index,
                    3 - index * 0.05,
                ),
                "801040": _row(
                    "801040",
                    "钢铁",
                    900 + index * 8,
                    12,
                    1.2,
                    25,
                    2,
                ),
            },
        ))

    data = sector_scores.build_scores_from_snapshots(snapshots)
    rows = {row["code"]: row for row in data["industries"]}
    agriculture = rows["801010"]

    assert data["schema_version"] == 5
    assert data["current_frequency"] == "monthly"
    assert data["methodology"]["classification"] == "申万一级行业（2021版，31个行业）"
    assert data["history_samples"] == 13
    assert agriculture["valuation"]["pe_percentile"] < 50
    assert agriculture["valuation"]["pb_percentile"] < 50
    assert agriculture["prosperity"]["earnings_yoy"] > 0
    assert agriculture["attention"]["turnover_rate_percentile"] == 100.0
    assert all(row["score"] is not None for row in rows.values())


def test_daily_snapshot_drives_current_values_with_monthly_history():
    monthly = []
    for index in range(13):
        year = 2023 + index // 12
        month = index % 12 + 1
        monthly.append((
            date(year, month, 28).isoformat(),
            {
                "801010": _row("801010", "农林牧渔", 1000 + index * 20, 30 - index, 3, 20, 1),
                "801030": _row("801030", "基础化工", 1200 + index * 10, 20, 2, 25, 2),
                "801040": _row("801040", "钢铁", 900 + index * 5, 12, 1.2, 15, 1.5),
            },
        ))
    daily = []
    for index in range(13):
        daily.append((
            date(2024, 2, index + 1).isoformat(),
            {
                "801010": _row("801010", "农林牧渔", 1300 + index, 17, 2.3, 5 + index, 0.5 + index * 0.1, 2),
                "801030": _row("801030", "基础化工", 1320, 20, 2, 20, 2, -1),
                "801040": _row("801040", "钢铁", 980, 12, 1.2, 12, 1.4, 0),
            },
        ))

    data = sector_scores.build_scores_from_snapshots(monthly, daily)
    agriculture = next(row for row in data["industries"] if row["code"] == "801010")

    assert data["current_frequency"] == "daily"
    assert data["as_of"] == "2024-02-13"
    assert data["monthly_as_of"] == "2024-01-28"
    assert data["daily_history_samples"] == 13
    assert agriculture["latest_return"] == 2
    assert agriculture["attention"]["daily_history_samples"] == 13
    assert agriculture["attention"]["turnover_rate_percentile"] == 100.0


def test_sw_snapshot_rejects_incomplete_data():
    try:
        sector_scores._snapshot_from_results([
            {
                "swindexcode": "801010",
                "swindexname": "农林牧渔",
                "bargaindate": "2026-06-30T08:00:00+08:00",
            },
        ])
    except ValueError as error:
        assert "不完整" in str(error)
    else:
        raise AssertionError("不完整的申万一级行业快照应被拒绝")


def test_sector_scores_api_contract(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    expected = {"schema_version": 4, "industries": []}
    monkeypatch.setattr(
        app_module.sector_scores_layer,
        "get_sector_scores",
        lambda force=False: {**expected, "force": force},
    )
    client = TestClient(app_module.app)

    response = client.get("/api/sector-scores?refresh=true")

    assert response.status_code == 200
    assert response.json()["data"]["schema_version"] == 4
    assert response.json()["data"]["force"] is True
