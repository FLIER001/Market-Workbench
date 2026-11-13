"""内置更新：检查 GitHub 上游的新提交并就地应用。

部署形态是 git 仓库 + `run.sh`（launchd 或手动启动），所以「更新」= `git pull
--ff-only` ＋ 依赖有变动时重装 requirements ＋ 原地 exec `run.sh` 重启（`run.sh`
本身就有「前端源码比 dist 新就重新构建」的判断，重启后即为新版本）。

三条红线，都是为了不把一台正在跑的机器按坏：

1. 只做快进（`--ff-only`）：本地有未推送提交或与远程分叉时直接报错，不合并、不丢改动。
2. 工作区脏（跟踪文件被改）时不更新：避开 pull 冲突与构建覆盖。
3. 依赖安装失败不重启：新代码可能 import 不到新包，重启只会进入崩溃-重启循环。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from version import read_version

# 上游默认取 origin/main：README 里给出 clone 的仓库，部署用户从它取更新。
# 需要跟踪别的远程/分支时用环境变量覆盖（例如自建镜像）。
REMOTE = os.environ.get("MW_UPDATE_REMOTE", "").strip() or "origin"
BRANCH = os.environ.get("MW_UPDATE_BRANCH", "").strip() or "main"

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SH = REPO_ROOT / "run.sh"

_GIT_TIMEOUT = 30
_APPLY_LOCK = threading.Lock()


def _git(*args: str, timeout: float = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """在仓库根执行 git。GIT_TERMINAL_PROMPT=0 让缺凭证时立刻失败而不是挂住 HTTP 请求。"""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _err(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"git 退出码 {proc.returncode}"


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _count(proc: subprocess.CompletedProcess) -> int:
    """`rev-list --count` 的输出；拿不到就按 0 计（宁可显示「无更新」也不让页面报错）。"""
    try:
        return int(_out(proc))
    except ValueError:
        return 0


def head_commit() -> str | None:
    """当前 HEAD 的短哈希。非 git 部署返回 None（前端据此退回按版本号判断重启）。"""
    try:
        return _out(_git("rev-parse", "--short", "HEAD")) or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _remote_version(remote_ref: str) -> str | None:
    """远程的版本号以 `frontend/package.json` 为准（与 version.py 同一真相）。"""
    try:
        raw = _out(_git("show", f"{remote_ref}:frontend/package.json"))
        return json.loads(raw)["version"] or None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None


def check() -> dict:
    """拉远程并比较 HEAD。永远返回结构化结果，不抛异常——页面要能显示原因。"""
    base = {
        "available": True,
        "fetched": False,
        "remote": REMOTE,
        "branch": BRANCH,
        "current_version": read_version(),
        "latest_version": None,
        "current_commit": None,
        "latest_commit": None,
        "behind": 0,
        "ahead": 0,
        "dirty": [],
        "commits": [],
        "can_apply": False,
        "message": "",
    }
    if not (REPO_ROOT / ".git").exists():
        return {**base, "available": False,
                "message": "当前目录不是 git 仓库（可能是解压安装的），内置更新不可用"}

    remote_ref = f"{REMOTE}/{BRANCH}"
    try:
        fetch = _git("fetch", "--quiet", "--prune", REMOTE,
                     f"+refs/heads/{BRANCH}:refs/remotes/{remote_ref}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**base, "message": f"拉取远程失败：{exc}"}
    if fetch.returncode != 0:
        return {**base, "message": f"拉取远程失败：{_err(fetch)}"}
    if not _out(_git("rev-parse", "--verify", remote_ref)):
        return {**base, "fetched": True, "message": f"远程没有 {remote_ref} 分支"}

    head = _out(_git("rev-parse", "HEAD"))
    remote_head = _out(_git("rev-parse", remote_ref))
    behind = _count(_git("rev-list", "--count", f"HEAD..{remote_ref}"))
    ahead = _count(_git("rev-list", "--count", f"{remote_ref}..HEAD"))
    dirty = [line.strip() for line in
             _out(_git("status", "--porcelain", "--untracked-files=no")).splitlines() if line.strip()]
    commits = []
    if behind:
        for line in _out(_git("log", "--format=%h%x09%s", "-n", "50",
                              f"HEAD..{remote_ref}")).splitlines():
            sha, _, subject = line.partition("\t")
            commits.append({"hash": sha, "subject": subject})

    result = {**base,
              "fetched": True,
              "current_version": base["current_version"],
              "latest_version": _remote_version(remote_ref) or base["current_version"],
              "current_commit": head[:7] or None,
              "latest_commit": remote_head[:7] or None,
              "behind": behind, "ahead": ahead, "dirty": dirty, "commits": commits}

    can_apply = behind > 0 and ahead == 0 and not dirty
    if behind == 0 and ahead == 0:
        message = "已是最新版本"
    elif behind == 0:
        message = f"远程没有新内容；本地有 {ahead} 个未推送提交"
    elif ahead:
        message = (f"本地与 {remote_ref} 已分叉（本地领先 {ahead} 个提交），"
                   f"内置更新只做快进，请手动合并")
    elif dirty:
        message = f"工作区有 {len(dirty)} 个未提交改动，先处理再更新（改动会被 pull 挡住）"
    else:
        message = (f"有 {behind} 个新提交，v{base['current_version']} → "
                   f"v{result['latest_version']}")
    return {**result, "can_apply": can_apply, "message": message}


def apply() -> dict:
    """快进到远程并重启。失败抛 ValueError，消息直接给页面显示。

    成功时重启是**异步**的（延迟 1s 执行 exec），HTTP 响应先回给浏览器；
    调用方应轮询 `/api/health` 的 `commit` 判断新进程是否已经起来。
    """
    if not _APPLY_LOCK.acquire(blocking=False):
        raise ValueError("已有更新正在执行，请稍候")
    try:
        status = check()
        if not status["available"] or not status["can_apply"]:
            raise ValueError(status["message"])

        before = status["current_commit"] or ""
        pull = _git("pull", "--ff-only", "--no-rebase", REMOTE, BRANCH, timeout=120)
        if pull.returncode != 0:
            raise ValueError(f"git pull 失败：{_err(pull)}")
        after = _out(_git("rev-parse", "HEAD"))
        changed = [p for p in _out(_git("diff", "--name-only", before, after)).splitlines() if p]

        if any(p.startswith("backend/requirements") for p in changed):
            install = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                cwd=REPO_ROOT / "backend", capture_output=True, text=True, timeout=600,
            )
            if install.returncode != 0:
                raise ValueError(
                    "代码已拉取，但依赖安装失败，未重启服务：" +
                    (install.stderr or install.stdout).strip().splitlines()[-1]
                )

        threading.Timer(1.0, _restart).start()
        return {
            "ok": True,
            "previous_version": status["current_version"],
            "version": read_version(),
            "commit": after[:7],
            "changed_files": len(changed),
            "restart_scheduled": True,
            "message": f"已更新到 v{read_version()}（{len(changed)} 个文件变动），正在重启服务",
        }
    finally:
        _APPLY_LOCK.release()


def _restart_argv() -> list[str]:
    """重启命令：优先复用 run.sh，它负责「源码比 dist 新就重建前端」。

    没有 run.sh 的部署退回按当前命令行重启（`python -m uvicorn` 与 uvicorn
    控制台脚本两种形态都覆盖）。
    """
    if RUN_SH.exists():
        return ["/bin/bash", str(RUN_SH)]
    if sys.argv[0].endswith("__main__.py"):
        return [sys.executable, "-m", "uvicorn", *sys.argv[1:]]
    return [sys.executable, *sys.argv]


def _restart() -> None:
    """原地 exec：进程号不变，launchd 的 KeepAlive 与手动启动都能接着跑。"""
    argv = _restart_argv()
    print(f"[update] 重启：{' '.join(argv)}", flush=True)
    try:
        # 必须用 argv 直传、不经 shell：重启命令里不能再多一层引号/解析风险。
        os.execv(argv[0], argv)  # noqa: S606 — 参数表直传，无 shell
    except OSError as exc:  # run.sh 被删/无 bash 这类极端情况，launchd 会重拉本进程
        print(f"[update] 重启失败，请手动重启：{exc}", file=sys.stderr, flush=True)
        os._exit(1)
