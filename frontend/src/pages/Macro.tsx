import { useEffect, useState } from "react";
import { RefreshCw, Globe, TrendingUp, Factory, Coins, Ship, Droplets, Landmark } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Sparkline } from "@/components/ui/Sparkline";
import { api, type MacroData, type MacroIndicator, type HistPoint } from "@/lib/api";
import { storageGet, storageSet, storageRemove } from "@/lib/storage";
import { cn } from "@/lib/utils";

const CACHE_KEY = "vr-macro-v1";

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

function loadCached(): MacroData | null {
  try {
    const raw = storageGet(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as MacroData;
    if (isValidMacro(parsed)) return parsed;
    storageRemove(CACHE_KEY);
    return null;
  } catch {
    return null;
  }
}

const fmt = (v: number | null | undefined, suffix = "") =>
  v == null ? "—" : `${v.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${suffix}`;

// A股红涨绿跌（与全站一致）
const tone = (v: number | null | undefined) =>
  v != null && v > 0 ? "text-danger" : v != null && v < 0 ? "text-success" : "text-muted-foreground";

// 指标卡片
function IndicatorCard({ ind, suffix }: { ind: MacroIndicator; suffix?: string }) {
  if (!isNum(ind?.value) || !isHist(ind?.hist)) return null;
  const vsForecast = ind.forecast != null ? ind.value - ind.forecast : null;
  const vsPrev = ind.prev != null ? ind.value - ind.prev : null;
  // PMI 景气类（新订单/生产/出口/预期/综合等）以 50 为荣枯线；购进/出厂价格是压力指标，不适用
  const isPricePmi = ind.label.includes("购进") || ind.label.includes("出厂");
  const isPmi = ind.label.includes("PMI") && !isPricePmi;
  // 景气 PMI 用荣枯线；价格 PMI 与常规指标按是否改善（prev 对比）判断颜色
  const aboveLine = isPmi ? ind.value >= 50 : ind.value > 0;
  const improving = vsPrev != null ? vsPrev > 0 : aboveLine;

  return (
    <GlassCard className="p-4">
      <div className="mb-1 flex items-baseline justify-between">
        <p className="text-xs text-muted-foreground">{ind.label}</p>
        <span className="text-[10px] text-muted-foreground/50">{ind.date}</span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className={cn("font-mono text-xl font-bold", tone(ind.value))}>
          {fmt(ind.value, suffix)}
        </span>
        {isPmi && (
          <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium",
            aboveLine ? "bg-danger/10 text-danger" : "bg-success/10 text-success")}>
            {aboveLine ? "荣枯线上" : "荣枯线下"}
          </span>
        )}
      </div>
      {/* 预期/前值对比 */}
      <div className="mt-0.5 flex items-center gap-3 text-[10px] text-muted-foreground/60">
        {ind.forecast != null && (
          <span>预期 {fmt(ind.forecast, suffix)}
            {vsForecast != null && (
              <span className={cn("ml-0.5", tone(vsForecast))}>
                {vsForecast > 0 ? "↑" : vsForecast < 0 ? "↓" : ""}
              </span>
            )}
          </span>
        )}
        {ind.prev != null && (
          <span>前值 {fmt(ind.prev, suffix)}
            {vsPrev != null && (
              <span className={cn("ml-0.5", tone(vsPrev))}>
                {vsPrev > 0 ? "↑" : vsPrev < 0 ? "↓" : ""}
              </span>
            )}
          </span>
        )}
      </div>
      {ind.hist.length > 1 && (
        <Sparkline data={ind.hist} height={44} className="mt-2"
          color={(isPmi ? aboveLine : improving) ? "--danger" : "--success"} valueSuffix={suffix || ""} />
      )}
    </GlassCard>
  );
}

const GROUP_META: Record<string, { icon: typeof Globe; desc: string }> = {
  "增长": { icon: TrendingUp, desc: "GDP / 工业增加值 / 营收 / 设备投资 · 生产端" },
  "物价": { icon: Globe, desc: "CPI / 核心 CPI / PPI · 通胀与工业价格" },
  "景气": { icon: Factory, desc: "官方 + 财新 PMI 及分项 · 荣枯线" },
  "价格价差": { icon: TrendingUp, desc: "PMI 购进-出厂价格 · 利润率压力" },
  "信用": { icon: Coins, desc: "M1/M2 / 社融 / 信贷分部门 / 私人信用脉冲" },
  "货币流动性": { icon: Droplets, desc: "财政存款 / 贷款需求 · 流动性" },
  "财政地产": { icon: Landmark, desc: "财政收支 / 专项债 / 地产销售与资金" },
  "盈利": { icon: TrendingUp, desc: "工业企业利润 / 库存周期" },
  "外贸全球": { icon: Ship, desc: "进出口 / 贸易差额 / 世界贸易量" },
};

export function Macro() {
  const [data, setData] = useState<MacroData | null>(loadCached);
  const [err, setErr] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    setErr(false);
    api.macro()
      .then((d) => { setData(d); if (isValidMacro(d)) storageSet(CACHE_KEY, JSON.stringify(d)); })
      .catch(() => setErr(true))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const indicators = data?.cn ?? {};
  const groups = data?.groups ?? {};
  const groupEntries = Object.entries(groups).filter(([, keys]) => keys.length > 0);

  // 单位映射
  const suffixFor = (key: string): string | undefined => {
    const pct = ["cpi", "core_cpi", "ppi", "m2", "m1", "gdp", "industrial", "industrial_revenue",
      "industrial_profit", "industrial_inventory", "exports", "imports", "m1_m2_spread",
      "social_financing_stock", "fai_equipment", "fiscal_expenditure", "fiscal_revenue_expenditure",
      "property_sales_area", "property_funds", "property_loans", "bank_survey"];
    if (pct.includes(key)) return "%";
    if (["private_credit_pulse", "price_spread"].includes(key)) return " pt";
    const yi = ["trade_balance", "social_financing", "special_bond_issuance",
      "household_ml_loan", "corp_ml_loan", "bill_financing", "fiscal_deposit"];
    if (yi.includes(key)) return " 亿";
    if (key === "trade_balance") return " 亿美元";
    return undefined;
  };

  return (
    <div>
      <PageHeader
        title="宏观面"
        subtitle="八层宏观框架 · 增长/物价/景气/信用/流动性/财政地产/盈利/外贸全球"
        actions={
          <button onClick={load} className="text-muted-foreground hover:text-primary" title="刷新">
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
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

      {groupEntries.map(([group, keys]) => {
        const meta = GROUP_META[group] ?? { icon: Globe, desc: "" };
        const Icon = meta.icon;
        return (
          <div key={group} className="mb-6">
            <div className="mb-3 flex items-center gap-2">
              <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
                <Icon className="h-4 w-4" /> {group}
              </h3>
              <span className="text-[11px] text-muted-foreground/50">{meta.desc}</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {keys.map((k) => {
                const ind = indicators[k];
                return ind ? <IndicatorCard key={k} ind={ind} suffix={suffixFor(k)} /> : null;
              })}
            </div>
          </div>
        );
      })}

      {data?.updated && (
        <p className="mt-2 text-right text-[11px] text-muted-foreground/50">数据更新：{data.updated}</p>
      )}
    </div>
  );
}
