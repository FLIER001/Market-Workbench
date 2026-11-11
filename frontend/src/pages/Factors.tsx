import { useState } from "react";
import { FactorBuilder } from "@/components/factors/FactorBuilder";
import { NationalTeamPanel } from "@/components/factors/NationalTeamPanel";
import { definitionText, type Definition } from "@/lib/factorLab";
import { AlertTriangle, ChevronLeft, Loader2, Plus } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { RefreshButton } from "@/components/ui/RefreshButton";
import { GlassCard } from "@/components/ui/GlassCard";
import { useFactorResearch } from "@/components/factors/useFactorResearch";
import {
  BiasNotes, ConfirmDialog, DataStatusPanel, Modal, StudyFormDialog,
} from "@/components/factors/StudyPanels";
import { StudyHeader, StudyList } from "@/components/factors/StudyListView";
import { RunDetail, RunTable } from "@/components/factors/RunViews";
import { CompareView } from "@/components/factors/CompareView";
import { DecisionView } from "@/components/factors/DecisionView";
import { inputClass } from "@/components/factors/shared";

// 因子研究 V2：研究（账号归属）→ 实验记录（服务端持久化任务）→ 对比（共同样本重算）→ 结论/锁定/留出期。
// 不做本地结果缓存；切研究、切账号、卸载都会使在途请求失效并清空页面状态。
export function Factors() {
  const r = useFactorResearch();
  const [dirty, setDirty] = useState(false);
  const canLeave = () => !dirty || window.confirm("当前草稿尚未运行，切换研究将丢失修改。继续？");
  const [draft, setDraft] = useState<Definition | null>(null);
  const detailRun = r.runs.find((run) => run.id === r.detailRunId) ?? null;
  const freezeIds = r.view === "runs" && detailRun ? [detailRun.id] : r.picks.map((run) => run.id);
  const frozenCount = new Set([...freezeIds, ...(r.study?.baseline_id ? [r.study.baseline_id] : [])]).size;
  const building = r.building;
  const openRun = (id: string) => {
    const run = r.runs.find((item) => item.id === id);
    if (run?.kind === "compare") {
      r.setComparePhase(run.phase); r.setCompareRunId(id); r.setView("compare");
    } else { r.setDetailRunId(id); r.setView("runs"); }
  };

  return (
    <div className="mx-auto max-w-7xl space-y-5 p-4 md:p-6">
      <PageHeader
        title="因子研究"
        actions={
          <>
            {r.view !== "studies" && (
              <button onClick={() => { if (canLeave()) r.closeStudy(); }} className="flex items-center gap-1.5 rounded-lg border border-border/60 px-3 py-2 text-xs">
                <ChevronLeft className="h-3.5 w-3.5" />研究列表
              </button>
            )}
            <RefreshButton onClick={r.refresh} />
          </>
        }
      />

      {r.error && (
        <div role="alert" className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger/5 p-3 text-xs text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="flex-1">{r.error}</span>
          <button onClick={() => r.setError(null)} className="underline">关闭</button>
        </div>
      )}
      {r.view === "studies" && <NationalTeamPanel />}
      {r.notice && (
        <div role="status" className="flex items-center gap-2 rounded-xl border border-primary/30 bg-primary/5 p-3 text-xs text-primary">
          <span className="flex-1">{r.notice}</span>
          <button onClick={() => r.setNotice(null)} className="underline">关闭</button>
        </div>
      )}

      {!r.dataReady && <DataStatusPanel status={r.status} onBuild={r.buildData} />}
      {r.dataReady && (
        <details className="rounded-xl border border-border/60 bg-card/30">
          <summary className="cursor-pointer px-4 py-2.5 text-xs font-medium">
            数据：{r.status?.catalog?.stocks ?? "—"} 只 · {r.status?.catalog?.date_min ?? "—"} ~ {r.status?.catalog?.date_max ?? "—"}{building ? " · 构建中" : ""}
          </summary>
          <div className="space-y-3 px-3 pb-3"><DataStatusPanel status={r.status} onBuild={r.buildData} /></div>
        </details>
      )}
      <BiasNotes status={r.status} />

      {r.loading && !r.studies.length && !r.study && (
        <GlassCard>
          <div role="status" className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />正在读取研究…
          </div>
        </GlassCard>
      )}

      {r.detailLoading && !r.study && (
        <GlassCard>
          <div role="status" className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />正在读取研究详情与实验记录…
          </div>
        </GlassCard>
      )}

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <FactorBuilder key={r.builderKey} r={r} onDefinition={setDraft} onDirty={setDirty} />
        <div className="min-w-0 space-y-4">
      {r.view === "studies" && !r.loading && (
        <StudyList studies={r.studies} busy={r.busy} onOpen={(id) => { if (canLeave()) r.openStudy(id); }} onArchive={r.archiveStudy}
          onCreate={() => r.setDialog({ kind: "study", mode: "create", source: null })}
          onCopy={(source) => r.setDialog({ kind: "study", mode: "copy", source })} />
      )}

      {r.study && r.view !== "studies" && (
        <>
          <StudyHeader study={r.study} runs={r.runs} status={r.status} onExport={r.exportStudy} />
          <nav aria-label="研究步骤" className="grid grid-cols-2 gap-1 rounded-xl border border-border/60 bg-background/30 p-1 sm:grid-cols-4">
            {(["runs", "compare", "decision"] as const).map((id) => (
              <button key={id} onClick={() => r.setView(id)} aria-pressed={r.view === id}
                className={`rounded-lg px-2 py-2.5 text-sm font-medium transition ${r.view === id ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-muted/40"}`}>
                {id === "runs" ? "版本记录" : id === "compare" ? "对比" : "研究结论"}
              </button>
            ))}
            <button onClick={() => r.openRunForm(null)} disabled={r.busy}
              className="flex items-center justify-center gap-1.5 rounded-lg bg-primary px-2 py-2.5 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50">
              <Plus className="h-3.5 w-3.5" />构建新版本
            </button>
          </nav>

          {r.view === "runs" && detailRun && <div className="rounded-lg border border-border/60 p-3 text-xs space-y-1">
            <p>报告版本：{detailRun.name} · {detailRun.phase === "development" ? "开发期" : detailRun.phase === "validation" ? "验证期" : "留出期"} · 未来 {r.study.config.horizon} 日</p>
            <code className="block break-all">{definitionText(detailRun.definition)} · {detailRun.definition?.direction === "low" ? "低值优先" : "高值优先"}</code>
            {draft && (definitionText(draft) !== definitionText(detailRun.definition) || draft.direction !== detailRun.definition?.direction) && <p role="status" className="text-warning">右侧为上次已运行版本，当前修改尚未检验。</p>}
          </div>}
          {r.view === "runs" && (detailRun ? (
            <RunDetail run={detailRun} study={r.study} horizon={r.horizon} onHorizon={r.setHorizon} busy={r.busy}
              runNames={r.runNames} isBaseline={r.study.baseline_id === detailRun.id}
              onBack={() => r.setDetailRunId(null)} onCancel={() => r.cancelRun(detailRun)}
              onRetry={() => r.retryRun(detailRun)} onBaseline={() => r.setBaseline(detailRun)}
              onBacktest={() => r.setDialog({ kind: "backtest", runId: detailRun.id })}
              onFreeze={() => r.setDialog({ kind: "confirm", action: "freeze" })}
              onCopy={() => r.openRunForm({
                name: `${detailRun.name} 副本`, definition: detailRun.definition, phase: "development",
                source_run_id: detailRun.source_run_ids[0],
              })} />
          ) : (
            <RunTable study={r.study} runs={r.runs} candidateIds={new Set(r.candidates.map((run) => run.id))}
              phase={r.comparePhase} onPhase={r.setComparePhase}
              selected={r.selected} pickCount={r.picks.length} busy={r.busy} onSelect={r.toggleSelected}
              onOpen={openRun} onCancel={r.cancelRun} onRetry={r.retryRun} onBaseline={r.setBaseline}
              onCopy={(run) => r.openRunForm({ name: `${run.name} 副本`, definition: run.definition, phase: "development", source_run_id: run.source_run_ids[0] })}
              onCompare={() => r.startCompare(r.picks.map((run) => run.id))} />
          ))}

          {r.view === "runs" && detailRun && <details open className="min-w-0 space-y-3">
            <summary className="cursor-pointer text-sm font-medium">版本记录与共同样本比较</summary>
            <RunTable study={r.study} runs={r.runs} candidateIds={new Set(r.candidates.map((run) => run.id))}
              phase={r.comparePhase} onPhase={r.setComparePhase} selected={r.selected} pickCount={r.picks.length}
              busy={r.busy} onSelect={r.toggleSelected} onOpen={openRun} onCancel={r.cancelRun} onRetry={r.retryRun}
              onBaseline={r.setBaseline} onCopy={(run) => r.openRunForm({ name: `${run.name} 副本`, definition: run.definition, phase: "development" })}
              onCompare={() => r.startCompare(r.picks.map((run) => run.id))} />
          </details>}

          {r.view === "compare" && (
            <CompareView study={r.study} runs={r.runs} candidates={r.candidates} picks={r.picks} busy={r.busy} phase={r.comparePhase}
              onPhase={(phase) => { r.setComparePhase(phase); r.setCompareRunId(null); }}
              compareRuns={r.compareRuns} compareRun={r.compareRun} result={r.compareResult}
              onSelectCompare={r.setCompareRunId} onToggle={r.toggleSelected}
              onCompare={() => r.startCompare(r.picks.map((run) => run.id))}
              onFreeze={() => r.setDialog({ kind: "confirm", action: "freeze" })} onDecision={() => r.setView("decision")}
              onCancel={r.cancelRun} onRetry={r.retryRun} horizon={r.horizon} onHorizon={r.setHorizon}
              sampleMode={r.sampleMode} onSampleMode={r.setSampleMode} />
          )}

          {r.view === "decision" && (
            <DecisionView study={r.study} runs={r.runs} frozenRuns={r.frozenRuns} busy={r.busy} dataReady={r.dataReady}
              sourceId={r.phaseSource?.id ?? ""} onSource={r.setPhaseSourceId} onOpen={openRun}
              evidenceIds={r.evidenceIds}
              draft={r.decisionDraft} onDraft={r.setDecisionDraft} onSave={r.saveDecision}
              onHoldout={() => r.setDialog({ kind: "confirm", action: "holdout" })} onValidate={r.startValidation}
              onCompare={() => r.setView("compare")} />
          )}
        </>
      )}

      {r.view !== "studies" && !r.study && !r.loading && !r.detailLoading && (
        <GlassCard><p className="py-8 text-center text-sm text-muted-foreground">研究不存在或已不可访问，请返回研究列表。</p></GlassCard>
      )}

        </div>
      </div>

      {r.dialog?.kind === "study" && <StudyFormDialog open onClose={() => r.setDialog(null)} status={r.status}
        title={r.dialog?.kind === "study" && r.dialog.mode === "copy" ? "复制研究" : "新建研究"}
        initial={r.dialog?.kind === "study" && r.dialog.mode === "copy" && r.dialog.source
          ? { name: `${r.dialog.source.name} 副本`, description: r.dialog.source.description, config: r.dialog.source.config }
          : null}
        onSubmit={r.createStudy} />}

      <ConfirmDialog open={r.dialog?.kind === "confirm" && r.dialog.action === "freeze"} onClose={() => r.setDialog(null)}
        title="锁定选中实验" confirmLabel="锁定" busy={r.busy} onConfirm={() => r.confirmFreeze(freezeIds)}>
        <p>锁定 {frozenCount} 个实验（含基线）。修改将另存新实验。</p>
        {!r.study?.baseline_id && <p>请先设置基线。</p>}
        {freezeIds.length === 0 && <p>请先选择实验。</p>}
      </ConfirmDialog>

      <ConfirmDialog open={r.dialog?.kind === "confirm" && r.dialog.action === "holdout"} onClose={() => r.setDialog(null)}
        title="查看留出期" confirmLabel="查看留出期" danger busy={r.busy} onConfirm={r.confirmHoldout}>
        <p className="font-medium text-warning">开始验证后将标记「留出期已使用」，无法撤回。</p>
        <p>来源：{r.dialog?.kind === "confirm" && r.dialog.retry ? `重试「${r.dialog.retry.name}」` : r.phaseSource?.name ?? "尚未锁定开发期实验"}</p>
      </ConfirmDialog>

      {r.dialog?.kind === "backtest" && (
        <Modal open onClose={() => { if (!r.busy) r.setDialog(null); }} title="组合回测" titleId="backtest-form" size="max-w-md">
          <div className="space-y-3 text-xs">
            <p className="text-muted-foreground">沿用当前实验的因子、方向与区间。</p>
            <label className="block space-y-1.5"><span className="text-muted-foreground">Top N 等权</span>
              <input type="number" min={1} max={500} value={Number.isFinite(r.backtestParams.top_n) ? r.backtestParams.top_n : ""}
                onChange={(e) => r.setBacktestParams({ ...r.backtestParams, top_n: e.target.valueAsNumber })} className={inputClass} />
            </label>
            <label className="block space-y-1.5"><span className="text-muted-foreground">调仓频率</span>
              <select value={r.backtestParams.freq} onChange={(e) => r.setBacktestParams({ ...r.backtestParams, freq: e.target.value })} className={inputClass}>
                <option value="monthly">月末信号 / 次日开盘</option>
                <option value="weekly">周末信号 / 次日开盘</option>
              </select>
            </label>
            <label className="block space-y-1.5"><span className="text-muted-foreground">交易成本倍数</span>
              <select value={r.backtestParams.cost} onChange={(e) => r.setBacktestParams({ ...r.backtestParams, cost: Number(e.target.value) })} className={inputClass}>
                {[0, 1, 2, 3].map((c) => <option key={c} value={c}>{c}x{c === 1 ? " · 基准成本" : ""}</option>)}
              </select>
            </label>
            <div className="flex justify-end gap-2">
              <button onClick={() => r.setDialog(null)} disabled={r.busy} className="rounded-lg border border-border/60 px-3 py-2">取消</button>
              <button onClick={r.startBacktest} disabled={r.busy || !Number.isInteger(r.backtestParams.top_n) || r.backtestParams.top_n < 1 || r.backtestParams.top_n > 500}
                className="rounded-lg bg-primary px-3 py-2 font-medium text-primary-foreground disabled:opacity-50">运行回测</button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

export default Factors;
