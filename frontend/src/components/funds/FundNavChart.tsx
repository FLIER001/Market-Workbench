import { useEffect, useMemo, useRef, useState } from "react";
import { LineChart } from "echarts/charts";
import {
  AriaComponent, AxisPointerComponent, DataZoomComponent, GridComponent, TooltipComponent,
} from "echarts/components";
import { init, use, type ECharts } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { AlertCircle, Loader2 } from "lucide-react";
import { api, type FundNavHistory } from "@/lib/api";

use([LineChart, GridComponent, TooltipComponent, AxisPointerComponent, DataZoomComponent, AriaComponent, CanvasRenderer]);

const RANGES = [
  { id: "3m", label: "3月", days: 63 },
  { id: "6m", label: "6月", days: 126 },
  { id: "1y", label: "1年", days: 250 },
  { id: "3y", label: "3年", days: 750 },
  { id: "all", label: "全部", days: 4000 },
] as const;

const cache = new Map<string, FundNavHistory>();

const cssColor = (name: string, alpha?: number) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return alpha == null ? `hsl(${value})` : `hsl(${value} / ${alpha})`;
};

// 单位净值走势（等比刻度，归一化涨幅更直观）。浅色网格 + 面积渐变的终端风。
export function FundNavChart({ code }: { code: string }) {
  const [range, setRange] = useState<(typeof RANGES)[number]["id"]>("1y");
  const [data, setData] = useState<FundNavHistory | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);

  useEffect(() => {
    const limit = RANGES.find((r) => r.id === range)!.days;
    const key = `${code}:${limit}`;
    const hit = cache.get(key);
    if (hit) { setData(hit); setErr(null); return; }
    setLoading(true);
    api.fundNav(code, limit)
      .then((d) => { cache.set(key, d); setData(d); setErr(null); })
      .catch((e) => { setErr(e?.message || "净值加载失败"); setData(null); })
      .finally(() => setLoading(false));
  }, [code, range]);

  const [themeTick, setThemeTick] = useState(0);
  useEffect(() => {
    const obs = new MutationObserver(() => setThemeTick((t) => t + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  const option = useMemo(() => {
    if (!data || data.rows.length === 0) return null;
    void themeTick; // 主题切换时重取 CSS 变量
    const rows = data.rows;
    const first = rows[0].nav;
    const foreground = cssColor("--foreground");
    const mutedForeground = cssColor("--muted-foreground");
    const border = cssColor("--border", 0.45);
    const background = cssColor("--background", 0.96);
    const primary = cssColor("--primary");
    return {
      animation: false,
      grid: { left: 46, right: 14, top: 14, bottom: 22 },
      tooltip: {
        trigger: "axis",
        backgroundColor: background, borderColor: border,
        textStyle: { color: foreground, fontSize: 12 },
        extraCssText: "box-shadow: 0 10px 28px rgba(0,0,0,.28); border-radius: 10px;",
        formatter: (ps: any) => {
          const p = Array.isArray(ps) ? ps[0] : ps;
          const i = p.dataIndex as number;
          const r = rows[i];
          const pct = ((r.nav / first - 1) * 100).toFixed(2);
          const day = r.day_pct != null ? `（日 ${r.day_pct > 0 ? "+" : ""}${r.day_pct}%）` : "";
          return `${r.date}<br/>净值 <b>${r.nav}</b>${day}<br/>区间 ${pct}%`;
        },
      },
      xAxis: {
        type: "category", data: rows.map((r) => r.date),
        axisLine: { lineStyle: { color: border } },
        axisLabel: { color: mutedForeground, fontSize: 10 }, axisTick: { show: false },
      },
      yAxis: {
        type: "value", scale: true,
        axisLabel: { color: mutedForeground, fontSize: 10 },
        splitLine: { lineStyle: { color: cssColor("--muted", 0.35) } },
      },
      dataZoom: [{ type: "inside" }],
      series: [{
        type: "line", data: rows.map((r) => r.nav), showSymbol: false, smooth: 0.2,
        lineStyle: { width: 1.6, color: primary },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: cssColor("--primary", 0.28) },
              { offset: 1, color: cssColor("--primary", 0.02) },
            ],
          },
        },
      }],
    };
  }, [data, themeTick]);

  useEffect(() => {
    if (!boxRef.current || !option) return;
    const chart = chartRef.current ?? init(boxRef.current);
    chartRef.current = chart;
    chart.setOption(option as any, true);
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [option]);

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  return (
    <div>
      <div className="mb-2 flex items-center gap-1">
        {RANGES.map((r) => (
          <button
            key={r.id}
            onClick={() => setRange(r.id)}
            className={`rounded-lg px-2 py-1 text-xs transition ${range === r.id ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-black/20"}`}
          >
            {r.label}
          </button>
        ))}
        {loading && <Loader2 className="ml-2 h-3.5 w-3.5 animate-spin text-muted-foreground" />}
      </div>
      {err ? (
        <div className="flex h-44 items-center justify-center gap-2 text-sm text-muted-foreground">
          <AlertCircle className="h-4 w-4" /> {err}
        </div>
      ) : (
        <div ref={boxRef} className="h-44 w-full" />
      )}
    </div>
  );
}
