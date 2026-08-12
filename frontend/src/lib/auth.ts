// 用户认证：token 存 localStorage（仅 token，不含密码），用户数据走后端按账号隔离。
// 解决纯 localStorage 存自选时换域名(localhost/127.0.0.1)/换浏览器就丢的问题。

import { storageGet, storageSet, storageRemove } from "./storage";

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
}

export function authHeaders(): Record<string, string> {
  const t = loadToken();
  const access = storageGet("vr-access-key") || "";
  return {
    ...(t ? { Authorization: `Bearer ${t}` } : {}),
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
  getData: () => req<Record<string, unknown>>("/auth/data"),
  setData: (key: string, value: unknown) => req<{ ok: boolean; version: number; updated_at: number }>("/auth/data/set", "POST", { key, value }),
  mergeData: (items: Record<string, unknown>) =>
    req<Record<string, unknown>>("/auth/data/merge", "POST", { items }),
};
