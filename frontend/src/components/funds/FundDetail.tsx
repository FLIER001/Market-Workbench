import { useEffect, useState } from "react";
import { Loader2, Star, StarOff, X } from "lucide-react";
import { api, type FundProfile } from "@/lib/api";
import { loadFundWatch, toggleFundWatch } from "@/lib/fundWatch";
import { cn } from "@/lib/utils";
import { FundNavChart } from "./FundNavChart";

interface Props {
  code: string;
  name?: string;
  onClose: () => void;
  onWatchChange?: () => void;
}

const fmtPct = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v}%`);
const pctColor = (v: number | null | undefined) =>
  v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground";

// 基金详情抽屉：净值走势 + 业绩指标 + 最新十大重仓。
export function FundDetail({ code, name, onClose, onWatchChange }: Props) {
  const [profile, setProfile] = useState<FundProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [watched, setWatched] = useState(() => loadFundWatch().some((x) => x.code === code));

  useEffect(() => {
    setLoading(true);
    api.fundProfile(code)
      .then((p) => setProfile(p))
      .catch(() => setProfile(null))
      .finally(() => setLoading(false));
  }, [code]);

  const m = profile?.metrics;
  return (
    <div className="mt-2 rounded-2xl border border-border/70 bg-black/20 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="font-mono text-xs text-muted-foreground">{code}</span>
          <span className="ml-2 font-semibold">{profile?.name || name || code}</span>
          {profile?.type && <span className="ml-2 rounded-md bg-black/30 px-1.5 py-0.5 text-xs text-muted-foreground">{profile.type}</span>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            title={watched ? "移出自选" : "加入自选"}
            onClick={() => {
              toggleFundWatch({ code, name: profile?.name || name || code, type: profile?.type });
              setWatched(!watched);
              onWatchChange?.();
            }}
            className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-black/30 hover:text-primary"
          >
            {watched ? <Star className="h-4 w-4 fill-primary text-primary" /> : <StarOff className="h-4 w-4" />}
          </button>
          <button onClick={onClose} className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-black/30">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {loading ? (
        <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> 加载档案…</div>
      ) : (
        <>
          {/* 左：净值历史线；右：重仓股及当日涨跌幅 */}
          <div className="grid gap-4 md:grid-cols-2">
            <div className="min-w-0">
              <FundNavChart code={code} />
            </div>
            <div className="min-w-0">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                最新重仓{profile?.holdings_quarter ? `（${profile.holdings_quarter.replace("股票投资明细", "")}）` : ""}
                <span className="ml-2 font-normal normal-case text-muted-foreground/70">当日涨跌幅</span>
              </div>
              {profile?.holdings?.length ? (
                <ul className="space-y-1 text-sm">
                  {profile.holdings.map((h) => (
                    <li key={h.stock_code} className="flex items-center justify-between gap-2 rounded-lg px-1 py-0.5 hover:bg-black/10">
                      <span className="flex min-w-0 items-baseline gap-1.5">
                        <span className="truncate">{h.stock_name}</span>
                        <span className="shrink-0 font-mono text-xs text-muted-foreground">{h.weight ?? "—"}%</span>
                      </span>
                      <span className={cn("shrink-0 font-mono text-xs font-semibold", pctColor(h.change_pct))}>
                        {fmtPct(h.change_pct)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-sm text-muted-foreground">暂无持仓明细（可能为债基/货基/指数联接）</div>
              )}
            </div>
          </div>

          <div className="mt-4">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">近一年指标（净值自算）</div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric label="年化收益" value={fmtPct(m?.ann_return)} cls={pctColor(m?.ann_return)} />
              <Metric label="最大回撤" value={fmtPct(m?.max_drawdown)} cls="text-success" />
              <Metric label="年化波动率" value={fmtPct(m?.volatility)} cls="text-muted-foreground" />
              <Metric label="夏普比率" value={m?.sharpe != null ? String(m.sharpe) : "—"} cls={pctColor(m?.sharpe)} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Metric({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-xl bg-black/20 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 font-mono font-semibold", cls)}>{value}</div>
    </div>
  );
}
