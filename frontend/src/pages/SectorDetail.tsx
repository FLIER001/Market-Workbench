import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Building2,
  CalendarDays,
  ChevronRight,
  ExternalLink,
  Landmark,
  Loader2,
  Network,
  Radio,
  TrendingDown,
  TrendingUp,
  Wrench,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { ChainGraph } from "@/components/sectors/ChainGraph";
import { resolveRefreshing } from "@/hooks/useSWR";
import sectorsData from "@/data/sectors.json";
import {
  sectorEvents,
  sectorResearch,
  type SectorEvent,
  type SectorWatchpoint,
} from "@/data/sectorResearch";
import {
  api,
  type ChainNodeStat,
  type IndustryChainData,
  type RadarData,
  type RadarItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";

interface RelevantNews extends RadarItem {
  track: string;
}

const policyTone = (score: number) =>
  score >= 2
    ? "border-danger/30 bg-danger/10 text-danger"
    : score === 1
      ? "border-primary/30 bg-primary/10 text-primary"
      : score === 0
        ? "border-border bg-muted/40 text-muted-foreground"
        : "border-warning/30 bg-warning/10 text-warning";

const stageTone = (stage?: "上游" | "中游" | "下游") =>
  stage === "上游"
    ? "border-sky-500/30 bg-sky-500/10 text-sky-500"
    : stage === "中游"
      ? "border-violet-500/30 bg-violet-500/10 text-violet-500"
      : "border-amber-500/30 bg-amber-500/10 text-amber-500";

const eventTone = (direction: SectorEvent["direction"]) =>
  direction === "positive"
    ? "border-danger/25 bg-danger/5"
    : direction === "negative"
      ? "border-success/25 bg-success/5"
      : "border-border/60 bg-muted/20";

const fmtPct = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${value.toFixed(1)}%`;

const fmtPctSigned = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;

const severityLabel = ["", "中度制约", "强制约"];

function ChainDepthSection({ chain }: { chain: IndustryChainData }) {
  const { structure, profit, reports } = chain;
  const [focusNode, setFocusNode] = useState<string | null>(null);
  const profitTableRef = useRef<HTMLDivElement>(null);

  const nodeById = useMemo(
    () => new Map(structure.nodes.map((node) => [node.id, node])),
    [structure.nodes],
  );
  const statById = useMemo(
    () => new Map((profit.node_stats || []).map((stat) => [stat.node_id, stat])),
    [profit.node_stats],
  );
  const bottleneckByNode = useMemo(() => {
    const map = new Map<string, number>();
    structure.bottlenecks.forEach((b) => map.set(b.node_id, (map.get(b.node_id) ?? 0) + 1));
    return map;
  }, [structure.bottlenecks]);
  const linksOf = useMemo(() => {
    const map = new Map<string, { from: string; to: string; kind: string }[]>();
    structure.links.forEach((link) => {
      for (const id of [link.from, link.to]) {
        map.set(id, [...(map.get(id) ?? []), link]);
      }
    });
    return map;
  }, [structure.links]);

  const selectNode = (nodeId: string) => {
    setFocusNode((prev) => (prev === nodeId ? null : nodeId));
    profitTableRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const settled = profit.settled_node;
  const focusedNode = focusNode ? nodeById.get(focusNode) : null;
  const focusedStat = focusNode ? statById.get(focusNode) : undefined;

  return (
    <section className="mb-6">
      <div className="mb-3">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Network className="h-4 w-4 text-primary" /> 产业链图谱
        </h3>
        <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
          {structure.length}｜{structure.summary}
        </p>
      </div>

      {/* 关系图：上下/平行/交叉关系 + 选中环节联动 */}
      <GlassCard className="mb-3">
        <ChainGraph chain={chain} focusNode={focusNode} onSelect={selectNode} />
      </GlassCard>

      {/* 选中环节卡：环节说明 + 上下游关系 + 公司 + 去筛选 */}
      {focusedNode && (
        <GlassCard glow className="mb-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className={cn("rounded-full border px-2 py-0.5 text-[10px] font-medium", stageTone(focusedNode.stage))}>
              {focusedNode.stage}
            </span>
            <h4 className="font-semibold">{focusedNode.name}</h4>
            {bottleneckByNode.get(focusedNode.id) && (
              <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[10px] text-warning">
                含 {bottleneckByNode.get(focusedNode.id)} 项瓶颈
              </span>
            )}
          </div>
          <p className="text-xs leading-relaxed text-muted-foreground">{focusedNode.description}</p>
          {(linksOf.get(focusedNode.id) ?? []).length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
              <span className="text-muted-foreground">关系</span>
              {(linksOf.get(focusedNode.id) ?? []).map((link) => {
                const isFrom = link.from === focusedNode.id;
                const other = nodeById.get(isFrom ? link.to : link.from);
                return (
                  <button
                    key={`${link.from}-${link.to}-${link.kind}`}
                    onClick={() => selectNode(isFrom ? link.to : link.from)}
                    className={cn(
                      "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 transition-colors hover:border-primary/50",
                      link.kind === "cross"
                        ? "border-dashed border-border/60 bg-muted/30 text-muted-foreground"
                        : "border-border/60 bg-muted/30 text-foreground",
                    )}
                  >
                    {isFrom ? <ChevronRight className="h-2.5 w-2.5" /> : <ArrowLeft className="h-2.5 w-2.5" />}
                    {isFrom ? "供给" : "来自"}
                    {other?.name ?? (isFrom ? link.to : link.from)}
                  </button>
                );
              })}
            </div>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            {focusedNode.companies.map((company) => (
              <Link
                key={company.code}
                to={`/stock-data?code=${company.code}`}
                className="group inline-flex items-center gap-2 rounded-lg border border-border/60 bg-background/30 px-3 py-2 transition-colors hover:border-primary/50 hover:bg-primary/10"
              >
                <span className="text-sm font-medium">{company.name}</span>
                <span className="font-mono text-[10px] text-muted-foreground">{company.code}</span>
                <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/50 transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
              </Link>
            ))}
            <Link
              to={`/screening?tab=stocks&q=${encodeURIComponent(
                `帮我核验这些公司（${focusedNode.name}环节）：${focusedNode.companies.map((c) => c.code).join("、")}（估值分位 + 资金流向 + 解禁）`,
              )}`}
              className="inline-flex items-center gap-2 rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-sm text-primary transition-colors hover:bg-primary/20"
            >
              带这批代码去筛选 <ChevronRight className="h-3.5 w-3.5" />
            </Link>
          </div>
          {focusedStat && (
            <p className="mt-2 font-mono text-[10px] text-muted-foreground">
              环节中位：毛利率 {fmtPct(focusedStat.gross_margin)} · 净利率 {fmtPct(focusedStat.net_margin)} · ROE {fmtPct(focusedStat.roe)} · 营收同比 {fmtPctSigned(focusedStat.revenue_yoy)}
            </p>
          )}
        </GlassCard>
      )}
      {focusNode === null && (
        <p className="mb-3 text-center text-[11px] text-muted-foreground">
          点击图谱中的环节卡查看上下游关系、代表公司与环节中位数（{structure.nodes.length} 个环节 · {new Set(structure.nodes.flatMap((n) => n.companies.map((c) => c.code))).size} 家公司）。
        </p>
      )}

      {/* 利润分布 */}
      <div ref={profitTableRef} className="mb-3">
        <GlassCard>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h4 className="text-sm font-semibold">利润分布</h4>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                {profit.source}；报告期 {profit.periods?.join(" / ") || "—"}
              </p>
            </div>
            {settled && (
              <div className="rounded-lg border border-primary/30 bg-primary/10 px-3 py-1.5 text-xs text-primary">
                利润沉淀环节：<span className="font-semibold">{settled.node_name}</span>
                （毛利率中位 {fmtPct(settled.gross_margin)} · ROE {fmtPct(settled.roe)}）
              </div>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border/60 text-[10px] text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">公司</th>
                  <th className="py-2 pr-3 font-medium">环节</th>
                  <th className="py-2 pr-3 text-right font-medium">毛利率</th>
                  <th className="py-2 pr-3 text-right font-medium">净利率</th>
                  <th className="py-2 pr-3 text-right font-medium">ROE</th>
                  <th className="py-2 pr-3 text-right font-medium">营收同比</th>
                </tr>
              </thead>
              <tbody>
                {structure.nodes.map((node) => {
                  const stat: ChainNodeStat | undefined = statById.get(node.id);
                  const rows = (profit.rows || []).filter((row) => row.node_id === node.id);
                  const highlighted = focusNode === node.id;
                  return (
                    <Fragment key={node.id}>
                      <tr className={cn("border-b border-border/40", highlighted && "bg-primary/5")}>
                        <td colSpan={2} className="py-2 pr-3">
                          <span className={cn("mr-1.5 rounded-full border px-1.5 py-0.5 text-[9px]", stageTone(node.stage))}>
                            {node.stage}
                          </span>
                          <span className={cn("font-semibold", highlighted && "text-primary")}>{node.name}</span>
                        </td>
                        <td className="py-2 pr-3 text-right font-mono">{fmtPct(stat?.gross_margin)}</td>
                        <td className="py-2 pr-3 text-right font-mono">{fmtPct(stat?.net_margin)}</td>
                        <td className="py-2 pr-3 text-right font-mono">{fmtPct(stat?.roe)}</td>
                        <td className="py-2 pr-3 text-right font-mono">{fmtPctSigned(stat?.revenue_yoy)}</td>
                      </tr>
                      {rows.map((row) => (
                        <tr
                          key={row.code}
                          className={cn(
                            "border-b border-border/20 text-muted-foreground",
                            highlighted && "bg-primary/5",
                          )}
                        >
                          <td className="py-1.5 pr-3">
                            <Link
                              to={`/stock-data?code=${row.code}`}
                              className="hover:text-primary"
                            >
                              {row.name} <span className="font-mono text-[10px]">{row.code}</span>
                            </Link>
                            {row.stale && <span className="ml-1 text-[9px] text-warning">旧值</span>}
                          </td>
                          <td className="py-1.5 pr-3 text-[10px]">{row.node_name}</td>
                          <td className="py-1.5 pr-3 text-right font-mono">{fmtPct(row.gross_margin)}</td>
                          <td className="py-1.5 pr-3 text-right font-mono">{fmtPct(row.net_margin)}</td>
                          <td className="py-1.5 pr-3 text-right font-mono">{fmtPct(row.roe)}</td>
                          <td className="py-1.5 pr-3 text-right font-mono">{fmtPctSigned(row.revenue_yoy)}</td>
                        </tr>
                      ))}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          {profit.stale_count > 0 && (
            <p className="mt-2 text-[10px] text-warning">
              {profit.stale_count} 家公司当日抓取失败，展示的是上次成功值。
            </p>
          )}
        </GlassCard>
      </div>

      {/* 短板与制约 + 景气传导 双栏 */}
      <div className="grid gap-3 lg:grid-cols-2">
        <GlassCard>
          <h4 className="mb-3 text-sm font-semibold">短板与制约</h4>
          <p className="mb-2 text-[10px] text-muted-foreground">瓶颈环节往往是超额利润与行情主线的来源；点击环节名可定位图谱。</p>
          <div className="space-y-2">
            {structure.bottlenecks.map((item) => {
              const node = nodeById.get(item.node_id);
              const highlighted = focusNode === item.node_id;
              return (
                <div
                  key={item.type_label}
                  className={cn(
                    "rounded-lg border p-3 transition-colors",
                    highlighted ? "border-primary/50 bg-primary/5" : "border-border/40 bg-muted/20",
                  )}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{item.type_label}</span>
                    {node && (
                      <button
                        onClick={() => selectNode(node.id)}
                        className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary hover:bg-primary/20"
                      >
                        {node.name}
                      </button>
                    )}
                    <span className="ml-auto flex items-center gap-1">
                      {[1, 2].map((dot) => (
                        <span
                          key={dot}
                          className={cn(
                            "h-1.5 w-1.5 rounded-full",
                            dot <= item.severity ? "bg-warning" : "bg-muted",
                          )}
                        />
                      ))}
                      <span className="text-[10px] text-muted-foreground">{severityLabel[item.severity]}</span>
                    </span>
                  </div>
                  <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{item.detail}</p>
                  <p className="mt-1 text-[10px] text-muted-foreground">现状：{item.domestic_share}</p>
                  <p className="mt-1 text-[10px] text-primary/80">{item.signal}</p>
                </div>
              );
            })}
          </div>
        </GlassCard>

        <GlassCard>
          <h4 className="mb-3 text-sm font-semibold">景气传导</h4>
          <p className="mb-2 text-[10px] text-muted-foreground">价格与订单沿链条传导：上游涨价谁吸收、谁转嫁。点击环节名可定位图谱。</p>
          <div className="space-y-2">
            {structure.transmission.notes.map((note) => (
              <div key={`${note.from}-${note.to}-${note.what}`} className="rounded-lg border border-border/40 bg-muted/20 p-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  <button
                    onClick={() => selectNode(note.from)}
                    className="rounded bg-muted/60 px-1.5 py-0.5 text-[10px] hover:bg-primary/15 hover:text-primary"
                  >
                    {nodeById.get(note.from)?.name || note.from}
                  </button>
                  <ChevronRight className="h-3 w-3 text-muted-foreground/60" />
                  <button
                    onClick={() => selectNode(note.to)}
                    className="rounded bg-muted/60 px-1.5 py-0.5 text-[10px] hover:bg-primary/15 hover:text-primary"
                  >
                    {nodeById.get(note.to)?.name || note.to}
                  </button>
                  <span className="text-xs font-medium">{note.what}</span>
                  <span
                    className={cn(
                      "ml-auto rounded-full border px-2 py-0.5 text-[10px]",
                      note.status === "传导中"
                        ? "border-danger/30 bg-danger/10 text-danger"
                        : "border-warning/30 bg-warning/10 text-warning",
                    )}
                  >
                    {note.status}
                  </span>
                </div>
                <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{note.mechanism}</p>
                <p className="mt-1 text-right text-[10px] text-muted-foreground/70">更新于 {note.updated_on}</p>
              </div>
            ))}
            {structure.transmission.watch_quotes.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 pt-1">
                <span className="text-[10px] text-muted-foreground">跟踪指标</span>
                {structure.transmission.watch_quotes.map((quote) => (
                  <span key={quote} className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                    {quote}
                  </span>
                ))}
              </div>
            )}
          </div>
        </GlassCard>
      </div>

      {/* 行业研报线索 */}
      {reports && reports.rows?.length > 0 && (
        <GlassCard className="mt-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="text-sm font-semibold">行业研报线索</h4>
            <p className="text-[10px] text-muted-foreground">{reports.source}</p>
          </div>
          <div className="divide-y divide-border/40">
            {reports.rows.map((row) => (
              <a
                key={row.info_code || row.title}
                href={row.info_code ? `https://pdf.dfcfw.com/pdf/H3_${row.info_code}_1.pdf` : undefined}
                target="_blank"
                rel="noreferrer"
                className="group flex items-start justify-between gap-3 py-2 first:pt-0 last:pb-0"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm group-hover:text-primary">{row.title}</p>
                  <p className="mt-0.5 text-[10px] text-muted-foreground">{row.org} · {row.industry}</p>
                </div>
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{row.date}</span>
              </a>
            ))}
          </div>
        </GlassCard>
      )}
    </section>
  );
}

function WatchpointColumn({
  title,
  items,
  positive,
}: {
  title: string;
  items: SectorWatchpoint[];
  positive: boolean;
}) {
  const Icon = positive ? TrendingUp : TrendingDown;
  return (
    <GlassCard className="h-full">
      <h4 className={cn("mb-3 flex items-center gap-1.5 text-sm font-semibold", positive ? "text-danger" : "text-success")}>
        <Icon className="h-4 w-4" /> {title}
      </h4>
      <div className="space-y-2">
        {items.map((item) => (
          <div key={item.title} className="rounded-lg border border-border/40 bg-muted/20 p-3">
            <div className="flex items-start justify-between gap-3">
              <p className="text-sm font-medium">{item.title}</p>
              <span className="shrink-0 rounded bg-background/50 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {item.window}
              </span>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{item.detail}</p>
          </div>
        ))}
      </div>
    </GlassCard>
  );
}

export function SectorDetail() {
  const { key } = useParams();
  const sector = sectorsData.sectors.find((item) => item.key === key);
  const research = key ? sectorResearch[key] : undefined;
  const [activeNode, setActiveNode] = useState(0);
  const [radar, setRadar] = useState<RadarData | null>(null);
  const [radarLoading, setRadarLoading] = useState(false);
  const [radarError, setRadarError] = useState<string | null>(null);
  const [chain, setChain] = useState<IndustryChainData | null>(null);
  const [chainLoading, setChainLoading] = useState(false);
  const [chainError, setChainError] = useState<string | null>(null);

  useEffect(() => {
    setActiveNode(0);
  }, [key]);

  useEffect(() => {
    setChainError(null);
    if (!key) return;
    const cacheKey = `vr-industry-chain:${key}:v1`;
    let cached: IndustryChainData | null = null;
    try {
      cached = JSON.parse(localStorage.getItem(cacheKey) || "null") as IndustryChainData | null;
      if (cached?.schema_version !== 1 || !cached.structure?.nodes?.length) cached = null;
    } catch { cached = null; }
    setChain(cached);
    setChainLoading(!cached);
    api.industryChain(key)
      .then((first) => resolveRefreshing(first, () => api.industryChain(key)))
      .then((next) => {
        setChain(next);
        try { localStorage.setItem(cacheKey, JSON.stringify(next)); } catch { /* storage unavailable */ }
      })
      .catch(() => setChainError(cached ? "产业链更新失败，继续展示上次缓存" : "产业链数据读取失败"))
      .finally(() => setChainLoading(false));
  }, [key]);

  useEffect(() => {
    if (!research) return;
    setRadarLoading(true);
    setRadarError(null);
    api.radar()
      .then(setRadar)
      .catch(() => setRadarError("近期资讯读取失败，可到资讯雷达刷新后再看"))
      .finally(() => setRadarLoading(false));
  }, [key, research]);

  const relevantNews = useMemo<RelevantNews[]>(() => {
    if (!radar || !research) return [];
    const tracks = radar.industries.filter((industry) => research.radarKeys.includes(industry.key));
    const keywords = research.newsKeywords.map((keyword) => keyword.toLowerCase());
    const all = tracks.flatMap((industry) => industry.items.map((item) => ({ ...item, track: industry.name })));
    const matched = all.filter((item) => {
      const blob = `${item.title} ${item.zh || ""} ${item.summary || ""}`.toLowerCase();
      return keywords.some((keyword) => blob.includes(keyword));
    });
    const pool = matched.length >= 3 ? matched : all;
    const seen = new Set<string>();
    return pool
      .filter((item) => {
        const identity = item.url || item.title;
        if (seen.has(identity)) return false;
        seen.add(identity);
        return true;
      })
      .sort((a, b) => b.time.localeCompare(a.time))
      .slice(0, 6);
  }, [radar, research]);

  if (!sector) {
    return (
      <div className="py-20 text-center text-muted-foreground">
        未找到该板块。<Link to="/sectors" className="text-primary">返回板块</Link>
      </div>
    );
  }

  if (!research) {
    return (
      <div>
        <Link to="/sectors" className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-4 w-4" /> 板块
        </Link>
        <PageHeader title={sector.label} subtitle={sector.tagline} />
        <GlassCard>
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <Wrench className="h-8 w-8 text-muted-foreground/50" />
            <p className="text-sm text-muted-foreground">该板块的研究证据仍在补充中。</p>
          </div>
        </GlassCard>
      </div>
    );
  }

  const selectedNode = research.nodes[Math.min(activeNode, research.nodes.length - 1)];
  const events = sectorEvents[sector.key] || [];
  const positive = research.watchpoints.filter((item) => item.direction === "positive");
  const negative = research.watchpoints.filter((item) => item.direction === "negative");
  const companyCount = new Set(research.nodes.flatMap((node) => node.companies.map((company) => company.code))).size;
  const aiContext = [
    `板块：${sector.label}`,
    `定位：${sector.tagline}`,
    `研究快照：${research.asOf}`,
    `政策环境：${research.policy.label}（置信度${research.policy.confidence}）`,
    `政策摘要：${research.policy.summary}`,
    "核心环节与代表企业：",
    ...research.nodes.map((node) => `- ${node.stage || "产业链"}｜${node.name}：${node.description}；${node.companies.map((company) => `${company.name}(${company.code})`).join("、")}`),
    "已知事件及预判（请严格区分事实依据与影响判断，并沿用传导—验证—失效框架）：",
    ...events.map((item) => `- ${item.date}｜${item.status}｜${item.title}；事实依据：${item.basis}；影响判断：${item.judgment}；置信度：${item.confidence}`),
    "持续观察点：",
    ...research.watchpoints.map((item) => `- ${item.direction === "positive" ? "潜在利好" : "潜在利空"}｜${item.title}：${item.detail}`),
  ].join("\n");

  return (
    <div>
      <Link to="/sectors" className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" /> 板块
      </Link>

      <PageHeader
        title={sector.label}
        subtitle={sector.tagline}
        actions={
          <AskAiButton
            context={aiContext}
            taskId={`sector:${sector.key}`}
            label="让 AI 深入研究"
            suggestions={["核心环节谁更有议价权", "未来半年催化与风险清单", "逐条核验政策影响路径", "代表企业如何分组比较"]}
          />
        }
      />

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <GlassCard className="p-3">
          <p className="text-[11px] text-muted-foreground">核心环节</p>
          <p className="mt-1 text-xl font-bold">{research.nodes.length}</p>
        </GlassCard>
        <GlassCard className="p-3">
          <p className="text-[11px] text-muted-foreground">代表企业</p>
          <p className="mt-1 text-xl font-bold">{companyCount}</p>
        </GlassCard>
        <GlassCard className="p-3">
          <p className="text-[11px] text-muted-foreground">已知未来事件</p>
          <p className="mt-1 text-xl font-bold">{events.length}</p>
        </GlassCard>
        <GlassCard className="p-3">
          <p className="text-[11px] text-muted-foreground">研究快照</p>
          <p className="mt-1 font-mono text-sm font-semibold">{research.asOf}</p>
        </GlassCard>
      </div>

      <section className="mb-6">
        <div className="mb-3 flex items-end justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-1.5 text-sm font-semibold">
              <Building2 className="h-4 w-4 text-primary" /> 核心环节与代表企业
            </h3>
            <p className="mt-1 text-[11px] text-muted-foreground">按产业链从上游到下游排列；环节数量随行业结构变化。点击企业进入个股数据页核验。</p>
          </div>
          <div className="flex items-center gap-1 text-[10px]">
            {(["上游", "中游", "下游"] as const).map((stage, index) => (
              <span key={stage} className="inline-flex items-center gap-1">
                <span className={cn("rounded-full border px-2 py-0.5", stageTone(stage))}>{stage}</span>
                {index < 2 && <ChevronRight className="h-3 w-3 text-muted-foreground/50" />}
              </span>
            ))}
          </div>
        </div>

        <GlassCard>
          <div className="mb-3 flex flex-wrap gap-1.5">
            {research.nodes.map((node, index) => (
              <button
                key={node.name}
                onClick={() => setActiveNode(index)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors",
                  index === activeNode
                    ? "border-primary/50 bg-primary/10 text-primary"
                    : "border-border/50 bg-muted/20 text-foreground hover:border-primary/30 hover:bg-muted/40",
                )}
              >
                <span className={cn("h-1.5 w-1.5 rounded-full", node.stage === "上游" ? "bg-sky-500" : node.stage === "中游" ? "bg-violet-500" : "bg-amber-500")} />
                {node.name}
              </button>
            ))}
          </div>

          <GlassCard glow>
            <div className="mb-3">
              <div className="flex items-center gap-2">
                <span className={cn("rounded-full border px-2 py-0.5 text-[10px] font-medium", stageTone(selectedNode.stage))}>
                  {selectedNode.stage}
                </span>
                <h4 className="font-semibold">{selectedNode.name}</h4>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{selectedNode.description}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {selectedNode.companies.map((company) => (
                <Link
                  key={company.code}
                  to={`/stock-data?code=${company.code}`}
                  className="group inline-flex items-center gap-2 rounded-lg border border-border/60 bg-background/30 px-3 py-2 transition-colors hover:border-primary/50 hover:bg-primary/10"
                >
                  <span className="text-sm font-medium">{company.name}</span>
                  <span className="font-mono text-[10px] text-muted-foreground">{company.code}</span>
                  <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/50 transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
                </Link>
              ))}
              <Link
                to={`/screening?tab=stocks&q=${encodeURIComponent(
                  `帮我核验这些公司（${selectedNode.name}环节）：${selectedNode.companies.map((c) => c.code).join("、")}（估值分位 + 资金流向 + 解禁）`,
                )}`}
                className="inline-flex items-center gap-2 rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-sm text-primary transition-colors hover:bg-primary/20"
              >
                带这批代码去筛选 <ChevronRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          </GlassCard>
        </GlassCard>
      </section>

      {chainLoading && !chain && (
        <section className="mb-6">
          <GlassCard>
            <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在读取产业链图谱与利润分布
            </div>
          </GlassCard>
        </section>
      )}
      {!chainLoading && chain && <ChainDepthSection chain={chain} />}
      {chain && (chainError || chain.cache_state === "error" || chain.profit.refresh_error || chain.reports.refresh_error) && (
        <p className="mb-3 text-[10px] text-warning">
          {chainError || chain.refresh_error || chain.profit.refresh_error || chain.reports.refresh_error}；当前展示上次成功缓存。
        </p>
      )}
      {!chainLoading && !chain && chainError && (
        <section className="mb-6">
          <GlassCard>
            <p className="py-4 text-center text-sm text-warning">{chainError}</p>
          </GlassCard>
        </section>
      )}

      <section className="mb-6">
        <div className="mb-3">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <CalendarDays className="h-4 w-4 text-primary" /> 已知事件基础上的未来预判
          </h3>
          <p className="mt-1 text-[11px] text-muted-foreground">
            “已官宣”只表示日期或事项已有公开依据；下方影响仍是条件判断，不等于事件一定构成利好或利空。
          </p>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {events.map((item) => (
            <GlassCard key={`${item.date}-${item.title}`} className={cn("h-full border", eventTone(item.direction))}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-primary/10 px-2 py-1 font-mono text-[10px] font-semibold text-primary">{item.date}</span>
                <span className={cn(
                  "rounded-full border px-2 py-0.5 text-[10px]",
                  item.status === "已官宣"
                    ? "border-success/30 bg-success/10 text-success"
                    : "border-warning/30 bg-warning/10 text-warning",
                )}>
                  {item.status}
                </span>
                <span className="text-[10px] text-muted-foreground">判断置信度：{item.confidence}</span>
              </div>
              <h4 className="mt-3 text-sm font-semibold">{item.title}</h4>
              <div className="mt-3 space-y-2 text-xs leading-relaxed">
                <p><span className="font-medium text-foreground">事实依据：</span><span className="text-muted-foreground">{item.basis}</span></p>
                <p><span className="font-medium text-foreground">影响判断：</span><span className="text-muted-foreground">{item.judgment}</span></p>
              </div>
              <a
                href={item.url}
                target="_blank"
                rel="noreferrer"
                className="mt-3 inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
              >
                {item.source} <ExternalLink className="h-3 w-3" />
              </a>
            </GlassCard>
          ))}
        </div>
      </section>

      <section className="mb-6">
        <div className="mb-3">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <CalendarDays className="h-4 w-4 text-primary" /> 持续跟踪的利好 / 利空触发器
          </h3>
          <p className="mt-1 text-[11px] text-muted-foreground">未给出确定日期的变量单独列示，避免与已官宣事件混在一起。</p>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          <WatchpointColumn title="潜在利好触发器" items={positive} positive />
          <WatchpointColumn title="潜在利空触发器" items={negative} positive={false} />
        </div>
      </section>

      <section className="mb-6">
        <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold">
          <Landmark className="h-4 w-4 text-primary" /> 当前政策环境
        </h3>
        <GlassCard>
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div className="max-w-2xl">
              <div className="flex flex-wrap items-center gap-2">
                <span className={cn("rounded-full border px-2.5 py-1 text-xs font-semibold", policyTone(research.policy.score))}>
                  {research.policy.label}
                </span>
                <span className="text-[11px] text-muted-foreground">证据置信度：{research.policy.confidence}</span>
              </div>
              <p className="mt-2 text-sm leading-relaxed">{research.policy.summary}</p>
            </div>
            <div className="min-w-52">
              <div className="mb-1.5 flex justify-between text-[10px] text-muted-foreground">
                <span>明显承压</span><span>中性</span><span>强支持</span>
              </div>
              <div className="grid grid-cols-5 gap-1">
                {[-2, -1, 0, 1, 2].map((score) => (
                  <div
                    key={score}
                    className={cn(
                      "h-2 rounded-full",
                      score === research.policy.score
                        ? score > 0 ? "bg-danger" : score < 0 ? "bg-success" : "bg-muted-foreground"
                        : "bg-muted",
                    )}
                  />
                ))}
              </div>
            </div>
          </div>

          <div className="mt-4 space-y-2 border-t border-border/40 pt-4">
            {research.policy.evidence.map((evidence) => (
              <a
                key={`${evidence.url}-${evidence.title}`}
                href={evidence.url}
                target="_blank"
                rel="noreferrer"
                className="flex items-start justify-between gap-3 rounded-lg bg-muted/20 px-3 py-2.5 transition-colors hover:bg-muted/40"
              >
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-sm font-medium">{evidence.title}</p>
                    <span className={cn(
                      "rounded-full px-1.5 py-0.5 text-[9px]",
                      evidence.direction === "positive"
                        ? "bg-danger/10 text-danger"
                        : evidence.direction === "negative"
                          ? "bg-success/10 text-success"
                          : "bg-warning/10 text-warning",
                    )}>
                      {evidence.direction === "positive" ? "支持" : evidence.direction === "negative" ? "约束" : "支持与约束并存"}
                    </span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">{evidence.source} · {evidence.date}</p>
                </div>
                <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              </a>
            ))}
          </div>
        </GlassCard>
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-1.5 text-sm font-semibold">
              <Radio className="h-4 w-4 text-primary" /> 近期可核验消息
            </h3>
            <p className="mt-1 text-[11px] text-muted-foreground">
              来自资讯雷达公开源；只作触发器线索，仍需点原文核实。
            </p>
          </div>
          <Link to="/intel" className="text-xs text-primary hover:underline">去资讯雷达刷新</Link>
        </div>

        <GlassCard>
          {radarLoading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在读取近期资讯
            </div>
          ) : radarError ? (
            <p className="py-8 text-center text-sm text-warning">{radarError}</p>
          ) : relevantNews.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              当前缓存里没有匹配消息，可先到资讯雷达刷新公开源。
            </p>
          ) : (
            <div className="divide-y divide-border/40">
              {relevantNews.map((item) => (
                <a
                  key={item.url || item.title}
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                  className="group flex items-start gap-3 py-3 first:pt-0 last:pb-0"
                >
                  <span className="mt-0.5 shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">{item.track}</span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium leading-relaxed group-hover:text-primary">{item.zh || item.title}</p>
                    <p className="mt-1 text-[11px] text-muted-foreground">{item.time} · {item.source}</p>
                  </div>
                  <ExternalLink className="mt-1 h-3.5 w-3.5 shrink-0 text-muted-foreground/50 group-hover:text-primary" />
                </a>
              ))}
            </div>
          )}
          {radar?.generated_at && (
            <p className="mt-4 border-t border-border/40 pt-3 text-right font-mono text-[10px] text-muted-foreground">
              资讯缓存：{radar.generated_at}
            </p>
          )}
        </GlassCard>
      </section>
    </div>
  );
}
