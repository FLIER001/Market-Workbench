"""生产 SPA 路由的 HTTP 验证；隔离 app import 的联网预热。"""
import ast
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "icon.svg").write_text("safe asset")
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (dist / "escape.txt").symlink_to(outside)
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in {"spa_index", "spa_fallback", "_require_api_key"}]
    namespace = dict(app=FastAPI(), HTTPException=HTTPException, Request=Request,
                     FileResponse=FileResponse, JSONResponse=JSONResponse, Path=Path,
                     _FRONTEND_DIST=dist, _API_KEY="test-only")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "spa_routes", "exec"), namespace)
    return TestClient(namespace["app"])


@pytest.mark.parametrize("url", ["/%2e%2e/%2e%2e/private.txt", "/%2Fetc/passwd", "/escape.txt"])
def test_spa_rejects_traversal_absolute_and_symlink_escape(client, url):
    assert client.get(url).status_code == 404


def test_spa_preserves_assets_and_client_routes(client):
    assert client.get("/icon.svg").text == "safe asset"
    assert client.get("/allocation").text == "<html>app</html>"
    assert client.get("/").status_code == 200
    assert client.get("/api/missing").status_code == 401
    assert client.get("/api/missing", headers={"Authorization": "Bearer test-only"}).status_code == 404
