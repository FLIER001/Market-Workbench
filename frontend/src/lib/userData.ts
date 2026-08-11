// 用户数据层：登录后读写走后端（按账号隔离），未登录/后端不可达时回退 localStorage。
// 这样自选/备注/笔记等在任何浏览器、任何域名下都跟随账号，不再丢失。

import { auth, loadToken } from "./auth";
import { storageGet, storageSet } from "./storage";

const PREFIX = "vr-";

function localKey(key: string): string {
  return PREFIX + key;
}

export function isLoggedIn(): boolean {
  return !!loadToken();
}

// 读：登录了就拉后端，失败回退本地；未登录直接读本地
export async function getUserData<T>(key: string, fallback: T): Promise<T> {
  if (isLoggedIn()) {
    try {
      const all = await auth.getData();
      if (key in all) return all[key] as T;
    } catch {
      /* 网络/后端异常时回退本地 */
    }
  }
  try {
    const raw = storageGet(localKey(key));
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

// 写：始终写本地（秒回 + 离线兜底），登录后同时异步推后端
export function setUserData(key: string, value: unknown): void {
  try {
    storageSet(localKey(key), JSON.stringify(value));
  } catch {
    /* ignore */
  }
  if (isLoggedIn()) {
    auth.setData(key, value).catch(() => {
      /* 推送失败不阻塞，本地已是最新 */
    });
  }
}

const MIGRATABLE_KEYS = [
  "watchlist", "watchlist-groups", "watchlist-notes",
  "etf-watchlist", "etf-watchlist-groups", "notes", "llm", "deep-analysis",
  "fund-watchlist",
];

// 检测本地有哪些数据可导入当前账号（本地有 & 云端还没有）。不自动推，由用户确认后再调 importLocalToAccount。
export async function pendingLocalData(): Promise<{ key: string; count: number; preview: string }[]> {
  if (!isLoggedIn()) return [];
  let remote: Record<string, unknown> = {};
  try {
    remote = await auth.getData();
  } catch {
    return [];
  }
  const out: { key: string; count: number; preview: string }[] = [];
  for (const key of MIGRATABLE_KEYS) {
    if (key in remote) continue; // 云端已有，不导入避免覆盖
    const raw = storageGet(localKey(key));
    if (!raw) continue;
    try {
      const parsed = JSON.parse(raw);
      let count = 0;
      let preview = "";
      if (key === "watchlist-groups" || key === "etf-watchlist-groups") {
        const codes = (parsed?.groups ?? []).flatMap((g: { codes?: string[] }) => g.codes ?? []);
        count = codes.length;
        preview = key === "watchlist-groups" ? "自选股" : "自选 ETF";
      } else if (key === "watchlist-notes") {
        count = Object.keys(parsed ?? {}).length;
        preview = "个股备注";
      } else if (key === "notes") {
        count = Array.isArray(parsed) ? parsed.length : 0;
        preview = "研究记录";
      } else if (key === "llm") {
        count = parsed ? 1 : 0;
        preview = "AI 接入配置";
      } else {
        count = Array.isArray(parsed) ? parsed.length : 1;
        preview = key;
      }
      if (count > 0) out.push({ key, count, preview });
    } catch {
      /* skip 非法 JSON */
    }
  }
  return out;
}

// 用户确认后，把本地数据导入账号（仅导入云端还没有的 key，不覆盖云端）
export async function importLocalToAccount(): Promise<{ migrated: string[] }> {
  if (!isLoggedIn()) return { migrated: [] };
  let remote: Record<string, unknown> = {};
  try {
    remote = await auth.getData();
  } catch {
    return { migrated: [] };
  }
  const toPush: Record<string, unknown> = {};
  for (const key of MIGRATABLE_KEYS) {
    if (key in remote) continue;
    const raw = storageGet(localKey(key));
    if (!raw) continue;
    try {
      toPush[key] = JSON.parse(raw);
    } catch {
      /* skip */
    }
  }
  if (Object.keys(toPush).length === 0) return { migrated: [] };
  try {
    await auth.mergeData(toPush);
  } catch {
    return { migrated: [] };
  }
  return { migrated: Object.keys(toPush) };
}

// 退出登录 / 切换账号时清空本地缓存的用户数据，避免下一个账号读到上一个的数据。
// 只清用户私有数据，保留主题、侧栏折叠这类本机外观设置。
export function clearLocalUserData(): void {
  for (const key of MIGRATABLE_KEYS) {
    try {
      localStorage.removeItem(localKey(key));
    } catch {
      /* ignore */
    }
  }
}

// ---- 轻量同步：现有页面用同步 localStorage 秒读，这里在写入时顺带推后端 ----
// key 映射：localStorage 的 vr-xxx ↔ 后端的 xxx
const SYNC_KEYS = new Set([
  "watchlist", "watchlist-groups", "watchlist-notes",
  "etf-watchlist", "etf-watchlist-groups", "notes", "llm", "deep-analysis",
  "fund-watchlist",
]);

export function syncKeyToBackend(localStorageKey: string, rawValue: string): void {
  if (!isLoggedIn()) return;
  const key = localStorageKey.replace(/^vr-/, "");
  if (!SYNC_KEYS.has(key)) return;
  try {
    auth.setData(key, JSON.parse(rawValue)).catch(() => { /* 离线忽略 */ });
  } catch {
    /* 非法 JSON 忽略 */
  }
}

// 登录后从后端拉取并合并到本地（后端有的覆盖本地，本地独有的推上去）
export async function pullBackendToLocal(): Promise<void> {
  if (!isLoggedIn()) return;
  let remote: Record<string, unknown> = {};
  try {
    remote = await auth.getData();
  } catch {
    return;
  }
  let hasDeepAnalysis = false;
  const remoteDA = (remote["deep-analysis"] || {}) as Record<string, unknown>;
  for (const key of Object.keys(remote)) {
    if (!SYNC_KEYS.has(key)) continue;
    if (key === "deep-analysis") {
      hasDeepAnalysis = true;
      continue; // 这个走 backgroundTasks 恢复，不走 localStorage
    }
    try {
      storageSet(localKey(key), JSON.stringify(remote[key]));
    } catch {
      /* ignore */
    }
  }
  if (hasDeepAnalysis) {
    // 清掉 localStorage 里的 idle 占位，让 backgroundTasks 模块重载时能读到后端的 done
    try {
      const tasks = JSON.parse(storageGet("vr-background-ai-tasks") || "{}") as Record<string, { status?: string; data?: unknown }>;
      let changed = false;
      for (const key of Object.keys(remoteDA)) {
        if (key in tasks && tasks[key]?.status === "idle") {
          delete tasks[key];
          changed = true;
        }
      }
      if (changed) storageSet("vr-background-ai-tasks", JSON.stringify(tasks));
    } catch { /* ignore */ }
    // 通知监听方（main.tsx）把已完成的深度分析任务注入 backgroundTasks
    window.dispatchEvent(new CustomEvent("vr:restore-deep-analysis", { detail: remoteDA }));
  }

  // 反向迁移：本地有但后端没有的已完成深度分析（同步功能上线前跑完的那些），推上去
  try {
    const localTasks = JSON.parse(storageGet("vr-background-ai-tasks") || "{}") as Record<string, { status?: string }>;
    const missing: Record<string, unknown> = {};
    for (const [key, task] of Object.entries(localTasks)) {
      if (!key.startsWith("watchlist-deep-analysis:")) continue;
      if (task.status !== "done" && task.status !== "error") continue;
      if (!(key in remoteDA)) missing[key] = task;
    }
    if (Object.keys(missing).length > 0) {
      const merged = { ...remoteDA, ...missing };
      auth.setData("deep-analysis", merged).catch(() => { /* 下次 pull 再试 */ });
    }
  } catch {
    /* 本地数据异常就跳过迁移 */
  }
}
