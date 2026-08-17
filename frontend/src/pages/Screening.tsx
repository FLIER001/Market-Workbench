import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/ui/PageHeader";
import { FundScreenPanel } from "@/components/funds/FundScreenPanel";
import { StockScreenPanel } from "@/components/screening/StockScreenPanel";
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
  const [searchParams, setSearchParams] = useSearchParams();
  const urlTab = searchParams.get("tab") === "stocks" ? "stocks" : null;
  const urlQuery = searchParams.get("q");
  // 带参跳转（如板块详情「带这批代码去筛选」）一次性消费，刷新不重复触发
  const [tab, setTab] = useState<ScreenTab>(urlTab ?? loadTab);
  const [prefill, setPrefill] = useState<string | null>(urlQuery);
  const consumed = urlTab || urlQuery ? false : true;

  if (!consumed) {
    try {
      searchParams.delete("tab");
      searchParams.delete("q");
      setSearchParams(searchParams, { replace: true });
    } catch { /* ignore */ }
  }

  const switchTab = (next: ScreenTab) => {
    setTab(next);
    setPrefill(null);
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

      {tab === "funds" ? <FundScreenPanel /> : <StockScreenPanel prefill={prefill} />}
    </div>
  );
}
