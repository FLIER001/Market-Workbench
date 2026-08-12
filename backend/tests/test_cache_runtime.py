import time

import cache_runtime


def setup_function():
    cache_runtime.reset_for_tests()


def test_stale_value_returns_immediately_and_refreshes_once():
    calls = []
    cache_runtime.seed("x", {"value": 1}, cached_at=time.time() - 100)

    def build():
        calls.append(1)
        time.sleep(0.05)
        return {"value": 2}

    first = cache_runtime.get("x", build, ttl=1)
    second = cache_runtime.get("x", build, ttl=1)
    assert first["value"] == second["value"] == 1
    assert first["cache_state"] == second["cache_state"] == "refreshing"
    time.sleep(0.08)
    assert cache_runtime.get("x", build, ttl=60)["value"] == 2
    assert len(calls) == 1


def test_failed_refresh_keeps_last_good():
    cache_runtime.seed("x", {"value": 1}, cached_at=time.time() - 100)
    result = cache_runtime.get("x", lambda: (_ for _ in ()).throw(RuntimeError("boom")), ttl=1)
    assert result["value"] == 1
    time.sleep(0.03)
    after = cache_runtime.get("x", lambda: {"value": 2}, ttl=1)
    assert after["value"] == 1
    assert after["cache_state"] == "error"
    assert after["refresh_error"] == "boom"
