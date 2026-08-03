import { useEffect, useState } from "react";
import { RefreshCw, Droplets, Landmark, TrendingUp, TrendingDown, Activity, Gauge, ChevronDown } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Sparkline } from "@/components/ui/Sparkline";
import { api, type LiquidityData, type LiquidityUsItem, type HistPoint, type IndexFlow, type CompositeIndex } from "@/lib/api";
import { storageGet, storageSet, storageRemove } from "@/lib/storage";
import { cn } from "@/lib/utils";

const CACHE_KEY = "vr-liquidity-v2";

// ---- 缓存结构校验：localStorage 里可能是旧版格式，字段缺失会把渲染弄崩 ----
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isHist = (v: unknown): v is HistPoint[] =>
  Array.isArray(v) && v.every((p) => p != null && isNum((p as HistPoint).v) && typeof (p as HistPoint).date === "string");

function isValidLiquidity(d: LiquidityData | null): d is LiquidityData {
  if (!d || typeof d !== "object") return false;
  // us：value/hist 必须齐全，否则 UsCard 或美联储卡片会抛错
  if (d.us != null) {
    if (typeof d.us !== "object") return false;
    for (const item of Object.values(d.us)) {
      if (!item || !isNum(item.value) || !isHist(item.hist)) return false;
    }
  }
  // cn：直方图字段若存在必须是有效点列（Sparkline 会对其求值）
  if (d.cn != null) {
    if (typeof d.cn !== "object") return false;
    if (d.cn.rzrqye_hist != null && !isHist(d.cn.rzrqye_hist)) return false;
    if (d.cn.rzjme_hist != null && !isHist(d.cn.rzjme_hist)) return false;
    if (d.cn.index_flows != null) {
      for (const f of Object.values(d.cn.index_flows)) {
        if (!f?.latest || !isNum(f.latest.v) || !isHist(f.hist)) return false;
      }
    }
  }
  // cn_indices / us_indices：IndexCard 需要 value 为数字；components（子指标）若存在须结构合法
  for (const group of [d.cn_indices, d.us_indices]) {
    if (group != null) {
      for (const idx of Object.values(group)) {
        if (!idx || !isNum(idx.value) || !isHist(idx.hist)) return false;
        if (idx.components != null &&
            (!Array.isArray(idx.components) ||
             idx.components.some((c) => !c || typeof c.label !== "string" || typeof c.value !== "string" ||
                                 !isNum(c.pct) || (c.hist != null && !isHist(c.hist))))) return false;
      }
    }
  }
  // fed_odds：strikes 需要 prob 为数字
  if (d.fed_odds?.strikes != null) {
    if (!Array.isArray(d.fed_odds.strikes) ||
        d.fed_odds.strikes.some((s) => !isNum(s?.strike) || !isNum(s?.prob))) return false;
  }
  return true;
}

// 读取缓存：格式不符就清掉，回退为无缓存（照常走网络加载）
function loadCached(): LiquidityData | null {
  try {
    const raw = storageGet(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as LiquidityData;
    if (isValidLiquidity(parsed)) return parsed;
    storageRemove(CACHE_KEY);
    return null;
  } catch {
    return null;
  }
}

const fmt = (v: number | null | undefined, suffix = "") =>
  v == null ? "—" : `${v.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${suffix}`;
const signed = (v: number | null | undefined, suffix = "") =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${v.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${suffix}`;
const yi = (v: number | null | undefined) => signed(v, " 亿");

// A股红涨绿跌（与全站一致）
const tone = (v: number | null | undefined) =>
  v != null && v > 0 ? "text-danger" : v != null && v < 0 ? "text-success" : "text-muted-foreground";

// US 数据卡片
function UsCard({ item }: { item: LiquidityUsItem }) {
  if (!isNum(item?.value) || !isHist(item?.hist)) return null;
  const chg = item.chg;
  return (
    <GlassCard className="p-4">
      <div className="mb-1 flex items-baseline justify-between">
        <p className="text-xs text-muted-foreground">{item.label}</p>
        <span className="text-[10px] text-muted-foreground/50">{item.date}</span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-xl font-bold">{fmt(item.value, ` ${item.unit}`)}</span>
        {chg != null && (
          <span className={cn("text-xs font-mono", tone(chg))}>{signed(chg, ` ${item.unit}`)}</span>
        )}
      </div>
      <Sparkline data={item.hist} height={48} className="mt-2" valueSuffix={` ${item.unit}`} />
    </GlassCard>
  );
}

// 国内指标块
function CnMetric({ label, value, sub, hist, color }: {
  label: string; value: string; sub?: string; hist?: HistPoint[]; color?: string;
}) {
  return (
    <GlassCard className="p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn("mt-0.5 font-mono text-xl font-bold", value.startsWith("+") ? "text-danger" : value.startsWith("-") ? "text-success" : "")}>{value}</p>
      {sub && <p className="mt-0.5 text-[10px] text-muted-foreground/50">{sub}</p>}
      {hist && hist.length > 1 && <Sparkline data={hist} height={44} className="mt-2" color={color || "--primary"} />}
    </GlassCard>
  );
}

// 国内主力净流入卡片（三指数 + 合计）
function IndexFlowCard({ flow }: { flow: IndexFlow }) {
  if (!flow?.latest || !isNum(flow.latest.v)) return null;
  const latest = flow.latest;
  return (
    <GlassCard className="p-4">
      <div className="mb-1 flex items-baseline justify-between">
        <p className="text-xs text-muted-foreground">{flow.name}</p>
        <span className="text-[10px] text-muted-foreground/50">{latest.date}</span>
      </div>
      <p className={cn("font-mono text-xl font-bold", tone(latest.v))}>{yi(latest.v)}</p>
      <Sparkline data={flow.hist} height={44} className="mt-2" />
    </GlassCard>
  );
}

// 美联储加息概率条
function FedOddsBar({ strikes }: { strikes: { strike: number; prob: number }[] }) {
  const valid = (strikes ?? []).filter((s) => isNum(s?.strike) && isNum(s?.prob));
  if (valid.length === 0) return null;
  // 按 strike 从低到高排列，每档宽度 = 概率
  const sorted = [...valid].sort((a, b) => b.strike - a.strike);
  const maxProb = Math.max(...sorted.map(s => s.prob), 1);
  return (
    <div className="space-y-1.5">
      {sorted.map((s) => (
        <div key={s.strike} className="flex items-center gap-2">
          <span className="w-16 shrink-0 text-right font-mono text-[10px] text-muted-foreground">
            {s.strike.toFixed(2)}%
          </span>
          <div className="relative h-4 flex-1 overflow-hidden rounded-sm bg-muted/20">
            <div
              className="absolute inset-y-0 left-0 rounded-sm bg-primary/70 transition-all"
              style={{ width: `${(s.prob / maxProb) * 100}%` }}
            />
          </div>
          <span className={cn("w-12 shrink-0 font-mono text-[10px]", s.prob > 50 ? "font-bold text-primary" : "text-muted-foreground/60")}>
            {s.prob}%
          </span>
        </div>
      ))}
    </div>
  );
}

// 综合指数卡片：分位 + 趋势 + 解读；点击卡片展开子指标（默认折叠），关联明细在卡片行下方的整行面板
function IndexCard({ idx, open, onToggle, detail }: {
  idx: CompositeIndex; open: boolean; onToggle: () => void; detail?: string;
}) {
  // 分档 → 有利/不利/中性（按指数自身方向：favorable=low 分低有利，high 分高有利）
  if (!isNum(idx?.value)) return null;
  const lowGood = idx.favorable !== "high";
  const favorableZone = lowGood ? idx.value < 30 : idx.value > 70;
  const unfavorableZone = lowGood ? idx.value > 70 : idx.value < 30;
  const zoneColor = favorableZone ? "text-success" : unfavorableZone ? "text-danger" : "text-muted-foreground";
  const zoneBg = favorableZone ? "bg-success/10" : unfavorableZone ? "bg-danger/10" : "bg-muted/20";
  const zoneLabel = favorableZone ? "有利" : unfavorableZone ? "不利" : "中性";
  const comps = Array.isArray(idx.components) ? idx.components : [];
  const expandable = comps.length > 0 || Boolean(detail);
  return (
    <GlassCard
      className={cn(
        "p-4",
        expandable && "cursor-pointer transition-colors hover:border-primary/30",
        open && "border-primary/40",
      )}
      onClick={expandable ? onToggle : undefined}
    >
      <div className="mb-1 flex items-baseline justify-between">
        <p className="text-xs font-medium text-muted-foreground">{idx.label}</p>
        <span className="flex items-center gap-1 text-[10px] text-muted-foreground/50">
          {idx.date}
          {expandable && <ChevronDown className={cn("h-3 w-3 transition-transform", open && "rotate-180")} />}
        </span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className={cn("font-mono text-2xl font-extrabold", zoneColor)}>{idx.value.toFixed(1)}</span>
        <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", zoneBg, zoneColor)}>{zoneLabel}</span>
      </div>
      {/* 分位条 */}
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted/20">
        <div
          className={cn("h-full rounded-full transition-all", favorableZone ? "bg-success/60" : unfavorableZone ? "bg-danger/60" : "bg-primary/40")}
          style={{ width: `${idx.value}%` }}
        />
      </div>
      <p className="mt-1.5 text-[10px] leading-relaxed text-muted-foreground/60">{idx.desc}</p>
      <Sparkline data={idx.hist} height={40} className="mt-2" />
      <p className="mt-1 text-[9px] text-muted-foreground/40">{idx.interpretation}</p>
      {open && comps.length > 0 && (
        <div className="mt-2 space-y-1 border-t border-border/40 pt-2">
          {comps.map((c) => (
            <div key={c.label}>
              <div className="flex items-center gap-2 text-[10px]">
                <span className="min-w-0 flex-1 truncate text-muted-foreground/70" title={c.label}>{c.label}</span>
                <span className="shrink-0 font-mono text-muted-foreground">{c.value}</span>
                <span className={cn("w-9 shrink-0 text-right font-mono", c.pct > 70 ? "text-danger" : c.pct < 30 ? "text-success" : "text-muted-foreground/60")}>
                  {c.pct.toFixed(0)}
                </span>
              </div>
              {c.hist && c.hist.length > 1 && (
                <Sparkline data={c.hist} height={30} className="mt-0.5" />
              )}
            </div>
          ))}
          <p className="pt-0.5 text-right text-[9px] text-muted-foreground/40">右列为各子指标分位</p>
        </div>
      )}
      {open && detail && (
        <p className="mt-1.5 text-[9px] text-muted-foreground/40">{detail}明细见下方展开面板</p>
      )}
    </GlassCard>
  );
}

export function Liquidity() {
  const [data, setData] = useState<LiquidityData | null>(loadCached);
  const [err, setErr] = useState(false);
  const [loading, setLoading] = useState(true);
  const [openIdx, setOpenIdx] = useState<string | null>(null);  // 展开子指标的指数，默认全部折叠

  const load = () => {
    setLoading(true);
    setErr(false);
    api.liquidity()
      .then((d) => { setData(d); if (isValidLiquidity(d)) storageSet(CACHE_KEY, JSON.stringify(d)); })
      .catch(() => setErr(true))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const cnData = data?.cn;
  const us = data?.us;
  const fedOdds = data?.fed_odds;
  const usIndices = data?.us_indices ? Object.values(data.us_indices) : [];

  const rateCards = us ? (["effr", "fed_target_u", "fed_target_l", "sofr", "dgs3m", "dgs2", "dgs10", "dgs30"]
    .filter((k) => us[k]).map((k) => us[k])) : [];
  const spreadCards = us ? (["t10y3m", "t10y2y"].filter((k) => us[k]).map((k) => us[k])) : [];
  const fedBalanceCards = us ? (["walcl", "rrp"].filter((k) => us[k]).map((k) => us[k])) : [];
  const indexFlows = cnData?.index_flows ? Object.values(cnData.index_flows) : [];
  const cnIndices = data?.cn_indices ? Object.values(data.cn_indices) : [];

  return (
    <div>
      <PageHeader
        title="资金面"
        subtitle="国内外重要流动性指标 · 含历史趋势与美联储利率"
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

      {/* 后端回退到 last-good 缓存：数据仍展示，仅提示缓存时间；点右上角刷新重试 */}
      {data?.stale && (
        <GlassCard className="mb-6 flex items-center gap-2 border-warning/30 bg-warning/5 p-3 text-xs text-warning">
          <span>数据源暂不可用，当前显示缓存数据{data.stale_since ? `（${data.stale_since}）` : ""}，稍后可点右上角刷新重试。</span>
        </GlassCard>
      )}

      {/* 国内 · 综合指数 */}
      {cnIndices.length > 0 && (
        <>
          <div className="mb-3 flex items-center gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
              <Gauge className="h-4 w-4" /> 国内 · 综合指数
            </h3>
            <span className="text-[11px] text-muted-foreground/50">多子指标加权合成 · 0-100 分位 · 各指数方向见卡片说明 · 点击卡片展开子指标与趋势图</span>
            {data?.updated && <span className="ml-auto text-[11px] text-muted-foreground/50">{data.updated}</span>}
          </div>
          <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
            {cnIndices.map((idx) => (
              <IndexCard key={idx.label} idx={idx} open={openIdx === idx.label}
                detail={idx.label === "杠杆情绪" ? "杠杆资金" : idx.label === "主力动量" ? "主力资金流" : undefined}
                onToggle={() => setOpenIdx((cur) => (cur === idx.label ? null : idx.label))} />
            ))}
          </div>

          {/* 展开面板：杠杆资金明细（文字 + 趋势图），占满整行 */}
          {openIdx === "杠杆情绪" && (
            <GlassCard className="mb-3 p-4">
              <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <Droplets className="h-3.5 w-3.5" /> 杠杆资金
                <span className="font-normal text-muted-foreground/50">两融余额 / 融资净买入 · 东财，T+1 披露</span>
              </p>
              <div className="grid gap-3 sm:grid-cols-3">
                <CnMetric
                  label={`两融余额${cnData?.date ? ` · ${cnData.date}` : ""}`}
                  value={fmt(cnData?.rzrqye_yi, " 亿")}
                  sub={cnData?.rzrqye_chg_yi != null ? `较上日 ${yi(cnData.rzrqye_chg_yi)}` : "杠杆资金存量"}
                  hist={cnData?.rzrqye_hist}
                  color="--danger"
                />
                <CnMetric
                  label="融资余额"
                  value={fmt(cnData?.rzye_yi, " 亿")}
                  sub={cnData?.rzye_chg_yi != null ? `较上日 ${yi(cnData.rzye_chg_yi)}` : "两融中融资部分"}
                  hist={cnData?.rzrqye_hist}
                  color="--primary"
                />
                <CnMetric
                  label="融资净买入"
                  value={yi(cnData?.rzjme_yi)}
                  sub="当日融资买入 − 偿还"
                  hist={cnData?.rzjme_hist}
                  color="--danger"
                />
              </div>
            </GlassCard>
          )}

          {/* 展开面板：主力资金流明细（文字 + 趋势图），占满整行 */}
          {openIdx === "主力动量" && (
            <GlassCard className="mb-3 p-4">
              <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <Activity className="h-3.5 w-3.5" /> 主力资金流
                <span className="font-normal text-muted-foreground/50">三大指数主力净流入 · 盘中实时（东财）</span>
              </p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {indexFlows.map((f) => <IndexFlowCard key={f.name} flow={f} />)}
                {cnData?.total_main_net_yi != null && (
                  <GlassCard className="p-4">
                    <p className="text-xs text-muted-foreground">全市场合计</p>
                    <p className={cn("mt-0.5 font-mono text-xl font-bold", tone(cnData.total_main_net_yi))}>
                      {yi(cnData.total_main_net_yi)}
                    </p>
                    <p className="mt-0.5 text-[10px] text-muted-foreground/50">三大指数主力净流入加总</p>
                  </GlassCard>
                )}
              </div>
            </GlassCard>
          )}

          <div className="mb-3" />
        </>
      )}

      {/* 国外（美国）· 综合指数 */}
      {usIndices.length > 0 && (
        <>
          <div className="mb-3 flex items-center gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
              <Gauge className="h-4 w-4" /> 国外（美国）· 综合指数
            </h3>
            <span className="text-[11px] text-muted-foreground/50">多子指标加权合成 · 0-100 分位 · 各指数方向见卡片说明 · 点击卡片展开子指标与趋势图</span>
          </div>
          <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
            {usIndices.map((idx) => (
              <IndexCard key={idx.label} idx={idx} open={openIdx === `us-${idx.label}`}
                onToggle={() => setOpenIdx((cur) => (cur === `us-${idx.label}` ? null : `us-${idx.label}`))} />
            ))}
          </div>
        </>
      )}

      {/* 国外（美国）· 美联储加息概率 */}
      {fedOdds && Array.isArray(fedOdds.strikes) && fedOdds.strikes.length > 0 && (
        <>
          <div className="mb-3 flex items-center gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
              <Landmark className="h-4 w-4" /> 美联储加息概率
            </h3>
            <span className="text-[11px] text-muted-foreground/50">
              {fedOdds.event}{fedOdds.meeting ? ` · ${fedOdds.meeting}` : ""} · Kalshi 市场
              {fedOdds.stale && (
                <span className="ml-1.5 rounded bg-warning/15 px-1.5 py-0.5 text-warning">
                  缓存{fedOdds.fetched_at ? ` · ${fedOdds.fetched_at}` : ""}
                </span>
              )}
            </span>
          </div>
          <GlassCard className="mb-6 p-4">
            <FedOddsBar strikes={fedOdds.strikes} />
            <p className="mt-3 text-[10px] text-muted-foreground/50">
              各档为「上限超过该利率」的市场隐含概率（%）；当前最可能区间 ≈ {fedOdds.likely_upper}
            </p>
          </GlassCard>
        </>
      )}

      {/* 国外（美国）· 美联储利率 */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
          <Landmark className="h-4 w-4" /> 国外（美国）· 美联储利率
        </h3>
        <span className="text-[11px] text-muted-foreground/50">联邦基金利率 / 目标区间上下限 / SOFR · FRED，近 1 年趋势</span>
      </div>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {rateCards.slice(0, 8).map((item) => <UsCard key={item.label} item={item} />)}
      </div>

      {/* 国外（美国）· 利差 */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
          <TrendingUp className="h-4 w-4" /> 国外（美国）· 收益率曲线利差
        </h3>
        <span className="text-[11px] text-muted-foreground/50">10Y−3M / 10Y−2Y · 倒挂为负（衰退信号）</span>
      </div>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {spreadCards.map((item) => <UsCard key={item.label} item={item} />)}
        <GlassCard className="p-4">
          <p className="text-xs text-muted-foreground">曲线形态解读</p>
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground/60">
            10Y−3M 利差 {us?.t10y3m ? (us.t10y3m.value > 0 ? "为正（正常向上）" : "为负（倒挂，衰退信号）") : "—"}；
            10Y−2Y 利差 {us?.t10y2y ? (us.t10y2y.value > 0 ? "为正" : "倒挂") : "—"}。
            倒挂通常预示未来 12-18 个月经济衰退概率上升。
          </p>
        </GlassCard>
      </div>

      {/* 国外（美国）· 美联储资产负债表 */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
          <TrendingDown className="h-4 w-4" /> 国外（美国）· 美联储资产负债表
        </h3>
        <span className="text-[11px] text-muted-foreground/50">总资产 / 隔夜逆回购 · FRED，近 1 年趋势</span>
      </div>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {fedBalanceCards.map((item) => <UsCard key={item.label} item={item} />)}
        <GlassCard className="p-4">
          <p className="text-xs text-muted-foreground">缩表进程</p>
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground/60">
            {us?.walcl && isNum(us.walcl.value) ? `总资产 ${fmt(us.walcl.value, " 亿")}，较 1 年前 ${
              Array.isArray(us.walcl.hist) && us.walcl.hist.length > 1
                ? `${us.walcl.hist[0].v > us.walcl.value ? "减少" : "增加"} ${fmt(Math.abs(us.walcl.hist[0].v - us.walcl.value), " 亿")}`
                : "—"
            }` : "—"}
            。逆回购 {us?.rrp ? `${fmt(us.rrp.value, " 十亿$")}` : "—"} 反映市场过剩流动性。
          </p>
        </GlassCard>
      </div>
    </div>
  );
}
