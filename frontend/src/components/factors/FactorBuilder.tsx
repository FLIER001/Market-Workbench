import { useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router-dom";
import { defaultConfig, PHASES, PHASE_LABEL, validateConfig, type Definition, type StudyConfig } from "@/lib/factorLab";
import type { FactorResearch } from "./useFactorResearch";
import { DefinitionEditor } from "./StudyPanels";
import { inputClass } from "./shared";

export const BUILDER_TEMPLATES = [
  { id: "momentum", name: "价格动量", desc: "过去 N 个交易日的收盘价涨幅", field: "收盘价", direction: "high" },
  { id: "reversal", name: "短期反转", desc: "过去 N 个交易日的收盘价涨幅（默认低值优先）", field: "收盘价", direction: "low" },
  { id: "volatility", name: "低波动", desc: "过去 N 个交易日日收益率的标准差", field: "日收益率", direction: "low" },
  { id: "volume", name: "成交量相对水平", desc: "当日成交量相对最近 N 日均量（含当日）", field: "成交量", direction: "high" },
] as const;
export function templateDefinition(id: string, window: number, direction: "high" | "low"): Definition {
  return { expr: id === "volatility" ? `ts_std(ret,${window})` : id === "volume" ? `volume / ts_mean(volume,${window})` : `close / close[${window}] - 1`, direction };
}

export function FactorBuilder({ r, onDefinition, onDirty }: { r: FactorResearch; onDefinition: (value: Definition) => void; onDirty: (value: boolean) => void }) {
  const [template, setTemplate] = useState("momentum");
  const [windowSize, setWindowSize] = useState(20);
  const [mode, setMode] = useState("template");
  const [direction, setDirection] = useState<"high" | "low">("high");
  const formulaVisited = useRef(false);
  const [formula, setFormula] = useState<Definition>({ expr: "close / close[20] - 1", direction: "high" });
  const [customName, setCustomName] = useState<string | null>(null);
  const [config, setConfig] = useState<StudyConfig | null>(null);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [edited, setEdited] = useState(false);
  const latestDefinition = useRef("");
  const blocker = useBlocker(({ currentLocation, nextLocation }) => edited && nextLocation.pathname !== "/login" && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("当前草稿尚未运行，离开将丢失修改。继续离开？")) blocker.proceed();
    else blocker.reset();
  }, [blocker]);
  useEffect(() => { onDirty(edited); }, [edited, onDirty]);
  const form = useRef<HTMLFormElement>(null);
  useEffect(() => { if (error) form.current?.querySelector<HTMLElement>('[role="alert"]')?.focus(); }, [error]);
  const selected = BUILDER_TEMPLATES.find((item) => item.id === template)!;
  const definition = mode === "template" ? templateDefinition(template, windowSize, direction) : formula;
  latestDefinition.current = JSON.stringify(definition);
  const name = customName ?? (mode === "template" ? `${windowSize}日${selected.name}` : "自定义公式");
  const activeConfig = r.study?.config ?? config;
  useEffect(() => { if (!config) setConfig(defaultConfig(r.status?.catalog)); }, [r.status, config]);
  useEffect(() => { onDefinition(definition); }, [definition.expr, definition.factor, definition.direction]);
  useEffect(() => {
    if (!edited) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [edited]);
  // “继续修改”从已有不可变记录载入口径；不把任意公式反解析成模板。
  useEffect(() => {
    if (r.dialog?.kind !== "run") return;
    if (r.dialog.initial?.definition) {
      if (edited && !window.confirm("当前草稿尚未运行，载入旧版本将替换草稿。继续？")) { r.setDialog(null); return; }
      formulaVisited.current = true; setFormula({ ...r.dialog.initial.definition }); setMode("formula");
      setCustomName(r.dialog.initial.name); setEdited(true);
    }
    r.setDialog(null);
    form.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [r.dialog]);
  const change = () => { setEdited(true); setError(""); };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!activeConfig) { setError("数据区间不足以生成合法研究设置，请先准备数据或调整日期。"); return; }
    const invalid = validateConfig(activeConfig);
    if (invalid) { setError(invalid); return; }
    if (!name.trim() || !(definition.expr ?? definition.factor ?? "").trim()) { setError("请填写名称和因子口径。"); return; }
    setError("");
    const snapshot = JSON.stringify(definition);
    try {
      await r.submitBuilder({ name: name.trim(), definition }, activeConfig);
      if (snapshot === latestDefinition.current) setEdited(false);
    } catch (e) {
      const message = e instanceof Error ? e.message : "提交失败";
      const unknown = /超时|连接不到/.test(message);
      setUncertain(unknown); setError(unknown ? "提交状态待确认。请刷新并核查我的研究和版本记录，确认后再运行，避免重复提交。" : message);
      form.current?.querySelector<HTMLElement>('[role="alert"]')?.focus();
    }
  };
  return <form ref={form} onSubmit={submit} onChange={change} className="glass min-w-0 space-y-4 p-4">
    <h2 className="text-base font-semibold">构建因子</h2>
    <p className="text-xs text-muted-foreground">选模板 → 调参数 → 运行检验 · 模板不代表有效性推荐</p>
    <div className="flex flex-wrap gap-2 text-xs">
      <button type="button" aria-pressed={mode === "template"} className="rounded-lg border px-3 py-2" onClick={() => {
        if (mode !== "template" && !window.confirm("返回模板将替换当前运行草稿，公式草稿仍保留。继续？")) return;
        setMode("template"); change();
      }}>选模板</button>
      <button type="button" aria-pressed={mode === "formula"} className="rounded-lg border px-3 py-2" onClick={() => { if (!formulaVisited.current) setFormula(definition); formulaVisited.current = true; setMode("formula"); change(); }}>写公式 / 内置因子</button>
    </div>
    {mode === "template" ? <>
      <div className="grid grid-cols-2 gap-2">{BUILDER_TEMPLATES.map((item) => <button type="button" key={item.id} aria-pressed={template === item.id}
        className={`min-w-0 rounded-lg border p-3 text-left text-xs ${template === item.id ? "border-primary bg-primary/10" : "border-border/60"}`}
        onClick={() => { setTemplate(item.id); setDirection(item.direction); change(); }}>
        <span className="block font-medium">{item.name}</span><span className="mt-1 block text-[11px] text-muted-foreground">{item.desc} · 需要{item.field}</span>
      </button>)}</div>
      <label className="block space-y-1 text-xs">回看窗口（交易日）
        <input required type="number" min={template === "volatility" ? 2 : 1} max={500} step={1} value={Number.isFinite(windowSize) ? windowSize : ""} onChange={(e) => setWindowSize(e.target.valueAsNumber)} className={inputClass} />
      </label>
      <div className="flex gap-2">{[5, 20, 60].map((n) => <button type="button" className="rounded border border-border/60 px-3 py-1 text-xs" key={n} onClick={() => { setWindowSize(n); change(); }}>{n} 日</button>)}</div>
      <label className="block space-y-1 text-xs">排序方向<select className={inputClass} value={direction} onChange={(e) => setDirection(e.target.value as "high" | "low")}><option value="high">高值优先</option><option value="low">低值优先</option></select></label>
      <p className="text-xs text-muted-foreground">{selected.desc.replace("N", String(windowSize))}，{direction === "high" ? "高" : "低"}值优先。方向不随结果自动翻转。</p>
      <details className="text-xs"><summary className="cursor-pointer">查看公式</summary><code className="mt-2 block break-all">{definition.expr}</code>
        <button type="button" className="mt-2 text-primary underline" onClick={() => { if (formulaVisited.current && !window.confirm("用当前模板替换已保留的公式草稿？")) return; formulaVisited.current = true; setFormula(definition); setMode("formula"); change(); }}>编辑此公式</button></details>
    </> : <DefinitionEditor doc={r.doc} definition={formula} onChange={setFormula} status={r.status} inlinePicker />}
    <label className="block space-y-1 text-xs">因子版本名称<input required maxLength={80} value={name} onChange={(e) => setCustomName(e.target.value)} className={inputClass} /></label>
    <div className="rounded-lg border border-border/60 p-3 text-xs space-y-2">
      <p className="font-medium">{r.detailLoading ? "正在读取研究固定配置…" : r.study ? `沿用当前研究：${r.study.name}` : "首次研究设置 · 名称沿用因子版本名"}</p>
      <p>主检验周期：未来 {activeConfig?.horizon ?? 5} 个交易日 · 本次仅运行开发期</p>
      {PHASES.map((phase) => <p key={phase} className="text-muted-foreground">{PHASE_LABEL[phase]}：{activeConfig?.[phase].start || "待设置"} ~ {activeConfig?.[phase].end || "待设置"}</p>)}
      {r.study ? <p className="text-muted-foreground">周期、日期与输入版本固定；更改条件请在我的研究中复制为新研究。</p> : <details><summary className="cursor-pointer">调整研究设置（默认是初始划分）</summary>
        <div className="mt-3 space-y-3">
          <label className="block">主检验周期<select className={inputClass} value={config?.horizon ?? 5} onChange={(e) => setConfig({ ...(config ?? emptyConfig()), horizon: Number(e.target.value) as StudyConfig["horizon"] })}>{[1, 5, 10, 20].map((h) => <option value={h} key={h}>未来 {h} 个交易日</option>)}</select></label>
          <label className="block">最小上市天数<input type="number" min={0} step={1} required value={config?.min_days_listed ?? 250} onChange={(e) => setConfig({ ...(config ?? emptyConfig()), min_days_listed: e.target.valueAsNumber })} className={inputClass} /></label>
          {PHASES.map((phase) => <div key={phase} className="grid grid-cols-1 gap-2 sm:grid-cols-2">{(["start", "end"] as const).map((edge) => <label key={edge}>{PHASE_LABEL[phase]}{edge === "start" ? "开始" : "结束"}<input type="date" required value={config?.[phase][edge] ?? ""} className={inputClass} onChange={(e) => { const base = config ?? emptyConfig(); setConfig({ ...base, [phase]: { ...base[phase], [edge]: e.target.value } }); }} /></label>)}</div>)}
        </div></details>}
    </div>
    {!r.dataReady && <p className="text-xs text-warning">尚无可用日线数据，请使用上方「构建数据」。当前草稿可继续编辑。</p>}
    {error && <p tabIndex={-1} role="alert" className="text-xs text-danger">{error}</p>}
    {uncertain && <button type="button" className="text-xs underline" onClick={() => { r.refresh(); if (window.confirm("请先核查我的研究与版本记录。已确认没有重复任务？")) setUncertain(false); }}>刷新并核查提交状态</button>}
    <button disabled={r.busy || r.loading || r.detailLoading || !r.dataReady || uncertain} className="w-full rounded-lg bg-primary px-4 py-3 text-sm font-medium text-primary-foreground disabled:opacity-50">{r.busy ? "正在提交…" : r.study ? "另存版本并运行" : "运行检验"}</button>
    <p className="text-[11px] text-muted-foreground">{edited ? "草稿尚未运行。" : "每次运行另存不可变版本。"}草稿仅保留在当前页面会话。</p>
  </form>;
}
function emptyConfig(): StudyConfig {
  return { horizon: 5, min_days_listed: 250, development: { start: "", end: "" }, validation: { start: "", end: "" }, holdout: { start: "", end: "" } };
}
