import { useEffect, useState } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { api, type IndexHistoryData, type MinuteKline } from "@/lib/api";
import { MinuteChart } from "@/components/ui/MinuteChart";
import { Sparkline } from "@/components/ui/Sparkline";
import { cn } from "@/lib/utils";

interface Props {
  symbol: string;
  minute: { data: MinuteKline | null; loading: boolean; err: string | null };
}

const periods = [1, 3, 5, 10];

export function IndexResearchPanel({ symbol, minute }: Props) {
  const [year, setYear] = useState(1);
  const [state, setState] = useState<{ data: IndexHistoryData | null; loading: boolean; error: boolean }>({ data: null, loading: true, error: false });

  useEffect(() => {
    let active = true;
    setState({ data: null, loading: true, error: false });
    setYear(1);
    api.indexHistory(symbol).then((data) => {
      if (active) setState({ data, loading: false, error: false });
    }).catch(() => {
      if (active) setState({ data: null, loading: false, error: true });
    });
    return () => { active = false; };
  }, [symbol]);

  const period = state.data?.periods?.[String(year)];
  const valuation = state.data?.valuation;
  const valuationPeriod = valuation?.percentiles?.[String(year)];
  return (
    <div className="minute-reveal grid border-t border-border bg-background lg:grid-cols-[190px_minmax(0,1fr)_minmax(0,1fr)]">
      <section className="border-b border-border p-4 lg:border-b-0 lg:border-r" aria-label="指数与估值百分位">
        <h3 className="text-xs font-semibold">指数百分位</h3>
        <div className="mt-2 grid grid-cols-4 gap-1 lg:grid-cols-2">
          {periods.map((item) => (
            <button key={item} onClick={() => setYear(item)} aria-pressed={year === item}
              className={cn("border border-border px-1 py-2 text-center", year === item ? "bg-primary/10 text-primary" : "hover:bg-muted")}>
              <span className="block text-[10px]">近{item}年</span>
              <span className="mt-1 block font-mono text-xs">{state.data?.periods?.[String(item)]?.percentile == null ? "—" : `${state.data.periods[String(item)].percentile}%`}</span>
            </button>
          ))}
        </div>
        <div className="mt-4 border-t border-border pt-3">
          <h3 className="text-xs font-semibold">估值</h3>
          <dl className="mt-2 space-y-2 text-xs">
            <div className="flex justify-between gap-2"><dt className="text-muted-foreground">PE</dt><dd className="font-mono">{valuation?.pe_ttm ?? "—"}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-muted-foreground">{valuationPeriod ? `近${year}年` : "近10年"} PE 分位</dt><dd className="font-mono">{valuationPeriod?.percentile ?? valuation?.percentile ?? "—"}{valuationPeriod?.percentile != null || valuation?.percentile != null ? "%" : ""}</dd></div>
          </dl>
          {state.loading && <p className="mt-3 text-[10px] text-muted-foreground">历史与估值加载中…</p>}
        </div>
      </section>

      <section className="min-w-0 border-b border-border p-4 lg:border-b-0 lg:border-r" aria-label={`近${year}年历史走势`}>
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-xs font-semibold">历史走势 · 近{year}年</h3>
          <span className="font-mono text-[10px] text-muted-foreground">{period ? `${period.start} — ${period.end}` : ""}</span>
        </div>
        <div className="mt-4 flex h-[220px] items-center justify-center">
          {state.loading ? <Loader2 className="h-5 w-5 animate-spin text-primary" /> : state.error ? (
            <p className="flex items-center gap-2 text-xs text-muted-foreground"><AlertCircle className="h-4 w-4 text-warning" />历史数据暂不可用</p>
          ) : period?.history?.length ? <Sparkline data={period.history} height={210} showLatest className="w-full" /> : (
            <p className="text-xs text-muted-foreground">该周期暂无历史数据</p>
          )}
        </div>
      </section>

      <section className="min-w-0 p-4" aria-label="当天走势">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold">当天走势</h3>
          {minute.data?.last_day && <span className="text-[10px] text-warning">上一交易日 {minute.data.date}</span>}
        </div>
        <div className="mt-1 h-[236px]">
          {minute.loading && <div className="flex h-full items-center justify-center gap-2 text-xs text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />分时加载中…</div>}
          {!minute.loading && minute.err && <div className="flex h-full items-center justify-center gap-2 text-xs text-muted-foreground"><AlertCircle className="h-4 w-4 text-warning" />{minute.err}</div>}
          {!minute.loading && !minute.err && minute.data && <MinuteChart key={symbol} data={minute.data} height={230} minHeight={190} />}
        </div>
      </section>
    </div>
  );
}
