import { BondScoreCharts } from "./BondScoreCharts";
import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import type { BondPriorsData, BondPriorRow } from "@/lib/api";
import { numberText as n, priorIsCurrent } from "@/lib/bondView";
import { cn } from "@/lib/utils";

function Estimate({ row }: { row: BondPriorRow }) {
  const current = priorIsCurrent(row) && row.forecast_bp != null && Number.isFinite(row.forecast_bp);
  const rate = row.unit === "yield_change_bp";
  const amount = row.forecast_bp == null ? null : row.forecast_bp / (rate ? 1 : 100);
  return <div data-prior-id={row.id}>
    {!rate && <div className="mb-4"><p className="text-sm text-muted-foreground">相对收益得分</p><p className="mt-2 flex flex-wrap items-baseline gap-2"><strong className="text-3xl tabular-nums">{current ? n(row.score, 1) : "—"}</strong><span className="text-sm text-muted-foreground">/ 100 · {current && row.score != null ? row.score >= 55 ? "偏强" : row.score <= 45 ? "偏弱" : "中性" : "暂停评分"}</span></p></div>}
    <p className="text-sm text-muted-foreground">未来 {row.horizon} 个交易观测日 · {rate ? "国债利率变化" : "债券指数持有收益"}</p>
    <p className="mt-3 text-xl font-semibold tracking-tight tabular-nums">{current ? n(amount, 2, true) : "—"}<span className="ml-1 text-base font-normal text-muted-foreground">{current ? rate ? "基点" : "%" : "暂不提供预测"}</span></p>
    <p className="mt-2 min-h-10 text-sm leading-relaxed text-muted-foreground">{!current ? "数据尚未更新齐，等待更新后再判断。" : rate ? "利率下降通常有利于债券价格；这里不是持有收益。" : "整个持有期的收益估计，非年化，未扣交易费用。"}</p>
    <p className="mt-3 text-xs text-muted-foreground">数据截至 {row.observed_at ?? "暂无"}</p>
    {!rate && <BondScoreCharts key={row.id} history={row.score_history ?? []} />}
    <details className="mt-4 border-t border-border/60 pt-3 text-sm">
      <summary className="cursor-pointer py-1 text-muted-foreground">查看依据与局限</summary>
      <div className="mt-3 space-y-3 text-sm leading-relaxed text-muted-foreground">
        {!rate && <p>得分比较预测收益与同一预测期的历史均值，并按历史波动调整。50 分表示没有相对优势；55／45 只是展示分界，未经交易效果校准。历史按当日已到期样本重算，但模型参数经过历史挑选，来源数据也可能修订。</p>}
        <p>可信度：仍在验证。历史测试有改善，但尚不足以证明未来能稳定预测，也不代表能通过择时赚取额外收益。</p>
        <p>{row.caveat}</p>
        {!current && <p>{row.reason ?? "观测日期滞后，当前估计已暂停。"}</p>}
        <p>同一预测期的历史均值：{n(row.baseline_bp == null ? null : row.baseline_bp / (rate ? 1 : 100), 2, true)} {rate ? "基点" : "%"}。正收益本身不等于跑赢基准。</p>
        <details><summary className="cursor-pointer py-1">模型检验与数据来源</summary><div className="mt-2 space-y-2 break-words text-xs">
          <p>{row.label} · 从下一观测日起计算；交易观测日不是自然日。</p>
          <p>相对历史均值的预测误差（MSE）改善：2020—2022 年 {n(row.evidence.audit_r2 * 100, 2, true)}%；2023—2026 年 {n(row.evidence.extension_r2 * 100, 2, true)}%。此处百分比不是投资收益。</p>
          <p>{row.evidence.q != null ? `多重检验 q=${n(row.evidence.q, 3)}，未达强证据门槛。` : "改善区间包含零，未获得独立验证。"}</p>
          <p>{row.model_version}</p>
          {row.training.map(t => <p key={t.member}>{t.member} · 训练 {t.n} 条 · 最后到期标签 {t.last_label_end}</p>)}
          <p>{Object.entries(row.source_dates).map(([key, date]) => `${key}：${date}`).join("；")}</p>
        </div></details>
      </div>
    </details>
  </div>;
}

export function BondPriorsPanel({ data, loading, busy, refresh }: {
  data: BondPriorsData | null; loading: boolean; busy: boolean; refresh: () => void;
}) {
  const [longHorizon, setLongHorizon] = useState(60);
  const groups = [
    { key: "short", title: "短债", tenor: "1 年以下", id: "short_index_120", note: "关注资金利率变化" },
    { key: "medium", title: "中期债", tenor: "3—5 年", id: "3-5Y_60", note: "兼顾持有收益与利率波动" },
    { key: "long", title: "长期债", tenor: "7—10 年", id: `7-10Y_${longHorizon}`, note: "对利率变化更敏感" },
  ];
  const shortRate = data?.rows.find(row => row.id === "gov3M_60");
  return <GlassCard>
    <div className="flex items-center justify-between gap-3">
      <div><h2 className="text-lg font-semibold">短、中、长期债怎么看</h2><p className="mt-1 text-sm text-muted-foreground">先看得分与走势，再看收益与依据。得分相对各自历史基准，低分不等于负收益；不同预测期不宜直接排名。</p></div>
      <button onClick={refresh} disabled={busy} aria-label="刷新债券预测" className="rounded-lg p-3 text-muted-foreground hover:bg-muted disabled:opacity-40"><RefreshCw className={cn("h-4 w-4", busy && "animate-spin")} /></button>
    </div>
    <p className="mt-4 rounded-lg bg-muted/40 px-3 py-2 text-sm text-muted-foreground">参考价值：有历史线索，仍待验证。以下数值不是收益承诺或买卖建议。</p>
    {loading && !data ? <p role="status" className="py-8 text-sm text-muted-foreground">正在更新预测数据…</p> : !data?.rows.length ? <p role="status" className="py-8 text-sm text-muted-foreground">预测暂不可用，请稍后刷新。</p> : <>
      <div className="mt-5 grid min-w-0 gap-4 lg:grid-cols-3">{groups.map(group => {
        const row = data.rows.find(item => item.id === group.id);
        return <section key={group.key} aria-label={group.title} className="min-w-0 rounded-xl border border-border/60 p-4">
          <div className="flex items-baseline justify-between gap-2"><h3 className="text-lg font-semibold">{group.title}</h3><span className="text-sm text-muted-foreground">{group.tenor}</span></div>
          <p className="mt-1 text-sm text-muted-foreground">{group.note}</p>
          <div className="my-4 flex min-h-9 items-center gap-1 text-xs">{group.key === "long" ? [20, 60].map(days => <button key={days} aria-pressed={longHorizon === days} onClick={() => setLongHorizon(days)} className={cn("rounded-lg px-3 py-2", longHorizon === days ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-muted")}>{days} 日预测</button>) : <span className="rounded-lg bg-muted/40 px-3 py-2">{group.key === "short" ? "120" : "60"} 日预测</span>}</div>
          {row ? <Estimate row={row} /> : <p role="status" className="py-4 text-sm text-muted-foreground">该期限暂无预测数据。</p>}
          {group.key === "short" && shortRate && <details className="mt-3 border-t border-border/60 pt-3 text-sm"><summary className="cursor-pointer py-1 text-muted-foreground">另看：3 个月国债利率</summary><div className="mt-4"><Estimate row={shortRate} /></div></details>}
        </section>;
      })}</div>
      <details className="mt-4 text-xs text-muted-foreground"><summary className="cursor-pointer py-2">数据说明</summary><p className="mt-2">收益对应综合债券指数，含信用及成分调整风险；数据过期或缺失时暂停预测。</p><p className="mt-2 break-all">版本 {data.input_fingerprint} · 更新 {data.generated_at.slice(0, 19).replace("T", " ")}</p>{data.notes?.map(note => <p key={note} className="mt-2">{note}</p>)}</details>
    </>}
  </GlassCard>;
}
