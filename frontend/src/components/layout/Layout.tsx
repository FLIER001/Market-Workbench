import { useEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";
import {
  Activity, Radar, LayoutGrid, Wallet, Settings, Search,
  Moon, Sun, ChevronsLeft, ChevronsRight, LineChart, Github, Globe, Zap,
  Star, FileText, Droplets, Loader2, PieChart, Coins, Gauge, Landmark, Flame, Scale, FlaskConical,
  ChevronDown, HeartPulse, type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";
import { storageGet, storageSet } from "@/lib/storage";
import { loadUser, clearSession, auth } from "@/lib/auth";
import { clearLocalUserData } from "@/lib/userData";
import { clearUserSWRCache } from "@/hooks/useSWR";
import { api, type SearchResult } from "@/lib/api";
import { useNavigate } from "react-router-dom";
import { LogOut } from "lucide-react";
import appPackage from "../../../package.json";

const APP_VERSION = `v${appPackage.version}`;
const REPO_URL = "https://github.com/FLIER001/Vibe-Research";

type NavItem = { to: string; icon: LucideIcon; label: string; match?: string };
type NavNode = NavItem & { children?: NavItem[] };

const NAV: NavNode[] = [
  { to: "/daily-review", icon: Activity, label: "市场全景" },
  {
    to: "/allocation",
    icon: Scale,
    label: "择时配置",
    children: [
      { to: "/macro", icon: Globe, label: "宏观面" },
      { to: "/liquidity", icon: Droplets, label: "资金面" },
    ],
  },
  { to: "/pulse", icon: Gauge, label: "全球预期" },
  { to: "/event-analysis", icon: Zap, label: "事件分析" },
  { to: "/intel", icon: Radar, label: "资讯" },
  { to: "/watchlist", icon: Star, label: "自选" },
  { to: "/portfolio", icon: Wallet, label: "持仓" },
  { to: "/factors", icon: FlaskConical, label: "因子研究", match: "/factors" },
  { to: "/sectors", icon: LayoutGrid, label: "行业研究", match: "/sectors" },
  { to: "/screening", icon: PieChart, label: "标的筛选", match: "/screening" },
  { to: "/bonds", icon: Landmark, label: "债市" },
  { to: "/gold", icon: Coins, label: "黄金" },
  { to: "/oil", icon: Flame, label: "油价" },
  { to: "/research", icon: FileText, label: "笔记", match: "/research" },
];

export function Layout() {
  const { pathname } = useLocation();
  const { dark, toggle } = useDarkMode();
  const nav = useNavigate();
  const user = loadUser();
  const [collapsed, setCollapsed] = useState(() => storageGet("vr-sidebar") === "collapsed");
  const [allocationOpen, setAllocationOpen] = useState(false);
  const [stockCode, setStockCode] = useState("");
  const [stockSuggestions, setStockSuggestions] = useState<SearchResult[]>([]);
  const [showStockSuggestions, setShowStockSuggestions] = useState(false);
  const [stockHighlight, setStockHighlight] = useState(-1);
  const [stockSearching, setStockSearching] = useState(false);
  const stockSearchRef = useRef<HTMLFormElement>(null);
  const stockDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stockRequestRef = useRef(0);

  const logout = () => {
    auth.logout().catch(() => { /* 后端不在也允许本地登出 */ });
    clearSession();
    // 退出时清空本地缓存的用户数据，避免下一个账号读到这个账号的自选/笔记
    clearLocalUserData();
    clearUserSWRCache();
    nav("/login", { replace: true });
  };

  const pickStock = (stock: SearchResult) => {
    nav(`/stock-data?code=${encodeURIComponent(stock.code)}`);
    setStockCode("");
    setStockSuggestions([]);
    setShowStockSuggestions(false);
    setStockHighlight(-1);
  };

  useEffect(() => {
    storageSet("vr-sidebar", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);

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

    return () => {
      if (stockDebounceRef.current) clearTimeout(stockDebounceRef.current);
    };
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

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className={cn(
        "glass z-10 m-2 flex shrink-0 flex-col rounded-2xl transition-all duration-200",
        collapsed ? "w-14" : "w-60",
      )}>
        {/* Brand */}
        <div className={cn("border-b border-border/50", collapsed ? "flex justify-center p-3" : "p-4")}>
          <Link to="/daily-review" className={cn("flex items-center", collapsed ? "justify-center" : "gap-2")}>
            <LineChart className="h-6 w-6 shrink-0 text-primary text-glow" />
            {!collapsed && (
              <span className="text-lg font-extrabold tracking-tight">
                Market <span className="text-primary">Workbench</span>
              </span>
            )}
          </Link>
          {!collapsed && <p className="mt-1 text-[11px] text-muted-foreground">本地市场研究工作台 · A股/美股/港股</p>}
        </div>

        {/* Nav */}
        <nav className={cn("flex-1 space-y-1 overflow-auto", collapsed ? "p-1.5" : "p-2.5")}>
          {NAV.map((item) => {
            if ("children" in item && item.children) {
              const { to, icon: Icon, label, children } = item;
              const active = pathname.startsWith(to);
              return (
                <div key={to}>
                  <div
                    className={cn(
                      "flex items-center rounded-lg text-sm transition-colors",
                      active
                        ? "bg-primary/15 font-medium text-primary shadow-glow"
                        : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                    )}
                  >
                    <Link
                      to={to}
                      title={collapsed ? label : undefined}
                      className={cn(
                        "flex flex-1 items-center",
                        collapsed ? "justify-center p-2.5" : "gap-2.5 px-3 py-2.5",
                      )}
                    >
                      <Icon className="h-4 w-4 shrink-0" />
                      {!collapsed && label}
                    </Link>
                    {!collapsed && (
                      <button
                        onClick={() => setAllocationOpen((open) => !open)}
                        className="mr-1.5 rounded p-1.5 opacity-70 transition-opacity hover:opacity-100"
                        title={allocationOpen ? "收起子板块" : "展开子板块"}
                      >
                        <ChevronDown
                          className={cn(
                            "h-3.5 w-3.5 transition-transform duration-200",
                            allocationOpen && "rotate-180",
                          )}
                        />
                      </button>
                    )}
                  </div>
                  {!collapsed && allocationOpen && (
                    <div className="mt-0.5 space-y-0.5 pl-4">
                      {children.map(({ to: childTo, icon: ChildIcon, label: childLabel }) => (
                        <Link
                          key={childTo}
                          to={childTo}
                          className={cn(
                            "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors",
                            pathname === childTo
                              ? "bg-primary/15 font-medium text-primary shadow-glow"
                              : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                          )}
                        >
                          <ChildIcon className="h-4 w-4 shrink-0" />
                          {childLabel}
                        </Link>
                      ))}
                    </div>
                  )}
                </div>
              );
            }
            const { to, icon: Icon, label, match } = item;
            const active = match ? pathname.startsWith(match) : pathname === to;
            return (
              <div key={to}>
                {(to === "/watchlist" || to === "/factors") && (
                  <div className="my-1.5 border-t border-border/50" aria-hidden="true" />
                )}
                <Link
                  to={to}
                  title={collapsed ? label : undefined}
                  className={cn(
                    "flex items-center rounded-lg text-sm transition-colors",
                    collapsed ? "justify-center p-2.5" : "gap-2.5 px-3 py-2.5",
                    active
                      ? "bg-primary/15 font-medium text-primary shadow-glow"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  {!collapsed && label}
                </Link>

              </div>
            );
          })}
        </nav>

        {/* Footer */}
        <div className={cn("border-t border-border/50", collapsed ? "flex flex-col items-center gap-2 p-2" : "space-y-2 p-3")}>
          {collapsed ? (
            <>
              <Link
                to="/settings"
                className={cn(
                  "rounded p-1.5 transition-colors",
                  pathname === "/settings" ? "bg-primary/15 text-primary shadow-glow" : "text-muted-foreground hover:text-foreground",
                )}
                title="接入 AI"
              >
                <Settings className="h-4 w-4" />
              </Link>
              <button onClick={toggle} className="rounded p-1.5 text-muted-foreground transition-colors hover:text-foreground" title={dark ? "亮色" : "暗色"}>
                {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </button>
              {user && (
                <button onClick={logout} className="rounded p-1.5 text-muted-foreground transition-colors hover:text-danger" title={`${user.username} · 退出登录`}>
                  <LogOut className="h-4 w-4" />
                </button>
              )}
              <button onClick={() => setCollapsed(false)} className="rounded p-1.5 text-muted-foreground transition-colors hover:text-foreground" title="展开">
                <ChevronsRight className="h-4 w-4" />
              </button>
              <Link
                to="/source-health"
                className={cn(
                  "rounded p-1.5 transition-colors",
                  pathname === "/source-health" ? "bg-primary/15 text-primary shadow-glow" : "text-muted-foreground hover:text-foreground",
                )}
                title="数据源健康"
              >
                <HeartPulse className="h-4 w-4" />
              </Link>
            </>
          ) : (
            <>
              <div className="flex items-center justify-between">
                {user ? (
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/20 text-[10px] font-bold text-primary">
                      {user.username.slice(0, 1).toUpperCase()}
                    </span>
                    <span className="max-w-20 truncate text-xs font-medium text-foreground">{user.username}</span>
                    <button onClick={logout} className="shrink-0 rounded p-1 text-muted-foreground transition-colors hover:text-danger" title="退出登录">
                      <LogOut className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ) : <span />}
                <div className="flex items-center gap-2">
                  <button onClick={toggle} className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground" title={dark ? "切换为亮色" : "切换为暗色"}>
                    {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
                  </button>
                  <Link
                    to="/settings"
                    className={cn(
                      "rounded p-1 transition-colors",
                      pathname === "/settings" ? "bg-primary/15 text-primary shadow-glow" : "text-muted-foreground hover:text-foreground",
                    )}
                    title="接入 AI"
                  >
                    <Settings className="h-3.5 w-3.5" />
                  </Link>
                  <a href={REPO_URL} target="_blank" rel="noreferrer" className="text-muted-foreground transition-colors hover:text-foreground" title="GitHub">
                    <Github className="h-3.5 w-3.5" />
                  </a>
                  <button onClick={() => setCollapsed(true)} className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground" title="收起">
                    <ChevronsLeft className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <p className="flex items-center justify-between text-[11px] leading-relaxed text-muted-foreground/60">
                {APP_VERSION}
                <Link
                  to="/source-health"
                  className={cn(
                    "rounded p-0.5 transition-colors",
                    pathname === "/source-health" ? "text-primary" : "hover:text-foreground",
                  )}
                  title="数据源健康"
                >
                  <HeartPulse className="h-3 w-3" />
                </Link>
              </p>
            </>
          )}
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto">
        <div className="relative mx-auto max-w-6xl px-6 py-6">
          <div className="relative z-20 mb-4 flex justify-center md:pointer-events-none md:absolute md:left-1/2 md:top-6 md:mb-0 md:w-[260px] md:-translate-x-1/2 lg:w-[280px]">
          <form
            ref={stockSearchRef}
            onSubmit={(event) => {
              event.preventDefault();
              const stock = stockHighlight >= 0
                ? stockSuggestions[stockHighlight]
                : stockSuggestions[0];
              if (stock) pickStock(stock);
            }}
            className="glass pointer-events-auto relative flex w-full items-center gap-2 rounded-xl px-2.5 py-1.5 shadow-lg"
          >
            {stockSearching
              ? <Loader2 className="ml-1 h-4 w-4 shrink-0 animate-spin text-primary" />
              : <Search className="ml-1 h-4 w-4 shrink-0 text-muted-foreground" />}
            <input
              value={stockCode}
              onChange={(event) => {
                setStockCode(event.target.value);
                setShowStockSuggestions(true);
                setStockHighlight(-1);
              }}
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
              placeholder="代码 / 中文 / 拼音，如 600519、茅台、MT、AAPL"
              aria-label="搜索股票"
              autoComplete="off"
              className="min-w-0 flex-1 bg-transparent px-1 py-1.5 text-sm outline-none placeholder:text-muted-foreground/55"
            />
            {showStockSuggestions && stockSuggestions.length > 0 && (
              <div className="absolute left-0 right-0 top-full mt-2 overflow-hidden rounded-xl border border-border bg-card py-1 shadow-xl">
                {stockSuggestions.map((stock, index) => (
                  <button
                    key={`${stock.market}:${stock.code}`}
                    type="button"
                    onClick={() => pickStock(stock)}
                    onMouseEnter={() => setStockHighlight(index)}
                    className={cn(
                      "flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-muted/60",
                      index === stockHighlight && "bg-muted/60",
                    )}
                  >
                    <span className="font-mono text-xs text-muted-foreground">{stock.code}</span>
                    <span className="min-w-0 flex-1 truncate">{stock.name}</span>
                    <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">{stock.market}</span>
                  </button>
                ))}
              </div>
            )}
          </form>
          </div>
          <Outlet />
        </div>
      </main>
    </div>
  );
}
