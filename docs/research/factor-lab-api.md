# Factor research implementation contract

Existing `/api/factor/*` endpoints remain compatible. New routes require the existing user-token authentication and return `{data: ...}`. All records are owned by the authenticated account. Frontend copy follows the concise prototype.

## Data

```ts
type Phase = 'development' | 'validation' | 'holdout';
type Definition = { factor?: string; expr?: string; direction: 'high' | 'low' };
type Period = {start: string; end: string};
type StudyConfig = {
  horizon: 1 | 5 | 10 | 20;
  min_days_listed: number;
  development: Period; validation: Period; holdout: Period;
};
type InputManifest = {id: string; data_version: string; files: Record<string, unknown>; code: Record<string, string>};
type Freeze = {id: string; run_ids: string[]; baseline_id: string; created_at: number};
type Decision = {decision: '继续验证' | '保留候选' | '暂缓' | '淘汰'; note: string; run_ids: string[]; created_at: number};
type Study = {
  id: string; name: string; description: string; config: StudyConfig;
  input_manifest: InputManifest; baseline_id: string | null; archived: boolean;
  freezes: Freeze[]; holdout_used_at: number | null; decisions: Decision[];
  created_at: number; updated_at: number;
};
type Run = {
  id: string; study_id: string; kind: 'evaluate' | 'compare' | 'backtest';
  name: string; definition: Definition | null; phase: Phase;
  source_run_ids: string[]; params: Record<string, unknown>;
  input_manifest: InputManifest;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  error: string | null; result: unknown | null;
  created_at: number; started_at: number | null; finished_at: number | null;
};
```

A factor definition contains exactly one of builtin `factor` or sandboxed `expr`. No temporary shared-factor save is used by new execution. Direction is applied once, before statistics and portfolio ranking. Source definitions and study configs cannot be overwritten. Config dates are ISO dates with non-overlapping ordered periods. Creation may precede dataset build; execution must validate input availability/version. Input paths in exported metadata are logical labels, not local absolute paths.

## Routes

- `GET /api/factor/research` -> `Study[]`
- `POST /api/factor/research` body `{name, description?, config}` -> `Study` (captures current input manifest)
- `GET /api/factor/research/{id}` -> `{study: Study, runs: Run[]}` (all job kinds, account scoped)
- `PUT /api/factor/research/{id}` body `{name?, description?, archived?, baseline_id?}` -> `Study`; no config changes. Baseline must be a completed development evaluate run belonging to this study.
- `POST /api/factor/research/{id}/runs` body `{name, definition?, phase?, source_run_id?}` -> queued `Run`. Development requires definition. Validation/holdout reuse a completed development source in a frozen selection; clients cannot change its definition/config. Using holdout permanently sets `holdout_used_at` after request validation. UI asks confirmation and labels it “留出期已使用”.
- `GET /api/factor/research/{id}/runs/{run_id}` -> `Run`
- `POST /api/factor/research/{id}/runs/{run_id}/cancel` -> `Run`; cancellation must not later be overwritten by completion.
- `POST /api/factor/research/{id}/compare` body `{run_ids: string[]}` -> queued compare `Run`. 2–4 distinct completed evaluate runs, same study/config/phase/manifest; common samples must be recomputed, never joined scalar summaries.
- `POST /api/factor/research/{id}/backtest` body `{run_id, top_n?, freq?, cost?}` -> queued backtest `Run`. Reuses the stored definition, direction, phase range and study inputs; no shared legacy artifact write.
- `POST /api/factor/research/{id}/freeze` body `{run_ids: string[]}` -> `Study`. Append immutable freeze entry for 1–4 completed development evaluate runs, including the study baseline (adding it if needed).
- `POST /api/factor/research/{id}/decisions` body `{decision, note, run_ids?: string[]}` -> `Study`. Nonempty note; append, do not overwrite; validate references.
- `GET /api/factor/research/{id}/export` -> `{format:'market-workbench-factor-study', version:1, study, runs}`. No auth tokens or other users' data.

Failure returns HTTP 400 for invalid configuration/unsupported comparison, 401 for missing session, 404 for inaccessible records, 409 for conflicting operations where appropriate. User-visible messages are short and specific. Explicit failed job records remain visible. Retry creates a new run.

## Computation module: backend/factor_analysis.py

These pure functions are shared by persistence jobs and tests:

- `input_manifest() -> dict`: input file fingerprints (bars/catalog/instruments/calendar/fundamentals) and calculation-code fingerprints. Consistency checked before and after execution. Hash large inputs incrementally with a stat-keyed cache; do not load entire files just to hash them.
- `validate_definition(definition: dict) -> dict`: validate builtin or compile expression, return normalized serializable definition.
- `validate_config(config: dict) -> dict`: normalize primary horizon, listing filter and ordered three periods. No future operators or made-up universes.
- `execute(definitions: list[dict], config: dict, phase: str, expected_manifest: dict, cancelled=None) -> dict`: load the panel once, preserve historical warmup, mask labels whose end trading date exceeds phase end. Return per-definition native statistics and recomputed common-sample statistics for all 1/5/10/20 horizons. `cancelled` is an optional callable checked between work units; cancelled execution raises an appropriate exception, never returns successful evidence.
- `backtest(definition: dict, config: dict, phase: str, params: dict, expected_manifest: dict, cancelled=None) -> dict`: reuse existing portfolio simulator with snapshot values, safe timing and no global artifact save.

Execution result (ordered by the supplied definitions):

```ts
type HorizonStats = {
  n_days: number; avg_coverage: number;
  sample: {input_rows:number; eligible_rows:number; factor_rows:number; paired_rows:number; used_rows:number; loss_ratio:number|null; skipped_days:number};
  ic: {ic_mean:number|null; rank_ic_mean:number|null; rank_ic_ir:number|null; rank_ic_positive_ratio:number|null};
  ic_series: {dates:string[]; values:number[]};
  quantile_returns: Record<string, number|null>;
  quantile_turnover: Record<string, number|null>;
  long_short_bp: number|null; rank_autocorr:number|null;
  by_year: Record<string, {rank_ic_mean:number|null; rank_ic_ir:number|null; days:number}>;
};
type AnalysisResult = {
  phase: Phase; start:string; end:string; input_manifest:InputManifest;
  horizons: number[];
  native: Array<{definition:Definition; by_horizon:Record<string,HorizonStats>}>;
  common: {by_horizon:Record<string,{results:HorizonStats[]; paired_rows:number}>} | null;
  correlation: {matrix:Array<Array<number|null>>; n_days:number; sample_rows:number} | null;
  biases:string[]; timing_note:string;
};
```

Completed standalone evaluate run stores this result with `native.length===1`. Compare result stores `native.length===N` plus `common` and daily cross-sectional rank correlation on the actual joint factor/stock/date sample. A factor-value correlation is not IC-series correlation. Show null/unavailable rather than zero for no valid evidence. Listing proxy and finance-data limitations remain disclosed; no automatic “valid factor” score.

## Persistence/jobs

Reuse the existing users SQLite connection/database with account-scoped research/run tables; keep large analysis results out of the generic user-data payload fetched by unrelated pages. No new dependencies. A single execution lane bounds full-market memory. Prefer an isolated calculation subprocess (existing codebase uses this pattern), if cancellation/restart and tests can be implemented cleanly; never concurrently load multiple whole-market panels. Pending/running jobs left by a service restart become explicit failures, with original specs retained. No computations during imports.

## Frontend ownership

`frontend/src/lib/factorLab.ts`: HTTP client and new types above, using existing `authHeaders` and compatible 401 handling; no modifications to generic api.ts needed unless supervisor integrates later.

`frontend/src/pages/Factors.tsx`: real study list + create/copy study config, experiment creation from builtin/expr/legacy custom snapshot, polling jobs from server, per-period detail and comparison/correlation, freeze + validate/holdout + decisions/export, existing-data status/build when needed. No demo values. Concise labels. Persisted completed results stay visible when editing a new run; full calculation stays server-side. Native dialogs, existing charts/components. Update existing frontend factor tests for the new actual page, not the old main-local handler names.
