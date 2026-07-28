import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Building2,
  CalendarDays,
  ChevronRight,
  ExternalLink,
  Landmark,
  Loader2,
  Radio,
  TrendingDown,
  TrendingUp,
  Wrench,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { AskAiButton } from "@/components/ui/AskAiButton";
import sectorsData from "@/data/sectors.json";
import {
  sectorEvents,
  sectorResearch,
  type SectorEvent,
  type SectorWatchpoint,
} from "@/data/sectorResearch";
import { api, type RadarData, type RadarItem } from "@/lib/api";
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

  useEffect(() => {
    setActiveNode(0);
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
        未找到该板块。<Link to="/sectors" className="text-primary">返回板块中心</Link>
      </div>
    );
  }

  if (!research) {
    return (
      <div>
        <Link to="/sectors" className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-4 w-4" /> 板块中心
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
        <ArrowLeft className="h-4 w-4" /> 板块中心
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

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {research.nodes.map((node, index) => (
            <button
              key={node.name}
              onClick={() => setActiveNode(index)}
              className={cn(
                "rounded-xl border p-3 text-left transition-colors",
                index === activeNode
                  ? "border-primary/50 bg-primary/10 shadow-glow"
                  : "border-border/50 bg-muted/20 hover:border-primary/30 hover:bg-muted/40",
              )}
            >
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="font-mono text-[10px] text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
                <span className={cn("rounded-full border px-2 py-0.5 text-[9px] font-medium", stageTone(node.stage))}>
                  {node.stage}
                </span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold leading-snug">{node.name}</span>
                <ChevronRight className={cn("h-4 w-4", index === activeNode ? "text-primary" : "text-muted-foreground/50")} />
              </div>
              <p className="mt-1 text-[10px] text-muted-foreground">{node.companies.length} 家代表企业</p>
            </button>
          ))}
        </div>

        <GlassCard glow className="mt-3">
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
          </div>
        </GlassCard>
      </section>

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
