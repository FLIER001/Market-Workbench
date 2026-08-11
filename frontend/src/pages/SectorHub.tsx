import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { SectorAnalysis } from "@/pages/Sectors";
import { SectorScoresPanel } from "@/pages/SectorScores";
import { PlateScoresPanel } from "@/pages/PlateScores";
import { cn } from "@/lib/utils";

type SectorTab = "plates" | "industries" | "analysis";

const TAB_KEY = "vr-sector-tab";

const loadTab = (): SectorTab => {
  try {
    const saved = localStorage.getItem(TAB_KEY);
    if (saved === "industries" || saved === "analysis") return saved;
    return "plates";
  } catch {
    return "plates";
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
        subtitle="板块评分 · 行业评分 · 板块分析"
      />

      <div className="mb-4 grid max-w-lg grid-cols-3 gap-2 rounded-xl border border-border/50 bg-muted/20 p-1.5">
        {([
          ["plates", "板块评分"],
          ["industries", "行业评分"],
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

      {tab === "plates" ? <PlateScoresPanel /> : tab === "industries" ? <SectorScoresPanel /> : <SectorAnalysis />}
    </div>
  );
}
