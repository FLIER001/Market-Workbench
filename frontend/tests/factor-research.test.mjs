import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import test from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const source = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function moduleAt(path, imports, globals = {}) {
  const compiled = ts.transpileModule(source(path), { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const context = { exports: {}, console, Error, AbortSignal, URL, Blob,
    require: (name) => name in imports ? imports[name] : require(name), ...globals };
  vm.runInNewContext(compiled, context, { filename: path });
  return context.exports;
}

function client(fetcher = async () => response({ data: [] })) {
  let token = "account-A", cleared = 0;
  const events = [], calls = [];
  class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
  const lab = moduleAt("lib/factorLab.ts", {
    "@/lib/api": { ApiError, authHeaders: () => ({ "X-VR-User-Token": token, "X-VR-Access-Key": "access" }) },
    "@/lib/auth": { loadToken: () => token, clearSession: () => { cleared++; token = ""; } },
    "@/lib/dataFreshness": { markPageData: () => {} },
  }, {
    fetch: (...args) => { calls.push(args); return fetcher(...args); },
    window: { dispatchEvent: (event) => events.push(event.type) },
    CustomEvent: class { constructor(type) { this.type = type; } },
  });
  return { lab, calls, events, setToken: (value) => { token = value; }, get cleared() { return cleared; } };
}
const response = (payload, status = 200) => ({ ok: status < 400, status, json: async () => payload });
const { lab } = client();
const manifest = { id: "M1", data_version: "DS1", files: {}, code: {} };
const study = (over = {}) => ({
  id: "s1", name: "研究", description: "", config: {
    horizon: 5, min_days_listed: 250,
    development: { start: "2018-01-01", end: "2019-12-31" },
    validation: { start: "2020-01-01", end: "2021-12-31" },
    holdout: { start: "2022-01-01", end: "2023-12-31" },
  }, input_manifest: manifest, baseline_id: null, archived: false,
  freezes: [], holdout_used_at: null, decisions: [], created_at: 1, updated_at: 1, ...over,
});
const run = (over = {}) => ({
  id: "r1", study_id: "s1", name: "原实验", kind: "evaluate", phase: "development",
  definition: { factor: "mom60", direction: "low" }, source_run_ids: [], params: {},
  input_manifest: manifest, status: "completed", error: null, result: null,
  created_at: 1, started_at: 2, finished_at: 3, ...over,
});
const stats = (value = null, dates = [], values = []) => ({
  n_days: dates.length, avg_coverage: 100,
  sample: { input_rows: 10, eligible_rows: 9, factor_rows: 8, paired_rows: 7, used_rows: 6, loss_ratio: null, skipped_days: 0 },
  ic: { ic_mean: value, rank_ic_mean: value, rank_ic_ir: null, rank_ic_positive_ratio: null },
  ic_series: { dates, values }, quantile_returns: { Q1: null, Q5: 0 }, quantile_turnover: { Q5: 0 },
  long_short_bp: null, rank_autocorr: null, by_year: {},
});
const analysis = (native = stats()) => ({ phase: "development", start: "2018-01-01", end: "2019-12-31",
  input_manifest: manifest, horizons: [1, 5, 10, 20],
  native: [{ definition: run().definition, by_horizon: { 5: native } }], common: null, correlation: null, biases: [], timing_note: "T 日信号",
});

test("HTTP client sends exact contract methods, encoded IDs, bodies and user-token headers", async () => {
  const c = client(async () => response({ data: { ok: true } }));
  const f = c.lab.factorLab, sid = "s/1", rid = "r/2";
  const operations = [
    [() => f.listStudies(), "/api/factor/research", "GET"],
    [() => f.createStudy({ name: "新研究", config: study().config }), "/api/factor/research", "POST", { name: "新研究", config: study().config }],
    [() => f.studyDetail(sid), "/api/factor/research/s%2F1", "GET"],
    [() => f.updateStudy(sid, { archived: true }), "/api/factor/research/s%2F1", "PUT", { archived: true }],
    [() => f.createRun(sid, { name: "验证", phase: "validation", source_run_id: rid }), "/api/factor/research/s%2F1/runs", "POST", { name: "验证", phase: "validation", source_run_id: rid }],
    [() => f.run(sid, rid), "/api/factor/research/s%2F1/runs/r%2F2", "GET"],
    [() => f.cancelRun(sid, rid), "/api/factor/research/s%2F1/runs/r%2F2/cancel", "POST"],
    [() => f.compare(sid, [rid, "r3"]), "/api/factor/research/s%2F1/compare", "POST", { run_ids: [rid, "r3"] }],
    [() => f.backtest(sid, { run_id: rid, cost: 0 }), "/api/factor/research/s%2F1/backtest", "POST", { run_id: rid, cost: 0 }],
    [() => f.freeze(sid, [rid]), "/api/factor/research/s%2F1/freeze", "POST", { run_ids: [rid] }],
    [() => f.decide(sid, { decision: "暂缓", note: "样本不足" }), "/api/factor/research/s%2F1/decisions", "POST", { decision: "暂缓", note: "样本不足" }],
    [() => f.exportStudy(sid), "/api/factor/research/s%2F1/export", "GET"],
    [() => f.dataStatus(), "/api/factor/lab", "GET"],
    [() => f.fields(), "/api/factor/fields", "GET"],
    [() => f.buildData(), "/api/factor/build", "POST"],
  ];
  for (const [action, url, method, body] of operations) {
    assert.deepEqual(plain(await action()), { ok: true });
    const [actualUrl, options] = c.calls.at(-1);
    assert.equal(actualUrl, url); assert.equal(options.method, method);
    assert.equal(options.headers["X-VR-User-Token"], "account-A");
    assert.equal(options.headers["X-VR-Access-Key"], "access");
    assert.deepEqual(options.body ? JSON.parse(options.body) : undefined, body);
  }
});

test("access-key 401 preserves session; current session 401 clears and notifies", async () => {
  const access = client(async () => response({ detail: "缺少 API Key（MW_API_KEY）" }, 401));
  await assert.rejects(access.lab.factorLab.listStudies(), /访问鉴权/);
  assert.equal(access.cleared, 0); assert.deepEqual(access.events, []);
  const expired = client(async () => response({ detail: "登录已过期" }, 401));
  await assert.rejects(expired.lab.factorLab.listStudies(), /重新登录/);
  assert.equal(expired.cleared, 1); assert.deepEqual(expired.events, ["vr:unauthorized"]);
});

test("old account response cannot expose data or log out the new account, including slow JSON", async () => {
  for (const status of [200, 401]) {
    const json = deferred();
    const c = client(async () => ({ ok: status === 200, status, json: () => json.promise }));
    const job = c.lab.factorLab.listStudies(); await flush(); c.setToken("account-B");
    json.resolve(status === 200 ? { data: [study()] } : { detail: "登录已过期" });
    await assert.rejects(job, /会话已切换/);
    assert.equal(c.cleared, 0); assert.deepEqual(c.events, []);
  }
});

test("server errors and timeout are visible, not converted to successful records", async () => {
  const failed = client(async () => response({ detail: "输入版本不同" }, 409));
  await assert.rejects(failed.lab.factorLab.createRun("s1", {}), (e) => e.status === 409 && e.message === "输入版本不同");
  const timeout = client(async () => { const e = new Error("timeout"); e.name = "TimeoutError"; throw e; });
  await assert.rejects(timeout.lab.factorLab.listStudies(), /请求超时/);
});

test("candidates exclude wrong accounts/studies, phases, versions and non-evaluate jobs", () => {
  const runs = [run(), run({ id: "other", study_id: "s2" }), run({ id: "v", phase: "validation" }),
    run({ id: "old", input_manifest: { ...manifest, id: "old" } }), run({ id: "failed", status: "failed" }),
    run({ id: "compare", kind: "compare" }), run({ id: "backtest", kind: "backtest" })];
  assert.deepEqual(plain(lab.compareCandidates(runs, study(), "development")).map((r) => r.id), ["r1"]);
  assert.equal(lab.hasActiveJobs([run({ status: "queued" })]), true);
  assert.equal(lab.hasActiveJobs([run({ status: "failed" }), run({ status: "cancelled" })]), false);
  let selected = [];
  for (const id of ["a", "b", "c", "d", "e"]) selected = lab.toggleSelection(selected, id);
  assert.deepEqual(plain(selected), ["a", "b", "c", "d"]);
});

test("calendar dates and ordered periods are validated; copies do not mutate study config", () => {
  assert.equal(lab.validateConfig(study().config), null);
  for (const date of ["2018-02-30", "2018-13-01", "2018/01/01"]) {
    assert.ok(lab.validateConfig({ ...study().config, development: { start: date, end: "2019-12-31" } }));
  }
  assert.ok(lab.validateConfig({ ...study().config, min_days_listed: 1.5 }));
  assert.ok(lab.validateConfig({ ...study().config, horizon: 7 }));
  assert.ok(lab.validateConfig({ ...study().config, validation: { start: "2019-12-31", end: "2021-12-31" } }));
  assert.equal(lab.defaultConfig(null), null);
  const config = lab.defaultConfig({ date_min: "2005-01-03", date_max: "2026-10-10" });
  assert.equal(lab.validateConfig(config), null);
  const cloned = lab.cloneConfig(config); cloned.development.start = "2010-01-01";
  assert.equal(config.development.start, "2005-01-03");
});

test("missing evidence stays null; real zero and zero differences remain zero", () => {
  assert.equal(lab.summaryMetrics(null).rank_ic, null);
  assert.equal(lab.summaryMetrics(stats(0)).rank_ic, 0);
  assert.equal(lab.summaryMetrics(stats(0)).turnover_q5, 0);
  assert.equal(lab.metricDelta(0, 0), 0); assert.equal(lab.metricDelta(null, 0), null);
  assert.equal(lab.commonStats(analysis(stats(0)), "5", 0), null);
  assert.equal(lab.nativeStats(analysis(stats(0)), "20", 0), null);
  assert.equal(lab.holdoutText(study({ holdout_used_at: 0 })), "留出期已使用");
});

test("factor coverage is a ratio, not the average stock count", () => {
  const s = stats(0);
  assert.equal(lab.factorCoverage(s), 8 / 9);
  assert.equal(lab.summaryMetrics(s).coverage, 100);
  assert.equal(lab.factorCoverage(null), null);
  assert.equal(lab.factorCoverage({ ...s, sample: { ...s.sample, eligible_rows: 0 } }), null);
  assert.equal(lab.factorCoverage({ ...s, sample: { ...s.sample, factor_rows: 0 } }), 0);
  assert.equal(lab.factorCoverage({ ...s, sample: { ...s.sample, factor_rows: 10 } }), null);
});

test("IC chart aligns actual dates, keeps gaps and does not invent common statistics", () => {
  const result = lab.alignICSeries([stats(0, ["2020-01-01", "2020-01-03"], [0, 0.3]), stats(0, ["2020-01-02"], [-0.2]), null]);
  assert.deepEqual(plain(result), { dates: ["2020-01-01", "2020-01-02", "2020-01-03"], values: [[0, null, 0.3], [null, -0.2, null], [null, null, null]] });
});

test("retry specs preserve each job kind, source, phase, cost=0 and immutable definitions", () => {
  const evaluation = run({ status: "failed" });
  const spec = lab.retrySpec(evaluation);
  assert.equal(spec.kind, "evaluate"); assert.equal(spec.body.definition.direction, "low");
  spec.body.definition.factor = "changed"; assert.equal(evaluation.definition.factor, "mom60");
  const validation = lab.retrySpec(run({ phase: "validation", source_run_ids: ["dev"] }));
  assert.equal(validation.body.source_run_id, "dev"); assert.equal("definition" in validation.body, false);
  assert.deepEqual(plain(lab.retrySpec(run({ kind: "compare", source_run_ids: ["a", "b"] }))), { kind: "compare", runIds: ["a", "b"] });
  assert.deepEqual(plain(lab.retrySpec(run({ kind: "backtest", source_run_ids: ["a"], params: { top_n: 25, freq: "weekly", cost: 0 } }))),
    { kind: "backtest", body: { run_id: "a", top_n: 25, freq: "weekly", cost: 0 } });
});

// 执行真实 hook / 表单的转译代码，模拟生命周期、网络和时钟，不检查旧 handler 名字。
function lifecycle() {
  const slots = [], pending = [];
  let cursor = 0, renderFn, result;
  const same = (a, b) => !!a && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const hooks = {
    useState(init) { const i = cursor++; if (!(i in slots)) slots[i] = typeof init === "function" ? init() : init;
      return [slots[i], (v) => { slots[i] = typeof v === "function" ? v(slots[i]) : v; }]; },
    useRef(v) { const i = cursor++; return slots[i] ?? (slots[i] = { current: v }); },
    useCallback(fn, deps) { const i = cursor++, old = slots[i]; if (!old || !same(old.deps, deps)) slots[i] = { fn, deps }; return slots[i].fn; },
    useMemo(fn, deps) { const i = cursor++, old = slots[i]; if (!old || !same(old.deps, deps)) slots[i] = { value: fn(), deps }; return slots[i].value; },
    useEffect(fn, deps) { const i = cursor++, old = slots[i]; if (!old || !same(old.deps, deps)) pending.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; }); },
  };
  const render = (fn = renderFn) => { renderFn = fn; cursor = 0; pending.length = 0; result = fn(); pending.splice(0).forEach((effect) => effect()); return result; };
  return { hooks, render, unmount: () => slots.forEach((slot) => slot?.cleanup?.()), get value() { return result; } };
}
function research(overrides = {}, detail = { study: study(), runs: [] }) {
  const life = lifecycle(), calls = [], timers = new Map(), events = new Map();
  let token = "account-A", counter = 0;
  const api = Object.fromEntries(Object.keys(lab.factorLab).map((name) => [name, async (...args) => {
    calls.push({ name, args: plain(args) });
    if (overrides[name]) return overrides[name](...args);
    if (name === "studyDetail") return detail;
    if (name === "listStudies") return [detail.study];
    if (name === "dataStatus") return { has_data: true, factors: [{ id: "mom60", name: "动量" }] };
    if (name === "fields") return { fields: [], custom: [], examples: [] };
    if (name === "updateStudy") return { ...detail.study, ...args[1] };
    return run({ id: "new", status: "queued", ...(args[1] ?? {}) });
  }]));
  const window = {
    location: { hash: "", pathname: "/factors", search: "" },
    history: { replaceState: (_a, _b, value) => { window.location.hash = value.startsWith("#") ? value : ""; } },
    setInterval: (fn, ms) => { const id = ++counter; timers.set(id, { fn, ms }); return id; },
    clearInterval: (id) => timers.delete(id), setTimeout: (fn) => fn(),
    addEventListener: (name, fn) => { const set = events.get(name) ?? new Set(); set.add(fn); events.set(name, set); },
    removeEventListener: (name, fn) => events.get(name)?.delete(fn),
  };
  const downloaded = [];
  const exports = moduleAt("components/factors/useFactorResearch.ts", {
    react: life.hooks, "@/lib/factorLab": { ...lab, factorLab: api }, "@/lib/auth": { loadToken: () => token },
  }, { window, document: { createElement: () => ({ click() { downloaded.push(this.download); } }) } });
  life.render(() => exports.useFactorResearch());
  return {
    life, calls, timers, downloaded, render: () => life.render(), get value() { return life.value; },
    setToken: (t) => { token = t; }, emit: (name, event = {}) => events.get(name)?.forEach((fn) => fn(event)),
    tick: (ms) => [...timers.values()].filter((timer) => timer.ms === ms).forEach((timer) => timer.fn()),
    async open() { await flush(); life.render(); life.value.openStudy("s1"); await flush(); life.render(); },
  };
}

test("study create/copy and archive use server persistence without opening an archived row", async () => {
  const h = research({ createStudy: async (body) => study({ id: "copy", ...body }) });
  await flush(); h.render();
  h.value.archiveStudy(study(), true); await flush(); h.render();
  assert.equal(h.value.study, null); assert.equal(h.value.studies[0].archived, true);
  await h.value.createStudy({ name: "副本", description: "", config: lab.cloneConfig(study().config) }); await flush(); h.render();
  assert.ok(h.calls.some((c) => c.name === "createStudy" && c.args[0].name === "副本"));
  assert.equal(h.value.busy, false); h.life.unmount();
});

test("draft editing preserves completed reports and a create failure stays a rejected operation", async () => {
  const original = run({ result: analysis(stats(0)) });
  const h = research({ createRun: async () => { throw new Error("公式无效"); } }, { study: study(), runs: [original] });
  await h.open(); h.value.openRunForm(null); h.render();
  assert.equal(h.value.runs[0], original);
  await assert.rejects(h.value.createRun({ name: "新实验", phase: "development", definition: { expr: "bad", direction: "high" } }), /公式无效/);
  h.render(); assert.equal(h.value.dialog.kind, "run"); assert.equal(h.value.notice, null);
  assert.equal(h.value.error, "公式无效"); assert.equal(h.value.runs[0], original); h.life.unmount();
});

test("polling recovers persisted jobs, avoids overlapping requests and stops at terminal status", async () => {
  const poll = deferred(); let reads = 0;
  const h = research({ studyDetail: async () => ++reads === 1 ? { study: study(), runs: [run({ status: "running" })] } : poll.promise });
  await h.open(); h.tick(5000); h.tick(5000); assert.equal(reads, 2);
  poll.resolve({ study: study(), runs: [run({ status: "cancelled" })] }); await flush(); h.render();
  assert.equal(h.value.runs[0].status, "cancelled");
  assert.equal([...h.timers.values()].some((timer) => timer.ms === 5000), false); h.life.unmount();
});

test("an earlier read cannot overwrite a cancellation or a newer read", async () => {
  const old = deferred(); let reads = 0;
  const h = research({ studyDetail: async () => ++reads === 1 ? { study: study(), runs: [run({ status: "running" })] } : old.promise,
    cancelRun: async () => run({ status: "cancelled" }) });
  await h.open(); h.tick(5000); h.value.cancelRun(h.value.runs[0]); await flush(); h.render();
  old.resolve({ study: study(), runs: [run({ status: "completed" })] }); await flush(); h.render();
  assert.equal(h.value.runs[0].status, "cancelled"); h.life.unmount();
});

test("late study reads, mutations and failures are ignored after switching study or unmounting", async () => {
  for (const result of ["resolve", "reject"]) {
    const job = deferred();
    const h = research({ cancelRun: () => job.promise }); await h.open();
    h.value.cancelRun(run({ status: "running" })); h.value.openStudy("s2");
    await flush(); h.render();
    if (result === "resolve") job.resolve(run({ id: "old-account-job", status: "cancelled" }));
    else job.reject(new Error("旧错误"));
    await flush(); h.render();
    assert.equal(h.value.runs.some((r) => r.id === "old-account-job"), false);
    assert.equal(h.value.error, null); h.life.unmount();
  }
  const read = deferred(); const h = research({ studyDetail: () => read.promise });
  h.value.openStudy("s1"); h.life.unmount(); read.resolve({ study: study(), runs: [run()] }); await flush(); h.render();
  assert.equal(h.value.study, null);
});

test("session changes discard drafts and study data; same-tab changes and old exports are guarded", async () => {
  const payload = deferred(); const h = research({ exportStudy: () => payload.promise }); await h.open();
  h.value.setDecisionDraft({ decision: "暂缓", note: "私有备注" }); h.render();
  h.value.exportStudy(); h.setToken("account-B"); h.tick(1000); await flush(); h.render();
  assert.equal(h.value.study, null); assert.equal(h.value.runs.length, 0); assert.equal(h.value.decisionDraft.note, "");
  payload.resolve({ study: study(), runs: [run()], format: "market-workbench-factor-study", version: 1 }); await flush(); h.render();
  assert.deepEqual(h.downloaded, []); assert.equal(h.value.notice, null); h.life.unmount();
});

test("retry actually uses compare and backtest endpoints, retaining original failed rows", async () => {
  for (const kind of ["compare", "backtest"]) {
    const original = run({ id: "failed", kind, status: "failed", source_run_ids: ["dev", "other"], params: { top_n: 25, freq: "weekly", cost: 0 } });
    const h = research({ [kind]: async () => run({ kind, id: "retry", status: "queued" }) }, { study: study(), runs: [original] });
    await h.open(); h.value.retryRun(original); await flush(); h.render();
    assert.equal(h.calls.at(-1).name, kind); assert.equal(h.value.runs[0].status, "failed"); assert.equal(h.value.runs[1].id, "retry");
    assert.equal(h.value.view, kind === "compare" ? "compare" : "runs"); h.life.unmount();
  }
});

test("validation uses selected frozen source; holdout and holdout retry wait for confirmation", async () => {
  const frozen = study({ baseline_id: "a", freezes: [{ id: "f", run_ids: ["a", "b"], baseline_id: "a", created_at: 1 }] });
  const h = research({}, { study: frozen, runs: [run({ id: "a" }), run({ id: "b" }), run({ id: "unlocked" })] });
  await h.open(); h.value.setPhaseSourceId("b"); h.render(); h.value.startValidation(); await flush(); h.render();
  const validation = h.calls.find((c) => c.name === "createRun");
  assert.deepEqual(validation.args[1], { name: "原实验 · 验证期", phase: "validation", source_run_id: "b" });
  h.value.setDialog({ kind: "confirm", action: "holdout" }); h.render();
  assert.equal(h.calls.filter((c) => c.name === "createRun").length, 1);
  await h.value.confirmHoldout(); await flush(); h.render();
  assert.equal(h.calls.filter((c) => c.name === "createRun").at(-1).args[1].phase, "holdout");
  const before = h.calls.filter((c) => c.name === "createRun").length;
  h.value.retryRun(run({ phase: "holdout", source_run_ids: ["b"], status: "failed" })); h.render();
  assert.equal(h.value.dialog.action, "holdout"); assert.equal(h.calls.filter((c) => c.name === "createRun").length, before);
  await h.value.confirmHoldout(); await flush(); h.render();
  assert.equal(h.calls.filter((c) => c.name === "createRun").length, before + 1); h.life.unmount();
});

test("unlocked sources cannot start validation; freeze includes baseline and refuses five runs", async () => {
  const h = research({ freeze: async () => study() }, { study: study({ baseline_id: "base" }), runs: ["base", "a", "b", "c", "d"].map((id) => run({ id })) });
  await h.open(); h.value.startValidation(); await flush(); h.render();
  assert.equal(h.calls.some((c) => c.name === "createRun"), false);
  h.value.confirmFreeze(["a", "b", "c", "d"]); h.render(); assert.equal(h.calls.some((c) => c.name === "freeze"), false);
  h.value.confirmFreeze(["a"]); await flush(); h.render();
  assert.deepEqual(h.calls.find((c) => c.name === "freeze").args[1], ["a", "base"]); h.life.unmount();
});

test("comparison history remains available without selection; changing phase drops incompatible picks", async () => {
  const comparison = run({ id: "cmp", kind: "compare", source_run_ids: ["a", "b"], result: analysis() });
  const h = research({}, { study: study(), runs: [run({ id: "a" }), run({ id: "b" }), comparison] });
  await h.open(); assert.equal(h.value.compareRun.id, "cmp"); assert.equal(h.value.picks.length, 0); assert.ok(h.value.compareResult);
  h.value.toggleSelected("a"); h.render(); assert.equal(h.value.picks.length, 1);
  h.value.setComparePhase("validation"); h.render(); assert.equal(h.value.selected.length, 0); assert.equal(h.value.compareRun, null); h.life.unmount();
});

test("decision save requires a note and uses visible evidence; failure retains draft", async () => {
  const h = research({ decide: async () => { throw new Error("保存失败"); } }); await h.open();
  h.value.saveDecision(); h.render(); assert.equal(h.calls.some((c) => c.name === "decide"), false);
  h.value.setDecisionDraft({ decision: "淘汰", note: " 样本不足 " }); h.render(); h.value.saveDecision(); await flush(); h.render();
  assert.equal(h.value.decisionDraft.note, " 样本不足 "); assert.equal(h.value.notice, null);
  assert.equal(h.calls.find((c) => c.name === "decide").args[1].note, "样本不足"); h.life.unmount();
});

const panel = ({ title, children }) => React.createElement("section", null, title, children);
const icons = new Proxy({}, { get: () => () => null });
function compareModule() {
  let option;
  const shared = {
    GlassCard: panel, StatusTag: ({ status }) => React.createElement("span", null, status),
    HorizonTabs: () => null, AllHorizonTable: () => null, CorrelationMatrix: () => React.createElement("span", null, "相关性矩阵"),
    QuantileChart: ({ stats }) => React.createElement("span", null, `quantile:${stats.quantile_returns.Q5}`),
    SampleAudit: ({ stats }) => React.createElement("span", null, `audit:${stats.sample.used_rows}`),
    TurnoverPanel: () => null, YearTable: () => null,
    fmt: (v, n = 3) => v == null ? "—" : v.toFixed(n), fmtInt: (v) => v == null ? "—" : String(v), fmtPct: (v) => v == null ? "—" : `${v * 100}%`,
    useChart: (value) => { option = value; return { boxRef: null, height: "h-72" }; },
  };
  const exports = moduleAt("components/factors/CompareView.tsx", {
    "lucide-react": icons, "@/components/ui/GlassCard": { GlassCard: panel }, "@/lib/factorLab": lab,
    "@/components/factors/shared": shared, "@/lib/chartColor": { cssColor: () => "#000" },
  });
  return { exports, get option() { return option; } };
}

test("real compare render uses persisted source order, common metrics/charts and horizon-synchronized evidence", () => {
  const c = compareModule();
  const commonA = stats(0.12, ["2020-01-02"], [0.12]), commonB = stats(0, ["2020-01-02"], [0]);
  commonA.sample.used_rows = 77; commonA.quantile_returns.Q5 = 15;
  const result = { ...analysis(), native: [
    { definition: run().definition, by_horizon: { 5: stats(0.9, ["2020-01-01"], [0.9]), 20: stats(-0.7) } },
    { definition: { expr: "ret", direction: "high" }, by_horizon: { 5: stats(0.8) } },
  ], common: { by_horizon: { 5: { results: [commonA, commonB], paired_rows: 77 } } } };
  const props = { study: study({ baseline_id: "a" }), runs: [run({ id: "a", name: "基线A" }), run({ id: "b", name: "版本B" })], candidates: [], picks: [],
    compareRuns: [], compareRun: run({ source_run_ids: ["a", "b"] }), result, phase: "development", horizon: "5", sampleMode: "common" };
  let html = renderToStaticMarkup(c.exports.CompareView(props));
  assert.ok(html.indexOf("1. 基线A") < html.indexOf("2. 版本B"));
  assert.match(html, /0\.120/); assert.match(html, /-0\.120/); assert.match(html, /audit:77/); assert.match(html, /quantile:15/);
  assert.deepEqual(plain(c.option.series.map((s) => s.data)), [[0.12], [0]]);
  assert.deepEqual(plain(c.option.xAxis.data), ["2020-01-02"]);
  html = renderToStaticMarkup(c.exports.CompareView({ ...props, horizon: "20" }));
  assert.match(html, /共同样本未返回/); assert.doesNotMatch(html, /-0\.700/);
  assert.deepEqual(plain(c.option.series.map((s) => s.data)), [[], []]);
  html = renderToStaticMarkup(c.exports.CompareView({ ...props, sampleMode: "native" }));
  assert.match(html, /0\.900/); assert.doesNotMatch(html, /基线差值（RankIC）/);
});

// 表单事件直接驱动真实状态；无需增加 DOM 测试依赖。
const nodes = (root) => {
  if (root == null || typeof root !== "object") return [];
  if (Array.isArray(root)) return root.flatMap(nodes);
  return [root, ...nodes(root.props?.children)];
};
function formHarness(exportName, initialProps) {
  const life = lifecycle(); let props = initialProps;
  const shared = { inputClass: "input", StatusTag: () => null };
  const exports = moduleAt("components/factors/StudyPanels.tsx", {
    react: life.hooks, "lucide-react": icons, "@/lib/factorLab": lab, "@/components/factors/shared": shared,
  });
  const render = (next = props) => { props = next; return life.render(() => exports[exportName](props)); };
  render(); render();
  return { life, render, get nodes() { return nodes(life.value); } };
}

test("data status distinguishes stock pool, stored stocks, failures and filters", () => {
  const exports = moduleAt("components/factors/StudyPanels.tsx", {
    react: React, "lucide-react": icons, "@/lib/factorLab": lab,
    "@/components/factors/shared": { inputClass: "input", StatusTag: () => null },
  });
  const status = { has_data: true, building: false,
    catalog: { stocks: 591, universe_stocks: 598, history_stocks: 596, failed_stocks: 2, filtered_stocks: 5, min_valid_bars: 500 },
    progress: { fetched: 598, total: 598, failed: 2 },
  };
  const text = renderToStaticMarkup(React.createElement(exports.DataStatusPanel, { status, onBuild() {} })).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
  assert.match(text, /股票池 598 只/);
  assert.match(text, /入库 591 只/);
  assert.match(text, /拉取失败 2 只/);
  assert.match(text, /筛除 5 只/);
  const pending = renderToStaticMarkup(React.createElement(exports.DataStatusPanel, {
    status: { ...status, building: true, progress: { fetched: 0, total: 0, failed: 0 } }, onBuild() {},
  }));
  assert.match(pending, /正在读取股票池/);
});

test("study form can be created before data build and polling does not erase entered dates", () => {
  const f = formHarness("StudyFormDialog", { open: true, title: "新建研究", onClose() {}, onSubmit: async () => {}, status: undefined });
  assert.equal(f.nodes.filter((n) => n.type === "input" && n.props.type === "date").length, 6);
  const name = f.nodes.find((n) => n.type === "input" && n.props.maxLength === 80);
  name.props.onChange({ target: { value: "手填研究" } }); f.render();
  f.render({ open: true, title: "新建研究", onClose() {}, onSubmit: async () => {}, status: { has_data: true, catalog: { date_min: "2005-01-01", date_max: "2026-01-01" } } });
  assert.equal(f.nodes.find((n) => n.type === "input" && n.props.maxLength === 80).props.value, "手填研究"); f.life.unmount();
});

test("run form only reuses frozen sources; errors keep the dialog and expression draft intact", async () => {
  let closed = 0;
  const props = { open: true, study: study(), runs: [run()], doc: undefined, status: { factors: [{ id: "mom60" }] },
    initial: { name: "公式", phase: "development", definition: { expr: "ret", direction: "low" } },
    onClose: () => closed++, onSubmit: async () => { throw new Error("公式错误"); } };
  const f = formHarness("RunFormDialog", props);
  const validation = f.nodes.find((n) => n.type === "button" && n.props.children === "验证期");
  assert.equal(validation.props.disabled, true);
  await f.nodes.find((n) => n.type === "button" && Array.isArray(n.props.children) && n.props.children.includes("运行实验")).props.onClick();
  f.render(); assert.equal(closed, 0); assert.ok(f.nodes.some((n) => n.props?.text === "公式错误"));
  f.render({ ...props, status: { factors: [{ id: "new" }] }, initial: { ...props.initial, name: "轮询变化" } });
  assert.equal(f.nodes.find((n) => n.type === "input" && n.props.maxLength === 80).props.value, "公式"); f.life.unmount();
});

test("inline builder retains a created study when run submission fails, then retries without duplicate study", async () => {
  let attempts = 0;
  const h = research({
    createStudy: async (body) => study({ ...body, id: "builder-study" }),
    createRun: async (sid, body) => {
      assert.equal(sid, "builder-study");
      if (++attempts === 1) throw new Error("invalid expression");
      return run({ id: "builder-run", study_id: sid, ...body });
    },
  });
  await flush(); h.render();
  const body = { name: "20日动量", definition: { expr: "close / close[20] - 1", direction: "high" } };
  await assert.rejects(h.value.submitBuilder(body, study().config), /invalid expression/);
  h.render();
  assert.equal(h.value.study.id, "builder-study");
  await h.value.submitBuilder(body, study().config); h.render();
  assert.equal(h.calls.filter((call) => call.name === "createStudy").length, 1);
  assert.equal(h.value.detailRunId, "builder-run");
  assert.equal(h.value.runs[0].definition.expr, body.definition.expr);
  h.life.unmount();
});

test("templates use raw returns and std with direction applied exactly once", () => {
  const builder = moduleAt("components/factors/FactorBuilder.tsx", {
    react: React, "react-router-dom": { useBlocker: () => ({ state: "unblocked" }) },
    "@/lib/factorLab": lab, "./StudyPanels": { DefinitionEditor: () => null }, "./shared": { inputClass: "input" },
  });
  assert.deepEqual(plain(builder.templateDefinition("momentum", 60, "high")), { expr: "close / close[60] - 1", direction: "high" });
  assert.deepEqual(plain(builder.templateDefinition("reversal", 5, "low")), { expr: "close / close[5] - 1", direction: "low" });
  assert.deepEqual(plain(builder.templateDefinition("volatility", 20, "low")), { expr: "ts_std(ret,20)", direction: "low" });
  assert.deepEqual(plain(builder.templateDefinition("volume", 20, "high")), { expr: "volume / ts_mean(volume,20)", direction: "high" });
});
