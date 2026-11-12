import { useId, useState } from "react";
import type { BondScorePoint } from "@/lib/api";
import { numberText as n } from "@/lib/bondView";

// Both plots share dates and a single cursor, including touch/keyboard selection.
export function BondScoreCharts({ history }: { history: BondScorePoint[] }) {
  const id = useId();
  const [selected, setSelected] = useState<number | null>(null);
  const [window, setWindow] = useState(252);
  const rows = history.slice(-window);
  if (rows.length < 2) return <p className="mt-4 text-sm text-muted-foreground">历史数据不足，暂不能绘图。</p>;
  const index = Math.min(selected ?? rows.length - 1, rows.length - 1);
  const current = rows[index];
  const start = Date.parse(rows[0].date), end = Date.parse(rows[rows.length - 1].date);
  const x = (i: number) => 38 + (Date.parse(rows[i].date) - start) / (end - start || 1) * 264;
  const nav = (clientX: number, rect: DOMRect) => {
    const time = start + Math.max(0, Math.min(1, (clientX - rect.left) / rect.width * 320 / 264 - 38 / 264)) * (end - start);
    setSelected(rows.reduce((best, row, i) => Math.abs(Date.parse(row.date) - time) < Math.abs(Date.parse(rows[best].date) - time) ? i : best, 0));
  };
  const plot = (score: boolean) => {
    const values = rows.map(r => score ? r.score : r.index);
    const finite = values.filter((v): v is number => v != null && Number.isFinite(v));
    if (!finite.length) return <p className="py-8 text-sm text-muted-foreground">得分历史暂不可用</p>;
    const min = score ? 0 : Math.min(...finite), max = score ? 100 : Math.max(...finite);
    const pad = score ? 0 : (max - min || 1) * .12;
    const lo = min - pad, hi = max + pad;
    const y = (v: number) => 116 - (v - lo) / (hi - lo || 1) * 102;
    let pen = false;
    const path = values.map((v, i) => {
      if (v == null || !Number.isFinite(v)) { pen = false; return ""; }
      const part = `${pen ? "L" : "M"}${x(i)},${y(v)}`; pen = true; return part;
    }).join(" ");
    const value = values[index];
    return <svg viewBox="0 0 320 145" role="img" aria-label={score ? "回溯得分历史，50为中性" : "财富指数走势，包含利息收益"}
      className="w-full touch-pan-y text-muted-foreground" onPointerMove={e => nav(e.clientX, e.currentTarget.getBoundingClientRect())}
      onPointerDown={e => nav(e.clientX, e.currentTarget.getBoundingClientRect())}>
      {score && <rect x="38" y={y(55)} width="264" height={y(45)-y(55)} fill="currentColor" opacity=".1" />}
      {(score ? [0,50,100] : [lo,(lo+hi)/2,hi]).map(v => <g key={v}><line x1="38" x2="302" y1={y(v)} y2={y(v)} stroke="currentColor" opacity=".2" strokeDasharray="3 3" /><text x="32" y={y(v)+4} fontSize="10" textAnchor="end" fill="currentColor">{n(v, score ? 0 : 1)}</text></g>)}
      <path d={path} fill="none" stroke={score ? "hsl(var(--primary))" : "#38bdf8"} strokeWidth="2" />
      <line x1={x(index)} x2={x(index)} y1="14" y2="116" stroke="currentColor" strokeDasharray="3 3" opacity=".6" />
      {value != null && Number.isFinite(value) && <circle cx={x(index)} cy={y(value)} r="3" fill={score ? "hsl(var(--primary))" : "#38bdf8"} />}
      <text x="38" y="137" fontSize="10" fill="currentColor">{rows[0].date.slice(2)}</text><text x="302" y="137" fontSize="10" textAnchor="end" fill="currentColor">{rows[rows.length-1].date.slice(2)}</text>
    </svg>;
  };
  return <div className="mt-4 border-t border-border/60 pt-4" data-score-charts>
    <div className="mb-3 flex items-center justify-between gap-2"><span className="text-xs text-muted-foreground">得分与走势对照</span><div className="flex gap-1">{[{n:63,label:"近3月"},{n:252,label:"近1年"}].map(w => <button key={w.n} aria-pressed={window === w.n} onClick={() => { setWindow(w.n); setSelected(null); }} className={`rounded px-2 py-2 text-xs ${window === w.n ? "bg-primary/15 text-primary" : "text-muted-foreground"}`}>{w.label}</button>)}</div></div>
    <div className="flex justify-between gap-2 text-xs"><span>回溯得分</span><strong>{n(current.score, 1)} / 100</strong></div>
    {plot(true)}
    <div className="mt-3 flex justify-between gap-2 text-xs"><span>财富指数（含利息）</span><strong>{n(current.index, 2)}</strong></div>
    {plot(false)}
    <label htmlFor={id} className="flex justify-between gap-2 text-xs text-muted-foreground"><span>滑动查看同一天</span><span>{current.date}</span></label>
    <input id={id} aria-label="查看得分与指数的历史日期" type="range" min="0" max={rows.length-1} value={index} onChange={e => setSelected(Number(e.target.value))} className="mt-1 h-6 w-full accent-primary" />
    <p className="mt-2 text-xs leading-relaxed text-muted-foreground">分数为回溯重算，不是当时发布的记录，也不是上涨概率。50 为中性，55／45 为展示分界。</p>
  </div>;
}
