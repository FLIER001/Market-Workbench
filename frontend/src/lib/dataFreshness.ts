// 顶栏「数据时点」：取本页载荷里后端自己标注的最新时点（as_of / updated /
// generated_at…），不是浏览器发出请求的时刻——同一份缓存可能比这次请求早几小时。
// 按路由存放，切页即取本页时点，别的页面刷新的时间不会串过来。
const byPath = new Map<string, DataTime & { tier: 1 | 2 }>();
const listeners = new Set<() => void>();

export type DataTime = { ms: number; hasTime: boolean };

// 一档：后端给的数据自身时点；二档：缓存/抓取时间，可作退路。
const TIER1 = new Set(["as_of", "data_as_of", "score_as_of", "source_as_of", "updated", "updated_at", "generated_at", "built_at", "observed_at", "calculated_at", "quote_time", "quote_date"]);
const TIER2 = new Set(["cached_at", "fetched_at", "assembled_at", "stale_since", "created_at", "ts"]);
// 载荷可能上千行（K 线/成分股），扫到上限就停：时点字段都在浅层。
const MAX_NODES = 4000;
const MAX_DEPTH = 4;

export function currentPath(): string {
  return typeof window === "undefined" ? "" : window.location?.pathname ?? "";
}

/** 记一份载荷的时点：一档覆盖二档，同档取更晚的。 */
export function markDataPayload(path: string, payload: unknown): void {
  const found = scanPayload(payload);
  if (!found) return;
  const prev = byPath.get(path);
  if (prev && (prev.tier < found.tier || (prev.tier === found.tier && prev.ms >= found.ms))) return;
  byPath.set(path, found);
  listeners.forEach((fn) => fn());
}

/** API 层用：按当前路由记（无 pathname 的环境如单测直接跳过）。 */
export function markPageData(payload: unknown): void {
  const path = currentPath();
  if (path) markDataPayload(path, payload);
}

export function dataTimeFor(path: string): DataTime | null {
  return byPath.get(path) ?? null;
}

export function subscribeRefresh(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

function scanPayload(payload: unknown): (DataTime & { tier: 1 | 2 }) | null {
  let bestTier: 1 | 2 = 2;
  let bestMs = 0;
  let bestHasTime = false;
  let budget = MAX_NODES;
  const walk = (node: unknown, depth: number) => {
    if (!node || depth > MAX_DEPTH || budget-- <= 0) return;
    if (Array.isArray(node)) {
      for (const item of node) walk(item, depth + 1);
      return;
    }
    if (typeof node !== "object") return;
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      const tier = TIER1.has(key) ? 1 : TIER2.has(key) ? 2 : 0;
      if (tier) {
        // 行情只给「16:14:58」这种钟点，配同层的 quote_date 才能定位到哪天
        const raw = key === "quote_time" && typeof value === "string" && !/^\d{4}/.test(value) && typeof (node as Record<string, unknown>).quote_date === "string"
          ? `${(node as Record<string, unknown>).quote_date}T${value}`
          : value;
        const parsed = parseTime(raw);
        if (parsed && (!bestMs || tier < bestTier || (tier === bestTier && parsed.ms > bestMs))) {
          bestTier = tier as 1 | 2;
          bestMs = parsed.ms;
          bestHasTime = parsed.hasTime;
        }
      } else {
        walk(value, depth + 1);
      }
    }
  };
  walk(payload, 0);
  return bestMs ? { ms: bestMs, hasTime: bestHasTime, tier: bestTier } : null;
}

const DATE = /^(\d{4})-(\d{2})-(\d{2})(.*)$/;

/** 只认带日期的写法；解析不了就跳过，不拿请求时刻凑数。
 * 纯日期（as_of: 2026-11-06）按本地零点，不能交给 Date.parse——那会按 UTC 算。 */
function parseTime(value: unknown): DataTime | null {
  if (typeof value === "number") {
    if (!Number.isFinite(value) || value <= 0) return null;
    const ms = value >= 1e12 ? value : value >= 1e9 ? value * 1000 : 0;
    return ms ? { ms, hasTime: true } : null;
  }
  if (typeof value !== "string") return null;
  const text = value.trim();
  const m = DATE.exec(text);
  if (!m) return null;
  const [, y, mo, d, rest] = m;
  if (/^[T ]\d{2}:\d{2}/.test(rest)) {
    const ms = Date.parse(text.replace(" ", "T"));
    return Number.isNaN(ms) ? null : { ms, hasTime: true };
  }
  if (rest.trim() === "") return { ms: new Date(+y, +mo - 1, +d).getTime(), hasTime: false };
  return null;
}

const WEEK = ["日", "一", "二", "三", "四", "五", "六"];
const pad = (n: number) => String(n).padStart(2, "0");

/** 2026-11-08（星期日） 16:53:32；只有日期的数据写成 2026-11-06（星期五） */
export function formatRefreshTime(t: DataTime): string {
  const d = new Date(t.ms);
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}（星期${WEEK[d.getDay()]}）`;
  return t.hasTime ? `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : day;
}
