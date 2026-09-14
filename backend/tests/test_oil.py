"""油价评分层纯函数测试（不触网）。"""

import json as _json
import math

import oil


def test_signal_labels():
    assert oil._signal_label(85) == "强利多"
    assert oil._signal_label(70) == "利多"
    assert oil._signal_label(50) == "中性"
    assert oil._signal_label(30) == "利空"
    assert oil._signal_label(10) == "强利空"


def test_pct_rank_neutral_on_small_sample():
    assert oil._pct_rank([1.0] * 5, 3.0) == 0.5
    # 25 个样本超过阈值 20 → 正常分位
    assert oil._pct_rank(list(range(25)), 24) == 24 / 25


def test_crowding_cap():
    assert oil._crowding_cap(90, 0.5) == 90
    assert oil._crowding_cap(90, 0.85) == 80
    assert oil._crowding_cap(90, 0.92) == 65
    assert oil._crowding_cap(90, 0.97) == 50


def test_parse_hf_quotes():
    raw = ('v_hf_OIL="88.83,2.02,88.66,88.82,88.95,86.20,05:58:45,87.07,87.11,0,1,1,'
           '2026-08-15,布伦特原油";v_hf_CL="82.45,1.48,82.40,82.43,82.99,80.71,'
           '04:59:59,81.25,81.27,0,5,4,2026-08-15,纽约原油";')
    q = oil._parse_hf_quotes(raw)
    assert q["OIL"]["price"] == 88.83
    assert q["OIL"]["change_pct"] == 2.02
    assert q["CL"]["name"] == "纽约原油"


def test_parse_daily_kline():
    raw = 'var t=([{"d":"2026-08-14","o":"88.0","h":"89.0","l":"87.5","c":"88.6","v":"1","p":"2","s":"88.0"},' \
          '{"date":"2026-08-15","open":"88.6","high":"89.2","low":"88.1","close":"88.8","volume":"2"}]);'
    pts = oil._parse_daily_kline(raw)
    assert pts == [{"date": "2026-08-14", "v": 88.6}, {"date": "2026-08-15", "v": 88.8}]


def test_eia_bulk_parse_from_fixture(tmp_path, monkeypatch):
    """bulk 解析：临时 zip 内嵌两条目标系列 + 一条干扰行；p_stocks 保留 6 年。"""
    import zipfile
    lines = [
        {"series_id": "PET.OTHER.W", "name": "x", "data": [["20260101", 1]]},
        {"series_id": "PET.WCESTUS1.W", "name": "stocks",
         "data": [["20260807", 424410], ["20260731", 406987]]},
        {"series_id": "PET.WCRSTUS1.W", "name": "total",
         "data": [["20260807", 723104], ["20260731", 704500]]},
    ]
    import json as _json
    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PET.txt", "\n".join(_json.dumps(x) for x in lines))
    zip_path = tmp_path / "pet.zip"
    zip_path.write_bytes(buf.getvalue())
    monkeypatch.setattr(oil, "_EIA_BULK_ZIP", str(zip_path))
    oil._BULK_CACHE.clear()
    got = oil._parse_eia_bulk()
    assert got["p_stocks"] == [("2026-07-31", 406987.0), ("2026-08-07", 424410.0)]


def test_eia_bulk_keeps_6y_for_stocks(tmp_path):
    """p_stocks 保留 312 点（6 年），其余 260 点（5 年）。"""
    import zipfile
    import json as _json
    import io as _io
    from datetime import date, timedelta

    def period(i: int) -> str:
        return (date(2020, 1, 3) + timedelta(weeks=i)).strftime("%Y%m%d")

    data = [[period(i), 1000 + i] for i in range(320)]
    row = {"series_id": "PET.WCESTUS1.W", "name": "stocks", "data": data}
    row2 = {"series_id": "PET.WCRFPUS2.W", "name": "prod", "data": data}
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PET.txt", _json.dumps(row) + "\n" + _json.dumps(row2))
    zip_path = tmp_path / "pet.zip"
    zip_path.write_bytes(buf.getvalue())
    import oil as _oil
    orig = _oil._EIA_BULK_ZIP
    _oil._EIA_BULK_ZIP = str(zip_path)
    _oil._BULK_CACHE.clear()
    try:
        got = _oil._parse_eia_bulk()
        assert len(got["p_stocks"]) == 312
        assert len(got["p_prod"]) == 260
    finally:
        _oil._EIA_BULK_ZIP = orig
        _oil._BULK_CACHE.clear()


def test_week_dist_wraps_year_end():
    assert oil._week_dist("2025-01-03", "2024-12-27") <= 1  # 跨年同周
    assert oil._week_dist("2026-08-04", "2026-08-07") == 0  # 同一周
    assert oil._week_dist("2026-01-01", "2026-07-01") > 1


def test_usd_index_formula_shape():
    idx = oil._usd_index_series()
    if idx:  # 网络可用时校验量级与有限性
        vals = [v for _, v in idx]
        assert all(80 < v < 150 for v in vals)
        assert all(math.isfinite(v) for v in vals)
        assert all(idx[i][0] < idx[i + 1][0] for i in range(len(idx) - 1))


def test_merge_dim_score_signals_in_0_100():
    a = [(f"2026-01-{i:02d}", float(i)) for i in range(1, 26)]
    b = [(f"2026-01-{i:02d}", -float(i)) for i in range(1, 26)]
    merged = oil._merge_dim_score_signals([(a, 0.7), (b, 0.3)])
    assert all(0 <= v <= 100 for _, v in merged)
    # 最后一点：a 上升 → 高分；b 下降 → 低分；0.7/0.3 加权后应偏高
    assert merged[-1][1] > 60


def test_last_value_on_or_before():
    m = {"2026-01-01": 1.0, "2026-01-05": 5.0}
    assert oil._last_value_on_or_before(m, "2026-01-03") == 1.0
    assert oil._last_value_on_or_before(m, "2026-01-07") == 5.0
    assert oil._last_value_on_or_before(m, "2025-12-31") is None


def test_eia_bulk_cache_cleared_between_parses(tmp_path, monkeypatch):
    """重解新 zip 前必须清 _BULK_CACHE，否则旧值驻留进程。"""
    import zipfile
    import json as _json
    import io as _io

    def make_zip(rows):
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("PET.txt", "\n".join(_json.dumps(x) for x in rows))
        return buf.getvalue()

    zip_path = tmp_path / "pet.zip"
    zip_path.write_bytes(make_zip(
        [{"series_id": "PET.WCESTUS1.W", "name": "stocks",
          "data": [["20260731", 406987], ["20260807", 424410]]}]))
    monkeypatch.setattr(oil, "_EIA_BULK_ZIP", str(zip_path))
    oil._BULK_CACHE.clear()
    first = oil._parse_eia_bulk()
    assert first["p_stocks"][-1] == ("2026-08-07", 424410.0)

    # 新 zip 周度更新后，重新解析必须拿到新值
    zip_path.write_bytes(make_zip(
        [{"series_id": "PET.WCESTUS1.W", "name": "stocks",
          "data": [["20260807", 424410], ["20260814", 430000]]}]))
    second = oil._parse_eia_bulk()
    assert second["p_stocks"][-1] == ("2026-08-14", 430000.0)


def test_structure_tail_alignment(tmp_path):
    """Brent/WTI 交易日错位时按末位对齐，不允许空输出。"""
    brent = [(f"2026-08-{10 + i:02d}", 85.0 + i) for i in range(5)]  # 08-10..14
    wti = [(f"2026-08-{9 + i:02d}", 79.0 + i) for i in range(5)]     # 08-09..13（错一日）
    spr = [("2026-08-07", 298694.0)]
    dos = [("2026-08-07", 24.7)]
    st = oil._build_structure(brent, wti, {"stale": False}, spr, dos)
    assert len(st["brent_wti"]) == 5
    # 末位配对：各取最后一根，价差 = Brent末 - WTI末
    assert st["brent_wti"][-1]["v"] == round(89.0 - 83.0, 2)


# ---------------------------------------------------------------------------
# WTI 暗盘（Hyperliquid xyz:CL 永续，7×24）
# ---------------------------------------------------------------------------

# 1789315200000 = 北京 2026-09-14 00:00；前一根 1789315140000 = 前一日 23:59
_HL_PREV = _json.dumps([{
    "t": 1789315140000, "T": 1789315199999, "s": "xyz:CL", "i": "1m",
    "o": "96.70", "c": "96.794", "h": "96.80", "l": "96.65", "v": "120", "n": 10,
}]).encode()
_HL_DAY = _json.dumps([
    {"t": 1789315200000, "T": 1789315259999, "s": "xyz:CL", "i": "1m",
     "o": "96.806", "c": "96.758", "h": "96.85", "l": "96.48", "v": "1000", "n": 50},
    {"t": 1789315260000, "T": 1789315319999, "s": "xyz:CL", "i": "1m",
     "o": "96.758", "c": "97.100", "h": "97.20", "l": "96.70", "v": "1500", "n": 60},
]).encode()
# metaAndAssetCtxs 返回 [meta, ctxs]；prevDayPx 故意与「午夜前最后一根」不同，
# 用于确认昨收优先取北京午夜口径而非 UTC 日界
_HL_CTX = _json.dumps([
    {"universe": [{"name": "xyz:CL", "maxLeverage": 20}]},
    [{"midPx": "99.092", "markPx": "99.122", "oraclePx": "99.453", "prevDayPx": "95.758",
      "dayBaseVlm": "1836486.8", "dayNtlVlm": "179207899.6", "openInterest": "2432722.4",
      "funding": "-0.0001889771"}],
]).encode()


def test_parse_hl_candles():
    """Hyperliquid candleSnapshot → 北京时钟分时点；收盘为 price，成交量为单根量。"""
    pts = oil._parse_hl_candles(_HL_DAY)
    assert len(pts) == 2
    assert pts[0]["time"] == "00:00" and math.isclose(pts[0]["price"], 96.758)
    assert pts[0]["ot"] == 1789315200000
    assert math.isclose(pts[0]["open"], 96.806) and math.isclose(pts[0]["high"], 96.85)
    assert math.isclose(pts[0]["low"], 96.48) and math.isclose(pts[0]["volume"], 1000.0)
    assert pts[1]["time"] == "00:01" and math.isclose(pts[1]["price"], 97.100)
    # 乱序输入按 openTime 升序归一（跨日：前一日 23:59 → 当日 00:00 → 00:01）
    merged = _json.dumps(_json.loads(_HL_PREV) + _json.loads(_HL_DAY)).encode()
    assert [p["time"] for p in oil._parse_hl_candles(merged)] == ["23:59", "00:00", "00:01"]
    # 非法/空输入 → []
    assert oil._parse_hl_candles(b"") == []
    assert oil._parse_hl_candles(b"[]") == []
    assert oil._parse_hl_candles(b'{"error":"x"}') == []


def test_wti_hyper_spot_aligns_and_accumulates(monkeypatch):
    """昨收取北京午夜前最后一根；末点用实时中间价覆盖；成交量累加为当日累计。"""
    calls: list[str] = []
    monkeypatch.setattr(oil, "_WTI_CACHE", {})
    monkeypatch.setattr(oil, "_WTI_CHART_CACHE", {})

    def fake_post(payload: dict, timeout: int = 15) -> bytes:
        kind = payload.get("type")
        calls.append(kind)
        if kind == "candleSnapshot":
            return _HL_PREV if payload["req"]["endTime"] < 1789315200000 else _HL_DAY
        if kind == "metaAndAssetCtxs":
            return _HL_CTX
        return b""

    monkeypatch.setattr(oil, "_hl_post", fake_post)

    d = oil.wti_hyper_spot()
    assert d["price"] == 99.092                    # midPx 优先
    assert d["prev_close"] == 96.794               # 北京午夜前最后一根，而非 prevDayPx
    assert d["change"] == 2.3
    # 涨跌幅口径与黄金页一致：用取整后的涨跌额 / 昨收，保证两页数字可比
    assert d["change_pct"] == 2.38
    assert d["open"] == 96.806                     # 当日首根开盘
    assert d["high"] == 97.2 and d["low"] == 96.48
    assert d["funding_annual"] == -165.54          # 小时费率 × 24 × 365
    assert d["open_interest"] == 2432722.4

    minute = d["minute"]
    assert minute["market_minutes"] == [[0, 1440]]  # 7×24：x 轴铺满当日
    pts = minute["points"]
    assert [p["volume"] for p in pts] == [1000.0, 2500.0]   # 累计量（MinuteChart 差分画柱）
    assert pts[-1]["price"] == 99.092                        # 图尾与卡片同价

    # 20 秒 TTL 内命中内存缓存，不再打上游
    before = len(calls)
    assert oil.wti_hyper_spot()["price"] == 99.092
    assert len(calls) == before


def test_wti_hyper_spot_stale_fallback(monkeypatch):
    """上游全挂时回退最近一次成功结果并打 stale 标，不抛异常。"""
    monkeypatch.setattr(oil, "_WTI_CACHE", {"wti": (0.0, {
        "name": "WTI 暗盘（Hyperliquid 永续）", "price": 99.0, "minute": None,
    })})
    monkeypatch.setattr(oil, "_WTI_CHART_CACHE", {})
    monkeypatch.setattr(oil, "_hl_post", lambda payload, timeout=15: b"")

    d = oil.wti_hyper_spot()
    assert d["price"] == 99.0 and d["stale"] is True


def test_wti_hyper_spot_empty_when_no_history(monkeypatch):
    """无缓存且上游不可用 → 返回空骨架（前端据此显示「暂不可用」）。"""
    monkeypatch.setattr(oil, "_WTI_CACHE", {})
    monkeypatch.setattr(oil, "_WTI_CHART_CACHE", {})
    monkeypatch.setattr(oil, "_hl_post", lambda payload, timeout=15: b"")

    d = oil.wti_hyper_spot()
    assert d["price"] is None and d["minute"] is None and d["stale"] is True


def test_futures_daily_history_batches_three_symbols(monkeypatch):
    """三个外盘品种日K：各按天数截取末尾；个别品种取数失败只让该条为空，不拖累其余。"""
    def fake_kline(sym: str, cn: bool = False) -> list[dict]:
        if sym == "CL":
            return []
        return [{"date": f"2026-01-{i % 28 + 1:02d}", "v": float(i)} for i in range(300)]

    monkeypatch.setattr(oil, "_daily_kline", fake_kline)
    d = oil.futures_daily_history(days=250)

    assert set(d["series"]) == {"brent", "wti", "ng"}
    assert d["series"]["brent"]["symbol"] == "OIL"
    assert d["series"]["ng"]["symbol"] == "NG"
    # 截取末尾 250 根（保留最新，不是最早）
    assert len(d["series"]["brent"]["points"]) == 250
    assert d["series"]["brent"]["points"][-1]["v"] == 299.0
    assert d["series"]["brent"]["stale"] is False
    # 单品种失败 → 该条空 + stale，其余不受影响
    assert d["series"]["wti"]["points"] == [] and d["series"]["wti"]["stale"] is True
    assert d["series"]["ng"]["stale"] is False
