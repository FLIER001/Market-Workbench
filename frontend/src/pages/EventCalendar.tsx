import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, CalendarDays, Loader2, RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type EventCalendarData } from "@/lib/api";
import { useHoldingCodes } from "@/hooks/useHoldingCodes";
import { flattenWatchGroups, loadWatchGroups } from "@/lib/watchlist";
import { cn } from "@/lib/utils";

const DAYS = [7, 30, 90] as const;
const WEEK = ["日", "一", "二", "三", "四", "五", "六"];
const KIND_STYLE: Record<string, string> = {
  预约披露: "border-primary/40 bg-primary/10 text-primary",
  限售解禁: "border-warning/40 bg-warning/10 text-warning",
  除权除息: "border-success/40 bg-success/10 text-success",
};

/**
 * 事件日历：自选与持仓接下来 7 / 30 / 90 天的预约披露、限售解禁、除权除息。
 * 每条写明来源；某个来源没取到会列在下面，不当成「没有安排」。
 */
export function EventCalendar() {
  const holdingCodes = useHoldingCodes();
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<EventCalendarData | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // 自选（股票 + ETF）与持仓合并去重；重复代码只查一次
  const codes = useMemo(() => {
    const watch = [
      ...flattenWatchGroups(loadWatchGroups("stock")),
      ...flattenWatchGroups(loadWatchGroups("etf")),
    ];
    return Array.from(new Set([...holdingCodes, ...watch]));
  }, [holdingCodes]);

  const load = useCallback(async (windowDays: number, refresh = false) => {
    if (codes.length === 0) {
      setData(null);
      return;
    }
    setLoading(true);
    setErr(null);
    try {
      setData(await api.eventCalendar(codes, windowDays, refresh));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "事件日历取数失败");
    } finally {
      setLoading(false);
    }
  }, [codes]);

  useEffect(() => { load(days); }, [load, days]);

  const grouped = useMemo(() => {
    const map = new Map<string, EventCalendarData["items"]>();
    for (const item of data?.items ?? []) {
      const list = map.get(item.date) ?? [];
      list.push(item);
      map.set(item.date, list);
    }
    return Array.from(map.entries());
  }, [data]);

  return (
    <div>
      <PageHeader
        title="事件日历"
        actions={
          <div className="flex items-center gap-2">
            {DAYS.map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={cn(
                  "rounded-lg border px-2.5 py-1 text-xs transition",
                  days === d ? "border-primary/50 bg-primary/15 text-primary" : "border-border/50 text-muted-foreground hover:text-foreground",
                )}
              >
                {d} 天
              </button>
            ))}
            <button
              onClick={() => load(days, true)}
              disabled={loading || codes.length === 0}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border/60 px-2.5 py-1 text-xs text-muted-foreground transition hover:border-primary/60 hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} /> 刷新
            </button>
          </div>
        }
      />

      <p className="mb-4 text-xs text-muted-foreground">
        自选与持仓接下来要发生的事，每条写来源 · 只做沪深 A 股
        <span className="ml-2 text-muted-foreground/60">
          共 {codes.length} 只{data ? ` · 已查 ${data.codes} 只 A 股` : ""}
        </span>
      </p>

      {codes.length === 0 && (
        <GlassCard>
          <p className="py-10 text-center text-sm text-muted-foreground">
            还不自选也没有持仓。<Link to="/watchlist" className="text-primary hover:underline">去自选页</Link>加几只，再回来看它们接下来的日子。
          </p>
        </GlassCard>
      )}

      {err && (
        <GlassCard className="mb-4">
          <p className="flex items-center gap-1.5 py-2 text-sm text-warning"><AlertCircle className="h-4 w-4" /> {err}</p>
        </GlassCard>
      )}

      {loading && !data && codes.length > 0 && (
        <GlassCard>
          <p className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> 取预约披露、限售解禁与分红送转…
          </p>
        </GlassCard>
      )}

      {data && (
        <GlassCard>
          {grouped.length === 0 ? (
            <p className="py-10 text-center text-sm text-muted-foreground">
              未来 {days} 天没有已排定的事件。
              <span className="mt-1 block text-xs text-muted-foreground/60">
                预约披露日由交易所统一公布，公布后才出现在这里；除权除息只列已经定下日子的方案。
              </span>
            </p>
          ) : (
            <div className="space-y-4">
              {grouped.map(([day, items]) => (
                <div key={day}>
                  <p className="mb-1.5 flex items-baseline gap-2 text-xs">
                    <CalendarDays className="h-3.5 w-3.5 text-primary" />
                    <span className="font-mono font-semibold">{day}</span>
                    <span className="text-muted-foreground">周{WEEK[new Date(day + "T00:00:00").getDay()]}</span>
                    <span className="text-muted-foreground/60">{items.length} 条</span>
                  </p>
                  <div className="space-y-1 pl-5">
                    {items.map((item, i) => (
                      <div key={`${item.code}-${item.kind}-${i}`} className="flex flex-wrap items-baseline gap-2 text-sm">
                        <span className={cn("rounded border px-1.5 py-0.5 text-[10px]", KIND_STYLE[item.kind] ?? "border-border/50 text-muted-foreground")}>
                          {item.kind}
                        </span>
                        <Link to={`/stock-data?code=${item.code}`} className="font-medium hover:text-primary hover:underline">
                          {item.name}
                        </Link>
                        <span className="font-mono text-[11px] text-muted-foreground/60">{item.code}</span>
                        <span className="text-xs text-muted-foreground">{item.detail}</span>
                        <span className="ml-auto text-[10px] text-muted-foreground/50">{item.source}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {data.gaps.length > 0 && (
            <ul className="mt-4 space-y-0.5 border-t border-border/40 pt-2 text-[10px] text-muted-foreground/70">
              {data.gaps.map((gap, i) => <li key={i}>· {gap}</li>)}
              <li>· 港股、美股、北交所、场内基金与可转债不在这些来源里，没查；「没有事件」和「没取到」分开写，不混在一起</li>
            </ul>
          )}

          <p className="mt-3 text-[10px] text-muted-foreground/60">
            数据截至 {data.as_of} · 窗口 {data.window.start} → {data.window.end} · 来源：{data.sources.join("、") || "—"}
          </p>
        </GlassCard>
      )}
    </div>
  );
}
