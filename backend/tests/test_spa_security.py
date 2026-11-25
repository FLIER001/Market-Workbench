"""生产 SPA 路由与鉴权中间件的 HTTP 验证（路径穿越 / 绝对路径 / 符号链接逃逸）。

做法：从真实 app.py 取出静态托管与中间件这几个函数，挂到一个独立的 FastAPI 上，
指向临时 dist 目录。命名空间直接取 app 模块的全局名——此前这里是手写的白名单，
app.py 新引入一个模块级名字（如 `_KEY_FREE_PATHS`）测试就 NameError，
本文件的路径穿越用例因此红了一段时间没人发现。
"""
import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "icon.svg").write_text("safe asset")
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (dist / "escape.txt").symlink_to(outside)
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"spa_index", "spa_fallback", "_require_api_key", "_bearer_token", "_authorization_bearer"}
    # spa_* 两个路由定义在 `if _FRONTEND_DIST.is_dir():` 块里，其余在模块顶层
    top = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top.append(stmt)
        elif isinstance(stmt, (ast.If, ast.Try)):
            top.extend(n for n in stmt.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    nodes = [n for n in top if n.name in wanted]
    assert {n.name for n in nodes} == wanted, "app.py 里找不到预期的路由/中间件函数"
    namespace = {k: v for k, v in vars(app_module).items() if not k.startswith("__")}
    namespace.update(app=FastAPI(), _FRONTEND_DIST=dist, _API_KEY="test-only",
                     _KEY_FREE_PATHS=frozenset(), _MAX_BODY_BYTES=1024)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "spa_routes", "exec"), namespace)
    return TestClient(namespace["app"])


@pytest.mark.parametrize("url", ["/%2e%2e/%2e%2e/private.txt", "/%2Fetc/passwd", "/escape.txt"])
def test_spa_rejects_traversal_absolute_and_symlink_escape(client, url):
    assert client.get(url).status_code == 404


def test_spa_preserves_assets_and_client_routes(client):
    assert client.get("/icon.svg").text == "safe asset"
    assert client.get("/allocation").text == "<html>app</html>"
    assert client.get("/").status_code == 200


def test_api_requires_key_via_bearer_or_header(client):
    assert client.get("/api/missing").status_code == 401
    assert client.get("/api/missing", headers={"Authorization": "Bearer test-only"}).status_code == 404
    assert client.get("/api/missing", headers={"X-VR-Access-Key": "test-only"}).status_code == 404
    # 前缀相同的错误密钥不能通过
    assert client.get("/api/missing", headers={"X-VR-Access-Key": "test-onl"}).status_code == 401


def test_oversized_body_rejected_before_handler(client):
    # 阈值在本夹具里调小到 1KB；超过即在中间件返回 413，请求体不会被读入
    r = client.post("/api/missing", headers={"X-VR-Access-Key": "test-only"}, content=b"x" * 2048)
    assert r.status_code == 413
