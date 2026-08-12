import json

import cache_runtime
import newsradar


def test_failed_rss_source_keeps_its_last_good_items(monkeypatch, tmp_path):
    config = {
        "fetch": {"recent_days": 7, "per_source": 6},
        "redline_keywords": [],
        "industries": [{"key": "ai", "name": "AI", "accent": "x"}],
        "sources": [
            {"hint": "ai", "name": "good", "url": "https://good"},
            {"hint": "ai", "name": "down", "url": "https://down"},
        ],
    }
    sources = tmp_path / "sources.json"
    cache = tmp_path / "radar.json"
    sources.write_text(json.dumps(config), encoding="utf-8")
    cache.write_text(json.dumps({
        "industries": [{"key": "ai", "items": [{"source": "down", "title": "old", "ts": 1}]}]
    }), encoding="utf-8")
    monkeypatch.setattr(newsradar, "SOURCES_FILE", str(sources))
    monkeypatch.setattr(newsradar, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(newsradar, "CACHE_FILE", str(cache))
    monkeypatch.setattr(newsradar, "LEGACY_CACHE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(newsradar, "_fetch_source", lambda src, *_: None if src["name"] == "down" else [])
    cache_runtime.reset_for_tests()

    result = newsradar.fetch_radar()

    assert result["stats"]["failed_sources"] == 1
    assert result["stats"]["stale_sources"] == ["down"]
    assert result["industries"][0]["items"][0]["title"] == "old"
