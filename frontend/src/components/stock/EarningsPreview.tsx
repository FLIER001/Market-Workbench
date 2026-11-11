import { useState } from "react";
import { AlertCircle, CalendarClock, Loader2 } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type EarningsPreviewData } from "@/lib/api";
import { cn } from "@/lib/utils";

const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
const color = (v: number | null | undefined) =>
  v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground";
const num = (v: number | null | undefined, digits = 2) => (v == null ? "—" : v.toFixed(digits));

/**
 * 财报前瞻与复盘：下一份定期报告的预约披露日、机构一致预期、历次发布后第一个交易日的涨跌，
 * 以及最近一期实际净利润落在业绩预告区间的哪一边。只描述过去与已排期的事，不预测下一次。
 */
export function EarningsPreview({ code }: { code: string }) {
  const [data, setData] = useState<EarningsPreviewData | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run(refresh = false) {
    setLoading(true);
    setErr(null);
    try {
      setData(await api.earningsPreview(code, refresh));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "财报前瞻取数失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <GlassCard className="mb-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <CalendarClock className="h-4 w-4 text-primary" /> 财报前瞻与复盘
        </h3>
        <button
          onClick={() => run(true)}
          disabled={loading}
          className="rounded-lg border border-border/60 px-2.5 py-1 text-xs text-muted-foreground transition hover:border-primary/60 hover:text-foreground disabled:opacity-50"
        >
          {loading ? "取数中…" : data ? "重新取数" : "查看"}
        </button>
      </div>

      {!data && !loading && !err && (
        <p className="py-4 text-center text-xs text-muted-foreground/60">
          点「查看」取数：下一份定期报告的预约披露日、机构一致预期、历次发布后首日涨跌
        </p>
      )}
      {loading && !data && (
        <p className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 取预约披露、业绩报表与日线…
        </p>
      )}
      {err && (
        <p className="flex items-center gap-1.5 py-2 text-xs text-warning">
          <AlertCircle className="h-3.5 w-3.5" /> {err}
        </p>
      )}

      {data?.ok && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="rounded-lg border border-border/40 bg-black/10 px-3 py-2">
              <p className="text-[11px] text-muted-foreground">下一份定期报告</p>
              {data.next_report ? (
                <>
                  <p className="mt-0.5 text-sm font-semibold">
                    {data.next_report.period_label}
                    <span className="ml-2 font-mono text-xs text-muted-foreground">{data.next_report.date}</span>
                  </p>
                  <p className="mt-0.5 text-[10px] text-muted-foreground/70">
                    {data.next_report.days_left >= 0 ? `还有 ${data.next_report.days_left} 天` : "已过期未披露"}
                    {data.next_report.rescheduled.length > 0 && ` · 改期 ${data.next_report.rescheduled.length} 次（最近 ${data.next_report.rescheduled[data.next_report.rescheduled.length - 1]}）`}
                    {data.next_report.first_appointed && data.next_report.first_appointed !== data.next_report.date
                      && ` · 首次预约 ${data.next_report.first_appointed}`}
                  </p>
                </>
              ) : (
                <p className="mt-0.5 text-xs text-muted-foreground">没有已公布的预约披露日</p>
              )}
            </div>

            <div className="rounded-lg border border-border/40 bg-black/10 px-3 py-2">
              <p className="text-[11px] text-muted-foreground">机构一致预期（每股收益）</p>
              {data.consensus.length === 0 ? (
                <p className="mt-0.5 text-xs text-muted-foreground">没有机构盈利预测</p>
              ) : (
                <div className="mt-1 space-y-0.5">
                  {data.consensus.slice(0, 3).map((row) => (
                    <p key={row.year} className="flex items-baseline gap-2 text-xs">
                      <span className="w-10 shrink-0 text-muted-foreground">{row.year}</span>
                      <span className="font-mono font-semibold">{num(row.mean)}</span>
                      <span className="font-mono text-[10px] text-muted-foreground/70">
                        {num(row.low)}–{num(row.high)} · {row.analysts ?? "—"} 家
                      </span>
                    </p>
                  ))}
                </div>
              )}
            </div>
          </div>

          {data.surprise && (
            <p className="mt-3 text-xs text-muted-foreground">
              {data.surprise.period_label}实际归母净利润 <b className="font-mono text-foreground">{num(data.surprise.actual_yi)} 亿元</b>
              ，预告区间 {num(data.surprise.forecast_low_yi)}–{num(data.surprise.forecast_high_yi)} 亿元
              {data.surprise.forecast_type && `（${data.surprise.forecast_type}）`}
              <span className={cn("ml-1", data.surprise.position === "落在区间内" ? "text-muted-foreground" : "text-warning")}>
                {data.surprise.position}
              </span>
            </p>
          )}

          {data.history.length > 0 && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[520px] text-xs">
                <thead>
                  <tr className="border-b border-border/50 text-left text-muted-foreground">
                    <th className="px-2 py-1.5 font-medium">报告期</th>
                    <th className="px-2 py-1.5 font-medium">披露日</th>
                    <th className="px-2 py-1.5 font-medium">首日涨跌</th>
                    <th className="px-2 py-1.5 font-medium">当天大盘</th>
                    <th className="px-2 py-1.5 font-medium">超额</th>
                    <th className="px-2 py-1.5 font-medium">EPS</th>
                    <th className="px-2 py-1.5 font-medium">归母净利(亿)</th>
                  </tr>
                </thead>
                <tbody>
                  {data.history.map((row, i) => (
                    <tr key={`${row.publish_date}-${i}`} className="border-b border-border/30">
                      <td className="px-2 py-1.5">
                        {row.period_label}
                        {row.same_day_releases ? <span className="ml-1 text-[10px] text-muted-foreground/60">同日 {row.same_day_releases} 份</span> : null}
                      </td>
                      <td className="px-2 py-1.5 font-mono text-muted-foreground">{row.publish_date}</td>
                      <td className={cn("px-2 py-1.5 font-mono", color(row.reaction))}>{pct(row.reaction)}</td>
                      <td className="px-2 py-1.5 font-mono text-muted-foreground">{pct(row.market)}</td>
                      <td className={cn("px-2 py-1.5 font-mono", color(row.excess))}>{pct(row.excess)}</td>
                      <td className="px-2 py-1.5 font-mono text-muted-foreground">{row.eps ?? "—"}</td>
                      <td className="px-2 py-1.5 font-mono text-muted-foreground">{num(row.net_profit_yi)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <ul className="mt-2 space-y-0.5 text-[10px] text-muted-foreground/70">
            {data.gaps.map((gap, i) => <li key={`g${i}`}>· {gap.label ? `${gap.label}：` : ""}{gap.reason}</li>)}
            {data.notes.map((note, i) => <li key={`n${i}`}>· {note}</li>)}
          </ul>
        </>
      )}
      {data && !data.ok && <p className="py-2 text-xs text-muted-foreground">{data.error}</p>}
    </GlassCard>
  );
}
