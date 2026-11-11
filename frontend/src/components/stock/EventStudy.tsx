import { useEffect, useState } from "react";
import { AlertCircle, ChevronDown, ChevronUp, Loader2, Zap } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type EventStudyData } from "@/lib/api";
import { cn } from "@/lib/utils";

const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
const color = (v: number | null | undefined) =>
  v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground";

/** 平均路径的小折线：只画中位累计超额收益，坐标按自有范围缩放。 */
function PathSpark({ offsets, values }: { offsets: number[]; values: (number | null)[] }) {
  const points = values.map((v, i) => ({ x: offsets[i], v: v ?? 0 }));
  if (points.length < 2) return null;
  const xs = points.map((p) => p.x);
  const vs = points.map((p) => p.v);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const rawMin = Math.min(0, ...vs), rawMax = Math.max(0, ...vs);
  const span = rawMax - rawMin || 1;
  const W = 320, H = 64;
  const sx = (x: number) => ((x - minX) / (maxX - minX || 1)) * W;
  const sy = (v: number) => H - ((v - rawMin) / span) * H;
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p.x).toFixed(1)},${sy(p.v).toFixed(1)}`).join(" ");
  const zeroY = sy(0);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-16 w-full" role="img" aria-label="平均累计超额收益路径">
      <line x1={0} y1={zeroY} x2={W} y2={zeroY} stroke="currentColor" strokeOpacity={0.2} strokeDasharray="3 3" />
      <line x1={sx(0)} y1={0} x2={sx(0)} y2={H} stroke="currentColor" strokeOpacity={0.15} strokeDasharray="2 3" />
      <path d={d} fill="none" stroke="currentColor" strokeWidth={1.6} className="text-primary" />
    </svg>
  );
}

/**
 * 事件回看：选一类事件，把这只股票历史上每一次排出来，看事件前 5 个交易日到后 20 个交易日
 * 相对沪深300的累计超额收益。只描述过去，不预测，不给买卖提示。
 */
export function EventStudy({ code }: { code: string }) {
  const [kinds, setKinds] = useState<{ key: string; label: string }[]>([]);
  const [kind, setKind] = useState("earnings_forecast");
  const [data, setData] = useState<EventStudyData | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    api.eventStudyKinds().then(setKinds).catch(() => setKinds([]));
  }, []);

  async function run(nextKind = kind, refresh = false) {
    setLoading(true);
    setErr(null);
    try {
      setData(await api.eventStudy(code, nextKind, refresh));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "事件回看取数失败");
    } finally {
      setLoading(false);
    }
  }

  const shown = data?.events.slice().reverse().slice(0, showAll ? undefined : 8) ?? [];

  return (
    <GlassCard className="mb-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Zap className="h-4 w-4 text-primary" /> 事件回看
        </h3>
        <button
          onClick={() => run(kind, true)}
          disabled={loading}
          className="rounded-lg border border-border/60 px-2.5 py-1 text-xs text-muted-foreground transition hover:border-primary/60 hover:text-foreground disabled:opacity-50"
        >
          {loading ? "取数中…" : data ? "重新取数" : "查看"}
        </button>
      </div>

      <div className="mb-2 flex flex-wrap gap-1.5">
        {kinds.map((item) => (
          <button
            key={item.key}
            onClick={() => { setKind(item.key); run(item.key); }}
            className={cn(
              "rounded-md border px-2 py-0.5 text-xs transition",
              kind === item.key
                ? "border-primary/50 bg-primary/15 text-primary"
                : "border-border/50 text-muted-foreground hover:text-foreground",
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      {!data && !loading && !err && (
        <p className="py-4 text-center text-xs text-muted-foreground/60">
          选一类事件点「查看」：列出每次事件前后 20 个交易日相对沪深300的超额收益
        </p>
      )}
      {loading && <p className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 取事件明细与日线…
      </p>}
      {err && <p className="flex items-center gap-1.5 py-2 text-xs text-warning"><AlertCircle className="h-3.5 w-3.5" /> {err}</p>}
      {data && !data.ok && <p className="py-2 text-xs text-muted-foreground">{data.error}</p>}

      {data?.ok && (
        <>
          <p className="mb-2 text-[11px] text-muted-foreground/70">
            {data.kind_label} · 共 {data.events.length} 次（窗口 {data.window?.pre}–后 {data.window?.post} 个交易日，对照 {data.benchmark_label}）
          </p>

          {data.summary.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[480px] text-xs">
                <thead>
                  <tr className="border-b border-border/50 text-left text-muted-foreground">
                    <th className="px-2 py-1.5 font-medium">分组</th>
                    <th className="px-2 py-1.5 font-medium">次数</th>
                    {data.horizons.map((h) => (
                      <th key={h.key} className="px-2 py-1.5 font-medium">{h.label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.summary.map((group) => (
                    <tr key={group.key} className="border-b border-border/30">
                      <td className="px-2 py-1.5">{group.label}</td>
                      <td className="px-2 py-1.5 font-mono text-muted-foreground">{group.events}</td>
                      {data.horizons.map((h) => {
                        const stats = group.horizons[h.key];
                        return (
                          <td key={h.key} className="px-2 py-1.5">
                            {stats.mean == null ? (
                              <span className="text-muted-foreground/50">样本不足</span>
                            ) : (
                              <>
                                <span className={cn("font-mono", color(stats.mean))}>{pct(stats.mean)}</span>
                                <span className="ml-1 font-mono text-[10px] text-muted-foreground/60">
                                  中位 {pct(stats.median)} · n={stats.n}
                                  {stats.positive_share != null && ` · 上涨 ${(stats.positive_share * 100).toFixed(0)}%`}
                                </span>
                              </>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {data.summary.some((g) => g.note) && (
            <ul className="mt-1 space-y-0.5 text-[10px] text-muted-foreground/70">
              {data.summary.filter((g) => g.note).map((g) => <li key={`note-${g.key}`}>· {g.label}：{g.note}</li>)}
            </ul>
          )}

          {data.mean_path && (
            <div className="mt-3">
              <p className="mb-1 text-[11px] text-muted-foreground">
                平均路径（{data.mean_path.n} 次窗口完整、无停牌）· 横轴交易日偏移，竖虚线是事件日
              </p>
              <PathSpark offsets={data.mean_path.offsets} values={data.mean_path.median} />
            </div>
          )}

          {data.upcoming && data.upcoming.length > 0 && (
            <div className="mt-3">
              <p className="mb-1 text-[11px] text-muted-foreground">已排期、还没到的</p>
              <ul className="space-y-0.5 text-xs">
                {data.upcoming.map((item) => (
                  <li key={item.date}>
                    <span className="font-mono text-muted-foreground">{item.date}</span> · {item.detail}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {shown.length > 0 && (
            <div className="mt-3">
              <div className="mb-1 flex items-center justify-between">
                <p className="text-[11px] text-muted-foreground">逐次结果（从新到旧）</p>
                {data.events.length > 8 && (
                  <button onClick={() => setShowAll(!showAll)}
                          className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
                    {showAll ? <>收起 <ChevronUp className="h-3 w-3" /></> : <>全部 {data.events.length} 次 <ChevronDown className="h-3 w-3" /></>}
                  </button>
                )}
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] text-xs">
                  <thead>
                    <tr className="border-b border-border/50 text-left text-muted-foreground">
                      <th className="px-2 py-1.5 font-medium">第 0 天</th>
                      <th className="px-2 py-1.5 font-medium">事件</th>
                      <th className="px-2 py-1.5 font-medium">前 5 日</th>
                      <th className="px-2 py-1.5 font-medium">0–1 日</th>
                      <th className="px-2 py-1.5 font-medium">0–5 日</th>
                      <th className="px-2 py-1.5 font-medium">0–20 日</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map((event) => (
                      <tr key={`${event.day0}-${event.date}`} className="border-b border-border/30">
                        <td className="px-2 py-1.5 font-mono text-muted-foreground">
                          {event.day0}
                          {event.halted_days > 0 && (
                            <span className="ml-1 text-[10px] text-warning" title="窗口里有停牌，不计入平均">停{event.halted_days}</span>
                          )}
                          {event.overlaps_previous && (
                            <span className="ml-1 text-[10px] text-muted-foreground/60" title="与上一次相隔不到 25 个交易日，窗口重叠">叠</span>
                          )}
                        </td>
                        <td className="px-2 py-1.5" title={event.detail}>
                          {event.label}
                          <span className="ml-1 block text-[10px] text-muted-foreground/60">{event.detail.slice(0, 28)}</span>
                        </td>
                        <td className={cn("px-2 py-1.5 font-mono", color(event.pre))}>{pct(event.pre)}</td>
                        <td className={cn("px-2 py-1.5 font-mono", color(event.d0_1))}>{pct(event.d0_1)}</td>
                        <td className={cn("px-2 py-1.5 font-mono", color(event.d0_5))}>{pct(event.d0_5)}</td>
                        <td className={cn("px-2 py-1.5 font-mono", color(event.d0_20))}>
                          {event.d0_20 == null ? <span className="text-muted-foreground/50">未满</span> : pct(event.d0_20)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <ul className="mt-2 space-y-0.5 text-[10px] text-muted-foreground/70">
            {data.gaps.map((gap, i) => (
              <li key={`g${i}`}>· {gap.date ? `${gap.date} ${gap.label ?? ""}：` : ""}{gap.reason}</li>
            ))}
            {data.notes.map((note, i) => <li key={`n${i}`}>· {note}</li>)}
          </ul>
        </>
      )}
    </GlassCard>
  );
}
