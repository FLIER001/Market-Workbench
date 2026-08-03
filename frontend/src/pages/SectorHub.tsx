import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { SectorAnalysis } from "@/pages/Sectors";
import { SectorScoresPanel } from "@/pages/SectorScores";
import { cn } from "@/lib/utils";

type SectorTab = "scores" | "analysis";

const TAB_KEY = "vr-sector-tab";

const loadTab = (): SectorTab => {
  try {
    return localStorage.getItem(TAB_KEY) === "analysis" ? "analysis" : "scores";
  } catch {
    return "scores";
  }
};

export function SectorHub() {
  const [tab, setTab] = useState<SectorTab>(loadTab);

  const switchTab = (next: SectorTab) => {
    setTab(next);
    try {
      localStorage.setItem(TAB_KEY, next);
    } catch { /* ignore */ }
  };

  return (
    <div>
      <PageHeader
        title="板块"
        subtitle="板块评分与板块分析 · 申万行业打分、产业链环节与代表企业"
      />

      <div className="mb-4 grid max-w-md grid-cols-2 gap-2 rounded-xl border border-border/50 bg-muted/20 p-1.5">
        {([
          ["scores", "板块评分"],
          ["analysis", "板块分析"],
        ] as const).map(([id, label]) => (
          <button
            key={id}
            onClick={() => switchTab(id)}
            className={cn(
              "flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors",
              tab === id
                ? "bg-primary/15 text-primary shadow-sm"
                : "text-muted-foreground hover:bg-muted/40 hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "scores" ? <SectorScoresPanel /> : <SectorAnalysis />}
    </div>
  );
}
