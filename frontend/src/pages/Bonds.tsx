import { useMemo, useState } from "react";
import { Activity, AlertCircle, Calculator, Coins, Gauge, Globe2, Landmark, Layers, Percent, PieChart, RefreshCw, Scale } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Sparkline } from "@/components/ui/Sparkline";
import { AskAiButton } from "@/components/ui/AskAiButton";
import {
  api,
  type BondsCalcData,
  type BondsFrameworkData,
  type BondsFrameworkState,
  type BondsOverviewData,
  type BondsPositioningData,
  type BondsSegmentRow,
  type BondsSegmentsData,
  type BondsSeriesPoint,
  type HistPoint,
} from "@/lib/api";
import { useSWR } from "@/hooks/useSWR";
import { cn } from "@/lib/utils";

// 债市页：按研究框架的定价链条组织（政策锚 → 资金 → 曲线/期限 → 信用 → 全球对照）。
// 只呈现客观数据；解读由用户自己的 AI 给出（AskAiButton 带本页上下文）。

const SPREAD_LABEL: Record<string, string> = {
  "10年-1年": "10Y-1Y 期限利差",
  "30年-1年": "30Y-1Y 期限利差",
  "3年-1年": "3Y-1Y 期限利差",
};

const CREDIT_LABEL: Record<string, string> = {
  "AAA-1年": "AAA 中短票 · 1Y",
  "AAA-3年": "AAA 中短票 · 3Y",
  "AAA-5年": "AAA 中短票 · 5Y",
  "商行债-1年": "AAA 商行债 · 1Y",
};

const FUNDING_ORDER = ["O/N", "1W", "1M", "3M", "6M", "1Y"];

function lastN<T>(arr: T[] | undefined, n: number): T[] {
  return arr && arr.length ? arr.slice(-n) : [];
}

function chgOf(hist: { v: number }[] | undefined, days = 1): number | null {
  if (!hist || hist.length <= days) return null;
  return hist[hist.length - 1].v - hist[hist.length - 1 - days].v;
}

function DeltaText({ v, unit = "bp", digits = 1 }: { v: number | null; unit?: string; digits?: number }) {
  if (v == null) return <span className="text-[11px] text-muted-foreground/60">—</span>;
  return (
    <span className={cn("text-[11px] font-semibold tabular-nums", v >= 0 ? "text-danger" : "text-success")}>
      {v >= 0 ? "+" : ""}{v.toFixed(digits)}{unit}
    </span>
  );
}

function MiniStat({
  icon: Icon, label, value, chg, hist, suffix = "", color = "--primary",
}: {
  icon: typeof Landmark; label: string; value: string; chg?: React.ReactNode;
  hist?: HistPoint[]; suffix?: string; color?: string;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-border/60 bg-background/30 p-3">
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Icon className="h-3.5 w-3.5 text-primary/80" /> {label}
      </div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-base font-bold tabular-nums leading-none">{value}{suffix && <span className="text-xs font-medium text-muted-foreground">{suffix}</span>}</span>
        {chg}
      </div>
      {hist && hist.length > 1 && <Sparkline data={hist} height={30} color={color} valueSuffix={suffix} />}
    </div>
  );
}

function toHist(series: BondsSeriesPoint[] | undefined): HistPoint[] {
  return (series ?? []).map((p) => ({ date: p.date, v: p.v }));
}

// —— 研究框架层：八状态仪表盘（§2.2 八个统一状态，[-2,+2]，正分=对债券价格有利）——

const STATE_TONE = (score: number | null) => {
  if (score == null) return { text: "text-muted-foreground", bg: "bg-muted", label: "数据缺" };
  if (score >= 0.75) return { text: "text-success", bg: "bg-success", label: "偏多" };
  if (score >= 0.25) return { text: "text-success/90", bg: "bg-success/80", label: "略偏多" };
  if (score > -0.25) return { text: "text-muted-foreground", bg: "bg-muted-foreground/50", label: "中性" };
  if (score > -0.75) return { text: "text-danger/90", bg: "bg-danger/80", label: "略偏空" };
  return { text: "text-danger", bg: "bg-danger", label: "偏空" };
};

function StateBar({ score }: { score: number | null }) {
  // [-2,+2] 映射到横条；中线为 0。正分向右（绿=利好债），负分向左（红=利空债）。
  const pct = score == null ? 0 : (score / 2) * 50;
  return (
    <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div className="absolute left-1/2 top-0 h-full w-px bg-border" />
      {score != null && (
        <div
          className={cn("absolute top-0 h-full rounded-full", score >= 0 ? "bg-success left-1/2" : "bg-danger")}
          style={{ width: `${Math.abs(pct)}%`, ...(score >= 0 ? {} : { left: `${50 - Math.abs(pct)}%` }) }}
        />
      )}
    </div>
  );
}

function FrameworkPanel({ fw }: { fw?: BondsFrameworkData }) {
  const [open, setOpen] = useState<BondsFrameworkState | null>(null);
  if (!fw?.states?.length) return null;
  return (
    <GlassCard>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Activity className="h-4 w-4 text-primary" /> 研究框架 · 八状态仪表盘
        </h3>
        <span className="text-xs text-muted-foreground">
          [-2, +2] · 正分 = 对债券价格有利 · 点击卡片看明细 · 覆盖 {fw.coverage}% · {fw.date}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {fw.states.map((s: BondsFrameworkState) => {
          const tone = STATE_TONE(s.score);
          return (
            <button
              key={s.key}
              onClick={() => setOpen(s)}
              className="rounded-xl border border-border/60 bg-background/30 p-3.5 text-left transition-colors hover:border-primary/40"
            >
              <div className="flex items-start justify-between gap-3">
                <span className="text-xs font-medium leading-snug">{s.name}</span>
                <span className="flex flex-col items-end leading-tight">
                  <span className={cn("text-lg font-bold tabular-nums", tone.text)}>
                    {s.score == null ? "—" : (s.score > 0 ? "+" : "") + s.score.toFixed(2)}
                  </span>
                  <span className="text-[10px] text-muted-foreground">{tone.label}</span>
                </span>
              </div>
              {s.hist && s.hist.length > 1 && (
                <div className="mt-2">
                  <Sparkline data={s.hist} height={44} valueSuffix="" />
                </div>
              )}
            </button>
          );
        })}
      </div>
      <p className="mt-3 text-[10px] leading-relaxed text-muted-foreground/70">
        方法：{fw.method}。{fw.notes?.[0]}
      </p>

      {/* 指标明细弹层 */}
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onClick={() => setOpen(null)}
        >
          <div
            className="max-h-[80vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-background p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <h4 className="text-sm font-semibold">{open.name}</h4>
                <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{open.meaning}</p>
              </div>
              <div className="flex items-center gap-3">
                <span className={cn("text-lg font-bold tabular-nums", STATE_TONE(open.score).text)}>
                  {open.score == null ? "—" : (open.score > 0 ? "+" : "") + open.score.toFixed(2)}
                </span>
                <button onClick={() => setOpen(null)} className="rounded-lg border border-border px-2 py-1 text-xs text-muted-foreground hover:border-primary/40 hover:text-primary">
                  关闭
                </button>
              </div>
            </div>
            <div className="mb-3"><StateBar score={open.score} /></div>
            {open.parts.length > 0 ? (
              <div className="space-y-2">
                {open.parts.map((p) => {
                  const hist = (p.hist ?? []).filter((x): x is { date: string; v: number } => x.v != null && !!x.date);
                  return (
                    <div key={p.key} className="rounded-lg border border-border/50 px-3 py-2">
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-xs">{p.label}</span>
                        <span className="flex shrink-0 items-center gap-3 text-[11px] tabular-nums">
                          <span className="text-muted-foreground">分位 {(p.pct * 100).toFixed(0)}%</span>
                          <span className="text-muted-foreground/60">权重 {p.weight}</span>
                          <span className={cn("font-semibold", p.score >= 0 ? "text-success" : "text-danger")}>
                            {p.score >= 0 ? "+" : ""}{p.score.toFixed(2)}
                          </span>
                        </span>
                      </div>
                      {hist.length > 1 && (
                        <div className="mt-1">
                          <Sparkline data={hist} height={44} valueSuffix="" />
                          <div className="mt-0.5 flex justify-between text-[10px] text-muted-foreground/60">
                            <span>{hist[0].date}</span>
                            <span>最新 {hist[hist.length - 1].v.toFixed(2)}</span>
                            <span>{hist[hist.length - 1].date}</span>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="py-6 text-center text-sm text-muted-foreground">该状态暂无可用指标</p>
            )}
            <p className="mt-3 text-[10px] leading-relaxed text-muted-foreground/60">
              分位 = 该指标近 60 期窗口内的相对位置（0% 最低，100% 最高）；单项分数由分位映射到 [-2,+2] 后按权重合成为状态分。
            </p>
          </div>
        </div>
      )}
    </GlassCard>
  );
}

// —— 分品种评分：短债到杠杆套息（框架 §11.2 权重先验 + §0.2 失效条件）——
function SegmentPanel({ seg }: { seg?: BondsSegmentsData }) {
  if (!seg?.rows?.length) return null;
  return (
    <GlassCard>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <PieChart className="h-4 w-4 text-primary" /> 分品种评分 · 短债 → 超长债 → 信用 → 杠杆套息
        </h3>
        <span className="text-xs text-muted-foreground">[-2,+2] · 相对排序，非买卖建议 · {seg.date}</span>
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {seg.rows.map((r: BondsSegmentRow) => {
          const tone = STATE_TONE(r.score);
          return (
            <div key={r.segment} className="rounded-xl border border-border/60 bg-background/30 p-3.5">
              <div className="flex items-start justify-between gap-3">
                <span className="text-sm font-semibold">{r.segment}</span>
                <span className="flex flex-col items-end leading-tight">
                  <span className={cn("text-lg font-bold tabular-nums", tone.text)}>
                    {r.score > 0 ? "+" : ""}{r.score.toFixed(2)}
                  </span>
                  <span className="text-[10px] text-muted-foreground">{tone.label}</span>
                </span>
              </div>
              {r.hist && r.hist.length > 1 && (
                <div className="mt-2">
                  <Sparkline data={r.hist} height={44} valueSuffix="" />
                </div>
              )}
              {r.carry_roll_bp_3m != null && (
                <div className="mt-1.5 text-[11px]">
                  <span className="text-muted-foreground">静态 carry+roll：</span>
                  <span className={cn("font-medium tabular-nums", r.carry_roll_bp_3m >= 0 ? "text-success" : "text-danger")}>
                    {r.carry_roll_bp_3m >= 0 ? "+" : ""}{r.carry_roll_bp_3m.toFixed(0)} bp / 3M
                  </span>
                </div>
              )}
              <p className="mt-2 border-t border-border/40 pt-2 text-[10px] leading-relaxed text-muted-foreground/70" title={r.invalidation}>
                <AlertCircle className="mr-0.5 inline h-3 w-3 align-[-1px]" />失效：{r.invalidation}
              </p>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground/70">{seg.method}</p>
    </GlassCard>
  );
}

// —— 仓位与拥挤度：国债期货量仓（框架 §9.2 Crowding）——
function PositioningPanel({ pos }: { pos?: BondsPositioningData }) {
  if (!pos?.contracts?.length) return null;
  return (
    <GlassCard>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Layers className="h-4 w-4 text-primary" /> 仓位与拥挤度 · 国债期货量仓
        </h3>
        <span className="text-xs text-muted-foreground">持仓分位越高 = 久期越拥挤 · 截至 {pos.date}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {pos.contracts.map((c) => {
          const pct = c.oi_pct_1y;
          const tone = pct >= 0.8 ? "text-danger" : pct >= 0.6 ? "text-warning" : "text-muted-foreground";
          return (
            <div key={c.symbol} className="rounded-xl border border-border/60 bg-background/30 p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium">{c.label} 主力</span>
                <span className="text-[10px] text-muted-foreground">{c.close?.toFixed(2) ?? "—"}</span>
              </div>
              <div className="mt-1 flex items-baseline gap-1.5">
                <span className={cn("text-base font-bold tabular-nums", tone)}>持仓分位 {(pct * 100).toFixed(0)}%</span>
              </div>
              <StateBar score={Math.min(2, Math.max(-2, -(pct - 0.5) * 4))} />
              <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
                <span>持仓 {(c.oi / 10000).toFixed(1)} 万手</span>
                <span>成交分位 {c.vol_pct_1y != null ? `${(c.vol_pct_1y * 100).toFixed(0)}%` : "—"}</span>
              </div>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground/70">{pos.method}</p>
    </GlassCard>
  );
}

// —— 计算层：carry / roll / breakeven 表（框架 §7.4，纯曲线推导）——
function CalcPanel({ calc }: { calc?: BondsCalcData }) {
  if (!calc?.rows?.length) return null;
  const best = calc.rows.reduce((a, b) => (b.total_static_bp_3m > a.total_static_bp_3m ? b : a));
  return (
    <GlassCard>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Calculator className="h-4 w-4 text-primary" /> 计算层 · Carry / Roll / Breakeven（3 个月持有，bp）
        </h3>
        <span className="text-xs text-muted-foreground">
          资金成本 Shibor O/N {calc.funding_cost?.toFixed(3) ?? "—"}% · 截至 {calc.date}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-xs tabular-nums">
          <thead>
            <tr className="border-b border-border/60 text-[10px] text-muted-foreground">
              <th className="py-1.5 text-left font-medium">期限</th>
              <th className="text-right font-medium">收益率 %</th>
              <th className="text-right font-medium">Carry</th>
              <th className="text-right font-medium">Roll</th>
              <th className="text-right font-medium">静态合计</th>
              <th className="text-right font-medium">盈亏平衡上行</th>
            </tr>
          </thead>
          <tbody>
            {calc.rows.map((r) => (
              <tr key={r.tenor} className={cn("border-b border-border/30", r.tenor === best.tenor && "bg-primary/5")}>
                <td className="py-1.5 text-left font-medium">{r.tenor}</td>
                <td className="text-right">{r.yield.toFixed(3)}</td>
                <td className={cn("text-right", r.carry_bp_3m >= 0 ? "text-success" : "text-danger")}>
                  {r.carry_bp_3m >= 0 ? "+" : ""}{r.carry_bp_3m.toFixed(1)}
                </td>
                <td className={cn("text-right", r.roll_bp_3m >= 0 ? "text-success" : "text-danger")}>
                  {r.roll_bp_3m >= 0 ? "+" : ""}{r.roll_bp_3m.toFixed(1)}
                </td>
                <td className={cn("text-right font-semibold", r.total_static_bp_3m >= 0 ? "text-success" : "text-danger")}>
                  {r.total_static_bp_3m >= 0 ? "+" : ""}{r.total_static_bp_3m.toFixed(1)}
                </td>
                <td className="text-right text-muted-foreground">
                  {r.breakeven_bp_3m == null ? "—" : `${r.breakeven_bp_3m.toFixed(1)} bp`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground/70">
        {calc.method} 盈亏平衡 = 收益率最多上行多少，票息+骑乘仍能覆盖资本损失（框架 §7.4）。
      </p>
    </GlassCard>
  );
}

export function Bonds() {
  const { data, loading, revalidate } = useSWR<BondsOverviewData>("bonds-overview", (fresh) => api.bondsOverview(fresh), []);
  const { data: fw } = useSWR<BondsFrameworkData>("bonds-framework", (fresh) => api.bondsFramework(fresh), []);
  const { data: seg } = useSWR<BondsSegmentsData>("bonds-segments", (fresh) => api.bondsSegments(fresh), []);

  const curve = data?.curve;
  const funding = data?.funding;
  const policy = data?.policy;
  const index = data?.index;
  const globe = data?.global;

  const yields = curve?.yields ?? {};
  const pos = data?.positioning;

  const aiContext = useMemo(() => {
    const parts: string[] = [];
    if (fw?.states?.length) {
      const st = fw.states
        .map((s) => `${s.name}=${s.score == null ? "缺数据" : s.score.toFixed(2)}`)
        .join("，");
      parts.push(`研究框架八状态（截至 ${fw.date}，[-2,+2]，正分=对债券价格有利）：${st}`);
    }
    if (seg?.rows?.length) {
      const sg = seg.rows
        .map((r) => `${r.segment}=${r.score.toFixed(2)}（${r.drivers[0]?.state ?? ""} ${r.drivers[0]?.contribution ?? ""}）`)
        .join("，");
      parts.push(`分品种评分（相对排序）：${sg}`);
    }
    if (curve?.curve?.length) {
      parts.push(`国债收益率曲线（截至 ${curve.date}）：${curve.curve.map((p) => `${p.tenor}=${p.value}%`).join("，")}`);
      const sp = Object.entries(curve.spreads || {})
        .map(([k, h]) => `${SPREAD_LABEL[k] || k}=${h.length ? h[h.length - 1].v.toFixed(1) : "—"}bp`).join("，");
      if (sp) parts.push(`期限利差：${sp}`);
      const cr = Object.entries(curve.credit || {})
        .map(([k, h]) => `${CREDIT_LABEL[k] || k}=${h.length ? h[h.length - 1].v.toFixed(1) : "—"}bp`).join("，");
      if (cr) parts.push(`信用利差：${cr}`);
    }
    if (funding?.series) {
      const f = FUNDING_ORDER.filter((k) => funding.series[k])
        .map((k) => `Shibor ${k}=${funding.series[k].slice(-1)[0]?.v.toFixed(3)}%`).join("，");
      if (f) parts.push(`资金利率（截至 ${funding.date}）：${f}`);
    }
    if (policy?.anchors?.length) {
      parts.push(`政策利率锚（${policy.date}）：${policy.anchors.map((a) => `${a.label}=${a.value}%（较上月 ${a.chg_bp ?? 0}bp）`).join("，")}`);
    }
    if (index?.series?.length) {
      const last = index.series[index.series.length - 1];
      parts.push(`中债-新综合指数（截至 ${index.date}）：${last.v.toFixed(2)}`);
    }
    if (globe?.series?.length) {
      const last = globe.series[globe.series.length - 1];
      const sp = globe.spread?.slice(-1)[0];
      parts.push(`中美 10Y 国债（截至 ${globe.date}）：中国 ${last.cn.toFixed(2)}%、美国 ${last.us.toFixed(2)}%、利差 ${sp ? sp.v.toFixed(1) : "—"}bp`);
    }
    if (pos?.contracts?.length) {
      const p = pos.contracts.map((c) => `${c.label}持仓${(c.oi / 10000).toFixed(1)}万手（近一年分位 ${(c.oi_pct_1y * 100).toFixed(0)}%）`).join("，");
      parts.push(`国债期货量仓（截至 ${pos.date}）：${p}`);
    }
    return parts.length ? `债市数据快照：\n${parts.join("\n")}` : "";
  }, [fw, seg, curve, funding, policy, index, globe, pos]);

  const hasAny = !!(curve?.curve?.length || funding?.series || policy?.anchors?.length || index?.series?.length || globe?.series?.length);

  const f = funding?.series ?? {};

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title="债市" subtitle="分品种评分 · 八状态框架 · 关键指标 · 计算层 · 仓位拥挤" actions={
        <div className="flex items-center gap-2">
          <AskAiButton
            context={aiContext || "债市页面数据加载中。"}
            taskId="bonds"
            suggestions={["当前收益率曲线形态说明什么", "资金利率与政策锚的偏离怎么理解", "信用利差近期变化说明什么"]}
          />
          <button
            onClick={() => revalidate(true)}
            disabled={loading}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:border-primary/40 hover:text-primary disabled:opacity-50"
          >
            <RefreshCw className={loading ? "h-4 w-4 animate-spin" : "h-4 w-4"} /> 刷新
          </button>
        </div>
      } />

      {loading && !data ? (
        <GlassCard className="flex items-center justify-center p-16 text-sm text-muted-foreground">
          正在加载债市数据…
        </GlassCard>
      ) : !hasAny ? (
        <GlassCard className="flex items-center justify-center p-16 text-sm text-muted-foreground">
          债市数据暂不可用（数据源无返回），稍后刷新重试
        </GlassCard>
      ) : (
        <div className="space-y-4">
          {/* —— 分品种评分：短债 → 超长 → 信用 → 杠杆套息（框架 §11.2，放最前）—— */}
          <SegmentPanel seg={seg ?? undefined} />

          {/* —— 框架层：八状态仪表盘（宏观→政策→资金→供需→曲线→信用→仓位→全球）—— */}
          <FrameworkPanel fw={fw ?? undefined} />

          {/* —— 顶部快览：一排关键数字（当期值 + 日变动 + 迷你走势）—— */}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <MiniStat icon={Percent} label="10Y 国债" suffix="%"
              value={yields["10年"]?.slice(-1)[0]?.v?.toFixed(2) ?? "—"}
              chg={<DeltaText v={chgOf(toHist(yields["10年"]))} unit="bp" digits={1} />}
              hist={toHist(lastN(yields["10年"], 60))} />
            <MiniStat icon={Scale} label="10Y-1Y 利差" suffix="bp"
              value={lastN(curve?.spreads?.["10年-1年"], 1).slice(-1)[0]?.v?.toFixed(1) ?? "—"}
              chg={<DeltaText v={chgOf(toHist(curve?.spreads?.["10年-1年"]))} />} />
            <MiniStat icon={Coins} label="Shibor O/N" suffix="%"
              value={f["O/N"]?.slice(-1)[0]?.v?.toFixed(3) ?? "—"}
              chg={<DeltaText v={chgOf(toHist(f["O/N"]))} unit="bp" digits={1} />} />
            <MiniStat icon={Gauge} label="Shibor 3M" suffix="%"
              value={f["3M"]?.slice(-1)[0]?.v?.toFixed(3) ?? "—"}
              chg={<DeltaText v={chgOf(toHist(f["3M"]))} unit="bp" digits={1} />} />
            <MiniStat icon={Landmark} label="LPR 1Y" suffix="%"
              value={policy?.anchors?.find((a) => a.key === "LPR_1Y")?.value?.toFixed(2) ?? "—"}
              chg={<DeltaText v={policy?.anchors?.find((a) => a.key === "LPR_1Y")?.chg_bp ?? null} />} />
            <MiniStat icon={Globe2} label="中美 10Y 利差" suffix="bp"
              value={globe?.spread?.slice(-1)[0]?.v?.toFixed(1) ?? "—"}
              chg={<DeltaText v={chgOf(toHist(globe?.spread))} />} />
          </div>

          {/* —— 计算层：carry / roll / breakeven（框架 §7.4 / §12.3）—— */}
          <CalcPanel calc={data?.calc} />

          {/* —— 仓位与拥挤度：国债期货量仓（框架 §9.2）—— */}
          <PositioningPanel pos={data?.positioning} />
        </div>
      )}
    </div>
  );
}
