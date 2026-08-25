"""用户数据导入/导出（备份迁移）回归测。全部离线、不联网。

覆盖：
- /api/auth/export：user_data + 两个账本齐、apiKey 抹掉、不带密码/会话/user id。
- /api/auth/import：merge 只补缺 key / replace 整体替换；账本 skip/merge/replace；
  format 不匹配 400；坏账本 400；未登录 401。
"""
import users
import portfolio as pf
import fund_portfolio as fpf
import app as app_module
from fastapi.testclient import TestClient

client = TestClient(app_module.app)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()
    monkeypatch.setattr(pf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pf, "PF_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(pf.astock, "tencent_quote", lambda codes: {})
    monkeypatch.setattr(fpf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(fpf, "FPF_FILE", str(tmp_path / "fund_portfolio.json"))
    monkeypatch.setattr(fpf.fund, "realtime_estimates", lambda codes, bypass=False: {})
    pf._invalidate()
    fpf._invalidate()
    # 每个用例独立账号，避免用例间通过真实 token 互相串
    user = users.register("backup-user", "secret1")
    token = users.login("backup-user", "secret1")["token"]
    return {"X-VR-User-Token": token}, int(user["id"])


def test_export_redacts_key_and_omits_credentials(tmp_path, monkeypatch):
    headers, uid = _setup(tmp_path, monkeypatch)
    users.set_data(uid, "watchlist", ["600519"])
    users.set_data(uid, "llm", {"provider": "deepseek", "apiKey": "sk-secret", "model": "x", "baseURL": "y"})
    pf.add_holding("600519", 100, 10, user_id=uid)
    fpf.add_holding("000001", 200, 1.5, user_id=uid)

    data = client.get("/api/auth/export", headers=headers).json()["data"]
    assert data["format"] == "vibe-research-user-data"
    assert data["data"]["watchlist"] == ["600519"]
    assert data["data"]["llm"]["apiKey"] == ""  # key 不出本机
    assert "password" not in str(data) and "token" not in str(data)
    assert data["user"] == {"username": "backup-user"}
    assert data["ledgers"]["portfolio"]["holdings"][0]["code"] == "600519"
    assert data["ledgers"]["fund_portfolio"]["holdings"][0]["code"] == "000001"


def test_import_requires_login(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert client.get("/api/auth/export").status_code == 401
    assert client.post("/api/auth/import", json={"payload": {}}).status_code == 401


def test_import_merge_only_fills_missing_keys(tmp_path, monkeypatch):
    headers, uid = _setup(tmp_path, monkeypatch)
    users.set_data(uid, "notes", [{"id": "current"}])
    export = client.get("/api/auth/export", headers=headers).json()["data"]
    users.set_data(uid, "watchlist", ["000001"])  # 导入后新增的本地 key

    r = client.post("/api/auth/import", headers=headers, json={"payload": export, "mode": "merge"})
    assert r.status_code == 200
    data = users.get_data(uid)
    assert data["notes"] == [{"id": "current"}]  # 已有 key 不被备份覆盖
    assert data["watchlist"] == ["000001"]  # 云端新增的也保留


def test_import_replace_restores_backup_state(tmp_path, monkeypatch):
    headers, uid = _setup(tmp_path, monkeypatch)
    users.set_data(uid, "notes", [{"id": "backup"}])
    export = client.get("/api/auth/export", headers=headers).json()["data"]
    users.set_data(uid, "notes", [{"id": "newer"}])
    users.set_data(uid, "watchlist", ["000001"])

    r = client.post("/api/auth/import", headers=headers, json={"payload": export, "mode": "replace"})
    assert r.status_code == 200
    data = users.get_data(uid)
    assert data["notes"] == [{"id": "backup"}]
    assert "watchlist" not in data  # 备份里没有的 key 被清掉（以备份为准）


def test_import_ledgers_skip_by_default(tmp_path, monkeypatch):
    headers, uid = _setup(tmp_path, monkeypatch)
    pf.add_holding("600519", 100, 10, user_id=uid)
    export = client.get("/api/auth/export", headers=headers).json()["data"]
    pf.remove_holding("600519", user_id=uid)

    r = client.post("/api/auth/import", headers=headers, json={"payload": export})
    assert r.status_code == 200
    assert pf._load(uid)["holdings"] == []  # 默认不动账本


def test_import_ledgers_merge_and_replace(tmp_path, monkeypatch):
    headers, uid = _setup(tmp_path, monkeypatch)
    export = {
        "format": "vibe-research-user-data", "version": 1, "data": {},
        "ledgers": {
            "portfolio": {"holdings": [{"code": "600519", "shares": 100, "cost": 10}]},
            "fund_portfolio": {"holdings": [{"code": "000001", "shares": 200, "cost": 1.5}]},
        },
    }
    # merge：已有 000001，只补 600519
    pf.add_holding("000001", 50, 9, user_id=uid)
    r = client.post("/api/auth/import", headers=headers, json={"payload": export, "ledgers_mode": "merge"})
    assert r.status_code == 200
    codes = sorted(h["code"] for h in pf._load(uid)["holdings"])
    assert codes == ["000001", "600519"]

    # replace：以备份为准，只剩 600519；基金账本也被写入
    r = client.post("/api/auth/import", headers=headers, json={"payload": export, "ledgers_mode": "replace"})
    assert r.status_code == 200
    assert [h["code"] for h in pf._load(uid)["holdings"]] == ["600519"]
    assert [h["code"] for h in fpf._load(uid)["holdings"]] == ["000001"]


def test_import_rejects_bad_format_and_bad_ledger(tmp_path, monkeypatch):
    headers, _ = _setup(tmp_path, monkeypatch)
    assert client.post("/api/auth/import", headers=headers, json={"payload": {"format": "other"}}).status_code == 400
    # version 字段损坏（非数字）→ 400 而不是 500
    assert client.post("/api/auth/import", headers=headers,
                       json={"payload": {"format": "vibe-research-user-data", "version": "abc", "data": {}}}).status_code == 400
    bad_ledger = {
        "format": "vibe-research-user-data", "version": 1, "data": {},
        "ledgers": {"portfolio": {"holdings": [{"code": "6005", "shares": 1, "cost": 1}]}},
    }
    r = client.post("/api/auth/import", headers=headers, json={"payload": bad_ledger, "ledgers_mode": "replace"})
    assert r.status_code == 400
    # 负股数 → 400
    neg_ledger = {
        "format": "vibe-research-user-data", "version": 1, "data": {},
        "ledgers": {"portfolio": {"holdings": [{"code": "600519", "shares": -1, "cost": 1}]}},
    }
    assert client.post("/api/auth/import", headers=headers, json={"payload": neg_ledger, "ledgers_mode": "replace"}).status_code == 400


def test_import_strips_derived_ledger_metadata(tmp_path, monkeypatch):
    """备份里的 ytd_open/ytd_year/last_refresh 是导出当时的派生值，导入时必须剥掉，
    否则本年盈亏会拿别处的年初基准重放。"""
    headers, uid = _setup(tmp_path, monkeypatch)
    payload = {
        "format": "vibe-research-user-data", "version": 1, "data": {},
        "ledgers": {"portfolio": {"holdings": [{"code": "600519", "shares": 100, "cost": 10}],
                                  "ytd_year": 2024, "ytd_open": {"600519": 999.0},
                                  "last_refresh": "2024-01-01 00:00", "version": 42}},
    }
    r = client.post("/api/auth/import", headers=headers, json={"payload": payload, "ledgers_mode": "replace"})
    assert r.status_code == 200
    saved = pf._load(uid)
    assert saved["holdings"][0]["code"] == "600519"
    assert "ytd_open" not in saved and "ytd_year" not in saved and "last_refresh" not in saved


def test_users_import_data_skips_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()
    user = users.register("unit-user", "secret1")
    payload = {"format": "vibe-research-user-data", "version": 1,
               "data": {"notes": [1], "hacker-key": {"evil": True}}}
    result = users.import_data(user["id"], payload, merge=True)
    assert result["applied"] == ["notes"]
    assert result["skipped_keys"] == ["hacker-key"]
    assert "hacker-key" not in users.get_data(user["id"])
