"""事件日历口径：代码分类、时间窗过滤、排序、缺口文案、天数兜底、解禁占比换算。"""
from datetime import date, timedelta

import pytest

import events_calendar as ec


def test_classify_splits_what_this_source_covers():
    usable, skipped = ec.classify(["000887", "600519", "510300", "110043", "00700", "AAPL", "920002"])
    assert usable == ["000887", "600519"]
    joined = " ".join(skipped)
    assert "场内基金 / 可转债" in joined
    assert "港股 / 美股" in joined
    assert "北交所" in joined


def test_classify_keeps_order_and_does_not_dedupe():
    # 去重由 build_calendar 负责，分类只判断能不能算
    usable, _ = ec.classify(["600519", "000887"])
    assert usable == ["600519", "000887"]


def test_calendar_window_filters_and_sorts(monkeypatch):
    today = date.today()
    inside = (today + timedelta(days=3)).isoformat()
    outside = (today + timedelta(days=120)).isoformat()

    monkeypatch.setattr(ec.astock, "tencent_quote", lambda codes: {c: {"name": f"股{c}"} for c in codes})
    monkeypatch.setattr(ec, "_appointments", lambda code, start, end: [
        {"kind": "预约披露", "date": inside, "detail": "2026 年年报", "source": "东财·预约披露时间"},
        {"kind": "预约披露", "date": outside, "detail": "更远的一期", "source": "东财·预约披露时间"},
    ])
    monkeypatch.setattr(ec, "_lockups", lambda code, start, end: [])
    monkeypatch.setattr(ec, "_dividends", lambda code, start, end: [
        {"kind": "除权除息", "date": inside, "detail": "10派2元", "source": "东财·分红送转"},
    ])

    result = ec.build_calendar(["600519", "600519", "00700"], days=30)

    assert result["codes"] == 1                      # 去重后只剩一只 A 股
    # 窗口外的 120 天后那条被汇总处以日期兜掉；剩下两条同一天
    assert len(result["items"]) == 2
    assert {item["kind"] for item in result["items"]} == {"预约披露", "除权除息"}
    assert all(item["date"] == inside for item in result["items"])
    assert result["items"][0]["name"] == "股600519"
    assert any("港股" in gap for gap in result["gaps"])


def test_calendar_falls_back_to_30_days(monkeypatch):
    monkeypatch.setattr(ec.astock, "tencent_quote", lambda codes: {})
    monkeypatch.setattr(ec, "_appointments", lambda code, start, end: [])
    monkeypatch.setattr(ec, "_lockups", lambda code, start, end: [])
    monkeypatch.setattr(ec, "_dividends", lambda code, start, end: [])
    result = ec.build_calendar(["600519"], days=45)
    assert result["window"]["days"] == 30


def test_calendar_without_usable_codes_reports_gaps_only():
    result = ec.build_calendar(["510300", "00700"], days=30)
    assert result["items"] == []
    assert result["codes"] == 0
    assert len(result["gaps"]) == 2


def test_calendar_caps_the_code_list(monkeypatch):
    monkeypatch.setattr(ec.astock, "tencent_quote", lambda codes: {})
    monkeypatch.setattr(ec, "_appointments", lambda code, start, end: [])
    monkeypatch.setattr(ec, "_lockups", lambda code, start, end: [])
    monkeypatch.setattr(ec, "_dividends", lambda code, start, end: [])
    codes = [f"60{i:04d}" for i in range(ec.MAX_CODES + 5)]
    result = ec.build_calendar(codes, days=30)
    assert result["codes"] == ec.MAX_CODES
    assert any("只查了前" in gap for gap in result["gaps"])


def test_calendar_source_failure_is_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(ec.astock, "tencent_quote", lambda codes: {})

    def boom(code, start, end):
        raise RuntimeError("上游 502")

    monkeypatch.setattr(ec, "_appointments", boom)
    monkeypatch.setattr(ec, "_lockups", lambda code, start, end: [])
    monkeypatch.setattr(ec, "_dividends", lambda code, start, end: [])
    result = ec.build_calendar(["600519"], days=30)
    assert any("预约披露" in gap and "上游 502" in gap for gap in result["gaps"])


def test_lockup_detail_converts_ratio_fraction_to_percent():
    # RPT_LIFT_STAGE 的 FREE_RATIO 是小数（0.0045 = 0.45%），写出来必须是百分数
    row = {"FREE_DATE": "2026-12-01 00:00:00", "CURRENT_FREE_SHARES": 9920.2025,
           "FREE_RATIO": 0.081735, "FREE_SHARES_TYPE": "定向增发机构配售股份"}
    items = []
    ec._rows = lambda *a, **k: [row]           # 直接打桩，避免联网
    try:
        items = ec._lockups("600519", date(2026, 11, 1), date(2026, 12, 31))
    finally:
        pass
    assert items[0]["detail"].startswith("解禁 9,920.20 万股")
    assert "占总股本 8.17%" in items[0]["detail"]
