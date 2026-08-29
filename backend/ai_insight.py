"""Shared cache/persistence rules for per-module AI insight payloads.

gold/oil/bonds/allocation each embed an LLM-written ``ai_insight`` field in
their score snapshot.  The rules used to be copy-pasted into every module;
they are identical everywhere:

- A stored insight is returned as-is while its ``ai_insight_at`` stamp is not
  older than the dataset's own timestamp (``stamp_field``); a rebuilt dataset
  makes the insight stale, so the next read regenerates it once.
- ``force`` skips the freshness check and always rebuilds.
- An LLM failure keeps the old text (memory first, since ``build`` warms the
  snapshot into the cache) so page copy never disappears.
- A successful rebuild is written back via ``cache_runtime.update_value`` and
  persisted, but only when the snapshot is usable (``writable``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import cache_runtime

BEIJING = timezone(timedelta(hours=8))


def now_stamp() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")


async def cached_insight(
    *,
    cache_key: str,
    build: Callable[[], Awaitable[Any]],
    valid: Callable[[Any], bool],
    writable: Callable[[dict], bool],
    save: Callable[[dict], None],
    stamp_field: str = "updated",
    force: bool = False,
) -> Any:
    """Return the cached insight, rebuilding once when stale/missing."""
    snap = cache_runtime.peek(cache_key) or {}
    old = snap.get("ai_insight") if valid(snap.get("ai_insight")) else ""
    if old and not force:
        updated = str(snap.get(stamp_field) or "")
        at = str(snap.get("ai_insight_at") or "")
        if not updated or not at or at >= updated:
            return old
    fresh = await build()
    if not fresh:
        # Cold cache: the peek above saw nothing, but build() warms the
        # snapshot through cache_runtime — re-read before giving up.
        warmed = cache_runtime.peek(cache_key) or {}
        cand = warmed.get("ai_insight")
        return cand if valid(cand) else old

    saved: dict = {}

    def _merge(current: Any) -> dict:
        if isinstance(current, dict) and writable(current):
            current["ai_insight"] = fresh
            current["ai_insight_at"] = now_stamp()
            saved["snap"] = current
        return current

    cache_runtime.update_value(cache_key, _merge)
    if saved:
        save(saved["snap"])  # disk I/O outside the cache lock
    return fresh
