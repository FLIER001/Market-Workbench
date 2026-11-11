"""MCP股票工具回归：兼容默认输出，扩展参数不吞错，明细按需返回。"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import astock
import mcp_server
import tools


def response(data):
    return SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)


def test_new_optional_parameters_are_exposed():
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools.TOOLS}
    assert {"start_date", "end_date", "page", "limit", "include_metadata"} <= schemas["query_announcements"]["properties"].keys()
    assert {"trade_date", "forward_days"} <= schemas["query_lockup"]["properties"].keys()
    assert schemas["query_kline"]["properties"]["detail_count"]["maximum"] == 250
    assert schemas["query_announcements"]["required"] == ["code"]


def test_announcement_tool_retains_url_and_forwards_metadata_options(monkeypatch):
    seen = []
    row = {"title": "公告", "date": "2026-10-23", "type": "其他", "url": "https://example.test/ann"}
    def fetch(code, **kwargs):
        seen.append((code, kwargs))
        return {"records": [row], "coverage": {"total": 1}} if kwargs["include_metadata"] else [row]
    monkeypatch.setattr(astock, "announcements", fetch)
    assert tools.exec_tool("query_announcements", {"code": "600519"}) == [row]
    data = tools.exec_tool("query_announcements", {
        "code": "600519", "start_date": "2026-10-01", "end_date": "2026-10-24",
        "page": 2, "limit": 100, "include_metadata": True})
    assert data["records"][0]["url"] == row["url"]
    assert seen[-1] == ("600519", {"start_date": "2026-10-01", "end_date": "2026-10-24",
                                      "page": 2, "limit": 100, "include_metadata": True})


def test_http_lockup_rejects_old_persisted_cache_shape(monkeypatch):
    import app
    current = {"history": [], "upcoming": [], "scheduled": [], "coverage": {"complete": True}}
    monkeypatch.setattr(astock, "lockup_expiry", lambda code: current)
    def cached(endpoint, code, ttl, fetch, **kwargs):
        assert endpoint == "lockup"
        assert not kwargs["valid"]({"history": [], "upcoming": []})
        assert kwargs["valid"](current)
        return fetch()
    monkeypatch.setattr(app, "_stock_cached", cached)
    assert app.lockup(code="600519") == {"data": current}


def test_lockup_tool_forwards_calendar_window(monkeypatch):
    monkeypatch.setattr(astock, "lockup_expiry", lambda code, **kwargs: {"code": code, **kwargs})
    data = tools.exec_tool("query_lockup", {"code": "688981", "trade_date": "2026-10-24", "forward_days": 0})
    assert data == {"code": "688981", "trade_date": "2026-10-24", "forward_days": 0}


def test_mcp_protocol_marks_source_failure_as_is_error(monkeypatch):
    def fail(*args, **kwargs):
        raise astock.DataSourceError("9501字段错误")
    monkeypatch.setattr(astock, "lockup_expiry", fail)
    sent = []
    monkeypatch.setattr(mcp_server, "_send", sent.append)
    mcp_server._handle({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                        "params": {"name": "query_lockup", "arguments": {"code": "600519"}}})
    assert sent[0]["id"] == 12
    assert sent[0]["result"]["isError"] is True
    assert "9501" in sent[0]["result"]["content"][0]["text"]


def test_news_retains_original_link_and_bounded_summary(monkeypatch):
    row = {"新闻标题": "公告进展", "发布时间": "2026-10-23", "文章来源": "媒体",
           "新闻链接": "https://example.test/news", "新闻内容": "摘" * 500}
    monkeypatch.setattr(astock, "stock_news", lambda code, limit: [row] * 20)
    result = tools.exec_tool("query_news", {"code": "600519"})
    assert len(result) == 15
    assert result[0]["新闻链接"] == row["新闻链接"]
    assert len(result[0]["新闻内容"]) == 300


@pytest.mark.parametrize("raw_date", [None, "", "not-a-date", "2026-13-01"])
def test_lockup_invalid_source_dates_are_errors(monkeypatch, raw_date):
    monkeypatch.setattr(astock, "eastmoney_datacenter", lambda *args, **kwargs: [{"FREE_DATE": raw_date}])
    with pytest.raises(astock.DataSourceError):
        astock.lockup_expiry("600519")


def test_kline_default_is_still_thirty_but_can_return_full_detail(monkeypatch):
    rows = [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
             "open": i + 1, "close": i + 1, "high": i + 2, "low": i + 0.5, "volume": 10}
            for i in range(250)]
    monkeypatch.setattr(tools, "_kline_tencent", lambda code, period, n: rows[-n:])
    default = tools.exec_tool("query_kline", {"code": "600519", "count": 250})
    assert len(default["recent"]) == 30
    assert default["coverage"]["complete"] is False
    full = tools.exec_tool("query_kline", {"code": "600519", "count": 250, "detail_count": 250})
    assert len(full["recent"]) == 250
    assert full["coverage"] == {"requested_bars": 250, "available_bars": 250,
                                 "returned_bars": 250, "detail_limit": 250, "complete": True}
    short = tools.exec_tool("query_kline", {"code": "600519", "count": 10, "detail_count": 250})
    assert len(short["recent"]) == 10
    assert short["summary"]["bars"] == 10


@pytest.mark.parametrize("invalid", [0, 251, True, 1.5, "250", None])
def test_kline_rejects_bad_detail_count_before_fetch(monkeypatch, invalid):
    calls = []
    monkeypatch.setattr(tools, "_kline_tencent", lambda *args: calls.append(args))
    result = tools.exec_tool("query_kline", {"code": "600519", "detail_count": invalid})
    assert "detail_count" in result["error"]
    assert calls == []


@pytest.mark.parametrize("payload", [{}, {"success": True, "result": None},
                                      {"success": True, "result": {}},
                                      {"success": True, "result": {"data": None}},
                                      {"success": True, "result": {"data": ["bad-row"]}}])
def test_strict_datacenter_missing_shape_is_not_empty(monkeypatch, payload):
    monkeypatch.setattr(astock, "em_get", lambda *args, **kwargs: response(payload))
    with pytest.raises(astock.DataSourceError):
        astock.eastmoney_datacenter("RPT_LIFT_STAGE", strict=True)


@pytest.mark.parametrize("payload", [{"success": 0, "data": {"list": [], "total_hits": 0}},
                                      {"error": "限流", "data": {"list": []}},
                                      {"data": {}}, {"data": {"total_hits": 2}},
                                      {"data": {"list": [{"notice_date": "bad", "title": "公告"}]}}])
def test_announcement_failure_or_malformed_rows_are_not_empty(monkeypatch, payload):
    import requests
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(payload))
    with pytest.raises(astock.DataSourceError):
        astock.announcements("600519")


def test_announcement_rejects_upstream_ignoring_window(monkeypatch):
    import requests
    payload = {"data": {"list": [{"notice_date": "2026-08-15", "title": "旧公告"}], "total_hits": 1}}
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response(payload))
    with pytest.raises(astock.DataSourceError):
        astock.announcements("600519", start_date="2026-10-01", end_date="2026-10-24", include_metadata=True)


@pytest.mark.parametrize("kwargs", [{"limit": 1.5}, {"limit": True}, {"page": 2.2},
                                     {"include_metadata": "false"}, {"code": "６００５１９"}])
def test_announcement_non_integer_or_boolean_controls_rejected(monkeypatch, kwargs):
    import requests
    calls = []
    monkeypatch.setattr(requests, "get", lambda *args, **kw: calls.append(kw))
    with pytest.raises(ValueError):
        astock.announcements(**{"code": "600519", **kwargs})
    assert calls == []
