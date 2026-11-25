"""pytest 配置：把 backend 目录加进 sys.path，注册 live 标记，隔离用户数据目录。"""
import atexit
import os
import shutil
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# 用户数据隔离：portfolio / myreports 在 import 时按 MW_DATA_DIR / MW_REPORTS_DIR 固化路径，
# 必须赶在任何测试模块 import app 之前指到临时目录——否则持仓 CRUD 类测试会增删真实
# ~/.market-workbench/ 里的用户数据（比如把用户真实持有的 600519 合并后删掉）。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="mw-test-data-")
os.environ["MW_USERS_DB"] = os.path.join(_TEST_DATA_DIR, "users.db")
os.environ["MW_DATA_DIR"] = _TEST_DATA_DIR
os.environ["MW_REPORTS_DIR"] = os.path.join(_TEST_DATA_DIR, "myreports")
# 进程退出时清掉临时目录（此前每跑一次都在系统临时目录里留一份）
atexit.register(lambda: shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True))

# 测试账号的用户名形状：t + 12 位十六进制。会话结束时按这个形状兜底清理。
_TEMP_USER = "t____________"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: 打真实数据源的网络冒烟测（会联网、可能受上游/限流影响；默认可 -m 'not live' 跳过）",
    )


def new_login() -> tuple[int, dict]:
    """建一个临时账号并登录，返回 (uid, 鉴权头)。"""
    import users

    name = f"t{uuid.uuid4().hex[:12]}"
    uid = users.register(name, "test-password-1")["id"]
    token = users.login(name, "test-password-1")["token"]
    return uid, {"X-VR-User-Token": token}


def auth_headers() -> dict:
    """只取鉴权头（供模块级客户端使用）；账号在会话结束时按用户名形状兜底清理。"""
    return new_login()[1]


def _drop_user(uid: int) -> None:
    import users

    with users._conn() as c:
        for table in ("sessions", "user_data", "user_data_backups"):
            c.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    """登录失败限流按「IP + 用户名」在进程内计数；用例之间互不继承失败次数。"""
    import app as app_module

    app_module._LOGIN_FAILS.clear()
    yield


def authed_client(app=None):
    """带登录态的测试客户端：所有请求自动附带一个临时账号的会话头（显式传入的 headers 仍优先）。
    供模块级 `client = TestClient(app)` 的旧用例使用——写端点现在默认要求登录。"""
    from fastapi.testclient import TestClient

    if app is None:
        import app as app_module
        app = app_module.app
    default = auth_headers()

    class _AuthedClient(TestClient):
        def build_request(self, method, url, **kwargs):
            headers = {**default, **(kwargs.pop("headers", None) or {})}
            return super().build_request(method, url, headers=headers, **kwargs)

    return _AuthedClient(app)


@pytest.fixture
def logged_in():
    """测试用登录态：临时账号 + 会话，鉴权头用完即删。
    写操作、强制刷新、调 LLM 的端点现在都要求登录，用到它们的用例带上这组头。"""
    uid, headers = new_login()
    yield headers
    _drop_user(uid)


def pytest_sessionfinish(session, exitstatus):
    import users

    with users._conn() as c:
        ids = [int(r["id"]) for r in c.execute("SELECT id FROM users WHERE username LIKE ?", (_TEMP_USER,))]
        for uid in ids:
            for table in ("sessions", "user_data", "user_data_backups"):
                c.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))
