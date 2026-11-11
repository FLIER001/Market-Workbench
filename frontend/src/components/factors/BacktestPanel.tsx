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
