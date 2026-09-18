"""Small stale-while-revalidate cache shared by expensive backend datasets.

The cache never discards a valid last-good value because of age.  Age only
decides whether one background refresh should be started.  Callers keep
ownership of persistence and validation so this module stays deliberately
boring and dependency-free.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

BEIJING = timezone(timedelta(hours=8))
_BACKOFF = (60, 300, 900, 1800)
# Cold builds hit slow upstreams (akshare etc.); a waiter must not hang forever
# if the owner wedges.  Past the deadline the caller gets the error decoration.
_COLD_WAIT_MAX = 120.0


@dataclass
class _Entry:
    value: Any = None
    cached_at: float = 0.0
    refreshing: bool = False
    error: str | None = None
    failures: int = 0
    retry_at: float = 0.0


_entries: dict[str, _Entry] = {}
_lock = threading.RLock()

# 按键维度天然无上限的缓存族（如 stock:<endpoint>:<code>——浏览过的股票越多、
# 常驻条目越多）。这类缓存必须定期淘汰，否则进程常驻内存只涨不跌。
# 「key 数量固定」的缓存（整页评分、单载荷快照）不要注册：淘汰它们只会触发
# 无谓重算，拿不到任何内存收益。
_groups: list[tuple[str, int]] = []


def register_group(prefix: str, keep: int) -> None:
    """声明某 key 前缀的常驻条数上限，超出后按 cached_at 淘汰最旧的。

    只对「键随用户输入无限增长」的缓存族调用。keep 的口径是
    「常用窗口 × 该族的端点种类数」，与磁盘侧 stock_cache.MAX_STOCKS 对齐即可。
    """
    _groups[:] = [g for g in _groups if g[0] != prefix]
    _groups.append((prefix, keep))


def _trim_group(key: str) -> None:
    """写入 key 后触发：命中前缀则做一次 LRU 淘汰，最多处理一组。

    只在写入路径（_refresh / get 的 warm 分支）调用，读路径零开销。
    `keep` 只约束「已落定」的条目：正在后台刷新的（含本次刚写入的那条）
    不计入也不淘汰，否则刚算好的结果会被自己清掉。
    """
    for prefix, keep in _groups:
        if not key.startswith(prefix):
            continue
        group = [k for k, entry in _entries.items()
                 if k.startswith(prefix) and not entry.refreshing]
        if len(group) > keep:
            group.sort(key=lambda k: _entries[k].cached_at)
            for old in group[:-keep]:
                _entries.pop(old, None)
        return


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, BEIJING).isoformat(timespec="seconds")


def _as_of(value: dict) -> str | None:
    for key in ("data_as_of", "as_of", "date", "quote_time", "updated", "generated_at"):
        if value.get(key):
            return str(value[key])
    return None


def _decorate(value: Any, entry: _Entry, state: str | None = None) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    resolved = state or ("refreshing" if entry.refreshing else "error" if entry.error else "fresh")
    out["cache_state"] = resolved
    out["cached_at"] = _fmt(entry.cached_at) if entry.cached_at else None
    out["data_as_of"] = _as_of(value)
    out["refresh_error"] = entry.error
    return out


def _time_from_value(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("cached_at") or value.get("generated_at") or value.get("updated")
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.timestamp()


def peek(key: str) -> Any:
    """Return the undecorated last-good value for partial-build merging."""
    with _lock:
        return _entries.get(key, _Entry()).value


def update_value(key: str, mutator: Callable[[Any], Any]) -> Any:
    """Mutate the last-good value in place under the lock and return it.

    The official way to patch a cached payload (e.g. merging an AI insight
    into the stored dict) without relying on ``peek`` handing out the live
    object.  Returns ``None`` when the key has no value yet; the mutator's
    non-None return value replaces the stored one.
    """
    with _lock:
        entry = _entries.get(key)
        if entry is None or entry.value is None:
            return None
        result = mutator(entry.value)
        if result is not None:
            entry.value = result
        return entry.value


def seed(key: str, value: Any, cached_at: float | None = None) -> None:
    if value is None:
        return
    with _lock:
        entry = _entries.setdefault(key, _Entry())
        if entry.value is None:
            entry.value = value
            entry.cached_at = cached_at or time.time()


def swap_in(key: str, value: Any, cached_at: float | None = None) -> None:
    """用磁盘快照原子替换内存值，供「子进程重建完成后的主进程换入」用。

    与 seed 的区别：无条件覆盖并清掉错误/退避状态。请求路径在锁内
    只会看到旧值或新值，**绝不会看到 miss**——这是调度器把重算下沉到
    子进程后，主进程拾取结果的安全通道（invalidate+get 有毫秒级
    冷建竞态，绝不能用在有真实流量的 key 上）。
    """
    if value is None:
        return
    with _lock:
        entry = _entries.setdefault(key, _Entry())
        entry.value = value
        entry.cached_at = cached_at or time.time()
        entry.error = None
        entry.failures = 0
        entry.retry_at = 0.0


def invalidate(key: str) -> None:
    """Hard invalidation is for schema/business invalidation, never mere age."""
    with _lock:
        _entries.pop(key, None)


def usable_snapshot(
    value: Any,
    required: tuple[str, ...],
    schema_version: Any | None = None,
) -> Any:
    """快照能力探测：关键字段齐全即可用作冷启动兜底，不要求 schema_version 相等。

    各模块的 warm() 原先普遍写成 `payload.get("schema_version") == N`，等于把
    「快照可用」绑死在 payload 结构不变上：任何一次结构演进都会让整张快照被判废，
    调用方随即走「无可用值 → 同步重建」分支，进程重启后首个请求要干等一次全量
    冷建（黄金实测 50s、油价 1-2 分钟），前端整段停在「首次计算中」。

    改为能力探测后，结构演进只让快照降级为兜底骨架（打 degraded 标记，由调用方
    后台刷新覆盖），而不是直接作废。required 支持点号嵌套路径（如 "timing.regime"）；
    建议直接复用调用方 cache_runtime.get(valid=...) 的那份判定条件，保持契约一致。

    注意：本函数只适用于「整张快照兜底」的单载荷缓存。按天追加的历史序列库
    （sector_scores / sw_level2_scores 的 snapshots 归档）不适用——那里版本门是
    正确语义，放宽会把不兼容的旧行合并进历史。
    """
    if not isinstance(value, dict):
        return None
    for path in required:
        node: Any = value
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if not node:
            return None
    if schema_version is not None and value.get("schema_version") != schema_version:
        value = dict(value)
        value["degraded"] = True
    return value


def _refresh(
    key: str,
    build: Callable[[], Any],
    valid: Callable[[Any], bool],
    save: Callable[[Any], None] | None,
) -> None:
    try:
        value = build()
        if not valid(value):
            raise ValueError("refresh returned invalid data")
        if save:
            save(value)
        now = time.time()
        with _lock:
            entry = _entries.setdefault(key, _Entry())
            entry.value = value
            entry.cached_at = now
            entry.error = None
            entry.failures = 0
            entry.retry_at = 0.0
            _trim_group(key)
    except Exception as exc:  # noqa: BLE001 - boundary stores failure for UI
        with _lock:
            entry = _entries.setdefault(key, _Entry())
            entry.failures += 1
            entry.error = str(exc)
            entry.retry_at = time.time() + _BACKOFF[min(entry.failures, len(_BACKOFF)) - 1]
    finally:
        with _lock:
            _entries.setdefault(key, _Entry()).refreshing = False


def get(
    key: str,
    build: Callable[[], Any],
    *,
    valid: Callable[[Any], bool] = bool,
    ttl: float,
    warm: Callable[[], Any] | None = None,
    warm_time: Callable[[], float] | None = None,
    save: Callable[[Any], None] | None = None,
    force: bool = False,
    decorate: bool = True,
) -> Any:
    """Return last-good immediately and refresh stale data once in background.

    With no usable value, the first request builds synchronously because there
    is nothing truthful to render.  ``force`` bypasses TTL/backoff but reuses an
    already-running refresh.
    """
    now = time.time()
    with _lock:
        entry = _entries.setdefault(key, _Entry())
        if entry.value is None and warm:
            warmed = warm()
            if warmed is not None:
                if isinstance(warmed, tuple) and len(warmed) == 2:
                    entry.cached_at, entry.value = float(warmed[0]), warmed[1]
                else:
                    entry.value = warmed
                    # 快照里若带自身的数据时点（as_of/updated 等），以它为准并封顶：
                    # 进程重启高频（launchd KeepAlive）时文件 mtime 恒新，但数据可能
                    # 是很久前的——按 mtime 判 fresh 会让页面长期展示旧数据时点。
                    entry.cached_at = warm_time() if warm_time else min(
                        now, _time_from_value(warmed) or now)
                _trim_group(key)

        if entry.value is not None:
            stale = force or now - entry.cached_at >= ttl
            can_start = not entry.refreshing and (force or now >= entry.retry_at)
            if stale and can_start:
                entry.refreshing = True
                threading.Thread(
                    target=_refresh, args=(key, build, valid, save), daemon=True,
                    name=f"cache-refresh:{key}",
                ).start()
            state = "refreshing" if entry.refreshing else "error" if entry.error else "stale" if stale else "fresh"
            return _decorate(entry.value, entry, state) if decorate else entry.value

        # Mark the cold build as in-flight so concurrent callers cannot start a
        # second external request.  They have no value to return, so wait below.
        if not entry.refreshing:
            entry.refreshing = True
            cold_owner = True
        else:
            cold_owner = False

    if cold_owner:
        _refresh(key, build, valid, save)
    else:
        # Cold starts are rare; wait for the owner instead of building twice.
        # Bounded so a wedged owner cannot hang this caller forever — falling
        # through returns the error decoration below.
        deadline = time.time() + _COLD_WAIT_MAX
        while time.time() < deadline:
            with _lock:
                if not _entries[key].refreshing:
                    break
            time.sleep(0.02)

    with _lock:
        entry = _entries[key]
        if entry.value is not None:
            return _decorate(entry.value, entry) if decorate else entry.value
        if not decorate:
            return None
        return {
            "cache_state": "error",
            "cached_at": None,
            "data_as_of": None,
            "refresh_error": entry.error or "no usable data",
        }


def reset_for_tests() -> None:
    with _lock:
        _entries.clear()
        # 组配置随状态一起清掉：测试各自 register_group，避免相互串味。
        _groups.clear()
