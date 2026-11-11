"""Memory-saving paths retain data, refresh semantics and worker isolation."""
import importlib
import json
import os
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

import cache_runtime
import factor_data
import score_worker


@pytest.mark.parametrize("start,end", [
    (None, None), ("2025-01-02", None), (None, "2025-01-03"),
    ("2025-01-02", "2025-01-03"), ("2030-01-01", None),
    ("2025-01-04", "2025-01-01"),
])
def test_chunked_panel_matches_full_read(tmp_path, monkeypatch, start, end):
    path = tmp_path / "bars.csv.gz"
    df = pd.DataFrame({
        "code": ["000001"] * 4 + ["600000"] * 4,
        "date": [f"2025-01-0{i}" for i in range(1, 5)] * 2,
        "open": [10.12, 9.123456789012, 11.5, 12.0] * 2,
        "close": [10.2, 9.2, 11.6, 12.1] * 2,
        "high": [11.0] * 8, "low": [8.0] * 8,
        "volume": [1.0, 2.0, float("nan"), 4.0] * 2,
        "amount": [100.0] * 8,
    })
    df.to_csv(path, index=False, compression="gzip")
    monkeypatch.setattr(factor_data, "_BARS_FILE", str(path))
    monkeypatch.setattr(factor_data, "_READ_CHUNK_ROWS", 3)
    expected = pd.read_csv(path, dtype={"code": str})
    if start:
        expected = expected[expected["date"] >= start]
    if end:
        expected = expected[expected["date"] <= end]
    result = factor_data.load_panel(start, end)
    # Empty selection may use RangeIndex rather than the filtered empty Index.
    pd.testing.assert_frame_equal(result, expected, check_index_type=False)
    assert result["code"].tolist() == expected["code"].tolist()


def test_chunked_empty_file_retains_columns(tmp_path, monkeypatch):
    path = tmp_path / "bars.csv.gz"
    pd.DataFrame(columns=["code", "date", "close"]).to_csv(path, index=False, compression="gzip")
    monkeypatch.setattr(factor_data, "_BARS_FILE", str(path))
    result = factor_data.load_panel("2025-01-01")
    assert result.empty and list(result.columns) == ["code", "date", "close"]


@pytest.mark.parametrize("module,key,getter,field", [
    ("sector_scores", "sector_scores:v6", "get_sector_scores", "industries"),
    ("sw_level2_scores", "sw_level2_scores:v2", "get_level2_scores", "industries"),
    ("plate_scores", "plate_scores:v5", "get_plate_scores", "boards"),
])
def test_request_refresh_uses_worker_and_preserves_last_good(monkeypatch, module, key, getter, field):
    layer = importlib.import_module(module)
    cache_runtime.invalidate(key)
    old = {field: [{"old": True}], "generated_at": "old"}
    new = {field: [{"new": True}], "generated_at": "new"}
    snapshot = [old]
    monkeypatch.setattr(layer, "_load_cache", lambda: snapshot[0])
    monkeypatch.setattr(layer, "_build", lambda: pytest.fail("heavy build must not run in serving process"))
    monkeypatch.setattr(layer, "_cache_ttl", lambda value: 3600)
    cache_runtime.seed(key, old)
    started, release = threading.Event(), threading.Event()

    def worker(name, load):
        assert name == module
        started.set()
        assert release.wait(3)
        snapshot[0] = new
        return load()

    monkeypatch.setattr(score_worker, "rebuild", worker)
    result = getattr(layer, getter)(force=True)
    assert started.wait(3)
    assert result[field] == old[field] and result["cache_state"] == "refreshing"
    release.set()
    deadline = time.monotonic() + 3
    while cache_runtime._entries[key].refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache_runtime.peek(key) == new

    def fail_worker(*a, **k):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(score_worker, "rebuild", fail_worker)
    getattr(layer, getter)(force=True)
    deadline = time.monotonic() + 3
    while cache_runtime._entries[key].refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache_runtime.peek(key) == new
    assert cache_runtime._entries[key].error == "worker failed"
    cache_runtime.invalidate(key)


def test_worker_exits_before_loading_snapshot(tmp_path, monkeypatch):
    """A real child writes a snapshot; no build allocation reaches the parent."""
    path = tmp_path / "snapshot.json"
    (tmp_path / "sector_scores.py").write_text(
        "import os,json\n"
        "def rebuild_snapshot():\n"
        "    buffer = bytearray(8 * 1024 * 1024)\n"
        "    with open('snapshot.json','w') as f:\n"
        "        json.dump({'pid':os.getpid(), 'industries':[{'score':42}]},f)\n"
    )
    monkeypatch.setattr(score_worker, "_BACKEND_DIR", str(tmp_path))
    result = score_worker.rebuild("sector_scores", lambda: json.loads(path.read_text()))
    assert result["pid"] != os.getpid()
    assert result["industries"] == [{"score": 42}]


def test_workers_are_serialized(monkeypatch):
    state_lock = threading.Lock()
    active, maximum = 0, 0

    def run(*a, **kw):
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with state_lock:
            active -= 1
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(score_worker.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda i: score_worker.rebuild("sector_scores", lambda: i), range(3))) == [0, 1, 2]
    assert maximum == 1
