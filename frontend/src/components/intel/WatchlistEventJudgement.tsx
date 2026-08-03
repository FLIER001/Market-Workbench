import { useState, type ReactNode } from "react";
import { AlertCircle, ChevronDown, ExternalLink, Loader2, RefreshCw, Star } from "lucide-react";
import { Link } from "react-router-dom";
import { api, ApiError, type Blocks, type Quote, type RadarData } from "@/lib/api";
import { backgroundTaskKey, startBackgroundTask, useBackgroundTask } from "@/lib/backgroundTasks";
import { chatStream, hasLlm } from "@/lib/llm";
import { loadUser } from "@/lib/auth";
import { loadWatchGroups, type WatchCollection } from "@/lib/watchlist";
import { cn } from "@/lib/utils";
import { SaveNoteButton } from "@/components/ui/SaveNoteButton";

type AssetKind = "stock" | "etf";
type EvidenceKind = "announcement" | "news" | "investment-news" | "web-search";

interface AssetSeed {
  code: string;
  kind: AssetKind;
  group: string;
}

interface WatchedAsset extends AssetSeed {
  name: string;
  sectors: string[];
  radarKeys: string[];
}

interface EventEvidence {
  id: string;
  kind: EvidenceKind;
  time: string;
  title: string;
  source: string;
  url?: string;
  code?: string;
  asset?: string;
  sector?: string;
  summary?: string;
}

interface EventSnapshot {
  asOf: string;
  radarAsOf: string | null;
  assets: WatchedAsset[];
  sectors: string[];
  evidence: EventEvidence[];
  counts: {
    stocks: number;
    etfs: number;
    announcements: number;
    news: number;
    investmentNews: number;
    webSearch: number;
  };
  issues: string[];
}

interface EventJudgementTaskData {
  analyses: Record<string, {
    status: "pending" | "running" | "done" | "error";
    text?: string;
    error?: string;
    evidence?: EventEvidence[];
    updatedAt?: string;
  }>;
  progress: { done: number; total: number };
  phase?: "refreshing" | "analyzing" | "done";
  activeAssetKey?: string;
  needKey?: boolean;
  snapshot?: EventSnapshot;
  text?: string; // 兼容旧版综合研判缓存；新版本不再使用。
}

const EMPTY_TASK: EventJudgementTaskData = {
  analyses: {},
  progress: { done: 0, total: 0 },
  phase: "done",
};
const MAX_ASSET_EVIDENCE = 6;

const RADAR_RULES: Record<string, string[]> = {
  ai: ["人工智能", "大模型", "算力", "云计算", "数据中心", "aigc", "ai"],
  semi: ["半导体", "芯片", "集成电路", "光刻", "存储", "封测"],
  robot: ["机器人", "自动化", "工业母机", "机械设备"],
  auto: ["汽车", "新能源车", "智能驾驶", "锂电", "充电桩"],
  energy: ["能源", "新能源", "光伏", "风电", "储能", "电力", "煤炭", "石油", "天然气"],
  bio: ["医药", "医疗", "生物", "创新药", "cro", "健康"],
  space: ["航天", "航空", "卫星", "军工", "低空"],
  security: ["网络安全", "信息安全", "信创"],
  tech: ["互联网", "软件", "计算机", "通信", "5g", "云服务", "传媒"],
  consumer: ["消费电子", "电子", "数码", "家电", "苹果产业链"],
  science: ["科研", "量子", "新材料", "前沿"],
};

const KIND_LABEL: Record<EvidenceKind, string> = {
  announcement: "公告",
  news: "个股新闻",
  "investment-news": "Investment News",
  "web-search": "联网核验",
};

const MAJOR_EVENT_PATTERN =
  /重大|并购|收购|重组|控制权|立案|调查|处罚|诉讼|仲裁|停产|事故|召回|制裁|禁令|获批|批准|中标|签订.{0,8}(合同|协议)|业绩预告|盈利预警|破产|退市|大额减持|大额增持|回购|分红方案|关税|政策.{0,6}(发布|调整)|merger|acquisition|investigation|sanction|recall|bankruptcy/i;

const oneParagraph = (value: string) =>
  value
    .replace(/```(?:markdown|md|text)?/gi, "")
    .replace(/```/g, "")
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/^\s*(?:[-*+]|\d+[.)])\s*/gm, "")
    .replace(/\*\*/g, "")
    .replace(/\s*\n+\s*/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();

const renderCitedParagraph = (value: string, evidence: EventEvidence[]): ReactNode[] => {
  const byId = new Map(evidence.map((item) => [item.id.toUpperCase(), item]));
  return oneParagraph(value)
    .split(/(\[[ANIW]\d+\])/gi)
    .filter(Boolean)
    .map((part, index) => {
      const match = part.match(/^\[([ANIW]\d+)\]$/i);
      if (!match) return part;
      const item = byId.get(match[1].toUpperCase());
      if (!item) return part;
      const title = `${KIND_LABEL[item.kind]}｜${item.source}｜${item.title}`;
      if (!item.url) {
        return (
          <span key={`${item.id}-${index}`} title={`${title}（暂无来源链接）`} className="font-mono text-primary">
            [{item.id}]
          </span>
        );
      }
      return (
        <a
          key={`${item.id}-${index}`}
          href={item.url}
          target="_blank"
          rel="noreferrer"
          title={title}
          aria-label={`打开来源：${item.title}`}
          className="mx-0.5 inline-flex items-center gap-0.5 rounded px-0.5 font-mono text-primary underline decoration-primary/35 underline-offset-2 hover:bg-primary/10 hover:decoration-primary"
        >
          [{item.id}]
          <ExternalLink className="h-2.5 w-2.5" />
        </a>
      );
    });
};

const loadAssetSeeds = (): AssetSeed[] => {
  const out: AssetSeed[] = [];
  const seen = new Set<string>();
  for (const kind of ["stock", "etf"] as WatchCollection[]) {
    for (const group of loadWatchGroups(kind)) {
      for (const code of group.codes) {
        const key = `${kind}:${code}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ code, kind, group: group.name });
      }
    }
  }
  return out;
};

const keywordMatches = (text: string, keyword: string) => {
  const lower = text.toLowerCase();
  const target = keyword.toLowerCase();
  if (/^[a-z0-9]+$/.test(target) && target.length <= 3) {
    return new RegExp(`(^|[^a-z0-9])${target.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}([^a-z0-9]|$)`, "i").test(lower);
  }
  return lower.includes(target);
};

const radarKeysFor = (text: string) => {
  const matched = Object.entries(RADAR_RULES)
    .filter(([, keywords]) => keywords.some((keyword) => keywordMatches(text, keyword)))
    .map(([key]) => key);
  return Array.from(new Set([...matched, "macro"]));
};

const etfTheme = (name: string) => {
  const beforeEtf = name.split(/ETF/i)[0]?.trim() || "";
  return beforeEtf.length >= 2 ? beforeEtf : "";
};

const parseTime = (value: string) => {
  const raw = (value || "").trim();
  let timestamp = Date.parse(raw);
  if (Number.isNaN(timestamp)) timestamp = Date.parse(raw.replace(" ", "T"));
  return Number.isNaN(timestamp) ? 0 : timestamp;
};

const uniqueEvidence = (items: Omit<EventEvidence, "id">[], limit: number) => {
  const seen = new Set<string>();
  const out: Omit<EventEvidence, "id">[] = [];
  for (const item of items) {
    const key = `${item.kind}:${item.title.trim().toLowerCase()}`;
    if (!item.title.trim() || seen.has(key)) continue;
    seen.add(key);
    out.push(item);
    if (out.length >= limit) break;
  }
  return out;
};

const radarWithFallback = async (): Promise<{ data: RadarData; stale: boolean }> => {
  try {
    return { data: await api.radarRefresh(), stale: false };
  } catch {
    return { data: await api.radar(), stale: true };
  }
};

async function gatherSnapshot(seeds: AssetSeed[]): Promise<EventSnapshot> {
  const codes = Array.from(new Set(seeds.map((asset) => asset.code)));
  const issues: string[] = [];

  const quotePromise = api.quote(codes.join(",")).catch(() => {
    issues.push("部分标的名称未能刷新");
    return {} as Record<string, Quote>;
  });
  const blockPromise = Promise.all(
    seeds.map((asset) =>
      api.blocks(asset.code)
        .then((blocks) => ({ key: `${asset.kind}:${asset.code}`, blocks }))
        .catch(() => ({ key: `${asset.kind}:${asset.code}`, blocks: null as Blocks | null })),
    ),
  );
  const announcementPromise = Promise.all(
    seeds.map((asset) =>
      api.announcements(asset.code)
        .then((items) => ({ asset, items }))
        .catch(() => ({ asset, items: [] })),
    ),
  );
  let newsUnavailable = false;
  const newsPromise = Promise.all(
    seeds.map((asset) =>
      api.news(asset.code)
        .then((items) => ({ asset, items }))
        .catch((error) => {
          if (error instanceof ApiError && error.status === 501) newsUnavailable = true;
          return { asset, items: [] };
        }),
    ),
  );

  const [quotes, blockRows, announcementRows, newsRows, radarResult] = await Promise.all([
    quotePromise,
    blockPromise,
    announcementPromise,
    newsPromise,
    radarWithFallback(),
  ]);
  if (newsUnavailable) issues.push("公开新闻数据源当前不可用");
  if (radarResult.stale) issues.push("Investment News 刷新失败，使用最近缓存");

  const blockByAsset = new Map(blockRows.map((row) => [row.key, row.blocks]));
  const radarNameByKey = new Map(radarResult.data.industries.map((industry) => [industry.key, industry.name]));
  const assets: WatchedAsset[] = seeds.map((seed) => {
    const quote = quotes[seed.code];
    const boards = blockByAsset.get(`${seed.kind}:${seed.code}`)?.boards || [];
    const boardNames = boards.map((board) => board.name).filter(Boolean).slice(0, 6);
    const groupHint = seed.group === "未分组" ? "" : seed.group;
    const fundTheme = seed.kind === "etf" ? etfTheme(quote?.name || "") : "";
    const matchText = [quote?.name || seed.code, groupHint, fundTheme, ...boardNames].join(" ");
    const radarKeys = radarKeysFor(matchText);
    const coarseSectors = radarKeys
      .filter((key) => key !== "macro")
      .map((key) => radarNameByKey.get(key))
      .filter((name): name is string => !!name);
    const sectors = Array.from(new Set([groupHint, fundTheme, ...boardNames, ...coarseSectors].filter(Boolean))).slice(0, 8);
    return {
      ...seed,
      name: quote?.name || seed.code,
      sectors,
      radarKeys,
    };
  });
  const assetByKey = new Map(assets.map((asset) => [`${asset.kind}:${asset.code}`, asset]));

  const announcements = uniqueEvidence(
    announcementRows
      .flatMap(({ asset, items }) => {
        const resolved = assetByKey.get(`${asset.kind}:${asset.code}`)!;
        return items.slice(0, MAX_ASSET_EVIDENCE).map((item) => ({
          kind: "announcement" as const,
          time: item.date || "",
          title: item.title.replace(/^[^:：]*[:：]/, ""),
          source: item.type || "公司公告",
          url: item.url,
          code: asset.code,
          asset: resolved.name,
          sector: resolved.sectors.slice(0, 3).join(" / "),
        }));
      })
      .sort((a, b) => parseTime(b.time) - parseTime(a.time)),
    24,
  );

  const publicNews = uniqueEvidence(
    newsRows
      .flatMap(({ asset, items }) => {
        const resolved = assetByKey.get(`${asset.kind}:${asset.code}`)!;
        return items.slice(0, MAX_ASSET_EVIDENCE).map((item) => ({
          kind: "news" as const,
          time: item.发布时间 || "",
          title: item.新闻标题 || "",
          source: item.文章来源 || "公开新闻",
          url: item.新闻链接,
          code: asset.code,
          asset: resolved.name,
          sector: resolved.sectors.slice(0, 3).join(" / "),
        }));
      })
      .sort((a, b) => parseTime(b.time) - parseTime(a.time)),
    30,
  );

  const wantedRadarKeys = new Set(assets.flatMap((asset) => asset.radarKeys));
  wantedRadarKeys.add("macro");
  const investmentNews = uniqueEvidence(
    radarResult.data.industries
      .filter((industry) => wantedRadarKeys.has(industry.key))
      .flatMap((industry) =>
        industry.items.slice(0, industry.key === "macro" ? 6 : 8).map((item) => ({
          kind: "investment-news" as const,
          time: item.time || "",
          title: item.zh || item.title,
          source: item.source,
          url: item.url,
          sector: industry.name,
          summary: item.summary,
        })),
      ),
    36,
  );

  const majorCandidates = assets
    .map((asset) => {
      const relatedSectorNews = investmentNews.filter((item) =>
        item.sector === "财经 / 宏观" || (!!item.sector && asset.sectors.includes(item.sector)),
      );
      const direct = [...announcements, ...publicNews].filter((item) => item.code === asset.code);
      const pool = asset.kind === "stock"
        ? [...direct, ...relatedSectorNews]
        : [...direct.filter((item) => item.kind === "news"), ...relatedSectorNews];
      const trigger = pool.find((item) => MAJOR_EVENT_PATTERN.test(item.title));
      return trigger ? { asset, trigger } : null;
    })
    .filter((item): item is { asset: WatchedAsset; trigger: Omit<EventEvidence, "id"> } => !!item);
  if (majorCandidates.length > 6) {
    issues.push(`重大事件较多，本次优先联网核验前 6 个标的`);
  }
  let webSearchFailed = false;
  const webSearchRows = await Promise.all(
    majorCandidates.slice(0, 6).map(async ({ asset, trigger }) => {
      try {
        const query = `${asset.name} ${trigger.title}`.slice(0, 160);
        const data = await api.publicNewsSearch(query, 5);
        return data.results.map((item) => ({
          kind: "web-search" as const,
          time: item.published,
          title: item.title,
          source: item.source,
          url: item.url,
          code: asset.code,
          asset: asset.name,
          sector: asset.sectors.slice(0, 3).join(" / "),
          summary: item.snippet,
        }));
      } catch {
        webSearchFailed = true;
        return [];
      }
    }),
  );
  if (webSearchFailed) issues.push("部分重大事件的联网补充核验失败");
  const webSearch = uniqueEvidence(webSearchRows.flat(), 30);

  const evidence: EventEvidence[] = [
    ...announcements.map((item, index) => ({ ...item, id: `A${index + 1}` })),
    ...publicNews.map((item, index) => ({ ...item, id: `N${index + 1}` })),
    ...investmentNews.map((item, index) => ({ ...item, id: `I${index + 1}` })),
    ...webSearch.map((item, index) => ({ ...item, id: `W${index + 1}` })),
  ];
  const sectors = Array.from(new Set(assets.flatMap((asset) => asset.sectors))).slice(0, 24);
  const unresolved = assets.filter((asset) => asset.sectors.length === 0).length;
  if (unresolved > 0) issues.push(`${unresolved} 个标的未识别到明确板块`);

  return {
    asOf: new Date().toLocaleString("zh-CN", { hour12: false }),
    radarAsOf: radarResult.data.generated_at,
    assets,
    sectors,
    evidence,
    counts: {
      stocks: assets.filter((asset) => asset.kind === "stock").length,
      etfs: assets.filter((asset) => asset.kind === "etf").length,
      announcements: announcements.length,
      news: publicNews.length,
      investmentNews: investmentNews.length,
      webSearch: webSearch.length,
    },
    issues,
  };
}

const assetKey = (asset: WatchedAsset) => `${asset.kind}:${asset.code}`;

const evidenceForAsset = (snapshot: EventSnapshot, asset: WatchedAsset) => {
  const direct = snapshot.evidence.filter((item) => item.code === asset.code);
  const sector = snapshot.evidence.filter((item) =>
    item.kind === "investment-news" &&
    (item.sector === "财经 / 宏观" || (!!item.sector && asset.sectors.includes(item.sector))),
  );
  const groups = [
    direct.filter((item) => item.kind === "web-search"),
    direct.filter((item) => item.kind !== "web-search" && MAJOR_EVENT_PATTERN.test(item.title)),
    direct.filter((item) => item.kind === "announcement"),
    direct.filter((item) => item.kind === "news"),
    sector.filter((item) => MAJOR_EVENT_PATTERN.test(item.title)),
    sector,
  ];
  const selected: EventEvidence[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const item of group) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      selected.push(item);
      if (selected.length >= 24) return selected;
    }
  }
  return selected;
};

const buildAssetPrompt = (snapshot: EventSnapshot, asset: WatchedAsset) => {
  const evidence = evidenceForAsset(snapshot, asset)
    .map((item) =>
      `[${item.id}] [${KIND_LABEL[item.kind]}] [${item.time || "时间未知"}] ` +
      `${item.asset ? `${item.asset}（${item.code}）｜` : ""}` +
      `${item.sector ? `${item.sector}｜` : ""}${item.source}｜${item.title}` +
      `${item.summary ? `｜${item.summary}` : ""}`,
    )
    .join("\n");
  const issues = snapshot.issues.length ? snapshot.issues.join("；") : "无";
  const subjectRule = asset.kind === "stock"
    ? "研判对象是这家公司本身；板块资讯只能作为经营环境或传导背景。"
    : "研判对象是该 ETF 对应的板块，不研判基金净值或价格；ETF 常规申赎、份额和基金运作公告只作低权重背景。";

  return `请为以下单一标的生成独立的「事件研判」：
- 类型：${asset.kind === "stock" ? "个股" : "ETF"}
- 标的：${asset.name}（${asset.code}）
- 自选分组：${asset.group}
- 对应板块：${asset.sectors.join("、") || "未识别"}

${subjectRule}
你的任务是形成观点，不是整理材料。先在内部完成“剔除常规噪音 → 按影响程度、可信度和时效性排序 → 只选唯一主线 → 重大事件必要时联网交叉核验 → 判断影响机制与失效条件”，不要输出这个过程。只抓住一个最影响未来 30 天判断的核心矛盾或重大事件，其他一般事件不要逐项分析。若现有证据指向真正重大的变化但外部核验仍不足，可以调用 search_public_news 联网补查一次；普通事件禁止联网搜索。公司公告权重高于媒体报道，ETF 常规基金运作公告只作低权重背景，Investment News 只用于板块与海外传导。

输出必须严格为一段连续中文，约 180-300 字：第一句直接给出明确判断，后面说明最关键的逻辑和不确定性，并自然引用 2-4 个最有力的证据编号（如 [A1][N2][W1]），优先引用带原文链接的证据。不要标题、列表、分点、表格、固定栏目或逐条复述证据；不要面面俱到。若没有足以形成事件观点的材料，就明确判断“当前没有强事件主线”，并说清什么变化会改变该判断。不给买卖建议，不评价目标价，不预测股价。

数据限制：${issues}

证据：
${evidence || "没有可用证据"}`;
};

const generateAssetJudgement = async (
  snapshot: EventSnapshot,
  asset: WatchedAsset,
  signal: AbortSignal,
) => {
  let text = "";
  const result = await chatStream(
    [{ role: "user", content: buildAssetPrompt(snapshot, asset) }],
    asset.kind === "stock"
      ? `针对个股 ${asset.name}（${asset.code}）进行事件研判。`
      : `针对 ETF ${asset.name}（${asset.code}）对应板块进行事件研判。`,
    { onDelta: (delta) => { text += delta; } },
    signal,
  );
  const finalText = oneParagraph(text || result.content);
  if (!finalText) throw new Error("AI 返回了空内容");
  return finalText;
};

function AssetAccordion({
  asset,
  analysis,
  evidence,
  refreshing,
  disabled,
  onRefresh,
  taskDone,
}: {
  asset: WatchedAsset;
  analysis?: EventJudgementTaskData["analyses"][string];
  evidence: EventEvidence[];
  refreshing: boolean;
  disabled: boolean;
  onRefresh: (asset: WatchedAsset) => void;
  taskDone: boolean;
}) {
  const status = analysis?.status;
  return (
    <details className="group overflow-hidden rounded-xl border border-border/55 bg-muted/10">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-3.5 py-3 hover:bg-muted/25">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{asset.name}</span>
            <span className="font-mono text-[11px] text-muted-foreground">{asset.code}</span>
            {asset.kind === "etf" && (
              <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">ETF</span>
            )}
          </div>
          {asset.kind === "etf" ? (
            <p className="mt-1 truncate text-[11px] text-muted-foreground">
              对应板块：{asset.sectors.slice(0, 4).join(" · ") || "未识别"}
            </p>
          ) : asset.group !== "未分组" ? (
            <p className="mt-1 text-[11px] text-muted-foreground">分组：{asset.group}</p>
          ) : null}
          {analysis?.updatedAt && (
            <p className="mt-1 text-[10px] text-muted-foreground/70">更新于 {analysis.updatedAt}</p>
          )}
        </div>
        <button
          type="button"
          disabled={disabled}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onRefresh(asset);
          }}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border/60 px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:bg-primary/10 hover:text-primary disabled:cursor-not-allowed disabled:opacity-45"
          title={`只刷新 ${asset.name} 的研判`}
        >
          {refreshing ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          刷新研判
        </button>
        <span className={cn(
          "shrink-0 text-[11px]",
          status === "done" ? "text-success" :
            status === "error" ? "text-destructive" :
              status === "running" ? "text-primary" : "text-muted-foreground",
        )}>
          {status === "running" ? "研判中" :
            status === "done" ? "已完成" :
              status === "error" ? "失败" :
                taskDone ? "暂无研判" : "等待研判"}
        </span>
        <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" />
      </summary>
      <div className="border-t border-border/45 px-4 py-4">
        {status === "error" && analysis?.error && (
          <p className="mb-2 text-xs text-destructive">{analysis.error}；已保留上次成功结果。</p>
        )}
        {analysis?.text ? (
          <>
            <p className="text-sm leading-7 text-foreground">
              {renderCitedParagraph(analysis.text, evidence)}
            </p>
            {status === "done" && (
              <div className="mt-3 border-t border-border/40 pt-2">
                <SaveNoteButton
                  kind="事件研判"
                  title={`${asset.name}事件研判`}
                  content={analysis.text}
                />
              </div>
            )}
          </>
        ) : status === "running" ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin text-primary" /> AI 正在研判…
          </p>
        ) : status === "error" ? (
          <p className="text-sm text-destructive">{analysis?.error || "研判失败"}</p>
        ) : (
          <p className="text-sm text-muted-foreground">点击上方“刷新研判”生成该标的分析。</p>
        )}
      </div>
    </details>
  );
}

export function WatchlistEventJudgement() {
  const initialSeeds = loadAssetSeeds();
  const [watchCounts, setWatchCounts] = useState({
    stocks: initialSeeds.filter((asset) => asset.kind === "stock").length,
    etfs: initialSeeds.filter((asset) => asset.kind === "etf").length,
  });
  const [localError, setLocalError] = useState<string | null>(null);
  const owner = loadUser()?.username || "local";
  const taskKey = backgroundTaskKey("watchlist-event-judgement", owner);
  const task = useBackgroundTask<EventJudgementTaskData>(taskKey, EMPTY_TASK);
  const running = task.status === "running";
  const snapshot = task.data.snapshot;
  const analyses = task.data.analyses || {};
  const progress = task.data.progress || { done: 0, total: snapshot?.assets.length || 0 };
  const phase = task.data.phase || (running ? "analyzing" : "done");
  const activeAssetKey = task.data.activeAssetKey;

  const analyseOne = async (
    sourceSnapshot: EventSnapshot,
    asset: WatchedAsset,
    previous: EventJudgementTaskData["analyses"][string] | undefined,
    update: (updater: (data: EventJudgementTaskData) => EventJudgementTaskData) => void,
    signal: AbortSignal,
  ) => {
    const key = assetKey(asset);
    update((data) => ({
      ...data,
      analyses: {
        ...data.analyses,
        [key]: { ...previous, status: "running", error: undefined },
      },
    }));
    try {
      const finalText = await generateAssetJudgement(sourceSnapshot, asset, signal);
      update((data) => ({
        ...data,
        analyses: {
          ...data.analyses,
          [key]: {
            status: "done",
            text: finalText,
            evidence: evidenceForAsset(sourceSnapshot, asset),
            updatedAt: new Date().toLocaleString("zh-CN", { hour12: false }),
          },
        },
        progress: { ...data.progress, done: data.progress.done + 1 },
      }));
    } catch (error) {
      const message = error instanceof ApiError
        ? error.message
        : error instanceof Error ? error.message : "研判失败";
      update((data) => ({
        ...data,
        analyses: {
          ...data.analyses,
          [key]: { ...previous, status: "error", error: message },
        },
        progress: { ...data.progress, done: data.progress.done + 1 },
      }));
    }
  };

  const refreshJudgement = () => {
    if (running) return;
    const seeds = loadAssetSeeds();
    const counts = {
      stocks: seeds.filter((asset) => asset.kind === "stock").length,
      etfs: seeds.filter((asset) => asset.kind === "etf").length,
    };
    setWatchCounts(counts);
    setLocalError(null);
    if (!seeds.length) {
      setLocalError("自选股和自选 ETF 均为空");
      return;
    }

    startBackgroundTask<EventJudgementTaskData>(
      taskKey,
      {
        analyses,
        progress: { done: 0, total: seeds.length },
        phase: "refreshing",
        activeAssetKey: undefined,
        needKey: false,
        snapshot,
      },
      async (update, signal) => {
        const nextSnapshot = await gatherSnapshot(seeds);
        const pendingAnalyses = Object.fromEntries(
          nextSnapshot.assets.map((asset) => [
            assetKey(asset),
            { ...analyses[assetKey(asset)], status: "pending" as const, error: undefined },
          ]),
        );
        const configured = hasLlm();
        update(() => ({
          analyses: pendingAnalyses,
          progress: { done: 0, total: nextSnapshot.assets.length },
          phase: configured ? "analyzing" : "done",
          activeAssetKey: undefined,
          needKey: !configured,
          snapshot: nextSnapshot,
        }));
        if (!configured) return;

        let cursor = 0;
        const worker = async () => {
          while (cursor < nextSnapshot.assets.length) {
            const index = cursor;
            cursor += 1;
            const asset = nextSnapshot.assets[index];
            await analyseOne(nextSnapshot, asset, analyses[assetKey(asset)], update, signal);
          }
        };
        await Promise.all(
          Array.from(
            { length: Math.min(2, nextSnapshot.assets.length) },
            () => worker(),
          ),
        );
        update((data) => ({ ...data, phase: "done", activeAssetKey: undefined }));
      },
    );
  };

  const refreshAsset = (asset: WatchedAsset) => {
    if (running) return;
    setLocalError(null);
    const key = assetKey(asset);
    const previous = analyses[key];
    startBackgroundTask<EventJudgementTaskData>(
      taskKey,
      {
        ...task.data,
        analyses,
        progress: { done: 0, total: 1 },
        phase: "refreshing",
        activeAssetKey: key,
        needKey: false,
      },
      async (update, signal) => {
        const freshSnapshot = await gatherSnapshot([{
          code: asset.code,
          kind: asset.kind,
          group: asset.group,
        }]);
        const refreshedAsset = freshSnapshot.assets[0];
        if (!refreshedAsset) throw new Error("未能刷新该标的数据");
        const configured = hasLlm();
        update((data) => ({
          ...data,
          phase: configured ? "analyzing" : "done",
          activeAssetKey: configured ? key : undefined,
          needKey: !configured,
          snapshot: data.snapshot || freshSnapshot,
        }));
        if (!configured) return;
        await analyseOne(freshSnapshot, refreshedAsset, previous, update, signal);
        update((data) => ({ ...data, phase: "done", activeAssetKey: undefined }));
      },
    );
  };

  const error = localError || (task.status === "error" ? task.error : null);
  const stockAssets = snapshot?.assets.filter((asset) => asset.kind === "stock") || [];
  const etfAssets = snapshot?.assets.filter((asset) => asset.kind === "etf") || [];

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1.5">
            <Star className="h-3.5 w-3.5 text-primary" />
            自选股 {watchCounts.stocks} · 自选 ETF {watchCounts.etfs}
          </span>
          {snapshot && <span>· 最近研判 {snapshot.asOf}</span>}
          {task.status === "running" && (
            <span>
              · {activeAssetKey
                ? phase === "refreshing" ? "单标的证据刷新中" : "单标的 AI 研判中"
                : phase === "refreshing" ? "刷新证据中" : `进度 ${progress.done}/${progress.total}`}
            </span>
          )}
        </div>
        <button
          onClick={refreshJudgement}
          disabled={running}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-3 py-1.5 text-sm font-medium text-primary transition-colors hover:bg-primary/25 disabled:opacity-50"
        >
          {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {activeAssetKey && running
            ? "单标的刷新中…"
            : phase === "refreshing" && running
              ? "刷新数据中…"
              : running ? "AI 研判中…" : "刷新全部研判"}
        </button>
      </div>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {task.data.needKey && (
        <div className="mb-4 rounded-lg border border-warning/30 bg-warning/5 p-3 text-sm text-muted-foreground">
          数据已经刷新。<Link to="/settings" className="text-primary">接入 AI</Link>后再次点击“刷新研判”即可生成结论。
        </div>
      )}

      {!snapshot && running ? (
        <div className="flex items-center justify-center gap-2 rounded-lg border border-border/50 py-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          正在刷新板块与多源证据…
        </div>
      ) : !snapshot && !error ? (
        <div className="rounded-lg border border-dashed border-border/70 p-8 text-center text-sm text-muted-foreground/70">
          点击“刷新研判”，汇总自选标的的板块、公告、新闻与 Investment News。
        </div>
      ) : snapshot ? (
        <>
          <div className="mb-4 grid gap-2 sm:grid-cols-3">
            <div className="rounded-lg bg-muted/25 px-3 py-2">
              <p className="text-[10px] text-muted-foreground">标的覆盖</p>
              <p className="mt-0.5 text-sm font-medium">{snapshot.counts.stocks} 股 · {snapshot.counts.etfs} ETF</p>
            </div>
            <div className="rounded-lg bg-muted/25 px-3 py-2">
              <p className="text-[10px] text-muted-foreground">标的层证据</p>
              <p className="mt-0.5 text-sm font-medium">
                {snapshot.counts.announcements} 公告 · {snapshot.counts.news} 新闻
                {snapshot.counts.webSearch > 0 ? ` · ${snapshot.counts.webSearch} 联网` : ""}
              </p>
            </div>
            <div className="rounded-lg bg-muted/25 px-3 py-2">
              <p className="text-[10px] text-muted-foreground">板块层证据</p>
              <p className="mt-0.5 text-sm font-medium">{snapshot.counts.investmentNews} 条 Investment News</p>
            </div>
          </div>

          {stockAssets.length > 0 && (
            <section className="mb-5">
              <div className="mb-2 flex items-baseline gap-2">
                <h4 className="text-sm font-semibold">个股</h4>
                <span className="text-[11px] text-muted-foreground">{stockAssets.length} 只</span>
              </div>
              <div className="space-y-2">
                {stockAssets.map((asset) => (
                  <AssetAccordion
                    key={assetKey(asset)}
                    asset={asset}
                    analysis={analyses[assetKey(asset)]}
                    evidence={analyses[assetKey(asset)]?.evidence || evidenceForAsset(snapshot, asset)}
                    refreshing={activeAssetKey === assetKey(asset)}
                    disabled={running}
                    onRefresh={refreshAsset}
                    taskDone={task.status === "done"}
                  />
                ))}
              </div>
            </section>
          )}

          {etfAssets.length > 0 && (
            <section className="mb-5">
              <div className="mb-2 flex items-baseline gap-2">
                <h4 className="text-sm font-semibold">ETF</h4>
                <span className="text-[11px] text-muted-foreground">{etfAssets.length} 只 · 按对应板块研判</span>
              </div>
              <div className="space-y-2">
                {etfAssets.map((asset) => (
                  <AssetAccordion
                    key={assetKey(asset)}
                    asset={asset}
                    analysis={analyses[assetKey(asset)]}
                    evidence={analyses[assetKey(asset)]?.evidence || evidenceForAsset(snapshot, asset)}
                    refreshing={activeAssetKey === assetKey(asset)}
                    disabled={running}
                    onRefresh={refreshAsset}
                    taskDone={task.status === "done"}
                  />
                ))}
              </div>
            </section>
          )}

          {snapshot.evidence.length > 0 && (
            <details className="mt-4 rounded-lg border border-border/50">
              <summary className="cursor-pointer px-3 py-2 text-xs text-muted-foreground">
                查看研判依据（{snapshot.evidence.length} 条）
              </summary>
              <div className="max-h-72 space-y-2 overflow-auto border-t border-border/40 p-3">
                {snapshot.evidence.map((item) => (
                  <a
                    key={item.id}
                    href={item.url || undefined}
                    target={item.url ? "_blank" : undefined}
                    rel="noreferrer"
                    className={cn(
                      "flex items-start gap-2 text-xs",
                      item.url && "group hover:text-primary",
                    )}
                  >
                    <span className="w-7 shrink-0 font-mono text-primary">[{item.id}]</span>
                    <span className="w-24 shrink-0 truncate text-muted-foreground">{KIND_LABEL[item.kind]}</span>
                    <span className="min-w-0 flex-1">
                      <b className="font-medium">{item.asset || item.sector || item.source}</b>
                      <span className="text-muted-foreground"> · {item.title}</span>
                    </span>
                    {item.url && <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground group-hover:text-primary" />}
                  </a>
                ))}
              </div>
            </details>
          )}

          {snapshot.issues.length > 0 && (
            <p className="mt-3 text-[11px] text-warning">数据提示：{snapshot.issues.join("；")}</p>
          )}
        </>
      ) : null}
    </div>
  );
}
