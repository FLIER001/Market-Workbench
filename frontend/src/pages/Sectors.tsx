import { Link } from "react-router-dom";
import { Building2, ChevronRight, Flame, Landmark } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import sectorsData from "@/data/sectors.json";
import { sectorResearch } from "@/data/sectorResearch";

export function Sectors() {
  const sectors = sectorsData.sectors;
  const hotCount = sectors.filter((s) => s.hot).length;

  return (
    <div>
      <PageHeader
        title="板块中心"
        subtitle={`${sectors.length} 个赛道 · 产业链环节、代表企业、事件观察点与政策证据`}
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {sectors.map((s) => {
          const research = sectorResearch[s.key];
          const companyCount = new Set(research?.nodes.flatMap((node) => node.companies.map((company) => company.code)) || []).size;
          return (
            <Link key={s.key} to={`/sectors/${s.key}`}>
              <GlassCard glow={s.hot} className="flex h-full flex-col justify-between">
                <div>
                  <div className="mb-1 flex items-center gap-2">
                    <h3 className="text-base font-bold">{s.label}</h3>
                    {s.hot && (
                      <span className="inline-flex items-center gap-0.5 rounded-full bg-accent/15 px-1.5 py-0.5 text-[10px] font-medium text-accent">
                        <Flame className="h-3 w-3" /> 热门
                      </span>
                    )}
                  </div>
                  <p className="text-xs leading-relaxed text-muted-foreground">{s.tagline}</p>
                  {research && (
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-muted-foreground">
                      <span className="inline-flex items-center gap-1 rounded-full bg-muted/40 px-2 py-1">
                        <Building2 className="h-3 w-3" /> {research.nodes.length} 环节 · {companyCount} 家代表企业
                      </span>
                      <span className="inline-flex items-center gap-1 rounded-full bg-muted/40 px-2 py-1">
                        <Landmark className="h-3 w-3" /> 政策：{research.policy.label}
                      </span>
                    </div>
                  )}
                </div>
                <div className="mt-3 flex items-center justify-between border-t border-border/50 pt-3 text-xs">
                  <span className="text-muted-foreground">
                    {research ? `更新至 ${research.asOf}` : "研究框架待补"}
                  </span>
                  <ChevronRight className="h-4 w-4 text-primary" />
                </div>
              </GlassCard>
            </Link>
          );
        })}
      </div>

      <p className="mt-4 text-center text-xs text-muted-foreground/60">
        共 {sectors.length} 个板块，其中 {hotCount} 个热门 · 企业为产业链研究入口，点击可进入个股数据核验
      </p>
    </div>
  );
}
