import { useEffect, useMemo, useRef, useState } from "react";
import { LineChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent, AxisPointerComponent, GridComponent, TooltipComponent,
} from "echarts/components";
import { init, use, type ECharts } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { AlertCircle, Loader2, TrendingUp } from "lucide-react";
import { api, type HolderIncreaseRow, type KLineData } from "@/lib/api";

use([LineChart, ScatterChart, GridComponent, TooltipComponent, AxisPointerComponent, AriaComponent, CanvasRenderer]);

const KLINE_COUNT = 120; // 约 6 个月交易日，覆盖 35 天增持回看窗口留足前后文
const cache = new Map<string, KLineData>();

const cssColor = (name: string, alpha?: number) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return alpha == null ? `hsl(${value})` : `hsl(${value} / ${alpha})`;
};

const fmtAmount = (value: number | null | undefined) => {
  if (value == null) return "—";
  if (value >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (value >= 1e4) return `${(value / 1e4).toFixed(0)}万`;
  return value.toLocaleString("zh-CN");
};
const fmtDate = (value: string) => (value ? value.slice(5).replace("-", "/") : "—");

// 把披露/买入日期吸附到 K 线轴上最近的交易日（披露日可能落在周末/停牌日）
const snapDate = (target: string, dates: string[]) => {
  if (!target) return null;
  let hit: string | null = null;
  for (const d of dates) {
    if (d <= target) hit = d;
    else break;
  }
  return hit ?? dates[0] ?? null;
};

// 展开行内的紧凑股价图：近 120 交易日收盘价 + 增持披露日期点（散点）+ 买入区间底纹。
export function HolderPriceChart({ row }: { row: HolderIncreaseRow }) {
  const [data, setData] = useState<KLineData | null>(() => cache.get(row.code) ?? null);
  const [loading, setLoading] = useState(!cache.has(row.code));
  const [err, setErr] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);

  useEffect(() => {
    const hit = cache.get(row.code);
    if (hit) { setData(hit); setErr(null); setLoading(false); return; }
    let alive = true;
    setLoading(true);
    api.kline(row.code, "day", KLINE_COUNT)
      .then((next) => {
        if (!alive) return;
        cache.set(row.code, next);
        setData(next);
        setErr(null);
      })
      .catch((e) => { if (alive) setErr(e instanceof Error ? e.message : "股价加载失败"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [row.code]);

  // 主题切换时用新 CSS 变量重绘
  const [themeTick, setThemeTick] = useState(0);
  useEffect(() => {
    const obs = new MutationObserver(() => setThemeTick((t) => t + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  const marked = useMemo(() => {
    if (!data?.rows.length) return null;
    const dates = data.rows.map((r) => r.date);
    const closes = data.rows.map((r) => r.close);

    // 按披露日聚合增持点（同日多笔合并进一个点的提示里）
    const byDate = new Map<string, typeof row.records>();
    for (const rec of row.records) {
      const d = snapDate(rec.activity_date, dates);
      if (!d) continue;
      byDate.set(d, [...(byDate.get(d) ?? []), rec]);
    }

    // 有区间的记录画出买入区间底纹
    const bands = row.records
      .filter((r) => r.start_date && r.end_date)
      .map((r) => [snapDate(r.start_date!, dates), snapDate(r.end_date!, dates)])
      .filter((pair): pair is [string, string] => Boolean(pair[0] && pair[1]));

    return { dates, closes, byDate, bands };
  }, [data, row.records]);

  // 图表实例只初始化一次并在卸载时销毁；数据/主题变化用 setOption 原地更新，
  // 避免 dispose+init 造成悬停期间画布闪动、折线消失。
  useEffect(() => {
    if (!boxRef.current) return;
    const chart = chartRef.current ?? init(boxRef.current);
    chartRef.current = chart;
    if (!marked) return;
    const { dates, closes, byDate, bands } = marked;
    const foreground = cssColor("--foreground");
    const muted = cssColor("--muted-foreground");
    const border = cssColor("--border", 0.45);
    const background = cssColor("--background", 0.96);
    const primary = cssColor("--primary");

    const points = [...byDate.entries()].map(([date, recs]) => {
      const idx = dates.indexOf(date);
      return { value: [idx, closes[idx]], recs };
    });

    const markerLines = (recs: typeof row.records) => recs.map((rec) => {
      const range = rec.start_date && rec.end_date ? `${fmtDate(rec.start_date)}~${fmtDate(rec.end_date)}` : fmtDate(rec.activity_date);
      return `${rec.person}（${rec.identity}）· ${range} · ${fmtAmount(rec.amount)}元`;
    });

    chart.setOption({
      animation: false,
      grid: { left: 46, right: 12, top: 12, bottom: 22 },
      tooltip: {
        trigger: "axis",
        confine: true,
        axisPointer: { type: "line", lineStyle: { color: muted, opacity: 0.5 } },
        backgroundColor: background,
        borderColor: border,
        textStyle: { color: foreground, fontSize: 12 },
        extraCssText: "pointer-events:none; box-shadow: 0 10px 28px rgba(0,0,0,.28); border-radius: 10px;",
        formatter: (params: { dataIndex: number }[]) => {
          const idx = params?.[0]?.dataIndex;
          if (idx == null) return "";
          const lines = [`<b>${dates[idx]}</b>`, `收盘 <b>${closes[idx]}</b>`];
          const recs = byDate.get(dates[idx]);
          if (recs?.length) lines.push(`<span style="color:${primary}">◆ 增持披露</span>`, ...markerLines(recs));
          return lines.join("<br/>");
        },
      },
      xAxis: {
        type: "category", data: dates, boundaryGap: false,
        axisLine: { lineStyle: { color: border } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 10, hideOverlap: true },
      },
      yAxis: {
        scale: true, position: "right", splitNumber: 4,
        axisLine: { show: false },
        axisLabel: { color: muted, fontSize: 10 },
        splitLine: { lineStyle: { color: border, type: "dashed" } },
      },
      series: [
        {
          type: "line", data: closes, showSymbol: false,
          lineStyle: { width: 1.4, color: primary },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: cssColor("--primary", 0.14) },
                { offset: 1, color: cssColor("--primary", 0) },
              ],
            },
          },
          markArea: bands.length ? {
            silent: true,
            data: bands.map(([lo, hi]) => ([
              { xAxis: lo, itemStyle: { color: cssColor("--primary", 0.08) } },
              { xAxis: hi },
            ])),
          } : undefined,
        },
        {
          type: "scatter", data: points, symbolSize: 9, z: 5,
          itemStyle: { color: primary, borderColor: background, borderWidth: 1.5 },
        },
      ],
    }, true);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(boxRef.current);
    return () => observer.disconnect();
  }, [marked, themeTick]);

  // 卸载时才销毁实例
  useEffect(() => () => {
    chartRef.current?.dispose();
    chartRef.current = null;
  }, []);

  const firstDate = row.records.length ? [...row.records].sort((a, b) => a.activity_date.localeCompare(b.activity_date))[0].activity_date : "";
  const changePct = (() => {
    if (!marked || !firstDate) return null;
    const start = snapDate(firstDate, marked.dates);
    if (!start) return null;
    const a = marked.closes[marked.dates.indexOf(start)];
    const b = marked.closes[marked.dates.length - 1];
    if (!a) return null;
    return ((b - a) / a) * 100;
  })();

  return (
    <div className="rounded-xl border border-border/50 bg-black/10 p-3">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2 text-xs">
        <span className="flex items-center gap-1.5 font-semibold"><TrendingUp className="h-3.5 w-3.5 text-primary" />股价走势与增持点位</span>
        <span className="text-muted-foreground">
          近 120 交易日 · {data?.adjustment || ""}
          {changePct != null && <> · 首次增持披露（{fmtDate(firstDate)}）以来 <b className={changePct >= 0 ? "text-danger" : "text-success"}>{changePct >= 0 ? "+" : ""}{changePct.toFixed(1)}%</b></>}
          {" "}· <span className="text-primary">●</span> 增持披露点 · <span className="text-primary/30">▎</span> 买入区间
        </span>
      </div>
      {err && !data ? (
        <div className="flex h-32 items-center justify-center gap-2 text-sm text-warning"><AlertCircle className="h-4 w-4" />{err}</div>
      ) : loading && !data ? (
        <div className="flex h-32 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin text-primary" />读取股价…</div>
      ) : (
        <div ref={boxRef} className="h-48 w-full" role="img" aria-label={`${row.name}股价走势与增持点位`} />
      )}
    </div>
  );
}
