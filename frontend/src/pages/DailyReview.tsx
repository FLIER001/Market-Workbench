import { useState, useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { Sparkles, Loader2, AlertCircle, RefreshCw, Gauge, ArrowDownUp, TrendingUp, TrendingDown, Flame, BarChart3, Globe, ChevronDown, LineChart } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, type IndexQuote, type MarketOverview, type ShortTermEmotion, type TurnoverTop, type GlobalIndex, type MinuteKline } from "@/lib/api";
import { hasLlm, chatStream } from "@/lib/llm";
import { SaveNoteButton } from "@/components/ui/SaveNoteButton";
import { MinuteChart } from "@/components/ui/MinuteChart";
import { storageGet, storageSet } from "@/lib/storage";
import { cn } from "@/lib/utils";
import { startBackgroundTask, useBackgroundTask } from "@/lib/backgroundTasks";

// A股红涨绿跌。全球市场（美股/港股指数）**也沿用红涨**——与整个看板及东财等中国平台一致，
// 对中国用户最不易看错（Simon 2026-07-05 确认；非国际绿涨惯例，是有意选择，勿改）。
const pctColor = (p: number) => (p > 0 ? "text-danger" : p < 0 ? "text-success" : "text-muted-foreground");
const fmt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const yi = (v: number | null) => (v == null ? "—" : `${fmt(v / 1e8)} 亿`); // 元 → 亿
const GLOBAL_IDX_CACHE_KEY = "vr-daily-global-idx-v2";
// 全球市场轮询间隔：与后端 /global/indices 的 5 分钟共享缓存对齐（轮更快也只是拿缓存）
const GLOBAL_IDX_REFRESH_MS = 5 * 60_000;
// A 股大盘指数卡 name → 腾讯完整代码（指数须带前缀，与后端 A_INDEX_CODES 对齐）
const A_INDEX_CODES: Record<string, string> = {
  "上证指数": "sh000001", "深证成指": "sz399001", "创业板指": "sz399006",
  "沪深300": "sh000300", "科创50": "sh000688", "北证50": "bj899050",
};

// 指数当日分时：A 股区 / 全球区各自独立选中一个指数，就地展开
type AMinuteSel = { id: string; name: string };
type GlobalMinuteSel = { region: string; id: string; name: string };

export function DailyReview() {
  const [indices, setIndices] = useState<IndexQuote[]>([]);
  const [idxErr, setIdxErr] = useState(false);
  const [needConfig, setNeedConfig] = useState(false);
  const reviewKey = `daily-review:${new Date().toISOString().slice(0, 10)}`;
  const reviewTask = useBackgroundTask<{ text: string }>(reviewKey, { text: "" });
  const review = reviewTask.data.text;
  const reviewLoading = reviewTask.status === "running";
  const reviewErr = reviewTask.status === "error" ? reviewTask.error : null;
  const [overview, setOverview] = useState<MarketOverview | null>(null);
  const [emotion, setEmotion] = useState<ShortTermEmotion | null>(null);
  const [turnover, setTurnover] = useState<TurnoverTop | null>(null);
  // 全球市场：点击日盘/夜盘综合卡片展开对应国家指数（默认收起，再点一下收起）
  const [expandSession, setExpandSession] = useState<"day" | "night" | null>(null);
  const [globalIdx, setGlobalIdx] = useState<GlobalIndex[]>(() => {
    try {
      const cached = storageGet(GLOBAL_IDX_CACHE_KEY);
      if (!cached) return [];
      const rows = JSON.parse(cached) as GlobalIndex[];
      // 旧缓存（时段修复前、无 hours_bj 字段）一律丢弃，避免旧开闭状态/缺时段标注残留
      return rows.length > 0 && rows[0].hours_bj ? rows : [];
    } catch { return []; }
  });
  // 始终持有最新列表，供 5 分钟轮询回调判断哪些市场已开盘（避免闭包拿到旧 state）
  const globalIdxRef = useRef<GlobalIndex[]>(globalIdx);
  // 点击某个指数卡 → 该指数分时图就地往下展开（再点同一个收起）。A 股区 / 全球区各自独立。
  const [aMinuteSel, setAMinuteSel] = useState<AMinuteSel | null>(null);
  const [aMinute, setAMinute] = useState<{ data: MinuteKline | null; loading: boolean; err: string | null }>({ data: null, loading: false, err: null });
  const [gMinuteSel, setGMinuteSel] = useState<GlobalMinuteSel | null>(null);
  const [gMinute, setGMinute] = useState<{ data: MinuteKline | null; loading: boolean; err: string | null }>({ data: null, loading: false, err: null });
  const aMinuteReqRef = useRef(0);
  const gMinuteReqRef = useRef(0);

  const toggleAMinute = (sel: AMinuteSel) => {
    if (aMinuteSel?.id === sel.id) { setAMinuteSel(null); return; }
    setAMinuteSel(sel);
    setAMinute({ data: null, loading: true, err: null });
    const reqId = ++aMinuteReqRef.current;
    api.minuteKline(sel.id)
      .then((d) => {
        if (reqId !== aMinuteReqRef.current) return;
        // 最新点锚定到指数卡价格，保证与卡片点位一致
        const card = indices.find((i) => A_INDEX_CODES[i.name] === sel.id);
        if (card && d.points.length > 0) {
          const last = d.points[d.points.length - 1];
          d = { ...d, points: [...d.points.slice(0, -1), { ...last, price: card.price }] };
        }
        setAMinute({ data: d, loading: false, err: null });
      })
      .catch(() => { if (reqId === aMinuteReqRef.current) setAMinute({ data: null, loading: false, err: "分时数据暂不可用（可能是非交易时段或数据源限流）" }); });
  };

  const toggleGMinute = (sel: GlobalMinuteSel) => {
    if (gMinuteSel?.id === sel.id) { setGMinuteSel(null); return; }
    setGMinuteSel(sel);
    setGMinute({ data: null, loading: true, err: null });
    const reqId = ++gMinuteReqRef.current;
    api.globalMinute(sel.id)
      .then((d) => {
        if (reqId !== gMinuteReqRef.current) return;
        // 东财分钟序列与指数卡同源（时间戳=各市场开盘对应的北京时间），但分时和卡片
        // 各自 5 分钟缓存、刷新时点不同 → 最新点可能有小幅滞后。把分时最新点锚定到
        // 卡片价格，保证「点开看到的点位 = 卡片上的点位」。
        const card = globalIdxRef.current.find((g) => g.key === sel.id);
        if (card?.price != null && d.points.length > 0) {
          const last = d.points[d.points.length - 1];
          d = { ...d, points: [...d.points.slice(0, -1), { ...last, price: card.price }] };
        }
        setGMinute({ data: d, loading: false, err: null });
      })
      .catch(() => { if (reqId === gMinuteReqRef.current) setGMinute({ data: null, loading: false, err: "分时数据暂不可用（可能是非交易时段或数据源限流）" }); });
  };

  // 分时图面板：就地往下展开，跟随被点击的网格/地区行
  const minutePanel = (st: { data: MinuteKline | null; loading: boolean; err: string | null }) => (
    <GlassCard className="relative mt-3 h-[380px] p-0">
      {st.data?.last_day && (
        <div className="absolute right-3 top-3 z-20">
          <span className="rounded bg-warning/15 px-2 py-0.5 text-[11px] text-warning">上一交易日 {st.data.date}</span>
        </div>
      )}
      {st.loading && (
        <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin text-primary" /> 分时数据加载中…
        </div>
      )}
      {!st.loading && st.err && (
        <div className="flex h-full items-center justify-center gap-2 px-6 text-center text-sm text-muted-foreground">
          <AlertCircle className="h-4 w-4 shrink-0 text-warning" /> {st.err}
        </div>
      )}
      {!st.loading && !st.err && st.data && <MinuteChart data={st.data} height={380} />}
    </GlassCard>
  );

  // 各数据块请求是否已结束：区分「加载中」与「数据源暂不可用」（非交易时段/被限流时后端返回空）
  const [ovDone, setOvDone] = useState(false);
  const [emoDone, setEmoDone] = useState(false);
  const [toDone, setToDone] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // 每 5 分钟增量刷新。开盘市场实时刷；闭市市场也要一起刷——否则美股 21:30 开盘后
  // 前端仍以为它闭市、永不请求，「收盘」徽章和旧价就一直挂着。后端对真闭市市场
  // 回用收盘价、不发数据源，所以这里全量刷不增加数据源压力。
  const refreshGlobal = () => {
    const cur = globalIdxRef.current;
    if (cur.length > 0) {
      // 全量刷（开盘+闭市）：闭市市场靠它摘掉「收盘」徽章并拿到开盘价
      api.globalIndices(cur.map((g) => g.key)).then((fresh) => {
        const map = new Map(cur.map((g) => [g.key, g]));
        for (const g of fresh) map.set(g.key, g);
        const next = [...map.values()];
        globalIdxRef.current = next;
        setGlobalIdx(next);
        storageSet(GLOBAL_IDX_CACHE_KEY, JSON.stringify(next));
      }).catch(() => {});
      return;
    }
    api.globalIndices().then((d) => {
      globalIdxRef.current = d;
      setGlobalIdx(d);
      storageSet(GLOBAL_IDX_CACHE_KEY, JSON.stringify(d));
    }).catch(() => {});
  };

  const loadIndices = () => {
    setRefreshing(true);
    Promise.allSettled([
      api.indices().then(setIndices).catch(() => setIdxErr(true)),
      refreshGlobal(),
      api.marketOverview().then(setOverview).catch(() => {}).finally(() => setOvDone(true)),
      api.emotion().then(setEmotion).catch(() => {}).finally(() => setEmoDone(true)),
      api.turnoverTop().then(setTurnover).catch(() => {}).finally(() => setToDone(true)),
    ]).finally(() => setRefreshing(false));
  };

  // 数据块占位：请求没回来 = 加载中；回来了但为空 = 数据源暂不可用（别让用户干等）
  const pending = (done: boolean) => (
    <p className="py-4 text-center text-sm text-muted-foreground/60">
      {done ? "暂无数据：可能是非交易时段或数据源暂时不可用，可点「大盘指数」旁的刷新重试" : "加载中…"}
    </p>
  );


  useEffect(() => {
    loadIndices();
    // 已开盘的国家指数每 5 分钟自动刷新一次（与后端共享缓存 TTL 对齐，日盘/夜盘
    // 综合涨跌幅随之更新）；闭市市场直接用本地展示缓存、不发请求。页面切到后台时
    // 暂停轮询省流量，切回前台立即补刷一次（闭市市场仍是本地合并，无请求）。
    const tick = () => { if (!document.hidden) refreshGlobal(); };
    const timer = setInterval(tick, GLOBAL_IDX_REFRESH_MS);
    const onVisible = () => { if (!document.hidden) refreshGlobal(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);



  const today = new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });

  const dataSummary = indices.length
    ? indices.map((i) => `${i.name} ${i.price}（${i.change_pct > 0 ? "+" : ""}${i.change_pct}%）`).join("；")
    : "（指数数据未取到）";

  const sentiment = overview?.sentiment;
  const sectors = overview?.sectors || [];

  // —— 把页面上各数据块都打包给 AI（指数只是其一）——
  const sentimentText = sentiment
    ? `涨${sentiment.up}/跌${sentiment.down}/平${sentiment.flat}家，涨停${sentiment.zt}（真实${sentiment.zt_real}）、跌停${sentiment.dt}（真实${sentiment.dt_real}），活跃度${sentiment.active}，市场广度${sentiment.breadth}`
    : "（情绪数据未取到）";

  const sectorText = sectors.length
    ? sectors.slice(0, 10).map((x) => `${x.name}${x.pct > 0 ? "+" : ""}${x.pct}%·主力净${x.net > 0 ? "+" : ""}${Math.round(x.net)}亿`).join("；")
    : "（板块资金未取到）";

  const emotionText = emotion
    ? `涨停${emotion.zt_count}家/跌停${emotion.dt_count}家/炸板${emotion.zb_count}家，最高连板${emotion.max_boards}板，连板股${emotion.lianban_count}只` +
      `，封板率${emotion.seal_rate ?? "—"}%，炸板率${emotion.break_rate ?? "—"}%，晋级率${emotion.promotion_rate ?? "—"}%` +
      (emotion.ladder?.length ? `，梯队[${emotion.ladder.map((t) => `${t.boards}板×${t.count}`).join(" ")}]` : "") +
      (emotion.lianban_stocks?.length ? `，连板龙头[${emotion.lianban_stocks.slice(0, 5).map((x) => `${x.name}${x.boards}板(${x.industry})`).join("、")}]` : "")
    : "（短线情绪未取到）";

  const turnoverText = turnover?.stocks?.length
    ? turnover.stocks.slice(0, 8).map((x) => `${x.name}${x.pct != null ? (x.pct > 0 ? "+" : "") + x.pct + "%" : ""}·额${x.amount != null ? Math.round(x.amount / 1e8) + "亿" : "—"}`).join("；")
    : "（成交额榜未取到）";

  const globalText = globalIdx.length
    ? globalIdx.map((g) => `${g.name}${g.change_pct != null ? (g.change_pct > 0 ? "+" : "") + g.change_pct + "%" : "—"}`).join("、")
    : "（外围未取到）";

  const runReview = async () => {
    setNeedConfig(false);
    if (!hasLlm()) { setNeedConfig(true); return; }
    const prompt =
      `你是 A 股职业复盘分析师。下面是今天盘面的客观数据，请基于此做一段**综合研判**，不要复述数据本身——数据读者已经在页面上看到了，你的价值在于「判断」。

` +
      `【大盘指数】${dataSummary}
` +
      `【市场情绪】${sentimentText}
` +
      `【板块资金】${sectorText}
` +
      `【短线情绪】${emotionText}
` +
      `【成交额榜】${turnoverText}
` +
      `【隔夜外围】${globalText}

` +
      `请按以下框架输出（用中文，精炼，每点 1-2 句，总篇幅控制在 400 字内）：
` +
      `1. **情绪周期定位**：用连板高度、炸板率、晋级率判断当前处于冰点/回暖/高潮/退潮哪个阶段，给出依据。炸板率>30%警惕分歧、>50%亏钱效应扩散；晋级率骤降+梯队断层是退潮信号。
` +
      `2. **指数与赚钱效应是否背离**：对比指数涨跌与涨跌停家数、活跃度。若指数红但下跌家数占多数，是权重护盘的"指数失真"，要点明。
` +
      `3. **资金主线与切换**：从板块资金流向判断当前主线是什么、是否有高位流出→低位承接的"高低切换"，主线是行业驱动（持续）还是事件驱动（易一日游）。
` +
      `4. **外围联动**：隔夜外围（尤其美股科技股/半导体）与今日 A 股对应板块的映射，是否有共振或背离。
` +
      `5. **次日观察点**：1-2 个客观、可验证的盘面信号（不预测涨跌、不推荐标的）。
` +
      `要求：观点明确、有判断有依据，不堆术语，不做投资建议，不复述上面的原始数据。`;
    startBackgroundTask(reviewKey, { text: "" }, async (update, signal) => {
      await chatStream([{ role: "user", content: prompt }], `今日大盘数据：${dataSummary}`, {
        onDelta: (t) => update((data) => ({ text: data.text + t })),
      }, signal);
    });
  };

  const sentCells = sentiment ? [
    { k: "上涨家数", v: sentiment.up, up: true },
    { k: "下跌家数", v: sentiment.down, up: false },
    { k: "平盘", v: sentiment.flat, up: null },
    { k: "涨停", v: sentiment.zt, up: true },
    { k: "真实涨停", v: sentiment.zt_real, up: true },
    { k: "跌停", v: sentiment.dt, up: false },
    { k: "真实跌停", v: sentiment.dt_real, up: false },
    { k: "活跃度", v: sentiment.active, up: null },
  ] : [];

  return (
    <div>
      <PageHeader
        title="市场全景"
        subtitle={`${today} · 大盘 / 情绪 / 板块资金一屏看全`}
        actions={
          /* AI 复盘：悬停展开复盘内容（如有），点击触发复盘 */
          <div className="group relative">
            <button
              onClick={runReview}
              disabled={reviewLoading}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow transition-colors hover:bg-primary/25 disabled:opacity-50"
            >
              {reviewLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              {reviewLoading ? "复盘中…" : review ? "重新复盘" : "AI 复盘"}
            </button>
            {/* 悬停展开的复盘内容浮层（向下展开，右对齐） */}
            <div className="invisible absolute right-0 top-full z-50 mt-2 w-[400px] max-w-[85vw] translate-y-1 opacity-0 transition-all duration-200 group-hover:visible group-hover:translate-y-0 group-hover:opacity-100">
              <GlassCard glow className="max-h-[60vh] overflow-auto p-4">
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="flex items-center gap-1.5 text-sm font-semibold"><Sparkles className="h-4 w-4 text-primary" /> AI 当日复盘</h3>
                  {review && !reviewLoading && <SaveNoteButton kind="复盘" title={`每日复盘 ${today}`} content={review} />}
                </div>
                {needConfig && (
                  <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/5 p-2.5 text-xs text-muted-foreground">
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
                    还没接入 AI。<Link to="/settings" className="text-primary">先去接入你的 AI</Link>。
                  </div>
                )}
                {reviewErr && (
                  <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2.5 text-xs text-destructive">
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {reviewErr}
                  </div>
                )}
                {reviewLoading && !review && (
                  <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" /> AI 正在复盘…
                  </div>
                )}
                {review ? (
                  <div className="prose prose-sm prose-invert max-w-none text-xs leading-relaxed text-foreground">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{review}</ReactMarkdown>
                  </div>
                ) : !needConfig && !reviewErr && !reviewLoading ? (
                  <p className="text-xs text-muted-foreground">
                    点上方按钮，系统把当天客观数据打包给你的 AI 生成复盘。
                    <b className="text-foreground">分析是它给的，我们只负责喂数据。</b>
                  </p>
                ) : null}
              </GlassCard>
            </div>
          </div>
        }
      />

      {/* 1. 大盘指数（实时） */}
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><LineChart className="h-4 w-4" /> 大盘指数</h3>
        <button onClick={loadIndices} className="text-muted-foreground hover:text-primary" title="刷新"><RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} /></button>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {indices.length === 0
          ? [1, 2, 3, 4, 5, 6].map((i) => (
              <GlassCard key={i} className="p-3">
                <p className="text-xs text-muted-foreground">{idxErr ? "行情未接通" : "加载中…"}</p>
                <p className="mt-1 font-mono text-lg font-bold text-muted-foreground/40">—</p>
              </GlassCard>
            ))
          : indices.map((i) => {
              const code = A_INDEX_CODES[i.name];
              const active = aMinuteSel?.id === code;
              return (
                <GlassCard
                  key={i.name}
                  onClick={code ? () => toggleAMinute({ id: code, name: i.name }) : undefined}
                  className={cn("p-3", code && "cursor-pointer transition-colors hover:border-primary/40", active && "border-primary/50 bg-primary/5")}
                >
                  <p className="truncate text-xs text-muted-foreground">{i.name}</p>
                  <p className={cn("mt-1 font-mono text-lg font-bold", pctColor(i.change_pct))}>{i.price}</p>
                  <p className={cn("text-xs", pctColor(i.change_pct))}>{i.change_pct > 0 ? "+" : ""}{i.change_pct}%</p>
                </GlassCard>
              );
            })}
      </div>
      {/* A 股指数分时：就地往下展开 */}
      <div className="mb-6">{aMinuteSel && minutePanel(aMinute)}</div>

      {/* 1b. 全球市场（隔夜外围 + 主要经济体指数） */}
      {globalIdx.length > 0 && (
        <>
          <div className="mb-3 flex items-center gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><Globe className="h-4 w-4" /> 全球市场</h3>
            <span className="text-[11px] text-muted-foreground/50">美股 · 港股 · 亚太 · 欧洲 · 南亚</span>
          </div>
          {(() => {
            const regionOrder = ["美股", "港股", "亚太", "欧洲", "南亚"];
            const grouped = new Map<string, typeof globalIdx>();
            for (const g of globalIdx) {
              if (!grouped.has(g.region)) grouped.set(g.region, []);
              grouped.get(g.region)!.push(g);
            }
            // 按市值权重计算日盘/夜盘综合涨跌幅（缺数据的指数跳过并归一化）
            const DAY = new Set(["港股", "亚太", "南亚"]);
            const NIGHT = new Set(["美股", "欧洲"]);
            const weighted = (regions: Set<string>): number | null => {
              let num = 0, den = 0;
              for (const g of globalIdx) {
                if (!regions.has(g.region) || g.change_pct == null || !g.weight) continue;
                num += g.change_pct * g.weight;
                den += g.weight;
              }
              return den > 0 ? Math.round((num / den) * 100) / 100 : null;
            };
            const dayChg = weighted(DAY);
            const nightChg = weighted(NIGHT);
            return (
              <>
              {(dayChg != null || nightChg != null) && (
                <div className="mb-4 grid grid-cols-2 gap-3">
                  {[
                    { label: "日盘综合", sub: "港股 · 亚太 · 南亚", val: dayChg, session: "day" as const },
                    { label: "夜盘综合", sub: "美股 · 欧洲（中国夜盘）", val: nightChg, session: "night" as const },
                  ].map((c) => {
                    const expanded = expandSession === c.session;
                    return (
                      <GlassCard
                        key={c.label}
                        onClick={() => setExpandSession(expanded ? null : c.session)}
                        className={cn("p-3 transition-colors", expanded && "border-primary/40 bg-primary/5")}
                      >
                        <div className="flex items-center justify-between">
                          <p className="text-xs text-muted-foreground">{c.label}</p>
                          <ChevronDown className={cn("h-3.5 w-3.5 text-muted-foreground/50 transition-transform", expanded && "rotate-180 text-primary")} />
                        </div>
                        <p className={cn("mt-0.5 font-mono text-2xl font-extrabold", c.val == null ? "text-foreground" : pctColor(c.val))}>
                          {c.val == null ? "—" : `${c.val > 0 ? "+" : ""}${c.val}%`}
                        </p>
                        <p className="mt-0.5 text-[10px] text-muted-foreground/50">{c.sub} · 按市值加权</p>
                      </GlassCard>
                    );
                  })}
                </div>
              )}
              {regionOrder
                .filter((region) => {
                  if (expandSession === "day") return DAY.has(region);
                  if (expandSession === "night") return NIGHT.has(region);
                  return false; // 默认收起，点击综合卡片才展开
                })
                .map((region) => {
              const items = grouped.get(region);
              if (!items || items.length === 0) return null;
              return (
                <div key={region} className="mb-4">
                  <p className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/60">
                    {region}
                    {(region === "美股" || region === "欧洲") && (
                      <span className="ml-1.5 normal-case text-muted-foreground/40">（中国夜盘）</span>
                    )}
                  </p>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                    {items.map((g) => (
                      <GlassCard
                        key={g.key}
                        onClick={() => toggleGMinute({ region, id: g.key, name: g.name })}
                        className={cn(
                          "cursor-pointer p-3 transition-colors hover:border-primary/40",
                          gMinuteSel?.id === g.key && "border-primary/50 bg-primary/5",
                        )}
                      >
                        <div className="flex items-center justify-between gap-1">
                          <p className="truncate text-xs text-muted-foreground">{g.name}</p>
                          {g.closed && <span className="shrink-0 rounded bg-muted/40 px-1 py-px text-[9px] text-muted-foreground/60">收盘</span>}
                        </div>
                        <p className={cn("mt-1 font-mono text-lg font-bold", g.change_pct == null ? "text-foreground" : pctColor(g.change_pct))}>{g.price ?? "—"}</p>
                        <p className={cn("text-xs", g.change_pct == null ? "text-muted-foreground" : pctColor(g.change_pct))}>
                          {g.change_pct == null ? "—" : `${g.change_pct > 0 ? "+" : ""}${g.change_pct}%`}
                        </p>
                        {g.hours_bj && g.hours_bj.length > 0 && (
                          <p className="mt-0.5 text-[9px] leading-tight text-muted-foreground/45" title="北京时间交易时段（含夏令时）">
                            北京 {g.hours_bj.join(" / ")}
                          </p>
                        )}
                      </GlassCard>
                    ))}
                  </div>
                  {/* 全球指数分时：就地往下展开（跟随该地区行） */}
                  {gMinuteSel?.region === region && minutePanel(gMinute)}
                </div>
                );
              })}
              </>
            );
          })()}
        </>
      )}



      {/* 3b. 市场情绪 */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><Gauge className="h-4 w-4" /> 市场情绪</h3>
        {sentiment?.date && <span className="text-[11px] text-muted-foreground/50">{sentiment.date}</span>}
      </div>
      <GlassCard className="mb-6">
        {!sentiment?.breadth ? (
          pending(ovDone)
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                { k: "大盘宽度", v: sentiment.breadth, hint: "冰点 / 偏弱 / 中性 / 偏强 / 普涨" },
                { k: "题材投机", v: sentiment.speculation, hint: "冰点 / 普通 / 活跃 / 亢奋" },
              ].map((m) => (
                <div key={m.k} className="rounded-lg bg-muted/25 p-4">
                  <p className="text-xs text-muted-foreground">{m.k}</p>
                  <p className="mt-1 text-2xl font-bold text-primary">{m.v}</p>
                  <p className="mt-1 text-[11px] text-muted-foreground/60">{m.hint}</p>
                </div>
              ))}
            </div>
            <div className="mt-3 grid grid-cols-4 gap-2">
              {sentCells.map((c) => (
                <div key={c.k} className="rounded-lg bg-muted/20 p-2 text-center">
                  <p className="truncate text-[11px] text-muted-foreground">{c.k}</p>
                  <p className={cn("mt-0.5 font-mono text-sm font-bold", c.up === null ? "text-foreground" : c.up ? "text-danger" : "text-success")}>{c.v}</p>
                </div>
              ))}
            </div>
          </>
        )}
      </GlassCard>

      {/* 4. 资金轮动 */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><ArrowDownUp className="h-4 w-4" /> 资金轮动</h3>
        <span className="text-[11px] text-muted-foreground/50">板块级净流入 / 流出 · Top 10</span>
      </div>
      <div className="mb-2 grid gap-4 md:grid-cols-2">
        {[
          { title: "流入 Top 10", icon: TrendingUp, color: "text-danger", rows: sectors.slice(0, 10) },
          { title: "流出 Top 10", icon: TrendingDown, color: "text-success", rows: [...sectors].slice(-10).reverse() },
        ].map((col) => (
          <GlassCard key={col.title}>
            <h4 className={cn("mb-3 flex items-center gap-1.5 text-sm font-semibold", col.color)}><col.icon className="h-4 w-4" /> {col.title}</h4>
            {col.rows.length === 0 ? (
              pending(ovDone)
            ) : (
              <div className="space-y-1.5">
                <div className="flex items-center gap-3 border-b border-border/50 pb-1.5 text-[11px] font-medium text-muted-foreground">
                  <span className="w-5">#</span>
                  <span className="flex-1">板块</span>
                  <span className="w-14 text-right">涨跌%</span>
                  <span className="w-20 text-right">净流入</span>
                  <span className="w-10 text-right">家数</span>
                </div>
                {col.rows.map((s, i) => (
                  <div key={s.name} className="flex items-center gap-3 border-b border-border/30 pb-1.5 text-sm last:border-0">
                    <span className="w-5 text-xs text-muted-foreground/50">{i + 1}</span>
                    <span className="flex-1 truncate">{s.name}</span>
                    <span className={cn("w-14 text-right font-mono text-xs", pctColor(s.pct))}>{s.pct > 0 ? "+" : ""}{s.pct}%</span>
                    <span className={cn("w-20 text-right font-mono text-xs", pctColor(s.net))}>{s.net > 0 ? "+" : ""}{fmt(s.net)} 亿</span>
                    <span className="w-10 text-right font-mono text-xs text-muted-foreground">{s.firms}</span>
                  </div>
                ))}
              </div>
            )}
          </GlassCard>
        ))}
      </div>

      {/* 6. 短线情绪（连板梯队 / 打板情绪，聚合口径零个股名） */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><Flame className="h-4 w-4" /> 短线情绪</h3>
        <span className="text-[11px] text-muted-foreground/50">连板股 · 打板情绪 · 客观公开榜单</span>
        {emotion?.date && <span className="ml-auto text-[11px] text-muted-foreground/50">{emotion.date}</span>}
      </div>
      <GlassCard className="mb-6">
        {!emotion || emotion.zt_count === undefined ? (
          pending(emoDone)
        ) : (
          <>
            {/* 关键计数 */}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[
                { k: "涨停", v: `${emotion.zt_count}`, cls: "text-danger" },
                { k: "跌停", v: `${emotion.dt_count}`, cls: "text-success" },
                { k: "最高连板", v: `${emotion.max_boards} 板`, cls: "text-primary" },
                { k: "连板（2板+）", v: `${emotion.lianban_count} 家`, cls: "text-primary" },
              ].map((c) => (
                <div key={c.k} className="rounded-lg bg-muted/25 p-3 text-center">
                  <p className="text-[11px] text-muted-foreground">{c.k}</p>
                  <p className={cn("mt-0.5 font-mono text-xl font-bold", c.cls)}>{c.v}</p>
                </div>
              ))}
            </div>
            {/* 打板情绪比率 */}
            <div className="mt-2 grid grid-cols-3 gap-2">
              {[
                { k: "封板率", v: emotion.seal_rate, hint: "封住 / 尝试涨停", strong: true },
                { k: "炸板率", v: emotion.break_rate, hint: "炸板 / 尝试涨停", strong: false },
                { k: "晋级率", v: emotion.promotion_rate, hint: "昨涨停今又停", strong: true },
              ].map((c) => (
                <div key={c.k} className="rounded-lg bg-muted/20 p-2.5 text-center">
                  <p className="text-[11px] text-muted-foreground">{c.k}</p>
                  <p className={cn("mt-0.5 font-mono text-sm font-bold", c.strong ? "text-danger" : "text-success")}>
                    {c.v == null ? "—" : `${(c.v * 100).toFixed(1)}%`}
                  </p>
                  <p className="mt-0.5 text-[10px] text-muted-foreground/50">{c.hint}</p>
                </div>
              ))}
            </div>
            {/* 连板股清单（2 板以上，客观公开榜单） */}
            <div className="mt-3">
              <p className="mb-1.5 text-[11px] text-muted-foreground">连板股（2 板以上连续涨停）· 客观公开榜单</p>
              {emotion.lianban_stocks.length === 0 ? (
                <p className="text-xs text-muted-foreground/50">今日无 2 板以上个股</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                        {["名称", "连板", "现价", "涨停%", "成交额", "流通市值", "概念"].map((h) => (
                          <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {emotion.lianban_stocks.map((s) => (
                        <tr key={s.code} className="border-b border-border/30">
                          <td className="px-2 py-2"><span className="font-medium">{s.name}</span> <span className="text-xs text-muted-foreground/50">{s.code}</span></td>
                          <td className="whitespace-nowrap px-2 py-2 font-mono font-bold text-primary">{s.boards} 板</td>
                          <td className="px-2 py-2 font-mono">{s.price}</td>
                          <td className="px-2 py-2 font-mono text-danger">+{s.pct}%</td>
                          <td className="whitespace-nowrap px-2 py-2 font-mono text-muted-foreground">{yi(s.amount)}</td>
                          <td className="whitespace-nowrap px-2 py-2 font-mono text-muted-foreground">{yi(s.float_cap)}</td>
                          <td className="whitespace-nowrap px-2 py-2 text-xs text-muted-foreground">{s.industry}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </GlassCard>

      {/* 7. 全市场成交额 TOP20（客观公开榜单） */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><BarChart3 className="h-4 w-4" /> 全市场成交额 TOP20</h3>
        <span className="text-[11px] text-muted-foreground/50">客观公开榜单</span>
        {turnover?.updated && <span className="ml-auto text-[11px] text-muted-foreground/50">{turnover.updated}</span>}
      </div>
      <GlassCard className="mb-6">
        {!turnover || turnover.stocks.length === 0 ? (
          pending(toDone)
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {["#", "名称", "现价", "涨跌%", "成交额", "总市值", "行业"].map((h) => (
                    <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {turnover.stocks.map((s, i) => (
                  <tr key={s.code} className="border-b border-border/30">
                    <td className="px-2 py-2 font-mono text-xs text-muted-foreground/50">{i + 1}</td>
                    <td className="px-2 py-2"><span className="font-medium">{s.name}</span> <span className="text-xs text-muted-foreground/50">{s.code}</span></td>
                    <td className="px-2 py-2 font-mono">{s.price ?? "—"}</td>
                    <td className={cn("px-2 py-2 font-mono", s.pct == null ? "text-muted-foreground" : pctColor(s.pct))}>
                      {s.pct == null ? "—" : `${s.pct > 0 ? "+" : ""}${s.pct}%`}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 font-mono">{yi(s.amount)}</td>
                    <td className="whitespace-nowrap px-2 py-2 font-mono text-muted-foreground">{yi(s.mcap)}</td>
                    <td className="whitespace-nowrap px-2 py-2 text-xs text-muted-foreground">{s.industry}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>


    </div>
  );
}
