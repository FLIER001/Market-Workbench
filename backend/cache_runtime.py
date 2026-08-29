"""Small stale-while-revalidate cache shared by expensive backend datasets.

The cache never discards a valid last-good value because of age.  Age only
decides whether one background refresh should be started.  Callers keep
ownership of persistence and validation so this module stays deliberately
boring and dependency-free.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

BEIJING = timezone(timedelta(hours=8))
_BACKOFF = (60, 300, 900, 1800)
# Cold builds hit slow upstreams (akshare etc.); a waiter must not hang forever
# if the owner wedges.  Past the deadline the caller gets the error decoration.
_COLD_WAIT_MAX = 120.0


@dataclass
class _Entry:
    value: Any = None
    cached_at: float = 0.0
    refreshing: bool = False
    error: str | None = None
    failures: int = 0
    retry_at: float = 0.0


_entries: dict[str, _Entry] = {}
_lock = threading.RLock()


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, BEIJING).isoformat(timespec="seconds")


def _as_of(value: dict) -> str | None:
    for key in ("data_as_of", "as_of", "date", "quote_time", "updated", "generated_at"):
        if value.get(key):
            return str(value[key])
    return None


def _decorate(value: Any, entry: _Entry, state: str | None = None) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    resolved = state or ("refreshing" if entry.refreshing else "error" if entry.error else "fresh")
    out["cache_state"] = resolved
    out["cached_at"] = _fmt(entry.cached_at) if entry.cached_at else None
    out["data_as_of"] = _as_of(value)
    out["refresh_error"] = entry.error
    return out


def _time_from_value(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("cached_at") or value.get("generated_at") or value.get("updated")
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.timestamp()


def peek(key: str) -> Any:
    """Return the undecorated last-good value for partial-build merging."""
    with _lock:
        return _entries.get(key, _Entry()).value


def update_value(key: str, mutator: Callable[[Any], Any]) -> Any:
    """Mutate the last-good value in place under the lock and return it.

    The official way to patch a cached payload (e.g. merging an AI insight
    into the stored dict) without relying on ``peek`` handing out the live
    object.  Returns ``None`` when the key has no value yet; the mutator's
    non-None return value replaces the stored one.
    """
    with _lock:
        entry = _entries.get(key)
        if entry is None or entry.value is None:
            return None
        result = mutator(entry.value)
        if result is not None:
            entry.value = result
        return entry.value


def seed(key: str, value: Any, cached_at: float | None = None) -> None:
    if value is None:
        return
    with _lock:
        entry = _entries.setdefault(key, _Entry())
        if entry.value is None:
            entry.value = value
            entry.cached_at = cached_at or time.time()


def invalidate(key: str) -> None:
    """Hard invalidation is for schema/business invalidation, never mere age."""
    with _lock:
        _entries.pop(key, None)


def _refresh(
    key: str,
    build: Callable[[], Any],
    valid: Callable[[Any], bool],
    save: Callable[[Any], None] | None,
) -> None:
    try:
        value = build()
        if not valid(value):
            raise ValueError("refresh returned invalid data")
        if save:
            save(value)
        now = time.time()
        with _lock:
            entry = _entries.setdefault(key, _Entry())
            entry.value = value
            entry.cached_at = now
            entry.error = None
            entry.failures = 0
            entry.retry_at = 0.0
    except Exception as exc:  # noqa: BLE001 - boundary stores failure for UI
        with _lock:
            entry = _entries.setdefault(key, _Entry())
            entry.failures += 1
            entry.error = str(exc)
            entry.retry_at = time.time() + _BACKOFF[min(entry.failures, len(_BACKOFF)) - 1]
    finally:
        with _lock:
            _entries.setdefault(key, _Entry()).refreshing = False


def get(
    key: str,
    build: Callable[[], Any],
    *,
    valid: Callable[[Any], bool] = bool,
    ttl: float,
    warm: Callable[[], Any] | None = None,
    warm_time: Callable[[], float] | None = None,
    save: Callable[[Any], None] | None = None,
    force: bool = False,
    decorate: bool = True,
) -> Any:
    """Return last-good immediately and refresh stale data once in background.

    With no usable value, the first request builds synchronously because there
    is nothing truthful to render.  ``force`` bypasses TTL/backoff but reuses an
    already-running refresh.
    """
    now = time.time()
    with _lock:
        entry = _entries.setdefault(key, _Entry())
        if entry.value is None and warm:
            warmed = warm()
            if warmed is not None:
                if isinstance(warmed, tuple) and len(warmed) == 2:
                    entry.cached_at, entry.value = float(warmed[0]), warmed[1]
                else:
                    entry.value = warmed
                    # 快照里若带自身的数据时点（as_of/updated 等），以它为准并封顶：
                    # 进程重启高频（launchd KeepAlive）时文件 mtime 恒新，但数据可能
                    # 是很久前的——按 mtime 判 fresh 会让页面长期展示旧数据时点。
                    entry.cached_at = warm_time() if warm_time else min(
                        now, _time_from_value(warmed) or now)

        if entry.value is not None:
            stale = force or now - entry.cached_at >= ttl
            can_start = not entry.refreshing and (force or now >= entry.retry_at)
            if stale and can_start:
                entry.refreshing = True
                threading.Thread(
                    target=_refresh, args=(key, build, valid, save), daemon=True,
                    name=f"cache-refresh:{key}",
                ).start()
            state = "refreshing" if entry.refreshing else "error" if entry.error else "stale" if stale else "fresh"
            return _decorate(entry.value, entry, state) if decorate else entry.value

        # Mark the cold build as in-flight so concurrent callers cannot start a
        # second external request.  They have no value to return, so wait below.
        if not entry.refreshing:
            entry.refreshing = True
            cold_owner = True
        else:
            cold_owner = False

    if cold_owner:
        _refresh(key, build, valid, save)
    else:
        # Cold starts are rare; wait for the owner instead of building twice.
        # Bounded so a wedged owner cannot hang this caller forever — falling
        # through returns the error decoration below.
        deadline = time.time() + _COLD_WAIT_MAX
        while time.time() < deadline:
            with _lock:
                if not _entries[key].refreshing:
                    break
            time.sleep(0.02)

    with _lock:
        entry = _entries[key]
        if entry.value is not None:
            return _decorate(entry.value, entry) if decorate else entry.value
        if not decorate:
            return None
        return {
            "cache_state": "error",
            "cached_at": None,
            "data_as_of": None,
            "refresh_error": entry.error or "no usable data",
        }


def reset_for_tests() -> None:
    with _lock:
        _entries.clear()
