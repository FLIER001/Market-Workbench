"""重算下沉子进程的三个环节：swap_in 原子换入、adopt_disk_snapshot 拾取、调度器包装。

这套机制的目标是把全市场评分重算的内存代价从主进程挪到子进程
（macOS 上主进程释放的堆不还给系统，实测每轮重算留几十 MB 永久残渣），
所以测试重点在**拾取通道的竞态安全**与**失败时保留旧值**。
"""

import types
import subprocess
import sys
from pathlib import Path

import pytest

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
    app._recalc_in_subprocess("sector_scores", "rebuild_snapshot()", lambda: adopted.append(1))
    assert adopted == [1]
    assert captured["args"][2] == "import sector_scores; sector_scores.rebuild_snapshot()"


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


@pytest.mark.parametrize("module,field", [("sector_scores", "industries"), ("sw_level2_scores", "industries"), ("plate_scores", "boards")])
def test_real_child_saves_new_snapshot_before_exit(tmp_path, module, field):
    """使用真实短命进程，避免 subprocess mock 漏掉 daemon 提前结束问题。"""
    marker = tmp_path / "saved"
    code = f'''
import time
from pathlib import Path
import {module} as m
m._load_cache = lambda: {{{field!r}: [{{"old": True}}], "generated_at": "old"}}
def build():
    time.sleep(0.05)
    return {{{field!r}: [{{"new": True}}], "generated_at": "new"}}
m._build = build
m._save_cache = lambda v: Path({str(marker)!r}).write_text(v["generated_at"])
m.rebuild_snapshot()
'''
    child = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert marker.read_text() == "new"


@pytest.mark.parametrize("module", ["sector_scores", "sw_level2_scores", "plate_scores"])
def test_empty_rebuild_does_not_overwrite_snapshot(monkeypatch, module):
    import importlib
    layer = importlib.import_module(module)
    monkeypatch.setattr(layer, "_build", lambda: {})
    monkeypatch.setattr(layer, "_save_cache", lambda v: pytest.fail("empty rebuild must not save"))
    with pytest.raises(ValueError):
        layer.rebuild_snapshot()


def test_plate_save_failure_is_not_success(monkeypatch, tmp_path):
    import plate_scores
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x")
    monkeypatch.setattr(plate_scores, "_PRIMARY_CACHE_FILE", str(blocked / "a.json"))
    monkeypatch.setattr(plate_scores, "_FALLBACK_CACHE_FILE", str(blocked / "b.json"))
    with pytest.raises(OSError):
        plate_scores._save_cache({"boards": [1]})
