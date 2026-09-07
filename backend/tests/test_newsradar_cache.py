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


def test_dedup_recurring_title_refreshes_baseline():
    """同名栏目窗口外合法复现后，标题基准必须跟着最近保留的那条走：
    倒序遍历 20h/70h/100h 前的三条同名——70h 那条是另一期（保留），
    100h 那条与 70h 只差 30h（窗内转载，应删）；若基准停在 20h 就会漏删。"""
    H = 3600
    items = [
        {"title": "每周综述", "url": "https://a.com/1", "ts": 1000 * H - 20 * H},
        {"title": "每周综述", "url": "https://b.com/2", "ts": 1000 * H - 70 * H},
        {"title": "每周综述", "url": "https://c.com/3", "ts": 1000 * H - 100 * H},
    ]
    out = newsradar._dedup(items)
    assert [it["url"] for it in out] == ["https://a.com/1", "https://b.com/2"]


def test_dedup_no_timestamp_keeps_both():
    """任一方无发布时间就不做标题去重：多显示一条只是冗余，判错删掉就是丢一篇。"""
    items = [
        {"title": "同名栏目", "url": "https://a.com/1", "ts": 1000},
        {"title": "同名栏目", "url": "https://b.com/2", "ts": 0},
    ]
    out = newsradar._dedup(items)
    assert len(out) == 2


def test_normalize_url_reencodes_query():
    """query 值里的转义分隔符必须重新转义：否则 ?id=a%26b%3Dc 与 ?id=a&b=c
    归一化成同一个 key，两篇不同文章被误合并（去重误删）。"""
    a = newsradar._normalize_url("https://x.com/p?id=a%26b%3Dc")
    b = newsradar._normalize_url("https://x.com/p?id=a&b=c")
    assert a != b
    # 跟踪参数照剥、真实参数保留
    c = newsradar._normalize_url("https://x.com/p?utm_source=rss&id=1")
    assert c == "https://x.com/p?id=1"


def test_dedup_same_url_across_sources_keeps_newest():
    """同 URL 只留一条且保留最新（倒序传入，第一条即最新）。"""
    items = [
        {"title": "新闻A", "url": "https://a.com/x?utm_source=rss#frag", "ts": 2000},
        {"title": "标题变了", "url": "https://a.com/x", "ts": 1000},
    ]
    out = newsradar._dedup(items)
    assert len(out) == 1
    assert out[0]["ts"] == 2000
