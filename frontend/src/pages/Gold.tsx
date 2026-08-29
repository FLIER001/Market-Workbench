import { useEffect, useMemo, useState } from "react";
import { RefreshCw, Coins, TrendingUp, TrendingDown, Minus, AlertTriangle, ChevronDown } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { MinuteChart } from "@/components/ui/MinuteChart";
import { Sparkline } from "@/components/ui/Sparkline";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { api, type GoldScoreData, type GoldIndicator, type HistPoint, type PaxgSpotData, type MinuteKline, type Au0HistData, type GoldInsight } from "@/lib/api";
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

const SPOT_REFRESH_MS = 20_000;
const OZ_GRAMS = 31.1034768; // 1 金衡盎司 = 31.1035 克，PAXG 每枚锚定 1 盎司

function LiveGoldCard({ paxg }: { paxg: PaxgSpotData | null }) {
  // PAXG-USD 暗盘分时已由后端一并返回；前端不再单独拉 AU0，直接用后端 minute。
  // 展示口径：近似折算国内金价（元/克）= PAXG USD × USDCNY ÷ 盎司克重；汇率缺失回退 USD。
  const fx = paxg?.usdcny ?? null;
  const toCny = (usd: number | null) => (usd != null && fx != null ? usd / OZ_GRAMS * fx : null);
  const minuteData: MinuteKline | null = paxg?.minute
    ? {
        date: paxg.minute.date,
        prev_close: toCny(paxg.minute.prev_close) ?? paxg.minute.prev_close,
        points: paxg.minute.points.map((p) => ({ ...p, price: toCny(p.price) ?? p.price })),
        market_minutes: [[0, 1440]],
      }
    : null;

  const lastPoint = minuteData?.points.length ? minuteData.points[minuteData.points.length - 1] : null;
  const price = paxg?.cny?.price ?? toCny(paxg?.price ?? null) ?? lastPoint?.price ?? null;
  const showCny = fx != null;
  const prevClose = paxg?.cny?.prev_close ?? toCny(paxg?.prev_close ?? null)
    ?? (minuteData && minuteData.prev_close ? toCny(minuteData.prev_close) : null);
  const change = paxg?.cny?.change ?? (prevClose != null && price != null ? price - prevClose : null);
  // CNY 显示时涨跌幅按 CNY 口径算，避免 CNY 差额配 USD 百分比的口径混杂
  const changePct = (prevClose != null && change != null && prevClose !== 0)
    ? (change / prevClose) * 100
    : paxg?.change_pct ?? null;
  const isUp = changePct != null && changePct > 0;
  const isDown = changePct != null && changePct < 0;
  const lastTime = minuteData && minuteData.points.length > 0 ? minuteData.points[minuteData.points.length - 1].time : null;

  return (
    <GlassCard className="flex h-full flex-col p-4">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">现货黄金暗盘</div>
          <div className="text-[10px] text-muted-foreground">
            {showCny ? `≈国内金价 · 元/克 · USDCNY ${fx!.toFixed(4)}` : "PAXG-USD · USD/枚"}
            <span className="text-muted-foreground/60"> · 7×24</span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-lg font-bold tabular-nums leading-none">
            {price != null ? price.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—"}
          </div>
          {showCny && paxg?.price != null && (
            <div className="mt-0.5 text-[10px] tabular-nums text-muted-foreground/70">
              PAXG {paxg.price.toFixed(2)} USD
            </div>
          )}
          <div className={cn("mt-0.5 flex items-center justify-end gap-1 text-[11px] font-semibold tabular-nums",
            isUp ? "text-danger" : isDown ? "text-success" : "text-muted-foreground")}>
            {isUp ? <TrendingUp className="h-3 w-3" /> : isDown ? <TrendingDown className="h-3 w-3" /> : null}
            <span>{change != null ? `${change > 0 ? "+" : ""}${change.toFixed(2)}` : "—"}</span>
            <span>{changePct != null ? `(${changePct > 0 ? "+" : ""}${changePct.toFixed(2)}%)` : ""}</span>
          </div>
        </div>
      </div>
      {lastTime && (
        <div className="mb-1 text-[10px] tabular-nums text-muted-foreground/60">
          {minuteData?.date} {lastTime}{paxg?.stale ? " · 缓存" : ""}
        </div>
      )}
      <div className="min-h-0 flex-1">
        {minuteData && minuteData.points.length >= 2 && paxg ? (
          <MinuteChart data={minuteData} height={180} compact />
        ) : (
          <div className="flex h-full min-h-[120px] items-center justify-center text-[11px] text-muted-foreground">
            分时数据暂不可用
          </div>
        )}
      </div>
      {showCny && (
        <div className="mt-1.5 text-[10px] leading-relaxed text-muted-foreground/50">
          由 PAXG-USD × USDCNY ÷ 31.1035 克/盎司 近似折算，与沪金实时价存在时差与升贴水差异。
        </div>
      )}
    </GlassCard>
  );
}

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

// 得分档 × 前瞻收益回测（2025-08 ~ 2026-08，评分时点约周频，伦敦金 PM 收盘）。
// 静态快照：样本仅 54 个时点，档位 n 很小，只作方向参考。
const SCORE_BAND_BACKTEST = [
  { band: "30-34", n: 4, w1: "-0.05% / 50%", w4: "-0.20% / 50%", w8: "-5.65% / 50%" },
  { band: "35-39", n: 3, w1: "-1.10% / 33%", w4: "+4.12% / 67%", w8: "-7.76% / 0%" },
  { band: "40-44", n: 8, w1: "+0.81% / 50%", w4: "-3.99% / 0%", w8: "-0.95% / 50%" },
  { band: "45-49", n: 11, w1: "+0.25% / 50%", w4: "+3.95% / 67%", w8: "+4.78% / 56%" },
  { band: "50-54", n: 9, w1: "+0.96% / 78%", w4: "+4.88% / 89%", w8: "+8.28% / 78%" },
  { band: "55-59", n: 8, w1: "+0.57% / 50%", w4: "+0.83% / 50%", w8: "+4.00% / 75%" },
  { band: "60-64", n: 7, w1: "+0.71% / 43%", w4: "-0.73% / 57%", w8: "+0.01% / 29%" },
  { band: "65-69", n: 4, w1: "+1.50% / 100%", w4: "+8.32% / 100%", w8: "+6.93% / 75%" },
  { band: "全样本", n: 54, w1: "+0.56% / 57%", w4: "+2.04% / 60%", w8: "+3.32% / 59%" },
];

function ScoreBandTable() {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-3 text-[11px] text-muted-foreground/70">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 transition-colors hover:text-foreground"
      >
        <ChevronDown className={cn("h-3 w-3 transition-transform", open && "rotate-180")} />
        得分档与金价前瞻收益（历史回测参考）
      </button>
      {open && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[420px] border-collapse text-[10px] tabular-nums">
            <thead>
              <tr className="border-b border-border/40 text-muted-foreground/70">
                <th className="py-1 pr-3 text-left font-normal">得分档</th>
                <th className="py-1 pr-3 text-right font-normal">n</th>
                <th className="py-1 pr-3 text-right font-normal">1周 均值/胜率</th>
                <th className="py-1 pr-3 text-right font-normal">4周 均值/胜率</th>
                <th className="py-1 text-right font-normal">8周 均值/胜率</th>
              </tr>
            </thead>
            <tbody>
              {SCORE_BAND_BACKTEST.map((r) => (
                <tr key={r.band} className={cn("border-b border-border/20",
                  r.band === "全样本" && "font-medium")}>
                  <td className="py-1 pr-3">{r.band}</td>
                  <td className="py-1 pr-3 text-right">{r.n}</td>
                  <td className={cn("py-1 pr-3 text-right", r.w1.startsWith("+") && "text-danger/80",
                    r.w1.startsWith("-") && "text-success/80")}>{r.w1}</td>
                  <td className={cn("py-1 pr-3 text-right", r.w4.startsWith("+") && "text-danger/80",
                    r.w4.startsWith("-") && "text-success/80")}>{r.w4}</td>
                  <td className={cn("py-1 text-right", r.w8.startsWith("+") && "text-danger/80",
                    r.w8.startsWith("-") && "text-success/80")}>{r.w8}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 leading-relaxed text-muted-foreground/60">
            样本为 2025-08 ~ 2026-08 共 54 个评分时点（约周频），收益按伦敦金 PM 收盘计算。
            部分档位样本极少（n&lt;5），波动大，仅作方向参考：低分档（&lt;45）中期偏弱、50+ 偏强，
            边界档（40-44、60-64）方向不稳定。历史统计不代表未来表现。
          </p>
        </div>
      )}
    </div>
  );
}

export function Gold() {
  const [err, setErr] = useState<string | null>(null);
  const [paxg, setPaxg] = useState<PaxgSpotData | null>(null);
  const [au0, setAu0] = useState<Au0HistData | null>(null);

  const { data: spot, loading, revalidating, revalidate } = useSWR<GoldScoreData>(
    "gold:v4",
    async (fresh) => {
      const d = await api.goldScore(fresh);
      if (!isValid(d)) throw new Error("数据格式异常");
      return d;
    }, [], (e) => setErr(e instanceof Error ? e.message : "加载失败"), { persist: true },
  );

  const load = () => { setErr(null); void revalidate(true); };

  // 实时金价轮询 + 沪金日K（低频，独立拉取）
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    const tick = async () => {
      // 页面在后台时暂停现货轮询（PAXG 近 24h 交易，无需后台空耗）
      if (!document.hidden) {
        try {
          const px = await api.paxgSpot();
          if (!cancelled) setPaxg(px);
        } catch { /* 静默保留旧值 */ }
      }
      if (!cancelled) timer = window.setTimeout(tick, SPOT_REFRESH_MS);
    };
    void tick();
    return () => { cancelled = true; if (timer !== null) window.clearTimeout(timer); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadAu0 = async () => {
      try {
        const h = await api.au0Hist(400);
        if (!cancelled && h.points.length) setAu0(h);
      } catch { /* 评分卡走势缺省即可 */ }
    };
    void loadAu0();
    const timer = window.setInterval(() => { if (!document.hidden) void loadAu0(); }, 60 * 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    const tick = () => { if (!document.hidden) void revalidate(); };
    const timer = window.setInterval(tick, 60 * 60_000);
    const onVisible = () => { if (!document.hidden) void revalidate(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [revalidate]);

  // AI 解读（三段）：独立 SWR 持久缓存——切页面秒显，挂载后台核对一跳
  // （命中后端快照，不烧 LLM）；评分日期变化时跟进，让后端按需重生成一次
  const { data: aiInsight, revalidating: insightLoading, revalidate: revalidateInsight } = useSWR<GoldInsight | null>(
    "gold-insight:v1", (fresh) => api.goldInsight(fresh).then((d) => d ?? null), [spot?.date], undefined, { persist: true },
  );

  // 手动重生成：只重调 LLM，不动评分数据
  const refreshInsight = () => { void revalidateInsight(true); };

  const total = spot?.gold_score ?? null;
  const totalHist = isHist(spot?.hist) ? spot.hist : [];
  // 国内金价（AU0）按评分趋势的同日期对齐：保证两张图 x 轴完全一致
  const au0Aligned = useMemo(() => {
    if (!au0 || totalHist.length < 2) return [];
    const byDate = new Map(au0.points.map((p) => [p.date, p.v]));
    const au0Dates = au0.points.map((p) => p.date);
    const out: HistPoint[] = [];
    let cursor = 0;
    for (const h of totalHist) {
      while (cursor < au0Dates.length && au0Dates[cursor] < h.date) cursor += 1;
      const d = au0Dates[cursor];
      const v = d === h.date ? byDate.get(d) : (d && d > h.date && cursor > 0 ? byDate.get(au0Dates[cursor - 1]) : byDate.get(d));
      if (v != null) out.push({ date: h.date, v });
    }
    return out;
  }, [au0, totalHist]);
  const unavailableSources = spot?.source_status?.filter((s) => s.status !== "fresh") ?? [];
  const tone = signalTone(total);
  const r = 52;
  const circ = 2 * Math.PI * r;
  const pct = total != null ? Math.max(0, Math.min(100, total)) / 100 : 0;

  const aiContext = spot
    ? [
        `黄金多维评分（${spot.date}）：总分 ${total != null ? total.toFixed(0) : "—"}/100，信号「${spot.signal ?? "—"}」，置信度 ${spot.confidence}，模式 ${spot.mode}，覆盖率 ${(spot.coverage * 100).toFixed(0)}%`,
        paxg?.price
          ? `现货黄金暗盘（PAXG-USD）：${paxg.cny?.price != null ? `≈国内金价 ${paxg.cny.price.toFixed(2)} 元/克（USDCNY ${(paxg.usdcny ?? 0).toFixed(4)} 折算）` : `${paxg.price.toFixed(2)} 美元/枚`} (${paxg.change_pct != null ? (paxg.change_pct > 0 ? "+" : "") + paxg.change_pct.toFixed(2) + "%" : "—"})`
          : "现货黄金暗盘：暂不可用",
        spot.top_positive_drivers.length ? `利多驱动：${spot.top_positive_drivers.join("、")}` : "利多驱动：暂无明显",
        spot.top_negative_drivers.length ? `利空驱动：${spot.top_negative_drivers.join("、")}` : "利空驱动：暂无明显",
        `维度得分：${DIM_ORDER.map((name) => {
          const d = spot.dimensions?.[name];
          return d ? `${name} ${d.score.toFixed(0)}` : `${name} 未接入`;
        }).join("；")}`,
        "指标明细：",
        ...spot.indicators.map((i) =>
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
        subtitle="5 维 8 指标 · ETF 超预期流量 · OFR 剔除安全资产"
        actions={
          <div className="flex items-center gap-2">
            {spot && (
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

      {spot && (
        <>
          {unavailableSources.length > 0 && (
            <div className="mb-3 text-[11px] text-warning">
              数据源异常：{unavailableSources.map((s) => `${s.label}${s.status === "stale"
                ? `（${s.stale_reason === "observation_lag" ? `观测滞后${s.age_days ?? ""}天` : "缓存回退"}）`
                : "（缺失）"}`).join("、")}
              {spot.stale_since ? `；最早缓存抓取于 ${spot.stale_since}` : ""}
            </div>
          )}

          <div className="mb-5 grid items-stretch gap-4 lg:grid-cols-[minmax(220px,1fr)_auto_minmax(220px,1fr)]">
            <LiveGoldCard paxg={paxg} />
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
                    {spot.signal ?? "—"}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {spot.date}
                  </div>
                  <div className="mt-2 max-w-52 text-[10px] leading-relaxed text-muted-foreground/70">
                    {total == null ? "得分缺失" : total >= 65 ? "建议买入"
                      : total >= 45 ? "建议持有或逢低买入"
                      : total >= 20 ? "建议观望或减仓"
                      : "建议离场观望"}
                  </div>
                </div>
              </div>
              {totalHist.length > 1 && (
                <div className="mt-2 border-t border-border/30 pt-2">
                  <span className="text-[9px] text-muted-foreground/45">近 1 年得分</span>
                  <Sparkline data={totalHist} height={38} className="mt-0.5"
                    color="--primary" valueSuffix=" 分" />
                  {au0Aligned.length > 1 && (
                    <div className="mt-1.5 border-t border-border/30 pt-1.5">
                      <span className="text-[9px] text-muted-foreground/45">
                        沪金主力 AU0 · CNY/克{au0?.stale ? " · 缓存" : ""}
                      </span>
                      <Sparkline data={au0Aligned} height={38} className="mt-0.5"
                        color="--warning" showLatest />
                    </div>
                  )}
                </div>
              )}
            </GlassCard>

            <GlassCard className="p-5">
              <div className="mb-3 flex items-center gap-2">
                <span className="text-xs font-medium text-muted-foreground">AI 解读</span>
                <button
                  onClick={refreshInsight}
                  disabled={insightLoading}
                  className="ml-auto rounded p-1 text-muted-foreground transition-colors hover:text-primary disabled:opacity-60"
                  title="重新生成 AI 解读">
                  <RefreshCw className={cn("h-3 w-3", insightLoading && "animate-spin")} />
                </button>
              </div>
              {aiInsight ? (
                <div className="space-y-1.5 text-sm leading-relaxed text-foreground/85">
                  <p><span className="font-medium text-primary/90">机会成本与美元</span>　{aiInsight.opportunity_cost}</p>
                  <p><span className="font-medium text-primary/90">资金与仓位</span>　{aiInsight.flows_positioning}</p>
                  <p><span className="font-medium text-primary/90">综合形势</span>　{aiInsight.overall}</p>
                </div>
              ) : (
                <div className="space-y-2.5">
                  {insightLoading
                    ? <p className="text-sm text-muted-foreground">AI 正在生成解读…</p>
                    : (
                      <>
                        <div className="flex items-start gap-2">
                          <TrendingUp className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
                          <div className="text-sm">
                            {spot.top_positive_drivers.length
                              ? spot.top_positive_drivers.join("、")
                              : <span className="text-muted-foreground">暂无明显利多驱动</span>}
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <TrendingDown className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                          <div className="text-sm">
                            {spot.top_negative_drivers.length
                              ? spot.top_negative_drivers.join("、")
                              : <span className="text-muted-foreground">暂无明显利空驱动</span>}
                          </div>
                        </div>
                      </>
                    )}
                </div>
              )}
              <div className="mt-2.5 flex items-start gap-2 border-t border-border/40 pt-2.5">
                <Minus className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <div className="text-[11px] text-muted-foreground">
                  数据状态：{spot.data_quality} · 更新于 {spot.updated}
                </div>
              </div>
            </GlassCard>
          </div>

          <GlassCard className="mb-5 p-5">
            <div className="mb-3 flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <Coins className="h-3.5 w-3.5" /> 维度得分
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {DIM_ORDER.map((name) => {
                const d = spot.dimensions?.[name];
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
                            <span className="text-[9px] text-muted-foreground/45">近 1 年得分</span>
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

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {spot.indicators.map((ind) => <IndicatorCard key={ind.key} ind={ind} />)}
          </div>

          <p className="mt-4 text-[11px] leading-relaxed text-muted-foreground/70">
            评分框架见 research/黄金价格多维评分系统技术方案_V2.0.md（实现版本 V2.1）。ETF 得分使用剔除价格动量解释后的
            超预期流量，OFR 压力剔除含黄金价格的安全资产类别；日频信号采用 3 日 EMA。高频指标按过去 5 年历史分位评分，
            央行指标按 2010 年以来月度历史分位评分。本页为黄金环境判断，不构成交易指令。
          </p>

          <ScoreBandTable />
        </>
      )}

      {!spot && !err && (
        <GlassCard className="flex items-center justify-center p-16 text-sm text-muted-foreground">
          <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> 首次计算中（需拉取 5 年历史数据，约 30 秒）…
        </GlassCard>
      )}
    </div>
  );
}
