// 自选股估值历史分位（近5年，PE-TTM / PB）。
//
// 几个刻意的选择：
// - **一次一码，并发 2**：分位数据来自百度股市通（日频历史序列），批量并发会被上游限流，
//   2 路并发在「首屏全部亮相」和「不给上游添压」之间取个平衡。
// - **localStorage 缓存 7 天**：历史分位一天最多动一次，反复拉是纯粹浪费。
//   缓存按 code 独立，加删自选互不影响。
// - **取不到（ETF/新股/源异常）记 negative-cache**：ETF 本来就没有个股估值分位序列，
//   不记住的话每次进页面都会对每只 ETF 白打一遍请求。
// - **历史值不参与分位绘制**：这里只搬运 current + percentile，分位带在个股数据页。

import { useEffect, useRef, useState } from "react";
import { api, type ValMetric } from "@/lib/api";

const CACHE_KEY = "vr-val-pctile-v1";
const TTL_MS = 7 * 24 * 3600_000;
const CONCURRENCY = 2;

export interface StockValPct {
  pe?: ValMetric;
  pb?: ValMetric;
}

type CacheShape = Record<string, { t: number; v: StockValPct | null }>;

function loadCache(): CacheShape {
  try {
    return JSON.parse(localStorage.getItem(CACHE_KEY) || "{}") as CacheShape;
  } catch {
    return {};
  }
}

function saveCache(cache: CacheShape) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch {
    /* 存储不可用时，本次会话内仍然有效 */
  }
}

export function useValuationPercentiles(codes: string[]): Record<string, StockValPct> {
  const [values, setValues] = useState<Record<string, StockValPct>>(() => {
    const cache = loadCache();
    const now = Date.now();
    const out: Record<string, StockValPct> = {};
    for (const code of codes) {
      const hit = cache[code];
      if (hit && hit.v && now - hit.t < TTL_MS) out[code] = hit.v;
    }
    return out;
  });
  const codesRef = useRef(codes);
  codesRef.current = codes;
  const inFlightRef = useRef(new Set<string>());

  useEffect(() => {
    const cache = loadCache();
    const now = Date.now();
    // 首屏先用缓存里还新鲜的部分铺一遍（自选列表变化时也要补）
    setValues((prev) => {
      const next = { ...prev };
      for (const code of codes) {
        if (next[code]) continue;
        const hit = cache[code];
        if (hit && hit.v && now - hit.t < TTL_MS) next[code] = hit.v;
      }
      return next;
    });

    const queue = codes.filter((code) => {
      const hit = cache[code];
      return !(hit && now - hit.t < TTL_MS) && !inFlightRef.current.has(code);
    });
    if (!queue.length) return;
    // 整队先占位：effect 因自选变化重跑（或 StrictMode 双跑）时，
    // 正在排队但还没开始拉的 code 不会被重复入队。
    queue.forEach((code) => inFlightRef.current.add(code));

    let cancelled = false;
    const runNext = async (): Promise<void> => {
      const code = queue.shift();
      if (!code || cancelled) return;
      try {
        const res = await api.percentile(code);
        const v: StockValPct | null = res?.metrics
          ? { pe: res.metrics.pe_ttm, pb: res.metrics.pb }
          : null;
        cache[code] = { t: Date.now(), v };
        saveCache(cache);
        // 不用 cancelled 拦截这里：上一轮 effect 拉到的数据仍然是新鲜有效的，
        // 值得写进 state（否则 StrictMode 下首跑的结果会被白白丢弃）。
        if (v && (v.pe || v.pb) && codesRef.current.includes(code)) {
          setValues((prev) => ({ ...prev, [code]: v }));
        }
      } catch {
        // 源异常 / ETF 无序列：记 negative-cache，24h 内不再重试（用缩短的 TTL 存 null）
        cache[code] = { t: Date.now() - (TTL_MS - 24 * 3600_000), v: null };
        saveCache(cache);
      } finally {
        inFlightRef.current.delete(code);
        void runNext();
      }
    };

    const workers = Array.from({ length: Math.min(CONCURRENCY, queue.length) }, () => runNext());
    void Promise.all(workers);
    return () => {
      cancelled = true;
      // 还没开始拉的 code 释放占位，让下一轮 effect 能重新排队
      queue.forEach((code) => inFlightRef.current.delete(code));
    };
  }, [codes]);

  return values;
}
