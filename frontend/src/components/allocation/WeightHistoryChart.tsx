import { useEffect, useMemo, useRef, useState } from "react";
import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { init, use, type ECharts } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
// 颜色统一走 chartColor（rgba 数值形式），否则 zrender 解析 hsl 空格语法失败，
// 悬停重绘会整条 series 消失，见 lib/chartColor.ts 顶部根因说明。
import { cssColor } from "@/lib/chartColor";
import type { WeightPoint } from "@/lib/api";

use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

// 四类资产的堆叠顺序与配色：与上方当前权重堆叠条同语义（危险色=股票、蓝=债券…）
const SERIES: { key: keyof Omit<WeightPoint, "date">; name: string; color: string }[] = [
  { key: "equity", name: "股票", color: "--danger" },
  { key: "bond", name: "债券", color: "--kalshi" },
  { key: "commodity", name: "商品", color: "--warning" },
  { key: "cash", name: "现金", color: "--primary" },
];

/** 四类资产建议持仓比例的堆叠面积图（0-100%，按月份采样，最后一点为当日目标）。 */
export function WeightHistoryChart({ points, height = 240 }: { points: WeightPoint[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);
  const [themeTick, setThemeTick] = useState(0);

  // 月度采样：每月取最后一日（权重只在换档时变，月内取末值即当月结论）
  const rows = useMemo(() => {
    const byMonth = new Map<string, WeightPoint>();
    for (const p of points) byMonth.set(p.date.slice(0, 7), p);
    return [...byMonth.entries()].map(([month, p]) => ({ month, ...p }));
  }, [points]);

  useEffect(() => {
    const obs = new MutationObserver(() => setThemeTick((v) => v + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    if (!ref.current || rows.length === 0) return;
    void themeTick; // 主题切换时重取 CSS 变量
    if (!chartRef.current) chartRef.current = init(ref.current);
    const grid = cssColor("--chart-grid", 0.55);
    const text = cssColor("--chart-text");
    const chart = chartRef.current;
    chart.setOption({
      animation: false,
      grid: { left: 4, right: 12, top: 26, bottom: 2, containLabel: true },
      legend: { top: 0, left: 0, itemWidth: 8, itemHeight: 8, itemGap: 12,
        textStyle: { color: text, fontSize: 10 } },
      tooltip: {
        trigger: "axis",
        backgroundColor: cssColor("--background", 0.96),
        borderColor: cssColor("--border", 0.45),
        textStyle: { color: text, fontSize: 11 },
        valueFormatter: (v: number) => `${Number(v).toFixed(1)}%`,
      },
      xAxis: {
        type: "category",
        data: rows.map((r) => r.month),
        boundaryGap: false,
        axisLine: { lineStyle: { color: grid } },
        axisTick: { show: false },
        axisLabel: {
          color: text,
          fontSize: 10,
          // 跨年也能分清：1 月只写年份，其余写月
          formatter: (v: string) => (v.endsWith("-01") ? v.slice(0, 4) : `${Number(v.slice(5))}月`),
        },
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 100,
        axisLabel: { color: text, fontSize: 10, formatter: "{value}%" },
        splitLine: { lineStyle: { color: grid } },
      },
      series: SERIES.map((s) => ({
        name: s.name,
        type: "line",
        stack: "weights",
        showSymbol: false,
        lineStyle: { width: 0 },
        areaStyle: { opacity: 0.6, color: cssColor(s.color) },
        itemStyle: { color: cssColor(s.color) },
        emphasis: { focus: "series" },
        data: rows.map((p) => Number(p[s.key])),
      })),
    }, true);
  }, [rows, themeTick]);

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  return <div ref={ref} style={{ height }} className="w-full" />;
}
