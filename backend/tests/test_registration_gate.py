"""注册开关回归测：默认关闭公开注册、空库放行首账号、环境变量可显式重开。

背景：注册原本对任何访客开放（任何人都可 POST /api/auth/register 自建账号
绕过「暂不开放注册」的运营意图）。现在：
- 默认（VR_ALLOW_REGISTRATION 未设）→ 有用户后 403；
- 用户库为空 → 放行（新部署要能建出主账号）；
- VR_ALLOW_REGISTRATION=1 → 重开网页注册。

全部离线；用户库 monkeypatch 到 tmp_path。
"""
import users
import app as app_module
from fastapi.testclient import TestClient

client = TestClient(app_module.app)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()


def _register(body=None):
    return client.post("/api/auth/register", json=body or {"username": "alice", "password": "secret1"})


def test_registration_closed_by_default_once_users_exist(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("VR_ALLOW_REGISTRATION", raising=False)
    monkeypatch.setattr(app_module, "_REGISTRATION", False)

    # 空库：放行首个账号
    assert _register().status_code == 200
    # 有用户后：关
    assert _register({"username": "bob", "password": "secret1"}).status_code == 403
    # 直连重试也无济于事（同一个端点，无旁路）
    assert client.post("/api/auth/register", json={"username": "bob", "password": "secret1"}).status_code == 403


def test_registration_open_with_env_flag(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "_REGISTRATION", True)
    assert _register().status_code == 200
    assert _register({"username": "bob", "password": "secret1"}).status_code == 200


def test_auth_config_reports_registration_state(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("VR_ALLOW_REGISTRATION", raising=False)
    monkeypatch.setattr(app_module, "_REGISTRATION", False)

    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json()["data"]["registration_open"] is True  # 空库 → 首账号放行

    _register()
    r = client.get("/api/auth/config")
    assert r.json()["data"]["registration_open"] is False


def test_register_validation_still_enforced(tmp_path, monkeypatch):
    """开关放行时，原校验（用户名长度/密码长度/重名）不受影响。"""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "_REGISTRATION", True)
    assert _register({"username": "a", "password": "secret1"}).status_code == 400
    assert _register({"username": "alice", "password": "123"}).status_code == 400
    assert _register().status_code == 200
    assert _register().status_code == 400  # 重名
