import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Crosshair,
  Gauge,
  RefreshCw,
  Search,
  Shield,
  TrendingUp,
} from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, type PlateScoreRow, type PlateScoresData } from "@/lib/api";
import { cn } from "@/lib/utils";
import { resolveRefreshing } from "@/hooks/useSWR";

type SortKey = "priority" | "strength" | "opportunity";
const LOCAL_CACHE_KEY = "vr-plate-scores-cache-v1";

const loadLocalCache = (): PlateScoresData | null => {
  try {
    const cached = JSON.parse(localStorage.getItem(LOCAL_CACHE_KEY) || "null");
    return cached?.boards?.length > 0 ? (cached as PlateScoresData) : null;
  } catch {
    return null;
  }
};

const saveLocalCache = (data: PlateScoresData) => {
  try {
    localStorage.setItem(LOCAL_CACHE_KEY, JSON.stringify(data));
  } catch {
    /* ignore */
  }
};

const format = (value: number | null | undefined, digits = 1) =>
  value == null || !Number.isFinite(value) ? "—" : value.toFixed(digits);

const stateTone: Record<string, string> = {
  主线可参与: "bg-primary/15 text-primary",
  强势观察: "bg-warning/15 text-warning",
  强势但不追: "bg-danger/15 text-danger",
  低位启动候选: "bg-success/15 text-success",
  左侧观察: "bg-success/10 text-success/80",
  中性观察: "bg-muted text-muted-foreground",
  弱势回避: "bg-muted text-muted-foreground/60",
  数据不足: "bg-muted text-muted-foreground/40",
};

// 矩阵点的状态配色（与主榜徽章同色系）
const quadrantDot: Record<string, string> = {
  主线可参与: "border-primary bg-primary/70",
  强势观察: "border-warning bg-warning/70",
  强势但不追: "border-danger bg-danger/70",
  低位启动候选: "border-success bg-success/70",
  左侧观察: "border-success/70 bg-success/40",
  中性观察: "border-muted-foreground/50 bg-muted-foreground/25",
  弱势回避: "border-muted-foreground/30 bg-muted-foreground/15",
};

// 子指标展示元数据：label / 取值范围说明 / 是否反向
const strengthMetrics: { key: keyof PlateScoreRow["strength"]["detail"]; label: string; weight: number; hint: string }[] = [
  { key: "relative_trend", label: "相对趋势", weight: 25, hint: "20/60日超额收益 vs 中证全指" },
  { key: "breadth_impulse", label: "扩散改善", weight: 30, hint: "等权−限权 20日收益差" },
  { key: "flow_confirmation", label: "交易确认", weight: 20, hint: "板块成交额占全市场的MA5/MA20变化" },
  { key: "trend_quality", label: "趋势稳定性", weight: 15, hint: "收益风险比+回撤+上涨日+路径效率" },
  { key: "leader_concentration", label: "龙头分散度", weight: 10, hint: "前3大收益贡献率（反向）" },
];

const opportunityMetrics: { key: keyof PlateScoreRow["opportunity"]["detail"]; label: string; weight: number; hint: string; reverse?: boolean }[] = [
  { key: "fundamental", label: "基本面景气", weight: 30, hint: "同一报告期加权均值+中位数+正增长占比" },
  { key: "earnings_revision", label: "盈利修正", weight: 20, hint: "下一预测年度EPS相对上月变化" },
  { key: "valuation_match", label: "估值匹配", weight: 15, hint: "真实近五年PE/PB历史分位", reverse: true },
  { key: "position_score", label: "价格位置", weight: 20, hint: "20/60日收益、MA60偏离及120日高点分位", reverse: true },
  { key: "crowding_score", label: "拥挤程度", weight: 15, hint: "换手率和成交额占比历史分位", reverse: true },
  { key: "catalyst", label: "催化持续", weight: 0, hint: "未取得结构化数据，暂不计分" },
];

const quadrantLegend = [
  { label: "主线可参与", cls: "border-primary bg-primary/70" },
  { label: "强势观察", cls: "border-warning bg-warning/70" },
  { label: "强势但不追", cls: "border-danger bg-danger/70" },
  { label: "低位启动/左侧", cls: "border-success bg-success/70" },
  { label: "中性/弱势", cls: "border-muted-foreground/40 bg-muted-foreground/20" },
];

// --- 矩阵轴：按实际数据自适应量程，后端分类阈值始终可见 ---
const floorTo = (v: number, step: number) => Math.floor(v / step) * step;
const ceilTo = (v: number, step: number) => Math.ceil(v / step) * step;

interface AxisDomain { min: number; max: number; ticks: number[] }

function fitDomain(values: number[], divider = 50, step = 10, pad = 4): AxisDomain {
  if (!values.length) return { min: 0, max: 100, ticks: [0, 20, 40, 50, 60, 80, 100] };
  let lo = floorTo(Math.min(...values) - pad, step);
  let hi = ceilTo(Math.max(...values) + pad, step);
  if (lo > divider) lo = floorTo(divider - pad, step);
  if (hi < divider) hi = ceilTo(divider + pad, step);
  if (hi - lo < step * 2) hi = lo + step * 2;
  const ticks: number[] = [];
  for (let t = lo; t <= hi; t += step) ticks.push(t);
  if (!ticks.includes(divider)) ticks.push(divider);
  return { min: lo, max: hi, ticks: ticks.sort((a, b) => a - b) };
}

const pct = (v: number, d: AxisDomain) => ((v - d.min) / (d.max - d.min)) * 100;


const signalLabel: Record<string, string> = {
  LEFT_SIDE_WATCH: "左侧观察",
  ROTATION_START: "轮动启动",
  MAIN_TREND_CONFIRMED: "主线确认",
  STRONG_BUT_OVERHEATED: "强势但不追",
  BREADTH_DIVERGENCE: "扩散背离",
  TREND_EXIT: "趋势退潮",
};

function ScoreBar({ value, color = "primary" }: { value: number | null; color?: "primary" | "success" | "warning" | "danger" }) {
  const colorMap = {
    primary: "bg-primary",
    success: "bg-success",
    warning: "bg-warning",
    danger: "bg-danger",
  };
  return (
    <div className="flex min-w-[72px] items-center gap-1.5">
      <span className="w-7 text-right font-mono text-xs font-semibold">{format(value, 0)}</span>
      <span className="h-1.5 w-9 overflow-hidden rounded-full bg-muted">
        <span
          className={cn("block h-full rounded-full", colorMap[color])}
          style={{ width: `${Math.max(0, Math.min(100, value || 0))}%` }}
        />
      </span>
    </div>
  );
}

function MetricCard({
  icon: Icon,
  label,
  value,
  note,
}: {
  icon: typeof Gauge;
  label: string;
  value: string;
  note: string;
}) {
  return (
    <GlassCard className="p-3.5">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Icon className="h-3.5 w-3.5 text-primary" />
        {label}
      </div>
      <div className="mt-1 text-lg font-bold">{value}</div>
      <p className="mt-0.5 text-[11px] text-muted-foreground/70">{note}</p>
    </GlassCard>
  );
}

function MetricBar({
  label,
  weight,
  value,
  hint,
  reverse,
  color,
}: {
  label: string;
  weight: number;
  value: number | null;
  hint: string;
  reverse?: boolean;
  color: "primary" | "success";
}) {
  const barColor = color === "primary" ? "bg-primary" : "bg-success";
  return (
    <div className="flex items-center gap-2 py-1">
      <div className="w-20 shrink-0">
        <div className="text-[11px] font-medium">{label}</div>
        <div className="text-[9px] text-muted-foreground/60">权重{weight}%{reverse ? " · 反向" : ""}</div>
      </div>
      <div className="relative h-3.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted/70">
        <span
          className={cn("block h-full rounded-full", barColor, value != null && value < 40 && "opacity-70")}
          style={{ width: `${Math.max(0, Math.min(100, value ?? 0))}%` }}
        />
      </div>
      <span className="w-8 shrink-0 text-right font-mono text-[11px] font-semibold">{format(value, 0)}</span>
      <span className="hidden w-44 shrink-0 truncate text-[9px] text-muted-foreground/50 lg:inline" title={hint}>{hint}</span>
    </div>
  );
}

function ExpandedBoardDetail({ row }: { row: PlateScoreRow }) {
  const s = row.strength.detail;
  const o = row.opportunity.detail;
  return (
    <div className="grid gap-x-8 gap-y-1 px-2 py-1 lg:grid-cols-2">
      <div>
        <div className="mb-1 flex items-center justify-between border-b border-border/40 pb-1">
          <span className="text-[11px] font-semibold text-primary">强度分 {format(row.strength.score, 1)}</span>
          <span className="text-[9px] text-muted-foreground/60">趋势强不强</span>
        </div>
        {strengthMetrics.map((m) => (
          <MetricBar key={m.key} label={m.label} weight={m.weight} value={s[m.key] ?? null} hint={m.hint} color="primary" />
        ))}
        <div className="mt-1 text-[9px] text-muted-foreground/50">
          超额收益 er20 {format(s.er20, 1)}% · er60 {format(s.er60, 1)}%
          {s.ma20_coverage != null && ` · MA20覆盖 ${format(s.ma20_coverage, 0)}%`}
        </div>
      </div>
      <div>
        <div className="mb-1 flex items-center justify-between border-b border-border/40 pb-1">
          <span className="text-[11px] font-semibold text-success">机会分 {format(row.opportunity.score, 1)}</span>
          <span className="text-[9px] text-muted-foreground/60">现在值不值得参与</span>
        </div>
        {opportunityMetrics.map((m) => (
          <MetricBar key={m.key} label={m.label} weight={m.weight} value={o[m.key]} hint={m.hint} reverse={m.reverse} color="success" />
        ))}
        <div className="mt-1 text-[9px] text-muted-foreground/50">
          反向指标分低 = 估值贵 / 位置高 / 太拥挤，防追涨
        </div>
        {row.opportunity.coverage && (
          <div className="mt-1 text-[9px] text-muted-foreground/50">
            覆盖：财务{format(row.opportunity.coverage.financial, 0)}% · 预期{format(row.opportunity.coverage.forecast, 0)}% · 估值{format(row.opportunity.coverage.valuation, 0)}%
          </div>
        )}
      </div>
    </div>
  );
}

export function PlateScoresPanel() {
  const [data, setData] = useState<PlateScoresData | null>(loadLocalCache);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("全部");
  const [sort, setSort] = useState<SortKey>("priority");
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const first = await api.plateScores(refresh);
      const next = await resolveRefreshing(first, () => api.plateScores(false));
      setData(next);
      saveLocalCache(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "板块评分加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const start = async () => {
      if (!data) {
        try {
          const cached = await api.plateScoresCache();
          if (!cancelled && cached) {
            setData(cached);
            saveLocalCache(cached);
          }
        } catch {
          /* continue */
        }
      }
      if (!cancelled) await load(false);
    };
    void start();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void load(false);
    }, 15 * 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const states = useMemo(() => {
    const values = new Set(data?.boards.map((r) => r.state) || []);
    return ["全部", ...Array.from(values)];
  }, [data]);

  const rows = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return [...(data?.boards || [])]
      .filter((r) => stateFilter === "全部" || r.state === stateFilter)
      .filter((r) => !keyword || r.board_name.toLowerCase().includes(keyword))
      .sort((a, b) => {
        const av = sort === "priority" ? a.priority : sort === "strength" ? a.strength.score : a.opportunity.score;
        const bv = sort === "priority" ? b.priority : sort === "strength" ? b.strength.score : b.opportunity.score;
        if (av == null) return 1;
        if (bv == null) return -1;
        return bv - av;
      });
  }, [data, stateFilter, query, sort]);

  const quadrantData = useMemo(() => {
    if (!data) return [];
    return data.boards
      .filter((b) => b.strength.score != null && b.opportunity.score != null)
      .map((b) => ({
        name: b.board_name,
        x: b.opportunity.score!,
        y: b.strength.score!,
        state: b.state,
        priority: b.priority,
      }));
  }, [data]);

  const xDivider = data?.thresholds?.opportunity ?? 50;
  const yDivider = data?.thresholds?.strong ?? 50;
  const xDomain = useMemo(() => fitDomain(quadrantData.map((d) => d.x), xDivider), [quadrantData, xDivider]);
  const yDomain = useMemo(() => fitDomain(quadrantData.map((d) => d.y), yDivider), [quadrantData, yDivider]);

  const quadrantCounts = useMemo(() => {
    const c = { tl: 0, tr: 0, bl: 0, br: 0 };
    quadrantData.forEach((d) => {
      if (d.x >= xDivider && d.y >= yDivider) c.tr += 1;
      else if (d.x < xDivider && d.y >= yDivider) c.tl += 1;
      else if (d.x >= xDivider && d.y < yDivider) c.br += 1;
      else c.bl += 1;
    });
    return c;
  }, [quadrantData, xDivider, yDivider]);

  const sortButton = (key: SortKey, label: string) => (
    <button
      onClick={() => setSort(key)}
      className={cn(
        "rounded-md px-2 py-1 text-[11px] transition-colors",
        sort === key ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
      )}
    >
      {label}
    </button>
  );

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">30 个主题板块 · 强度分 × 机会分 双维评分</p>
        <button
          onClick={() => void load(true)}
          disabled={loading}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border/60 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:text-primary disabled:opacity-50"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          重新计算
        </button>
      </div>

      {error && (
        <GlassCard className="mb-4 border-danger/30 p-4 text-sm text-danger">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4" />
            {error}
          </div>
          <p className="mt-1 pl-6 text-xs text-muted-foreground">首次计算需拉取全量行情，后续会使用本地缓存。</p>
        </GlassCard>
      )}

      {loading && !data ? (
        <GlassCard className="py-16 text-center">
          <RefreshCw className="mx-auto h-5 w-5 animate-spin text-primary" />
          <p className="mt-3 text-sm text-muted-foreground">正在拉取板块行情与基本面数据…</p>
        </GlassCard>
      ) : data ? (
        <>
          {loading && (
            <div className="mb-3 flex items-center gap-2 rounded-lg border border-primary/25 bg-primary/10 px-3 py-2 text-xs text-primary">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              正在后台刷新，当前显示缓存…
            </div>
          )}
          {(data.stale || data.refresh_error) && (
            <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              当前展示上次成功缓存；本次刷新失败：{data.refresh_error || "数据源暂不可用"}
            </div>
          )}
          {data.is_intraday && (
            <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              当前为盘中快照，评分会随行情变化；收盘后重新计算得到完整日值。
            </div>
          )}

          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MetricCard
              icon={Gauge}
              label="板块覆盖"
              value={`${data.board_count} 个板块`}
              note={`${data.is_intraday ? "盘中" : "收盘"} ${data.as_of}`}
            />
            <MetricCard
              icon={TrendingUp}
              label="强度权重"
              value="趋势25% 扩散30%"
              note="交易20% 稳定15% 集中10%"
            />
            <MetricCard
              icon={Crosshair}
              label="机会权重"
              value="基本面30% 预期20%"
              note="估值15% 位置20% 拥挤15%"
            />
            <MetricCard
              icon={Shield}
              label="硬约束"
              value="机会<40 不参与"
              note="拥挤≥15 不追高"
            />
          </div>

          {/* 强度-机会二维矩阵 */}
          <GlassCard className="mb-4 p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold">强度—机会矩阵</h3>
              <div className="flex flex-wrap items-center gap-2.5">
                {quadrantLegend.map((l) => (
                  <span key={l.label} className="inline-flex items-center gap-1 text-[10px] text-muted-foreground/80">
                    <span className={cn("inline-block h-2 w-2 rounded-full border", l.cls)} />
                    {l.label}
                  </span>
                ))}
              </div>
            </div>

            {/* 全宽矩阵：左 Y 轴 + 图区 + 底部 X 轴（分界线与后端状态阈值一致） */}
            <div className="flex gap-1.5">
              <span className="flex w-3 items-center justify-center text-[10px] tracking-widest text-muted-foreground [writing-mode:vertical-lr]">
                强度分 →
              </span>
              <div className="min-w-0 flex-1">
                <div className="relative h-[420px] overflow-hidden rounded-lg">
                  {(() => {
                    const xLine = pct(xDivider, xDomain);
                    const yLine = 100 - pct(yDivider, yDomain);
                    const grid = (t: number[], axis: "x" | "y") =>
                      t.filter((v) => v !== (axis === "x" ? xDivider : yDivider)).map((v) =>
                        axis === "x" ? (
                          <div key={`v-${v}`} className="pointer-events-none absolute top-0 h-full w-px bg-border/20" style={{ left: `${pct(v, xDomain)}%` }} />
                        ) : (
                          <div key={`h-${v}`} className="pointer-events-none absolute left-0 h-px w-full bg-border/20" style={{ top: `${100 - pct(v, yDomain)}%` }} />
                        ),
                      );
                    return (
                      <>
                        <div className="absolute left-0 top-0 bg-danger/5" style={{ width: `${xLine}%`, height: `${yLine}%` }} />
                        <div className="absolute top-0 bg-primary/5" style={{ left: `${xLine}%`, right: 0, height: `${yLine}%` }} />
                        <div className="absolute bottom-0 left-0 bg-muted/25" style={{ width: `${xLine}%`, top: `${yLine}%` }} />
                        <div className="absolute bottom-0 bg-success/5" style={{ left: `${xLine}%`, right: 0, top: `${yLine}%` }} />
                        <div className="pointer-events-none absolute inset-0 rounded-lg border border-border/50" />
                        {/* 象限标签 + 计数 */}
                        <div className="absolute left-0 top-0 flex w-1/2 items-start justify-between p-2" style={{ width: `${xLine}%` }}>
                          <span className="text-[10px] font-medium text-danger/70">强势但不追</span>
                          <span className="text-[10px] tabular-nums text-danger/50">{quadrantCounts.tl}</span>
                        </div>
                        <div className="absolute top-0 flex items-start justify-between p-2" style={{ left: `${xLine}%`, right: 0 }}>
                          <span className="text-[10px] tabular-nums text-primary/50">{quadrantCounts.tr}</span>
                          <span className="text-[10px] font-medium text-primary/70">主线可参与</span>
                        </div>
                        <div className="absolute bottom-0 left-0 flex items-end justify-between p-2" style={{ width: `${xLine}%` }}>
                          <span className="text-[10px] font-medium text-muted-foreground/50">弱势回避</span>
                          <span className="text-[10px] tabular-nums text-muted-foreground/40">{quadrantCounts.bl}</span>
                        </div>
                        <div className="absolute bottom-0 flex items-end justify-between p-2" style={{ left: `${xLine}%`, right: 0 }}>
                          <span className="text-[10px] tabular-nums text-success/50">{quadrantCounts.br}</span>
                          <span className="text-[10px] font-medium text-success/70">左侧/启动</span>
                        </div>
                        {/* 网格线（跳过动态状态分界） */}
                        {grid(xDomain.ticks, "x")}
                        {grid(yDomain.ticks, "y")}
                        <div className="pointer-events-none absolute top-0 h-full w-px bg-foreground/30" style={{ left: `${xLine}%` }} />
                        <div className="pointer-events-none absolute left-0 h-px w-full bg-foreground/30" style={{ top: `${yLine}%` }} />
                        {/* 数据点（细点，半透明填充减少重叠遮盖） */}
                        {quadrantData.map((d) => (
                          <div
                            key={d.name}
                            className="group absolute z-10 -translate-x-1/2 -translate-y-1/2"
                            style={{ left: `${pct(d.x, xDomain)}%`, top: `${100 - pct(d.y, yDomain)}%` }}
                          >
                            <div className="pointer-events-none absolute left-1/2 top-1/2 h-px w-[200vw] -translate-x-1/2 bg-foreground/25 opacity-0 group-hover:opacity-60" />
                            <div className="pointer-events-none absolute left-1/2 top-1/2 h-[200vh] w-px -translate-y-1/2 bg-foreground/25 opacity-0 group-hover:opacity-60" />
                            <div
                              className={cn(
                                "relative h-2 w-2 rounded-full border transition-transform group-hover:scale-[1.8]",
                                quadrantDot[d.state] || "border-muted-foreground/40 bg-muted-foreground/20",
                              )}
                            />
                            <div className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded bg-popover px-2 py-1 text-[10px] text-popover-foreground opacity-0 shadow-md transition-opacity group-hover:opacity-100">
                              {d.name} · 强度{format(d.y, 0)} 机会{format(d.x, 0)}
                            </div>
                          </div>
                        ))}
                      </>
                    );
                  })()}
                </div>
                {/* X 轴刻度 */}
                <div className="relative mt-1 h-3.5 text-[10px] tabular-nums text-muted-foreground/60">
                  {xDomain.ticks.map((t) => (
                    <span key={t} className="absolute -translate-x-1/2" style={{ left: `${pct(t, xDomain)}%` }}>{t}</span>
                  ))}
                </div>
                <p className="mt-0.5 text-center text-[10px] text-muted-foreground">机会分 →</p>
              </div>
            </div>
          </GlassCard>

          {/* 板块主榜 */}
          <GlassCard className="min-w-0 p-0">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/50 px-4 py-3">
              <div className="flex items-center gap-1.5">
                <div className="relative">
                  <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="搜索板块"
                    className="h-8 w-32 rounded-lg border border-border/60 bg-background/30 pl-8 pr-2 text-xs outline-none focus:border-primary/60"
                  />
                </div>
                <select
                  value={stateFilter}
                  onChange={(e) => setStateFilter(e.target.value)}
                  className="h-8 rounded-lg border border-border/60 bg-background/30 px-2 text-xs outline-none"
                >
                  {states.map((s) => <option key={s}>{s}</option>)}
                </select>
              </div>
              <div className="flex flex-wrap items-center">
                <span className="mr-1 text-[11px] text-muted-foreground/60">排序</span>
                {sortButton("priority", "优先级")}
                {sortButton("strength", "强度")}
                {sortButton("opportunity", "机会")}
              </div>
            </div>

            <div className="max-h-[600px] overflow-auto">
              <table className="w-full min-w-[760px] text-left text-xs">
                <thead className="sticky top-0 z-[1] bg-card/95 text-[11px] text-muted-foreground backdrop-blur">
                  <tr>
                    <th className="px-4 py-2.5 font-medium">板块</th>
                    <th className="px-3 py-2.5 font-medium">强度分</th>
                    <th className="px-3 py-2.5 font-medium">机会分</th>
                    <th className="px-3 py-2.5 font-medium">优先级</th>
                    <th className="px-3 py-2.5 font-medium">状态</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/40">
                  {rows.map((row) => {
                    const isOpen = expanded === row.board_code;
                    return (
                    <Fragment key={row.board_code}>
                    <tr
                      onClick={() => setExpanded(isOpen ? null : row.board_code)}
                      className={cn("cursor-pointer transition-colors hover:bg-muted/25", isOpen && "bg-muted/20")}
                    >
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-1.5 font-medium">
                          {isOpen ? (
                            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-primary" />
                          ) : (
                            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
                          )}
                          {row.board_name}
                        </div>
                        <div className="pl-5 text-[10px] text-muted-foreground/60">
                          {row.board_code} · 有效{row.effective_constituent_count ?? row.constituent_count}/{row.constituent_count}只 · {row.board_group}
                          {row.confidence && ` · 置信度${row.confidence}`}
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <ScoreBar value={row.strength.score} color="primary" />
                        <div className="mt-0.5 text-[10px] text-muted-foreground">
                          趋势{format(row.strength.detail.relative_trend, 0)} 扩散{format(row.strength.detail.breadth_impulse, 0)}
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <ScoreBar value={row.opportunity.score} color="success" />
                        <div className="mt-0.5 text-[10px] text-muted-foreground">
                          基本面{format(row.opportunity.detail.fundamental, 0)} 估值{format(row.opportunity.detail.valuation_match, 0)}
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="font-mono text-sm font-bold">{format(row.priority)}</span>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className={cn("inline-block rounded px-1.5 py-0.5 text-[10px]", stateTone[row.state] || "bg-muted text-muted-foreground")}>
                          {row.state}
                        </span>
                        {row.signal && (
                          <div className="mt-0.5 text-[10px] text-muted-foreground/60">
                            {signalLabel[row.signal] || row.signal}
                          </div>
                        )}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="bg-muted/10">
                        <td colSpan={5} className="px-4 py-2">
                          <ExpandedBoardDetail row={row} />
                        </td>
                      </tr>
                    )}
                    </Fragment>
                    );
                  })}
                </tbody>
              </table>
              {rows.length === 0 && <p className="py-12 text-center text-sm text-muted-foreground">没有匹配板块</p>}
            </div>
            <div className="border-t border-border/50 px-4 py-2 text-[11px] text-muted-foreground/60">
              显示 {rows.length}/{data.board_count} · 强度分确认趋势，机会分判断价值，不作为投资建议
            </div>
          </GlassCard>

          {/* 口径说明 */}
          <GlassCard className="mt-4 p-4">
            <details>
              <summary className="cursor-pointer text-sm font-semibold">评分口径与依据</summary>
              <div className="mt-3 grid gap-4 text-[11px] leading-relaxed text-muted-foreground lg:grid-cols-[minmax(0,1fr)_310px]">
                <div className="space-y-2">
                  <p>{data.methodology.framework}</p>
                  {data.methodology.definitions.map((d) => <p key={d}>· {d}</p>)}
                  {data.methodology.hard_constraints.map((c) => <p key={c}>· {c}</p>)}
                </div>
                <div className="border-t border-border/50 pt-2 lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0">
                  {data.methodology.sources.map((s) =>
                    s.url ? (
                      <a key={s.label} href={s.url} target="_blank" rel="noreferrer" className="block truncate text-primary/80 hover:text-primary">
                        {s.label}
                      </a>
                    ) : (
                      <span key={s.label} className="block truncate">{s.label}</span>
                    ),
                  )}
                </div>
              </div>
            </details>
          </GlassCard>
        </>
      ) : null}
    </div>
  );
}
