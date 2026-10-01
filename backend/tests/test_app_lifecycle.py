"""导入无预热副作用，ASGI 启动仍注册所有后台任务。"""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

import app


def test_import_does_not_start_background_jobs(monkeypatch):
    for layer, method in ((app.pf, "start_scheduler"), (app.fpf, "start_scheduler"),
                          (app.fund_pfs, "start_scheduler"), (app.newsradar, "start_scheduler"),
                          (app.fedwatch_layer, "warmup"), (app.fedwatch_layer, "start_scheduler"),
                          (app.score_scheduler, "start"), (app.mem_watchdog, "start")):
        def fail(*a, **kw):
            raise AssertionError("background job started on import")
        monkeypatch.setattr(layer, method, fail)
    spec = importlib.util.spec_from_file_location("app_import_check", Path(app.__file__))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_asgi_lifespan_starts_jobs_once(monkeypatch):
    starts = []
    monkeypatch.setattr(app, "_start_background_jobs", lambda: starts.append(1))
    with TestClient(app.app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").status_code == 200
    assert starts == [1]


def test_scheduler_selects_synchronous_worker_entry(monkeypatch):
    tasks = []
    for layer, method in ((app.pf, "start_scheduler"), (app.fpf, "start_scheduler"),
                          (app.fund_pfs, "start_scheduler"), (app.newsradar, "start_scheduler"),
                          (app.fedwatch_layer, "warmup"), (app.fedwatch_layer, "start_scheduler"),
                          (app.mem_watchdog, "start")):
        monkeypatch.setattr(layer, method, lambda *a, **kw: None)
    monkeypatch.setattr(app, "_warm_holder_increase", lambda: None)
    monkeypatch.setattr(app, "_warm_expensive_datasets", lambda: None)
    monkeypatch.setattr(app.score_scheduler, "start", lambda *a: tasks.extend(a))
    calls = []
    monkeypatch.setattr(app, "_recalc_in_subprocess", lambda module, entry, adopt: calls.append((module, entry)))
    app._start_background_jobs()
    for task in tasks:
        task()
    assert calls == [("sector_scores", "rebuild_snapshot()"), ("sw_level2_scores", "rebuild_snapshot()"), ("plate_scores", "rebuild_snapshot()")]
