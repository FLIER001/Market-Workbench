"""云账号显示名回归测。

背景：影子账号的 username 是 `wb:<cloud_id>` 这种内部标识，曾经被直接回给前端，
登录横幅与侧栏就显示成 `wb:6MCV1c77...`。现在 display_name 单独一列：
- 首次云登录写入云端昵称；
- 再次登录时云端没返回昵称，沿用旧昵称，不退回影子 id；
- 云端改名会同步；
- 完全没昵称时给人读占位「WorkBuddy 用户」，不泄露 id。

全部离线；用户库 monkeypatch 到 tmp_path。
"""
import users
import app as app_module
from fastapi.testclient import TestClient

client = TestClient(app_module.app)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()


def _me(token: str):
    return client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})


def test_display_name_survives_relogin(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    first = users.ensure_cloud_user("cid-1", "K")
    assert first["username"] == "K"

    # 云端第二次没给昵称（缓存里只有 id），不能退回 wb:cid-1
    again = users.ensure_cloud_user("cid-1", "")
    assert again["username"] == "K"
    assert again["id"] == first["id"]


def test_display_name_follows_cloud_rename(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    users.ensure_cloud_user("cid-2", "K")
    renamed = users.ensure_cloud_user("cid-2", "Kevin")
    assert renamed["username"] == "Kevin"


def test_anonymous_cloud_user_gets_readable_label(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    u = users.ensure_cloud_user("cid-3", "")
    assert u["username"] == "WorkBuddy 用户"
    assert not u["username"].startswith("wb:")


def test_me_returns_display_name_not_shadow_id(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    u = users.ensure_cloud_user("cid-4", "Amy")
    token = users.issue_session(u["id"])
    body = _me(token).json()["data"]
    assert body["username"] == "Amy"


def test_password_account_keeps_plain_username(tmp_path, monkeypatch):
    """普通账号没有 display_name，展示名必须还是用户名本身，不能被占位符顶掉。"""
    _setup(tmp_path, monkeypatch)
    reg = users.register("flier", "secret123")
    assert reg["username"] == "flier"
    login = users.login("flier", "secret123")
    assert login["username"] == "flier"
    assert users.resolve_token(login["token"])["username"] == "flier"


def test_shadow_account_cannot_password_login(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    users.ensure_cloud_user("cid-5", "K")
    try:
        users.login("wb:cid-5", "whatever")
    except ValueError:
        return
    raise AssertionError("影子账号不应允许密码登录")
