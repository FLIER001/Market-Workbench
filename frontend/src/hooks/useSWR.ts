import { useCallback, useEffect, useRef, useState } from "react";
import { loadUser } from "@/lib/auth";

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
 * - 手动「刷新」按钮要求后端绕过软 TTL，但仍复用同键飞行任务，避免重复抓源。
 *
 * 用法：const { data, loading, mutate } = useSWR("key", fetcher, [deps]);
 * 依赖变化时自动重取（缓存仍先渲染）。返回的 setData 兼作 mutate 写入缓存。
 */
export type CachePayload = { cache_state?: "fresh" | "stale" | "refreshing" | "error"; cached_at?: string | null };
type SWROptions = { persist?: boolean; scope?: "public" | "user" };

export function useSWR<T>(key: string, fetcher: (fresh?: boolean) => Promise<T>, deps: readonly unknown[] = [], onError?: (e: unknown) => void, opts?: SWROptions) {
  const scopedKey = opts?.scope === "user" ? `user:${loadUser()?.id ?? "anonymous"}:${key}` : key;
  const cache = (useSWR as unknown as { _c?: Map<string, unknown> })._c ??
    ((useSWR as unknown as { _c: Map<string, unknown> })._c = new Map());
  // 挂载初值：内存缓存 > localStorage 持久化 > null。有任意一层命中即秒开。
  const [data, setData] = useState<T | null>(() => {
    if (cache.has(scopedKey)) return cache.get(scopedKey) as T;
    if (opts?.persist) return loadPersisted<T>(scopedKey);
    return null;
  });
  const [loading, setLoading] = useState(data === null);
  // 任何一次重取在飞行中即为 true（含后台静默重取）。
  // 初值 true：挂载即触发后台重取，图标首帧就转，与“缓存秒开 + 后台更新”状态一致。
  const [revalidating, setRevalidating] = useState(true);
  const alive = useRef(true);
  const dataRef = useRef(data);
  dataRef.current = data;
  // 当前 key：异步回填写入前用它校验，避免上一个 key（如上一个窗口）的轮询结果盖掉新 key 的数据。
  const keyRef = useRef(scopedKey);
  keyRef.current = scopedKey;

  const revalidate = useCallback(async (force = false) => {
    if (force && dataRef.current === null) setLoading(true);
    setRevalidating(true);
    const requestKey = scopedKey;
    const current = () => alive.current && keyRef.current === requestKey;
    // 首帧落地：后端即便在后台重拉，也要先把手里的这批结果交给页面；
    // 轮询只负责后续追新。此前首个响应被轮询循环扣住，切窗口要等 17s 才换表。
    const commit = (v: T) => {
      writeCache(requestKey, v, opts?.persist);
      if (current()) { setData(v); setLoading(false); }
    };
    // 手动刷新也复用同一飞行请求；force 只让首个请求要求后端检查软 TTL。
    let p = inflight.get(scopedKey) as Promise<T> | undefined;
    if (!p) {
      p = (async () => {
        let next = await fetcher(force);
        commit(next);
        // 后端正在后台刷新时继续追新；页面隐藏即暂停，恢复可见后继续。
        for (const delay of REFRESH_POLL_DELAYS) {
          if ((next as CachePayload)?.cache_state !== "refreshing") break;
          await waitUntilVisible(delay);
          if (!current()) break;
          next = await fetcher(false);
          commit(next);
        }
        return next;
      })();
      inflight.set(scopedKey, p);
    }
    try {
      const d = await p;
      if (current()) setData(d);
    } catch (e) {
      onError?.(e);
    } finally {
      if (inflight.get(scopedKey) === p) inflight.delete(scopedKey);
      if (current()) { setLoading(false); setRevalidating(false); }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopedKey, opts?.persist]);

  // 每次挂载/依赖变化都重取；StrictMode 双挂载下，cleanup 只标记本次失效，
  // 真正的 setData 由仍存活的那次执行完成（alive 在 effect 启动时重置为 true）。
  useEffect(() => {
    alive.current = true;
    // 切 key（如切窗口）时先同步换成本 key 的缓存值：命中即秒开，未命中立即回加载态。
    // 此前这里不动 data，切窗口后页面还挂着上一个窗口的行，直到新请求回来才换——「不及时」的来源。
    const cached = cache.has(scopedKey)
      ? (cache.get(scopedKey) as T)
      : (opts?.persist ? loadPersisted<T>(scopedKey) : null);
    setData(cached ?? null);
    setLoading(cached == null);
    revalidate();
    return () => { alive.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revalidate, ...deps]);

  const setAndCache = useCallback((v: T | null) => {
    if (v == null) { cache.delete(scopedKey); clearPersisted(scopedKey); }
    else writeCache(scopedKey, v, opts?.persist);
    setData(v);
  }, [cache, scopedKey, opts?.persist]);

  return { data, setData: setAndCache, loading, revalidating, revalidate };
}

// ---------------------------------------------------------------------------
// 持久化与去重辅助
// ---------------------------------------------------------------------------

// 同一 key 飞行中的请求，用于并发去重（StrictMode 双挂载、多组件同 key 复用）。
const inflight = new Map<string, Promise<unknown>>();

const STORE_PREFIX = "vr-swr:";

// 后端后台重建最长约 80s（40 只候选股的公告串行拉取解析），轮询窗口必须盖过它，
// 否则「完成后自动更新」名不副实：页面停在旧时点，直到下次挂载才追上。
const REFRESH_POLL_DELAYS = [2000, 5000, 10_000, 20_000, 30_000, 30_000, 30_000];

async function waitUntilVisible(delay: number): Promise<void> {
  if (!document.hidden) await new Promise((resolve) => window.setTimeout(resolve, delay));
  if (!document.hidden) return;
  await new Promise<void>((resolve) => {
    const onVisible = () => {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", onVisible);
      resolve();
    };
    document.addEventListener("visibilitychange", onVisible);
  });
}

export function clearUserSWRCache(): void {
  const cache = (useSWR as unknown as { _c?: Map<string, unknown> })._c;
  for (const key of [...(cache?.keys() || [])]) if (key.startsWith("user:")) cache?.delete(key);
  try {
    for (let i = localStorage.length - 1; i >= 0; i -= 1) {
      const key = localStorage.key(i);
      if (key?.startsWith(`${STORE_PREFIX}user:`)) localStorage.removeItem(key);
    }
  } catch { /* storage unavailable */ }
}

/**
 * 后台刷新追新：拿到首个响应就先交给 onValue 上屏，之后每轮轮询结果继续上屏，
 * 最后返回终值。用于自定义页面状态的慢数据集（板块/行业评分、产业链）。
 *
 * 注意：后端 cache_state=refreshing 时响应里带的是上次成功结果（last-good），
 * 不是空壳，所以先上屏不会闪白；首个响应若仍为空由调用方自行守卫。
 */
export async function pollRefreshing<T extends CachePayload>(
  first: T,
  fetcher: () => Promise<T>,
  onValue: (v: T) => void,
): Promise<T> {
  let value = first;
  onValue(value);
  for (const delay of REFRESH_POLL_DELAYS) {
    if (value.cache_state !== "refreshing") break;
    await waitUntilVisible(delay);
    value = await fetcher();
    onValue(value);
  }
  return value;
}

// localStorage 持久化层：opts.persist=true 时启用。隐私模式 / 序列化失败静默降级。
// 写入带时间戳；读取时超过 PERSIST_MAX_AGE_MS 的旧值直接丢弃——后端长期不可达时
// persist 层会一直秒显旧数据（页面看似正常但时点是旧的），过期即回退 loading 态。
const PERSIST_MAX_AGE_MS = 7 * 24 * 60 * 60_000;

function loadPersisted<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(STORE_PREFIX + key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { v: T; t?: number };
    // 旧格式（无 t）是 2026-09 前的遗留值：时点不可知，按最旧处理直接淘汰，
    // 否则它会无限期秒显（曾导致油价页长期显示 8-14 的解读）。
    if (parsed.t == null || Date.now() - parsed.t > PERSIST_MAX_AGE_MS) {
      localStorage.removeItem(STORE_PREFIX + key);
      return null;
    }
    return parsed.v;
  } catch {
    return null;
  }
}

function writeCache<T>(key: string, v: T, persist?: boolean) {
  const cache = (useSWR as unknown as { _c?: Map<string, unknown> })._c;
  cache?.set(key, v);
  if (!persist) return;
  const slot = STORE_PREFIX + key;
  const serialized = safeStringify({ v, t: Date.now() });
  if (serialized == null) return;
  if (putPersisted(slot, serialized)) return;
  // 配额满：淘汰最旧的一批 persist 项后重试一次。仍失败说明不是配额问题
  // （隐私模式 / storage 被禁用），记一笔便于排查——不再静默。
  evictOldestPersisted(slot);
  if (putPersisted(slot, serialized)) return;
  warnPersistFailure(key);
}

// 配额是持久化层唯一真实的失效原因：全站 20+ 处 persist 共用同一个 ~5MB 池
// （localStorage 按 UTF-16 计费，黄金单页 180KB payload 就吃掉约 360KB）。
// 旧写法在 catch 里静默吞掉 QuotaExceededError，一旦写不进去就「永久」失效——
// 现象正是「每次硬刷新都退回首次计算」，而且没有任何可观测信号。
const persistWarned = new Set<string>();

function safeStringify(value: unknown): string | null {
  try {
    return JSON.stringify(value);
  } catch {
    return null;
  }
}

function putPersisted(slot: string, serialized: string): boolean {
  try {
    localStorage.setItem(slot, serialized);
    return true;
  } catch {
    return false;
  }
}

/** 按写入时间淘汰最旧的 25% persist 项（保留当前这次要写的 key）。 */
function evictOldestPersisted(protectSlot: string): void {
  try {
    const entries: { slot: string; t: number }[] = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const slot = localStorage.key(i);
      if (!slot || !slot.startsWith(STORE_PREFIX) || slot === protectSlot) continue;
      let t = 0;
      const raw = localStorage.getItem(slot);
      if (raw) t = (JSON.parse(raw) as { t?: number }).t ?? 0;
      entries.push({ slot, t });
    }
    entries.sort((a, b) => a.t - b.t);
    for (const e of entries.slice(0, Math.max(1, Math.ceil(entries.length * 0.25)))) {
      localStorage.removeItem(e.slot);
    }
  } catch {
    /* storage 不可用：无物可淘汰 */
  }
}

function warnPersistFailure(key: string): void {
  if (persistWarned.has(key)) return;   // 轮询场景下只提示一次，避免刷屏
  persistWarned.add(key);
  console.warn(
    `[swr] 持久化写入失败（配额满或 storage 被禁用）：${key}。` +
    "该 key 将退化为每次访问都重新拉取后端。",
  );
}

function clearPersisted(key: string) {
  try { localStorage.removeItem(STORE_PREFIX + key); } catch { /* ignore */ }
}
