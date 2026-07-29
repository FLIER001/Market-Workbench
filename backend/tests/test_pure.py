"""纯逻辑单测（无网络、快、确定）：市场前缀、估值计算、行情解析。"""
import math

import astock


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
    assert rows[0] == {
        "date": "2026-07-28",
        "open": 1299.0,
        "close": 1320.0,
        "high": 1320.0,
        "low": 1289.52,
        "volume": 53135.0,
    }
    assert rows[-1]["close"] == 1334.01
