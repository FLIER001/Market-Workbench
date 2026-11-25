"""路径单一真相：各模块的数据目录、用户库、研报目录都从这里取。

MW_DATA_DIR 是根目录（空串视同未设置，避免 Path("") 落到进程工作目录）；
其余路径均可单独覆盖。此前这个解析表达式在后端重复了 27 处、空串处理互不一致，
`market.py` 与 `users.py` 还各自绕过了 MW_DATA_DIR。
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOT = Path.home() / ".market-workbench"


def _override(name: str) -> str:
    return os.environ.get(name, "").strip()


DATA_DIR = _override("MW_DATA_DIR") or str(_DEFAULT_ROOT)
# 用户库默认落在数据目录内；历史上默认与 ~/.market-workbench/users.db 等价。
USERS_DB = _override("MW_USERS_DB") or os.path.join(DATA_DIR, "users.db")
REPORTS_DIR = _override("MW_REPORTS_DIR") or str(Path(DATA_DIR) / "myreports")


def subdir(*parts: str) -> str:
    """数据目录下的子路径。"""
    return os.path.join(DATA_DIR, *parts)
