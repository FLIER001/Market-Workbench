import { useEffect, useState } from "react";
import { RefreshCw, Scale, TrendingUp, Droplets, Zap, AlertTriangle, ChevronDown, Target } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Sparkline } from "@/components/ui/Sparkline";
import { api, type AllocationData, type AllocationInsight, type TimingPart, type HistPoint } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isHist = (v: unknown): v is HistPoint[] =>
  Array.isArray(v) && v.every((p) => p != null && isNum((p as HistPoint).v) && typeof (p as HistPoint).date === "string");

function isValidAllocation(d: AllocationData | null): d is AllocationData {
  if (!d || typeof d !== "object") return false;
  return !!d.timing && typeof d.timing.regime === "string" && !!d.allocation && Array.isArray(d.allocation.rows);
}

// 高分/偏多 = 红（A股口径），低分/偏空 = 绿
function scoreTone(score: number | null | undefined) {
  if (score == null) return { text: "text-muted-foreground", bg: "bg-muted/20", bar: "bg-muted/40", label: "—" };
  if (score >= 75) return { text: "text-danger", bg: "bg-danger/10", bar: "bg-danger/70", label: "强偏多" };
  if (score >= 60) return { text: "text-danger", bg: "bg-danger/10", bar: "bg-danger/60", label: "偏多" };
  if (score >= 40) return { text: "text-muted-foreground", bg: "bg-muted/20", bar: "bg-primary/40", label: "中性" };
  if (score >= 25) return { text: "text-success", bg: "bg-success/10", bar: "bg-success/60", label: "偏空" };
  return { text: "text-success", bg: "bg-success/10", bar: "bg-success/70", label: "强偏空" };
}

const pctText = (v: number | null | undefined, digits = 1) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;

// 资产在堆叠条中的区分色（非语义色，仅区分）
const ASSET_BAR: Record<string, string> = {
  equity: "bg-danger/70",
  bond: "bg-sky-500/60",
  commodity: "bg-warning/70",
  cash: "bg-emerald-500/60",
};

// 三层证据卡：分数 + 子成分贡献双向条（可展开）
function EvidenceCard({ title, icon: Icon, score, state, date, parts, hist, source, desc }: {
  title: string;
  icon: typeof TrendingUp;
  score: number | null;
  state?: string | null;
  date?: string;
  parts?: TimingPart[] | null;
  hist?: HistPoint[];
  source?: string;
  desc?: string;
}) {
  const [open, setOpen] = useState(false);
  const t = scoreTone(score);
  const covered = (parts ?? []).filter((p) => p.contribution != null);
  const maxAbs = Math.max(...covered.map((p) => Math.abs(p.contribution as number)), 1);
  return (
    <GlassCard className="p-4">
      <button type="button" className="flex w-full items-center gap-3 text-left"
        onClick={() => setOpen((v) => !v)}>
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="truncate text-sm font-semibold">{title}</span>
            {state && <span className={cn("rounded px-1.5 py-px text-[10px] font-medium", t.bg, t.text)}>{state}</span>}
          </span>
          <span className="mt-0.5 block truncate text-[10px] text-muted-foreground/50">
            {desc || source || ""}{date ? ` · ${date}` : ""}
          </span>
        </span>
        <span className="flex items-baseline gap-1">
          <span className={cn("font-mono text-2xl font-bold leading-none", t.text)}>
            {score != null ? score.toFixed(0) : "—"}
          </span>
          <ChevronDown className={cn("h-3.5 w-3.5 text-muted-foreground/40 transition-transform", open && "rotate-180")} />
        </span>
      </button>
      {hist && hist.length > 1 && (
        <div className="mt-2">
          <Sparkline data={hist} height={40} color="--primary" valueSuffix=" 分" />
        </div>
      )}
      {open && covered.length > 0 && (
        <div className="mt-2 space-y-1 border-t border-border/40 pt-2">
          {covered.map((p) => {
            const pos = (p.contribution ?? 0) >= 0;
            const width = p.contribution != null ? (Math.abs(p.contribution) / maxAbs) * 50 : 0;
            return (
              <div key={p.name} className="grid grid-cols-[minmax(0,8.5rem)_1fr_2.6rem] items-center gap-2 px-1">
                <span className="truncate text-[11px] text-foreground/75" title={p.value || p.name}>
                  {p.name}{p.value ? `（${p.value}）` : ""}
                </span>
                <span className="relative flex h-2 items-center">
                  <span className="absolute left-1/2 h-3 w-px bg-border/60" />
                  <span className={cn("absolute h-2 rounded-full", pos ? "bg-danger/60" : "bg-success/60")}
                    style={pos ? { left: "50%", width: `${width}%` } : { right: "50%", width: `${width}%` }} />
                </span>
                <span className={cn("text-right font-mono text-[10px]", pos ? "text-danger" : "text-success")}>
                  {pctText(p.contribution, 2)}
                </span>
              </div>
            );
          })}
          <p className="px-1 pt-1 text-[9px] leading-snug text-muted-foreground/40">
            贡献 = (子分−50)/100 × 权重；正=利多（红右）/ 负=利空（绿左）
          </p>
        </div>
      )}
    </GlassCard>
  );
}

export function Allocation() {
  const [err, setErr] = useState(false);
  // AI 三段解读（宏观/流动性/市场确认）：后端存快照，这里只做局部替换 + 手动重生成
  const [aiInsight, setAiInsight] = useState<AllocationInsight | null>(null);
  const [insightLoading, setInsightLoading] = useState(false);
  const { data, loading, revalidating, revalidate } = useSWR<AllocationData>(
    "allocation:v1",
    async (fresh) => {
      const d = await api.allocation(fresh);
      if (!isValidAllocation(d)) throw new Error("择时配置数据格式异常");
      return d;
    }, [], () => setErr(true), { persist: true },
  );
  const load = () => { setErr(false); void revalidate(true); };

  // 数据就绪后拉一次 AI 解读（非 force：后端有缓存就直接返回，秒开；没缓存才调 LLM）
  useEffect(() => {
    if (!data?.timing) return;
    let cancelled = false;
    setInsightLoading(true);
    api.allocationInsight()
      .then((d) => { if (!cancelled && d) setAiInsight(d); })
      .catch(() => { /* 解读不可用时回落模板结论句 */ })
      .finally(() => { if (!cancelled) setInsightLoading(false); });
    return () => { cancelled = true; };
  }, [data?.timing?.score, data?.timing?.regime, data?.as_of]);

  // 手动重生成：只重调 LLM，不动择时数据
  const refreshInsight = () => {
    setInsightLoading(true);
    api.allocationInsight(true)
      .then((d) => { if (d) setAiInsight(d); })
      .catch(() => { /* 失败保留已有解读 */ })
      .finally(() => setInsightLoading(false));
  };
  useEffect(() => {
    const tick = () => { if (!document.hidden) void revalidate(); };
    const timer = window.setInterval(tick, 30 * 60_000);
    const onVisible = () => { if (!document.hidden) void revalidate(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [revalidate]);

  const t = data?.timing;
  const a = data?.allocation;
  const tt = t ? scoreTone(t.score) : null;
  const hist = t && isHist(t.hist) ? t.hist : [];
  const chg5d = hist.length >= 6 ? hist[hist.length - 1].v - hist[hist.length - 6].v : null;

  return (
    <div>
      <PageHeader
        title="择时配置"
        subtitle="市场环境研判 → 股 / 债 / 商品 / 现金目标仓位 · 自上而下决策链"
        actions={
          <button onClick={load} className="text-muted-foreground hover:text-primary" title="刷新">
            <RefreshCw className={cn("h-4 w-4", (loading || revalidating) && "animate-spin")} />
          </button>
        }
      />

      {(err || data?.cache_state === "error") && !data?.timing && (
        <GlassCard className="mb-6 p-6 text-center text-sm text-muted-foreground">
          数据加载失败，请稍后重试。
        </GlassCard>
      )}
      {data?.cache_state === "stale" && (
        <GlassCard className="mb-6 flex items-center gap-2 border-warning/30 bg-warning/5 p-3 text-xs text-warning">
          <AlertTriangle className="h-3.5 w-3.5" />
          <span>部分上游数据源暂不可用，当前显示缓存结果，稍后可点右上角刷新重试。</span>
        </GlassCard>
      )}

      {loading && !data && (
        <div className="mb-6 space-y-2">
          <div className="h-36 animate-pulse rounded-xl bg-muted/40" />
          <div className="h-24 animate-pulse rounded-xl bg-muted/30" />
        </div>
      )}

      {t && a && tt && data && (
        <>
          {/* ——— 首屏结论卡 ——— */}
          <GlassCard className="mb-4 p-4">
            <div className="grid gap-4 lg:grid-cols-[1fr_auto]">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2.5">
                  <span className={cn("rounded-lg px-3 py-1 text-xl font-extrabold", tt.bg, tt.text)}>
                    {t.regime_label}
                  </span>
                  {t.score != null && (
                    <span className="font-mono text-sm text-muted-foreground">择时分 {t.score.toFixed(0)}/100</span>
                  )}
                  <span className={cn("rounded px-2 py-0.5 text-xs font-semibold",
                    t.recommended_action.includes("increase") ? "bg-danger/10 text-danger"
                      : t.recommended_action.includes("reduce") ? "bg-success/10 text-success"
                        : "bg-muted/30 text-muted-foreground")}>
                    {t.recommended_action_label}
                  </span>
                  <span className="text-[11px] text-muted-foreground/50">数据时点 {data.as_of}</span>
                </div>
                <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5">
                  <span className="text-xs text-muted-foreground">风险预算
                    <span className="ml-1.5 font-mono text-lg font-bold text-foreground">{t.risk_budget_multiplier.toFixed(2)}×</span>
                    <span className="ml-1 text-[10px] text-muted-foreground/50">（相对中性组合）</span>
                  </span>
                  <span className="text-xs text-muted-foreground">现金底仓
                    <span className="ml-1.5 font-mono text-lg font-bold text-foreground">{(t.cash_floor * 100).toFixed(0)}%</span>
                  </span>
                  <span className="text-xs text-muted-foreground">再平衡触发
                    <span className={cn("ml-1.5 font-mono text-lg font-bold",
                      a.rebalance_trigger ? "text-warning" : "text-muted-foreground")}>
                      {a.rebalance_trigger ? "是" : "否"}
                    </span>
                    {a.regime_changed && <span className="ml-1 rounded bg-warning/15 px-1 text-[10px] text-warning">风险等级跨档</span>}
                  </span>
                </div>
                <div className="mt-3 flex items-center gap-1.5">
                  <span className="text-xs font-semibold text-primary/90">AI 解读</span>
                  <button
                    onClick={refreshInsight}
                    disabled={insightLoading}
                    className="ml-auto rounded p-1 text-muted-foreground transition-colors hover:text-primary disabled:opacity-60"
                    title="重新生成 AI 解读">
                    <RefreshCw className={cn("h-3 w-3", insightLoading && "animate-spin")} />
                  </button>
                </div>
                {aiInsight ? (
                  <div className="mt-1.5 space-y-1 rounded-lg bg-muted/20 p-2.5 text-sm leading-relaxed text-foreground/85">
                    <p><span className="font-medium text-primary/90">宏观</span>　{aiInsight.macro}</p>
                    <p><span className="font-medium text-primary/90">流动性</span>　{aiInsight.liquidity}</p>
                    <p><span className="font-medium text-primary/90">市场确认</span>　{aiInsight.market_confirm}</p>
                  </div>
                ) : (
                  <p className="mt-1.5 rounded-lg bg-muted/20 p-2.5 text-sm leading-relaxed text-foreground/85">
                    {insightLoading ? "AI 正在生成解读…" : t.text}
                  </p>
                )}
                {t.gates.length > 0 && (
                  <div className="mt-2 space-y-1">
                    {t.gates.map((g) => (
                      <p key={g.rule} className="flex items-center gap-1.5 text-[11px] text-warning">
                        <AlertTriangle className="h-3 w-3 shrink-0" />{g.desc}
                      </p>
                    ))}
                  </div>
                )}
              </div>
              {/* 择时分管线图 */}
              <div className="flex min-w-0 flex-col justify-center lg:w-72">
                <div className="flex items-baseline gap-2">
                  <span className="text-[10px] text-muted-foreground/45">择时分（近 1 年逐日回放）</span>
                  {chg5d != null && (
                    <span className={cn("rounded bg-muted/30 px-1.5 py-px font-mono text-[10px]",
                      chg5d > 1 ? "text-danger" : chg5d < -1 ? "text-success" : "text-muted-foreground")}>
                      近5日{pctText(chg5d, 1)}
                    </span>
                  )}
                </div>
                {hist.length > 1 ? (
                  <Sparkline data={hist} height={110} color="--primary" valueSuffix=" 分" showLatest />
                ) : (
                  <p className="mt-2 text-[10px] text-muted-foreground/40">历史回放积累中</p>
                )}
              </div>
            </div>
          </GlassCard>

          {/* ——— 三层证据 ——— */}
          <div className="mb-2 flex items-center gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
              <Scale className="h-4 w-4" /> 三层证据
            </h3>
            <span className="text-[11px] text-muted-foreground/50">0.40×宏观 + 0.35×流动性 + 0.25×市场确认 · 点击展开子成分</span>
          </div>
          <div className="mb-4 grid gap-2 md:grid-cols-3">
            <EvidenceCard title="宏观" icon={TrendingUp}
              score={data.evidence.macro?.score} state={data.evidence.macro?.state ?? tt.label}
              date={data.evidence.macro?.date} parts={data.evidence.macro?.parts}
              hist={data.evidence.macro?.hist} source={data.evidence.macro?.source}
              desc="中期先验（模块加权，回测权重）" />
            <EvidenceCard title="流动性" icon={Droplets}
              score={data.evidence.liquidity?.score} state={data.evidence.liquidity?.state ?? undefined}
              date={data.evidence.liquidity?.date} parts={data.evidence.liquidity?.parts}
              hist={data.evidence.liquidity?.hist} source={data.evidence.liquidity?.source}
              desc="风险承受能力（资金/政策/杠杆温度）" />
            <EvidenceCard title="市场确认" icon={Zap}
              score={data.evidence.market_confirm?.score} state={data.evidence.market_confirm?.parts?.length
                ? `压力 ${data.evidence.market_confirm.risk_pressure_score?.toFixed(0) ?? "—"}` : undefined}
              parts={data.evidence.market_confirm?.parts} hist={data.evidence.market_confirm?.hist}
              desc={data.evidence.market_confirm?.desc} />
          </div>

          {/* ——— 目标配置 ——— */}
          <div className="mb-2 flex items-center gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
              <Target className="h-4 w-4" /> 目标配置
            </h3>
            <span className="text-[11px] text-muted-foreground/50">
              风险等级锚点 ± 资产分带限偏离 · 合计 100%{a.last_weights ? " · 「较当前」= 较上次建议" : ""}
            </span>
          </div>
          <GlassCard className="mb-4 p-4">
            {/* 目标权重堆叠条 */}
            <div className="mb-3 flex h-7 w-full overflow-hidden rounded-lg border border-border/40">
              {a.rows.map((r) => (
                <div key={r.asset} className={cn("flex items-center justify-center", ASSET_BAR[r.asset] ?? "bg-muted/40")}
                  style={{ width: `${r.target}%` }} title={`${r.name} ${r.target.toFixed(1)}%`}>
                  {r.target >= 8 && (
                    <span className="truncate px-1 text-[10px] font-semibold text-white/95">{r.name} {r.target.toFixed(0)}%</span>
                  )}
                </div>
              ))}
            </div>
            {!a.resolved && (
              <p className="mb-3 flex items-center gap-1.5 rounded bg-warning/10 px-2 py-1.5 text-[11px] text-warning">
                <AlertTriangle className="h-3 w-3 shrink-0" />{a.resolve_note}
              </p>
            )}
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-b border-border/40 text-[10px] text-muted-foreground/60">
                    <th className="py-1.5 text-left font-medium">资产</th>
                    <th className="text-right font-medium">锚点</th>
                    <th className="text-right font-medium">目标</th>
                    <th className="text-right font-medium">较当前{a.last_weights ? "" : "（=较中性）"}</th>
                    <th className="text-right font-medium">较中性</th>
                    <th className="text-right font-medium">建议</th>
                  </tr>
                </thead>
                <tbody>
                  {a.rows.map((r) => {
                    const d = r.vs_last ?? r.vs_base;
                    return (
                      <tr key={r.asset} className="border-b border-border/20 last:border-0">
                        <td className="py-2">
                          <span className="mr-1.5 inline-block h-2 w-2 rounded-full align-middle" aria-hidden
                            style={{ background: "currentColor" }} />
                          <span className={cn("font-medium align-middle", r.asset === "equity" ? "text-danger/80"
                            : r.asset === "bond" ? "text-sky-400" : r.asset === "commodity" ? "text-warning" : "text-emerald-400")}>
                            {r.name}
                          </span>
                          <span className="ml-2 font-mono text-[11px] text-muted-foreground/60">
                            资产分 {a.asset_scores[r.asset]?.score != null ? a.asset_scores[r.asset].score!.toFixed(0) : "—"}
                          </span>
                        </td>
                        <td className="text-right font-mono text-muted-foreground/70">{r.anchor}%</td>
                        <td className="text-right font-mono font-bold text-foreground">{r.target.toFixed(1)}%</td>
                        <td className={cn("text-right font-mono", d > 0.3 ? "text-danger" : d < -0.3 ? "text-success" : "text-muted-foreground/50")}>
                          {r.vs_last != null ? pctText(r.vs_last) : pctText(r.vs_base)}
                        </td>
                        <td className={cn("text-right font-mono", r.vs_base > 0.3 ? "text-danger" : r.vs_base < -0.3 ? "text-success" : "text-muted-foreground/50")}>
                          {pctText(r.vs_base)}
                        </td>
                        <td className="text-right text-xs text-muted-foreground">{r.suggestion}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
              {a.rows.filter((r) => r.asset !== "cash").map((r) => (
                <div key={r.asset} className="rounded-lg border border-border/30 bg-muted/10 p-2.5">
                  <p className="text-xs font-semibold">{r.name}
                    <span className="ml-1.5 text-[10px] font-normal text-muted-foreground/50">主要支持 / 约束 / 仓位含义</span>
                  </p>
                  <p className="mt-1 text-[11px] leading-relaxed text-foreground/70">
                    <span className="text-danger/80">支持</span> {r.support.join("、") || "—"}
                  </p>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-foreground/70">
                    <span className="text-success/90">约束</span> {r.constraint.join("、") || "—"}
                  </p>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground/70">{r.meaning}</p>
                </div>
              ))}
              <div className="rounded-lg border border-border/30 bg-muted/10 p-2.5">
                <p className="text-xs font-semibold">现金
                  <span className="ml-1.5 text-[10px] font-normal text-muted-foreground/50">{a.cash_yield_note}</span>
                </p>
                <p className="mt-1 text-[11px] leading-relaxed text-foreground/70">
                  <span className="text-danger/80">支持</span> 流动性缓冲、择时现金底仓约束
                </p>
                <p className="mt-0.5 text-[11px] leading-relaxed text-foreground/70">
                  <span className="text-success/90">约束</span> 风险偏好上升时机会成本高
                </p>
                <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground/70">
                  降至择时底仓 {(t.cash_floor * 100).toFixed(0)}%
                </p>
              </div>
            </div>
          </GlassCard>

          {/* ——— 组合风险 + 反转条件 ——— */}
          <div className="grid gap-4 lg:grid-cols-2">
            <GlassCard className="p-4">
              <h3 className="text-sm font-semibold text-muted-foreground">组合风险摘要</h3>
              <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                <span className="text-muted-foreground/60">股债相关性（60日）</span>
                <span className="text-right font-mono">{a.correlation.stock_bond_corr_60d != null ? a.correlation.stock_bond_corr_60d.toFixed(2) : "—"}</span>
                <span className="text-muted-foreground/60">股债相关性（120日）</span>
                <span className="text-right font-mono">{a.correlation.stock_bond_corr_120d != null ? a.correlation.stock_bond_corr_120d.toFixed(2) : "—"}</span>
                {Object.entries(a.correlation.vols ?? {}).map(([name, v]) => (
                  <span key={name} className="col-span-2 grid grid-cols-2 border-t border-border/20 pt-1">
                    <span className="text-muted-foreground/60">{name} 20日年化波动</span>
                    <span className="text-right font-mono">
                      {v.vol_20d_ann.toFixed(1)}%
                      {v.pct_1y != null && <span className="ml-1 text-[10px] text-muted-foreground/50">（{v.pct_1y.toFixed(0)}%分位）</span>}
                    </span>
                  </span>
                ))}
              </div>
              <p className="mt-2.5 text-[10px] leading-snug text-muted-foreground/40">
                {a.correlation.window}；V1 相关性/波动为解释层摘要，通过分散化评分与风险预算进入结论，未做数值优化。
              </p>
            </GlassCard>
            <GlassCard className="p-4">
              <h3 className="text-sm font-semibold text-muted-foreground">反转条件（何时改变结论）</h3>
              <ul className="mt-2 space-y-1.5">
                {t.invalidation.map((s) => (
                  <li key={s} className="flex items-start gap-1.5 text-xs leading-relaxed text-foreground/75">
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-warning" />{s}
                  </li>
                ))}
                <li className="flex items-start gap-1.5 text-xs leading-relaxed text-foreground/75">
                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-warning" />
                  任一资产目标权重变化 ≥3pct 或风险等级跨档时触发再平衡建议
                </li>
              </ul>
              <p className="mt-2.5 border-t border-border/30 pt-2 text-[10px] leading-snug text-muted-foreground/40">
                {data.notes.join("；")}模型：{data.model_version}
              </p>
            </GlassCard>
          </div>

          {/* 方法说明 */}
          <GlassCard className="mt-4 p-4">
            <h3 className="text-sm font-semibold text-muted-foreground">方法</h3>
            <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground/70">{data.method}</p>
          </GlassCard>
        </>
      )}
    </div>
  );
}
