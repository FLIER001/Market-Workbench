// 因子研究 V2 结果展示的公共件：图表容器、指标格式、状态标签、样本审计、相关性矩阵。
// 颜色统一走 lib/chartColor.ts（zrender 可解析的 rgb/rgba），不要在本地重写取色。
import { useEffect, useRef } from "react";
import { BarChart, LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { init, use, type EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { cssColor } from "@/lib/chartColor";
import {
  HORIZONS, RUN_STATUS_LABEL, PHASE_LABEL,
  type HorizonKey, type HorizonStats, type Run, type RunStatus, type AnalysisResult,
} from "@/lib/factorLab";

use([LineChart, BarChart, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

export function useChart(option: EChartsCoreOption | null, height = "h-56") {
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!boxRef.current || !option) return;
    const chart = init(boxRef.current);
    chart.setOption(option, true);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(boxRef.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [option]);
  return { boxRef, height };
}

/** null / 缺失显示「—」，0 就是 0：不把无证据当成 0。 */
export function fmt(v: number | null | undefined, digits = 3): string {
  return v == null || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

export function fmtPct(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function fmtInt(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? "—" : v.toLocaleString();
}

export const inputClass =
  "w-full rounded-lg border border-border/60 bg-background/50 px-3 py-2 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary";

const STATUS_CLASS: Record<RunStatus, string> = {
  queued: "bg-muted/40 text-muted-foreground",
  running: "bg-primary/15 text-primary",
  completed: "bg-success/15 text-success",
  failed: "bg-danger/15 text-danger",
  cancelled: "bg-muted/40 text-muted-foreground",
};

export function StatusTag({ status }: { status: RunStatus }) {
  return (
    <span className={`inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] ${STATUS_CLASS[status]}`}>
      {RUN_STATUS_LABEL[status]}
    </span>
  );
}

export function PhaseTag({ phase }: { phase: Run["phase"] }) {
  return <span className="rounded bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">{PHASE_LABEL[phase]}</span>;
}

/** 预测周期切换：IC / 分组收益 / 样本信息同步切换。 */
export function HorizonTabs({ value, onChange, label = "预测周期" }: {
  value: HorizonKey; onChange: (h: HorizonKey) => void; label?: string;
}) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <div className="flex rounded-lg border border-border/60 p-0.5">
        {HORIZONS.map((h) => (
          <button key={h} type="button" aria-pressed={value === h} onClick={() => onChange(h)}
            className={`rounded px-2.5 py-1 tabular-nums transition ${value === h ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-muted/40"}`}>
            {h} 日
          </button>
        ))}
      </div>
    </div>
  );
}

export function MetricCell({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  const color = tone === "up" ? "text-success" : tone === "down" ? "text-danger" : "";
  return (
    <div className="rounded-xl border border-border/60 bg-background/30 p-3">
      <p className="text-[11px] text-muted-foreground">{label}</p>
      <p className={`mt-1 text-sm font-bold tabular-nums ${color}`}>{value}</p>
    </div>
  );
}

const signedTone = (v: number | null | undefined) =>
  v == null || !Number.isFinite(v) || v === 0 ? undefined : v > 0 ? ("up" as const) : ("down" as const);

export function HorizonMetrics({ stats }: { stats: HorizonStats }) {
  const rows: Array<[string, string, ("up" | "down") | undefined]> = [
    ["RankIC 均值", fmt(stats.ic.rank_ic_mean), signedTone(stats.ic.rank_ic_mean)],
    ["IC IR（未年化）", fmt(stats.ic.rank_ic_ir), undefined],
    ["IC 均值", fmt(stats.ic.ic_mean), signedTone(stats.ic.ic_mean)],
    ["正 IC 比例", fmtPct(stats.ic.rank_ic_positive_ratio), undefined],
    ["Q5-Q1 日均", `${fmt(stats.long_short_bp, 1)} bp`, signedTone(stats.long_short_bp)],
    ["因子秩自相关", fmt(stats.rank_autocorr), undefined],
    ["日均覆盖", `${fmt(stats.avg_coverage, 0)} 只`, undefined],
    ["样本天数", String(stats.n_days), undefined],
  ];
  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
      {rows.map(([label, value, tone]) => <MetricCell key={label} label={label} value={value} tone={tone} />)}
    </div>
  );
}

export function ICChart({ stats, title }: { stats: HorizonStats; title: string }) {
  const chart = useChart({
    grid: { left: 48, right: 12, top: 16, bottom: 28 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: stats.ic_series.dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: [{
      type: "bar", name: "RankIC", data: stats.ic_series.values,
      itemStyle: {
        color: (p: { value: number }) =>
          (p.value ?? 0) >= 0 ? cssColor("--success", "0.7") : cssColor("--danger", "0.7"),
      },
    }],
    dataZoom: [{ type: "inside" }],
  });
  return (
    <GlassPanel title={title}>
      <div ref={chart.boxRef} role="img" aria-label={`${title}；逐日数值见导出数据`} className={`${chart.height} w-full`} />
    </GlassPanel>
  );
}

export function QuantileChart({ stats, horizon, title }: { stats: HorizonStats; horizon: HorizonKey; title: string }) {
  const quantiles = ["Q1", "Q2", "Q3", "Q4", "Q5"];
  const chart = useChart({
    grid: { left: 48, right: 12, top: 16, bottom: 24 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: quantiles, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: [{
      type: "bar", name: `${horizon} 日累计前瞻收益(bp)`,
      data: quantiles.map((q) => stats.quantile_returns?.[q] ?? null),
      itemStyle: {
        color: (p: { value: number | null }) =>
          (p.value ?? 0) >= 0 ? cssColor("--success", "0.7") : cssColor("--danger", "0.7"),
      },
    }],
  });
  return (
    <GlassPanel title={title}>
      <div ref={chart.boxRef} role="img" aria-label={`${title}；数值见下方表格`} className={`${chart.height} w-full`} />
      <div className="mt-2 grid grid-cols-5 gap-2 text-center text-xs tabular-nums">
        {quantiles.map((q) => (
          <div key={q}>
            <span className="text-muted-foreground">{q}</span>
            <p>{fmt(stats.quantile_returns?.[q], 1)} bp</p>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">Q1 → Q5 因子值递增；各信号日累计前瞻收益均值，不是可交易净值，不扣成本。</p>
    </GlassPanel>
  );
}

export function SampleAudit({ stats }: { stats: HorizonStats }) {
  const cells: Array<[string, string]> = [
    ["原始记录", fmtInt(stats.sample.input_rows)],
    ["上市龄过滤后", fmtInt(stats.sample.eligible_rows)],
    ["因子有效", fmtInt(stats.sample.factor_rows)],
    ["收益配对", fmtInt(stats.sample.paired_rows)],
    ["IC 使用", fmtInt(stats.sample.used_rows)],
    ["跳过日期", fmtInt(stats.sample.skipped_days)],
  ];
  return (
    <GlassPanel title="样本审计">
      <div className="grid grid-cols-3 gap-2 text-xs">
        {cells.map(([label, value]) => (
          <div key={label} className="rounded-lg bg-background/40 p-2">
            <p className="text-muted-foreground">{label}</p>
            <p className="mt-1 font-semibold tabular-nums">{value}</p>
          </div>
        ))}
      </div>
      <p className="mt-3 text-[11px] text-muted-foreground">
        IC 样本损失 {fmtPct(stats.sample.loss_ratio)} · 日均覆盖 {fmt(stats.avg_coverage, 0)} 只 · 样本天数 {stats.n_days}
      </p>
    </GlassPanel>
  );
}

export function YearTable({ stats, title = "分年 RankIC" }: { stats: HorizonStats; title?: string }) {
  const rows = Object.entries(stats.by_year ?? {});
  return (
    <GlassPanel title={title}>
      {rows.length === 0 ? <p className="text-xs text-muted-foreground">无分年样本。</p> : (
        <table className="w-full text-xs tabular-nums [&_th]:whitespace-nowrap">
          <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
            <tr><th className="py-1 text-left">年份</th><th className="text-right">RankIC</th><th className="text-right">IR</th><th className="text-right">天数</th></tr>
          </thead>
          <tbody>
            {rows.map(([year, r]) => (
              <tr key={year} className="border-b border-border/30">
                <td className="py-1">{year}</td>
                <td className={`text-right ${r.rank_ic_mean == null ? "" : r.rank_ic_mean >= 0 ? "text-success" : "text-danger"}`}>{fmt(r.rank_ic_mean)}</td>
                <td className="text-right">{fmt(r.rank_ic_ir)}</td>
                <td className="text-right">{r.days}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </GlassPanel>
  );
}

export function TurnoverPanel({ stats }: { stats: HorizonStats }) {
  const quantiles = ["Q1", "Q2", "Q3", "Q4", "Q5"];
  return (
    <GlassPanel title="分组换手">
      <div className="grid grid-cols-5 gap-2 text-center text-xs tabular-nums">
        {quantiles.map((q) => (
          <div key={q} className="rounded-lg bg-background/40 p-2">
            <span className="text-muted-foreground">{q}</span>
            <p className="mt-1 font-semibold">{fmtPct(stats.quantile_turnover?.[q])}</p>
          </div>
        ))}
      </div>
      <p className="mt-3 text-[11px] text-muted-foreground">当期分组新增成员比例，不是交易换手；高持久性不等于预测有效。</p>
    </GlassPanel>
  );
}

/** 全部预测周期一览：切换周期时也要能看到其它周期是否一致。 */
export function AllHorizonTable({ byHorizon, title = "各周期一览" }: {
  byHorizon: Record<string, HorizonStats | undefined>; title?: string;
}) {
  return (
    <GlassPanel title={title}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[540px] text-xs tabular-nums [&_th]:whitespace-nowrap">
          <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
            <tr>
              <th className="py-1 text-left">周期</th><th className="text-right">RankIC</th><th className="text-right">IC IR</th>
              <th className="text-right">正 IC</th><th className="text-right">Q5-Q1</th><th className="text-right">覆盖率</th>
              <th className="text-right">IC 使用</th><th className="text-right">损失</th><th className="text-right">天数</th>
            </tr>
          </thead>
          <tbody>
            {HORIZONS.map((h) => {
              const stats = byHorizon[h];
              return (
                <tr key={h} className="border-b border-border/30">
                  <td className="py-1">{h} 日</td>
                  <td className={`text-right ${stats?.ic.rank_ic_mean == null ? "" : stats.ic.rank_ic_mean >= 0 ? "text-success" : "text-danger"}`}>{fmt(stats?.ic.rank_ic_mean)}</td>
                  <td className="text-right">{fmt(stats?.ic.rank_ic_ir)}</td>
                  <td className="text-right">{fmtPct(stats?.ic.rank_ic_positive_ratio)}</td>
                  <td className="text-right">{fmt(stats?.long_short_bp, 1)}</td>
                  <td className="text-right">{fmt(stats?.avg_coverage, 0)}</td>
                  <td className="text-right">{fmtInt(stats?.sample.used_rows)}</td>
                  <td className="text-right">{fmtPct(stats?.sample.loss_ratio)}</td>
                  <td className="text-right">{stats?.n_days ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </GlassPanel>
  );
}

/** 因子值日度横截面秩相关矩阵（不是 IC 序列相关性）。 */
export function CorrelationMatrix({ correlation, names, title = "因子值相关性" }: {
  correlation: NonNullable<AnalysisResult["correlation"]>; names: string[]; title?: string;
}) {
  return (
    <GlassPanel title={title}>
      <div className="overflow-x-auto">
        <table className="text-xs tabular-nums">
          <thead>
            <tr>
              <th className="px-2 py-1 text-left text-[10px] text-muted-foreground" />
              {names.map((name, i) => <th key={i} className="px-2 py-1 text-left text-[10px] font-medium text-muted-foreground">{name}</th>)}
            </tr>
          </thead>
          <tbody>
            {correlation.matrix.map((row, i) => (
              <tr key={i}>
                <td className="whitespace-nowrap px-2 py-1 text-[11px] text-muted-foreground">{names[i] ?? i + 1}</td>
                {row.map((value, j) => (
                  <td key={j} className="px-2 py-1 text-center"
                    style={value == null ? undefined : { backgroundColor: cssColor(value >= 0 ? "--primary" : "--danger", Math.min(0.7, Math.abs(value))) }}
                    title={`${names[i] ?? i + 1} × ${names[j] ?? j + 1}`}>
                    {fmt(value, 2)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[11px] leading-relaxed text-muted-foreground">
        按日横截面计算两因子值的秩相关（样本 {fmtInt(correlation.sample_rows)} 行 / {correlation.n_days} 天），
        不是 IC 序列相关性，也不代表增量收益。
      </p>
    </GlassPanel>
  );
}

// GlassCard 的轻量别名，避免每个结果卡片都重复标题排版。
export function GlassPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="glass p-5">
      <h3 className="mb-3 text-sm font-semibold">{title}</h3>
      {children}
    </div>
  );
}
