"""鉴权边界守卫：写操作与高成本端点必须要求登录。

背景（2026-11-25 审查）：`/api/chat`、`/api/myreports*`、`/api/factor/custom`、
`/api/radar/refresh`、`/api/deep-analysis` 等写操作与花钱的端点此前没有登录校验，
任何能访问端口的人都能调用。这类缺口没有任何测试守着，加端点时再漏一次也不会被发现。

两层检查：
- 静态：app.py 里每个非 GET 的路由处理函数都必须调用 `_current_user(...)`；
  带 refresh / force 开关的 GET 路由，开关生效时也必须要求登录。
- 动态：不带必填参数也能路由到的非 GET 端点，匿名调用必须 401。
"""
import ast
import inspect
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app as app_module
import factor_lab_routes

# 登录前就必须可达的端点（拿不到 token 时也要能调）
_PUBLIC_WRITE_ROUTES = {
    "/api/auth/login", "/api/auth/register", "/api/auth/cloud",
    "/api/auth/cloud/data-probe",
    "/api/auth/logout",  # 退出登录对匿名请求幂等，无需 token
}

_APP_SOURCE = Path(app_module.__file__).read_text(encoding="utf-8")


def _handler_functions() -> dict[str, list[ast.FunctionDef]]:
    """路由路径 → app.py 里对应的处理函数（含嵌套在 if 块里的）。"""
    tree = ast.parse(_APP_SOURCE)
    out: dict[str, list[ast.FunctionDef]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                    and isinstance(deco.func.value, ast.Name) and deco.func.value.id == "app"):
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    out.setdefault(deco.args[0].value, []).append(node)
    return out


# 处理函数里出现这些名字即视为有登录门槛：
# - _current_user：直接要求登录
# - _portfolio_user_id：持仓账本的账号归属，内部即 _current_user
# - _require_user_if：仅当 refresh / force 开关生效时要求登录（以裸函数经 run_in_threadpool 调用）
_LOGIN_GATES = ("_current_user", "_portfolio_user_id")


def _mentions(node: ast.FunctionDef, names: tuple[str, ...]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def test_static_every_mutating_route_requires_login():
    handlers = _handler_functions()
    offenders = []
    for route in app_module.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        mutating = set(route.methods) - {"GET", "HEAD", "OPTIONS"}
        if not mutating or route.path in _PUBLIC_WRITE_ROUTES:
            continue
        nodes = handlers.get(route.path) or []
        if not nodes:  # APIRouter 挂载的路由不在此处（见下一个用例）
            continue
        if not all(_mentions(n, _LOGIN_GATES) for n in nodes):
            offenders.append(route.path)
    assert not offenders, f"这些写端点没有调用 _current_user：{sorted(set(offenders))}"


def test_static_factor_research_router_authenticates_before_handler():
    # 因子研究路由统一在 ScopedRoute 里鉴权（先鉴权再解析请求体）
    src = inspect.getsource(factor_lab_routes)
    assert "current_user_callback(request)" in src
    assert src.index("current_user_callback(request)") < src.index("return await handler(request)")


def test_static_refresh_flags_are_login_gated():
    """带 refresh / force 参数的 GET 路由，处理函数必须调用 `_require_user_if` 或 `_current_user`。"""
    handlers = _handler_functions()
    offenders = []
    for route in app_module.app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        names = {p.name for p in route.dependant.query_params}
        flag = next((n for n in ("refresh", "force") if n in names), None)
        if flag is None:
            continue
        nodes = handlers.get(route.path) or []
        gated = all(_mentions(n, _LOGIN_GATES + ("_require_user_if",)) for n in nodes)
        if not gated:
            offenders.append(f"{route.path} ({flag})")
    assert not offenders, f"这些强制刷新开关没有登录门槛：{offenders}"


def test_dynamic_anonymous_mutating_requests_are_rejected():
    """全部非 GET 的 /api 路由（不含路径参数、不在匿名白名单）：匿名调用一律 401。
    中间件在解析请求体之前判定，所以不需要为每个端点构造合法请求体。"""
    # 不进 lifespan：这里只验鉴权，不启动调度与预热
    client = TestClient(app_module.app)
    checked, bad = 0, []
    for route in app_module.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        if set(route.methods) <= {"GET", "HEAD", "OPTIONS"}:
            continue
        if "{" in route.path or route.path.rstrip("/") in _PUBLIC_WRITE_ROUTES:
            continue  # 带路径参数的端点走同一道中间件；白名单见上
        for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
            response = client.request(method, route.path, json={})
            checked += 1
            if response.status_code != 401:
                bad.append((method, route.path, response.status_code))
    assert checked >= 15, "动态检查覆盖到的端点过少，守卫失效"
    assert not bad, f"匿名调用没有被 401 挡下：{bad}"


def test_anonymous_write_allowlist_is_exactly_the_auth_entrypoints():
    # 白名单本身也要钉住：往里加路径等于放开一个匿名写端点，必须有意识地改这里
    assert app_module._ANONYMOUS_WRITE_PATHS == {
        "/api/auth/login", "/api/auth/register", "/api/auth/cloud", "/api/auth/logout",
    }


def test_dynamic_anonymous_refresh_is_rejected(logged_in, monkeypatch):
    # 数据层一律打桩：真打 /api/sector-scores 会触发一次全市场重算（分钟级，且会起一个
    # 占着 score_worker 锁的子进程，把后面 test_subprocess_recalc 之类要用这把锁的用例挂住）。
    calls = []
    monkeypatch.setattr(app_module.sector_scores_layer, "get_sector_scores",
                        lambda force=False: (calls.append(force) or {"schema_version": 4, "industries": []}))
    client = TestClient(app_module.app)
    assert client.get("/api/sector-scores").status_code == 200   # 不带 refresh 的普通读取保持开放
    assert calls == [False]
    # refresh=true：匿名 401 且数据层根本没被调用；登录后放行到处理函数
    calls.clear()
    r = client.get("/api/sector-scores?refresh=true")
    assert r.status_code == 401 and calls == []
    r = client.get("/api/sector-scores?refresh=true", headers=logged_in)
    assert r.status_code == 200 and calls == [True]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
