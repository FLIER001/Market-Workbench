// 因子研究 V2 的弹层与面板：数据状态、研究表单、实验表单、不可撤回确认。
// 弹层用原生 <dialog>（自带焦点陷阱与 Esc 语义），按钮文案保持短动词。
import { useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, BookOpen, Database, Loader2, Play, X } from "lucide-react";
import {
  cloneConfig, defaultConfig, definitionText, validateConfig, frozenRunIds, freezeCandidates, PHASE_LABEL, KIND_LABEL,
  type Definition, type FactorFieldsDoc, type FactorLabStatus, type Horizon, type Phase, type Run, type Study,
  type StudyConfig,
} from "@/lib/factorLab";
import { inputClass, StatusTag } from "@/components/factors/shared";

// —— 原生 dialog 基础件：showModal/close 与 open 状态同步 ——
export function Modal({ open, title, titleId, onClose, children, size = "max-w-2xl" }: {
  open: boolean; title: string; titleId: string; onClose: () => void; children: ReactNode; size?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  if (!open) return null;
  return (
    <dialog ref={ref} aria-labelledby={titleId} onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={onClose}
      className={`w-[calc(100%-2rem)] ${size} rounded-2xl border border-border/60 bg-card p-0 text-foreground shadow-2xl backdrop:bg-black/50`}>
      <div className="max-h-[85vh] overflow-y-auto p-5" onClick={(event) => event.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between gap-3">
          <h3 id={titleId} className="text-sm font-semibold">{title}</h3>
          <button onClick={onClose} aria-label="关闭" className="rounded-lg p-1 text-muted-foreground hover:bg-black/20">
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </dialog>
  );
}

function FormError({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p role="alert" className="rounded-lg border border-danger/30 bg-danger/5 p-2 text-xs text-danger">{text}</p>
  );
}

// —— 数据状态：仅在没有数据或构建中时展开，其余收进折叠区 ——
export function DataStatusPanel({ status, onBuild }: { status: FactorLabStatus | undefined; onBuild: () => void }) {
  const building = !!status?.building || !!status?.fundamentals?.building;
  const progress = status?.progress;
  const pct = progress && progress.total > 0
    ? Math.min(100, Math.round((progress.fetched / progress.total) * 100)) : 0;
  return (
    <div className="glass space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Database className="h-4 w-4 text-primary" /> 研究数据
        </div>
        <span className="text-xs text-muted-foreground">
          {status == null ? "正在读取状态…" : status.has_data
            ? `${status.catalog?.stocks ?? "—"} 只 · ${status.catalog?.date_min ?? "—"} ~ ${status.catalog?.date_max ?? "—"}`
            : "尚无可用日线数据"}
        </span>
        <button onClick={onBuild} disabled={building || status == null}
          className="flex items-center gap-1.5 rounded-lg bg-primary/20 px-3 py-1.5 text-xs font-medium text-primary transition hover:bg-primary/30 disabled:opacity-50">
          {building ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          {status?.has_data ? "重建数据" : "构建数据"}
        </button>
      </div>
      {status?.catalog?.universe_stocks != null && (
        <p className="text-xs text-muted-foreground">
          股票池 {status.catalog.universe_stocks} 只 · 入库 {status.catalog.stocks} 只 ·
          拉取失败 {status.catalog.failed_stocks ?? "—"} 只 · 筛除 {status.catalog.filtered_stocks ?? "—"} 只
          {status.catalog.min_valid_bars != null && `（至少 ${status.catalog.min_valid_bars} 根有效日线）`}
        </p>
      )}
      {!status?.has_data && !building && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          先构建日线数据，再运行实验。研究可以先创建；构建后请复制研究，捕获新的输入版本。
        </p>
      )}
      {building && (
        <div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {status?.building
              ? progress?.total ? `日线：${progress.fetched} / ${progress.total} 只 · 失败 ${progress.failed}` : "正在读取股票池…"
              : status?.fundamentals?.building ? `财务：${status.fundamentals.progress.done} / ${status.fundamentals.progress.total} 个报告期` : "构建中…"}
          </p>
        </div>
      )}
      {progress?.error && <p className="text-xs text-danger">上次日线构建失败：{progress.error}</p>}
      {status?.fundamentals?.progress.error && <p className="text-xs text-danger">上次财务构建失败：{status.fundamentals.progress.error}</p>}
      {status?.fundamentals?.has_data && !building && (
        <p className="text-xs text-muted-foreground">
          财务（PIT）：{status.fundamentals.built?.stocks} 只 · {status.fundamentals.built?.report_dates} 个报告期 · {status.fundamentals.pit_note}
        </p>
      )}
    </div>
  );
}

/** 数据限制提示：始终可展开，但默认折叠，不占正文。 */
export function BiasNotes({ status }: { status: FactorLabStatus | undefined }) {
  const notes = [...(status?.biases ?? []), ...(status?.fundamentals?.biases ?? [])];
  if (notes.length === 0) return null;
  return (
    <details className="rounded-xl border border-warning/30 bg-warning/5 px-4 py-3 text-[11px] leading-relaxed text-muted-foreground">
      <summary className="cursor-pointer font-medium">数据限制</summary>
      <ul className="mt-2 space-y-1">
        {notes.map((note) => <li key={note}>{note}</li>)}
      </ul>
    </details>
  );
}

// —— 研究表单（新建 / 复制）：日期三段可编辑，默认取目录区间 ——
export function StudyFormDialog({ open, onClose, status, title, initial, onSubmit }: {
  open: boolean; onClose: () => void; status: FactorLabStatus | undefined;
  title: string; initial?: { name: string; description: string; config: StudyConfig } | null;
  onSubmit: (body: { name: string; description: string; config: StudyConfig }) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [config, setConfig] = useState<StudyConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName(initial?.name ?? "");
    setDescription(initial?.description ?? "");
    setConfig(initial ? cloneConfig(initial.config) : defaultConfig(status?.catalog ?? null) ?? {
      horizon: 5, min_days_listed: 250,
      development: { start: "", end: "" }, validation: { start: "", end: "" }, holdout: { start: "", end: "" },
    });
    setError(null);
  }, [open]);

  const period = (phase: Phase) => config?.[phase] ?? { start: "", end: "" };
  const setPeriod = (phase: Phase, key: "start" | "end", value: string) => {
    setConfig((prev) => (prev ? { ...prev, [phase]: { ...prev[phase], [key]: value } } : prev));
  };

  const submit = async () => {
    if (!config) { setError("数据区间不可用，请检查数据状态"); return; }
    if (!name.trim()) { setError("请填写研究名称"); return; }
    const invalid = validateConfig(config);
    if (invalid) { setError(invalid); return; }
    setBusy(true); setError(null);
    try {
      await onSubmit({ name: name.trim(), description: description.trim(), config });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={() => { if (!busy) onClose(); }} title={title} titleId="study-form-title">
      {config === null ? (
        <p className="text-xs text-muted-foreground">正在初始化日期。</p>
      ) : (
        <div className="space-y-4">
          <label className="block space-y-1.5 text-xs">
            <span className="text-muted-foreground">研究名称</span>
            <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} className={inputClass} placeholder="动量窗口比较" />
          </label>
          <label className="block space-y-1.5 text-xs">
            <span className="text-muted-foreground">一句话说明（可留空）</span>
            <input value={description} maxLength={200} onChange={(e) => setDescription(e.target.value)} className={inputClass} placeholder="比较不同回看窗口的预测力与换手" />
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block space-y-1.5 text-xs">
              <span className="text-muted-foreground">预测周期（研究内固定）</span>
              <select value={config.horizon} onChange={(e) => setConfig({ ...config, horizon: Number(e.target.value) as Horizon })} className={inputClass}>
                {[1, 5, 10, 20].map((h) => <option key={h} value={h}>{h} 日</option>)}
              </select>
            </label>
            <label className="block space-y-1.5 text-xs">
              <span className="text-muted-foreground">最小上市天数</span>
              <input type="number" min={0} step={1} value={Number.isFinite(config.min_days_listed) ? config.min_days_listed : ""}
                onChange={(e) => setConfig({ ...config, min_days_listed: e.target.valueAsNumber })} className={inputClass} />
            </label>
          </div>
          <div className="space-y-3">
            {(["development", "validation", "holdout"] as Phase[]).map((phase) => {
              const value = period(phase);
              const min = status?.catalog?.date_min;
              const max = status?.catalog?.date_max;
              const prevEnd = phase === "validation" ? config.development.end : phase === "holdout" ? config.validation.end : undefined;
              return (
                <div key={phase} className="rounded-xl border border-border/60 bg-background/30 p-3">
                  <p className="mb-2 text-xs font-medium">{PHASE_LABEL[phase]}</p>
                  <div className="grid grid-cols-2 gap-2">
                    <label className="space-y-1 text-[11px] text-muted-foreground">开始
                      <input type="date" value={value.start} min={min} max={value.end || max}
                        onChange={(e) => setPeriod(phase, "start", e.target.value)} className={inputClass} />
                    </label>
                    <label className="space-y-1 text-[11px] text-muted-foreground">结束
                      <input type="date" value={value.end} min={value.start || prevEnd || min} max={max}
                        onChange={(e) => setPeriod(phase, "end", e.target.value)} className={inputClass} />
                    </label>
                  </div>
                  {phase !== "development" && <p className="mt-2 text-[11px] text-muted-foreground">必须排在上一阶段结束之后（顺序且不重叠）。</p>}
                </div>
              );
            })}
          </div>
          {!status?.has_data && <p className="text-xs text-muted-foreground">可先填写日期并创建研究，运行需要已构建的输入数据。</p>}
          <FormError text={error} />
          <div className="flex justify-end gap-2">
            <button onClick={onClose} disabled={busy} className="rounded-lg border border-border/60 px-3 py-2 text-xs">取消</button>
            <button onClick={submit} disabled={busy}
              className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50">
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}保存
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

// —— 指标与函数选择器（沿用原版，公式仍是沙箱表达式，不写共享因子文件）——
function TokenPicker({ doc, open, onClose, onInsert, onFillExample, inline = false }: {
  doc: FactorFieldsDoc | undefined; open: boolean; onClose: () => void;
  onInsert: (token: string, cursorOffset?: number) => void; onFillExample: (expr: string) => void; inline?: boolean;
}) {
  const [search, setSearch] = useState("");
  const matches = (text: string) => text.toLowerCase().includes(search.toLowerCase());
  const funcs: { token: string; cursor: number; desc: string }[] = [
    { token: "ts_mean(x,20)", cursor: 8, desc: "n 日滚动均值（按个股）" },
    { token: "ts_std(x,20)", cursor: 8, desc: "n 日滚动标准差（波动）" },
    { token: "ts_sum(x,20)", cursor: 8, desc: "n 日滚动求和" },
    { token: "ts_max(x,20)", cursor: 8, desc: "n 日滚动最大值" },
    { token: "ts_min(x,20)", cursor: 8, desc: "n 日滚动最小值" },
    { token: "ts_delta(x,20)", cursor: 10, desc: "x - x 滞后 n 日" },
    { token: "ts_corr(x,y,20)", cursor: 8, desc: "x 与 y 的 n 日滚动相关" },
    { token: "ts_cov(x,y,20)", cursor: 8, desc: "x 与 y 的 n 日滚动协方差" },
    { token: "ts_rank(x,20)", cursor: 8, desc: "当日值在最近 n 日的分位" },
    { token: "ts_skew(x,20)", cursor: 8, desc: "n 日滚动偏度" },
    { token: "ts_kurt(x,20)", cursor: 8, desc: "n 日滚动超额峰度" },
    { token: "ts_rsv(x,20)", cursor: 8, desc: "(x-n 日低)/(n 日高-低)" },
    { token: "close[5]", cursor: 6, desc: "滞后写法（此例 = 5 日前收盘）" },
    { token: "cs_rank(x)", cursor: 8, desc: "按日横截面排名" },
    { token: "cs_zscore(x)", cursor: 10, desc: "按日横截面标准化" },
    { token: "abs(x)", cursor: 4, desc: "绝对值" },
    { token: "log(x)", cursor: 4, desc: "自然对数（非正数为缺失）" },
    { token: "sqrt(x)", cursor: 5, desc: "平方根（非正数为缺失）" },
  ];
  if (!open || !doc) return null;
  const content = <>
      <label className="block mb-3 text-xs">搜索字段与函数<input className={inputClass} value={search} onChange={(e) => setSearch(e.target.value)} /></label>
      <p className="mb-1 text-xs font-semibold text-foreground/80">价量字段（日线，前复权）</p>
      <div className="mb-4 grid grid-cols-2 gap-1.5 md:grid-cols-4">
        {doc.fields.filter((f) => !f.name.startsWith("fn_") && matches(f.name + f.desc)).map((f) => (
          <button key={f.name} onClick={() => onInsert(f.name)}
            className="rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
            <span className="block font-mono text-xs font-semibold">{f.name}</span>
            <span className="block text-[10px] leading-tight text-muted-foreground">{f.desc}</span>
          </button>
        ))}
      </div>
      <p className="mb-1 text-xs font-semibold text-foreground/80">财务字段（PIT：按公告日对齐）</p>
      <div className="mb-4 grid grid-cols-2 gap-1.5 md:grid-cols-4">
        {doc.fields.filter((f) => f.name.startsWith("fn_") && matches(f.name + f.desc)).map((f) => (
          <button key={f.name} onClick={() => onInsert(f.name)}
            className="rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
            <span className="block font-mono text-xs font-semibold">{f.name}</span>
            <span className="block text-[10px] leading-tight text-muted-foreground">{f.desc}</span>
          </button>
        ))}
      </div>
      <p className="mb-1 text-xs font-semibold text-foreground/80">时序 / 截面函数</p>
      <div className="mb-4 grid grid-cols-2 gap-1.5 md:grid-cols-3">
        {funcs.filter((f) => matches(f.token + f.desc)).map((f) => (
          <button key={f.token} onClick={() => onInsert(f.token, f.cursor)}
            className="rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
            <span className="block font-mono text-xs font-semibold">{f.token}</span>
            <span className="block text-[10px] leading-tight text-muted-foreground">{f.desc}</span>
          </button>
        ))}
      </div>
      <p className="mb-1 text-xs font-semibold text-foreground/80">示例公式</p>
      <div className="space-y-1.5">
        {(doc.examples ?? []).filter((x) => matches(x.expr + x.desc)).map((x) => (
          <button key={x.expr} onClick={() => onFillExample(x.expr)}
            className="w-full rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
            <span className="block font-mono text-xs">{x.expr}</span>
            <span className="block text-[10px] text-muted-foreground">{x.desc}</span>
          </button>
        ))}
      </div>
      <p className="mt-4 text-[10px] leading-relaxed text-muted-foreground">
        语法：四则运算 + - * /、负号、括号；x[n] 表示滞后 n 日；窗口 n 为 1-500 的整数。公式只用 T 日及以前数据。
      </p>
    </>;
  return inline ? <div className="min-w-0 rounded-lg border border-border/60 p-3">{content}</div>
    : <Modal open={open} onClose={onClose} title="可用指标与函数" titleId="factor-token-title">{content}</Modal>;
}

/** 因子口径编辑：内置因子 / 沙箱公式 / 旧自定义因子快照。 */
export function DefinitionEditor({ doc, definition, onChange, status, readOnly, inlinePicker = false }: {
  doc: FactorFieldsDoc | undefined; definition: Definition; onChange: (next: Definition) => void;
  status: FactorLabStatus | undefined; readOnly?: boolean; inlinePicker?: boolean;
}) {
  const [picker, setPicker] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const mode: "factor" | "expr" = "factor" in definition ? "factor" : "expr";
  const expr = definition.expr ?? "";

  const insert = (token: string, cursorOffset?: number) => {
    const ta = taRef.current;
    if (!inlinePicker) setPicker(false);
    if (!ta) { onChange({ expr: expr + token, direction: definition.direction }); return; }
    const start = ta.selectionStart ?? expr.length;
    const end = ta.selectionEnd ?? expr.length;
    onChange({ expr: expr.slice(0, start) + token + expr.slice(end), direction: definition.direction });
    requestAnimationFrame(() => {
      ta.focus();
      const pos = start + (cursorOffset ?? token.length);
      ta.setSelectionRange(pos, pos);
    });
  };

  if (readOnly) {
    return (
      <div className="space-y-2 rounded-xl border border-border/60 bg-background/30 p-3 text-xs">
        <p className="font-mono">{definitionText(definition)}</p>
        <p className="text-muted-foreground">方向：{definition.direction === "low" ? "低值优先" : "高值优先"} · 沿用来源实验口径，只读。</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <TokenPicker inline={inlinePicker} doc={doc} open={picker} onClose={() => setPicker(false)} onInsert={insert}
        onFillExample={(value) => { onChange({ expr: value, direction: definition.direction }); setPicker(false); }} />
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted-foreground">口径</span>
        <div className="flex rounded-lg border border-border/60 p-0.5">
          <button type="button" aria-pressed={mode === "factor"}
            onClick={() => onChange({ factor: status?.factors?.[0]?.id ?? "", direction: definition.direction })}
            className={`rounded px-2.5 py-1 ${mode === "factor" ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}>内置因子</button>
          <button type="button" aria-pressed={mode === "expr"}
            onClick={() => onChange({ expr, direction: definition.direction })}
            className={`rounded px-2.5 py-1 ${mode === "expr" ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}>公式</button>
        </div>
        <button type="button" onClick={() => setPicker(true)} disabled={!doc}
          className="flex items-center gap-1 rounded-lg border border-border/60 px-2 py-1 text-[11px] text-muted-foreground hover:border-primary/50 hover:text-primary">
          <BookOpen className="h-3 w-3" /> 指标与函数
        </button>
      </div>
      {mode === "factor" ? (
        <label className="block space-y-1.5 text-xs">
          <span className="text-muted-foreground">内置因子</span>
          <select value={definition.factor ?? ""} onChange={(e) => onChange({ factor: e.target.value, direction: definition.direction })} className={inputClass}>
            <option value="" disabled>选择因子</option>
            <optgroup label="价量因子">
              {(status?.factors ?? []).map((f) => <option key={f.id} value={f.id}>{f.id} · {f.name}</option>)}
            </optgroup>
            {!!status?.fin_factors?.length && (
              <optgroup label="财务因子（公告日对齐）">
                {status.fin_factors.map((f) => (
                  <option key={f.id} value={f.id} disabled={!status.fundamentals?.has_data}>{f.id} · {f.name}</option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
      ) : (
        <label className="block space-y-1.5 text-xs">
          <span className="text-muted-foreground">公式（沙箱表达式，本次实验专用快照）</span>
          <textarea ref={taRef} rows={3} value={expr} onChange={(e) => onChange({ expr: e.target.value, direction: definition.direction })}
            placeholder="-cs_zscore(ts_std(ret,20))" className={`${inputClass} font-mono`} />
        </label>
      )}
      <label className="block space-y-1.5 text-xs">
        <span className="text-muted-foreground">方向</span>
        <select value={definition.direction} onChange={(e) => onChange({ ...definition, direction: e.target.value as "high" | "low" })} className={inputClass}>
          <option value="high">高值优先</option>
          <option value="low">低值优先</option>
        </select>
      </label>
      {mode === "factor" && !status?.fundamentals?.has_data && <p className="text-xs text-warning">财务模板需要公告日对齐的 PIT 财务数据，目前缺少该数据。请展开上方研究数据查看构建状态。</p>}
      <details className="text-[11px] text-muted-foreground"><summary className="cursor-pointer">方向说明</summary>方向只在这里设定，不根据结果自动翻向。</details>
      {!!doc?.custom?.length && (
        <details className="rounded-lg border border-border/60 bg-background/30 p-2 text-xs">
          <summary className="cursor-pointer text-muted-foreground">沿用旧自定义因子的公式</summary>
          <div className="mt-2 space-y-1">
            {doc.custom.map((c) => (
              <button key={c.id} type="button" onClick={() => onChange({ expr: c.expr, direction: definition.direction })}
                className="block w-full rounded border border-border/50 p-1.5 text-left hover:border-primary/50">
                <span className="font-mono text-[11px]">{c.expr}</span>
                <span className="ml-2 text-[10px] text-muted-foreground">{c.name}</span>
              </button>
            ))}
          </div>
          <p className="mt-2 text-[10px] text-muted-foreground">只复制公式到本次实验，不修改共享因子库。</p>
        </details>
      )}
    </div>
  );
}

// —— 实验表单（新建 / 复制）：开发期编辑口径，验证期复用已完成的开发期实验 ——
export function RunFormDialog({ open, onClose, study, runs, doc, status, initial, onSubmit }: {
  open: boolean; onClose: () => void; study: Study; runs: Run[]; doc: FactorFieldsDoc | undefined;
  status: FactorLabStatus | undefined;
  initial?: { name: string; definition: Definition | null; phase: Phase; source_run_id?: string } | null;
  onSubmit: (body: { name: string; definition?: Definition; phase: Phase; source_run_id?: string }) => Promise<void>;
}) {
  const frozen = frozenRunIds(study);
  const sources = freezeCandidates(runs, study).filter((run) => frozen.includes(run.id));
  const [name, setName] = useState("");
  const [phase, setPhase] = useState<Phase>("development");
  const [sourceId, setSourceId] = useState("");
  const [definition, setDefinition] = useState<Definition>({ factor: "", direction: "high" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    const sourceRun = initial?.source_run_id ? sources.find((run) => run.id === initial.source_run_id) : undefined;
    const initialPhase = initial?.phase === "validation" ? "validation" : "development";
    setPhase(initialPhase);
    setName(initial?.name ?? "");
    setDefinition(initial?.definition ?? sourceRun?.definition ?? { factor: status?.factors?.[0]?.id ?? "", direction: "high" });
    setSourceId(initial?.source_run_id ?? sources[0]?.id ?? "");
    setError(null);
    // sources 随轮询变化，只在打开时取值，避免编辑中被重置。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const sourceRun = sources.find((run) => run.id === sourceId) ?? null;
  const reusable = sources.length > 0;

  const submit = async () => {
    if (!name.trim()) { setError("请填写实验名称"); return; }
    if (phase === "development") {
      if ("factor" in definition ? !definition.factor?.trim() : !definition.expr?.trim()) {
        setError("factor" in definition ? "请选择内置因子" : "公式为空");
        return;
      }
    } else if (!sourceRun) {
      setError("请先锁定开发期实验");
      return;
    }
    setBusy(true); setError(null);
    try {
      await onSubmit(phase === "development"
        ? { name: name.trim(), definition, phase }
        : { name: name.trim(), phase, source_run_id: sourceId });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建实验失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={() => { if (!busy) onClose(); }} title={initial ? "复制实验" : "新建实验"} titleId="run-form-title">
      <div className="space-y-4">
        <label className="block space-y-1.5 text-xs">
          <span className="text-muted-foreground">实验名称</span>
          <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} className={inputClass} placeholder="60 日动量" />
        </label>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">阶段</span>
          <div className="flex rounded-lg border border-border/60 p-0.5">
            <button type="button" aria-pressed={phase === "development"} onClick={() => setPhase("development")}
              className={`rounded px-2.5 py-1 ${phase === "development" ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}>开发期</button>
            <button type="button" aria-pressed={phase === "validation"} disabled={!reusable}
              onClick={() => setPhase("validation")} title={reusable ? "" : "请先锁定开发期实验"}
              className={`rounded px-2.5 py-1 disabled:opacity-40 ${phase === "validation" ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}>验证期</button>
          </div>
          <span className="text-muted-foreground">预测周期 {study.config.horizon} 日（研究内固定）</span>
        </div>
        {phase === "development" ? (
          <DefinitionEditor doc={doc} definition={definition} onChange={setDefinition} status={status} />
        ) : (
          <div className="space-y-2">
            <label className="block space-y-1.5 text-xs">
              <span className="text-muted-foreground">复用的开发期实验</span>
              <select value={sourceId} onChange={(e) => setSourceId(e.target.value)} className={inputClass}>
                {sources.map((run) => <option key={run.id} value={run.id}>{run.name}</option>)}
              </select>
            </label>
            {sourceRun && <DefinitionEditor doc={doc} definition={sourceRun.definition ?? { direction: "high" }} onChange={() => {}} status={status} readOnly />}
            <p className="text-[11px] text-muted-foreground">验证期沿用来源实验的口径与方向，不能在这里修改。</p>
          </div>
        )}
        <FormError text={error} />
        <div className="flex justify-end gap-2">
          <button onClick={onClose} disabled={busy} className="rounded-lg border border-border/60 px-3 py-2 text-xs">取消</button>
          <button onClick={submit} disabled={busy}
            className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50">
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}运行实验
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 不可撤回操作（留出期查看 / 锁定）的确认弹层。 */
export function ConfirmDialog({ open, onClose, title, confirmLabel, onConfirm, busy, children, danger }: {
  open: boolean; onClose: () => void; title: string; confirmLabel: string;
  onConfirm: () => void; busy: boolean; children: ReactNode; danger?: boolean;
}) {
  return (
    <Modal open={open} onClose={() => { if (!busy) onClose(); }} title={title} titleId="confirm-title" size="max-w-lg">
      <div className="space-y-4">
        <div className="flex gap-2 text-xs leading-relaxed">
          {danger && <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />}
          <div className="space-y-2 text-muted-foreground">{children}</div>
        </div>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} disabled={busy} className="rounded-lg border border-border/60 px-3 py-2 text-xs">取消</button>
          <button onClick={onConfirm} disabled={busy}
            className={`flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium disabled:opacity-50 ${danger ? "bg-warning/25 text-warning" : "bg-primary text-primary-foreground"}`}>
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}{confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 运行历史行（实验详情与结论页共用的小件）。 */
export function RunRefList({ runs, highlight, onOpen }: { runs: Run[]; highlight?: string[]; onOpen?: (id: string) => void }) {
  if (runs.length === 0) return <p className="text-xs text-muted-foreground">无关联实验。</p>;
  return (
    <ul className="space-y-1 text-xs">
      {runs.map((run) => (
        <li key={run.id} className={`flex flex-wrap items-center gap-2 rounded-lg border border-border/50 px-2 py-1.5 ${highlight?.includes(run.id) ? "bg-primary/5" : ""}`}>
          <StatusTag status={run.status} />
          {onOpen ? <button onClick={() => onOpen(run.id)} className="font-medium text-primary underline">{run.name}</button> : <span className="font-medium">{run.name}</span>}
          <span className="text-muted-foreground">{KIND_LABEL[run.kind]} · {PHASE_LABEL[run.phase]}</span>
          <span className="font-mono text-[10px] text-muted-foreground">{definitionText(run.definition)}</span>
          {run.status === "failed" && run.error && <span className="text-danger">{run.error}</span>}
        </li>
      ))}
    </ul>
  );
}
