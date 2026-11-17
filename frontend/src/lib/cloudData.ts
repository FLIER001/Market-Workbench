// 云数据库用户数据层。
//
// 表 public.user_data(owner_id, key, value, updated_at)：owner_id 由数据库 DEFAULT auth.uid()
// 填充，RLS 策略只放行 owner_id = auth.uid() 的行。所以这里绝不传 owner_id / token / user id，
// 身份由 SDK 的共享请求层自动带上。
//
// 只有「通过 WorkBuddy 账号登录」且云会话仍有效时才走这里；账号密码登录仍走后端 SQLite，
// 避免两个数据源互相覆盖。
import { cloud, CLOUD_ENABLED } from "./cloud";
import { storageGet } from "./storage";

const TABLE = "user_data";
export const CLOUD_DATA_FLAG = "vr-cloud-data";

// 与后端 users.ALLOWED_KEYS 保持一致，防止往云里塞任意 key。
const ALLOWED_KEYS = new Set([
  "watchlist", "watchlist-groups", "watchlist-notes",
  "etf-watchlist", "etf-watchlist-groups",
  "notes", "llm", "deep-analysis",
  "fund-watchlist",
]);

type Envelope<T> = { data: T | null; error: { message?: string } | null };

function unwrap<T>(result: Envelope<T>): T {
  if (result.error) throw new Error(result.error.message || "云数据库请求失败");
  return (result.data ?? []) as T;
}

/** 当前会话是否应该读写云数据库。 */
export async function cloudDataActive(): Promise<boolean> {
  if (!CLOUD_ENABLED) return false;
  if (storageGet(CLOUD_DATA_FLAG) !== "1") return false;
  try {
    const { data } = await cloud.auth.getSession();
    return !!data;
  } catch {
    return false;
  }
}

/** 读全部数据，形状与后端 /api/auth/data 一致：{ key: value }。 */
export async function cloudGetAll(): Promise<Record<string, unknown>> {
  const rows = unwrap<{ key: string; value: unknown }[]>(
    await cloud.database.from(TABLE).select("key, value"),
  );
  const out: Record<string, unknown> = {};
  for (const row of rows) out[row.key] = row.value;
  return out;
}

/** 写入单个 key（先更新，没命中再插入）。 */
export async function cloudSet(key: string, value: unknown): Promise<void> {
  if (!ALLOWED_KEYS.has(key)) throw new Error(`不允许的数据 key: ${key}`);
  const now = new Date().toISOString();
  const updated = unwrap<{ key: string }[]>(
    await cloud.database
      .from(TABLE)
      .update({ value, updated_at: now })
      .eq("key", key)
      .select("key"),
  );
  if (updated.length > 0) return;
  unwrap(await cloud.database.from(TABLE).insert({ key, value }));
}

/** 批量写入，返回写入后的全量数据（与后端 /api/auth/data/merge 一致）。 */
export async function cloudMerge(items: Record<string, unknown>): Promise<Record<string, unknown>> {
  const existing = unwrap<{ key: string }[]>(
    await cloud.database.from(TABLE).select("key"),
  );
  const have = new Set(existing.map((r) => r.key));
  const fresh: { key: string; value: unknown }[] = [];
  for (const [key, value] of Object.entries(items)) {
    if (!ALLOWED_KEYS.has(key)) continue;
    if (have.has(key)) await cloudSet(key, value);
    else fresh.push({ key, value });
  }
  if (fresh.length > 0) unwrap(await cloud.database.from(TABLE).insert(fresh));
  return cloudGetAll();
}

/** 导入备份：只补云表里还没有的 key，不覆盖线上较新的数据。 */
export async function cloudMergeMissing(items: Record<string, unknown>): Promise<void> {
  const existing = unwrap<{ key: string }[]>(
    await cloud.database.from(TABLE).select("key"),
  );
  const have = new Set(existing.map((r) => r.key));
  const fresh = Object.entries(items)
    .filter(([key]) => ALLOWED_KEYS.has(key) && !have.has(key))
    .map(([key, value]) => ({ key, value }));
  if (fresh.length > 0) unwrap(await cloud.database.from(TABLE).insert(fresh));
}

/** 导入备份（replace 模式）：清掉自己所有行后整批写回。 */
export async function cloudReplaceAll(items: Record<string, unknown>): Promise<void> {
  unwrap(
    await cloud.database.from(TABLE).delete().neq("key", ""),
  );
  const rows = Object.entries(items)
    .filter(([key]) => ALLOWED_KEYS.has(key))
    .map(([key, value]) => ({ key, value }));
  if (rows.length > 0) unwrap(await cloud.database.from(TABLE).insert(rows));
}

/** 首次云登录：云表为空时，把浏览器本地已有的数据搬上去。
 *  云表一旦有数据就完全不动，避免用旧浏览器副本覆盖较新的线上数据。 */
export async function migrateLocalToCloud(): Promise<string[]> {
  if ((await cloudDataActive()) === false) return [];
  const existing = await cloudGetAll();
  if (Object.keys(existing).length > 0) return [];

  const items: Record<string, unknown> = {};
  for (const key of ALLOWED_KEYS) {
    const raw = storageGet("vr-" + key);
    if (!raw) continue;
    try {
      items[key] = JSON.parse(raw);
    } catch {
      /* 本地脏数据跳过 */
    }
  }
  if (Object.keys(items).length === 0) return [];
  await cloudMerge(items);
  return Object.keys(items);
}
