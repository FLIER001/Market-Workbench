import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { RefreshCw, Loader2, Trash2, AlertCircle, X, ChevronsUpDown, ChevronUp, ChevronDown, LineChart } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { StockSearchInput } from "@/components/ui/StockSearchInput";
import { FundPortfolioPanel } from "@/components/funds/FundPortfolioPanel";
import { isTradingHours } from "@/hooks/useLiveQuotes";
import { useSWR } from "@/hooks/useSWR";
import { useFreshOnEnter } from "@/hooks/useFreshOnEnter";
import { api, ApiError, type PortfolioData, type Holding, type TimingSignal, type FundPortfolioData, type LegacyPortfolioStatus } from "@/lib/api";
import { publishHoldingCodes } from "@/hooks/useHoldingCodes";
import { cn } from "@/lib/utils";

const REFRESH_MS = 3000; // 可见且交易时段内，证券行情按 3 秒快照更新
const pnlColor = (v: number) => (v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground");
const fmt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
// 单价类（现价/成本/清仓价）最多 4 位小数：ETF/基金常见 3-4 位，截断成 2 位会与市值/盈亏对不上账
const fmtPx = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
type SortKey = "name" | "day_pnl" | "price" | "shares" | "cost" | "market_value" | "pnl" | "pnl_pct";
type SortDir = "asc" | "desc";
const COLS: { label: string; key: SortKey | null }[] = [
  { label: "名称", key: "name" },
  { label: "当日盈亏", key: "day_pnl" },
  { label: "现价 / 成本", key: "price" },
  { label: "数量", key: "shares" },
  { label: "市值", key: "market_value" },
  { label: "浮动盈亏", key: "pnl" },
  { label: "盈亏%", key: "pnl_pct" },
  { label: "择时", key: null },
  { label: "", key: null },
];

// 择时信号展示样式：加仓红、减仓绿（A股口径红涨绿跌），观望灰
const SIGNAL_STYLE: Record<string, string> = {
  add: "border-danger/40 bg-danger/10 text-danger",
  reduce: "border-success/40 bg-success/10 text-success",
  watch: "border-border bg-black/20 text-muted-foreground",
};

export function Portfolio() {
  // 子栏目：场内证券（股票/ETF 持仓）与场外基金（公募基金持仓）分开记账
  const [kind, setKind] = useState<"securities" | "fund">("securities");
  const [legacy, setLegacy] = useState<LegacyPortfolioStatus | null>(null);
  const [importingLegacy, setImportingLegacy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const onLoadError = useCallback((e: unknown) => {
    setErr(e instanceof ApiError ? e.message : "加载失败");
  }, []);
  const { data, setData, revalidating: refreshing, revalidate } = useSWR<PortfolioData>(
    "portfolio",
    (fresh?: boolean) => api.portfolio(fresh),
    [],
    onLoadError,
    { persist: true, scope: "user" },
  );
  // 场外基金数据：与 FundPortfolioPanel 共用 key，秒开 + 并发去重；这里仅用于 tab 计数与 AI 上下文
  const { data: fundPortfolio, revalidating: fundRefreshing, revalidate: revalidateFund } = useSWR<FundPortfolioData>("fund:portfolio", (fresh?: boolean) => api.fundPortfolio(fresh), [], undefined, { persist: true, scope: "user" });
  // 5 分钟缓存窗口：点进持仓栏目，距上次刷新超过 5 分钟就真重算，5 分钟内直接看缓存。
  useFreshOnEnter("portfolio", revalidate);
  useFreshOnEnter("fund:portfolio", revalidateFund);
  useEffect(() => {
    api.portfolioLegacyStatus().then(setLegacy).catch(() => setLegacy(null));
  }, []);
  // 页面头部的「刷新」在场外子栏目时通过信号转发给 FundPortfolioPanel 执行
  const [fundRefreshTick, setFundRefreshTick] = useState(0);
  const [code, setCode] = useState("");
  const [shares, setShares] = useState("");
  const [cost, setCost] = useState("");
  const [boughtDate, setBoughtDate] = useState("");
  const sharesRef = useRef<HTMLInputElement>(null);
  const [, setAdding] = useState(false);
  // 清仓录入
  const [cCode, setCCode] = useState("");
  const [cDate, setCDate] = useState("");
  const [cPrice, setCPrice] = useState("");
  const [cShares, setCShares] = useState("");
  const [, setClosing] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  // 择时信号：随持仓加载后懒拉一次；展开某只看规则细节
  const [signals, setSignals] = useState<Record<string, TimingSignal>>({});
  const [openSignal, setOpenSignal] = useState<string | null>(null);
  // 清仓弹窗状态
  const [closeTarget, setCloseTarget] = useState<string | null>(null);

  const todayStr = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  };

  // 打开清仓弹窗：自动填充当天日期、现价、全部股数
  const openClose = (c: string) => {
    const h = data?.holdings.find((x) => x.code === c);
    setCCode(c);
    setCDate(todayStr());
    setCPrice(h ? fmtPx(h.price) : "");
    setCShares(h ? String(h.shares) : "");
    setCloseTarget(c);
    setErr(null);
  };

  // 快捷选择清仓股数：全部 / 1/2 / 1/3 / 1/4（按当前持仓总股数计算）
  const pickCloseShares = (fraction: 1 | 2 | 3 | 4) => {
    const h = data?.holdings.find((x) => x.code === closeTarget);
    if (!h) return;
    const value = fraction === 1 ? h.shares : h.shares / fraction;
    setCShares(String(parseFloat(value.toFixed(2))));
  };

  const closeClose = () => {
    setCloseTarget(null); setCCode(""); setCDate(""); setCPrice(""); setCShares("");
  };

  // 持仓代码广播给自选等页面（自动并入自选 + 持仓标注）
  useEffect(() => {
    if (data) publishHoldingCodes(data.holdings.map((h) => h.code));
  }, [data]);

  const load = useCallback(async (manual = false) => {
    await revalidate(manual);
    api.portfolioTiming()
      .then((r) => setSignals(r.signals || {}))
      .catch(() => { /* 信号失败不影响持仓主数据 */ });
  }, [revalidate]);

  useEffect(() => {
    if (data) setErr(null);
  }, [data]);

  const holdingCodes = data?.holdings.map((h) => h.code).join(",") || "";
  useEffect(() => {
    if (!holdingCodes) {
      setSignals({});
      return;
    }
    let alive = true;
    api.portfolioTiming()
      .then((r) => { if (alive) setSignals(r.signals || {}); })
      .catch(() => { /* 信号失败不影响持仓主数据 */ });
    return () => { alive = false; };
  }, [holdingCodes]);

  useEffect(() => {
    // 定时刷新只在「A股交易时段 + 页面在前台」时执行：收盘后/切到后台时持仓盈亏不变，不必刷。
    let timer: number | null = null;
    let cancelled = false;
    const tick = async () => {
      if (!document.hidden && isTradingHours()) await revalidate(true);
      if (!cancelled) timer = window.setTimeout(tick, REFRESH_MS);
    };
    timer = window.setTimeout(tick, REFRESH_MS);
    // 页面切回前台 / 开盘时补刷一次
    const onVisible = () => { if (!document.hidden) void revalidate(true); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [revalidate]);

  const add = async (overrideCode?: string) => {
    const c = (overrideCode || code).trim();
    if (!/^\d{6}$/.test(c)) { setErr("请输入 6 位股票代码"); return; }
    const s = parseFloat(shares), cc = parseFloat(cost);
    if (!(s > 0) || !Number.isFinite(cc)) { setErr("数量须大于 0，成本价请填数字（可为负）"); return; }
    setAdding(true); setErr(null);
    try {
      setData(await api.addHolding(c, s, cc, boughtDate || undefined));
      setCode(""); setShares(""); setCost(""); setBoughtDate("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "添加失败");
    } finally {
      setAdding(false);
    }
  };

  // 下拉选中股票后：拉取最新行情填成本价，买入日期默认当天（可改），光标跳转到数量框
  const pickStock = (c: string) => {
    setCode(c);
    setBoughtDate(todayStr());
    api.quote(c).then((quotes) => {
      const q = quotes[c];
      if (q && q.price) setCost(fmtPx(q.price));
    }).catch(() => {});
    // 光标跳到数量输入框（等 DOM 更新后）
    setTimeout(() => sharesRef.current?.focus(), 100);
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
      closeClose();
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
  // 累计盈亏 = 浮动盈亏 + 已实现盈亏
  const cumulativePnl = (totals?.pnl ?? 0) + (data?.realized_pnl ?? 0);
  const cumulativePnlPct = totals && totals.cost > 0
    ? ((totals.pnl + (data?.realized_pnl ?? 0)) / totals.cost) * 100
    : 0;
  const sortedHoldings = useMemo(() => {
    if (!sortKey) return holdings;
    const dir = sortDir === "asc" ? 1 : -1;
    return [...holdings].sort((a: Holding, b: Holding) => {
      const va = a[sortKey], vb = b[sortKey];
      if (typeof va === "string" && typeof vb === "string") return va.localeCompare(vb, "zh") * dir;
      return ((va as number) - (vb as number)) * dir;
    });
  }, [holdings, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir(key === "name" ? "asc" : "desc");
    }
  };

  const aiContext = totals
    ? `【场内证券】（本地数据）：\n` + holdings.map((h) => `${h.name}(${h.code}) ${h.shares}股 成本${h.cost} 现价${h.price} 浮盈${h.pnl}(${h.pnl_pct}%)`).join("\n") +
      `\n汇总：市值${totals.market_value} 总浮盈${totals.pnl}(${totals.pnl_pct}%)`
    : "【场内证券】：暂无记录。";
  // 整体持仓上下文：场内证券 + 场外基金合并给 AI
  const combinedAiContext = [
    aiContext,
    "",
    "【场外基金】",
    fundPortfolio && fundPortfolio.holdings.length > 0
      ? fundPortfolio.holdings.map((h) => `${h.name}(${h.code}) ${h.shares}份 成本${h.cost} 净值${h.nav} 浮盈${h.pnl}(${h.pnl_pct}%)`).join("\n") +
        `\n汇总：市值${fundPortfolio.totals.market_value} 总浮盈${fundPortfolio.totals.pnl}(${fundPortfolio.totals.pnl_pct}%)`
      : "暂无记录。",
    "",
    "整体汇总：场内市值 " + (totals?.market_value ?? 0) +
      " + 场外市值 " + (fundPortfolio?.totals.market_value ?? 0) +
      " = " + fmt((totals?.market_value ?? 0) + (fundPortfolio?.totals.market_value ?? 0)) +
      "；总浮盈 " + fmt((totals?.pnl ?? 0) + (fundPortfolio?.totals.pnl ?? 0)),
  ].join("\n");
  const hasAnyHolding = (holdings.length > 0 || (fundPortfolio?.holdings.length ?? 0) > 0);

  return (
    <div>
      <PageHeader
        title="持仓"
        subtitle="场内证券 / 场外基金分开记账，行情实时计算"
        actions={
          <div className="flex items-center gap-2">
            {hasAnyHolding && (
              <AskAiButton context={combinedAiContext} taskId="portfolio" label="让 AI 看我的整体持仓"
                suggestions={["整体持仓集中在哪些方向", "场内场外结构上有什么风险", "帮我梳理一下"]} />
            )}
            <button
              onClick={() => {
                if (kind === "securities") {
                  load(true);
                } else {
                  setFundRefreshTick((t) => t + 1);
                  revalidateFund(true);
                }
              }}
              disabled={kind === "securities" ? refreshing : fundRefreshing}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              {(kind === "securities" ? refreshing : fundRefreshing) ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新
            </button>
          </div>
        }
      />

      {legacy && ((kind === "securities" && legacy.securities.available && legacy.securities.target_empty) ||
        (kind === "fund" && legacy.fund.available && legacy.fund.target_empty)) && (() => {
        const status = kind === "securities" ? legacy.securities : legacy.fund;
        const field = kind === "securities" ? "securities" : "fund";
        return (
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warning/35 bg-warning/10 px-4 py-3 text-sm">
            <span className="text-warning">
              检测到旧版{kind === "securities" ? "场内证券" : "场外基金"}账本：{status.holdings} 项持仓、{status.closed} 条卖出记录。旧文件仍只读保留。
            </span>
            <button
              disabled={importingLegacy}
              onClick={async () => {
                setImportingLegacy(true);
                try {
                  await api.importLegacyPortfolio(kind);
                  setLegacy((current) => current ? { ...current, [field]: { ...status, target_empty: false } } : current);
                  if (kind === "securities") await revalidate(true);
                  else await revalidateFund(true);
                } catch (error) {
                  setErr(error instanceof ApiError ? error.message : "导入旧账本失败");
                } finally {
                  setImportingLegacy(false);
                }
              }}
              className="rounded-lg border border-warning/40 px-3 py-1.5 text-xs text-warning hover:bg-warning/10 disabled:opacity-50"
            >
              {importingLegacy ? "导入中…" : "导入当前账号"}
            </button>
          </div>
        );
      })()}

      {/* 子栏目切换：场内证券 / 场外基金（风格同自选栏目） */}
      <div className="mb-4 grid max-w-lg grid-cols-2 gap-2 rounded-xl border border-border/50 bg-muted/20 p-1.5">
        {([
          ["securities", "场内证券", holdings.length],
          ["fund", "场外基金", fundPortfolio?.holdings.length ?? 0],
        ] as const).map(([id, label, count]) => (
          <button
            key={id}
            onClick={() => setKind(id)}
            className={cn(
              "flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors",
              kind === id
                ? "bg-primary/15 text-primary shadow-sm"
                : "text-muted-foreground hover:bg-muted/40 hover:text-foreground",
            )}
          >
            {label}
            <span className="rounded-full bg-background/50 px-1.5 py-0.5 text-[10px] opacity-70">{count}</span>
          </button>
        ))}
      </div>

      {kind === "fund" ? (
        <FundPortfolioPanel refreshSignal={fundRefreshTick} />
      ) : (
      <>
      {/* 汇总 */}
      {totals && holdings.length > 0 && (
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
          <StatCard label="总市值" value={fmt(totals.market_value)} cls="text-foreground" />
          <StatCard label="浮动盈亏" value={(totals.pnl > 0 ? "+" : "") + fmt(totals.pnl)} cls={pnlColor(totals.pnl)}
                   sub={(totals.pnl_pct > 0 ? "+" : "") + totals.pnl_pct + "%"} subCls={pnlColor(totals.pnl)} />
          <StatCard label="累计盈亏" value={(cumulativePnl > 0 ? "+" : "") + fmt(cumulativePnl)} cls={pnlColor(cumulativePnl)}
                   sub={(cumulativePnlPct > 0 ? "+" : "") + cumulativePnlPct.toFixed(2) + "%"} subCls={pnlColor(cumulativePnl)} />
          <StatCard label="当日盈亏" value={(totals.day_pnl > 0 ? "+" : "") + fmt(totals.day_pnl)} cls={pnlColor(totals.day_pnl)}
                   sub={(totals.day_pnl_pct > 0 ? "+" : "") + totals.day_pnl_pct + "%"} subCls={pnlColor(totals.day_pnl)} />
          <StatCard label="本年盈亏"
                   value={(data?.ytd_pnl != null ? (data.ytd_pnl > 0 ? "+" : "") + fmt(data.ytd_pnl) : "—")}
                   cls={data?.ytd_pnl != null ? pnlColor(data.ytd_pnl) : ""}
                   sub={data?.ytd_pnl_pct != null ? (data.ytd_pnl_pct > 0 ? "+" : "") + data.ytd_pnl_pct + "%" : undefined}
                   subCls={data?.ytd_pnl != null ? pnlColor(data.ytd_pnl) : undefined}
                   title="年前买入按年初价、年内买入按成本价计，含本年已清仓实现盈亏" />
        </div>
      )}

      {/* 录入：添加持仓（内联条，同场外基金风格） */}
      {/* 录入：添加持仓（内联条，同场外基金风格） */}
      <GlassCard className="relative z-10 mb-4">
        <div className="flex flex-wrap items-center gap-2">
          <StockSearchInput value={code} onChange={setCode} onPick={pickStock} placeholder="搜股票 / ETF：代码 / 中文 / 首字母" className="w-64" />
          <input value={shares} onChange={(e) => setShares(e.target.value.replace(/[^\d.]/g, ""))} placeholder="数量（股）"
                 ref={sharesRef}
                 className="w-28 rounded-xl border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/60" />
          <input value={cost} onChange={(e) => setCost(e.target.value.replace(/[^\d.-]/g, "").replace(/(?!^)-/g, ""))} placeholder="成本价，可负"
                 className="w-28 rounded-xl border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/60" />
          <input type="date" value={boughtDate} onChange={(e) => setBoughtDate(e.target.value)} title="买入日期：年内买入按成本计本年盈亏，留空按年前持有计"
                 className="w-36 rounded-xl border border-border bg-black/20 px-3 py-2 text-sm text-muted-foreground outline-none focus:border-primary/60" />
          <button onClick={() => add()} className="rounded-xl bg-primary/80 px-4 py-2 text-sm font-semibold text-primary-foreground transition hover:bg-primary">
            添加
          </button>
        </div>
        {err && (
          <div className="mt-3 flex items-center gap-2 text-sm text-danger"><AlertCircle className="h-4 w-4 shrink-0" /> {err}</div>
        )}
      </GlassCard>

      {/* 持仓表 */}
      <GlassCard glow className="mb-4">
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
                  {COLS.map((col) => {
                    const k = col.key;
                    return (
                    <th key={col.label || "action"} className="whitespace-nowrap px-2 py-2 font-medium">
                      {k ? (
                        <button onClick={() => toggleSort(k)} className="inline-flex items-center gap-1 hover:text-foreground">
                          {col.label}
                          {sortKey === k ? (
                            sortDir === "asc" ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />
                          ) : (
                            <ChevronsUpDown className="h-3 w-3 text-muted-foreground/40" />
                          )}
                        </button>
                      ) : col.label}
                    </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {sortedHoldings.map((h) => (
                  <SignalRows key={h.code} h={h} sig={signals[h.code]}
                    open={openSignal === h.code}
                    onToggle={() => setOpenSignal(openSignal === h.code ? null : h.code)}
                    onRemove={() => remove(h.code)}
                    onClose={() => openClose(h.code)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>

      {/* 清仓弹窗：屏幕中央 */}
      {closeTarget && createPortal(
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4 backdrop-blur-[2px]"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeClose();
          }}
          role="presentation"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="securities-close-title"
            className="w-full max-w-md rounded-2xl border border-border/70 bg-background/95 p-5 shadow-2xl"
          >
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h3 id="securities-close-title" className="text-base font-semibold">清仓</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {data?.holdings.find((x) => x.code === closeTarget)?.name || closeTarget} ·{" "}
                  <span className="font-mono">{closeTarget}</span>
                </p>
              </div>
              <button onClick={closeClose} className="rounded-md p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground" title="关闭">
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-3.5">
              <div className="grid grid-cols-3 gap-2.5">
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">清仓日期</span>
                  <input type="date" value={cDate} onChange={(e) => setCDate(e.target.value)}
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm outline-none focus:border-primary/60" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">清仓价</span>
                  <input value={cPrice} onChange={(e) => setCPrice(e.target.value.replace(/[^\d.]/g, ""))} placeholder="卖出价"
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm font-mono outline-none focus:border-primary/60" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-muted-foreground">股数</span>
                  <input value={cShares} onChange={(e) => setCShares(e.target.value.replace(/[^\d.]/g, ""))} placeholder="如 100"
                         className="w-full rounded-xl border border-border bg-muted/20 px-2.5 py-2 text-sm font-mono outline-none focus:border-primary/60" />
                </label>
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-muted-foreground">快捷股数：</span>
                {([
                  [1, "全部"],
                  [2, "1/2"],
                  [3, "1/3"],
                  [4, "1/4"],
                ] as const).map(([fraction, label]) => (
                  <button
                    key={fraction}
                    onClick={() => pickCloseShares(fraction)}
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
                <button onClick={closeClose} className="h-8 rounded-lg px-3 text-xs text-muted-foreground hover:bg-muted/50">取消</button>
                <button onClick={() => addClose()} className="h-8 rounded-lg bg-primary/80 px-4 text-xs font-semibold text-primary-foreground transition hover:bg-primary">确认清仓</button>
              </div>
            </div>
          </div>
        </div>,
        document.body,
      )}

      {/* 已清仓列表：和持仓明细一样，标题放进卡片内 */}
      <GlassCard className="mb-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="font-semibold">已清仓</h3>
          {closed.length > 0 && data && (
            <span className="text-sm">
              已实现盈亏合计 <b className={cn("font-mono", pnlColor(data.realized_pnl))}>{data.realized_pnl > 0 ? "+" : ""}{fmt(data.realized_pnl)}</b>
            </span>
          )}
        </div>
        {closed.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground/60">还没有清仓记录。卖出后在上面记一笔，作为已实现盈亏的历史。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {["名称", "清仓日期", "清仓价", "股数", "成本", "已实现盈亏", "盈亏%", "清仓后", ""].map((h) => (
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
                    <td className={cn("px-2 py-2.5 font-mono", pnlColor(c.post_close_pct ?? 0))}>
                      {c.post_close_pct == null ? "—" : `${c.post_close_pct > 0 ? "+" : ""}${c.post_close_pct}%`}
                    </td>
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

      </>
      )}
    </div>
  );
}

/** 汇总卡片：标题 + 主数值 + 可选百分比（场外基金同款样式）。 */
function StatCard({ label, value, cls, sub, subCls, title }: {
  label: string; value: string; cls?: string; sub?: string; subCls?: string; title?: string;
}) {
  return (
    <GlassCard className="p-3" title={title}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn("mt-1 font-mono text-lg font-bold", cls)}>{value}</p>
      {sub && <p className={cn("mt-0.5 text-xs", subCls)}>{sub}</p>}
    </GlassCard>
  );
}

/** 持仓明细行 + 可展开的择时信号详情行。 */
function SignalRows({ h, sig, open, onToggle, onRemove, onClose }: {
  h: Holding;
  sig: TimingSignal | undefined;
  open: boolean;
  onToggle: () => void;
  onRemove: () => void;
  onClose: () => void;
}) {
  const style = sig?.signal ? SIGNAL_STYLE[sig.signal] : "border-border bg-black/20 text-muted-foreground";
  return (
    <>
      <tr className="border-b border-border/30">
        <td className="px-2 py-2.5">
          <Link
            to={`/stock-data?code=${h.code}`}
            className="font-medium underline-offset-4 transition-colors hover:text-primary hover:underline"
            title={`打开 ${h.name || h.code} 的个股数据`}
          >
            {h.name}
          </Link>
          <span className="ml-1.5 font-mono text-xs text-muted-foreground/60">{h.code}</span>
        </td>
        <td className={cn("px-2 py-2.5 font-mono", pnlColor(h.day_pnl))}>
          <span>{h.day_pnl > 0 ? "+" : ""}{fmt(h.day_pnl)}</span>
          <span className="block text-xs opacity-70">{h.day_pnl_pct > 0 ? "+" : ""}{h.day_pnl_pct}%</span>
        </td>
        <td className="px-2 py-2.5 font-mono">
          <span>{fmtPx(h.price)}</span>
          <span className="block text-xs text-muted-foreground">{fmtPx(h.cost)}</span>
          {h.bought_date && <span className="block text-[10px] text-muted-foreground/60">{h.bought_date} 买入</span>}
        </td>
        <td className="px-2 py-2.5 font-mono text-muted-foreground">{fmt(h.shares)}</td>
        <td className="px-2 py-2.5 font-mono">{fmt(h.market_value)}</td>
        <td className={cn("px-2 py-2.5 font-mono", pnlColor(h.pnl))}>{h.pnl > 0 ? "+" : ""}{fmt(h.pnl)}</td>
        <td className={cn("px-2 py-2.5 font-mono", pnlColor(h.pnl))}>{h.pnl_pct > 0 ? "+" : ""}{h.pnl_pct}%</td>
        <td className="px-2 py-2.5">
          {sig === undefined ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground/40" />
          ) : (
            <button onClick={onToggle} title={sig.action}
              className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs whitespace-nowrap hover:opacity-80", style)}>
              <LineChart className="h-3 w-3" />
              {sig.signal_label}
              <span className="font-mono tracking-tight">{sig.strength_label}</span>
              {open ? <ChevronUp className="h-3 w-3 opacity-60" /> : <ChevronDown className="h-3 w-3 opacity-60" />}
            </button>
          )}
        </td>
        <td className="px-2 py-2.5">
          <div className="flex items-center justify-end gap-1">
            <button onClick={onClose} className="rounded-lg px-2 py-1 text-xs text-muted-foreground transition hover:bg-black/20 hover:text-foreground">清仓</button>
            <button onClick={onRemove} className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-black/20 hover:text-destructive" title="删除">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </td>
      </tr>
      {open && sig && (
        <tr className="border-b border-border/30 bg-black/20">
          <td colSpan={9} className="px-3 py-3">
            <p className={cn("text-xs font-medium", sig.signal ? SIGNAL_STYLE[sig.signal].split(" ").pop() : "text-muted-foreground")}>
              {sig.action}
            </p>
            <ul className="mt-1.5 space-y-0.5 text-xs text-muted-foreground">
              {sig.details.map((d, i) => <li key={i}>· {d}</li>)}
            </ul>
            <p className="mt-1.5 text-[11px] text-muted-foreground/60">
              数据截至 {sig.as_of || "—"}
              {sig.since && ` · 信号触发于 ${sig.since}${sig.age_days > 0 ? `（第 ${sig.age_days} 个交易日，强度随时间衰减）` : "（当日）"}`}
              {sig.pending && "（盘中：信号以收盘确认为准，当前仅预警）"}
              {" "}· {sig.rule} · 规则化技术指标提示，非投资建议
            </p>
          </td>
        </tr>
      )}
    </>
  );
}
