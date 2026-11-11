"""因子研究工作台路由回归：契约、账号隔离、错误码（不联网，不跑真实计算）。

计算替身复用 test_factor_lab.FakeAnalysis；路由测只验证入队与状态机，
真实子进程计算见 test_factor_lab.test_real_worker_compute_integration。
"""

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from test_factor_lab import FakeAnalysis, _valid_config

import factor_lab
import factor_lab_routes
import users


def _current_user(request: Request) -> dict:
    bearer = request.headers.get("authorization", "")
    user = users.resolve_token(bearer[7:] if bearer.startswith("Bearer ") else "")
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return user


@pytest.fixture
def client(tmp_path, monkeypatch):
    factor_lab.stop_worker()
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()
    factor_lab.init_db()
    factor_lab.start_worker()
    monkeypatch.setattr(factor_lab, "_analysis", lambda: FakeAnalysis())
    monkeypatch.setattr(factor_lab, "submit", lambda rid: None)  # 路由测不跑子进程
    app = FastAPI()
    app.include_router(factor_lab_routes.create_router(_current_user))
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        factor_lab.stop_worker()


def _auth(name: str) -> dict:
    users.register(name, "secret1")
    token = users.login(name, "secret1")["token"]
    return {"Authorization": f"Bearer {token}"}


def _uid(headers) -> int:
    user = users.resolve_token(headers["Authorization"][7:])
    assert user is not None
    return user["id"]


def _create_study(client, headers, name="研究A"):
    r = client.post("/api/factor/research", headers=headers,
                    json={"name": name, "description": "说明", "config": _valid_config()})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _dev_run(client, headers, sid, name="dev", factor="mom20"):
    r = client.post(f"/api/factor/research/{sid}/runs", headers=headers,
                    json={"name": name, "definition": {"factor": factor, "direction": "high"}})
    assert r.status_code == 200, r.text
    run = r.json()["data"]
    factor_lab.finish_run(run["id"], status="completed", result={"native": [{}], "horizons": [1, 5, 10, 20]})
    return run


def test_requires_authentication(client):
    assert client.get("/api/factor/research").status_code == 401
    assert client.post("/api/factor/research", json={"name": "x", "config": _valid_config()}).status_code == 401


def test_study_crud_and_detail_shape(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    assert study["config"]["horizon"] == 5
    assert study["baseline_id"] is None and study["archived"] is False
    assert client.get("/api/factor/research", headers=headers).json()["data"][0]["id"] == study["id"]

    detail = client.get(f"/api/factor/research/{study['id']}", headers=headers).json()["data"]
    assert set(detail) == {"study", "runs"} and detail["runs"] == []

    renamed = client.put(f"/api/factor/research/{study['id']}", headers=headers, json={"name": "改名"})
    assert renamed.json()["data"]["name"] == "改名"
    archived = client.put(f"/api/factor/research/{study['id']}", headers=headers, json={"archived": True})
    assert archived.json()["data"]["archived"] is True


def test_invalid_config_is_400(client):
    headers = _auth("u1")
    bad = {**_valid_config(), "horizon": 2}
    r = client.post("/api/factor/research", headers=headers, json={"name": "x", "config": bad})
    assert r.status_code == 400
    assert r.json()["detail"]


def test_cross_account_records_are_404(client):
    headers_a = _auth("u1")
    study = _create_study(client, headers_a)
    run = _dev_run(client, headers_a, study["id"])
    headers_b = _auth("u2")

    assert client.get("/api/factor/research", headers=headers_b).json()["data"] == []  # B 看不到 A 的研究
    assert client.get(f"/api/factor/research/{study['id']}", headers=headers_b).status_code == 404
    assert client.put(f"/api/factor/research/{study['id']}", headers=headers_b, json={"name": "x"}).status_code == 404
    assert client.get(f"/api/factor/research/{study['id']}/runs/{run['id']}", headers=headers_b).status_code == 404
    assert client.post(f"/api/factor/research/{study['id']}/runs/{run['id']}/cancel", headers=headers_b).status_code == 404
    assert client.get(f"/api/factor/research/{study['id']}/export", headers=headers_b).status_code == 404
    assert client.post(f"/api/factor/research/{study['id']}/freeze", headers=headers_b, json={"run_ids": [run["id"]]}).status_code == 404
    # A 自己可以访问
    assert client.get(f"/api/factor/research/{study['id']}/runs/{run['id']}", headers=headers_a).status_code == 200


def test_run_requires_definition_and_returns_queued(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    assert client.post(f"/api/factor/research/{study['id']}/runs", headers=headers,
                       json={"name": "无定义"}).status_code == 400
    run = _dev_run(client, headers, study["id"])
    assert run["status"] == "queued" and run["kind"] == "evaluate" and run["phase"] == "development"
    fetched = client.get(f"/api/factor/research/{study['id']}/runs/{run['id']}", headers=headers).json()["data"]
    assert fetched["status"] == "completed"  # finish_run 已落库


def test_cancel_endpoint(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    r = client.post(f"/api/factor/research/{study['id']}/runs", headers=headers,
                    json={"name": "dev", "definition": {"factor": "mom20", "direction": "high"}})
    rid = r.json()["data"]["id"]
    out = client.post(f"/api/factor/research/{study['id']}/runs/{rid}/cancel", headers=headers).json()["data"]
    assert out["status"] == "cancelled"
    assert client.post(f"/api/factor/research/{study['id']}/runs/run_missing/cancel", headers=headers).status_code == 404


def test_validation_and_holdout_flow(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    dev = _dev_run(client, headers, study["id"])

    # 未设基线不能冻结
    assert client.post(f"/api/factor/research/{study['id']}/freeze", headers=headers,
                       json={"run_ids": [dev["id"]]}).status_code == 409
    # 基线必须是本研究已完成的开发期检验
    assert client.put(f"/api/factor/research/{study['id']}", headers=headers,
                      json={"baseline_id": "run_missing"}).status_code == 404
    client.put(f"/api/factor/research/{study['id']}", headers=headers, json={"baseline_id": dev["id"]})
    frozen = client.post(f"/api/factor/research/{study['id']}/freeze", headers=headers,
                         json={"run_ids": [dev["id"]]}).json()["data"]
    assert frozen["freezes"][0]["baseline_id"] == dev["id"]

    # 验证期沿用来源定义，不能另传
    bad = client.post(f"/api/factor/research/{study['id']}/runs", headers=headers,
                      json={"name": "v", "phase": "validation", "source_run_id": dev["id"],
                            "definition": {"factor": "mom60", "direction": "low"}})
    assert bad.status_code == 400
    ok = client.post(f"/api/factor/research/{study['id']}/runs", headers=headers,
                     json={"name": "v", "phase": "validation", "source_run_id": dev["id"]})
    assert ok.json()["data"]["definition"] == dev["definition"]

    # 留出期：使用后不可撤回
    h = client.post(f"/api/factor/research/{study['id']}/runs", headers=headers,
                    json={"name": "h", "phase": "holdout", "source_run_id": dev["id"]})
    assert h.status_code == 200
    assert client.get(f"/api/factor/research/{study['id']}", headers=headers).json()["data"]["study"]["holdout_used_at"]
    marked_at = client.get(f"/api/factor/research/{study['id']}", headers=headers).json()["data"]["study"]["holdout_used_at"]
    assert client.post(f"/api/factor/research/{study['id']}/runs", headers=headers,
                       json={"name": "h2", "phase": "holdout", "source_run_id": dev["id"]}).status_code == 200
    assert client.get(f"/api/factor/research/{study['id']}", headers=headers).json()["data"]["study"]["holdout_used_at"] == marked_at


def test_compare_endpoint_rules(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    a = _dev_run(client, headers, study["id"], name="a")
    b = _dev_run(client, headers, study["id"], name="b", factor="mom60")
    assert client.post(f"/api/factor/research/{study['id']}/compare", headers=headers,
                       json={"run_ids": [a["id"]]}).status_code == 400
    assert client.post(f"/api/factor/research/{study['id']}/compare", headers=headers,
                       json={"run_ids": [a["id"], a["id"]]}).status_code == 400
    queued = client.post(f"/api/factor/research/{study['id']}/compare", headers=headers,
                         json={"run_ids": [a["id"], b["id"]]})
    assert queued.status_code == 200
    assert queued.json()["data"]["kind"] == "compare"


def test_compare_rejects_mismatched_manifest(client, monkeypatch):
    headers = _auth("u1")
    study = _create_study(client, headers)
    a = _dev_run(client, headers, study["id"], name="a")
    monkeypatch.setattr(factor_lab, "_analysis", lambda: FakeAnalysis("dv2"))  # 换了输入数据
    b = _dev_run(client, headers, study["id"], name="b", factor="mom60")
    r = client.post(f"/api/factor/research/{study['id']}/compare", headers=headers,
                    json={"run_ids": [a["id"], b["id"]]})
    assert r.status_code == 400
    assert "输入数据" in r.json()["detail"]


def test_backtest_endpoint(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    dev = _dev_run(client, headers, study["id"])
    r = client.post(f"/api/factor/research/{study['id']}/backtest", headers=headers,
                    json={"run_id": dev["id"], "top_n": 20, "freq": "weekly", "cost": 0.5})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["kind"] == "backtest" and data["params"] == {"top_n": 20, "freq": "weekly", "cost": 0.5}
    assert client.post(f"/api/factor/research/{study['id']}/backtest", headers=headers,
                       json={"run_id": dev["id"], "freq": "daily"}).status_code == 400


def test_decisions_endpoint(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    dev = _dev_run(client, headers, study["id"])
    assert client.post(f"/api/factor/research/{study['id']}/decisions", headers=headers,
                       json={"decision": "继续验证", "note": "  "}).status_code == 400
    assert client.post(f"/api/factor/research/{study['id']}/decisions", headers=headers,
                       json={"decision": "通过", "note": "x"}).status_code == 400
    assert client.post(f"/api/factor/research/{study['id']}/decisions", headers=headers,
                       json={"decision": "暂缓", "note": "x", "run_ids": ["run_missing"]}).status_code == 404
    ok = client.post(f"/api/factor/research/{study['id']}/decisions", headers=headers,
                     json={"decision": "保留候选", "note": "先保留", "run_ids": [dev["id"]]})
    assert ok.json()["data"]["decisions"][0]["decision"] == "保留候选"


def test_export_endpoint(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    _dev_run(client, headers, study["id"])
    data = client.get(f"/api/factor/research/{study['id']}/export", headers=headers).json()["data"]
    assert data["format"] == "market-workbench-factor-study"
    assert data["version"] == 1
    assert data["study"]["id"] == study["id"]
    assert len(data["runs"]) == 1


def test_unknown_study_is_404(client):
    headers = _auth("u1")
    assert client.get("/api/factor/research/st_missing", headers=headers).status_code == 404
    assert client.get("/api/factor/research/st_missing/export", headers=headers).status_code == 404


@pytest.mark.parametrize("method,suffix", [
    ("GET", ""), ("PUT", ""), ("POST", "/runs"),
    ("GET", "/runs/{rid}"), ("POST", "/runs/{rid}/cancel"),
    ("POST", "/compare"), ("POST", "/backtest"), ("POST", "/freeze"),
    ("POST", "/decisions"), ("GET", "/export"),
])
def test_every_study_route_authenticates_and_hides_foreign_studies(client, method, suffix):
    owner = _auth("owner")
    study = _create_study(client, owner)
    run = _dev_run(client, owner, study["id"])
    outsider = _auth("outsider")
    url = f"/api/factor/research/{study['id']}" + suffix.format(rid=run["id"])
    # Scope precedes even malformed bodies, not only happy-path models.
    kwargs = {"content": "not-json", "headers": {"Content-Type": "application/json"}} if method in ("POST", "PUT") else {}
    assert client.request(method, url, **kwargs).status_code == 401
    kwargs["headers"] = {**outsider, "Content-Type": "application/json"}
    assert client.request(method, url, **kwargs).status_code == 404


@pytest.mark.parametrize("method,suffix,body", [
    ("POST", "", []), ("POST", "", {"name": 123, "config": _valid_config()}),
    ("POST", "", {"name": "x"}), ("POST", "", {"name": "x", "config": {**_valid_config(), "horizon": True}}),
    ("POST", "", {"name": "x", "config": {**_valid_config(), "min_days_listed": "20"}}),
    ("POST", "", {"name": "x", "config": {**_valid_config(), "universe": "made-up"}}),
    ("PUT", "/{sid}", {"config": _valid_config()}),
    ("PUT", "/{sid}", {"input_manifest": {}}), ("PUT", "/{sid}", {"holdout_used_at": None}),
    ("PUT", "/{sid}", {"name": None}), ("PUT", "/{sid}", {"archived": "false"}),
    ("POST", "/{sid}/runs", {"name": "x", "definition": {"factor": 123, "direction": "high"}}),
    ("POST", "/{sid}/runs", {"name": "x", "definition": {"factor": "mom20", "direction": "high", "shell": "x"}}),
    ("POST", "/{sid}/runs", {"name": "x", "phase": ""}),
    ("POST", "/{sid}/compare", {"run_ids": "not-an-array"}),
    ("POST", "/{sid}/compare", {"run_ids": [123, "x"]}),
    ("POST", "/{sid}/freeze", {"run_ids": {}}),
    ("POST", "/{sid}/decisions", {"decision": "暂缓", "note": 123}),
    ("POST", "/{sid}/decisions", {"decision": "暂缓", "note": "x", "run_ids": "x"}),
    ("POST", "/{sid}/backtest", {}), ("POST", "/{sid}/backtest", {"run_id": "x", "top_n": True}),
    ("POST", "/{sid}/backtest", {"run_id": "x", "top_n": 2.5}),
    ("POST", "/{sid}/backtest", {"run_id": "x", "cost": "1.0"}),
])
def test_contract_body_errors_are_400_not_422(client, method, suffix, body):
    headers = _auth("u1")
    study = _create_study(client, headers)
    url = "/api/factor/research" + suffix.format(sid=study["id"])
    response = client.request(method, url, headers=headers, json=body)
    assert response.status_code == 400, response.text
    assert isinstance(response.json()["detail"], str) and len(response.json()["detail"]) <= 80


@pytest.mark.parametrize("method,suffix", [("POST", ""), ("PUT", "/{sid}"), ("POST", "/{sid}/runs"),
    ("POST", "/{sid}/compare"), ("POST", "/{sid}/backtest"), ("POST", "/{sid}/freeze"),
    ("POST", "/{sid}/decisions")])
def test_malformed_json_is_400(client, method, suffix):
    headers = _auth("u1")
    study = _create_study(client, headers)
    response = client.request(method, "/api/factor/research" + suffix.format(sid=study["id"]),
                              headers={**headers, "Content-Type": "application/json"}, content="{")
    assert response.status_code == 400


def test_cross_account_and_cross_study_references_are_404(client):
    owner = _auth("owner")
    foreign_study = _create_study(client, owner)
    foreign = _dev_run(client, owner, foreign_study["id"])
    viewer = _auth("viewer")
    own = _create_study(client, viewer)
    local = _dev_run(client, viewer, own["id"])
    sibling = _create_study(client, viewer, "sibling")
    sibling_run = _dev_run(client, viewer, sibling["id"])
    root = f"/api/factor/research/{own['id']}"
    for rid in (foreign["id"], sibling_run["id"], "run_missing", "x' OR 1=1 --"):
        requests = [("PUT", "", {"baseline_id": rid}),
                    ("POST", "/runs", {"name": "v", "phase": "validation", "source_run_id": rid}),
                    ("POST", "/runs", {"name": "h", "phase": "holdout", "source_run_id": rid}),
                    ("POST", "/compare", {"run_ids": [local["id"], rid]}),
                    ("POST", "/backtest", {"run_id": rid}),
                    ("POST", "/freeze", {"run_ids": [rid]}),
                    ("POST", "/decisions", {"decision": "暂缓", "note": "x", "run_ids": [rid]})]
        for method, suffix, body in requests:
            response = client.request(method, root + suffix, headers=viewer, json=body)
            assert response.status_code == 404, (method, suffix, response.text)
        assert client.get(root + f"/runs/{rid}", headers=viewer).status_code == 404
        assert client.post(root + f"/runs/{rid}/cancel", headers=viewer).status_code == 404
    assert client.get(root, headers=viewer).json()["data"]["study"]["holdout_used_at"] is None


def test_order_conflicts_and_cancelled_results_remain_visible(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    root = f"/api/factor/research/{study['id']}"
    queued = client.post(root + "/runs", headers=headers,
                         json={"name": "pending", "definition": {"factor": "mom20", "direction": "high"}}).json()["data"]
    completed = _dev_run(client, headers, study["id"])
    assert client.put(root, headers=headers, json={"baseline_id": queued["id"]}).status_code == 409
    assert client.post(root + "/backtest", headers=headers, json={"run_id": queued["id"]}).status_code == 409
    assert client.post(root + "/compare", headers=headers, json={"run_ids": [queued["id"], completed["id"]]}).status_code == 409
    assert client.post(root + "/runs", headers=headers,
                       json={"name": "v", "phase": "validation", "source_run_id": completed["id"]}).status_code == 409
    assert client.post(root + "/runs", headers=headers,
                       json={"name": "dev", "source_run_id": completed["id"], "definition": completed["definition"]}).status_code == 400
    cancelled = client.post(root + f"/runs/{queued['id']}/cancel", headers=headers).json()["data"]
    assert cancelled["status"] == "cancelled" and cancelled["result"] is None
    assert not factor_lab.finish_run(queued["id"], status="completed", result={"fake": True})
    assert client.get(root + f"/runs/{queued['id']}", headers=headers).json()["data"] == cancelled
    assert client.post(root + f"/runs/{completed['id']}/cancel", headers=headers).json()["data"]["status"] == "completed"
    client.put(root, headers=headers, json={"archived": True})
    assert client.post(root + "/runs", headers=headers,
                       json={"name": "x", "definition": completed["definition"]}).status_code == 409


def test_invalid_holdout_does_not_mark_and_cancel_cannot_unmark(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    dev = _dev_run(client, headers, study["id"])
    root = f"/api/factor/research/{study['id']}"
    client.put(root, headers=headers, json={"baseline_id": dev["id"]})
    client.post(root + "/freeze", headers=headers, json={"run_ids": [dev["id"]]})
    bad = client.post(root + "/runs", headers=headers,
                      json={"name": " ", "phase": "holdout", "source_run_id": dev["id"]})
    assert bad.status_code == 400
    assert client.get(root, headers=headers).json()["data"]["study"]["holdout_used_at"] is None
    holdout = client.post(root + "/runs", headers=headers,
                         json={"name": "h", "phase": "holdout", "source_run_id": dev["id"]}).json()["data"]
    before = client.get(root, headers=headers).json()["data"]["study"]
    client.post(root + f"/runs/{holdout['id']}/cancel", headers=headers)
    client.put(root, headers=headers, json={"archived": True, "baseline_id": None})
    assert client.get(root, headers=headers).json()["data"]["study"]["holdout_used_at"] == before["holdout_used_at"]


def test_expression_and_sql_injection_are_rejected_or_stored_as_data(client, tmp_path, monkeypatch):
    import factor_analysis

    headers = _auth("u1")
    literal = "study'); DROP TABLE factor_studies; --"
    study = _create_study(client, headers, literal)
    assert study["name"] == literal
    root = f"/api/factor/research/{study['id']}"
    monkeypatch.setattr(factor_lab, "_analysis", lambda: factor_analysis)
    sentinel = tmp_path / "injected"
    for definition in (
        {"expr": f"__import__('os').system('touch {sentinel}')", "direction": "high"},
        {"expr": "close.__class__", "direction": "high"},
        {"expr": "ts_shift(close,-1)", "direction": "high"},
        {"factor": "mom20; import os", "direction": "high"},
        {"factor": "custom:mutable", "direction": "high"},
        {"factor": "mom20", "expr": "close", "direction": "high"},
        {"factor": "mom20", "expr": None, "direction": "high"},
    ):
        response = client.post(root + "/runs", headers=headers, json={"name": "attack", "definition": definition})
        assert response.status_code == 400, response.text
    assert not sentinel.exists()
    assert client.get(root, headers=headers).json()["data"]["runs"] == []
    assert client.get("/api/factor/research/x' OR 1=1 --", headers=headers).status_code == 404


def test_export_contains_only_owned_history_and_no_tokens(client):
    headers = _auth("u1")
    study = _create_study(client, headers)
    dev = _dev_run(client, headers, study["id"])
    other_headers = _auth("u2")
    other_study = _create_study(client, other_headers)
    other_run = _dev_run(client, other_headers, other_study["id"])
    root = f"/api/factor/research/{study['id']}"
    client.put(root, headers=headers, json={"baseline_id": dev["id"]})
    first = client.post(root + "/freeze", headers=headers, json={"run_ids": [dev["id"]]}).json()["data"]
    second = client.post(root + "/freeze", headers=headers, json={"run_ids": [dev["id"]]}).json()["data"]
    assert second["freezes"][0] == first["freezes"][0] and len(second["freezes"]) == 2
    for note in ("first", "second"):
        assert client.post(root + "/decisions", headers=headers,
                           json={"decision": "暂缓", "note": note, "run_ids": [dev["id"]]}).status_code == 200
    response = client.get(root + "/export", headers=headers)
    export = response.json()["data"]
    assert [d["note"] for d in export["study"]["decisions"]] == ["first", "second"]
    assert [r["id"] for r in export["runs"]] == [dev["id"]]
    assert other_study["id"] not in response.text and other_run["id"] not in response.text
    assert headers["Authorization"].split()[1] not in response.text
    assert other_headers["Authorization"].split()[1] not in response.text
