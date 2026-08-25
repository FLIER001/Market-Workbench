"""后台用户管理 CLI —— 注册关闭后，账号从这里添加/删除/改密。

用法（在 backend 目录、用跑服务的同一套 Python）：
    python add_user.py add <用户名> [密码]     # 密码缺省则交互式输入（不回显）
    python add_user.py list                    # 列出所有账号（只读）
    python add_user.py passwd <用户名> [新密码] # 重置密码（吊销该用户全部会话）
    python add_user.py remove <用户名>          # 删账号 + 其 user_data / 持仓账本

    # 密码走参数会进 shell 历史，私密环境用交互输入更稳妥：
    python add_user.py add alice

直接操作 users.db（PBKDF2 哈希 + 盐），不经过 HTTP、不需要后端在跑。
持仓账本文件 portfolio.user-<id>.json / fund_portfolio.user-<id>.json 随
remove 一并清理；sessions 里该用户的 token 全部失效（改密/删号都吊销）。
"""

from __future__ import annotations

import getpass
import os
import sqlite3
import sys

import users


def _ask_password(prompt: str) -> str:
    pw = getpass.getpass(prompt)
    if len(pw) < 6:
        raise SystemExit("密码至少 6 位")
    return pw


def cmd_add(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("用法：add <用户名> [密码]")
    username = argv[0]
    password = argv[1] if len(argv) > 1 else _ask_password("密码（≥6 位）: ")
    user = users.register(username, password)
    print(f"已添加：{user['username']}（id={user['id']}）")


def cmd_list(_argv: list[str]) -> None:
    with users._conn() as c:
        rows = c.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()
    if not rows:
        print("（空）尚无账号")
        return
    for r in rows:
        print(f"  {r['id']:>4}  {r['username']}")


def cmd_passwd(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("用法：passwd <用户名> [新密码]")
    username = argv[0].strip()
    new_pw = argv[1] if len(argv) > 1 else _ask_password("新密码（≥6 位）: ")
    if len(new_pw) < 6:
        raise SystemExit("密码至少 6 位")
    salt = users.secrets.token_hex(16)
    pw_hash = users._hash_pw(new_pw, salt)
    with users._conn() as c:
        cur = c.execute(
            "UPDATE users SET pw_hash=?, salt=? WHERE username=?",
            (pw_hash, salt, username),
        )
        if cur.rowcount == 0:
            raise SystemExit(f"账号不存在：{username}")
        c.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username=?)", (username,))
    print(f"已重置密码并吊销全部会话：{username}")


def cmd_remove(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("用法：remove <用户名>")
    username = argv[0].strip()
    with users._conn() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise SystemExit(f"账号不存在：{username}")
        uid = int(row["id"])
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM user_data WHERE user_id=?", (uid,))
        c.execute("DELETE FROM user_data_backups WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    for name in (f"portfolio.user-{uid}.json", f"fund_portfolio.user-{uid}.json"):
        path = os.path.join(users.CACHE_DIR, name)
        if os.path.exists(path):
            os.remove(path)
    print(f"已删除账号及其全部数据：{username}（id={uid}）")


_COMMANDS = {"add": cmd_add, "list": cmd_list, "passwd": cmd_passwd, "remove": cmd_remove}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help") or sys.argv[1] not in _COMMANDS:
        raise SystemExit(__doc__)
    try:
        _COMMANDS[sys.argv[1]](sys.argv[2:])
    except ValueError as e:  # users.register 的校验错误（重名/长度）
        raise SystemExit(f"失败：{e}")
    except sqlite3.Error as e:
        raise SystemExit(f"数据库错误：{e}")


if __name__ == "__main__":
    main()
