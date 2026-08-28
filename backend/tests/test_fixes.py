"""审计修复回归测（2026-07-05，全部离线）：
鉴权中间件 / 持仓 CRUD 与坏文件降级 / 估值脏数据防护 / 涨停池脏数值 /
空结果不缓存 / akshare 缺失降级 / 无 index 工具调用归位 / CLI 流式超时。
"""
import pytest
import time
from datetime import datetime
from fastapi.testclient import TestClient

import app as app_module
import astock
import chat
import cli_runtime
import market
import cache_runtime
import portfolio as pf

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def legacy_ledger_unit_scope(monkeypatch):
    """旧 CRUD 用例只验证账本算法；HTTP 鉴权/隔离由专门用例覆盖。"""
    monkeypatch.setattr(app_module, "_portfolio_user_id", lambda request: None)


# ── VR_API_KEY 鉴权中间件 ───────────────────────────────────────────

def test_api_key_auth(monkeypatch):
    monkeypatch.setattr(app_module, "_API_KEY", "sekret")
    assert client.get("/api/health").status_code == 200  # health 豁免
    assert client.get("/api/quote?codes=abc").status_code == 401  # 缺头
    assert client.get("/api/quote?codes=abc", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # 正确 key → 通过鉴权、走到参数校验层（400 而非 401，不联网）
    assert client.get("/api/quote?codes=abc", headers={"Authorization": "Bearer sekret"}).status_code == 400


# ── 持仓：本地 JSON CRUD（不联网，行情打桩） ────────────────────────

@pytest.fixture()
def tmp_pf(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pf, "PF_FILE", str(tmp_path / "portfolio.json"))
    pf._invalidate()  # 进程级响应缓存跨测试隔离：每个用例从空缓存开始
    monkeypatch.setattr(astock, "tencent_quote",
                        lambda codes: {c: {"name": f"股{c}", "price": 10.0, "last_close": 9.5} for c in codes})
    return tmp_path


def test_portfolio_crud_roundtrip(tmp_pf):
    assert client.get("/api/portfolio").json()["data"]["holdings"] == []

    r = client.post("/api/portfolio/holding", json={"code": "600519", "shares": 100, "cost": 8.0})
    assert r.status_code == 200
    h = r.json()["data"]["holdings"][0]
    assert h["code"] == "600519"
    assert h["pnl"] == pytest.approx((10.0 - 8.0) * 100)
    # 当日盈亏 = (现价 - 昨收) × 股数
    assert h["day_pnl"] == pytest.approx((10.0 - 9.5) * 100)
    assert h["day_pnl_pct"] == pytest.approx((10.0 - 9.5) / 9.5 * 100, abs=0.01)
    assert r.json()["data"]["totals"]["day_pnl"] == pytest.approx(50.0)
    assert r.json()["data"]["totals"]["day_pnl_pct"] == pytest.approx(50.0 / 950.0 * 100, abs=0.01)

    # 同代码加仓 → 加权平均成本
    client.post("/api/portfolio/holding", json={"code": "600519", "shares": 100, "cost": 12.0})
    h = client.get("/api/portfolio").json()["data"]["holdings"][0]
    assert h["shares"] == 200
    assert h["cost"] == pytest.approx(10.0)

    # 清仓全部 200 股，成本缺省 → 自动取持仓加权成本 10.0，持仓同步清空
    r = client.post("/api/portfolio/close", json={"code": "600519", "date": "2026-07-05", "price": 11.0, "shares": 200})
    assert r.status_code == 200
    assert r.json()["data"]["closed"][0]["pnl"] == pytest.approx(200.0)
    assert r.json()["data"]["closed"][0]["cost"] == pytest.approx(10.0)
    assert r.json()["data"]["holdings"] == []

    assert client.delete("/api/portfolio/close?index=0").json()["data"]["closed"] == []
    assert client.post("/api/portfolio/refresh").status_code == 200


def test_portfolio_update_holding_overrides(tmp_pf):
    """修改持仓：直接覆盖数量/成本/日期（区别于添加的加权合并），改完浮盈按新值算。"""
    client.post("/api/portfolio/holding", json={"code": "600519", "shares": 100, "cost": 8.0})
    r = client.put("/api/portfolio/holding", json={"code": "600519", "shares": 200, "cost": 9.0, "bought_date": "2026-01-05"})
    assert r.status_code == 200
    h = r.json()["data"]["holdings"][0]
    assert h["shares"] == 200
    assert h["cost"] == pytest.approx(9.0)
    assert h["bought_date"] == "2026-01-05"
    assert h["pnl"] == pytest.approx((10.0 - 9.0) * 200)

    # 不传日期 → 保持原值
    r = client.put("/api/portfolio/holding", json={"code": "600519", "shares": 200, "cost": 9.5})
    assert r.json()["data"]["holdings"][0]["bought_date"] == "2026-01-05"

    # 校验与边界：坏代码 / 非正数量 / 日期格式 / 不存在的持仓
    assert client.put("/api/portfolio/holding", json={"code": "abc", "shares": 1, "cost": 1}).status_code == 400
    assert client.put("/api/portfolio/holding", json={"code": "600519", "shares": 0, "cost": 1}).status_code == 400
    assert client.put("/api/portfolio/holding", json={"code": "600519", "shares": 1, "cost": 1, "bought_date": "2026/01/05"}).status_code == 400
    assert client.put("/api/portfolio/holding", json={"code": "000002", "shares": 1, "cost": 1}).status_code == 400

    client.delete("/api/portfolio/holding?code=600519")


def test_portfolio_partial_close_deducts_holding(tmp_pf):
    """部分清仓：从当前持仓扣减股数、成本不变；未持仓代码必须显式给成本。"""
    client.post("/api/portfolio/holding", json={"code": "600519", "shares": 300, "cost": 8.0})
    r = client.post("/api/portfolio/close", json={"code": "600519", "date": "2026-07-06", "price": 9.0, "shares": 100})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["closed"][0]["pnl"] == pytest.approx(100.0)
    assert d["closed"][0]["cost"] == pytest.approx(8.0)
    h = d["holdings"][0]
    assert h["shares"] == 200
    assert h["cost"] == pytest.approx(8.0)

    # 未持仓的代码无法自动取成本 → 明确 400，而不是按 0 成本算
    r = client.post("/api/portfolio/close", json={"code": "000001", "date": "2026-07-06", "price": 9.0, "shares": 100})
    assert r.status_code == 400
    # 显式给成本仍可用（覆盖：记录一笔当前持仓之外的历史清仓）
    r = client.post("/api/portfolio/close", json={"code": "000001", "date": "2026-07-06", "price": 9.0, "shares": 100, "cost": 10.0})
    assert r.status_code == 200
    assert r.json()["data"]["closed"][-1]["pnl"] == pytest.approx(-100.0)

    client.delete("/api/portfolio/holding?code=600519")
    client.delete("/api/portfolio/close?index=0")
    client.delete("/api/portfolio/close?index=0")


def test_portfolio_fetches_current_and_closed_quotes_once(tmp_pf, monkeypatch):
    calls = []

    def quote(codes):
        calls.append(codes)
        return {c: {"name": f"股{c}", "price": 10.0, "last_close": 9.5} for c in codes}

    monkeypatch.setattr(astock, "tencent_quote", quote)
    pf._save({
        "holdings": [{"code": "600519", "shares": 100, "cost": 8.0}],
        "closed": [{"code": "000001", "name": "平安银行", "date": "2026-07-06",
                    "price": 9.0, "shares": 100, "cost": 8.0, "pnl": 100.0, "pnl_pct": 12.5}],
    })

    data = pf.get_portfolio()

    assert calls == [["600519", "000001"]]
    assert data["closed"][0]["post_close_pct"] == pytest.approx(11.11)


def test_portfolio_add_validation(tmp_pf):
    assert client.post("/api/portfolio/holding", json={"code": "abc", "shares": 1, "cost": 1}).status_code == 400
    assert client.post("/api/portfolio/holding", json={"code": "600519", "shares": 0, "cost": 1}).status_code == 400


def test_portfolio_corrupt_file_returns_empty(tmp_pf):
    (tmp_pf / "portfolio.json").write_text("{broken json", encoding="utf-8")
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    assert r.json()["data"]["holdings"] == []


# ── issue #13：加仓合并成本保留 4 位小数（ETF/基金成本常见 3-4 位） ──

def test_portfolio_merge_cost_keeps_4_decimals(tmp_pf):
    client.post("/api/portfolio/holding", json={"code": "510300", "shares": 100, "cost": 1.0001})
    client.post("/api/portfolio/holding", json={"code": "510300", "shares": 100, "cost": 1.0003})
    h = client.get("/api/portfolio").json()["data"]["holdings"][0]
    assert h["cost"] == pytest.approx(1.0002, abs=1e-9)


# ── issue #12：旧版数据在仓库内 .cache/，重下载会丢 → 自动迁到用户目录 ──

def test_portfolio_legacy_migration(tmp_path, monkeypatch):
    old = tmp_path / "repo-cache" / "portfolio.json"
    old.parent.mkdir()
    old.write_text('{"holdings": [{"code": "600519", "shares": 100, "cost": 8.0}]}', encoding="utf-8")
    monkeypatch.setattr(pf, "_OLD_PF_FILE", str(old))
    monkeypatch.setattr(pf, "CACHE_DIR", str(tmp_path / "userdata"))
    monkeypatch.setattr(pf, "PF_FILE", str(tmp_path / "userdata" / "portfolio.json"))
    pf._migrate_legacy()
    assert pf._load()["holdings"][0]["code"] == "600519"
    # 新位置已有数据 → 再跑迁移不覆盖
    pf._save({"holdings": []})
    pf._migrate_legacy()
    assert pf._load()["holdings"] == []


def test_myreports_legacy_migration(tmp_path, monkeypatch):
    import myreports as mr

    old = tmp_path / "repo-cache" / "myreports"
    old.mkdir(parents=True)
    (old / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.delenv("VR_REPORTS_DIR", raising=False)
    monkeypatch.setattr(mr, "_OLD_DEFAULT_DIR", old)
    monkeypatch.setattr(mr, "REPORTS_DIR", tmp_path / "userdata" / "myreports")
    # 上次复制中断留下的半截临时目录，不该挡住这次迁移
    stale = tmp_path / "userdata" / "myreports.migrate.tmp"
    stale.mkdir(parents=True)
    (stale / "partial.bin").write_text("x", encoding="utf-8")
    mr._migrate_legacy()
    dst = tmp_path / "userdata" / "myreports"
    assert (dst / "index.json").exists()
    assert not (dst / "partial.bin").exists()  # 半截内容没混进正式目录


# ── full_valuation：一致预期缺「均值」/ '-' 占位不再 502 ─────────────

_QUOTE = {"600519": {"name": "贵州茅台", "price": 100.0, "mcap_yi": 1000, "pe_ttm": 20.0, "pb": 5.0}}


def test_full_valuation_dirty_forecast(monkeypatch):
    monkeypatch.setattr(astock, "tencent_quote", lambda codes: _QUOTE)
    monkeypatch.setattr(astock, "profit_forecast", lambda code: [
        {"年度": "2026", "预测机构数": "-"},  # 缺「均值」+ 脏机构数
        {"年度": "2027", "均值": "-"},        # '-' 占位
    ])
    out = astock.full_valuation("600519")
    assert out["eps_26e"] is None
    assert out["eps_27e"] is None
    assert out["pe_26e"] is None


def test_full_valuation_string_numbers(monkeypatch):
    monkeypatch.setattr(astock, "tencent_quote", lambda codes: _QUOTE)
    monkeypatch.setattr(astock, "profit_forecast", lambda code: [
        {"年度": "2026年", "均值": "2.0", "预测机构数": "12"},
        {"年度": "2027年", "均值": 2.4},
    ])
    out = astock.full_valuation("600519")
    assert out["eps_26e"] == 2.0
    assert out["analyst_count"] == 12
    assert out["pe_26e"] == 50.0


# ── 短线情绪：涨停池脏数值（'-' 占位）不再让排序崩溃 ────────────────

def test_emotion_dirty_amount(monkeypatch):
    pools = {
        "getTopicZTPool": [
            {"c": "600001", "n": "甲", "lbc": 3, "p": 10000, "zdp": 10.0, "amount": "-", "ltsz": None, "hybk": "X"},
            {"c": "600002", "n": "乙", "lbc": 2, "p": "-", "zdp": None, "amount": 5e8, "ltsz": 1e9, "hybk": "Y"},
        ],
        "getTopicZBPool": [],
        "getTopicDTPool": [],
        "getYesterdayZTPool": [{}],
    }
    monkeypatch.setattr(astock, "em_zt_topic_pool", lambda ep, d, sort="": pools.get(ep, []))
    out = market._emotion()
    stocks = out["lianban_stocks"]
    assert [s["code"] for s in stocks] == ["600001", "600002"]  # 排序没崩、按连板数降序
    assert stocks[0]["amount"] is None    # '-' 归一为 None
    assert stocks[1]["price"] == 0.0      # p='-' 归一后按 0 展示
    assert stocks[1]["amount"] == 5e8


# ── 缓存：数据源故障的空结果不缓存 5 分钟 ───────────────────────────

def test_cached_skips_empty():
    market._CACHE.pop("k_test", None)
    calls = []

    def flaky():
        calls.append(1)
        return {} if len(calls) == 1 else {"ok": 1}

    assert market._cached("k_test", flaky) == {}
    assert market._cached("k_test", flaky) == {"ok": 1}  # 空结果没被缓存 → 下次重试成功
    assert market._cached("k_test", flaky) == {"ok": 1}  # 非空已缓存，不再调用
    assert len(calls) == 2
    market._CACHE.pop("k_test", None)


# ── 分层缓存：源故障回退 last-good（指标不空窗） + 退避不每请求重建 ──────────

def _reset_layered(key):
    market._LAYERED.pop(key, None)
    market._FAILED_STREAK.pop(key, None)
    cache_runtime.invalidate(key)


def test_layered_falls_back_to_last_good():
    key = "k_layered"
    _reset_layered(key)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            return {"us": {"x": 1}}
        raise RuntimeError("source down")

    first = market._layered_get(key, flaky, valid=lambda v: bool(v.get("us")))
    assert first["us"] == {"x": 1} and first["cache_state"] == "fresh"
    # 过期后源故障：回退 last-good，标注 stale，数据仍在
    second = market._layered_get(key, flaky, valid=lambda v: bool(v.get("us")), ttl=0)
    assert second["us"] == {"x": 1}
    assert second["cache_state"] == "refreshing"
    time.sleep(0.03)
    # 退避窗口内不再调用 build（不打爆故障源），仍回 last-good
    third = market._layered_get(key, flaky, valid=lambda v: bool(v.get("us")), ttl=0)
    assert third["us"] == {"x": 1} and len(calls) == 2
    assert third["cache_state"] == "error" and third["refresh_error"] == "source down"
    # 冷启动 warm 快照：全新 key、无 last-good，直接给快照并标 stale
    warm_calls = []

    def boom():
        warm_calls.append(1)
        raise RuntimeError("down")

    _reset_layered("k_warm")
    warmed = market._layered_get("k_warm", boom, valid=bool, warm=lambda: {"cn": {"snapshot": 1}}, ttl=0)
    assert warmed["cn"] == {"snapshot": 1} and warmed["cache_state"] == "refreshing"
    _reset_layered(key)


def test_sub_cached_keeps_last_good():
    key = "k_sub"
    market._SUB.pop(key, None)
    market._SUB_LAST.pop(key, None)
    calls = []

    def flaky():
        calls.append(1)
        return {"v": 1} if len(calls) == 1 else {}

    assert market._sub_cached(key, flaky) == {"v": 1}
    market._SUB[key] = (0, {"v": 1})  # 强制过期
    out = market._sub_cached(key, flaky)  # 源故障 → last-good
    assert out == {"v": 1}
    assert len(calls) >= 2  # 后台异步重试可能追加一次调用
    # last-good 过期兜底窗口外、源仍空 → 返回空值（不假装有数据）
    market._SUB_LAST[key] = (0, {"v": 1})
    market._SUB.pop(key, None)
    assert market._sub_cached(key, flaky) == {}
    market._SUB.pop(key, None)
    market._SUB_LAST.pop(key, None)


def test_gold_series_cache_marks_fallback_stale():
    import gold_score
    key = "gold:test:stale"
    market._SUB.pop(key, None)
    market._SUB_LAST.pop(key, None)
    gold_score._SOURCE_STATUS.pop(key, None)

    rows = gold_score._series_cached(key, lambda: [("2026-08-07", 1.0)], ttl=0)
    assert rows == [("2026-08-07", 1.0)]
    assert gold_score._SOURCE_STATUS[key]["status"] == "fresh"

    rows = gold_score._series_cached(key, lambda: [], ttl=0)
    assert rows == [("2026-08-07", 1.0)]
    assert gold_score._SOURCE_STATUS[key]["status"] == "stale"
    market._SUB.pop(key, None)
    market._SUB_LAST.pop(key, None)


# ── akshare 未安装：market 降级返回空，不挡服务 ─────────────────────

def test_market_degrades_without_akshare(monkeypatch):
    def boom():
        raise astock.DependencyMissing("akshare 未安装")

    monkeypatch.setattr(astock, "_akshare", boom)
    assert market._sentiment() == {}
    assert market._sectors() == []


# ── 流式工具调用：非标网关不带 index 时按 id 归位、不串参数 ──────────

def test_stream_tool_calls_without_index(monkeypatch):
    deltas_rounds = [
        [  # 第一轮：增量全部不带 index —— 续块无 id、新调用带新 id
            {"tool_calls": [{"id": "call_a", "function": {"name": "query_quote", "arguments": '{"codes":'}}]},
            {"tool_calls": [{"function": {"arguments": '["600519"]}'}}]},
            {"tool_calls": [{"id": "call_b", "function": {"name": "query_news", "arguments": '{"code":"600519"}'}}]},
        ],
        [{"content": "答案"}],  # 第二轮：纯文本收尾
    ]
    state = {"round": 0}
    monkeypatch.setattr(chat, "_call_llm_stream", lambda cfg, messages, use_tools: None)

    def fake_iter(_resp):
        i = state["round"]
        state["round"] += 1
        yield from deltas_rounds[i]

    monkeypatch.setattr(chat, "_iter_sse_deltas", fake_iter)
    executed = []
    monkeypatch.setattr(chat, "_exec_tool", lambda name, args: (executed.append((name, args)), {"ok": 1})[1])

    events = list(chat.run_chat_stream(
        {"baseURL": "http://x", "apiKey": "k", "model": "m"},
        [{"role": "user", "content": "q"}],
    ))
    assert ("query_quote", {"codes": ["600519"]}) in executed  # 参数没被串坏
    assert ("query_news", {"code": "600519"}) in executed      # 两个调用各归各槽
    assert events[-1]["type"] == "done"


# ── CLI 流式：子进程挂起时超时真正生效（不再无限期阻塞） ────────────

def test_run_cli_stream_timeout(monkeypatch):
    monkeypatch.setattr(cli_runtime, "_CLI_TIMEOUT_S", 1)
    monkeypatch.setitem(cli_runtime._CLI_DEFS, "fake", {
        "bins": ["python3"],
        "delivery": "stdin",
        "build_args": lambda _: ["-c", "import time\nprint('x', flush=True)\ntime.sleep(30)"],
        "env": {},
    })
    chunks = []
    with pytest.raises(RuntimeError, match="超时"):
        for line in cli_runtime.run_cli_stream("fake", "s", "u"):
            chunks.append(line)
    assert chunks and chunks[0].strip() == "x"  # 挂起前的输出已正常流出


# ── 本年盈亏（YTD）分段口径：买入日期决定基准价 ────────────────────

@pytest.fixture()
def ytd_env(tmp_path, monkeypatch):
    """离线 YTD 环境：行情/K 线打桩，账本指向临时目录。"""
    monkeypatch.setattr(pf, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pf, "PF_FILE", str(tmp_path / "portfolio.json"))
    pf._invalidate()
    year = datetime.now(pf.BEIJING).year
    monkeypatch.setattr(astock, "tencent_quote",
                        lambda codes: {c: {"name": f"股{c}", "price": 12.0, "last_close": 11.0} for c in codes})
    # 年初基准价 10.0（本年度首个交易日收盘）
    kline = [{"date": f"{year}-01-02", "close": 10.0}, {"date": f"{year}-06-01", "close": 11.0}]
    monkeypatch.setattr(astock, "tencent_kline", lambda code, period="day", count=260: kline)
    return tmp_path


def test_ytd_year_buy_uses_cost_as_base(ytd_env):
    """年内买入：按成本→现价计，不掺年初到买入之间的涨跌。"""
    pf.add_holding("600519", 100, 9.0, f"{datetime.now(pf.BEIJING).year}-03-01")
    data = pf.get_portfolio(fresh=True)
    assert data["ytd_pnl"] == pytest.approx((12.0 - 9.0) * 100)
    assert data["ytd_pnl_pct"] == pytest.approx((12.0 - 9.0) / 9.0 * 100, abs=0.01)


def test_ytd_last_year_buy_uses_year_open(ytd_env):
    """年前买入：按年初价→现价计（含年内分红除权由前复权口径近似）。"""
    pf.add_holding("600519", 100, 8.0, "2024-06-01")
    data = pf.get_portfolio(fresh=True, refresh_bases=True)
    assert data["ytd_pnl"] == pytest.approx((12.0 - 10.0) * 100)
    # 年初基准已落账本：后续普通 GET 不拉 K 线也能重放出同样结果
    data2 = pf.get_portfolio(fresh=True)
    assert data2["ytd_pnl"] == pytest.approx((12.0 - 10.0) * 100)


def test_ytd_legacy_no_date_treated_as_last_year(ytd_env):
    """旧账本无日期：按年前持有计（兼容不强制补录）。"""
    pf._save({"holdings": [{"code": "600519", "shares": 100, "cost": 8.0}], "closed": []})
    data = pf.get_portfolio(fresh=True, refresh_bases=True)
    assert data["ytd_pnl"] == pytest.approx((12.0 - 10.0) * 100)


def test_ytd_includes_this_year_closed_pnl(ytd_env):
    """本年已清仓的实现盈亏并入本年盈亏；往年清仓不计。"""
    year = datetime.now(pf.BEIJING).year
    pf._save({"holdings": [], "closed": [
        {"code": "600519", "date": f"{year}-02-01", "price": 11.0, "shares": 100, "cost": 9.0, "pnl": 200.0},
        {"code": "000001", "date": f"{year - 1}-12-01", "price": 11.0, "shares": 100, "cost": 9.0, "pnl": 150.0},
    ]})
    data = pf.get_portfolio(fresh=True)
    assert data["ytd_pnl"] == pytest.approx(200.0)


def test_ytd_mixed_segments_sum(ytd_env):
    """年内 + 年前混合持仓：分段求和。"""
    year = datetime.now(pf.BEIJING).year
    pf.add_holding("600519", 100, 9.0, f"{year}-03-01")   # 成本 9 → 12：+300
    pf.add_holding("000001", 200, 8.0, "2024-01-10")       # 年初 10 → 12：+400
    data = pf.get_portfolio(fresh=True, refresh_bases=True)
    assert data["ytd_pnl"] == pytest.approx(300.0 + 400.0)
