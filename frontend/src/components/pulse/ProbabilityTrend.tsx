import { useEffect, useMemo, useRef, useState } from "react";
import { LineChart } from "echarts/charts";
import {
  GridComponent, TooltipComponent,
} from "echarts/components";
import { init, use, type ECharts } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

export interface TrendPoint {
  t: number | null;
  p: number | null;
}

interface Props {
  points: TrendPoint[];
  height?: number;
}

const cssColor = (name: string, alpha?: number) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return alpha == null ? `hsl(${value})` : `hsl(${value} / ${alpha})`;
};

/** Yes 概率（%）随时间走势 —— 移植自 globalpercent（Apache-2.0），适配本主题。 */
export function ProbabilityTrend({ points, height = 280 }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);
  const [themeTick, setThemeTick] = useState(0);

  useEffect(() => {
    const obs = new MutationObserver(() => setThemeTick((t) => t + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  const rows = useMemo(
    () =>
      points
        .filter((d): d is { t: number; p: number } => typeof d.t === "number" && typeof d.p === "number")
        .map((d) => {
          const date = new Date(d.t * 1000);
          return { label: `${date.getMonth() + 1}/${date.getDate()}`, value: +(d.p * 100).toFixed(1) };
        }),
    [points],
  );

  useEffect(() => {
    if (!ref.current || rows.length === 0) return;
    void themeTick; // 主题切换时重取 CSS 变量
    if (!chartRef.current) chartRef.current = init(ref.current);
    const chart = chartRef.current;
    const foreground = cssColor("--foreground");
    const mutedForeground = cssColor("--muted-foreground");
    const border = cssColor("--border", 0.45);
    const background = cssColor("--background", 0.96);
    const primary = cssColor("--primary");
    chart.setOption({
      animation: false,
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        backgroundColor: background,
        borderColor: border,
        textStyle: { color: foreground, fontSize: 12 },
        valueFormatter: (v: unknown) => `${v}%`,
      },
      xAxis: {
        type: "category",
        data: rows.map((r) => r.label),
        boundaryGap: false,
        axisLabel: { color: mutedForeground, fontSize: 11 },
        axisLine: { lineStyle: { color: border } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 100,
        axisLabel: { color: mutedForeground, fontSize: 11, formatter: "{value}%" },
        splitLine: { lineStyle: { color: cssColor("--muted", 0.35) } },
      },
      series: [
        {
          name: "Yes 概率",
          type: "line",
          smooth: true,
          showSymbol: false,
          data: rows.map((r) => r.value),
          lineStyle: { color: primary, width: 2 },
          areaStyle: { color: primary + "22" },
        },
      ],
    });
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, [rows, themeTick]);

  if (rows.length === 0) {
    return <div className="text-sm text-muted-foreground p-4">暂无趋势数据</div>;
  }
  return <div ref={ref} style={{ height }} />;
}
