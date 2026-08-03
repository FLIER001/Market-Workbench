import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { MyReportsPanel } from "@/pages/MyReports";
import { NotesPanel } from "@/pages/Notes";
import { cn } from "@/lib/utils";

type ResearchTab = "reports" | "notes";

const TAB_KEY = "vr-research-tab";

const loadTab = (): ResearchTab => {
  try {
    return localStorage.getItem(TAB_KEY) === "notes" ? "notes" : "reports";
  } catch {
    return "reports";
  }
};

export function ResearchHub() {
  const [tab, setTab] = useState<ResearchTab>(loadTab);

  const switchTab = (next: ResearchTab) => {
    setTab(next);
    try {
      localStorage.setItem(TAB_KEY, next);
    } catch { /* ignore */ }
  };

  return (
    <div>
      <PageHeader
        title="研究"
        subtitle="我的研报与研究记录 · 研报归档、AI 分析沉淀"
      />

      <div className="mb-4 grid max-w-md grid-cols-2 gap-2 rounded-xl border border-border/50 bg-muted/20 p-1.5">
        {([
          ["reports", "我的研报"],
          ["notes", "研究记录"],
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

      {tab === "reports" ? <MyReportsPanel /> : <NotesPanel />}
    </div>
  );
}
