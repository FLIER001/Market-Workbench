"""择时信号规则测（离线）：打桩前复权日 K，验证加仓/减仓/观望分支与强度。"""
import app as app_module
import timing
from fastapi.testclient import TestClient

client = TestClient(app_module.app)


def _rows(closes, vols=None):
    vols = vols or [100.0] * len(closes)
    return [{"date": f"2026-06-{i + 1:02d}", "close": c, "volume": v} for i, (c, v) in enumerate(zip(closes, vols))]


def test_breakout_add_signal():
    # 前 55 日平缓在 10，最后一日放量收 10.6：同时站上 MA20×1.01 与 50 日高点×1.01 → 突破确认加仓
    closes = [10.0] * 55 + [10.6]
    vols = [100.0] * 55 + [300.0]
    sig = timing.compute_signal("600519", _rows(closes, vols))
    assert sig["signal"] == "add"
    assert sig["signal_label"] == "加仓"
    assert sig["strength"] >= 2
    assert sig["as_of"]


def test_reduce_below_ma20():
    # 高位平台后连续 25 日下跌：收盘跌破 MA20×0.99 且量能转弱 → 减仓
    closes = [10.0] * 30 + [10.0 - 0.15 * k for k in range(1, 26)]
    sig = timing.compute_signal("000001", _rows(closes))
    assert sig["signal"] == "reduce"
    assert "20 日均线" in sig["action"]


def test_watch_inside_band():
    # 长期横盘：收盘在 MA20±1% 过滤带内 → 观望
    closes = [10.0 + (0.003 if i % 2 else -0.003) for i in range(60)]
    sig = timing.compute_signal("510300", _rows(closes))
    assert sig["signal"] == "watch"
    assert sig["strength"] == 0


def test_insufficient_data():
    sig = timing.compute_signal("123456", _rows([10.0] * 10))
    assert sig["signal"] is None
    assert sig["signal_label"] == "数据不足"


def test_timing_api_empty_portfolio():
    r = client.get("/api/portfolio/timing")
    assert r.status_code == 200
    assert r.json()["data"]["signals"] == {}


def test_get_timing_signals_with_stub(monkeypatch):
    closes = [10.0] * 55 + [10.6]
    monkeypatch.setattr(timing, "_load_rows", lambda code: _rows(closes))
    monkeypatch.setattr(timing, "_cache", {})
    out = timing.get_timing_signals(["600519"])
    assert out["600519"]["signal"] == "add"
