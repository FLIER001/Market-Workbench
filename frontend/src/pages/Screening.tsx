import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { FundScreenPanel } from "@/components/funds/FundScreenPanel";
import { cn } from "@/lib/utils";

type ScreenTab = "funds" | "stocks";

const TAB_KEY = "vr-screening-tab";

const loadTab = (): ScreenTab => {
  try {
    const saved = localStorage.getItem(TAB_KEY);
    if (saved === "stocks") return saved;
    return "funds";
  } catch {
    return "funds";
  }
};

export function Screening() {
  const [tab, setTab] = useState<ScreenTab>(loadTab);

  const switchTab = (next: ScreenTab) => {
    setTab(next);
    try {
      localStorage.setItem(TAB_KEY, next);
    } catch { /* ignore */ }
  };

  return (
    <div>
      <PageHeader
        title="标的筛选"
        subtitle="基金筛选 · 个股筛选"
      />

      <div className="mb-4 grid max-w-sm grid-cols-2 gap-2 rounded-xl border border-border/50 bg-muted/20 p-1.5">
        {([
          ["funds", "基金"],
          ["stocks", "个股"],
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

      {tab === "funds" ? <FundScreenPanel /> : <EmptyStocks />}
    </div>
  );
}

function EmptyStocks() {
  return (
    <div className="flex min-h-48 flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border/60 text-sm text-muted-foreground">
      <p>个股筛选 · 待建设</p>
    </div>
  );
}
