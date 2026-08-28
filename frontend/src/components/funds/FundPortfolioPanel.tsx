import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertCircle, ChevronDown, ChevronUp, ChevronsUpDown, Loader2, Pencil, Trash2, X } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { api, ApiError, type FundPortfolioData, type FundSearchResult } from "@/lib/api";
import { fundRefreshIntervalMs } from "@/hooks/useLiveQuotes";
import { cn } from "@/lib/utils";
import { useSWR } from "@/hooks/useSWR";
import { FundSearchInput } from "./FundSearchInput";
import { FundDetail } from "./FundDetail";

const pnlColor = (v: number) => (v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground");
const fmt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const fmtPx = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
const fmtPct = (v: number | null) => v == null ? "—" : `${v > 0 ? "+" : ""}${v}%`;
type FSortKey = "name" | "nav" | "cost" | "estimate_pct" | "today_return_amount" | "yesterday_return_amount" | "market_value" | "shares" | "pnl";
type FSortDir = "asc" | "desc";

// 基金持仓：按最新公布净值算浮动盈亏；交易时段叠加盘中估值（推算值，与净值分列）。
export function FundPortfolioPanel({ refreshSignal }: { refreshSignal?: number }) {
  const { data, setData, revalidate } = useSWR<FundPortfolioData>("fund:portfolio", (fresh?: boolean) => api.fundPortfolio(fresh), [], undefined, { persist: true, scope: "user" });
  const [err, setErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<FundSearchResult | null>(null);
  const [shares, setShares] = useState("");
  const [cost, setCost] = useState("");
  const [boughtDate, setBoughtDate] = useState("");
  const sharesRef = useRef<HTMLInputElement>(null);
  const [detail, setDetail] = useState<{ code: string; name: string; from?: "holdings" | "closed" } | null>(null);
  // 卖出录入
  const [sellCode, setSellCode] = useState<string | null>(null);
  const [sellDate, setSellDate] = useState("");
  const [sellNav, setSellNav] = useState("");
  const [sellShares, setSellShares] = useState("");
  const [selling, setSelling] = useState(false);
  const [adding, setAdding] = useState(false);
  const [sortKey, setSortKey] = useState<FSortKey | null>(null);
  const [sortDir, setSortDir] = useState<FSortDir>("desc");
  // 修改持仓弹窗状态（改份额 / 成本净值 / 买入日期）
  const [editCode, setEditCode] = useState<string | null>(null);
  const [eShares, setEShares] = useState("");
  const [eCost, setECost] = useState("");
  const [eBoughtDate, setEBoughtDate] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);

  const todayStr = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  };

  // 点击卖出：弹出居中的卖出弹窗，自动填充「当天日期 + 最新净值 + 全部份额」
  const openSell = (code: string) => {
    const h = data?.holdings.find((x) => x.code === code);
    setSellCode(code);
    setSellDate(todayStr());
    setSellNav(h ? String(h.nav) : "");
    setSellShares(h ? String(h.shares) : "");
  };

  // 快捷选择卖出份额：全部 / 1/2 / 1/3 / 1/4（按当前持仓总份额计算）
  const pickSellShares = (fraction: 1 | 2 | 3 | 4) => {
    const h = data?.holdings.find((x) => x.code === sellCode);
    if (!h) return;
    const value = fraction === 1 ? h.shares : h.shares / fraction;
    setSellShares(String(parseFloat(value.toFixed(2))));
  };

  const closeSell = () => {
    setSellCode(null); setSellDate(""); setSellNav(""); setSellShares("");
  };

  // 打开修改弹窗：预填当前持仓的份额 / 成本净值 / 买入日期
  const openEdit = (code: string) => {
    const h = data?.holdings.find((x) => x.code === code);
    if (!h) return;
    setEShares(String(h.shares));
    setECost(fmtPx(h.cost));
    setEBoughtDate(h.bought_date || "");
    setEditCode(code);
    setErr(null);
  };

  const closeEdit = () => {
    setEditCode(null); setEShares(""); setECost(""); setEBoughtDate("");
  };

  const saveEdit = async () => {
    if (savingEdit || !editCode) return;
    const s = parseFloat(eShares), c = parseFloat(eCost);
    if (!(s > 0)) { setErr("份额须大于 0"); return; }
    if (!Number.isFinite(c) || c <= 0) { setErr("成本净值须大于 0"); return; }
    setSavingEdit(true); setErr(null);
    try {
      setData(await api.updateFundHolding(editCode, s, c, eBoughtDate || undefined));
      closeEdit();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "修改失败");
    } finally {
      setSavingEdit(false);
    }
  };

  // 排序：名称按中文排序，数值列空值排最后
  const sortedHoldings = useMemo(() => {
    const rows = data?.holdings || [];
    if (!sortKey) return rows;
    const dir = sortDir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      if (sortKey === "name") return a.name.localeCompare(b.name, "zh") * dir;
      const va = a[sortKey], vb = b[sortKey];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return ((va as number) - (vb as number)) * dir;
    });
  }, [data, sortKey, sortDir]);

  const toggleSort = (key: FSortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir(key === "name" ? "asc" : "desc");
    }
  };

  const SortTh = ({ label, k, title }: { label: string; k: FSortKey | null; title?: string }) => (
    <th className="whitespace-nowrap px-4 py-3 font-medium" title={title}>
      {k ? (
        <button onClick={() => toggleSort(k)} className="inline-flex items-center gap-1 hover:text-foreground">
          {label}
          {sortKey === k ? (
            sortDir === "asc" ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronsUpDown className="h-3 w-3 text-muted-foreground/40" />
          )}
        </button>
      ) : label}
    </th>
  );

  // 下拉选中基金后：拉取行情填成本净值，买入日期默认当天（可改），光标跳转到份额框
  const pickFund = (f: FundSearchResult) => {
    setPicked(f);
    setBoughtDate(todayStr());
    api.fundQuote([f.code]).then((quotes) => {
      const q = quotes[f.code];
      if (q && q.nav) setCost(fmtPx(q.nav));
    }).catch(() => {});
    setTimeout(() => sharesRef.current?.focus(), 100);
  };

  const load = useCallback(async (manual = false) => {
    try {
      await revalidate(manual);
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "加载失败");
    }
  }, [revalidate]);

  // 收到父组件的外部刷新信号时执行一次手动刷新
  useEffect(() => {
    if (refreshSignal && refreshSignal > 0) {
      revalidate(true);
    }
  }, [refreshSignal, revalidate]);

  useEffect(() => {
    let timer: number | null = null;
    let cancelled = false;
    const tick = async () => {
      const interval = fundRefreshIntervalMs();
      if (!document.hidden && interval != null) await load(true);
      if (!cancelled) timer = window.setTimeout(tick, interval ?? 60_000);
    };
    timer = window.setTimeout(tick, fundRefreshIntervalMs() ?? 60_000);
    const onVisible = () => { if (!document.hidden && fundRefreshIntervalMs() != null) void load(true); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  const add = async () => {
    if (!picked) { setErr("请先搜索选择基金"); return; }
    const s = parseFloat(shares), c = parseFloat(cost);
    if (!(s > 0)) { setErr("份额须大于 0"); return; }
    if (!Number.isFinite(c) || c <= 0) { setErr("成本净值须大于 0"); return; }
    setErr(null);
    setAdding(true);
    try {
      setData(await api.addFundHolding(picked.code, s, c, boughtDate || undefined));
      setPicked(null); setShares(""); setCost(""); setBoughtDate("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "添加失败");
    } finally {
      setAdding(false);
    }
  };

  const sell = async () => {
    if (!sellCode || selling) return;
    const n = parseFloat(sellNav), s = parseFloat(sellShares);
    if (!sellDate) { setErr("请选卖出日期"); return; }
    if (!(n > 0) || !(s > 0)) { setErr("卖出净值与份额须大于 0"); return; }
    setErr(null);
    setSelling(true);
    try {
      setData(await api.closeFundPosition(sellCode, sellDate, n, s));
      closeSell();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "卖出失败");
    } finally {
      setSelling(false);
    }
  };

  const t = data?.totals;
  // 累计盈亏 = 浮动盈亏 + 已实现盈亏
  const cumulativePnl = data ? (data.totals?.pnl ?? 0) + data.realized_pnl : 0;
  const cumulativePnlPct = data && data.totals?.cost > 0
    ? ((data.totals.pnl + data.realized_pnl) / data.totals.cost) * 100
    : null;
  return (
    <div className="space-y-4">
      {/* 汇总条 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
        <Stat label="总市值" value={t ? fmt(t.market_value) : "—"} />
        <Stat label="浮动盈亏" value={t ? fmt(t.pnl) : "—"} cls={t ? pnlColor(t.pnl) : ""}
              sub={t ? `${t.pnl_pct > 0 ? "+" : ""}${t.pnl_pct}%` : undefined} />
        <Stat label="累计盈亏" value={data ? fmt(cumulativePnl) : "—"} cls={data ? pnlColor(cumulativePnl) : ""}
              sub={cumulativePnlPct != null ? `${cumulativePnlPct > 0 ? "+" : ""}${cumulativePnlPct.toFixed(2)}%` : undefined} />
        <Stat label="今日收益" value={t?.today_pnl != null ? fmt(t.today_pnl) : "—"}
              cls={t?.today_pnl != null ? pnlColor(t.today_pnl) : ""}
              sub={t?.today_pnl_pct != null ? fmtPct(t.today_pnl_pct) : undefined} />
        <Stat label="昨日收益" value={t?.yesterday_pnl != null ? fmt(t.yesterday_pnl) : "—"}
              cls={t?.yesterday_pnl != null ? pnlColor(t.yesterday_pnl) : ""}
              sub={t?.yesterday_pnl_pct != null ? fmtPct(t.yesterday_pnl_pct) : undefined} />
        <Stat label="本年盈亏" value={data?.ytd_pnl != null ? (data.ytd_pnl > 0 ? "+" : "") + fmt(data.ytd_pnl) : "—"}
              cls={data?.ytd_pnl != null ? pnlColor(data.ytd_pnl) : ""}
              sub={data?.ytd_pnl_pct != null ? fmtPct(data.ytd_pnl_pct) : undefined} />
      </div>

      <GlassCard className="relative z-10">
        <div className="flex flex-wrap items-center gap-2">
          <FundSearchInput className="w-72" onPick={pickFund} />
          {picked && (
            <span className="rounded-lg bg-primary/15 px-2 py-1 text-xs text-primary">
              {picked.code} {picked.name}
            </span>
          )}
          <input value={shares} onChange={(e) => setShares(e.target.value)} placeholder="份额"
                 ref={sharesRef}
                 className="w-28 rounded-xl border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/60" />
          <input value={cost} onChange={(e) => setCost(e.target.value)} placeholder="成本净值"
                 className="w-28 rounded-xl border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/60" />
          <input type="date" value={boughtDate} onChange={(e) => setBoughtDate(e.target.value)} title="买入日期：年内买入按成本净值计本年盈亏，留空按年前持有计"
                 className="w-36 rounded-xl border border-border bg-black/20 px-3 py-2 text-sm text-muted-foreground outline-none focus:border-primary/60" />
          <button onClick={add} disabled={adding || !picked}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-primary/80 px-4 py-2 text-sm font-semibold text-primary-foreground transition hover:bg-primary disabled:cursor-not-allowed disabled:opacity-50">
            {adding && <Loader2 className="h-4 w-4 animate-spin" />}
            添加
          </button>
        </div>
        {err && (
          <div className="mt-3 flex items-center gap-2 text-sm text-danger"><AlertCircle className="h-4 w-4" /> {err}</div>
        )}
      </GlassCard>

      {/* 持仓表 */}
      <GlassCard className="overflow-x-auto p-0">
        {data?.updated && (
          <div className="flex items-center justify-end px-4 pt-3">
            <span className="text-xs text-muted-foreground/60">更新于 {data.updated}</span>
          </div>
        )}
        {!data ? (
          <div className="flex h-32 items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
        ) : data.holdings.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            还没有基金持仓。上方搜索基金，输入份额与成本净值即可记账。
          </div>
        ) : (
          <table className="w-full min-w-[860px] text-sm">
            <thead>
              <tr className="border-b border-border/60 text-left text-xs text-muted-foreground">
                <SortTh label="基金" k="name" />
                <SortTh label="最新净值 / 成本" k="nav" />
                <SortTh label="盘中估值" k="estimate_pct" title="官方估值已下线。指数基金按跟踪指数/场内ETF实时行情估算；主动股基按上季十大重仓推算；港股/QDII按海外指数代理（闭市显示隔夜，标「夜」）" />
                <SortTh label="今日收益" k="today_return_amount" title="仅显示北京时间当天已确认净值收益；未公布时显示空值" />
                <SortTh label="昨日收益" k="yesterday_return_amount" title="上一已确认净值日收益，金额按当前份额计算" />
                <SortTh label="市值 / 份额" k="market_value" />
                <SortTh label="浮动盈亏" k="pnl" />
                <th className="px-4 py-3 font-medium" />
              </tr>
            </thead>
            <tbody>
              {sortedHoldings.map((h) => (
                <Fragment key={h.code}>
                <tr className="border-b border-border/30 transition hover:bg-black/10">
                  <td className="px-4 py-3">
                    <button className="text-left" onClick={() => setDetail(detail?.code === h.code && detail?.from !== "closed" ? null : { code: h.code, name: h.name, from: "holdings" })}>
                      <div className="font-medium text-primary hover:underline">{h.name}</div>
                      <div className="font-mono text-xs text-muted-foreground">{h.code}</div>
                    </button>
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {fmtPx(h.nav)}
                    <div className="text-xs text-muted-foreground">成本 {fmtPx(h.cost)}</div>
                    {h.bought_date && <div className="text-[10px] text-muted-foreground/60">{h.bought_date} 买入</div>}
                  </td>
                  <td className={cn("px-4 py-3 font-mono", h.estimate_pct != null ? pnlColor(h.estimate_pct) : "text-muted-foreground")}>
                    {h.estimate_pct != null ? `${h.estimate_pct > 0 ? "+" : ""}${h.estimate_pct}%` : "—"}
                    {h.estimate_pct != null && h.estimate_stale && (
                      <span
                        className="ml-0.5 text-xs text-muted-foreground"
                        title={`海外市场已闭市，显示最近一场(隔夜)涨跌幅${h.estimate_proxy ? `，按「${h.estimate_proxy}」代理` : ""}`}
                      >
                        夜
                      </span>
                    )}
                  </td>
                  <td className={cn("px-4 py-3 font-mono", h.today_return_amount != null ? pnlColor(h.today_return_amount) : "text-muted-foreground")}>
                    {h.today_return_amount != null ? fmt(h.today_return_amount) : "—"}
                    {h.today_return_pct != null && <div className="text-xs">{fmtPct(h.today_return_pct)}</div>}
                  </td>
                  <td className={cn("px-4 py-3 font-mono", h.yesterday_return_amount != null ? pnlColor(h.yesterday_return_amount) : "text-muted-foreground")}>
                    {h.yesterday_return_amount != null ? fmt(h.yesterday_return_amount) : "—"}
                    {h.yesterday_return_pct != null && <div className="text-xs">{fmtPct(h.yesterday_return_pct)}</div>}
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {fmt(h.market_value)}
                    <div className="text-xs text-muted-foreground">{fmt(h.shares)} 份</div>
                  </td>
                  <td className={cn("px-4 py-3 font-mono", pnlColor(h.pnl))}>
                    {fmt(h.pnl)}
                    <div className="text-xs">{h.pnl_pct > 0 ? "+" : ""}{h.pnl_pct}%</div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => openEdit(h.code)}
                        className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-black/20 hover:text-foreground" title="修改份额 / 成本"
                      ><Pencil className="h-3.5 w-3.5" /></button>
                      <button
                        onClick={() => openSell(h.code)}
                        className="rounded-lg px-2 py-1 text-xs text-muted-foreground transition hover:bg-black/20 hover:text-foreground"
                      >卖出</button>
                      <button
                        onClick={async () => { try { setData(await api.removeFundHolding(h.code)); } catch { /* ignore */ } }}
                        className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-black/20 hover:text-danger" title="删除"
                      ><Trash2 className="h-3.5 w-3.5" /></button>
                    </div>
                  </td>
                </tr>
                {detail?.code === h.code && detail.from !== "closed" && (
                  <tr className="border-b border-border/30">
                    <td colSpan={8} className="bg-black/10 px-4 pb-3">
                      <FundDetail code={h.code} name={h.name} onClose={() => setDetail(null)} />
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </GlassCard>

      {/* 卖出弹窗：屏幕中央 */}
      {sellCode && createPortal(
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4 backdrop-blur-[2px]"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeSell();
          }}
          role="presentation"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="fund-sell-title"
            className="w-full max-w-md rounded-2xl border border-border/70 bg-background/95 p-5 shadow-2xl"
          >
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h3 id="fund-sell-title" className="text-base font-semibold">卖出基金</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {data?.holdings.find((x) => x.code === sellCode)?.name || sellCode} ·{" "}
                  <span className="font-mono">{sellCode}</span>
                </p>
              </div>
              <button
                onClick={closeSell}
                className="rounded-md p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                title="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-3.5">
              <div className="grid grid-cols-3 gap-2.5">
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">卖出日期</span>
                  <input type="date" value={sellDate} onChange={(e) => setSellDate(e.target.value)}
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm outline-none focus:border-primary/60" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">卖出净值</span>
                  <input value={sellNav} onChange={(e) => setSellNav(e.target.value.replace(/[^\d.]/g, ""))} placeholder="如 1.2345"
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm font-mono outline-none focus:border-primary/60" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">卖出份额</span>
                  <input value={sellShares} onChange={(e) => setSellShares(e.target.value.replace(/[^\d.]/g, ""))} placeholder="如 1000"
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm font-mono outline-none focus:border-primary/60" />
                </label>
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-muted-foreground">快捷份额：</span>
                {([
                  [1, "全部"],
                  [2, "1/2"],
                  [3, "1/3"],
                  [4, "1/4"],
                ] as const).map(([fraction, label]) => (
                  <button
                    key={fraction}
                    onClick={() => pickSellShares(fraction)}
                    className="rounded-lg border border-border/60 bg-muted/20 px-2.5 py-1 text-xs text-muted-foreground transition hover:border-primary/50 hover:text-primary"
                  >
                    {label}
                  </button>
                ))}
              </div>

              {err && (
                <div className="flex items-center gap-2 text-sm text-danger">
                  <AlertCircle className="h-4 w-4 shrink-0" /> {err}
                </div>
              )}

              <div className="flex items-center justify-end gap-2 border-t border-border/40 pt-3">
                <button
                  onClick={closeSell}
                  className="h-8 rounded-lg px-3 text-xs text-muted-foreground hover:bg-muted/50"
                >
                  取消
                </button>
                <button
                  onClick={sell}
                  disabled={selling}
                  className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-primary/80 px-4 text-xs font-semibold text-primary-foreground transition hover:bg-primary disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {selling && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  {selling ? "卖出中…" : "确认卖出"}
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body,
      )}

      {/* 修改持仓弹窗：屏幕中央 */}
      {editCode && createPortal(
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4 backdrop-blur-[2px]"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeEdit();
          }}
          role="presentation"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="fund-edit-title"
            className="w-full max-w-md rounded-2xl border border-border/70 bg-background/95 p-5 shadow-2xl"
          >
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h3 id="fund-edit-title" className="text-base font-semibold">修改基金持仓</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {data?.holdings.find((x) => x.code === editCode)?.name || editCode} ·{" "}
                  <span className="font-mono">{editCode}</span>
                </p>
              </div>
              <button
                onClick={closeEdit}
                className="rounded-md p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                title="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-3.5">
              <div className="grid grid-cols-3 gap-2.5">
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">份额</span>
                  <input value={eShares} onChange={(e) => setEShares(e.target.value.replace(/[^\d.]/g, ""))}
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm font-mono outline-none focus:border-primary/60" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">成本净值</span>
                  <input value={eCost} onChange={(e) => setECost(e.target.value.replace(/[^\d.]/g, ""))} placeholder="如 1.2345"
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm font-mono outline-none focus:border-primary/60" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">买入日期</span>
                  <input type="date" value={eBoughtDate} onChange={(e) => setEBoughtDate(e.target.value)}
                         title="年内买入按成本净值计本年盈亏，年前买入按年初净值计；清空则按年前持有计"
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm text-muted-foreground outline-none focus:border-primary/60" />
                </label>
              </div>

              {err && (
                <div className="flex items-center gap-2 text-sm text-danger">
                  <AlertCircle className="h-4 w-4 shrink-0" /> {err}
                </div>
              )}

              <div className="flex items-center justify-end gap-2 border-t border-border/40 pt-3">
                <button
                  onClick={closeEdit}
                  className="h-8 rounded-lg px-3 text-xs text-muted-foreground hover:bg-muted/50"
                >
                  取消
                </button>
                <button
                  onClick={saveEdit}
                  disabled={savingEdit}
                  className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-primary/80 px-4 text-xs font-semibold text-primary-foreground transition hover:bg-primary disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {savingEdit && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  {savingEdit ? "保存中…" : "保存"}
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body,
      )}

      {/* 已卖出 */}
      {data && data.closed.length > 0 && (
        <GlassCard className="overflow-x-auto p-0">
          <div className="border-b border-border/60 px-4 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">已卖出</div>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                {["基金", "卖出日期", "份额 @ 净值", "已实现盈亏", "卖出后", ""].map((h) => (
                  <th key={h} className="whitespace-nowrap px-4 py-2 font-medium" title={h === "卖出后" ? "卖出后至今该基金涨跌幅：正=卖早了，负=卖对了" : undefined}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.closed.map((c, i) => (
                <Fragment key={`${c.code}-${i}`}>
                <tr className="border-b border-border/30 last:border-0">
                  <td className="px-4 py-2.5">
                    <button
                      className="text-left"
                      onClick={() => setDetail(detail?.code === c.code && detail?.from === "closed" ? null : { code: c.code, name: c.name, from: "closed" })}
                      title="点击展开基金详情（净值走势 / 业绩 / 重仓）"
                    >
                      <span className="font-medium text-primary hover:underline">{c.name}</span>
                      <span className="ml-2 font-mono text-xs text-muted-foreground">{c.code}</span>
                    </button>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground">{c.date}</td>
                  <td className="px-4 py-2.5 font-mono">{fmt(c.shares)} 份 @ {fmtPx(c.nav)}</td>
                  <td className={cn("px-4 py-2.5 font-mono", pnlColor(c.pnl))}>{fmt(c.pnl)}（{c.pnl_pct > 0 ? "+" : ""}{c.pnl_pct}%）</td>
                  <td className={cn("px-4 py-2.5 font-mono", pnlColor(c.post_close_pct ?? 0))} title="卖出后至今涨跌幅（按最新净值）">
                    {c.post_close_pct == null ? "—" : `${c.post_close_pct > 0 ? "+" : ""}${c.post_close_pct}%`}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={async () => { try { setData(await api.removeFundClosed(i)); } catch { /* ignore */ } }}
                      className="rounded-lg p-1 text-muted-foreground transition hover:bg-black/20 hover:text-danger" title="删除记录"
                    ><Trash2 className="h-3.5 w-3.5" /></button>
                  </td>
                </tr>
                {detail?.code === c.code && detail.from === "closed" && (
                  <tr className="border-b border-border/30 last:border-0">
                    <td colSpan={6} className="bg-black/10 px-4 pb-3">
                      <FundDetail code={c.code} name={c.name} onClose={() => setDetail(null)} />
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </GlassCard>
      )}
    </div>
  );
}

function Stat({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <GlassCard className="p-3.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-1 font-mono text-lg font-bold", cls)}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
    </GlassCard>
  );
}
