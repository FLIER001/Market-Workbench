import { useEffect, useId, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/utils";

const cssVar = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const hsl = (name: string, alpha?: number) =>
  `hsl(${cssVar(name)}${alpha == null ? "" : ` / ${alpha}`})`;

// 布局常量。两张图放进**同一个 SVG**、共用一套 x 映射，这是「日期严格对齐」的实现方式：
// 两个独立 SVG 各自算宽度，滚动条/圆整误差会让竖虚线在两张图上差出 1px 级偏移。
const PAD = { left: 50, right: 16, top: 10, bottom: 18 };
const DEF_TIMING_H = 168; // 上图：择时分曲线（默认；被放进 1/2 窄栏时由调用方调小）
const DEF_BENCH_H = 132; // 下图：全A指数（默认）
const GAP = 18; // 两图之间的留白

// 绘图区高度 → y 轴刻度条数：矮图配少刻度，否则 6 条刻度挤在 100px 里全是线
const tickCountFor = (h: number) => Math.max(3, Math.min(6, Math.round(h / 27)));

// 择时分档位边界（后端 _RISK_LEVELS），作为上图的参考线；40-60 为中性区
const NEUTRAL_LO = 40;
const NEUTRAL_HI = 60;

const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

export interface ReplayPoint {
  date: string;
  v: number;
}

interface Scale {
  lo: number;
  hi: number;
  ticks: number[];
}

/** 「好看」的刻度步长（1/2/2.5/5/10 × 10^n）。 */
function niceStep(raw: number): number {
  if (!(raw > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  const s = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return s * mag;
}

function buildScale(values: number[], maxTicks = 6, minTicks = 3): Scale {
  const finite = values.filter((v) => Number.isFinite(v));
  if (!finite.length) return { lo: 0, hi: 1, ticks: [0, 1] };
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = max - min || Math.abs(max) || 1;
  const pad = span * 0.08; // 8% 留边，曲线不贴顶/贴底
  const lo = min - pad;
  const hi = max + pad;
  const countOf = (s: number) => Math.floor(hi / s + 1e-9) - Math.ceil(lo / s - 1e-9) + 1;
  // 值域上下界**不**取整到刻度：取整会让顶格刻度远高于峰值（择时分峰值 82 时顶格落在
  // 100，图上方空出近 1/4 高度）。改为贴着数据留边，刻度取落在 [lo,hi] 内的整档值。
  let step = niceStep((hi - lo) / maxTicks);
  for (let i = 0; i < 12 && countOf(step) > maxTicks; i++) {
    const next = niceStep(step * 1.05);
    if (next <= step) break;
    step = next;
  }
  for (let i = 0; i < 12 && countOf(step) < minTicks; i++) {
    const raw = step / 10;
    const next = [1, 2, 2.5, 5].map((m) => m * raw).filter((s) => s < step).pop();
    if (!next) break;
    step = next;
  }
  const ticks: number[] = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) {
    ticks.push(Number(v.toFixed(6)));
  }
  return { lo, hi, ticks };
}

/**
 * 共用横轴 = 两序列日期的并集（区间取交集，避免一端多出的历史把另一条拉平）。
 * 序列本身已按日期升序，任一序列缺当日值时该点不画，不做前向填充臆造走势。
 */
function buildAxis(a: ReplayPoint[], b: ReplayPoint[]): string[] {
  if (!a.length) return b.map((p) => p.date);
  if (!b.length) return a.map((p) => p.date);
  const lo = a[0].date > b[0].date ? a[0].date : b[0].date;
  const hi = a[a.length - 1].date < b[b.length - 1].date ? a[a.length - 1].date : b[b.length - 1].date;
  const set = new Set<string>();
  for (const p of a) if (p.date >= lo && p.date <= hi) set.add(p.date);
  for (const p of b) if (p.date >= lo && p.date <= hi) set.add(p.date);
  return [...set].sort();
}

/** 连续非空段的 [起, 止] 下标；缺值的点在折线上断开而不是跨过去连一条假线。 */
function runsOf(values: (number | null)[]): [number, number][] {
  const runs: [number, number][] = [];
  let start = -1;
  values.forEach((v, i) => {
    const ok = v != null && Number.isFinite(v);
    if (ok && start < 0) start = i;
    if (!ok && start >= 0) {
      runs.push([start, i - 1]);
      start = -1;
    }
  });
  if (start >= 0) runs.push([start, values.length - 1]);
  return runs;
}

function linePath(values: (number | null)[], xOf: (i: number) => number, yOf: (v: number) => number): string {
  return runsOf(values)
    .map(([s, e]) => {
      let d = `M${xOf(s).toFixed(2)},${yOf(values[s] as number).toFixed(2)}`;
      for (let i = s + 1; i <= e; i++) d += `L${xOf(i).toFixed(2)},${yOf(values[i] as number).toFixed(2)}`;
      return d;
    })
    .join(" ");
}

function areaPath(values: (number | null)[], xOf: (i: number) => number, yOf: (v: number) => number, baseY: number): string {
  return runsOf(values)
    .map(([s, e]) => {
      let d = `M${xOf(s).toFixed(2)},${baseY.toFixed(2)}L${xOf(s).toFixed(2)},${yOf(values[s] as number).toFixed(2)}`;
      for (let i = s + 1; i <= e; i++) d += `L${xOf(i).toFixed(2)},${yOf(values[i] as number).toFixed(2)}`;
      d += `L${xOf(e).toFixed(2)},${baseY.toFixed(2)}Z`;
      return d;
    })
    .join(" ");
}

const fmtScore = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
const fmtIndex = (v: number | null | undefined) =>
  v == null ? "—" : v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (v: number | null) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);

/** 轴刻度日期：首个刻度带年月，跨年处也带年月，其余只留月-日。 */
function tickLabel(date: string, first: boolean): string {
  return first || date.slice(5, 7) === "01" ? date.slice(0, 7) : date.slice(5);
}

interface Props {
  /** 择时分逐日回放（近 1 年，按日期升序） */
  timing: ReplayPoint[];
  /** 全A指数日收盘；缺失时下图显示占位说明，横轴仍沿用择时分的日期轴 */
  benchmark?: ReplayPoint[];
  benchmarkLabel?: string;
  className?: string;
  /** 上图/下图绘图区高度。默认 168/132；被放进 1/2 窄栏、需要把整张卡压矮时显式调小
   *  （刻度条数会跟着高度自适应，不会在小图上挤成一团）。 */
  timingHeight?: number;
  benchHeight?: number;
}

/**
 * 择时配置 · 双图联动回放。
 *
 * 上图 = 择时分逐日回放，下图 = 全A指数走势，两图共用同一条交易日横轴；鼠标悬停在任一张图上
 * 都会在**两图同时**画出同一条垂直虚线参考线（被悬停的那张额外给水平线与定位点），
 * 顶部读数行同步显示该交易日的两条数值。
 */
export function TimingReplayChart({
  timing, benchmark, benchmarkLabel, className,
  timingHeight = DEF_TIMING_H, benchHeight = DEF_BENCH_H,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  // 初值取最小可用宽而不是「大概是这么宽」：SVG 是替换元素，首帧宽度会被父级的
  // min-content 计算计入，给大了会把所在网格列顶爆（挂载后 ResizeObserver 立刻量到真值）。
  const [width, setWidth] = useState(320);
  const [hover, setHover] = useState<{ idx: number; panel: "timing" | "benchmark" } | null>(null);
  const [, setThemeTick] = useState(0);
  const uid = useId().replace(/:/g, "");
  const hTiming = timingHeight;
  const hBench = benchHeight;

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setWidth(Math.max(320, el.getBoundingClientRect().width));
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 主题（<html> 的 .light/.dark）切换后重读 CSS 变量重绘
  useEffect(() => {
    const mo = new MutationObserver(() => setThemeTick((t) => t + 1));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => mo.disconnect();
  }, []);

  const bench = benchmark ?? [];
  const axis = useMemo(() => buildAxis(timing, bench), [timing, bench]);
  const n = axis.length;

  const timingVals = useMemo(() => {
    const m = new Map(timing.map((p) => [p.date, p.v]));
    return axis.map((d) => m.get(d) ?? null);
  }, [axis, timing]);
  const benchVals = useMemo(() => {
    const m = new Map(bench.map((p) => [p.date, p.v]));
    return axis.map((d) => m.get(d) ?? null);
  }, [axis, bench]);

  const s1 = useMemo(() => buildScale(timingVals.filter((v): v is number => v != null), tickCountFor(hTiming)),
    [timingVals, hTiming]);
  const s2 = useMemo(() => buildScale(benchVals.filter((v): v is number => v != null), tickCountFor(hBench)),
    [benchVals, hBench]);

  const svgH = PAD.top + hTiming + GAP + hBench + PAD.bottom;
  const plotW = Math.max(10, width - PAD.left - PAD.right);
  const xOf = (i: number) => PAD.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yTop1 = PAD.top;
  const yOf1 = (v: number) => yTop1 + hTiming - ((v - s1.lo) / (s1.hi - s1.lo)) * hTiming;
  const yTop2 = PAD.top + hTiming + GAP;
  const yOf2 = (v: number) => yTop2 + hBench - ((v - s2.lo) / (s2.hi - s2.lo)) * hBench;

  // 图例/读数所在的下标：悬停时取悬停日，否则取最新交易日
  const lastIdx = n - 1;
  const selIdx = hover ? hover.idx : lastIdx;
  const selDate = n ? axis[selIdx] : "";
  const tSel = selIdx >= 0 ? timingVals[selIdx] : null;
  const bSel = selIdx >= 0 ? benchVals[selIdx] : null;

  // 区间统计（不随悬停变化，标「区间」以示区别）
  const benchPct = useMemo(() => {
    const vals = benchVals.filter((v): v is number => v != null);
    if (vals.length < 2 || !vals[0]) return null;
    return (vals[vals.length - 1] / vals[0] - 1) * 100;
  }, [benchVals]);
  const chg5d = useMemo(() => {
    const vals = timingVals.filter((v): v is number => v != null);
    return vals.length >= 6 ? vals[vals.length - 1] - vals[vals.length - 6] : null;
  }, [timingVals]);

  const line1 = linePath(timingVals, xOf, yOf1);
  const area1 = areaPath(timingVals, xOf, yOf1, yTop1 + hTiming);
  const line2 = linePath(benchVals, xOf, yOf2);
  const area2 = areaPath(benchVals, xOf, yOf2, yTop2 + hBench);

  const stroke1 = hsl("--primary");
  // 区间涨跌决定下图颜色：A股口径红涨绿跌
  const benchFinite = benchVals.filter((v): v is number => v != null);
  const benchUp = benchFinite.length > 1 && benchFinite[benchFinite.length - 1] >= benchFinite[0];
  const stroke2 = hsl(benchUp ? "--danger" : "--success");

  const onMove = (e: React.MouseEvent) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect || n < 2) return;
    const ratio = (e.clientX - rect.left - PAD.left) / plotW;
    const idx = Math.max(0, Math.min(n - 1, Math.round(ratio * (n - 1))));
    const y = e.clientY - rect.top;
    setHover({ idx, panel: y <= PAD.top + hTiming + GAP / 2 ? "timing" : "benchmark" });
  };

  const lastIdxOf = (vals: (number | null)[]) => {
    for (let i = vals.length - 1; i >= 0; i--) if (vals[i] != null) return i;
    return -1;
  };
  const tail1 = lastIdxOf(timingVals);
  const tail2 = lastIdxOf(benchVals);

  // x 轴刻度：5 等分
  const xTicks = useMemo(() => {
    if (n <= 1) return [{ i: 0, first: true, anchor: "start" as const }];
    return [0, 0.25, 0.5, 0.75, 1].map((r, k) => ({
      i: Math.round(r * (n - 1)),
      first: k === 0,
      anchor: (k === 0 ? "start" : k === 4 ? "end" : "middle") as "start" | "middle" | "end",
    }));
  }, [n]);

  const hoverX = hover ? xOf(hover.idx) : null;
  // 悬停日期的气泡占位：静态刻度若与之重叠就不画，避免叠字
  const bubW = 64;
  const bubX = hoverX == null ? null : Math.max(PAD.left, Math.min(width - PAD.right - bubW, hoverX - bubW / 2));

  const gridStroke = hsl("--chart-grid");
  const tickFill = hsl("--chart-text");

  return (
    <div className={className}>
      {/* 读数行：悬停时显示悬停交易日，否则显示最新交易日 */}
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[11px]">
        <span className="flex items-baseline gap-1.5">
          <span className="inline-block h-1.5 w-3 rounded-full" style={{ background: stroke1 }} />
          <span className="text-muted-foreground">择时分</span>
          <span className="font-mono text-sm font-bold" style={{ color: stroke1 }}>{fmtScore(tSel)}</span>
          {chg5d != null && (
            <span className={cn("rounded bg-muted/40 px-1.5 py-px font-mono text-[10px]",
              chg5d > 1 ? "text-danger" : chg5d < -1 ? "text-success" : "text-muted-foreground")}>
              近5日{chg5d > 0 ? "+" : ""}{chg5d.toFixed(1)}
            </span>
          )}
        </span>
        <span className="flex items-baseline gap-1.5">
          <span className="inline-block h-1.5 w-3 rounded-full" style={{ background: stroke2 }} />
          <span className="text-muted-foreground">{benchmarkLabel || "全A指数"}</span>
          <span className="font-mono text-sm font-bold" style={{ color: stroke2 }}>{fmtIndex(bSel)}</span>
          {benchPct != null && (
            <span className={cn("rounded bg-muted/40 px-1.5 py-px font-mono text-[10px]",
              benchPct > 0 ? "text-danger" : benchPct < 0 ? "text-success" : "text-muted-foreground")}>
              区间{fmtPct(benchPct)}
            </span>
          )}
        </span>
        <span className="ml-auto font-mono text-[11px] text-muted-foreground/70">
          {hover ? selDate : `最新 ${selDate}`}
        </span>
      </div>

      <div
        ref={wrapRef}
        className="relative mt-2 cursor-crosshair select-none"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        <svg width={width} height={svgH} className="block">
          <defs>
            <linearGradient id={`t-${uid}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke1} stopOpacity="0.26" />
              <stop offset="100%" stopColor={stroke1} stopOpacity="0.01" />
            </linearGradient>
            <linearGradient id={`b-${uid}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke2} stopOpacity="0.22" />
              <stop offset="100%" stopColor={stroke2} stopOpacity="0.01" />
            </linearGradient>
          </defs>

          {/* ===== 上图：择时分 ===== */}
          {/* 中性区 40-60（档位边界，与后端 _RISK_LEVELS 同源） */}
          {s1.lo < NEUTRAL_HI && s1.hi > NEUTRAL_LO && (
            <>
              <rect
                x={PAD.left}
                y={yOf1(Math.min(NEUTRAL_HI, s1.hi))}
                width={plotW}
                height={Math.max(0, yOf1(Math.max(NEUTRAL_LO, s1.lo)) - yOf1(Math.min(NEUTRAL_HI, s1.hi)))}
                fill={hsl("--muted-foreground", 0.05)}
              />
              <text x={PAD.left + 5} y={yOf1(NEUTRAL_HI) + 11} fontSize="9" fill={tickFill} opacity="0.6">
                中性区 {NEUTRAL_LO}–{NEUTRAL_HI}
              </text>
            </>
          )}
          {s1.ticks.map((t) => (
            <g key={`g1-${t}`}>
              <line x1={PAD.left} y1={yOf1(t)} x2={width - PAD.right} y2={yOf1(t)} stroke={gridStroke} strokeWidth="1" />
              <text x={PAD.left - 6} y={yOf1(t) + 3.5} textAnchor="end" fontSize="9" fontFamily={MONO} fill={tickFill}>
                {t.toFixed(0)}
              </text>
            </g>
          ))}
          <path d={area1} fill={`url(#t-${uid})`} />
          <path d={line1} fill="none" stroke={stroke1} strokeWidth="1.6" strokeLinejoin="round" />

          {/* 两图分隔 */}
          <line x1={PAD.left} y1={yTop1 + hTiming + GAP / 2} x2={width - PAD.right} y2={yTop1 + hTiming + GAP / 2}
            stroke={gridStroke} strokeWidth="1" />

          {/* ===== 下图：全A指数 ===== */}
          {tail2 >= 0 ? (
            <>
              {s2.ticks.map((t) => (
                <g key={`g2-${t}`}>
                  <line x1={PAD.left} y1={yOf2(t)} x2={width - PAD.right} y2={yOf2(t)} stroke={gridStroke} strokeWidth="1" />
                  <text x={PAD.left - 6} y={yOf2(t) + 3.5} textAnchor="end" fontSize="9" fontFamily={MONO} fill={tickFill}>
                    {t.toFixed(0)}
                  </text>
                </g>
              ))}
              <path d={area2} fill={`url(#b-${uid})`} />
              <path d={line2} fill="none" stroke={stroke2} strokeWidth="1.6" strokeLinejoin="round" />
            </>
          ) : (
            <text x={PAD.left + plotW / 2} y={yTop2 + hBench / 2} textAnchor="middle" fontSize="11" fill={tickFill}>
              全A指数数据暂不可用
            </text>
          )}

          {/* ===== 最新点 ===== */}
          {tail1 >= 0 && (
            <circle cx={xOf(tail1)} cy={yOf1(timingVals[tail1] as number)} r="3" fill={stroke1}
              stroke={hsl("--background")} strokeWidth="2" />
          )}
          {tail2 >= 0 && (
            <circle cx={xOf(tail2)} cy={yOf2(benchVals[tail2] as number)} r="3" fill={stroke2}
              stroke={hsl("--background")} strokeWidth="2" />
          )}

          {/* ===== x 轴日期刻度（共用一条轴，只在下图下方标一次） ===== */}
          {xTicks.map((t, k) => (
            <text
              key={`x-${k}`}
              x={xOf(t.i)}
              y={svgH - 6}
              textAnchor={t.anchor}
              fontSize="9"
              fontFamily={MONO}
              fill={tickFill}
              opacity={bubX != null && xOf(t.i) > bubX - 30 && xOf(t.i) < bubX + bubW + 30 ? 0 : 1}
            >
              {axis[t.i] ? tickLabel(axis[t.i], t.first) : ""}
            </text>
          ))}

          {/* ===== 联动十字光标 ===== */}
          {hover && hoverX != null && (
            <>
              {/* 垂直虚线：一条线同时贯穿上下两图 —— 双图同步的核心 */}
              <line
                x1={hoverX} y1={PAD.top - 6} x2={hoverX} y2={yTop2 + hBench + 4}
                stroke={hsl("--foreground")} strokeWidth="1" strokeDasharray="3 3" opacity="0.45"
              />
              {/* 水平线 + 定位点只画在被悬停的那张图上 */}
              {hover.panel === "timing" && timingVals[hover.idx] != null && (
                <>
                  <line x1={PAD.left} y1={yOf1(timingVals[hover.idx] as number)} x2={width - PAD.right}
                    y2={yOf1(timingVals[hover.idx] as number)} stroke={stroke1} strokeWidth="1"
                    strokeDasharray="3 3" opacity="0.45" />
                  <circle cx={hoverX} cy={yOf1(timingVals[hover.idx] as number)} r="3.2" fill={stroke1}
                    stroke={hsl("--background")} strokeWidth="2" />
                </>
              )}
              {hover.panel === "benchmark" && benchVals[hover.idx] != null && (
                <>
                  <line x1={PAD.left} y1={yOf2(benchVals[hover.idx] as number)} x2={width - PAD.right}
                    y2={yOf2(benchVals[hover.idx] as number)} stroke={stroke2} strokeWidth="1"
                    strokeDasharray="3 3" opacity="0.45" />
                  <circle cx={hoverX} cy={yOf2(benchVals[hover.idx] as number)} r="3.2" fill={stroke2}
                    stroke={hsl("--background")} strokeWidth="2" />
                </>
              )}
              {/* 横轴上的日期气泡 */}
              {bubX != null && (
                <g>
                  <rect x={bubX} y={svgH - PAD.bottom + 1} width={bubW} height={PAD.bottom - 4} rx="3"
                    fill={hsl("--muted")} stroke={hsl("--border")} />
                  <text x={bubX + bubW / 2} y={svgH - PAD.bottom + 11.5} textAnchor="middle" fontSize="9"
                    fontFamily={MONO} fill={hsl("--foreground")}>
                    {selDate.slice(5)}
                  </text>
                </g>
              )}
            </>
          )}
        </svg>
      </div>
    </div>
  );
}
