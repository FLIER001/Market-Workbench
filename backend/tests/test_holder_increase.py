"""高管/股东增持数据层测试：身份分层、归一化、窗口切分、评分、缓存 warm/force。"""
from datetime import datetime, timedelta, timezone

import holder_increase as hi
import cache_runtime

BEIJING = hi.BEIJING


def _today():
    return datetime.now(BEIJING).date()


def _rec(code, person, tier, activity, *, amount=5_000_000, ratio=0.1, ongoing=False,
         shares=100_000, start=None, end=None, identity=None):
    return {
        "code": code, "name": f"股票{code}", "person": person,
        "tier": tier, "identity": identity or tier,
        "amount": amount, "shares": shares, "price": 10.0,
        "activity_date": activity, "notice_date": activity,
        "start_date": start, "end_date": end,
        "ratio_pct": ratio, "reason": "", "market": "",
        "ongoing": ongoing, "source": "exec" if tier in ("chairman", "exec", "relative") else "holder",
    }


def _cold(monkeypatch):
    """清空缓存并让快照不参与，下一次 get 走 monkeypatch 过的拉取函数。"""
    monkeypatch.setattr(hi, "_load_snapshot", lambda: None)
    monkeypatch.setattr(hi, "_save_snapshot", lambda value: None)
    cache_runtime.invalidate(hi.RAW_KEY)


def _sample_records():
    today = _today()
    return [
        _rec("000001", "张三", "chairman", today.isoformat(), amount=2e8, ratio=0.6, ongoing=True,
             start=(today - timedelta(days=5)).isoformat(), end=(today + timedelta(days=20)).isoformat()),
        _rec("000001", "李四", "big_holder", (today - timedelta(days=1)).isoformat(), amount=5e7),
        _rec("000002", "王五", "holder", (today - timedelta(days=2)).isoformat()),
        _rec("000003", "赵六", "exec", (today - timedelta(days=10)).isoformat()),
        _rec("000004", "钱七", "holder", (today - timedelta(days=40)).isoformat()),
    ]


# —— 身份分层 ——

def test_identity_from_position_tiers():
    assert hi._identity_from_position("董事长", "本人")[0] == "chairman"
    assert hi._identity_from_position("实际控制人,董事长", "本人")[0] == "chairman"
    assert hi._identity_from_position("副董事长,总经理", "本人")[0] == "exec"   # 副董事长不是最高档
    assert hi._identity_from_position("副总经理", "配偶")[0] == "relative"
    assert hi._identity_from_position("", "本人")[0] == "exec"


# —— 拉取归一化 ——

def test_fetch_exec_increase_keeps_only_increase(monkeypatch):
    rows = [
        {"SECURITY_CODE": "002456", "SECURITY_NAME": "欧菲光", "PERSON_NAME": "张三",
         "POSITION_NAME": "董事,副总经理", "PERSON_DSE_RELATION": "本人",
         "CHANGE_DATE": "2026-08-28 00:00:00", "CHANGE_AMOUNT": 1502211.24,
         "AVERAGE_PRICE": 7.7076, "BEGIN_HOLD_NUM": 1203600, "END_HOLD_NUM": 1398500},
        {"SECURITY_CODE": "600000", "SECURITY_NAME": "浦发银行", "PERSON_NAME": "李四",
         "POSITION_NAME": "董事", "PERSON_DSE_RELATION": "本人",
         "CHANGE_DATE": "2026-08-27 00:00:00", "CHANGE_AMOUNT": 900000,
         "AVERAGE_PRICE": 10.0, "BEGIN_HOLD_NUM": 5000, "END_HOLD_NUM": 4000},  # 减持
    ]
    seen = {}
    def fake_pages(report, filter_str, *, sort_columns="", max_pages=1):
        seen["report"] = report
        return rows
    monkeypatch.setattr(hi, "_fetch_pages", fake_pages)
    out = hi._fetch_exec_increase(_today())
    assert seen["report"] == "RPT_EXECUTIVE_HOLD_DETAILS"
    assert len(out) == 1
    r = out[0]
    assert r["code"] == "002456" and r["tier"] == "exec"
    assert r["shares"] == 1398500 - 1203600
    assert r["amount"] == 1502211.0
    assert r["activity_date"] == "2026-08-28"


def test_fetch_holder_increase_amount_and_ongoing(monkeypatch):
    recent = [
        {"SECURITY_CODE": "002203", "SECURITY_NAME_ABBR": "海亮股份", "HOLDER_NAME": "海亮集团",
         "NOTICE_DATE": "2026-08-29 00:00:00", "START_DATE": "2026-08-04 00:00:00",
         "END_DATE": "2026-08-28 00:00:00", "TRADE_DATE": "2026-08-28 00:00:00",
         "CHANGE_NUM": 1569.5105, "TRADE_AVERAGE_PRICE": 8.0, "HOLD_RATIO": 27.38,
         "MARKET": "二级市场"},
    ]
    ongoing = recent + [
        {"SECURITY_CODE": "600111", "SECURITY_NAME_ABBR": "北方稀土", "HOLDER_NAME": "某基金",
         "NOTICE_DATE": "2026-07-01 00:00:00", "START_DATE": "2026-07-01 00:00:00",
         "END_DATE": "2099-12-31 00:00:00", "TRADE_DATE": "",
         "CHANGE_NUM": 100, "HOLD_RATIO": 3.0, "MARKET": "二级市场"},
    ]
    queries = []
    def fake_pages(report, filter_str, *, sort_columns="", max_pages=1):
        queries.append(filter_str)
        return ongoing if "END_DATE" in filter_str else recent
    monkeypatch.setattr(hi, "_fetch_pages", fake_pages)
    out = hi._fetch_holder_increase(_today())
    assert len(queries) == 2  # 近期 + 进行中 两个查询
    by_code = {r["code"]: r for r in out}
    hl = by_code["002203"]
    assert hl["amount"] == round(1569.5105 * 1e4 * 8.0, 0)
    assert hl["tier"] == "big_holder" and hl["ongoing"] is False  # 区间已过 → 非进行中
    bf = by_code["600111"]
    assert bf["tier"] == "holder" and bf["ongoing"] is True       # END_DATE 在未来 → 进行中


# —— 窗口切分与评分 ——

def test_window_slicing(monkeypatch):
    _cold(monkeypatch)
    monkeypatch.setattr(hi, "_fetch_exec_increase", lambda today: [r for r in _sample_records() if r["source"] == "exec"])
    monkeypatch.setattr(hi, "_fetch_holder_increase", lambda today: [r for r in _sample_records() if r["source"] == "holder"])

    w1 = {r["code"] for r in hi.get_holder_increase("1d")["rows"]}
    w7 = {r["code"] for r in hi.get_holder_increase("7d")["rows"]}
    w30 = {r["code"] for r in hi.get_holder_increase("30d")["rows"]}
    wall = {r["code"] for r in hi.get_holder_increase("all")["rows"]}

    assert w1 == {"000001"}                    # 昨日0:00至今：today 与 today-1 两笔都属 000001
    assert w7 == {"000001", "000002"}          # today-2 也在 7 日内
    assert w30 == {"000001", "000002", "000003"}
    assert wall == {"000001", "000002", "000003", "000004"}   # 最全口径：无结束信号全部入选


def test_scoring_strong_vs_weak(monkeypatch):
    _cold(monkeypatch)
    monkeypatch.setattr(hi, "_fetch_exec_increase", lambda today: _sample_records()[:1])
    monkeypatch.setattr(hi, "_fetch_holder_increase", lambda today: _sample_records()[2:3])
    rows = {r["code"]: r for r in hi.get_holder_increase("30d")["rows"]}

    strong = rows["000001"]  # 董事长增持 2 亿、占股本 0.6%、计划进行中
    assert strong["score"] >= 70 and strong["grade"] == "strong"
    # 拆分：身份40 + 金额25 + 比例15 + 单人单笔4 + 进行中10
    assert strong["breakdown"] == {"identity": 40, "amount": 25, "ratio": 15, "count": 4, "recency": 10}
    weak = rows["000002"]    # 普通股东小额、2 天前
    assert weak["score"] < 55 and weak["grade"] == "normal"

    ordered = [r["code"] for r in hi.get_holder_increase("30d")["rows"]]
    assert ordered.index("000001") < ordered.index("000002")   # 按分数降序


def test_window_anchors_on_start_date():
    """窗口按增持开始日（区间起点）筛选；区间展示不含公告日。"""
    today = _today()
    start = (today - timedelta(days=20)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    late = _rec("000050", "孙八", "big_holder", today.isoformat(),
                amount=9e7, start=start, end=end)   # 20天前开始，今天才公告（触及/进展类）

    assert hi._aggregate([late], "1d", {}) == []    # 开始日不在近1日 → 不算新事件
    assert hi._aggregate([late], "7d", {}) == []
    rows30 = hi._aggregate([late], "30d", {})
    assert {r["code"] for r in rows30} == {"000050"}
    assert rows30[0]["period"] == f"{start[5:].replace('-', '/')} ~ {end[5:].replace('-', '/')}"
    assert rows30[0]["cumulative"] is False          # 19 天跨度不算累计行

    fresh = _rec("000051", "周九", "big_holder", (today - timedelta(days=2)).isoformat(),
                 start=(today - timedelta(days=1)).isoformat())
    assert {r["code"] for r in hi._aggregate([fresh], "1d", {})} == {"000051"}  # 昨天才开始的增持入选


def test_ongoing_uses_plan_dates_for_period(monkeypatch):
    _cold(monkeypatch)
    monkeypatch.setattr(hi, "_fetch_exec_increase", lambda today: [])
    monkeypatch.setattr(hi, "_fetch_holder_increase", lambda today: _sample_records()[:1])
    row = hi.get_holder_increase("7d")["rows"][0]
    assert row["ongoing"] is True
    assert "~" in row["period"]


# —— 缓存行为 ——

def test_get_warms_from_snapshot_without_building(monkeypatch):
    records = _sample_records()
    monkeypatch.setattr(hi, "_load_snapshot", lambda: {"records": records, "updated": "u"})
    monkeypatch.setattr(hi, "_build_raw", lambda: (_ for _ in ()).throw(AssertionError("must not build")))
    cache_runtime.invalidate(hi.RAW_KEY)

    out = hi.get_holder_increase("30d")

    assert out["updated"] == "u"
    assert len(out["rows"]) == 3          # 30 日窗口内 3 只
    assert out["total_records"] == 5


def test_build_raw_raises_on_empty(monkeypatch):
    _cold(monkeypatch)
    monkeypatch.setattr(hi, "_fetch_exec_increase", lambda today: [])
    monkeypatch.setattr(hi, "_fetch_holder_increase", lambda today: [])

    out = hi.get_holder_increase("7d")

    assert out["rows"] == []
    assert out["refresh_error"]  # 空数据被视为构建失败并记录


# —— 进行中判定与计划解析 ——

def test_plan_candidates_broadest():
    """「全部 · 进行中」候选 = 回看窗口内所有有增持记录的股票（最全口径）。"""
    today = _today()
    a = _rec("000010", "张三", "exec", (today - timedelta(days=30)).isoformat())   # 单笔、久远 → 也入选
    b = _rec("000020", "李四", "holder", (today - timedelta(days=40)).isoformat())
    c = _rec("000030", "王五", "holder", (today - timedelta(days=1)).isoformat(), ongoing=True)
    assert hi._plan_candidates([a, b, c], today) == {"000010", "000020", "000030"}


def test_parse_plan_amount_and_deadline():
    assert hi._parse_plan_amount("拟增持金额不低于人民币40亿元")[0] == 40e8
    assert hi._parse_plan_amount("合计增持金额不低于3,000万元")[0] == 3000e4
    assert hi._parse_plan_amount("增持不超过2亿元") is None      # 上限不作达成依据
    assert hi._parse_plan_deadline("至2025年12月8日止", "2024-12-09") == "2025-12-08"
    assert hi._parse_plan_deadline("实施期限为6个月", "2026-03-01") == "2026-09-01"
    assert hi._parse_plan_deadline("无期限信息", "2026-03-01") is None


def test_plan_ended_rules():
    today = _today()
    assert hi._plan_ended({"done": True, "amount": None, "deadline": None}, 0, "", today) is True
    reached = {"done": False, "amount": 1e8, "deadline": None}
    assert hi._plan_ended(reached, 1.2e8, "2026-08-20", today) is True       # 金额达成计划下限
    assert hi._plan_ended(reached, 0.5e8, "2026-08-20", today) is False
    expired = {"done": False, "amount": None, "deadline": "2026-08-01"}
    assert hi._plan_ended(expired, 0, "2026-07-31", today) is True           # 已过期且期限后无增持
    assert hi._plan_ended(expired, 0, today.isoformat(), today) is False     # 过期但仍在增持（可能新计划）
    assert hi._plan_ended(None, 1e9, today.isoformat(), today) is False


def test_all_window_excludes_ended_plans():
    today = _today()
    # 000011 累计 1.3 亿 ≥ 计划下限 1 亿 → 已结束剔除；000012 无计划 → 保留
    r1a = _rec("000011", "张三", "exec", (today - timedelta(days=5)).isoformat(), amount=8e7)
    r1b = _rec("000011", "张三", "exec", (today - timedelta(days=2)).isoformat(), amount=5e7)
    r2a = _rec("000012", "李四", "exec", (today - timedelta(days=5)).isoformat())
    r2b = _rec("000012", "李四", "exec", (today - timedelta(days=1)).isoformat())
    plans = {"000011": {"done": False, "amount": 1e8, "amount_label": "≥1亿元", "deadline": None,
                        "notice_date": (today - timedelta(days=20)).isoformat(), "title": "计划"}}
    rows = hi._aggregate([r1a, r1b, r2a, r2b], "all", plans)
    assert {r["code"] for r in rows} == {"000012"}
    assert rows[0]["plan"] is None          # 保留行无计划信息；已达成计划的 000011 被剔除


def test_build_raw_includes_plans(monkeypatch):
    _cold(monkeypatch)
    today = _today()
    recs = [
        _rec("000021", "张三", "exec", (today - timedelta(days=5)).isoformat()),
        _rec("000021", "张三", "exec", (today - timedelta(days=2)).isoformat()),
    ]
    monkeypatch.setattr(hi, "_fetch_exec_increase", lambda today: recs)
    monkeypatch.setattr(hi, "_fetch_holder_increase", lambda today: [])
    fetched = []
    def fake_plan(code, today):
        fetched.append(code)
        return {"done": False, "amount": None, "amount_label": "", "deadline": None,
                "notice_date": "2026-08-01", "title": "t"}
    monkeypatch.setattr(hi, "_fetch_plan", fake_plan)
    payload = hi._build_raw()
    assert set(payload["plans"]) == {"000021"} and fetched == ["000021"]


# —— 端点契约 ——

def test_endpoint_window_validation(monkeypatch):
    """400 校验 + 200 接线；mock 数据层避免测试联网冷启动。"""
    from fastapi.testclient import TestClient
    import app as app_module
    monkeypatch.setattr(app_module.holder_increase, "get_holder_increase",
                        lambda window, force=False: {"window": window, "rows": []})
    client = TestClient(app_module.app)
    assert client.get("/api/event/holder-increase?window=2w").status_code == 400
    r = client.get("/api/event/holder-increase?window=1d")
    assert r.status_code == 200
    assert r.json()["data"]["window"] == "1d"
