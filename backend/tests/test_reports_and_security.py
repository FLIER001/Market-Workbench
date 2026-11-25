"""我的研报 + 安全守卫（SSRF / 成本负数 / 日期）回归测。全部离线、不联网。

覆盖 2026-07-07 粉丝反馈批量修复中新增/加固的后端面：
- myreports：文件名分行业、存取删、类型白名单、data:URI 守卫、原子写。
- chat._check_base_url：防 SSRF（本地放行本机、始终挡云元数据、公网姿态挡内网）。
- 成本允许负数、清仓日期格式校验。
"""
import base64
import sys

import pytest
from fastapi.testclient import TestClient

import app as app_module
import chat
import myreports as mr

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def legacy_ledger_unit_scope(monkeypatch):
    """账本归属固定为全局文件；模块级 client 带登录态（写端点默认要求登录）。
    需要验证匿名行为的用例自己建不带头的 TestClient。"""
    import conftest
    monkeypatch.setattr(app_module, "_portfolio_user_id", lambda request: None)
    monkeypatch.setattr(sys.modules[__name__], "client", conftest.authed_client())

_B64 = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 test").decode()


# ---- 我的研报 ----

def test_classify_by_filename():
    assert mr.classify("东吴证券_中际旭创_光模块深度.pdf") == "光互联"
    assert mr.classify("宇树科技_人形机器人.pdf") == "人形机器人"
    assert mr.classify("随手记.txt") == "未分类"


def test_report_roundtrip_and_delete(logged_in):
    r = client.post("/api/myreports", json={"name": "长鑫_HBM_深度.pdf", "content_b64": _B64}, headers=logged_in)
    assert r.status_code == 200
    meta = r.json()["data"]
    assert meta["industry"] == "HBM存储"
    rid = meta["id"]
    try:
        assert any(x["id"] == rid for x in client.get("/api/myreports", headers=logged_in).json()["data"])
        assert client.get(f"/api/myreports/file/{rid}", headers=logged_in).status_code == 200
    finally:
        assert client.delete(f"/api/myreports/{rid}", headers=logged_in).json()["data"]["ok"] is True
    assert client.get(f"/api/myreports/file/{rid}", headers=logged_in).status_code == 404


def test_report_illegal_type_400(logged_in):
    r = client.post("/api/myreports", json={"name": "x.exe", "content_b64": _B64}, headers=logged_in)
    assert r.status_code == 400


def test_report_data_uri_without_comma_400(logged_in):
    # 之前会 IndexError→500，现应 400
    assert client.post("/api/myreports", json={"name": "a.pdf", "content_b64": "data:"}, headers=logged_in).status_code == 400


def test_report_missing_file_404(logged_in):
    assert client.get("/api/myreports/file/does-not-exist", headers=logged_in).status_code == 404


def test_reports_require_login_and_are_isolated_per_user(logged_in):
    # 研报是用户私有资料：未登录 401；另一个登录用户看不到、删不掉、下载不到本人的记录
    import conftest
    anonymous = TestClient(app_module.app)  # 模块级 client 自带登录态，这里要匿名的
    assert anonymous.get("/api/myreports").status_code == 401
    assert anonymous.post("/api/myreports", json={"name": "a.pdf", "content_b64": _B64}).status_code == 401
    rid = client.post("/api/myreports", json={"name": "a.pdf", "content_b64": _B64},
                      headers=logged_in).json()["data"]["id"]
    other = conftest.auth_headers()
    assert client.get("/api/myreports", headers=other).json()["data"] == []
    assert client.get(f"/api/myreports/file/{rid}", headers=other).status_code == 404
    assert client.delete(f"/api/myreports/{rid}", headers=other).json()["data"]["ok"] is False
    assert client.get(f"/api/myreports/file/{rid}", headers=logged_in).status_code == 200


# ---- SSRF 守卫 ----

def _allowed(url: str) -> bool:
    try:
        chat._check_base_url(url)
        return True
    except RuntimeError:
        return False


def test_ssrf_local_mode():
    assert chat._PUBLIC_MODE is False  # 测试进程未设 MW_API_KEY
    assert _allowed("https://api.deepseek.com") is True
    assert _allowed("http://127.0.0.1:11434") is True   # 本机 Ollama 等，本地放行
    assert _allowed("http://169.254.169.254/latest") is False  # 云元数据，始终挡
    assert _allowed("ftp://evil/x") is False


def test_ssrf_public_mode_blocks_internal(monkeypatch):
    monkeypatch.setattr(chat, "_PUBLIC_MODE", True)
    assert _allowed("http://192.168.1.1") is False
    assert _allowed("http://10.0.0.5") is False
    assert _allowed("http://127.0.0.1:11434") is False
    # 注：公网域名在 public 姿态会走真实 DNS 解析核对，为保持离线不在此断言


# ---- 成本负数 / 日期 ----

def test_negative_cost_accepted():
    r = client.post("/api/portfolio/holding", json={"code": "600519", "shares": 100, "cost": -5.5})
    assert r.status_code == 200
    client.request("DELETE", "/api/portfolio/holding", params={"code": "600519"})  # 清理


def test_zero_shares_rejected():
    assert client.post("/api/portfolio/holding", json={"code": "600519", "shares": 0, "cost": 10}).status_code == 400


def test_close_bad_date_400():
    r = client.post("/api/portfolio/close",
                    json={"code": "600519", "date": "2025-13-45", "price": 10, "shares": 100, "cost": 5})
    assert r.status_code == 400
