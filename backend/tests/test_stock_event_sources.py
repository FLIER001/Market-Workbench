"""公告 / 东财数据中心 / 解禁日历 数据层契约测（全部打桩，不联网）。

覆盖：
  - announcements：参数透传（begin_time/end_time/page_index/page_size）、默认列表形状与 url、
    入参校验、coverage 元数据（total/returned/has_more/complete）、错误与「空」的区分。
  - eastmoney_datacenter：strict=False 老行为（失败退化成 []）；strict=True 下网络/HTTP/JSON/
    上游报错/结构异常一律抛 DataSourceError，仅实测 code=9201 且无数据才算空。
  - lockup_expiry：历史/未来分桶与日期边界、错桶行排除、新单位字段（缺数为 None）、
    校验边界、上游错误不被吞成空。
"""
from datetime import datetime, timedelta

import pytest
import requests

import astock
from astock import DataSourceError

CODE = "600519"


# ---------------------------------------------------------------------------
# 打桩工具
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload=None, status=200, bad_json=False):
        self._payload = payload
        self.status_code = status
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error", response=self)

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def patch_ann_http(monkeypatch, response):
    """拦截 astock.announcements 内部的 requests.get，返回调用记录。"""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "headers": headers, "timeout": timeout})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def ann_payload(rows=(), total=None):
    data = {"list": list(rows)}
    if total is not None:
        data["total_hits"] = total
    return {"data": data}


def ann_row(art="AN2026010100001", title="关于回购股份的公告", date="2026-01-02 00:00:00"):
    return {"notice_date": date, "title": title, "art_code": art,
            "columns": [{"column_name": "重大事项"}]}


class DCResponse:
    def __init__(self, payload=None, status=200, bad_json=False):
        self._payload = payload
        self.status_code = status
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def patch_em_get(monkeypatch, response):
    """拦截 astock.em_get（东财数据中心底层入口）。"""
    calls = []

    def fake_em_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {}})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(astock, "em_get", fake_em_get)
    return calls


def datacenter_ok(rows):
    return DCResponse({"version": "x", "success": True, "code": 0, "message": "ok",
                       "result": {"pages": 1, "data": rows}})


# ---------------------------------------------------------------------------
# announcements —— 默认形状 / URL 参数
# ---------------------------------------------------------------------------

def test_announcements_default_call_keeps_list_shape_and_url(monkeypatch):
    calls = patch_ann_http(monkeypatch, FakeResponse(ann_payload([ann_row()], total=1)))
    out = astock.announcements(CODE)          # 老调用方式（tools.py / app.py 同款）
    assert isinstance(out, list) and len(out) == 1
    row = out[0]
    assert set(row) == {"date", "title", "type", "url"}
    assert row["url"] == f"https://data.eastmoney.com/notices/detail/{CODE}/AN2026010100001.html"
    params = calls[0]["params"]
    assert params["stock_list"] == CODE
    assert params["page_index"] == 1 and params["page_size"] == 15
    assert "begin_time" not in params and "end_time" not in params


def test_announcements_passes_dates_and_paging_to_upstream(monkeypatch):
    calls = patch_ann_http(monkeypatch, FakeResponse(ann_payload([ann_row()], total=7)))
    out = astock.announcements(CODE, limit=20, start_date="2026-01-01",
                               end_date="2026-01-31", page=3)
    assert isinstance(out, list)
    params = calls[0]["params"]
    assert params["begin_time"] == "2026-01-01" and params["end_time"] == "2026-01-31"
    assert params["page_index"] == 3 and params["page_size"] == 20
    assert calls[0]["url"] == astock._ANN_URL


def test_announcements_omits_absent_date_bounds(monkeypatch):
    calls = patch_ann_http(monkeypatch, FakeResponse(ann_payload([], total=0)))
    assert astock.announcements(CODE, start_date="2026-01-01") == []
    assert calls[0]["params"]["begin_time"] == "2026-01-01"
    assert "end_time" not in calls[0]["params"]


# ---------------------------------------------------------------------------
# announcements —— 入参校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"code": "60051"},
    {"code": "60051X"},
    {"code": ""},
    {"limit": 0},
    {"limit": 101},
    {"page": 0},
    {"start_date": "2026/01/01"},
    {"end_date": "2026-13-01"},
    {"start_date": "2026-02-01", "end_date": "2026-01-01"},
])
def test_announcements_validation_rejects_bad_input(monkeypatch, kwargs):
    calls = patch_ann_http(monkeypatch, FakeResponse(ann_payload([])))
    with pytest.raises(ValueError):
        astock.announcements(**{"code": CODE, **kwargs})
    assert calls == []      # 校验失败不发请求


# ---------------------------------------------------------------------------
# announcements —— 错误 vs 空
# ---------------------------------------------------------------------------

def test_announcements_http_error_raises(monkeypatch):
    patch_ann_http(monkeypatch, FakeResponse({}, status=500))
    with pytest.raises(DataSourceError):
        astock.announcements(CODE)


def test_announcements_network_error_raises(monkeypatch):
    patch_ann_http(monkeypatch, requests.ConnectionError("boom"))
    with pytest.raises(DataSourceError):
        astock.announcements(CODE)


def test_announcements_bad_json_raises(monkeypatch):
    patch_ann_http(monkeypatch, FakeResponse(bad_json=True))
    with pytest.raises(DataSourceError):
        astock.announcements(CODE)


@pytest.mark.parametrize("payload", [
    [],                                      # 顶层不是对象
    {"message": "系统繁忙"},                  # 缺 data
    {"data": []},                            # data 不是对象
    {"data": {"list": "oops"}},              # list 不是数组
    {"data": {"list": ["oops"]}},            # 条目不是对象
])
def test_announcements_invalid_payload_raises(monkeypatch, payload):
    patch_ann_http(monkeypatch, FakeResponse(payload))
    with pytest.raises(DataSourceError):
        astock.announcements(CODE)


def test_announcements_empty_result_is_not_an_error(monkeypatch):
    patch_ann_http(monkeypatch, FakeResponse(ann_payload([], total=0)))
    assert astock.announcements(CODE) == []
    meta = astock.announcements(CODE, start_date="2026-01-01", end_date="2026-01-31",
                                include_metadata=True)
    assert meta["records"] == [] and meta["coverage"]["total"] == 0


# ---------------------------------------------------------------------------
# announcements —— coverage 元数据
# ---------------------------------------------------------------------------

def test_announcements_metadata_complete_when_single_query_covers_interval(monkeypatch):
    rows = [ann_row(art=f"AN{i}") for i in range(3)]
    patch_ann_http(monkeypatch, FakeResponse(ann_payload(rows, total=3)))
    meta = astock.announcements(CODE, limit=10, start_date="2026-01-01",
                                end_date="2026-01-31", include_metadata=True)
    assert len(meta["records"]) == 3
    cov = meta["coverage"]
    assert cov == {"start_date": "2026-01-01", "end_date": "2026-01-31", "page": 1,
                   "page_size": 10, "total": 3, "returned": 3, "has_more": False,
                   "complete": True}


def test_announcements_metadata_nonzero_total_with_empty_page_is_incomplete(monkeypatch):
    patch_ann_http(monkeypatch, FakeResponse(ann_payload([], total=12)))
    meta = astock.announcements(CODE, start_date="2026-01-01", end_date="2026-01-31",
                                include_metadata=True)
    cov = meta["coverage"]
    assert meta["records"] == []
    assert cov["total"] == 12 and cov["returned"] == 0
    assert cov["has_more"] is True
    assert cov["complete"] is False


def test_announcements_metadata_zero_hits_claims_complete(monkeypatch):
    patch_ann_http(monkeypatch, FakeResponse(ann_payload([], total=0)))
    cov = astock.announcements(CODE, start_date="2026-01-01", end_date="2026-01-31",
                               include_metadata=True)["coverage"]
    assert cov["total"] == 0 and cov["has_more"] is False and cov["complete"] is True


def test_announcements_metadata_later_page_is_incomplete(monkeypatch):
    rows = [ann_row(art="AN_p2")]
    patch_ann_http(monkeypatch, FakeResponse(ann_payload(rows, total=11)))
    cov = astock.announcements(CODE, limit=10, page=2, start_date="2026-01-01",
                               end_date="2026-01-31", include_metadata=True)["coverage"]
    assert cov["page"] == 2 and cov["has_more"] is False and cov["complete"] is False


def test_announcements_metadata_one_sided_interval_is_incomplete(monkeypatch):
    patch_ann_http(monkeypatch, FakeResponse(ann_payload([ann_row()], total=1)))
    cov = astock.announcements(CODE, start_date="2026-01-01", include_metadata=True)["coverage"]
    assert cov["complete"] is False
    assert cov["end_date"] is None


def test_announcements_metadata_missing_total_is_unknown(monkeypatch):
    patch_ann_http(monkeypatch, FakeResponse(ann_payload([ann_row()])))
    cov = astock.announcements(CODE, start_date="2026-01-01", end_date="2026-01-31",
                               include_metadata=True)["coverage"]
    assert cov["total"] is None and cov["has_more"] is None and cov["complete"] is False


# ---------------------------------------------------------------------------
# eastmoney_datacenter —— 默认（非 strict）行为不变
# ---------------------------------------------------------------------------

def test_datacenter_default_swallows_failures_as_before(monkeypatch):
    patch_em_get(monkeypatch, requests.ConnectionError("boom"))
    assert astock.eastmoney_datacenter("RPT_LIFT_STAGE") == []

    patch_em_get(monkeypatch, DCResponse(bad_json=True))
    assert astock.eastmoney_datacenter("RPT_LIFT_STAGE") == []

    patch_em_get(monkeypatch, DCResponse({"result": {"data": []}, "success": True}))
    assert astock.eastmoney_datacenter("RPT_LIFT_STAGE") == []


def test_datacenter_default_returns_rows(monkeypatch):
    patch_em_get(monkeypatch, datacenter_ok([{"A": 1}]))
    assert astock.eastmoney_datacenter("RPT_LIFT_STAGE", page_size=5) == [{"A": 1}]


# ---------------------------------------------------------------------------
# eastmoney_datacenter —— strict=True
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response", [
    requests.ConnectionError("boom"),
    requests.Timeout("slow"),
    DCResponse({}, status=502),
    DCResponse({}, status=429),
    DCResponse(bad_json=True),
    DCResponse([1, 2, 3]),                                    # 顶层不是对象
    DCResponse({"success": True, "code": 0, "result": {"data": {"A": 1}}}),  # data 不是列表
    DCResponse({"success": False, "code": 9501, "message": "failed"}),
    DCResponse({"success": True, "code": 9501, "message": "failed"}),
    DCResponse({"success": False, "code": 5000, "message": "other"}),
    DCResponse({"success": False, "code": 9201, "result": {"data": [{"X": 1}]}}),  # 9201 但有数据
])
def test_datacenter_strict_raises_on_failure(monkeypatch, response):
    patch_em_get(monkeypatch, response)
    with pytest.raises(DataSourceError):
        astock.eastmoney_datacenter("RPT_LIFT_STAGE", strict=True)


def test_datacenter_strict_treats_9201_without_data_as_empty(monkeypatch):
    patch_em_get(monkeypatch, DCResponse({"success": True, "code": 9201,
                                          "message": "返回数据为空", "result": None}))
    assert astock.eastmoney_datacenter("RPT_LIFT_STAGE", strict=True) == []


def test_datacenter_strict_returns_rows_and_passes_params(monkeypatch):
    calls = patch_em_get(monkeypatch, datacenter_ok([{"A": 1}, {"A": 2}]))
    rows = astock.eastmoney_datacenter("RPT_LIFT_STAGE", filter_str='(X="1")', page_size=7,
                                       sort_columns="FREE_DATE", sort_types="-1", strict=True)
    assert rows == [{"A": 1}, {"A": 2}]
    params = calls[0]["params"]
    assert params["reportName"] == "RPT_LIFT_STAGE" and params["filter"] == '(X="1")'
    assert params["pageSize"] == "7" and params["pageNumber"] == "1"


# ---------------------------------------------------------------------------
# lockup_expiry —— 分桶 / 日期边界 / 单位字段
# ---------------------------------------------------------------------------

def lockup_rows(dates, able=1000.0, free=2000.0, ratio=1.23):
    return [{"FREE_DATE": f"{d} 00:00:00", "FREE_SHARES_TYPE": "定向增发",
             "FREE_SHARES": free, "ABLE_FREE_SHARES": able, "FREE_RATIO": ratio} for d in dates]


def patch_datacenter(monkeypatch, history_rows, future_rows):
    calls = []

    def fake(report_name, columns="ALL", filter_str="", page_size=50, sort_columns="",
             sort_types="-1", page_number=1, *, strict=False):
        calls.append({"report_name": report_name, "filter": filter_str, "page_size": page_size,
                      "sort_columns": sort_columns, "sort_types": sort_types, "strict": strict})
        assert report_name == "RPT_LIFT_STAGE"
        return future_rows if ">=" in filter_str else history_rows

    monkeypatch.setattr(astock, "eastmoney_datacenter", fake)
    return calls


def test_lockup_date_buckets_and_query_contract(monkeypatch):
    history_raw = lockup_rows(["2025-12-20", "2025-11-01"])
    future_raw = lockup_rows(["2026-03-01", "2026-06-30"])
    calls = patch_datacenter(monkeypatch, history_raw, future_raw)

    out = astock.lockup_expiry(CODE, trade_date="2026-02-01", forward_days=30)

    assert calls[0]["filter"] == f'(SECURITY_CODE="{CODE}")(FREE_DATE<\'2026-02-01\')'
    assert calls[0]["page_size"] == 15 and calls[0]["sort_types"] == "-1"
    assert calls[1]["filter"] == f'(SECURITY_CODE="{CODE}")(FREE_DATE>=\'2026-02-01\')'
    assert calls[1]["page_size"] == 20 and calls[1]["sort_types"] == "1"
    assert "FREE_DATE<=" not in calls[1]["filter"]     # 未来查询不带结束日
    assert all(c["strict"] for c in calls)

    assert [r["date"] for r in out["history"]] == ["2025-12-20", "2025-11-01"]
    # scheduled 返回全部已排期（含窗口外），upcoming 只留窗口内
    assert [r["date"] for r in out["scheduled"]] == ["2026-03-01", "2026-06-30"]
    assert [r["date"] for r in out["upcoming"]] == ["2026-03-01"]
    assert [r["date"] for r in out["records"]] == ["2025-11-01", "2025-12-20",
                                                   "2026-03-01", "2026-06-30"]
    assert out["status"] == "ok" and out["as_of"] == "2026-02-01"
    cov = out["coverage"]
    assert cov["source"] == "eastmoney:RPT_LIFT_STAGE"
    assert cov["history_limit"] == 15 and cov["future_limit"] == 20
    assert cov["start_date"] == "2026-02-01" and cov["end_date"] == "2026-03-03"
    assert cov["complete"] is True


def test_lockup_today_is_inclusive_and_forward_zero(monkeypatch):
    patch_datacenter(monkeypatch,
                     lockup_rows(["2026-02-01", "2026-01-31"]),   # 上游忽略过滤，含当日
                     lockup_rows(["2026-02-01", "2026-02-02", "2026-01-31"]))
    out = astock.lockup_expiry(CODE, trade_date="2026-02-01", forward_days=0)
    assert [r["date"] for r in out["history"]] == ["2026-01-31"]
    assert [r["date"] for r in out["scheduled"]] == ["2026-02-01", "2026-02-02"]
    assert [r["date"] for r in out["upcoming"]] == ["2026-02-01"]      # 含今天、不含明天
    assert out["coverage"]["excluded_rows"] == 2                      # 错桶行被排除并计数


def test_lockup_forward_window_end_inclusive(monkeypatch):
    patch_datacenter(monkeypatch, [], lockup_rows(["2026-03-03", "2026-03-04"]))
    out = astock.lockup_expiry(CODE, trade_date="2026-02-01", forward_days=30)
    assert [r["date"] for r in out["upcoming"]] == ["2026-03-03"]     # today+30 含端点
    assert [r["date"] for r in out["scheduled"]] == ["2026-03-03", "2026-03-04"]


def test_lockup_rows_keep_legacy_keys_and_add_unit_fields(monkeypatch):
    patch_datacenter(monkeypatch, [], lockup_rows(["2026-02-10"]))
    row = astock.lockup_expiry(CODE, trade_date="2026-02-01")["scheduled"][0]
    body = {
        "date": "2026-02-10", "type": "定向增发", "shares": 2000.0,
        "able_shares": 1000.0, "ratio": 1.23,
        "unlock_shares_wan": 1000.0,          # ← ABLE_FREE_SHARES
        "post_unlock_float_wan": 2000.0,      # ← FREE_SHARES（legacy shares 同值）
        "ratio_raw": 1.23, "quantity_unit": "万股",
    }
    assert row == body


def test_lockup_missing_numbers_are_none_not_zero(monkeypatch):
    patch_datacenter(monkeypatch, [], [{"FREE_DATE": "2026-02-10", "FREE_SHARES_TYPE": "首发"}])
    row = astock.lockup_expiry(CODE, trade_date="2026-02-01")["scheduled"][0]
    assert row["unlock_shares_wan"] is None
    assert row["post_unlock_float_wan"] is None
    assert row["ratio_raw"] is None


def test_lockup_status_empty_when_no_rows(monkeypatch):
    patch_datacenter(monkeypatch, [], [])
    out = astock.lockup_expiry(CODE, trade_date="2026-02-01")
    assert out["status"] == "empty"
    assert out["history"] == [] and out["upcoming"] == [] and out["scheduled"] == []
    assert out["records"] == [] and out["coverage"]["complete"] is True


def test_lockup_complete_false_when_page_limit_hit(monkeypatch):
    patch_datacenter(monkeypatch, lockup_rows([f"2025-01-{d:02d}" for d in range(1, 16)]), [])
    out = astock.lockup_expiry(CODE, trade_date="2026-02-01")
    assert len(out["history"]) == 15 and out["coverage"]["complete"] is False


def test_lockup_metadata_warns_ratio_is_not_percent(monkeypatch):
    patch_datacenter(monkeypatch, [], lockup_rows(["2026-02-10"]))
    out = astock.lockup_expiry(CODE, trade_date="2026-02-01")
    assert out["quantity_unit"] == "万股"
    assert any("百分比" in w for w in out["warnings"])


@pytest.mark.parametrize("kwargs", [
    {"code": "60051"},
    {"code": "ABCDEF"},
    {"trade_date": "2026/02/01"},
    {"trade_date": "2026-02-30"},
    {"forward_days": -1},
    {"forward_days": 366},
    {"forward_days": "abc"},
])
def test_lockup_validation(monkeypatch, kwargs):
    calls = patch_datacenter(monkeypatch, [], [])
    with pytest.raises(ValueError):
        astock.lockup_expiry(**{"code": CODE, **kwargs})
    assert calls == []


def test_lockup_default_trade_date_is_today(monkeypatch):
    calls = patch_datacenter(monkeypatch, [], [])
    astock.lockup_expiry(CODE)
    today = datetime.now(astock.BEIJING).strftime("%Y-%m-%d")
    end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=90)).strftime("%Y-%m-%d")
    assert calls[0]["filter"].endswith(f"FREE_DATE<'{today}')")
    assert calls[1]["filter"].endswith(f"FREE_DATE>='{today}')")
    # 默认 forward_days=90 的窗口终点
    assert (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days == 90


def test_lockup_propagates_upstream_error(monkeypatch):
    """strict 查询失败必须冒泡（MCP 侧才能标 isError），不得退化成全空。"""
    patch_em_get(monkeypatch, requests.ConnectionError("boom"))
    with pytest.raises(DataSourceError):
        astock.lockup_expiry(CODE, trade_date="2026-02-01")


def test_lockup_propagates_upstream_code_9501(monkeypatch):
    patch_em_get(monkeypatch, DCResponse({"success": False, "code": 9501, "message": "failed"}))
    with pytest.raises(DataSourceError):
        astock.lockup_expiry(CODE, trade_date="2026-02-01")
