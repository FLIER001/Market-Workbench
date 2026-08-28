import { Fragment, useMemo, useState } from "react";
import { AlertCircle, ChevronDown, ChevronUp, Database, Loader2, RefreshCw, Search, ShieldCheck, Star } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type PFSCandidate, type PFSData, type PFSTier } from "@/lib/api";
import { loadFundWatch, toggleFundWatch } from "@/lib/fundWatch";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";
import { FundDetail } from "./FundDetail";

const STRATEGIES = ["", "主动股票", "偏股混合", "灵活配置", "平衡混合"];
const TIERS: { id: "" | PFSTier; label: string }[] = [
  { id: "", label: "全部分层" },
  { id: "core_buy", label: "核心买入" },
  { id: "potential_buy", label: "潜力买入" },
  { id: "watch", label: "观察" },
  { id: "review", label: "复核" },
  { id: "exclude", label: "排除" },
];
const TIER_STYLE: Record<PFSTier, string> = {
  core_buy: "bg-danger/15 text-danger border-danger/30",
  potential_buy: "bg-primary/15 text-primary border-primary/30",
  watch: "bg-info/15 text-info border-info/30",
  review: "bg-warning/15 text-warning border-warning/30",
  exclude: "bg-muted text-muted-foreground border-border",
};
const TIER_LABEL: Record<PFSTier, string> = {
  core_buy: "核心买入", potential_buy: "潜力买入", watch: "观察", review: "复核", exclude: "排除",
};
const Q_LABELS: Record<string, string> = { manager: "经理/团队", process: "流程", verified_skill: "净值证据", platform: "平台", implementation: "产品实现" };
const P_LABELS: Record<string, string> = { evidence: "证据积累", capacity: "规模空间", bandwidth: "管理带宽", flow: "资金拥挤", platform_trend: "平台趋势", implementation: "实现优势" };
const fmt = (value: number | null | undefined, digits = 1) => value == null ? "—" : value.toFixed(digits);

export function FundScreenPanel() {
  const [strategy, setStrategy] = useState("");
  const [tier, setTier] = useState<"" | PFSTier>("");
  const [pool, setPool] = useState("");
  const [keyword, setKeyword] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [manualRefresh, setManualRefresh] = useState(false);
  const [watched, setWatched] = useState(() => new Set(loadFundWatch().map((item) => item.code)));
  const { data, loading, revalidating, revalidate } = useSWR<PFSData>("fund:pfs:v1", async (fresh) => {
    const result = await api.fundPfs(fresh);
    setErr(null);
    return result;
  }, [], (error: unknown) => setErr(error instanceof ApiError ? error.message : "PFS 加载失败"), { persist: true });

  const rows = useMemo(() => (data?.rows || []).filter((row) => {
    if (strategy && row.strategy !== strategy) return false;
    if (tier && row.tier !== tier) return false;
    const days = row.team_tenure_days || 0;
    if (pool === "mature" && days < 1095) return false;
    if (pool === "emerging" && (days < 548 || days >= 1095)) return false;
    const query = keyword.trim().toLowerCase();
    return !query || row.name.toLowerCase().includes(query) || row.code.startsWith(query) || row.manager.toLowerCase().includes(query);
  }), [data, keyword, pool, strategy, tier]);

  const refresh = async () => {
    setManualRefresh(true);
    try { await revalidate(true); setErr(null); }
    catch (error) { setErr(error instanceof ApiError ? error.message : "PFS 刷新失败"); }
    finally { setManualRefresh(false); }
  };
  const toggleWatch = (row: PFSCandidate) => {
    toggleFundWatch({ code: row.code, name: row.name, type: row.fund_type });
    setWatched(new Set(loadFundWatch().map((item) => item.code)));
  };

  return (
    <div className="space-y-4">
      <GlassCard glow>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="h-4 w-4 text-primary" />PFS V3.0 · Manager-First</div>
          </div>
          <div className="text-right text-xs text-muted-foreground">
            <div>数据日 {data?.as_of || "—"} · 生成 {data?.generated_at || "—"}</div>
          </div>
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          <Stat label="主动权益份额 Universe" value={data?.universe_count} />
          <Stat label="完成深筛" value={data?.candidate_count} />
          <Stat label="观察 / 复核" value={(data?.tier_counts.watch || 0) + (data?.tier_counts.review || 0)} />
          <Stat label="直接证据覆盖" value={data ? `${data.methodology.direct_coverage_pct}%` : undefined} />
          <Stat label="替代指标 / 缺失" value={data ? `${data.methodology.proxy_coverage_pct}% / ${data.methodology.missing_coverage_pct}%` : undefined} />
        </div>
      </GlassCard>

      <GlassCard>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex flex-wrap items-center gap-1">
            {STRATEGIES.map((item) => <button key={item || "all"} onClick={() => setStrategy(item)}
              className={cn("rounded-lg px-2.5 py-1.5 text-xs transition", strategy === item ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-black/20")}>{item || "全部策略"}</button>)}
          </div>
          <div className="h-5 w-px bg-border/60" />
          <select value={pool} onChange={(event) => setPool(event.target.value)} className="rounded-lg border border-border bg-black/20 px-2.5 py-1.5 text-xs outline-none">
            <option value="">成熟 + 新锐</option><option value="mature">成熟池（≥3年）</option><option value="emerging">新锐池（18–36月）</option>
          </select>
          <select value={tier} onChange={(event) => setTier(event.target.value as "" | PFSTier)} className="rounded-lg border border-border bg-black/20 px-2.5 py-1.5 text-xs outline-none">
            {TIERS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
          <label className="relative min-w-44 flex-1 sm:max-w-64"><Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
            <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="基金 / 代码 / 经理"
              className="w-full rounded-lg border border-border bg-black/20 py-1.5 pl-8 pr-2 text-xs outline-none focus:border-primary/60" /></label>
          <button onClick={refresh} disabled={manualRefresh || loading} className="rounded-lg border border-border p-1.5 text-muted-foreground transition hover:text-primary disabled:opacity-40" title="重建 PFS 快照">
            <RefreshCw className={cn("h-4 w-4", (manualRefresh || revalidating) && "animate-spin")} />
          </button>
        </div>
        {err && <div className="mt-3 flex items-center gap-2 text-sm text-danger"><AlertCircle className="h-4 w-4" />{err}</div>}
      </GlassCard>

      <GlassCard className="overflow-x-auto p-0">
        {!data ? <div className="flex h-48 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" />首次构建需汇总经理任期与风险数据…</div> : <>
          <table className="w-full min-w-[1040px] text-sm">
            <thead><tr className="border-b border-border/60 text-left text-xs text-muted-foreground">
              <th className="px-4 py-3 font-medium">基金 / 现任团队</th><th className="px-3 py-3 font-medium">真实策略初标</th><th className="px-3 py-3 font-medium">PFS</th>
              <th className="px-3 py-3 font-medium">Q / P</th><th className="px-3 py-3 font-medium">Confidence</th><th className="px-3 py-3 font-medium">经理规模 / 产品</th><th className="px-3 py-3 font-medium">分层</th><th className="px-3 py-3" />
            </tr></thead>
            <tbody>{rows.map((row) => <Fragment key={row.code}>
              <tr onClick={() => setExpanded(expanded === row.code ? null : row.code)} className="cursor-pointer border-b border-border/30 transition hover:bg-black/10">
                <td className="px-4 py-3"><div className="max-w-72 truncate font-medium">{row.name}</div><div className="mt-0.5 text-xs text-muted-foreground"><span className="font-mono">{row.code}</span> · {row.manager || "经理缺失"} · {row.platform || "平台缺失"}</div></td>
                <td className="px-3 py-3"><div>{row.strategy}</div><div className="text-xs text-muted-foreground">{row.candidate_type}</div></td>
                <td className="px-3 py-3"><div className={cn("font-mono text-lg font-bold", row.final_score >= 64 ? "text-primary" : row.final_score >= 61 ? "text-info" : "text-muted-foreground")}>{fmt(row.final_score)}</div><div className="text-[10px] text-muted-foreground">Raw {fmt(row.raw_score)}</div></td>
                <td className="px-3 py-3 font-mono text-xs"><span className="text-foreground">{fmt(row.quality_score)}</span><span className="text-muted-foreground"> / </span><span className="text-primary">{fmt(row.potential_score)}</span></td>
                <td className="px-3 py-3"><div className="font-mono">{Math.round(row.confidence * 100)}%</div><div className="mt-1 h-1.5 w-20 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${row.confidence * 100}%` }} /></div></td>
                <td className="px-3 py-3 font-mono text-xs"><div>{row.manager_aum != null ? `${fmt(row.manager_aum)}亿` : "—"}</div><div className="text-muted-foreground">{row.manager_fund_count != null ? `${row.manager_fund_count} 个产品` : "—"}</div></td>
                <td className="px-3 py-3"><span className={cn("inline-flex rounded-full border px-2 py-1 text-xs", TIER_STYLE[row.tier])}>{TIER_LABEL[row.tier]}</span></td>
                <td className="px-3 py-3 text-right"><button onClick={(event) => { event.stopPropagation(); toggleWatch(row); }} title={watched.has(row.code) ? "移出自选" : "加入自选"} className="rounded-lg p-1.5 text-muted-foreground hover:text-primary"><Star className={cn("h-4 w-4", watched.has(row.code) && "fill-primary text-primary")} /></button>{expanded === row.code ? <ChevronUp className="inline h-4 w-4" /> : <ChevronDown className="inline h-4 w-4" />}</td>
              </tr>
              {expanded === row.code && <tr className="border-b border-border/40"><td colSpan={8} className="bg-black/10 px-4 py-4"><ResearchCard row={row} /><FundDetail code={row.code} name={row.name} onClose={() => setExpanded(null)} onWatchChange={() => setWatched(new Set(loadFundWatch().map((item) => item.code)))} /></td></tr>}
            </Fragment>)}</tbody>
          </table>
          {!rows.length && <div className="py-12 text-center text-sm text-muted-foreground">当前条件没有候选基金</div>}
        </>}
      </GlassCard>

      {data && <GlassCard><details><summary className="cursor-pointer text-sm font-semibold"><Database className="mr-2 inline h-4 w-4 text-primary" />数据口径、缺口与模型纪律</summary>
        <div className="mt-3 grid gap-4 text-xs leading-5 text-muted-foreground lg:grid-cols-2"><div>{data.methodology.definitions.map((item) => <p key={item}>• {item}</p>)}</div><div><div className="font-semibold text-foreground">尚未公开稳定取得</div>{data.methodology.limitations.map((item) => <p key={item}>• {item}</p>)}</div></div>
      </details></GlassCard>}
      <p className="text-xs text-muted-foreground">PFS 是研究候选生成器，不是收益承诺或自动交易信号；买入分层必须同时满足方案阈值，缺失证据不会被历史涨幅替代。</p>
    </div>
  );
}

function ResearchCard({ row }: { row: PFSCandidate }) {
  return <div className="space-y-4">
    <div className="grid gap-3 lg:grid-cols-3"><ListCard title="Why Good?" items={row.why_good} /><ListCard title="Why Potential?" items={row.why_potential} /><ListCard title="What Breaks the Thesis?" items={row.breaks_thesis} warning /></div>
    {(row.gate_failures.length > 0 || row.review_reasons.length > 0 || row.risk_notes.length > 0) && <div className="rounded-xl border border-warning/30 bg-warning/5 p-3 text-xs text-warning">{[...row.gate_failures, ...row.review_reasons, ...row.risk_notes].map((item) => <div key={item}>• {item}</div>)}</div>}
    <div className="grid gap-4 lg:grid-cols-2"><ScoreGroup title={`Quality ${fmt(row.quality_score)}`} labels={Q_LABELS} values={row.quality_components} /><ScoreGroup title={`Potential ${fmt(row.potential_score)}`} labels={P_LABELS} values={row.potential_components} /></div>
    <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
      <Metric label="当前团队上任" value={row.team_start_date || "—"} />
      <Metric label="当前团队任期" value={row.team_tenure_days != null ? `${(row.team_tenure_days / 365).toFixed(1)}年` : "—"} />
      <Metric label={`${row.risk_period || "任期净值"}最大回撤`} value={row.risk_metrics.max_drawdown != null ? `${fmt(row.risk_metrics.max_drawdown)}%` : `${fmt(row.nav_metrics.max_drawdown)}%`} />
      <Metric label={`${row.risk_period || "任期净值"}夏普`} value={fmt(row.risk_metrics.sharpe, 2)} />
      <Metric label="年度固定费率" value={row.annual_fee_pct != null ? `${fmt(row.annual_fee_pct)}%` : "—"} />
      <Metric label="申购 / 赎回" value={`${row.purchase_status || "—"} / ${row.redemption_status || "—"}`} />
      <Metric label="公开字段覆盖" value={`${row.data_coverage}%`} />
    </div>
    <div>
      <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">任期净值与季度真实披露</div>
      <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Metric label="任期年化收益" value={pct(row.nav_metrics.ann_return)} />
        <Metric label="任期Sortino" value={fmt(row.nav_metrics.sortino, 2)} />
        <Metric label="日收益CVaR 95%" value={pct(row.nav_metrics.cvar95)} />
        <Metric label="最大回撤后恢复" value={row.nav_metrics.unrecovered ? "尚未恢复" : row.nav_metrics.recovery_days != null ? `${row.nav_metrics.recovery_days}天` : "—"} />
        <Metric label="Rolling 12M正收益" value={pct(row.nav_metrics.rolling_12m_positive_ratio)} />
        <Metric label="Rolling 36M正收益" value={pct(row.nav_metrics.rolling_36m_positive_ratio)} />
        <Metric label={`净份额流入率${row.scale_metrics.date ? ` · ${row.scale_metrics.date}` : ""}`} value={pct(row.scale_metrics.net_share_flow_rate)} />
        <Metric label="季度赎回率" value={pct(row.scale_metrics.redemption_rate)} />
        <Metric label="季度AUM变化" value={pct(row.scale_metrics.aum_growth_1q)} />
        <Metric label={`机构持有${row.holder_metrics.date ? ` · ${row.holder_metrics.date}` : ""}`} value={pct(row.holder_metrics.institution_pct)} />
        <Metric label="机构持有变化" value={row.holder_metrics.institution_change == null ? "—" : `${row.holder_metrics.institution_change > 0 ? "+" : ""}${fmt(row.holder_metrics.institution_change)}pct`} />
        <Metric label="季度Flow得分" value={fmt(row.scale_metrics.quarterly_flow_score, 0)} />
      </div>
    </div>
  </div>;
}

function ScoreGroup({ title, labels, values }: { title: string; labels: Record<string, string>; values: Record<string, number> }) {
  return <div className="rounded-xl border border-border/60 bg-black/10 p-3"><div className="mb-2 text-xs font-semibold">{title}</div><div className="space-y-2">{Object.entries(values).map(([key, value]) => <div key={key} className="grid grid-cols-[72px_1fr_36px] items-center gap-2 text-xs"><span className="text-muted-foreground">{labels[key] || key}</span><div className="h-1.5 overflow-hidden rounded-full bg-muted"><div className={cn("h-full", value === 50 ? "bg-muted-foreground/40" : "bg-primary")} style={{ width: `${value}%` }} /></div><span className="text-right font-mono">{fmt(value, 0)}</span></div>)}</div></div>;
}

function ListCard({ title, items, warning }: { title: string; items: string[]; warning?: boolean }) {
  return <div className={cn("rounded-xl border p-3", warning ? "border-warning/25 bg-warning/5" : "border-border/60 bg-black/10")}><div className="mb-2 text-xs font-semibold">{title}</div><ul className="space-y-1 text-xs leading-5 text-muted-foreground">{items.map((item) => <li key={item}>• {item}</li>)}</ul></div>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="rounded-xl bg-black/20 px-3 py-2"><div className="text-[10px] text-muted-foreground">{label}</div><div className="mt-1 text-xs font-semibold">{value}</div></div>; }
function Stat({ label, value }: { label: string; value: number | string | undefined }) { return <div className="rounded-xl border border-border/50 bg-black/15 px-3 py-2"><div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div><div className="mt-1 font-mono text-lg font-bold">{value ?? "—"}</div></div>; }
function pct(value: number | null | undefined) { return value == null ? "—" : `${value > 0 ? "+" : ""}${fmt(value)}%`; }
