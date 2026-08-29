import { Fragment, useState } from "react";
import { AlertCircle, ChevronDown, ChevronUp, Loader2, RefreshCw, TrendingUp } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import {
  api, ApiError,
  type HolderIncreaseData, type HolderIncreaseRecord, type HolderIncreaseRow, type HolderIncreaseWindow,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";
import { HolderPriceChart } from "./HolderPriceChart";

const WINDOWS: { id: HolderIncreaseWindow; label: string }[] = [
  { id: "1d", label: "近1日" },
  { id: "7d", label: "过去7日" },
  { id: "30d", label: "过去30日" },
  { id: "all", label: "全部 · 进行中" },
];

const TIER_STYLE: Record<string, string> = {
  chairman: "bg-primary/15 text-primary border-primary/30",
  exec: "bg-info/15 text-info border-info/30",
  big_holder: "bg-warning/15 text-warning border-warning/30",
  relative: "bg-muted text-muted-foreground border-border",
  holder: "bg-muted text-muted-foreground border-border",
};
const GRADE_STYLE: Record<string, string> = {
  strong: "bg-success/15 text-success border-success/30",
  watch: "bg-warning/15 text-warning border-warning/30",
  normal: "bg-muted text-muted-foreground border-border",
};
const GRADE_LABEL: Record<string, string> = { strong: "强信号", watch: "值得关注", normal: "一般" };

const fmtAmount = (value: number | null | undefined) => {
  if (value == null) return "—";
  if (value >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (value >= 1e4) return `${(value / 1e4).toFixed(0)}万`;
  return value.toLocaleString("zh-CN");
};
const fmtDate = (value: string) => (value ? value.slice(5).replace("-", "/") : "—");

export function HolderIncreasePanel() {
  const [win, setWin] = useState<HolderIncreaseWindow>("7d");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [manualRefresh, setManualRefresh] = useState(false);
  const { data, loading, revalidating, revalidate } = useSWR<HolderIncreaseData>(
    `event:holder-increase:v1:${win}`,
    async (fresh) => {
      const result = await api.holderIncrease(win, fresh);
      setErr(null);
      return result;
    },
    [win],
    (error: unknown) => setErr(error instanceof ApiError ? error.message : "增持数据加载失败"),
    { persist: true },
  );

  const refresh = async () => {
    setManualRefresh(true);
    try { await revalidate(true); setErr(null); }
    catch (error) { setErr(error instanceof ApiError ? error.message : "增持数据刷新失败"); }
    finally { setManualRefresh(false); }
  };

  const rows = data?.rows || [];
  const refreshing = data?.cache_state === "refreshing";

  return (
    <div className="space-y-4">
      <GlassCard glow>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold">
              <TrendingUp className="h-4 w-4 text-primary" />高管 / 管理层 / 股东增持名单
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              评分 0-100 = 身份分量 40（实控人/董事长 &gt; 董监高 &gt; 大股东 &gt; 亲属）+ 金额规模 25 + 占股本比例 15 + 笔数人数 10 + 新近度/进行中 10
            </p>
          </div>
          <div className="text-right text-xs text-muted-foreground">
            <div>数据时间 {data?.updated ? new Date(data.updated).toLocaleString("zh-CN", { hour12: false }) : "—"}</div>
            <div>{data?.source === "eastmoney" ? "东方财富数据中心" : data?.source || ""}{data?.total_records != null ? ` · 原始记录 ${data.total_records} 条` : ""}</div>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <div className="flex flex-wrap items-center gap-1">
            {WINDOWS.map((item) => (
              <button key={item.id} onClick={() => { setWin(item.id); setExpanded(null); }}
                className={cn("rounded-lg px-2.5 py-1.5 text-xs transition",
                  win === item.id ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-black/20")}>
                {item.label}
              </button>
            ))}
          </div>
          <div className="h-5 w-px bg-border/60" />
          <button onClick={refresh} disabled={manualRefresh || loading}
            className="rounded-lg border border-border p-1.5 text-muted-foreground transition hover:text-primary disabled:opacity-40" title="强制刷新（后台重拉）">
            <RefreshCw className={cn("h-4 w-4", (manualRefresh || revalidating) && "animate-spin")} />
          </button>
        </div>
        {err && <div className="mt-3 flex items-center gap-2 text-sm text-danger"><AlertCircle className="h-4 w-4" />{err}</div>}
        {!err && refreshing && <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="h-3.5 w-3.5 animate-spin" />后台正在重拉数据，完成后自动更新…</div>}
      </GlassCard>

      <GlassCard className="overflow-x-auto p-0">
        {!data && loading ? (
          <div className="flex h-48 items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />首次拉取高管与股东增持记录…
          </div>
        ) : (
          <table className="w-full min-w-[960px] text-sm">
            <thead>
              <tr className="border-b border-border/60 text-left text-xs text-muted-foreground">
                <th className="px-4 py-3 font-medium">股票</th>
                <th className="px-3 py-3 font-medium">最高身份</th>
                <th className="px-3 py-3 font-medium">增持时间</th>
                <th className="px-3 py-3 text-right font-medium">已增持金额</th>
                <th className="px-3 py-3 font-medium">计划增持金额</th>
                <th className="px-3 py-3 font-medium">计划结束日期</th>
                <th className="px-3 py-3 font-medium">综合评分</th>
                <th className="px-3 py-3" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => <Fragment key={row.code}>
                <tr onClick={() => setExpanded(expanded === row.code ? null : row.code)}
                  className="cursor-pointer border-b border-border/30 transition hover:bg-black/10">
                  <td className="px-4 py-3">
                    <div className="max-w-56 truncate font-medium">{row.name}</div>
                    <div className="mt-0.5 font-mono text-xs text-muted-foreground">{row.code} · {row.people}人/{row.count}笔</div>
                  </td>
                  <td className="px-3 py-3"><span className={cn("inline-flex rounded-full border px-2 py-1 text-xs", TIER_STYLE[row.tier] || TIER_STYLE.holder)}>{row.identity}</span></td>
                  <td className="px-3 py-3 font-mono text-xs text-muted-foreground">
                    <span title={row.cumulative ? "整轮增持的累计披露区间（起点≈计划/窗口起点，金额为区间累计）" : "该批披露的实际买入区间"}>{row.period}</span>
                    {row.cumulative && <span className="ml-1 rounded bg-muted px-1 py-0.5 text-[10px]">累计</span>}
                    {row.ongoing && <span className="ml-1 rounded border border-success/30 bg-success/15 px-1 py-0.5 text-[10px] text-success">区间未结束</span>}
                  </td>
                  <td className="px-3 py-3 text-right font-mono tabular-nums">{fmtAmount(row.total_amount)}</td>
                  <td className="px-3 py-3">
                    {row.plan?.amount_label
                      ? <span className="font-mono text-xs" title={row.plan.title || "公告解析到的增持计划"}>{row.plan.amount_label}</span>
                      : <span className="text-xs text-muted-foreground">—</span>}
                  </td>
                  <td className="px-3 py-3">
                    {row.plan?.deadline ? (
                      <span className={cn("font-mono text-xs", new Date(row.plan.deadline) < new Date() && "text-muted-foreground")}
                        title={row.plan.title || "公告解析到的增持计划"}>
                        {fmtDate(row.plan.deadline)}{new Date(row.plan.deadline) < new Date() && " ·已过"}
                      </span>
                    ) : <span className="text-xs text-muted-foreground">—</span>}
                  </td>
                  <td className="px-3 py-3">
                    <div className="flex items-center gap-2">
                      <span className={cn("font-mono text-lg font-bold", row.grade === "strong" ? "text-success" : row.grade === "watch" ? "text-warning" : "text-muted-foreground")}>{row.score}</span>
                      <span className={cn("inline-flex rounded-full border px-2 py-0.5 text-[10px]", GRADE_STYLE[row.grade])}>{GRADE_LABEL[row.grade]}</span>
                    </div>
                  </td>
                  <td className="px-3 py-3 text-right">{expanded === row.code ? <ChevronUp className="inline h-4 w-4" /> : <ChevronDown className="inline h-4 w-4" />}</td>
                </tr>
                {expanded === row.code && (
                  <tr className="border-b border-border/40">
                    <td colSpan={8} className="bg-black/10 px-4 py-4">
                      <div className="space-y-3">
                        <HolderPriceChart row={row} />
                        <RecordList row={row} />
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>)}
            </tbody>
          </table>
        )}
        {data && !rows.length && (
          <div className="py-12 text-center text-sm text-muted-foreground">
            {win === "all" ? "当前没有未结束的增持计划" : "该窗口内没有高管/股东增持记录"}
          </div>
        )}
      </GlassCard>

      <p className="text-xs text-muted-foreground">
        数据来自交易所公开披露（高管持股变动明细 + 股东增减持），评分仅汇总公开事实用于研究排序，不构成任何买卖建议。
        「近1日 / 过去7日 / 过去30日」按增持开始日（披露区间起点，无区间则按首次披露日）落入窗口筛选，进展类披露不会把早已开始的增持重新拉进短窗口；
        「全部 · 进行中」= 最全口径：回看期（约 35 天）内所有有增持记录的股票默认入选，只有出现明确结束信号才剔除（已达成计划金额、已过计划期限且期限后无增持、或披露实施完毕）；
        增持时间来自披露原文——每周触刻度披露的是该批实际买入区间（标"累计"的行是整轮增持聚成一条，起点≈计划/窗口起点、金额为区间累计）；
        计划增持金额 / 计划结束日期由公告标题与正文解析（best-effort，仅解析到计划的行有值），解析不到时显示"—"并按仍在增持处理。
      </p>
    </div>
  );
}

function RecordList({ row }: { row: HolderIncreaseRow }) {
  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">增持明细（{row.records.length} 笔）</div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-xs">
          <thead>
            <tr className="border-b border-border/50 text-left text-muted-foreground">
              <th className="py-2 pr-3 font-medium">增持人</th>
              <th className="py-2 pr-3 font-medium">身份</th>
              <th className="py-2 pr-3 text-right font-medium">金额</th>
              <th className="py-2 pr-3 text-right font-medium">股数</th>
              <th className="py-2 pr-3 text-right font-medium">均价</th>
              <th className="py-2 pr-3 font-medium">日期 / 区间</th>
              <th className="py-2 font-medium">方式</th>
            </tr>
          </thead>
          <tbody>
            {row.records.map((record, index) => <RecordRow key={`${record.person}-${record.activity_date}-${index}`} record={record} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RecordRow({ record }: { record: HolderIncreaseRecord }) {
  const dates = record.start_date && record.end_date
    ? `${fmtDate(record.start_date)} ~ ${fmtDate(record.end_date)}`
    : fmtDate(record.activity_date);
  return (
    <tr className="border-b border-border/25">
      <td className="py-2 pr-3 font-medium">{record.person}</td>
      <td className="py-2 pr-3"><span className={cn("inline-flex rounded-full border px-1.5 py-0.5 text-[10px]", TIER_STYLE[record.tier] || TIER_STYLE.holder)}>{record.identity}</span>{record.ongoing && <span className="ml-1 text-[10px] text-success">未结束</span>}</td>
      <td className="py-2 pr-3 text-right font-mono tabular-nums">{fmtAmount(record.amount)}</td>
      <td className="py-2 pr-3 text-right font-mono tabular-nums text-muted-foreground">{record.shares ? `${(record.shares / 1e4).toFixed(1)}万` : "—"}</td>
      <td className="py-2 pr-3 text-right font-mono tabular-nums text-muted-foreground">{record.price != null ? record.price.toFixed(2) : "—"}</td>
      <td className="py-2 pr-3 font-mono text-muted-foreground">{dates}</td>
      <td className="py-2 text-muted-foreground">{record.reason || record.market || "—"}</td>
    </tr>
  );
}
