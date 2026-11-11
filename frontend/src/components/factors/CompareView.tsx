import { GitCompare, Lock, NotebookPen } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import {
  MAX_COMPARE, PHASE_LABEL, alignICSeries, commonStats, factorCoverage, isActiveStatus, metricDelta, nativeStats,
  type AnalysisResult, type HorizonKey, type HorizonStats, type Phase, type Run, type Study,
} from "@/lib/factorLab";
import {
  AllHorizonTable, CorrelationMatrix, HorizonTabs, QuantileChart, SampleAudit, StatusTag,
  TurnoverPanel, YearTable, useChart, fmt, fmtInt, fmtPct,
} from "@/components/factors/shared";
import { cssColor } from "@/lib/chartColor";

const SERIES_COLORS = ["--primary", "--warning", "--success", "--danger"] as const;
interface Props {
  study: Study; runs: Run[]; candidates: Run[]; picks: Run[]; busy: boolean; phase: Phase; onPhase: (phase: Phase) => void;
  compareRuns: Run[]; compareRun: Run | null; result: AnalysisResult | null;
  onSelectCompare: (id: string) => void; onToggle: (id: string) => void;
  onCompare: () => void; onFreeze: () => void; onDecision: () => void;
  onCancel: (run: Run) => void; onRetry: (run: Run) => void;
  horizon: HorizonKey; onHorizon: (h: HorizonKey) => void;
  sampleMode: "native" | "common"; onSampleMode: (mode: "native" | "common") => void;
}

function MetricsTable({ names, stats, baselineIndex = -1, native }: {
  names: string[]; stats: Array<HorizonStats | null>; baselineIndex?: number; native?: Array<HorizonStats | null>;
}) {
  const rows: Array<[string, (s: HorizonStats | null) => string]> = [
    ["RankIC", (s) => fmt(s?.ic.rank_ic_mean)], ["IC IR", (s) => fmt(s?.ic.rank_ic_ir)],
    ["正 IC 比例", (s) => fmtPct(s?.ic.rank_ic_positive_ratio)],
    ["Q5 换手", (s) => fmtPct(s?.quantile_turnover.Q5)],
    ["日均样本（只）", (s) => fmt(s?.avg_coverage, 1)],
    ["IC 使用行", (s) => fmtInt(s?.sample.used_rows)], ["天数", (s) => fmtInt(s?.n_days)],
  ];
  return <div className="overflow-x-auto"><table className="w-full text-xs tabular-nums">
    <thead className="border-b border-border/60 text-muted-foreground"><tr>
      <th className="py-2 text-left">指标</th>
      {names.map((name, i) => <th key={i} className="max-w-56 px-2 text-right break-all">{name}</th>)}
    </tr></thead>
    <tbody>
      {rows.map(([label, pick]) => <tr key={label} className="border-b border-border/30">
        <td className="py-1.5">{label}</td>{stats.map((s, i) => <td key={i} className="px-2 text-right">{pick(s)}</td>)}
      </tr>)}
      <tr className="border-b border-border/30"><td className="py-1.5" title="各因子自身有效记录占比，不以共同样本代替">因子覆盖率</td>
        {(native ?? stats).map((s, i) => <td key={i} className="px-2 text-right">{fmtPct(factorCoverage(s))}</td>)}
      </tr>
      {baselineIndex >= 0 && <tr><td className="py-1.5">基线差值（RankIC）</td>
        {stats.map((s, i) => <td key={i} className="px-2 text-right">{fmt(metricDelta(s?.ic.rank_ic_mean, stats[baselineIndex]?.ic.rank_ic_mean))}</td>)}
      </tr>}
    </tbody>
  </table></div>;
}

export function CompareView({
  study, runs, candidates, picks, busy, phase, onPhase, compareRuns, compareRun, result,
  onSelectCompare, onToggle, onCompare, onFreeze, onDecision, onCancel, onRetry,
  horizon, onHorizon, sampleMode, onSampleMode,
}: Props) {
  // 结果顺序以服务端任务的来源为准，不能用当前勾选顺序给旧报告改标签。
  const names = result?.native.map((item, i) => {
    const source = runs.find((run) => run.id === compareRun?.source_run_ids[i]);
    return `${i + 1}. ${source?.name ?? item.definition.factor ?? item.definition.expr ?? "因子"}`;
  }) ?? [];
  const useCommon = sampleMode === "common";
  const stats = names.map((_, i) => useCommon ? commonStats(result, horizon, i) : nativeStats(result, horizon, i));
  const ownStats = names.map((_, i) => nativeStats(result, horizon, i));
  const baselineIndex = useCommon && study.baseline_id ? compareRun?.source_run_ids.indexOf(study.baseline_id) ?? -1 : -1;
  const series = alignICSeries(stats);
  const chart = useChart(result ? {
    grid: { left: 48, right: 12, top: 28, bottom: 28 }, tooltip: { trigger: "axis" },
    legend: { top: 0, textStyle: { fontSize: 10 }, data: names },
    xAxis: { type: "category", data: series.dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: names.map((name, i) => ({ type: "line", name, showSymbol: false, connectNulls: false,
      lineStyle: { width: 1.4, color: cssColor(SERIES_COLORS[i % SERIES_COLORS.length]) }, data: series.values[i] })),
  } : null, "h-72");

  return <div className="space-y-4">
    <GlassCard>
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex rounded-lg border border-border/60 p-0.5">
          {(["development", "validation", "holdout"] as Phase[]).map((item) =>
            <button key={item} aria-pressed={phase === item} onClick={() => onPhase(item)}
              className={`rounded px-2.5 py-1 ${phase === item ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}>{PHASE_LABEL[item]}</button>)}
        </div>
        <span>已选 {picks.length} / {MAX_COMPARE}</span>
        <button onClick={onCompare} disabled={busy || picks.length < 2} className="flex items-center gap-1 rounded-lg bg-primary/20 px-3 py-2 text-primary disabled:opacity-50"><GitCompare className="h-3.5 w-3.5" />对比选中</button>
        <button onClick={onFreeze} disabled={busy || !picks.length || phase !== "development" || !study.baseline_id}
          className="flex items-center gap-1 rounded-lg border border-border/60 px-3 py-2 disabled:opacity-50"><Lock className="h-3.5 w-3.5" />锁定选中实验</button>
        <button onClick={onDecision} className="flex items-center gap-1 rounded-lg border border-border/60 px-3 py-2"><NotebookPen className="h-3.5 w-3.5" />记录结论</button>
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        {candidates.length ? candidates.map((run) => <label key={run.id} className="flex items-center gap-2 rounded-lg border border-border/60 px-2 py-1.5">
          <input type="checkbox" checked={picks.some((r) => r.id === run.id)} disabled={!picks.some((r) => r.id === run.id) && picks.length >= MAX_COMPARE}
            onChange={() => onToggle(run.id)} />{run.name}{run.id === study.baseline_id ? "（基线）" : ""}
        </label>) : <p className="text-muted-foreground">无同阶段、同数据版本的已完成实验。</p>}
      </div>
      <HorizonTabs value={horizon} onChange={onHorizon} />
    </GlassCard>

    {picks.length > 0 && (!result || picks.length !== compareRun?.source_run_ids.length
      || picks.some((run) => !compareRun?.source_run_ids.includes(run.id))) && <details className="glass p-4">
      <summary className="cursor-pointer text-xs font-medium">选中实验 · 各自样本</summary>
      <MetricsTable names={picks.map((run) => run.name)} stats={picks.map((run) => nativeStats(run.result as AnalysisResult, horizon, 0))} />
    </details>}

    <details className="glass p-4" open={!!compareRun && compareRun.status !== "completed"}>
      <summary className="cursor-pointer text-xs font-medium">对比记录 · {compareRuns.length} 条</summary>
      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        {compareRuns.map((run) => <button key={run.id} onClick={() => onSelectCompare(run.id)} aria-pressed={run.id === compareRun?.id}
          className={`flex items-center gap-2 rounded-lg border px-2 py-1.5 ${run.id === compareRun?.id ? "border-primary bg-primary/5" : "border-border/60"}`}>
          <StatusTag status={run.status} />{run.name} · {run.source_run_ids.length} 个实验
        </button>)}
        {!compareRuns.length && <span className="text-muted-foreground">尚未运行对比。</span>}
      </div>
      {compareRun && <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        <StatusTag status={compareRun.status} />
        {compareRun.error && <span role="alert" className="text-danger">{compareRun.error}</span>}
        {isActiveStatus(compareRun.status) && <button onClick={() => onCancel(compareRun)} disabled={busy} className="text-danger">取消</button>}
        {(compareRun.status === "failed" || compareRun.status === "cancelled") && <button onClick={() => onRetry(compareRun)} disabled={busy} className="text-primary">重试</button>}
      </div>}
    </details>

    {result && <>
      <GlassCard>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 text-xs">
          <h3 className="text-sm font-semibold">{useCommon ? "共同样本" : "各自样本"} · {horizon} 日</h3>
          <div className="flex rounded-lg border border-border/60 p-0.5">
            {(["common", "native"] as const).map((mode) => <button key={mode} onClick={() => onSampleMode(mode)} aria-pressed={sampleMode === mode}
              className={`rounded px-2.5 py-1 ${sampleMode === mode ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}>{mode === "common" ? "共同样本" : "各自样本"}</button>)}
          </div>
        </div>
        <MetricsTable names={names} stats={stats} baselineIndex={baselineIndex} native={ownStats} />
        <p className="mt-2 text-xs text-muted-foreground">共同配对行 {fmtInt(result.common?.by_horizon[horizon]?.paired_rows)} · {result.start} ~ {result.end}</p>
        {baselineIndex < 0 && <p className="mt-2 text-xs text-muted-foreground">未包含基线或未使用共同样本，不显示基线差值。</p>}
        {useCommon && !result.common?.by_horizon[horizon] && <p className="mt-2 text-xs text-warning">共同样本未返回，不用各自样本替代。</p>}
      </GlassCard>
      <GlassCard>
        <h3 className="mb-3 text-sm font-semibold">RankIC 走势 · {useCommon ? "共同样本" : "各自样本"}</h3>
        <div ref={chart.boxRef} role="img" aria-label="逐日 RankIC；数值见导出数据" className={`${chart.height} w-full`} />
      </GlassCard>
      {result.correlation ? <CorrelationMatrix correlation={result.correlation} names={names} />
        : <GlassCard><p className="text-xs text-muted-foreground">因子值相关性未计算，不显示替代数字。</p></GlassCard>}
      {result.native.map((item, i) => <details key={i} className="glass p-4">
        <summary className="cursor-pointer text-sm font-medium">{names[i]} · 分组与样本</summary>
        <p className="my-3 break-all font-mono text-xs">{item.definition.factor ?? item.definition.expr} · {item.definition.direction === "low" ? "低值优先" : "高值优先"}</p>
        {stats[i] ? <div className="grid gap-3 lg:grid-cols-2">
          <QuantileChart stats={stats[i]} horizon={horizon} title="分组收益（bp）" />
          <SampleAudit stats={stats[i]} /><TurnoverPanel stats={stats[i]} /><YearTable stats={stats[i]} />
        </div> : <p className="text-xs text-muted-foreground">该周期无可用证据。</p>}
        <AllHorizonTable byHorizon={useCommon
          ? Object.fromEntries(Object.entries(result.common?.by_horizon ?? {}).map(([h, entry]) => [h, entry.results[i]]))
          : item.by_horizon} title={`${names[i]} · 各周期`} />
      </details>)}
    </>}
    <details className="glass p-4 text-xs text-muted-foreground">
      <summary className="cursor-pointer font-medium">对比说明</summary>
      <p className="mt-2">数据不同的实验先重跑。共同样本由服务端按同一批股票与日期重算；各自覆盖不同，不直接排名。因子相关性不代表增量收益。</p>
      {result && <><p className="mt-2">{result.timing_note}</p><ul className="mt-2 space-y-1">{result.biases.map((bias) => <li key={bias}>{bias}</li>)}</ul></>}
    </details>
  </div>;
}
