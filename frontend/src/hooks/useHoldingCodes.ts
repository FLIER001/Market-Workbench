// 持仓代码集合 —— 供自选等页面轻量知道“哪些代码在我持仓里”。
//
// 只拉一次 /api/portfolio 取 code（30 秒模块级缓存，多组件共用不重复打行情接口）。
// Portfolio 页每次拿到新持仓数据后调用 publishHoldingCodes：写 localStorage 兜底缓存 +
// 广播 vr-holdings-changed，本 hook 监听后立即作废缓存重新拉取，自选页标注/并入随之刷新。

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export const HOLDINGS_CHANGED_EVENT = "vr-holdings-changed";
const LS_KEY = "vr-portfolio-codes";
const CACHE_TTL_MS = 30_000;

let cache: { codes: string[]; at: number } | null = null;
let inflight: Promise<string[]> | null = null;

export function publishHoldingCodes(codes: string[]) {
  cache = { codes, at: Date.now() };
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(codes));
  } catch {
    /* 存储不可用：内存缓存已更新，本次会话照常 */
  }
  window.dispatchEvent(new CustomEvent(HOLDINGS_CHANGED_EVENT));
}

function loadFromStorage(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(LS_KEY) || "[]");
    return Array.isArray(value)
      ? value.filter((code): code is string => typeof code === "string" && /^\d{6}$/.test(code))
      : [];
  } catch {
    return [];
  }
}

async function fetchCodes(): Promise<string[]> {
  try {
    const data = await api.portfolio();
    const codes = (data.holdings || []).map((h) => h.code).filter((code) => /^\d{6}$/.test(code));
    cache = { codes, at: Date.now() };
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(codes));
    } catch {
      /* 同上 */
    }
    return codes;
  } catch {
    // 后端没起 / 行情异常：退回本地缓存，宁可标注旧一点也不清空
    return loadFromStorage();
  }
}

function getCodes(force = false): Promise<string[]> {
  if (!force && cache && Date.now() - cache.at < CACHE_TTL_MS) return Promise.resolve(cache.codes);
  if (!inflight) {
    inflight = fetchCodes().finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

/** 当前持仓代码集合。后端不可用时回退到最近一次成功快照。 */
export function useHoldingCodes(): string[] {
  const [codes, setCodes] = useState<string[]>(() => cache?.codes ?? loadFromStorage());

  useEffect(() => {
    let alive = true;
    const pull = (force: boolean) => {
      getCodes(force).then((next) => {
        if (alive) setCodes(next);
      });
    };
    pull(false);
    const onChanged = () => pull(true);
    window.addEventListener(HOLDINGS_CHANGED_EVENT, onChanged);
    return () => {
      alive = false;
      window.removeEventListener(HOLDINGS_CHANGED_EVENT, onChanged);
    };
  }, []);

  return codes;
}
