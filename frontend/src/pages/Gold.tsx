import { useEffect, useState } from "react";
import { RefreshCw, Coins, TrendingUp, TrendingDown, Minus, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Sparkline } from "@/components/ui/Sparkline";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { api, type GoldScoreData, type GoldIndicator, type HistPoint } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isHist = (v: unknown): v is HistPoint[] =>
  Array.isArray(v) && v.every((p) => p != null && isNum((p as HistPoint).v) && typeof (p as HistPoint).date === "string");

function isValid(d: GoldScoreData | null): d is GoldScoreData {
  if (!d || typeof d !== "object") return false;
  if (d.schema_version !== 3 || !Array.isArray(d.indicators)) return false;
  for (const i of d.indicators) {
    if (!i || typeof i.key !== "string" || !isHist(i.hist)) return false;
  }
  return true;
}

// 分数 → 信号色（分越高越利多黄金；用站点既有语义色，高=利多=红暖、低=利空=绿冷）
function signalTone(score: number | null) {
  if (score == null) return { text: "text-muted-foreground", bar: "bg-muted", label: "无数据" };
  if (score >= 80) return { text: "text-danger", bar: "bg-danger", label: "强利多" };
  if (score >= 65) return { text: "text-danger", bar: "bg-danger/80", label: "利多" };
  if (score >= 45) return { text: "text-warning", bar: "bg-warning", label: "中性" };
  if (score >= 20) return { text: "text-success", bar: "bg-success/80", label: "利空" };
  return { text: "text-success", bar: "bg-success", label: "强利空" };
}

const DIM_ORDER = ["机会成本与美元", "投资资金与仓位", "金融风险与避险", "结构性需求", "趋势确认"];
const DIM_WEIGHT: Record<string, string> = {
  "机会成本与美元": "40%", "投资资金与仓位": "25%", "金融风险与避险": "10%",
  "结构性需求": "15%", "趋势确认": "10%",
};

function IndicatorCard({ ind }: { ind: GoldIndicator }) {
  const tone = signalTone(ind.score);
  return (
    <GlassCard className="flex flex-col gap-2 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{ind.label}</div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            {ind.dimension} · 权重 {Math.round(ind.weight * 100)}%
            {ind.effective_weight != null && ind.effective_weight < ind.weight
              ? ` · 时效折减至 ${Math.round(ind.effective_weight * 100)}%` : ""}
          </div>
        </div>
        <div className={cn("shrink-0 text-right", tone.text)}>
          <div className="text-xl font-bold tabular-nums leading-none">
            {ind.score != null ? ind.score.toFixed(0) : "—"}
          </div>
          <div className="mt-0.5 text-[10px] opacity-80">{tone.label}</div>
        </div>
      </div>
      <div className="flex items-baseline gap-2">
        <span className="text-sm font-semibold tabular-nums">{ind.value_text ?? "—"}</span>
        {ind.chg != null && (
          <span className={cn("text-[11px] tabular-nums",
            ind.chg > 0 ? "text-danger" : ind.chg < 0 ? "text-success" : "text-muted-foreground")}>
            {ind.chg > 0 ? "+" : ""}{ind.chg}
          </span>
        )}
        <span className="ml-auto text-[10px] text-muted-foreground">{ind.date ?? ""}</span>
      </div>
      {ind.hist.length >= 2 && (
        <Sparkline data={ind.hist} height={40} color="--primary" showLatest />
      )}
      {ind.note && <div className="text-[10px] leading-relaxed text-muted-foreground/80">{ind.note}</div>}
    </GlassCard>
  );
}

export function Gold() {
  const [err, setErr] = useState<string | null>(null);
  const { data, loading, revalidating, revalidate } = useSWR<GoldScoreData>(
    "gold:v4",
    async (fresh) => {
      const d = await api.goldScore(fresh);
      if (!isValid(d)) throw new Error("数据格式异常");
      return d;
    }, [], (e) => setErr(e instanceof Error ? e.message : "加载失败"), { persist: true },
  );
  const load = () => { setErr(null); void revalidate(true); };
  useEffect(() => {
    const tick = () => { if (!document.hidden) void revalidate(); };
    const timer = window.setInterval(tick, 60 * 60_000);
    const onVisible = () => { if (!document.hidden) void revalidate(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [revalidate]);

  const total = data?.gold_score ?? null;
  const totalHist = isHist(data?.hist) ? data.hist : [];
  const unavailableSources = data?.source_status?.filter((s) => s.status !== "fresh") ?? [];
  const tone = signalTone(total);
  const r = 52;
  const circ = 2 * Math.PI * r;
  const pct = total != null ? Math.max(0, Math.min(100, total)) / 100 : 0;

  // 问 AI 上下文：把本页总分、信号、驱动、维度和指标明细打包（与持仓页同一套路）
  const aiContext = data
    ? [
        `黄金多维评分（${data.date}）：总分 ${total != null ? total.toFixed(0) : "—"}/100，信号「${data.signal ?? "—"}」，置信度 ${data.confidence}，模式 ${data.mode}，覆盖率 ${(data.coverage * 100).toFixed(0)}%`,
        data.top_positive_drivers.length ? `利多驱动：${data.top_positive_drivers.join("、")}` : "利多驱动：暂无明显",
        data.top_negative_drivers.length ? `利空驱动：${data.top_negative_drivers.join("、")}` : "利空驱动：暂无明显",
        `维度得分：${DIM_ORDER.map((name) => {
          const d = data.dimensions?.[name];
          return d ? `${name} ${d.score.toFixed(0)}` : `${name} 未接入`;
        }).join("；")}`,
        "指标明细：",
        ...data.indicators.map((i) =>
          `- ${i.label}（${i.dimension}，权重${Math.round(i.weight * 100)}%${i.effective_weight != null && i.effective_weight < i.weight ? `，时效折减至${Math.round(i.effective_weight * 100)}%` : ""}）：${i.value_text ?? "—"}${i.chg != null ? `，变动 ${i.chg > 0 ? "+" : ""}${i.chg}` : ""}，得分 ${i.score != null ? i.score.toFixed(0) : "—"}，日期 ${i.date ?? "—"}${i.note ? `；${i.note}` : ""}`),
        unavailableSources.length
          ? `数据状态：${unavailableSources.map((s) => `${s.label}${s.status === "stale" ? "（滞后/缓存）" : "（缺失）"}`).join("、")}`
          : "数据状态：全部 fresh",
      ].join("\n")
    : "";

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="黄金"
        subtitle="5维8指标 · ETF超预期流量 · OFR剔除安全资产"
        actions={
          <div className="flex items-center gap-2">
            {data && (
              <AskAiButton context={aiContext} taskId="gold" label="问 AI"
                suggestions={["分析当前黄金局势和未来趋势研判", "当前最大的利多和利空是什么", "接下来重点关注哪些指标变化"]} />
            )}
            <button
              onClick={load}
              disabled={loading || revalidating}
              className="flex items-center gap-1.5 rounded-lg border border-border/60 px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", (loading || revalidating) && "animate-spin")} />
              刷新
            </button>
          </div>
        }
      />

      {err && (
        <GlassCard className="mb-4 flex items-center gap-2 border-danger/30 text-sm text-danger">
          <AlertTriangle className="h-4 w-4 shrink-0" /> {err}
        </GlassCard>
      )}

      {data && (
        <>
          {unavailableSources.length > 0 && (
            <div className="mb-3 text-[11px] text-warning">
              数据源异常：{unavailableSources.map((s) => `${s.label}${s.status === "stale"
                ? `（${s.stale_reason === "observation_lag" ? `观测滞后${s.age_days ?? ""}天` : "缓存回退"}）`
                : "（缺失）"}`).join("、")}
              {data.stale_since ? `；最早缓存抓取于 ${data.stale_since}` : ""}
            </div>
          )}

          {/* 总分 + 信号 + 驱动 */}
          <div className="mb-5 grid gap-4 md:grid-cols-[auto_1fr]">
            <GlassCard glow className="p-5">
              <div className="flex items-center gap-5">
                <div className="relative h-32 w-32">
                  <svg viewBox="0 0 120 120" className="h-full w-full -rotate-90">
                    <circle cx="60" cy="60" r={r} fill="none" strokeWidth="10"
                      className="stroke-muted/30" />
                    <circle cx="60" cy="60" r={r} fill="none" strokeWidth="10"
                      strokeLinecap="round"
                      strokeDasharray={circ}
                      strokeDashoffset={circ * (1 - pct)}
                      className={cn("transition-all duration-700",
                        total != null && total >= 65 ? "stroke-danger"
                        : total != null && total >= 45 ? "stroke-warning" : "stroke-success")}
                    />
                  </svg>
                  <div className="absolute inset-0 flex flex-col items-center justify-center">
                    <span className={cn("text-3xl font-extrabold tabular-nums leading-none", tone.text)}>
                      {total != null ? total.toFixed(0) : "—"}
                    </span>
                    <span className="mt-1 text-[10px] text-muted-foreground">/ 100</span>
                  </div>
                </div>
                <div>
                  <div className={cn("text-2xl font-extrabold", tone.text)}>
                    {data.signal ?? "—"}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {data.date}
                  </div>
                  <div className="mt-2 max-w-52 text-[10px] leading-relaxed text-muted-foreground/70">
                    {data.mode}
                  </div>
                </div>
              </div>
              {totalHist.length > 1 && (
                <div className="mt-2 border-t border-border/30 pt-2">
                  <span className="text-[9px] text-muted-foreground/45">近1年得分</span>
                  <Sparkline data={totalHist} height={38} className="mt-0.5"
                    color="--primary" valueSuffix=" 分" />
                </div>
              )}
            </GlassCard>

            <GlassCard className="p-5">
              <div className="mb-3 text-xs font-medium text-muted-foreground">主要驱动</div>
              <div className="space-y-2.5">
                <div className="flex items-start gap-2">
                  <TrendingUp className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
                  <div className="text-sm">
                    {data.top_positive_drivers.length
                      ? data.top_positive_drivers.join("、")
                      : <span className="text-muted-foreground">暂无明显利多驱动</span>}
                  </div>
                </div>
                <div className="flex items-start gap-2">
                  <TrendingDown className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                  <div className="text-sm">
                    {data.top_negative_drivers.length
                      ? data.top_negative_drivers.join("、")
                      : <span className="text-muted-foreground">暂无明显利空驱动</span>}
                  </div>
                </div>
                <div className="flex items-start gap-2 border-t border-border/40 pt-2.5">
                  <Minus className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                  <div className="text-[11px] text-muted-foreground">
                    数据状态：{data.data_quality} · 更新于 {data.updated}
                  </div>
                </div>
              </div>
            </GlassCard>
          </div>

          {/* 维度得分 */}
          <GlassCard className="mb-5 p-5">
            <div className="mb-3 flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <Coins className="h-3.5 w-3.5" /> 维度得分
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {DIM_ORDER.map((name) => {
                const d = data.dimensions?.[name];
                const t = signalTone(d?.score ?? null);
                const hist = isHist(d?.hist) ? d.hist : [];
                return (
                  <div key={name} className="rounded-lg border border-border/40 p-3">
                    <div className="flex items-baseline justify-between">
                      <span className="text-xs text-muted-foreground">{name}</span>
                      <span className="text-[10px] text-muted-foreground/70">{DIM_WEIGHT[name]}</span>
                    </div>
                    {d ? (
                      <>
                        <div className={cn("mt-1 text-lg font-bold tabular-nums", t.text)}>
                          {d.score.toFixed(0)}
                          <span className="ml-1.5 text-[10px] font-normal opacity-75">{t.label}</span>
                        </div>
                        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted/30">
                          <div className={cn("h-full rounded-full transition-all", t.bar)}
                            style={{ width: `${Math.max(2, Math.min(100, d.score))}%` }} />
                        </div>
                        {hist.length > 1 && (
                          <div className="mt-2">
                            <span className="text-[9px] text-muted-foreground/45">近1年得分</span>
                            <Sparkline data={hist} height={34} className="mt-0.5"
                              color="--primary" valueSuffix=" 分" />
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="mt-1 text-sm text-muted-foreground/60">未接入</div>
                    )}
                  </div>
                );
              })}
            </div>
          </GlassCard>

          {/* 指标卡 */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {data.indicators.map((ind) => <IndicatorCard key={ind.key} ind={ind} />)}
          </div>

          <p className="mt-4 text-[11px] leading-relaxed text-muted-foreground/70">
            评分框架见 research/黄金价格多维评分系统技术方案_V2.0.md（实现版本 V2.1）。ETF得分使用剔除价格动量解释后的
            超预期流量，OFR压力剔除含黄金价格的安全资产类别；日频信号采用3日EMA。高频指标按过去5年历史分位评分，
            央行指标按2010年以来月度历史分位评分。本页为黄金环境判断，不构成交易指令。
          </p>
        </>
      )}

      {!data && !err && (
        <GlassCard className="flex items-center justify-center p-16 text-sm text-muted-foreground">
          <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> 首次计算中（需拉取 5 年历史数据，约 30 秒）…
        </GlassCard>
      )}
    </div>
  );
}
