import { useEffect, useMemo, useState } from "react";
import { RefreshCw, Flame, TrendingUp, TrendingDown, Minus, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { MinuteChart } from "@/components/ui/MinuteChart";
import { Sparkline } from "@/components/ui/Sparkline";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { PageSkeleton } from "@/components/ui/PageSkeleton";
import { api, type OilScoreData, type OilIndicator, type OilSpotData, type OilSpotQuote, type BrentHistData, type HistPoint, type OilInsight, type OilHyperSpotData, type MinuteKline, type FuturesHistData } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isHist = (v: unknown): v is HistPoint[] =>
  Array.isArray(v) && v.every((p) => p != null && isNum((p as HistPoint).v) && typeof (p as HistPoint).date === "string");

function isValid(d: OilScoreData | null): d is OilScoreData {
  if (!d || typeof d !== "object") return false;
  if (d.schema_version !== 1 || !Array.isArray(d.indicators)) return false;
  for (const i of d.indicators) {
    if (!i || typeof i.key !== "string" || !isHist(i.hist)) return false;
  }
  return true;
}

// 油价与 A 股/黄金同用红涨绿跌：高分（利多）=红
function signalTone(score: number | null) {
  if (score == null) return { text: "text-muted-foreground", bar: "bg-muted", label: "无数据" };
  if (score >= 80) return { text: "text-danger", bar: "bg-danger", label: "强利多" };
  if (score >= 65) return { text: "text-danger", bar: "bg-danger/80", label: "利多" };
  if (score >= 45) return { text: "text-warning", bar: "bg-warning", label: "中性" };
  if (score >= 20) return { text: "text-success", bar: "bg-success/80", label: "利空" };
  return { text: "text-success", bar: "bg-success", label: "强利空" };
}

const SPOT_REFRESH_MS = 20_000;

function IndicatorCard({ ind }: { ind: OilIndicator }) {
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
        <span className="ml-auto text-[10px] text-muted-foreground">{ind.date ?? ""}</span>
      </div>
      {ind.hist.length >= 2 && (
        <Sparkline data={ind.hist} height={40} color="--primary" showLatest />
      )}
      {ind.note && <div className="text-[10px] leading-relaxed text-muted-foreground/80">{ind.note}</div>}
    </GlassCard>
  );
}

function StructureSparkline({ title, data, color, unit, note }: {
  title: string; data: HistPoint[]; color: string; unit: string; note?: string;
}) {
  const latest = data.length ? data[data.length - 1] : null;
  return (
    <div className="rounded-lg border border-border/40 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-muted-foreground">{title}</span>
        {latest && (
          <span className="text-sm font-bold tabular-nums">
            {latest.v.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}
            <span className="ml-1 text-[10px] font-normal text-muted-foreground">{unit}</span>
          </span>
        )}
      </div>
      {data.length > 1 ? (
        <div className="mt-1.5">
          <Sparkline data={data} height={44} color={color} showLatest />
        </div>
      ) : (
        <div className="mt-2 text-[11px] text-muted-foreground/60">数据暂不可用</div>
      )}
      {note && <div className="mt-1 text-[10px] leading-relaxed text-muted-foreground/60">{note}</div>}
    </div>
  );
}

// 实时行情可切换的四个标的：默认 WTI 暗盘，另可切到三个外盘品种。
// 只留 label / unit —— 数据源与口径说明用户明确要求不在卡内出现（页面底部有统一说明）。
type OilTabKey = "hyper" | "brent" | "ny" | "gas";

const OIL_TABS: { key: OilTabKey; label: string; unit: string }[] = [
  { key: "hyper", label: "WTI 暗盘", unit: "USD/桶" },
  { key: "brent", label: "布伦特原油", unit: "USD/桶" },
  { key: "ny", label: "纽约原油", unit: "USD/桶" },
  { key: "gas", label: "美国天然气", unit: "USD/MMBtu" },
];

// 外盘品种聚焦视图：腾讯外盘连续合约只有单点实时报价（没有分时序列），
// 所以配一条新浪全球期货的日K折线（近 250 个交易日），把「当前价处在中期走势的什么位置」补上。
// 高度刻意压扁：本卡是顶部三列里最左的 1/3 栏，要跟右侧评分卡 / AI 解读卡保持齐平。
function SpotTabView({ label, q, hist }: {
  label: string;
  q: OilSpotQuote | null;
  hist: HistPoint[];
}) {
  const up = q?.change_pct != null && q.change_pct > 0;
  const down = q?.change_pct != null && q.change_pct < 0;
  const tone = up ? "text-danger" : down ? "text-success" : "text-muted-foreground";
  const price = q?.price ?? null;
  const low = q?.low ?? null;
  const high = q?.high ?? null;
  const prev = q?.prev_close ?? null;
  const pos = low != null && high != null && price != null && high > low
    ? Math.max(0, Math.min(1, (price - low) / (high - low)))
    : null;
  const amplitude = high != null && low != null && prev != null && prev > 0
    ? ((high - low) / prev) * 100
    : null;

  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className={cn("text-3xl font-bold tabular-nums leading-none", tone)}>
          {price != null ? price.toFixed(2) : "—"}
        </span>
        <span className={cn("flex items-center gap-0.5 text-sm font-semibold tabular-nums", tone)}>
          {up ? <TrendingUp className="h-3.5 w-3.5" /> : down ? <TrendingDown className="h-3.5 w-3.5" /> : null}
          {q?.change_pct != null ? `${q.change_pct > 0 ? "+" : ""}${q.change_pct.toFixed(2)}%` : "—"}
        </span>
      </div>

      <div className="mt-2.5">
        {hist.length > 1 ? (
          <Sparkline data={hist} height={113} color="--primary" showLatest />
        ) : (
          <div className="flex h-[113px] items-center justify-center text-[10px] text-muted-foreground/50">
            走势暂不可用
          </div>
        )}
      </div>

      {pos != null && (
        <div className="mt-2.5">
          <div className="relative h-1 rounded-full bg-muted/40">
            <div
              className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary"
              style={{ left: `${pos * 100}%` }}
            />
          </div>
          <div className="mt-1 flex items-baseline justify-between text-[9px] text-muted-foreground/70">
            <span>低 <b className="font-mono font-medium text-success">{low!.toFixed(2)}</b></span>
            <span className="text-muted-foreground/45">当日区间位置</span>
            <span>高 <b className="font-mono font-medium text-danger">{high!.toFixed(2)}</b></span>
          </div>
        </div>
      )}

      <div className="mt-2.5 grid grid-cols-4 gap-1 border-t border-border/30 pt-2 text-[10px] text-muted-foreground">
        <span>昨收 <b className="font-mono font-medium text-foreground">
          {prev != null ? prev.toFixed(2) : "—"}</b></span>
        <span>最高 <b className="font-mono font-medium text-danger">
          {high != null ? high.toFixed(2) : "—"}</b></span>
        <span>最低 <b className="font-mono font-medium text-success">
          {low != null ? low.toFixed(2) : "—"}</b></span>
        <span>振幅 <b className="font-mono font-medium text-foreground">
          {amplitude != null ? `${amplitude.toFixed(2)}%` : "—"}</b></span>
      </div>

      {/* 数据源说明放在视图内部最后一行：这样暗盘 tab 不会为它空出一整行，
          省下的高度全给走势图（原来它单独占一行，两卡高度对齐时要额外占位）。 */}
      <div className="mt-1.5 text-[10px] leading-relaxed text-muted-foreground/50">
        外盘＝腾讯报价＋新浪 250 日线；周末/结算窗口看暗盘补齐。
      </div>
    </div>
  );
}

// 实时行情面板（tab 切换）：默认 WTI 暗盘（Hyperliquid 7×24 分时），
// 也可切到三个外盘品种（腾讯实时报价 + 新浪日K折线）。
// 整卡放在油价页顶部三列网格的最左栏（1/3 宽），右侧依次是评分卡与 AI 解读。
// 两者口径互补——外盘看实时报价，暗盘补休市时段（周末 / 假期 / 每日结算窗口）的那一段。
function LiveOilPanel({ hyper, spot, futures }: {
  hyper: OilHyperSpotData | null;
  spot: OilSpotData | null;
  futures: FuturesHistData | null;
}) {
  const [tab, setTab] = useState<OilTabKey>("hyper");

  const chart: MinuteKline | null = useMemo(() => {
    if (!hyper?.minute || hyper.minute.points.length < 2) return null;
    return {
      date: hyper.minute.date,
      prev_close: hyper.minute.prev_close,
      points: hyper.minute.points.map((p) => ({ ...p, price: p.price ?? 0 })),
      // 7×24 连续市场：x 轴固定铺满当日 00:00–24:00
      market_minutes: [[0, 1440]],
    };
  }, [hyper]);

  const short = (v: number | null | undefined, unit: string) => {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e8) return `${(v / 1e8).toFixed(2)}亿${unit}`;
    if (a >= 1e4) return `${(v / 1e4).toFixed(1)}万${unit}`;
    return `${v.toFixed(0)}${unit}`;
  };

  // tab → 外盘取数键（后端 series 用 brent/wti/ng，tab 用更直白的 ny/gas）
  const extKey = tab === "hyper" ? null : tab;
  const seriesKey = extKey === "brent" ? "brent" : extKey === "ny" ? "wti" : extKey === "gas" ? "ng" : null;
  const extQuote = extKey === "brent" ? spot?.brent : extKey === "ny" ? spot?.wti : extKey === "gas" ? spot?.ng : null;
  const extHist = seriesKey ? futures?.series?.[seriesKey]?.points ?? [] : [];
  const stale = extKey ? Boolean(seriesKey && futures?.series?.[seriesKey]?.stale) : Boolean(hyper?.stale);
  const active = OIL_TABS.find((t) => t.key === tab)!;
  const isHyper = extKey === null;
  // 报价时间戳并到「口径行」右侧，省掉视图区里单独一行（卡要压矮）
  const stampLabel = isHyper
    ? (hyper ? `${hyper.date} ${hyper.time}` : "")
    : (extQuote ? `${extQuote.date} ${extQuote.time}` : "");

  return (
    <GlassCard className="p-3.5">
      {/* 标题行右侧挂「单位 + 报价时间」：原先它们是 tabs 下面独立一行的口径说明，
          用户要求删掉那一行——时间挪上来，省下的高度全部还给分时图。 */}
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
          <Flame className="h-3.5 w-3.5" /> 实时行情
        </span>
        <span className="ml-auto flex shrink-0 items-baseline gap-1.5 text-[10px] text-muted-foreground/60">
          <span className="text-muted-foreground/45">{active.unit}{stale ? " · 缓存" : ""}</span>
          <span className="tabular-nums text-muted-foreground/45">{stampLabel}</span>
        </span>
      </div>

      {/* 分段控件（项目无 Tabs 组件，用朴素按钮实现）。aria-pressed 供无头浏览器断言定位。
          宽度铺满整行、四个标签等分（`flex-1`），不再是一个窄窄的 inline-flex 胶囊。 */}
      <div className="mt-2.5 flex w-full gap-0.5 rounded-lg border border-border/50 bg-muted/20 p-0.5">
        {OIL_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            aria-pressed={tab === t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              "flex-1 rounded-md px-1 py-0.5 text-center text-[11px] transition-colors",
              tab === t.key
                ? "bg-background font-medium text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* 视图区固定最小高度：四个 tab 内容长短不一，切换时卡片高度不跳。
          下限 243 是两边实测后取齐的结果——暗盘 tab 192 图 + 8 外边距 + 43 统计行 = 243，
          外盘 tab「报价 + 113 折线 + 区间条 + 四列统计 + 数据源行」= 243，两边正好一样高。 */}
      <div className="mt-2 min-h-[243px]">
        {isHyper ? (
          <>
            {chart ? (
              <MinuteChart data={chart} height={192} minHeight={192} />
            ) : (
              <div className="flex h-40 items-center justify-center text-[11px] text-muted-foreground">
                暗盘分时暂不可用
              </div>
            )}

            {/* 1/3 窄栏里 MinuteChart 标题行放不下这几个数（按容器宽自动隐藏），挪到此处补回。
                刻意只留 6 组、收成两行——多一行就把卡片顶高，跟右侧两卡不齐了。 */}
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/30 pt-2 text-[10px] text-muted-foreground/70">
              <span>今开 <b className="font-mono font-medium text-foreground">{hyper?.open != null ? hyper.open.toFixed(2) : "—"}</b></span>
              <span>最高 <b className="font-mono font-medium text-danger">{hyper?.high != null ? hyper.high.toFixed(2) : "—"}</b></span>
              <span>最低 <b className="font-mono font-medium text-success">{hyper?.low != null ? hyper.low.toFixed(2) : "—"}</b></span>
              <span>总量 <b className="font-mono font-medium text-foreground">{hyper?.volume != null ? short(hyper.volume, "桶") : "—"}</b></span>
              <span>持仓 <b className="font-mono font-medium text-foreground">{short(hyper?.open_interest, "桶")}</b></span>
              <span>
                费率(1h){" "}
                <b className={cn("font-mono font-medium",
                  (hyper?.funding ?? 0) > 0 ? "text-danger" : (hyper?.funding ?? 0) < 0 ? "text-success" : "text-foreground")}>
                  {hyper?.funding != null ? `${(hyper.funding * 100).toFixed(4)}%` : "—"}
                </b>
                <span className="text-muted-foreground/60">
                  {" "}年化 {hyper?.funding_annual != null ? `${hyper.funding_annual.toFixed(1)}%` : "—"}
                </span>
              </span>
            </div>
          </>
        ) : (
          <SpotTabView label={active.label} q={extQuote ?? null} hist={extHist} />
        )}
      </div>
    </GlassCard>
  );
}

export function Oil() {
  const [err, setErr] = useState<string | null>(null);
  const [spot, setSpot] = useState<OilSpotData | null>(null);
  const [brent, setBrent] = useState<BrentHistData | null>(null);
  const [wtiHyper, setWtiHyper] = useState<OilHyperSpotData | null>(null);
  const [futures, setFutures] = useState<FuturesHistData | null>(null);

  const { data: score, loading, revalidating, revalidate } = useSWR<OilScoreData>(
    "oil:v1",
    async (fresh) => {
      const d = await api.oilScore(fresh);
      if (!isValid(d)) throw new Error("数据格式异常");
      return d;
    }, [], (e) => setErr(e instanceof Error ? e.message : "加载失败"), { persist: true },
  );

  const load = () => { setErr(null); void revalidate(true); };

  // 实时行情 + WTI 暗盘轮询（20 秒档，与后端缓存对齐；后台标签页暂停）
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    const tick = async () => {
      if (!document.hidden) {
        const [px, hy] = await Promise.allSettled([api.oilSpot(), api.oilWtiHyper()]);
        if (!cancelled) {
          if (px.status === "fulfilled") setSpot(px.value);
          if (hy.status === "fulfilled") setWtiHyper(hy.value);
        }
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

  // 布伦特日K（低频，独立拉取）
  useEffect(() => {
    let cancelled = false;
    const loadBrent = async () => {
      try {
        const h = await api.brentHist(400);
        if (!cancelled && h.points.length) setBrent(h);
      } catch { /* 评分卡走势缺省即可 */ }
    };
    void loadBrent();
    const timer = window.setInterval(() => { if (!document.hidden) void loadBrent(); }, 60 * 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  // 三个外盘品种的日K迷你走势（低频，1 小时刷新即可——日线盘中只有末根在动）
  useEffect(() => {
    let cancelled = false;
    const loadFutures = async () => {
      try {
        const h = await api.futuresHist(250);
        if (!cancelled) setFutures(h);
      } catch { /* 迷你走势缺省即可，不阻塞卡片其余部分 */ }
    };
    void loadFutures();
    const timer = window.setInterval(() => { if (!document.hidden) void loadFutures(); }, 60 * 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  // AI 解读（三段）：独立 SWR 持久缓存——切页面秒显，挂载后台核对一跳
  // （命中后端快照，不烧 LLM）；评分日期变化时跟进，让后端按需重生成一次。
  // v2：v1 的 persist 值在部分浏览器冻结在旧时点（写回链路未成功且无 t 戳可淘汰），
  // 换 key 一次性丢弃旧值；配合 useSWR 新格式 7 天淘汰。
  const { data: aiInsight, revalidating: insightLoading, revalidate: revalidateInsight } = useSWR<OilInsight | null>(
    "oil-insight:v2", (fresh) => api.oilInsight(fresh).then((d) => d ?? null), [score?.date], undefined, { persist: true },
  );

  // 手动重生成：只重调 LLM，不动评分数据
  const refreshInsight = () => { void revalidateInsight(true); };

  const total = score?.oil_score ?? null;
  const totalHist = isHist(score?.hist) ? score.hist : [];
  // 布伦特按评分趋势的同日期对齐：保证得分与油价两张图 x 轴完全一致（与黄金 AU0 同模式）。
  // 评分趋势含非交易日（前向填充），且最新评分日可能晚于布伦特最新交易日：
  // 超出末端的日期沿用最后一根布伦特收盘，避免曲线尾部落空。
  const brentAligned = useMemo(() => {
    if (!brent || totalHist.length < 2) return [];
    const byDate = new Map(brent.points.map((p) => [p.date, p.v]));
    const brentDates = brent.points.map((p) => p.date);
    const out: HistPoint[] = [];
    let cursor = 0;
    for (const h of totalHist) {
      while (cursor < brentDates.length && brentDates[cursor] < h.date) cursor += 1;
      let v: number | undefined;
      if (cursor < brentDates.length && brentDates[cursor] === h.date) {
        v = byDate.get(h.date);
      } else if (cursor > 0) {
        // h.date 介于两根 K 线之间（或超出末端）：用最近的前一根
        v = byDate.get(brentDates[cursor - 1]);
      }
      if (v != null) out.push({ date: h.date, v });
    }
    return out;
  }, [brent, totalHist]);
  const unavailableSources = score?.source_status?.filter((s) => s.status !== "fresh") ?? [];
  const tone = signalTone(total);
  const r = 52;
  const circ = 2 * Math.PI * r;
  const pct = total != null ? Math.max(0, Math.min(100, total)) / 100 : 0;
  const structure = score?.structure;
  const dimOrder = score?.dimension_order?.map((d) => d.name)
    ?? Object.keys(score?.dimensions ?? {});

  const aiContext = score
    ? [
        `油价多维评分（${score.date}）：总分 ${total != null ? total.toFixed(0) : "—"}/100，信号「${score.signal ?? "—"}」，置信度 ${score.confidence}，模式 ${score.mode}，覆盖率 ${(score.coverage * 100).toFixed(0)}%`,
        spot?.brent?.price
          ? `Brent ${spot.brent.price.toFixed(2)} USD（${spot.brent.change_pct != null ? (spot.brent.change_pct > 0 ? "+" : "") + spot.brent.change_pct.toFixed(2) + "%" : "—"}）、WTI ${spot?.wti?.price?.toFixed(2) ?? "—"} USD、天然气 ${spot?.ng?.price?.toFixed(3) ?? "—"} USD`
          : "实时行情：暂不可用",
        wtiHyper?.price
          ? `WTI 暗盘（Hyperliquid 永续，7×24 含周末）：${wtiHyper.price.toFixed(2)} USD/桶（${wtiHyper.change_pct != null ? (wtiHyper.change_pct > 0 ? "+" : "") + wtiHyper.change_pct.toFixed(2) + "%" : "—"}），持仓 ${wtiHyper.open_interest != null ? (wtiHyper.open_interest / 1e4).toFixed(1) + " 万桶" : "—"}，资金费率年化 ${wtiHyper.funding_annual != null ? wtiHyper.funding_annual.toFixed(1) + "%" : "—"}`
          : "WTI 暗盘：暂不可用",
        score.top_positive_drivers.length ? `利多驱动：${score.top_positive_drivers.join("、")}` : "利多驱动：暂无明显",
        score.top_negative_drivers.length ? `利空驱动：${score.top_negative_drivers.join("、")}` : "利空驱动：暂无明显",
        `维度得分：${dimOrder.map((name) => {
          const d = score.dimensions?.[name];
          return d ? `${name} ${d.score.toFixed(0)}` : `${name} 未接入`;
        }).join("；")}`,
        "指标明细：",
        ...score.indicators.map((i) =>
          `- ${i.label}（${i.dimension}，权重${Math.round(i.weight * 100)}%${i.effective_weight != null && i.effective_weight < i.weight ? `，时效折减至${Math.round(i.effective_weight * 100)}%` : ""}）：${i.value_text ?? "—"}，得分 ${i.score != null ? i.score.toFixed(0) : "—"}，日期 ${i.date ?? "—"}${i.note ? `；${i.note}` : ""}`),
        structure ? `结构层：Brent-WTI ${structure.brent_wti.length ? structure.brent_wti[structure.brent_wti.length - 1].v.toFixed(2) : "—"} USD；SC/Brent 比率 ${structure.sc_brent_ratio.length ? structure.sc_brent_ratio[structure.sc_brent_ratio.length - 1].v.toFixed(3) : "—"}；SPR ${structure.spr.length ? (structure.spr[structure.spr.length - 1].v / 1000).toFixed(0) : "—"} 百万桶` : "",
        unavailableSources.length
          ? `数据状态：${unavailableSources.map((s) => `${s.label}${s.status === "stale" ? "（滞后/缓存）" : "（缺失）"}`).join("、")}`
          : "数据状态：全部 fresh",
      ].filter(Boolean).join("\n")
    : "";

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="油价"
        subtitle="边际物理稀缺 · 供给弹性 · 炼化需求 · 风险溢价（框架 V1.0）"
        actions={
          <div className="flex items-center gap-2">
            {score && (
              <AskAiButton context={aiContext} taskId="oil" label="问 AI"
                suggestions={["分析当前原油市场局势和未来趋势研判", "当前最大的利多和利空是什么", "库存和供给端有哪些值得警惕的信号"]} />
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

      {score && unavailableSources.length > 0 && (
        <div className="mb-3 text-[11px] text-warning">
          数据源异常：{unavailableSources.map((s) => `${s.label}${s.status === "stale"
            ? `（观测滞后${s.age_days ?? "?"}天）` : "（缺失）"}`).join("、")}
        </div>
      )}

      {/* 顶部三列：实时行情（左 1/3，暗盘分时图 + tab 切外盘）｜评分卡｜AI 解读。
          等高靠「压缩高的那张」而不是「拉伸矮的两张」：实时行情卡把分时图压到 152、
          外盘折线压到 88、口径与时间并成一行、统计行收成两行，实测外框 334px；
          网格用 items-start 让三卡各取自然高度——评分卡 328 / AI 解读 330，三者底边差 ≤6px，
          既不强制 stretch（那会把矮卡拉出几百 px 死白），也不受 AI 文案长短影响。 */}
      <div className="mb-5 grid items-start gap-4 lg:grid-cols-3">
        <LiveOilPanel hyper={wtiHyper} spot={spot} futures={futures} />

        {score && (
          <>
            <GlassCard glow className="p-5">
              <div className="flex items-center gap-4">
                <div className="relative h-32 w-32 shrink-0">
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
                    {score.signal ?? "—"}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">{score.date}</div>
                  <div className="mt-2 max-w-52 text-[10px] leading-relaxed text-muted-foreground/70">
                    {total == null ? "得分缺失" : total >= 65 ? "偏紧格局，回调视为机会"
                      : total >= 45 ? "多空均衡，区间思路"
                      : total >= 20 ? "偏松格局，反弹视为风险"
                      : "显著过剩，规避多头暴露"}
                  </div>
                </div>
              </div>
              {totalHist.length > 1 && (
                <div className="mt-2 border-t border-border/30 pt-2">
                  <span className="text-[9px] text-muted-foreground/45">近 1 年得分</span>
                  <Sparkline data={totalHist} height={38} className="mt-0.5"
                    color="--primary" valueSuffix=" 分" />
                  {brentAligned.length > 1 && (
                    <div className="mt-1.5 border-t border-border/30 pt-1.5">
                      <span className="text-[9px] text-muted-foreground/45">
                        布伦特连续 · USD/桶{brent?.stale ? " · 缓存" : ""}
                      </span>
                      <Sparkline data={brentAligned} height={38} className="mt-0.5"
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
                  <p><span className="font-medium text-primary/90">稀缺与供需</span>　{aiInsight.scarcity_demand}</p>
                  <p><span className="font-medium text-primary/90">计价与溢价</span>　{aiInsight.pricing_premium}</p>
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
                            {score.top_positive_drivers.length
                              ? score.top_positive_drivers.join("、")
                              : <span className="text-muted-foreground">暂无明显利多驱动</span>}
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <TrendingDown className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                          <div className="text-sm">
                            {score.top_negative_drivers.length
                              ? score.top_negative_drivers.join("、")
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
                  数据状态：{score.data_quality} · 更新于 {score.updated}
                </div>
              </div>
            </GlassCard>
          </>
        )}

        {!score && !err && (
          <GlassCard className="flex min-h-40 items-center justify-center p-5 text-sm text-muted-foreground lg:col-span-2">
            评分卡计算中…
          </GlassCard>
        )}
      </div>

      {score && (
        <>
          <GlassCard className="mb-5 p-5">
            <div className="mb-1 flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <Flame className="h-3.5 w-3.5" /> 维度得分
            </div>
            <p className="mb-3 text-[10px] leading-relaxed text-muted-foreground/60">
              研究链条：物理稀缺（库存）→ 供给响应 → 炼化需求 → 计价环境 → 风险溢价与仓位确认
            </p>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {dimOrder.map((name) => {
                const d = score.dimensions?.[name];
                const t = signalTone(d?.score ?? null);
                const hist = isHist(d?.hist) ? d.hist : [];
                const meta = score.dimension_order?.find((m) => m.name === name);
                return (
                  <div key={name} className="rounded-lg border border-border/40 p-3">
                    <div className="flex items-baseline justify-between">
                      <span className="text-xs text-muted-foreground">{name}</span>
                      <span className="text-[10px] text-muted-foreground/70">
                        {d ? `${Math.round(d.weight * 100)}%` : ""}
                      </span>
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
                            <span className="text-[9px] text-muted-foreground/45">近 1 年</span>
                            <Sparkline data={hist} height={30} className="mt-0.5"
                              color="--primary" />
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="mt-1 text-sm text-muted-foreground/60">未接入</div>
                    )}
                    {meta?.note && (
                      <div className="mt-1.5 text-[10px] text-muted-foreground/50">{meta.note}</div>
                    )}
                  </div>
                );
              })}
            </div>
          </GlassCard>

          {structure && (
            <GlassCard className="mb-5 p-5">
              <div className="mb-1 text-xs font-medium text-muted-foreground">
                价格结构与供需锚（客观数据，不计入评分）
              </div>
              <p className="mb-3 text-[10px] leading-relaxed text-muted-foreground/60">
                {structure.note}
              </p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <StructureSparkline title="Brent-WTI 价差" data={structure.brent_wti}
                  color="--warning" unit="USD"
                  note="走阔=美湾物流/库存瓶颈或布伦特体系更紧" />
                <StructureSparkline title="SC/Brent 比率" data={structure.sc_brent_ratio}
                  color="--primary" unit=""
                  note={`SC÷(Brent×USDCNY ${structure.usdcny?.toFixed(3) ?? "—"})；>1=亚太采购溢价`} />
                <StructureSparkline title="美国 SPR 战略储备" data={structure.spr}
                  color="--danger" unit="千桶"
                  note="政策性缓冲；持续回补=边际需求" />
                <StructureSparkline title="商业库存需求天数" data={structure.days_of_supply}
                  color="--success" unit="天"
                  note="库存相对炼厂需求的可用度（不含 SPR）" />
              </div>
            </GlassCard>
          )}

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {score.indicators.map((ind) => <IndicatorCard key={ind.key} ind={ind} />)}
          </div>

          <p className="mt-4 text-[11px] leading-relaxed text-muted-foreground/70">
            评分框架见 research/oil_price_analysis_framework_v1.0.html（实现版本 V1.0）。
            研究主线：边际物理稀缺 → 预期库存 → 风险溢价；价格结构层只呈现客观事实。
            数据源：EIA 周度（Petroleum Bulk）、CFTC WTI 持仓、GPR 地缘风险指数、
            frankfurter 合成美元指数、腾讯/新浪行情。周频指标按 5 年历史分位评分。
            油价结论须注明基准（Brent/WTI/SC），本页为油市环境判断，不构成交易指令。
          </p>
        </>
      )}

      {!score && !err && (
        <PageSkeleton
          title="首次计算中"
          detail="正在拉取 EIA 周度库存、CFTC 持仓与 GPR 地缘风险历史并计算 5 年分位，实测约 1-2 分钟，仅首次需要。"
          hint="算完会写入本地快照，之后打开直接秒开，不会再等。"
          cards={8}
          cardHeight="h-40"
        />
      )}
    </div>
  );
}
