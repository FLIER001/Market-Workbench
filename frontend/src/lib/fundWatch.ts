// 自选基金 —— 与自选股同构：localStorage 秒读，登录后自动同步到后端账号（key: fund-watchlist）。

import { syncKeyToBackend } from "@/lib/userData";

const STORAGE_KEY = "vr-fund-watchlist";

export interface FundWatchItem { code: string; name: string; type?: string }

const valid = (v: unknown): FundWatchItem[] =>
  Array.isArray(v)
    ? Array.from(new Map(
        v.filter((x): x is FundWatchItem =>
          !!x && typeof x === "object" && /^\d{6}$/.test((x as FundWatchItem).code))
          .map((x) => [(x as FundWatchItem).code, x]),
      ).values())
    : [];

export function loadFundWatch(): FundWatchItem[] {
  try {
    return valid(JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"));
  } catch {
    return [];
  }
}

export function saveFundWatch(items: FundWatchItem[]) {
  try {
    const payload = JSON.stringify(valid(items));
    localStorage.setItem(STORAGE_KEY, payload);
    syncKeyToBackend(STORAGE_KEY, payload);
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

export function toggleFundWatch(item: FundWatchItem): FundWatchItem[] {
  const list = loadFundWatch();
  const idx = list.findIndex((x) => x.code === item.code);
  const next = idx >= 0 ? list.filter((x) => x.code !== item.code) : [...list, item];
  saveFundWatch(next);
  return next;
}
