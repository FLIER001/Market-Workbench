"""事件回看与财报前瞻的口径：窗口起算、停牌不计入、档位截断、少于 8 次不给平均、
解禁占比单位（东财给的是小数）、同日多份合并、事件归一化与端点形状。
"""
import pandas as pd
import pytest

import stock_events as se


def _cal(n=40, start="2020-01-01"):
    return pd.DatetimeIndex(pd.bdate_range(start, periods=n))


def _series(cal, *, step=0.01, drop=()):
    values = [100.0 * (1 + step) ** i for i in range(len(cal))]
    return pd.Series([v for i, v in enumerate(values) if i not in drop], index=cal[[i for i in range(len(cal)) if i not in drop]])


def _flat(cal, value=3000.0):
    return pd.Series([value] * len(cal), index=cal)


# ——— 窗口 ———


def test_window_counts_horizons_and_flags_suspension():
    cal = _cal()
    stock = _series(cal, drop=(12,))          # 第 12 天停牌，落在第 0 天之后
    window, why = se._window(cal[10], cal, stock, _flat(cal))

    assert why is None
    assert window["day0"] == cal[10].date().isoformat()
    assert window["halted_days"] == 1
    # 事件前 5 个交易日都是 +1%，指数不动
    assert window["pre"] == pytest.approx(0.05, abs=1e-9)
    assert window["d0_5"] is not None
    assert len(window["path"]) == se.PRE + 1 + se.POST


def test_window_marks_recent_event_as_incomplete():
    cal = _cal()
    window, _ = se._window(cal[30], cal, _series(cal), _flat(cal))
    assert window["d0_20"] is None            # 之后不够 20 个交易日
    assert window["days_after"] == len(cal) - 31


def test_window_needs_six_prior_trading_days():
    cal = _cal()
    window, why = se._window(cal[3], cal, _series(cal), _flat(cal))
    assert window is None
    assert "不够" in why


def test_window_rejects_unlisted_stock():
    cal = _cal()
    late = pd.Series([100.0] * 5, index=cal[25:30])
    window, why = se._window(cal[30], cal, late, _flat(cal))
    assert window is None
    assert "还没上市" in why


# ——— 统计 ———


def _event(direction="other", halted=0, **horizons):
    base = {"direction": direction, "halted_days": halted,
            "pre": 0.01, "d0_1": 0.01, "d0_5": 0.02, "d0_20": 0.03}
    base.update(horizons)
    return base


def test_summary_withholds_mean_below_min_events():
    events = [_event() for _ in range(se.MIN_EVENTS - 1)]
    summary = se._summary(events, {"all": "全部"})
    assert summary[0]["events"] == se.MIN_EVENTS - 1
    assert summary[0]["horizons"]["d0_5"]["mean"] is None
    assert "不给平均" in summary[0]["note"]


def test_summary_excludes_suspended_events_but_lists_them():
    events = [_event() for _ in range(se.MIN_EVENTS)] + [_event(halted=2)]
    summary = se._summary(events, {"all": "全部"})
    assert summary[0]["events"] == se.MIN_EVENTS + 1
    assert summary[0]["horizons"]["d0_5"]["n"] == se.MIN_EVENTS
    assert summary[0]["horizons"]["d0_5"]["mean"] == pytest.approx(0.02, abs=1e-6)
    assert "停牌" in summary[0]["note"]


def test_summary_skips_empty_group():
    summary = se._summary([_event(direction="up")], {"all": "全部", "up": "向好", "down": "向差"})
    assert [row["key"] for row in summary] == ["all", "up"]


def test_mean_path_needs_full_windows():
    full = _event(path=[0.0] * (se.PRE + 1 + se.POST))
    assert se._mean_path([full] * se.MIN_EVENTS)["n"] == se.MIN_EVENTS
    assert se._mean_path([full] * (se.MIN_EVENTS - 1)) is None
    short = _event(path=[0.0] * 3)
    assert se._mean_path([short] * se.MIN_EVENTS) is None


# ——— 事件归一化 ———


def test_forecast_events_group_by_notice_date_and_map_direction():
    rows = [
        {"NOTICE_DATE": "2024-07-13 00:00:00", "REPORT_DATE": "2024-06-30 00:00:00",
         "PREDICT_FINANCE": se.NET_PROFIT, "PREDICT_TYPE": "略增",
         "PREDICT_AMT_LOWER": 690000000, "PREDICT_AMT_UPPER": 750000000},
        {"NOTICE_DATE": "2024-07-13 00:00:00", "REPORT_DATE": "2024-06-30 00:00:00",
         "PREDICT_FINANCE": "每股收益", "PREDICT_TYPE": "略增"},
        {"NOTICE_DATE": "2023-01-20 00:00:00", "REPORT_DATE": "2022-12-31 00:00:00",
         "PREDICT_FINANCE": se.NET_PROFIT, "PREDICT_TYPE": "首亏"},
        {"NOTICE_DATE": None, "PREDICT_TYPE": "预增"},
    ]
    events, bad = se._forecast_events(rows)

    assert bad == 1
    assert len(events) == 2
    first = [e for e in events if e["date"] == "2024-07-13"][0]
    assert first["label"] == "2024 年中报预告"
    assert first["direction"] == "up"
    assert "亿元" in first["detail"]          # 690,000,000 元 → 6.90 亿元，不把原值抬上去
    assert [e["direction"] for e in events if e["date"] == "2023-01-20"] == ["down"]


def test_lockup_ratio_is_a_fraction_and_converts_to_percent():
    rows = [
        {"FREE_DATE": "2018-06-26 00:00:00", "CURRENT_FREE_SHARES": 54.425,
         "FREE_RATIO": 0.00044665, "FREE_SHARES_TYPE": "股权激励限售股份"},
        {"FREE_DATE": "2017-05-08 00:00:00", "CURRENT_FREE_SHARES": 9920.2,
         "FREE_RATIO": 0.0817, "FREE_SHARES_TYPE": "定向增发机构配售股份"},
        {"FREE_DATE": "2999-01-01 00:00:00", "CURRENT_FREE_SHARES": 10.0,
         "FREE_RATIO": 0.02, "FREE_SHARES_TYPE": "首发原股东限售股份"},
    ]
    past, bad, upcoming = se._lockup_events(rows)

    assert bad == 0
    assert len(upcoming) == 1                 # 还没到的单独列，不参与超额收益
    big = [e for e in past if e["date"] == "2017-05-08"][0]
    assert big["direction"] == "large"        # 8.17% ≥ 1%
    assert "占总股本 8.17%" in big["detail"]
    small = [e for e in past if e["date"] == "2018-06-26"][0]
    assert small["direction"] == "small"


def test_dragon_tiger_and_survey_details_are_readable():
    dragons, _ = se._dragon_events([
        {"TRADE_DATE": "2026-10-16 00:00:00", "EXPLAIN": "普通席位买入", "BILLBOARD_DEAL_AMT": 3.8e8},
    ])
    assert dragons[0]["detail"] == "普通席位买入，成交 3.80 亿元"

    surveys, _ = se._survey_events([
        {"NOTICE_DATE": "2026-10-21 00:00:00", "RECEIVE_START_DATE": "2026-10-21 00:00:00",
         "RECEIVE_OBJECT": "参与公司可转债网上路演的投资者"},
    ])
    assert "接待日 2026-10-21" in surveys[0]["detail"]
    assert "003" not in surveys[0]["detail"]  # 源里的对象类型是代码，不往界面上抛


# ——— 端点形状 ———


def _patch_sources(monkeypatch, events, *, stock=None, bench=None):
    monkeypatch.setattr(se, "_event_rows", lambda code, kind: events)
    cal = _cal(120)
    monkeypatch.setattr(se, "_klines", lambda code: stock if stock is not None else _series(cal))
    monkeypatch.setattr(se, "_benchmark", lambda: bench if bench is not None else _flat(cal))


def test_event_study_shape_and_overlap_flag(monkeypatch):
    rows = [{"NOTICE_DATE": f"2020-03-{day:02d} 00:00:00", "REPORT_DATE": "2019-12-31 00:00:00",
             "PREDICT_FINANCE": se.NET_PROFIT, "PREDICT_TYPE": "预增"} for day in (2, 5, 6)]
    _patch_sources(monkeypatch, rows)

    result = se.build_event_study("000887", "earnings_forecast")

    assert result["ok"] is True
    assert result["kind_label"] == "业绩预告"
    assert [h["label"] for h in result["horizons"]] == ["事件前 5 日", "第 0–1 日", "第 0–5 日", "第 0–20 日"]
    # 同一天公告的合并成一次；相邻两次窗口重叠要标出来
    assert len(result["events"]) == 3
    assert any(e.get("overlaps_previous") for e in result["events"])


def test_event_study_rejects_unknown_kind_and_bse():
    assert se.build_event_study("000887", "nope")["ok"] is False
    assert se.build_event_study("920002", "lockup")["ok"] is False


def test_event_study_without_events_says_so(monkeypatch):
    _patch_sources(monkeypatch, [])
    result = se.build_event_study("000887", "survey")
    assert result["ok"] is True
    assert result["events"] == []
    assert "没查到" in result["notes"][0]


def test_earnings_preview_merges_same_day_releases(monkeypatch):
    cal = _cal(200, start="2025-01-01")
    monkeypatch.setattr(se, "_klines", lambda code: _series(cal))
    monkeypatch.setattr(se, "_benchmark", lambda: _flat(cal))
    monkeypatch.setattr(se, "_appointments", lambda code: [
        {"REPORT_DATE": "2025-03-31 00:00:00", "APPOINT_PUBLISH_DATE": "2025-04-29 00:00:00",
         "ACTUAL_PUBLISH_DATE": "2025-04-29 00:00:00"},
        {"REPORT_DATE": "2024-12-31 00:00:00", "APPOINT_PUBLISH_DATE": "2025-04-29 00:00:00",
         "ACTUAL_PUBLISH_DATE": "2025-04-29 00:00:00"},
        {"REPORT_DATE": "2027-03-31 00:00:00", "APPOINT_PUBLISH_DATE": "2099-04-25 00:00:00"},
    ])
    monkeypatch.setattr(se, "_reports", lambda code: [
        {"REPORTDATE": "2025-03-31 00:00:00", "BASIC_EPS": 0.24, "PARENT_NETPROFIT": 3.2e8},
    ])
    monkeypatch.setattr(se, "_consensus", lambda code: [
        {"年度": "2026", "预测机构数": 6, "最小值": 0.96, "均值": 1.04, "最大值": 1.21},
    ])
    monkeypatch.setattr(se, "_event_rows", lambda code, kind: [])

    result = se.build_earnings_preview("000887")

    assert result["ok"] is True
    assert result["next_report"]["period_label"] == "2027 年一季报"
    assert result["consensus"][0]["mean"] == pytest.approx(1.04)
    assert len(result["history"]) == 1          # 年报 + 一季报同一天 → 合成一条
    assert result["history"][0]["same_day_releases"] == 2
    assert "、" in result["history"][0]["period_label"]
    assert result["history"][0]["reaction"] is not None
