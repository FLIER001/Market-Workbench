"""内存看门狗的安全阀必须可靠——判断写错会导致服务连环重启，所以逐条覆盖。"""

import mem_watchdog


def test_footprint_is_plausible():
    foot = mem_watchdog.footprint_mb()
    assert foot is not None
    assert 1.0 < foot < 4096.0


def test_grace_period_blocks_restart():
    """启动 15 分钟内即使内存已超限也不触发（覆盖冷启动预热本身的峰值）。"""
    now = 10_000.0
    assert mem_watchdog.should_restart(now, now - 60, 900.0, 0.0) is None


def test_cooldown_blocks_restart():
    """距上次自重启不足 30 分钟则整轮禁用——防止判断出错时连环重启。"""
    now = 10_000.0
    started = now - mem_watchdog._GRACE - 1
    assert mem_watchdog.should_restart(now, started, 900.0, now - 60) is None


def test_footprint_over_limit_triggers():
    now = 10_000.0
    started = now - mem_watchdog._GRACE - 1
    foot = mem_watchdog._MAX_FOOTPRINT_MB + 1
    assert mem_watchdog.should_restart(now, started, foot, 0.0) == "mem"


def test_uptime_over_limit_triggers():
    """内存没涨也得定期回收：macOS 上碎片不会自己还回来。"""
    now = 100_000.0
    started = now - mem_watchdog._MAX_UPTIME - 1
    assert mem_watchdog.should_restart(now, started, 100.0, 0.0) == "uptime"


def test_healthy_process_never_restarts():
    now = 100_000.0
    started = now - mem_watchdog._GRACE - 60
    assert mem_watchdog.should_restart(now, started, 300.0, 0.0) is None


def test_unreadable_footprint_does_not_trigger_mem():
    """读不到 footprint（libproc 失败）时按「不触发」处理，退回 uptime 兜底。"""
    now = 10_000.0
    started = now - mem_watchdog._GRACE - 1
    assert mem_watchdog.should_restart(now, started, None, 0.0) is None


def test_stamp_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_watchdog, "_STAMP", str(tmp_path / "stamp"))
    assert mem_watchdog._last_restart() == 0.0
    mem_watchdog._mark_restart(1234.5)
    assert mem_watchdog._last_restart() == 1234.5


def test_disabled_by_env_does_not_start_thread(monkeypatch):
    import threading

    monkeypatch.setattr(mem_watchdog, "_ENABLED", False)
    before = len(threading.enumerate())
    mem_watchdog.start()
    assert len(threading.enumerate()) == before
