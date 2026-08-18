from datetime import datetime

import asyncio

import bonds
import industry_chain
import timing_alloc


def test_bonds_overview_force_reaches_every_source(monkeypatch):
    calls = []

    def getter(name):
        def get(force=False):
            calls.append((name, force))
            return {"cache_state": "refreshing", "cached_at": "2026-08-17T10:00:00+08:00"}
        return get

    for name in ("curve", "shibor", "policy", "index", "global", "calc", "positioning"):
        monkeypatch.setattr(bonds, f"get_{name}", getter(name))

    result = bonds._overview_payload(force=True)

    assert calls == [(name, True) for name in ("curve", "shibor", "policy", "index", "global", "calc", "positioning")]
    assert result["cache_state"] == "refreshing"


def test_bonds_overview_warms_from_disk_without_building(monkeypatch):
    snapshot = {"curve": {"curve": [{"tenor": "10年"}]}, "cache_state": "fresh"}
    monkeypatch.setattr(bonds, "_load_snapshot", lambda name: snapshot if name == "overview" else None)
    monkeypatch.setattr(bonds, "_overview_payload", lambda force=False: (_ for _ in ()).throw(AssertionError("must not build")))
    bonds.cache_runtime.invalidate("bonds:overview")

    result = bonds.get_overview()

    assert result["curve"] == snapshot["curve"]


def test_industry_financial_force_bypasses_fresh_cache(monkeypatch):
    calls = []
    cached = {"fetched_at": datetime.now(industry_chain.BEIJING).isoformat(), "gross_margin": 1}
    monkeypatch.setattr(industry_chain, "_load_fin_cache", lambda: {"stocks": {"000001": cached}})
    monkeypatch.setattr(industry_chain, "_save_fin_cache", lambda _data: None)

    def financials(code):
        calls.append(code)
        return {"period": "2026Q2", "gross_margin": "20%"}

    monkeypatch.setattr(industry_chain.astock, "financials", financials)
    result = industry_chain._fetch_financials(["000001"], force=True)

    assert calls == ["000001"]
    assert result["000001"]["gross_margin"] == 20


def test_industry_chain_keeps_previous_dynamic_blocks_on_failure(monkeypatch):
    chain = {"key": "x", "nodes": [{"id": "n", "name": "N", "companies": []}]}
    previous = {
        "profit": {"rows": [{"code": "old"}]},
        "reports": {"rows": [{"title": "old"}]},
    }
    monkeypatch.setattr(industry_chain, "_load_chain_cache", lambda _chain: previous)
    monkeypatch.setattr(industry_chain, "_build_profit", lambda _chain, force=False: (_ for _ in ()).throw(RuntimeError("finance down")))
    monkeypatch.setattr(industry_chain, "_build_reports", lambda _chain: (_ for _ in ()).throw(RuntimeError("reports down")))

    result = industry_chain._build_chain(chain)

    assert result["profit"]["rows"] == previous["profit"]["rows"]
    assert result["reports"]["rows"] == previous["reports"]["rows"]
    assert result["profit"]["refresh_error"] == "finance down"
    assert result["reports"]["refresh_error"] == "reports down"


def test_allocation_warms_from_disk_without_building(monkeypatch):
    snapshot = {"schema_version": 1, "timing": {"regime": "neutral"}}
    monkeypatch.setattr(timing_alloc, "_load_snapshot", lambda: snapshot)
    monkeypatch.setattr(timing_alloc, "_payload", lambda: (_ for _ in ()).throw(AssertionError("must not build")))
    timing_alloc.cache_runtime.invalidate("timing:allocation")

    result = timing_alloc.get_timing_allocation()

    assert result["timing"] == snapshot["timing"]


# —— AI 解读（择时配置页）——

def _seed_allocation_payload():
    payload = {
        "timing": {"score": 64.0, "regime": "risk_on", "regime_label": "偏多",
                   "risk_budget_multiplier": 1.15, "cash_floor": 0.05,
                   "gates": [], "invalidation": ["流动性跌破 30"]},
        "evidence": {
            "macro": {"score": 55.0, "state": "中性", "date": "2026-07-31",
                      "parts": [{"name": "增长景气", "weight": 20, "score": 60.0,
                                 "value": "60/100", "contribution": 2.0}]},
            "liquidity": {"score": 70.0, "state": "偏多", "date": "2026-08-15",
                          "parts": [{"name": "杠杆温度", "weight": 30, "score": 72.0,
                                     "value": "72/100", "contribution": 6.6}]},
            "market_confirm": {"score": 61.0, "date": "2026-08-15",
                               "parts": [{"name": "趋势", "weight": 45, "score": 66.0,
                                          "value": "全指+3.2%", "contribution": 7.2}]},
        },
    }
    timing_alloc.cache_runtime.invalidate("timing:allocation")
    timing_alloc.cache_runtime.seed("timing:allocation", payload)
    return payload


def test_ai_insight_prompt_carries_three_evidence_layers(monkeypatch):
    import pulse.pulse_insight as pi

    _seed_allocation_payload()
    prompts = []

    async def fake_chat(prompt):
        prompts.append(prompt)
        return {"macro": "外需驱动，地产财政缺位。",
                "liquidity": "大单流入与杠杆温度背离。",
                "market_confirm": "价格确认宏观改善，仓位未过热。"}

    monkeypatch.setattr(pi, "chat_json", fake_chat)

    out = asyncio.run(timing_alloc._build_insight())

    assert "宏观" in prompts[0] and "流动性" in prompts[0] and "市场确认" in prompts[0]
    assert "禁止复述" in prompts[0]
    assert out == {"macro": "外需驱动，地产财政缺位。",
                   "liquidity": "大单流入与杠杆温度背离。",
                   "market_confirm": "价格确认宏观改善，仓位未过热。"}


def test_ai_insight_partial_llm_answer_rejected(monkeypatch):
    import pulse.pulse_insight as pi

    _seed_allocation_payload()

    async def partial(_prompt):
        return {"macro": "只有一段"}  # 缺 liquidity/market_confirm → 整体判失败

    monkeypatch.setattr(pi, "chat_json", partial)

    assert asyncio.run(timing_alloc._build_insight()) == ""


def test_ai_insight_cached_read_skips_llm(monkeypatch):
    import pulse.pulse_insight as pi

    payload = _seed_allocation_payload()
    payload["ai_insight"] = {"macro": "a", "liquidity": "b", "market_confirm": "c"}
    async def must_not_call(_prompt):
        raise AssertionError("cached read must not call LLM")
    monkeypatch.setattr(pi, "chat_json", must_not_call)

    assert asyncio.run(timing_alloc.get_ai_insight(force=False)) == {
        "macro": "a", "liquidity": "b", "market_confirm": "c"}


def test_ai_insight_refresh_keeps_old_text_on_llm_failure(monkeypatch):
    import pulse.pulse_insight as pi

    payload = _seed_allocation_payload()
    payload["ai_insight"] = {"macro": "旧a", "liquidity": "旧b", "market_confirm": "旧c"}
    saved = {}
    monkeypatch.setattr(timing_alloc, "_save_snapshot", lambda snap: saved.update(snap))
    async def empty(_prompt):
        return {}
    monkeypatch.setattr(pi, "chat_json", empty)

    # LLM 失败（空串）时回退缓存里的旧解读，不让页面文字消失
    assert asyncio.run(timing_alloc.get_ai_insight(force=True)) == {
        "macro": "旧a", "liquidity": "旧b", "market_confirm": "旧c"}
    assert "ai_insight" not in saved
