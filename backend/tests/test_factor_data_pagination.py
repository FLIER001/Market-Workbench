"""股票池完整分页与构建审计；不访问真实接口。"""
import json

import pandas as pd
import pytest

import factor_data


class Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return {"data": self.data}


def rows(start, count, name="正常股份"):
    return [{"f12": f"{600000+i:06d}", "f14": name, "f21": 100-i, "f13": 1}
            for i in range(start, start + count)]


@pytest.mark.parametrize("total,actual_size", [(2300, 100), (125, 20)])
def test_uses_actual_unique_count_and_can_fetch_more_than_twenty_pages(monkeypatch, total, actual_size):
    pages = []

    def fetch(_url, params, **_kwargs):
        page = int(params["pn"])
        pages.append(page)
        assert params["pz"] == "100"
        assert params["fid"] == "f12" and params["po"] == "0"
        start = (page - 1) * actual_size
        return Response({"total": total, "diff": rows(start, min(actual_size, total - start))})

    monkeypatch.setattr(factor_data.astock, "em_get", fetch)
    result = factor_data.fetch_instruments()
    assert len(result) == total
    assert len({item["code"] for item in result}) == total
    assert pages == list(range(1, (total + actual_size - 1) // actual_size + 1))


def test_filters_names_only_after_counting_complete_source(monkeypatch):
    pages = []

    def fetch(_url, params, **_kwargs):
        page = int(params["pn"])
        pages.append(page)
        return Response({"total": 6, "diff": rows((page-1)*2, 2, "ST股份" if page < 3 else "正常股份")})

    monkeypatch.setattr(factor_data.astock, "em_get", fetch)
    result = factor_data.fetch_instruments()
    assert len(result) == 2
    assert pages == [1, 2, 3]


@pytest.mark.parametrize("mode", ["missing", "duplicate", "changed", "invalid"])
def test_incomplete_or_inconsistent_pages_are_not_accepted(monkeypatch, mode):
    def fetch(_url, params, **_kwargs):
        page = int(params["pn"])
        if page == 1:
            return Response({"total": 4, "diff": rows(0, 2)})
        if mode == "missing":
            return Response(None)
        if mode == "duplicate":
            return Response({"total": 4, "diff": rows(0, 2)})
        if mode == "changed":
            return Response({"total": 5, "diff": rows(2, 2)})
        return Response({"total": 4, "diff": [{"f12": "bad"}]})

    monkeypatch.setattr(factor_data.astock, "em_get", fetch)
    with pytest.raises(RuntimeError):
        factor_data.fetch_instruments()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(factor_data, "DATA_DIR", str(tmp_path))
    for field, filename in (("_BARS_FILE", "bars.csv.gz"), ("_INSTRUMENTS_FILE", "instruments.csv.gz"),
                            ("_CALENDAR_FILE", "calendar.json"), ("_CATALOG_FILE", "catalog.json")):
        monkeypatch.setattr(factor_data, field, str(tmp_path / filename))
    monkeypatch.setattr(factor_data, "_STATE", {
        "building": False, "pre_acquired": False, "fetched": 0, "failed": 0, "total": 0,
        "error": None, "started_at": None, "done_at": None,
    })
    return tmp_path


def test_failed_universe_fetch_preserves_old_files(store, monkeypatch):
    original = {"bars.csv.gz": b"old bars", "catalog.json": b'{"stocks":591}'}
    for name, content in original.items():
        (store / name).write_bytes(content)

    def fail():
        raise RuntimeError("股票池分页未完成")

    monkeypatch.setattr(factor_data, "fetch_instruments", fail)
    with pytest.raises(RuntimeError, match="分页未完成"):
        factor_data.build_dataset()
    assert factor_data.build_state()["building"] is False
    assert factor_data.build_state()["error"] == "股票池分页未完成"
    for name, content in original.items():
        assert (store / name).read_bytes() == content


def test_catalog_distinguishes_failures_from_quality_filtering(store, monkeypatch):
    instruments = [{"code": code, "name": code, "float_mcap": 1} for code in ("600001", "600002", "600003")]
    monkeypatch.setattr(factor_data, "fetch_instruments", lambda: instruments)
    dates = pd.bdate_range("2022-01-03", periods=600).strftime("%Y-%m-%d")
    bars = [{"date": d, "open": 10, "close": 10, "high": 11, "low": 9, "volume": 100, "amount": 1000} for d in dates]
    monkeypatch.setattr(factor_data, "fetch_stock_history", lambda code: bars if code == "600001" else bars[:100] if code == "600002" else [])
    monkeypatch.setattr(factor_data.astock, "index_daily_em", lambda *args, **kwargs: [{"date": "2022-01-03", "close": 3000}])
    result = factor_data.build_dataset(max_workers=1)
    assert {key: result[key] for key in ("stocks", "universe_stocks", "history_stocks", "failed_stocks", "filtered_stocks", "min_valid_bars")} == {
        "stocks": 1, "universe_stocks": 3, "history_stocks": 2, "failed_stocks": 1, "filtered_stocks": 1, "min_valid_bars": 500,
    }
    assert json.loads((store / "catalog.json").read_text())["failed_stocks"] == 1


def test_quality_filter_does_not_publish_an_empty_dataset(store, monkeypatch):
    (store / "catalog.json").write_text('{"stocks":591}')
    monkeypatch.setattr(factor_data, "fetch_instruments", lambda: [{"code": "600001", "name": "测试", "float_mcap": 1}])
    monkeypatch.setattr(factor_data, "fetch_stock_history", lambda _code: [
        {"date": "2026-01-02", "open": 10, "close": 10, "high": 11, "low": 9, "volume": 100, "amount": 1000}])
    with pytest.raises(RuntimeError, match="保留原数据"):
        factor_data.build_dataset(max_workers=1)
    assert (store / "catalog.json").read_text() == '{"stocks":591}'


def test_transient_failure_retries_same_page_without_skipping(monkeypatch):
    calls = []
    def fetch(_url, params, **kwargs):
        calls.append(int(params['pn']))
        if len(calls) in (2, 3):
            raise ConnectionError('transient')
        page = int(params['pn'])
        return Response({'total': 4, 'diff': rows((page - 1) * 2, 2)})
    monkeypatch.setattr(factor_data.astock, 'em_get', fetch)
    assert len(factor_data.fetch_instruments()) == 4
    assert calls == [1, 2, 2, 2]
