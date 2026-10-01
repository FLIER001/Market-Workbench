"""用临时项目和命令替身执行启动脚本，不启动服务或 npm 联网。"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def project(tmp_path):
    source = Path(__file__).resolve().parents[2] / "run.sh"
    shutil.copyfile(source, tmp_path / "run.sh")
    for folder in ("frontend/src", "frontend/dist", "backend/.venv/bin", "bin"):
        (tmp_path / folder).mkdir(parents=True)
    for file in ("frontend/src/app.ts", "frontend/package.json", "frontend/package-lock.json", "frontend/vite.config.ts", "frontend/index.html", "frontend/tailwind.config.ts", "frontend/postcss.config.js", "frontend/tsconfig.json"):
        (tmp_path / file).write_text("old")
    (tmp_path / "bin/npm").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_COMMAND_LOG"\n')
    (tmp_path / "backend/.venv/bin/python").write_text('#!/bin/sh\nprintf "python:%s:%s\\n" "${MW_HOST:-}" "${MW_API_KEY:-}" >> "$TEST_COMMAND_LOG"\n')
    for file in ("bin/npm", "backend/.venv/bin/python"):
        (tmp_path / file).chmod(0o755)
    for path in tmp_path.rglob("*"):
        os.utime(path, (1000, 1000))
    (tmp_path / "frontend/dist/index.html").write_text("built")
    os.utime(tmp_path / "frontend/dist/index.html", (2000, 2000))
    return tmp_path


def run(project):
    log = project / "calls"
    env = {**os.environ, "PATH": f"{project / 'bin'}:{os.environ['PATH']}", "TEST_COMMAND_LOG": str(log), "MW_ENV_FILE": str(project / "backend/.env.local")}
    p = subprocess.run(["bash", str(project / "run.sh")], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return log.read_text()


@pytest.mark.parametrize("file", ["vite.config.ts", "index.html", "tailwind.config.ts", "postcss.config.js", "tsconfig.json", "package-lock.json"])
def test_changed_build_configuration_rebuilds(project, file):
    os.utime(project / "frontend" / file, (3000, 3000))
    assert "run build" in run(project)


def test_current_build_skips_npm_and_loads_private_environment(project):
    (project / "backend/.env.local").write_text('MW_HOST=127.0.0.2\nMW_API_KEY=test-only\n')
    calls = run(project)
    assert "run build" not in calls
    assert "python:127.0.0.2:test-only" in calls


def test_deleted_source_rebuilds(project):
    (project / "frontend/src/app.ts").unlink()
    assert "run build" in run(project)
