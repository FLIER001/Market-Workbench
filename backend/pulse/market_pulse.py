"""Multi-source probability overview — the Market Workbench pulse panel's backend.

Merges Polymarket + Kalshi into one module-grouped view so the dashboard can show
the whole market's probability state at a glance (each module = 货币政策 / 宏观经济 /
地缘政治 / 政治选举 / 股指大宗 / AI科技, plus a collapsed reference group for
crypto / sports / pop-culture).

Both sources are public, read-only, no auth. Classification is centralised in
``market_taxonomy`` so the two feeds land in the same buckets. Chinese titles reuse
the existing translation cache. A pinned snapshot keeps normal loads instant; the
refresh button (``force=True``) re-pulls both sources and re-pins.

Ported from https://github.com/simonlin1212/globalpercent (Apache-2.0).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from . import kalshi_signals
from . import market_taxonomy
from . import polymarket_signals

logger = logging.getLogger(__name__)

def _snapshot_path() -> Path:
    base = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
    return Path(base) / "pulse" / "pulse_snapshot.json"


def _load_snapshot() -> dict[str, Any] | None:
    try:
        data = json.loads(_snapshot_path().read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _save_snapshot(overview: dict[str, Any]) -> None:
    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(overview, ensure_ascii=False), "utf-8")
        temp.replace(path)
    except OSError as exc:
        logger.warning("pulse snapshot save failed: %s", exc)


async def _shaped_polymarket(force: bool) -> list[dict[str, Any]]:
    """Polymarket raw markets shaped + classified by the shared taxonomy.

    Uses ``pull_raw_markets`` (not ``fetch_markets``) so classification/caps are the
    aggregator's job and nothing is pre-dropped — this keeps international elections
    etc. that the legacy panel's narrower classifier discarded.
    """
    raw = await polymarket_signals.pull_raw_markets(pages=3, force=force)
    shaped: list[dict[str, Any]] = []
    for market in raw:
        question = market.get("question") or ""
        module = market_taxonomy.classify(question)
        row = polymarket_signals._shape(market, module)
        row["source"] = "polymarket"
        shaped.append(row)
    return shaped


def _group_by_module(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket markets into ordered modules, cap floods, summarise each module."""
    buckets: dict[str, list[dict[str, Any]]] = {m: [] for m in market_taxonomy.MODULES}
    for market in markets:
        module = market.get("topic")
        buckets.setdefault(module, []).append(market)

    modules: list[dict[str, Any]] = []
    for key in market_taxonomy.MODULES:
        group = buckets.get(key, [])
        group.sort(key=lambda m: m.get("volume_24h") or 0.0, reverse=True)
        cap = market_taxonomy.MODULE_CAPS.get(key)
        if cap is not None:
            group = group[:cap]
        if not group:
            continue
        source_counts: dict[str, int] = {}
        for market in group:
            src = market.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        modules.append({
            "key": key,
            "core": key in market_taxonomy.CORE_SET,
            "market_count": len(group),
            "volume_24h": sum(m.get("volume_24h") or 0.0 for m in group),
            "source_counts": source_counts,
            "markets": group,
        })
    return modules


async def _translate(markets: list[dict[str, Any]]) -> None:
    """Attach Chinese titles in-place (best-effort, bounded, self-healing cache).

    Uses the optional LLM bridge in ``pulse_translate`` (configured via
    ``VR_PULSE_LLM_BASE_URL`` / ``VR_PULSE_LLM_API_KEY`` / ``VR_PULSE_LLM_MODEL``).
    When unconfigured, titles stay English-only — no failure, no noise.
    """
    try:
        from .pulse_translate import translate_questions

        questions = [m["question"] for m in markets if m.get("question")]
        # Refresh is async/background now, so we can afford to translate *everything*
        # before pinning — the panel keeps serving the old snapshot meanwhile. The cache
        # warms permanently, so only brand-new titles cost time on later rebuilds.
        zh_map = await translate_questions(questions, llm_timeout=300.0)
        for market in markets:
            market["question_zh"] = zh_map.get(market.get("question"))
    except Exception:  # noqa: BLE001 — degrade to English
        pass


_rebuilding = False  # guards against piling up concurrent background rebuilds
_refresh_error: str | None = None
_last_attempt_at: str | None = None


def _snapshot_cached_at() -> str | None:
    try:
        return datetime.fromtimestamp(_snapshot_path().stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


def _with_cache_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose refresh state without polluting the persisted last-good snapshot."""
    state = "refreshing" if _rebuilding else "error" if _refresh_error else "fresh"
    return {
        **snapshot,
        "cache_state": state,
        "cached_at": snapshot.get("cached_at") or _snapshot_cached_at(),
        "data_as_of": snapshot.get("as_of"),
        "refresh_error": _refresh_error,
        "refresh_attempted_at": _last_attempt_at,
        "updating": _rebuilding,
    }


async def _build() -> dict[str, Any]:
    """Pull both sources, classify, translate, group, and pin. The slow path
    (Kalshi's full event book can take 1-8 min when the API is loaded)."""
    pm, ks = await asyncio.gather(
        _shaped_polymarket(force=True),
        kalshi_signals.fetch_shaped(force=True),
    )
    # An empty upstream response is not a truthful new market snapshot.  Keep the
    # previous disk snapshot instead of replacing it with a transient outage.
    if not pm or not ks:
        missing = ", ".join(name for name, rows in (("Polymarket", pm), ("Kalshi", ks)) if not rows)
        raise ValueError(f"empty source response: {missing}")
    merged = pm + ks
    await _translate(merged)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    overview = {
        "as_of": now,
        "cached_at": now,
        "sources": ["polymarket", "kalshi"],
        "module_order": market_taxonomy.MODULES,
        "core_modules": market_taxonomy.CORE_MODULES,
        "modules": _group_by_module(merged),
    }
    _save_snapshot(overview)
    return overview


async def _background_rebuild() -> None:
    global _rebuilding, _refresh_error
    try:
        await _build()
        _refresh_error = None
    except Exception as exc:  # noqa: BLE001 — best-effort; snapshot keeps serving
        _refresh_error = str(exc)
        logger.warning("pulse background rebuild failed: %s", exc)
    finally:
        _rebuilding = False


async def fetch_overview(force: bool = False) -> dict[str, Any]:
    """The merged, module-grouped probability overview for the panel.

    Normal loads serve the pinned snapshot instantly. ``force=True`` (refresh button)
    kicks off a background rebuild and returns the current snapshot immediately with
    ``updating: True`` — Kalshi's full-book pull is too slow to block on, and the panel
    is a slow-moving gauge, so the frontend polls until ``as_of`` advances. Cold start
    with no snapshot builds synchronously (nothing else to show).
    """
    global _rebuilding, _refresh_error, _last_attempt_at
    snap = _load_snapshot()

    if snap is None:
        built = await _build()
        _refresh_error = None
        return _with_cache_state(built)

    if force and not _rebuilding:
        _last_attempt_at = datetime.now().astimezone().isoformat(timespec="seconds")
        _rebuilding = True
        asyncio.create_task(_background_rebuild())

    return _with_cache_state(snap)
