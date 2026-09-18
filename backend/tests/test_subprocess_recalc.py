"""重算下沉子进程的三个环节：swap_in 原子换入、adopt_disk_snapshot 拾取、调度器包装。

这套机制的目标是把全市场评分重算的内存代价从主进程挪到子进程
（macOS 上主进程释放的堆不还给系统，实测每轮重算留几十 MB 永久残渣），
所以测试重点在**拾取通道的竞态安全**与**失败时保留旧值**。
"""

import types

import cache_runtime
import sector_scores


def setup_function():
    cache_runtime.reset_for_tests()


def _snapshot(gen: str) -> dict:
    return {"schema_version": sector_scores._SCHEMA_VERSION,
            "industries": [], "generated_at": gen}


def test_swap_in_replaces_unconditionally_and_clears_error():
    """swap_in 与 seed 的区别：无条件覆盖、清错误态——这是调度器拾取通道。"""
    cache_runtime.seed("k", {"old": True}, cached_at=1.0)
    entry = cache_runtime._entries["k"]
    entry.error = "boom"
    entry.failures = 3

    cache_runtime.swap_in("k", {"new": True})

    assert cache_runtime.peek("k") == {"new": True}
    assert entry.error is None and entry.failures == 0
    assert entry.cached_at > 1.0


def test_swap_in_ignores_none():
    """子进程失败常表现为读不到快照（None）——绝不能把缓存清成空。"""
    cache_runtime.seed("k", {"old": True})
    cache_runtime.swap_in("k", None)
    assert cache_runtime.peek("k") == {"old": True}


def test_adopt_swaps_in_fresher_snapshot(monkeypatch):
    cache_runtime.seed("sector_scores:v6", {"industries": [], "generated_at": "old"}, cached_at=1.0)
    monkeypatch.setattr(sector_scores, "_load_cache", lambda: _snapshot("new"))

    assert sector_scores.adopt_disk_snapshot() is True
    adopted = cache_runtime.peek("sector_scores:v6")
    assert adopted["generated_at"] == "new"


def test_adopt_keeps_old_value_when_snapshot_not_newer(monkeypatch):
    """子进程失败/无新数据：快照 generated_at 没变就绝不能把旧值标成新的。"""
    cache_runtime.seed("sector_scores:v6", {"industries": [], "generated_at": "same"}, cached_at=1.0)
    monkeypatch.setattr(sector_scores, "_load_cache", lambda: _snapshot("same"))

    assert sector_scores.adopt_disk_snapshot() is False
    assert cache_runtime.peek("sector_scores:v6")["generated_at"] == "same"
    assert cache_runtime._entries["sector_scores:v6"].cached_at == 1.0  # 时戳未被动过


def test_adopt_no_snapshot_at_all(monkeypatch):
    monkeypatch.setattr(sector_scores, "_load_cache", lambda: None)
    assert sector_scores.adopt_disk_snapshot() is False


def test_recalc_wrapper_success_invokes_adopt(monkeypatch):
    """子进程成功（退出码 0）→ adopt 被调用。

    同时校验命令串必须是 `import m; m.fn(...)` 形态——写成
    `import m; fn(...)` 会 NameError（首次上线实测踩过：import 不把
    模块成员带进作用域），替身测试不校验命令串就拦不住这类 bug。
    """
    import app

    captured = {}
    adopted = []

    def fake_run(args, **kwargs):
        captured["args"] = args
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    app._recalc_in_subprocess("sector_scores", "get_sector_scores(force=True)", lambda: adopted.append(1))
    assert adopted == [1]
    assert captured["args"][2] == "import sector_scores; sector_scores.get_sector_scores(force=True)"


def test_recalc_wrapper_failure_keeps_old(monkeypatch):
    """子进程失败（非零退出/超时/OSError）→ adopt 不被调用，内存旧值不动。"""
    import app

    for fake in (
        types.SimpleNamespace(returncode=1),
        None,  # TimeoutExpired 场景由 side_effect 抛出
    ):
        adopted = []
        if fake is None:
            def boom(*a, **k):
                raise app.subprocess.TimeoutExpired(cmd="x", timeout=1)
            monkeypatch.setattr(app.subprocess, "run", boom)
        else:
            monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: fake)
        app._recalc_in_subprocess("sector_scores", "x", lambda: adopted.append(1))
        assert adopted == []
