import { useEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Activity, Radar, LayoutGrid, Wallet, Settings, Search, Menu, X,
  Moon, Sun, ChevronsLeft, ChevronsRight, LineChart, Github, Globe, Zap,
  Star, FileText, Droplets, Loader2, PieChart, Coins, Gauge, Landmark, Flame, Scale, FlaskConical, Building2,
  ChevronDown, HeartPulse, LogOut, type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";
import { storageGet, storageSet } from "@/lib/storage";
import { loadUser, clearSession, auth } from "@/lib/auth";
import { clearLocalUserData } from "@/lib/userData";
import { clearUserSWRCache } from "@/hooks/useSWR";
import { formatRefreshTime, dataTimeFor, subscribeRefresh, type DataTime } from "@/lib/dataFreshness";
import { api, type SearchResult } from "@/lib/api";
const REPO_URL = "https://github.com/FLIER001/Market-Workbench";

type NavItem = { to: string; icon: LucideIcon; label: string; archive: string; match?: string; children?: NavItem[] };
type NavGroup = { label: string; items: NavItem[] };

const NAV_GROUPS: NavGroup[] = [
  {
    label: "市场",
    items: [
      { to: "/daily-review", icon: Activity, label: "市场全景", archive: "01" },
      {
        to: "/allocation", icon: Scale, label: "择时配置", archive: "02",
        children: [
          { to: "/macro", icon: Globe, label: "宏观面", archive: "02·1" },
          { to: "/liquidity", icon: Droplets, label: "资金面", archive: "02·2" },
        ],
      },
      { to: "/pulse", icon: Gauge, label: "全球预期", archive: "03" },
      { to: "/intel", icon: Radar, label: "资讯", archive: "04" },
    ],
  },
  {
    label: "组合",
    items: [
      { to: "/watchlist", icon: Star, label: "自选", archive: "05" },
      { to: "/portfolio", icon: Wallet, label: "持仓", archive: "06" },
    ],
  },
  {
    label: "研究",
    items: [
      { to: "/factors", icon: FlaskConical, label: "因子研究", archive: "07", match: "/factors" },
      { to: "/sectors", icon: LayoutGrid, label: "行业研究", archive: "08", match: "/sectors" },
      { to: "/screening", icon: PieChart, label: "标的筛选", archive: "09", match: "/screening" },
      { to: "/event-analysis", icon: Zap, label: "事件分析", archive: "10" },
      { to: "/bonds", icon: Landmark, label: "债市", archive: "11" },
      { to: "/fedwatch", icon: Building2, label: "美联储", archive: "12" },
      { to: "/gold", icon: Coins, label: "黄金", archive: "13" },
      { to: "/oil", icon: Flame, label: "油价", archive: "14" },
      { to: "/research", icon: FileText, label: "研究", archive: "15", match: "/research" },
    ],
  },
];

export function Layout() {
  const { pathname } = useLocation();
  const { dark, toggle } = useDarkMode();
  const navigate = useNavigate();
  const user = loadUser();
  const [collapsed, setCollapsed] = useState(() => storageGet("vr-sidebar") === "collapsed");
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 767px)").matches);
  const [mobileOpen, setMobileOpen] = useState(false);
  const allocationActive = ["/allocation", "/macro", "/liquidity"].includes(pathname);
  const [allocationOpen, setAllocationOpen] = useState(false);
  // 离开择时配置栏目就折叠子栏目，回到该栏目也不自动展开
  useEffect(() => { if (!allocationActive) setAllocationOpen(false); }, [allocationActive]);
  const compact = collapsed && !narrow;
  const pageTitle = NAV_GROUPS.flatMap((group) => group.items).flatMap((item) => [item, ...(item.children ?? [])])
    .find((item) => item.match ? pathname.startsWith(item.match) : pathname === item.to)?.label ?? "研究工作台";
  // 栏目标题后展示的是本页数据自身的时点（见 lib/dataFreshness）
  const [dataTime, setDataTime] = useState<DataTime | null>(() => dataTimeFor(pathname));
  const [stockCode, setStockCode] = useState("");
  const [stockSuggestions, setStockSuggestions] = useState<SearchResult[]>([]);
  const [showStockSuggestions, setShowStockSuggestions] = useState(false);
  const [stockHighlight, setStockHighlight] = useState(-1);
  const [stockSearching, setStockSearching] = useState(false);
  const stockSearchRef = useRef<HTMLFormElement>(null);
  const stockInputRef = useRef<HTMLInputElement>(null);
  const stockDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stockRequestRef = useRef(0);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 767px)");
    const onChange = () => setNarrow(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  useEffect(() => setMobileOpen(false), [pathname]);
  useEffect(() => {
    setDataTime(dataTimeFor(pathname));
    return subscribeRefresh(() => setDataTime(dataTimeFor(pathname)));
  }, [pathname]);
  useEffect(() => storageSet("vr-sidebar", collapsed ? "collapsed" : "expanded"), [collapsed]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = target?.matches("input, textarea, select, [contenteditable='true']");
      if (((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") || (event.key === "/" && !typing)) {
        event.preventDefault();
        stockInputRef.current?.focus();
      }
      if (event.key === "Escape") setMobileOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const logout = () => {
    auth.logout().catch(() => { /* 后端不在也允许本地登出 */ });
    clearSession();
    clearLocalUserData();
    clearUserSWRCache();
    navigate("/login", { replace: true });
  };

  const pickStock = (stock: SearchResult) => {
    navigate(`/stock-data?code=${encodeURIComponent(stock.code)}`);
    setStockCode("");
    setStockSuggestions([]);
    setShowStockSuggestions(false);
    setStockHighlight(-1);
  };

  useEffect(() => {
    if (stockDebounceRef.current) clearTimeout(stockDebounceRef.current);
    const query = stockCode.trim();
    if (!query) {
      stockRequestRef.current += 1;
      setStockSuggestions([]);
      setShowStockSuggestions(false);
      setStockSearching(false);
      return;
    }
    stockDebounceRef.current = setTimeout(() => {
      const requestId = ++stockRequestRef.current;
      setStockSearching(true);
      api.search(query)
        .then((results) => {
          if (requestId !== stockRequestRef.current) return;
          setStockSuggestions(results);
          setShowStockSuggestions(true);
          setStockHighlight(-1);
        })
        .catch(() => {
          if (requestId !== stockRequestRef.current) return;
          setStockSuggestions([]);
          setShowStockSuggestions(false);
        })
        .finally(() => {
          if (requestId === stockRequestRef.current) setStockSearching(false);
        });
    }, 300);
    return () => { if (stockDebounceRef.current) clearTimeout(stockDebounceRef.current); };
  }, [stockCode]);

  useEffect(() => {
    const closeSuggestions = (event: MouseEvent) => {
      if (!stockSearchRef.current?.contains(event.target as Node)) {
        setShowStockSuggestions(false);
        setStockHighlight(-1);
      }
    };
    document.addEventListener("mousedown", closeSuggestions);
    return () => document.removeEventListener("mousedown", closeSuggestions);
  }, []);

  const navLink = ({ to, icon: Icon, label, archive, match }: NavItem, child = false) => {
    const active = match ? pathname.startsWith(match) : pathname === to;
    return (
      <Link
        key={to}
        to={to}
        title={compact ? label : undefined}
        aria-current={active ? "page" : undefined}
        className={cn("nav-link", child && "nav-link-child", active && "nav-link-active", compact && "justify-center px-0")}
      >
        {compact ? <Icon className={cn("h-[17px] w-[17px]", child && "h-[15px] w-[15px]")} aria-hidden="true" /> : <><span className="truncate">{label}</span><span className="ml-auto font-mono text-[10px] text-[#a8b6a4]/70">{archive}</span></>}
      </Link>
    );
  };

  // 带子栏目的行：折叠按钮排在编号左侧，编号仍与其它栏目同在右端，保持竖向对齐
  const navParentRow = (item: NavItem) => {
    const active = item.match ? pathname.startsWith(item.match) : pathname === item.to;
    return (
      <div className={cn("nav-link", active && "nav-link-active")}>
        <Link to={item.to} aria-current={active ? "page" : undefined} className="min-w-0 flex-1 truncate">{item.label}</Link>
        {active && (
          <button
            onClick={() => setAllocationOpen((open) => !open)}
            className="p-1 text-[#a8b6a4] transition-colors hover:text-white"
            aria-label={allocationOpen ? "收起择时配置子栏目" : "展开择时配置子栏目"}
            aria-expanded={allocationOpen}
          >
            <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", allocationOpen && "rotate-180")} />
          </button>
        )}
        <span className="font-mono text-[10px] text-[#a8b6a4]/70">{item.archive}</span>
      </div>
    );
  };

  return (
    <div className="flex h-dvh overflow-hidden bg-background">
      <a href="#main-content" className="skip-link">跳到主要内容</a>
      {mobileOpen && (
        <button aria-label="关闭导航" onClick={() => setMobileOpen(false)} className="fixed inset-0 z-40 bg-black/45 md:hidden" />
      )}
      <aside
        aria-label="主导航"
        className={cn(
          "app-sidebar fixed inset-y-0 left-0 z-50 flex w-[230px] shrink-0 flex-col transition-[transform,width] duration-200 md:relative md:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
          compact && "md:w-[72px]",
        )}
      >
        <div className={cn("flex h-[88px] shrink-0 items-center border-b border-white/10 px-[18px]", compact && "h-[58px] justify-center px-2")}>
          <Link to="/daily-review" className="min-w-0" aria-label="Market Workbench 首页">
            {compact ? <LineChart className="h-5 w-5" /> : (
              <>
                <span className="block truncate text-[14px] font-semibold uppercase tracking-[0.08em] text-[#f4f1e8]">Market Workbench</span>
                <span className="mt-1 block text-[11px] tracking-[0.04em] text-[#a8b6a4]">个人投资研究工作台</span>
              </>
            )}
          </Link>
          <button onClick={() => setMobileOpen(false)} className="ml-auto p-2 text-[#c8d0c5] md:hidden" aria-label="关闭导航">
            <X className="h-5 w-5" />
          </button>
        </div>

        <nav className={cn("flex-1 overflow-y-auto px-3 py-4", compact && "px-2")}>
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="mb-5 last:mb-0">
              {!compact && <p className="mb-1.5 px-3 text-[10px] font-medium tracking-[0.16em] text-[#a8b6a4]">{group.label}</p>}

              <div className="space-y-0.5">
                {group.items.map((item) => (
                  <div key={item.to}>
                    {item.children && !compact ? navParentRow(item) : navLink(item)}
                    {item.children && !compact && allocationActive && allocationOpen && (
                      <div className={cn("mt-0.5 space-y-0.5", !compact && "ml-5 border-l border-white/10 pl-2")}>
                        {item.children.map((child) => navLink(child, true))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className={cn("shrink-0 border-t border-white/10 p-3", compact && "px-2")}>
          <div className={cn("flex items-center gap-1", compact ? "flex-col" : "justify-between")}>
            <div className={cn("flex items-center gap-1", compact && "flex-col")}>
              <Link to="/settings" className={cn("sidebar-tool", pathname === "/settings" && "bg-white/10 text-white")} title="接入 AI" aria-label="接入 AI">
                <Settings className="h-4 w-4" />
              </Link>
              <Link to="/source-health" className={cn("sidebar-tool", pathname === "/source-health" && "bg-white/10 text-white")} title="数据源健康" aria-label="数据源健康">
                <HeartPulse className="h-4 w-4" />
              </Link>
              <a href={REPO_URL} target="_blank" rel="noreferrer" className="sidebar-tool" title="GitHub" aria-label="GitHub 仓库">
                <Github className="h-4 w-4" />
              </a>
              <button onClick={toggle} className="sidebar-tool" title={dark ? "切换为亮色" : "切换为暗色"} aria-label={dark ? "切换为亮色" : "切换为暗色"}>
                {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </button>
            </div>
            <button onClick={() => setCollapsed((value) => !value)} className="sidebar-tool hidden md:inline-flex" title={collapsed ? "展开导航" : "收起导航"} aria-label={collapsed ? "展开导航" : "收起导航"}>
              {collapsed ? <ChevronsRight className="h-4 w-4" /> : <ChevronsLeft className="h-4 w-4" />}
            </button>
          </div>
          {user && (
            <div className={cn("mt-2 flex items-center border-t border-white/10 pt-2", compact ? "flex-col gap-1" : "gap-2")}>
              <span className="flex h-7 w-7 shrink-0 items-center justify-center bg-[#2f7664] text-xs font-semibold text-white">
                {user.username.slice(0, 1).toUpperCase()}
              </span>
              {!compact && <span className="min-w-0 flex-1 truncate text-xs text-[#dce4d6]">{user.username}</span>}
              {!compact && <button onClick={logout} className="sidebar-tool" title="退出登录" aria-label="退出登录"><LogOut className="h-4 w-4" /></button>}
            </div>
          )}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="app-topbar z-30 flex shrink-0 flex-wrap items-center gap-3 px-4 py-3 md:h-[58px] md:flex-nowrap md:px-[22px] md:py-0">
            <button onClick={() => setMobileOpen(true)} className="p-2 text-muted-foreground hover:bg-muted md:hidden" aria-label="打开导航">
              <Menu className="h-5 w-5" />
            </button>
            <Link to="/daily-review" className="flex items-center gap-2 font-semibold md:hidden">
              <LineChart className="h-5 w-5 text-primary" /><span>Market Workbench</span>
            </Link>
            <div className="hidden items-baseline gap-3 md:flex">
              <span className="text-[17px] font-semibold tracking-[0.02em]">{pageTitle}</span>
              <span className="h-3.5 w-px bg-border" aria-hidden="true" />
              <span className="font-mono text-[11px] tracking-[0.04em] text-muted-foreground" title={dataTime ? "本页数据的最新时点" : "本页数据未携带时点"}>
                {dataTime ? formatRefreshTime(dataTime) : "—"}
              </span>
            </div>
            <form
              ref={stockSearchRef}
              role="search"
              onSubmit={(event) => {
                event.preventDefault();
                const stock = stockHighlight >= 0 ? stockSuggestions[stockHighlight] : stockSuggestions[0];
                if (stock) pickStock(stock);
              }}
              className="relative order-2 flex min-w-0 flex-1 items-center md:order-none md:ml-auto md:w-[240px] md:flex-none lg:w-[280px]"
            >
              {/* 图标居中交给外层 span：animate-spin 的 keyframes 会覆盖元素自身的 -translate-y-1/2，
                  直接套在定位元素上会让图标在旋转过程中上下窜动 */}
              <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center" aria-hidden="true">
                {stockSearching
                  ? <Loader2 className="h-4 w-4 animate-spin text-primary" />
                  : <Search className="h-4 w-4 text-muted-foreground" />}
              </span>
              <input
                ref={stockInputRef}
                value={stockCode}
                onChange={(event) => { setStockCode(event.target.value); setShowStockSuggestions(true); setStockHighlight(-1); }}
                onFocus={() => stockSuggestions.length > 0 && setShowStockSuggestions(true)}
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setStockHighlight((index) => Math.min(index + 1, stockSuggestions.length - 1));
                  } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setStockHighlight((index) => Math.max(index - 1, -1));
                  } else if (event.key === "Escape") {
                    setShowStockSuggestions(false);
                    setStockHighlight(-1);
                  }
                }}
                placeholder="检索 股票 / 代码 / 拼音"
                aria-label="搜索股票"
                aria-expanded={showStockSuggestions && stockSuggestions.length > 0}
                aria-controls="stock-search-results"
                autoComplete="off"
                className="app-search"
              />
              <kbd className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 border border-border px-1.5 font-mono text-[10px] text-muted-foreground">/</kbd>
              {showStockSuggestions && stockSuggestions.length > 0 && (
                <div id="stock-search-results" className="absolute left-0 right-0 top-full z-50 mt-2 overflow-hidden border border-border bg-card py-1">
                  {stockSuggestions.map((stock, index) => (
                    <button
                      key={`${stock.market}:${stock.code}`}
                      type="button"
                      onClick={() => pickStock(stock)}
                      onMouseEnter={() => setStockHighlight(index)}
                      className={cn("flex w-full items-center gap-3 px-3 py-2.5 text-left text-sm hover:bg-muted/60", index === stockHighlight && "bg-muted/60")}
                    >
                      <span className="font-mono text-xs tabular-nums text-muted-foreground">{stock.code}</span>
                      <span className="min-w-0 flex-1 truncate">{stock.name}</span>
                      <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">{stock.market}</span>
                    </button>
                  ))}
                </div>
              )}
            </form>
            <div id="page-actions" className="order-3 flex w-full shrink-0 items-center justify-end gap-2 md:order-none md:w-auto" />
        </header>
        <main id="main-content" className="min-w-0 flex-1 overflow-auto" tabIndex={-1}>
          <div className={pathname === "/daily-review" ? "daily-archive" : "mx-auto max-w-[1440px] px-4 py-6 md:px-7 md:py-7"}>
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
