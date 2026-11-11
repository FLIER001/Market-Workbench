// 实验记录表 + 实验详情。
// 表里的每个筛选/禁用条件都对应契约里的硬约束：同阶段、同数据版本、已完成才能对比。
import { ChevronLeft, Loader2, Lock, Table2 } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import {
  KIND_LABEL, MAX_COMPARE, PHASE_LABEL, factorCoverage, isActiveStatus, isFrozen, manifestMatches, nativeStats, summaryMetrics,
  type AnalysisResult, type HorizonKey, type HorizonStats, type Phase, type Run, type Study,
} from "@/lib/factorLab";
import {
  AllHorizonTable, HorizonMetrics, HorizonTabs, ICChart, PhaseTag, QuantileChart, SampleAudit, StatusTag,
  TurnoverPanel, YearTable, fmt, fmtInt, fmtPct,
} from "@/components/factors/shared";
import { BacktestPanel } from "@/components/factors/BacktestPanel";
import type { RunFormInitial } from "@/components/factors/useFactorResearch";

const statsOf = (run: Run, horizon: HorizonKey): HorizonStats | null =>
  run.kind === "evaluate" && run.status === "completed"
    ? nativeStats(run.result as AnalysisResult, horizon, 0) : null;

export function RunTable({ study, runs, candidateIds, selected, pickCount, busy, onSelect, onOpen, onCancel, onRetry, onBaseline, onCopy, onCompare, phase, onPhase }: {
  study: Study; runs: Run[]; candidateIds: Set<string>; selected: string[]; pickCount: number; busy: boolean;
  phase: Phase; onPhase: (phase: Phase) => void;
  onSelect: (id: string) => void; onOpen: (id: string) => void; onCancel: (run: Run) => void;
  onRetry: (run: Run) => void; onBaseline: (run: Run) => void; onCopy: (run: Run) => void; onCompare: () => void;
}) {
  const horizonKey = String(study.config.horizon) as HorizonKey;
  const rows = [...runs].sort((a, b) => b.created_at - a.created_at);
  return (
    <GlassCard>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold"><Table2 className="h-4 w-4 text-primary" />实验记录</h3>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <label className="text-muted-foreground">对比阶段 <select value={phase} onChange={(e) => onPhase(e.target.value as Phase)} className="rounded border border-border/60 bg-background px-2 py-1">
            {(["development", "validation", "holdout"] as Phase[]).map((p) => <option key={p} value={p}>{PHASE_LABEL[p]}</option>)}
          </select></label>
          <span className="text-muted-foreground">已选 {pickCount} / {MAX_COMPARE}</span>
          <button onClick={onCompare} disabled={busy || pickCount < 2}
            className="rounded-lg bg-primary/20 px-3 py-1.5 font-medium text-primary disabled:opacity-50">对比选中</button>
        </div>
      </div>
      {rows.length === 0 ? (
        <p className="py-6 text-center text-xs text-muted-foreground">还没有版本。在左侧选模板、调整参数并运行检验。</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-xs tabular-nums [&_th]:whitespace-nowrap">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr>
                <th className="py-2" /><th className="py-2 text-left">实验</th><th className="text-left">阶段</th><th className="text-left">状态</th>
                <th className="text-right">RankIC（{horizonKey} 日）</th><th className="text-right">IC IR</th>
                <th className="text-right">Q5 换手</th><th className="text-right" title="有因子值的记录占比">因子覆盖率</th><th className="text-right">样本天数</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((run) => {
                const stats = statsOf(run, horizonKey);
                const metrics = summaryMetrics(stats);
                const isBaseline = study.baseline_id === run.id;
                const selectable = candidateIds.has(run.id);
                const stale = !manifestMatches(run, study);
                const development = run.phase === "development";
                return (
                  <tr key={run.id} className={`border-b border-border/30 ${isBaseline ? "bg-primary/5" : ""}`}>
                    <td className="py-2 text-center">
                      <input type="checkbox" aria-label={`选择 ${run.name}`} checked={selected.includes(run.id)} disabled={!selectable || (!selected.includes(run.id) && pickCount >= MAX_COMPARE)}
                        onChange={() => onSelect(run.id)} />
                    </td>
                    <td className="py-2">
                      <button onClick={() => onOpen(run.id)} className="text-left font-medium text-primary hover:underline">{run.name}</button>
                      {isBaseline && <span className="ml-2 rounded bg-primary/15 px-1.5 py-0.5 text-[10px] text-primary">基线</span>}
                      {isFrozen(study, run.id) && <span className="ml-2 rounded bg-warning/15 px-1.5 py-0.5 text-[10px] text-warning">已锁定</span>}
                      <span className="mt-0.5 block max-w-[260px] truncate font-mono text-[10px] text-muted-foreground" title={run.definition?.factor ?? run.definition?.expr ?? ""}>
                        {run.definition ? `${run.definition.factor ?? run.definition.expr} · ${run.definition.direction === "low" ? "低值优先" : "高值优先"}` : KIND_LABEL[run.kind]}
                      </span>
                    </td>
                    <td className="whitespace-nowrap py-2"><PhaseTag phase={run.phase} /></td>
                    <td className="py-2">
                      <StatusTag status={run.status} />
                      {stale && <span className="ml-1 whitespace-nowrap rounded bg-warning/15 px-1.5 py-0.5 text-[10px] text-warning" title="输入数据版本不同，不能直接对比">数据不同</span>}
                      {run.status === "failed" && run.error && (
                        <span className="mt-0.5 block max-w-[200px] truncate text-[10px] text-danger" title={run.error}>{run.error}</span>
                      )}
                    </td>
                    <td className="text-right">{stats ? fmt(metrics.rank_ic) : "—"}</td>
                    <td className="text-right">{stats ? fmt(metrics.ic_ir) : "—"}</td>
                    <td className="text-right">{stats ? fmtPct(metrics.turnover_q5) : "—"}</td>
                    <td className="text-right">{fmtPct(factorCoverage(stats))}</td>
                    <td className="text-right">{stats ? fmtInt(metrics.n_days) : "—"}</td>
                    <td className="whitespace-nowrap py-2 text-right">
                      {isActiveStatus(run.status) && <button onClick={() => onCancel(run)} disabled={busy} className="text-danger hover:underline disabled:opacity-50">取消</button>}
                      {(run.status === "failed" || run.status === "cancelled") && (
                        <button onClick={() => onRetry(run)} disabled={busy} className="text-primary hover:underline disabled:opacity-50">重试</button>
                      )}
                      {run.kind === "evaluate" && run.definition && <button onClick={() => onCopy(run)} disabled={busy} className="ml-3 text-muted-foreground hover:text-foreground">继续修改</button>}
                      {run.kind === "evaluate" && run.status === "completed" && development && !stale && !isBaseline &&
                        <button onClick={() => onBaseline(run)} disabled={busy} className="ml-3 text-muted-foreground hover:text-foreground disabled:opacity-50">设为基线</button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <details className="mt-3 text-[11px] text-muted-foreground">
        <summary className="cursor-pointer">指标与对比说明</summary>
        <p className="mt-2">选择 2-{MAX_COMPARE} 个同阶段、同数据的已完成实验。Q5 换手不是交易换手。</p>
      </details>
    </GlassCard>
  );
}

export function RunDetail({ run, study, horizon, onHorizon, busy, runNames, onBack, onCopy, onCancel, onRetry, onBaseline, onBacktest, onFreeze, isBaseline }: {
  run: Run; study: Study; horizon: HorizonKey; onHorizon: (h: HorizonKey) => void; busy: boolean;
  runNames: Record<string, string>; onBack: () => void; onCopy: () => void; onCancel: () => void;
  onRetry: () => void; onBaseline: () => void; onBacktest: () => void; onFreeze: () => void; isBaseline: boolean;
}) {
  const result = run.status === "completed" && (run.kind === "evaluate" || run.kind === "compare")
    ? (run.result as AnalysisResult | null) : null;
  const native: HorizonStats | null = result ? nativeStats(result, horizon, 0) : null;
  const stale = !manifestMatches(run, study);
  const developmentEvaluate = run.kind === "evaluate" && run.status === "completed" && run.phase === "development" && !stale;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button onClick={onBack} className="flex items-center gap-1.5 rounded-lg border border-border/60 px-3 py-2 text-xs">
          <ChevronLeft className="h-3.5 w-3.5" />返回实验记录
        </button>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {isActiveStatus(run.status) && <button onClick={onCancel} disabled={busy} className="rounded-lg border border-danger/40 px-3 py-2 text-danger disabled:opacity-50">取消运行</button>}
          {(run.status === "failed" || run.status === "cancelled") && <button onClick={onRetry} disabled={busy} className="rounded-lg border border-border/60 px-3 py-2 disabled:opacity-50">重试</button>}
          {run.kind === "evaluate" && run.definition && <button onClick={onCopy} disabled={busy} className="rounded-lg border border-border/60 px-3 py-2 disabled:opacity-50">继续修改</button>}
          {run.kind === "evaluate" && run.status === "completed" && !stale && <button onClick={onBacktest} disabled={busy} className="rounded-lg border border-border/60 px-3 py-2 disabled:opacity-50">运行回测</button>}
          {developmentEvaluate && (
            <>
              {!isBaseline && <button onClick={onBaseline} disabled={busy} className="rounded-lg border border-border/60 px-3 py-2 disabled:opacity-50">设为基线</button>}
              <button onClick={onFreeze} disabled={busy || !study.baseline_id} className="flex items-center gap-1.5 rounded-lg border border-border/60 px-3 py-2 disabled:opacity-50">
                <Lock className="h-3.5 w-3.5" />锁定
              </button>
            </>
          )}
        </div>
      </div>

      <GlassCard>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex flex-wrap items-center gap-2 text-base font-semibold">
              {run.name} <PhaseTag phase={run.phase} /> <StatusTag status={run.status} />
              <span className="rounded bg-muted/40 px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">{KIND_LABEL[run.kind]}</span>
              {isBaseline && <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[10px] text-primary">基线</span>}
            </h2>
            <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
              {run.definition ? `${run.definition.factor ?? run.definition.expr} · ${run.definition.direction === "low" ? "低值优先" : "高值优先"}` : KIND_LABEL[run.kind]} ·
              数据 {run.input_manifest?.data_version ?? "—"}
            </p>
          </div>
          {result && <p className="text-[11px] text-muted-foreground">区间 {result.start} ~ {result.end} · {study.config.horizon} 日为主检验周期</p>}
        </div>
        {isActiveStatus(run.status) && (
          <p role="status" className="mt-3 flex items-center gap-2 text-xs text-primary">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {run.status === "queued" ? "排队中，服务端按空闲顺序执行" : "服务端运行中"}；离开页面不影响任务，回来自动刷新。
          </p>
        )}
        {run.status === "failed" && <p className="mt-3 rounded-lg border border-danger/30 bg-danger/5 p-2 text-xs text-danger">{run.error ?? "运行失败"}</p>}
        {run.status === "cancelled" && <p className="mt-3 text-xs text-muted-foreground">已取消，记录保留；重试会创建新实验。</p>}
        {stale && <p className="mt-3 text-xs text-warning">输入数据版本与当前研究不一致，不能与研究内其它实验直接对比排名。</p>}
        {run.source_run_ids.length > 0 && (
          <p className="mt-3 text-[11px] text-muted-foreground">
            来源实验：{run.source_run_ids.map((id) => runNames[id] ?? id).join(" / ")}
          </p>
        )}
      </GlassCard>

      {run.kind === "backtest" && run.status === "completed" && <BacktestPanel result={run.result} />}
      {run.kind === "backtest" && run.status !== "completed" && (
        <GlassCard><p className="text-xs text-muted-foreground">回测结果在任务完成后显示。</p></GlassCard>
      )}

      {result && native && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <HorizonTabs value={horizon} onChange={onHorizon} />
            <span className="text-[11px] text-muted-foreground">切换周期时 IC、分组收益与样本信息同步切换。</span>
          </div>
          <HorizonMetrics stats={native} />
          <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 360px), 1fr))" }}>
            <ICChart stats={native} title={`预测能力 · ${horizon} 日 RankIC`} />
            <QuantileChart stats={native} horizon={horizon} title="分组单调性 · 累计前瞻收益" />
          </div>
          <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 360px), 1fr))" }}>
            <SampleAudit stats={native} />
            <TurnoverPanel stats={native} />
          </div>
          <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 360px), 1fr))" }}>
            <YearTable stats={native} />
            <AllHorizonTable byHorizon={result.native[0]?.by_horizon ?? {}}
              title={`${result.native[0] ? (result.native[0].definition.factor ?? result.native[0].definition.expr ?? "因子") : "因子"} · 各周期`} />
          </div>
          <details className="glass p-4">
            <summary className="mb-2 cursor-pointer text-sm font-semibold">口径与限制</summary>
            <p className="text-[11px] leading-relaxed text-muted-foreground">{result.timing_note}</p>
            <ul className="mt-2 space-y-1 text-[11px] leading-relaxed text-muted-foreground">
              {result.biases.map((bias) => <li key={bias}>{bias}</li>)}
            </ul>
            <p className="mt-2 text-[11px] text-muted-foreground">未计算项显示「—」，不按 0 处理；同阶段对比需服务端重算共同样本，不把各自汇总相减。</p>
            <pre className="mt-3 overflow-auto text-[10px]">{JSON.stringify({ definition: run.definition, config: study.config, input_manifest: run.input_manifest, params: run.params }, null, 2)}</pre>
          </details>
        </>
      )}
      {result && !native && (
        <GlassCard><p className="text-xs text-muted-foreground">该周期无可用统计（样本不足或未返回）；未计算不按 0 展示，请切换周期查看。</p></GlassCard>
      )}
      {run.status === "completed" && !result && run.kind === "evaluate" && (
        <GlassCard><p className="text-xs text-muted-foreground">结果结构无法识别，请重试创建一个新实验。</p></GlassCard>
      )}
    </div>
  );
}

export type { RunFormInitial };
export type { Phase };
