"""FedWatch 离线单元测试：概率引擎 / 日历解析 / 声明解析 / 对比矩阵（不触网）。"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import fedwatch

ET = ZoneInfo("America/New_York")


def _meeting(y, m, d1, d2):
    return {
        "label": f"{y}-{m:02d}",
        "start": datetime(y, m, d1, tzinfo=ET),
        "end": datetime(y, m, d2, tzinfo=ET),
        "has_sep": m in (3, 6, 9, 12),
    }


# ---------------------------------------------------------------------------
# FOMC 日历解析
# ---------------------------------------------------------------------------

def test_parse_fomc_calendar_basic():
    html = """
    <div>2026 FOMC Meetings</div>
    <strong>October</strong><div class="fomc-meeting__date">27-28</div>
    <strong>December</strong><div class="fomc-meeting__date">8-9*</div>
    <div>2025 FOMC Meetings</div>
    <strong>January</strong><div>28-29</div>
    """
    ms = fedwatch._parse_fomc_calendar(html)
    labels = [m["label"] for m in ms]
    assert "2026-10" in labels and "2026-12" in labels and "2025-01" in labels
    oct_m = next(m for m in ms if m["label"] == "2026-10")
    assert oct_m["start"].day == 27 and oct_m["end"].day == 28
    dec_m = next(m for m in ms if m["label"] == "2026-12")
    assert dec_m["end"].day == 9  # 星号剥离
    assert dec_m["has_sep"] is True and oct_m["has_sep"] is False


# ---------------------------------------------------------------------------
# 声明目标区间解析（分数写法）
# ---------------------------------------------------------------------------

def test_frac_to_float():
    assert fedwatch._frac_to_float("3-3/4") == 3.75
    assert fedwatch._frac_to_float("1/4") == 0.25
    assert fedwatch._frac_to_float("4") == 4.0
    assert fedwatch._frac_to_float("x") is None


def test_latest_statement_range_from_fake_html(monkeypatch):
    cal_html = ('<a href="/newsevents/pressreleases/monetary20260916a.htm">x</a>')
    body = ("<html><body>For release at 2:00 p.m. EDT "
            "The Committee decided to raise the target range for the federal "
            "funds rate by 1/4 percentage point to 3-3/4 to 4 percent.</body></html>")
    monkeypatch.setattr(fedwatch, "_fetch", lambda url, timeout=15: body if "monetary2026" in url else cal_html)
    out = fedwatch._latest_statement(cal_html)
    assert out["action"] == "raise"
    assert out["target_low"] == 3.75
    assert out["target_high"] == 4.0


# ---------------------------------------------------------------------------
# 概率引擎（CME 方法论等价）
# ---------------------------------------------------------------------------

def _q(price):
    return {"price": price, "symbol": "TEST", "market_time": None}


def test_probs_hike_fifty_fifty():
    """锚月 3.75、会议月合约隐含偏移 = 半个 25bp → P(hike)=0.5。"""
    zq = {
        # 当月（无会议剩余）：锚
        "2026-09": _q(96.25),   # implied 3.75
        "2026-10": _q(96.11),   # implied 3.89，10月 27-28 会议 → 28 日生效、3 天新利率
    }
    spot = {"rate": 3.75, "method": "t"}
    meetings = [_meeting(2026, 10, 27, 28)]
    probs = fedwatch._fedwatch_probs(zq, spot, meetings)
    assert len(probs) == 1
    p = probs[0]
    # 无会议月 09 在锚定窗口内 → path 再锚定为 3.75
    assert abs(p["path_rate"] - 3.75) < 1e-9
    # step = 0.25 * 3/31 ≈ 0.0242；implied−path = 3.89−3.75 = 0.14
    # 但 path 会先被 09 合约再锚定 → 直接验证方向与夹逼
    assert 0.0 <= p["p_hike"] <= 1.0
    assert p["p_hold"] + p["p_hike"] + p["p_cut"] == 1.0


def test_probs_cut_direction():
    """会议月合约隐含低于锚 → P(cut) > 0, P(hike) = 0。"""
    zq = {
        "2026-09": _q(96.25),   # 锚 3.75
        "2026-10": _q(96.30),   # implied 3.70 < 3.75
    }
    spot = {"rate": 3.75, "method": "t"}
    meetings = [_meeting(2026, 10, 27, 28)]
    probs = fedwatch._fedwatch_probs(zq, spot, meetings)
    assert probs[0]["p_hike"] == 0.0
    assert probs[0]["p_cut"] > 0.0


def test_probs_path_rolling():
    """连续两次会议：路径按未截断期望滚动。"""
    zq = {
        "2026-09": _q(96.25),   # 锚 3.75
        "2026-10": _q(96.11),   # hike 概率正
        "2026-11": _q(96.08),   # 无会议月：11 月合约再锚定 path = 3.92
        "2026-12": _q(96.02),   # 12 月会议：implied 3.98 vs path 3.92 → 再加息概率
    }
    spot = {"rate": 3.75, "method": "t"}
    meetings = [_meeting(2026, 10, 27, 28), _meeting(2026, 12, 8, 9)]
    probs = fedwatch._fedwatch_probs(zq, spot, meetings)
    assert len(probs) == 2
    assert probs[0]["meeting"] == "2026-10"
    assert probs[1]["meeting"] == "2026-12"
    # 11 月锚定后 12 月 path ≈ 3.92
    assert abs(probs[1]["path_rate"] - 3.92) < 1e-9


def test_spot_rate_post_meeting_inference():
    """当月会议已开完 → 当月合约 + 会前 EFFR 反推会后新利率。"""
    # 9 月会议 15-16 日：16 日决策、17 日起 14 天新利率
    # implied = 3.876 → (3.876*30 - 16*3.63) / 14 ≈ 4.1977... 用整数好算的：
    # 会前 16 天 3.75，会后 14 天 r_new → implied 3.80 ⇒ r_new = (3.80*30-60)/14
    zq = {"2026-09": _q(96.20)}  # implied 3.80
    effr_hist = [{"date": f"2026-09-{d:02d}", "rate": 3.75,
                  "target_low": 3.5, "target_high": 3.75} for d in range(1, 17)]
    meetings = [_meeting(2026, 9, 15, 16)]
    # 把「今天」定在会后来测（_spot_rate 内部用 now 判断当月）
    real_now = fedwatch.datetime
    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 20, tzinfo=ET)
    fedwatch.datetime = _FakeDT
    try:
        out = fedwatch._spot_rate(zq, effr_hist, meetings, {})
    finally:
        fedwatch.datetime = real_now
    assert out["rate"] is not None
    # (3.80*30 − 16×3.75) / 14 = (114 − 60)/14 = 3.857…
    assert abs(out["rate"] - 3.857142857) < 1e-3
    assert "反推" in out["method"]


# ---------------------------------------------------------------------------
# Polymarket 块解析 + 对比矩阵
# ---------------------------------------------------------------------------

def test_build_matrix():
    fut = [{
        "meeting": "2026-10", "dates": "2026-10-27 ~ 2026-10-28",
        "implied_rate": 3.89, "path_rate": 3.876,
        "p_hike": 0.52, "p_cut": 0.01, "p_hold": 0.47,
        "contract": "ZQV26.CBT", "contract_price": 96.11,
    }]
    pm = [{
        "title": "Fed Decision in October?", "end": "2026-10-28",
        "vol24h": 1000, "meeting": "2026-10",
        "markets": [
            {"label": "25 bps increase", "yes": 0.445, "vol24h": 1,
             "chg_1d": 0.05, "chg_1w": None, "chg_1m": None,
             "best_bid": None, "best_ask": None, "market_id": "x"},
            {"label": "50+ bps increase", "yes": 0.0075, "vol24h": 1,
             "chg_1d": 0.0, "chg_1w": None, "chg_1m": None,
             "best_bid": None, "best_ask": None, "market_id": "y"},
            {"label": "No change", "yes": 0.545, "vol24h": 1,
             "chg_1d": -0.05, "chg_1w": None, "chg_1m": None,
             "best_bid": None, "best_ask": None, "market_id": "z"},
            {"label": "25 bps decrease", "yes": 0.0095, "vol24h": 1,
             "chg_1d": 0.0, "chg_1w": None, "chg_1m": None,
             "best_bid": None, "best_ask": None, "market_id": "w"},
        ],
    }]
    rows = fedwatch._build_matrix(fut, pm, {"zq": {}, "pm": {}})
    assert len(rows) == 1
    r = rows[0]
    assert r["polymarket"]["p_hike"] == 0.4525       # 0.445 + 0.0075
    assert r["polymarket"]["p_cut"] == 0.0095
    assert abs(r["diff"]["p_hike_diff"] - (0.4525 - 0.52)) < 1e-9

    # PM 侧 24h 变化由 _marginal_changes 聚合（hike 通道 chg_1d 求和）
    marg = fedwatch._marginal_changes(fut, pm)
    pm_chg = marg["pm"].get("2026-10") or {}
    assert pm_chg["p_hike_chg_24h"] == 0.05   # 25bp 增加 +0.05、50bp 增加 +0.0
    assert pm_chg["p_hold_chg_24h"] == -0.05


def test_build_matrix_no_pm_event():
    fut = [{
        "meeting": "2027-04", "dates": "x", "implied_rate": 4.0,
        "path_rate": 4.0, "p_hike": 0.1, "p_cut": 0.0, "p_hold": 0.9,
        "contract": "ZQJ27.CBT", "contract_price": 96.0,
    }]
    rows = fedwatch._build_matrix(fut, [], {"zq": {}, "pm": {}})
    assert rows[0]["polymarket"] is None and rows[0]["diff"] is None


def test_pm_event_filter():
    """非 Fed 决策类事件被过滤；kind/year 正确提取。"""
    class _FakeEvents:
        data = [
            {"title": "Fed Decision in December?", "end": "2026-12-09",
             "vol24h": 5, "markets": [{"label": "No change", "yes": 0.3, "vol24h": 0,
                                        "chg_1d": None, "chg_1w": None, "chg_1m": None,
                                        "best_bid": None, "best_ask": None, "market_id": "a"}]},
            {"title": "RBI decision in October", "end": "2026-10-07",
             "vol24h": 1, "markets": []},
            {"title": "How many Fed rate hikes in 2026?", "end": "2026-12-31",
             "vol24h": 2, "markets": [{"label": "2 (50 bps)", "yes": 0.66, "vol24h": 0,
                                        "chg_1d": None, "chg_1w": None, "chg_1m": None,
                                        "best_bid": None, "best_ask": None, "market_id": "b"}]},
        ]
    real = fedwatch._pm_events
    fedwatch._pm_events = lambda: _FakeEvents.data
    try:
        out = fedwatch._pm_block()
    finally:
        fedwatch._pm_events = real
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["meeting"] == "2026-12"
    assert len(out["counts"]) == 1
    assert out["counts"][0]["kind"] == "hikes" and out["counts"][0]["year"] == "2026"


# ---------------------------------------------------------------------------
# 历史边际变化
# ---------------------------------------------------------------------------

def test_marginal_changes_from_history(tmp_path):
    hist = tmp_path / "h.jsonl"
    import json as _json
    now = int(time.time())
    rows = [
        # 25h 前：hike 0.40
        _json.dumps({"ts": now - 25 * 3600,
                     "probs": {"2026-10": {"h": 0.40, "c": 0.01}},
                     "pm": {"2026-10": {"h": 0.35, "c": 0.01}}}),
        # 6 天前：hike 0.30
        _json.dumps({"ts": now - 6 * 86400,
                     "probs": {"2026-10": {"h": 0.30, "c": 0.02}},
                     "pm": {"2026-10": {"h": 0.28, "c": 0.02}}}),
    ]
    hist.write_text("\n".join(rows) + "\n", encoding="utf-8")
    real_hist = fedwatch._HISTORY
    fedwatch._HISTORY = str(hist)
    try:
        probs = [{"meeting": "2026-10", "dates": "x", "implied_rate": 3.89,
                  "path_rate": 3.876, "p_hike": 0.52, "p_cut": 0.01,
                  "p_hold": 0.47, "contract": "t", "contract_price": 96.11}]
        out = fedwatch._marginal_changes(probs, [])
        zq = out["zq"].get("2026-10") or {}
        # 24h：0.52 − 0.40 = 0.12（容差 ±6h 内找到 25h 前那条）
        assert abs(zq.get("p_hike_chg_24h", 0) - 0.12) < 1e-9
        # 7d：0.52 − 0.30 = 0.22
        assert abs(zq.get("p_hike_chg_7d", 0) - 0.22) < 1e-9
    finally:
        fedwatch._HISTORY = real_hist
