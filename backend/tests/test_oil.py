"""油价评分层纯函数测试（不触网）。"""

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
