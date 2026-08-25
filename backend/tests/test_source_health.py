"""source_health 纯逻辑测试：不联网（探活函数全部打桩），验证汇总与节流语义。"""
import time

import cache_runtime
import source_health


def setup_function():
    cache_runtime.reset_for_tests()
    source_health._last_probe.clear()
    source_health._last_probe_at = 0.0


def _stub_probes(monkeypatch, fail_keys=()):
    """把探活清单整体换成桩：默认 ok，fail_keys 里的返回错误串。"""
    stubs = []
    for key, name, pages, _fn in source_health._PROBES:
        error = "模拟故障" if key in fail_keys else None
        stubs.append((key, name, pages, lambda error=error: error))
    monkeypatch.setattr(source_health, "_PROBES", stubs)


def test_report_summarizes_failures(monkeypatch):
    _stub_probes(monkeypatch, fail_keys=("eia", "polymarket"))
    report = source_health.build_report(force=True)
    assert report["summary"]["upstream_total"] == len(source_health._PROBES)
    assert set(report["summary"]["upstream_failed"]) == {"EIA 石油数据", "Polymarket"}
    assert report["summary"]["all_ok"] is False
    assert report["summary"]["dataset_error"] == []


def test_report_all_ok(monkeypatch):
    _stub_probes(monkeypatch)
    report = source_health.build_report(force=True)
    assert report["summary"]["all_ok"] is True
    assert all(u["status"] == "ok" for u in report["upstreams"])
    assert all(u["latency_ms"] >= 0 for u in report["upstreams"])


def test_probe_throttled_within_window(monkeypatch):
    calls = []

    def counting_probe():
        calls.append(1)
        return None

    stubs = [(key, name, pages, counting_probe) for key, name, pages, _fn in source_health._PROBES]
    monkeypatch.setattr(source_health, "_PROBES", stubs)
    source_health.build_report(force=True)
    first_count = len(calls)
    assert first_count == len(stubs)
    # 不带 force 的第二次调用应直接复用节流结果，不再探活
    source_health.build_report(force=False)
    assert len(calls) == first_count
    # force=True 绕过节流，每个源再探一次
    source_health.build_report(force=True)
    assert len(calls) == first_count * 2


def test_datasets_read_runtime_cache_state():
    cache_runtime.seed("gold_score_v3", {"indicators": [1]}, cached_at=time.time() - 10)
    cache_runtime.seed("oil_score_v1", {"indicators": [1]}, cached_at=time.time() - 10)
    rows = {d["key"]: d for d in source_health.collect_datasets()}
    assert rows["gold_score_v3"]["cache_state"] == "fresh"
    assert rows["gold_score_v3"]["cached_at"] is not None
    assert rows["gold_score_v3"]["page"] == "黄金"
    # 未访问过的页面不出现
    assert "bonds:curve" not in rows


def test_probe_failure_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("网络栈炸了")

    stubs = [(key, name, pages, boom) for key, name, pages, _fn in source_health._PROBES]
    monkeypatch.setattr(source_health, "_PROBES", stubs)
    report = source_health.build_report(force=True)  # 不应抛
    assert report["summary"]["upstream_total"] == len(stubs)
    assert len(report["summary"]["upstream_failed"]) == len(stubs)


def test_single_source_probe_only_reprobes_target(monkeypatch):
    calls = []

    def make(error=None):
        def fn():
            calls.append(1)
            return error
        return fn

    # eia 桩失败、其余 ok
    stubs = [(key, name, pages, make("模拟故障" if key == "eia" else None))
             for key, name, pages, _fn in source_health._PROBES]
    monkeypatch.setattr(source_health, "_PROBES", stubs)
    source_health.build_report(force=True)
    total_calls = len(calls)

    # 只重探 eia：其余源不再被调用
    calls.clear()
    report = source_health.build_report(only=["eia"])
    assert len(calls) == 1
    assert report["summary"]["upstream_total"] == len(stubs)
    assert report["summary"]["upstream_failed"] == ["EIA 石油数据"]
    # 单源结果合并进上次结果：其余源仍在报告里
    assert len(report["upstreams"]) == len(stubs)

    # 单源成功后重探：失败名单应清空
    calls.clear()
    good = [(key, name, pages, make(None)) for key, name, pages, _fn in stubs]
    monkeypatch.setattr(source_health, "_PROBES", good)
    report = source_health.build_report(only=["eia"])
    assert report["summary"]["upstream_failed"] == []
    assert report["summary"]["all_ok"] is True


def test_single_source_probe_unknown_key_is_noop(monkeypatch):
    calls = []

    def fn():
        calls.append(1)
        return None

    stubs = [(key, name, pages, fn) for key, name, pages, _fn in source_health._PROBES]
    monkeypatch.setattr(source_health, "_PROBES", stubs)
    source_health.build_report(force=True)
    calls.clear()
    report = source_health.build_report(only=["no_such_source"])
    assert len(calls) == 0  # 未知 key 不触发任何探活
    assert report["summary"]["upstream_total"] == len(stubs)
