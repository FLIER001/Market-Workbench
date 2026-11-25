"""2026-11-25 全项目审查的修复回归：登录防撞库、云账号防预劫持、会话哈希、
体积上限、SSRF 重定向、表达式重复求值、诊断接口、TLS 证书链、路径单一真相。全部离线。"""
import inspect
import json
import os
import threading
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as app_module
import chat
import cli_runtime
import factor_expr
import mcp_server
import paths
import users

client = TestClient(app_module.app)


# ── 登录：失败限流 ────────────────────────────────────────────────

def test_login_locks_out_after_repeated_failures():
    username = "brute-target"
    users.register(username, "correct-password")
    key = f"testclient|{username}"
    codes = [client.post("/api/auth/login", json={"username": username, "password": "wrong"}).status_code
             for _ in range(app_module._LOGIN_MAX_FAILS)]
    assert codes == [400] * app_module._LOGIN_MAX_FAILS
    # 窗口已满：即使密码正确也拒绝，直到窗口滑过
    assert client.post("/api/auth/login", json={"username": username, "password": "correct-password"}).status_code == 429
    # 换一个用户名不受影响（限流键含用户名）
    assert client.post("/api/auth/login", json={"username": "someone-else", "password": "x"}).status_code == 400


def test_login_limiter_window_expires():
    username = "window-target"
    users.register(username, "correct-password")
    key = f"testclient|{username}"
    for _ in range(app_module._LOGIN_MAX_FAILS):
        client.post("/api/auth/login", json={"username": username, "password": "wrong"})
    assert app_module._login_blocked(key)
    with app_module._LOGIN_FAILS_LOCK:
        stale = app_module._LOGIN_FAILS[key]
        for i in range(len(stale)):
            stale[i] -= app_module._LOGIN_WINDOW_S + 1
    assert not app_module._login_blocked(key)


def test_unknown_user_costs_a_password_hash(monkeypatch):
    """用户不存在时也要算一次 PBKDF2：否则「不存在」比「密码错误」快一个数量级，可枚举用户名。"""
    calls = []
    real = users._hash_pw
    monkeypatch.setattr(users, "_hash_pw", lambda pw, salt: (calls.append(salt), real(pw, salt))[1])
    assert users._verify("no-such-user", "whatever") is None
    assert len(calls) == 1
    users.register("known-user", "correct-password")
    calls.clear()
    assert users._verify("known-user", "wrong") is None
    assert len(calls) == 1


# ── 云账号：wb: 前缀与 cloud_id ──────────────────────────────────

def test_register_rejects_cloud_shadow_prefix():
    with pytest.raises(ValueError):
        users.register("wb:someone-elses-cloud-id", "password1")


def test_pre_registered_shadow_name_is_not_taken_over():
    """抢注 wb:<cloud_id> 后，真正的云用户登录不会被映射到抢注者账号上。"""
    # 修复前 register 允许 wb: 前缀：模拟一个当时抢注出来的普通账号（现在是真 PBKDF2 哈希的密码账号）
    salt = "cd" * 16
    with users._conn() as c:
        hijacker_id = int(c.execute(
            "INSERT INTO users (username, pw_hash, salt, created_at) VALUES (?,?,?,?)",
            ("wb:hijack-me", users._hash_pw("attacker-password", salt), salt, time.time())).lastrowid)
    cloud = users.ensure_cloud_user("hijack-me", "Victim")
    assert cloud["id"] != hijacker_id
    # 再次登录仍落到同一个影子账号
    assert users.ensure_cloud_user("hijack-me", "Victim")["id"] == cloud["id"]
    with users._conn() as c:
        row = c.execute("SELECT cloud_id, pw_hash FROM users WHERE id=?", (hijacker_id,)).fetchone()
        shadow = c.execute("SELECT cloud_id, pw_hash FROM users WHERE id=?", (cloud["id"],)).fetchone()
    assert row["cloud_id"] is None            # 抢注账号不获得 cloud_id
    assert shadow["cloud_id"] == "hijack-me"  # 云账号按 cloud_id 匹配
    assert shadow["pw_hash"] == users._CLOUD_ONLY_HASH


def test_legacy_plaintext_shadow_rows_migrate_by_hash_marker():
    """老库里的 wb: 行只有密码哈希为 !cloud-only 的才迁移出 cloud_id。"""
    with users._conn() as c:
        c.execute("INSERT INTO users (username, pw_hash, salt, created_at, display_name) VALUES (?,?,?,?,?)",
                  ("wb:legacy", users._CLOUD_ONLY_HASH, "ab" * 16, time.time(), None))
        c.execute("INSERT INTO users (username, pw_hash, salt, created_at, display_name) VALUES (?,?,?,?,?)",
                  ("wb:legacy-fake", "deadbeef", "ab" * 16, time.time(), None))
        c.execute("UPDATE users SET cloud_id = NULL WHERE username IN ('wb:legacy', 'wb:legacy-fake')")
    users.init_db()
    with users._conn() as c:
        rows = dict(c.execute("SELECT username, cloud_id FROM users WHERE username LIKE 'wb:legacy%'").fetchall())
    assert rows == {"wb:legacy": "legacy", "wb:legacy-fake": None}


# ── 会话 ────────────────────────────────────────────────────────

def test_session_tokens_are_stored_hashed_and_migrated():
    users.register("hash-user", "correct-password")
    token = users.login("hash-user", "correct-password")["token"]
    with users._conn() as c:
        stored = c.execute("SELECT token FROM sessions").fetchall()
    assert all(len(r["token"]) == 64 for r in stored)
    assert token not in {r["token"] for r in stored}       # 库里没有明文
    assert users.resolve_token(token)["username"] == "hash-user"
    # 老库的明文会话在下次启动时就地迁移，登录态不丢
    with users._conn() as c:
        c.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                  ("legacy-plaintext-token", users.login("hash-user", "correct-password")["user_id"],
                   time.time(), time.time() + 100))
    users.init_db()
    assert users.resolve_token("legacy-plaintext-token")["username"] == "hash-user"
    users.logout("legacy-plaintext-token")
    assert users.resolve_token("legacy-plaintext-token") is None


def test_request_path_does_not_write_sessions_table():
    """鉴权请求不再顺手 DELETE 过期会话（此前每个请求一次写事务）。"""
    users.register("quiet-user", "correct-password")
    token = users.login("quiet-user", "correct-password")["token"]
    statements = []
    real_conn = users._conn

    def traced_conn():
        conn = real_conn()
        conn.set_trace_callback(statements.append)
        return conn

    users.resolve_token(token)  # 预热模块级清理时间片
    statements.clear()
    original, users._conn = users._conn, traced_conn
    try:
        users.resolve_token(token)
        users.resolve_token(token)
    finally:
        users._conn = original
    writes = [sql for sql in statements if sql.lstrip().upper().startswith(("DELETE", "INSERT", "UPDATE"))]
    assert writes == [], writes


def test_sqlite_connections_use_wal_and_busy_timeout():
    """每个连接都开 WAL 与 busy_timeout：读写不互斥，并发请求不再因「正在读」而拿不到写锁。"""
    conn = users._conn()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


# ── 体积上限 ───────────────────────────────────────────────────

def test_oversized_body_is_rejected_413(logged_in):
    big = "x" * (app_module._MAX_BODY_BYTES + 1)
    r = client.post("/api/auth/data/set", headers=logged_in, json={"key": "notes", "value": big})
    assert r.status_code == 413


def test_single_user_data_key_is_capped(logged_in):
    r = client.post("/api/auth/data/set", headers=logged_in,
                    json={"key": "notes", "value": "x" * (users.MAX_VALUE_BYTES + 1)})
    assert r.status_code == 400


# ── SSRF：不跟随重定向 ──────────────────────────────────────────

@pytest.mark.parametrize("stream", [False, True])
def test_llm_requests_never_follow_redirects(monkeypatch, stream):
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(chat.requests, "post", fake_post)
    cfg = {"baseURL": "https://api.deepseek.com", "apiKey": "k", "model": "m"}
    if stream:
        chat._call_llm_stream(cfg, [{"role": "user", "content": "hi"}], use_tools=False)
    else:
        chat._call_llm(cfg, [{"role": "user", "content": "hi"}], use_tools=False)
    assert seen["allow_redirects"] is False


def test_redirect_response_is_an_error(monkeypatch):
    class _Resp:
        status_code = 302
        text = ""

    monkeypatch.setattr(chat.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError, match="重定向"):
        chat._call_llm({"baseURL": "https://evil.example", "apiKey": "k", "model": "m"},
                       [{"role": "user", "content": "hi"}], use_tools=False)


# ── 因子表达式：不重复求值 ──────────────────────────────────────

def test_nested_expression_evaluates_each_subtree_once(monkeypatch):
    original = factor_expr._eval
    field_evals = {"n": 0}

    def counting(node, w):
        if node[0] == "field":
            field_evals["n"] += 1
        return original(node, w)

    monkeypatch.setattr(factor_expr, "_eval", counting)
    frame = pd.DataFrame({"a": [float(i) for i in range(40)]})
    for depth in (1, 4, 12):
        field_evals["n"] = 0
        expr = "close"
        for _ in range(depth):
            expr = f"ts_mean({expr},5)"
        factor_expr.evaluate(factor_expr.compile_expr(expr), {"close": frame})
        # 此前为 2^depth（深度 12 → 4096 次）
        assert field_evals["n"] == 1


# ── CLI agent 开关 ──────────────────────────────────────────────

def test_auto_agent_clis_are_gated(monkeypatch):
    monkeypatch.setattr(cli_runtime, "_AGENTS_ENABLED", False)
    monkeypatch.setattr(cli_runtime, "_find_bin", lambda name: f"/usr/local/bin/fake-{name}")
    for kind in ("qwen", "deepseek", "codex"):
        assert cli_runtime.detect_cli(kind) is None
        assert "MW_CLI_AGENTS" in cli_runtime.agent_notice(kind)
        with pytest.raises(cli_runtime.CliUnavailable):
            cli_runtime.run_cli(kind, "sys", "user")
    # Claude 一侧禁用了工具，不受开关影响
    assert cli_runtime.detect_cli("claude") == "/usr/local/bin/fake-claude"
    # 显式开启后恢复可用
    monkeypatch.setattr(cli_runtime, "_AGENTS_ENABLED", True)
    assert cli_runtime.detect_cli("qwen") == "/usr/local/bin/fake-qwen"
    assert cli_runtime.agent_notice("qwen") is None


# ── 诊断接口 ───────────────────────────────────────────────────

def test_cloud_diag_and_selftest_require_login():
    assert client.get("/api/auth/cloud/diag").status_code == 401
    assert client.get("/api/auth/cloud/selftest").status_code == 401
    assert client.post("/api/auth/cloud/data-probe", json={"ok": True}).status_code == 401


def test_cloud_probe_never_retains_response_body(logged_in, monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def __init__(self):
            self.text = '{"user": {"id": "u1", "email": "someone@example.com", "phone": "13800000000"}}'

        def json(self):
            return {"user": {"id": "u1", "email": "someone@example.com", "phone": "13800000000"}}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(app_module, "_CLOUD_ENDPOINT", "https://cloud.example")
    monkeypatch.setattr(app_module, "_CLOUD_PUBLISHABLE_KEY", "pk")
    app_module._cloud_user_from_token("abc.def.ghi", None)
    probes = app_module._CLOUD_LAST_PROBE["probes"]
    assert all("body" not in p for p in probes)
    dumped = repr(app_module._CLOUD_LAST_PROBE)
    assert "someone@example.com" not in dumped and "13800000000" not in dumped
    # 未登录读不到诊断信息；登录后可读
    assert client.get("/api/auth/cloud/diag").status_code == 401
    assert client.get("/api/auth/cloud/diag", headers=logged_in).status_code == 200


def test_health_is_async_so_long_jobs_cannot_starve_it():
    assert inspect.iscoroutinefunction(app_module.health)


# ── 深度分析并发上限 ────────────────────────────────────────────

def test_deep_analysis_caps_concurrency(monkeypatch, logged_in):
    release = threading.Event()
    state = {"calls": 0, "peak": 0}
    monkeypatch.setattr(app_module, "_DEEP_ANALYSIS_KEY", "k")
    monkeypatch.setattr(app_module, "_DEEP_ANALYSIS_SLOTS", threading.BoundedSemaphore(1))

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def slow_post(*a, **k):
        state["calls"] += 1
        state["peak"] = max(state["peak"], state["calls"])
        release.wait(5)
        state["calls"] -= 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", slow_post)
    results = []

    def call():
        results.append(client.post("/api/deep-analysis", headers=logged_in, json={"prompt": "x"}).status_code)

    t = threading.Thread(target=call)
    t.start()
    deadline = time.time() + 5
    while state["calls"] == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert client.post("/api/deep-analysis", headers=logged_in, json={"prompt": "x"}).status_code == 429
    release.set()
    t.join(10)
    assert results == [200]


# ── MCP server ──────────────────────────────────────────────────

def test_mcp_server_ignores_non_object_lines(monkeypatch, capsys):
    out = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: out.append(obj))
    monkeypatch.setattr("sys.stdin", iter(['[1,2,3]', '42', '"str"',
                                           json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})]))
    mcp_server.main()  # 不应抛异常
    assert out and out[-1]["id"] == 1


# ── 路径单一真相 ────────────────────────────────────────────────

def test_paths_resolve_from_single_source(monkeypatch):
    import importlib
    expected_default = str(Path.home() / ".market-workbench")
    with monkeypatch.context() as mp:
        mp.setattr(paths.os, "environ", {"MW_DATA_DIR": "", "MW_USERS_DB": "", "MW_REPORTS_DIR": ""})
        fresh = importlib.reload(paths)
        assert fresh.DATA_DIR == expected_default                      # 空串视同未设置
        assert fresh.USERS_DB == os.path.join(expected_default, "users.db")  # 用户库默认在数据目录内
        assert fresh.REPORTS_DIR == os.path.join(expected_default, "myreports")
        mp.setattr(paths.os, "environ", {"MW_DATA_DIR": "/srv/mw", "MW_REPORTS_DIR": ""})
        fresh = importlib.reload(paths)
        assert fresh.DATA_DIR == "/srv/mw" and fresh.USERS_DB == "/srv/mw/users.db"
        assert fresh.subdir("x", "y.json") == "/srv/mw/x/y.json"
    importlib.reload(paths)  # 还原为测试进程的真实配置


def test_no_module_resolves_data_dir_ad_hoc():
    """数据目录只从 paths 取；模块里再出现硬编码的 ~/.market-workbench 解析会漂移。"""
    source = Path(app_module.__file__).parent
    offenders = [py.name for py in source.glob("*.py")
                if py.name not in {"paths.py", "conftest.py"}
                and 'os.environ.get("MW_DATA_DIR")' in py.read_text(encoding="utf-8")]
    assert not offenders, f"这些模块绕过了 paths.py：{offenders}"


# ── TLS 证书链 ──────────────────────────────────────────────────

def test_swsresearch_bundle_completes_the_chain_without_disabling_verification():
    import certifi
    import tls_verify

    bundle = tls_verify.swsresearch_verify()
    assert Path(bundle).exists()
    text = Path(bundle).read_text(encoding="utf-8")
    roots = Path(certifi.where()).read_text(encoding="utf-8")
    assert text.startswith(roots.strip())                            # 保留全部公共根证书
    assert text.count("BEGIN CERTIFICATE") == roots.count("BEGIN CERTIFICATE") + 1
    assert "BEGIN CERTIFICATE" in text[len(roots.strip()):]          # 且补上了缺失的中间证书
    assert "GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1" not in roots  # （它确实不在公共根证书里）
    assert tls_verify.ca_bundle("swsresearch", Path("/nonexistent/x.pem")).endswith(".pem")  # 文件缺失也退回标准根证书


def test_source_code_has_no_disabled_tls_verification():
    source = Path(app_module.__file__).parent
    offenders = [py.name for py in source.glob("*.py") if py.name != "tls_verify.py"
                 if "verify=False" in py.read_text(encoding="utf-8")]
    assert not offenders, f"这些模块关闭了 TLS 校验：{offenders}"
