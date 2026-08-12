"""用户体系 —— 用户名/密码注册登录 + 每用户独立数据（SQLite）。

设计目标：把原来散在浏览器 localStorage 的自选/备注/笔记/LLM 配置收到服务端，
按用户隔离持久化。这样换浏览器、换域名(localhost vs 127.0.0.1)、换电脑都不丢。

- 密码：PBKDF2-HMAC-SHA256（标准库 hashlib，20 万次迭代 + 随机盐），不存明文。
- 会话：登录发随机 token（secrets.token_urlsafe），存 sessions 表，带过期时间。
- 数据：user_data 表按 (user_id, key) 存 JSON 字符串，前端整存整取。

合规：本机自托管单人/家人使用场景。不联网校验、不上传任何数据。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".vibe-research")
os.makedirs(CACHE_DIR, exist_ok=True)
DB_FILE = os.path.join(CACHE_DIR, "users.db")

_PBKDF2_ROUNDS = 200_000
_SESSION_TTL = 60 * 60 * 24 * 30  # 30 天

# 允许按用户隔离存储的数据 key（前端白名单，防止任意写）
ALLOWED_KEYS = {
    "watchlist", "watchlist-groups", "watchlist-notes",
    "etf-watchlist", "etf-watchlist-groups",
    "notes", "llm", "deep-analysis",
    "fund-watchlist",
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   username TEXT UNIQUE NOT NULL,
                   pw_hash TEXT NOT NULL,
                   salt TEXT NOT NULL,
                   created_at REAL NOT NULL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   token TEXT PRIMARY KEY,
                   user_id INTEGER NOT NULL,
                   created_at REAL NOT NULL,
                   expires_at REAL NOT NULL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS user_data (
                   user_id INTEGER NOT NULL,
                   key TEXT NOT NULL,
                   value TEXT NOT NULL,
                   updated_at REAL NOT NULL,
                   PRIMARY KEY (user_id, key))"""
        )
        columns = {row["name"] for row in c.execute("PRAGMA table_info(user_data)").fetchall()}
        if "version" not in columns:
            c.execute("ALTER TABLE user_data ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        c.execute(
            """CREATE TABLE IF NOT EXISTS user_data_backups (
                   user_id INTEGER NOT NULL,
                   key TEXT NOT NULL,
                   value TEXT NOT NULL,
                   version INTEGER NOT NULL,
                   backed_up_at REAL NOT NULL,
                   PRIMARY KEY (user_id, key))"""
        )


def _hash_pw(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return dk.hex()


def register(username: str, password: str) -> dict:
    username = (username or "").strip()
    if not (2 <= len(username) <= 32):
        raise ValueError("用户名需 2-32 个字符")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    salt = secrets.token_hex(16)
    pw_hash = _hash_pw(password, salt)
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users (username, pw_hash, salt, created_at) VALUES (?,?,?,?)",
                (username, pw_hash, salt, time.time()),
            )
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("用户名已被占用")
    return {"id": uid, "username": username}


def _verify(username: str, password: str) -> sqlite3.Row | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row:
        return None
    if hmac.compare_digest(_hash_pw(password, row["salt"]), row["pw_hash"]):
        return row
    return None


def login(username: str, password: str) -> dict:
    row = _verify(username, password)
    if not row:
        raise ValueError("用户名或密码错误")
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, row["id"], now, now + _SESSION_TTL),
        )
    return {"token": token, "username": row["username"], "user_id": row["id"]}


def user_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def user_ids() -> list[int]:
    with _conn() as c:
        return [int(row["id"]) for row in c.execute("SELECT id FROM users ORDER BY id").fetchall()]


def resolve_token(token: str) -> dict | None:
    """token → {id, username}；过期/不存在返回 None，并顺手清理过期会话。"""
    if not token:
        return None
    now = time.time()
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        row = c.execute(
            """SELECT s.user_id AS uid, u.username AS uname
                 FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND s.expires_at >= ?""",
            (token, now),
        ).fetchone()
    if not row:
        return None
    return {"id": row["uid"], "username": row["uname"]}


def logout(token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def get_data(user_id: int) -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT key, value FROM user_data WHERE user_id=?", (user_id,)
        ).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def set_data(user_id: int, key: str, value) -> dict:
    if key not in ALLOWED_KEYS:
        raise ValueError(f"不允许的数据 key: {key}")
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    now = time.time()
    with _conn() as c:
        previous = c.execute(
            "SELECT value, version FROM user_data WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone()
        version = int(previous["version"] or 1) + 1 if previous else 1
        if previous:
            c.execute(
                """INSERT INTO user_data_backups (user_id, key, value, version, backed_up_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(user_id, key) DO UPDATE SET
                     value=excluded.value, version=excluded.version, backed_up_at=excluded.backed_up_at""",
                (user_id, key, previous["value"], previous["version"], now),
            )
        c.execute(
            """INSERT INTO user_data (user_id, key, value, updated_at, version) VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at, version=excluded.version""",
            (user_id, key, payload, now, version),
        )
    return {"ok": True, "key": key, "version": version, "updated_at": now}


def merge_data(user_id: int, items: dict) -> dict:
    """批量写入（前端首次迁移用）。只接受白名单 key。"""
    for k, v in items.items():
        set_data(user_id, k, v)
    return get_data(user_id)


init_db()
