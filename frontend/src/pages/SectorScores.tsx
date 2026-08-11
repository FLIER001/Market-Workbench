import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  ChevronRight,
  CircleDollarSign,
  Gauge,
  RefreshCw,
  Search,
  TrendingUp,
} from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import {
  api,
  type SectorScoreRow,
  type SectorScoresData,
  type SwLevel2Data,
  type SwLevel2Row,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type SortKey = "score" | "valuation" | "prosperity" | "attention" | "crowding";
const LOCAL_CACHE_KEY = "vr-sector-scores-cache-v1";
const LEVEL2_CACHE_KEY = "vr-sector-level2-cache-v1";

const loadLevel2Cache = (): SwLevel2Data | null => {
  try {
    const cached = JSON.parse(localStorage.getItem(LEVEL2_CACHE_KEY) || "null");
    return cached?.industries?.length > 0 ? (cached as SwLevel2Data) : null;
  } catch {
    return null;
  }
};

const loadLocalCache = (): SectorScoresData | null => {
  try {
    const cached = JSON.parse(localStorage.getItem(LOCAL_CACHE_KEY) || "null");
    return cached?.industries?.length > 0 ? cached as SectorScoresData : null;
  } catch {
    return null;
  }
};

const saveLocalCache = (data: SectorScoresData) => {
  try {
    localStorage.setItem(LOCAL_CACHE_KEY, JSON.stringify(data));
  } catch {
    /* 浏览器存储不可用时仍可使用本次会话结果 */
  }
};

const scoreValue = (row: SectorScoreRow, key: SortKey) => {
  if (key === "score") return row.score;
  if (key === "valuation") return row.valuation.score;
  if (key === "prosperity") return row.prosperity.score;
  if (key === "attention") return row.attention.score;
  return row.crowding.risk;
};

const format = (value: number | null | undefined, suffix = "", digits = 1) =>
  value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(digits)}${suffix}`;

const signed = (value: number | null | undefined, suffix: string) =>
  value == null || !Number.isFinite(value)
    ? "—"
    : `${value > 0 ? "+" : ""}${value.toFixed(1)}${suffix}`;

const shortDate = (value: string) => {
  const parts = value.split("-");
  return parts.length === 3 ? `${Number(parts[1])}月${Number(parts[2])}日` : value;
};

const changeTone = (value: number | null | undefined) =>
  value == null ? "text-muted-foreground" : value > 0 ? "text-danger" : value < 0 ? "text-success" : "text-muted-foreground";

const phaseTone: Record<SectorScoreRow["phase"], string> = {
  综合占优: "bg-primary/15 text-primary",
  赔率观察: "bg-warning/15 text-warning",
  集中风险: "bg-danger/15 text-danger",
  相对偏弱: "bg-success/15 text-success",
  中性观察: "bg-muted text-muted-foreground",
};

function ScoreBar({ value, risk = false }: { value: number | null; risk?: boolean }) {
  return (
    <div className="flex min-w-[76px] items-center gap-2">
      <span className={cn("w-8 text-right font-mono text-xs font-semibold", risk && (value || 0) >= 80 ? "text-danger" : "text-foreground")}>
        {format(value, "", 0)}
      </span>
      <span className="h-1.5 w-9 overflow-hidden rounded-full bg-muted">
        <span
          className={cn("block h-full rounded-full", risk && (value || 0) >= 80 ? "bg-danger" : "bg-primary")}
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

function Level2SubTable({
  rows,
  asOf,
  loading,
  error,
}: {
  rows: SwLevel2Row[];
  asOf: string | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading && rows.length === 0) {
    return (
      <div className="flex items-center gap-2 px-2 py-6 text-xs text-muted-foreground">
        <RefreshCw className="h-3.5 w-3.5 animate-spin text-primary" />
        正在读取申万二级行业评分…
      </div>
    );
  }
  if (error && rows.length === 0) {
    return <p className="px-2 py-4 text-xs text-danger">二级行业评分加载失败：{error}</p>;
  }
  if (rows.length === 0) {
    return <p className="px-2 py-4 text-xs text-muted-foreground">该一级行业下暂无二级行业数据</p>;
  }
  return (
    <div className="overflow-x-auto px-2 py-2">
      <table className="w-full min-w-[860px] text-left text-[11px]">
        <thead>
          <tr className="text-[10px] text-muted-foreground/70">
            <th className="px-2 py-1.5 font-medium">二级行业</th>
            <th className="px-2 py-1.5 font-medium">综合</th>
            <th className="px-2 py-1.5 font-medium">估值赔率</th>
            <th className="px-2 py-1.5 font-medium">盈利景气</th>
            <th className="px-2 py-1.5 font-medium">交易确认</th>
            <th className="px-2 py-1.5 font-medium">集中风险</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/30">
          {rows.map((row) => (
            <tr key={row.code} className="hover:bg-muted/10">
              <td className="px-2 py-1.5">
                <span className="font-medium">{row.name}</span>
                <span className={cn("ml-1.5 font-mono", changeTone(row.latest_return))}>
                  {signed(row.latest_return, "%")}
                </span>
              </td>
              <td className="px-2 py-1.5">
                <ScoreBar value={row.score} />
                <span className={cn("mt-0.5 inline-block rounded px-1 py-0.5 text-[9px]", phaseTone[row.phase])}>
                  {row.phase}
                </span>
              </td>
              <td className="px-2 py-1.5">
                <ScoreBar value={row.valuation.score} />
                <div className="mt-0.5 text-[9px] text-muted-foreground">
                  PE {format(row.valuation.pe)} / {format(row.valuation.pe_percentile, "%分位")}
                  {" · "}PB {format(row.valuation.pb)} / {format(row.valuation.pb_percentile, "%分位")}
                </div>
              </td>
              <td className="px-2 py-1.5">
                <ScoreBar value={row.prosperity.score} />
                <div className="mt-0.5 whitespace-nowrap text-[9px] text-muted-foreground">
                  较3个月前 <span className={changeTone(row.prosperity.earnings_3m)}>{signed(row.prosperity.earnings_3m, "%")}</span>
                  {" · "}较12个月前 <span className={changeTone(row.prosperity.earnings_yoy)}>{signed(row.prosperity.earnings_yoy, "%")}</span>
                </div>
              </td>
              <td className="px-2 py-1.5">
                <ScoreBar value={row.attention.score} />
                <div className="mt-0.5 text-[9px] text-muted-foreground">
                  换手 {format(row.attention.turnover_rate, "%")} / {format(row.attention.turnover_rate_percentile, "%分位")}
                  {" · "}占比 {format(row.attention.turnover_share, "%")}
                </div>
              </td>
              <td className="px-2 py-1.5">
                <ScoreBar value={row.crowding.risk} risk />
                <div className="mt-0.5 text-[9px] text-muted-foreground">
                  扣 {format(row.crowding.penalty)}
                  {row.data_quality.missing.length > 0 && ` · 缺${row.data_quality.missing.join("/")}`}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-2 pb-1 pt-1.5 text-[10px] text-muted-foreground/60">
        申万二级行业 · {asOf ? `${shortDate(asOf)} 日频` : ""} · 与一级评分同一公式，排名在 131 个二级行业内横向比较
      </p>
    </div>
  );
}

export function SectorScoresPanel() {
  const [data, setData] = useState<SectorScoresData | null>(loadLocalCache);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState("全部");
  const [sort, setSort] = useState<SortKey>("score");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [level2, setLevel2] = useState<SwLevel2Data | null>(loadLevel2Cache);
  const [level2Loading, setLevel2Loading] = useState(false);
  const [level2Error, setLevel2Error] = useState<string | null>(null);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.sectorScores(refresh);
      setData(next);
      saveLocalCache(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "行业评分加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const start = async () => {
      if (!data) {
        try {
          const cached = await api.sectorScoresCache();
          if (!cancelled && cached) {
            setData(cached);
            saveLocalCache(cached);
          }
        } catch {
          /* 没有后端缓存时继续进行正常首次计算 */
        }
      }
      if (!cancelled) await load(false);
    };
    void start();
    return () => {
      cancelled = true;
    };
    // 仅在页面首次进入时执行；data 是启动瞬间的缓存快照。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    const start = async () => {
      if (!level2) {
        try {
          const cached = await api.sectorScoresLevel2Cache();
          if (!cancelled && cached) {
            setLevel2(cached);
            try {
              localStorage.setItem(LEVEL2_CACHE_KEY, JSON.stringify(cached));
            } catch {
              /* 忽略本地存储失败 */
            }
          }
        } catch {
          /* 无后端缓存时走正常加载 */
        }
      }
      setLevel2Loading(true);
      try {
        const next = await api.sectorScoresLevel2(false);
        if (cancelled) return;
        setLevel2(next);
        setLevel2Error(null);
        try {
          localStorage.setItem(LEVEL2_CACHE_KEY, JSON.stringify(next));
        } catch {
          /* 忽略本地存储失败 */
        }
      } catch (err) {
        if (!cancelled) {
          setLevel2Error(err instanceof Error ? err.message : "二级行业指标加载失败");
        }
      } finally {
        if (!cancelled) setLevel2Loading(false);
      }
    };
    void start();
    return () => {
      cancelled = true;
    };
    // 仅首次进入加载；level2 为启动瞬间的缓存快照。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const level2ByLevel1 = useMemo(() => {
    const grouped = new Map<string, SwLevel2Row[]>();
    for (const row of level2?.industries || []) {
      const list = grouped.get(row.level1_code) || [];
      list.push(row);
      grouped.set(row.level1_code, list);
    }
    return grouped;
  }, [level2]);

  const phases = useMemo(() => {
    const values = new Set(data?.industries.map((row) => row.phase) || []);
    return ["全部", ...Array.from(values)];
  }, [data]);

  const rows = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return [...(data?.industries || [])]
      .filter((row) => phase === "全部" || row.phase === phase)
      .filter((row) => !keyword || row.name.toLowerCase().includes(keyword) || row.code.toLowerCase().includes(keyword))
      .sort((a, b) => {
        const av = scoreValue(a, sort);
        const bv = scoreValue(b, sort);
        if (av == null) return 1;
        if (bv == null) return -1;
        return sort === "crowding" ? av - bv : bv - av;
      });
  }, [data, phase, query, sort]);

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
        <p className="text-xs text-muted-foreground">申万一级行业 · 最新交易日状态 × 月度历史锚 · 点击行业行展开二级行业指标</p>
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
          <p className="mt-1 pl-6 text-xs text-muted-foreground">首次计算需读取申万历史月报，后续会使用本地缓存。</p>
        </GlassCard>
      )}

      {loading && !data ? (
        <GlassCard className="py-16 text-center">
          <RefreshCw className="mx-auto h-5 w-5 animate-spin text-primary" />
          <p className="mt-3 text-sm text-muted-foreground">正在读取申万月度历史与最新交易日数据…</p>
        </GlassCard>
      ) : data ? (
        <>
          {loading && (
            <div className="mb-3 flex items-center gap-2 rounded-lg border border-primary/25 bg-primary/10 px-3 py-2 text-xs text-primary">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              正在后台读取最新数据，当前先显示缓存；完成后会自动更新。
            </div>
          )}
          {(data.stale || data.refresh_error) && (
            <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              当前展示上次成功缓存；本次刷新失败：{data.refresh_error || "数据源暂不可用"}
            </div>
          )}
          {data.current_frequency !== "daily" && (
            <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              最新日频数据暂不可用，当前退回月报口径：{data.daily_error || "日频数据源未返回完整样本"}
            </div>
          )}
          <div className="mb-3 rounded-lg border border-primary/25 bg-primary/10 px-3 py-2.5 text-xs">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-semibold text-primary">
                数据截至 {shortDate(data.as_of)}{data.quote_time ? ` ${data.quote_time}` : ""}
              </span>
              <span>{data.current_source_label || "申万数据"}</span>
              {data.is_intraday && <span className="rounded bg-warning/15 px-1.5 py-0.5 text-warning">盘中动态值</span>}
              {data.coverage_pct != null && (
                <span className="text-muted-foreground">
                  成分行情覆盖 {data.quoted_component_count}/{data.component_count}（{format(data.coverage_pct, "%")}）
                </span>
              )}
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
              {data.calculation_method || data.methodology.frequency}
              {data.classification_as_of && ` · 申万分类更新 ${data.classification_as_of}`}
            </p>
          </div>
          {data.is_intraday && (
            <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              当前为盘中快照，成交与换手尚未形成完整交易日，交易确认和综合评分会随行情继续变化；收盘后重新计算得到完整日值。
            </div>
          )}
          {data.current_source !== "tencent_constituent_aggregate" && data.aggregate_error && (
            <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              腾讯成分聚合暂不可用，已回退申万备用源：{data.aggregate_error}
            </div>
          )}

          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MetricCard
              icon={Gauge}
              label="评分覆盖"
              value={`${data.industries.length} 个申万一级行业`}
              note={`${data.is_intraday ? "盘中" : data.current_frequency === "daily" ? "交易日" : "月报"} ${data.as_of}${data.quote_time ? ` ${data.quote_time}` : ""}`}
            />
            <MetricCard
              icon={BookOpen}
              label="历史区间"
              value={`${data.history_samples} 个月`}
              note={
                data.history_partial
                  ? `${data.history_start} 起 · 月报成功 ${data.history_samples}/${data.history_requested || 60}`
                  : `${data.history_start} 至 ${data.monthly_as_of}`
              }
            />
            <MetricCard icon={TrendingUp} label="景气权重" value={`${data.methodology.weights.prosperity}%`} note="较12个月前 70% + 较3个月前 30%" />
            <MetricCard
              icon={CircleDollarSign}
              label="估值 / 交易确认"
              value={`${data.methodology.weights.valuation}% / ${data.methodology.weights.attention}%`}
              note={`交易确认使用近 ${data.daily_history_samples || "—"} 个交易日`}
            />
          </div>

          <div className="mb-4 space-y-4">
            <GlassCard className="min-w-0 p-0">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/50 px-4 py-3">
                <div className="flex items-center gap-1.5">
                  <div className="relative">
                    <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="搜索行业"
                      className="h-8 w-36 rounded-lg border border-border/60 bg-background/30 pl-8 pr-2 text-xs outline-none focus:border-primary/60"
                    />
                  </div>
                  <select
                    value={phase}
                    onChange={(event) => setPhase(event.target.value)}
                    className="h-8 rounded-lg border border-border/60 bg-background/30 px-2 text-xs outline-none"
                  >
                    {phases.map((item) => <option key={item}>{item}</option>)}
                  </select>
                </div>
                <div className="flex flex-wrap items-center">
                  <span className="mr-1 text-[11px] text-muted-foreground/60">排序</span>
                  {sortButton("score", "综合")}
                  {sortButton("valuation", "估值")}
                  {sortButton("prosperity", "景气")}
                  {sortButton("attention", "确认")}
                  {sortButton("crowding", "低集中")}
                </div>
              </div>

              <div className="max-h-[660px] overflow-auto">
                <table className="w-full min-w-[820px] text-left text-xs">
                  <thead className="sticky top-0 z-[1] bg-card/95 text-[11px] text-muted-foreground backdrop-blur">
                    <tr>
                      <th className="px-4 py-2.5 font-medium">行业</th>
                      <th className="px-3 py-2.5 font-medium">综合</th>
                      <th className="px-3 py-2.5 font-medium">估值赔率</th>
                      <th className="px-3 py-2.5 font-medium">盈利景气</th>
                      <th className="px-3 py-2.5 font-medium">交易确认</th>
                      <th className="px-3 py-2.5 font-medium">集中风险</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40">
                    {rows.map((row) => {
                      const isOpen = expanded === row.code;
                      const childRows = level2ByLevel1.get(row.code) || [];
                      return (
                      <Fragment key={row.code}>
                      <tr
                        className={cn(
                          "cursor-pointer transition-colors hover:bg-muted/20",
                          isOpen && "bg-muted/15",
                        )}
                        onClick={() => setExpanded(isOpen ? null : row.code)}
                        aria-expanded={isOpen}
                      >
                        <td className="px-4 py-2.5">
                          <div className="flex items-center gap-1.5 font-medium">
                            {isOpen ? (
                              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-primary" />
                            ) : (
                              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
                            )}
                            {row.name}
                          </div>
                          <div className="flex items-center gap-1.5 text-[10px]">
                            <span className="font-mono text-muted-foreground/60">{row.code}</span>
                            <span className={changeTone(row.latest_return)}>
                              {shortDate(data.as_of)} {signed(row.latest_return, "%")}
                            </span>
                          </div>
                        </td>
                        <td className="px-3 py-2.5">
                          <ScoreBar value={row.score} />
                          <span className={cn("mt-1 inline-block rounded px-1.5 py-0.5 text-[10px]", phaseTone[row.phase])}>
                            {row.phase}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <ScoreBar value={row.valuation.score} />
                          <div className="mt-1 text-[10px] text-muted-foreground">
                            PE {format(row.valuation.pe)} / {format(row.valuation.pe_percentile, "%分位")}
                            {" · "}PB {format(row.valuation.pb)} / {format(row.valuation.pb_percentile, "%分位")}
                          </div>
                        </td>
                        <td className="px-3 py-2.5">
                          <ScoreBar value={row.prosperity.score} />
                          <div className="mt-1 whitespace-nowrap text-[10px] text-muted-foreground">
                            较3个月前 <span className={changeTone(row.prosperity.earnings_3m)}>{signed(row.prosperity.earnings_3m, "%")}</span>
                            {" · "}较12个月前 <span className={changeTone(row.prosperity.earnings_yoy)}>{signed(row.prosperity.earnings_yoy, "%")}</span>
                          </div>
                        </td>
                        <td className="px-3 py-2.5">
                          <ScoreBar value={row.attention.score} />
                          <div className="mt-1 text-[10px] text-muted-foreground">
                            换手 {format(row.attention.turnover_rate, "%")} / {format(row.attention.turnover_rate_percentile, "%分位")}
                            {" · "}成交占比 {format(row.attention.turnover_share, "%")}
                          </div>
                        </td>
                        <td className="px-3 py-2.5">
                          <ScoreBar value={row.crowding.risk} risk />
                          <div className="mt-1 text-[10px] text-muted-foreground">
                            活跃双高 · 扣 {format(row.crowding.penalty)}
                            {row.data_quality.missing.length > 0 && ` · 缺${row.data_quality.missing.join("/")}`}
                          </div>
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-muted/10">
                          <td colSpan={6} className="px-4 py-1">
                            <Level2SubTable
                              rows={childRows}
                              asOf={level2?.as_of || null}
                              loading={level2Loading}
                              error={level2Error}
                            />
                          </td>
                        </tr>
                      )}
                      </Fragment>
                      );
                    })}
                  </tbody>
                </table>
                {rows.length === 0 && <p className="py-12 text-center text-sm text-muted-foreground">没有匹配行业</p>}
              </div>
              <div className="border-t border-border/50 px-4 py-2 text-[11px] text-muted-foreground/60">
                显示 {rows.length}/{data.industries.length} · 分数均为同口径相对观察值
              </div>
            </GlassCard>

            <GlassCard className="p-4">
              <details>
                <summary className="cursor-pointer text-sm font-semibold">口径与研究依据</summary>
                <div className="mt-3 grid gap-4 text-[11px] leading-relaxed text-muted-foreground lg:grid-cols-[minmax(0,1fr)_310px]">
                  <div className="space-y-2">
                    <p>{data.methodology.classification}</p>
                    <p>{data.methodology.frequency}</p>
                    {data.methodology.definitions.map((item) => <p key={item}>· {item}</p>)}
                    <p>· {data.methodology.penalty}</p>
                  </div>
                  <div className="border-t border-border/50 pt-2 lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0">
                    {data.methodology.sources.map((source) => (
                      source.url ? (
                        <a key={source.label} href={source.url} target="_blank" rel="noreferrer" className="block truncate text-primary/80 hover:text-primary">
                          {source.label}
                        </a>
                      ) : (
                        <span key={source.label} className="block truncate">{source.label}</span>
                      )
                    ))}
                  </div>
                </div>
              </details>
            </GlassCard>
          </div>
        </>
      ) : null}
    </div>
  );
}
