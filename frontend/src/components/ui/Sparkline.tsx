import { useRef, useState, useMemo } from "react";
import type { HistPoint } from "@/lib/api";
import { cn } from "@/lib/utils";

const cssVar = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

interface Props {
  data: HistPoint[];
  height?: number;
  color?: string;          // CSS 变量名，如 --primary / --danger
  area?: boolean;
  className?: string;
  valueSuffix?: string;
  showLatest?: boolean;
}

// 纯 SVG sparkline：没有 canvas，自然不会被 .glass 的 backdrop-filter 擦除/闪烁。
// 定位点 + 十字参考线 + tooltip 用绝对定位 HTML 元素精确映射。
export function Sparkline({ data, height = 44, color = "--primary", area = true, className, valueSuffix = "", showLatest = false }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  // 构建 SVG path（viewBox 坐标系 0..w × 0..h）
  const { pathD, areaD, points } = useMemo(() => {
    if (data.length < 2) return { pathD: "", areaD: "", points: [] };
    const w = 1000; // viewBox 宽（拉伸用，stroke 不受影响因为 vector-effect）
    const h = 100;
    const values = data.map((d) => d.v);
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (max - min < 1e-9) { min -= 0.5; max += 0.5; }
    const padY = (max - min) * 0.06;
    min -= padY; max += padY;

    const pts = values.map((v, i) => ({
      x: (i / (values.length - 1)) * w,
      y: h - ((v - min) / (max - min)) * h,
    }));

    const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");
    const a = `${d} L${w},${h} L0,${h} Z`;
    return { pathD: d, areaD: a, points: pts };
  }, [data]);

  if (data.length < 2) return null;

  const stroke = cssVar(color);
  const hoverPoint = hoverIdx != null ? points[hoverIdx] : null;
  const hoverDatum = hoverIdx != null ? data[hoverIdx] : null;

  const onMove = (e: React.MouseEvent) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = (e.clientX - rect.left) / rect.width;
    const idx = Math.round(ratio * (data.length - 1));
    setHoverIdx(Math.max(0, Math.min(data.length - 1, idx)));
  };

  return (
    <div
      ref={wrapRef}
      className={cn("relative w-full", className)}
      style={{ height }}
      onMouseMove={onMove}
      onMouseLeave={() => setHoverIdx(null)}
    >
      {/* SVG 折线：preserveAspectRatio="none" 拉伸填充，stroke 用 vector-effect 保持视觉粗细 */}
      <svg
        className="absolute inset-0 h-full w-full"
        viewBox="0 0 1000 100"
        preserveAspectRatio="none"
      >
        {area && <path d={areaD} fill={`hsl(${stroke} / 0.12)`} />}
        <path d={pathD} fill="none" stroke={`hsl(${stroke})`} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
        {showLatest && points.length > 0 && (
          <circle cx={points[points.length - 1].x} cy={points[points.length - 1].y} r="3.5"
            fill={`hsl(${stroke})`} stroke="hsl(var(--background))" strokeWidth="2"
            vectorEffect="non-scaling-stroke" />
        )}
        {/* hover 十字线 */}
        {hoverPoint && (
          <>
            <line x1={hoverPoint.x} y1={0} x2={hoverPoint.x} y2={100} stroke={`hsl(${stroke})`} strokeWidth="1" strokeDasharray="3 3" opacity="0.4" vectorEffect="non-scaling-stroke" />
            <line x1={0} y1={hoverPoint.y} x2={1000} y2={hoverPoint.y} stroke={`hsl(${stroke})`} strokeWidth="1" strokeDasharray="3 3" opacity="0.3" vectorEffect="non-scaling-stroke" />
          </>
        )}
      </svg>

      {/* hover 定位点：用百分比定位在容器上，与 SVG 坐标一致 */}
      {hoverPoint && (
        <div
          className="pointer-events-none absolute z-10 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background shadow-md"
          style={{
            left: `${(hoverPoint.x / 1000) * 100}%`,
            top: `${hoverPoint.y}%`,
            backgroundColor: `hsl(${stroke})`,
          }}
        />
      )}

      {/* tooltip */}
      {hoverPoint && hoverDatum && (
        <div
          className="pointer-events-none absolute z-10 whitespace-nowrap rounded-md border border-border/40 bg-background/95 px-2 py-1 text-[10px] font-mono shadow-lg"
          style={{
            left: `${(hoverPoint.x / 1000) * 100}%`,
            top: 0,
            transform: hoverPoint.x / 1000 > 0.75
              ? "translate(-100%, -8px)"
              : hoverPoint.x / 1000 < 0.25
                ? "translate(0, -8px)"
                : "translate(-50%, -8px)",
          }}
        >
          <span className="text-muted-foreground">{hoverDatum.date}</span>{" "}
          <b>{hoverDatum.v.toLocaleString("zh-CN", { maximumFractionDigits: 3 })}{valueSuffix}</b>
        </div>
      )}
    </div>
  );
}
