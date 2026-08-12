"""Optional Chinese title translation for the pulse panel.

The upstream reference uses the host app's LLM client. Market Workbench keeps the
LLM config client-side, so this module is an *optional* bridge configured purely
by environment variables — no config, no LLM call, panel stays English-only:

    VR_PULSE_LLM_BASE_URL=https://api.openai.com/v1
    VR_PULSE_LLM_API_KEY=sk-...
    VR_PULSE_LLM_MODEL=gpt-4o-mini

Translations are cached on disk keyed by the stable English title, so only brand
new titles ever cost an LLM call. Everything is best-effort: any config / network
/ parse error degrades to English-only and self-heals on later rebuilds.

Pattern (batches + inter-batch delay + multi-round sweep) ported from
https://github.com/simonlin1212/globalpercent (Apache-2.0).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("VR_PULSE_LLM_BASE_URL", "").strip()
_API_KEY = os.environ.get("VR_PULSE_LLM_API_KEY", "").strip()
_MODEL = os.environ.get("VR_PULSE_LLM_MODEL", "").strip()
_ENABLED = bool(_BASE_URL and _API_KEY and _MODEL)

_BATCH = 4
_BATCH_DELAY = 0.8
_CACHE: dict[str, str] | None = None


def _cache_path() -> Path:
    base = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
    return Path(base) / "pulse" / "pulse_translations.json"


def _load_cache() -> dict[str, str]:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_cache_path().read_text("utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            _CACHE = {}
    return _CACHE


def _save_cache() -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_CACHE, ensure_ascii=False), "utf-8")
    except OSError as exc:
        logger.warning("pulse translation cache save failed: %s", exc)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


async def _translate_batch(questions: list[str]) -> dict[str, str]:
    numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    prompt = (
        "把下面的预测市场标题逐条翻译成简洁自然的中文。"
        "保留人名、地名、机构、缩写（如 Fed、GDP、Nvidia、OpenAI）原样。"
        '只返回一个 JSON 对象，key 为序号字符串，value 为中文译文，不要任何解释或代码块标记。\n\n'
        f"{numbered}"
    )
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        parsed = _extract_json(text)
    except Exception as exc:  # noqa: BLE001 — best-effort translation
        logger.warning("pulse translation batch failed: %s", exc)
        return {}

    out: dict[str, str] = {}
    for i, question in enumerate(questions):
        value = parsed.get(str(i + 1))
        if isinstance(value, str) and value.strip():
            out[question] = value.strip()
    return out


async def _translate_missing(missing: list[str], cache: dict[str, str], max_rounds: int = 6) -> None:
    """Sweep uncached titles in small batches, saving after each batch. A short
    pause between batches avoids provider rate limits; the multi-round loop only
    stops after two consecutive no-progress rounds (one transient rate-limited
    round must not abort the whole sweep)."""
    stalls = 0
    for _ in range(max_rounds):
        remaining = [q for q in missing if q not in cache]
        if not remaining:
            return
        progressed = False
        for i in range(0, len(remaining), _BATCH):
            before = len(cache)
            cache.update(await _translate_batch(remaining[i : i + _BATCH]))
            _save_cache()
            if len(cache) > before:
                progressed = True
            await asyncio.sleep(_BATCH_DELAY)
        stalls = 0 if progressed else stalls + 1
        if stalls >= 2:
            return


async def translate_questions(questions: list[str], llm_timeout: float = 300.0) -> dict[str, str]:
    """Return ``{en_question: zh}``. Cache hits resolve instantly; only uncached
    titles hit the LLM and only that work is bounded by ``llm_timeout``. With no
    LLM configured this returns the cached subset (usually empty) immediately."""
    cache = _load_cache()
    unique = list(dict.fromkeys(q for q in questions if q))
    if not _ENABLED:
        return {q: cache[q] for q in unique if q in cache}
    missing = [q for q in unique if q not in cache]
    if missing:
        try:
            await asyncio.wait_for(_translate_missing(missing, cache), timeout=llm_timeout)
        except Exception:  # noqa: BLE001 — best-effort (incl. asyncio.TimeoutError)
            pass  # finished batches are already in `cache`; hits returned below
    return {q: cache[q] for q in unique if q in cache}
