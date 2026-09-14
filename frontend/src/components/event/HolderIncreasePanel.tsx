import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle, ChevronDown, ChevronUp, Database, ExternalLink, Loader2, RefreshCw, TrendingUp,
} from "lucide-react";
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
// 日期一律 YYYY/MM/DD（带全年份）；残缺日期不显示，避免出现丢年份的"12/09"式值。
const fmtDate = (value?: string | null) => {
  const iso = (value || "").slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso.split("-").join("/") : "—";
};

export function HolderIncreasePanel() {
  const [win, setWin] = useState<HolderIncreaseWindow>("7d");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [manualRefresh, setManualRefresh] = useState(false);
  // 缓存键带 v2：计划日期改为 start/end_date、新增 amount_done，旧持久化值结构不兼容。
  const { data, loading, revalidating, revalidate } = useSWR<HolderIncreaseData>(
    `event:holder-increase:v2:${win}`,
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
            <Loader2 className="h-5 w-5 animate-spin" />加载增持数据（本地有缓存会先显示上次结果，后台自动更新）…
          </div>
        ) : (
          <table className="w-full min-w-[1080px] text-sm">
            <thead>
              <tr className="border-b border-border/60 text-left text-xs text-muted-foreground">
                <th className="px-4 py-3 font-medium">股票</th>
                <th className="px-3 py-3 font-medium">最高身份</th>
                <th className="px-3 py-3 font-medium">计划开始日期</th>
                <th className="px-3 py-3 font-medium">计划结束日期</th>
                <th className="px-3 py-3 font-medium">最新增持日期</th>
                <th className="px-3 py-3 text-right font-medium">已增持金额</th>
                <th className="px-3 py-3 font-medium">计划增持金额</th>
                <th className="px-3 py-3 font-medium">综合评分</th>
                <th className="px-3 py-3" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => <Fragment key={row.code}>
                <tr onClick={() => setExpanded(expanded === row.code ? null : row.code)}
                  className="cursor-pointer border-b border-border/30 transition hover:bg-black/10">
                  <td className="px-4 py-3">
                    {/* 股票名跳个股数据页（与持仓/自选一致）；行本身点击是展开，这里阻断冒泡避免误收起 */}
                    <Link
                      to={`/stock-data?code=${row.code}`}
                      onClick={(event) => event.stopPropagation()}
                      className="block max-w-56 truncate font-medium underline-offset-4 transition-colors hover:text-primary hover:underline"
                      title={`打开 ${row.name}（${row.code}）的个股数据`}
                    >
                      {row.name}
                    </Link>
                    <div className="mt-0.5 font-mono text-xs text-muted-foreground">{row.code} · {row.people}人/{row.count}笔</div>
                  </td>
                  <td className="px-3 py-3"><span className={cn("inline-flex rounded-full border px-2 py-1 text-xs", TIER_STYLE[row.tier] || TIER_STYLE.holder)}>{row.identity}</span></td>
                  <PlanDateCell row={row} which="start" />
                  <PlanDateCell row={row} which="end" />
                  <td className="px-3 py-3 font-mono text-xs">
                    <span title={`最新增持日 ${row.latest_date || "—"}｜披露区间 ${row.period}`}>{fmtDate(row.latest_date)}</span>
                    {row.ongoing && <span className="ml-1 rounded border border-success/30 bg-success/15 px-1 py-0.5 font-sans text-[10px] text-success">区间未结束</span>}
                    {row.cumulative && <span className="ml-1 rounded bg-muted px-1 py-0.5 font-sans text-[10px]">累计</span>}
                  </td>
                  <td className="px-3 py-3 text-right font-mono tabular-nums"
                    title={row.amount_done_partial
                      ? `计划开始日早于数据回看期（近 ${data?.lookback_days ?? 35} 天），只统计到回看期内的增持金额，实际应不低于该值`
                      : `计划开始日之后的增持金额合计（回看期近 ${data?.lookback_days ?? 35} 天）`}>
                    {row.amount_done_partial && <span className="text-muted-foreground">≥</span>}{fmtAmount(row.amount_done)}
                  </td>
                  <td className="px-3 py-3">
                    {row.plan?.amount_label
                      ? <PlanLink plan={row.plan} label={row.plan.amount_label} />
                      : <span className="text-xs text-muted-foreground">—</span>}
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
                    <td colSpan={9} className="bg-black/10 px-4 py-4">
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

      <SourceList data={data} />

      <p className="text-xs text-muted-foreground">
        「近1日 / 过去7日 / 过去30日」按增持开始日（披露区间起点，无区间则按首次披露日）落入窗口筛选，进展类披露不会把早已开始的增持重新拉进短窗口；
        「全部 · 进行中」= 最全口径：回看期（约 {data?.lookback_days ?? 35} 天）内所有有增持记录的股票默认入选，只有出现明确结束信号才剔除（已达成计划金额、已过计划期限且期限后无增持、或披露实施完毕）；
        计划开始/结束日期与计划增持金额由增持计划公告正文解析（best-effort，取公告写明的金额下限）；公告未写明起始日时按计划公告日推定并以虚线标注，解析不到的显示"—"；
        计划开始日早于回看期时，已增持金额只覆盖回看期内的披露，以"≥"标注；表中所有日期均为 YYYY/MM/DD（含年份）。
      </p>
    </div>
  );
}

// 计划日期单元格：有公告依据的可点开原文；未解析到写明起始日的按公告日推定，虚线弱化。
function PlanDateCell({ row, which }: { row: HolderIncreaseRow; which: "start" | "end" }) {
  const plan = row.plan;
  const value = which === "start" ? plan?.start_date : plan?.end_date;
  if (!plan || !value) return <td className="px-3 py-3 font-mono text-xs text-muted-foreground">—</td>;
  if (which === "start") {
    const inferred = !plan.window_parsed;
    return (
      <td className="px-3 py-3 font-mono text-xs">
        <PlanLink
          plan={plan}
          label={fmtDate(value)}
          className={cn(inferred && "border-b border-dashed border-muted-foreground/60 text-muted-foreground")}
          title={inferred
            ? `公告未写明起始日，按计划公告日推定：${fmtDate(plan.notice_date)}｜${plan.title}`
            : `计划起始日（公告原文解析）｜${plan.title}`}
        />
      </td>
    );
  }
  const overdue = value < new Date().toISOString().slice(0, 10);
  return (
    <td className="px-3 py-3 font-mono text-xs">
      <PlanLink
        plan={plan}
        label={`${fmtDate(value)}${overdue ? " ·已过" : ""}`}
        className={cn(overdue && "text-muted-foreground")}
        title={`计划结束日（公告原文解析）｜${plan.title}`}
      />
    </td>
  );
}

// 计划相关单元格的统一入口：能拿到计划公告链接时点开原文，否则只显示文本。
function PlanLink({ plan, label, className, title }: {
  plan: NonNullable<HolderIncreaseRow["plan"]>; label: string; className?: string; title?: string;
}) {
  const tip = title || plan.title || "公告解析到的增持计划";
  if (!plan.notice_url) return <span className={cn("font-mono text-xs", className)} title={tip}>{label}</span>;
  return (
    <a href={plan.notice_url} target="_blank" rel="noreferrer" title={tip}
      className={cn("font-mono text-xs transition hover:text-primary", className)}>
      {label}<ExternalLink className="ml-0.5 inline h-3 w-3 opacity-50" />
    </a>
  );
}

function SourceList({ data }: { data?: HolderIncreaseData | null }) {
  const sources = data?.sources || [];
  return (
    <GlassCard>
      <div className="flex items-center gap-2 text-xs font-semibold text-muted-foreground">
        <Database className="h-3.5 w-3.5" />数据来源
        <span className="font-normal">
          数据时间 {data?.updated ? new Date(data.updated).toLocaleString("zh-CN", { hour12: false }) : "—"}
          {` · 回看期 ${data?.lookback_days ?? 35} 天 · 原始记录 ${data?.total_records ?? "—"} 条`}
        </span>
      </div>
      <div className="mt-3 space-y-2">
        {sources.map((source) => (
          <div key={source.key} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs">
            <a href={source.url} target="_blank" rel="noreferrer"
              className="inline-flex items-center gap-1 font-medium transition hover:text-primary">
              {source.label}<ExternalLink className="h-3 w-3 opacity-60" />
            </a>
            <span className="text-muted-foreground">{source.provider}</span>
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-[10px] text-muted-foreground">{source.dataset}</code>
            <span className="text-muted-foreground">覆盖字段：{source.fields}；原始出处：{source.origin}</span>
          </div>
        ))}
        {!sources.length && <div className="text-xs text-muted-foreground">来源信息随接口返回，暂不可用。</div>}
        <div className="text-xs text-muted-foreground">
          每只股票的每条增持与增持计划均可点开对应披露原文（表格内链接），评分仅汇总公开事实用于研究排序，不构成任何买卖建议。
        </div>
      </div>
    </GlassCard>
  );
}

function RecordList({ row }: { row: HolderIncreaseRow }) {
  const fallbackUrl = `https://data.eastmoney.com/notices/stock/${row.code}.html`;
  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">增持明细（{row.records.length} 笔）</div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[880px] text-xs">
          <thead>
            <tr className="border-b border-border/50 text-left text-muted-foreground">
              <th className="py-2 pr-3 font-medium">增持人</th>
              <th className="py-2 pr-3 font-medium">身份</th>
              <th className="py-2 pr-3 text-right font-medium">金额</th>
              <th className="py-2 pr-3 text-right font-medium">股数</th>
              <th className="py-2 pr-3 text-right font-medium">均价</th>
              <th className="py-2 pr-3 font-medium">增持日</th>
              <th className="py-2 pr-3 font-medium">披露区间</th>
              <th className="py-2 pr-3 font-medium">方式</th>
              <th className="py-2 font-medium">公告</th>
            </tr>
          </thead>
          <tbody>
            {row.records.map((record, index) => (
              <RecordRow key={`${record.person}-${record.activity_date}-${index}`} record={record} fallbackUrl={fallbackUrl} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RecordRow({ record, fallbackUrl }: { record: HolderIncreaseRecord; fallbackUrl: string }) {
  const range = record.start_date && record.end_date
    ? `${fmtDate(record.start_date)} ~ ${fmtDate(record.end_date)}`
    : record.start_date ? `${fmtDate(record.start_date)} ~` : "";
  return (
    <tr className="border-b border-border/25">
      <td className="py-2 pr-3 font-medium">{record.person}</td>
      <td className="py-2 pr-3"><span className={cn("inline-flex rounded-full border px-1.5 py-0.5 text-[10px]", TIER_STYLE[record.tier] || TIER_STYLE.holder)}>{record.identity}</span>{record.ongoing && <span className="ml-1 text-[10px] text-success">未结束</span>}</td>
      <td className="py-2 pr-3 text-right font-mono tabular-nums">{fmtAmount(record.amount)}</td>
      <td className="py-2 pr-3 text-right font-mono tabular-nums text-muted-foreground">{record.shares ? `${(record.shares / 1e4).toFixed(1)}万` : "—"}</td>
      <td className="py-2 pr-3 text-right font-mono tabular-nums text-muted-foreground">{record.price != null ? record.price.toFixed(2) : "—"}</td>
      <td className="py-2 pr-3 font-mono" title={record.trade_date ? `成交日 ${fmtDate(record.trade_date)}` : "无成交日，取增持区间截止日或公告日"}>{fmtDate(record.buy_date || record.activity_date)}</td>
      <td className="py-2 pr-3 font-mono text-muted-foreground" title={range ? `披露区间 ${range}` : "单笔增持（无区间）"}>{range || "—"}</td>
      <td className="py-2 pr-3 text-muted-foreground">{record.reason || record.market || "—"}</td>
      <td className="py-2">
        <a href={record.url || fallbackUrl} target="_blank" rel="noreferrer"
          className="inline-flex items-center gap-0.5 text-[11px] text-muted-foreground transition hover:text-primary"
          title={record.url ? "打开对应披露公告" : "打开该股票的公告列表"}>
          公告<ExternalLink className="h-3 w-3" />
        </a>
      </td>
    </tr>
  );
}
