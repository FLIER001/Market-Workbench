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


def test_group_limit_evicts_oldest_by_cached_at():
    """键随用户输入无限增长的缓存族（stock:<endpoint>:<code>）必须淘汰，
    否则浏览过的股票越多、常驻条目越多——这是后端内存只涨不跌的主因。

    keep 只约束「已落定」的条目：正在刷新的（含本次刚写入的那条）不计数，
    否则刚算好的结果会被自己淘汰掉。
    """
    cache_runtime.register_group("stock:", 3)
    for i in range(5):
        cache_runtime.seed(f"stock:info:{i}", {"i": i}, cached_at=1000.0 + i)
    cache_runtime.seed("unrelated", {"keep": True}, cached_at=1.0)

    # 任意一次写入路径（这里走冷建）都会触发该族的淘汰
    cache_runtime.get("stock:info:new", lambda: {"i": "new"}, ttl=600)

    # 已落定的 5 条超上限 3 → 最旧的 2 条出局；本次写入的那条必须留下
    assert cache_runtime.peek("stock:info:0") is None
    assert cache_runtime.peek("stock:info:1") is None
    assert cache_runtime.peek("stock:info:2") == {"i": 2}
    assert cache_runtime.peek("stock:info:4") == {"i": 4}
    assert cache_runtime.peek("stock:info:new") == {"i": "new"}
    # 其他前缀不受牵连：淘汰它们只会带来无谓重算
    assert cache_runtime.peek("unrelated") == {"keep": True}


def test_group_under_limit_keeps_everything():
    cache_runtime.register_group("stock:", 10)
    cache_runtime.seed("stock:info:0", {"i": 0}, cached_at=1.0)
    cache_runtime.get("stock:info:1", lambda: {"i": 1}, ttl=600)
    assert cache_runtime.peek("stock:info:0") == {"i": 0}
    assert cache_runtime.peek("stock:info:1") == {"i": 1}


def test_unregistered_keys_are_never_evicted():
    """没注册组的缓存（整页评分等 key 数量固定的）不该被碰。"""
    for i in range(50):
        cache_runtime.get(f"score:{i}", lambda i=i: {"i": i}, ttl=600)
    assert cache_runtime.peek("score:0") == {"i": 0}
