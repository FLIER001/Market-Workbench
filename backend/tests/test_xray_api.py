"""新端点的接线与账号隔离：未登录取不到、登录取得到、字段形状对得上。
网络全打桩，只验路由与序列化，不碰真实数据源。
"""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import abnormal
import app as app_module
import cache_runtime
import portfolio as pf
import portfolio_xray as pxf


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pf, "PF_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(
        app_module.users, "resolve_token",
        lambda token: {"id": 1, "username": "one"} if token == "one" else None,
    )
    pf._invalidate()
    cache_runtime.reset_for_tests()
    pf.add_holding("600000", 100, 10.0, "2025-06-01", user_id=1)

    dates = [str(d.date()) for d in pd.bdate_range("2025-06-02", periods=200)]

    def fake_kline(code, period="day", count=250):
        return [{"date": d, "close": 10.0 + i * 0.05} for i, d in enumerate(dates[-count:])]

    def fake_history(code):
        return [{"date": d, "close": 10.0 + i * 0.05} for i, d in enumerate(dates)]

    def fake_index(secid, days=250):
        return [{"date": d, "close": 3000.0 + i} for i, d in enumerate(dates)]

    # pxf.astock 与 abnormal.astock 是同一个模块对象：桩只打一次，否则后者覆盖前者
    monkeypatch.setattr(pxf.astock, "tencent_quote",
                        lambda codes: {code: {"name": "浦发银行", "price": 20.0} for code in codes})
    monkeypatch.setattr(pxf.astock, "tencent_kline", fake_kline)
    monkeypatch.setattr(pxf.astock, "index_daily_em", fake_index)
    monkeypatch.setattr(pxf.factor_data, "fetch_stock_history", fake_history)
    monkeypatch.setattr(pxf, "_industry", lambda code: {"path": "金融-银行-股份制与城商行", "label": "银行"})
    return TestClient(app_module.app)


@pytest.mark.parametrize("path", ["/api/portfolio/xray", "/api/portfolio/replay"])
def test_new_endpoints_require_login(client, path):
    assert client.get(path).status_code == 401


def test_xray_endpoint_returns_metrics(client):
    resp = client.get("/api/portfolio/xray", headers={"X-VR-User-Token": "one"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is True, data
    assert data["inputs"]["symbols"] == ["600000"]
    assert data["inputs"]["names"] == {"600000": "浦发银行"}
    assert data["concentration"]["effective_n"] is not None
    assert data["tail_risk"]["var_95"] is not None
    assert data["industry_exposure"]["rows"] == [{
        "industry": "银行", "weight": 1.0,
        "members": [{"code": "600000", "name": "浦发银行", "path": "金融-银行-股份制与城商行"}],
    }]


def test_replay_endpoint_returns_five_scenarios(client):
    resp = client.get("/api/portfolio/replay", headers={"X-VR-User-Token": "one"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is True, data
    assert [s["id"] for s in data["scenarios"]] == [s["id"] for s in pxf.SCENARIOS]
    assert data["benchmark_label"] == "沪深300指数"


def test_replay_marks_windows_the_holding_did_not_exist(client):
    # 造的数据只从 2025-06 开始，五段历史窗口里一只都没有价格
    resp = client.get("/api/portfolio/replay", headers={"X-VR-User-Token": "one"})
    scenarios = resp.json()["data"]["scenarios"]
    assert all(s["portfolio"] is None for s in scenarios)
    assert all(s["gaps"] for s in scenarios)


def test_abnormal_endpoint_shape(client):
    resp = client.get("/api/stock/abnormal?codes=600000,510300,00700",
                      headers={"X-VR-User-Token": "one"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [row["code"] for row in data["rows"]] == ["600000"]
    assert data["rows"][0]["board"] == "sh_main"
    assert data["rows"][0]["result"]["abnormal"]["days"] == 3
    assert data["rule_source"].startswith("上交所 / 深交所")
    assert any("基金 / 债券" in gap for gap in data["gaps"])


def test_abnormal_endpoint_without_codes(client):
    resp = client.get("/api/stock/abnormal", headers={"X-VR-User-Token": "one"})
    assert resp.status_code == 200
    assert resp.json()["data"]["gaps"] == ["没有传入代码"]
