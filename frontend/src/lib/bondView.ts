export function changeBp(series: { v: number }[] | undefined, days = 1, storedInPercent = true): number | null {
  if (!series || series.length <= days) return null;
  const delta = series[series.length - 1].v - series[series.length - 1 - days].v;
  return Number.isFinite(delta) ? delta * (storedInPercent ? 100 : 1) : null;
}
export function numberText(value: number | null | undefined, digits = 2, signed = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const n = Math.abs(value) < .5 * 10 ** -digits ? 0 : value;
  return `${signed && n > 0 ? "+" : ""}${n.toFixed(digits)}`;
}
export function breakEvenText(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value < 0 ? `需下行 ${Math.abs(value).toFixed(2)} bp 保本` : `可上行 ${value.toFixed(2)} bp`;
}
export const sourceStatus: Record<string, string> = {
  fresh: "已更新", stale: "滞后", missing: "缺失", unknown: "时点未知",
  unverified: "待核验", time_unknown: "抓取时间未知", fallback: "回退数据", refreshing: "更新中",
};

// Recheck persisted responses on the client: yesterday's ready flag must not
// revive a stale prior while the server is still being refreshed.
export function priorIsCurrent(row: {status: string; observed_at: string | null}, today = new Date().toLocaleDateString("sv-SE", {timeZone: "Asia/Shanghai"})): boolean {
  if (row.status !== "ready" || !row.observed_at) return false;
  const origin = Date.parse(row.observed_at), now = Date.parse(today);
  if (!Number.isFinite(origin) || !Number.isFinite(now) || now < origin || now-origin > 7*86400000) return false;
  for (let t = origin+86400000; t < now; t += 86400000) {
    const weekday = new Date(t).getUTCDay();
    if (weekday > 0 && weekday < 6) return false;
  }
  return true;
}
