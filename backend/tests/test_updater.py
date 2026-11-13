"""内置更新（updater.py）的离线测试：git 调用全部打桩，不联网、不动真实仓库。"""
import subprocess

import pytest

import updater

PACKAGE_JSON = '{"name": "market-workbench-frontend", "version": "9.9.9"}'


def _proc(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], code, stdout=stdout, stderr=stderr)


def _fake_git(responses: dict):
    """按参数匹配的假 git；未命中返回空输出的成功，便于只写关心的那几条。"""

    def fake(*args, timeout=None):
        joined = " ".join(args)
        if joined in responses:
            return responses[joined]
        if args[:2] == ("rev-parse", "--verify"):
            return _proc(args[-1] + "\n")
        return _proc()

    return fake


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updater, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(updater, "REMOTE", "origin")
    monkeypatch.setattr(updater, "BRANCH", "main")
    return tmp_path


def test_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "REPO_ROOT", tmp_path)
    s = updater.check()
    assert s["available"] is False
    assert s["can_apply"] is False
    assert "git 仓库" in s["message"]


def test_fetch_failure_is_reported_not_raised(repo, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git({
        "fetch --quiet --prune origin +refs/heads/main:refs/remotes/origin/main":
            _proc(stderr="fatal: unable to access 'https://github.com/…'", code=128),
    }))
    s = updater.check()
    assert s["fetched"] is False
    assert s["can_apply"] is False
    assert "拉取远程失败" in s["message"]


def test_up_to_date(repo, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git({
        "rev-parse HEAD": _proc("aaaaaaa1111\n"),
        "rev-parse origin/main": _proc("aaaaaaa1111\n"),
        "rev-list --count HEAD..origin/main": _proc("0\n"),
        "rev-list --count origin/main..HEAD": _proc("0\n"),
    }))
    s = updater.check()
    assert s["fetched"] is True
    assert s["behind"] == 0 and s["ahead"] == 0
    assert s["can_apply"] is False
    assert s["message"] == "已是最新版本"


def test_behind_is_applicable(repo, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git({
        "rev-parse HEAD": _proc("aaaaaaa1111\n"),
        "rev-parse origin/main": _proc("bbbbbbb2222\n"),
        "rev-list --count HEAD..origin/main": _proc("2\n"),
        "rev-list --count origin/main..HEAD": _proc("0\n"),
        "status --porcelain --untracked-files=no": _proc(""),
        "log --format=%h%x09%s -n 50 HEAD..origin/main":
            _proc("bbbbbbb\tfeat: 新面板\naaaaaaa\tfix: 修数\n"),
        "show origin/main:frontend/package.json": _proc(PACKAGE_JSON),
    }))
    s = updater.check()
    assert s["can_apply"] is True
    assert s["latest_version"] == "9.9.9"
    assert [c["subject"] for c in s["commits"]] == ["feat: 新面板", "fix: 修数"]
    assert "2 个新提交" in s["message"]


def test_dirty_worktree_blocks_update(repo, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git({
        "rev-parse HEAD": _proc("aaaaaaa1111\n"),
        "rev-parse origin/main": _proc("bbbbbbb2222\n"),
        "rev-list --count HEAD..origin/main": _proc("1\n"),
        "status --porcelain --untracked-files=no": _proc(" M backend/app.py\n"),
    }))
    s = updater.check()
    assert s["dirty"] == ["M backend/app.py"]
    assert s["can_apply"] is False
    assert "未提交改动" in s["message"]


def test_diverged_blocks_update(repo, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git({
        "rev-parse HEAD": _proc("aaaaaaa1111\n"),
        "rev-parse origin/main": _proc("bbbbbbb2222\n"),
        "rev-list --count HEAD..origin/main": _proc("3\n"),
        "rev-list --count origin/main..HEAD": _proc("1\n"),
    }))
    s = updater.check()
    assert s["can_apply"] is False
    assert "分叉" in s["message"]


def test_bad_rev_list_output_does_not_raise(repo, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git({
        "rev-list --count HEAD..origin/main": _proc("not-a-number\n"),
        "rev-list --count origin/main..HEAD": _proc("", code=1),
    }))
    s = updater.check()
    assert s["behind"] == 0 and s["ahead"] == 0


def test_apply_refuses_when_not_applicable(repo, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git({
        "rev-parse HEAD": _proc("aaaaaaa1111\n"),
        "rev-parse origin/main": _proc("aaaaaaa1111\n"),
        "rev-list --count HEAD..origin/main": _proc("0\n"),
        "rev-list --count origin/main..HEAD": _proc("0\n"),
    }))
    with pytest.raises(ValueError) as e:
        updater.apply()
    assert "已是最新版本" in str(e.value)


def test_apply_pulls_and_schedules_restart(repo, monkeypatch):
    restarts = []
    state = {"pulled": False}

    def fake(*args, timeout=None):
        # pull 之后 HEAD 才变成远程那个提交，所以要按调用顺序回答
        if args[:2] == ("rev-parse", "--verify"):
            return _proc(args[-1] + "\n")
        if args == ("rev-parse", "HEAD"):
            return _proc("bbbbbbb2222\n" if state["pulled"] else "aaaaaaa1111\n")
        if args == ("rev-list", "--count", "HEAD..origin/main"):
            return _proc("0\n" if state["pulled"] else "1\n")
        if args[0] == "pull":
            state["pulled"] = True
            return _proc("Fast-forward\n")
        if args[0] == "diff":
            return _proc("backend/app.py\nfrontend/src/pages/Settings.tsx\n")
        if args[0] == "show":
            return _proc(PACKAGE_JSON)
        if args[0] == "status":
            return _proc()
        return _proc("bbbbbbb2222\n")   # rev-parse origin/main 等

    monkeypatch.setattr(updater, "_git", fake)
    monkeypatch.setattr(updater.threading, "Timer", lambda delay, fn: restarts.append(fn) or type(
        "T", (), {"start": lambda self: None})())
    result = updater.apply()
    assert result["ok"] is True
    assert result["commit"] == "bbbbbbb"
    assert result["changed_files"] == 2
    assert len(restarts) == 1


# —— 路由接线 ——（只验证接线与鉴权，不跑真 git）
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

client = TestClient(app_module.app)


def test_check_route(monkeypatch):
    monkeypatch.setattr(app_module.updater, "check",
                        lambda: {"available": True, "can_apply": False, "message": "已是最新版本"})
    r = client.get("/api/update/check")
    assert r.status_code == 200
    assert r.json()["data"]["message"] == "已是最新版本"


def test_apply_route_needs_login():
    assert client.post("/api/update/apply").status_code == 401


def test_health_exposes_commit():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert "commit" in r.json()
