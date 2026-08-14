import { useMemo, useState } from "react";
import {
  TrendingUp, TrendingDown, ExternalLink, RefreshCw,
  ChevronDown, ChevronRight,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { ProbabilityTrend, type TrendPoint } from "@/components/pulse/ProbabilityTrend";
import { api, type PulseOverview, type PulseModule, type PulseMarket } from "@/lib/api";
import { useSWR } from "@/hooks/useSWR";
import { cn } from "@/lib/utils";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function isValidOverview(d: PulseOverview | null): d is PulseOverview {
  return !!d && Array.isArray(d.modules) && typeof d.as_of === "string";
}

export function Pulse() {
  const [err, setErr] = useState<string | null>(null);
  const [showReference, setShowReference] = useState(false);
  const [selected, setSelected] = useState<PulseMarket | null>(null);
  const [history, setHistory] = useState<TrendPoint[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const { data, loading, revalidating, revalidate } = useSWR<PulseOverview>(
    "pulse:v1",
    async (fresh) => {
      const d = await api.pulseOverview(fresh);
      if (!isValidOverview(d)) throw new Error("预期概率数据格式异常");
      return d;
    },
    [],
    (e) => setErr(e instanceof Error ? e.message : "加载失败"),
    { persist: true },
  );

  const refreshing = revalidating || !!data?.updating;

  const load = async () => {
    setErr(null);
    const prevAsOf = data?.as_of;
    try {
      await revalidate(true);
    } finally {
      // 后台重建（Kalshi 全量 1-8 分钟）：轮询直到 as_of 前进
      if (prevAsOf) {
        for (let i = 0; i < 24; i++) {
          await sleep(20000);
          try {
            const fresh = await api.pulseOverview(false);
            if (fresh.as_of !== prevAsOf && !fresh.updating) {
              await revalidate(false);
              break;
            }
            if (fresh.cache_state === "error") {
              await revalidate(false);
              setErr(`更新失败，继续展示上次成功数据：${fresh.refresh_error || "数据源暂不可用"}`);
              break;
            }
          } catch { /* 瞬时失败继续轮询 */ }
        }
      }
    }
  };

  const selectMarket = (m: PulseMarket) => {
    setSelected(m);
    setHistory([]);
    if (!m.token_id_yes) return; // Kalshi 无趋势 token
    setHistoryLoading(true);
    api.pulseHistory(m.token_id_yes, "1m")
      .then((points) => setHistory(points))
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false));
  };

  const coreModules = useMemo(() => (data?.modules ?? []).filter((m) => m.core), [data]);
  const refModules = useMemo(() => (data?.modules ?? []).filter((m) => !m.core), [data]);
  const refCount = refModules.reduce((n, m) => n + m.market_count, 0);

  return (
    <div>
      <PageHeader
        title="全球预期概率"
        subtitle="Polymarket + Kalshi 双源 · 全市场宏观情绪温度计（货币政策 / 宏观 / 地缘 / 政治 / 大宗 / AI）"
        actions={
          <div className="flex flex-col items-end gap-1">
            <button
              onClick={() => void load()}
              disabled={refreshing}
              className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors hover:border-primary disabled:opacity-60"
              title="重新拉取两站最新概率"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
              {refreshing ? "更新中…" : "更新"}
            </button>
            {refreshing ? (
              <span className="text-[11px] text-primary/80">双源重建中（Kalshi 全量约 1-8 分钟，完成自动刷新）</span>
            ) : data?.as_of ? (
              <span className="text-[11px] text-muted-foreground/70">数据时点 {data.as_of.replace("T", " ")}</span>
            ) : null}
          </div>
        }
      />

      {err && (
        <div className="mb-4 rounded border border-danger/30 bg-danger/5 p-3 text-sm text-danger">{err}</div>
      )}

      {data?.cache_state === "error" && !err && (
        <div className="mb-4 rounded border border-warning/30 bg-warning/5 p-3 text-sm text-warning">
          本次更新失败，当前展示上次成功缓存：{data.refresh_error || "数据源暂不可用"}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_380px]">
        <div className="flex flex-col gap-5">
          {loading && !data ? (
            [1, 2, 3].map((i) => <div key={i} className="h-40 animate-pulse rounded-lg bg-muted/50" />)
          ) : (
            <>
              {coreModules.map((g) => (
                <ModuleSection key={g.key} group={g} selected={selected} onSelect={selectMarket} />
              ))}

              {refModules.length > 0 && (
                <div className="border-t pt-4">
                  <button
                    onClick={() => setShowReference((v) => !v)}
                    className="flex items-center gap-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
                  >
                    {showReference ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                    其他 · 参考（加密 / 体育 / 娱乐）· {refCount} 个
                    <span className="text-[11px] text-muted-foreground/60">非投资锚，仅参考</span>
                  </button>
                  {showReference && (
                    <div className="mt-4 flex flex-col gap-5">
                      {refModules.map((g) => (
                        <ModuleSection key={g.key} group={g} selected={selected} onSelect={selectMarket} />
                      ))}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        <div className="w-full self-start lg:sticky lg:top-4">
          {!selected ? (
            <GlassCard>
              <p className="text-sm text-muted-foreground">
                点选左侧任一事件查看详情。Polymarket 事件附 Yes 概率历史趋势；Kalshi 事件展示当前概率。
              </p>
            </GlassCard>
          ) : (
            <DetailPanel market={selected} history={history} historyLoading={historyLoading} />
          )}
        </div>
      </div>

      <p className="mt-6 border-t pt-3 text-xs text-muted-foreground/70">
        数据来自 Polymarket + Kalshi 公开 API（免登录只读）。已剔除体育/世界杯/加密刷屏（折叠到参考组），
        各模块按 24h 成交取热门。仅作全球宏观情绪参考，<b>非投资建议</b>。涨跌按 A 股习惯红涨绿跌。
        <span className="ml-2 text-muted-foreground/50">移植自 globalpercent（Apache-2.0）</span>
      </p>
    </div>
  );
}

interface ModuleSectionProps {
  group: PulseModule;
  selected: PulseMarket | null;
  onSelect: (m: PulseMarket) => void;
}

function ModuleSection({ group, selected, onSelect }: ModuleSectionProps) {
  const pm = group.source_counts.polymarket ?? 0;
  const ks = group.source_counts.kalshi ?? 0;
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center gap-2.5">
        <span className={cn("h-2.5 w-2.5 rounded-full", moduleAccent(group.key))} />
        <h2 className="text-base font-semibold">{group.key}</h2>
        <span className="text-[11px] text-muted-foreground">{group.market_count} 个</span>
        <span className="text-[11px] text-muted-foreground/70">成交 {fmtVol(group.volume_24h)}</span>
        <span className="ml-auto flex items-center gap-1.5 text-[10px]">
          {pm > 0 && <span className="rounded bg-pm/15 px-1.5 py-0.5 text-pm">PM {pm}</span>}
          {ks > 0 && <span className="rounded bg-kalshi/15 px-1.5 py-0.5 text-kalshi">Kalshi {ks}</span>}
        </span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {group.markets.map((m) => (
          <MarketRow
            key={`${m.source}-${m.slug ?? m.question}`}
            market={m}
            active={selected?.slug === m.slug && selected?.source === m.source}
            onClick={() => onSelect(m)}
          />
        ))}
      </div>
    </section>
  );
}

interface MarketRowProps {
  market: PulseMarket;
  active: boolean;
  onClick: () => void;
}

function MarketRow({ market, active, onClick }: MarketRowProps) {
  const pct = market.prob_yes != null ? Math.round(market.prob_yes * 100) : null;
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-lg border p-2.5 text-left transition-colors",
        active ? "border-primary bg-primary/5" : "hover:border-primary/50",
      )}
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <SourceBadge source={market.source} />
        <span className="text-[10px] text-muted-foreground/70">{fmtDate(market.end_date)}</span>
      </div>
      <p className="line-clamp-2 text-[13px] font-medium leading-snug">{market.question}</p>
      {market.question_zh && (
        <p className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-muted-foreground">{market.question_zh}</p>
      )}
      {market.pick_label && (
        <p className="mt-1 inline-block rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-600 dark:text-amber-400">
          档位 · {market.pick_label}（市场预期线）
        </p>
      )}
      <div className="mt-1.5 flex items-center gap-2">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full bg-primary" style={{ width: `${pct ?? 0}%` }} />
        </div>
        <span className="w-11 text-right text-[13px] font-semibold tabular-nums">{fmtPct(market.prob_yes)}</span>
      </div>
      <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
        <ChangeBadge value={market.change_24h} suffix="24h" />
        <span className="ml-auto">成交 {fmtVol(market.volume_24h)}</span>
      </div>
    </button>
  );
}

function DetailPanel({ market, history, historyLoading }: {
  market: PulseMarket; history: TrendPoint[]; historyLoading: boolean;
}) {
  const url =
    market.source === "polymarket" && market.slug
      ? `https://polymarket.com/event/${market.slug}`
      : market.source === "kalshi" && market.series_ticker
        ? `https://kalshi.com/markets/${market.series_ticker}`
        : null;
  return (
    <GlassCard className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="mb-1 flex items-center gap-2">
            <SourceBadge source={market.source} />
            <span className="text-[11px] text-muted-foreground">{market.topic}</span>
          </div>
          <h2 className="text-sm font-semibold leading-snug">{market.question}</h2>
          {market.question_zh && (
            <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{market.question_zh}</p>
          )}
        </div>
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="shrink-0 text-muted-foreground transition-colors hover:text-primary" title="查看原始市场">
            <ExternalLink className="h-4 w-4" />
          </a>
        )}
      </div>
      <div className="flex items-baseline gap-2">
        <span className="text-3xl font-bold">{fmtPct(market.prob_yes)}</span>
        <span className="text-sm text-muted-foreground">
          {market.pick_label ? `P（${market.pick_label}）` : "Yes 概率"}
        </span>
        <ChangeBadge value={market.change_24h} suffix="24h" />
      </div>
      {market.pick_label && (
        <p className="text-xs text-muted-foreground/80">
          多档位事件：展示「市场预期线」（概率最接近 50% 的档位 = 市场隐含水平）。其余档位见原始市场。
        </p>
      )}
      {market.source === "polymarket" ? (
        historyLoading ? (
          <div className="h-[280px] animate-pulse rounded bg-muted/40" />
        ) : (
          <ProbabilityTrend points={history} />
        )
      ) : (
        <div className="rounded border bg-muted/20 p-3 text-xs text-muted-foreground">
          Kalshi 暂无趋势曲线（v1）。当前为快照概率，点右上角可在 Kalshi 查看历史。
        </div>
      )}
    </GlassCard>
  );
}

function SourceBadge({ source }: { source: PulseMarket["source"] }) {
  const isPm = source === "polymarket";
  return (
    <span className={cn(
      "rounded px-1.5 py-0.5 text-[10px] font-medium",
      isPm ? "bg-pm/15 text-pm" : "bg-kalshi/15 text-kalshi",
    )}>
      {isPm ? "Polymarket" : "Kalshi"}
    </span>
  );
}

function ChangeBadge({ value, suffix }: { value: number | null; suffix: string }) {
  if (value == null || value === 0) {
    return <span className="text-muted-foreground/60">— {suffix}</span>;
  }
  const up = value > 0;
  const cls = up ? "text-danger" : "text-success"; // A股习惯：红涨绿跌
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span className={cn("inline-flex items-center gap-0.5", cls)}>
      <Icon className="h-3 w-3" />
      {(value > 0 ? "+" : "")}{(value * 100).toFixed(1)}pt {suffix}
    </span>
  );
}

function moduleAccent(key: string): string {
  const map: Record<string, string> = {
    货币政策: "bg-amber-500",
    宏观经济: "bg-orange-500",
    地缘政治: "bg-red-500",
    政治选举: "bg-blue-500",
    股指大宗: "bg-emerald-500",
    AI科技: "bg-violet-500",
    加密: "bg-yellow-500",
    体育: "bg-slate-400",
    娱乐: "bg-pink-400",
    其他: "bg-gray-400",
  };
  return map[key] ?? "bg-gray-400";
}

function fmtPct(p: number | null): string {
  return p == null ? "—" : `${(p * 100).toFixed(1)}%`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  return iso.slice(0, 10);
}

function fmtVol(v: number | null): string {
  if (v == null) return "—";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}
