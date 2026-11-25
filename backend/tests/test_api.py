"""API 验证/契约测（FastAPI TestClient）。大多在校验层就返回，不联网、可靠。"""
import pytest
from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.parametrize("path", [
    "/api/quote?codes=abc",
    "/api/valuation?code=12",
    "/api/margin?code=notcode",
    "/api/holders?code=1234567",
    "/api/announcements?code=",
])
def test_bad_code_400(path):
    assert client.get(path).status_code == 400


def test_index_history_rejects_unknown_symbol():
    assert client.get("/api/indices/history?symbol=sh999999").status_code == 404


def test_industry_top_range():
    assert client.get("/api/industry?top=2").status_code == 422   # ge=5
    assert client.get("/api/industry?top=999").status_code == 422  # le=50


def test_public_news_search_validation():
    assert client.get("/api/public-news-search?q=x").status_code == 422
    assert client.get("/api/public-news-search?q=major&count=9").status_code == 422


def test_chat_requires_login():
    # 对话端点会调本机 CLI 订阅 / 任意 OpenAI 兼容端点，不能匿名调用
    r = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
        "llm": {"model": "x", "baseURL": "http://x", "apiKey": "k"},
    })
    assert r.status_code == 401


def test_chat_empty_messages_400(logged_in):
    r = client.post("/api/chat", headers=logged_in, json={"messages": [], "llm": {"model": "x", "baseURL": "http://x", "apiKey": "k"}})
    assert r.status_code == 400


def test_chat_api_missing_key_400(logged_in):
    # API 接入缺 baseURL/apiKey → 400（在开流前拦下）
    r = client.post("/api/chat", headers=logged_in, json={
        "messages": [{"role": "user", "content": "hi"}],
        "llm": {"provider": "deepseek", "model": "deepseek-chat", "baseURL": "", "apiKey": ""},
    })
    assert r.status_code == 400


def test_chat_cli_not_installed_400(logged_in):
    # 订阅接入选一个本机没装的 CLI → 400 明确提示（不静默失败）
    r = client.post("/api/chat", headers=logged_in, json={
        "messages": [{"role": "user", "content": "hi"}],
        "llm": {"provider": "cli-qwen", "model": "qwen-code", "baseURL": "", "apiKey": ""},
    })
    # qwen 以全自动模式运行、默认关闭：无论本机装没装都应被挡下（403 说明原因；未装时 400）
    assert r.status_code in (400, 403)


def test_chat_auto_agent_cli_disabled_by_default(monkeypatch):
    # 即使本机装了 qwen（这里伪造二进制存在），默认也不放行全自动 agent
    import cli_runtime
    monkeypatch.setattr(cli_runtime, "_find_bin", lambda name: f"/usr/local/bin/fake-{name}")
    r = client.post("/api/chat", headers=__import__("conftest").auth_headers(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "llm": {"provider": "cli-qwen", "model": "qwen-code"},
    })
    assert r.status_code == 403 and "MW_CLI_AGENTS" in r.json()["detail"]


def test_global_stock_404(monkeypatch):
    """无法解析的美股/港股代码 → 404（不 500、不崩）。"""
    import gstock
    monkeypatch.setattr(gstock, "us_hk_stock", lambda q: {})
    assert client.get("/api/global/stock?symbol=ZZZZ").status_code == 404


def test_gstock_quote_full_null_shape():
    """行情取不到时 `_quote_from({})` 仍返回完整 null 形状（契合 GlobalQuote 类型），不是空 dict。"""
    import gstock
    q = gstock._quote_from({})
    assert set(q) == {"code", "name", "price", "open", "high", "low", "prev_close", "amount", "mcap", "change_pct"}
    assert all(v is None for v in q.values())
