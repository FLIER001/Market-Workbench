from datetime import datetime

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
