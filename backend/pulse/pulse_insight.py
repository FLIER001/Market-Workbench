"""Per-module LLM insight: summarise the top-probability events and judge impact.

Runs once per rebuild for the core (expanded) modules only. Selection is
deterministic (top events by Yes probability); the LLM only writes the Chinese
event lines and the investment-impact call. The LLM config comes from the
``VR_PULSE_LLM_*`` env vars, or — when unset — from the user's 「接入 AI」
config stored in the users DB (same JSON the Settings page syncs). Either way
unconfigured or failing calls degrade to a plain top-events fallback, never a
failed rebuild.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .pulse_translate import _BASE_URL, _API_KEY, _MODEL, _extract_json

logger = logging.getLogger(__name__)

_CONTEXT_PICKS = 4
_EVENT_LINES = 4


def _configured() -> bool:
    return bool(_BASE_URL and _API_KEY and _MODEL)


def chat_json(prompt: str) -> dict[str, Any]:
    """Public re-export of the shared LLM call (async). Other pages (e.g. 择时
    配置) reuse the same endpoint discovery: env override first, then the
    user's saved 「接入 AI」config."""
    return _chat_json(prompt)


def _user_llm() -> dict[str, str] | None:
    """The first user's saved 「接入 AI」config (API providers only; CLI providers
    don't fit the background-rebuild call model). First user wins — this app
    is a self-hosted, single-household tool."""
    try:
        import users

        with users._conn() as conn:
            row = conn.execute(
                "SELECT value FROM user_data WHERE key='llm' ORDER BY user_id LIMIT 1"
            ).fetchone()
        if not row:
            return None
        cfg = json.loads(row["value"])
        base, key, model = (str(cfg.get(k) or "") for k in ("baseURL", "apiKey", "model"))
        if base and key and model and not str(cfg.get("provider") or "").startswith("cli-"):
            return {"base": base, "key": key, "model": model}
    except Exception:  # noqa: BLE001 — best-effort config discovery
        pass
    return None


def _endpoint() -> tuple[str, dict[str, str], str] | None:
    if _configured():
        return _BASE_URL, _API_KEY, _MODEL
    user = _user_llm()
    if user:
        return user["base"], user["key"], user["model"]
    return None


def pick_top_events(markets: list[dict[str, Any]], limit: int = _CONTEXT_PICKS) -> list[dict[str, Any]]:
    """Highest-probability markets of a module (ties broken by 24h volume)."""
    ranked = sorted(
        (m for m in markets if m.get("prob_yes") is not None),
        key=lambda m: (m["prob_yes"], m.get("volume_24h") or 0.0),
        reverse=True,
    )
    return ranked[:limit]


def fallback_insight(module: str, markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """No-LLM card content: the module's top events with inline probabilities."""
    picks = pick_top_events(markets, _EVENT_LINES)
    if not picks:
        return None
    events = [
        f"{m.get('question_zh') or m['question']}（{m['prob_yes'] * 100:.0f}%）"
        for m in picks
    ]
    return {"module": module, "events": events, "impact": ""}


def _market_anchor() -> str:
    """当前市场现状摘要，作为研判的锚点。国内取宏观指标，海外取利率水位与
    Fed 降息概率（宏观面/资金面口径），不用股指日内涨跌。全部 best-effort：
    取不到就返回空串，prompt 里少了锚点照样能出结论。"""
    lines: list[str] = []
    try:
        from market import get_macro

        macro = get_macro()
        cn = macro.get("cn", {}) if isinstance(macro, dict) else {}
        for key in ("cpi", "ppi", "pmi", "gdp"):
            ind = cn.get(key)
            if isinstance(ind, dict) and ind.get("value") is not None:
                lines.append(
                    f"中国{ind.get('label', key)} {ind['value']}（前值 {ind.get('prev')}，{ind.get('date')}）"
                )
    except Exception:  # noqa: BLE001 — anchor 是增强项
        pass
    try:
        from market import get_liquidity

        liq = get_liquidity()
        us = liq.get("us", {}) if isinstance(liq, dict) else {}
        for key, label in (("effr", "有效联邦基金利率"), ("dgs10", "美债10Y"), ("dgs2", "美债2Y"), ("t10y2y", "10Y-2Y利差")):
            ind = us.get(key)
            if isinstance(ind, dict) and ind.get("value") is not None:
                lines.append(f"{label} {ind['value']}%")
        fed = liq.get("fed_odds")
        if isinstance(fed, dict) and fed.get("event"):
            lines.append(f"利率期货（{fed['event']}）")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(f"- {l}" for l in lines[:10])


def _prompt(module: str, picks: list[dict[str, Any]], anchor: str) -> str:
    lines = "\n".join(
        f"- {m['question']}（Yes 概率 {m['prob_yes'] * 100:.0f}%）"
        for m in picks
    )
    anchor_block = (
        f"\n\n当前市场现状/一致预期（锚点，仅供背景，禁止复述进正文）：\n{anchor}\n" if anchor else "\n"
    )
    return (
        f"你是宏观策略分析师。当前市场现状与一致预期：{anchor_block}\n"
        f"预测市场「{module}」板块概率最高的事件（Polymarket + Kalshi 的边际信息）：\n{lines}\n\n"
        "市场现状属于全局背景，页面顶部另有一张现状卡片专门描述，本卡只写本模块的边际变化。输出：\n"
        f"- impact：2 句以内、120 字以内的中文，是卡片正文。只写「{module}」板块预测市场定价相对一致预期的边际变化"
        "（在为什么定价、和主流预期不同的点），末尾直接给方向结论（如「利好黄金、利空美元」）。"
        "信息密度要高、措辞凝练：像电报一样砍掉修辞和比喻（如「掷硬币水平」「资金正在下注」），"
        "不写「正在为…定价」「说明市场认为」这类冗余框架句，直接说事实和数字。"
        "观点必须鲜明：只说最确定的那一个方向，禁止两面下注式表述（如「若A则X，若B则Y」「偏中性」「视情况而定」），"
        "禁止复述宏观数据现状，不要出现「若按市场预期兑现」之类的前缀\n"
        f"- events：{_EVENT_LINES} 条左右中文短句，每条概括一个事件，形如「事件（概率%）」，覆盖多个不同事件，不要合并成一句\n"
        '只返回 JSON：{"impact": "...", "events": ["...", ...]}，不要解释或代码块标记。'
    )


async def _chat_json(prompt: str) -> dict[str, Any]:
    """One LLM call returning a parsed JSON object (empty dict on any failure)."""
    endpoint = _endpoint()
    if not endpoint:
        return {}
    base_url, api_key, model = endpoint
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # 思考型模型的 reasoning 段共用这个预算；给太小会 reasoning 没写完就被截断，
        # 正文（content）根本轮不到输出（finish_reason=length）
        "max_tokens": 8000,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            # glm 等思考型模型偶发把正文落在 reasoning_content、content 留空 ——
            # 先取 content；为空再在 reasoning 里找「键名相同」的扁平 JSON（推理里
            # 会引用其它对象，按键名过滤避免拿错）
            texts = [message.get("content") or ""]
            reasoning = message.get("reasoning_content") or ""
            if reasoning:
                keys = re.findall(r'"(\w+)":', prompt.rsplit("只返回 JSON", 1)[-1])
                key_pat = "|".join(re.escape(k) for k in keys) if keys else "impact|events|status|insight"
                for match in re.finditer(r"\{[^{}]*\}", reasoning):
                    if re.search(f'"({key_pat})":', match.group(0)):
                        texts.append(match.group(0))
            for text in texts:
                if not text:
                    continue
                try:
                    candidate = _extract_json(text)
                except ValueError:
                    continue
                if candidate:
                    return candidate
    except Exception as exc:  # noqa: BLE001 — best-effort insight
        logger.warning("pulse llm call failed: %s", exc)
    return {}


async def module_insight(module: str, markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One insight card per module; falls back to top events without an LLM."""
    picks = pick_top_events(markets)
    if not picks:
        return None
    base = fallback_insight(module, markets)
    if not base:
        return base
    anchor = _market_anchor()
    parsed = await _chat_json(_prompt(module, picks, anchor))

    events = [
        str(e).strip() for e in parsed.get("events", []) if isinstance(e, str) and e.strip()
    ]
    if events:
        base["events"] = events[:_EVENT_LINES]
    if isinstance(parsed.get("impact"), str) and parsed["impact"].strip():
        base["impact"] = parsed["impact"].strip()
    return base


def _status_fallback(anchor: str) -> str:
    return anchor.replace("- ", "").replace("\n", "；")


async def status_insight() -> str:
    """现状长条卡：国内国际市场现状/一致预期的简评（与模块卡分工——本卡不谈边际）。"""
    anchor = _market_anchor()
    if not anchor or not _endpoint():
        return _status_fallback(anchor) if anchor else ""
    parsed = await _chat_json(
        f"你是宏观策略分析师。当前市场现状/一致预期（数据源，国内宏观指标 + 海外利率与政策预期）：\n{anchor}\n\n"
        "请输出 2 句、120 字以内的中文：一句国内宏观现状与政策预期，一句海外利率/流动性现状与 Fed 政策预期。"
        "信息密度要高、措辞凝练，保留数值。"
        "只描述宏观与资金面现状和一致预期，不引用股指日内涨跌幅，不涉及预测市场边际变化，不给投资建议。"
        '只返回 JSON：{"status": "..."}，不要解释或代码块标记。'
    )
    status = parsed.get("status")
    return status.strip() if isinstance(status, str) and status.strip() else _status_fallback(anchor)


async def overall_insight(status: str, module_insights: list[dict[str, Any]]) -> str:
    """综合研判卡（现状条上方）：综合国内国际现状 + 四个板块边际结论，给出
    利好/利空哪些大类资产的总研判。module_insights 为各核心模块已生成的卡。"""
    if not _endpoint():
        return ""
    module_lines = "\n".join(
        f"- {m.get('module')}：{m.get('impact', '')}".rstrip("：")
        for m in module_insights
        if isinstance(m, dict) and (m.get("impact") or m.get("events"))
    )
    if not status and not module_lines:
        return ""
    parsed = await _chat_json(
        "你是宏观策略分析师。以下是当前市场信息：\n\n"
        f"国内国际现状：\n{status or '（缺）'}\n\n"
        f"各预测市场板块的边际研判：\n{module_lines or '（缺）'}\n\n"
        "请综合以上现状与各板块边际信息，输出 3 句、150 字以内的中文综合研判：当前形势的主线是什么，"
        "对大类资产（A股/美股/黄金/原油/美元/债券等）明确利好哪些、利空哪些（方向结论必须在句中直接给出）。"
        "信息密度要高、措辞凝练，直接说结论，不写「综合来看」「需要注意」这类套话，"
        "观点必须鲜明，禁止两面下注式表述。"
        '只返回 JSON：{"overall": "..."}，不要解释或代码块标记。'
    )
    overall = parsed.get("overall")
    return overall.strip() if isinstance(overall, str) and overall.strip() else ""
