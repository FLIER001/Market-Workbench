import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { loadToken } from "@/lib/auth";
import {
  MAX_COMPARE, RUN_POLL_MS, compareCandidates, factorLab, freezeCandidates, frozenRunIds,
  hasActiveJobs, retrySpec, selectedCandidates,
  type AnalysisResult, type DecisionKind, type Definition, type FactorFieldsDoc, type FactorLabStatus,
  type HorizonKey, type Phase, type Run, type Study, type StudyConfig,
} from "@/lib/factorLab";

const HASH_PREFIX = "#factor-study=";
export type View = "studies" | "runs" | "compare" | "decision";
export interface RunFormInitial {
  name: string; definition: Definition | null; phase: Phase; source_run_id?: string;
}
export type Dialog =
  | { kind: "study"; mode: "create" | "copy"; source: Study | null }
  | { kind: "run"; initial: RunFormInitial | null }
  | { kind: "confirm"; action: "freeze" | "holdout"; retry?: Run }
  | { kind: "backtest"; runId: string }
  | null;

export function useFactorResearch() {
  const [builderKey, setBuilderKey] = useState(0);
  const [status, setStatus] = useState<FactorLabStatus>();
  const [doc, setDoc] = useState<FactorFieldsDoc>();
  const [studies, setStudies] = useState<Study[]>([]);
  const [study, setStudy] = useState<Study | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [view, setView] = useState<View>("studies");
  const [selected, setSelected] = useState<string[]>([]);
  const [comparePhase, setComparePhase] = useState<Phase>("development");
  const [compareRunId, setCompareRunId] = useState<string | null>(null);
  const [detailRunId, setDetailRunId] = useState<string | null>(null);
  const [horizon, setHorizon] = useState<HorizonKey>("5");
  const [sampleMode, setSampleMode] = useState<"native" | "common">("common");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [decisionDraft, setDecisionDraft] = useState<{ decision: DecisionKind; note: string }>({ decision: "继续验证", note: "" });
  const [backtestParams, setBacktestParams] = useState({ top_n: 50, freq: "monthly", cost: 1 });
  const [phaseSourceId, setPhaseSourceId] = useState("");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const gen = useRef(0);
  const sessionRef = useRef(loadToken());
  const mounted = useRef(true);
  const activeRef = useRef("");
  const detailSeq = useRef(0);
  const detailPending = useRef(false);
  const initializedStudy = useRef("");
  const busyRef = useRef(false);
  const current = useCallback((token: number) => mounted.current && token === gen.current && sessionRef.current === loadToken(), []);
  const fail = useCallback((e: unknown, fallback: string) => setError(e instanceof Error ? e.message : fallback), []);

  const loadStudies = useCallback(async () => {
    const token = gen.current;
    try {
      const list = await factorLab.listStudies();
      if (current(token)) setStudies(list);
    } catch (e) { if (current(token)) fail(e, "研究列表加载失败"); }
  }, [current, fail]);

  const loadStatus = useCallback(async () => {
    const session = loadToken();
    try {
      const value = await factorLab.dataStatus();
      if (mounted.current && session === loadToken()) setStatus(value);
    } catch (e) { if (mounted.current && session === loadToken()) fail(e, "数据状态加载失败"); }
  }, [fail]);

  const loadFields = useCallback(async () => {
    const session = loadToken();
    try {
      const value = await factorLab.fields();
      if (mounted.current && session === loadToken()) setDoc(value);
    } catch { /* 公式目录失败不阻止读取已保存的结果。 */ }
  }, []);

  const loadDetail = useCallback(async (targetId: string) => {
    if (!targetId) return;
    const token = gen.current;
    const seq = ++detailSeq.current;
    detailPending.current = true;
    try {
      const detail = await factorLab.studyDetail(targetId);
      if (!current(token) || activeRef.current !== targetId || seq !== detailSeq.current) return;
      if (initializedStudy.current !== targetId) {
        initializedStudy.current = targetId;
        setHorizon(String(detail.study.config.horizon) as HorizonKey);
      }
      setStudy(detail.study); setRuns(detail.runs);
    } catch (e) {
      // 刷新失败保留已读取的报告。
      if (current(token) && activeRef.current === targetId && seq === detailSeq.current) fail(e, "研究详情加载失败");
    } finally {
      if (seq === detailSeq.current) detailPending.current = false;
      if (current(token) && activeRef.current === targetId) setDetailLoading(false);
    }
  }, [current, fail]);

  const closeStudy = useCallback(() => {
    setBuilderKey((value) => value + 1);
    gen.current++; detailSeq.current++; detailPending.current = false;
    activeRef.current = ""; initializedStudy.current = ""; busyRef.current = false;
    setStudy(null); setRuns([]); setSelected([]); setCompareRunId(null); setDetailRunId(null);
    setView("studies"); setDialog(null); setDetailLoading(false); setBusy(false);
    setDecisionDraft({ decision: "继续验证", note: "" }); setPhaseSourceId("");
    setBacktestParams({ top_n: 50, freq: "monthly", cost: 1 }); setError(null); setNotice(null);
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }, []);

  const openStudy = useCallback((targetId: string) => {
    closeStudy();
    activeRef.current = targetId;
    setComparePhase("development"); setSampleMode("common"); setView("runs"); setDetailLoading(true);
    window.history.replaceState(null, "", HASH_PREFIX + targetId);
    loadDetail(targetId);
  }, [closeStudy, loadDetail]);

  useEffect(() => {
    mounted.current = true;
    const hash = window.location.hash;
    if (hash.startsWith(HASH_PREFIX)) openStudy(hash.slice(HASH_PREFIX.length));
    loadStatus(); loadFields();
    loadStudies().finally(() => { if (mounted.current) setLoading(false); });
    return () => { mounted.current = false; gen.current++; };
  }, [loadFields, loadStatus, loadStudies, openStudy]);

  const polling = !!study && hasActiveJobs(runs);
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => {
      if (!detailPending.current && !busyRef.current) loadDetail(activeRef.current);
    }, RUN_POLL_MS);
    return () => window.clearInterval(timer);
  }, [polling, loadDetail]);

  const building = !!status?.building || !!status?.fundamentals?.building;
  useEffect(() => {
    if (!building) return;
    const timer = window.setInterval(loadStatus, 10_000);
    return () => window.clearInterval(timer);
  }, [building, loadStatus]);

  useEffect(() => {
    // storage 不通知同一标签页；焦点与轻量检查也处理同页登录/登出。
    const check = () => {
      const session = loadToken();
      if (session === sessionRef.current) return;
      sessionRef.current = session;
      closeStudy(); setStudies([]); setDoc(undefined); setStatus(undefined);
      if (session) { loadStudies(); loadStatus(); loadFields(); }
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === "vr-auth-token" || event.key === "vr-auth-user") check();
    };
    const timer = window.setInterval(check, 1000);
    window.addEventListener("storage", onStorage);
    window.addEventListener("focus", check);
    window.addEventListener("vr:unauthorized", check);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("focus", check);
      window.removeEventListener("vr:unauthorized", check);
    };
  }, [closeStudy, loadFields, loadStatus, loadStudies]);

  const refresh = useCallback(() => {
    setError(null); setNotice(null);
    loadStatus(); loadFields(); loadStudies();
    if (activeRef.current) loadDetail(activeRef.current);
  }, [loadFields, loadStatus, loadDetail, loadStudies]);

  const act = useCallback(async <T,>(work: () => Promise<T>, after: (value: T) => void, fallback: string, rethrow = false) => {
    if (busyRef.current || !current(gen.current)) {
      if (rethrow) throw new Error("请等待当前操作完成或刷新会话");
      return false;
    }
    const token = gen.current;
    busyRef.current = true; detailSeq.current++;
    setBusy(true); setError(null); setNotice(null);
    try {
      const value = await work();
      if (!current(token)) {
        if (rethrow) throw new Error("页面或会话已切换");
        return false;
      }
      detailSeq.current++; after(value);
      return true;
    } catch (e) {
      if (current(token)) fail(e, fallback);
      if (rethrow) throw e;
      return false;
    } finally {
      if (current(token)) { busyRef.current = false; detailPending.current = false; setBusy(false); }
    }
  }, [current, fail]);

  const upsertRun = useCallback((run: Run) => {
    setRuns((prev) => prev.some((item) => item.id === run.id)
      ? prev.map((item) => item.id === run.id ? run : item) : [...prev, run]);
  }, []);
  const syncStudy = useCallback((updated: Study) => {
    setStudy((prev) => prev?.id === updated.id ? updated : prev);
    setStudies((prev) => prev.map((item) => item.id === updated.id ? updated : item));
  }, []);

  const candidates = useMemo(() => study ? compareCandidates(runs, study, comparePhase) : [], [runs, study, comparePhase]);
  const picks = useMemo(() => selectedCandidates(selected, candidates), [candidates, selected]);
  const freezeReady = useMemo(() => study ? freezeCandidates(runs, study) : [], [runs, study]);
  const frozenRuns = useMemo(() => {
    if (!study) return [];
    const ids = frozenRunIds(study);
    return freezeReady.filter((run) => ids.includes(run.id));
  }, [freezeReady, study]);
  const phaseSource = frozenRuns.find((run) => run.id === phaseSourceId) ?? frozenRuns[0] ?? null;
  const baselineRun = runs.find((run) => run.id === study?.baseline_id) ?? null;
  const compareRuns = useMemo(() => runs.filter((run) => run.kind === "compare" && run.phase === comparePhase)
    .sort((a, b) => b.created_at - a.created_at), [runs, comparePhase]);
  const compareRun = compareRuns.find((run) => run.id === compareRunId) ?? compareRuns[0] ?? null;
  const compareResult = compareRun?.status === "completed" ? compareRun.result as AnalysisResult | null : null;
  const runNames = Object.fromEntries(runs.map((run) => [run.id, run.name]));
  const evidenceIds = picks.length ? picks.map((run) => run.id) : compareRun?.source_run_ids ?? [];

  const createStudy = useCallback(async (body: { name: string; description: string; config: StudyConfig }) => {
    await act(() => factorLab.createStudy(body), (created) => {
      setStudies((prev) => [created, ...prev]); openStudy(created.id);
    }, "创建研究失败", true);
  }, [act, openStudy]);
  const createRun = useCallback(async (body: { name: string; definition?: Definition; phase: Phase; source_run_id?: string }) => {
    await act(() => factorLab.createRun(activeRef.current, body), (created) => {
      upsertRun(created); setDetailRunId(created.id); setView("runs"); setDialog(null); setNotice("已提交实验");
    }, "创建实验失败", true);
  }, [act, upsertRun]);

  // 页内首次运行：创建研究成功即保留归属，后续实验失败可在同一研究重试。
  const submitBuilder = useCallback(async (body: { name: string; definition: Definition }, config: StudyConfig) => {
    await act(async () => {
      let studyId = activeRef.current;
      const token = gen.current;
      if (!studyId) {
        const created = await factorLab.createStudy({ name: body.name, config });
        if (!current(token)) throw new Error("页面或会话已切换");
        activeRef.current = created.id; initializedStudy.current = created.id;
        setStudy(created); setStudies((prev) => [created, ...prev]);
        setRuns([]); setView("runs"); setHorizon(String(config.horizon) as HorizonKey);
        window.history.replaceState(null, "", HASH_PREFIX + created.id);
        studyId = created.id;
      }
      return factorLab.createRun(studyId, { ...body, phase: "development" });
    }, (created) => {
      upsertRun(created); setDetailRunId(created.id); setView("runs"); setNotice("已提交新版本 · 开发期");
    }, "提交失败", true);
  }, [act, current, upsertRun]);

  const retryJob = useCallback((run: Run) => {
    const spec = retrySpec(run);
    return act(() => spec.kind === "compare" ? factorLab.compare(activeRef.current, spec.runIds)
      : spec.kind === "backtest" ? factorLab.backtest(activeRef.current, spec.body)
      : factorLab.createRun(activeRef.current, spec.body),
    (created) => {
      upsertRun(created); setDialog(null); setNotice("已创建重试记录");
      if (created.kind === "compare") { setComparePhase(created.phase); setCompareRunId(created.id); setView("compare"); }
      else { setDetailRunId(created.id); setView("runs"); }
    }, "重试失败");
  }, [act, upsertRun]);
  const retryRun = useCallback((run: Run) => {
    if (run.phase === "holdout") setDialog({ kind: "confirm", action: "holdout", retry: run });
    else retryJob(run);
  }, [retryJob]);
  const cancelRun = useCallback((run: Run) => {
    act(() => factorLab.cancelRun(activeRef.current, run.id), upsertRun, "取消失败");
  }, [act, upsertRun]);
  const setBaseline = useCallback((run: Run) => {
    act(() => factorLab.updateStudy(activeRef.current, { baseline_id: run.id }), syncStudy, "设置基线失败");
  }, [act, syncStudy]);
  const startCompare = useCallback((ids: string[]) => {
    act(() => factorLab.compare(activeRef.current, ids), (created) => {
      upsertRun(created); setCompareRunId(created.id); setComparePhase(created.phase);
      setSampleMode("common"); setView("compare"); setDialog(null); setNotice("已提交对比");
    }, "创建对比失败");
  }, [act, upsertRun]);
  const startBacktest = useCallback(() => {
    if (dialog?.kind !== "backtest") return;
    act(() => factorLab.backtest(activeRef.current, { run_id: dialog.runId, ...backtestParams }), (created) => {
      upsertRun(created); setDetailRunId(created.id); setView("runs"); setDialog(null); setNotice("已提交回测");
    }, "创建回测失败");
  }, [act, backtestParams, dialog, upsertRun]);

  const startPhaseRun = useCallback(async (phase: "validation" | "holdout") => {
    if (!phaseSource) { setError("请先锁定开发期实验"); return; }
    const targetId = activeRef.current;
    const token = gen.current;
    await act(() => factorLab.createRun(targetId, {
      name: `${phaseSource.name} · ${phase === "validation" ? "验证期" : "留出期"}`,
      phase, source_run_id: phaseSource.id,
    }), (created) => {
      upsertRun(created); setDetailRunId(created.id); setView("runs"); setDialog(null);
      setNotice(phase === "holdout" ? "留出期已使用" : "已提交验证期实验");
    }, "创建实验失败");
    // 请求被接受时已使用留出期，即使随后计算失败也刷新该事实。
    if (current(token)) loadDetail(targetId);
  }, [act, current, loadDetail, phaseSource, upsertRun]);
  const confirmHoldout = useCallback(async () => {
    if (dialog?.kind === "confirm" && dialog.retry) {
      const token = gen.current;
      await retryJob(dialog.retry);
      if (current(token)) loadDetail(activeRef.current);
    } else await startPhaseRun("holdout");
  }, [current, dialog, loadDetail, retryJob, startPhaseRun]);
  const startValidation = useCallback(() => { startPhaseRun("validation"); }, [startPhaseRun]);
  const confirmFreeze = useCallback((ids: string[]) => {
    if (!study?.baseline_id) { setError("请先设置基线"); return; }
    const runIds = [...new Set([...ids, study.baseline_id])];
    if (!ids.length || runIds.length > MAX_COMPARE || runIds.some((id) => !freezeReady.some((run) => run.id === id))) {
      setError("请选择最多 4 个已完成的开发期实验（含基线）"); return;
    }
    act(() => factorLab.freeze(activeRef.current, runIds), (updated) => {
      syncStudy(updated); setDialog(null); setNotice("已锁定选中实验");
    }, "锁定失败");
  }, [act, freezeReady, study, syncStudy]);
  const saveDecision = useCallback(() => {
    if (!decisionDraft.note.trim()) { setError("备注不能为空"); return; }
    act(() => factorLab.decide(activeRef.current, {
      decision: decisionDraft.decision, note: decisionDraft.note.trim(), run_ids: evidenceIds,
    }), (updated) => {
      syncStudy(updated); setDecisionDraft({ decision: decisionDraft.decision, note: "" }); setNotice("已记录结论");
    }, "保存结论失败");
  }, [act, decisionDraft, evidenceIds, syncStudy]);
  const archiveStudy = useCallback((target: Study, archived: boolean) => {
    act(() => factorLab.updateStudy(target.id, { archived }), (updated) => {
      syncStudy(updated); setNotice(archived ? "已归档" : "已恢复");
    }, "保存失败");
  }, [act, syncStudy]);
  const exportStudy = useCallback(() => {
    act(() => factorLab.exportStudy(activeRef.current), (payload) => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = `factor-study-${payload.study.id.slice(0, 8)}.json`; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, "导出失败");
  }, [act]);
  const buildData = useCallback(() => {
    act(() => factorLab.buildData(), (result) => {
      setStatus((prev) => prev ? { ...prev, building: true } : prev); loadStatus();
      setNotice(result.started ? "已开始构建数据" : "已有构建任务在运行");
    }, "构建失败");
  }, [act, loadStatus]);
  const toggleSelected = useCallback((runId: string) => {
    setSelected((prev) => prev.includes(runId) ? prev.filter((id) => id !== runId)
      : prev.length >= MAX_COMPARE ? prev : [...prev, runId]);
  }, []);
  const changeComparePhase = useCallback((phase: Phase) => {
    setComparePhase(phase); setSelected([]); setCompareRunId(null);
  }, []);
  const openRunForm = useCallback((initial: RunFormInitial | null) => setDialog({ kind: "run", initial }), []);

  return {
    builderKey, status, doc, studies, study, runs, loading, detailLoading, busy, error, notice,
    dataReady: !!status?.has_data, building, setError, setNotice,
    view, setView, selected, toggleSelected, comparePhase, setComparePhase: changeComparePhase, compareRunId, setCompareRunId,
    detailRunId, setDetailRunId, horizon, setHorizon, sampleMode, setSampleMode,
    dialog, setDialog, decisionDraft, setDecisionDraft, backtestParams, setBacktestParams, phaseSource, phaseSourceId, setPhaseSourceId,
    candidates, picks, freezeReady, frozenRuns, baselineRun, compareRuns, compareRun, compareResult, runNames, evidenceIds,
    refresh, openStudy, closeStudy, createStudy, createRun, submitBuilder, retryRun, cancelRun, setBaseline,
    startCompare, startBacktest, confirmFreeze, confirmHoldout, startValidation, saveDecision,
    archiveStudy, exportStudy, buildData, openRunForm, loadDetail,
  };
}
export type FactorResearch = ReturnType<typeof useFactorResearch>;
