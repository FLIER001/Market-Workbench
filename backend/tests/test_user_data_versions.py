import users


def test_user_data_versions_and_one_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()
    user = users.register("tester", "secret1")
    first = users.set_data(user["id"], "notes", {"a": 1})
    second = users.set_data(user["id"], "notes", {"a": 2})
    third = users.set_data(user["id"], "notes", {"a": 3})

    assert (first["version"], second["version"], third["version"]) == (1, 2, 3)
    with users._conn() as conn:
        backup = conn.execute(
            "SELECT value, version FROM user_data_backups WHERE user_id=? AND key=?",
            (user["id"], "notes"),
        ).fetchone()
    assert backup["version"] == 2
    assert '"a": 2' in backup["value"]
