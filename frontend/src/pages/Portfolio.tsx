import { useState, useEffect, useCallback } from "react";
import { ShieldCheck, RefreshCw, Loader2, Trash2, AlertCircle } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { StockSearchInput } from "@/components/ui/StockSearchInput";
import { isTradingHours } from "@/hooks/useLiveQuotes";
import { api, ApiError, type PortfolioData } from "@/lib/api";
import { cn } from "@/lib/utils";

const REFRESH_MS = 30 * 60 * 1000; // 每半小时自动刷新
const pnlColor = (v: number) => (v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground");
const fmt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
// 单价类（现价/成本/清仓价）最多 4 位小数：ETF/基金常见 3-4 位，截断成 2 位会与市值/盈亏对不上账
const fmtPx = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 4 });

export function Portfolio() {
  const [data, setData] = useState<PortfolioData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [code, setCode] = useState("");
  const [shares, setShares] = useState("");
  const [cost, setCost] = useState("");
  const [, setAdding] = useState(false);
  // 清仓录入
  const [cCode, setCCode] = useState("");
  const [cDate, setCDate] = useState("");
  const [cPrice, setCPrice] = useState("");
  const [cShares, setCShares] = useState("");
  const [, setClosing] = useState(false);

  const load = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      setData(manual ? await api.refreshPortfolio() : await api.portfolio());
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "加载失败");
    } finally {
      if (manual) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
    // 定时刷新只在「A股交易时段 + 页面在前台」时执行：收盘后/切到后台时持仓盈亏不变，不必刷。
    const tick = () => {
      if (!document.hidden && isTradingHours()) load();
    };
    const t = setInterval(tick, REFRESH_MS);
    // 页面切回前台 / 开盘时补刷一次
    const onVisible = () => { if (!document.hidden) load(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", onVisible); };
  }, [load]);

  const add = async (overrideCode?: string) => {
    const c = (overrideCode || code).trim();
    if (!/^\d{6}$/.test(c)) { setErr("请输入 6 位股票代码"); return; }
    const s = parseFloat(shares), cc = parseFloat(cost);
    if (!(s > 0) || !Number.isFinite(cc)) { setErr("数量须大于 0，成本价请填数字（可为负）"); return; }
    setAdding(true); setErr(null);
    try {
      setData(await api.addHolding(c, s, cc));
      setCode(""); setShares(""); setCost("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "添加失败");
    } finally {
      setAdding(false);
    }
  };

  const remove = async (c: string) => {
    try { setData(await api.removeHolding(c)); } catch { /* ignore */ }
  };

  const addClose = async (overrideCode?: string) => {
    const c = (overrideCode || cCode).trim();
    if (!/^\d{6}$/.test(c)) { setErr("清仓记录：请输入 6 位代码"); return; }
    const p = parseFloat(cPrice), s = parseFloat(cShares);
    if (!cDate) { setErr("请选清仓日期"); return; }
    if (!(p > 0) || !(s > 0)) { setErr("清仓价 / 股数须大于 0"); return; }
    setClosing(true); setErr(null);
    try {
      // 成本不传：后端用添加持仓时录入的成本计算已实现盈亏，并从当前持仓扣减股数
      setData(await api.closePosition(c, cDate, p, s));
      setCCode(""); setCDate(""); setCPrice(""); setCShares("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "添加清仓记录失败");
    } finally {
      setClosing(false);
    }
  };

  const removeClosed = async (i: number) => {
    try { setData(await api.removeClosed(i)); } catch { /* ignore */ }
  };

  const holdings = data?.holdings || [];
  const totals = data?.totals;
  const closed = data?.closed || [];

  const aiContext = totals
    ? `我的持仓（本地数据）：\n` + holdings.map((h) => `${h.name}(${h.code}) ${h.shares}股 成本${h.cost} 现价${h.price} 浮盈${h.pnl}(${h.pnl_pct}%)`).join("\n") +
      `\n汇总：市值${totals.market_value} 总浮盈${totals.pnl}(${totals.pnl_pct}%)`
    : "我的持仓：暂无记录。";

  return (
    <div>
      <PageHeader
        title="持仓"
        subtitle="自己录、存在本地，实时看浮动盈亏"
        actions={
          <div className="flex items-center gap-2">
            {holdings.length > 0 && (
              <AskAiButton context={aiContext} taskId="portfolio" label="让 AI 看我的持仓"
                suggestions={["我的持仓集中在哪些方向", "结构上有什么风险", "帮我梳理一下"]} />
            )}
            <button onClick={() => load(true)} disabled={refreshing}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50">
              {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新
            </button>
          </div>
        }
      />

      <div className="mb-4 flex items-start gap-2 rounded-lg border border-success/25 bg-success/5 p-3 text-xs text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-success" />
        <span>持仓<b className="text-foreground">只存在你本地</b>，不上传、不进仓库。行情每半小时自动刷新，也可手动刷新。本产品不提供标的、不给建议，只帮你把自己的账理清楚。</span>
      </div>

      {/* 汇总 */}
      {totals && holdings.length > 0 && (
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
          {[
            { k: "当日盈亏", v: (totals.day_pnl > 0 ? "+" : "") + fmt(totals.day_pnl), c: pnlColor(totals.day_pnl) },
            { k: "当日盈亏比例", v: (totals.day_pnl_pct > 0 ? "+" : "") + totals.day_pnl_pct + "%", c: pnlColor(totals.day_pnl) },
            { k: "总市值", v: fmt(totals.market_value), c: "text-foreground" },
            { k: "浮动盈亏", v: (totals.pnl > 0 ? "+" : "") + fmt(totals.pnl), c: pnlColor(totals.pnl) },
            { k: "盈亏比例", v: (totals.pnl_pct > 0 ? "+" : "") + totals.pnl_pct + "%", c: pnlColor(totals.pnl) },
          ].map((m) => (
            <GlassCard key={m.k} className="p-3">
              <p className="text-xs text-muted-foreground">{m.k}</p>
              <p className={cn("mt-1 font-mono text-lg font-bold", m.c)}>{m.v}</p>
            </GlassCard>
          ))}
        </div>
      )}

      {/* 录入：添加持仓 / 添加清仓记录，并列放在持仓明细上方 */}
      <div className="mb-4 grid gap-4 lg:grid-cols-[3fr_4fr]">
        <GlassCard>
          <h3 className="mb-3 text-sm font-semibold">添加持仓</h3>
          <div className="flex flex-wrap items-end gap-1.5">
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">股票代码</label>
              <StockSearchInput value={code} onChange={setCode} onPick={(c) => add(c)} placeholder="代码 / 中文 / 首字母" className="w-28" />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">数量（股）</label>
              <input value={shares} onChange={(e) => setShares(e.target.value.replace(/[^\d.]/g, ""))} placeholder="如 100"
                className="w-20 rounded-lg border border-border bg-black/20 px-2.5 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">成本价</label>
              <input value={cost} onChange={(e) => setCost(e.target.value.replace(/[^\d.-]/g, "").replace(/(?!^)-/g, ""))} placeholder="如 12.5，可负"
                className="w-24 rounded-lg border border-border bg-black/20 px-2.5 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <button onClick={() => add()}
              className="rounded-lg bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90">
              添加
            </button>
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground/60">同一代码再次添加会按加权平均成本合并（加仓）。</p>
        </GlassCard>

        <GlassCard className="overflow-x-auto">
          <h3 className="mb-3 text-sm font-semibold">添加清仓记录</h3>
          <div className="flex flex-nowrap items-end gap-1.5">
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">股票代码</label>
              <StockSearchInput value={cCode} onChange={setCCode} onPick={(c) => addClose(c)} placeholder="代码 / 中文 / 首字母" className="w-32" />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">清仓日期</label>
              <input type="date" value={cDate} onChange={(e) => setCDate(e.target.value)}
                className="rounded-lg border border-border bg-black/20 px-2 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">清仓价</label>
              <input value={cPrice} onChange={(e) => setCPrice(e.target.value.replace(/[^\d.]/g, ""))} placeholder="卖出价"
                className="w-20 rounded-lg border border-border bg-black/20 px-2.5 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">股数</label>
              <input value={cShares} onChange={(e) => setCShares(e.target.value.replace(/[^\d.]/g, ""))} placeholder="如 100"
                className="w-20 rounded-lg border border-border bg-black/20 px-2.5 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <button onClick={() => addClose()}
              className="rounded-lg bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90">
              添加
            </button>
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground/60">成本自动取添加持仓时录入的买入成本，股数会同步从当前持仓扣减。</p>
        </GlassCard>
      </div>

      {err && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" /> {err}
        </div>
      )}

      {/* 持仓表 */}
      <GlassCard glow>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="font-semibold">持仓明细</h3>
          {data?.updated && <span className="text-xs text-muted-foreground/60">更新于 {data.updated}</span>}
        </div>
        {holdings.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground/60">还没有持仓记录，用上面的表单添加一笔。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {["名称", "当日盈亏", "现价", "数量", "成本", "市值", "浮动盈亏", "盈亏%", ""].map((h) => (
                    <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {holdings.map((h) => (
                  <tr key={h.code} className="border-b border-border/30">
                    <td className="px-2 py-2.5">
                      <span className="font-medium">{h.name}</span>
                      <span className="ml-1.5 font-mono text-xs text-muted-foreground/60">{h.code}</span>
                    </td>
                    <td className={cn("px-2 py-2.5 font-mono", pnlColor(h.day_pnl))}>{h.day_pnl > 0 ? "+" : ""}{fmt(h.day_pnl)}</td>
                    <td className="px-2 py-2.5 font-mono">{fmtPx(h.price)}</td>
                    <td className="px-2 py-2.5 font-mono text-muted-foreground">{fmt(h.shares)}</td>
                    <td className="px-2 py-2.5 font-mono text-muted-foreground">{fmtPx(h.cost)}</td>
                    <td className="px-2 py-2.5 font-mono">{fmt(h.market_value)}</td>
                    <td className={cn("px-2 py-2.5 font-mono", pnlColor(h.pnl))}>{h.pnl > 0 ? "+" : ""}{fmt(h.pnl)}</td>
                    <td className={cn("px-2 py-2.5 font-mono", pnlColor(h.pnl))}>{h.pnl_pct > 0 ? "+" : ""}{h.pnl_pct}%</td>
                    <td className="px-2 py-2.5">
                      <button onClick={() => remove(h.code)} className="text-muted-foreground/50 hover:text-destructive" title="删除">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>

      {/* 已清仓列表 */}
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-muted-foreground">已清仓</h3>
        {closed.length > 0 && data && (
          <span className="text-sm">
            已实现盈亏合计 <b className={cn("font-mono", pnlColor(data.realized_pnl))}>{data.realized_pnl > 0 ? "+" : ""}{fmt(data.realized_pnl)}</b>
          </span>
        )}
      </div>
      <GlassCard>
        {closed.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground/60">还没有清仓记录。卖出后在上面记一笔，作为已实现盈亏的历史。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {["名称", "清仓日期", "清仓价", "股数", "成本", "已实现盈亏", "盈亏%", ""].map((h) => (
                    <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {closed.map((c, i) => (
                  <tr key={i} className="border-b border-border/30">
                    <td className="px-2 py-2.5">
                      <span className="font-medium">{c.name}</span>
                      <span className="ml-1.5 font-mono text-xs text-muted-foreground/60">{c.code}</span>
                    </td>
                    <td className="px-2 py-2.5 font-mono text-muted-foreground">{c.date}</td>
                    <td className="px-2 py-2.5 font-mono">{fmtPx(c.price)}</td>
                    <td className="px-2 py-2.5 font-mono text-muted-foreground">{fmt(c.shares)}</td>
                    <td className="px-2 py-2.5 font-mono text-muted-foreground">{fmtPx(c.cost)}</td>
                    <td className={cn("px-2 py-2.5 font-mono", pnlColor(c.pnl))}>{c.pnl > 0 ? "+" : ""}{fmt(c.pnl)}</td>
                    <td className={cn("px-2 py-2.5 font-mono", pnlColor(c.pnl))}>{c.pnl_pct > 0 ? "+" : ""}{c.pnl_pct}%</td>
                    <td className="px-2 py-2.5">
                      <button onClick={() => removeClosed(i)} className="text-muted-foreground/50 hover:text-destructive" title="删除">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>

    </div>
  );
}
