// Vibe-Research 后端 API 客户端。/api → vite 代理到本地 FastAPI（默认 8900）。
// 后端未启动或数据源异常时抛 ApiError，页面据此优雅降级。

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

// 后端访问密钥（对应后端部署时的 VR_API_KEY，公网部署防蹭用）。只存本地浏览器。
const ACCESS_KEY = "vr-access-key";

export function loadAccessKey(): string {
  try {
    return localStorage.getItem(ACCESS_KEY) || "";
  } catch {
    return "";
  }
}

export function saveAccessKey(key: string) {
  try {
    if (key) localStorage.setItem(ACCESS_KEY, key);
    else localStorage.removeItem(ACCESS_KEY);
  } catch {
    /* 隐私模式等场景 localStorage 不可用 */
  }
}

export function authHeaders(): Record<string, string> {
  const k = loadAccessKey();
  return k ? { Authorization: `Bearer ${k}` } : {};
}

export interface MyReport {
  id: string; name: string; industry: string; size: number; ext: string; ts: number;
}

// 下载/预览研报：带鉴权头 fetch → blob → 触发浏览器下载（<a download> 无法带 Authorization，故走 blob）。
export async function downloadReport(id: string, name: string): Promise<void> {
  const resp = await fetch(`/api/myreports/file/${id}`, { headers: authHeaders() });
  if (!resp.ok) throw new ApiError(`下载失败 HTTP ${resp.status}`, resp.status);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function request<T>(path: string, method: "GET" | "POST" | "DELETE" = "GET", body?: unknown): Promise<T> {
  let resp: Response;
  const headers: Record<string, string> = { ...authHeaders() };
  const opts: RequestInit = { method };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (Object.keys(headers).length > 0) opts.headers = headers;
  try {
    resp = await fetch(`/api${path}`, opts);
  } catch {
    throw new ApiError("连接不到后端，请先启动 backend（uvicorn app:app --port 8900）", 0);
  }
  let payload: any = null;
  try {
    payload = await resp.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      throw new ApiError("后端开启了访问鉴权（VR_API_KEY）：请在「接入 AI」页底部填写后端访问密钥", 401);
    }
    throw new ApiError(payload?.detail || `HTTP ${resp.status}`, resp.status);
  }
  return (payload?.data ?? payload) as T;
}

const get = <T>(path: string) => request<T>(path, "GET");

export interface Quote {
  name: string; price: number; last_close: number; change_pct: number;
  pe_ttm: number; pb: number; mcap_yi: number; turnover_pct: number;
  limit_up: number; limit_down: number; vol_ratio?: number;
}

export interface Valuation {
  name: string; code: string; price: number; mcap_yi: number;
  pe_ttm: number; pb: number;
  eps_26e: number | null; eps_27e: number | null; pe_26e: number | null;
  cagr_pct: number | null; peg: number | null; digest_years: number | null;
  analyst_count: number; forecast_note?: string;
}

export interface Report {
  title: string; publishDate: string; orgSName: string;
  emRatingName?: string; indvInduName?: string; pdfUrl?: string | null;
}

export interface ValMetric {
  current: number; percentile: number; min: number; max: number;
  p20: number; p50: number; p80: number; n: number;
}
export interface ValPercentile {
  period: string; metrics: { pe_ttm?: ValMetric; pb?: ValMetric };
}

export interface Announcement {
  date: string; title: string; type: string; url: string;
}

export interface Financials {
  period: string | null;
  revenue: string | null; revenue_yoy: string | null;
  net_profit: string | null; net_profit_yoy: string | null;
  eps: string | null; bvps: string | null; roe: string | null;
  gross_margin: string | null; net_margin: string | null; op_cf_ps: string | null;
}

export interface KLineRow {
  date: string;
  open: number;
  close: number;
  high: number;
  low: number;
  volume: number;
}

export interface KLineData {
  code: string;
  period: "day" | "week" | "month";
  adjustment: string;
  source: string;
  as_of: string | null;
  rows: KLineRow[];
}

export interface NewsItem {
  新闻标题?: string; 发布时间?: string; 文章来源?: string; 新闻链接?: string;
}

export interface IndexQuote {
  name: string; price: number; change_pct: number; change_amt: number;
}

export interface MarketSentiment {
  up: number; down: number; flat: number; zt: number; zt_real: number; dt: number; dt_real: number;
  active: string; breadth: string; speculation: string; date: string;
}
export interface SectorFlow {
  name: string; pct: number; net: number; inflow: number; outflow: number; firms: number;
}
export interface MarketOverview {
  sentiment: MarketSentiment; sectors: SectorFlow[]; updated: string;
}

// 短线情绪：连板梯队 / 最高连板 / 炸板率 / 封板率 / 晋级率 / 涨跌停家数 + 连板股清单（客观公开榜单）
export interface EmotionTier { boards: number; count: number; plus: boolean }
export interface LianbanStock {
  code: string; name: string; boards: number;
  price: number; pct: number; amount: number | null; float_cap: number | null; industry: string;
}
export interface ShortTermEmotion {
  date: string;
  zt_count: number; dt_count: number; zb_count: number;
  max_boards: number; lianban_count: number;
  ladder: EmotionTier[];
  lianban_stocks: LianbanStock[];
  seal_rate: number | null; break_rate: number | null; promotion_rate: number | null;
  yzt_count: number;
}

// 全市场成交额榜（客观公开榜单）
export interface TurnoverStock {
  code: string; name: string;
  price: number | null; pct: number | null;
  amount: number | null; mcap: number | null; float_cap: number | null; industry: string;
}
export interface TurnoverTop { stocks: TurnoverStock[]; updated: string }

export interface RadarItem {
  title: string; url: string; time: string; source: string; summary?: string; zh?: string;
}
export interface Industry {
  key: string; name: string; accent: string; total: number; items: RadarItem[];
}
export interface RadarData {
  generated_at: string | null; recent_days: number; industries: Industry[];
  stats: { industries: number; total_sources: number; failed_sources?: number };
}
export interface PublicNewsSearchResult {
  title: string; url: string; snippet: string; published: string; source: string;
}
export interface PublicNewsSearchData {
  query: string; results: PublicNewsSearchResult[];
}

export interface Holding {
  code: string; name: string; price: number; shares: number; cost: number;
  market_value: number; pnl: number; pnl_pct: number; day_pnl: number; day_pnl_pct: number;
}
export interface ClosedPosition {
  code: string; name: string; date: string; price: number; shares: number; cost: number;
  pnl: number; pnl_pct: number;
}
export interface PortfolioData {
  holdings: Holding[];
  totals: { market_value: number; cost: number; pnl: number; pnl_pct: number; day_pnl: number; day_pnl_pct: number };
  closed: ClosedPosition[];
  realized_pnl: number;
  updated: string; last_refresh: string | null;
}
// 持仓择时信号（research/A股优质个股中短期择时策略.md 规则，后端按前复权日 K 计算）
export interface TimingSignal {
  code: string;
  signal: "add" | "reduce" | "watch" | null;
  signal_label: string;
  strength: number;         // 0-3
  strength_label: string;   // ★ 或 —
  action: string;
  details: string[];
  as_of: string | null;
  pending: boolean;         // 当日盘中：信号未收盘确认
  rule: string;
}

// 资金面 / 筹码 / 信号（v3.3 并入，均为「用户查的那只股」的公开数据）
export interface MarginRow { date: string; rzye: number; rzmre: number; rzche: number; rqye: number; rqmcl: number; rzrqye: number }
export interface BlockTradeRow { date: string; price: number; close: number; premium_pct: number; vol: number; amount: number; buyer: string; seller: string }
export interface HolderRow { date: string; holder_num: number; change_ratio: number; avg_shares: number }
export interface DividendRow { date: string; bonus_rmb: number; transfer_ratio: number; bonus_ratio: number | null; plan: string }
export interface FundFlowRow { date: string; main_net: number; small_net: number; mid_net: number; large_net: number; super_net: number }
export interface DtSeat { name: string; buy_amt: number; sell_amt: number; net: number }
export interface DragonTiger {
  records: { date: string; reason: string; net_buy: number; turnover: number }[];
  seats: { buy: DtSeat[]; sell: DtSeat[] };
  institution: { buy_amt: number; sell_amt: number; net_amt: number };
}
export interface LockupRow { date: string; type: string; shares: number; able_shares: number; ratio: number }
export interface Lockup { history: LockupRow[]; upcoming: LockupRow[] }
export interface Board { name: string; code: string; change_pct: number | string; lead_stock: string }
export interface Blocks { total: number; boards: Board[]; concept_tags: string[] }
export interface HotConcept { concept: string; bk: string; hit: number }
export interface QaRow { company: string; question: string; answer: string | null; answerer: string; ask_time: string }
export interface IndustryRow { rank: number; name: string; change_pct: number; code: string; up_count: number; down_count: number }
export interface IndustryData { top: IndustryRow[]; bottom: IndustryRow[]; total: number }

export interface SectorScoreRow {
  code: string;
  name: string;
  score: number | null;
  phase: "综合占优" | "赔率观察" | "集中风险" | "相对偏弱" | "中性观察";
  latest_return: number | null;
  valuation: {
    score: number | null;
    pe: number | null;
    pe_percentile: number | null;
    pb: number | null;
    pb_percentile: number | null;
    history_samples: number;
  };
  prosperity: {
    score: number | null;
    earnings_3m: number | null;
    earnings_yoy: number | null;
  };
  attention: {
    score: number | null;
    turnover_rate: number | null;
    turnover_rate_percentile: number | null;
    turnover_share: number | null;
    turnover_share_percentile: number | null;
    daily_history_samples: number;
  };
  crowding: {
    risk: number | null;
    penalty: number;
  };
  data_quality: {
    history_samples: number;
    missing: string[];
  };
}

export interface SectorScoresData {
  schema_version: number;
  as_of: string;
  current_frequency: "daily" | "monthly";
  current_source?: "tencent_constituent_aggregate" | "sws_daily_fallback" | "sws_monthly_fallback";
  current_source_label?: string;
  quote_time?: string | null;
  classification_as_of?: string | null;
  official_anchor_as_of?: string | null;
  component_count?: number | null;
  quoted_component_count?: number | null;
  coverage_pct?: number | null;
  calculation_method?: string | null;
  is_intraday?: boolean;
  monthly_as_of: string;
  daily_history_samples: number;
  daily_error?: string | null;
  history_start: string;
  history_samples: number;
  history_requested?: number;
  history_partial?: boolean;
  generated_at: string;
  stale: boolean;
  refresh_error?: string;
  aggregate_error?: string | null;
  classification_error?: string | null;
  industries: SectorScoreRow[];
  methodology: {
    classification: string;
    frequency: string;
    weights: { valuation: number; prosperity: number; attention: number };
    penalty: string;
    definitions: string[];
    sources: { label: string; url: string | null }[];
  };
}

// 全球市场（美股 / 港股，移植自 global-stock-data · 东财域内源）
export interface GlobalIndex {
  key: string; name: string; region: string;
  price: number | null; change_pct: number | null;
  weight?: number;      // 该市场总市值近似（万亿美元），用于加权
  session?: string;     // 日盘 / 夜盘（中国视角）
  closed?: boolean;     // true = 该市场已闭市，显示的是收盘价
  hours_bj?: string[];  // 该市场交易时段换算成北京时间（含夏令时），如 ["21:30-次日04:00"]
}
export interface GlobalQuote {
  code: string; name: string;
  price: number | null; open: number | null; high: number | null; low: number | null;
  prev_close: number | null; amount: number | null; mcap: number | null; change_pct: number | null;
}
export interface GlobalMetrics {
  report_date: string;
  revenue: number | null; revenue_yoy: number | null; net_profit: number | null;
  eps: number | null; roe: number | null; gross_margin: number | null;
  net_margin: number | null; debt_ratio: number | null;
}
export interface GlobalStock {
  code: string; name: string; market: string;
  quote: GlobalQuote; metrics: GlobalMetrics | null;
}
export interface HkCashflowItem { amount: number | null; yoy: number | null }
export interface HkCashflowPeriod {
  report_date: string; report: string | null;
  currency: string | null; account_standard: string | null;
  items: Record<string, HkCashflowItem>;
}
export interface HkCashflow {
  code: string; name: string; market: string;
  currency: string | null; item_order: string[]; periods: HkCashflowPeriod[];
}


// 资金供给（独立页面 · 国内 / 国外美国，含历史趋势 + 美联储利率 + 加息概率）
export interface HistPoint { date: string; v: number }
export interface LiquidityUsItem {
  label: string; unit: string; value: number; date: string; chg: number | null; hist: HistPoint[];
}
export interface IndexFlow {
  name: string; hist: HistPoint[]; latest: HistPoint;
}
export interface LiquidityCn {
  date?: string;
  rzye_yi?: number; rzye_chg_yi?: number | null;
  rzrqye_yi?: number; rzrqye_chg_yi?: number | null;
  rzjme_yi?: number;
  total_main_net_yi?: number;
  rzrqye_hist?: HistPoint[]; rzjme_hist?: HistPoint[];
  index_flows?: Record<string, IndexFlow>;
}
export interface FedOddsStrike { strike: number; prob: number }
export interface FedOdds {
  event: string; meeting: string; likely_upper: string;
  strikes: FedOddsStrike[];
  stale?: boolean;      // true = 数据源暂不可用，展示的是最近一次缓存值
  fetched_at?: string;  // 数据实际获取时间（stale 时用于提示）
}
export interface IndexComponent { label: string; value: string; pct: number; hist?: HistPoint[] }
export interface CompositeIndex {
  value: number; label: string; desc: string; date: string;
  favorable?: "high" | "low";  // high=分高有利，low=分低有利（缺省按 low）
  hist: HistPoint[]; interpretation: string;
  components?: IndexComponent[];  // 子指标：当前值 + 各自分位，点击指数卡展开
}
export interface LiquidityData {
  cn: LiquidityCn;
  cn_indices?: Record<string, CompositeIndex>;
  us: Record<string, LiquidityUsItem>;
  us_indices?: Record<string, CompositeIndex>;
  fed_odds?: FedOdds;
  updated: string;
  /** 后端源故障回退 last-good 缓存时为 true，stale_since 为缓存生成时间 */
  stale?: boolean;
  stale_since?: string;
}

// 宏观面（国内重要宏观经济指标 · GDP/CPI/PPI/PMI/M2/工业增加值/进出口/贸易差额/社融）
export interface MacroIndicator {
  label: string;
  value: number;
  forecast: number | null;
  prev: number | null;
  date: string;
  hist: HistPoint[];
}
export interface MacroData {
  cn: Record<string, MacroIndicator>;
  groups: Record<string, string[]>;
  updated: string;
  stale?: boolean;
  stale_since?: string;
}

// 分时图（当日分钟级）
export interface MinutePoint { time: string; price: number; volume: number }
export interface MinuteKline { date: string; prev_close: number; points: MinutePoint[]; last_day?: boolean; market_minutes?: [number, number][] }

export interface SearchResult {
  code: string; name: string; market: string;
}

export const api = {
  health: () => get<{ ok: boolean }>("/health"),
  indices: () => get<IndexQuote[]>("/indices"),
  marketOverview: () => get<MarketOverview>("/market/overview"),
  liquidity: () => get<LiquidityData>("/market/liquidity"),
  macro: () => get<MacroData>("/market/macro"),
  emotion: () => get<ShortTermEmotion>("/market/emotion"),
  turnoverTop: () => get<TurnoverTop>("/market/turnover-top"),
  // 传 keys = 只刷新这几个市场（已开盘的）；不传 = 全量快照
  globalIndices: (keys?: string[]) =>
    get<GlobalIndex[]>(`/global/indices${keys?.length ? `?keys=${encodeURIComponent(keys.join(","))}` : ""}`),
  // 全球指数当日分时（key = 后端 _INDICES 的 key，如 spx/hsi/n225）
  globalMinute: (key: string) => get<MinuteKline>(`/global/minute?key=${encodeURIComponent(key)}`),
  globalStock: (symbol: string) => get<GlobalStock>(`/global/stock?symbol=${encodeURIComponent(symbol)}`),
  hkCashflow: (symbol: string) => get<HkCashflow>(`/global/hk/cashflow?symbol=${encodeURIComponent(symbol)}`),
  radar: () => get<RadarData>("/radar"),
  radarRefresh: () => request<RadarData>("/radar/refresh", "POST"),
  publicNewsSearch: (q: string, count = 5) =>
    get<PublicNewsSearchData>(`/public-news-search?q=${encodeURIComponent(q)}&count=${count}`),
  portfolio: () => get<PortfolioData>("/portfolio"),
  addHolding: (code: string, shares: number, cost: number) => request<PortfolioData>("/portfolio/holding", "POST", { code, shares, cost }),
  removeHolding: (code: string) => request<PortfolioData>(`/portfolio/holding?code=${code}`, "DELETE"),
  refreshPortfolio: () => request<PortfolioData>("/portfolio/refresh", "POST"),
  portfolioTiming: () => get<{ signals: Record<string, TimingSignal> }>("/portfolio/timing"),
  closePosition: (code: string, date: string, price: number, shares: number, cost?: number) =>
    request<PortfolioData>("/portfolio/close", "POST", { code, date, price, shares, ...(cost !== undefined ? { cost } : {}) }),
  removeClosed: (index: number) => request<PortfolioData>(`/portfolio/close?index=${index}`, "DELETE"),
  valuation: (code: string) => get<Valuation>(`/valuation?code=${code}`),
  minuteKline: (code: string) => get<MinuteKline>(`/kline/minute?code=${encodeURIComponent(code)}`),
  kline: (code: string, period: KLineData["period"], count = 250) =>
    get<KLineData>(`/kline/chart?code=${encodeURIComponent(code)}&period=${period}&count=${count}`),
  percentile: (code: string) => get<ValPercentile>(`/valuation/percentile?code=${code}`),
  financials: (code: string) => get<Financials>(`/financials?code=${code}`),
  announcements: (code: string) => get<Announcement[]>(`/announcements?code=${code}`),
  quote: (codes: string) => get<Record<string, Quote>>(`/quote?codes=${codes}`),
  reports: (code: string) => get<Report[]>(`/reports?code=${code}`),
  news: (code: string) => get<NewsItem[]>(`/news?code=${code}`),
  margin: (code: string) => get<MarginRow[]>(`/margin?code=${code}`),
  blockTrade: (code: string) => get<BlockTradeRow[]>(`/block-trade?code=${code}`),
  holders: (code: string) => get<HolderRow[]>(`/holders?code=${code}`),
  dividend: (code: string) => get<DividendRow[]>(`/dividend?code=${code}`),
  fundFlow: (code: string) => get<FundFlowRow[]>(`/fund-flow?code=${code}`),
  dragonTiger: (code: string) => get<DragonTiger>(`/dragon-tiger?code=${code}`),
  lockup: (code: string) => get<Lockup>(`/lockup?code=${code}`),
  blocks: (code: string) => get<Blocks>(`/blocks?code=${code}`),
  hotConcepts: (code: string) => get<HotConcept[]>(`/hot-concepts?code=${code}`),
  investorQa: (code: string) => get<QaRow[]>(`/investor-qa?code=${code}`),
  industry: (top = 20) => get<IndustryData>(`/industry?top=${top}`),
  sectorScoresCache: () => get<SectorScoresData | null>("/sector-scores/cache"),
  sectorScores: (refresh = false) =>
    get<SectorScoresData>(`/sector-scores${refresh ? "?refresh=true" : ""}`),
  search: (q: string) => get<SearchResult[]>(`/search?q=${encodeURIComponent(q)}`),
  myReports: () => get<MyReport[]>("/myreports"),
  uploadReport: (name: string, contentB64: string) =>
    request<MyReport>("/myreports", "POST", { name, content_b64: contentB64 }),
  deleteReport: (id: string) => request<{ ok: boolean }>(`/myreports/${id}`, "DELETE"),
};
