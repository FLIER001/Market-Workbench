import fund_portfolio as fpf
import portfolio as pf
import app as app_module
from fastapi.testclient import TestClient


def test_security_ledgers_are_isolated_by_user(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pf, "PF_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(pf.astock, "tencent_quote", lambda codes: {})
    pf._invalidate()
    pf.add_holding("600000", 100, 10, user_id=1)
    pf.add_holding("000001", 200, 9, user_id=2)
    assert [row["code"] for row in pf._load(1)["holdings"]] == ["600000"]
    assert [row["code"] for row in pf._load(2)["holdings"]] == ["000001"]


def test_fund_ledgers_are_isolated_by_user(tmp_path, monkeypatch):
    monkeypatch.setattr(fpf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(fpf, "FPF_FILE", str(tmp_path / "fund_portfolio.json"))
    monkeypatch.setattr(fpf.fund, "realtime_estimates", lambda codes, bypass=False: {})
    fpf._invalidate()
    fpf.add_holding("000001", 100, 1.0, user_id=1)
    fpf.add_holding("000002", 200, 2.0, user_id=2)
    assert [row["code"] for row in fpf._load(1)["holdings"]] == ["000001"]
    assert [row["code"] for row in fpf._load(2)["holdings"]] == ["000002"]


def test_portfolio_api_requires_login_and_isolates_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(app_module.users, "resolve_token", lambda token: {"id": 1, "username": "one"} if token == "one" else {"id": 2, "username": "two"} if token == "two" else None)
    monkeypatch.setattr(app_module.astock, "tencent_quote", lambda codes: {
        code: {"name": code, "price": 10.0, "last_close": 10.0} for code in codes
    })
    pf._invalidate()
    client = TestClient(app_module.app)
    assert client.get("/api/portfolio").status_code == 401
    one = {"X-VR-User-Token": "one"}
    two = {"X-VR-User-Token": "two"}
    assert client.post("/api/portfolio/holding", headers=one, json={"code": "600519", "shares": 1, "cost": 8}).status_code == 200
    assert client.get("/api/portfolio", headers=one).json()["data"]["holdings"][0]["code"] == "600519"
    assert client.get("/api/portfolio", headers=two).json()["data"]["holdings"] == []
