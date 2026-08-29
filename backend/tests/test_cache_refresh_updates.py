"""刷新/缓存加固的回归测试：cache_runtime 等待封顶与 update_value、
共享 AI 解读骨架（ai_insight.py）、bonds 基础 key 磁盘快照、pulse 快照超龄
数据-only 重建。"""

import asyncio
import os
import threading
import time

import cache_runtime
import bonds

import ai_insight
from pulse import market_pulse


# ---------------------------------------------------------------------------
# cache_runtime：冷启动等待封顶 + update_value
# ---------------------------------------------------------------------------

def test_cold_wait_is_bounded_when_owner_wedges(monkeypatch):
    cache_runtime.reset_for_tests()
    monkeypatch.setattr(cache_runtime, "_COLD_WAIT_MAX", 0.2)
    started = threading.Event()
    release = threading.Event()

    def wedged_build():
        started.set()
        release.wait(5)  # 模拟卡死的上游
        return {"v": 1}

    owner = threading.Thread(
        target=lambda: cache_runtime.get("wedge", wedged_build, ttl=60), daemon=True)
    owner.start()
    assert started.wait(2)

    begin = time.time()
    result = cache_runtime.get("wedge", wedged_build, ttl=60)
    elapsed = time.time() - begin
    release.set()
    owner.join(5)

    assert elapsed < 5, "等待者必须被冷启动封顶释放，而不是无限挂起"
    assert result["cache_state"] == "error"


def test_update_value_mutates_cached_payload_in_place():
    cache_runtime.reset_for_tests()
    cache_runtime.seed("uv", {"score": 1, "rows": [1]})
    touched = []

    def merge(value):
        touched.append(True)
        value["rows"].append(2)
        value["ai_insight"] = {"t": 1}
        return value

    out = cache_runtime.update_value("uv", merge)
    assert touched == [True]
    assert out == {"score": 1, "rows": [1, 2], "ai_insight": {"t": 1}}
    assert cache_runtime.peek("uv")["rows"] == [1, 2]
    # 无值的 key：返回 None，mutator 不执行
    assert cache_runtime.update_value("missing", merge) is None
    assert touched == [True]


def test_warm_snapshot_age_capped_by_data_timestamp():
    """launchd 高频重启场景：磁盘快照 mtime 恒新，但数据时点可能很旧。
    warm 锚定数据时点（updated/as_of）后，旧快照在重启后立即判 stale 触发后台重建，
    而不是按「刚写入的文件」再 fresh 一个 TTL。"""
    cache_runtime.reset_for_tests()
    built = []
    cache_runtime.get(
        "warm_old", lambda: (built.append(1), {"v": 2})[1], ttl=3600,
        warm=lambda: {"v": 1, "updated": "2026-08-18 09:00"},
    )
    # 快照时点 08-18 早于 TTL 起点 → 读到的就是 stale 且已开后台刷新
    assert built == [1], "旧数据时点的快照必须立刻触发后台重建"
    state = cache_runtime.get("warm_old", lambda: {"v": 3}, ttl=3600)
    assert state["cache_state"] in ("refreshing", "fresh", "stale")
    assert state["v"] == 1  # 后台重建完成前，先返回快照值


def test_warm_snapshot_fresh_data_stays_fresh():
    """快照时点是刚刚（updated=当前时间）→ 与旧行为一致：fresh，不触发重建。"""
    cache_runtime.reset_for_tests()
    built = []
    from datetime import datetime
    from cache_runtime import BEIJING
    now_txt = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
    out = cache_runtime.get(
        "warm_new", lambda: (built.append(1), {"v": 2})[1], ttl=3600,
        warm=lambda: {"v": 1, "updated": now_txt},
    )
    assert built == [], "新数据时点的快照不应触发重建"
    assert out["cache_state"] == "fresh"


# ---------------------------------------------------------------------------
# ai_insight 共享骨架：命中规则、失败保旧、force、落盘
# ---------------------------------------------------------------------------

def _run_insight(cache_key, snap, build, saves, **overrides):
    cache_runtime.reset_for_tests()
    if snap is not None:
        cache_runtime.seed(cache_key, snap)
    kwargs = dict(
        cache_key=cache_key,
        build=build,
        valid=lambda v: isinstance(v, dict) and bool(v.get("segments")),
        writable=lambda s: bool(s.get("indicators")),
        save=lambda s: saves.append(s),
        stamp_field="updated",
    )
    kwargs.update(overrides)
    return asyncio.run(ai_insight.cached_insight(**kwargs))


def test_shared_insight_fresh_hit_skips_llm():
    calls = []

    async def build():
        calls.append(1)
        return {"segments": {"a": "新"}}

    snap = {"updated": "2026-08-29 10:00", "ai_insight_at": "2026-08-29 11:00",
            "ai_insight": {"segments": {"a": "旧"}}, "indicators": [1]}
    out = _run_insight("k1", snap, build, [])
    assert calls == []
    assert out == {"segments": {"a": "旧"}}


def test_shared_insight_stale_regenerates_and_persists():
    calls = []

    async def build():
        calls.append(1)
        return {"segments": {"a": "新"}}

    snap = {"updated": "2026-08-29 12:00", "ai_insight_at": "2026-08-29 10:00",
            "ai_insight": {"segments": {"a": "旧"}}, "indicators": [1]}
    saves: list = []
    out = _run_insight("k2", snap, build, saves)

    assert calls == [1]
    assert out == {"segments": {"a": "新"}}
    stored = cache_runtime.peek("k2")
    assert stored["ai_insight"] == {"segments": {"a": "新"}}
    assert stored["ai_insight_at"] >= "2026-08-29 12:00" or " " in stored["ai_insight_at"]
    assert saves and saves[0]["ai_insight"] == {"segments": {"a": "新"}}


def test_shared_insight_failure_keeps_old_without_saving():
    async def fail():
        return ""

    snap = {"updated": "2026-08-29 12:00", "ai_insight_at": "2026-08-29 10:00",
            "ai_insight": {"segments": {"a": "旧"}}, "indicators": [1]}
    saves: list = []
    out = _run_insight("k3", snap, fail, saves)

    assert out == {"segments": {"a": "旧"}}
    assert saves == []
    assert cache_runtime.peek("k3")["ai_insight"] == {"segments": {"a": "旧"}}


def test_shared_insight_force_rebuilds_even_when_fresh():
    calls = []

    async def build():
        calls.append(1)
        return {"segments": {"a": "新"}}

    snap = {"updated": "2026-08-29 10:00", "ai_insight_at": "2026-08-29 11:00",
            "ai_insight": {"segments": {"a": "旧"}}, "indicators": [1]}
    out = _run_insight("k4", snap, build, [], force=True)

    assert calls == [1]
    assert out == {"segments": {"a": "新"}}


# ---------------------------------------------------------------------------
# bonds：基础 key 快照 warm（重启免网络）+ 快照落盘失败不影响请求
# ---------------------------------------------------------------------------

def test_bonds_basic_key_warms_from_disk_without_building(monkeypatch):
    snapshot = {"date": "2026-08-28", "yields": {"1年": [{"date": "2026-08-28", "v": 1.5}]}}
    monkeypatch.setattr(bonds, "_load_snapshot", lambda name: snapshot if name == "curve" else None)
    monkeypatch.setattr(
        bonds, "_curve_payload",
        lambda: (_ for _ in ()).throw(AssertionError("curve snapshot warm must not hit network")))
    cache_runtime.invalidate("bonds:curve")

    result = bonds.get_curve()

    assert result["yields"] == snapshot["yields"]
    cache_runtime.invalidate("bonds:curve")


def test_bonds_snapshot_save_failure_is_tolerated(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(bonds, "DATA_DIR", str(blocker))  # makedirs 撞上文件 → OSError

    bonds._save_snapshot("curve", {"a": 1})  # 不应抛出：落盘是 best-effort


# ---------------------------------------------------------------------------
# pulse：快照超龄触发数据-only 重建（零研判类 LLM），手动 force 仍全量
# ---------------------------------------------------------------------------

def _seed_pulse_snapshot(monkeypatch, tmp_path, *, age_s: float):
    monkeypatch.setenv("VR_DATA_DIR", str(tmp_path))
    old = {
        "as_of": "2026-08-28T09:00:00+08:00",
        "status": "旧现状",
        "overall": "旧综合",
        "modules": [
            {"key": "货币政策", "core": True, "insight": "旧卡解读", "markets": []},
            {"key": "宏观经济", "core": True, "markets": []},
        ],
    }
    market_pulse._save_snapshot(old)
    stale = time.time() - age_s
    os.utime(market_pulse._snapshot_path(), (stale, stale))


def _stub_pulse_sources(monkeypatch):
    row = {"question": "Will X?", "topic": "货币政策", "volume_24h": 10.0, "source": "polymarket"}
    krow = {"question": "K?", "topic": "宏观经济", "volume_24h": 5.0, "source": "kalshi"}
    monkeypatch.setattr(market_pulse, "_shaped_polymarket",
                        lambda force: asyncio.sleep(0, result=[row]))
    monkeypatch.setattr(market_pulse.kalshi_signals, "fetch_shaped",
                        lambda force: asyncio.sleep(0, result=[krow]))

    async def no_translate(_rows):
        return None

    def must_not_call(*_a, **_k):
        raise AssertionError("data-only rebuild must not call insight LLM")

    monkeypatch.setattr(market_pulse, "_translate", no_translate)
    monkeypatch.setattr(market_pulse, "status_insight", must_not_call)
    monkeypatch.setattr(market_pulse, "overall_insight", must_not_call)
    monkeypatch.setattr(market_pulse, "module_insight", must_not_call)


def test_pulse_data_only_rebuild_carries_old_ai_and_skips_llm(monkeypatch, tmp_path):
    _seed_pulse_snapshot(monkeypatch, tmp_path, age_s=market_pulse.STALE_AFTER_S + 10)
    _stub_pulse_sources(monkeypatch)

    built = asyncio.run(market_pulse._build(include_ai=False))

    by_key = {m["key"]: m for m in built["modules"]}
    assert by_key["货币政策"]["insight"] == "旧卡解读"
    assert "insight" not in by_key["宏观经济"]  # 旧快照没有的卡不编造
    assert built["status"] == "旧现状"
    assert built["overall"] == "旧综合"
    assert built["as_of"] > "2026-08-28"  # 概率数据是新的


def test_pulse_stale_read_triggers_data_only_rebuild(monkeypatch, tmp_path):
    _seed_pulse_snapshot(monkeypatch, tmp_path, age_s=market_pulse.STALE_AFTER_S + 10)
    _stub_pulse_sources(monkeypatch)
    spawned = []

    def spy(include_ai=True):
        spawned.append(include_ai)
        return asyncio.sleep(0)

    monkeypatch.setattr(market_pulse, "_rebuilding", False)
    monkeypatch.setattr(market_pulse, "_background_rebuild", spy)

    overview = asyncio.run(market_pulse.fetch_overview())

    assert spawned == [False], "超龄读触发数据-only（非 force → include_ai=False）"
    assert overview["cache_state"] == "refreshing"
    assert overview["updating"] is True
    assert overview["status"] == "旧现状"  # 读请求立刻返回旧快照


def test_pulse_fresh_read_does_not_rebuild(monkeypatch, tmp_path):
    _seed_pulse_snapshot(monkeypatch, tmp_path, age_s=60)
    _stub_pulse_sources(monkeypatch)
    spawned = []

    def spy(include_ai=True):
        spawned.append(include_ai)
        return asyncio.sleep(0)

    monkeypatch.setattr(market_pulse, "_rebuilding", False)
    monkeypatch.setattr(market_pulse, "_background_rebuild", spy)

    overview = asyncio.run(market_pulse.fetch_overview())

    assert spawned == []
    assert overview["cache_state"] == "fresh"


def test_pulse_force_read_triggers_full_rebuild(monkeypatch, tmp_path):
    _seed_pulse_snapshot(monkeypatch, tmp_path, age_s=60)
    _stub_pulse_sources(monkeypatch)
    spawned = []

    def spy(include_ai=True):
        spawned.append(include_ai)
        return asyncio.sleep(0)

    monkeypatch.setattr(market_pulse, "_rebuilding", False)
    monkeypatch.setattr(market_pulse, "_background_rebuild", spy)

    asyncio.run(market_pulse.fetch_overview(force=True))

    assert spawned == [True]
