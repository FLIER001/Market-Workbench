// 研究列表 + 研究头：名称、基线、最近实验/结论、留出期状态；操作为继续研究 / 归档。
import { useEffect, useRef, useState } from "react";
import { loadToken } from "@/lib/auth";
import { Download, Plus, Sigma } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { factorLab, holdoutText, isActiveStatus, RUN_STATUS_LABEL, type FactorLabStatus, type Run, type Study } from "@/lib/factorLab";

// 列表接口只含研究元信息；按需读详情，不预加载所有完整报告。
function StudySummary({ study }: { study: Study }) {
  const [summary, setSummary] = useState<{ baseline: string; latest: string }>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const load = async () => {
    if (busy || summary) return;
    const session = loadToken();
    setBusy(true); setError("");
    try {
      const detail = await factorLab.studyDetail(study.id);
      if (!alive.current || session !== loadToken()) return;
      const latest = detail.runs.filter((run) => run.kind === "evaluate").sort((a, b) => b.created_at - a.created_at)[0];
      setSummary({ baseline: detail.runs.find((run) => run.id === detail.study.baseline_id)?.name ?? "未设置",
        latest: latest ? `${latest.name} · ${RUN_STATUS_LABEL[latest.status]}` : "无实验" });
    } catch (e) {
      if (alive.current && session === loadToken()) setError(e instanceof Error ? e.message : "读取失败");
    } finally { if (alive.current && session === loadToken()) setBusy(false); }
  };
  return <details onToggle={(e) => { if (e.currentTarget.open) load(); }}>
    <summary className="cursor-pointer">{study.baseline_id ? "已设基线" : "未设基线"} · 查看</summary>
    {busy && <p>读取中…</p>}
    {summary && <div className="mt-1"><p>基线：{summary.baseline}</p><p>最近：{summary.latest}</p></div>}
    {error && <p role="alert" className="text-danger">{error}<button onClick={load} className="ml-2 underline">重试</button></p>}
  </details>;
}

export function StudyList({ studies, busy, onOpen, onCreate, onCopy, onArchive }: {
  studies: Study[]; busy: boolean; onOpen: (id: string) => void;
  onCreate: () => void; onCopy: (study: Study) => void; onArchive: (study: Study, archived: boolean) => void;
}) {
  const row = (item: Study) => {
    const latest = item.decisions[item.decisions.length - 1];
    return (
      <tr key={item.id} className={`border-b border-border/30 ${item.archived ? "opacity-60" : ""}`}>
        <td className="py-2">
          <button onClick={() => onOpen(item.id)} className="text-left font-medium text-primary hover:underline">{item.name}</button>
          {item.description && <span className="mt-0.5 block text-[11px] text-muted-foreground">{item.description}</span>}
        </td>
        <td className="text-xs text-muted-foreground">{item.input_manifest?.data_version ?? "—"}</td>
        <td className="text-xs text-muted-foreground"><StudySummary study={item} /></td>
        <td className="whitespace-nowrap text-xs text-muted-foreground">{item.config.development.start} ~ {item.config.development.end}</td>
        <td className="whitespace-nowrap text-xs text-muted-foreground">{item.config.horizon} 日 · 上市 ≥ {item.config.min_days_listed} 天</td>
        <td className="text-xs">
          {latest
            ? <><span>{latest.decision}</span><span className="mt-0.5 block max-w-[220px] truncate text-[10px] text-muted-foreground" title={latest.note}>{latest.note}</span></>
            : <span className="text-muted-foreground">未记录</span>}
        </td>
        <td className="whitespace-nowrap text-xs text-muted-foreground">{holdoutText(item)}</td>
        <td className="whitespace-nowrap py-2 text-right text-xs">
          <button onClick={() => onOpen(item.id)} className="mr-3 text-primary hover:underline">继续研究</button>
          <button onClick={() => onCopy(item)} disabled={busy} className="mr-3 text-muted-foreground hover:text-foreground disabled:opacity-50">复制</button>
          <button onClick={() => onArchive(item, !item.archived)} disabled={busy} className="text-muted-foreground hover:text-foreground disabled:opacity-50">
            {item.archived ? "恢复" : "归档"}
          </button>
        </td>
      </tr>
    );
  };
  return (
    <GlassCard>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold"><Sigma className="h-4 w-4 text-primary" />研究列表</h2>
        <button onClick={onCreate} className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground">
          <Plus className="h-3.5 w-3.5" />新建研究
        </button>
      </div>
      {studies.length === 0 ? (
        <p className="py-6 text-center text-xs text-muted-foreground">还没有研究。新建研究后即可创建实验、对比与记录结论。</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr>
                <th className="py-2 text-left">研究</th><th className="text-left">数据版本</th><th className="text-left">基线 / 最近实验</th><th className="text-left">开发期</th>
                <th className="text-left">预测周期</th><th className="text-left">最新结论</th><th className="text-left">留出期</th><th />
              </tr>
            </thead>
            <tbody>
              {studies.filter((item) => !item.archived).map(row)}
              {studies.filter((item) => item.archived).map(row)}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-[11px] text-muted-foreground">研究数据按账号隔离；归档不删除实验记录与结论，可随时恢复。</p>
    </GlassCard>
  );
}

export function StudyHeader({ study, runs, status, onExport }: {
  study: Study; runs: Run[]; status: FactorLabStatus | undefined; onExport: () => void;
}) {
  const active = runs.filter((run) => isActiveStatus(run.status)).length;
  const failed = runs.filter((run) => run.status === "failed").length;
  const baseline = runs.find((run) => run.id === study.baseline_id);
  return (
    <GlassCard>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{study.name}</h2>
          {study.description && <p className="mt-1 text-xs text-muted-foreground">{study.description}</p>}
        </div>
        <button onClick={onExport} className="flex items-center gap-1.5 rounded-lg border border-border/60 px-3 py-2 text-xs">
          <Download className="h-3.5 w-3.5" />导出
        </button>
      </div>
      <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-[11px] text-muted-foreground">
        <span>数据 {study.input_manifest?.data_version ?? "—"}{status?.has_data ? "" : "（数据未构建）"}</span>
        <span>开发 {study.config.development.start} ~ {study.config.development.end}</span>
        <span>验证 {study.config.validation.start} ~ {study.config.validation.end}</span>
        <span>留出 {study.config.holdout.start} ~ {study.config.holdout.end}</span>
        <span>预测周期 {study.config.horizon} 日 · 分 5 组 · 上市 ≥ {study.config.min_days_listed} 天</span>
        <span>基线 {baseline ? baseline.name : "未设置"}</span>
        <span className={study.holdout_used_at != null ? "text-warning" : ""}>{holdoutText(study)}</span>
        <span>{active > 0 ? `${active} 个任务运行中` : "无运行中任务"}{failed > 0 ? ` · ${failed} 个失败` : ""}</span>
      </div>
    </GlassCard>
  );
}
