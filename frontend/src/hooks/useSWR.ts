import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 轻量 SWR：模块级缓存 + 后台重取。
 *
 * - 首次（缓存为空）：拉数据，loading=true，组件显示加载态；
 * - 再次挂载（缓存有值）：立即返回缓存（loading=false，秒开），
 *   同时后台重新拉取，成功后静默替换；
 * - 内存缓存仅存活于本次会话（模块级 Map，刷新页面即清）；
 *   传 opts.persist=true 时再叠加一层 localStorage 持久化——
 *   刷新页面 / 重开浏览器后仍先用上次结果秒开，后台拉新静默替换。
 *   适合基金等「后端冷启慢、但用户期望即开即看」的页面。
 * - 同一 key 的并发请求自动去重（首个飞行中的请求复用，不重复打后端）；
 * - 手动「刷新」按钮仍走 fetcher 强制拉新（绕过去重）。
 *
 * 用法：const { data, loading, mutate } = useSWR("key", fetcher, [deps]);
 * 依赖变化时自动重取（缓存仍先渲染）。返回的 setData 兼作 mutate 写入缓存。
 */
export function useSWR<T>(key: string, fetcher: () => Promise<T>, deps: readonly unknown[] = [], onError?: (e: unknown) => void, opts?: { persist?: boolean }) {
  const cache = (useSWR as unknown as { _c?: Map<string, unknown> })._c ??
    ((useSWR as unknown as { _c: Map<string, unknown> })._c = new Map());
  // 挂载初值：内存缓存 > localStorage 持久化 > null。有任意一层命中即秒开。
  const [data, setData] = useState<T | null>(() => {
    if (cache.has(key)) return cache.get(key) as T;
    if (opts?.persist) return loadPersisted<T>(key);
    return null;
  });
  const [loading, setLoading] = useState(data === null);
  // 任何一次重取在飞行中即为 true（含后台静默重取）。
  // 初值 true：挂载即触发后台重取，图标首帧就转，与“缓存秒开 + 后台更新”状态一致。
  const [revalidating, setRevalidating] = useState(true);
  const alive = useRef(true);

  const revalidate = useCallback(async (force = false) => {
    if (force) setLoading(true);
    setRevalidating(true);
    // 并发去重：同一 key 已有飞行中的请求时复用其 Promise，不重复打后端。
    // force=true（手动刷新）绕过去重，强制重新拉取。
    let p = (force ? undefined : inflight.get(key)) as Promise<T> | undefined;
    if (!p) {
      p = (async () => fetcher())();
      if (!force) inflight.set(key, p);
    }
    try {
      const d = await p;
      writeCache(key, d, opts?.persist);
      if (alive.current) setData(d);
    } catch (e) {
      onError?.(e);
    } finally {
      if (!force) inflight.delete(key);
      if (alive.current) { setLoading(false); setRevalidating(false); }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, opts?.persist]);

  // 每次挂载/依赖变化都重取；StrictMode 双挂载下，cleanup 只标记本次失效，
  // 真正的 setData 由仍存活的那次执行完成（alive 在 effect 启动时重置为 true）。
  useEffect(() => {
    alive.current = true;
    revalidate();
    return () => { alive.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revalidate, ...deps]);

  const setAndCache = useCallback((v: T | null) => {
    if (v == null) { cache.delete(key); clearPersisted(key); }
    else writeCache(key, v, opts?.persist);
    setData(v);
  }, [cache, key, opts?.persist]);

  return { data, setData: setAndCache, loading, revalidating, revalidate };
}

// ---------------------------------------------------------------------------
// 持久化与去重辅助
// ---------------------------------------------------------------------------

// 同一 key 飞行中的请求，用于并发去重（StrictMode 双挂载、多组件同 key 复用）。
const inflight = new Map<string, Promise<unknown>>();

const STORE_PREFIX = "vr-swr:";

// localStorage 持久化层：opts.persist=true 时启用。隐私模式 / 序列化失败静默降级。
function loadPersisted<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(STORE_PREFIX + key);
    if (!raw) return null;
    return (JSON.parse(raw) as { v: T }).v;
  } catch {
    return null;
  }
}

function writeCache<T>(key: string, v: T, persist?: boolean) {
  const cache = (useSWR as unknown as { _c?: Map<string, unknown> })._c;
  cache?.set(key, v);
  if (persist) {
    try { localStorage.setItem(STORE_PREFIX + key, JSON.stringify({ v })); } catch { /* 配额满 / 隐私模式：跳过持久化 */ }
  }
}

function clearPersisted(key: string) {
  try { localStorage.removeItem(STORE_PREFIX + key); } catch { /* ignore */ }
}
