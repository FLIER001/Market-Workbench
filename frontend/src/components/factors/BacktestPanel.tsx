// 回测结果面板：复用既有探索性组合模拟的展示（净值/指标/成本压力/分年），
// 数据来自所选已完成实验发起的 backtest 任务（契约未细化 result 结构，按现有模拟器字段读取）。
import { useChart, fmt, fmtPct } from "@/components/factors/shared";
import { cssColor } from "@/lib/chartColor";

export interface BacktestMetrics {
  total_return: number; ann_return: number; ann_vol: number | null; sharpe: number | null;
  max_drawdown: number; win_rate: number | null; ann_turnover: number; n_days: number;
}
export interface BacktestResult {
  factor?: string; factor_name?: string; data_version?: string;
  params?: { start?: string; end?: string; top_n?: number; freq?: string; cost_multiplier?: number };
  metrics: BacktestMetrics;
  cost_stress?: Record<string, BacktestMetrics>;
  nav?: { dates: string[]; strategy: number[]; benchmark: number[] };
  yearly_returns?: Record<string, number>;
  biases?: string[];
  timing_note?: string;
  report?: FactorReport;
  sensitivity?: { points: FactorSensitivityPoint[]; note: string };
}
export interface FactorReport {
  monthly_returns: Record<string, number>;
  drawdowns: { peak: string; trough: string; depth: number | null; recovered: string | null; days: number | null; drawdown_days?: number; note?: string }[];
  sharpe_ci: { sharpe: number | null; low: number | null; high: number | null; iters: number; level?: number; note?: string };
  segments: { label: string; start: string | null; end: string | null; days: number; return: number | null; annualized: number | null; sharpe: number | null; max_drawdown: number | null }[];
}
export interface FactorSensitivityPoint {
  top_n: number; is_current: boolean; ann_return: number | null; sharpe: number | null; max_drawdown: number | null;
}

function isBacktestResult(value: unknown): value is BacktestResult {
  const metrics = (value as BacktestResult | null)?.metrics;
  return !!metrics && typeof metrics.total_return === "number";
}

function NavChart({ nav }: { nav: NonNullable<BacktestResult["nav"]> }) {
  const chart = useChart({
    grid: { left: 48, right: 12, top: 28, bottom: 28 },
    tooltip: { trigger: "axis" },
    legend: { top: 0, textStyle: { fontSize: 10 }, data: ["策略净值", "基准（全池等权）"] },
    xAxis: { type: "category", data: nav.dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: [
      { type: "line", name: "策略净值", data: nav.strategy, showSymbol: false,
        lineStyle: { width: 1.6, color: cssColor("--primary") }, areaStyle: { color: cssColor("--primary", "0.10") } },
      { type: "line", name: "基准（全池等权）", data: nav.benchmark, showSymbol: false,
        lineStyle: { width: 1.2, color: cssColor("--muted-foreground", "0.8") } },
    ],
  }, "h-64");
  return <div ref={chart.boxRef} role="img" aria-label="策略与全池等权基准净值" className={`${chart.height} w-full rounded-xl border border-border/40 bg-background/20 p-1`} />;
}

/** 水下回撤：净值 / 历史最高 − 1，全程在 0 轴下方。 */
function DrawdownChart({ nav }: { nav: NonNullable<BacktestResult["nav"]> }) {
  const series: number[] = [];
  let peak = -Infinity;
  for (const value of nav.strategy) {
    peak = Math.max(peak, value);
    series.push(peak > 0 ? Number(((value / peak - 1) * 100).toFixed(2)) : 0);
  }
  const chart = useChart({
    grid: { left: 44, right: 12, top: 12, bottom: 24 },
    tooltip: { trigger: "axis", valueFormatter: (v: unknown) => `${v}%` },
    xAxis: { type: "category", data: nav.dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", max: 0, axisLabel: { fontSize: 10, formatter: "{value}%" }, splitLine: { lineStyle: { color: cssColor("--border", "0.4") } } },
    series: [{
      type: "line", name: "水下回撤", data: series, showSymbol: false,
      lineStyle: { width: 1.2, color: cssColor("--danger") },
      areaStyle: { color: cssColor("--danger", "0.18") },
    }],
  }, "h-40");
  return <div ref={chart.boxRef} role="img" aria-label="水下回撤" className={`${chart.height} w-full rounded-xl border border-border/40 bg-background/20 p-1`} />;
}

/** 月度收益热力：行=年、列=月，颜色深浅按涨跌幅。 */
function MonthlyHeatmap({ monthly }: { monthly: Record<string, number> }) {
  const months = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(2, "0"));
  const years = Array.from(new Set(Object.keys(monthly).map((k) => k.slice(0, 4)))).sort();
  if (years.length === 0) return null;
  const max = Math.max(0.01, ...Object.values(monthly).map((v) => Math.abs(v)));
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-[11px] tabular-nums">
        <thead>
          <tr className="text-muted-foreground">
            <th className="py-1 text-left font-medium">年</th>
            {months.map((m) => <th key={m} className="py-1 text-center font-medium">{Number(m)}</th>)}
          </tr>
        </thead>
        <tbody>
          {years.map((year) => (
            <tr key={year}>
              <td className="py-0.5 text-muted-foreground">{year}</td>
              {months.map((month) => {
                const value = monthly[`${year}-${month}`];
                if (value === undefined) return <td key={month} className="p-0.5" />;
                const alpha = Math.min(0.75, Math.abs(value) / max * 0.75);
                const bg = value >= 0 ? cssColor("--danger", String(alpha)) : cssColor("--success", String(alpha));
                return (
                  <td key={month} className="p-0.5">
                    <span className="block rounded px-1 py-1 text-center" style={{ background: bg }} title={`${year}-${month} ${fmtPct(value)}`}>
                      {value === 0 ? "0" : fmtPct(value)}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 报表块：夏普区间 / 最深回撤段 / 分段稳定性 / 邻参敏感度。 */
function ReportBlock({ report, sensitivity }: { report: NonNullable<BacktestResult["report"]>; sensitivity?: BacktestResult["sensitivity"] }) {
  const ci = report.sharpe_ci;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="glass p-5">
        <h3 className="mb-3 text-sm font-semibold">夏普区间</h3>
        {ci.sharpe == null ? (
          <p className="text-xs text-muted-foreground">{ci.note ?? "样本不足，未给区间"}</p>
        ) : (
          <>
            <p className="text-lg font-bold tabular-nums">
              {fmt(ci.sharpe)}
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                95% 区间 {fmt(ci.low)} ~ {fmt(ci.high)}
              </span>
            </p>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
              {ci.note ?? "区间反映这段样本自身的抽样波动"}（重抽 {ci.iters} 次）
            </p>
          </>
        )}
      </div>
      <div className="glass p-5">
        <h3 className="mb-3 text-sm font-semibold">最深回撤段</h3>
        {report.drawdowns.length === 0 ? (
          <p className="text-xs text-muted-foreground">区间内没有回撤段。</p>
        ) : (
          <table className="w-full text-xs tabular-nums">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr><th className="py-1 text-left">峰值</th><th className="py-1 text-left">谷底</th><th className="py-1 text-right">深度</th><th className="py-1 text-right">复原</th></tr>
            </thead>
            <tbody>
              {report.drawdowns.map((row, i) => (
                <tr key={i} className="border-b border-border/30">
                  <td className="py-1">{row.peak}</td>
                  <td className="py-1">{row.trough}</td>
                  <td className="py-1 text-right text-danger">{fmtPct(row.depth)}</td>
                  <td className="py-1 text-right text-muted-foreground">{row.recovered ?? "未复原"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {report.segments.length > 0 && (
        <div className="glass p-5">
          <h3 className="mb-3 text-sm font-semibold">分段稳定性</h3>
          <table className="w-full text-xs tabular-nums">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr><th className="py-1 text-left">区间</th><th className="py-1 text-right">收益</th><th className="py-1 text-right">年化</th><th className="py-1 text-right">夏普</th><th className="py-1 text-right">最大回撤</th></tr>
            </thead>
            <tbody>
              {report.segments.map((row, i) => (
                <tr key={i} className="border-b border-border/30">
                  <td className="py-1">{row.label}<span className="ml-1 text-[10px] text-muted-foreground/60">{row.start} → {row.end}</span></td>
                  <td className="py-1 text-right">{fmtPct(row.return)}</td>
                  <td className="py-1 text-right">{fmtPct(row.annualized)}</td>
                  <td className="py-1 text-right">{fmt(row.sharpe)}</td>
                  <td className="py-1 text-right text-danger">{fmtPct(row.max_drawdown)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">各段单独算；段间差别大说明结论依赖特定时期。</p>
        </div>
      )}
      {sensitivity && sensitivity.points.length > 1 && (
        <div className="glass p-5">
          <h3 className="mb-3 text-sm font-semibold">邻参敏感度（只改持仓数）</h3>
          <table className="w-full text-xs tabular-nums">
            <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
              <tr><th className="py-1 text-left">持仓数</th><th className="py-1 text-right">年化</th><th className="py-1 text-right">夏普</th><th className="py-1 text-right">最大回撤</th></tr>
            </thead>
            <tbody>
              {sensitivity.points.map((point) => (
                <tr key={point.top_n} className={point.is_current ? "border-b border-border/30 bg-primary/5" : "border-b border-border/30"}>
                  <td className="py-1">{point.top_n}{point.is_current ? "（当前）" : ""}</td>
                  <td className="py-1 text-right">{fmtPct(point.ann_return)}</td>
                  <td className="py-1 text-right">{fmt(point.sharpe)}</td>
                  <td className="py-1 text-right text-danger">{fmtPct(point.max_drawdown)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">{sensitivity.note}</p>
        </div>
      )}
    </div>
  );
}

export function BacktestPanel({ result }: { result: unknown }) {
  if (!isBacktestResult(result)) {
    return <p className="text-xs text-muted-foreground">回测结果结构无法识别，请重新运行。</p>;
  }
  const m = result.metrics;
  const rows: Array<[string, string]> = [
    ["区间收益", fmtPct(m.total_return)], ["年化收益", fmtPct(m.ann_return)],
    ["年化波动", fmtPct(m.ann_vol)], ["夏普", fmt(m.sharpe)],
    ["最大回撤", fmtPct(m.max_drawdown)], ["日胜率", fmtPct(m.win_rate)],
    ["年化单边换手", fmt(m.ann_turnover, 1)], ["交易日数", String(m.n_days)],
  ];
  const current = result.params?.cost_multiplier;
  return (
    <div className="space-y-4">
      {result.nav && <NavChart nav={result.nav} />}
      {result.nav && <DrawdownChart nav={result.nav} />}
      {result.report && Object.keys(result.report.monthly_returns).length > 0 && (
        <div className="glass p-5">
          <h3 className="mb-3 text-sm font-semibold">月度收益热力</h3>
          <MonthlyHeatmap monthly={result.report.monthly_returns} />
        </div>
      )}
      {result.report && <ReportBlock report={result.report} sensitivity={result.sensitivity} />}
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="glass p-5">
          <h3 className="mb-3 text-sm font-semibold">组合指标</h3>
          <div className="grid grid-cols-2 gap-2">
            {rows.map(([label, value]) => (
              <div key={label} className="rounded-xl border border-border/60 bg-background/30 p-2.5">
                <p className="text-[11px] text-muted-foreground">{label}</p>
                <p className="text-sm font-bold tabular-nums">{value}</p>
              </div>
            ))}
          </div>
        </div>
        <div className="glass p-5">
          <h3 className="mb-3 text-sm font-semibold">成本压力测试（区间收益）</h3>
          {result.cost_stress ? (
            <table className="w-full text-xs tabular-nums">
              <thead className="border-b border-border/60 text-[10px] text-muted-foreground">
                <tr><th className="py-1 text-left">成本倍数</th><th className="text-right">区间收益</th><th className="text-right">年化</th><th className="text-right">最大回撤</th></tr>
              </thead>
              <tbody>
                {Object.entries(result.cost_stress).map(([key, r]) => (
                  <tr key={key} className={key === `${current}x` ? "border-b border-border/30 bg-primary/5" : "border-b border-border/30"}>
                    <td className="py-1">{key}{key === `${current}x` ? "（当前）" : ""}</td>
                    <td className="text-right">{fmtPct(r.total_return)}</td>
                    <td className="text-right">{fmtPct(r.ann_return)}</td>
                    <td className="text-right">{fmtPct(r.max_drawdown)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="text-xs text-muted-foreground">无成本压力数据。</p>}
          <h3 className="mb-2 mt-4 text-sm font-semibold">分年收益</h3>
          <div className="flex flex-wrap gap-2">
            {Object.entries(result.yearly_returns ?? {}).map(([year, r]) => (
              <div key={year} className="rounded-lg border border-border/60 bg-background/30 px-2.5 py-1.5">
                <span className="text-[11px] text-muted-foreground">{year}</span>
                <span className={`ml-2 text-xs font-bold tabular-nums ${r >= 0 ? "text-success" : "text-danger"}`}>{fmtPct(r)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      {result.timing_note && <p className="text-[11px] leading-relaxed text-muted-foreground">{result.timing_note}</p>}
      {!!result.biases?.length && <p className="text-[11px] leading-relaxed text-muted-foreground">{result.biases.join("；")}</p>}
    </div>
  );
}
