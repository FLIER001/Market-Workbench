"""Run score builds in short-lived processes, across all refresh paths.

One worker at a time keeps startup, request refreshes and the clock scheduler
from allocating several market panels concurrently. The caller retains its
last-good value when a worker fails; only a successful worker's disk snapshot
is loaded back into the serving process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Callable, TypeVar

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCK = threading.Lock()
_T = TypeVar("_T")


def rebuild(module: str, load: Callable[[], _T], *, entry: str = "rebuild_snapshot()") -> _T:
    with _LOCK:
        subprocess.run(
            [sys.executable, "-c", f"import {module}; {module}.{entry}"],
            cwd=_BACKEND_DIR,
            timeout=1800,
            check=True,
        )
        return load()
