// 研究结论：决定（继续验证 / 保留候选 / 暂缓 / 淘汰）+ 备注，追加不覆盖。
// 验证期与留出期在这里发起；留出期查看不可撤回，入口走确认弹层。
import { Archive, Lock, NotebookPen } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { holdoutText, latestDecision, type DecisionKind, type Run, type Study } from "@/lib/factorLab";
import { inputClass } from "@/components/factors/shared";
import { RunRefList } from "@/components/factors/StudyPanels";

const DECISIONS: DecisionKind[] = ["继续验证", "保留候选", "暂缓", "淘汰"];

export function DecisionView({ study, runs, frozenRuns, busy, dataReady, draft, onDraft, onSave, onHoldout, onValidate, onCompare, sourceId, onSource, onOpen, evidenceIds }: {
  study: Study; runs: Run[]; frozenRuns: Run[]; busy: boolean; dataReady: boolean;
  sourceId: string; onSource: (id: string) => void; onOpen: (id: string) => void; evidenceIds: string[];
  draft: { decision: DecisionKind; note: string }; onDraft: (value: { decision: DecisionKind; note: string }) => void;
  onSave: () => void; onHoldout: () => void; onValidate: () => void; onCompare: () => void;
}) {
  const holdoutRuns = runs.filter((run) => run.phase === "holdout");
  const validationRuns = runs.filter((run) => run.phase === "validation");
  const latest = latestDecision(study);
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-4">
        <GlassCard>
          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold"><NotebookPen className="h-4 w-4 text-primary" />研究结论</h3>
          <label className="mb-2 block space-y-1.5 text-xs">
            <span className="text-muted-foreground">决定</span>
            <select value={draft.decision} onChange={(e) => onDraft({ ...draft, decision: e.target.value as DecisionKind })} className={inputClass}>
              {DECISIONS.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <label className="block space-y-1.5 text-xs">
            <span className="text-muted-foreground">备注（原因和下一步）</span>
            <textarea rows={4} value={draft.note} onChange={(e) => onDraft({ ...draft, note: e.target.value })}
              placeholder="记录原因、样本限制与下一步；未验证的实验不写「通过」。" className={inputClass} />
          </label>
          <p className="mt-2 text-xs text-muted-foreground">关联实验：{evidenceIds.map((id) => runs.find((run) => run.id === id)?.name ?? id).join(" / ") || "未选择"}</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button onClick={onSave} disabled={busy || !draft.note.trim()}
              className="rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50">保存结论</button>
            <button onClick={onCompare} className="rounded-lg border border-border/60 px-3 py-2 text-xs">返回对比</button>
            <span className="text-[11px] text-muted-foreground">结论只追加、不覆盖；未计算项不标为通过。</span>
          </div>
        </GlassCard>

        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">结论记录</h3>
          {study.decisions.length === 0 ? <p className="text-xs text-muted-foreground">尚无结论记录。</p> : (
            <ul className="space-y-2">
              {[...study.decisions].reverse().map((item, index) => (
                <li key={`${item.created_at}-${index}`} className="rounded-lg border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{item.decision}</span>
                    <span className="text-[10px] text-muted-foreground">{new Date(item.created_at * 1000).toLocaleString()}</span>
                    {item.run_ids.length > 0 && <span className="text-[10px] text-muted-foreground">关联 {item.run_ids.length} 个实验</span>}
                  </p>
                  <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{item.note}</p>
                  <div className="mt-2 flex flex-wrap gap-2">{item.run_ids.map((id) => <button key={id} onClick={() => onOpen(id)} className="text-primary underline">{runs.find((run) => run.id === id)?.name ?? id}</button>)}</div>
                </li>
              ))}
            </ul>
          )}
          {latest && <p className="mt-2 text-[11px] text-muted-foreground">最新：{latest.decision}</p>}
        </GlassCard>
      </div>

      <div className="space-y-4">
        <GlassCard>
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold"><Lock className="h-4 w-4 text-primary" />已锁定实验</h3>
          {study.freezes.length === 0 ? <p className="text-xs text-muted-foreground">尚未锁定。在对比页点「锁定选中实验」。</p> : (
            <ul className="space-y-2 text-xs">
              {study.freezes.map((freeze) => (
                <li key={freeze.id} className="rounded-lg border border-border/60 bg-background/30 p-2">
                  <p className="text-[11px] text-muted-foreground">{new Date(freeze.created_at * 1000).toLocaleString()} · 编号 {freeze.id.slice(0, 8)}</p>
                  <ul className="mt-1 space-y-0.5">
                    {freeze.run_ids.map((rid) => (
                      <li key={rid}>{runs.find((item) => item.id === rid)?.name ?? rid}{rid === freeze.baseline_id ? "（基线）" : ""}</li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[11px] text-muted-foreground">锁定记录不可修改；后续修改另存为新实验。</p>
        </GlassCard>

        <GlassCard>
          <label className="mb-3 block space-y-1.5 text-xs">
            <span className="text-muted-foreground">验证 / 留出期来源</span>
            <select value={sourceId} onChange={(e) => onSource(e.target.value)} disabled={!frozenRuns.length || busy} className={inputClass}>
              {!frozenRuns.length && <option value="">请先锁定开发期实验</option>}
              {frozenRuns.map((run) => <option key={run.id} value={run.id}>{run.name}</option>)}
            </select>
          </label>
          <h3 className="mb-2 text-sm font-semibold">验证期</h3>
          <p className="text-xs text-muted-foreground">
            {validationRuns.length === 0 ? "未计算。验证期复用开发期选定版本，不能改口径。" : `${validationRuns.length} 个实验`}
          </p>
          <button onClick={onValidate} disabled={busy || !dataReady || !sourceId}
            className="mt-3 rounded-lg border border-border/60 px-3 py-2 text-xs disabled:opacity-50">运行验证期</button>
          {validationRuns.length > 0 && <div className="mt-3"><RunRefList runs={validationRuns} onOpen={onOpen} /></div>}
        </GlassCard>

        <GlassCard>
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold"><Archive className="h-4 w-4 text-primary" />留出期</h3>
          <p className="text-xs text-muted-foreground">{holdoutText(study)}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {frozenRuns.length === 0 ? "先锁定实验，再查看留出期。" : `已锁定 ${frozenRuns.length} 个实验，可从中选一个查看留出期。`}
          </p>
          <button onClick={onHoldout} disabled={busy || frozenRuns.length === 0 || !dataReady}
            className="mt-3 rounded-lg border border-warning/40 px-3 py-2 text-xs text-warning disabled:opacity-50">查看留出期</button>
          <p className="mt-2 text-[11px] text-muted-foreground">查看即记录，不能撤回；留出期通过也不代表实盘可用。</p>
          {holdoutRuns.length > 0 && <div className="mt-3"><RunRefList runs={holdoutRuns} onOpen={onOpen} /></div>}
          {study.holdout_used_at != null && (
            <p className="mt-2 text-[11px] text-warning">留出期已使用：{new Date(study.holdout_used_at * 1000).toLocaleString()}</p>
          )}
        </GlassCard>

        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">回测记录</h3>
          <p className="text-xs text-muted-foreground">回测在实验详情页对单个已完成实验发起，用于验证选定组合，不替代因子检验。</p>
          <div className="mt-3">
            <RunRefList runs={runs.filter((run) => run.kind === "backtest")} onOpen={onOpen} />
          </div>
        </GlassCard>
      </div>
    </div>
  );
}
