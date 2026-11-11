"""异动接近度的规则口径：板块归属、3 日累计偏离、达到后重新计算、10 日同向次数、
严重异常波动、上市初期不纳入、指数缺日、非 A 股与基金不进计算。
"""
import pandas as pd
import pytest

import abnormal as ab


def _dates(n, start="2026-01-05"):
    return [str(d.date()) for d in pd.bdate_range(start, periods=n)]


def _bars(prices, start="2026-01-05"):
    return [{"date": d, "close": float(p)} for d, p in zip(_dates(len(prices), start), prices)]


def _flat_index(n, value=3000.0, start="2026-01-05"):
    return [{"date": d, "close": value} for d in _dates(n, start)]


RULE = ab.BOARD_RULES["sz_main"]          # 3 日 ±20%、10 日 ±100/−50%、4 次同向算严重
RULE_STAR = ab.BOARD_RULES["star"]        # 3 日 ±30%


@pytest.mark.parametrize("code,board", [
    ("600519", "sh_main"), ("601398", "sh_main"), ("603259", "sh_main"), ("605588", "sh_main"),
    ("688012", "star"), ("689009", "star"),
    ("000887", "sz_main"), ("002371", "sz_main"),
    ("300308", "chinext"), ("301001", "chinext"),
    ("920002", "bse"), ("830799", "bse"),
    ("510300", None), ("159915", None), ("110043", None), ("00700", None), ("AAPL", None),
])
def test_board_of(code, board):
    assert ab.board_of(code) == board


def test_fund_and_bond_codes_are_not_stocks():
    for code in ("510300", "159915", "161725", "110043", "123456"):
        assert ab.is_fund_or_bond(code) is True
    for code in ("600519", "000887", "300308"):
        assert ab.is_fund_or_bond(code) is False


def test_three_day_deviation_reaches_the_standard():
    # 40 天平盘后连涨三天：3 日累计偏离累到 21% 就达到 20% 的标准，
    # 达到后从次一交易日起重新计算，所以「当天」的读数回到 10%（不累计算旧账）
    stock = _bars([100.0] * 40 + [110.0, 121.0, 133.1])
    index = _flat_index(43)
    result = ab.proximity(stock, index, RULE)

    assert [item["kind"] for item in result["recent"]] == ["abnormal"]
    assert result["recent"][0]["dir"] == "up"
    assert result["abnormal"]["up"] == pytest.approx(10.0, abs=1e-6)
    assert result["abnormal"]["reached"] is None
    # 重新计算后的窗口起点就是最后一根 K 线那天
    assert result["since"] == result["as_of"]


def test_flat_market_has_nothing_reached():
    result = ab.proximity(_bars([100.0] * 40), _flat_index(40), RULE)
    assert result["abnormal"]["reached"] is None
    assert result["level"] == 0
    assert result["recent"] == []


def test_deviation_is_measured_against_the_index_not_absolute():
    # 个股涨 25%，指数同期也涨 20% ⇒ 偏离只有 5%，没到 20%
    stock = _bars([100.0] * 37 + [110.0, 121.0, 125.0])
    index = _flat_index(40)
    index[37:] = [{"date": d, "close": v} for d, v in
                  zip(_dates(40)[37:], [3000.0, 3300.0, 3600.0])]
    result = ab.proximity(stock, index, RULE)
    assert result["abnormal"]["up"] == pytest.approx(5.0, abs=1e-6)
    assert result["abnormal"]["reached"] is None


def test_fourth_same_direction_move_makes_it_severe():
    # 连涨三轮、各轮后平一天，摆四轮：10 日内第 4 次向上异常波动 ⇒ 严重异常波动
    prices = [100.0]
    for _ in range(4):
        for _ in range(3):
            prices.append(prices[-1] * 1.25)
        prices.append(prices[-1])
    result = ab.proximity(_bars(prices), _flat_index(len(prices)), RULE)

    kinds = [item["kind"] for item in result["recent"]]
    assert "abnormal" in kinds
    assert "severe" in kinds
    assert result["same_dir"]["need"] == RULE["count"]


def test_star_board_uses_a_wider_standard():
    stock = _bars([100.0] * 40 + [110.0, 118.0, 126.0])   # 3 日 +26%
    index = _flat_index(43)
    assert ab.proximity(stock, index, RULE_STAR)["abnormal"]["reached"] is None
    assert ab.proximity(stock, index, RULE)["abnormal"]["reached"] == "up"


def test_new_listing_days_are_excluded():
    # 只有 5 根 K 线：上市后前 5 个交易日不纳入 ⇒ 没有可算的交易日
    result = ab.proximity(_bars([10.0, 11.0, 12.0, 13.0, 14.0]), _flat_index(5), RULE,
                          listed_in_range=True)
    assert "error" in result
    assert "不纳入计算" in result["error"]


def test_missing_index_day_is_reported_and_skipped():
    stock = _bars([100.0] * 40)
    index = _flat_index(40)
    index = index[:20] + index[21:]          # 指数缺一天
    result = ab.proximity(stock, index, RULE)
    assert any("缺 1 个交易日" in note for note in result["notes"])


def test_no_bars_returns_error_not_zero():
    assert "error" in ab.proximity([], [], RULE)


def test_for_codes_classifies_what_it_cannot_compute(monkeypatch):
    monkeypatch.setattr(ab, "_stock_bars", lambda code: _bars([100.0] * 40))
    monkeypatch.setattr(ab, "_index_bars", lambda rule: _flat_index(40))
    monkeypatch.setattr(ab.astock, "tencent_quote", lambda codes: {c: {"name": f"股{c}"} for c in codes})

    out = ab.for_codes(["000887", "510300", "920002", "00700"])

    assert [row["code"] for row in out["rows"]] == ["000887"]
    joined = " ".join(out["gaps"])
    assert "基金 / 债券 1 只" in joined
    assert "北交所 1 只" in joined
    assert "港股 / 美股" in joined
    assert "换手率" in joined                 # 主板换手率那一条始终声明没算
