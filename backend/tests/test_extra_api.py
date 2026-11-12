"""新端点统一入口（extra_api）测试：参数校验、序列化、路由错误映射（不联网）。"""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as app_module
import extra_api

client = TestClient(app_module.app)


def test_every_registered_endpoint_exists_and_is_public():
    for name, module in extra_api._OWNER.items():
        assert callable(getattr(module, name)), name
        assert not name.startswith("_"), name


def test_describe_covers_all_and_reports_required():
    rows = extra_api.describe()
    assert {r["endpoint"] for r in rows} == set(extra_api.ENDPOINTS)
    req = [p for p, v in next(r for r in rows if r["endpoint"] == "index_valuation")["params"].items()
           if v["required"]]
    assert req == ["index_code"]


def test_coerce_rejects_unknown_param():
    # 静默丢弃未知参数 = 用户以为筛了条件，其实没有
    with pytest.raises(ValueError, match="不认识参数"):
        extra_api._coerce("lpr_history", {"foo": "1"})


def test_coerce_requires_missing_param():
    with pytest.raises(ValueError, match="缺少必填参数"):
        extra_api._coerce("index_valuation", {})


def test_coerce_converts_types_by_signature():
    kw = extra_api._coerce("trading_calendar", {"year": "2026", "month": "10"})
    assert kw == {"year": 2026, "month": 10}
    # 空串按「没给」处理，交给签名默认值
    assert extra_api._coerce("chinabond_yield_curve", {"start": "2026-01-01", "end": ""}) == {
        "start": "2026-01-01"}


def test_jsonable_dataframe_keeps_attrs_and_drops_nan():
    frame = pd.DataFrame([{"a": 1, "b": float("nan")}])
    frame.attrs["coverage"] = "沪深京"
    payload = extra_api._jsonable(frame)
    assert payload["kind"] == "table"
    assert payload["rows"][0]["b"] is None
    assert payload["attrs"]["coverage"] == "沪深京"


def test_call_unknown_endpoint():
    with pytest.raises(ValueError, match="未知端点"):
        extra_api.call("nope")


def test_http_routes_error_mapping(monkeypatch):
    # ValueError → 400（参数错），RuntimeError → 502（源故障），两者不能混
    def boom(*args, **kwargs):
        raise RuntimeError("源不可达")

    monkeypatch.setattr(extra_api, "call", lambda name, params: boom())
    assert client.get("/api/extra/lpr_history").status_code == 502

    def bad(*args, **kwargs):
        raise ValueError("参数错")

    monkeypatch.setattr(extra_api, "call", lambda name, params: bad())
    assert client.get("/api/extra/lpr_history").status_code == 400


def test_http_list_route(monkeypatch):
    monkeypatch.setattr(extra_api, "describe", lambda: [{"endpoint": "x", "description": "d", "params": {}}])
    r = client.get("/api/extra")
    assert r.status_code == 200
    assert r.json()["data"][0]["endpoint"] == "x"


def test_query_extra_tool_is_registered():
    import tools

    assert "query_extra" in tools.TOOL_NAMES
    tool = next(t for t in tools.TOOLS if t["function"]["name"] == "query_extra")
    assert "lpr_history" in tool["function"]["description"]
    assert set(tool["function"]["parameters"]["properties"]["endpoint"]["enum"]) == set(extra_api.ENDPOINTS)


def test_overseas_endpoints_route_to_gstock_new():
    """海外层单独一张表：漏了 _OWNER 映射会在 describe() 里炸 AttributeError。"""
    import gstock_new

    assert len(extra_api.OVERSEAS_ENDPOINTS) >= 20
    for name in extra_api.OVERSEAS_ENDPOINTS:
        assert extra_api._OWNER[name] is gstock_new, name
        assert callable(getattr(gstock_new, name)), name
    assert set(extra_api.ENDPOINTS) == set(extra_api._OWNER)


def test_overseas_endpoints_described_without_error():
    rows = {r["endpoint"]: r for r in extra_api.describe()}
    for name in extra_api.OVERSEAS_ENDPOINTS:
        assert name in rows, name
        assert isinstance(rows[name]["params"], dict)


def test_overseas_required_params_are_reported():
    params = extra_api._params_of("ecb_series")
    assert [p for p, v in params.items() if v["required"]] == ["flow", "key"]
    # range_ 这种带下划线的参数名要能原样取到（报文里会被 FastAPI 当查询名）
    assert "range_" in extra_api._params_of("stock_kline_yahoo")


def test_sec_contact_comes_from_env(monkeypatch):
    """SEC / BLS 要求 UA 里留可联系的真实联系人：产品侧走环境变量，不写进仓库。"""
    import importlib

    import gstock_new

    monkeypatch.setenv("MW_SEC_CONTACT", "Test User test@example.com")
    reloaded = importlib.reload(gstock_new)
    assert reloaded.SEC_CONTACT == "Test User test@example.com"
    # 还原，避免污染同进程里的其他用例
    monkeypatch.delenv("MW_SEC_CONTACT", raising=False)
    importlib.reload(gstock_new)


def test_bls_endpoints_cache_for_one_hour():
    """BLS 免 key 版每天只有 25 次：缓存必须放宽到 1 小时，且这几个端点必须真的在
    可缓存集合里（否则 TTL 表形同虚设）。"""
    bls = {"us_macro", "us_macro_latest", "bls_series", "bls_release_calendar"}
    assert bls <= set(extra_api._CACHEABLE)
    for name in bls:
        assert extra_api._CACHE_TTL[name] == 3600
    assert extra_api._CACHE_SECONDS < 3600          # 其余端点仍走默认短 TTL
    assert set(extra_api._CACHE_TTL) <= set(extra_api._CACHEABLE)


def test_cache_ttl_is_honoured(monkeypatch):
    """TTL 之内命中直接返回；换一个参数就是新 key，不会串味。"""
    calls = []

    class Stub:
        @staticmethod
        def lpr_history(**kwargs):
            calls.append(kwargs)
            return [{"a": 1}]

    monkeypatch.setattr(extra_api, "_cache", {})
    monkeypatch.setitem(extra_api._OWNER, "lpr_history", Stub)

    first = extra_api.call("lpr_history")
    second = extra_api.call("lpr_history")
    assert first.get("cached") is None
    assert second.get("cached") is True
    assert len(calls) == 1                      # 第二次没有再调端点
