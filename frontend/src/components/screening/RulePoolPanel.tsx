import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, ChevronDown, ChevronUp, Loader2, ListChecks } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type PoolResult } from "@/lib/api";
import { useHoldingCodes } from "@/hooks/useHoldingCodes";
import { flattenWatchGroups, loadWatchGroups } from "@/lib/watchlist";
import { cn } from "@/lib/utils";

type Range = { min: string; max: string };
const EMPTY: Range = { min: "", max: "" };
const FIELDS = [
  { key: "pe_ttm", label: "市盈率 TTM", unit: "倍" },
  { key: "pb", label: "市净率", unit: "倍" },
  { key: "mcap_yi", label: "总市值", unit: "亿" },
  { key: "turnover_pct", label: "当日换手率", unit: "%" },
] as const;

const pct = (v: number | null) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);
const color = (v: number | null) => (v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "");

const GROUP_META = {
  pass: { label: "全部满足", cls: "text-success" },
  missing: { label: "有缺数据", cls: "text-warning" },
  fail: { label: "有不满足", cls: "text-muted-foreground" },
} as const;

/**
 * 规则核对候选池：按条件逐只核对，只排除、不排名；每只每条写明满足 / 不满足 / 缺数据。
 * 和「个股」那个 tab 的 AI 逐项核验不同，这里同一组条件跑两次结果一样。
 */
export function RulePoolPanel() {
  const holdingCodes = useHoldingCodes();
  const [universes, setUniverses] = useState<{ key: string; label: string }[]>([]);
  const [universe, setUniverse] = useState("watch");
  const [ranges, setRanges] = useState<Record<string, Range>>({
    pe_ttm: { ...EMPTY }, pb: { ...EMPTY }, mcap_yi: { ...EMPTY }, turnover_pct: { ...EMPTY },
  });
  const [excludeSt, setExcludeSt] = useState(true);
  const [excludeHalted, setExcludeHalted] = useState(true);
  const [data, setData] = useState<PoolResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [openGroup, setOpenGroup] = useState<keyof typeof GROUP_META | null>("pass");

  const codes = useMemo(() => {
    const watch = [
      ...flattenWatchGroups(loadWatchGroups("stock")),
      ...flattenWatchGroups(loadWatchGroups("etf")),
    ];
    return Array.from(new Set([...holdingCodes, ...watch]));
  }, [holdingCodes]);

  useEffect(() => {
    api.poolUniverses().then(setUniverses).catch(() => setUniverses([]));
  }, []);

  async function run() {
    setLoading(true);
    setErr(null);
    try {
      const conditions: Record<string, { min?: number; max?: number }> = {};
      for (const [key, range] of Object.entries(ranges)) {
        const min = range.min.trim() === "" ? undefined : Number(range.min);
        const max = range.max.trim() === "" ? undefined : Number(range.max);
        if (min === undefined && max === undefined) continue;
        conditions[key] = { min, max };
      }
      const result = await api.screeningPool({
        universe,
        codes,
        exclude_st: excludeSt,
        exclude_halted: excludeHalted,
        ...conditions,
      });
      setData(result);
      setOpenGroup(result.counts.pass > 0 ? "pass" : result.counts.missing > 0 ? "missing" : "fail");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "核对失败");
    } finally {
      setLoading(false);
    }
  }

  const byCode = useMemo(() => {
    const map = new Map<string, PoolResult["rows"][number]>();
    for (const row of data?.rows ?? []) map.set(row.code, row);
    return map;
  }, [data]);

  return (
    <div className="space-y-4">
      <GlassCard>
        <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold">
          <ListChecks className="h-4 w-4 text-primary" /> 规则核对
          <span className="text-xs font-normal text-muted-foreground/60">只排除、不排名；每条写明满足 / 不满足 / 缺数据</span>
        </h3>

        <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">范围</span>
          <select
            value={universe}
            onChange={(e) => setUniverse(e.target.value)}
            className="rounded-lg border border-border/60 bg-background/40 px-2 py-1.5 outline-none hover:border-primary/50"
          >
            <option value="watch">自选 / 持仓（{codes.length} 只）</option>
            {universes.map((u) => <option key={u.key} value={u.key}>{u.label} 成分股</option>)}
          </select>
          <label className="ml-2 inline-flex items-center gap-1.5">
            <input type="checkbox" checked={excludeSt} onChange={(e) => setExcludeSt(e.target.checked)} />
            排除 ST / 退市整理
          </label>
          <label className="inline-flex items-center gap-1.5">
            <input type="checkbox" checked={excludeHalted} onChange={(e) => setExcludeHalted(e.target.checked)} />
            排除当日无成交
          </label>
        </div>

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {FIELDS.map((field) => (
            <div key={field.key} className="rounded-lg border border-border/50 bg-background/30 px-2.5 py-2">
              <p className="mb-1 text-[11px] text-muted-foreground">{field.label}（{field.unit}）</p>
              <div className="flex items-center gap-1.5">
                <input
                  value={ranges[field.key].min}
                  onChange={(e) => setRanges({ ...ranges, [field.key]: { ...ranges[field.key], min: e.target.value } })}
                  placeholder="下限"
                  inputMode="decimal"
                  className="w-full rounded border border-border/60 bg-background/40 px-1.5 py-1 text-xs outline-none focus:border-primary/60"
                />
                <span className="text-muted-foreground/50">~</span>
                <input
                  value={ranges[field.key].max}
                  onChange={(e) => setRanges({ ...ranges, [field.key]: { ...ranges[field.key], max: e.target.value } })}
                  placeholder="上限"
                  inputMode="decimal"
                  className="w-full rounded border border-border/60 bg-background/40 px-1.5 py-1 text-xs outline-none focus:border-primary/60"
                />
              </div>
            </div>
          ))}
        </div>

        <div className="mt-3 flex items-center gap-2">
          <button
            onClick={run}
            disabled={loading || (universe === "watch" && codes.length === 0)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary/80 px-3 py-1.5 text-xs font-semibold text-primary-foreground transition hover:bg-primary disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ListChecks className="h-3.5 w-3.5" />}
            {loading ? "核对中…" : "开始核对"}
          </button>
          <span className="text-[11px] text-muted-foreground/70">条件留空 = 不看这一条；区间可只填一边</span>
        </div>

        {err && <p className="mt-2 flex items-center gap-1.5 text-xs text-warning"><AlertCircle className="h-3.5 w-3.5" /> {err}</p>}
      </GlassCard>

      {data && (
        <GlassCard>
          <p className="mb-3 text-xs text-muted-foreground">
            {data.scope_label} · 共核对 {data.rows.length} 只
            {data.as_of && ` · 行情日 ${data.as_of}`}
            <span className="ml-2 text-muted-foreground/60">{data.source}</span>
          </p>

          {(["pass", "missing", "fail"] as const).map((key) => {
            const meta = GROUP_META[key];
            const list = data.groups[key];
            const open = openGroup === key;
            return (
              <div key={key} className="mb-2 rounded-lg border border-border/50">
                <button
                  onClick={() => setOpenGroup(open ? null : key)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-muted/20"
                >
                  {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                  <span className={cn("font-medium", meta.cls)}>{meta.label}</span>
                  <span className="font-mono text-muted-foreground">{list.length}</span>
                  {list.length === 0 && <span className="text-muted-foreground/50">—</span>}
                </button>
                {open && list.length > 0 && (
                  <div className="overflow-x-auto border-t border-border/40">
                    <table className="w-full min-w-[620px] text-xs">
                      <thead>
                        <tr className="border-b border-border/40 text-left text-[10px] text-muted-foreground">
                          <th className="px-3 py-1.5 font-medium">代码</th>
                          <th className="px-3 py-1.5 font-medium">名称</th>
                          <th className="px-3 py-1.5 font-medium">现价</th>
                          <th className="px-3 py-1.5 font-medium">涨跌</th>
                          <th className="px-3 py-1.5 font-medium">核验明细</th>
                        </tr>
                      </thead>
                      <tbody>
                        {list.map((code) => {
                          const row = byCode.get(code);
                          if (!row) return null;
                          return (
                            <tr key={code} className="border-b border-border/30">
                              <td className="px-3 py-1.5 font-mono text-muted-foreground">{row.code}</td>
                              <td className="px-3 py-1.5">
                                <Link to={`/stock-data?code=${row.code}`} className="hover:text-primary hover:underline">{row.name}</Link>
                              </td>
                              <td className="px-3 py-1.5 font-mono">{row.price ?? "—"}</td>
                              <td className={cn("px-3 py-1.5 font-mono", color(row.change_pct))}>{pct(row.change_pct)}</td>
                              <td className="px-3 py-1.5">
                                <div className="flex flex-wrap gap-1">
                                  {row.checks.map((check) => (
                                    <span
                                      key={check.key}
                                      title={check.reason ?? `${check.label} ${check.value ?? "—"}${check.unit}`}
                                      className={cn(
                                        "rounded border px-1.5 py-0.5 text-[10px]",
                                        check.status === "pass" && "border-border/50 text-muted-foreground",
                                        check.status === "missing" && "border-warning/40 bg-warning/10 text-warning",
                                        check.status === "fail" && "border-destructive/40 bg-destructive/10 text-destructive",
                                      )}
                                    >
                                      {check.label} {check.value ?? "—"}
                                    </span>
                                  ))}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}

          <ul className="mt-2 space-y-0.5 text-[10px] text-muted-foreground/70">
            {data.gaps.map((gap, i) => <li key={i}>· {gap}</li>)}
            <li>· {data.note}</li>
          </ul>
        </GlassCard>
      )}
    </div>
  );
}
