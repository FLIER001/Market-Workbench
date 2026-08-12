import { useEffect, useRef, useState } from "react";
import {
  BarChart,
  CandlestickChart as EChartsCandlestickChart,
  LineChart,
} from "echarts/charts";
import {
  AriaComponent,
  AxisPointerComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { init, use, type ECharts, type EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { AlertCircle, ChartCandlestick, Loader2, RefreshCw } from "lucide-react";
import { api, type KLineData, type KLineRow, type MinuteKline } from "@/lib/api";
import { isTradingHours } from "@/hooks/useLiveQuotes";
import { cn } from "@/lib/utils";
import { GlassCard } from "./GlassCard";
import { MinuteChart } from "./MinuteChart";

use([
  EChartsCandlestickChart,
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  AxisPointerComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
  CanvasRenderer,
]);

type Period = KLineData["period"] | "minute";

const PERIODS: { id: Period; label: string; count: number; visible: number }[] = [
  { id: "minute", label: "分时", count: 1, visible: 1 },
  { id: "day", label: "日K", count: 250, visible: 250 },
  { id: "week", label: "周K", count: 200, visible: 80 },
  { id: "month", label: "月K", count: 120, visible: 60 },
];

const memoryCache = new Map<string, KLineData>();
const minuteCache = new Map<string, MinuteKline>();

const movingAverage = (rows: KLineRow[], days: number): (number | "-")[] =>
  rows.map((_, index) => {
    if (index < days - 1) return "-";
    const window = rows.slice(index - days + 1, index + 1);
    return Number((window.reduce((sum, row) => sum + row.close, 0) / days).toFixed(3));
  });

const compactVolume = (value: number) => {
  if (value >= 1e8) return `${(value / 1e8).toFixed(1)}亿`;
  if (value >= 1e4) return `${(value / 1e4).toFixed(1)}万`;
  return String(Math.round(value));
};

const cssColor = (name: string, alpha?: number) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return alpha == null ? `hsl(${value})` : `hsl(${value} / ${alpha})`;
};

function chartOption(data: KLineData, visibleCount: number): EChartsCoreOption {
  const { rows } = data;
  const dates = rows.map((row) => row.date);
  const up = cssColor("--danger");
  const down = cssColor("--success");
  const foreground = cssColor("--foreground");
  const mutedForeground = cssColor("--muted-foreground");
  const border = cssColor("--border", 0.45);
  const background = cssColor("--background", 0.96);
  const start = Math.max(0, ((rows.length - visibleCount) / Math.max(rows.length, 1)) * 100);

  return {
    animation: false,
    aria: {
      enabled: true,
      description: `${data.code} ${data.adjustment}${data.period === "day" ? "日" : data.period === "week" ? "周" : "月"}K线，共 ${rows.length} 个观察点。`,
    },
    color: ["#f6c453", "#62a8ff", "#b08cff"],
    legend: {
      top: 4,
      left: 8,
      itemWidth: 14,
      itemHeight: 2,
      textStyle: { color: mutedForeground, fontSize: 11 },
      data: ["MA20", "MA200"],
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", lineStyle: { color: mutedForeground, opacity: 0.55 } },
      backgroundColor: background,
      borderColor: border,
      textStyle: { color: foreground, fontSize: 12 },
      extraCssText: "box-shadow: 0 10px 28px rgba(0,0,0,.28); border-radius: 10px;",
      formatter: (params: any[]) => {
        const index = params?.[0]?.dataIndex;
        const row = rows[index];
        if (!row) return "";
        const tone = row.close >= row.open ? up : down;
        const change = row.open ? ((row.close - row.open) / row.open) * 100 : 0;
        return [
          `<b>${row.date}</b>`,
          `开 <b>${row.open}</b>　收 <b style="color:${tone}">${row.close}</b>`,
          `高 <b>${row.high}</b>　低 <b>${row.low}</b>`,
          `涨跌 <b style="color:${tone}">${change > 0 ? "+" : ""}${change.toFixed(2)}%</b>`,
          `成交量 <b>${compactVolume(row.volume)}</b>`,
        ].join("<br/>");
      },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 12, right: 58, top: 42, height: "58%" },
      { left: 12, right: 58, top: "73%", height: "13%" },
    ],
    xAxis: [
      {
        type: "category",
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: border } },
        axisLabel: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        min: "dataMin",
        max: "dataMax",
      },
      {
        type: "category",
        gridIndex: 1,
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: border } },
        axisLabel: { color: mutedForeground, fontSize: 10, hideOverlap: true },
        axisTick: { show: false },
        splitLine: { show: false },
        min: "dataMin",
        max: "dataMax",
      },
    ],
    yAxis: [
      {
        scale: true,
        position: "right",
        splitNumber: 5,
        axisLine: { show: false },
        axisLabel: { color: mutedForeground, fontSize: 10 },
        splitLine: { lineStyle: { color: border, type: "dashed" } },
      },
      {
        scale: true,
        gridIndex: 1,
        position: "right",
        splitNumber: 2,
        axisLine: { show: false },
        axisLabel: {
          color: mutedForeground,
          fontSize: 9,
          formatter: (value: number) => compactVolume(value),
        },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      {
        type: "inside",
        xAxisIndex: [0, 1],
        start,
        end: 100,
        minValueSpan: 10,
      },
      {
        type: "slider",
        xAxisIndex: [0, 1],
        start,
        end: 100,
        bottom: 4,
        height: 18,
        borderColor: "transparent",
        backgroundColor: cssColor("--muted", 0.35),
        fillerColor: cssColor("--primary", 0.14),
        dataBackground: {
          lineStyle: { color: mutedForeground, opacity: 0.35 },
          areaStyle: { color: mutedForeground, opacity: 0.08 },
        },
        selectedDataBackground: {
          lineStyle: { color: cssColor("--primary"), opacity: 0.6 },
          areaStyle: { color: cssColor("--primary"), opacity: 0.12 },
        },
        handleStyle: { color: cssColor("--primary"), borderColor: cssColor("--primary") },
        textStyle: { color: mutedForeground, fontSize: 9 },
      },
    ],
    series: [
      {
        name: "K线",
        type: "candlestick",
        data: rows.map((row) => [row.open, row.close, row.low, row.high]),
        itemStyle: {
          color: up,
          color0: down,
          borderColor: up,
          borderColor0: down,
        },
      },
      {
        name: "MA20",
        type: "line",
        data: movingAverage(rows, 20),
        showSymbol: false,
        smooth: false,
        lineStyle: { width: 1.1, color: "#f6c453" },
      },
      {
        name: "MA200",
        type: "line",
        data: movingAverage(rows, 200),
        showSymbol: false,
        smooth: false,
        lineStyle: { width: 1.1, color: "#b08cff" },
      },
      {
        name: "成交量",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: rows.map((row) => ({
          value: row.volume,
          itemStyle: { color: row.close >= row.open ? up : down, opacity: 0.65 },
        })),
        barMaxWidth: 10,
      },
    ],
  };
}

export function StockKLineChart({ code, name, volRatio }: { code: string; name: string; volRatio?: number }) {
  const [period, setPeriod] = useState<Period>("day");
  const [data, setData] = useState<KLineData | null>(null);
  const [minuteData, setMinuteData] = useState<MinuteKline | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const chartRef = useRef<HTMLDivElement>(null);
  const requestIdRef = useRef(0);

  const load = (nextPeriod: Period, force = false) => {
    const cacheKey = `${code}:${nextPeriod}`;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);

    if (nextPeriod === "minute") {
      const cached = minuteCache.get(cacheKey);
      if (cached && !force) setMinuteData(cached);
      api.minuteKline(code)
        .then((next) => {
          if (requestId !== requestIdRef.current) return;
          minuteCache.set(cacheKey, next);
          setMinuteData(next);
        })
        .catch((reason) => {
          if (requestId !== requestIdRef.current) return;
          setError(reason instanceof Error ? reason.message : "分时数据加载失败");
        })
        .finally(() => {
          if (requestId === requestIdRef.current) setLoading(false);
        });
      return;
    }

    const config = PERIODS.find((item) => item.id === nextPeriod)!;
    const cached = memoryCache.get(cacheKey);
    if (cached && !force) setData(cached);
    api.kline(code, nextPeriod as KLineData["period"], config.count, force)
      .then((next) => {
        if (requestId !== requestIdRef.current) return;
        memoryCache.set(cacheKey, next);
        setData(next);
      })
      .catch((reason) => {
        if (requestId !== requestIdRef.current) return;
        setError(reason instanceof Error ? reason.message : "K线加载失败");
      })
      .finally(() => {
        if (requestId === requestIdRef.current) setLoading(false);
      });
  };

  useEffect(() => {
    if (period === "minute") {
      setMinuteData(minuteCache.get(`${code}:${period}`) || null);
    } else {
      setData(memoryCache.get(`${code}:${period}`) || null);
    }
    load(period);
    // code 切换后重新读取，period 由按钮事件更新。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, period]);

  useEffect(() => {
    if (period !== "minute") return;
    const tick = () => {
      if (!document.hidden && isTradingHours()) load("minute", true);
    };
    const timer = window.setInterval(tick, 30_000);
    const onVisible = () => { if (!document.hidden) tick(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // `load` intentionally reads current code/period from this render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, period]);

  useEffect(() => {
    if (!chartRef.current || period === "minute" || !data?.rows.length) return;
    const chart: ECharts = init(chartRef.current);
    const config = PERIODS.find((item) => item.id === period)!;
    chart.setOption(chartOption(data, config.visible), true);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(chartRef.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [data, period]);

  const latest = data?.rows[data.rows.length - 1];

  return (
    <GlassCard className="mb-4 p-0">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/50 px-4 py-3">
        <div>
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <ChartCandlestick className="h-4 w-4 text-primary" />
            {period === "minute" ? `${name} 分时走势` : `${name} K线`}
          </h3>
          <p className="mt-1 text-[11px] text-muted-foreground/65">
            {period === "minute"
              ? minuteData ? `${minuteData.date} · ${minuteData.points.length} 分钟点 · 昨收 ${minuteData.prev_close}` : "分时图（当日分钟级）"
              : data ? `${data.adjustment} · ${data.source} · ${data.rows.length} 个观察点${data.as_of ? ` · 截至 ${data.as_of}` : ""}` : "价格与成交量"}
          </p>
        </div>
        <div className="flex items-center gap-1">
          {PERIODS.map((item) => (
            <button
              key={item.id}
              onClick={() => setPeriod(item.id)}
              className={cn(
                "rounded-md px-2.5 py-1 text-xs transition-colors",
                period === item.id
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
              )}
            >
              {item.label}
            </button>
          ))}
          <button
            onClick={() => load(period, true)}
            disabled={loading}
            className="ml-1 rounded-md p-1 text-muted-foreground hover:bg-muted/50 hover:text-primary disabled:opacity-50"
            title="刷新K线"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          </button>
        </div>
      </div>

      {period === "minute" ? (
        error && !minuteData ? (
          <div className="flex h-[420px] items-center justify-center gap-2 px-4 text-sm text-warning">
            <AlertCircle className="h-4 w-4" />
            {error}
          </div>
        ) : !minuteData ? (
          <div className="flex h-[420px] items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />
            正在读取分时数据…
          </div>
        ) : (
          <>
            {error && (
              <div className="mx-4 mt-3 flex items-center gap-1.5 rounded-md bg-warning/10 px-2.5 py-1.5 text-[11px] text-warning">
                <AlertCircle className="h-3 w-3" /> 刷新失败：{error}
              </div>
            )}
            <MinuteChart data={minuteData} height={460} volRatio={volRatio} />
          </>
        )
      ) : error && !data ? (
        <div className="flex h-[420px] items-center justify-center gap-2 px-4 text-sm text-warning">
          <AlertCircle className="h-4 w-4" />
          {error}
        </div>
      ) : !data ? (
        <div className="flex h-[420px] items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          正在读取K线…
        </div>
      ) : (
        <>
          {loading && (
            <div className="absolute opacity-0" aria-live="polite">正在更新K线</div>
          )}
          {error && (
            <div className="mx-4 mt-3 flex items-center gap-1.5 rounded-md bg-warning/10 px-2.5 py-1.5 text-[11px] text-warning">
              <AlertCircle className="h-3 w-3" /> 刷新失败，当前继续显示上次数据：{error}
            </div>
          )}
          <div
            ref={chartRef}
            className="h-[460px] w-full"
            role="img"
            aria-label={`${name}的${data!.adjustment}${period === "day" ? "日" : period === "week" ? "周" : "月"}K线与成交量`}
          />
          {latest && (
            <p className="border-t border-border/40 px-4 py-2 text-[10px] text-muted-foreground/55">
              最新一根：开 {latest.open} · 高 {latest.high} · 低 {latest.low} · 收 {latest.close} · 成交量 {compactVolume(latest.volume)}
              {" · "}拖动底部滑块或滚轮缩放，悬停查看 OHLC
            </p>
          )}
        </>
      )}
    </GlassCard>
  );
}

export default StockKLineChart;
