import { useState } from "react";
import { AlertCircle, Loader2, ShieldAlert } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type XrayReport } from "@/lib/api";
import { cn } from "@/lib/utils";

const pct = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${(v * 100).toFixed(digits)}%`;
const num = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : v.toFixed(digits);

/**
 * 持仓体检：按当前市值加权的集中度、波动、回撤、尾部风险、相关性、行业暴露。
 * 只给数字：不出综合分，超没超线由用户自己看；缺口单独列出来，不当成 0。
 */
export function XrayPanel({ holdings }: { holdings: number }) {
  const [data, setData] = useState<XrayReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run(refresh = false) {
    setLoading(true);
    setErr(null);
    try {
      setData(await api.portfolioXray(refresh));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "体检失败");
    } finally {
      setLoading(false);
    }
  }

  const cc = data?.concentration;
  const vol = data?.volatility;
  const dd = data?.drawdown;
  const tail = data?.tail_risk;
  const corr = data?.correlation;
  const contrib = data?.risk_contribution?.shares;
  const industry = data?.industry_exposure;

  return (
    <GlassCard className="mb-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 font-semibold">
          <ShieldAlert className="h-4 w-4 text-primary" /> 持仓体检
        </h3>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {data?.as_of && <span>数据截至 {data.as_of}</span>}
          <button
            onClick={() => run(true)}
            disabled={loading || holdings === 0}
            className="rounded-lg border border-border/60 px-2.5 py-1 transition hover:border-primary/60 hover:text-foreground disabled:opacity-50"
          >
            {loading ? "计算中…" : data ? "重新体检" : "开始体检"}
          </button>
        </div>
      </div>

      {holdings === 0 && (
        <p className="py-6 text-center text-sm text-muted-foreground/60">还没有持仓，先记一笔再体检。</p>
      )}

      {err && (
        <p className="flex items-center gap-1.5 py-3 text-sm text-warning">
          <AlertCircle className="h-4 w-4" /> {err}
        </p>
      )}

      {loading && !data && (
        <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> 取日线、算指标…（首次要拉每只一年的日 K）
        </p>
      )}

      {data && !data.ok && (
        <p className="py-3 text-sm text-muted-foreground">{data.error}</p>
      )}

      {data?.ok && data.inputs && (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Metric label="有效持仓数" value={num(cc?.effective_n)} sub={`最大一只 ${pct(cc?.top1_weight)}`}
                    title="有效持仓数 = 1 / HHI；3 只等权时为 3，1 只全仓时为 1" />
            <Metric label="年化波动" value={pct(vol?.annualized_vol)}
                    sub={vol?.downside_deviation_annualized != null ? `下行 ${pct(vol.downside_deviation_annualized)}` : undefined} />
            <Metric label="最大回撤" value={pct(dd?.max_drawdown)}
                    sub={dd?.max_drawdown_trough ? `谷底 ${dd.max_drawdown_trough}` : undefined} />
            <Metric label="单日 95% VaR" value={pct(tail?.var_95)}
                    sub={tail?.expected_shortfall_95 != null ? `ES ${pct(tail.expected_shortfall_95)}` : undefined}
                    title="历史模拟法：按过去窗口的经验分布取分位，不做正态假设" />
            <Metric label="分散度" value={num(data.diversification?.diversification_ratio)}
                    sub={data.diversification?.note ?? "越高越分散"}
                    title="各资产波动率的加权平均 ÷ 组合波动率；1 = 完全没有分散效果" />
            <Metric label="平均相关性" value={num(corr?.avg_pairwise_abs)}
                    sub={corr?.max_pair ? `最高 ${corr.max_pair.symbols.join("/")} ${num(corr.max_pair.corr)}` : undefined}
                    title="两两相关系数取绝对值后的平均；接近 1 说明涨跌几乎同步" />
            <Metric label="对齐交易日" value={String(data.inputs.aligned_days)}
                    sub={`${data.inputs.first_date} 起`} title="所有持仓都有价格的重叠交易日数量" />
            <Metric label="组合市值" value={num(data.inputs.total_value, 0)}
                    sub={data.source} />
          </div>

          {contrib && (
            <div className="mt-4">
              <p className="mb-1.5 text-xs font-medium text-muted-foreground">
                风险贡献 vs 权重（占比大于权重 = 这只比它的钱更「吵」）
              </p>
              <div className="space-y-1">
                {data.inputs.symbols.map((code) => {
                  const share = contrib[code];
                  const weight = data.inputs?.weights[code];
                  const over = share != null && weight != null && share > weight + 0.02;
                  return (
                    <div key={code} className="flex items-center gap-2 text-xs">
                      <span className="w-16 shrink-0 font-mono text-muted-foreground">{code}</span>
                      <div className="h-1.5 flex-1 rounded-full bg-muted/40">
                        <div className={cn("h-1.5 rounded-full", over ? "bg-danger/70" : "bg-primary/60")}
                             style={{ width: `${Math.min(100, (share ?? 0) * 100)}%` }} />
                      </div>
                      <span className={cn("w-14 shrink-0 text-right font-mono", over ? "text-danger" : "")}>{pct(share)}</span>
                      <span className="w-14 shrink-0 text-right font-mono text-muted-foreground">权重 {pct(weight)}</span>
                      <span className="w-16 shrink-0 text-right font-mono text-muted-foreground">
                        σ {pct(data.asset_vol_annualized?.[code])}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {industry && industry.rows.length > 0 && (
            <div className="mt-4">
              <p className="mb-1.5 text-xs font-medium text-muted-foreground">行业暴露（东财行业分类，按权重降序）</p>
              <div className="flex flex-wrap gap-1.5">
                {industry.rows.map((row) => (
                  <span key={row.industry}
                        className={cn("rounded-md border px-2 py-0.5 text-xs",
                          (row.weight ?? 0) >= 0.5 ? "border-danger/40 bg-danger/10 text-danger" : "border-border/50 text-muted-foreground")}>
                    {row.industry} {pct(row.weight)}
                  </span>
                ))}
              </div>
            </div>
          )}

          {(data.gaps.length > 0 || (data.notes?.length ?? 0) > 0) && (
            <ul className="mt-4 space-y-0.5 text-[11px] text-muted-foreground/70">
              {data.gaps.map((gap, i) => (
                <li key={`gap-${i}`}>· 没算进组合：{gap.name || gap.code} — {gap.reason}</li>
              ))}
              {data.notes?.map((note, i) => <li key={`note-${i}`}>· {note}</li>)}
              <li>· 只给数字和口径，不出综合分；阈值（集中度、波动、回撤）由你自己定。</li>
            </ul>
          )}
        </>
      )}
    </GlassCard>
  );
}

function Metric({ label, value, sub, title }: { label: string; value: string; sub?: string; title?: string }) {
  return (
    <div className="rounded-lg border border-border/40 bg-black/10 px-3 py-2" title={title}>
      <p className="text-[11px] text-muted-foreground">{label}</p>
      <p className="mt-0.5 font-mono text-base font-semibold">{value}</p>
      {sub && <p className="mt-0.5 truncate text-[10px] text-muted-foreground/70">{sub}</p>}
    </div>
  );
}
