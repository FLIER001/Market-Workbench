// 因子研究 V2 客户端：/api/factor/research 系列（账号归属的研究、实验记录与持久化任务）。
//
// 复用 api.ts 的 authHeaders 与 401 语义：脚本因子研究的接口都要求登录会话
// （后端 _current_user），401 只可能是两种情况——访问密钥缺失（MW_API_KEY）
// 或会话过期。前者不能清会话（否则误登出），后者按既有 vr:unauthorized 事件处理。
//
// 本模块不做任何本地缓存：研究/实验结果只存在组件内存与后端，切换账号即丢弃。

import { ApiError, authHeaders, type FactorCatalog, type FactorFieldsDoc, type FactorLabStatus } from "@/lib/api";
import { clearSession, loadToken } from "@/lib/auth";
import { markPageData } from "@/lib/dataFreshness";

const TIMEOUT_MS = 60_000;

export const ACCESS_KEY_HINT = "后端开启了访问鉴权（MW_API_KEY）：请在「接入 AI」页底部填写后端访问密钥";
export const SESSION_HINT = "未登录或登录已过期，请重新登录";

// 复用既有 api.ts 的目录/状态类型，避免重复声明。
export type { FactorCatalog, FactorFieldsDoc, FactorLabStatus };

/** 401 归因：访问密钥缺失 vs 会话过期。纯函数，测试直接覆盖。 */
export function unauthorizedKind(status: number, detail: string): "none" | "session" | "access_key" {
  if (status !== 401) return "none";
  return /api key/i.test(detail) ? "access_key" : "session";
}

async function labFetch<T>(path: string, method: "GET" | "POST" | "PUT" = "GET", body?: unknown): Promise<T> {
  const session = loadToken();
  const checkSession = () => {
    if (session !== loadToken()) throw new ApiError("会话已切换，请刷新", 0);
  };
  const headers: Record<string, string> = { ...authHeaders() };
  const opts: RequestInit = { method };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (Object.keys(headers).length > 0) opts.headers = headers;
  let resp: Response;
  try {
    resp = await fetch(`/api${path}`, { ...opts, signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch (err) {
    checkSession();
    const timedOut = err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError");
    throw new ApiError(timedOut ? `请求超时（${TIMEOUT_MS / 1000}s），稍后重试` : "连接不到后端，请确认服务已启动", 0);
  }
  let payload: { detail?: unknown; data?: unknown } | null = null;
  try {
    payload = await resp.json();
  } catch {
    /* 非 JSON 响应（如反向代理错误页） */
  }
  // 旧会话的成功响应和 401 都不能影响当前账号。
  checkSession();
  if (!resp.ok) {
    const detail = typeof payload?.detail === "string" ? payload.detail : "";
    const kind = unauthorizedKind(resp.status, detail);
    if (kind === "session") {
      clearSession();
      // 会话失效 → 既有路由守卫监听该事件跳回登录页。
      if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent("vr:unauthorized"));
      throw new ApiError(SESSION_HINT, 401);
    }
    if (kind === "access_key") throw new ApiError(ACCESS_KEY_HINT, 401);
    throw new ApiError(detail || `HTTP ${resp.status}`, resp.status);
  }
  const data = (payload?.data ?? payload) as T;
  markPageData(data);
  return data;
}

const id = (value: string) => encodeURIComponent(value);

// ---------------------------------------------------------------------------
// 契约类型（docs/architecture.md 的「因子研究接口契约」）
// ---------------------------------------------------------------------------

export type Phase = "development" | "validation" | "holdout";
export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type RunKind = "evaluate" | "compare" | "backtest";
export type Horizon = 1 | 5 | 10 | 20;
export type HorizonKey = "1" | "5" | "10" | "20";
export type DecisionKind = "继续验证" | "保留候选" | "暂缓" | "淘汰";

export interface Definition { factor?: string; expr?: string; direction: "high" | "low" }
export interface Period { start: string; end: string }
export interface StudyConfig {
  horizon: Horizon;
  min_days_listed: number;
  development: Period; validation: Period; holdout: Period;
}
export interface InputManifest {
  id: string; data_version: string;
  files: Record<string, unknown>; code: Record<string, string>;
}
export interface Freeze { id: string; run_ids: string[]; baseline_id: string; created_at: number }
export interface Decision { decision: DecisionKind; note: string; run_ids: string[]; created_at: number }
export interface Study {
  id: string; name: string; description: string; config: StudyConfig;
  input_manifest: InputManifest; baseline_id: string | null; archived: boolean;
  freezes: Freeze[]; holdout_used_at: number | null; decisions: Decision[];
  created_at: number; updated_at: number;
}
export interface Run {
  id: string; study_id: string; kind: RunKind;
  name: string; definition: Definition | null; phase: Phase;
  source_run_ids: string[]; params: Record<string, unknown>;
  input_manifest: InputManifest;
  status: RunStatus; error: string | null; result: unknown | null;
  created_at: number; started_at: number | null; finished_at: number | null;
}
export interface StudyDetail { study: Study; runs: Run[] }
export interface StudyExport {
  format: "market-workbench-factor-study"; version: 1; study: Study; runs: Run[];
}
export interface HorizonSample {
  input_rows: number; eligible_rows: number; factor_rows: number; paired_rows: number;
  used_rows: number; loss_ratio: number | null; skipped_days: number;
}
export interface HorizonStats {
  n_days: number; avg_coverage: number;
  sample: HorizonSample;
  ic: {
    ic_mean: number | null; rank_ic_mean: number | null;
    rank_ic_ir: number | null; rank_ic_positive_ratio: number | null;
  };
  ic_series: { dates: string[]; values: number[] };
  quantile_returns: Record<string, number | null>;
  quantile_turnover: Record<string, number | null>;
  long_short_bp: number | null; rank_autocorr: number | null;
  by_year: Record<string, { rank_ic_mean: number | null; rank_ic_ir: number | null; days: number }>;
}
export interface NativeResult { definition: Definition; by_horizon: Record<string, HorizonStats> }
export interface AnalysisResult {
  phase: Phase; start: string; end: string; input_manifest: InputManifest;
  horizons: number[];
  native: NativeResult[];
  common: { by_horizon: Record<string, { results: HorizonStats[]; paired_rows: number }> } | null;
  correlation: { matrix: Array<Array<number | null>>; n_days: number; sample_rows: number } | null;
  biases: string[]; timing_note: string;
}

export const HORIZONS: HorizonKey[] = ["1", "5", "10", "20"];
export const PHASES: Phase[] = ["development", "validation", "holdout"];
export const MAX_COMPARE = 4;

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消",
};
export const PHASE_LABEL: Record<Phase, string> = {
  development: "开发期", validation: "验证期", holdout: "留出期",
};
export const KIND_LABEL: Record<RunKind, string> = { evaluate: "实验", compare: "对比", backtest: "回测" };

export const RUN_POLL_MS = 5_000;

// ---------------------------------------------------------------------------
// 纯逻辑（页面用它决策，node:test 直接测）
// ---------------------------------------------------------------------------

export function isActiveStatus(status: RunStatus): boolean {
  return status === "queued" || status === "running";
}

export function hasActiveJobs(runs: Run[]): boolean {
  return runs.some((run) => isActiveStatus(run.status));
}

export function definitionText(definition: Definition | null | undefined): string {
  if (!definition) return "—";
  return definition.factor ?? definition.expr ?? "—";
}

/** 数据版本不同（输入清单 id 不一致）的实验不得直接对比排名。 */
export function manifestMatches(run: Run, study: Study): boolean {
  const studyId = study.input_manifest?.id;
  return !!studyId && run.input_manifest?.id === studyId;
}

/** 可参与对比的实验：同研究、同阶段、已完成、数据版本一致的 evaluate。 */
export function compareCandidates(runs: Run[], study: Study, phase: Phase): Run[] {
  return runs.filter((run) =>
    run.study_id === study.id && run.kind === "evaluate" && run.status === "completed" && run.phase === phase && manifestMatches(run, study),
  );
}

/** 可锁定的实验：已完成开发期 evaluate（数据版本一致）。 */
export function freezeCandidates(runs: Run[], study: Study): Run[] {
  return compareCandidates(runs, study, "development");
}

export function toggleSelection(selected: string[], runId: string, max = MAX_COMPARE): string[] {
  if (selected.includes(runId)) return selected.filter((value) => value !== runId);
  if (selected.length >= max) return selected;
  return [...selected, runId];
}

export function selectedCandidates(selected: string[], candidates: Run[]): Run[] {
  return candidates.filter((run) => selected.includes(run.id));
}

export function frozenRunIds(study: Study): string[] {
  return [...new Set(study.freezes.flatMap((freeze) => freeze.run_ids))];
}

/** 是否已被任一锁定记录覆盖（页面用来标记已锁定实验）。 */
export function isFrozen(study: Study, runId: string): boolean {
  return frozenRunIds(study).includes(runId);
}

export function holdoutText(study: Study): string {
  return study.holdout_used_at != null ? "留出期已使用" : "留出期：未查看";
}

export function latestDecision(study: Study): Decision | null {
  return study.decisions.length > 0 ? study.decisions[study.decisions.length - 1] : null;
}

/** 有因子值的记录占比；日均股票数不是覆盖率。 */
export function factorCoverage(stats: HorizonStats | null | undefined): number | null {
  const sample = stats?.sample;
  if (!sample || !Number.isInteger(sample.eligible_rows) || sample.eligible_rows <= 0
    || !Number.isInteger(sample.factor_rows) || sample.factor_rows < 0 || sample.factor_rows > sample.eligible_rows) return null;
  return sample.factor_rows / sample.eligible_rows;
}

export function summaryMetrics(stats: HorizonStats | null | undefined) {
  return {
    rank_ic: stats?.ic.rank_ic_mean ?? null,
    ic_ir: stats?.ic.rank_ic_ir ?? null,
    positive_ratio: stats?.ic.rank_ic_positive_ratio ?? null,
    turnover_q5: stats?.quantile_turnover?.Q5 ?? null,
    coverage: stats?.avg_coverage ?? null,
    loss_ratio: stats?.sample?.loss_ratio ?? null,
    n_days: stats?.n_days ?? null,
  };
}

/** 缺少共同样本时保持缺失，不用各自样本冒充。 */
export function commonStats(result: AnalysisResult | null, horizon: HorizonKey, index: number): HorizonStats | null {
  const entry = result?.common?.by_horizon?.[horizon];
  const stats = entry?.results?.[index];
  return stats ?? null;
}

export function nativeStats(result: AnalysisResult | null, horizon: HorizonKey, index: number): HorizonStats | null {
  return result?.native?.[index]?.by_horizon?.[horizon] ?? null;
}

export function definitionAt(result: AnalysisResult | null, index: number): Definition | null {
  return result?.native?.[index]?.definition ?? null;
}

const MONTH_END = (date: Date) => new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0));
const iso = (date: Date) => date.toISOString().slice(0, 10);
const addDays = (date: Date, days: number) => new Date(date.getTime() + days * 86_400_000);

/** ISO 日期校验 + 三段有序不重叠。返回错误文案或 null。 */
export function validateConfig(config: StudyConfig): string | null {
  const isDate = (value: string) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value ?? "")) return false;
    const date = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(date.getTime()) && iso(date) === value;
  };
  for (const phase of PHASES) {
    const period = config[phase];
    if (!isDate(period?.start) || !isDate(period?.end)) return `${PHASE_LABEL[phase]}需要完整日期`;
    if (period.start > period.end) return `${PHASE_LABEL[phase]}开始日期晚于结束日期`;
  }
  if (config.development.end >= config.validation.start) return "开发期与验证期必须按时间分开";
  if (config.validation.end >= config.holdout.start) return "验证期与留出期必须按时间分开";
  if (![1, 5, 10, 20].includes(config.horizon)) return "预测周期取值不支持";
  if (!Number.isInteger(config.min_days_listed) || config.min_days_listed < 0) return "最小上市天数需为不小于 0 的整数";
  return null;
}

/** 按数据目录区间推导默认三段（等分到月末，留出一日间隔避免边界重叠）。 */
export function defaultConfig(catalog: FactorCatalog | null | undefined, horizon: Horizon = 5): StudyConfig | null {
  const min = catalog?.date_min;
  const max = catalog?.date_max;
  if (!min || !max || min >= max) return null;
  const start = new Date(`${min}T00:00:00Z`);
  const stop = new Date(`${max}T00:00:00Z`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(stop.getTime())) return null;
  const days = Math.floor((stop.getTime() - start.getTime()) / 86_400_000);
  if (days < 90) return null;
  const first = iso(MONTH_END(addDays(start, Math.floor(days / 3))));
  const second = iso(MONTH_END(addDays(start, Math.floor((days * 2) / 3))));
  const config: StudyConfig = {
    horizon,
    min_days_listed: 250,
    development: { start: min, end: first },
    validation: { start: iso(addDays(new Date(`${first}T00:00:00Z`), 1)), end: second },
    holdout: { start: iso(addDays(new Date(`${second}T00:00:00Z`), 1)), end: max },
  };
  return validateConfig(config) ? null : config;
}

/** 新建/复制实验时按已有基线兜底，避免默认值与后端目录不一致。 */
export function cloneConfig(config: StudyConfig, horizon?: Horizon): StudyConfig {
  return {
    ...config,
    horizon: horizon ?? config.horizon,
    development: { ...config.development },
    validation: { ...config.validation },
    holdout: { ...config.holdout },
  };
}

/** 重试保留任务种类和参数，创建新记录。 */
export function retrySpec(run: Run) {
  if (run.kind === "compare") return { kind: "compare" as const, runIds: [...run.source_run_ids] };
  if (run.kind === "backtest") return { kind: "backtest" as const, body: {
    run_id: run.source_run_ids[0],
    top_n: run.params.top_n as number | undefined, freq: run.params.freq as string | undefined,
    cost: run.params.cost as number | undefined,
  } };
  return { kind: "evaluate" as const, body: {
    name: `${run.name} · 重试`, phase: run.phase,
    ...(run.phase === "development" && run.definition ? { definition: { ...run.definition } } : {}),
    ...(run.phase !== "development" ? { source_run_id: run.source_run_ids[0] } : {}),
  } };
}

/** 只对齐已计算 IC 的日期；缺失点留空，不重算、不填 0。 */
export function alignICSeries(stats: Array<HorizonStats | null>) {
  const dates = [...new Set(stats.flatMap((s) => s?.ic_series.dates ?? []))].sort();
  const values = stats.map((s) => {
    const byDate = new Map(s?.ic_series.dates.map((date, i) => [date, s.ic_series.values[i]]) ?? []);
    return dates.map((date) => byDate.get(date) ?? null);
  });
  return { dates, values };
}

export function metricDelta(value: number | null | undefined, baseline: number | null | undefined): number | null {
  return value == null || baseline == null || !Number.isFinite(value) || !Number.isFinite(baseline) ? null : value - baseline;
}

export const factorLab = {
  listStudies: () => labFetch<Study[]>("/factor/research"),
  createStudy: (body: { name: string; description?: string; config: StudyConfig }) =>
    labFetch<Study>("/factor/research", "POST", body),
  studyDetail: (studyId: string) => labFetch<StudyDetail>(`/factor/research/${id(studyId)}`),
  updateStudy: (
    studyId: string,
    body: { name?: string; description?: string; archived?: boolean; baseline_id?: string },
  ) => labFetch<Study>(`/factor/research/${id(studyId)}`, "PUT", body),
  createRun: (
    studyId: string,
    body: { name: string; definition?: Definition; phase?: Phase; source_run_id?: string },
  ) => labFetch<Run>(`/factor/research/${id(studyId)}/runs`, "POST", body),
  run: (studyId: string, runId: string) => labFetch<Run>(`/factor/research/${id(studyId)}/runs/${id(runId)}`),
  cancelRun: (studyId: string, runId: string) =>
    labFetch<Run>(`/factor/research/${id(studyId)}/runs/${id(runId)}/cancel`, "POST"),
  compare: (studyId: string, runIds: string[]) =>
    labFetch<Run>(`/factor/research/${id(studyId)}/compare`, "POST", { run_ids: runIds }),
  backtest: (studyId: string, body: { run_id: string; top_n?: number; freq?: string; cost?: number }) =>
    labFetch<Run>(`/factor/research/${id(studyId)}/backtest`, "POST", body),
  freeze: (studyId: string, runIds: string[]) =>
    labFetch<Study>(`/factor/research/${id(studyId)}/freeze`, "POST", { run_ids: runIds }),
  decide: (studyId: string, body: { decision: DecisionKind; note: string; run_ids?: string[] }) =>
    labFetch<Study>(`/factor/research/${id(studyId)}/decisions`, "POST", body),
  exportStudy: (studyId: string) => labFetch<StudyExport>(`/factor/research/${id(studyId)}/export`),
  // 既有接口（数据状态/公式目录），不在本轮契约变更范围内，按原样复用。
  dataStatus: () => labFetch<FactorLabStatus>("/factor/lab"),
  fields: () => labFetch<FactorFieldsDoc>("/factor/fields"),
  buildData: () => labFetch<{ building: boolean; started: boolean }>("/factor/build", "POST"),
};
