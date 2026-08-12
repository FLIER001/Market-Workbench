import { useEffect, useState } from "react";
import {
  RefreshCw, Globe, TrendingUp, Factory, Coins, Landmark, Gauge, Activity, Zap,
  ChevronDown,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Sparkline } from "@/components/ui/Sparkline";
import { api, type MacroData, type MacroIndicator, type MacroModule, type MacroSubModule, type HistPoint } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isHist = (v: unknown): v is HistPoint[] =>
  Array.isArray(v) && v.every((p) => p != null && isNum((p as HistPoint).v) && typeof (p as HistPoint).date === "string");

function isValidMacro(d: MacroData | null): d is MacroData {
  if (!d || typeof d !== "object") return false;
  if (d.cn != null) {
    if (typeof d.cn !== "object") return false;
    for (const item of Object.values(d.cn)) {
      if (!item || !isNum(item.value) || !isHist(item.hist)) return false;
    }
  }
  return true;
}

const fmt = (v: number | null | undefined, suffix = "") =>
  v == null ? "—" : `${v.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${suffix}`;

// A股红涨绿跌（与全站一致）
const tone = (v: number | null | undefined) =>
  v != null && v > 0 ? "text-danger" : v != null && v < 0 ? "text-success" : "text-muted-foreground";

// 图标名 → 组件
const ICONS: Record<string, typeof Globe> = {
  Globe, TrendingUp, Factory, Coins, Landmark, Gauge, Activity, Zap,
};

// 模块得分 → 颜色与状态（0-100，越高越有利）
function scoreTone(score: number) {
  if (score >= 70) return { text: "text-danger", bg: "bg-danger/10", bar: "bg-danger/60", label: "偏热" };
  if (score >= 55) return { text: "text-warning", bg: "bg-warning/10", bar: "bg-warning/60", label: "偏暖" };
  if (score >= 45) return { text: "text-muted-foreground", bg: "bg-muted/20", bar: "bg-primary/40", label: "中性" };
  if (score >= 30) return { text: "text-success", bg: "bg-success/10", bar: "bg-success/60", label: "偏冷" };
  return { text: "text-success", bg: "bg-success/10", bar: "bg-success/60", label: "收缩" };
}

// 景气状态徽章（V1.0 §9）
function ClimateBadges({ mod }: { mod: MacroModule }) {
  if (!mod.state || mod.score == null) return null;
  const st = scoreTone(mod.score);
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1">
      <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-semibold", st.bg, st.text)}>{mod.state}</span>
      {mod.mom != null && (
        <span className={cn("rounded bg-muted/30 px-1.5 py-0.5 font-mono text-[10px]",
          mod.mom > 0 ? "text-danger" : mod.mom < 0 ? "text-success" : "text-muted-foreground")}>
          {mod.mom > 0 ? "+" : ""}{mod.mom.toFixed(1)} {mod.direction ?? ""}
        </span>
      )}
      {mod.quadrant && (
        <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
          {mod.quadrant}
        </span>
      )}
    </div>
  );
}

// 紧凑模块 chip：图标 + 名称 + 分数 + 分位条，点击展开明细。
function ModuleChip({ mod, open, onToggle }: { mod: MacroModule; open: boolean; onToggle: () => void }) {
  const t = mod.score != null ? scoreTone(mod.score) : null;
  const Icon = ICONS[mod.icon] ?? Globe;
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "group w-full rounded-lg border border-border/40 bg-muted/10 px-3 py-2.5 text-left transition-colors",
        "hover:border-primary/40 hover:bg-muted/20",
        open && "border-primary/50 bg-primary/5",
      )}
    >
      <div className="flex items-center gap-2">
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground group-hover:text-primary" />
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-foreground">{mod.name}</span>
        <ChevronDown className={cn("h-3 w-3 shrink-0 text-muted-foreground/40 transition-transform", open && "rotate-180")} />
      </div>
      <div className="mt-1.5 flex items-baseline gap-1.5">
        {mod.score != null && t ? (
          <>
            <span className={cn("font-mono text-xl font-extrabold leading-none", t.text)}>{mod.score.toFixed(1)}</span>
            <span className={cn("rounded px-1 py-px text-[9px] font-medium leading-tight", t.bg, t.text)}>{t.label}</span>
          </>
        ) : (
          <span className="text-[11px] font-semibold text-muted-foreground/60">覆盖不足</span>
        )}
      </div>
      {mod.score != null && t && (
        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-muted/20">
          <div className={cn("h-full rounded-full transition-all", t.bar)} style={{ width: `${mod.score}%` }} />
        </div>
      )}
      <ClimateBadges mod={mod} />
    </button>
  );
}

// 景气子模块分组块
function SubModuleBlock({ sub, indicators }: { sub: MacroSubModule; indicators: Record<string, MacroIndicator> }) {
  const t = sub.score != null ? scoreTone(sub.score) : null;
  const keys = sub.indicators.filter((k) => indicators[k]);
  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-xs font-semibold text-foreground">{sub.name}</span>
        <span className="text-[10px] text-muted-foreground/50">权重 {sub.weight}%</span>
        <span className="text-[10px] text-muted-foreground/40">覆盖 {(sub.coverage ?? 0).toFixed(0)}% · 置信度 {(sub.confidence ?? 0).toFixed(0)}%</span>
        {sub.score != null && t ? (
          <span className={cn("ml-auto rounded px-1.5 py-0.5 font-mono text-[11px] font-bold", t.bg, t.text)}>
            {sub.score.toFixed(1)}
          </span>
        ) : (
          <span className="ml-auto rounded bg-muted/20 px-1.5 py-0.5 text-[10px] text-muted-foreground/50">覆盖不足</span>
        )}
      </div>
      {sub.score != null && t && (
        <div className="mb-2 h-1 w-full overflow-hidden rounded-full bg-muted/20">
          <div className={cn("h-full rounded-full", t.bar)} style={{ width: `${sub.score}%` }} />
        </div>
      )}
      {keys.length > 0 ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {keys.map((k) => (
            <IndicatorCard key={k} ind={indicators[k]} suffix={suffixFor(k)} />
          ))}
        </div>
      ) : (
        <p className="text-[10px] text-muted-foreground/40">该子模块有效覆盖不足；名义权重仍保留，不会放大其他指标。</p>
      )}
      {/* 子模块内指标分位贡献 */}
      {sub.used.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
          {sub.used.map((u) => (
            <span key={u.key} className="text-[10px] text-muted-foreground/70">
              {indicators[u.key]?.label ?? u.key}
              <span className={cn("ml-1 font-mono", u.pct > 70 ? "text-danger" : u.pct < 30 ? "text-success" : "")}>
                {u.pct.toFixed(0)}
              </span>
              <span className="ml-0.5 text-muted-foreground/40">
                ({u.weight}%{u.freshness != null && u.freshness < 1 ? `·滞后×${u.freshness}` : ""})
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// 具体指标卡
function IndicatorCard({ ind, suffix }: { ind: MacroIndicator; suffix?: string }) {
  if (!isNum(ind?.value)) return null;
  const vsForecast = ind.forecast != null ? ind.value - ind.forecast : null;
  const vsPrev = ind.prev != null ? ind.value - ind.prev : null;
  const isPricePmi = ind.label.includes("购进") || ind.label.includes("出厂");
  const isPmi = ind.label.includes("PMI") && !isPricePmi;
  const aboveLine = isPmi ? ind.value >= 50 : ind.value > 0;
  const improving = vsPrev != null ? vsPrev > 0 : aboveLine;
  const hist = isHist(ind.hist) ? ind.hist : [];
  const statusText = ind.meta?.status === "fallback" ? "回退" : ind.meta?.status === "stale" ? "滞后" : "已更新";
  const statusTone = ind.meta?.status === "fresh" ? "text-success" : "text-warning";

  return (
    <div className="rounded-lg border border-border/40 bg-muted/10 p-3">
      <div className="mb-0.5 flex items-baseline justify-between">
        <p className="text-[11px] text-muted-foreground">{ind.label}</p>
        <span className="flex items-center gap-1 text-[10px] text-muted-foreground/50">
          {ind.meta && <span className={statusTone}>{statusText}</span>}{ind.date}
        </span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className={cn("font-mono text-lg font-bold", tone(ind.value))}>
          {fmt(ind.value, suffix)}
        </span>
        {isPmi && (
          <span className={cn("rounded px-1 py-0.5 text-[9px] font-medium",
            aboveLine ? "bg-danger/10 text-danger" : "bg-success/10 text-success")}>
            {aboveLine ? "线上" : "线下"}
          </span>
        )}
      </div>
      <div className="mt-0.5 flex items-center gap-2.5 text-[10px] text-muted-foreground/60">
        {ind.forecast != null && (
          <span>预期 {fmt(ind.forecast, suffix)}
            {vsForecast != null && <span className={cn("ml-0.5", tone(vsForecast))}>{vsForecast > 0 ? "↑" : vsForecast < 0 ? "↓" : ""}</span>}
          </span>
        )}
        {ind.prev != null && (
          <span>前值 {fmt(ind.prev, suffix)}
            {vsPrev != null && <span className={cn("ml-0.5", tone(vsPrev))}>{vsPrev > 0 ? "↑" : vsPrev < 0 ? "↓" : ""}</span>}
          </span>
        )}
      </div>
      {hist.length > 1 && (
        <Sparkline data={hist} height={38} className="mt-1.5"
          color={(isPmi ? aboveLine : improving) ? "--danger" : "--success"} valueSuffix={suffix || ""} />
      )}
      {ind.source && (
        <p className="mt-1 text-[9px] text-muted-foreground/40">
          {ind.meta?.source_url ? (
            <a href={ind.meta.source_url} target="_blank" rel="noreferrer" className="hover:text-primary" onClick={(e) => e.stopPropagation()}>
              {ind.source}
            </a>
          ) : ind.source}
          {ind.meta && <span> · {ind.meta.quality === "proxy" ? "代理" : ind.meta.quality === "derived" ? "派生" : "直接"} · {ind.meta.frequency === "daily" ? "日" : ind.meta.frequency === "quarterly" ? "季" : "月"}频</span>}
        </p>
      )}
    </div>
  );
}

// 单位映射
function suffixFor(key: string): string | undefined {
  const pct = ["cpi", "core_cpi", "ppi", "m2", "m1", "gdp", "industrial", "industrial_revenue",
    "industrial_profit", "industrial_inventory", "exports", "imports", "m1_m2_spread",
    "social_financing_stock", "fai_equipment", "fiscal_expenditure", "fiscal_revenue_expenditure",
    "property_sales_area", "property_funds", "property_loans", "bank_survey",
    "private_credit_growth", "market_breadth", "eps_revision_breadth", "mkt_margin_balance",
    "profit_breadth", "equity_risk_premium", "new_high_breadth"];
  if (pct.includes(key)) return "%";
  if (key === "world_trade_yoy_3mma") return "%";
  if (["price_spread", "industrial_momentum", "services_momentum",
    "order_inventory_spread"].includes(key)) return " pt";
  if (["dr007_policy_spread", "ncd_aaa_spread", "credit_spread_aaa"].includes(key)) return " bp";
  const yi = ["trade_balance", "social_financing", "special_bond_issuance",
    "household_ml_loan", "corp_ml_loan", "bill_financing", "fiscal_deposit", "nonbank_deposit",
    "mkt_margin_netbuy"];
  if (key === "trade_balance") return " 亿美元";
  if (yi.includes(key)) return " 亿";
  if (key === "policy_execution") return " /3";
  return undefined;
}

export function Macro() {
  const [err, setErr] = useState(false);
  const [openModule, setOpenModule] = useState<string | null>(null);
  const { data, loading, revalidating, revalidate } = useSWR<MacroData>(
    "macro:v5",
    async (fresh) => {
      const d = await api.macro(fresh);
      if (!isValidMacro(d)) throw new Error("宏观数据格式异常");
      return d;
    }, [], () => setErr(true), { persist: true },
  );
  const load = () => { setErr(false); void revalidate(true); };
  useEffect(() => {
    const tick = () => { if (!document.hidden) void revalidate(); };
    const timer = window.setInterval(tick, 30 * 60_000);
    const onVisible = () => { if (!document.hidden) void revalidate(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [revalidate]);

  const indicators = data?.cn ?? {};
  const modules = data?.modules ?? [];
  const clusters = data?.clusters?.length ? data.clusters : [
    { name: "宏观模块", desc: "", modules: modules.map((m) => m.name) },
  ];
  const active = modules.find((m) => m.name === openModule);

  return (
    <div>
      <PageHeader
        title="宏观面"
        subtitle="五类因果层 · 八模块状态 · 点击模块展开具体指标"
        actions={
          <button onClick={load} className="text-muted-foreground hover:text-primary" title="刷新">
            <RefreshCw className={cn("h-4 w-4", (loading || revalidating) && "animate-spin")} />
          </button>
        }
      />

      {err && !data && (
        <GlassCard className="mb-6 p-6 text-center text-sm text-muted-foreground">
          数据加载失败，请稍后重试。
        </GlassCard>
      )}

      {data?.stale && (
        <GlassCard className="mb-6 flex items-center gap-2 border-warning/30 bg-warning/5 p-3 text-xs text-warning">
          <span>数据源暂不可用，当前显示缓存数据{data.stale_since ? `（${data.stale_since}）` : ""}，稍后可点右上角刷新重试。</span>
        </GlassCard>
      )}

      {/* 五类因果层 × 八模块：五列等宽流程图，左→右即宏观传导方向；一级聚类不另造总分。 */}
      <div className="mb-2 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
          <Gauge className="h-4 w-4" /> 宏观模块状态
        </h3>
        <span className="text-[11px] text-muted-foreground/50">0-100 分位 · 点击模块展开指标</span>
        {data?.updated && <span className="ml-auto text-[11px] text-muted-foreground/50">{data.updated}</span>}
      </div>
      <div className="mb-4 grid gap-2 md:grid-cols-3 lg:grid-cols-5">
        {clusters.map((cluster, ci) => {
          const rows = cluster.modules.map((name) => modules.find((m) => m.name === name)).filter((m): m is MacroModule => Boolean(m));
          if (!rows.length) return null;
          return (
            <section key={cluster.name} className="relative flex flex-col rounded-xl border border-border/30 bg-muted/5 p-2.5">
              {/* 聚类标题 + 因果箭头（桌面端列间显示传导方向） */}
              <div className="mb-2 px-0.5">
                <h4 className="flex items-center gap-1 text-[11px] font-semibold text-foreground">
                  {cluster.name}
                  {ci < clusters.length - 1 && (
                    <span className="hidden text-muted-foreground/30 lg:inline">→</span>
                  )}
                </h4>
                <p className="mt-0.5 text-[9px] leading-snug text-muted-foreground/50">{cluster.desc}</p>
              </div>
              <div className="flex flex-col gap-2">
                {rows.map((m) => (
                  <ModuleChip key={m.name} mod={m} open={openModule === m.name}
                    onToggle={() => setOpenModule((cur) => (cur === m.name ? null : m.name))} />
                ))}
              </div>
            </section>
          );
        })}
      </div>

      {/* 展开的指标明细 */}
      {active && (
        <GlassCard className="mb-6 p-4">
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
            <h3 className="text-sm font-semibold text-foreground">{active.name} · 指标明细</h3>
            <span className="text-[11px] text-muted-foreground/50">{active.desc}</span>
            <div className="ml-auto flex items-center gap-2">
              <span className="text-[10px] text-muted-foreground/50">
                覆盖 {(active.coverage ?? 0).toFixed(0)}% · 置信度 {(active.confidence ?? 0).toFixed(0)}%
              </span>
              <span className="rounded bg-muted/30 px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                {active.score != null ? `模块得分 ${active.score.toFixed(1)}` : "覆盖不足"}
              </span>
            </div>
          </div>
          {/* 近12个月模块得分走势（从总览卡收进来，明细处展开） */}
          {isHist(active.hist) && active.hist.length > 1 && (
            <div className="mb-3">
              <span className="text-[10px] text-muted-foreground/45">近12个月模块得分</span>
              <Sparkline data={active.hist} height={48} className="mt-1" color="--primary" valueSuffix=" 分" showLatest />
            </div>
          )}
          {active.submodules && active.submodules.length > 0 ? (
            // 国内增长与景气模块：按 4 子模块分组展示
            <div className="space-y-4">
              {active.submodules.map((sub) => (
                <SubModuleBlock key={sub.name} sub={sub} indicators={indicators} />
              ))}
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {active.indicators.map((k) => {
                const ind = indicators[k];
                return ind ? <IndicatorCard key={k} ind={ind} suffix={suffixFor(k)} /> : null;
              })}
            </div>
          )}
          {/* 各指标对得分的贡献（方向调整后分位）；景气模块已在子模块内展示，跳过 */}
          {!active.submodules && active.used.length > 0 && (
            <div className="mt-3 border-t border-border/40 pt-2">
              <p className="mb-1.5 text-[10px] text-muted-foreground/50">各指标分位贡献（已按方向调整，权重见括号）</p>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {active.used.map((u) => (
                  <span key={u.key} className="text-[10px] text-muted-foreground/70">
                    {indicators[u.key]?.label ?? u.key}
                    <span className={cn("ml-1 font-mono", u.pct > 70 ? "text-danger" : u.pct < 30 ? "text-success" : "")}>
                      {u.pct.toFixed(0)}
                    </span>
                    <span className="ml-0.5 text-muted-foreground/40">({u.direction === "down" ? "↓" : "↑"}{u.weight})</span>
                  </span>
                ))}
              </div>
            </div>
          )}
        </GlassCard>
      )}

      {!active && modules.length > 0 && (
        <p className="mb-6 text-center text-[11px] text-muted-foreground/50">
          点击上方任意模块，展开该模块的具体指标、趋势与分位贡献
        </p>
      )}
    </div>
  );
}
