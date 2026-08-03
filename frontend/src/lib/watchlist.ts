// 自选股 / 自选 ETF —— 本地 localStorage 秒读，登录后自动同步到后端账号。
// 行情复用 /api/quote；复盘仍只读取自选股，ETF 清单保持独立。

import { syncKeyToBackend } from "@/lib/userData";

export type WatchCollection = "stock" | "etf";
const STORAGE_KEYS: Record<WatchCollection, { flat: string; groups: string }> = {
  stock: { flat: "vr-watchlist", groups: "vr-watchlist-groups" },
  etf: { flat: "vr-etf-watchlist", groups: "vr-etf-watchlist-groups" },
};
const STOCK_NOTES_KEY = "vr-watchlist-notes";
export const WATCH_NOTE_MAX_LENGTH = 120;
export const DEFAULT_WATCH_GROUP_ID = "default";

export interface WatchGroup {
  id: string;
  name: string;
  codes: string[];
}

export type WatchNotes = Record<string, string>;

const validCodes = (value: unknown): string[] =>
  Array.isArray(value)
    ? Array.from(new Set(value.filter((code): code is string => typeof code === "string" && /^\d{6}$/.test(code))))
    : [];

const loadLegacyCodes = (collection: WatchCollection): string[] => {
  try {
    return validCodes(JSON.parse(localStorage.getItem(STORAGE_KEYS[collection].flat) || "[]"));
  } catch {
    return [];
  }
};

const normalizeGroups = (value: unknown, legacyCodes: string[] = []): WatchGroup[] => {
  const source = Array.isArray(value) ? value : [];
  const seenIds = new Set<string>();
  const seenCodes = new Set<string>();
  const groups: WatchGroup[] = [];

  for (const item of source) {
    if (!item || typeof item !== "object") continue;
    const raw = item as Partial<WatchGroup>;
    const id = typeof raw.id === "string" ? raw.id.trim() : "";
    const name = typeof raw.name === "string" ? raw.name.trim().slice(0, 24) : "";
    if (!id || !name || seenIds.has(id)) continue;
    seenIds.add(id);
    const codes = validCodes(raw.codes).filter((code) => {
      if (seenCodes.has(code)) return false;
      seenCodes.add(code);
      return true;
    });
    groups.push({ id, name, codes });
  }

  let defaultGroup = groups.find((group) => group.id === DEFAULT_WATCH_GROUP_ID);
  if (!defaultGroup) {
    defaultGroup = { id: DEFAULT_WATCH_GROUP_ID, name: "未分组", codes: [] };
    groups.unshift(defaultGroup);
  } else {
    defaultGroup.name = "未分组";
  }
  for (const code of legacyCodes) {
    if (!seenCodes.has(code)) {
      defaultGroup.codes.push(code);
      seenCodes.add(code);
    }
  }
  return groups;
};

export function flattenWatchGroups(groups: WatchGroup[]): string[] {
  return Array.from(new Set(groups.flatMap((group) => group.codes)));
}

export function loadWatchGroups(collection: WatchCollection = "stock"): WatchGroup[] {
  const legacy = loadLegacyCodes(collection);
  try {
    const payload = JSON.parse(localStorage.getItem(STORAGE_KEYS[collection].groups) || "null");
    return normalizeGroups(payload?.groups, legacy);
  } catch {
    return normalizeGroups([], legacy);
  }
}

export function saveWatchGroups(groups: WatchGroup[], collection: WatchCollection = "stock") {
  const normalized = normalizeGroups(groups);
  try {
    const groupsPayload = JSON.stringify({ version: 1, groups: normalized });
    localStorage.setItem(STORAGE_KEYS[collection].groups, groupsPayload);
    syncKeyToBackend(STORAGE_KEYS[collection].groups, groupsPayload);
    // 保留旧扁平键，供旧版本及尚未改造的联动页面读取。
    const flatPayload = JSON.stringify(flattenWatchGroups(normalized));
    localStorage.setItem(STORAGE_KEYS[collection].flat, flatPayload);
    syncKeyToBackend(STORAGE_KEYS[collection].flat, flatPayload);
  } catch {
    /* 存储不可用时，本次页面状态仍可继续使用 */
  }
}

export function loadWatch(collection: WatchCollection = "stock"): string[] {
  return flattenWatchGroups(loadWatchGroups(collection));
}

export function saveWatch(codes: string[], collection: WatchCollection = "stock") {
  const wanted = new Set(validCodes(codes));
  const groups = loadWatchGroups(collection).map((group) => ({
    ...group,
    codes: group.codes.filter((code) => wanted.has(code)),
  }));
  const assigned = new Set(flattenWatchGroups(groups));
  const defaultGroup = groups.find((group) => group.id === DEFAULT_WATCH_GROUP_ID)!;
  for (const code of wanted) {
    if (!assigned.has(code)) defaultGroup.codes.push(code);
  }
  saveWatchGroups(groups, collection);
}

export function loadWatchNotes(): WatchNotes {
  try {
    const payload = JSON.parse(localStorage.getItem(STOCK_NOTES_KEY) || "{}");
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
    return Object.fromEntries(
      Object.entries(payload)
        .filter(([code, note]) => /^\d{6}$/.test(code) && typeof note === "string")
        .map(([code, note]) => [code, (note as string).trim().slice(0, WATCH_NOTE_MAX_LENGTH)])
        .filter(([, note]) => note.length > 0),
    );
  } catch {
    return {};
  }
}

export function saveWatchNotes(notes: WatchNotes) {
  try {
    const payload = JSON.stringify(notes);
    localStorage.setItem(STOCK_NOTES_KEY, payload);
    syncKeyToBackend(STOCK_NOTES_KEY, payload);
  } catch {
    /* 存储不可用时，本次页面状态仍可继续使用 */
  }
}

// 从任意文本里抽取 6 位 A 股代码（逗号 / 空格 / 换行 / 顿号分隔都行，方便一次粘贴一串）。
export function parseCodes(raw: string): string[] {
  const tokens = raw.split(/[^\d]+/).filter(Boolean);
  return Array.from(new Set(tokens.filter((t) => /^\d{6}$/.test(t))));
}

export function isEtfCode(code: string): boolean {
  return /^5\d{5}$/.test(code) || /^(15|16)\d{4}$/.test(code);
}

// 把用户输入的一串代码并入已有自选，返回去重后的新列表 + 实际新增数量。
export function addCodes(existing: string[], raw: string): { next: string[]; added: number } {
  const incoming = parseCodes(raw).filter((c) => !existing.includes(c));
  return { next: [...existing, ...incoming], added: incoming.length };
}

export function assignCodesToGroup(
  groups: WatchGroup[],
  groupId: string,
  raw: string,
): { next: WatchGroup[]; added: number; moved: number; unchanged: number } {
  const normalized = normalizeGroups(groups);
  const targetId = normalized.some((group) => group.id === groupId)
    ? groupId
    : DEFAULT_WATCH_GROUP_ID;
  const incoming = parseCodes(raw);
  const currentGroupByCode = new Map<string, string>();
  for (const group of normalized) {
    for (const code of group.codes) currentGroupByCode.set(code, group.id);
  }
  const added = incoming.filter((code) => !currentGroupByCode.has(code)).length;
  const moved = incoming.filter((code) => {
    const current = currentGroupByCode.get(code);
    return current != null && current !== targetId;
  }).length;
  const unchanged = incoming.length - added - moved;
  const incomingSet = new Set(incoming);
  const next = normalized.map((group) => ({
    ...group,
    codes: group.codes.filter((code) => !incomingSet.has(code)),
  }));
  const target = next.find((group) => group.id === targetId)!;
  target.codes.push(...incoming);
  return { next, added, moved, unchanged };
}
