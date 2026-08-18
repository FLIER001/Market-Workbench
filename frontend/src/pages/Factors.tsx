import { useCallback, useEffect, useRef, useState } from "react";
import { BarChart, LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { init, use, type ECharts } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import {
  AlertTriangle, BookOpen, Database, FlaskConical, Loader2, Pencil, Play,
  RefreshCw, Trash2, X,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import {
  api, type FactorBacktestData, type FactorEvaluateData, type FactorFieldsDoc,
  type FactorLabStatus,
} from "@/lib/api";

use([LineChart, BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

// 因子实验室：横截面选股因子的检验（Alphalens 口径）与探索性组合回测。
// 数据为探索级（幸存者偏差/前复权/无 point-in-time 状态），结果仅作研究证据。

const cssColor = (name: string, alpha?: string) => {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return alpha == null ? `hsl(${v})` : `hsl(${v} / ${alpha})`;
};

function useChart(option: object | null, height = "h-56") {
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);
  useEffect(() => {
    if (!boxRef.current || !option) return;
    const chart = chartRef.current ?? init(boxRef.current);
    chartRef.current = chart;
    chart.setOption(option as never, true);
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [option]);
  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);
  return { boxRef, height };
}

function fmt(v: number | null | undefined, digits = 3): string {
  return v == null ? "—" : v.toFixed(digits);
}

function fmtPct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

// —— 数据状态卡（构建进度 + 偏差提示）——
function DataPanel({ lab, onBuild }: { lab: FactorLabStatus | undefined; onBuild: () => void }) {
  const building = lab?.building;
  const pct = lab && lab.progress.total > 0
    ? Math.min(100, Math.round((lab.progress.fetched / lab.progress.total) * 100)) : 0;
  return (
    <GlassCard>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Database className="h-4 w-4 text-primary" /> 日线数据
        </div>
        {lab?.has_data ? (
          <span className="text-xs text-muted-foreground">
            {lab.catalog?.stocks} 只 · {lab.catalog?.date_min} ~ {lab.catalog?.date_max} · 构建于 {lab.catalog?.built_at}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">未构建</span>
        )}
        <button
          onClick={onBuild}
          disabled={building}
          className="flex items-center gap-1.5 rounded-lg bg-primary/20 px-3 py-1.5 text-xs font-medium text-primary transition hover:bg-primary/30 disabled:opacity-50"
        >
          {building ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          {lab?.has_data ? "重建数据" : "构建数据"}
        </button>
      </div>
      {building && (
        <div className="mt-3">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            阶段 1/2 日线：{lab?.progress.fetched} / {lab?.progress.total} 只（失败 {lab.progress.failed}）· 约 20-40 分钟；
            之后阶段 2/2 财务（2006-今逐报告期，约 10-20 分钟）
          </p>
        </div>
      )}
      {lab?.fundamentals?.building && !building && (
        <p className="mt-2 text-xs text-muted-foreground">
          阶段 2/2 财务：{lab.fundamentals.progress.done} / {lab.fundamentals.progress.total} 个报告期 ·
          已累计 {lab.fundamentals.progress.rows} 行
        </p>
      )}
      {lab?.fundamentals?.has_data && !lab.fundamentals.building && (
        <p className="mt-2 text-xs text-muted-foreground">
          财务（PIT）：{lab.fundamentals.built?.stocks} 只 · {lab.fundamentals.built?.report_dates} 个报告期 ·
          公告日 {lab.fundamentals.built?.notice_date_min} ~ {lab.fundamentals.built?.notice_date_max} · {lab.fundamentals.pit_note}
        </p>
      )}
      {lab?.progress.error && (
        <p className="mt-2 text-xs text-danger">上次日线构建失败：{lab.progress.error}</p>
      )}
      {lab?.fundamentals?.progress.error && (
        <p className="mt-2 text-xs text-danger">上次财务构建失败：{lab.fundamentals.progress.error}</p>
      )}
    </GlassCard>
  );
}

// —— 检验结果：指标 + IC 时序 + 分组收益 ——
function EvaluatePanel({ data }: { data: FactorEvaluateData }) {
  const ic = useChart({
    grid: { left: 48, right: 12, top: 24, bottom: 28 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: data.ic_series.dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: [{
      type: "bar", data: data.ic_series.values, name: "RankIC",
      itemStyle: {
        color: (p: { value: number }) =>
          p.value >= 0 ? cssColor("--success", "0.7") : cssColor("--danger", "0.7"),
      },
    }],
    dataZoom: [{ type: "inside" }],
  });
  const qs = ["Q1", "Q2", "Q3", "Q4", "Q5"];
  const qchart = useChart({
    grid: { left: 48, right: 12, top: 24, bottom: 24 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: qs, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: [{
      type: "bar", name: "日均收益(bp)",
      data: qs.map((q) => data.quantile_returns?.[q] ?? null),
      itemStyle: {
        color: (p: { value: number | null }) =>
          (p.value ?? 0) >= 0 ? cssColor("--success", "0.7") : cssColor("--danger", "0.7"),
      },
    }],
  });
  const icRows = [
    ["RankIC 均值", fmt(data.ic.rank_ic_mean)],
    ["RankIC IR", fmt(data.ic.rank_ic_ir)],
    ["IC 均值", fmt(data.ic.ic_mean)],
    ["正 IC 比例", fmtPct(data.ic.rank_ic_positive_ratio)],
    ["Q5-Q1 日均", `${fmt(data.long_short_bp, 1)} bp`],
    ["Q5 超额日均", `${fmt(data.long_excess_bp, 1)} bp`],
    ["因子秩自相关", fmt(data.rank_autocorr)],
    ["样本天数", String(data.n_days)],
  ];
  const decayRows = Object.entries(data.ic_decay ?? {});
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {icRows.map(([k, v]) => (
          <div key={k} className="rounded-xl border border-border/60 bg-background/30 p-3">
            <p className="text-[11px] text-muted-foreground">{k}</p>
            <p className="mt-1 text-sm font-bold tabular-nums">{v}</p>
          </div>
        ))}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">RankIC 时序</h3>
          <div ref={ic.boxRef} className={`${ic.height} w-full`} />
        </GlassCard>
        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">五分组日均收益（bp，Q5 = 因子值最大）</h3>
          <div ref={qchart.boxRef} className={`${qchart.height} w-full`} />
        </GlassCard>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">IC 衰减（不同前瞻期 RankIC）</h3>
          <table className="w-full text-xs tabular-nums">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr><th className="py-1 text-left">前瞻期</th><th className="text-right">RankIC</th><th className="text-right">IR</th></tr>
            </thead>
            <tbody>
              {decayRows.map(([h, r]) => (
                <tr key={h} className="border-b border-border/30">
                  <td className="py-1">{h} 日</td>
                  <td className={r.rank_ic_mean >= 0 ? "text-right text-success" : "text-right text-danger"}>{fmt(r.rank_ic_mean)}</td>
                  <td className="text-right">{fmt(r.rank_ic_ir)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </GlassCard>
        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">分年 RankIC</h3>
          <table className="w-full text-xs tabular-nums">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr><th className="py-1 text-left">年份</th><th className="text-right">RankIC</th><th className="text-right">IR</th><th className="text-right">天数</th></tr>
            </thead>
            <tbody>
              {Object.entries(data.by_year ?? {}).map(([y, r]) => (
                <tr key={y} className="border-b border-border/30">
                  <td className="py-1">{y}</td>
                  <td className={r.rank_ic_mean >= 0 ? "text-right text-success" : "text-right text-danger"}>{fmt(r.rank_ic_mean)}</td>
                  <td className="text-right">{fmt(r.rank_ic_ir)}</td>
                  <td className="text-right">{r.days}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </GlassCard>
      </div>
    </div>
  );
}

// —— 回测结果：净值曲线 + 指标 + 成本压力 ——
function BacktestPanel({ data }: { data: FactorBacktestData }) {
  const nav = useChart({
    grid: { left: 48, right: 12, top: 28, bottom: 28 },
    tooltip: { trigger: "axis" },
    legend: { top: 0, textStyle: { fontSize: 10 }, data: ["策略净值", "基准（全池等权）"] },
    xAxis: { type: "category", data: data.nav.dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: [
      { type: "line", name: "策略净值", data: data.nav.strategy, showSymbol: false,
        lineStyle: { width: 1.6, color: cssColor("--primary") },
        areaStyle: { color: cssColor("--primary", "0.10") } },
      { type: "line", name: "基准（全池等权）", data: data.nav.benchmark, showSymbol: false,
        lineStyle: { width: 1.2, color: cssColor("--muted-foreground", "0.8") } },
    ],
    dataZoom: [{ type: "inside" }],
  }, "h-64");
  const m = data.metrics;
  const rows: [string, string][] = [
    ["区间收益", fmtPct(m.total_return)], ["年化收益", fmtPct(m.ann_return)],
    ["年化波动", fmtPct(m.ann_vol)], ["夏普", fmt(m.sharpe)],
    ["最大回撤", fmtPct(m.max_drawdown)], ["日胜率", fmtPct(m.win_rate)],
    ["年化单边换手", fmt(m.ann_turnover, 1)], ["交易日数", String(m.n_days)],
  ];
  return (
    <div className="space-y-4">
      <div ref={nav.boxRef} className={`${nav.height} w-full rounded-xl border border-border/40 bg-background/20 p-1`} />
      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">组合指标</h3>
          <div className="grid grid-cols-2 gap-2">
            {rows.map(([k, v]) => (
              <div key={k} className="rounded-xl border border-border/60 bg-background/30 p-2.5">
                <p className="text-[11px] text-muted-foreground">{k}</p>
                <p className="text-sm font-bold tabular-nums">{v}</p>
              </div>
            ))}
          </div>
        </GlassCard>
        <GlassCard>
          <h3 className="mb-2 text-sm font-semibold">成本压力测试（区间收益）</h3>
          <table className="w-full text-xs tabular-nums">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr><th className="py-1 text-left">成本倍数</th><th className="text-right">区间收益</th><th className="text-right">年化</th><th className="text-right">最大回撤</th></tr>
            </thead>
            <tbody>
              {Object.entries(data.cost_stress ?? {}).map(([k, r]) => (
                <tr key={k} className={k === `${data.params.cost_multiplier}x` ? "border-b border-border/30 bg-primary/5" : "border-b border-border/30"}>
                  <td className="py-1">{k}{k === `${data.params.cost_multiplier}x` ? "（当前）" : ""}</td>
                  <td className="text-right">{fmtPct(r.total_return)}</td>
                  <td className="text-right">{fmtPct(r.ann_return)}</td>
                  <td className="text-right">{fmtPct(r.max_drawdown)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3 className="mb-2 mt-4 text-sm font-semibold">分年收益</h3>
          <div className="flex flex-wrap gap-2">
            {Object.entries(data.yearly_returns ?? {}).map(([y, r]) => (
              <div key={y} className="rounded-lg border border-border/60 bg-background/30 px-2.5 py-1.5">
                <span className="text-[11px] text-muted-foreground">{y}</span>
                <span className={`ml-2 text-xs font-bold tabular-nums ${r >= 0 ? "text-success" : "text-danger"}`}>
                  {fmtPct(r)}
                </span>
              </div>
            ))}
          </div>
        </GlassCard>
      </div>
    </div>
  );
}

// —— 因子构建（独立子板块）：公式编辑 + 字段/函数选择器 + 已存因子管理 ———

// 指令选择弹层：字段 / 函数 / 示例三组，点击即插入公式光标处
function TokenPicker({
  doc, open, onClose, onInsert, onFillExample,
}: {
  doc: FactorFieldsDoc | undefined; open: boolean; onClose: () => void;
  onInsert: (token: string, cursorOffset?: number) => void;
  onFillExample: (expr: string) => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open || !doc) return null;
  // 函数名带参数骨架，插入后光标落在括号内
  const funcs: { token: string; cursor: number; desc: string }[] = [
    { token: "ts_mean(x,20)", cursor: 8, desc: "n 日滚动均值（按个股）" },
    { token: "ts_std(x,20)", cursor: 8, desc: "n 日滚动标准差（波动）" },
    { token: "ts_sum(x,20)", cursor: 8, desc: "n 日滚动求和" },
    { token: "ts_max(x,20)", cursor: 8, desc: "n 日滚动最大值" },
    { token: "ts_min(x,20)", cursor: 8, desc: "n 日滚动最小值" },
    { token: "ts_delta(x,20)", cursor: 10, desc: "x - x 滞后 n 日（n 日变化）" },
    { token: "ts_corr(x,y,20)", cursor: 8, desc: "x 与 y 的 n 日滚动相关" },
    { token: "ts_cov(x,y,20)", cursor: 8, desc: "x 与 y 的 n 日滚动协方差" },
    { token: "ts_rank(x,20)", cursor: 8, desc: "当日值在最近 n 日的分位(0-1)" },
    { token: "ts_skew(x,20)", cursor: 8, desc: "n 日滚动偏度（分布左/右偏）" },
    { token: "ts_kurt(x,20)", cursor: 8, desc: "n 日滚动超额峰度（尾部厚度）" },
    { token: "ts_rsv(x,20)", cursor: 8, desc: "(x-n日低)/(n日高-低)，KDJ 的 RSV" },
    { token: "close[5]", cursor: 6, desc: "滞后写法（此例 = 5 日前收盘）" },
    { token: "cs_rank(x)", cursor: 8, desc: "按日横截面排名" },
    { token: "cs_zscore(x)", cursor: 10, desc: "按日横截面标准化" },
    { token: "abs(x)", cursor: 4, desc: "绝对值" },
    { token: "log(x)", cursor: 4, desc: "自然对数（非正数为缺失）" },
    { token: "sign(x)", cursor: 5, desc: "符号" },
    { token: "sqrt(x)", cursor: 5, desc: "平方根（非正数为缺失）" },
  ];
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}>
      <div className="max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-border/60 bg-card p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <BookOpen className="h-4 w-4 text-primary" /> 可用指标与函数
          </h3>
          <button onClick={onClose} className="rounded-lg p-1 text-muted-foreground hover:bg-black/20">
            <X className="h-4 w-4" />
          </button>
        </div>
        <p className="mb-1 text-xs font-semibold text-foreground/80">价量字段（日线，前复权）</p>
        <div className="mb-1.5 text-[10px] text-muted-foreground">
          覆盖价格/成交/涨跌/K 线形态/多周期动量与量比。
        </div>
        <div className="mb-4 grid grid-cols-2 gap-1.5 md:grid-cols-4">
          {doc.fields.filter((f) => !f.name.startsWith("fn_")).map((f) => (
            <button key={f.name} onClick={() => onInsert(f.name)}
              className="rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
              <span className="block font-mono text-xs font-semibold">{f.name}</span>
              <span className="block text-[10px] leading-tight text-muted-foreground">{f.desc}</span>
            </button>
          ))}
        </div>
        <p className="mb-1 text-xs font-semibold text-foreground/80">财务字段（PIT：按公告日对齐，2006-今）</p>
        <div className="mb-1.5 text-[10px] text-muted-foreground">
          每个交易日取「实际公告日 ≤ 当日」的最新报告期数据——财报公告前不可见，杜绝前视偏差。需先构建财务数据（构建按钮含两阶段）。
        </div>
        <div className="mb-4 grid grid-cols-2 gap-1.5 md:grid-cols-4">
          {doc.fields.filter((f) => f.name.startsWith("fn_")).map((f) => (
            <button key={f.name} onClick={() => onInsert(f.name)}
              className="rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
              <span className="block font-mono text-xs font-semibold">{f.name}</span>
              <span className="block text-[10px] leading-tight text-muted-foreground">{f.desc}</span>
            </button>
          ))}
        </div>
        <p className="mb-1 text-xs font-semibold text-foreground/80">时序 / 截面函数</p>
        <div className="mb-4 grid grid-cols-2 gap-1.5 md:grid-cols-3">
          {funcs.map((f) => (
            <button key={f.token} onClick={() => onInsert(f.token, f.cursor)}
              className="rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
              <span className="block font-mono text-xs font-semibold">{f.token}</span>
              <span className="block text-[10px] leading-tight text-muted-foreground">{f.desc}</span>
            </button>
          ))}
        </div>
        <p className="mb-1 text-xs font-semibold text-foreground/80">示例公式（点击整条填入，再改参数）</p>
        <div className="space-y-1.5">
          {doc.examples.map((x) => (
            <button key={x.expr} onClick={() => onFillExample(x.expr)}
              className="w-full rounded-lg border border-border/60 bg-background/40 p-2 text-left transition hover:border-primary/50 hover:bg-primary/10">
              <span className="block font-mono text-xs">{x.expr}</span>
              <span className="block text-[10px] text-muted-foreground">{x.desc}</span>
            </button>
          ))}
        </div>
        <p className="mt-4 text-[10px] leading-relaxed text-muted-foreground">
          语法：四则运算 + - * /、负号、括号；x[n] 表示滞后 n 日；窗口 n 为 1-500 的整数。
          公式只用 T 日及以前数据，T+1 起配前瞻收益。
        </p>
      </div>
    </div>
  );
}

function BuilderPanel({
  doc, onSaved, onUseFactor, hasData,
}: {
  doc: FactorFieldsDoc | undefined; onSaved: () => void;
  onUseFactor: (fid: string) => void; hasData: boolean;
}) {
  const [fid, setFid] = useState("");
  const [name, setName] = useState("");
  const [expr, setExpr] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // 在光标处插入 token（函数骨架光标落在括号内第一个参数处）
  const insert = (token: string, cursorOffset?: number) => {
    const ta = taRef.current;
    if (!ta) { setExpr(expr + token); return; }
    const start = ta.selectionStart ?? expr.length;
    const end = ta.selectionEnd ?? expr.length;
    const next = expr.slice(0, start) + token + expr.slice(end);
    setExpr(next);
    setPickerOpen(false);
    requestAnimationFrame(() => {
      ta.focus();
      const pos = start + (cursorOffset ?? token.length);
      ta.setSelectionRange(pos, pos);
    });
  };

  const fillExample = (e: string) => {
    setExpr(e);
    setPickerOpen(false);
  };

  const startEdit = (id: string, n: string, e: string) => {
    setFid(id); setName(n); setExpr(e); setMsg(null); setTestResult(null);
  };

  const validate = async (thenSave: boolean) => {
    if (!expr.trim()) { setMsg({ ok: false, text: "公式为空" }); return; }
    if (thenSave && (!fid.trim() || !name.trim())) {
      setMsg({ ok: false, text: "保存前需填因子 id 与名称" }); return;
    }
    setBusy(true); setMsg(null);
    try {
      await api.factorValidate(fid || "tmp", name || "tmp", expr);
      if (!thenSave) { setMsg({ ok: true, text: "公式语法正确" }); return; }
      await api.factorCustomSave(fid.trim(), name.trim(), expr);
      setMsg({ ok: true, text: `已保存 ${fid}。点右侧列表的「检验」即可查看效果。` });
      setFid(""); setName("");
      onSaved();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  // 试跑：保存为临时因子 → 直接 evaluate（覆盖同名临时因子，用完即删）
  const testRun = async () => {
    if (!hasData) { setMsg({ ok: false, text: "请先在上方构建日线数据" }); return; }
    if (!expr.trim()) { setMsg({ ok: false, text: "公式为空" }); return; }
    setTesting(true); setTestResult(null); setMsg(null);
    const tmpId = `__tmp_${Date.now().toString(36)}`;
    try {
      await api.factorCustomSave(tmpId, "临时试跑", expr);
      const r = await api.factorEvaluate(`custom:${tmpId}`);
      setTestResult(
        `RankIC ${r.ic.rank_ic_mean.toFixed(4)} · 正 IC 比例 ${(r.ic.rank_ic_positive_ratio * 100).toFixed(0)}%` +
        ` · Q5-Q1 ${r.long_short_bp.toFixed(1)}bp/日 · 样本 ${r.n_days} 天`,
      );
    } catch (e) {
      setTestResult(null);
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      api.factorCustomDelete(tmpId).catch(() => {});
      setTesting(false);
      onSaved(); // 刷新列表（清掉临时因子）
    }
  };

  const del = async (id: string) => {
    try { await api.factorCustomDelete(id); onSaved(); } catch { /* 忽略 */ }
  };

  return (
    <GlassCard>
      <TokenPicker doc={doc} open={pickerOpen} onClose={() => setPickerOpen(false)}
        onInsert={insert} onFillExample={fillExample} />
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <FlaskConical className="h-4 w-4 text-primary" /> 因子构建
        </h3>
        <span className="text-xs text-muted-foreground">不知道写什么？点「指标与函数」从列表里选</span>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <div className="flex gap-2">
            <label className="flex flex-1 flex-col gap-1 text-xs">
              <span className="text-muted-foreground">因子 id（字母/数字/下划线，保存后不可与他因子重复）</span>
              <input value={fid} onChange={(e) => setFid(e.target.value)} placeholder="low_vol"
                className="rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5 font-mono text-sm" />
            </label>
            <label className="flex flex-1 flex-col gap-1 text-xs">
              <span className="text-muted-foreground">名称（显示用）</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="低波动"
                className="rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5 text-sm" />
            </label>
          </div>
          <label className="flex flex-col gap-1 text-xs">
            <span className="flex items-center justify-between">
              <span className="text-muted-foreground">公式</span>
              <button onClick={() => setPickerOpen(true)}
                className="flex items-center gap-1 rounded-lg border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground transition hover:border-primary/50 hover:text-primary">
                <BookOpen className="h-3 w-3" /> 指标与函数
              </button>
            </span>
            <textarea ref={taRef} value={expr} onChange={(e) => setExpr(e.target.value)} rows={3}
              placeholder="点右上「指标与函数」选择，或直接输入，如 -cs_zscore(ts_std(ret,20))"
              className="rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5 font-mono text-sm" />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button onClick={() => validate(false)} disabled={busy}
              className="rounded-lg border border-border/60 px-3 py-1.5 text-xs text-muted-foreground transition hover:bg-black/20 disabled:opacity-50">
              校验语法
            </button>
            <button onClick={testRun} disabled={testing || !hasData}
              className="flex items-center gap-1 rounded-lg border border-border/60 px-3 py-1.5 text-xs text-muted-foreground transition hover:bg-black/20 disabled:opacity-50">
              {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              试跑（快速看 IC）
            </button>
            <button onClick={() => validate(true)} disabled={busy}
              className="rounded-lg bg-primary/20 px-3 py-1.5 text-xs font-medium text-primary transition hover:bg-primary/30 disabled:opacity-50">
              保存因子
            </button>
          </div>
          {(msg || testResult) && (
            <div className={`rounded-lg border p-2 text-xs ${
              testResult ? "border-success/30 bg-success/5 text-success"
                : msg?.ok ? "border-success/30 bg-success/5 text-success"
                  : "border-danger/30 bg-danger/5 text-danger"}`}>
              {testResult ?? msg?.text}
            </div>
          )}
          {!hasData && (
            <p className="text-[11px] text-muted-foreground">数据未构建时只能校验语法和保存，「试跑/检验/回测」需要先构建日线数据。</p>
          )}
        </div>
        <div>
          <p className="mb-2 text-xs font-semibold text-foreground/80">已保存的自定义因子</p>
          {doc && doc.custom.length > 0 ? (
            <table className="w-full text-xs">
              <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
                <tr><th className="py-1 text-left">id</th><th className="text-left">名称</th><th className="text-left">公式</th><th /></tr>
              </thead>
              <tbody>
                {doc.custom.map((c) => (
                  <tr key={c.id} className="border-b border-border/30">
                    <td className="py-1.5 font-mono">{c.id}</td>
                    <td>{c.name}</td>
                    <td className="max-w-[200px] truncate font-mono text-[11px] text-muted-foreground" title={c.expr}>{c.expr}</td>
                    <td className="whitespace-nowrap text-right">
                      <button onClick={() => onUseFactor(`custom:${c.id}`)}
                        className="mr-2 text-primary hover:underline">去检验</button>
                      <button onClick={() => startEdit(c.id, c.name, c.expr)}
                        className="mr-2 text-muted-foreground hover:text-foreground" title="载入编辑">
                        <Pencil className="inline h-3 w-3" />
                      </button>
                      <button onClick={() => del(c.id)} className="text-danger hover:underline" title="删除">
                        <Trash2 className="inline h-3 w-3" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-xs text-muted-foreground">
              暂无自定义因子。左侧写公式（或从「指标与函数」选）→ 校验 → 保存，然后点「去检验」看完整表现。
            </p>
          )}
        </div>
      </div>
    </GlassCard>
  );
}

export function Factors() {
  const [tab, setTab] = useState<"lab" | "builder">("lab");
  const [lab, setLab] = useState<FactorLabStatus | undefined>();
  const [doc, setDoc] = useState<FactorFieldsDoc | undefined>();
  const [factor, setFactor] = useState("mom60");
  const [topN, setTopN] = useState(50);
  const [freq, setFreq] = useState<"weekly" | "monthly">("monthly");
  const [cost, setCost] = useState(1);
  const [evalData, setEvalData] = useState<FactorEvaluateData | undefined>();
  const [btData, setBtData] = useState<FactorBacktestData | undefined>();
  const [loadingEval, setLoadingEval] = useState(false);
  const [loadingBt, setLoadingBt] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const loadLab = useCallback(() => {
    api.factorLab().then(setLab).catch(() => setLab(undefined));
  }, []);

  const loadDoc = useCallback(() => {
    api.factorFields().then(setDoc).catch(() => setDoc(undefined));
  }, []);

  useEffect(() => {
    loadLab();
    loadDoc();
  }, [loadLab, loadDoc]);

  // 构建中每 10s 轮询进度
  useEffect(() => {
    if (!lab?.building) return;
    const t = window.setInterval(loadLab, 10_000);
    return () => window.clearInterval(t);
  }, [lab?.building, loadLab]);

  const build = () => {
    api.factorBuild().then(() => loadLab()).catch((e) => setErr(String(e.message ?? e)));
  };

  const runEvaluate = () => {
    if (!lab?.has_data) { setErr("请先构建日线数据"); return; }
    setLoadingEval(true); setErr(null);
    api.factorEvaluate(factor).then(setEvalData)
      .catch((e) => { setEvalData(undefined); setErr(e.message ?? "因子检验失败"); })
      .finally(() => setLoadingEval(false));
  };

  const runBacktest = () => {
    if (!lab?.has_data) { setErr("请先构建日线数据"); return; }
    setLoadingBt(true); setErr(null);
    api.factorBacktest({ factor, top_n: topN, freq, cost })
      .then(setBtData)
      .catch((e) => { setBtData(undefined); setErr(e.message ?? "回测失败"); })
      .finally(() => setLoadingBt(false));
  };

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 md:p-6">
      <PageHeader
        title="因子研究"
        subtitle="横截面选股因子检验（Alphalens 口径）与探索性组合回测 · 探索级数据，仅作研究证据，不构成投资建议"
        actions={
          <button onClick={loadLab} className="flex items-center gap-1.5 rounded-lg border border-border/60 px-3 py-1.5 text-xs text-muted-foreground transition hover:bg-black/20">
            <RefreshCw className="h-3.5 w-3.5" /> 刷新状态
          </button>
        }
      />

      {lab && lab.biases.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl border border-warning/30 bg-warning/5 p-3 text-xs text-warning-foreground/90">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div>
            探索级数据边界：{lab.biases.map((b) => b.split(":")[0]).join(" · ")}。
            回测结果不可直接外推实盘，仅供研究参考。
          </div>
        </div>
      )}

      {err && (
        <div className="flex items-center gap-2 rounded-xl border border-danger/30 bg-danger/5 p-3 text-xs text-danger">
          <AlertTriangle className="h-4 w-4" /> {err}
        </div>
      )}

      <DataPanel lab={lab} onBuild={build} />

      {/* 子板块切换：检验与回测 / 因子构建 */}
      <div className="flex gap-1 rounded-xl border border-border/60 bg-background/30 p-1">
        {([["lab", "检验与回测"], ["builder", "因子构建"]] as const).map(([id, label]) => (
          <button key={id} onClick={() => setTab(id)}
            className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition ${
              tab === id ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-black/20"}`}>
            {label}
          </button>
        ))}
      </div>

      {tab === "builder" && (
        <BuilderPanel doc={doc} onSaved={loadDoc} onUseFactor={(fid) => { setFactor(fid); setTab("lab"); }}
          hasData={!!lab?.has_data} />
      )}

      {tab === "lab" && (
      <>
      <GlassCard>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">因子</span>
            <select value={factor} onChange={(e) => setFactor(e.target.value)}
              className="max-w-[320px] rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5 text-sm">
              <optgroup label="价量因子">
                {(lab?.factors ?? [{ id: "mom60", name: "60 日动量" }]).map((f) => (
                  <option key={f.id} value={f.id}>{f.id} · {f.name}</option>
                ))}
              </optgroup>
              {(lab?.fin_factors?.length ?? 0) > 0 && (
                <optgroup label="财务因子（PIT，需构建财务数据）">
                  {lab?.fin_factors?.map((f) => (
                    <option key={f.id} value={f.id}>{f.id} · {f.name}</option>
                  ))}
                </optgroup>
              )}
              {(doc?.custom ?? []).length > 0 && (
                <optgroup label="自定义">
                  {(doc?.custom ?? []).map((c) => (
                    <option key={`custom:${c.id}`} value={`custom:${c.id}`}>custom:{c.id} · {c.name}</option>
                  ))}
                </optgroup>
              )}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">持仓数</span>
            <input type="number" min={1} max={500} value={topN}
              onChange={(e) => setTopN(Number(e.target.value) || 50)}
              className="w-24 rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5 text-sm tabular-nums" />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">调仓频率</span>
            <select value={freq} onChange={(e) => setFreq(e.target.value as "weekly" | "monthly")}
              className="rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5 text-sm">
              <option value="monthly">月调仓</option>
              <option value="weekly">周调仓</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">成本倍数</span>
            <select value={cost} onChange={(e) => setCost(Number(e.target.value))}
              className="rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5 text-sm">
              <option value={0}>0x（无成本）</option>
              <option value={1}>1x（基准成本）</option>
              <option value={2}>2x</option>
              <option value={3}>3x</option>
            </select>
          </label>
          <button onClick={runEvaluate} disabled={loadingEval || !lab?.has_data}
            className="flex items-center gap-1.5 rounded-lg bg-primary/20 px-3 py-1.5 text-xs font-medium text-primary transition hover:bg-primary/30 disabled:opacity-50">
            {loadingEval ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="h-3.5 w-3.5" />}
            因子检验
          </button>
          <button onClick={runBacktest} disabled={loadingBt || !lab?.has_data}
            className="flex items-center gap-1.5 rounded-lg bg-primary/20 px-3 py-1.5 text-xs font-medium text-primary transition hover:bg-primary/30 disabled:opacity-50">
            {loadingBt ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            组合回测
          </button>
        </div>
        {!lab?.has_data && lab && (
          <p className="mt-3 text-xs text-muted-foreground">
            因子检验与回测需要先构建全市场日线数据（首次构建约 20-40 分钟，构建一次后可反复使用）。
          </p>
        )}
      </GlassCard>

      {evalData && (
        <GlassCard>
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-semibold">单因子检验 · {evalData.factor_name}</h3>
            <span className="text-xs text-muted-foreground">{evalData.timing_note} · {evalData.start} ~ {evalData.end}</span>
          </div>
          <EvaluatePanel data={evalData} />
        </GlassCard>
      )}

      {btData && (
        <GlassCard>
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-semibold">探索性组合回测 · {btData.factor_name}</h3>
            <span className="text-xs text-muted-foreground">{btData.timing_note} · {btData.params.start} ~ {btData.params.end}</span>
          </div>
          <BacktestPanel data={btData} />
        </GlassCard>
      )}
      </>
      )}
    </div>
  );
}
