"""PIT 财务数据层：公告日对齐纪律测试（合成数据，不打真 HTTP）。"""
import sys

import pandas as pd

sys.path.insert(0, ".")
sys.path.insert(0, "tests")


def _fake_fund() -> pd.DataFrame:
    """两只股票 × 三期：关键在公告日与报告期的错位。

    600001: 2024 年报（report 12-31）公告日 2025-03-30；
            2025 一季报（report 03-31）公告日 2025-04-25。
    600002: 2024 年报公告日 2025-04-20（晚报的）。
    """
    rows = []
    def add(code, rd, nd, eps):
        rows.append({"code": code, "report_date": rd, "notice_date": nd, "revenue": 1e9,
                     "net_profit": 1e8, "eps": eps, "bps": 10.0, "roe": 8.0,
                     "rev_yoy": 10.0, "profit_yoy": 12.0, "gross_margin": 30.0,
                     "ocf_ps": 0.5, "eps_deducted": eps * 0.9})
    add("600001", "2024-12-31", "2025-03-30", 1.0)
    add("600001", "2025-03-31", "2025-04-25", 0.3)
    add("600002", "2024-12-31", "2025-04-20", 2.0)
    return pd.DataFrame(rows)


def _install_fund(monkeypatch, df: pd.DataFrame):
    import factor_pit

    monkeypatch.setattr(factor_pit, "pit_table", lambda: df.copy())
    monkeypatch.setattr(factor_pit, "_load_meta", lambda: {"periods": ["2024-12-31"]})


def test_pit_visibility_boundary(monkeypatch):
    """公告日纪律：公告日当天可见（收盘后出公告按 T 日可见），此前一律不可见。"""
    import factor_pit

    _install_fund(monkeypatch, _fake_fund())
    dates = ["2025-03-29", "2025-03-30", "2025-04-24", "2025-04-25"]
    panel = factor_pit.as_of_panel(dates)

    def eps_at(code: str, date: str):
        return panel.loc[date, (code, "eps")]

    # 600001：3-29 什么都看不到（NaN）；3-30 见年报；4-24 仍是年报；4-25 切一季报
    assert pd.isna(eps_at("600001", "2025-03-29"))
    assert eps_at("600001", "2025-03-30") == 1.0
    assert eps_at("600001", "2025-04-24") == 1.0
    assert eps_at("600001", "2025-04-25") == 0.3

    # 600002：4-20 才公告，之前必须看不到（哪怕 600001 已经有数据）
    assert pd.isna(eps_at("600002", "2025-03-30"))
    assert eps_at("600002", "2025-04-24") == 2.0


def test_pit_not_report_date_aligned(monkeypatch):
    """反面断言：按报告期对齐是前视偏差。若引擎用 report_date 对齐，
    2025-03-29 就能看到 12-31 年报——本测试锁定这不是当前行为。"""
    import factor_pit

    _install_fund(monkeypatch, _fake_fund())
    panel = factor_pit.as_of_panel(["2025-03-29"])
    # 12-31 报告期的报告日是 3 月末，1-2 月绝对不可见
    assert pd.isna(panel.loc["2025-03-29", ("600001", "eps")])


def test_fund_factor_end_to_end(monkeypatch):
    """fn_* 字段走通公式引擎：EP = fn_eps/close（合成面板取 2025 年段对齐财务公告日）。"""
    import factor_expr
    import factors

    from test_factors import _install_panel, _make_panel

    panel = _make_panel(seed=5)
    # 合成面板默认 2024 年；把每只股票后 60 根 K 线改成 2025-04-21 起以覆盖财务公告日
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=220)]
    span25 = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-04-21", periods=60)]
    new_dates = dates[:160] + span25
    panel = panel.copy()
    for code in panel["code"].unique():
        panel.loc[panel["code"] == code, "date"] = new_dates
    _install_panel(monkeypatch, panel)
    _install_fund(monkeypatch, _fake_fund())

    ast = factor_expr.compile_expr("fn_eps/close")
    fields = factor_expr.build_fields(panel)
    fund_wide = factors._fund_wide_fields(fields["close"].index.tolist())
    assert fund_wide is not None and "fn_eps" in fund_wide
    out = factor_expr.evaluate(ast, fields, fund_wide)
    assert out.notna().sum().sum() > 0
    # 公告日之后 600001 的 EP = 0.3/close（一季报 eps 0.3）
    tail = out["600001"].dropna()
    assert len(tail) > 0


def test_fund_fields_missing_errors(monkeypatch):
    """财务数据未构建时，公式引用 fn_* 应报可读错误而非静默 NaN。"""
    import factor_data
    import factor_expr
    import factor_pit

    from test_factors import _install_panel, _make_panel

    panel = _make_panel(seed=5)
    _install_panel(monkeypatch, panel)
    monkeypatch.setattr(factor_data, "data_version", lambda: "test")
    # 财务表不存在
    monkeypatch.setattr(factor_pit, "pit_table",
                        lambda: (_ for _ in ()).throw(FileNotFoundError("未构建")))
    monkeypatch.setattr(factor_pit, "_FUND_FILE", "/nonexistent/fund.csv.gz")

    ast = factor_expr.compile_expr("fn_eps/close")
    fields = factor_expr.build_fields(panel)
    from factors import _fund_wide_fields
    assert _fund_wide_fields(fields["close"].index.tolist()) is None
    try:
        factor_expr.evaluate(ast, fields, None)
        raise AssertionError("应当报错")
    except ValueError as e:
        assert "财务" in str(e)
