"""申万二级行业指标层测试（离线：不访问外网）。"""

from datetime import date, timedelta

import sw_level2_scores as layer


def test_level1_mapping_segments():
    """二级指数代码段映射与跨段例外核对。"""
    cases = {
        ("801014", "种植业"): "801010",
        ("801016", "渔业"): "801010",
        ("801111", "白色家电"): "801110",
        ("801881", "乘用车"): "801880",
        ("801882", "摩托车及其他"): "801880",
        ("801193", "证券Ⅱ"): "801790",
        ("801194", "保险Ⅱ"): "801790",
        ("801951", "煤炭开采"): "801950",
        ("801073", "通用设备"): "801890",
        ("801101", "计算机设备"): "801750",
        ("801102", "通信设备"): "801770",
        ("801221", "通信服务"): "801770",
        ("801991", "航空机场"): "801170",
        ("801996", "旅游及景区"): "801210",
        ("801995", "电视广播Ⅱ"): "801760",
        ("801993", "教育"): "801210",
        ("801083", "其他电子Ⅱ"): "801080",
    }
    for (code, name), expected in cases.items():
        assert layer._level1_code(code, name) == expected, (code, name)


def _synthetic_snapshots():
    """构造 14 个月月报 + 15 个交易日日频的最小可用样本。"""
    monthly = []
    base = date(2025, 6, 30)
    for index in range(14):
        day = (base + timedelta(days=31 * index)).replace(day=28)
        monthly.append((
            day.isoformat(),
            {
                "801014": {
                    "code": "801014", "name": "种植业", "close": 1000 + index * 10,
                    "return_pct": 0.5, "turnover_rate": 2.0, "pe": 20 + index,
                    "pb": 1.5, "turnover_share": 0.5, "float_market_cap": 100.0,
                    "dividend_yield": 1.0,
                },
                "801951": {
                    "code": "801951", "name": "煤炭开采", "close": 2000 + index * 5,
                    "return_pct": -0.2, "turnover_rate": 1.0, "pe": 10 + index * 0.5,
                    "pb": 1.1, "turnover_share": 0.8, "float_market_cap": 300.0,
                    "dividend_yield": 4.0,
                },
            },
        ))
    daily = []
    start = date(2026, 7, 20)
    for index in range(15):
        day = start + timedelta(days=index)
        daily.append((
            day.isoformat(),
            {
                "801014": {
                    "code": "801014", "name": "种植业", "close": 1150 + index,
                    "return_pct": 0.3, "turnover_rate": 2.5, "pe": 33.0,
                    "pb": 1.6, "turnover_share": 0.6, "float_market_cap": 105.0,
                    "dividend_yield": 0.9,
                },
                "801951": {
                    "code": "801951", "name": "煤炭开采", "close": 2075 + index,
                    "return_pct": -0.1, "turnover_rate": 1.2, "pe": 16.0,
                    "pb": 1.2, "turnover_share": 0.9, "float_market_cap": 305.0,
                    "dividend_yield": 3.8,
                },
            },
        ))
    return monthly, daily


def test_build_level2_payload_structure():
    monthly, daily = _synthetic_snapshots()
    payload = layer.build_level2_payload(monthly_snapshots=monthly, daily_snapshots=daily)
    assert payload["industry_count"] == 2
    assert payload["as_of"] == daily[-1][0]
    rows = {row["code"]: row for row in payload["industries"]}
    assert rows["801014"]["level1_code"] == "801010"
    assert rows["801951"]["level1_code"] == "801950"
    for row in rows.values():
        assert row["score"] is not None
        assert 0 <= row["score"] <= 100
        assert row["valuation"]["score"] is not None
        assert row["valuation"]["pe_percentile"] is not None
        assert row["prosperity"]["score"] is not None
        assert row["attention"]["score"] is not None
        assert row["attention"]["turnover_rate_percentile"] is not None
        assert row["crowding"]["risk"] is not None
        assert row["phase"] in ("综合占优", "赔率观察", "集中风险", "相对偏弱", "中性观察")
    assert payload["methodology"]["classification"].startswith("申万二级行业")
    assert payload["methodology"]["weights"] == {"valuation": 30, "prosperity": 40, "attention": 30}


def test_build_level2_requires_history():
    monthly, daily = _synthetic_snapshots()
    try:
        layer.build_level2_payload(monthly_snapshots=monthly[:5], daily_snapshots=daily)
    except ValueError as error:
        assert "不足 12 期" in str(error)
    else:
        raise AssertionError("历史不足时应报错")
