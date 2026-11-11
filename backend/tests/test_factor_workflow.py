"""Real app/auth/lifespan integration with isolated SQLite and synthetic inputs."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_factor_lab import _write_dataset

import app
import factor_lab
import users


@pytest.mark.parametrize("startup_fails", [False, True])
def test_app_lifespan_stops_factor_worker(tmp_path, monkeypatch, startup_fails):
    factor_lab.stop_worker()
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()

    def background():
        assert factor_lab._STARTED
        with users._conn() as connection:
            assert connection.execute("SELECT COUNT(*) FROM factor_runs").fetchone()[0] == 0
        if startup_fails:
            raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(app, "_start_background_jobs", background)
    try:
        if startup_fails:
            with pytest.raises(RuntimeError, match="synthetic startup failure"), TestClient(app.app):
                pytest.fail("startup should fail")
        else:
            with TestClient(app.app) as client:
                assert factor_lab._STARTED
                assert client.get("/api/health").status_code == 200
                assert client.get("/api/factor/research").status_code == 401
        assert not factor_lab._STARTED
        assert factor_lab._THREAD is None
    finally:
        factor_lab.stop_worker()


_WORKFLOW = r'''
import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient
import app
import factor_data
import factor_lab
import users

assert users.DB_FILE == os.environ["MW_USERS_DB"]
assert users.user_count() == 0
assert not factor_lab._STARTED and factor_lab._THREAD is None, "import started worker"
paths = [getattr(child, "path", "") for route in app.app.routes
         for child in getattr(getattr(route, "original_router", None), "routes", [route])]
assert paths.count("/api/factor/research") == 2
assert "/api/factor/lab" in paths, "legacy route missing"
if "/{path:path}" in paths:
    assert paths.index("/api/factor/research") < paths.index("/{path:path}")

# Keep the actual factor lifecycle/worker; disable only unrelated schedulers/warmups.
app._start_background_jobs = lambda: None
app._API_KEY = "integration-access-key"
app._REGISTRATION = True
dates = factor_data.load_calendar()
config = {
    "horizon": 5, "min_days_listed": 1,
    "development": {"start": dates[10], "end": dates[120]},
    "validation": {"start": dates[121], "end": dates[160]},
    "holdout": {"start": dates[161], "end": dates[200]},
}
files_before = {p.name: p.read_bytes() for p in Path(factor_data.DATA_DIR).iterdir() if p.is_file()}
with TestClient(app.app) as client:
    assert factor_lab._STARTED
    assert client.get("/api/factor/research").status_code == 401
    access = {"X-VR-Access-Key": app._API_KEY}
    assert client.get("/api/factor/research", headers=access).status_code == 401

    def request(method, path, body=None, headers=None):
        response = client.request(method, path, headers=headers or owner, json=body)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def login(name):
        response = client.post("/api/auth/register", headers=access,
                               json={"username": name, "password": "secret1"})
        assert response.status_code == 200, response.text
        response = client.post("/api/auth/login", headers=access,
                               json={"username": name, "password": "secret1"})
        assert response.status_code == 200, response.text
        token = response.json()["data"]["token"]
        return {**access, "X-VR-User-Token": token}

    owner = login("workflow-owner")
    outsider = login("workflow-outsider")
    me = request("GET", "/api/auth/me")
    assert me["username"] == "workflow-owner"
    assert request("GET", "/api/auth/me", headers={**access,
        "Authorization": "Bearer " + owner["X-VR-User-Token"]}) == me
    study = request("POST", "/api/factor/research", {"name": "Synthetic workflow", "config": config})
    root = "/api/factor/research/" + study["id"]

    def wait(run):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = request("GET", root + "/runs/" + run["id"])
            if current["status"] in factor_lab.TERMINAL:
                assert current["status"] == "completed", current
                return current
            time.sleep(.05)
        raise AssertionError("worker did not finish: " + run["id"])

    a = request("POST", root + "/runs", {"name": "builtin",
        "definition": {"factor": "mom20", "direction": "high"}})
    b = request("POST", root + "/runs", {"name": "expression",
        "definition": {"expr": "cs_rank(close)", "direction": "low"}})
    assert a["status"] == b["status"] == "queued"
    a, b = wait(a), wait(b)
    assert a["finished_at"] <= b["started_at"], "execution lane overlapped"
    assert a["result"]["native"][0]["by_horizon"]["5"]["sample"]["paired_rows"] > 0
    compare = wait(request("POST", root + "/compare", {"run_ids": [a["id"], b["id"]]}))
    assert compare["result"]["common"]["by_horizon"]["5"]["paired_rows"] > 0
    backtest = wait(request("POST", root + "/backtest", {"run_id": b["id"], "top_n": 10}))
    assert backtest["definition"] == b["definition"]
    request("PUT", root, {"baseline_id": a["id"]})
    frozen = request("POST", root + "/freeze", {"run_ids": [b["id"]]})
    assert frozen["freezes"][0]["run_ids"] == [a["id"], b["id"]]
    for phase in ("validation", "holdout"):
        staged = wait(request("POST", root + "/runs", {
            "name": phase, "phase": phase, "source_run_id": b["id"]}))
        assert staged["definition"] == b["definition"]
        assert staged["result"]["phase"] == phase
    request("POST", root + "/decisions", {"decision": "保留候选", "note": "Synthetic integration",
        "run_ids": [a["id"], b["id"]]})
    exported = request("GET", root + "/export")
    assert exported["format"] == "market-workbench-factor-study" and exported["version"] == 1
    assert len(exported["runs"]) == 6 and exported["study"]["holdout_used_at"]
    assert exported["study"]["decisions"][0]["run_ids"] == [a["id"], b["id"]]
    text = json.dumps(exported)
    assert str(Path(os.environ["MW_DATA_DIR"]).resolve()) not in text
    assert owner["X-VR-User-Token"] not in text and outsider["X-VR-User-Token"] not in text
    assert request("GET", "/api/auth/data") == {}, "large results leaked to generic payload"
    assert request("GET", "/api/factor/research", headers=outsider) == []
    for path in (root, root + "/runs/" + a["id"], root + "/export"):
        assert client.get(path, headers=outsider).status_code == 404
    assert request("GET", root)["runs"] == exported["runs"]
    request("POST", "/api/auth/logout")
    assert client.get(root, headers=owner).status_code == 401
assert not factor_lab._STARTED and factor_lab._THREAD is None
assert {p.name: p.read_bytes() for p in Path(factor_data.DATA_DIR).iterdir() if p.is_file()} == files_before
with TestClient(app.app) as client:
    response = client.get(root, headers=outsider)
    assert response.status_code == 404
    session = client.post("/api/auth/login", headers=access,
                         json={"username": "workflow-owner", "password": "secret1"}).json()["data"]
    restored = client.get(root, headers={**access, "X-VR-User-Token": session["token"]}).json()["data"]
    assert len(restored["runs"]) == 6 and all(r["status"] == "completed" for r in restored["runs"])
    assert restored["study"]["holdout_used_at"] == exported["study"]["holdout_used_at"]
assert not factor_lab._STARTED and factor_lab._THREAD is None
'''


def test_real_app_synthetic_file_workflow(tmp_path):
    data_dir = tmp_path / "data"
    _write_dataset(str(data_dir))
    env = {**os.environ, "MW_DATA_DIR": str(data_dir), "MW_USERS_DB": str(tmp_path / "users.db"),
           "MW_REPORTS_DIR": str(tmp_path / "reports")}
    # Fresh imports bind both parent and calculation children to synthetic files.
    result = subprocess.run([sys.executable, "-c", _WORKFLOW], cwd=Path(app.__file__).parent,
                            env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
