import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { MinuteKline } from "@/lib/api";

const TOTAL_MINUTES = 242;
const HEADER_HEIGHT = 58;

const cssVar = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const hsl = (name: string, alpha?: number) =>
  `hsl(${cssVar(name)}${alpha == null ? "" : ` / ${alpha}`})`;

const minuteIndex = (time: string) => {
  const value = Number.parseInt(time.slice(0, 2), 10) * 60 + Number.parseInt(time.slice(2), 10);
  return value <= 690 ? value - 570 : value - 780 + 121;
};

const clockTime = (index: number) => {
  const value = index <= 120 ? index + 570 : index + 780 - 121;
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
};

const compactVolume = (value: number) => {
  if (value >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (value >= 1e4) return `${(value / 1e4).toFixed(1)}万`;
  return String(Math.round(value));
};

type ChartSize = { width: number; height: number };

// 专业行情终端式分时图：蓝色价格主线 + 黄色成交均价 + 红绿量柱。
// 使用实际像素尺寸作为 SVG 坐标，避免 viewBox 拉伸导致文字、线条变形。
export function MinuteChart({
  data,
  height = 460,
  volRatio,
}: {
  data: MinuteKline;
  height?: number;
  volRatio?: number;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [size, setSize] = useState<ChartSize>({ width: 1000, height });
  const [, setThemeTick] = useState(0);
  const gradientId = `minute-area-${useId().replace(/:/g, "")}`;

  useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const update = () => {
      const rect = element.getBoundingClientRect();
      setSize({ width: Math.max(rect.width, 320), height: Math.max(rect.height, 320) });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeTick((tick) => tick + 1));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  const prevClose = data.prev_close;
  const points = data.points;
  const svgHeight = Math.max(size.height - HEADER_HEIGHT, 260);
  const leftAxis = size.width < 560 ? 46 : 56;
  const rightAxis = size.width < 560 ? 48 : 58;
  const chartWidth = Math.max(size.width - leftAxis - rightAxis, 180);
  const priceHeight = Math.max(180, Math.round((svgHeight - 48) * 0.76));
  const volumeTop = priceHeight + 16;
  const volumeHeight = Math.max(svgHeight - volumeTop - 28, 42);
  const panelBottom = volumeTop + volumeHeight;
  const xAt = (index: number) => leftAxis + (index / (TOTAL_MINUTES - 1)) * chartWidth;

  const model = useMemo(() => {
    const prices: (number | null)[] = new Array(TOTAL_MINUTES).fill(null);
    const volumes: number[] = new Array(TOTAL_MINUTES).fill(0);
    const orderedPoints = [...points].sort((a, b) => minuteIndex(a.time) - minuteIndex(b.time));
    let previousCumulative = 0;
    let firstIdx = TOTAL_MINUTES;
    let lastIdx = -1;

    for (const point of orderedPoints) {
      const index = minuteIndex(point.time);
      if (index < 0 || index >= TOTAL_MINUTES) continue;
      prices[index] = point.price;
      volumes[index] = Math.max(point.volume - previousCumulative, 0);
      previousCumulative = Math.max(point.volume, previousCumulative);
      firstIdx = Math.min(firstIdx, index);
      lastIdx = Math.max(lastIdx, index);
    }

    // 只填补已发生交易时段内的缺口，不把盘中最新价错误延伸至收盘。
    if (lastIdx >= 0) {
      for (let index = firstIdx + 1; index <= lastIdx; index += 1) {
        if (prices[index] == null) prices[index] = prices[index - 1];
      }
    }

    let cumulativeAmount = 0;
    let cumulativeVolume = 0;
    const averages: (number | null)[] = new Array(TOTAL_MINUTES).fill(null);
    for (let index = firstIdx; index <= lastIdx; index += 1) {
      const price = prices[index];
      if (price != null && volumes[index] > 0) {
        cumulativeAmount += price * volumes[index];
        cumulativeVolume += volumes[index];
      }
      averages[index] = cumulativeVolume > 0 ? cumulativeAmount / cumulativeVolume : price;
    }

    const observed = [
      ...prices.slice(Math.max(firstIdx, 0), lastIdx + 1),
      ...averages.slice(Math.max(firstIdx, 0), lastIdx + 1),
    ].filter((value): value is number => value != null);
    const rawMin = Math.min(...observed);
    const rawMax = Math.max(...observed);
    let low = rawMin;
    let high = rawMax;
    if (prevClose > 0) {
      const deviation = Math.max(
        Math.abs(rawMax - prevClose),
        Math.abs(prevClose - rawMin),
        prevClose * 0.002,
      );
      low = prevClose - deviation * 1.08;
      high = prevClose + deviation * 1.08;
    } else {
      const padding = Math.max((high - low) * 0.08, high * 0.001, 1e-6);
      low -= padding;
      high += padding;
    }

    const yAt = (price: number) => ((high - price) / Math.max(high - low, 1e-9)) * priceHeight;
    let pricePath = "";
    let averagePath = "";
    for (let index = firstIdx; index <= lastIdx; index += 1) {
      if (prices[index] != null) {
        pricePath += `${pricePath ? "L" : "M"}${xAt(index).toFixed(1)},${yAt(prices[index]!).toFixed(1)}`;
      }
      if (averages[index] != null) {
        averagePath += `${averagePath ? "L" : "M"}${xAt(index).toFixed(1)},${yAt(averages[index]!).toFixed(1)}`;
      }
    }
    const areaPath =
      pricePath && lastIdx >= 0
        ? `${pricePath}L${xAt(lastIdx).toFixed(1)},${priceHeight}L${xAt(firstIdx).toFixed(1)},${priceHeight}Z`
        : "";

    const maxVolume = Math.max(...volumes, 1);
    const bars = volumes.map((volume, index) => {
      const barHeight = (volume / maxVolume) * volumeHeight;
      const previousPrice = index > firstIdx ? prices[index - 1] : prevClose;
      const currentPrice = prices[index];
      return {
        x: xAt(index),
        y: volumeTop + volumeHeight - barHeight,
        height: barHeight,
        isUp: currentPrice != null && previousPrice != null ? currentPrice >= previousPrice : true,
      };
    });

    const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
      const price = high - ratio * (high - low);
      return {
        price,
        pct: prevClose > 0 ? ((price - prevClose) / prevClose) * 100 : 0,
        y: yAt(price),
      };
    });

    return {
      prices,
      volumes,
      averages,
      firstIdx,
      lastIdx,
      low,
      high,
      yAt,
      pricePath,
      averagePath,
      areaPath,
      bars,
      maxVolume,
      ticks,
    };
  }, [points, prevClose, priceHeight, volumeHeight, volumeTop, chartWidth, leftAxis]);

  if (points.length < 2 || model.lastIdx < 0) return null;

  const {
    prices,
    volumes,
    averages,
    firstIdx,
    lastIdx,
    yAt,
    pricePath,
    averagePath,
    areaPath,
    bars,
    maxVolume,
    ticks,
  } = model;
  const latestPrice = prices[lastIdx] ?? prevClose;
  const openPrice = prices[firstIdx] ?? latestPrice;
  const observedPrices = prices.slice(firstIdx, lastIdx + 1).filter((value): value is number => value != null);
  const highPrice = Math.max(...observedPrices);
  const lowPrice = Math.min(...observedPrices);
  const change = latestPrice - prevClose;
  const changePct = prevClose > 0 ? (change / prevClose) * 100 : 0;
  const trend = change > 0 ? "up" : change < 0 ? "down" : "flat";

  const priceColor = document.documentElement.classList.contains("light") ? "#2563eb" : "#62a8ff";
  const averageColor = "#f2b84b";
  const upColor = hsl("--danger");
  const downColor = hsl("--success");
  const flatColor = hsl("--muted-foreground");
  const trendColor = trend === "up" ? upColor : trend === "down" ? downColor : flatColor;
  const foreground = hsl("--foreground");
  const muted = hsl("--muted-foreground");
  const border = hsl("--border");
  const background = hsl("--background");

  const hover =
    hoverIdx != null && prices[hoverIdx] != null
      ? {
          index: hoverIdx,
          price: prices[hoverIdx]!,
          average: averages[hoverIdx],
          volume: volumes[hoverIdx],
          pct: prevClose > 0 ? ((prices[hoverIdx]! - prevClose) / prevClose) * 100 : 0,
          time: clockTime(hoverIdx),
          x: xAt(hoverIdx),
        }
      : null;

  const handlePointerMove = (event: React.PointerEvent) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    const localX = event.clientX - rect.left;
    const index = Math.round(((localX - leftAxis) / chartWidth) * (TOTAL_MINUTES - 1));
    setHoverIdx(Math.max(firstIdx, Math.min(lastIdx, index)));
  };

  const timeTicks = [
    { index: 0, label: "09:30", anchor: "start" as const },
    { index: 60, label: "10:30", anchor: "middle" as const },
    { index: 120.5, label: "11:30 / 13:00", anchor: "middle" as const },
    { index: 181, label: "14:00", anchor: "middle" as const },
    { index: 241, label: "15:00", anchor: "end" as const },
  ];
  const latestAverage = averages[lastIdx];
  const totalVolume = points[points.length - 1]?.volume ?? 0;

  return (
    <div
      ref={wrapRef}
      className="relative w-full select-none overflow-hidden"
      style={{ height }}
      onPointerMove={handlePointerMove}
      onPointerLeave={() => setHoverIdx(null)}
      role="img"
      aria-label={`${data.date}分时图，最新价${latestPrice.toFixed(2)}，涨跌幅${changePct.toFixed(2)}%`}
    >
      <div className="absolute inset-x-0 top-0 flex h-[58px] items-center justify-between gap-4 border-b border-border/35 px-4">
        <div className="flex min-w-0 items-baseline gap-2.5">
          <span className="font-mono text-[22px] font-semibold tracking-tight" style={{ color: trendColor }}>
            {latestPrice.toFixed(2)}
          </span>
          <span className="font-mono text-xs font-semibold" style={{ color: trendColor }}>
            {change > 0 ? "+" : ""}
            {change.toFixed(2)}
            <span className="ml-1.5">
              {changePct > 0 ? "+" : ""}
              {changePct.toFixed(2)}%
            </span>
          </span>
          <span className="hidden text-[10px] text-muted-foreground/50 sm:inline">截至 {clockTime(lastIdx)}</span>
        </div>
        <div className="hidden items-center gap-4 text-[10px] md:flex">
          <span className="text-muted-foreground">今开 <b className="ml-1 font-mono font-medium text-foreground">{openPrice.toFixed(2)}</b></span>
          <span className="text-muted-foreground">最高 <b className="ml-1 font-mono font-medium text-danger">{highPrice.toFixed(2)}</b></span>
          <span className="text-muted-foreground">最低 <b className="ml-1 font-mono font-medium text-success">{lowPrice.toFixed(2)}</b></span>
          <span className="text-muted-foreground">总量 <b className="ml-1 font-mono font-medium text-foreground">{compactVolume(totalVolume)}</b></span>
          {volRatio != null && (
            <span className="text-muted-foreground">
              量比{" "}
              <b className={volRatio > 1.5 ? "font-mono text-danger" : volRatio < 0.8 ? "font-mono text-success" : "font-mono text-foreground"}>
                {volRatio.toFixed(2)}
              </b>
            </span>
          )}
        </div>
      </div>

      <svg
        className="absolute inset-x-0 bottom-0"
        width="100%"
        height={svgHeight}
        viewBox={`0 0 ${size.width} ${svgHeight}`}
        preserveAspectRatio="none"
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={priceColor} stopOpacity="0.2" />
            <stop offset="68%" stopColor={priceColor} stopOpacity="0.045" />
            <stop offset="100%" stopColor={priceColor} stopOpacity="0" />
          </linearGradient>
        </defs>

        <rect x={leftAxis} y={0} width={chartWidth} height={panelBottom} fill={hsl("--muted", 0.035)} />

        {ticks.map((tick, index) => (
          <line
            key={`horizontal-${index}`}
            x1={leftAxis}
            y1={tick.y}
            x2={leftAxis + chartWidth}
            y2={tick.y}
            stroke={border}
            strokeWidth={index === 2 ? 0.8 : 0.6}
            strokeDasharray={index === 2 ? "5 4" : "2 4"}
            opacity={index === 2 ? 0.7 : 0.42}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {timeTicks.map((tick) => (
          <line
            key={`vertical-${tick.index}`}
            x1={xAt(tick.index)}
            y1={0}
            x2={xAt(tick.index)}
            y2={panelBottom}
            stroke={border}
            strokeWidth="0.6"
            strokeDasharray={tick.index === 120.5 ? "5 4" : "2 4"}
            opacity={tick.index === 120.5 ? 0.58 : 0.32}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        <line
          x1={leftAxis}
          y1={volumeTop - 8}
          x2={leftAxis + chartWidth}
          y2={volumeTop - 8}
          stroke={border}
          strokeWidth="0.7"
          opacity="0.55"
          vectorEffect="non-scaling-stroke"
        />

        {areaPath && <path d={areaPath} fill={`url(#${gradientId})`} />}
        <path
          d={pricePath}
          fill="none"
          stroke={priceColor}
          strokeWidth="1.8"
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
        <path
          d={averagePath}
          fill="none"
          stroke={averageColor}
          strokeWidth="1.15"
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity="0.95"
          vectorEffect="non-scaling-stroke"
        />

        {bars.slice(firstIdx, lastIdx + 1).map((bar, offset) => (
          <rect
            key={firstIdx + offset}
            x={bar.x - Math.max(chartWidth / TOTAL_MINUTES / 2 - 0.35, 0.65)}
            y={bar.y}
            width={Math.max(chartWidth / TOTAL_MINUTES - 0.7, 1.3)}
            height={bar.height}
            rx="0.5"
            fill={bar.isUp ? upColor : downColor}
            opacity="0.62"
          />
        ))}

        <line
          x1={xAt(lastIdx)}
          y1={yAt(latestPrice)}
          x2={leftAxis + chartWidth}
          y2={yAt(latestPrice)}
          stroke={trendColor}
          strokeWidth="0.8"
          strokeDasharray="3 3"
          opacity="0.48"
          vectorEffect="non-scaling-stroke"
        />
        <circle cx={xAt(lastIdx)} cy={yAt(latestPrice)} r="3.2" fill={priceColor} stroke={background} strokeWidth="1.5" />

        {hover && (
          <>
            <line
              x1={hover.x}
              y1={0}
              x2={hover.x}
              y2={panelBottom}
              stroke={muted}
              strokeWidth="0.8"
              strokeDasharray="3 3"
              opacity="0.62"
              vectorEffect="non-scaling-stroke"
            />
            <line
              x1={leftAxis}
              y1={yAt(hover.price)}
              x2={leftAxis + chartWidth}
              y2={yAt(hover.price)}
              stroke={muted}
              strokeWidth="0.8"
              strokeDasharray="3 3"
              opacity="0.48"
              vectorEffect="non-scaling-stroke"
            />
            <circle cx={hover.x} cy={yAt(hover.price)} r="3.5" fill={priceColor} stroke={background} strokeWidth="1.5" />
          </>
        )}

        {ticks.map((tick, index) => (
          <g key={`axis-${index}`}>
            <text
              x={leftAxis - 8}
              y={Math.min(Math.max(tick.y, 9), priceHeight - 7)}
              textAnchor="end"
              dominantBaseline="middle"
              fontSize="10"
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
              fill={foreground}
              opacity="0.68"
            >
              {tick.price.toFixed(2)}
            </text>
            <text
              x={leftAxis + chartWidth + 8}
              y={Math.min(Math.max(tick.y, 9), priceHeight - 7)}
              textAnchor="start"
              dominantBaseline="middle"
              fontSize="10"
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
              fill={tick.pct > 0.0001 ? upColor : tick.pct < -0.0001 ? downColor : muted}
              opacity="0.78"
            >
              {tick.pct > 0 ? "+" : ""}
              {tick.pct.toFixed(2)}%
            </text>
          </g>
        ))}

        <text
          x={leftAxis - 8}
          y={volumeTop + 2}
          textAnchor="end"
          dominantBaseline="hanging"
          fontSize="9"
          fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
          fill={muted}
          opacity="0.6"
        >
          {compactVolume(maxVolume)}
        </text>
        {timeTicks.map((tick) => (
          <text
            key={`time-${tick.index}`}
            x={xAt(tick.index)}
            y={panelBottom + 12}
            textAnchor={tick.anchor}
            dominantBaseline="middle"
            fontSize="10"
            fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
            fill={muted}
            opacity="0.62"
          >
            {tick.label}
          </text>
        ))}
      </svg>

      <div className="pointer-events-none absolute left-[64px] top-[66px] flex items-center gap-3 text-[10px]">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <i className="h-0.5 w-3 rounded-full" style={{ backgroundColor: priceColor }} />
          价格
        </span>
        {latestAverage != null && (
          <span className="flex items-center gap-1.5 text-muted-foreground">
            <i className="h-0.5 w-3 rounded-full" style={{ backgroundColor: averageColor }} />
            均价 <b className="font-mono font-medium" style={{ color: averageColor }}>{latestAverage.toFixed(2)}</b>
          </span>
        )}
      </div>

      {hover && (
        <div
          className="pointer-events-none absolute z-10 min-w-[148px] rounded-lg border border-border/55 bg-background/95 px-3 py-2 text-[10px] shadow-xl backdrop-blur-md"
          style={{
            left: Math.min(Math.max(hover.x, leftAxis), size.width - rightAxis),
            top: HEADER_HEIGHT + 14,
            transform: hover.x > size.width * 0.66 ? "translateX(calc(-100% - 10px))" : "translateX(10px)",
          }}
        >
          <div className="mb-1.5 flex items-center justify-between gap-5 text-muted-foreground">
            <span>{data.date}</span>
            <b className="font-mono font-medium text-foreground">{hover.time}</b>
          </div>
          <div className="grid grid-cols-[auto_auto] gap-x-4 gap-y-1">
            <span className="text-muted-foreground">价格</span>
            <b className="text-right font-mono" style={{ color: hover.pct >= 0 ? upColor : downColor }}>
              {hover.price.toFixed(2)}　{hover.pct > 0 ? "+" : ""}
              {hover.pct.toFixed(2)}%
            </b>
            <span className="text-muted-foreground">均价</span>
            <b className="text-right font-mono font-medium" style={{ color: averageColor }}>
              {hover.average?.toFixed(2) ?? "—"}
            </b>
            <span className="text-muted-foreground">成交量</span>
            <b className="text-right font-mono font-medium text-foreground">{hover.volume > 0 ? compactVolume(hover.volume) : "—"}</b>
          </div>
        </div>
      )}
    </div>
  );
}
