"""全球预期概率（pulse）模块测试：路由、taxonomy 与 last-good（不联网）。"""
import asyncio
import json
import pytest
from fastapi.testclient import TestClient

import app as app_module
from pulse import market_taxonomy

client = TestClient(app_module.app)


def test_pulse_overview_route_registered():
    # 路由必须存在：缺 token 的 history 校验 422（说明路由挂上了）
    r = client.get("/api/pulse/history")
    assert r.status_code == 422
    r = client.get("/api/pulse/history?token_id=x&interval=bad")
    assert r.status_code == 422


def test_taxonomy_core_order():
    assert market_taxonomy.CORE_MODULES == ["货币政策", "宏观经济", "地缘政治", "股指大宗"]
    assert market_taxonomy.REFERENCE_MODULES[:2] == ["政治选举", "AI科技"]
    assert market_taxonomy.CORE_SET == frozenset(market_taxonomy.CORE_MODULES)


def test_taxonomy_classify_priority():
    # 高信号优先：货币 > 宏观；地缘 > 政治；世界杯 > 地缘；加密 > 股指
    assert market_taxonomy.classify("Will the Fed cut rates in June?", "Economics") == "货币政策"
    assert market_taxonomy.classify("US recession odds this year", "Economics") == "宏观经济"
    assert market_taxonomy.classify("Will Iran attack Israel?", "World") == "地缘政治"
    assert market_taxonomy.classify("Who will win the World Cup?", "Sports") == "体育"
    assert market_taxonomy.classify("Will Bitcoin hit $100k?", "Crypto") == "加密"
    assert market_taxonomy.classify("Will Nvidia beat Q3 earnings?", "Companies") == "AI科技"


def test_taxonomy_kalshi_fallback():
    assert market_taxonomy.classify("Something generic about the economy", "Economics") == "宏观经济"
    assert market_taxonomy.classify("Random title", None) == "其他"


def test_polymarket_history_retries_flaky_clob(monkeypatch):
    """CLOB prices-history 间歇性 HTTP 000（2026-09-09 复测仍在），首两次失败第三次
    成功时必须返回数据而不是抛错。"""
    from pulse import polymarket_signals

    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"history": [{"t": 1, "p": 0.5}]}

    async def fake_get(client, url, params=None, headers=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise polymarket_signals.httpx.ConnectError("flaky edge")
        return FakeResp()

    monkeypatch.setattr(polymarket_signals.httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(polymarket_signals, "asyncio", _InstantAsyncio())

    points = asyncio.run(polymarket_signals.fetch_history("tok", "1w"))
    assert calls["n"] == 3
    assert points == [{"t": 1, "p": 0.5}]


class _InstantAsyncio:
    """sleep() 直通 0：避免 monkeypatch 全局 asyncio 导致事件循环递归。"""

    @staticmethod
    def sleep(_: float):
        return asyncio.sleep(0)


def test_polymarket_history_raises_after_retries(monkeypatch):
    from pulse import polymarket_signals

    async def always_fail(client, url, params=None, headers=None):
        raise polymarket_signals.httpx.ConnectError("down")

    monkeypatch.setattr(polymarket_signals.httpx.AsyncClient, "get", always_fail)
    monkeypatch.setattr(polymarket_signals, "asyncio", _InstantAsyncio())

    with pytest.raises(polymarket_signals.httpx.ConnectError):
        asyncio.run(polymarket_signals.fetch_history("tok", "1w", fidelity=1))


def test_snapshot_dir_respects_vr_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MW_DATA_DIR", str(tmp_path))
    from pulse import market_pulse

    p = market_pulse._snapshot_path()
    assert str(tmp_path) in str(p)
    assert p.name == "pulse_snapshot.json"


def test_empty_source_does_not_overwrite_snapshot(monkeypatch, tmp_path):
    from pulse import market_pulse

    monkeypatch.setenv("MW_DATA_DIR", str(tmp_path))
    old = {"as_of": "2026-08-13T10:00:00+08:00", "modules": [{"key": "宏观经济"}]}
    market_pulse._save_snapshot(old)
    monkeypatch.setattr(market_pulse, "_shaped_polymarket", lambda force: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(market_pulse.kalshi_signals, "fetch_shaped", lambda force: asyncio.sleep(0, result=[{"question": "x"}]))

    with pytest.raises(ValueError, match="Polymarket"):
        asyncio.run(market_pulse._build())

    assert json.loads(market_pulse._snapshot_path().read_text("utf-8")) == old


def test_failed_background_refresh_reports_error_and_keeps_last_good(monkeypatch, tmp_path):
    from pulse import market_pulse

    monkeypatch.setenv("MW_DATA_DIR", str(tmp_path))
    old = {"as_of": "2026-08-13T10:00:00+08:00", "modules": [{"key": "宏观经济"}]}
    market_pulse._save_snapshot(old)
    monkeypatch.setattr(market_pulse, "_rebuilding", True)
    monkeypatch.setattr(market_pulse, "_refresh_error", None)

    async def fail(include_ai=True):
        raise RuntimeError("source down")

    monkeypatch.setattr(market_pulse, "_build", fail)
    asyncio.run(market_pulse._background_rebuild())
    result = asyncio.run(market_pulse.fetch_overview())

    assert result["as_of"] == old["as_of"]
    assert result["cache_state"] == "error"
    assert result["refresh_error"] == "source down"
    assert result["updating"] is False
