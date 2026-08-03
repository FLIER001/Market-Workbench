import { useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { TrendingUp, FileText, Newspaper, Rss, RefreshCw, Loader2, ExternalLink, AlertCircle, Sparkles, Lightbulb, Star } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { SaveNoteButton } from "@/components/ui/SaveNoteButton";
import { api, ApiError, type RadarData, type Industry, type Announcement, type NewsItem } from "@/lib/api";
import { loadWatch } from "@/lib/watchlist";
import { hasLlm, chatStream } from "@/lib/llm";
import { storageGet, storageSet } from "@/lib/storage";
import { cn } from "@/lib/utils";
import { cancelBackgroundTask, startBackgroundTask, updateBackgroundTask, useBackgroundTask } from "@/lib/backgroundTasks";
import { WatchlistEventJudgement } from "@/components/intel/WatchlistEventJudgement";

const TABS = [
  { key: "events", label: "自选事件研判", icon: TrendingUp, integrated: false, desc: "基于自选标的、关联板块与多源证据的 AI 事件研判" },
  { key: "filings", label: "A股公告", icon: FileText, integrated: false, desc: "汇总关注列表里各个股的近期公告（东财公开披露）" },
  { key: "news", label: "公开新闻", icon: Newspaper, integrated: false, desc: "汇总关注列表里各个股的近期新闻（公开源）" },
  { key: "investment-news", label: "Investment News", icon: Rss, integrated: true, desc: "12 赛道全球公开 RSS 资讯（集成自 investment-news 仓库）" },
];

interface Digest {
  loading?: boolean; text?: string; err?: string; needKey?: boolean;
  analysisLoading?: boolean; analysis?: string; analysisErr?: string;
}
interface IntelTaskData {
  digests: Record<string, Digest>;
  bulk: { running: boolean; done: number; total: number };
}

// 持久化 digests 到 localStorage，使切换页面再回来仍保留提炼结果。
const DIGEST_KEY = "vr-intel-digests";
const RADAR_KEY = "vr-intel-radar";
const INTEL_TASK_KEY = "intel:analysis";
const EMPTY_BULK = { running: false, done: 0, total: 0 };

function loadDigests(): Record<string, Digest> {
  try {
    const saved = JSON.parse(storageGet(DIGEST_KEY) || "{}") as Record<string, Digest>;
    return Object.fromEntries(
      Object.entries(saved)
        .map(([key, digest]) => {
          const { loading: _loading, analysisLoading: _analysisLoading, ...stable } = digest || {};
          return [key, stable] as const;
        })
        .filter(([, digest]) => digest.text || digest.err || digest.needKey || digest.analysis || digest.analysisErr),
    );
  } catch {
    return {};
  }
}
function saveDigests(d: Record<string, Digest>) {
  // loading 属于当前请求的瞬时状态；持久化后刷新页面会恢复成一个永远无法结束的“假请求”。
  const stable = Object.fromEntries(
    Object.entries(d)
      .map(([key, digest]) => {
        const { loading: _loading, analysisLoading: _analysisLoading, ...rest } = digest;
        return [key, rest] as const;
      })
      .filter(([, digest]) => digest.text || digest.err || digest.needKey || digest.analysis || digest.analysisErr),
  );
  storageSet(DIGEST_KEY, JSON.stringify(stable));
}

function InvestmentNewsPanel() {
  const [data, setData] = useState<RadarData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [active, setActive] = useState("ai");
  const [refreshing, setRefreshing] = useState(false);
  const intelTask = useBackgroundTask<IntelTaskData>(INTEL_TASK_KEY, { digests: loadDigests(), bulk: EMPTY_BULK });
  const { digests, bulk } = intelTask.data;

  useEffect(() => {
    // 优先从 localStorage 恢复缓存的 radar 数据，避免每次进页面都白屏等待
    const cached = storageGet(RADAR_KEY);
    if (cached) {
      try { setData(JSON.parse(cached)); } catch { /* ignore */ }
    }
    api.radar().then((d) => { setData(d); storageSet(RADAR_KEY, JSON.stringify(d)); }).catch((e) => setErr(e instanceof ApiError ? e.message : "加载失败"));
  }, []);

  // digests 变化时持久化
  useEffect(() => { saveDigests(digests); }, [digests]);

  const refresh = async () => {
    setRefreshing(true); setErr(null);
    try {
      const d = await api.radarRefresh();
      setData(d);
      storageSet(RADAR_KEY, JSON.stringify(d));
      // 刷新成功后自动触发一键提炼全部要点
      if (hasLlm()) {
        // 延迟 0ms 让 UI 先更新到非 refreshing 状态
        setTimeout(() => genAll(d), 0);
      }
    }
    catch (e) { setErr(e instanceof ApiError ? e.message : "刷新失败"); }
    finally { setRefreshing(false); }
  };

  const industries: Industry[] = data?.industries || [];
  const cur = industries.find((i) => i.key === active) || industries[0];
  const hasData = !!data?.generated_at;

  const runDigest = async (
    ind: Industry,
    update: (updater: (data: IntelTaskData) => IntelTaskData) => void,
    signal: AbortSignal,
  ) => {
    update((state) => ({ ...state, digests: { ...state.digests, [ind.key]: { loading: true } } }));
    const items = ind.items.slice(0, 25);
    // 给资讯编号 [0]..[24]，AI 引用时标注，渲染时替换为可点击链接
    const ctx = items.map((it, i) => `[${i}] [${it.time}] ${it.source}｜${it.zh || it.title}`).join("\n");
    const prompt =
      `以下是「${ind.name}」赛道近期资讯（每条以 [编号] 开头）。请提炼「今日要点」：
` +
      `1. 严格分为「## 国内」和「## 国外」两个小节，国内外合计通常 5-10 条；按实际重要信息量增减，宁缺毋滥，不为凑数收录一般性资讯；
` +
      `2. 不要求国内外数量均分；只挑影响范围大、时效性强、信息增量高的事件，去掉重复报道，并按重要性从高到低排列；某侧确无重要更新就写「无重要更新」；
` +
      `3. 每条用「- 」列点，一句话说清主体、事件及关键变化（≤40 字）；每条结尾必须标注来源编号，格式 [^编号]（如 [^3]），可标多个如 [^1][^4]；
` +
      `4. 只客观陈述事件 / 趋势，不推荐标的、不预测涨跌、不构成建议；
` +
      `5. 不要多余前后缀、不要总结段。

${ctx}`;
    try {
      let acc = "";
      // 120 秒超时兜底：网络断开或 LLM 无响应时不至于永远转圈
      let timeoutId: ReturnType<typeof setTimeout> | undefined;
      try {
        const timeout = new Promise<never>((_, reject) => {
          timeoutId = setTimeout(() => reject(new Error("请求超时（120 秒），请检查网络或 AI 服务状态")), 120_000);
        });
        const result = await Promise.race([
          chatStream([{ role: "user", content: prompt }], `${ind.name}赛道资讯`, {
            onDelta: (t) => {
              acc += t;
              update((state) => ({ ...state, digests: { ...state.digests, [ind.key]: { text: acc } } }));
            },
          }, signal),
          timeout,
        ]);
        const text = (acc || result.content).trim();
        if (!text) throw new Error("AI 返回了空内容，请检查模型配置后重试");
        update((state) => ({ ...state, digests: { ...state.digests, [ind.key]: { text } } }));
      } finally {
        if (timeoutId) clearTimeout(timeoutId);
      }
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : "生成失败";
      update((state) => ({ ...state, digests: { ...state.digests, [ind.key]: { err: msg } } }));
    }
  };

  const genDigest = (ind: Industry) => {
    if (!hasLlm()) {
      updateBackgroundTask<IntelTaskData>(INTEL_TASK_KEY, { digests, bulk }, (state) => ({
        ...state, digests: { ...state.digests, [ind.key]: { needKey: true } },
      }));
      return;
    }
    startBackgroundTask(
      INTEL_TASK_KEY,
      { digests, bulk: { ...bulk, running: false } },
      (update, signal) => runDigest(ind, update, signal),
    );
  };

  const retryDigest = (ind: Industry) => {
    cancelBackgroundTask(INTEL_TASK_KEY);
    genDigest(ind);
  };

  // 深入分析：基于已提炼的要点，逐条分析影响 + 对投资的影响
  const genAnalysis = (ind: Industry) => {
    const dg = digests[ind.key];
    if (!hasLlm() || !dg?.text) return;
    const items = ind.items.slice(0, 25);
    const ctx = items.map((it, i) => `[${i}] [${it.time}] ${it.source}｜${it.zh || it.title}`).join("\n");
    const prompt =
      `以下是「${ind.name}」赛道的今日要点（已按国内/国外分类，[^编号] 对应下方资讯列表）：

${dg.text}

` +
      `请对每条要点做「深入分析」，要求：
` +
      `1. 保持「## 国内」「## 国外」结构，每条要点一个子条目（复述要点原文 ≤20 字）；
` +
      `2. 每条下分两行写：「影响：」（对行业 / 产业链 / 竞争格局的实际影响，≤60 字）和「投资视角：」（对二级市场相关板块 / 个股的客观影响路径，点明利好或承压方向但不推荐具体买卖，≤60 字）；
` +
      `3. 每条结尾保留来源编号 [^编号]；
` +
      `4. 全部基于下方资讯事实推演，不确定的写明「待验证」，不编造数据，不构成投资建议。

资讯列表：
${ctx}`;
    startBackgroundTask(INTEL_TASK_KEY, { digests, bulk }, async (update, signal) => {
      update((state) => ({
        ...state,
        digests: { ...state.digests, [ind.key]: { ...state.digests[ind.key], analysisLoading: true, analysisErr: undefined } },
      }));
      try {
        let acc = "";
        let timeoutId: ReturnType<typeof setTimeout> | undefined;
        try {
          const timeout = new Promise<never>((_, reject) => {
            timeoutId = setTimeout(() => reject(new Error("请求超时（120 秒），请检查网络或 AI 服务状态")), 120_000);
          });
          const result = await Promise.race([
            chatStream([{ role: "user", content: prompt }], `${ind.name}深入分析`, {
              onDelta: (t) => {
                acc += t;
                update((state) => ({
                  ...state,
                  digests: { ...state.digests, [ind.key]: { ...state.digests[ind.key], analysis: acc, analysisLoading: true } },
                }));
              },
            }, signal),
            timeout,
          ]);
          const analysis = (acc || result.content).trim();
          if (!analysis) throw new Error("AI 返回了空内容，请检查模型配置后重试");
          update((state) => ({
            ...state,
            digests: { ...state.digests, [ind.key]: { ...state.digests[ind.key], analysis, analysisLoading: false } },
          }));
        } finally {
          if (timeoutId) clearTimeout(timeoutId);
        }
      } catch (e) {
        const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : "分析失败";
        update((state) => ({
          ...state,
          digests: { ...state.digests, [ind.key]: { ...state.digests[ind.key], analysisLoading: false, analysisErr: msg } },
        }));
      }
    });
  };

  // 一键提炼全部赛道要点（串行，带进度；单赛道按需的按钮仍保留）
  const genAll = (override?: RadarData) => {
    if (intelTask.status === "running") return;
    if (!hasLlm()) {
      if (cur) updateBackgroundTask<IntelTaskData>(INTEL_TASK_KEY, { digests, bulk }, (state) => ({
        ...state, digests: { ...state.digests, [cur.key]: { needKey: true } },
      }));
      return;
    }
    const base = override || data;
    const targets = (base?.industries || []).filter((i) => i.items.length > 0);
    if (!targets.length) return;
    startBackgroundTask(INTEL_TASK_KEY, { digests, bulk: { running: true, done: 0, total: targets.length } }, async (update, signal) => {
      for (const ind of targets) {
        if (signal.aborted) break;
        await runDigest(ind, update, signal);
        if (signal.aborted) break;
        update((state) => ({ ...state, bulk: { ...state.bulk, done: state.bulk.done + 1 } }));
      }
      if (!signal.aborted) {
        update((state) => ({ ...state, bulk: { ...state.bulk, running: false } }));
      }
    });
  };

  const dg = cur ? digests[cur.key] : undefined;

  // 渲染要点文本：先把 [^N] 提取成可点击来源 chip，正文用 ReactMarkdown 渲染剩余部分。
  // 每个 <li> 单独处理（md 按行拆），普通段落（## 国内 / ## 国外 标题）原样渲染。
  const DigestBody = ({ text, items }: { text: string; items: Industry["items"] }) => {
    const lines = text.split("\n");
    return (
      <div className="space-y-0.5 text-sm leading-snug text-foreground">
        {lines.map((line, li) => {
          const refs = Array.from(line.matchAll(/\[\^(\d+)\]/g)).map((m) => Number(m[1]));
          const clean = line.replace(/\s*\[\^\d+\]/g, "").trim();
          if (!clean.trim()) return null;
          const chips = refs.filter((n) => items[n]).map((n) => (
            <a key={n} href={items[n].url} target="_blank" rel="noreferrer"
              title={`${items[n].source}｜${items[n].zh || items[n].title}`}
              className="ml-1 inline-flex items-center gap-0.5 rounded bg-primary/10 px-1 py-0.5 text-[10px] leading-none text-primary no-underline hover:bg-primary/25">
              {items[n].source}<ExternalLink className="h-2.5 w-2.5" />
            </a>
          ));
          const heading = clean.match(/^##\s+(.+)$/);
          if (heading) {
            return (
              <div key={li} className={cn("text-xs font-semibold text-primary/90", li > 0 && "pt-1.5")}>
                {heading[1]}
              </div>
            );
          }
          const bullet = clean.replace(/^[-*]\s+/, "");
          return (
            <div key={li} className="flex items-start gap-1.5">
              {/^[-*]\s+/.test(clean) && <span className="mt-[0.42em] h-1 w-1 shrink-0 rounded-full bg-primary/70" />}
              <span className="min-w-0 flex-1">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ p: ({ children }) => <span>{children}</span> }}>
                  {bullet}
                </ReactMarkdown>
                {chips}
              </span>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">
          {hasData ? `${data!.stats.total_sources} 个公开源 · 近 ${data!.recent_days} 天 · 更新于 ${data!.generated_at}` : "12 赛道 · 108 个公开源"}
        </span>
        <div className="flex items-center gap-2">
          {hasData && (
            <button onClick={() => genAll()} disabled={bulk.running || refreshing}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-3 py-1.5 text-sm font-medium text-primary shadow-glow hover:bg-primary/25 disabled:opacity-50">
              {bulk.running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              {bulk.running ? `提炼中 ${bulk.done}/${bulk.total}` : "一键提炼全部要点"}
            </button>
          )}
          <button onClick={refresh} disabled={refreshing || bulk.running}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50">
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            {refreshing ? "抓取中…" : "刷新"}
          </button>
        </div>
      </div>

      {err && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" /> {err}
        </div>
      )}

      {!hasData && !err ? (
        <div className="rounded-lg border border-dashed border-border/70 p-8 text-center text-sm text-muted-foreground/70">
          还没有抓取资讯，点上方<b className="text-foreground">「刷新」</b>拉取（约 20-40 秒）。
        </div>
      ) : (
        <>
          {/* 赛道筛选 —— 暖橙边框 pill */}
          <div className="mb-4 flex flex-wrap gap-2">
            {industries.map((ind) => (
              <button key={ind.key} onClick={() => setActive(ind.key)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors",
                  active === ind.key
                    ? "border-primary bg-primary/15 font-medium text-primary shadow-glow"
                    : "border-primary/25 text-muted-foreground hover:border-primary/60 hover:text-foreground",
                )}>
                <span className="h-2 w-2 rounded-full" style={{ background: ind.accent }} />
                {ind.name}<span className="text-muted-foreground/50">{ind.items.length}</span>
              </button>
            ))}
          </div>

          {cur && (
            <>
              {/* 今日要点总结框（暖橙框） */}
              <div className="mb-3 rounded-xl border border-primary/30 bg-primary/5 p-3">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-sm font-semibold text-primary">
                    <Lightbulb className="h-4 w-4" /> 今日要点 · {cur.name}
                  </span>
                  {(dg?.text || dg?.err || dg?.needKey) && (
                    <button onClick={() => genDigest(cur)} className="text-xs text-muted-foreground hover:text-primary">重新提炼</button>
                  )}
                </div>
                {dg?.loading ? (
                  <div className="flex items-center justify-between gap-3">
                    <p className="flex items-center gap-2 text-sm text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> AI 正在读这个赛道的资讯…
                    </p>
                    <button
                      onClick={() => retryDigest(cur)}
                      className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border/60 px-2.5 py-1 text-xs text-muted-foreground hover:border-primary/50 hover:text-primary"
                      title="中止当前请求并重新提炼这个赛道"
                    >
                      <RefreshCw className="h-3 w-3" /> 重试
                    </button>
                  </div>
                ) : dg?.text ? (
                  <>
                    <DigestBody text={dg.text} items={cur.items.slice(0, 25)} />
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <SaveNoteButton kind="今日要点" title={`${cur.name} 今日要点`} content={dg.text} />
                      <button onClick={() => genAnalysis(cur)} disabled={dg.analysisLoading}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/20 disabled:opacity-50">
                        {dg.analysisLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <TrendingUp className="h-3.5 w-3.5" />}
                        {dg.analysis ? "重新深入分析" : "深入分析"}
                      </button>
                    </div>
                    {dg.analysisLoading && !dg.analysis && (
                      <p className="mt-2 flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="h-3 w-3 animate-spin" /> 正在逐条分析影响…</p>
                    )}
                    {dg.analysisErr && (
                      <div className="mt-2 flex items-center gap-3">
                        <p className="flex-1 text-xs text-destructive">{dg.analysisErr}</p>
                        <button onClick={() => genAnalysis(cur)}
                          className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border/60 px-2.5 py-1 text-xs text-muted-foreground hover:border-primary/50 hover:text-primary">
                          <RefreshCw className="h-3 w-3" /> 重试
                        </button>
                      </div>
                    )}
                    {dg.analysis && (
                      <div className="mt-3 rounded-lg border border-border/60 bg-background/40 p-3">
                        <p className="mb-1.5 text-xs font-semibold text-muted-foreground">深入分析 · 影响与投资视角</p>
                        <DigestBody text={dg.analysis} items={cur.items.slice(0, 25)} />
                        <div className="mt-2"><SaveNoteButton kind="深入分析" title={`${cur.name} 深入分析`} content={dg.analysis} /></div>
                      </div>
                    )}
                  </>
                ) : dg?.needKey ? (
                  <p className="text-sm text-muted-foreground">还没接入 AI。<Link to="/settings" className="text-primary">先接入你的 AI</Link>，即可一键提炼本赛道今日要点。</p>
                ) : dg?.err ? (
                  <div className="flex items-center gap-3">
                    <p className="flex-1 text-sm text-destructive">{dg.err}</p>
                    <button onClick={() => genDigest(cur)}
                      className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border/60 px-2.5 py-1 text-xs text-muted-foreground hover:border-primary/50 hover:text-primary">
                      <RefreshCw className="h-3 w-3" /> 重试
                    </button>
                  </div>
                ) : (
                  <button onClick={() => genDigest(cur)}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-3 py-1.5 text-sm font-medium text-primary hover:bg-primary/25">
                    <Sparkles className="h-4 w-4" /> 让 AI 提炼今日要点
                  </button>
                )}
              </div>

              {/* 资讯列表 */}
              <div className="space-y-2">
                {cur.items.length === 0 ? (
                  <p className="py-6 text-center text-sm text-muted-foreground/60">近 {data!.recent_days} 天该赛道暂无更新</p>
                ) : (
                  cur.items.map((it, i) => (
                    <a key={i} href={it.url} target="_blank" rel="noreferrer"
                      className="group flex items-baseline gap-3 border-b border-border/30 pb-2 text-sm last:border-0">
                      <span className="w-24 shrink-0 font-mono text-xs text-muted-foreground/70">{it.time}</span>
                      <span className="w-20 shrink-0 truncate text-xs text-muted-foreground">{it.source}</span>
                      <span className="flex-1 group-hover:text-primary">{it.zh || it.title}</span>
                      <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground/0 group-hover:text-primary/60" />
                    </a>
                  ))
                )}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

// 关注股公告 / 新闻聚合：从本地关注列表取代码，复用个股接口批量拉取、按时间倒序合并。
// 只做公开信息聚合，标的均为用户自己关注列表里的，不预置、不推荐。
interface FeedRow { code: string; name: string; when: string; title: string; meta?: string; url?: string }
const MAX_ROWS = 60;

function WatchlistFeed({ kind }: { kind: "filings" | "news" }) {
  const [codes, setCodes] = useState<string[]>(loadWatch);
  const [rows, setRows] = useState<FeedRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [depNote, setDepNote] = useState<string | null>(null);

  const load = useCallback(async (cs: string[]) => {
    if (!cs.length) { setRows([]); return; }
    setLoading(true); setErr(null); setDepNote(null);
    try {
      // 股名（一次批量），失败则退回显示代码
      const nameOf: Record<string, string> = {};
      try {
        const quotes = await api.quote(cs.join(","));
        for (const c of cs) if (quotes[c]?.name) nameOf[c] = quotes[c].name;
      } catch { /* 忽略：无股名不影响公告/新闻 */ }

      const out: FeedRow[] = [];
      if (kind === "filings") {
        const res = await Promise.all(
          cs.map((c) => api.announcements(c).then((a) => ({ c, a })).catch(() => ({ c, a: [] as Announcement[] }))),
        );
        for (const { c, a } of res)
          for (const x of a)
            out.push({ code: c, name: nameOf[c] || c, when: x.date, title: x.title.replace(/^[^:：]*[:：]/, ""), meta: x.type, url: x.url });
      } else {
        let dep: string | null = null;
        const res = await Promise.all(
          cs.map((c) =>
            api.news(c).then((n) => ({ c, n })).catch((e) => {
              if (e instanceof ApiError && e.status === 501) dep = e.message;
              return { c, n: [] as NewsItem[] };
            }),
          ),
        );
        for (const { c, n } of res)
          for (const x of n)
            out.push({ code: c, name: nameOf[c] || c, when: x.发布时间 || "", title: x.新闻标题 || "", url: x.新闻链接 });
        if (dep && out.length === 0) setDepNote(dep);
      }
      // 按真实时间倒序：多新闻源的时间字符串格式不统一（有无秒/斜杠日期），字典序会排乱
      const ts = (s: string) => {
        const raw = (s || "").trim();
        let t = Date.parse(raw);
        if (Number.isNaN(t)) t = Date.parse(raw.replace(" ", "T"));
        return Number.isNaN(t) ? 0 : t;
      };
      out.sort((p, q) => ts(q.when) - ts(p.when));
      setRows(out.slice(0, MAX_ROWS));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [kind]);

  useEffect(() => { const cs = loadWatch(); setCodes(cs); load(cs); }, [load]);

  const refresh = () => { const cs = loadWatch(); setCodes(cs); load(cs); };

  if (!codes.length) {
    return (
      <div className="rounded-lg border border-dashed border-border/70 p-8 text-center text-sm text-muted-foreground/70">
        还没有关注股票。到<Link to="/daily-review" className="text-primary">「市场全景」</Link>加自选（6 位代码），这里会汇总它们的{kind === "filings" ? "公告" : "新闻"}。
      </div>
    );
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Star className="h-3.5 w-3.5 text-primary/70" /> 关注 {codes.length} 只 · 共 {rows.length} 条{kind === "filings" ? "公告" : "新闻"}（近期）
        </span>
        <button onClick={refresh} disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {loading ? "拉取中…" : "刷新"}
        </button>
      </div>

      {err && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" /> {err}
        </div>
      )}

      {depNote ? (
        <p className="py-6 text-center text-xs text-warning">{depNote}（安装后新闻即可用）</p>
      ) : loading && rows.length === 0 ? (
        <p className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> 正在汇总关注股的{kind === "filings" ? "公告" : "新闻"}…</p>
      ) : rows.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground/60">关注列表里的个股近期暂无{kind === "filings" ? "公告" : "新闻"}。</p>
      ) : (
        <div className="space-y-2">
          {rows.map((r, i) => (
            <a key={i} href={r.url || undefined} target={r.url ? "_blank" : undefined} rel="noreferrer"
              className={cn("group flex items-baseline gap-3 border-b border-border/30 pb-2 text-sm last:border-0", r.url && "cursor-pointer")}>
              <span className="w-20 shrink-0 font-mono text-xs text-muted-foreground/70">{(r.when || "").slice(kind === "filings" ? 0 : 5, kind === "filings" ? 10 : 16)}</span>
              <span className="w-16 shrink-0 truncate text-xs text-primary/90" title={r.code}>{r.name}</span>
              {kind === "filings" && r.meta && <span className="hidden w-20 shrink-0 truncate text-xs text-muted-foreground sm:block">{r.meta}</span>}
              <span className="flex-1 group-hover:text-primary">{r.title}</span>
              {r.url && <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground/0 group-hover:text-primary/60" />}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

export function Intel() {
  const [tab, setTab] = useState("investment-news");
  const cur = TABS.find((t) => t.key === tab)!;

  return (
    <div>
      <PageHeader title="资讯" subtitle="多来源资讯中心：AI 帮你跨源捞资讯、提炼要点" />

      <div className="mb-4 flex flex-wrap gap-2">
        {TABS.map(({ key, label, icon: Icon, integrated }) => (
          <button key={key} onClick={() => setTab(key)}
            className={cn("inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors",
              tab === key ? "bg-primary/15 font-medium text-primary shadow-glow" : "text-muted-foreground hover:bg-muted/50")}>
            <Icon className="h-4 w-4" /> {label}
            {integrated && <span className="rounded-full bg-primary/20 px-1.5 py-0.5 text-[9px] font-medium text-primary">集成</span>}
          </button>
        ))}
      </div>

      <GlassCard glow>
        <div className="mb-3 flex items-center gap-2">
          <cur.icon className="h-5 w-5 text-primary" />
          <h3 className="font-semibold">{cur.label}</h3>
          {cur.integrated && <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] text-primary">investment-news</span>}
        </div>
        {cur.key === "events" ? (
          <WatchlistEventJudgement />
        ) : cur.key === "investment-news" ? (
          <InvestmentNewsPanel />
        ) : cur.key === "filings" ? (
          <WatchlistFeed kind="filings" />
        ) : cur.key === "news" ? (
          <WatchlistFeed kind="news" />
        ) : null}
      </GlassCard>

      <p className="mt-3 text-[11px] text-muted-foreground/60">
        公告与新闻来自自选标的公开信息；Investment News 按关联板块筛选，重大事件按需联网核验。
      </p>
    </div>
  );
}
