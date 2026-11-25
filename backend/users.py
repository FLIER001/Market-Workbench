"""用户体系 —— 用户名/密码注册登录 + 每用户独立数据（SQLite）。

设计目标：把原来散在浏览器 localStorage 的自选/备注/笔记/LLM 配置收到服务端，
按用户隔离持久化。这样换浏览器、换域名(localhost vs 127.0.0.1)、换电脑都不丢。

- 密码：PBKDF2-HMAC-SHA256（标准库 hashlib，20 万次迭代 + 随机盐），不存明文。
- 会话：登录发随机 token（secrets.token_urlsafe），**库里只存其 SHA-256**，带过期时间。
  库文件泄露时拿不到可直接使用的 token；过期会话在写入路径按时间片清理（不再每请求删一次）。
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

import paths

DB_FILE = paths.USERS_DB
try:
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
except OSError as exc:
    raise RuntimeError("无法创建账号数据目录") from exc

_PBKDF2_ROUNDS = 200_000
_SESSION_TTL = 60 * 60 * 24 * 30  # 30 天
# 用户不存在时用它算一次等价耗时的哈希：否则「用户不存在」比「密码错误」快一个
# 数量级，响应时间即可枚举用户名。
_DUMMY_SALT = "00" * 16
# 云身份影子账号：用户名前缀与密码哈希标记（不可能匹配 PBKDF2 输出，故无法走密码登录）
_CLOUD_PREFIX = "wb:"
_CLOUD_ONLY_HASH = "!cloud-only"
# 过期会话清理的最小间隔（秒）
_CLEANUP_INTERVAL = 600
_last_cleanup = 0.0

# 单个用户数据 key 的体积上限（字节）。导出备份里最大的是深度分析结果，2MB 足够。
MAX_VALUE_BYTES = 2 * 1024 * 1024


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=5)
    conn.row_factory = sqlite3.Row
    # WAL：读写不互斥，并发请求下不再因「正在读」而拿不到写锁
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cleanup_sessions(c: sqlite3.Connection, now: float, force: bool = False) -> None:
    """过期会话按时间片清理。请求路径不再写库（此前每个鉴权请求都执行一次 DELETE），
    清理挪到建会话与启动时；间隔内重复调用直接跳过。"""
    global _last_cleanup
    if not force and now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))


# 允许按用户隔离存储的数据 key（前端白名单，防止任意写）
ALLOWED_KEYS = {
    "watchlist", "watchlist-groups", "watchlist-notes",
    "etf-watchlist", "etf-watchlist-groups",
    "notes", "llm", "deep-analysis",
    "fund-watchlist",
}


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
        # 影子账号（WorkBuddy 云身份）的 username 是 `wb:<cloud_id>` 这种内部标识，
        # 展示名要单独存一列，否则界面会露出 `wb:6MCV...`。老库就地补列，幂等。
        ucols = {row["name"] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        if "display_name" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        if "cloud_id" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN cloud_id TEXT")
        # 云身份改按独立的 cloud_id 匹配，不再按用户名：用户名可以被任何人抢先注册，
        # 按名查找会把受害者映射到抢注者的账号上。迁移时只认真正的影子账号
        # （密码哈希为 !cloud-only），抢注出来的同名账号不获得 cloud_id。
        c.execute("UPDATE users SET cloud_id = substr(username, 4) "
                  "WHERE cloud_id IS NULL AND username LIKE 'wb:%' AND pw_hash = ?",
                  (_CLOUD_ONLY_HASH,))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_cloud_id "
                  "ON users(cloud_id) WHERE cloud_id IS NOT NULL")
        # 会话令牌由明文改为只存哈希：token_urlsafe(32) 是 43 字符，SHA-256 十六进制是 64 字符，
        # 长度即可区分，老库就地迁移、幂等。
        rows = c.execute("SELECT token FROM sessions WHERE length(token) != 64").fetchall()
        for row in rows:
            c.execute("UPDATE sessions SET token = ? WHERE token = ?",
                      (_token_hash(row["token"]), row["token"]))
        _cleanup_sessions(c, time.time(), force=True)
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


def _label(display_name: str | None, username: str) -> str:
    """对外展示名：昵称优先；影子账号没昵称时给可读占位，不把内部 id 甩给用户。"""
    name = (display_name or "").strip()
    if name:
        return name
    return "WorkBuddy 用户" if username.startswith("wb:") else username


def register(username: str, password: str) -> dict:
    username = (username or "").strip()
    if not (2 <= len(username) <= 32):
        raise ValueError("用户名需 2-32 个字符")
    if username.startswith(_CLOUD_PREFIX):
        # wb: 前缀留给云身份影子账号；允许注册会让后来云登录的用户被映射到这个账号上
        raise ValueError("用户名不允许以 wb: 开头")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    salt = secrets.token_hex(16)
    pw_hash = _hash_pw(password, salt)
    now = time.time()
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users (username, pw_hash, salt, created_at) VALUES (?,?,?,?)",
                (username, pw_hash, salt, now),
            )
            uid = cur.lastrowid
    except sqlite3.IntegrityError as exc:
        raise ValueError("用户名已被占用") from exc
    return {"id": uid, "username": username}


def _verify(username: str, password: str) -> sqlite3.Row | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row:
        _hash_pw(password, _DUMMY_SALT)  # 与真实校验等耗，避免用响应时间枚举用户名
        return None
    if hmac.compare_digest(_hash_pw(password, row["salt"]), row["pw_hash"]):
        return row
    return None


def _issue(user_id: int) -> str:
    """建会话：返回明文 token（只发给客户端一次），库里存其哈希。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _conn() as c:
        _cleanup_sessions(c, now)
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_token_hash(token), user_id, now, now + _SESSION_TTL),
        )
    return token


def login(username: str, password: str) -> dict:
    row = _verify(username, password)
    if not row:
        raise ValueError("用户名或密码错误")
    return {"token": _issue(row["id"]),
            "username": _label(row["display_name"], row["username"]), "user_id": row["id"]}


def user_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def ensure_cloud_user(external_id: str, display_name: str = "") -> dict:
    """云身份 → 本地影子账号。

    云登录（WorkBuddy 账号 / 邮箱验证码）由云服务认证，本地只保存映射与业务数据。
    影子账号的密码哈希写死为不可能匹配 PBKDF2 输出的标记，因此它无法走密码登录。
    只按 cloud_id 匹配；用户名 `wb:<id>` 仅作展示用途，被普通账号占用时换一个。
    """
    display = (display_name or "").strip()
    now = time.time()
    with _conn() as c:
        row = c.execute("SELECT id, username, display_name FROM users WHERE cloud_id=?", (external_id,)).fetchone()
        if row:
            # 云端昵称会变（改名、换绑账号），每次登录同步一次；取不到就保留旧值。
            if display and display != (row["display_name"] or ""):
                c.execute("UPDATE users SET display_name=? WHERE id=?", (display, row["id"]))
            elif not display:
                display = row["display_name"] or ""
            return {"id": int(row["id"]), "username": _label(display, row["username"])}
        salt = secrets.token_hex(16)
        try:
            cur = c.execute(
                "INSERT INTO users (username, pw_hash, salt, created_at, display_name, cloud_id) VALUES (?,?,?,?,?,?)",
                (f"{_CLOUD_PREFIX}{external_id}", _CLOUD_ONLY_HASH, salt, now, display or None, external_id),
            )
        except sqlite3.IntegrityError:
            # 修复前允许注册 wb: 前缀，老库里可能已有同名普通账号：不接管它，换个内部名
            cur = c.execute(
                "INSERT INTO users (username, pw_hash, salt, created_at, display_name, cloud_id) VALUES (?,?,?,?,?,?)",
                (f"{_CLOUD_PREFIX}{external_id}#{secrets.token_hex(3)}", _CLOUD_ONLY_HASH, salt, now,
                 display or None, external_id),
            )
        return {"id": int(cur.lastrowid), "username": _label(display, f"{_CLOUD_PREFIX}{external_id}")}


def issue_session(user_id: int) -> str:
    return _issue(int(user_id))


def user_ids() -> list[int]:
    with _conn() as c:
        return [int(row["id"]) for row in c.execute("SELECT id FROM users ORDER BY id").fetchall()]


def resolve_token(token: str) -> dict | None:
    """token → {id, username}；过期/不存在返回 None。只读，不写库。"""
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            """SELECT s.user_id AS uid, u.username AS uname, u.display_name AS dname
                 FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND s.expires_at >= ?""",
            (_token_hash(token), time.time()),
        ).fetchone()
    if not row:
        return None
    return {"id": row["uid"], "username": _label(row["dname"], row["uname"])}


def logout(token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (_token_hash(token),))


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
    if len(payload.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError(f"数据过大：单个 key 上限 {MAX_VALUE_BYTES // 1024 // 1024}MB")
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


EXPORT_FORMAT = "market-workbench-user-data"
EXPORT_VERSION = 1


def export_data(user_id: int, username: str) -> dict:
    """导出该账号全部 user_data 为可下载 JSON（自选/备注/研究记录/AI 配置等）。

    不含密码哈希、会话 token、持仓账本（账本走各自端点的独立导出）。"""
    now = time.time()
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": now,
        "exported_at_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
        "user": {"id": user_id, "username": username},
        "data": get_data(user_id),
    }


def import_data(user_id: int, payload: dict, merge: bool = True) -> dict:
    """导入 export_data 产物。merge=True 只补云端没有的 key（默认，安全）；
    merge=False 整体替换——先清掉白名单 key 再写入，用于「以备份为准」恢复。

    校验失败抛 ValueError（端点转 400）；密码/会话永不在导出文件里，无可导入项。"""
    if not isinstance(payload, dict):
        raise ValueError("导入内容必须是 JSON 对象")
    if payload.get("format") != EXPORT_FORMAT:
        raise ValueError("不是本产品的用户数据导出文件（format 不匹配）")
    try:
        file_version = int(payload.get("version") or 0)
    except (TypeError, ValueError):
        raise ValueError("文件版本号损坏") from None
    if file_version > EXPORT_VERSION:
        raise ValueError(f"文件版本 v{file_version} 高于当前支持的 v{EXPORT_VERSION}，请先升级程序")
    data = payload.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("文件内 data 字段损坏")
    incoming = {k: v for k, v in data.items() if k in ALLOWED_KEYS}
    skipped = sorted(set(data) - set(incoming))
    if merge:
        existing = get_data(user_id)
        applied = {k: v for k, v in incoming.items() if k not in existing}
    else:
        with _conn() as c:
            for key in ALLOWED_KEYS:
                c.execute("DELETE FROM user_data WHERE user_id=? AND key=?", (user_id, key))
        applied = incoming
    for k, v in applied.items():
        set_data(user_id, k, v)
    return {"ok": True, "applied": sorted(applied), "skipped_keys": skipped}


init_db()
