import { useMemo, useRef, useState } from "react";
import type { MinuteKline } from "@/lib/api";

const cssVar = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// SVG 分时图：纯 SVG 渲染，无 canvas，不会被 .glass backdrop-filter 擦除。
// 参照东财/同花顺分时图：价格线 + 均价线 + 昨收参考线 + 成交量柱 + hover 十字线。
export function MinuteChart({ data, height = 460, volRatio }: { data: MinuteKline; height?: number; volRatio?: number }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const prevClose = data.prev_close;
  const points = data.points;

  // 时间 → 分钟索引（09:30=0, 11:30=120, 13:00=121, 15:00=241）
  const toMin = (t: string) => {
    const h = parseInt(t.slice(0, 2), 10);
    const m = parseInt(t.slice(2), 10);
    const total = h * 60 + m;
    return total <= 690 ? total - 570 : total - 780 + 121; // 570=9*60+30, 780=13*60
  };
  const toTime = (min: number) => {
    if (min <= 120) {
      const t = min + 570;
      return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
    }
    const t = min + 780 - 121;
    return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
  };

  const TOTAL = 242;
  const W = 1000, H_PRICE = 100, H_VOL = 30, VOL_TOP = 115;

  const { priceArr, avgArr, volArr, pricePath, avgPath, volBars } = useMemo(() => {
    const pa: (number | null)[] = new Array(TOTAL).fill(null);
    const va: number[] = new Array(TOTAL).fill(0);
    for (const p of points) {
      const idx = toMin(p.time);
      if (idx >= 0 && idx < TOTAL) { pa[idx] = p.price; va[idx] = p.volume; }
    }
    for (let i = 1; i < TOTAL; i++) if (pa[i] == null) pa[i] = pa[i - 1];
    const fv = pa.find((v) => v != null);
    if (fv != null) for (let i = 0; i < TOTAL && pa[i] == null; i++) pa[i] = fv;

    let cumA = 0, cumV = 0;
    const aa: (number | null)[] = new Array(TOTAL).fill(null);
    for (let i = 0; i < TOTAL; i++) {
      if (pa[i] != null && va[i] > 0) { cumA += pa[i]! * va[i]; cumV += va[i]; aa[i] = cumV > 0 ? cumA / cumV : pa[i]; }
      else if (i > 0) aa[i] = aa[i - 1];
    }

    const prices = pa.filter((v): v is number => v != null);
    const minP = Math.min(...prices), maxP = Math.max(...prices);
    const range = Math.max(maxP - minP, prevClose * 0.001);
    const lo = Math.min(minP, prevClose) - range * 0.05;
    const hi = Math.max(maxP, prevClose) + range * 0.05;
    const scaleY = (p: number) => ((hi - p) / (hi - lo)) * H_PRICE;

    // Build paths
    let pp = "", ap = "";
    for (let i = 0; i < TOTAL; i++) {
      const x = (i / (TOTAL - 1)) * W;
      if (pa[i] != null) pp += (i === 0 ? "M" : "L") + `${x.toFixed(1)},${scaleY(pa[i]!).toFixed(1)}`;
      if (aa[i] != null) ap += (i === 0 ? "M" : "L") + `${x.toFixed(1)},${scaleY(aa[i]!).toFixed(1)}`;
    }

    const mVol = Math.max(...va, 1);
    const bars = va.map((v, i) => {
      const x = (i / (TOTAL - 1)) * W;
      const bh = (v / mVol) * H_VOL;
      const isUp = pa[i] != null && prevClose > 0 ? pa[i]! >= prevClose : true;
      return { x, y: VOL_TOP + H_VOL - bh, h: bh, isUp };
    });

    return { priceArr: pa, avgArr: aa, volArr: va, pricePath: pp, avgPath: ap, volBars: bars, maxVol: mVol, yLo: lo, yHi: hi, scaleY };
  }, [data]);

  if (points.length < 2) return null;

  const upColor = cssVar("--danger");
  const downColor = cssVar("--success");
  const mutedColor = cssVar("--muted-foreground");

  const hov = hoverIdx != null ? {
    price: priceArr[hoverIdx],
    avg: avgArr[hoverIdx],
    vol: volArr[hoverIdx],
    time: toTime(hoverIdx),
    x: (hoverIdx / (TOTAL - 1)) * 100,
    pct: priceArr[hoverIdx] != null && prevClose > 0
      ? ((priceArr[hoverIdx]! - prevClose) / prevClose * 100)
      : null,
  } : null;

  const onMove = (e: React.MouseEvent) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = (e.clientX - rect.left) / rect.width;
    setHoverIdx(Math.max(0, Math.min(TOTAL - 1, Math.round(ratio * (TOTAL - 1)))));
  };

  // 关键时间标签
  const timeLabels = [0, 30, 60, 90, 120, 121, 151, 181, 211, 241]
    .filter((v, i, a) => a.indexOf(v) === i)
    .map((min) => ({ min, label: toTime(min), x: (min / (TOTAL - 1)) * 100 }));

  return (
    <div ref={wrapRef} className="relative w-full" style={{ height }} onMouseMove={onMove} onMouseLeave={() => setHoverIdx(null)}>
      <svg className="absolute inset-0 h-full w-full" viewBox={`0 0 ${W} ${VOL_TOP + H_VOL + 20}`} preserveAspectRatio="none">
        {/* 昨收参考线 */}
        {prevClose > 0 && (
          <line
            x1={0} y1={((Math.max(...priceArr.filter((v): v is number => v != null), prevClose) + Math.min(...priceArr.filter((v): v is number => v != null), prevClose)) / 2 > prevClose
              ? H_PRICE * 0.35 : H_PRICE * 0.65)}
            x2={W} y2={((Math.max(...priceArr.filter((v): v is number => v != null), prevClose) + Math.min(...priceArr.filter((v): v is number => v != null), prevClose)) / 2 > prevClose
              ? H_PRICE * 0.35 : H_PRICE * 0.65)}
            stroke={`hsl(${mutedColor})`} strokeWidth="0.8" strokeDasharray="4 4" opacity="0.4" vectorEffect="non-scaling-stroke"
          />
        )}
        {/* 价格线 */}
        <path d={pricePath} fill="none" stroke={`hsl(${upColor})`} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
        {/* 均价线 */}
        <path d={avgPath} fill="none" stroke="#f6c453" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        {/* 成交量柱 */}
        {volBars.map((b, i) => (
          <rect key={i} x={b.x - 1.5} y={b.y} width={3} height={b.h} fill={`hsl(${b.isUp ? upColor : downColor})`} opacity="0.55" />
        ))}
        {/* hover 十字线 */}
        {hoverIdx != null && (
          <>
            <line x1={(hoverIdx / (TOTAL - 1)) * W} y1={0} x2={(hoverIdx / (TOTAL - 1)) * W} y2={VOL_TOP + H_VOL}
              stroke={`hsl(${mutedColor})`} strokeWidth="0.8" strokeDasharray="3 3" opacity="0.4" vectorEffect="non-scaling-stroke" />
            {priceArr[hoverIdx] != null && (
              <circle cx={(hoverIdx / (TOTAL - 1)) * W} cy={0} r="3" fill={`hsl(${upColor})`}
                style={{ transform: `translateY(${((Math.max(...priceArr.filter((v): v is number => v != null)) - priceArr[hoverIdx]!) / (Math.max(...priceArr.filter((v): v is number => v != null)) - Math.min(...priceArr.filter((v): v is number => v != null)) || 1)) * H_PRICE}px)` }} />
            )}
          </>
        )}
      </svg>

      {/* 量比 + 时间标签 */}
      {volRatio != null && (
        <div className="absolute bottom-5 right-2 text-[10px] font-mono text-muted-foreground/60">
          量比 <b className={volRatio > 1.5 ? "text-danger" : volRatio < 0.8 ? "text-success" : "text-foreground"}>{volRatio.toFixed(2)}</b>
        </div>
      )}
      <div className="absolute bottom-0 left-0 right-0 flex justify-between px-1 text-[9px] text-muted-foreground/50">
        {timeLabels.map((t) => (
          <span key={t.min} style={{ position: "absolute", left: `${t.x}%`, transform: "translateX(-50%)" }}>{t.label}</span>
        ))}
      </div>

      {/* hover 信息 */}
      {hov && hov.price != null && (
        <div className="pointer-events-none absolute z-10 whitespace-nowrap rounded-md border border-border/40 bg-background/95 px-2.5 py-1.5 text-[10px] font-mono shadow-lg"
          style={{ left: `${hov.x}%`, top: 0, transform: hov.x > 70 ? "translate(-100%, 4px)" : "translate(4px, 4px)" }}>
          <div className="text-muted-foreground">{hov.time}</div>
          <div>价格 <b className={hov.pct != null && hov.pct >= 0 ? "text-danger" : "text-success"}>{hov.price.toFixed(2)}</b>
            {hov.pct != null && <span className={hov.pct >= 0 ? "text-danger" : "text-success"}> {hov.pct > 0 ? "+" : ""}{hov.pct.toFixed(2)}%</span>}
          </div>
          {hov.avg != null && <div className="text-muted-foreground">均价 {hov.avg.toFixed(2)}</div>}
          <div className="text-muted-foreground">量 {hov.vol > 0 ? (hov.vol >= 10000 ? `${(hov.vol / 10000).toFixed(1)}万` : hov.vol) : "—"}</div>
        </div>
      )}
    </div>
  );
}
