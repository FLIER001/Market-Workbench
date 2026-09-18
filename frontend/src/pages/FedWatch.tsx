// 美联储利率追踪（FedWatch）：权威信号（ZQ 期货自算概率）× 预测市场（Polymarket）对比。
// 页面结构：
//   顶部：下次会议倒计时 + 当前目标区间 + 即期锚 + 更新时间
//   主区：会议概率对比矩阵（每次会议一张卡：ZQ 自算 vs Polymarket vs 概率差 + 24h 边际变化）
//   次区：Polymarket 年内加息/降息次数 + 年底利率水平分布
//   底部：数据源健康 + 方法论说明
import { useEffect, useMemo, useState } from "react";
import { RefreshCw, Loader2, Landmark, TrendingUp, TrendingDown, Minus, ExternalLink, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, type FedWatchData, type FedWatchMatrixRow, type FedWatchPmEvent } from "@/lib/api";
import { useSWR } from "@/hooks/useSWR";
import { cn } from "@/lib/utils";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

function isValid(d: FedWatchData | null): d is FedWatchData {
  return !!d && !!d.official && Array.isArray(d.matrix);
}

const pct = (v: number | null | undefined, digits = 1) =>
  isNum(v) ? `${(v * 100).toFixed(digits)}%` : "—";
const pctSigned = (v: number | null | undefined, digits = 1) =>
  isNum(v) ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}pp` : "—";

// 概率条：三段（cut/hold/hike）水平堆叠
function ProbBar({ hike, hold, cut }: { hike: number; hold: number; cut: number }) {
  const h = Math.max(0, Math.min(1, hike));
  const c = Math.max(0, Math.min(1, cut));
  const o = Math.max(0, 1 - h - c);
  void hold;
  return (
    <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted">
      <div className="bg-emerald-500/80 transition-all" style={{ width: `${c * 100}%` }} title={`降息 ${pct(c)}`} />
      <div className="bg-zinc-500/60 transition-all" style={{ width: `${o * 100}%` }} title={`不变 ${pct(o)}`} />
      <div className="bg-rose-500/80 transition-all" style={{ width: `${h * 100}%` }} title={`加息 ${pct(h)}`} />
    </div>
  );
}

function ChgChip({ v, label }: { v: number | null | undefined; label: string }) {
  if (!isNum(v) || v === 0) {
    return (
      <span className="inline-flex items-center gap-0.5 text-[11px] text-muted-foreground">
        <Minus className="h-3 w-3" />{label} —
      </span>
    );
  }
  const up = v > 0;
  return (
    <span className={cn("inline-flex items-center gap-0.5 text-[11px] font-medium",
      up ? "text-rose-500" : "text-emerald-500")}>
      {up ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
      {label} {pctSigned(v)}
    </span>
  );
}

// 单次会议对比卡
function MeetingCard({ row, cmeArchive }: { row: FedWatchMatrixRow; cmeArchive?: FedWatchData["cme_archive"] }) {
  const cme = row.cme_equiv;
  const pm = row.polymarket;
  const diff = row.diff;
  const [ym, y] = [row.meeting.slice(5), row.meeting.slice(0, 4)];
  const meetingLabel = `${y}年${parseInt(ym, 10)}月`;
  const diffTone = (v: number | null | undefined) => {
    if (!isNum(v)) return "text-muted-foreground";
    const a = Math.abs(v);
    if (a >= 0.08) return "font-bold text-amber-500";
    if (a >= 0.04) return "font-semibold text-amber-600/80";
    return "text-muted-foreground";
  };
  return (
    <GlassCard className="flex flex-col gap-3 p-4">
      <div className="flex items-baseline justify-between gap-2">
        <div>
          <div className="text-sm font-bold">{meetingLabel} <span className="text-xs font-normal text-muted-foreground">{row.dates}</span></div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            隐含利率 {isNum(row.implied_rate) ? `${row.implied_rate.toFixed(3)}%` : "—"} · 合约 {cme.contract} {isNum(cme.contract_price) ? cme.contract_price.toFixed(3) : "—"}
          </div>
        </div>
      </div>

      {/* ZQ 自算（CME 等价口径） */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs">
          <span className="font-medium">ZQ 期货自算<span className="ml-1 text-[10px] text-muted-foreground">(CME 口径)</span></span>
          <span className="tabular-nums text-muted-foreground">
            加息 <b className="text-rose-500">{pct(cme.p_hike)}</b> · 不变 {pct(cme.p_hold)} · 降息 <b className="text-emerald-500">{pct(cme.p_cut)}</b>
          </span>
        </div>
        <ProbBar hike={cme.p_hike} hold={cme.p_hold} cut={cme.p_cut} />
        {cme.chg && Object.keys(cme.chg).length > 0 && (
          <div className="flex flex-wrap gap-x-3 gap-y-1 pt-0.5">
            {isNum(cme.chg.p_hike_chg_24h) && <ChgChip v={cme.chg.p_hike_chg_24h} label="加息24h" />}
            {isNum(cme.chg.p_cut_chg_24h) && <ChgChip v={cme.chg.p_cut_chg_24h} label="降息24h" />}
            {isNum(cme.chg.p_hike_chg_7d) && <ChgChip v={cme.chg.p_hike_chg_7d} label="加息7d" />}
            {isNum(cme.chg.p_cut_chg_7d) && <ChgChip v={cme.chg.p_cut_chg_7d} label="降息7d" />}
          </div>
        )}
      </div>

      {/* Polymarket */}
      {pm ? (
        <div className="space-y-1.5 border-t border-border/60 pt-2.5">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium">Polymarket<span className="ml-1 text-[10px] text-muted-foreground">24h量 ${Math.round(pm.vol24h).toLocaleString()}</span></span>
            <span className="tabular-nums text-muted-foreground">
              加息 <b className="text-rose-500">{pct(pm.p_hike)}</b> · 不变 {pct(pm.p_hold)} · 降息 <b className="text-emerald-500">{pct(pm.p_cut)}</b>
            </span>
          </div>
          <ProbBar hike={pm.p_hike} hold={pm.p_hold ?? 0} cut={pm.p_cut} />
          {pm.chg && (
            <div className="flex flex-wrap gap-x-3 gap-y-1 pt-0.5">
              {isNum(pm.chg.p_hike_chg_24h) && <ChgChip v={pm.chg.p_hike_chg_24h} label="加息24h" />}
              {isNum(pm.chg.p_cut_chg_24h) && <ChgChip v={pm.chg.p_cut_chg_24h} label="降息24h" />}
              {isNum(pm.chg.p_hold_chg_24h) && <ChgChip v={pm.chg.p_hold_chg_24h} label="不变24h" />}
            </div>
          )}
        </div>
      ) : (
        <div className="border-t border-border/60 pt-2.5 text-xs text-muted-foreground">Polymarket 暂无对应事件</div>
      )}

      {/* CME 官方口径存档对照（第三方存档，可能滞后） */}
      {(() => {
        // 会议日期串 'YYYY-MM-DD ~ YYYY-MM-DD' → 结束日匹配存档键
        const end = (row.dates || "").split("~")[1]?.trim();
        const arch = end ? cmeArchive?.meetings?.[end] : undefined;
        if (!arch) return null;
        return (
          <div className="rounded-lg bg-muted/40 px-3 py-2 text-[11px] leading-relaxed">
            <span className="text-muted-foreground">CME 官方口径（存档 {cmeArchive?.snapshot_date}）：</span>
            <span className="tabular-nums">加息 <b className="text-rose-500">{arch.hike.toFixed(1)}%</b> · 不变 {arch.no_change.toFixed(1)}% · 降息 {arch.ease.toFixed(1)}%</span>
            <span className="ml-1 text-muted-foreground">（{arch.contract} @ {arch.mid_price}）</span>
          </div>
        );
      })()}

      {/* 两源概率差 */}
      {diff && pm && (
        <div className="rounded-lg bg-muted/40 px-3 py-2 text-[11px] leading-relaxed">
          <span className="text-muted-foreground">两源分歧（PM − ZQ）：</span>
          <span className={cn("tabular-nums", diffTone(diff.p_hike_diff))}>加息 {pctSigned(diff.p_hike_diff)}</span>
          <span className="mx-1 text-muted-foreground">·</span>
          <span className={cn("tabular-nums", diffTone(diff.p_cut_diff))}>降息 {pctSigned(diff.p_cut_diff)}</span>
          {Math.abs(diff.p_hike_diff) >= 0.08 || Math.abs(diff.p_cut_diff) >= 0.08 ? (
            <span className="ml-1.5 text-amber-500">⚠ 显著分歧</span>
          ) : null}
        </div>
      )}
    </GlassCard>
  );
}

// Polymarket 年内次数 / 年底水平分布
function PmDistributionCard({ event, title, note }: { event: FedWatchPmEvent | undefined; title: string; note?: string }) {
  if (!event || !event.markets.length) return null;
  const sorted = [...event.markets]
    .filter((m) => isNum(m.yes))
    .sort((a, b) => (a.yes ?? 0) - (b.yes ?? 0));
  const max = Math.max(...sorted.map((m) => m.yes ?? 0), 0.01);
  return (
    <GlassCard className="flex flex-col gap-2.5 p-4">
      <div>
        <div className="text-sm font-bold">{title}</div>
        {note && <div className="mt-0.5 text-[11px] text-muted-foreground">{note}</div>}
      </div>
      <div className="flex flex-col gap-1.5">
        {sorted.map((m) => (
          <div key={m.label} className="flex items-center gap-2">
            <span className="w-20 shrink-0 truncate text-right text-[11px] text-muted-foreground" title={m.label}>{m.label}</span>
            <div className="h-4 flex-1 overflow-hidden rounded bg-muted">
              <div
                className="flex h-full items-center justify-end rounded bg-gradient-to-l from-sky-500/70 to-sky-600/40 pr-1.5 text-[10px] font-bold text-white transition-all"
                style={{ width: `${Math.max(((m.yes ?? 0) / max) * 100, 12)}%` }}
              >
                {(m.yes ?? 0) >= max * 0.25 ? pct(m.yes, 0) : ""}
              </div>
            </div>
            {isNum(m.chg_1d) && m.chg_1d !== 0 && (
              <span className={cn("w-14 shrink-0 text-right text-[10px] tabular-nums", m.chg_1d > 0 ? "text-rose-500" : "text-emerald-500")}>
                {pctSigned(m.chg_1d)}
              </span>
            )}
          </div>
        ))}
      </div>
    </GlassCard>
  );
}

export function FedWatch() {
  const [err, setErr] = useState<string | null>(null);
  const { data, loading, revalidating, revalidate } = useSWR<FedWatchData>(
    "fedwatch:v1",
    async (fresh) => {
      const d = await api.fedWatch(fresh);
      if (!isValid(d)) throw new Error("数据格式异常");
      return d;
    }, [], (e) => setErr(e instanceof Error ? e.message : "加载失败"), { persist: true },
  );

  const load = () => { setErr(null); void revalidate(true); };

  // 概率快照敏感：60 秒软轮询 + 可见时刷新
  useEffect(() => {
    const tick = () => { if (!document.hidden) void revalidate(); };
    const timer = window.setInterval(tick, 60_000);
    const onVisible = () => { if (!document.hidden) void revalidate(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [revalidate]);

  const off = data?.official;
  const nextMeeting = off?.next_meeting;
  const stmt = off?.latest_statement;
  const effr = off?.effr;
  const spot = data?.futures?.spot;
  const matrix = data?.matrix ?? [];
  const pmCounts = data?.polymarket?.counts ?? [];
  const pmLevel = data?.polymarket?.level ?? [];
  const hikeCount = pmCounts.find((e) => e.kind === "hike");
  const cutCount = pmCounts.find((e) => e.kind === "cuts" || e.kind === "cut");
  const levelEv = pmLevel.find((e) => /end of/i.test(e.title));
  const sources = data?.source_status ?? [];
  const unavailable = sources.filter((s) => s.status !== "fresh");
  const futError = data?.futures?.error;
  const pmError = data?.polymarket?.error;

  const targetRange = useMemo(() => {
    if (stmt?.target_low != null && stmt?.target_high != null)
      return `${stmt.target_low}–${stmt.target_high}%`;
    if (effr?.target_low != null && effr?.target_high != null)
      return `${effr.target_low}–${effr.target_high}%`;
    return null;
  }, [stmt, effr]);

  const daysAway = nextMeeting?.days_away;
  const daysLabel = isNum(daysAway)
    ? daysAway >= 1 ? `${Math.floor(daysAway)} 天` : `${Math.max(0, Math.round(daysAway * 24))} 小时`
    : null;

  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      <PageHeader
        title="美联储利率追踪"
        subtitle="权威信号（ZQ 联邦基金期货 × CME 官方方法论自算）× 预测市场（Polymarket）· 概率差与边际变化"
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              className="flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium transition-colors hover:bg-muted"
            >
              {revalidating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              刷新
            </button>
            {stmt?.url && (
              <a
                href={stmt.url} target="_blank" rel="noreferrer"
                className="flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
              >
                <ExternalLink className="h-3.5 w-3.5" /> 最新声明
              </a>
            )}
          </div>
        }
      />

      {err && (
        <GlassCard className="mb-4 flex items-center gap-2 border-danger/30 text-sm text-danger">
          <AlertTriangle className="h-4 w-4 shrink-0" />{err}
        </GlassCard>
      )}
      {loading && !data && (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />首次拉取中（ZQ 合约逐个获取，约 20-40 秒）…
        </div>
      )}

      {data && (
        <>
          {/* 顶部状态卡 */}
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <GlassCard className="p-4">
              <div className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
                <Landmark className="h-3.5 w-3.5" />下次 FOMC
              </div>
              <div className="mt-1.5 text-xl font-extrabold tabular-nums">
                {nextMeeting?.label ? `${nextMeeting.label.replace("-", "年")}月` : "—"}
              </div>
              <div className="text-[11px] text-muted-foreground">
                {nextMeeting?.dates ?? "—"}{daysLabel ? ` · 还有 ${daysLabel}` : ""}
              </div>
            </GlassCard>
            <GlassCard className="p-4">
              <div className="text-[11px] font-medium text-muted-foreground">当前目标区间</div>
              <div className="mt-1.5 text-xl font-extrabold tabular-nums">{targetRange ?? "—"}</div>
              <div className="text-[11px] text-muted-foreground">
                {stmt?.date ? `上次声明 ${stmt.date}` : effr?.date ? `EFFR 观测 ${effr.date}` : ""}
                {stmt?.action ? ` · ${stmt.action === "raise" ? "加息" : stmt.action === "lower" ? "降息" : "维持"}` : ""}
              </div>
            </GlassCard>
            <GlassCard className="p-4">
              <div className="text-[11px] font-medium text-muted-foreground">EFFR 有效利率</div>
              <div className="mt-1.5 text-xl font-extrabold tabular-nums">
                {effr ? `${effr.rate.toFixed(2)}%` : "—"}
              </div>
              <div className="text-[11px] text-muted-foreground">{effr?.date ?? "—"} · NY Fed</div>
            </GlassCard>
            <GlassCard className="p-4">
              <div className="text-[11px] font-medium text-muted-foreground">市场隐含即期利率</div>
              <div className="mt-1.5 text-xl font-extrabold tabular-nums">
                {spot && isNum(spot.rate) ? `${spot.rate.toFixed(3)}%` : "—"}
              </div>
              <div className="truncate text-[11px] text-muted-foreground" title={spot?.method}>{spot?.method ?? "—"}</div>
            </GlassCard>
          </div>

          {(futError || pmError) && (
            <GlassCard className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 border-amber-500/30 p-3 text-xs text-amber-600 dark:text-amber-400">
              <span className="flex items-center gap-1.5 font-semibold"><AlertTriangle className="h-3.5 w-3.5" />部分数据源异常</span>
              {futError && <span>ZQ：{futError}</span>}
              {pmError && <span>Polymarket：{pmError}</span>}
              <span className="text-muted-foreground">展示 last-good 快照（{data.updated}）</span>
            </GlassCard>
          )}
          {data.futures?.stale_quotes && !futError && (
            <GlassCard className="mb-4 flex flex-wrap items-center gap-2 border-amber-500/30 p-3 text-xs text-amber-600 dark:text-amber-400">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              ZQ 数据源限流中：下方自算概率为限流前快照（Polymarket 侧为实时），恢复后自动更新
            </GlassCard>
          )}

          {/* 会议概率对比矩阵 */}
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="text-sm font-bold">未来会议概率对比</h2>
            <span className="text-[11px] text-muted-foreground">{data.marginal?.history_note ?? ""}</span>
          </div>
          {matrix.length > 0 ? (
            <div className="mb-6 grid gap-3 md:grid-cols-2">
              {matrix.map((row) => <MeetingCard key={row.meeting} row={row} cmeArchive={data.cme_archive} />)}
            </div>
          ) : (
            <GlassCard className="mb-6 flex min-h-32 flex-col items-center justify-center gap-2 p-5 text-sm text-muted-foreground">
              <span>概率数据暂不可用（ZQ 限流或数据源异常，稍后自动重试）</span>
              {data.cme_archive?.meetings && Object.keys(data.cme_archive.meetings).length > 0 && (
                <span className="text-[11px]">
                  参考 · CME 官方口径存档（{data.cme_archive.snapshot_date}）：
                  {Object.entries(data.cme_archive.meetings).slice(0, 3).map(([d, m], i) => (
                    <span key={d}>{i > 0 && " · "}{d.slice(0, 7)} 加息 {m.hike.toFixed(0)}%</span>
                  ))}
                </span>
              )}
            </GlassCard>
          )}

          {/* Polymarket 分布 */}
          {(hikeCount || levelEv || cutCount) && (
            <>
              <h2 className="mb-2 text-sm font-bold">Polymarket 预测分布</h2>
              <div className="mb-6 grid gap-3 md:grid-cols-2 lg:grid-cols-3">
                <PmDistributionCard event={hikeCount} title="2026 年加息次数" note="YES 概率 + 24h 变化" />
                <PmDistributionCard event={cutCount} title="2026 年降息次数" note="YES 概率 + 24h 变化" />
                <PmDistributionCard event={levelEv} title="2026 年底利率水平" note="YES 概率 + 24h 变化" />
              </div>
            </>
          )}

          {/* 底部：方法论 + 数据源 */}
          <div className="grid gap-3 lg:grid-cols-2">
            <GlassCard className="p-4 text-[11px] leading-relaxed text-muted-foreground">
              <div className="mb-1.5 text-xs font-bold text-foreground">方法论</div>
              · <b>ZQ 自算概率</b>：30 天联邦基金期货价格（Yahoo）× CME FedWatch 官方方法论——合约价隐含该月日均 EFFR 期望，
              会议月合约相对「无行动路径」的偏移即为升降息概率（25bp 步长、决策次日生效、无会议月再锚定）。与 CME 官网数值可能有数个百分点差异（非 tick 级实时）。
              <br />· <b>Polymarket</b>：gamma API 官方概率，24h/1w/1m 变化为官方口径。
              <br />· <b>两源分歧</b> ≥8pp 标黄：ZQ 是机构资金定价、PM 是散户/事件资金定价，显著分歧常发生在数据发布或联储表态前后。
              <br />· <b>ZQ 侧 24h/7d 变化</b>：本地快照历史自算（每 10 分钟积累一次），需要运行一段时间后可用。
            </GlassCard>
            <GlassCard className="p-4">
              <div className="mb-1.5 text-xs font-bold">数据源</div>
              <div className="flex flex-wrap gap-1.5">
                {sources.map((s) => (
                  <span
                    key={s.key}
                    className={cn("rounded-md px-2 py-1 text-[11px]",
                      s.status === "fresh" ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400" : "bg-amber-500/10 text-amber-600 dark:text-amber-400")}
                  >
                    {s.label}{s.status === "fresh" ? "" : "（异常）"}
                  </span>
                ))}
              </div>
              {unavailable.length === 0 && (
                <div className="mt-2 text-[11px] text-muted-foreground">更新于 {data.updated}</div>
              )}
            </GlassCard>
          </div>
        </>
      )}
    </div>
  );
}
