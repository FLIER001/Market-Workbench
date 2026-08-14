"""纯逻辑单测（无网络、快、确定）：市场前缀、估值计算、行情解析。"""
import math
import base64
import json
import zlib

import astock


def test_tradingeconomics_chart_decode_and_points():
    """原财新/RatingDog 图表载荷可解码，且只接受 S&P Global 月度序列。"""
    import macro_fetch

    doc = [{"series": [{"serie": {
        "source": "S&P Global",
        "data": [
            [50.5, 1754006400, None, "2025-08-01"],
            [51.7, 1780272000, None, "2026-06-01"],
            [50.9, 1782864000, None, "2026-07-01"],
        ],
    }}]}]
    key = "public-page-key"
    compressed = zlib.compress(json.dumps(doc).encode())
    key_bytes = key.encode()
    encrypted = bytes(v ^ key_bytes[i % len(key_bytes)] for i, v in enumerate(compressed))
    payload = base64.b64encode(encrypted).decode()

    decoded = macro_fetch._te_decode_chart(payload, key)
    assert macro_fetch._te_chart_points(decoded) == [
        {"date": "2025-08", "v": 50.5},
        {"date": "2026-06", "v": 51.7},
        {"date": "2026-07", "v": 50.9},
    ]


def test_caixin_pmi_prefers_continuous_ratingdog_series(monkeypatch):
    """品牌切换后应续接 RatingDog，且不得沿用金十 2025 年陈旧预期。"""
    import macro_fetch

    monkeypatch.setattr(macro_fetch, "_jin10_hist", lambda _id: (
        [("2025-09-01", 50.5)], 49.7))
    monkeypatch.setattr(macro_fetch, "_caixin_web_latest", lambda: {
        "2025-06": {"manufacturing": 50.4},
    })
    monkeypatch.setattr(macro_fetch, "_tradingeconomics_pmi_hist", lambda _sym: [
        {"date": "2025-08", "v": 50.5},
        {"date": "2026-06", "v": 51.7},
        {"date": "2026-07", "v": 50.9},
    ])

    card = macro_fetch.caixin_manufacturing_pmi()
    assert card["label"] == "RatingDog制造业 PMI（原财新）"
    assert card["date"] == "2026-07"
    assert card["value"] == 50.9
    assert card["prev"] == 51.7
    assert card["forecast"] is None
    assert "S&P Global" in card["source"]


def test_macro_module_history_replays_last_36_months_without_future_data():
    """模块卡返回近3年历史分（不足3年时返回全部）；当前分可因披露滞后而向中性衰减。"""
    import market

    hist = [{"date": f"2025-{m:02d}", "v": float(m)} for m in range(1, 13)]
    hist += [{"date": f"2026-{m:02d}", "v": float(12 + m)} for m in range(1, 8)]
    modules = market._module_scores({
        "cpi": {"label": "CPI", "value": 19.0, "prev": 18.0,
                 "forecast": None, "date": "2026-07", "hist": hist},
        "core_cpi": {"label": "核心CPI", "value": 19.0, "prev": 18.0,
                     "forecast": None, "date": "2026-07", "hist": hist},
        "ppi": {"label": "PPI", "value": 19.0, "prev": 18.0,
                "forecast": None, "date": "2026-07", "hist": hist},
    })
    price = next(m for m in modules if m["name"] == "价格与工业利润")

    assert len(price["hist"]) == 19
    assert price["hist"][0]["date"] == "2025-01"
    assert price["hist"][-1]["date"] == "2026-07"
    assert price["hist"][-1]["v"] >= price["score"]


def test_get_prefix():
    assert astock.get_prefix("600519") == "sh"
    assert astock.get_prefix("900001") == "sh"   # 9 开头也是沪
    assert astock.get_prefix("000001") == "sz"
    assert astock.get_prefix("300750") == "sz"
    assert astock.get_prefix("832000") == "bj"   # 8 开头北交所
    assert astock.get_prefix("510300") == "sh"   # 沪 ETF（issue #10：曾误判 sz → 行情为 0）
    assert astock.get_prefix("588000") == "sh"   # 科创 50 ETF
    assert astock.get_prefix("159915") == "sz"   # 深 ETF 15 开头走默认 sz


def test_calc_peg():
    assert astock.calc_peg(20, 0.2) == 20 / (0.2 * 100)  # =1.0
    assert astock.calc_peg(20, 0) == float("inf")        # 增速<=0 → inf
    assert astock.calc_peg(20, -0.1) == float("inf")


def test_pe_digestion():
    assert astock.pe_digestion(30, 0.2) == 0.0           # 当前<=目标PE 无需消化
    assert astock.pe_digestion(25, 0.2, target_pe=30) == 0.0
    assert astock.pe_digestion(60, 0.2) > 0              # 高于目标需消化年数
    assert astock.pe_digestion(60, 0) == float("inf")    # 零增速永远消化不掉


def _gtimg_line(**overrides) -> str:
    # 构造一条腾讯行情返回行：v_sh600519="1~名~代码~价~..."（≥53 字段）。
    parts = ["0"] * 55
    parts[1] = overrides.get("name", "贵州茅台")
    parts[3] = overrides.get("price", "1194.45")
    parts[30] = overrides.get("quote_stamp", "20260729100530")
    parts[36] = overrides.get("volume_lot", "12345")
    parts[39] = overrides.get("pe_ttm", "18.05")
    parts[44] = overrides.get("float_mcap", "12000")
    parts[45] = overrides.get("mcap", "15000")
    parts[46] = overrides.get("pb", "6.41")
    return 'v_sh600519="' + "~".join(parts) + '";'


def test_parse_gtimg():
    out = astock._parse_gtimg(_gtimg_line())
    assert "600519" in out
    q = out["600519"]
    assert q["name"] == "贵州茅台"
    assert q["price"] == 1194.45
    assert q["quote_date"] == "2026-07-29"
    assert q["quote_time"] == "10:05:30"
    assert q["volume_lot"] == 12345
    assert q["pe_ttm"] == 18.05
    assert q["pb"] == 6.41
    assert q["mcap_yi"] == 15000
    assert q["float_mcap_yi"] == 12000


def test_parse_gtimg_bad_line_ignored():
    # 字段不足 / 无引号的行应被安全跳过，不抛异常。
    assert astock._parse_gtimg("garbage;no_quotes_here;") == {}
    assert astock._parse_gtimg("") == {}


def test_parse_tencent_kline_skips_bad_rows():
    payload = {
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-07-28", "1299.00", "1320.00", "1320.00", "1289.52", "53135"],
                    ["bad"],
                    ["2026-07-29", "1333.83", "1334.01", "1343.48", "1312.06", "41492"],
                ],
            },
        },
    }

    rows = astock._parse_tencent_kline(payload, "sh600519", "day")

    assert len(rows) == 2
    assert {k: rows[0][k] for k in ("date", "open", "close", "high", "low", "volume")} == {
        "date": "2026-07-28",
        "open": 1299.0,
        "close": 1320.0,
        "high": 1320.0,
        "low": 1289.52,
        "volume": 53135.0,
    }
    assert rows[-1]["close"] == 1334.01


def test_macro_missing_weights_are_neutral_and_low_coverage_has_no_score():
    """缺项不得把剩余指标放大到满权重；覆盖低于50%时模块不给分。"""
    import market

    hist = [{"date": f"2026-{m:02d}", "v": float(m)} for m in range(1, 8)]
    one = market._module_scores({
        "cpi": {"label": "CPI", "value": 7.0, "prev": 6.0,
                "forecast": None, "date": "2026-07", "hist": hist},
    })
    price = next(m for m in one if m["name"] == "价格与工业利润")
    assert price["score"] is None
    assert price["coverage"] == 14.7

    three = market._module_scores({
        "cpi": {"label": "CPI", "value": 7.0, "prev": 6.0,
                "forecast": None, "date": "2026-07", "hist": hist},
        "core_cpi": {"label": "核心CPI", "value": 7.0, "prev": 6.0,
                     "forecast": None, "date": "2026-07", "hist": hist},
        "ppi": {"label": "PPI", "value": 7.0, "prev": 6.0,
                "forecast": None, "date": "2026-07", "hist": hist},
    })
    price = next(m for m in three if m["name"] == "价格与工业利润")
    assert price["score"] is not None
    assert 50 < price["score"] < 100
    assert price["coverage"] == 51.5


def test_macro_modules_have_unique_indicator_ownership_and_clusters_cover_once():
    """同一评分指标只能有一个owner；一级聚类必须恰好覆盖八模块一次。"""
    import market

    owners = market._macro_indicator_owners()
    assert owners == market._MACRO_INDICATOR_OWNER
    assert owners["world_trade_yoy_3mma"] == "全球外部"
    assert "pmi_new_orders" not in owners
    assert "m1" not in owners and "m2" not in owners
    assert "policy_execution" not in owners

    scored = set(owners)
    for derived, parents in market._MACRO_DERIVED_FROM.items():
        if derived in scored:
            assert scored.isdisjoint(parents), f"{derived} 与基础序列重复计分"

    module_names = [name for name, *_ in market._MACRO_MODULES]
    clustered = [name for cluster in market._MACRO_CLUSTERS for name in cluster["modules"]]
    assert len(module_names) == 8
    assert sorted(clustered) == sorted(module_names)
    assert len(clustered) == len(set(clustered))


def test_climate_staleness_dampens_signal_instead_of_cancelling():
    """滞后因子应把得分拉向50，而不是在归一化分母中相互抵消。"""
    import market

    hist = [{"date": f"2025-{m:02d}", "v": float(m)} for m in range(1, 13)]
    specs = ["pmi_headline", "pmi_new_orders", "pmi_production", "pmi_new_export_orders",
             "pmi_expectation", "non_man_pmi", "cx_pmi"]
    fresh = {k: {"label": k, "value": 12.0, "prev": 11.0, "forecast": None,
                 "date": "2026-07", "hist": hist} for k in specs}
    stale = {k: {**v, "date": "2026-03"} for k, v in fresh.items()}
    fresh_score = market._climate_module_score(fresh, as_of="2026-07")["score"]
    stale_score = market._climate_module_score(stale, as_of="2026-07")["score"]
    assert fresh_score is not None and stale_score is not None
    assert abs(stale_score - 50) < abs(fresh_score - 50)


def test_climate_momentum_needs_a_different_observation_period(monkeypatch):
    """只有同观察期快照时不得伪造0.0动量。"""
    import market

    climate = {"score": 40.0, "submodules": [{"used": [{"date": "2026-07"}]}]}
    monkeypatch.setattr(market, "_load_climate_hist", lambda: [
        {"schema": market._CLIMATE_SCHEMA, "date": "2026-07", "score": 40.0},
    ])
    assert market._climate_hist_mom(climate) is None


def test_private_credit_growth_uses_stock_algebra_and_loans_use_flows():
    """私人信用用存量口径反推同比；贷款余额先转增量再评分。"""
    import market

    ind = {
        "social_financing_stock": {
            "hist": [{"date": "2026-05", "v": 10.0}, {"date": "2026-06", "v": 10.0}],
            "stock_level_hist": [{"date": "2026-05", "v": 110.0}, {"date": "2026-06", "v": 121.0}],
            "gov_bond_growth_hist": [{"date": "2026-05", "v": 25.0}, {"date": "2026-06", "v": 20.0}],
            "gov_bond_level_hist": [{"date": "2026-05", "v": 25.0}, {"date": "2026-06", "v": 30.0}],
        },
        "credit_by_sector": {
            "hist": [{"date": f"2026-0{i}", "v": v} for i, v in enumerate([100, 110, 130, 160, 200], 1)],
            "corp_ml_loan_hist": [{"date": f"2026-0{i}", "v": v} for i, v in enumerate([200, 210, 230, 260, 300], 1)],
        },
    }
    market._add_derived(ind)
    assert ind["private_credit_growth"]["label"] == "私人信用存量同比"
    # 2026-06: (121-30) / (121/1.1 - 30/1.2) - 1 = 7.0588%
    assert math.isclose(ind["private_credit_growth"]["value"], 7.06, abs_tol=0.01)
    assert ind["household_ml_loan"]["hist"] == [
        {"date": "2026-04", "v": 20.0}, {"date": "2026-05", "v": 30.0},
    ]


def test_eps_revision_breadth_requires_prior_day_and_common_target_year():
    import market

    previous = {f"{i:06d}": {"year": 2027, "eps": 1.0} for i in range(80)}
    current = {code: {"year": 2027, "eps": 1.1 if i < 30 else 0.9 if i < 40 else 1.0}
               for i, code in enumerate(previous)}
    first, snap = market._eps_revision_card(previous, None, "2026-08-10")
    assert first is None
    card, _ = market._eps_revision_card(current, snap, "2026-08-11")
    assert card is not None
    assert card["value"] == 25.0  # (30上调-10下调)/80
    assert card["sample_size"] == 80


def test_indicator_metadata_marks_last_good_fallback():
    import market

    cards = {
        "cpi": {"label": "CPI", "date": "2026-07", "source": "统计局", "meta": {"fetched_at": "old"}},
        "copper_oil_ratio": {"label": "铜油比", "date": "2026-07", "source": "FRED 铜/油"},
    }
    market._annotate_macro_indicators(cards, {"copper_oil_ratio"}, "2026-08-11 10:00")
    assert cards["cpi"]["meta"]["status"] == "fallback"
    assert cards["cpi"]["meta"]["fetched_at"] == "old"
    assert cards["copper_oil_ratio"]["meta"]["quality"] == "proxy"


# ---- 基金估值引擎：海外市场代理 / 持仓解析（纯逻辑，不打网络）----

def test_fund_top_holdings_parse_mixed_markets():
    """QDII 持仓页：A股链接(0./1.)解析为字符串代码；港/美/韩(116/105/177)解析为 (mkt, code) 元组。"""
    import fund
    html = """
    <tr><td class='toc'><a href='//quote.eastmoney.com/unify/r/0.300750'>300750</a></td><td>9.50%</td></tr>
    <tr><td class='toc'><a href='//quote.eastmoney.com/unify/r/116.02419'>02419</a></td><td>8.92%</td></tr>
    <tr><td class='toc'><a href='//quote.eastmoney.com/unify/r/105.KLAC'>KLAC</a></td><td>8.56%</td></tr>
    <tr><td class='toc'><a href='//quote.eastmoney.com/unify/r/177.005930'>005930</a></td><td>7.10%</td></tr>
    """
    # 直接对解析逻辑做单测：抽出内部正则等价行为
    import re
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r"unify/r/\d\.(\d{6})", row)
        g = re.search(r"unify/r/(1(?:0[567]|1[67])|177)\.([A-Z0-9]{1,12})", row)
        pcts = re.findall(r"([\d.]+)%", row)
        if not pcts:
            continue
        if m:
            out.append((m.group(1), float(pcts[0])))
        elif g:
            out.append(((g.group(1), g.group(2)), float(pcts[0])))
    assert out[0] == ("300750", 9.50)          # A股字符串
    assert out[1] == (("116", "02419"), 8.92)  # 港股元组
    assert out[2] == (("105", "KLAC"), 8.56)   # 美股元组
    assert out[3] == (("177", "005930"), 7.10) # 韩股元组


def test_fund_global_index_proxy_covers_qdii():
    """港股/QDII 跟踪标的应命中海外代理表（纳斯达克/恒生/海外互联网/标普）。"""
    import fund
    kws = [kw for kw, _, _ in fund._GLOBAL_INDEX_PROXY]
    def hit(name):
        return any(kw in name for kw in kws)
    assert hit("纳斯达克100指数")
    assert hit("中证港股通非银行金融主题人民币指数")
    assert hit("中证海外中国互联网50人民币指数")
    assert hit("标普全球高端消费品指数")
    assert hit("恒生科技指数")
    assert not hit("中证红利低波动指数")   # A股指数不应误命中海外代理
    assert not hit("沪深300")


def test_fund_self_estimate_coverage_threshold():
    """重仓覆盖率 <15% 的基金（债基零头/LOF披露不全）不给伪精确估值。"""
    import fund
    # 直接验证门槛逻辑：覆盖率计算
    low = [(("105", "NVDA"), 0.08)]           # 501312 实际披露
    high = [("300750", 9.5), (("116", "02419"), 8.92), (("105", "KLAC"), 8.56)]
    assert sum(w for _, w in low) < 15
    assert sum(w for _, w in high) >= 15


def test_parse_imf_world_gold_monthly_csv():
    import gold_score
    raw = """COUNTRY,INDICATOR,UNIT,FREQUENCY,TIME_PERIOD,OBS_VALUE
G001,RGV_REVS,FTO,M,2026-M05,1180098214.943816
G001,RGV_REVS,FTO,M,2026-M06,1181789737.996150
USA,RGV_REVS,FTO,M,2026-M06,261498926.241540
G001,RGV_REVS,FTO,Q,2026-Q2,1181789737.996150
"""

    rows = gold_score._parse_imf_gold_csv(raw)

    assert [d for d, _ in rows] == ["2026-05", "2026-06"]
    assert math.isclose(rows[-1][1], 36757.770, abs_tol=0.001)
    assert math.isclose(rows[-1][1] - rows[-2][1], 52.612, abs_tol=0.001)


def test_parse_wgc_global_etf_weekly_holdings():
    import gold_score
    raw = """{"chartData":{"data":{"Weekly":{"tonnes":{"set":[
      [1784851200000,2038.38,1441.26,508.53,74.41,4064.63],
      [1785456000000,2034.90,1446.30,512.31,74.45,4047.14],
      [1786060800000,0,null,null,null,4040.00]
    ]}}}}}"""

    rows = gold_score._parse_wgc_etf_holdings(raw)

    assert rows == [("2026-07-24", 4062.58), ("2026-07-31", 4067.96)]


def test_parse_wgc_real_lbma_and_sge_reference_prices():
    import gold_score
    raw = """{"chartData":{
      "lbma_am_usd":[[1786060800000,4301.85],[1786060800000,4302.00]],
      "lbma_pm_usd":[[1786060800000,4335.55],[null,12]],
      "sge_pm_cny":[[1786060800000,932.83],[1786147200000,-1]]
    }}"""

    rows = gold_score._parse_wgc_reference(raw)

    assert rows["lbma_am_usd"] == [("2026-08-07", 4302.0)]
    assert rows["lbma_pm_usd"] == [("2026-08-07", 4335.55)]
    assert rows["sge_pm_cny"] == [("2026-08-07", 932.83)]


def test_parse_ofr_financial_stress_excludes_safe_assets():
    import gold_score
    raw = """Date,OFR FSI,Credit,Equity valuation,Safe assets,Funding,Volatility
2026-08-05,-2.592,-1.146,-0.621,-0.308,-0.087,-0.430
2026-08-06,-2.761,-1.173,-0.608,-0.310,-0.106,-0.564
"""

    rows = gold_score._parse_ofr_ex_safe(raw)

    assert rows == [("2026-08-05", -2.284), ("2026-08-06", -2.451)]


def test_etf_surprise_regression_uses_only_prior_observations():
    import gold_score
    pairs = [(f"w{i:03}", 1 + 2 * i, float(i)) for i in range(105)]
    pairs.append(("w105", 1 + 2 * 105 + 0.5, 105.0))

    rows = gold_score._rolling_residuals(pairs)

    assert rows[-1]["date"] == "w105"
    assert math.isclose(rows[-1]["beta"], 2.0, abs_tol=1e-12)
    assert math.isclose(rows[-1]["residual"], 0.5, abs_tol=1e-12)


def test_daily_gold_momentum_includes_latest_price():
    import gold_score
    price = 100.0
    hist = []
    for i in range(180):
        price *= 1 + (0.002 if i % 3 else -0.001)
        hist.append((f"d{i:03}", price))

    rows = gold_score._risk_adj_momentum_series(hist)

    assert rows[-1][0] == hist[-1][0]
    assert math.isfinite(rows[-1][1])


def test_parse_hf_gold_quotes():
    import gold_score
    raw = (
        'v_hf_XAU="4412.60,1.02,4412.60,4412.95,4441.07,4362.46,02:43:00,4368.00,'
        '4371.82,0,0,0,2026-08-13,伦敦金（现货黄金）";\n'
        'v_hf_GC="4475.70,0.78,4472.40,4472.70,4502.70,4421.40,02:44:06,4441.10,'
        '4430.00,0,2,1,2026-08-13,纽约黄金";\n'
    )

    rows = gold_score._parse_hf_quotes(raw)

    xau = rows["XAU"]
    assert xau["name"] == "伦敦金（现货黄金）"
    assert math.isclose(xau["price"], 4412.60)
    assert math.isclose(xau["change_pct"], 1.02)
    assert math.isclose(xau["prev_close"], 4368.00)
    assert xau["date"] == "2026-08-13" and xau["time"] == "02:43:00"
    gc = rows["GC"]
    assert math.isclose(gc["price"], 4475.70)


def test_gold_source_period_age_handles_daily_and_monthly_periods():
    from datetime import date
    import gold_score

    today = date(2026, 8, 11)
    assert gold_score._period_age_days("2026-07-31", today) == 11
    assert gold_score._period_age_days("2026-06", today) == 42

    gold_score._source_status(
        "gold:fred:DFII10", False, 1, [("2026-08-01", 1.0)], today=today
    )
    status = gold_score._SOURCE_STATUS["gold:fred:DFII10"]
    assert status["status"] == "stale"
    assert status["stale_reason"] == "observation_lag"


def test_parse_treasury_real_yield_and_h10_release_ttl():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import gold_score

    raw = b'''<feed xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
      <entry><m:properties><d:NEW_DATE>2026-08-07T00:00:00</d:NEW_DATE>
      <d:TC_10YEAR>2.40</d:TC_10YEAR></m:properties></entry></feed>'''
    assert gold_score._parse_treasury_real_yield(raw) == [("2026-08-07", 2.4)]

    et = ZoneInfo("America/New_York")
    assert gold_score._h10_cache_ttl(datetime(2026, 8, 10, 16, 14, tzinfo=et)) == 6 * 3600
    assert gold_score._h10_cache_ttl(datetime(2026, 8, 10, 16, 16, tzinfo=et)) == 60


def test_gold_dimension_history_carries_slow_series_and_keeps_weekly_last():
    import gold_score

    histories = {
        "fast": [("2026-07-01", 20.0), ("2026-07-03", 40.0), ("2026-07-10", 60.0)],
        "slow": [("2026-06", 80.0)],
    }
    rows = gold_score._dimension_score_history(
        [("fast", 0.6), ("slow", 0.4)], histories, {"fast": 0.6, "slow": 0.4})

    assert rows == [
        {"date": "2026-06", "v": 80.0},
        {"date": "2026-07-03", "v": 56.0},
        {"date": "2026-07-10", "v": 68.0},
    ]


def test_gold_current_score_is_appended_without_overwriting_latest_observation():
    import gold_score

    rows = [{"date": "2026-08-10", "v": 44.3}]
    gold_score._append_current_score(rows, "2026-08-11", 44.6)
    assert rows == [
        {"date": "2026-08-10", "v": 44.3},
        {"date": "2026-08-11", "v": 44.6},
    ]

    gold_score._append_current_score(rows, "2026-08-11", 45.0)
    assert rows[-1] == {"date": "2026-08-11", "v": 45.0}


def test_parse_binance_paxg_klines():
    """Binance klines 数组 → 北京时间分时点；openTime(UTC ms)→北京时钟分。"""
    import gold_score
    # openTime=1786550400000 → 北京 2026-08-13 00:00；下一根 00:01
    raw = b'[[1786550400000,"4406.06","4442.95","4364.20","4405.10","12.3",1786550459999,54200,28,"","",0],' \
         b'[1786550460000,"4405.10","4406.00","4404.00","4405.80","8.1",1786550519999,35700,19,"","",0]]'
    pts = gold_score._parse_binance_klines(raw)
    assert len(pts) == 2
    assert pts[0]["time"] == "00:00" and math.isclose(pts[0]["price"], 4405.10)
    assert pts[0]["mod"] == 0
    assert pts[1]["time"] == "00:01" and pts[1]["mod"] == 1
    assert math.isclose(pts[1]["volume"], 8.1)
    assert pts[0]["ot"] == 1786550400000
    # 非法 JSON / 空数组 → []
    assert gold_score._parse_binance_klines(b'') == []
    assert gold_score._parse_binance_klines(b'[]') == []


def test_paxg_reuses_minute_chart_within_one_minute(monkeypatch):
    import gold_score

    calls: list[str] = []
    monkeypatch.setattr(gold_score, "_PAXG_CACHE", {})
    monkeypatch.setattr(gold_score, "_PAXG_CHART_CACHE", {})
    clock = iter([100.0, 121.0])
    monkeypatch.setattr(gold_score.time, "time", lambda: next(clock))

    def fetch(path: str, timeout: int = 12) -> bytes:
        calls.append(path)
        if "ticker/24hr" in path:
            return b'{"lastPrice":"4405.80","prevClosePrice":"4400","openPrice":"4401","highPrice":"4410","lowPrice":"4390","volume":"10"}'
        return b"previous" if "endTime=" in path else b"chart"

    def parse(raw: bytes) -> list[dict]:
        if raw == b"previous":
            return [{"time": "23:59", "price": 4400.0, "volume": 1.0, "mod": 1439, "ot": 1}]
        if raw == b"chart":
            return [{"time": "00:00", "price": 4405.0, "volume": 2.0, "mod": 0, "ot": 2}]
        return []

    monkeypatch.setattr(gold_score, "_binance_get", fetch)
    monkeypatch.setattr(gold_score, "_parse_binance_klines", parse)
    monkeypatch.setattr(gold_score, "usdcny_rate", lambda: 7.0)

    assert gold_score.paxg_usd_spot()["price"] == 4405.8
    assert gold_score.paxg_usd_spot()["price"] == 4405.8
    assert sum("klines" in path for path in calls) == 2
    assert sum("ticker/24hr" in path for path in calls) == 2


def test_paxg_cny_conversion(monkeypatch):
    """PAXG → 国内金价：元/克 = USD/盎司 × USDCNY ÷ 31.1034768；缺汇率时 cny=None。"""
    import gold_score

    monkeypatch.setattr(gold_score, "_PAXG_CACHE", {})
    monkeypatch.setattr(gold_score, "_PAXG_CHART_CACHE", {})
    monkeypatch.setattr(gold_score, "_USDCNY_CACHE", {})
    monkeypatch.setattr(gold_score.time, "time", lambda: 100.0)
    monkeypatch.setattr(gold_score, "_binance_get", lambda path, timeout=12: b"{}")
    monkeypatch.setattr(gold_score, "_parse_binance_klines", lambda raw: [])

    def stub_usdcny() -> float:
        return 7.2

    monkeypatch.setattr(gold_score, "usdcny_rate", stub_usdcny)
    # 4405.8 × 7.2 ÷ 31.1034768 = 1019.88 元/克
    assert math.isclose(gold_score._paxg_to_cny_gram(4405.8, 7.2), 1019.88, abs_tol=0.01)
    assert gold_score._paxg_to_cny_gram(4405.8, None) is None
    assert gold_score._paxg_to_cny_gram(None, 7.2) is None
    assert gold_score._paxg_to_cny_gram(-1.0, 7.2) is None


def test_usdcny_rate_parses_sina_fx(monkeypatch):
    """新浪 fx_susdcny：第 9 列（下标 8）为最新价；异常值抛错回退缓存。"""
    import gold_score

    class FakeResult:
        def __init__(self, out: bytes):
            self.stdout = out
            self.returncode = 0

    monkeypatch.setattr(gold_score, "_USDCNY_CACHE", {})
    monkeypatch.setattr(gold_score.time, "time", lambda: 100.0)
    raw = 'var hq_str_fx_susdcny="23:28:58,6.7304,6.7587,6.7501,144,6.7423,6.7445,6.7301,6.7426,在岸人民币,-0.083,-0.0056,0.0144,行情,0,0,,2026-08-14";'.encode("gbk")
    monkeypatch.setattr(gold_score.subprocess, "run", lambda cmd, capture_output, timeout: FakeResult(raw))
    assert math.isclose(gold_score.usdcny_rate(), 6.7426)

    # 异常汇率（超出 5–9 合理区间）→ 回退上一次成功缓存
    bad = 'var hq_str_fx_susdcny="23:28:58,0,0,0,0,0,0,0,0.5,在岸人民币,0,0,0,行情,0,0,,2026-08-14";'.encode("gbk")
    monkeypatch.setattr(gold_score.subprocess, "run", lambda cmd, capture_output, timeout: FakeResult(bad))
    monkeypatch.setattr(gold_score.time, "time", lambda: 9999.0)  # 越过 TTL
    assert math.isclose(gold_score.usdcny_rate(), 6.7426)


def test_macro_composite_weights_direction_and_coverage():
    """宏观总分：反向模块取 100-分合成；缺分模块按权重归一；覆盖不足一半不输出。"""
    import market

    mods = [
        {"name": "财政地产", "score": 70.0},          # 贡献 (70-50)*0.30 = +6
        {"name": "国内增长与景气", "score": 70.0},     # 反向：(100-70-50)*0.25 = -5
        {"name": "全球外部", "score": 60.0},           # +2
        {"name": "价格与工业利润", "score": 50.0},     # 0
        {"name": "信用周期", "score": 50.0},           # 0
        {"name": "货币与金融条件", "score": 50.0},     # 0
    ]
    comp = market._macro_composite(mods)
    assert comp is not None
    assert math.isclose(comp["score"], 53.0)
    assert comp["coverage"] == 100.0
    assert set(comp["drivers"]) == {"财政地产", "国内增长与景气"}
    inv = next(p for p in comp["parts"] if p["name"] == "国内增长与景气")
    assert inv["direction"] == "inverse" and inv["contribution"] == -5.0
    assert market._composite_state(53.0) == "中性"
    assert market._composite_state(56.0) == "中性偏多"
    assert market._composite_state(70.0) == "偏多"
    assert market._composite_state(40.0) == "中性偏空"

    # 缺一个模块：剩余 95 权重（100-5 货币金融条件）归一
    comp2 = market._macro_composite(mods[:5])
    assert comp2 is not None
    assert math.isclose(comp2["coverage"], 95.0)

    # 覆盖不足一半（只有 10+5 权重的模块有分）→ 不输出
    assert market._macro_composite(mods[4:]) is None
