import json

import stock_cache


def test_stock_cache_keeps_only_twenty_recent_symbols(monkeypatch, tmp_path):
    target = tmp_path / "stocks.json"
    monkeypatch.setattr(stock_cache, "CACHE_FILE", str(target))
    for index in range(22):
        stock_cache.save("valuation", f"{index:06d}", {"price": index})

    payload = json.loads(target.read_text())
    assert len(payload["stocks"]) == 20
    assert len(payload["entries"]) == 20
    assert stock_cache.warm("valuation", "000021")[1] == {"price": 21}
    assert stock_cache.warm("valuation", "000000") is None
