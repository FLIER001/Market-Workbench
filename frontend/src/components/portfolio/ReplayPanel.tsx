import { useState } from "react";
import { AlertCircle, ChevronDown, ChevronUp, History, Loader2 } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type ReplayData, type ReplayScenario } from "@/lib/api";
import { cn } from "@/lib/utils";

const pct = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${(v * 100).toFixed(digits)}%`;
const color = (v: number | null | undefined) =>
  v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground";

/**
 * 历史情景回放：按现在的持仓比例，把几段历史大跌期重放一遍。
 * 只回看，不预测；有价格的持仓不到一半市值时不给组合数字，只列各只涨跌。
 */
export function ReplayPanel({ holdings }: { holdings: number }) {
  const [data, setData] = useState<ReplayData | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  async function run(refresh = false) {
    setLoading(true);
    setErr(null);
    try {
      const next = await api.portfolioReplay(refresh);
      setData(next);
      setOpen((current) => current ?? next.scenarios.find((s) => s.portfolio)?.id ?? null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "回放失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <GlassCard className="mb-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 font-semibold">
          <History className="h-4 w-4 text-primary" /> 历史情景回放
        </h3>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {data?.benchmark_label && <span>对照 {data.benchmark_label}</span>}
          <button
            onClick={() => run(true)}
            disabled={loading || holdings === 0}
            className="rounded-lg border border-border/60 px-2.5 py-1 transition hover:border-primary/60 hover:text-foreground disabled:opacity-50"
          >
            {loading ? "回放中…" : data ? "重新回放" : "开始回放"}
          </button>
        </div>
      </div>

      {holdings === 0 && (
        <p className="py-6 text-center text-sm text-muted-foreground/60">还没有持仓，先记一笔再回放。</p>
      )}

      {err && (
        <p className="flex items-center gap-1.5 py-3 text-sm text-warning">
          <AlertCircle className="h-4 w-4" /> {err}
        </p>
      )}

      {loading && !data && (
        <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> 拉上市以来的日线、逐段对齐…（首次较慢）
        </p>
      )}

      {data && !data.ok && <p className="py-3 text-sm text-muted-foreground">{data.error}</p>}

      {data?.ok && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  <th className="px-2 py-2 font-medium">时期</th>
                  <th className="px-2 py-2 font-medium">组合涨跌</th>
                  <th className="px-2 py-2 font-medium">期间最大回撤</th>
                  <th className="px-2 py-2 font-medium">最差单日</th>
                  <th className="px-2 py-2 font-medium">同期基准</th>
                  <th className="px-2 py-2 font-medium">覆盖</th>
                </tr>
              </thead>
              <tbody>
                {data.scenarios.map((s) => (
                  <ReplayRow key={s.id} s={s} open={open === s.id} onToggle={() => setOpen(open === s.id ? null : s.id)} />
                ))}
              </tbody>
            </table>
          </div>
          <ul className="mt-3 space-y-0.5 text-[11px] text-muted-foreground/70">
            {data.notes.map((note, i) => <li key={i}>· {note}</li>)}
          </ul>
        </>
      )}
    </GlassCard>
  );
}

function ReplayRow({ s, open, onToggle }: { s: ReplayScenario; open: boolean; onToggle: () => void }) {
  const p = s.portfolio;
  const b = s.benchmark;
  return (
    <>
      <tr className={cn("border-b border-border/30", open && "bg-primary/[0.035]")}>
        <td className="px-2 py-2.5">
          <button onClick={onToggle} className="inline-flex items-center gap-1 text-left hover:text-primary">
            {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            <span>{s.label}</span>
          </button>
          <span className="ml-1.5 block font-mono text-[10px] text-muted-foreground/60">{s.start} → {s.end}</span>
        </td>
        <td className={cn("px-2 py-2.5 font-mono", color(p?.return))}>{pct(p?.return)}</td>
        <td className="px-2 py-2.5 font-mono text-muted-foreground">{pct(p?.max_drawdown)}</td>
        <td className={cn("px-2 py-2.5 font-mono", color(p?.worst_day))}>
          {pct(p?.worst_day)}
          {p?.worst_day_date && <span className="ml-1 text-[10px] text-muted-foreground/60">{p.worst_day_date}</span>}
        </td>
        <td className="px-2 py-2.5 font-mono text-muted-foreground">
          {b ? pct(b.return) : <span title={s.benchmark_error ?? undefined} className="text-warning">—</span>}
        </td>
        <td className="px-2 py-2.5 font-mono text-xs text-muted-foreground">{pct(s.coverage)}</td>
      </tr>
      {open && (
        <tr className="border-b border-border/30 bg-black/20">
          <td colSpan={6} className="px-3 py-2.5">
            {s.portfolio_note && <p className="mb-1.5 text-[11px] text-warning">{s.portfolio_note}</p>}
            {s.benchmark_error && <p className="mb-1.5 text-[11px] text-warning">{s.benchmark_error}</p>}
            {s.holdings.length > 0 && (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 font-medium">标的</th>
                    <th className="py-1 font-medium">权重</th>
                    <th className="py-1 font-medium">区间涨跌</th>
                    <th className="py-1 font-medium">贡献</th>
                    <th className="py-1 font-medium">停牌日</th>
                  </tr>
                </thead>
                <tbody>
                  {s.holdings.map((h) => (
                    <tr key={h.code}>
                      <td className="py-1">
                        <span>{h.name}</span>
                        <span className="ml-1 font-mono text-[10px] text-muted-foreground/60">{h.code}</span>
                      </td>
                      <td className="py-1 font-mono text-muted-foreground">{pct(h.weight)}</td>
                      <td className={cn("py-1 font-mono", color(h.return))}>{pct(h.return)}</td>
                      <td className={cn("py-1 font-mono", color(h.contribution))}>{pct(h.contribution)}</td>
                      <td className="py-1 font-mono text-muted-foreground">{h.halted_days || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {s.gaps.map((gap, i) => (
              <p key={i} className="mt-1 text-[11px] text-muted-foreground/70">· 没算进这段：{gap.name || gap.code} — {gap.reason}</p>
            ))}
          </td>
        </tr>
      )}
    </>
  );
}
