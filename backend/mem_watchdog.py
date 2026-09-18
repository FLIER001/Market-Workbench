"""内存看门狗：常驻内存超限或运行过久时主动退出，由 launchd KeepAlive 拉回。

为什么需要它
------------
macOS 上 Python/NumPy 释放的页不会还给系统：实测 2M 行 DataFrame 用完后
`del` + `gc.collect()`，footprint 纹丝不动，`PYTHONMALLOC=malloc` 与
`malloc_zone_pressure_relief()` 也无效（见 .workbuddy/memory/MEMORY.md）。
后端 uvicorn 实测跑 95 分钟即达 692.7MB / 峰值 843.4MB，其中约 500MB 是
「已释放但未归还」的运行时增量。**内存只涨不跌，唯一回收手段是重启进程。**
（2026-09-15 起评分重算已下沉子进程，稳态约 300MB；本模块降级为兜底安全网，
阈值 500MB = 稳态 + 200MB 意外增量余量。）

本机环境禁止 launchd 写操作（`bootstrap`/`load`/`crontab`/`sudo` 全部被拦），
装不了定时重启 job，所以把重启逻辑放进进程内：自己判断、自己退出，
交给已有的 `KeepAlive` 拉起。

安全阀（**改动前务必保留**）
----------------------------
1. 启动宽限：进程起来 `_GRACE` 秒内绝不触发，覆盖冷启动预热本身的内存峰值。
2. 冷却闸门：退出前把时间戳写进 `_STAMP`，启动时读取；距上次自重启不足
   `_MIN_INTERVAL` 秒则整轮禁用。**即使判断逻辑写错也不会连环重启。**
3. 双条件：只有「footprint 超 `_MAX_FOOTPRINT_MB`」或「运行超 `_MAX_UPTIME`」
   才触发，任一条件都不至于误伤。
4. 随时可关：环境变量 `VR_MEM_WATCHDOG=0` 时完全不注册线程。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

_ENABLED = os.environ.get("VR_MEM_WATCHDOG", "1").lower() not in ("0", "false", "no")
_POLL = 60.0              # 每分钟巡检一次（rusage 一次开销可忽略）；间隔越短触发越精准
_GRACE = 15 * 60          # 启动宽限：15 分钟内绝不触发
_MIN_INTERVAL = 30 * 60   # 两次自重启之间的最小间隔（冷却闸门）
_MAX_FOOTPRINT_MB = 500.0  # 稳态约 300MB（评分重算已下沉子进程）；超此线=兜底回收
_MAX_UPTIME = 8 * 3600    # 兜底：即便内存没涨，跑满 8 小时也回收一次

_STAMP = os.path.join(
    os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research"),
    "backend_last_selfrestart",
)

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
_RUSAGE_INFO_V2 = 2


def footprint_mb() -> float | None:
    """当前进程 physical footprint（用户体感的内存，与活动监视器同口径）。

    注意读的是 rusage_info_v2 的 offset 72；offset 80 是 ri_proc_start_abstime，
    读错了会得到一个荒谬的大值。取不到时返回 None，调用方按「不触发」处理。
    """
    buf = (ctypes.c_uint8 * 2048)()
    if _libc.proc_pid_rusage(ctypes.c_int(os.getpid()), _RUSAGE_INFO_V2, ctypes.byref(buf)) != 0:
        return None
    return ctypes.c_uint64.from_buffer(buf, 72).value / 1048576.0


def should_restart(now: float, started: float, foot: float | None,
                   last_restart: float) -> str | None:
    """纯判定：该重启就返回原因（"mem" / "uptime"），否则 None。便于单测覆盖。"""
    if now - started < _GRACE:
        return None
    if now - last_restart < _MIN_INTERVAL:
        return None
    if foot is not None and foot > _MAX_FOOTPRINT_MB:
        return "mem"
    if now - started > _MAX_UPTIME:
        return "uptime"
    return None


def _last_restart() -> float:
    try:
        with open(_STAMP, encoding="utf-8") as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return 0.0


def _mark_restart(now: float) -> None:
    try:
        os.makedirs(os.path.dirname(_STAMP), exist_ok=True)
        with open(_STAMP, "w", encoding="utf-8") as handle:
            handle.write(str(now))
    except OSError:
        pass


def _loop(started: float) -> None:
    while True:
        time.sleep(_POLL)
        now = time.time()
        reason = should_restart(now, started, footprint_mb(), _last_restart())
        if reason is None:
            continue
        _mark_restart(now)
        foot = footprint_mb()
        foot_text = f"{foot:.0f}MB" if foot is not None else "n/a"
        print(
            f"[mem-watchdog] 触发回收（{reason}）：footprint={foot_text} "
            f"uptime={(now - started) / 3600:.1f}h → 主动退出，"
            f"KeepAlive 拉起后由磁盘快照兜底（首请求毫秒级）",
            flush=True,
        )
        os._exit(0)


def start() -> None:
    if not _ENABLED:
        return
    threading.Thread(
        target=_loop, args=(time.time(),), daemon=True, name="mem-watchdog"
    ).start()
    # 留一行启动日志：这套机制只在「该回收」时才出声，不打印的话运维无法确认它
    # 究竟有没有挂上（线程数会被预热线程淹没，看不出来）。
    print(
        f"[mem-watchdog] 已挂载：footprint 超 {_MAX_FOOTPRINT_MB:.0f}MB 或运行超 "
        f"{_MAX_UPTIME / 3600:.0f}h 时主动退出回收内存，由 KeepAlive 拉起"
        f"（启动宽限 {_GRACE // 60}min / 冷却 {_MIN_INTERVAL // 60}min）",
        flush=True,
    )
