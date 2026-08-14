import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { MinuteKline } from "@/lib/api";

const HEADER_HEIGHT = 58;
const COMPACT_HEADER_HEIGHT = 0;
// A 股交易日分钟槽数（09:30-11:30 / 13:00-15:00），用于决定 x 轴刻度密度
const A_SHARE_MINUTES = 242;

const cssVar = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const hsl = (name: string, alpha?: number) =>
  `hsl(${cssVar(name)}${alpha == null ? "" : ` / ${alpha}`})`;

const compactVolume = (value: number) => {
  if (value >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (value >= 1e4) return `${(value / 1e4).toFixed(1)}万`;
  return String(Math.round(value));
};

// "0930" → 当日第 N 分钟（自午夜）
const minuteOfDay = (time: string) =>
  {
    const [h, m] = time.includes(":") ? time.split(":") : [time.slice(0, 2), time.slice(2)];
    return Number.parseInt(h, 10) * 60 + Number.parseInt(m, 10);
  };

// 当日第 N 分钟 → "09:30"；跨午夜槽位（如 26:30）显示为正常时钟（02:30）
const clockLabel = (mod: number) =>
  `${String(Math.floor((mod % (24 * 60)) / 60)).padStart(2, "0")}:${String(mod % 60).padStart(2, "0")}`;

// 均匀抽 5 个 x 轴刻度（按分钟槽下标）
function buildTimeTicks(total: number): { index: number; label: string; anchor: "start" | "middle" | "end" }[] {
  if (total <= 1) return [{ index: 0, label: "", anchor: "start" }];
  return [0, 0.25, 0.5, 0.75, 1].map((r, i) => ({
    index: Math.round(r * (total - 1)),
    label: "",
    anchor: (i === 0 ? "start" : i === 4 ? "end" : "middle") as "start" | "middle" | "end",
  }));
}

type ChartSize = { width: number; height: number };

// 专业行情终端式分时图：蓝色价格主线 + 黄色成交均价 + 红绿量柱。
// 使用实际像素尺寸作为 SVG 坐标，避免 viewBox 拉伸导致文字、线条变形。
export function MinuteChart({
  data,
  height = 460,
  volRatio,
  compact = false,
}: {
  data: MinuteKline;
  height?: number;
  volRatio?: number;
  compact?: boolean;
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
      // compact 模式贴合容器实际高度；普通模式保留 320 下限（header+量柱）。
      setSize({ width: Math.max(rect.width, 320), height: compact ? Math.max(rect.height, 60) : Math.max(rect.height, 320) });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [compact]);

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeTick((tick) => tick + 1));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  const prevClose = data.prev_close;
  const points = data.points;
  // x 轴分钟槽数：优先用后端注入的 market_minutes（完整交易时段）计算；
  // 无 market_minutes（A 股个股）时回退到实际数据跨度与 A 股 242 的较大值。
  const { totalMinutes, slotOffset, slotRanges } = useMemo(() => {
    const mm = data.market_minutes;
    if (mm && mm.length > 0) {
      // 跨午夜处理：close_mod 可能 > 1440（如美股 close=1680=次日04:00）
      // 统一把每个 session 的 close 规范化到 > open 的连续区间
      const ranges: { start: number; end: number }[] = [];
      for (const [s, e] of mm) {
        let open = s;
        let close = e;
        // 跨午夜（close <= open）：close 加 1440 变成次日
        if (close <= open) close += 24 * 60;
        ranges.push({ start: open, end: close });
      }
      // 合并重叠/连续区间，计算总跨度。
      // 排序：跨午夜的时段（如 21:00–次日02:30）应排最前，让夜盘成为坐标原点。
      ranges.sort((a, b) => {
        const aOvernight = a.end > 24 * 60;
        const bOvernight = b.end > 24 * 60;
        if (aOvernight && !bOvernight) return -1;
        if (!aOvernight && bOvernight) return 1;
        return a.start - b.start;
      });
      const merged: { start: number; end: number }[] = [];
      for (const r of ranges) {
        const last = merged[merged.length - 1];
        if (last && r.start <= last.end) {
          last.end = Math.max(last.end, r.end);
        } else {
          merged.push({ ...r });
        }
      }
      const totalSpan = merged.reduce((sum, r) => sum + (r.end - r.start) + 1, 0);
      const offset = merged[0].start;
      const slotR = merged.map(r => ({ start: r.start - offset, end: r.end - offset }));
      return { totalMinutes: Math.max(totalSpan, A_SHARE_MINUTES), slotOffset: offset, slotRanges: slotR };
    }
    // A 股或无 market_minutes：回退到原逻辑
    if (points.length === 0) return { totalMinutes: A_SHARE_MINUTES, slotOffset: 0, slotRanges: null };
    const mods = points.map((p) => minuteOfDay(p.time));
    const minMod = Math.min(...mods);
    const span = Math.max(...mods) - minMod + 1;
    return { totalMinutes: Math.max(A_SHARE_MINUTES, Math.min(span, 24 * 60)), slotOffset: minMod, slotRanges: null };
  }, [points, data.market_minutes]);
  const headerH = compact ? COMPACT_HEADER_HEIGHT : HEADER_HEIGHT;
  // compact 模式：图表贴合容器高度（无 header）；普通模式保留 260 下限。
  const svgHeight = compact ? size.height : Math.max(size.height - headerH, 260);
  const leftAxis = size.width < 560 ? 46 : 56;
  const rightAxis = size.width < 560 ? 48 : 58;
  const chartWidth = Math.max(size.width - leftAxis - rightAxis, 180);
  // compact：价格区占满全高、不画量柱；普通：价格 76% + 量柱 24%。
  const priceHeight = compact ? svgHeight : Math.max(180, Math.round((svgHeight - 48) * 0.76));
  const volumeTop = compact ? svgHeight : priceHeight + 16;
  const volumeHeight = compact ? 0 : Math.max(svgHeight - volumeTop - 28, 42);
  const panelBottom = volumeTop + volumeHeight;
  const xAt = (index: number) => leftAxis + (index / (totalMinutes - 1)) * chartWidth;

  const model = useMemo(() => {
    // 分钟槽下标 = 相对该市场首个交易时段开盘的偏移
    // 有 slotRanges 时跳过午休空隙（多段交易时段按交易分钟数压缩）
    const firstMod = slotOffset ?? Math.min(...points.map((p) => minuteOfDay(p.time)));
    const slot = (time: string) => {
      let mod = minuteOfDay(time);
      if (slotRanges) {
        // 跨午夜市场（如美股 21:30→次日04:00）：数据时间戳可能已过零点，
        // 需加 1440 对齐到 market_minutes 的连续区间
        if (mod < slotOffset) mod += 24 * 60;
        // 有交易时段定义：按实际交易分钟数映射（跳过午休）
        let acc = 0;
        for (const r of slotRanges) {
          if (mod - slotOffset >= r.start && mod - slotOffset <= r.end) {
            return Math.min(acc + (mod - slotOffset - r.start), totalMinutes - 1);
          }
          acc += r.end - r.start;
        }
        // 不在任何已知时段内：返回最近的边界
        return Math.min(acc, totalMinutes - 1);
      }
      return Math.min(mod - firstMod, totalMinutes - 1);
    };
    const prices: (number | null)[] = new Array(totalMinutes).fill(null);
    const volumes: number[] = new Array(totalMinutes).fill(0);
    // 隔夜市场（如美股 21:30→次日 04:00）：原始时钟分钟用于跨零点排序
    const rawMod: (number | null)[] = new Array(totalMinutes).fill(null);
    const orderedPoints = [...points].sort((a, b) => slot(a.time) - slot(b.time));
    let previousCumulative = 0;
    let firstIdx = totalMinutes;
    let lastIdx = -1;

    for (const point of orderedPoints) {
      const index = slot(point.time);
      if (index < 0 || index >= totalMinutes) continue;
      prices[index] = point.price;
      rawMod[index] = minuteOfDay(point.time);
      volumes[index] = Math.max(point.volume - previousCumulative, 0);
      previousCumulative = Math.max(point.volume, previousCumulative);
      firstIdx = Math.min(firstIdx, index);
      if (index > lastIdx) lastIdx = index;
    }

    // 期货跨午夜：时间戳 00:00–02:30 会被 slot 映射到 24h+ 的连续槽位，
    // 但数据点排序后它们排在最后，导致 lastIdx 被错压到 00:00 附近。
    // 修正为最后一个真正有数据的槽位。
    if (slotRanges) {
      let maxDataIdx = -1;
      for (let i = 0; i < totalMinutes; i++) {
        if (prices[i] != null) maxDataIdx = i;
      }
      if (maxDataIdx >= 0) lastIdx = maxDataIdx;
    }

    // 只填补已发生交易时段内的缺口，不把盘中最新价错误延伸至收盘。
    // 有 slotRanges 时只填补同一时段内的空隙（跳过午休），无则全段填补。
    if (lastIdx >= 0) {
      for (let index = firstIdx + 1; index <= lastIdx; index += 1) {
        if (prices[index] == null) {
          // 无 slotRanges 或同一交易时段内：前向填充
          if (!slotRanges) {
            prices[index] = prices[index - 1];
          } else {
            // 检查此 index 是否在某个交易时段范围内
            let acc = 0;
            let inRange = false;
            for (const r of slotRanges) {
              if (index >= acc && index < acc + (r.end - r.start)) {
                inRange = true;
                break;
              }
              acc += r.end - r.start;
            }
            if (inRange) prices[index] = prices[index - 1];
            // 不在交易时段（午休区）的空隙保持 null，不填充
          }
        }
      }
    }

    let cumulativeAmount = 0;
    let cumulativeVolume = 0;
    const averages: (number | null)[] = new Array(totalMinutes).fill(null);
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
      rawMod,
      firstMod,
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
  }, [points, prevClose, totalMinutes, priceHeight, volumeHeight, volumeTop, chartWidth, leftAxis]);

  if (points.length < 2 || model.lastIdx < 0) return null;

  const {
    prices,
    volumes,
    averages,
    firstIdx,
    lastIdx,
    rawMod,
    firstMod,
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

  // 该槽位的真实时钟：优先用 rawMod（实际数据时间戳），无数据时按 slotRanges 反推
  const clockAt = (index: number) => {
    if (rawMod[index] != null) return clockLabel(rawMod[index]!);
    // 无数据点覆盖：从 slot 索引反推到实际时钟分钟（处理午休跳过）
    if (slotRanges) {
      let acc = 0;
      for (const r of slotRanges) {
        if (index >= acc && index < acc + (r.end - r.start)) {
          const mod = slotOffset + r.start + (index - acc);
          // 跨午夜：mod 可能 > 1440，取模
          return clockLabel(mod % (24 * 60));
        }
        acc += r.end - r.start;
      }
      // 超出所有交易时段（保底放大的空槽）：钳制到最后一个交易时段的收盘时刻
      const lastR = slotRanges[slotRanges.length - 1];
      return clockLabel((slotOffset + lastR.end) % (24 * 60));
    }
    return clockLabel((firstMod + index) % (24 * 60));
  };

  const hover =
    hoverIdx != null && prices[hoverIdx] != null
      ? {
          index: hoverIdx,
          price: prices[hoverIdx]!,
          average: averages[hoverIdx],
          volume: volumes[hoverIdx],
          pct: prevClose > 0 ? ((prices[hoverIdx]! - prevClose) / prevClose) * 100 : 0,
          time: clockAt(hoverIdx),
          x: xAt(hoverIdx),
        }
      : null;

  const handlePointerMove = (event: React.PointerEvent) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    const localX = event.clientX - rect.left;
    const index = Math.round(((localX - leftAxis) / chartWidth) * (totalMinutes - 1));
    setHoverIdx(Math.max(firstIdx, Math.min(lastIdx, index)));
  };

  // 按交易时段边界标注刻度（每个时段的开/收盘），比均匀抽样更直观
  const timeTicks = useMemo(() => {
    if (!slotRanges) {
      const ticks = buildTimeTicks(totalMinutes);
      for (const t of ticks) t.label = clockAt(t.index);
      return ticks;
    }
    // 只标注关键节点：夜盘开始、休市、日盘收盘
    const ticks: { index: number; label: string; anchor: "start" | "middle" | "end" }[] = [];
    let acc = 0;
    let dayStartIdx = 0;
    for (let i = 0; i < slotRanges.length; i++) {
      if (i === 1) dayStartIdx = acc; // 第二段（日盘）开始前
      acc += slotRanges[i].end - slotRanges[i].start;
    }
    const totalEnd = acc;
    ticks.push({ index: 0, label: clockAt(0), anchor: "start" });
    ticks.push({ index: dayStartIdx, label: "休市", anchor: "middle" });
    ticks.push({ index: totalEnd, label: clockAt(totalEnd), anchor: "end" });
    return ticks;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [totalMinutes, firstMod, rawMod, slotRanges]);
  const latestAverage = averages[lastIdx];
  const totalVolume = points[points.length - 1]?.volume ?? 0;
  const hasVolume = totalVolume > 0;

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
      {!compact && (
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
            <span className="hidden text-[10px] text-muted-foreground/50 sm:inline">截至 {clockAt(lastIdx)}</span>
          </div>
          <div className="hidden items-center gap-4 text-[10px] md:flex">
            <span className="text-muted-foreground">今开 <b className="ml-1 font-mono font-medium text-foreground">{openPrice.toFixed(2)}</b></span>
            <span className="text-muted-foreground">最高 <b className="ml-1 font-mono font-medium text-danger">{highPrice.toFixed(2)}</b></span>
            <span className="text-muted-foreground">最低 <b className="ml-1 font-mono font-medium text-success">{lowPrice.toFixed(2)}</b></span>
            {hasVolume && <span className="text-muted-foreground">总量 <b className="ml-1 font-mono font-medium text-foreground">{compactVolume(totalVolume)}</b></span>}
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
      )}

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
            strokeDasharray={tick.index === Math.round(0.5 * (totalMinutes - 1)) ? "5 4" : "2 4"}
            opacity={tick.index === Math.round(0.5 * (totalMinutes - 1)) ? 0.58 : 0.32}
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
        {hasVolume && !compact && (
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
        )}

        {!compact && bars.slice(firstIdx, lastIdx + 1).map((bar, offset) => (
          <rect
            key={firstIdx + offset}
            x={bar.x - Math.max(chartWidth / totalMinutes / 2 - 0.35, 0.65)}
            y={bar.y}
            width={Math.max(chartWidth / totalMinutes - 0.7, 1.3)}
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

        {!compact && (
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
        )}
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

      {!compact && (
        <div className="pointer-events-none absolute left-[64px] top-[66px] flex items-center gap-3 text-[10px]">
          <span className="flex items-center gap-1.5 text-muted-foreground">
            <i className="h-0.5 w-3 rounded-full" style={{ backgroundColor: priceColor }} />
            价格
          </span>
          {latestAverage != null && hasVolume && (
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <i className="h-0.5 w-3 rounded-full" style={{ backgroundColor: averageColor }} />
              均价 <b className="font-mono font-medium" style={{ color: averageColor }}>{latestAverage.toFixed(2)}</b>
            </span>
          )}
        </div>
      )}

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
            {hasVolume && (
              <>
                {!compact && (
                  <>
                    <span className="text-muted-foreground">均价</span>
                    <b className="text-right font-mono font-medium" style={{ color: averageColor }}>
                      {hover.average?.toFixed(2) ?? "—"}
                    </b>
                  </>
                )}
                <span className="text-muted-foreground">成交量</span>
                <b className="text-right font-mono font-medium text-foreground">{hover.volume > 0 ? compactVolume(hover.volume) : "—"}</b>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
