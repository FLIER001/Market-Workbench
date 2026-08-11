import { Fragment, useState } from "react";
import { AlertCircle, ChevronDown, ChevronUp, Loader2, SlidersHorizontal, Star } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type FundScreenData, type FundScreenRow } from "@/lib/api";
import { loadFundWatch, toggleFundWatch } from "@/lib/fundWatch";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";
import { FundDetail } from "./FundDetail";

const TYPES = ["", "股票", "混合", "债券", "指数", "QDII", "货币"] as const;
const pctColor = (v: number | null | undefined) =>
  v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground";
const fmtPct = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v}`);

// 筛选基金：全市场业绩排行 + 4433 法则 + 业绩下限（吸收 investool 的 4433 方法论）。
export function FundScreenPanel() {
  const [fundType, setFundType] = useState<string>("");
  const [r4433, setR4433] = useState(false);
  const [sortBy, setSortBy] = useState<string>("近1年");
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const [minY1, setMinY1] = useState("");
  const [minM6, setMinM6] = useState("");
  const [minY3, setMinY3] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [watched, setWatched] = useState<Set<string>>(() => new Set(loadFundWatch().map((x) => x.code)));
  const [detail, setDetail] = useState<{ code: string; name: string } | null>(null);

  const screenKey = ["fund:screen", fundType, r4433, sortBy, order, minY1, minM6, minY3].join("|");
  const { data, loading, revalidating } = useSWR<FundScreenData>(screenKey, async () => {
    const d = await api.fundScreen({
      type: fundType || undefined,
      r4433,
      sort_by: sortBy,
      order,
      min_y1: minY1 ? parseFloat(minY1) : undefined,
      min_m6: minM6 ? parseFloat(minM6) : undefined,
      min_y3: minY3 ? parseFloat(minY3) : undefined,
      limit: 100,
    });
    setErr(null);
    return d;
  },
    [fundType, r4433, sortBy, order, minY1, minM6, minY3],
    (e: unknown) => setErr(e instanceof ApiError ? e.message : "筛选失败"),
    { persist: true });

  const toggleWatch = (row: FundScreenRow) => {
    toggleFundWatch({ code: row.code, name: row.name });
    setWatched(new Set(loadFundWatch().map((x) => x.code)));
  };

  const sortHeader = (p: string) => (
    <button
      className="inline-flex items-center gap-0.5 hover:text-foreground"
      onClick={() => { if (sortBy === p) setOrder(order === "desc" ? "asc" : "desc"); else { setSortBy(p); setOrder("desc"); } }}
    >
      {p}
      {sortBy === p && (order === "desc" ? <ChevronDown className="h-3 w-3" /> : <ChevronUp className="h-3 w-3" />)}
    </button>
  );

  return (
    <div className="space-y-4">
      <GlassCard>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1">
            {TYPES.map((t) => (
              <button key={t || "all"} onClick={() => setFundType(t)}
                className={cn("rounded-lg px-2.5 py-1.5 text-xs transition",
                  fundType === t ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-black/20")}>
                {t || "全部"}
              </button>
            ))}
          </div>
          <div className="h-5 w-px bg-border/60" />
          <button onClick={() => setR4433(!r4433)}
            className={cn("rounded-lg px-2.5 py-1.5 text-xs font-semibold transition",
              r4433 ? "bg-primary/25 text-primary" : "border border-border text-muted-foreground hover:bg-black/20")}
            title="4433 法则：近1年前1/4；近2/3年与今年来前1/3">
            4433
          </button>
          <button onClick={() => setShowAdvanced(!showAdvanced)}
            className={cn("rounded-lg p-1.5 transition", showAdvanced ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-black/20")}
            title="业绩下限过滤">
            <SlidersHorizontal className="h-4 w-4" />
          </button>
          <div className="flex-1" />
          {(loading || revalidating) && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
        </div>
        {showAdvanced && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>下限（%）：</span>
            {[
              { label: "近6月 ≥", v: minM6, set: setMinM6 },
              { label: "近1年 ≥", v: minY1, set: setMinY1 },
              { label: "近3年 ≥", v: minY3, set: setMinY3 },
            ].map(({ label, v, set }) => (
              <label key={label} className="flex items-center gap-1">
                {label}
                <input value={v} onChange={(e) => set(e.target.value.replace(/[^\d.-]/g, ""))}
                  className="w-16 rounded-lg border border-border bg-black/20 px-2 py-1 text-foreground outline-none focus:border-primary/60" />
              </label>
            ))}
          </div>
        )}
        {err && <div className="mt-3 flex items-center gap-2 text-sm text-danger"><AlertCircle className="h-4 w-4" /> {err}</div>}
      </GlassCard>

      <GlassCard className="overflow-x-auto p-0">
        {!data ? (
          <div className="flex h-40 items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
        ) : (
          <>
            <div className="flex items-center justify-between border-b border-border/60 px-4 py-2.5 text-xs text-muted-foreground">
              <span>匹配 {data.total_matched} 只 / 全市场 {data.total_all} 只{r4433 ? "（4433 法则）" : ""}，按{data.sort_by}排序</span>
            </div>
            <table className="w-full min-w-[860px] text-sm">
              <thead>
                <tr className="border-b border-border/60 text-left text-xs text-muted-foreground">
                  <th className="px-4 py-3 font-medium">基金</th>
                  {(["近1月", "近3月", "近6月", "近1年", "近3年", "今年来"] as const).map((p) => (
                    <th key={p} className="px-3 py-3 font-medium">{sortHeader(p)}</th>
                  ))}
                  <th className="px-3 py-3 font-medium" />
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <Fragment key={r.code}>
                  <tr className="border-b border-border/30 transition hover:bg-black/10">
                    <td className="px-4 py-2.5">
                      <button className="text-left" onClick={() => setDetail(detail?.code === r.code ? null : { code: r.code, name: r.name })}>
                        <div className="max-w-56 truncate font-medium text-primary hover:underline">{r.name}</div>
                        <div className="font-mono text-xs text-muted-foreground">{r.code}</div>
                      </button>
                    </td>
                    {(["近1月", "近3月", "近6月", "近1年", "近3年", "今年来"] as const).map((p) => (
                      <td key={p} className={cn("px-3 py-2.5 font-mono text-xs", pctColor(r[p] as number | null))}>
                        {fmtPct(r[p] as number | null)}
                      </td>
                    ))}
                    <td className="px-3 py-2.5 text-right">
                      <button onClick={() => toggleWatch(r)}
                        className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-black/20 hover:text-primary"
                        title={watched.has(r.code) ? "移出自选" : "加入自选"}>
                        <Star className={cn("h-3.5 w-3.5", watched.has(r.code) && "fill-primary text-primary")} />
                      </button>
                    </td>
                  </tr>
                  {detail?.code === r.code && (
                    <tr className="border-b border-border/30">
                      <td colSpan={8} className="bg-black/10 px-4 pb-3">
                        <FundDetail code={r.code} name={r.name} onClose={() => setDetail(null)}
                                    onWatchChange={() => setWatched(new Set(loadFundWatch().map((x) => x.code)))} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </>
        )}
      </GlassCard>
      <p className="text-xs text-muted-foreground">
        业绩数据为东财天天基金公开排行，历史业绩不预示未来表现；4433 法则为经验筛选规则，不构成投资建议。
      </p>
    </div>
  );
}
