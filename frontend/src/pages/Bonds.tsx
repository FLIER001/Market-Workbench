import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Activity, AlertCircle, Calculator, Layers, RefreshCw, Sparkles, X } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { RefreshButton } from "@/components/ui/RefreshButton";
import { GlassCard } from "@/components/ui/GlassCard";
import { ColdStartNotice, SkeletonBlock } from "@/components/ui/PageSkeleton";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { BondPriorsPanel } from "@/components/bonds/BondPriorsPanel";
import { api, type BondsOverviewData, type BondsFrameworkState, type BondsSeriesPoint, type BondsInsight, type BondsCalcRow } from "@/lib/api";
import { useSWR } from "@/hooks/useSWR";
import { cn } from "@/lib/utils";
import { changeBp, numberText as n, breakEvenText, sourceStatus } from "@/lib/bondView";

const SOURCE_LABEL: Record<string, string> = { curve: "收益率曲线", funding: "Shibor", policy: "LPR", index: "财富指数", global: "中美利率", positioning: "期货量仓" };
const FREQUENCY: Record<string, string> = { daily: "日", monthly: "月", quarterly: "季" };
const QUALITY: Record<string, string> = { direct: "直接来源", proxy: "代理指标", derived: "派生值", derived_unvalidated: "未验证推导", proxy_unverified_policy: "政策锚待核验的代理" };

function HistoryChart({ points, fixedScore = false }: { points: BondsSeriesPoint[]; fixedScore?: boolean }) {
  const id = useId();
  const [selected, setSelected] = useState<number | null>(null);
  const rows = points.filter(p => p.date && Number.isFinite(p.v)).map(p => ({ ...p, t: Date.parse(p.date) })).filter(p => Number.isFinite(p.t)).sort((a, b) => a.t - b.t);
  if (rows.length < 2) return <p className="text-xs text-muted-foreground">历史观测不足</p>;
  const min = fixedScore ? -2 : Math.min(...rows.map(p => p.v));
  const max = fixedScore ? 2 : Math.max(...rows.map(p => p.v));
  const span = max - min || 1;
  const lo = min - (fixedScore ? 0 : span * .08), hi = max + (fixedScore ? 0 : span * .08);
  const tx = (t: number) => 45 + (t - rows[0].t) / (rows[rows.length - 1].t - rows[0].t || 1) * 600;
  const vy = (v: number) => 105 - (v - lo) / (hi - lo || 1) * 95;
  const current = rows[Math.min(selected ?? rows.length - 1, rows.length - 1)];
  return <div className="min-w-0">
    <svg viewBox="0 0 660 130" role="img" aria-label={`${rows[0].date}至${rows[rows.length - 1].date}走势`} className="h-28 w-full text-muted-foreground">
      {[lo, (hi + lo) / 2, hi].map(v => <g key={v}><line x1="45" x2="645" y1={vy(v)} y2={vy(v)} stroke="currentColor" opacity=".15" /><text x="40" y={vy(v) + 4} textAnchor="end" fill="currentColor" fontSize="12">{n(v, 1)}</text></g>)}
      <path d={rows.map((p, i) => `${i ? "L" : "M"}${tx(p.t)},${vy(p.v)}`).join(" ")} fill="none" stroke="hsl(var(--primary))" strokeWidth="2" />
      <circle cx={tx(current.t)} cy={vy(current.v)} r="4" fill="hsl(var(--primary))" />
      <text x="45" y="125" fontSize="12" fill="currentColor">{rows[0].date}</text><text x="645" y="125" textAnchor="end" fontSize="12" fill="currentColor">{rows[rows.length - 1].date}</text>
    </svg>
    <label htmlFor={id} className="flex justify-between gap-2 text-xs text-muted-foreground"><span>查看观测</span><span>{current.date} · {n(current.v, 3)}</span></label>
    <input id={id} type="range" min="0" max={rows.length - 1} value={selected ?? rows.length - 1} onChange={e => setSelected(Number(e.target.value))} className="h-5 w-full accent-primary" />
  </div>;
}

function StateModal({ state, close }: { state: BondsFrameworkState; close: () => void }) {
  const dialog = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const prior = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    node?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      if (e.key !== "Tab" || !node) return;
      const items = [...node.querySelectorAll<HTMLElement>('button,a[href],input,summary,[tabindex="0"]')].filter(e => e.getClientRects().length > 0);
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === node)) { e.preventDefault(); last?.focus(); }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    };
    const priorOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = priorOverflow; prior?.focus(); };
  }, [close]);
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3" onClick={close}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} onClick={e => e.stopPropagation()} className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-border bg-background p-5 shadow-xl">
      <div className="flex items-start justify-between gap-4"><div><h2 id={titleId} className="font-semibold">{state.name}</h2><p className="mt-1 text-sm text-muted-foreground">{state.meaning}</p></div><button onClick={close} aria-label="关闭指标明细" className="rounded p-2 hover:bg-muted"><X className="h-5 w-5" /></button></div>
      <p className="my-3 rounded-lg bg-muted/40 p-3 text-xs leading-relaxed text-muted-foreground">历史按当时可得值事后重建；分数未经预测验证。</p>
      <div className="space-y-4">{state.parts.map(p => <section key={p.key} className="rounded-xl border border-border p-3">
        <div className="flex flex-wrap justify-between gap-2"><h3 className="text-sm font-medium">{p.label}</h3><span className="text-sm tabular-nums">{n(p.value, 3)} · 变化 {n(p.change, 3, true)}</span></div>
        <p className="mt-2 text-xs text-muted-foreground">观察 {p.date ?? "未知"} · 发布 {p.release_at ?? "未知"} · {sourceStatus[p.status ?? "unknown"] ?? p.status} · {QUALITY[p.quality ?? ""] ?? p.quality}</p>
        <p className="mt-1 text-xs text-muted-foreground">分位 {p.pct == null ? "—" : n(p.pct * 100, 0) + "%"} · 窗口 {p.window} {FREQUENCY[p.frequency ?? ""] ?? "期"} · 有效 {p.sample_size} 点 · 起点 {p.window_start ?? "—"}</p>
        <p className="mt-1 text-xs text-muted-foreground">{p.source} {p.source_url && <a href={p.source_url} target="_blank" rel="noreferrer" className="text-primary underline">查看来源</a>}</p>
        <HistoryChart points={(p.hist ?? []).filter((x): x is BondsSeriesPoint => !!x.date && x.v != null)} />
        <details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">实验假设与分数</summary><p className="mt-1">分数 {n(p.score)}，权重 {p.weight}。分数只表示这一指标的历史相对位置。</p></details>
      </section>)}</div>
      {!state.parts.length && <p className="py-8 text-center text-sm text-muted-foreground">此维度数据不足</p>}
      {state.hist?.length ? <details className="mt-4 text-sm"><summary className="cursor-pointer">事后重建的状态分数历史</summary><HistoryChart points={state.hist} fixedScore /></details> : null}
    </div>
  </div>;
}

function FrameworkPanel({ data }: { data: BondsOverviewData }) {
  const [open, setOpen] = useState<BondsFrameworkState | null>(null);
  const close = useCallback(() => setOpen(null), []);
  const states = data.framework?.states ?? [];
  return <GlassCard>
    <h2 className="flex items-center gap-2 text-sm font-semibold"><Activity className="h-4 w-4 text-primary" /> 八维研究观察</h2>
    <p className="mt-1 text-xs text-muted-foreground">每个维度对应一类指标。点开看原值、变化频率与来源；单个维度只描述一项状态，不能单独当作方向判断。</p>
    <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">{states.map(s => <button key={s.key} onClick={() => setOpen(s)} className="rounded-xl border border-border/60 p-3 text-left hover:border-primary/50 focus-visible:outline focus-visible:outline-primary">
      <span className="text-sm font-medium">{s.name}</span><p className="mt-2 text-xs leading-relaxed text-muted-foreground">{s.meaning}</p>
      <p className="mt-3 text-xs text-muted-foreground">{s.parts.filter(p => p.status === "fresh").length}/{s.parts.length} 项已更新 · 看明细</p>
    </button>)}</div>
    {open && <StateModal state={open} close={close} />}
  </GlassCard>;
}

function SegmentPanel({ data, insight, busy, refreshInsight }: { data: BondsOverviewData; insight: BondsInsight | null; busy: boolean; refreshInsight: () => void }) {
  const rows = data.segments?.rows ?? [];
  const [aiSegment, setAiSegment] = useState("");
  const selectedSegment = rows.find(r => r.segment === aiSegment)?.segment ?? rows[0]?.segment;
  const segmentLabel = (name: string) => name.replace("短债(1-3Y)", "1—3年债");
  const calc = data.calc?.rows ?? [];
  const levered = rows.find(r => r.segment.includes("杠杆"));
  const card = (r: typeof rows[number]) => {
    const anchor = calc.find(c => c.tenor === (r.segment.includes("杠杆") ? "1年" : r.anchor_tenor));
    return <div key={r.segment} className="rounded-xl border border-border/60 bg-background/30 p-4">
      <h3 className="text-sm font-semibold">{r.segment}</h3>
      {anchor ? <><div className="mt-3 grid grid-cols-2 gap-2 text-xs"><span className="text-muted-foreground">未融资静态收益</span><span className="text-right tabular-nums">{n(anchor.gross_static_bp_3m, 1, true)} bp / 3M</span><span className="text-muted-foreground">融资后（代理成本）</span><span className="text-right tabular-nums">{n(anchor.total_static_bp_3m, 1, true)} bp / 3M</span><span className="text-muted-foreground">修正久期</span><span className="text-right">{n(anchor.modified_duration, 2)} 年</span></div><p className="mt-2 text-xs text-muted-foreground">{breakEvenText(anchor.breakeven_bp_3m)}</p></> : <p className="mt-3 text-xs text-muted-foreground">{r.segment.includes("信用") ? "只作 AAA 利差观察。缺单券现金流与融资数据，未计算收益估值。" : "融资套息单列观察。回购成本与杠杆约束尚未接入。"}</p>}
      <p className="mt-3 border-t border-border/50 pt-3 text-xs leading-relaxed text-muted-foreground"><AlertCircle className="mr-1 inline h-3 w-3" />反证与风险：{r.invalidation}</p>
      <details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer">实验分数 · 覆盖 {r.coverage ?? 0}%</summary><p className="mt-2">分数 {n(r.score)}；覆盖低于 60% 时不出分。</p>{r.drivers.map(d => <p key={d.state} className="mt-1">{d.state} · 贡献 {n(d.contribution, 2, true)} · 权重 {d.weight}</p>)}{r.hist?.length ? <HistoryChart points={r.hist} fixedScore /> : null}</details>
    </div>;
  };
  return <GlassCard>
    {/* User preference: keep AI expanded at the top; market/curve panels stay below research. */}
    <section aria-label="AI解读" className="mb-4 rounded-xl bg-muted/20 p-4">
      <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" /><h2 className="text-sm font-semibold text-primary">AI 数据解读</h2><button onClick={refreshInsight} disabled={busy || !data.input_fingerprint} title="重新生成同一快照解读" aria-label="重新生成AI解读" className="ml-auto rounded p-2 disabled:opacity-40"><RefreshCw className={cn("h-4 w-4", busy && "animate-spin")} /></button></div>
      {insight ? <><p className="mt-2 text-sm leading-relaxed">{insight.ranking}</p><div className="mt-4 border-t border-border/50 pt-3"><label className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">看单个品种<select aria-label="选择AI解读品种" value={selectedSegment ?? ""} onChange={e => setAiSegment(e.target.value)} className="max-w-full rounded-lg border border-border bg-background px-3 py-2 text-foreground">{rows.map(r => <option key={r.segment} value={r.segment}>{segmentLabel(r.segment)}</option>)}</select></label>{selectedSegment && <p className="mt-3 text-sm leading-relaxed text-foreground/80">{insight.segments[selectedSegment]}</p>}</div><p className="mt-2 text-xs text-muted-foreground">同快照版本 · 生成 {insight.generated_at?.slice(0, 19).replace("T", " ")}</p></> : <p className="mt-3 text-sm text-muted-foreground">{busy ? "正在核对数据并生成同快照解读…" : "暂无解读，点右上角重新生成。"}</p>}
    </section>
    <details className="mt-3 border-t border-border/60 pt-4"><summary className="cursor-pointer text-sm text-muted-foreground">利率不变时，持有收益如何计算？</summary><div className="mt-4">
    <p className="mt-1 text-xs text-muted-foreground">持有 3 个月，按曲线不动计算。融资后收益只在资金与曲线同一天有数据时给出。</p>
    <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">{rows.filter(r => !r.segment.includes("杠杆")).map(card)}</div>
    {levered && <details className="mt-3 rounded-xl border border-border/60 p-3 text-sm"><summary className="cursor-pointer font-medium">独立策略观察 · 融资套息</summary><div className="mt-3">{card(levered)}</div></details>}
    </div></details>
  </GlassCard>;
}

function SourcePanel({ data }: { data: BondsOverviewData }) {
  const sources = data.sources ?? [];
  const issues = sources.filter(s => s.status !== "fresh");
  return <div className="rounded-xl border border-border bg-background/40 px-4 py-3 text-xs leading-relaxed">
    <p><strong>{data.updated_sources ?? 0}/{data.total_sources ?? 0} 个来源已更新</strong> · 生成 {data.generated_at?.slice(0, 19).replace("T", " ") ?? "未知"} · {data.cache_state === "refreshing" ? "后台更新中" : data.cache_state === "error" ? "更新失败，保留旧数据" : "已载入快照"}</p>
    {issues.length > 0 && <p role="status" className="mt-2 text-warning">{issues.map(s => `${SOURCE_LABEL[s.key] ?? s.key}：${sourceStatus[s.status] ?? s.status}（${s.date ?? "无观察日期"}）`).join("；")}</p>}
    <details className="mt-2"><summary className="cursor-pointer text-muted-foreground">数据日期与来源明细</summary><p className="mt-2 text-muted-foreground">统一输入版本 {data.input_fingerprint ?? "待生成"}</p><div className="mt-2 space-y-2">{sources.map(s => <p key={s.key}><a className="text-primary underline" href={s.source_url} target="_blank" rel="noreferrer">{SOURCE_LABEL[s.key] ?? s.key}</a> · 观察 {s.date ?? "未知"} · 抓取 {s.fetched_at?.slice(0, 19).replace("T", " ") ?? "未知"} · {QUALITY[s.quality]} · {s.calendar_basis}{s.error ? ` · ${s.error}` : ""}</p>)}</div></details>
  </div>;
}

function MarketStats({ data }: { data: BondsOverviewData }) {
  const yields = data.curve?.yields ?? {};
  const changes = (h: { v: number }[] | undefined) => <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground"><span className="whitespace-nowrap">较前日 <span className="tabular-nums text-foreground">{n(changeBp(h), 2, true)}</span> bp</span><span className="whitespace-nowrap">近 5 日 <span className="tabular-nums text-foreground">{n(changeBp(h, 5), 2, true)}</span> bp</span></p>;
  return <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">{["1年", "5年", "10年", "30年"].map(t => <div key={t} className="rounded-xl border border-border/60 p-3"><p className="text-xs text-muted-foreground">{t}国债 · {yields[t]?.slice(-1)[0]?.date ?? "缺失"}</p><p className="mt-2 text-lg font-semibold tabular-nums">{n(yields[t]?.slice(-1)[0]?.v, 3)}%</p>{changes(yields[t])}</div>)}{["O/N", "3M"].map(t => <div key={t} className="rounded-xl border border-border/60 p-3"><p className="text-xs text-muted-foreground">Shibor {t} · {data.funding?.date ?? "缺失"}</p><p className="mt-2 text-lg font-semibold tabular-nums">{n(data.funding?.series[t]?.slice(-1)[0]?.v, 3)}%</p><p className="mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground"><span className="whitespace-nowrap">较前日 <span className="tabular-nums text-foreground">{n(changeBp(data.funding?.series[t]), 2, true)}</span> bp</span><span className="whitespace-nowrap">无担保报价</span></p></div>)}</div>;
}

function CurvePanel({ data }: { data: BondsOverviewData }) {
  const [compare, setCompare] = useState(true);
  const comparisons = (data.curve_comparisons ?? []).map(c => ({ ...c, points: [...c.points].sort((a, b) => a.years - b.years) }));
  const series = compare ? comparisons : comparisons.slice(0, 1);
  const values = series.flatMap(s => s.points.map(p => p.value));
  const lo = values.length ? Math.min(...values) - .1 : 0, hi = values.length ? Math.max(...values) + .1 : 1;
  const x = (v: number) => 45 + v / 30 * 600, y = (v: number) => 185 - (v - lo) / (hi - lo) * 160;
  const colors = ["hsl(var(--primary))", "#38bdf8", "#a78bfa"];
  const current = comparisons[0];
  const spread = (a: string, b: string) => { const ap = current?.points.find(p => p.tenor === a), bp = current?.points.find(p => p.tenor === b); return ap && bp ? (ap.value - bp.value) * 100 : null; };
  return <GlassCard>
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">国债收益率曲线</h2><label className="flex gap-2 text-xs"><input type="checkbox" checked={compare} onChange={e => setCompare(e.target.checked)} />叠加上期曲线</label></div>
    {values.length ? <svg viewBox="0 0 670 225" role="img" aria-label="国债收益率曲线，横轴为真实剩余年限，纵轴为年化收益率百分比" className="mt-3 h-64 w-full text-muted-foreground">
      {[lo, (lo + hi) / 2, hi].map(v => <g key={v}><line x1="45" x2="645" y1={y(v)} y2={y(v)} stroke="currentColor" opacity=".15" /><text x="40" y={y(v) + 4} textAnchor="end" fill="currentColor" fontSize="12">{n(v, 2)}%</text></g>)}
      {[0, 1, 5, 10, 20, 30].map(t => <text key={t} x={x(t)} y="211" fill="currentColor" fontSize="12" textAnchor="middle">{t}Y</text>)}
      {series.map((s, i) => <path key={s.label} d={s.points.map((p, j) => `${j ? "L" : "M"}${x(p.years)},${y(p.value)}`).join(" ")} fill="none" stroke={colors[i]} strokeWidth={i ? 1.5 : 2.5} strokeDasharray={i ? "5 3" : undefined} />)}
    </svg> : <p className="py-8 text-sm text-muted-foreground">曲线数据不足</p>}
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">{series.map((s, i) => <span key={s.label} style={{ color: colors[i] }}>{s.label.replace("观测交易日", "交易日")} · {s.date}</span>)}</div>
    <p className="mt-3 text-sm tabular-nums">{spread("10年", "2年") != null && <>10年与2年利差 {n(spread("10年", "2年"), 1)} bp · </>}30年与10年利差 {n(spread("30年", "10年"), 1)} bp</p>
    {spread("10年", "2年") == null && <p className="mt-1 text-xs text-muted-foreground">缺 2 年观测，2s10s 留空。</p>}
    <details className="mt-3 text-xs"><summary className="cursor-pointer">逐期限数值</summary><div className="mt-2 overflow-x-auto"><table className="w-full min-w-[360px] text-left tabular-nums"><thead><tr><th className="p-2">期限</th>{comparisons.map(c => <th key={c.label}>{c.label} %</th>)}</tr></thead><tbody>{current?.points.map(p => <tr key={p.tenor}><td className="p-2">{p.tenor}</td>{comparisons.map(c => <td key={c.label}>{n(c.points.find(x => x.tenor === p.tenor)?.value, 4)}</td>)}</tr>)}</tbody></table></div></details>
  </GlassCard>;
}

function ObservationPanel({ data }: { data: BondsOverviewData }) {
  const groups = [{ title: "期限利差 · bp", rows: data.curve?.spreads }, { title: "AAA信用/品种利差 · bp", rows: data.curve?.credit }];
  return <GlassCard><h2 className="text-sm font-semibold">利差与政策利率观察</h2><p className="mt-2 text-xs leading-relaxed text-muted-foreground">政策利率锚：{data.policy?.anchors?.map(a => `${a.label.split("（")[0]} ${n(a.value)}%（${a.date}）`).join("、")}。同业存单、拆借与质押回购属于不同口径。</p><p className="mt-1 text-xs">中美 10 年期利差 {n(data.global?.spread?.slice(-1)[0]?.v, 1)} bp（{data.global?.date ?? "缺失"}）。汇率数据尚未接入。</p><div className="mt-3 grid gap-3 md:grid-cols-2">{groups.map(g => <details key={g.title} className="rounded-xl border border-border/60 p-3"><summary className="cursor-pointer text-sm font-medium">{g.title}</summary>{Object.entries(g.rows ?? {}).map(([label, h]) => <div key={label} className="mt-4"><p className="text-xs">{label} · 最新 {n(h.slice(-1)[0]?.v)} · 较前日 {n(changeBp(h, 1, false), 2, true)} · {h.slice(-1)[0]?.date}</p><HistoryChart points={h} /></div>)}</details>)}</div></GlassCard>;
}

function CalcPanel({ data }: { data: BondsOverviewData }) {
  const [net, setNet] = useState(false);
  const calc = data.calc;
  if (!calc?.rows?.length) return null;
  const scenario = (r: BondsCalcRow, b: number) => { const v = r.scenarios.find(s => s.shift_bp === b); return n(net ? v?.net_bp : v?.gross_bp, 1, true); };
  return <GlassCard>
    <h2 className="flex items-center gap-2 text-sm font-semibold"><Calculator className="h-4 w-4 text-primary" /> 标准化假想债 · 3 个月持有</h2>
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"><span>曲线 {calc.date}</span><span>资金 {calc.funding_date ?? "缺失"}</span><span>融资代理 Shibor O/N {n(calc.funding_cost, 3)}%</span></div>
    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{calc.funding_cost == null ? "资金缺失或日期不齐，融资后收益与保本幅度留空。" : "融资成本按简单利息计，不含实际回购、杠杆与交易费用。"}</p>
    <div className="mt-3 overflow-x-auto" tabIndex={0} aria-label="债券情景表，可左右滚动"><table className="w-full min-w-[780px] text-right text-xs tabular-nums"><thead><tr className="border-b border-border text-muted-foreground">{["期限", "收益率%", "票息收益 bp", "骑乘收益 bp", "合计（未融资）", "合计（融资后）", "久期 年", "DV01/百元", "保本幅度"].map(t => <th key={t} className="p-2 font-medium">{t}</th>)}</tr></thead><tbody>{calc.rows.map(r => <tr key={r.tenor} className="border-b border-border/40"><td className="p-2 font-medium">{r.tenor}</td><td>{n(r.yield, 3)}</td><td>{n(r.gross_carry_bp_3m, 1)}</td><td>{n(r.roll_bp_3m, 1)}</td><td>{n(r.gross_static_bp_3m, 1, true)}</td><td>{n(r.total_static_bp_3m, 1, true)}</td><td>{n(r.modified_duration)}</td><td>{n(r.dv01_per_100, 4)}</td><td className="pl-3">{breakEvenText(r.breakeven_bp_3m)}</td></tr>)}</tbody></table></div>
    <p className="mt-2 text-xs text-muted-foreground md:hidden">表格可左右滑动查看全部列。</p>
    <details className="mt-4"><summary className="cursor-pointer text-sm">收益率平移情景</summary><p className="mt-3 text-xs text-muted-foreground">假设整条曲线平移，不是预测区间。</p><label className="mt-2 flex gap-2 text-xs"><input type="checkbox" checked={net} onChange={e => setNet(e.target.checked)} />扣融资代理成本</label><div className="mt-2 overflow-x-auto"><table className="w-full min-w-[380px] text-right text-xs tabular-nums"><thead><tr><th className="p-2">期限</th>{[-25, -10, 10, 25].map(b => <th key={b}>{b > 0 ? "+" : ""}{b}bp</th>)}</tr></thead><tbody>{calc.rows.map(r => <tr key={r.tenor} className="border-b border-border/30"><td className="p-2">{r.tenor}</td>{[-25, -10, 10, 25].map(b => <td key={b}>{scenario(r, b)} bp</td>)}</tr>)}</tbody></table></div></details>
    <details className="mt-4 text-xs leading-relaxed text-muted-foreground"><summary className="cursor-pointer">现金流与资金假设</summary><p className="mt-2">{calc.method}</p></details>
  </GlassCard>;
}

function PositioningPanel({ data }: { data: BondsOverviewData }) {
  const pos = data.positioning;
  return <GlassCard><h2 className="flex items-center gap-2 text-sm font-semibold"><Layers className="h-4 w-4 text-primary" /> 国债期货持仓热度</h2><p className="mt-1 text-xs text-muted-foreground">{pos?.date ?? "缺失"} · 分位按近 250 个交易观测日计，主力换月与市场扩容会削弱可比性；持仓高低不表示净多空方向。</p><div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">{pos?.contracts?.map(c => <div key={c.symbol} className="rounded-xl border border-border/60 p-3"><p className="text-sm font-medium">{c.label} 主力</p><p className="mt-2 text-sm">持仓分位 {n(c.oi_pct_1y * 100, 0)}%</p><p className="mt-1 text-xs text-muted-foreground">持仓 {n(c.oi / 10000, 1)} 万手 · 成交分位 {n(c.vol_pct_1y == null ? null : c.vol_pct_1y * 100, 0)}%</p></div>)}</div></GlassCard>;
}

function ValidationPanel({ data }: { data: BondsOverviewData }) {
  const report = data.validation;
  const rows = report?.results.filter(r => r.partition === "final_holdout" || r.status === "insufficient_training_labels") ?? [];
  return <GlassCard><h2 className="text-sm font-semibold">初版基线模型的回溯检验</h2><p className="mt-2 text-sm leading-relaxed">{report?.conclusion ?? "尚未运行验证。"}</p><p className="mt-2 text-xs text-muted-foreground">初版对照模型，参数与上方候选不同。样本 {report?.data_start ?? "—"}—{report?.data_end ?? "—"}，共 {report?.observations ?? 0} 日；最后 30% 作留出测试，未做参数挑选。</p><details className="mt-3"><summary className="cursor-pointer text-sm">测试区间结果与局限</summary><div className="mt-2 overflow-x-auto"><table className="w-full min-w-[650px] text-right text-xs tabular-nums"><thead><tr>{["目标", "期限", "模型", "预测数", "RMSE bp", "R²/零变化", "R²/历史均值", "MSE改善95%区间（对均值）"].map(h => <th key={h} className="p-2 font-medium">{h}</th>)}</tr></thead><tbody>{rows.map((r, i) => <tr key={i} className="border-b border-border/40"><td className="p-2">{r.target}</td><td>{r.horizon}D</td><td>{r.model ?? "数据不足"}</td><td>{r.n}</td><td>{n(r.rmse)}</td><td>{n(r.oos_r2_vs_zero, 3)}</td><td>{n(r.oos_r2_vs_mean, 3)}</td><td>{r.mse_improvement_vs_mean_95ci?.map(v => n(v)).join(" ~ ") ?? "—"}</td></tr>)}</tbody></table></div>{report?.limitations.map(x => <p key={x} className="mt-2 text-xs leading-relaxed text-muted-foreground">· {x}</p>)}<p className="mt-2 text-xs text-muted-foreground">R² 分别以零变化和历史均值为基准，负值表示比基准更差。预测区间重叠，观测数不等于独立样本数。</p></details>{report?.economics?.length ? <details className="mt-3"><summary className="cursor-pointer text-sm">分段指数代理 · 成本敏感性与回撤</summary><div className="mt-2 overflow-x-auto"><table className="w-full min-w-[600px] text-right text-xs"><thead><tr>{["目标/期限", "模型", "每次调仓成本bp", "非重叠期数", "累计超额%", "超额回撤%"].map(h => <th className="p-2" key={h}>{h}</th>)}</tr></thead><tbody>{report.economics.flatMap(e => e.results.map((r, i) => <tr key={`${e.target}-${e.horizon}-${i}`} className="border-b border-border/40"><td className="p-2">{e.target}/{e.horizon}D</td><td>{r.model}</td><td>{r.cost_bp}</td><td>{r.trades_n}</td><td>{n(r.cumulative_excess_pct)}</td><td>{n(r.max_drawdown_excess_pct)}</td></tr>))}</tbody></table></div><p className="mt-2 text-xs text-muted-foreground">按债券指数与资金代理估算，非实际可交易收益。</p></details> : null}</GlassCard>;
}

export function Bonds() {
  const { data, loading, revalidating, revalidate } = useSWR<BondsOverviewData>("bonds-overview:v2", fresh => api.bondsOverview(fresh), [], undefined, { persist: true });
  const { data: priors, loading: priorLoading, revalidating: priorBusy, revalidate: refreshPriors } = useSWR("bonds-priors:v1", fresh => api.bondsPriors(fresh), [], undefined, { persist: true });
  const fingerprint = data?.input_fingerprint ?? "pending";
  const { data: ai, revalidating: insightBusy, revalidate: revalidateInsight } = useSWR<BondsInsight | null>(`bonds-insight:v3:${fingerprint}`, fresh => fingerprint === "pending" ? Promise.resolve(null) : api.bondsInsight(fresh), [fingerprint], undefined, { persist: true });
  const insight = ai?.input_fingerprint === data?.input_fingerprint ? ai : null;
  const refresh = async () => { await Promise.all([revalidate(true), refreshPriors(true)]); await revalidateInsight(); };
  const context = data ? `债市研究快照（未经预测验证，不根据实验分数作买卖排序）：${JSON.stringify({ snapshot: data.input_fingerprint, candidate_priors: priors, sources: data.sources, curve: data.curve?.curve, calc: data.calc, states: data.framework?.states.map(s => ({ name: s.name, meaning: s.meaning, parts: s.parts.map(p => ({ ...p, hist: undefined })) })) })}` : "债市数据加载中";
  return <div className="mx-auto min-w-0 max-w-6xl">
    <PageHeader title="债市" actions={<div className="flex gap-2"><AskAiButton context={context} taskId="bonds" label="AI 复盘" suggestions={["解释当前曲线与历史的变化", "哪些数据缺口影响判断", "静态收益与压力情景如何区分"]} /><RefreshButton onClick={() => void refresh()} loading={loading || revalidating} /></div>} />
    {loading && !data ? <><ColdStartNotice title="正在准备债市研究快照" detail="核对收益率曲线、指标来源和同版本计算结果。" hint="旧数据会保留真实日期，更新完成后自动替换。" /><SkeletonBlock className="h-64" /></> : !data ? <GlassCard>债市快照暂不可用，请刷新重试。</GlassCard> : data.schema_version !== 2 ? <div className="space-y-4"><GlassCard><p className="text-sm">当前运行的后端尚未加载债市 V2。</p><p className="mt-2 text-xs text-muted-foreground">行情观察仍可查看；现金流情景、指标版本和预测验证在重启后端后启用。</p></GlassCard><MarketStats data={data} /><ObservationPanel data={data} /></div> : <div className="space-y-4">
      <SegmentPanel data={data} insight={insight ?? null} busy={insightBusy} refreshInsight={() => void revalidateInsight(true)} />
      <BondPriorsPanel data={priors} loading={priorLoading} busy={priorBusy} refresh={() => void refreshPriors(true)} />
      <h2 className="pt-3 text-base font-semibold">市场行情</h2>
      <MarketStats data={data} />
      <CurvePanel data={data} />
      <details className="rounded-xl border border-border/60 p-5"><summary className="cursor-pointer text-sm font-medium">深入研究<span className="ml-2 font-normal text-muted-foreground">指标、情景测算与历史检验</span></summary><div className="mt-4 space-y-4">
        <FrameworkPanel data={data} />
        <ObservationPanel data={data} />
        <CalcPanel data={data} />
        <PositioningPanel data={data} />
        <ValidationPanel data={data} />
      </div></details>
      <details className="rounded-xl border border-border/60 p-5"><summary className="cursor-pointer text-sm text-muted-foreground">数据来源明细 · {data.updated_sources ?? 0}/{data.total_sources ?? 0} 个来源已更新{data.cache_state === "error" ? " · 更新失败" : ""}</summary><div className="mt-4"><SourcePanel data={data} /></div></details>
      <p className="px-1 pb-2 text-xs leading-relaxed text-muted-foreground">数据取自各公开来源的最新可得快照。指标分数与情景测算未经实盘验证，不构成投资建议。</p>
    </div>}
  </div>;
}
