"""择时信号规则测（离线）：打桩前复权日 K，验证事件式信号与强度。

核心约束（对应用户反馈）：信号是"事件"，不是"位置镜像"——
跌多了不会更强，涨多了不会自动变加仓；事件过期后回到观望。
"""
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
    # 高位平台后刚开始下跌：最后一日收盘跌破 MA20×0.99 且量能转弱 → 当日触发减仓
    closes = [10.0] * 30 + [9.8]
    sig = timing.compute_signal("000001", _rows(closes))
    assert sig["signal"] == "reduce"
    assert sig["age_days"] == 0
    assert "跌破" in sig["action"]


def test_reduce_signal_decays_over_days():
    # 同一跌破事件：触发当日强度最高，随后逐日衰减（过期不重复报警）
    base = [10.0] * 30 + [9.8]
    day0 = timing.compute_signal("000004", _rows(base))
    day4 = timing.compute_signal("000004", _rows(base + [base[-1]] * 4))
    assert day0["strength"] > day4["strength"] > 0
    assert day4["age_days"] == 4


def test_aging_downtrend_is_watch_not_reduce():
    # 跌破发生在 25 个交易日前（信号早已过期）：当前只是"位置在 20 日线下方"，
    # 不应再显示减仓——跌了不代表要减，过期事件闭嘴
    closes = [10.0] * 30 + [10.0 - 0.1 * k for k in range(1, 26)] + [7.6] * 12
    sig = timing.compute_signal("000002", _rows(closes))
    assert sig["signal"] == "watch", sig
    assert "不因下跌补仓" in sig["action"]


def test_reduce_strength_not_deeper_not_stronger():
    # 同一跌破事件后，跌得更深不应使强度更高（与"越跌越减"相反）
    base = [10.0] * 30 + [10.0 - 0.1 * k for k in range(1, 6)]
    shallow = timing.compute_signal("000003", _rows(base))
    deeper = timing.compute_signal("000003", _rows(base + [base[-1] - 0.5]))
    assert shallow["signal"] == deeper["signal"] == "reduce"
    assert deeper["strength"] <= shallow["strength"]


def test_breakout_then_hold_decays():
    # 突破当日强加仓；10 个交易日后仍维持 → 降为"持有"且强度衰减
    closes = [10.0] * 55 + [10.6] + [10.6] * 9
    sig = timing.compute_signal("600000", _rows(closes))
    assert sig["signal"] == "add"
    assert sig["signal_label"] == "持有"
    assert sig["strength"] < 3


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
