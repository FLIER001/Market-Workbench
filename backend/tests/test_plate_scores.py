"""板块双评分纯逻辑与 API 契约测试。"""

import json
import math
from unittest.mock import patch

import plate_scores


def _make_kline(code, days=130, base=100.0, drift=0.001):
    """生成模拟日K线。"""
    rows = []
    for i in range(days):
        date = f"2026-{3 + i // 28:02d}-{i % 28 + 1:02d}"
        close = base * (1 + drift * i)
        rows.append({
            "date": date,
            "open": close * 0.99,
            "close": close,
            "high": close * 1.01,
            "low": close * 0.98,
            "volume": 100000 + i * 100,
        })
    return rows


def _make_quote(code, name, mcap=500.0, pe=25.0, pb=3.0, turnover=2.0, amount=50000.0):
    return {
        "name": name,
        "price": 50.0,
        "last_close": 49.5,
        "change_pct": 1.0,
        "float_mcap_yi": mcap,
        "pe_ttm": pe,
        "pb": pb,
        "turnover_pct": turnover,
        "amount_wan": amount,
    }


def test_load_constituents():
    boards = plate_scores._load_constituents()
    assert len(boards) == 30
    for b in boards:
        assert b["board_code"].startswith("BK")
        assert b["board_name"]
        assert len(b["constituents"]) >= 5
        for c in b["constituents"]:
            assert len(c["code"]) == 6
            assert c["name"]


def test_enrich_boards_caps_at_300_most_relevant(tmp_path, monkeypatch):
    """补充股按流通市值降序补足，每板块总数不超过 300；不足 300 不补。"""
    monkeypatch.setattr(plate_scores, "_BOARD_CACHE_FILE", str(tmp_path / "board_cache.json"))
    candidates_big = [
        {"code": f"{i:06d}", "name": f"S{i}", "float_mcap": 10000.0 - i, "source": "eastmoney:BKTEST"}
        for i in range(500)
    ]
    candidates_small = [
        {"code": f"{i:06d}", "name": f"S{i}", "float_mcap": 100.0 - i, "source": "eastmoney:BKTEST"}
        for i in range(30)
    ]

    def fake_fetch(board_codes, limit=None):
        return candidates_big if "BKTEST" in board_codes else candidates_small

    monkeypatch.setattr(plate_scores.astock, "concept_constituents_em", fake_fetch)
    manual = [{"code": "999999", "name": "人工核心", "membership_type": "core", "business_relevance": 1.0}]
    boards = [
        {"board_code": "BK001", "board_name": "大板块", "constituents": manual},
        {"board_code": "BK002", "board_name": "小板块", "constituents": manual},
    ]
    with patch.dict(plate_scores._EM_BOARD_CODES, {"BK001": ["BKTEST"], "BK002": ["BKSMALL"]}, clear=True):
        enriched = plate_scores._enrich_boards(boards)
    big, small = enriched
    assert len(big["constituents"]) == plate_scores._MAX_CONSTITUENTS  # 截断到300
    assert len(small["constituents"]) == 1 + 30  # 不足300不补
    assert big["constituents"][0]["code"] == "999999"  # 人工核心股始终在最前
    supplements = big["constituents"][1:]
    assert supplements[0]["code"] == "000000"  # 按流通市值降序保留最相关
    assert supplements[-1]["business_relevance"] == 0.75


def test_percentile():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
    assert plate_scores._percentile(values, 6.5) == 50.0
    assert plate_scores._percentile(values, 12.0) == round(11 / 12 * 100, 1)
    assert plate_scores._percentile(values, 1.0) == 0.0
    assert plate_scores._percentile([], 5.0) is None
    assert plate_scores._percentile(values, None) is None


def test_weighted():
    assert plate_scores._weighted([(80.0, 0.5), (60.0, 0.5)]) == 70.0
    assert plate_scores._weighted([(80.0, 1.0), (None, 1.0)]) == 80.0
    assert plate_scores._weighted([(None, 1.0)]) is None


def test_median():
    assert plate_scores._median([1.0, 2.0, 3.0]) == 2.0
    assert plate_scores._median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert plate_scores._median([]) is None


def test_pct_line():
    """分位阈值：夹在 floor/cap 之间，随数据自适应。"""
    vals = list(range(40, 80))  # 40..79
    line = plate_scores._pct_line(vals, 0.70, floor=55.0, cap=75.0)
    assert 55.0 <= line <= 75.0
    # 数据整体偏低时 floor 兜底，偏高时 cap 封顶
    assert plate_scores._pct_line([10, 12, 14], 0.70, floor=55.0, cap=75.0) == 55.0
    assert plate_scores._pct_line([90, 95, 99], 0.70, floor=55.0, cap=75.0) == 75.0
    assert plate_scores._pct_line([], 0.70, floor=55.0, cap=75.0) == 75.0


def test_classify_state_percentile_driven():
    """分位驱动状态：相对强弱而非固定阈值。"""
    # 强度64在 30 家里排前30%（strong_line≈60）→ 主线可参与；旧固定70阈值则不会
    assert plate_scores._classify_state(64, 62, strong_line=60.0, weak_line=48.0, opp_line=58.0) == "主线可参与"
    assert plate_scores._classify_state(49, 62, strong_line=60.0, weak_line=50.0, opp_line=58.0) == "左侧观察"
    assert plate_scores._classify_state(49, 45, strong_line=60.0, weak_line=50.0, opp_line=58.0) == "弱势回避"


def test_classify_state():
    assert plate_scores._classify_state(75, 70) == "主线可参与"
    assert plate_scores._classify_state(75, 50) == "强势观察"
    assert plate_scores._classify_state(75, 40) == "强势但不追"
    assert plate_scores._classify_state(55, 70) == "低位启动候选"
    assert plate_scores._classify_state(45, 70) == "左侧观察"
    assert plate_scores._classify_state(45, 50) == "中性观察"
    assert plate_scores._classify_state(45, 40) == "弱势回避"
    assert plate_scores._classify_state(None, 50) == "数据不足"
    assert plate_scores._classify_state(80, 40, crowding_penalty=16) == "强势但不追"
    assert plate_scores._classify_state(55, 50, crowding_penalty=16) == "中性观察"
    assert plate_scores._classify_state(45, 70, crowding_penalty=16) == "弱势回避"


def test_build_board_index():
    codes = [f"60{i:04d}" for i in range(15)]
    constituents = [{"code": code, "name": code, "business_relevance": 1.0} for code in codes]
    klines = {code: _make_kline(code, 130, 20 + i, 0.001) for i, code in enumerate(codes)}
    quotes = {code: _make_quote(code, code, 500 + i * 50) for i, code in enumerate(codes)}
    result = plate_scores._build_board_index(constituents, klines, quotes)
    assert result is not None
    assert len(result["dates"]) > 20
    assert abs(sum(result["weights"].values()) - 1.0) < 0.01
    assert max(result["weights"].values()) <= 0.07 + 1e-9
    assert sum(sorted(result["weights"].values(), reverse=True)[:5]) <= 0.35 + 1e-9
    # 派生日序列
    derived = plate_scores._derive_daily_series(result["weights"], klines, result["dates"], result["kline_idx"])
    assert len(derived["blended_nav"]) == len(result["dates"])
    assert len(derived["amount"]) == len(result["dates"])
    assert all(a > 0 for a in derived["amount"][1:])  # 成交额已补全，不再只有最后一天


def test_build_board_index_insufficient():
    """成分股不足时返回 None。"""
    constituents = [{"code": "300750", "name": "宁德时代", "business_relevance": 1.0}]
    result = plate_scores._build_board_index(constituents, {}, {})
    assert result is None


def test_calc_strength():
    """强度分计算。"""
    codes = ["300750", "002594", "601012"]
    klines = {
        "300750": _make_kline("300750", 130, 100.0, 0.002),
        "002594": _make_kline("002594", 130, 50.0, 0.0015),
        "601012": _make_kline("601012", 130, 20.0, 0.001),
    }
    dates = sorted(set.intersection(*[{r["date"] for r in klines[c]} for c in codes]))
    board_index = {
        "dates": dates,
        "weights": {"300750": 0.5, "002594": 0.3, "601012": 0.2},
        "kline_idx": {c: {r["date"]: (i, r) for i, r in enumerate(klines[c])} for c in codes},
    }
    benchmark = [{"date": d, "close": 5000 + i * 2, "amount": 1e11} for i, d in enumerate(dates)]
    cross = {"top3_weight": [0.8, 0.9, 1.0]}
    result = plate_scores._calc_strength(board_index, klines, benchmark, cross)
    assert result["score"] is not None
    assert 0 <= result["score"] <= 100
    assert result["detail"]["relative_trend"] is not None
    assert result["detail"]["flow_confirmation"] is not None  # 成交额已补全，不再恒100
    assert result["detail"]["leader_concentration"] is not None


def test_flow_confirmation_not_constant():
    """资金确认不再恒为100：放量上涨>缩量，放量下跌<缩量。"""
    up = _make_kline("X", 130, 100.0, 0.003)
    for i, r in enumerate(up):  # 成交量递增 → 放量上涨
        r["volume"] = 100000 * (1 + i * 0.05)
    down = _make_kline("X", 130, 100.0, -0.003)
    for i, r in enumerate(down):  # 成交量递增但价格下跌 → 放量下跌
        r["volume"] = 100000 * (1 + i * 0.05)

    def _score(kl):
        dates = [r["date"] for r in kl]
        bi = {"dates": dates, "weights": {"X": 1.0}, "kline_idx": {"X": {r["date"]: (i, r) for i, r in enumerate(kl)}}}
        bench = [{"date": d, "close": 5000, "amount": 1e11} for d in dates]
        return plate_scores._calc_strength(bi, {"X": kl}, bench, {"top3_weight": [1.0]})["detail"]["flow_confirmation"]

    assert _score(up) > 60   # 放量上涨 → 偏高分
    assert _score(down) < 50  # 放量下跌 → 低于中性（量价结合）
    assert _score(up) > _score(down)  # 同量下，涨>跌


def test_calc_opportunity():
    """机会分计算。"""
    board_index = {
        "dates": [],
        "weights": {"300750": 0.5, "002594": 0.3, "601012": 0.2},
    }
    quotes = {
        "300750": _make_quote("300750", "宁德时代", 16000, 21, 4.7, 0.6, 987781),
        "002594": _make_quote("002594", "比亚迪", 3000, 30, 3.6, 1.2, 363415),
        "601012": _make_quote("601012", "隆基绿能", 1500, 15, 2.0, 1.5, 200000),
    }
    yjbb = {
        "300750": {"profit_yoy": 30.0, "revenue_yoy": 25.0, "roe": 15.0, "gross_margin": 22.0},
        "002594": {"profit_yoy": 20.0, "revenue_yoy": 15.0, "roe": 12.0, "gross_margin": 18.0},
        "601012": {"profit_yoy": -10.0, "revenue_yoy": -5.0, "roe": 5.0, "gross_margin": 12.0},
    }
    constituents = [
        {"code": "300750", "name": "宁德时代"},
        {"code": "002594", "name": "比亚迪"},
        {"code": "601012", "name": "隆基绿能"},
    ]
    klines = {
        "300750": _make_kline("300750", 130, 100.0, 0.002),
        "002594": _make_kline("002594", 130, 50.0, 0.001),
        "601012": _make_kline("601012", 130, 20.0, 0.0015),
    }
    dates = sorted(set.intersection(*[{r["date"] for r in klines[c]} for c in klines]))
    board_index["dates"] = dates
    board_index["kline_idx"] = {c: {r["date"]: (i, r) for i, r in enumerate(klines[c])} for c in klines}
    factors = {
        code: {
            "pe_percentile": 30.0 + i * 5,
            "pb_percentile": 25.0 + i * 5,
            "forecast": {"revision_pct": -2.0 + i * 3},
            "stale": False,
        }
        for i, code in enumerate(board_index["weights"])
    }
    benchmark = [{"date": d, "close": 5000, "amount": 1e11} for d in dates]
    result = plate_scores._calc_opportunity(board_index, klines, quotes, yjbb, factors, benchmark)
    assert result["score"] is not None
    assert 0 <= result["score"] <= 100
    assert result["detail"]["fundamental"] is not None
    assert result["detail"]["valuation_match"] is not None
    assert result["detail"]["crowding_score"] is not None
    assert result["detail"]["position_score"] is not None
    assert result["detail"]["crowding_score"] is not None
    assert result["detail"]["earnings_revision"] is not None


def test_capped_weights_meet_both_constraints():
    raw = {str(i): 100 - i for i in range(15)}
    weights = plate_scores._capped_weights(raw)
    assert math.isclose(sum(weights.values()), 1.0)
    assert max(weights.values()) <= 0.07 + 1e-9
    assert sum(sorted(weights.values(), reverse=True)[:5]) <= 0.35 + 1e-9


def test_cross_section_rank():
    rows = [
        {"board_code": "A", "value": 80},
        {"board_code": "B", "value": 60},
        {"board_code": "C", "value": 90},
    ]
    ranks = plate_scores._cross_section_rank(rows, "value")
    assert ranks["C"] == 100.0
    assert ranks["A"] == 50.0
    assert ranks["B"] == 0.0
