// 用户认证：token 存 localStorage（仅 token，不含密码），用户数据走后端按账号隔离。
// 解决纯 localStorage 存自选时换域名(localhost/127.0.0.1)/换浏览器就丢的问题。

import { storageGet, storageSet, storageRemove } from "./storage";
import { cloud } from "./cloud";
import {
  CLOUD_DATA_FLAG,
  cloudDataActive,
  cloudGetAll,
  cloudMerge,
  cloudMergeMissing,
  cloudReplaceAll,
  cloudSet,
  migrateLocalToCloud,
} from "./cloudData";

const TOKEN_KEY = "vr-auth-token";
const USER_KEY = "vr-auth-user";

export interface AuthUser {
  id: number;
  username: string;
}

export function loadToken(): string {
  return storageGet(TOKEN_KEY) || "";
}

export function loadUser(): AuthUser | null {
  try {
    const raw = storageGet(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

export function saveSession(token: string, user: AuthUser) {
  storageSet(TOKEN_KEY, token);
  storageSet(USER_KEY, JSON.stringify(user));
}

export function clearSession() {
  storageRemove(TOKEN_KEY);
  storageRemove(USER_KEY);
  storageRemove("vr-just-logged-in");
  storageRemove(CLOUD_DATA_FLAG);
}

export function authHeaders(): Record<string, string> {
  const t = loadToken();
  const access = storageGet("vr-access-key") || "";
  return {
    // 两个都发：直连时 Authorization 够用；经过某些网关/反代时它可能被当成网关自身的鉴权头
    // 而不再透传，X-VR-User-Token 是后端同样支持的通道，能兜住这种情况。
    ...(t ? { Authorization: `Bearer ${t}`, "X-VR-User-Token": t } : {}),
    ...(access ? { "X-VR-Access-Key": access } : {}),
  };
}

async function req<T>(path: string, method: "GET" | "POST" = "GET", body?: unknown): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders() };
  const opts: RequestInit = { method };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (Object.keys(headers).length > 0) opts.headers = headers;
  const resp = await fetch(`/api${path}`, opts);
  let payload: any = null;
  try {
    payload = await resp.json();
  } catch {
    /* ignore */
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      clearSession();
      // 会话失效时通知路由守卫跳回登录页
      window.dispatchEvent(new CustomEvent("vr:unauthorized"));
    }
    throw new Error(payload?.detail || `HTTP ${resp.status}`);
  }
  return (payload?.data ?? payload) as T;
}

export const auth = {
  register: (username: string, password: string) =>
    req<{ token: string; username: string; user_id: number; first_user: boolean }>(
      "/auth/register", "POST", { username, password },
    ),
  login: (username: string, password: string) =>
    req<{ token: string; username: string; user_id: number }>("/auth/login", "POST", { username, password }),
  me: () => req<AuthUser>("/auth/me"),
  logout: () => req<{ ok: boolean }>("/auth/logout", "POST"),
  // 云会话用户读写云数据库（RLS 按 owner_id 隔离）；其余会话沿用后端 SQLite。
  getData: async (): Promise<Record<string, unknown>> =>
    (await cloudDataActive()) ? cloudGetAll() : req<Record<string, unknown>>("/auth/data"),
  setData: async (key: string, value: unknown): Promise<{ ok: boolean; version: number; updated_at: number }> => {
    if (await cloudDataActive()) {
      await cloudSet(key, value);
      return { ok: true, version: 1, updated_at: Date.now() / 1000 };
    }
    return req("/auth/data/set", "POST", { key, value });
  },
  mergeData: async (items: Record<string, unknown>): Promise<Record<string, unknown>> =>
    (await cloudDataActive())
      ? cloudMerge(items)
      : req<Record<string, unknown>>("/auth/data/merge", "POST", { items }),
  exportData: async (): Promise<UserDataExport> => {
    const base = await req<UserDataExport>("/auth/export");
    if (await cloudDataActive()) {
      const data = await cloudGetAll();
      const llm = data.llm;
      // 与后端一致：备份文件离开本机前抹掉 AI key。
      if (llm && typeof llm === "object") data.llm = { ...(llm as object), apiKey: "" };
      base.data = data;
      base.user = { username: loadUser()?.username };
    }
    return base;
  },
  importData: async (
    payload: unknown,
    mode: "merge" | "replace",
    ledgersMode: "skip" | "merge" | "replace",
  ): Promise<{ ok: boolean; applied: string[]; skipped_keys: string[]; ledgers_mode: string }> => {
    // 账本始终由后端处理（不在云表里）；user_data 部分按当前数据源落库。
    const result = await req<{ ok: boolean; applied: string[]; skipped_keys: string[]; ledgers_mode: string }>(
      "/auth/import", "POST", { payload, mode, ledgers_mode: ledgersMode },
    );
    if (await cloudDataActive()) {
      const data = (payload as UserDataExport | null)?.data;
      if (data && typeof data === "object") {
        if (mode === "replace") await cloudReplaceAll(data);
        else await cloudMergeMissing(data);
      }
    }
    return result;
  },
};

// ---------------------------------------------------------------------------
// 云身份登录（WorkBuddy 账号）
// 云端只负责认证；拿到 access token 后换一个本地会话，业务接口沿用原有 user_id 隔离。
// ---------------------------------------------------------------------------

function sdkErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "message" in error) {
    const m = (error as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return fallback;
}

/** 跳转到 WorkBuddy 授权页。必须是用户手势触发的同标签页跳转（PKCE 存在 sessionStorage）。 */
export async function startWorkBuddyLogin(): Promise<void> {
  const { data, error } = await cloud.auth.signInWithOAuth({
    provider: "workbuddy",
    redirectTo: window.location.origin,
  });
  if (error) throw new Error(sdkErrorMessage(error, "无法发起 WorkBuddy 登录"));
  if (!data?.url) throw new Error("未获取到授权地址");
  window.location.assign(data.url);
}

/** 浏览器侧先用 SDK 自校验一次，结果随请求上报（不含 token），供服务端诊断对齐。 */
async function clientProbe(): Promise<Record<string, unknown>> {
  try {
    const { data, error } = await cloud.auth.getUser();
    if (error) {
      const e = error as { code?: string; status?: number; message?: string };
      return { ok: false, code: e.code ?? null, status: e.status ?? null, message: (e.message ?? "").slice(0, 120) };
    }
    return { ok: true, id: data?.id ?? null };
  } catch (e) {
    return { ok: false, thrown: String(e).slice(0, 120) };
  }
}

/** 云会话 → 本地会话。 */
export async function bridgeCloudSession(): Promise<void> {
  // 先做浏览器侧自校验：SDK 可能会顺手续期并轮换 token，
  // 所以要等它跑完再取 token，避免拿一枚刚被轮换掉的旧 token 去后端。
  let probe = await clientProbe();
  let accessToken = await cloud.auth.getAccessToken();
  if (!accessToken) {
    const tail = probe.ok ? "" : `（本机校验：${probe.code ?? probe.message ?? "失败"}）`;
    throw new Error(`云会话为空，请重新登录${tail}`);
  }

  const post = async (token: string, p: Record<string, unknown>) => {
    const r = await fetch("/api/auth/cloud", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_token: token, client_probe: p }),
    });
    const payload = (await r.json().catch(() => null)) as
      | { detail?: string; data?: { token: string; user_id: number; username: string } }
      | null;
    return { status: r.status, payload };
  };

  let resp = await post(accessToken, probe);

  // 数据面把 token 判成失效时，强制换一次再试：SDK 的续期是轮换式的，
  // 刚登录就撞上 invalid_grant 往往是一枚已被轮换的旧 token。
  if (resp.status === 401) {
    const refreshed = await cloud.auth.refreshSession();
    const next = refreshed.data?.accessToken;
    if (next) {
      accessToken = next;
      probe = await clientProbe();
      resp = await post(accessToken, probe);
    }
  }

  if (resp.status >= 400 || !resp.payload?.data) {
    const tail = probe.ok ? "本机校验通过" : `本机校验失败：${probe.code ?? probe.thrown ?? probe.message ?? "未知"}`;
    const detail = resp.payload?.detail
      ? `${resp.payload.detail} · ${tail}`
      : `本地会话建立失败（HTTP ${resp.status}）`;
    throw new Error(detail);
  }
  saveSession(resp.payload.data.token, {
    id: resp.payload.data.user_id,
    username: resp.payload.data.username,
  });
  storageSet("vr-just-logged-in", "1");
  // 标记本次登录来自云身份：用户数据读写切到云数据库（RLS 按 owner_id 隔离）。
  storageSet(CLOUD_DATA_FLAG, "1");
  // 首次云登录：云表为空时把浏览器本地数据搬上去；云表已有数据则完全不动。
  // 这一步必然真实读写云数据库（RLS），结果上报给后端，便于线上核验数据面是否可用。
  let dataProbe: Record<string, unknown>;
  try {
    const moved = await migrateLocalToCloud();
    dataProbe = { ok: true, migrated: moved };
    if (moved.length > 0) console.info("[cloud-data] 已迁移本地数据:", moved.join(", "));
  } catch (e) {
    // 迁移失败不阻断登录；本地副本仍在，下次登录会重试。
    console.warn("[cloud-data] 首次迁移失败", e);
    dataProbe = { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
  void fetch("/api/auth/cloud/data-probe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(dataProbe),
  }).catch(() => {});
}

/** 处理第三方登录回调；没有回调参数时返回 false。 */
export async function completeWorkBuddyLogin(): Promise<boolean> {
  const { data, error } = await cloud.auth.handleWorkBuddyCallback(window.location.href);
  if (error) throw new Error(sdkErrorMessage(error, "登录回调处理失败"));
  if (!data) return false;
  await bridgeCloudSession();
  return true;
}

// 用户数据导出文件（/api/auth/export 产物）
export interface UserDataExport {
  format: string;
  version: number;
  exported_at: number;
  exported_at_text: string;
  user: { username?: string };
  data: Record<string, unknown>;
  ledgers?: {
    portfolio?: { holdings?: unknown[] };
    fund_portfolio?: { holdings?: unknown[] };
  };
}
