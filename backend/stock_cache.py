"""Persistent last-good cache for the 20 most recently queried securities."""

from __future__ import annotations

import json
import os
import threading
import time

DATA_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
CACHE_FILE = os.path.join(DATA_DIR, "stock_query_cache.json")
MAX_STOCKS = 20
_LOCK = threading.Lock()


def _read() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(value: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    temp = CACHE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
    os.replace(temp, CACHE_FILE)


def warm(endpoint: str, code: str):
    with _LOCK:
        item = (_read().get("entries") or {}).get(f"{endpoint}:{code}")
    if not isinstance(item, dict) or "value" not in item:
        return None
    return float(item.get("cached_at") or time.time()), item["value"]


def save(endpoint: str, code: str, value) -> None:
    now = time.time()
    with _LOCK:
        payload = _read()
        entries = payload.setdefault("entries", {})
        stocks = payload.setdefault("stocks", {})
        stocks[code] = now
        entries[f"{endpoint}:{code}"] = {"cached_at": now, "value": value}
        keep = set(sorted(stocks, key=stocks.get, reverse=True)[:MAX_STOCKS])
        payload["stocks"] = {stock: stocks[stock] for stock in keep}
        payload["entries"] = {
            key: item for key, item in entries.items()
            if key.rsplit(":", 1)[-1] in keep
        }
        payload["schema_version"] = 1
        _write(payload)
