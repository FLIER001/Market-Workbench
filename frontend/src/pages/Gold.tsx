import { useEffect, useMemo, useState } from "react";
import { RefreshCw, Coins, TrendingUp, TrendingDown, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { RefreshButton } from "@/components/ui/RefreshButton";
import { GlassCard } from "@/components/ui/GlassCard";
import { MinuteChart } from "@/components/ui/MinuteChart";
import { Sparkline } from "@/components/ui/Sparkline";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { PageSkeleton } from "@/components/ui/PageSkeleton";
import { api, type GoldScoreData, type GoldIndicator, type HistPoint, type PaxgSpotData, type MinuteKline, type GoldRiskForecast, type GoldDirectionForecast, type GoldInsight } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isHist = (v: unknown): v is HistPoint[] =>
  Array.isArray(v) && v.every((p) => p != null && isNum((p as HistPoint).v) && typeof (p as HistPoint).date === "string");

// 能力探测代替版本号相等（与后端 _load_gold_snapshot 同一原则）：
// payload 结构演进、schema_version 提升，不应让整页变成「数据格式异常」——
// 只要关键字段还在就照常渲染，缺失字段各自降级。hist 的逐点校验保留，
// 它是真正会影响作图的安全检查。
function isValid(d: GoldScoreData | null): d is GoldScoreData {
  if (!d || typeof d !== "object") return false;
  if (typeof d.schema_version !== "number" || !Array.isArray(d.indicators)) return false;
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

const SPOT_REFRESH_MS = 20_000;
const OZ_GRAMS = 31.1034768; // 1 金衡盎司 = 31.1035 克，PAXG 每枚锚定 1 盎司

function LiveGoldCard({ paxg }: { paxg: PaxgSpotData | null }) {
  // PAXG/USDT 暗盘分时已由后端一并返回；前端不再单独拉 AU0，直接用后端 minute。
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
    ?? minuteData?.prev_close;
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
          <div className="truncate text-sm font-medium">黄金暗盘</div>
          <div className="text-[10px] text-muted-foreground">
            {showCny ? `≈国内金价 · 元/克 · USDCNY ${fx!.toFixed(4)}` : "PAXG/USDT · USDT/枚"}
            <span className="text-muted-foreground/60"> · 7×24</span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-lg font-bold tabular-nums leading-none">
            {price != null ? price.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—"}
          </div>
          {showCny && paxg?.price != null && (
            <div className="mt-0.5 text-[10px] tabular-nums text-muted-foreground/70">
              PAXG {paxg.price.toFixed(2)} USDT
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
          由 PAXG/USDT × USDCNY ÷ 31.1035 折算，假设USDT≈USD，与沪金存在基差。
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
            {ind.score == null ? " · 暂无评分" : ""}
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
      {(ind.signal_hist ?? ind.hist).length >= 2 && (
        <Sparkline data={ind.signal_hist ?? ind.hist} height={40} color="--primary" showLatest />
      )}
      {ind.note && <div className="text-[10px] leading-relaxed text-muted-foreground/80">{ind.note}</div>}
    </GlassCard>
  );
}


function Forecasts({ sourceUpdated }: { sourceUpdated?: string }) {
  const [directionError,setDirectionError]=useState(false);
  const [riskError,setRiskError]=useState(false);
  const {data:direction}=useSWR<GoldDirectionForecast>("gold:direction:0.1",async()=>{
    const d=await api.goldDirection();setDirectionError(false);return d;
  },[sourceUpdated],()=>setDirectionError(true));
  const {data:risk}=useSWR<GoldRiskForecast>("gold:risk:1.0",async()=>{
    const d=await api.goldRisk();setRiskError(false);return d;
  },[sourceUpdated],()=>setRiskError(true));
  const dReady=direction?.status==="ready"&&!directionError;
  const rReady=risk?.status==="ready"&&!riskError;
  const pct=(n?:number|null)=>n==null?"—":`${(n*100).toFixed(1)}%`;
  return <div className="mt-5 grid gap-4 md:grid-cols-2">
    <GlassCard className="p-5">
      <div className="mb-3 text-xs font-medium text-muted-foreground">20日涨跌倾向</div>
      <div className="flex items-baseline gap-3"><strong className="text-2xl tabular-nums">{dReady?direction.score?.toFixed(1):"—"}</strong><span className="text-sm">{dReady?direction.direction:"暂停判断"}</span></div>
      <p className="mt-2 text-[11px] text-muted-foreground">≥52偏多 · ≤48偏空 · 其余中性；方向分非上涨概率。</p>
      {(!dReady||direction?.record_error)&&<p className="mt-2 text-[11px] text-warning">{directionError?"暂时读取失败":direction?.record_error??direction?.reason??"正在读取…"}</p>}
      <details className="mt-3 text-[11px] text-muted-foreground"><summary className="cursor-pointer">参数与历史检验</summary><div className="mt-2 space-y-2 leading-relaxed"><p>60日趋势90%＋5日反转10%，以20日波动标准化，保留25%信号。</p>{direction?.validation&&<p>{direction.validation.sample_count}个历史周时点：平衡准确率{pct(direction.validation.balanced_accuracy)}；普通命中率{pct(direction.validation.accuracy)}，一律偏多{pct(direction.validation.always_up_accuracy)}。历史调参结果，尚无独立前瞻验证。</p>}<p>价格截至 {direction?.observed_at??"—"} · 模型 {direction?.model_version??"—"}</p></div></details>
    </GlassCard>
    <GlassCard className="p-5">
      <div className="mb-3 text-xs font-medium text-muted-foreground">20日波动风险</div>
      <div className="flex items-baseline gap-3"><strong className="text-2xl tabular-nums">{pct(rReady?risk.annualized_volatility:null)}</strong><span className="text-xs text-muted-foreground">年化 · 20日尺度 {pct(rReady?risk.horizon_volatility:null)}</span></div>
      <p className="mt-2 text-[11px] text-muted-foreground">过去20日 {pct(rReady?risk.trailing20_volatility:null)} · 60日 {pct(rReady?risk.trailing60_volatility:null)}（年化）</p>
      {!rReady&&<p className="mt-2 text-[11px] text-warning">{riskError?"暂时读取失败":risk?.reason??"正在读取…"}</p>}
      <details className="mt-3 text-[11px] text-muted-foreground"><summary className="cursor-pointer">参数与历史检验</summary><div className="mt-2 space-y-2 leading-relaxed"><p>EWMA 0.94 / 0.97 / 0.99方差等权；预测振幅，不判断涨跌。20日尺度为换算值，不是价格覆盖区间。</p>{risk?.validation&&<p>历史MSE较过去20/60日波动下降 {pct(risk.validation.mse_improvement_vs20)} / {pct(risk.validation.mse_improvement_vs60)}，属于调参后的探索结果。</p>}<p>价格截至 {risk?.observed_at??"—"} · 模型 {risk?.model_version??"—"}</p></div></details>
    </GlassCard>
  </div>;
}

// 首屏骨架：仅在「内存 / 本地持久化 / 后端磁盘快照」三层全部落空（真·首次冷启动）
// 时出现。这类情形实测全量重建约 50 秒，此前这里只有一行文字、整页空白。
// 骨架与文案统一由 components/ui/PageSkeleton 提供。
export function Gold() {
  const [err, setErr] = useState<string | null>(null);
  const [paxg, setPaxg] = useState<PaxgSpotData | null>(null);


  const { data: spot, loading, revalidating, revalidate } = useSWR<GoldScoreData>(
    "gold:environment:v3",
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
    const tick = () => { if (!document.hidden) void revalidate(); };
    const timer = window.setInterval(tick, 60 * 60_000);
    const onVisible = () => { if (!document.hidden) void revalidate(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [revalidate]);

  // AI 解读（三段）：独立 SWR 持久缓存——切页面秒显，挂载后台核对一跳
  // （命中后端快照，不烧 LLM）；评分日期变化时跟进，让后端按需重生成一次。
  // v2：v1 的 persist 值在部分浏览器冻结在旧时点（写回链路未成功且无 t 戳可淘汰），
  // 换 key 一次性丢弃旧值；配合 useSWR 新格式 7 天淘汰。
  const { data: aiInsight, revalidating: insightLoading, revalidate: revalidateInsight } = useSWR<GoldInsight | null>(
    `gold-insight:v3:${spot?.state_id??"pending"}`, (fresh) => api.goldInsight(fresh).then((d) => d ?? null), [spot?.state_id], undefined, {persist:false},
  );

  // 手动重生成：只重调 LLM，不动评分数据
  const refreshInsight = () => { void revalidateInsight(true); };

  const complete = spot?.model_version === "gold-environment-3.0" && spot.score_status === "complete";
  const total = complete ? spot.gold_score : null;
  const totalHist = isHist(spot?.hist) ? spot.hist : [];
  const referenceAligned = useMemo(() => {
    const prices = spot?.benchmark?.usd ?? [];
    const out: HistPoint[] = [];
    let cursor = 0;
    for (const h of totalHist) {
      while (cursor < prices.length && prices[cursor].date <= h.date) cursor += 1;
      if (cursor > 0 && Date.parse(h.date)-Date.parse(prices[cursor-1].date) <= 7*86400000)
        out.push({date:h.date,v:prices[cursor-1].v});
    }
    return out;
  }, [spot?.benchmark?.usd, totalHist]);
  const unavailableSources = spot?.source_status?.filter((s) => s.active && s.usable === false) ?? [];
  const tone = signalTone(total);
  const r = 52;
  const circ = 2 * Math.PI * r;
  const pct = total != null ? Math.max(0, Math.min(100, total)) / 100 : 0;

  const aiContext = spot
    ? [
        `黄金多维评分（${spot.date}）：总分 ${total != null ? total.toFixed(0) : "—"}/100，信号「${complete ? spot.signal : "暂无评分"}」，置信度 ${spot.confidence}，模式 ${spot.mode}，覆盖率 ${spot.coverage.toFixed(0)}%`,
        paxg?.price
          ? `黄金暗盘（PAXG/USDT）：${paxg.cny?.price != null ? `≈国内金价 ${paxg.cny.price.toFixed(2)} 元/克（USDCNY ${(paxg.usdcny ?? 0).toFixed(4)} 折算）` : `${paxg.price.toFixed(2)} 美元/枚`} (${paxg.change_pct != null ? (paxg.change_pct > 0 ? "+" : "") + paxg.change_pct.toFixed(2) + "%" : "—"})`
          : "黄金暗盘：暂不可用",
        spot.top_positive_drivers.length ? `利多驱动：${spot.top_positive_drivers.join("、")}` : "利多驱动：暂无明显",
        spot.top_negative_drivers.length ? `利空驱动：${spot.top_negative_drivers.join("、")}` : "利空驱动：暂无明显",
        `维度得分：${DIM_ORDER.map((name) => {
          const d = spot.dimensions?.[name];
          return d ? `${name} ${d.score?.toFixed(0) ?? "—"}` : `${name} 未接入`;
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
        actions={
          <div className="flex items-center gap-2">
            {spot && (
              <AskAiButton context={aiContext} taskId="gold" label="AI 复盘"
                suggestions={["分析当前黄金局势和未来趋势研判", "当前最大的利多和利空是什么", "接下来重点关注哪些指标变化"]} />
            )}
            <RefreshButton onClick={load} loading={loading || revalidating} />
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
          {/* 有数据就立刻渲染，后台刷新状态只作为一行提示 —— 而不是让整页回到 loading。
              这是「秒开 + 静默追新」的关键：旧值可用时永远不阻塞首屏。 */}
          {spot.cache_state && spot.cache_state !== "fresh" && (
            <div className="mb-3 flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <RefreshCw className="h-3 w-3 animate-spin" />
              {spot.cache_state === "refreshing"
                ? `正在后台刷新（当前为 ${spot.cached_at ? spot.cached_at.slice(0, 16).replace("T", " ") : spot.updated} 的数据）…`
                : spot.cache_state === "error"
                  ? "后台刷新失败，当前展示最近一次成功结果"
                  : "当前为缓存结果，后台正在更新"}
            </div>
          )}

          {spot.degraded && (
            <div className="mb-3 text-[11px] text-warning">
              当前为旧结构快照兜底（字段可能不全），后台正在按新结构重建。
            </div>
          )}

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
                    {complete ? spot.signal : "暂无评分"}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {spot.date}
                  </div>
                  <div className="mt-2 max-w-52 text-[10px] leading-relaxed text-muted-foreground/70">
                    {total == null ? "当前输入不足，暂停评分" : "黄金环境分"}
                  </div>
                </div>
              </div>
              {totalHist.length > 1 && (
                <div className="mt-2 border-t border-border/30 pt-2">
                  <span className="text-[9px] text-muted-foreground/45">得分走势</span>
                  <Sparkline data={totalHist} height={38} className="mt-0.5"
                    color="--primary" valueSuffix=" 分" />
                  {referenceAligned.length > 1 && (
                    <div className="mt-1.5 border-t border-border/30 pt-1.5">
                      <span className="text-[9px] text-muted-foreground/45">
                        LBMA PM · USD/oz
                      </span>
                      <Sparkline data={referenceAligned} height={38} className="mt-0.5"
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
              {complete && aiInsight ? (
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
                      <span className="text-[10px] text-muted-foreground/70">{Math.round((d?.weight ?? 0) * 100)}%</span>
                    </div>
                    {d ? (
                      <>
                        <div className={cn("mt-1 text-lg font-bold tabular-nums", t.text)}>
                          {d.score?.toFixed(0) ?? "—"}
                          <span className="ml-1.5 text-[10px] font-normal opacity-75">{t.label}</span>
                        </div>
                        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted/30">
                          <div className={cn("h-full rounded-full transition-all", t.bar)}
                            style={{ width: `${Math.max(2, Math.min(100, d.score ?? 0))}%` }} />
                        </div>
                        {hist.length > 1 && (
                          <div className="mt-2">
                            <span className="text-[9px] text-muted-foreground/45">得分走势</span>
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
            固定五维八指标权重；ETF使用流量残差，OFR剔除安全资产。指标按滚动历史分位计分，央行采用2010年以来历史。
          </p>

          <Forecasts sourceUpdated={spot.updated} />
        </>
      )}

      {!spot && !err && (
        <PageSkeleton
          title="首次计算中"
          detail="正在拉取 5 年历史数据并计算历史分位，实测约 50 秒，仅首次需要。"
          hint="算完会写入本地快照，之后打开直接秒开，不会再等。"
          cards={6}
          cardHeight="h-36"
        />
      )}
    </div>
  );
}
