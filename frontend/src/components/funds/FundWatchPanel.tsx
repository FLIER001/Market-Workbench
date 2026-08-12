import { Fragment, useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw, Star, Trash2 } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, type FundQuote } from "@/lib/api";
import { loadFundWatch, saveFundWatch, toggleFundWatch, type FundWatchItem } from "@/lib/fundWatch";
import { fundRefreshIntervalMs } from "@/hooks/useLiveQuotes";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";
import { FundSearchInput } from "./FundSearchInput";
import { FundDetail } from "./FundDetail";

const pctColor = (v: number | null | undefined) =>
  v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground";

// 自选基金：实时估值 + 最新净值双列（对标 x2rr/funds 的估值 vs 净值处理）。
// 目前挂在「自选」栏目下；onChange 用于让父级同步计数等状态。
export function FundWatchPanel({ onChange }: { onChange?: () => void }) {
  const [list, setList] = useState<FundWatchItem[]>(() => loadFundWatch());
  const { data: quoteMap, setData: setQuoteMap, loading, revalidating, revalidate } =
    useSWR<Record<string, FundQuote>>("fund:watch-quotes", () =>
      api.fundQuote(loadFundWatch().map((x) => x.code)), [], undefined, { persist: true });
  const quotes = quoteMap ?? {};
  const [detail, setDetail] = useState<{ code: string; name: string } | null>(null);

  const refreshQuotes = useCallback(async (items: FundWatchItem[]) => {
    if (items.length === 0) { setQuoteMap({}); return; }
    try {
      setQuoteMap(await api.fundQuote(items.map((x) => x.code)));
    } catch {
      /* 行情失败保留旧数据 */
    }
  }, [setQuoteMap]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    const tick = async () => {
      const interval = fundRefreshIntervalMs();
      if (!document.hidden && interval != null) await revalidate(true);
      if (!cancelled) timer = window.setTimeout(tick, interval ?? 60_000);
    };
    timer = window.setTimeout(tick, fundRefreshIntervalMs() ?? 60_000);
    return () => { cancelled = true; if (timer != null) window.clearTimeout(timer); };
  }, [revalidate]);

  const add = (f: { code: string; name: string; type: string }) => {
    const next = toggleFundWatch(f);
    setList(next);
    refreshQuotes(next);
    onChange?.();
  };

  const remove = (code: string) => {
    const next = list.filter((x) => x.code !== code);
    saveFundWatch(next);
    setList(next);
    onChange?.();
  };

  return (
    <div className="space-y-4">
      <GlassCard className="relative z-10">
        <div className="flex flex-wrap items-center gap-2">
          <FundSearchInput className="w-80" onPick={add} placeholder="搜基金加入自选" />
          <div className="flex-1" />
          <button onClick={() => revalidate(true)} className="rounded-xl border border-border p-2 text-muted-foreground transition hover:bg-black/20" title="刷新估值">
            <RefreshCw className={cn("h-4 w-4", (loading || revalidating) && "animate-spin")} />
          </button>
        </div>
      </GlassCard>

      <GlassCard className="overflow-x-auto p-0">
        {list.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            <Star className="mr-2 h-4 w-4" /> 还没有自选基金，上方搜索添加。
          </div>
        ) : (
          <table className="w-full min-w-[820px] text-sm">
            <thead>
              <tr className="border-b border-border/60 text-left text-xs text-muted-foreground">
                <th className="px-4 py-3 font-medium">基金</th>
                <th className="px-4 py-3 font-medium">盘中估值</th>
                <th className="px-4 py-3 font-medium">最新净值</th>
                <th className="px-4 py-3 font-medium" title="仅显示北京时间当天已确认净值收益；未公布时显示空值">今日收益</th>
                <th className="px-4 py-3 font-medium" title="上一已确认净值日收益">昨日收益</th>
                <th className="px-4 py-3 font-medium">估值时间</th>
                <th className="px-4 py-3 font-medium" />
              </tr>
            </thead>
            <tbody>
              {list.map((f) => {
                const q = quotes[f.code];
                return (
                  <Fragment key={f.code}>
                  <tr className="border-b border-border/30 transition hover:bg-black/10">
                    <td className="px-4 py-3">
                      <button className="text-left" onClick={() => setDetail(detail?.code === f.code ? null : { code: f.code, name: f.name })}>
                        <div className="font-medium text-primary hover:underline">{q?.name || f.name}</div>
                        <div className="font-mono text-xs text-muted-foreground">{f.code}{f.type ? ` · ${f.type}` : ""}</div>
                      </button>
                    </td>
                    <td className={cn("px-4 py-3 font-mono font-semibold", pctColor(q?.estimate_pct))}>
                      {q?.estimate_pct != null ? `${q.estimate_pct > 0 ? "+" : ""}${q.estimate_pct}%` : "—"}
                      {q?.estimate_pct != null && q?.estimate_stale && (
                        <span
                          className="ml-0.5 text-xs font-normal text-muted-foreground"
                          title={`海外市场已闭市，显示最近一场(隔夜)涨跌幅${q?.estimate_proxy ? `，按「${q.estimate_proxy}」代理` : ""}`}
                        >
                          夜
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {q?.nav ?? "—"}
                      <div className="text-xs text-muted-foreground">{q?.nav_date || ""}</div>
                    </td>
                    <td className={cn("px-4 py-3 font-mono font-semibold", pctColor(q?.today_return_pct))}>
                      {q?.today_return_pct != null ? `${q.today_return_pct > 0 ? "+" : ""}${q.today_return_pct}%` : "—"}
                    </td>
                    <td className={cn("px-4 py-3 font-mono font-semibold", pctColor(q?.yesterday_return_pct))}>
                      {q?.yesterday_return_pct != null ? `${q.yesterday_return_pct > 0 ? "+" : ""}${q.yesterday_return_pct}%` : "—"}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">{q?.estimate_time || "非交易时段"}</td>
                    <td className="px-4 py-3 text-right">
                      <button onClick={() => remove(f.code)} className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-black/20 hover:text-danger" title="移出自选">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                  {detail?.code === f.code && (
                    <tr className="border-b border-border/30">
                      <td colSpan={7} className="bg-black/10 px-4 pb-3">
                        <FundDetail code={f.code} name={f.name} onClose={() => setDetail(null)}
                                    onWatchChange={() => setList(loadFundWatch())} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </GlassCard>
      {revalidating && list.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="h-3 w-3 animate-spin" /> 刷新估值中…</div>
      )}
    </div>
  );
}
