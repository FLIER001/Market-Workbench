// Market Workbench 后端 API 客户端。/api → vite 代理到本地 FastAPI（默认 8900）。
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
  const token = localStorage.getItem("vr-auth-token") || "";
  return {
    ...(k ? { "X-VR-Access-Key": k } : {}),
    ...(token ? { "X-VR-User-Token": token } : {}),
  };
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

// 默认请求超时：后端挂起时 fetch 原本会一直吊着，现在统一封顶。
// 慢接口（AI 解读等待 LLM、RSS 全量重建、持仓重算等）在各自方法上传 SLOW_TIMEOUT_MS。
const DEFAULT_TIMEOUT_MS = 30_000;
const SLOW_TIMEOUT_MS = 150_000;

function isTimeoutError(err: unknown): boolean {
  return err instanceof DOMException && (err.name === "TimeoutError" || err.name === "AbortError");
}

async function request<T>(
  path: string,
  method: "GET" | "POST" | "PUT" | "DELETE" = "GET",
  body?: unknown,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  let resp: Response;
  const headers: Record<string, string> = { ...authHeaders() };
  const opts: RequestInit = { method };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (Object.keys(headers).length > 0) opts.headers = headers;
  try {
    resp = await fetch(`/api${path}`, { ...opts, signal: AbortSignal.timeout(timeoutMs) });
  } catch (err) {
    if (isTimeoutError(err)) {
      throw new ApiError(`请求超时（${Math.round(timeoutMs / 1000)}s）：后端可能正在重建数据，稍后重试`, 504);
    }
    throw new ApiError("连接不到后端，请先启动 backend（uvicorn app:app --port 8900）", 0);
  }
  let payload: any = null;
  try {
    payload = await resp.json();
  } catch (err) {
    // 响应体读取阶段也可能超时（大 payload + 慢链路）
    if (isTimeoutError(err)) {
      throw new ApiError(`请求超时（${Math.round(timeoutMs / 1000)}s）：后端可能正在重建数据，稍后重试`, 504);
    }
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

const get = <T>(path: string, timeoutMs?: number) => request<T>(path, "GET", undefined, timeoutMs);

export interface Quote {
  name: string; price: number; last_close: number; change_pct: number;
  pe_ttm: number; pb: number; mcap_yi: number; turnover_pct: number;
  limit_up: number; limit_down: number; vol_ratio?: number;
}

export interface Valuation {
  name: string; code: string; price: number; mcap_yi: number;
  pe_ttm: number; pb: number;
  exchange?: string;
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
  /** 被剔除的无效读数条数（PE<0 亏损 / PB<=0 资不抵债 / PEG<=0 增速≤0）。 */
  dropped?: number;
}
export interface ValPercentile {
  period: string; metrics: { pe_ttm?: ValMetric; pb?: ValMetric; peg?: ValMetric };
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
  stats: { industries: number; total_sources: number; failed_sources?: number; stale_sources?: string[] };
  cache_state?: "fresh" | "stale" | "refreshing" | "error";
  cached_at?: string | null; refresh_error?: string | null;
}
export interface PublicNewsSearchResult {
  title: string; url: string; snippet: string; published: string; source: string;
}
export interface PublicNewsSearchData {
  query: string; results: PublicNewsSearchResult[];
}

export interface Holding {
  code: string; name: string; price: number; shares: number; cost: number;
  bought_date: string | null;  // 买入日期（最早一笔）；未填为 null，YTD 按年前买入计
  market_value: number; pnl: number; pnl_pct: number; day_pnl: number; day_pnl_pct: number;
}
export interface ClosedPosition {
  code: string; name: string; date: string; price: number; shares: number; cost: number;
  pnl: number; pnl_pct: number;
  post_close_pct: number | null;  // 清仓后至今涨跌幅（现价 vs 清仓价）；现价取不到为 null
}
export interface PortfolioData {
  holdings: Holding[];
  totals: { market_value: number; cost: number; pnl: number; pnl_pct: number; day_pnl: number; day_pnl_pct: number };
  closed: ClosedPosition[];
  realized_pnl: number;
  ytd_pnl?: number; ytd_pnl_pct?: number;
  updated: string; last_refresh: string | null;
}
// 持仓择时信号（research/A股优质个股中短期择时策略.md 规则，后端按前复权日 K 计算）
// 事件式：信号只在规则触发日产生并带有效期/衰减，不是"位置镜像"
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
  since: string | null;     // 当前信号事件的触发日期
  age_days: number;         // 距触发日的交易日数（当日为 0）
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
    activity_level?: number | null;
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

export interface SwLevel2Row {
  code: string;
  name: string;
  level1_code: string;
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
    activity_level?: number | null;
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

export interface SwLevel2Data {
  schema_version: number;
  as_of: string;
  monthly_as_of: string;
  history_start: string;
  history_samples: number;
  daily_history_samples: number;
  industry_count: number;
  level1_names: Record<string, string>;
  industries: SwLevel2Row[];
  is_intraday?: boolean;
  generated_at: string;
  stale: boolean;
  refresh_error?: string;
  cache_state?: "fresh" | "stale" | "refreshing" | "error";
  cached_at?: string | null;
  methodology: {
    classification: string;
    frequency: string;
    weights: { valuation: number; prosperity: number; attention: number };
    penalty: string;
    definitions: string[];
    sources: { label: string; url: string | null }[];
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
  cache_state?: "fresh" | "stale" | "refreshing" | "error";
  cached_at?: string | null;
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


// ---- 板块双评分（强度 + 机会） ----
export interface PlateScoreRow {
  board_code: string;
  board_name: string;
  board_group: string;
  sector_key: string | null;
  rank: number;
  strength: {
    score: number | null;
    detail: {
      relative_trend: number | null;
      breadth_impulse: number | null;
      flow_confirmation: number | null;
      trend_quality: number | null;
      leader_concentration: number | null;
      er20: number | null;
      er60: number | null;
      ma20_coverage?: number | null;
      ma20_coverage_change?: number | null;
      top3_return_contribution?: number | null;
    };
  };
  opportunity: {
    score: number | null;
    detail: {
      fundamental: number | null;
      earnings_revision: number | null;
      valuation_match: number | null;
      position_score: number | null;
      crowding_score: number | null;
      catalyst: number | null;
    };
    coverage?: {
      financial: number;
      forecast: number;
      valuation: number;
      stale_factor_count: number;
    };
  };
  priority: number | null;
  raw_priority?: number | null;
  state: string;
  signal: string | null;
  constituent_count: number;
  effective_constituent_count?: number;
  crowding_penalty?: number;
  confidence?: "高" | "中" | "低";
  data_quality?: {
    flags: string[];
    configured_count?: number;
    source_count?: number;
    effective_count?: number;
    history_days?: number;
    max_weight_pct?: number;
    top5_weight_pct?: number;
    financial?: number;
    forecast?: number;
    valuation?: number;
    stale_factor_count?: number;
  };
}

export interface PlateScoresData {
  schema_version: number;
  as_of: string;
  generated_at: string;
  is_intraday: boolean;
  quote_time?: string | null;
  financial_period?: string;
  thresholds?: { strong: number; weak: number; opportunity: number };
  board_count: number;
  boards: PlateScoreRow[];
  stale?: boolean;
  refresh_error?: string;
  cache_state?: "fresh" | "stale" | "refreshing" | "error";
  cached_at?: string | null;
  methodology: {
    framework: string;
    strength_weights: Record<string, number>;
    opportunity_weights: Record<string, number>;
    hard_constraints: string[];
    definitions: string[];
    sources: { label: string; url: string | null }[];
  };
}

// ---- 产业链纵深（静态链结构 + 动态财务中位数） ----
export interface ChainCatalogItem {
  key: string;
  sector_key: string;
  label: string;
  length: "长链" | "短链";
  node_count: number;
  company_count: number;
  bottleneck_count: number;
}
export interface ChainCatalogData {
  version: string;
  chains: ChainCatalogItem[];
}
export interface ChainCompany { code: string; name: string }
export interface ChainNode {
  id: string;
  stage: "上游" | "中游" | "下游";
  name: string;
  description: string;
  companies: ChainCompany[];
}
export type ChainLinkKind = "supply" | "cross";
export interface ChainLink {
  from: string;
  to: string;
  kind: ChainLinkKind;
}
export interface ChainEvidence {
  fact: string;
  source: string;
  date: string;
  url?: string;
}
export interface ChainBottleneck {
  node_id: string;
  type: string;
  type_label: string;
  severity: 1 | 2;
  detail: string;
  domestic_share: string;
  signal: string;
  evidence?: ChainEvidence[];
}
export interface ChainTransmissionNote {
  from: string;
  to: string;
  what: string;
  mechanism: string;
  status: "传导中" | "待验证";
  updated_on: string;
  evidence?: ChainEvidence[];
}
export interface ChainTransmission {
  direction: string;
  notes: ChainTransmissionNote[];
  watch_quotes: string[];
}
export interface ChainProfitRow {
  code: string;
  name: string;
  node_id: string;
  node_name: string;
  stage: "上游" | "中游" | "下游";
  period: string | null;
  gross_margin: number | null;
  net_margin: number | null;
  roe: number | null;
  revenue_yoy: number | null;
  stale: boolean;
}
export interface ChainNodeStat {
  node_id: string;
  node_name: string;
  stage: "上游" | "中游" | "下游";
  company_count: number;
  sample_count: number;
  gross_margin: number | null;
  net_margin: number | null;
  roe: number | null;
  revenue_yoy: number | null;
}
export interface ChainSettledNode {
  node_id: string;
  node_name: string;
  stage: string;
  gross_margin: number | null;
  roe: number | null;
  note: string;
}
export interface ChainReportRow {
  title: string;
  date: string;
  org: string;
  industry: string;
  info_code: string;
}
export interface IndustryChainData {
  schema_version: number;
  generated_at: string;
  chain_version: string;
  structure: {
    key: string;
    sector_key: string;
    label: string;
    length: "长链" | "短链";
    summary: string;
    nodes: ChainNode[];
    links: ChainLink[];
    bottlenecks: ChainBottleneck[];
    transmission: ChainTransmission;
  };
  profit: {
    rows: ChainProfitRow[];
    node_stats: ChainNodeStat[];
    settled_node: ChainSettledNode | null;
    periods: string[];
    stale_count: number;
    source: string;
    error?: string;
    stale?: boolean;
    refresh_error?: string;
  };
  reports: {
    count: number;
    rows: ChainReportRow[];
    source: string;
    error?: string;
    stale?: boolean;
    refresh_error?: string;
  };
  cache_state?: "fresh" | "stale" | "refreshing" | "error";
  cached_at?: string | null;
  refresh_error?: string | null;
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


// 资金面（独立页面 · 国内 / 美国，含来源日期与逐项回退状态）
export interface HistPoint { date: string; v: number }
export interface LiquidityUsItem {
  label: string; unit: string; value: number; date: string; chg: number | null; hist: HistPoint[];
  source?: string; frequency?: string; source_date?: string; fetched_at?: string;
  stale?: boolean; fallback_reason?: string;
}
export interface IndexFlow {
  name: string; hist: HistPoint[]; latest: HistPoint;
  source?: string; frequency?: string; source_date?: string; fetched_at?: string;
  stale?: boolean; fallback_reason?: string;
}
export interface LiquidityCn {
  date?: string;
  rzye_yi?: number; rzye_chg_yi?: number | null;
  rzrqye_yi?: number; rzrqye_chg_yi?: number | null;
  rzjme_yi?: number;
  rzrqye_hist?: HistPoint[]; rzjme_hist?: HistPoint[];
  index_flows?: Record<string, IndexFlow>;
  source?: string; frequency?: string; source_date?: string; fetched_at?: string;
  stale?: boolean; fallback_reason?: string;
}
export interface FedOddsStrike { strike: number; prob: number }
export interface FedOdds {
  event: string; meeting: string; likely_upper: string;
  strikes: FedOddsStrike[];
  stale?: boolean;      // true = 数据源暂不可用，展示的是最近一次缓存值
  fetched_at?: string;  // 数据实际获取时间（stale 时用于提示）
  source?: string; frequency?: string; fallback_reason?: string;
}
export interface IndexComponent { label: string; value: string; pct: number; date?: string; hist?: HistPoint[] }
export interface CompositeIndex {
  value: number; label: string; desc: string; date: string;
  kind?: "stress" | "state" | "warning" | "auxiliary";
  favorable?: "high" | "low";
  hist: HistPoint[]; interpretation: string;
  components?: IndexComponent[];  // 子指标：当前值 + 各自分位，点击指数卡展开
  source?: string; frequency?: string; coverage?: number; fetched_at?: string;
  stale?: boolean; fallback_reason?: string;
}
export interface LiquidityStaleSource { label: string; date?: string; fetched_at?: string; reason: string }
// 资金面综合得分：单指数按权重合成的单一 0-100 分（越高越宽松友好）
export interface LiquidityCompositePart {
  name: string;
  weight: number;
  score: number | null;        // 已按方向归一（压力类取 100-分位）
  contribution: number | null; // (分-50)×权重，正=利多
}
export interface LiquidityComposite {
  schema: number;
  label: string;
  score: number;
  state: string; // 偏多/中性偏多/中性/中性偏空/偏空
  coverage: number;
  drivers: string[];
  parts: LiquidityCompositePart[];
  hist?: HistPoint[];
  desc: string;
  stale?: boolean;
  fetched_at?: string;
}
export interface LiquidityData {
  cn: LiquidityCn;
  cn_indices?: Record<string, CompositeIndex>;
  cn_composite?: LiquidityComposite | null;
  us: Record<string, LiquidityUsItem>;
  us_indices?: Record<string, CompositeIndex>;
  us_composite?: LiquidityComposite | null;
  fed_odds?: FedOdds;
  updated: string;
  assembled_at?: string;
  /** 后端源故障回退 last-good 缓存时为 true，stale_since 为缓存生成时间 */
  stale?: boolean;
  stale_since?: string;
  freshness?: { stale: boolean; stale_count: number; stale_sources: LiquidityStaleSource[] };
}

// 宏观面（国内重要宏观经济指标 · GDP/CPI/PPI/PMI/M2/工业增加值/进出口/贸易差额/社融）
export interface MacroIndicator {
  label: string;
  value: number;
  forecast: number | null;
  prev: number | null;
  date: string;
  hist: HistPoint[];
  unit?: string;
  source?: string;
  desc?: string;
  meta?: {
    observation_period: string | null;
    release_at: string | null;
    fetched_at: string | null;
    status: "fresh" | "stale" | "fallback";
    frequency: "daily" | "monthly" | "quarterly";
    quality: "direct" | "derived" | "proxy";
    scope: string | null;
    source_url: string | null;
    owner_module: string | null;
    derived_from: string[];
  };
}
export interface MacroCluster {
  name: string;
  desc: string;
  modules: string[];
}
export interface MacroData {
  cn: Record<string, MacroIndicator>;
  groups: Record<string, string[]>;
  modules?: MacroModule[];
  composite?: MacroComposite | null;
  clusters?: MacroCluster[];
  updated: string;
  stale?: boolean;
  stale_since?: string;
}

// 宏观总分：8 模块按回测权重合成的单一 0-100 分（越高越利多）
export interface MacroCompositePart {
  name: string;
  weight: number;
  direction: "direct" | "inverse"; // inverse=该模块对收益为反向，合成取 100-分
  score: number | null;
  contribution: number | null; // (调整分-50)×权重，正=利多
}
export interface MacroComposite {
  schema: number;
  score: number;
  state: string; // 偏多/中性偏多/中性/中性偏空/偏空
  coverage: number;
  drivers: string[];
  parts: MacroCompositePart[];
  hist?: HistPoint[];
  benchmark?: { label: string; hist: HistPoint[] }; // 回测同源基准（中证全A月末收盘）
  desc: string;
}

// 8 模块得分（方向调整后分位加权 0-100）
export interface MacroModuleUsed {
  key: string;
  direction: "up" | "down";
  weight: number;
  pct: number;
  freshness?: number;
  date?: string;
}
// 景气子模块（V1.0：国内官方/市场化/实际活动/全球）
export interface MacroSubModule {
  name: string;
  weight: number;          // 占景气总分的名义权重（百分点）
  score: number | null;    // null=该子模块整体缺源，未计入总分
  coverage: number;
  confidence: number;
  indicators: string[];
  used: MacroModuleUsed[];
}
export interface MacroModule {
  name: string;
  icon: string;
  desc: string;
  score: number | null;
  coverage: number;
  confidence: number;
  hist?: HistPoint[];      // 最近3年模块得分（按月回放）
  indicators: string[];
  used: MacroModuleUsed[];
  // 景气模块 V1.0 扩展字段
  submodules?: MacroSubModule[];
  state?: string;          // 强扩张/扩张/弱扩张/弱收缩/收缩/深度收缩
  mom?: number | null;     // 景气动量（环比）
  direction?: string;      // 改善/恶化/持平
  quadrant?: string;       // 复苏/扩张/放缓/衰退
}

// 分时图（当日分钟级）
export interface MinutePoint { time: string; price: number; volume: number }
export interface MinuteKline { date: string; prev_close: number; points: MinutePoint[]; last_day?: boolean; market_minutes?: [number, number][] }

export interface SearchResult {
  code: string; name: string; market: string;
}


// ---- 基金模块（公募）----
export interface FundSearchResult { code: string; name: string; type: string }
export interface FundQuote {
  name: string; estimate_pct: number | null; estimate_time: string | null;
  nav: number | null; nav_date: string | null;
  today_return_pct: number | null; today_return_date: string | null;
  yesterday_return_pct: number | null; yesterday_return_date: string | null;
  estimate_source?: string | null; // 'self'=上季重仓推算 'index'=A股指数代理 'global_index'=港/美/韩指数代理
  estimate_stale?: boolean;  // 海外市场闭市：估值为最近一场(隔夜)涨跌幅
  estimate_proxy?: string | null; // 指数代理所用的跟踪标的指数名
}
export interface FundNavRow { date: string; nav: number; acc_nav: number | null; day_pct: number | null }
export interface FundNavHistory { code: string; rows: FundNavRow[]; count: number }
export interface FundMetrics {
  code: string; ann_return: number | null; max_drawdown: number | null;
  volatility: number | null; sharpe: number | null; n: number;
}
export interface FundProfileHolding { stock_code: string; stock_name: string; weight: number | null; quarter: string; change_pct?: number | null }
export interface FundProfile {
  code: string; name: string; type: string;
  holdings: FundProfileHolding[]; holdings_quarter: string | null;
  metrics: FundMetrics | null;
}
export interface FundScreenRow {
  code: string; name: string; date: string; nav: number | null; day_pct: number | null;
  [period: string]: string | number | null; // 近1周/近1月/.../成立来
}
export interface FundScreenData {
  total_all: number; total_matched: number; rows: FundScreenRow[]; sort_by: string;
}
export type PFSTier = "core_buy" | "potential_buy" | "watch" | "review" | "exclude";
export interface PFSRiskMetrics {
  risk_return_peer?: number | null; resilience_peer?: number | null;
  volatility?: number | null; sharpe?: number | null; max_drawdown?: number | null;
}
export interface PFSNavMetrics {
  n?: number; start_date?: string; end_date?: string; ann_return?: number | null;
  volatility?: number | null; sortino?: number | null; max_drawdown?: number | null;
  cvar95?: number | null; recovery_days?: number | null; unrecovered?: boolean;
  rolling_12m_positive_ratio?: number | null; rolling_24m_positive_ratio?: number | null;
  rolling_36m_positive_ratio?: number | null;
}
export interface PFSScaleMetrics {
  date?: string; net_assets?: number | null; ending_shares?: number | null;
  net_share_flow?: number | null; net_share_flow_rate?: number | null;
  redemption_rate?: number | null; aum_growth_1q?: number | null; quarterly_flow_score?: number | null;
}
export interface PFSHolderMetrics {
  date?: string; institution_pct?: number | null; individual_pct?: number | null;
  internal_pct?: number | null; institution_change?: number | null;
}
export interface PFSCandidate {
  code: string; name: string; fund_type: string; strategy: string; data_date: string | null;
  manager: string; manager_count: number; platform: string;
  manager_career_days: number | null; manager_aum: number | null; manager_fund_count: number | null;
  team_start_date: string | null; team_tenure_days: number | null;
  purchase_status: string; redemption_status: string; fee_pct: number | null;
  annual_fee_pct?: number | null; annual_fee_items?: Record<string, number>;
  share_classes?: { code: string; name: string; purchase_fee_pct: number | null }[];
  return_percentiles: Record<string, number | null>;
  quality_score: number; potential_score: number; raw_score: number; confidence: number;
  risk_penalty: number; final_score: number; tier: PFSTier; candidate_type: string;
  quality_components: Record<string, number>; potential_components: Record<string, number>;
  confidence_components: Record<string, number>;
  risk_period: string | null; risk_metrics: PFSRiskMetrics; data_coverage: number;
  nav_metrics: PFSNavMetrics; scale_metrics: PFSScaleMetrics; holder_metrics: PFSHolderMetrics;
  gate_pass: boolean; gate_failures: string[]; review_reasons: string[]; risk_notes: string[];
  why_good: string[]; why_potential: string[]; breaks_thesis: string[]; detail_errors: string[];
  [period: string]: unknown;
}
export interface PFSData {
  schema_version: number; generated_at: string; as_of: string; stale: boolean; refresh_error?: string;
  universe_count: number; candidate_count: number; matched_count: number;
  cache_state?: "fresh" | "stale" | "refreshing" | "error"; cached_at?: string | null;
  tier_counts: Record<PFSTier, number>; rows: PFSCandidate[];
  pit_store?: { path: string; observation_count: number; pit_usable_count: number; historical_publication_dates: string };
  methodology: {
    name: string; formula: string; direct_coverage_pct: number; proxy_coverage_pct: number; missing_coverage_pct: number;
    limitations: string[]; definitions: string[];
    sources: { label: string; scope: string; status: string }[];
  };
}
export interface FundHolding {
  code: string; name: string;
  nav: number; nav_date: string | null;
  estimate_pct: number | null; estimate_time: string | null;
  estimate_source?: string | null;
  estimate_stale?: boolean;
  estimate_proxy?: string | null;
  shares: number; cost: number;
  bought_date: string | null;  // 买入日期（最早一笔）；未填为 null，YTD 按年前买入计
  market_value: number; pnl: number; pnl_pct: number;
  day_pnl: number | null;
  today_return_amount: number | null; today_return_pct: number | null; today_return_date: string | null;
  yesterday_return_amount: number | null; yesterday_return_pct: number | null; yesterday_return_date: string | null;
}
export interface FundClosedPosition {
  code: string; name: string; date: string; nav: number; shares: number; cost: number;
  pnl: number; pnl_pct: number;
  post_close_pct: number | null;  // 卖出后至今涨跌幅；最新净值取不到为 null
}
export interface FundPortfolioData {
  holdings: FundHolding[];
  totals: {
    market_value: number; cost: number; pnl: number; pnl_pct: number; day_estimate_pnl: number | null;
    today_pnl: number | null; today_pnl_pct: number | null;
    yesterday_pnl: number | null; yesterday_pnl_pct: number | null;
  };
  closed: FundClosedPosition[];
  realized_pnl: number;
  ytd_pnl: number | null; ytd_pnl_pct: number | null;  // 本年盈亏（分段口径，同场内证券）
  updated: string; last_refresh: string | null;
}
export interface LegacyPortfolioStatus {
  securities: { available: boolean; target_empty: boolean; holdings: number; closed: number };
  fund: { available: boolean; target_empty: boolean; holdings: number; closed: number };
}

// ---- 黄金多维评分（方案 V2.1）----
export interface GoldIndicator {
  key: string; label: string; dimension: string; weight: number;
  effective_weight?: number; data_status?: "fresh" | "stale";
  value: number | null; value_text: string | null; chg: number | null; date: string | null;
  score: number | null; signal: number | null; hist: HistPoint[]; note: string;
  raw_signal?: number; expected_signal?: number; model_beta?: number;
}
export interface GoldScoreData {
  schema_version: number;
  date: string; gold_score: number | null; signal: string | null; confidence: string;
  hist?: HistPoint[];
  coverage: number;
  mode: string; dimensions: Record<string, { score: number; weight: number; effective_weight?: number; hist?: HistPoint[] }>;
  indicators: GoldIndicator[];
  top_positive_drivers: string[]; top_negative_drivers: string[];
  data_quality: string; updated: string;
  stale?: boolean; stale_since?: string;
  source_status?: Array<{
    key: string; label: string; status: "fresh" | "stale" | "missing";
    fetched_at: string | null; latest_period: string | null;
    stale_reason?: "fallback" | "observation_lag" | null;
    age_days?: number | null; max_age_days?: number | null;
  }>;
}

// 沪金主力（AU0）日K收盘序列：评分卡旁国内金价近1年走势
export interface Au0HistData {
  symbol: string;
  points: HistPoint[];
  fetched_at: string | null;
  stale?: boolean;
}
// 实时金价（伦敦金 XAU / 纽约金 GC，腾讯 hf_ 行情）
export interface GoldSpotQuote {
  name: string;
  price: number;
  change_pct: number | null;
  prev_close: number | null;
  high: number | null;
  low: number | null;
  time: string;
  date: string;
}
export interface GoldSpotData {
  xau: GoldSpotQuote | null;
  gc: GoldSpotQuote | null;
  fetched_at: string | null;
  stale?: boolean;
}

// 国内金价（沪金99 AU9999 / 黄金延期 AUTD，CNY/克）
export interface CnGoldQuote {
  name: string;
  price: number;
  prev_close: number | null;
  change: number | null;
  change_pct: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  time: string;
  date: string;
}
export interface CnGoldSpotData {
  au0: CnGoldQuote | null;
  au9999: CnGoldQuote | null;
  autd: CnGoldQuote | null;
  fetched_at: string | null;
  stale?: boolean;
}

// PAXG-USD 暗盘现货（Binance 公共镜像，7×24）
export interface PaxgSpotData {
  name: string | null;
  price: number | null;
  prev_close: number | null;
  change: number | null;
  change_pct: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  volume: number | null;
  time: string | null;
  date: string | null;
  fetched_at: string | null;
  stale?: boolean;
  usdcny?: number | null;
  // 国内金价近似折算（PAXG USD/盎司 × USDCNY ÷ 31.1035 克）；汇率缺失时为 null
  cny?: {
    price: number | null;
    prev_close: number | null;
    change: number | null;
    open: number | null;
    high: number | null;
    low: number | null;
  } | null;
  minute: {
    date: string;
    prev_close: number;
    points: (MinutePoint & { price: number | null })[];
  } | null;
}

// ---- 油价多维评分（框架 V1.0）----
export interface OilIndicator {
  key: string; label: string; dimension: string; weight: number;
  effective_weight?: number; data_status?: "fresh" | "stale";
  value: number | null; value_text: string | null; chg: number | null; date: string | null;
  score: number | null; signal: number | null; hist: HistPoint[]; note: string;
}
export interface OilDimensionMeta {
  name: string;
  note: string;
}
export interface OilStructure {
  brent_wti: HistPoint[];
  sc_brent_ratio: HistPoint[];
  spr: HistPoint[];
  days_of_supply: HistPoint[];
  usdcny: number | null;
  note: string;
}
export interface OilScoreData {
  schema_version: number;
  date: string; oil_score: number | null; signal: string | null; confidence: string;
  hist?: HistPoint[];
  coverage: number;
  mode: string;
  dimensions: Record<string, { score: number; weight: number; effective_weight?: number; hist?: HistPoint[] }>;
  dimension_order?: OilDimensionMeta[];
  indicators: OilIndicator[];
  top_positive_drivers: string[]; top_negative_drivers: string[];
  structure?: OilStructure;
  data_quality: string; updated: string;
  stale?: boolean;
  source_status?: Array<{
    key: string; label: string; status: "fresh" | "stale" | "missing";
    latest_period: string | null; age_days?: number | null; max_age_days?: number | null;
  }>;
}
// 布伦特连续（OIL）日K收盘序列：评分卡旁油价近1年走势
export interface BrentHistData {
  symbol: string;
  points: HistPoint[];
  fetched_at: string | null;
  stale?: boolean;
}
// 实时油价（腾讯 hf_ 行情）
export interface OilSpotQuote {
  name: string;
  price: number;
  change_pct: number | null;
  prev_close: number | null;
  high: number | null;
  low: number | null;
  time: string;
  date: string;
}
export interface OilSpotData {
  brent: OilSpotQuote | null;
  wti: OilSpotQuote | null;
  ng: OilSpotQuote | null;
  fetched_at: string | null;
  stale?: boolean;
}

// ---- 全球预期概率（Polymarket + Kalshi 双源，globalpercent 移植） ----
export interface PulseMarket {
  question: string | null;
  question_zh: string | null;
  topic: string;
  outcomes: string[];
  prices: (number | null)[];
  prob_yes: number | null;
  pick_label?: string | null;
  change_24h: number | null;
  change_7d: number | null;
  volume_24h: number | null;
  liquidity: number | null;
  end_date: string | null;
  slug: string | null;
  series_ticker?: string | null;
  token_id_yes: string | null;
  source: "polymarket" | "kalshi";
  kalshi_category?: string | null;
}

export interface PulseInsight {
  module: string;
  events: string[];
  impact: string;
}

export interface PulseModule {
  key: string;
  core: boolean;
  market_count: number;
  volume_24h: number;
  source_counts: Partial<Record<"polymarket" | "kalshi", number>>;
  markets: PulseMarket[];
  insight?: PulseInsight | null;
}

export interface PulseOverview {
  as_of: string;
  sources: string[];
  module_order: string[];
  core_modules: string[];
  status?: string | null;
  overall?: string | null;
  modules: PulseModule[];
  updating?: boolean;
  cache_state?: "fresh" | "stale" | "refreshing" | "error";
  cached_at?: string | null;
  data_as_of?: string | null;
  refresh_error?: string | null;
  refresh_attempted_at?: string | null;
}

export interface PulseHistoryPoint {
  t: number | null;
  p: number | null;
}

// —— 债市（中债收益率曲线 + 期限/信用利差）——
export interface BondsCurvePoint { tenor: string; value: number }
export interface BondsCurveData {
  date: string;
  curve: BondsCurvePoint[];
  yields?: Record<string, HistPoint[]>;
  spreads: Record<string, HistPoint[]>;
  credit: Record<string, HistPoint[]>;
  source: string;
  cache_state?: string;
  cached_at?: string | null;
  data_as_of?: string | null;
  refresh_error?: string | null;
}

// —— 债市总览（曲线 / 资金 / 政策锚 / 指数 / 全球对照聚合）——
export interface BondsSeriesPoint { date: string; v: number }
export interface BondsFundingData {
  date: string;
  series: Record<string, BondsSeriesPoint[]>;
  source: string;
}
export interface BondsPolicyAnchor {
  key: string;
  label: string;
  value: number | null;
  chg_bp: number | null;
  date: string;
}
export interface BondsPolicyData {
  date: string;
  anchors: BondsPolicyAnchor[];
  lpr_1y: BondsSeriesPoint[];
  lpr_5y: BondsSeriesPoint[];
  source: string;
}
export interface BondsIndexData {
  date: string;
  series: BondsSeriesPoint[];
  source: string;
}
export interface BondsGlobalPoint { date: string; cn: number; us: number }
export interface BondsGlobalData {
  date: string;
  series: BondsGlobalPoint[];
  spread: BondsSeriesPoint[];
  source: string;
}
// —— 债市计算层：carry / roll / breakeven（曲线推导）——
export interface BondsCalcRow {
  tenor: string;
  years: number;
  yield: number;
  carry_bp_3m: number;
  roll_bp_3m: number;
  total_static_bp_3m: number;
  breakeven_bp_3m: number | null;
}
export interface BondsCalcData {
  date: string;
  funding_cost: number | null;
  horizon_years: number;
  rows: BondsCalcRow[];
  method: string;
  source: string;
}
// —— 仓位与拥挤度：国债期货量仓 ——
export interface BondsPositionContract {
  symbol: string;
  label: string;
  date: string;
  close: number | null;
  oi: number;
  oi_pct_1y: number;
  volume: number | null;
  vol_pct_1y: number | null;
}
export interface BondsPositioningData {
  date: string;
  contracts: BondsPositionContract[];
  source: string;
  method: string;
}
// —— 分品种评分：短债/中短/长债/超长/信用/杠杆套息 ——
export interface BondsSegmentDriver {
  state: string;
  contribution: number;
  weight: number;
}
export interface BondsSegmentRow {
  segment: string;
  score: number;
  drivers: BondsSegmentDriver[];
  hist?: BondsSeriesPoint[];
  invalidation: string;
  anchor_tenor?: string;
  carry_roll_bp_3m?: number;
  breakeven_bp_3m?: number | null;
}
export interface BondsSegmentsData {
  date: string;
  rows: BondsSegmentRow[];
  method: string;
  notes: string[];
  cache_state?: string;
  cached_at?: string | null;
  data_as_of?: string | null;
  refresh_error?: string | null;
}
export interface BondsInsight {
  ranking: string;
  segments: Record<string, string>;
}
export interface BondsOverviewData {
  curve?: BondsCurveData;
  funding?: BondsFundingData;
  policy?: BondsPolicyData;
  index?: BondsIndexData;
  global?: BondsGlobalData;
  calc?: BondsCalcData;
  positioning?: BondsPositioningData;
  cache_state?: "fresh" | "stale" | "refreshing" | "error";
  cached_at?: string | null;
  refresh_error?: string | null;
}

// —— 债市研究框架：八状态仪表盘 ——
export interface BondsFrameworkPart {
  key: string;
  label: string;
  pct: number;
  score: number;
  weight: number;
  hist?: { date: string | null; v: number | null }[];
}
export interface BondsFrameworkState {
  key: string;
  name: string;
  score: number | null;
  meaning: string;
  hist?: BondsSeriesPoint[];
  parts: BondsFrameworkPart[];
}
export interface BondsFrameworkData {
  date: string;
  states: BondsFrameworkState[];
  coverage: number;
  notes: string[];
  method: string;
  cache_state?: string;
  cached_at?: string | null;
  data_as_of?: string | null;
  refresh_error?: string | null;
}

// —— 择时 + 大类资产配置（资管层）——
export interface TimingPart {
  name: string;
  weight: number;
  score: number | null;
  value?: string | null; // 当前读数文本（市场确认子项）
  contribution: number | null;
}
export interface TimingGate {
  rule: string;
  desc: string;
  capped_to?: string;
  raised_to?: string;
}
export interface TimingBlock {
  score: number | null;
  regime: string;
  regime_label: string;
  risk_budget_multiplier: number;
  cash_floor: number;
  recommended_action: string;
  recommended_action_label: string;
  gates: TimingGate[];
  invalidation: string[];
  text: string;
  parts: TimingPart[];
  hist?: HistPoint[];
  drivers?: { name: string; contribution: number }[];
}
export interface MarketConfirmBlock {
  score: number | null;
  parts: TimingPart[];
  hist?: HistPoint[];
  drivers?: { name: string; contribution: number }[];
  desc: string;
  risk_pressure_score?: number | null;
  breadth_score?: number | null;
  trend_score?: number | null;
  crowding_score?: number | null;
}
export interface EvidenceBlock {
  score: number | null;
  state?: string | null;
  date?: string;
  parts?: TimingPart[] | null;
  hist?: HistPoint[];
  source?: string;
}
export interface AssetScoreBlock {
  score: number | null;
  parts: TimingPart[];
  drivers: { name: string; contribution: number }[];
}
export interface AllocationRow {
  asset: string;
  name: string;
  anchor: number;
  target: number;
  last: number | null;
  vs_last: number | null;
  vs_base: number;
  suggestion: string;
  support: string[];
  constraint: string[];
  meaning: string;
}
export interface AllocationBlock {
  regime: string;
  regime_label: string;
  anchor: Record<string, number>;
  base_weights: Record<string, number>;
  target_weights: Record<string, number>;
  last_weights: Record<string, number> | null;
  last_as_of?: string | null;
  regime_changed: boolean;
  rebalance_trigger: boolean;
  rows: AllocationRow[];
  resolved: boolean;
  resolve_note: string;
  asset_scores: Record<string, AssetScoreBlock>;
  correlation: {
    window: string;
    stock_bond_corr_60d: number | null;
    stock_bond_corr_120d: number | null;
    stock_cmd_corr_60d: number | null;
    stock_cmd_basis?: string;
    vols?: Record<string, { vol_20d_ann: number; pct_1y: number | null }>;
  };
  cash_yield_note: string;
}
export interface AllocationInsight {
  macro: string;
  liquidity: string;
  market_confirm: string;
}
export interface GoldInsight {
  opportunity_cost: string;
  flows_positioning: string;
  overall: string;
}
export interface OilInsight {
  scarcity_demand: string;
  pricing_premium: string;
  overall: string;
}
export interface AllocationData {
  schema_version: number;
  model_version: string;
  as_of: string;
  timing: TimingBlock;
  evidence: {
    macro: EvidenceBlock;
    liquidity: EvidenceBlock;
    market_confirm: MarketConfirmBlock;
  };
  allocation: AllocationBlock;
  text: string;
  method: string;
  notes: string[];
  updated: string;
  cache_state?: string;
  cached_at?: string | null;
  data_as_of?: string | null;
  refresh_error?: string | null;
}

// 数据源健康检查：上游探活 + 各页面缓存/快照状态
export interface SourceHealthUpstream {
  key: string; name: string; pages: string;
  status: "ok" | "fail";
  error: string | null;
  latency_ms: number;
  probed_at?: string;
}

export interface SourceHealthDataset {
  key: string; name: string; page: string;
  cached_at: string | null;
  cache_state: "fresh" | "refreshing" | "error";
  refresh_error: string | null;
  detail?: string;
}

export interface SourceHealthData {
  checked_at: string;
  version: string;
  upstreams: SourceHealthUpstream[];
  datasets: SourceHealthDataset[];
  summary: {
    upstream_total: number;
    upstream_failed: string[];
    dataset_error: string[];
    all_ok: boolean;
  };
}

// 事件分析 · 高管/管理层/股东增持
export interface HolderIncreaseRecord {
  person: string;
  identity: string;
  tier: string;
  amount: number;
  shares: number;
  price: number | null;
  activity_date: string;
  notice_date: string;
  start_date: string | null;
  end_date: string | null;
  ratio_pct: number;
  reason: string;
  market: string;
  ongoing: boolean;
  source: string;
}

export interface HolderIncreasePlan {
  title: string;
  notice_date: string;
  amount: number | null;
  amount_label: string;
  deadline: string | null;
  done: boolean;
}

export interface HolderIncreaseRow {
  code: string;
  name: string;
  score: number;
  grade: "strong" | "watch" | "normal";
  breakdown: { identity: number; amount: number; ratio: number; count: number; recency: number };
  tier: string;
  identity: string;
  people: number;
  count: number;
  total_amount: number;
  latest_date: string;
  period: string;
  cumulative: boolean;
  ongoing: boolean;
  plan: HolderIncreasePlan | null;
  records: HolderIncreaseRecord[];
}

export interface HolderIncreaseData {
  window: string;
  updated: string | null;
  source: string;
  total_records: number;
  rows: HolderIncreaseRow[];
  cache_state?: string;
  cached_at?: string | null;
  data_as_of?: string | null;
  refresh_error?: string | null;
}

export type HolderIncreaseWindow = "1d" | "7d" | "30d" | "all";

export const api = {
  health: () => get<{ ok: boolean }>("/health"),
  authConfig: () => get<{ registration_open: boolean }>("/auth/config"),
  sourceHealth: (refresh = false, source?: string) =>
    get<SourceHealthData>(`/source-health${refresh ? "?refresh=true" : ""}${source ? `${refresh ? "&" : "?"}source=${encodeURIComponent(source)}` : ""}`),
  indices: () => get<IndexQuote[]>("/indices"),
  holderIncrease: (window: HolderIncreaseWindow, refresh = false) => {
    const qs = new URLSearchParams({ window });
    if (refresh) qs.set("refresh", "true");
    return get<HolderIncreaseData>(`/event/holder-increase?${qs.toString()}`, SLOW_TIMEOUT_MS);
  },
  marketOverview: () => get<MarketOverview>("/market/overview"),
  liquidity: (refresh = false) => get<LiquidityData>(`/market/liquidity${refresh ? "?refresh=true" : ""}`),
  macro: (refresh = false) => get<MacroData>(`/market/macro${refresh ? "?refresh=true" : ""}`),
  bondsCurve: (refresh = false) => get<BondsCurveData>(`/bonds/curve${refresh ? "?refresh=true" : ""}`),
  bondsOverview: (refresh = false) => get<BondsOverviewData>(`/bonds/overview${refresh ? "?refresh=true" : ""}`),
  allocation: (refresh = false) => get<AllocationData>(`/allocation${refresh ? "?refresh=true" : ""}`),
  allocationInsight: (refresh = false) =>
    get<AllocationInsight | null>(`/allocation/insight${refresh ? "?refresh=true" : ""}`, SLOW_TIMEOUT_MS).then((d) => d ?? null),
  bondsFramework: (refresh = false) => get<BondsFrameworkData>(`/bonds/framework${refresh ? "?refresh=true" : ""}`),
  bondsCalc: (refresh = false) => get<BondsCalcData>(`/bonds/calc${refresh ? "?refresh=true" : ""}`),
  bondsPositioning: (refresh = false) => get<BondsPositioningData>(`/bonds/positioning${refresh ? "?refresh=true" : ""}`),
  bondsSegments: (refresh = false) => get<BondsSegmentsData>(`/bonds/segments${refresh ? "?refresh=true" : ""}`),
  bondsInsight: (refresh = false) =>
    get<BondsInsight | null>(`/bonds/insight${refresh ? "?refresh=true" : ""}`, SLOW_TIMEOUT_MS).then((d) => d ?? null),
  goldScore: (refresh = false) => get<GoldScoreData>(`/gold/score${refresh ? "?refresh=true" : ""}`),
  goldInsight: (refresh = false) =>
    get<GoldInsight | null>(`/gold/insight${refresh ? "?refresh=true" : ""}`, SLOW_TIMEOUT_MS).then((d) => d ?? null),
  au0Hist: (days = 400) => get<Au0HistData>(`/gold/au0-hist?days=${days}`),
  goldSpot: () => get<GoldSpotData>("/gold/spot"),
  cnGoldSpot: () => get<CnGoldSpotData>("/gold/cn-spot"),
  paxgSpot: () => get<PaxgSpotData>("/gold/paxg"),
  oilScore: (refresh = false) => get<OilScoreData>(`/oil/score${refresh ? "?refresh=true" : ""}`),
  oilInsight: (refresh = false) =>
    get<OilInsight | null>(`/oil/insight${refresh ? "?refresh=true" : ""}`, SLOW_TIMEOUT_MS).then((d) => d ?? null),
  oilSpot: () => get<OilSpotData>("/oil/spot"),
  brentHist: (days = 400) => get<BrentHistData>(`/oil/brent-hist?days=${days}`),
  pulseOverview: (refresh = false) => get<PulseOverview>(`/pulse/overview${refresh ? "?refresh=true" : ""}`),
  pulseInsight: (module: string) =>
    get<PulseInsight | null>(`/pulse/insight?module=${encodeURIComponent(module)}`, SLOW_TIMEOUT_MS),
  pulseStatus: () => get<string | null>("/pulse/insight?module=" + encodeURIComponent("现状"), SLOW_TIMEOUT_MS),
  pulseHistory: (tokenId: string, interval = "1w") =>
    get<{ history: PulseHistoryPoint[] }>(`/pulse/history?token_id=${encodeURIComponent(tokenId)}&interval=${interval}`)
      .then((d) => d.history),
  emotion: () => get<ShortTermEmotion>("/market/emotion"),
  turnoverTop: () => get<TurnoverTop>("/market/turnover-top"),
  // 传 keys = 只刷新这几个市场（已开盘的）；不传 = 全量快照
  globalIndices: (keys?: string[]) =>
    get<GlobalIndex[]>(`/global/indices${keys?.length ? `?keys=${encodeURIComponent(keys.join(","))}` : ""}`),
  // 全球指数当日分时（key = 后端 _INDICES 的 key，如 spx/hsi/n225）
  globalMinute: (key: string) => get<MinuteKline>(`/global/minute?key=${encodeURIComponent(key)}`),
  globalStock: (symbol: string, refresh = false) => get<GlobalStock>(`/global/stock?symbol=${encodeURIComponent(symbol)}${refresh ? "&refresh=true" : ""}`),
  hkCashflow: (symbol: string) => get<HkCashflow>(`/global/hk/cashflow?symbol=${encodeURIComponent(symbol)}`),
  radar: () => get<RadarData>("/radar"),
  radarRefresh: () => request<RadarData>("/radar/refresh", "POST", undefined, SLOW_TIMEOUT_MS),
  publicNewsSearch: (q: string, count = 5) =>
    get<PublicNewsSearchData>(`/public-news-search?q=${encodeURIComponent(q)}&count=${count}`),
  portfolio: (fresh = false) => get<PortfolioData>(`/portfolio${fresh ? "?fresh=true" : ""}`),
  portfolioLegacyStatus: () => get<LegacyPortfolioStatus>("/portfolio/legacy-status"),
  importLegacyPortfolio: (kind: "securities" | "fund") =>
    request<PortfolioData | FundPortfolioData>(`/portfolio/import-legacy?kind=${kind}`, "POST"),
  addHolding: (code: string, shares: number, cost: number, boughtDate?: string) =>
    request<PortfolioData>("/portfolio/holding", "POST", { code, shares, cost, ...(boughtDate ? { bought_date: boughtDate } : {}) }),
  removeHolding: (code: string) => request<PortfolioData>(`/portfolio/holding?code=${code}`, "DELETE"),
  updateHolding: (code: string, shares: number, cost: number, boughtDate?: string) =>
    request<PortfolioData>("/portfolio/holding", "PUT", { code, shares, cost, ...(boughtDate ? { bought_date: boughtDate } : {}) }),
  refreshPortfolio: () => request<PortfolioData>("/portfolio/refresh", "POST", undefined, SLOW_TIMEOUT_MS),
  portfolioTiming: () => get<{ signals: Record<string, TimingSignal> }>("/portfolio/timing"),
  closePosition: (code: string, date: string, price: number, shares: number, cost?: number) =>
    request<PortfolioData>("/portfolio/close", "POST", { code, date, price, shares, ...(cost !== undefined ? { cost } : {}) }),
  removeClosed: (index: number) => request<PortfolioData>(`/portfolio/close?index=${index}`, "DELETE"),
  valuation: (code: string, refresh = false) => get<Valuation>(`/valuation?code=${code}${refresh ? "&refresh=true" : ""}`),
  minuteKline: (code: string) => get<MinuteKline>(`/kline/minute?code=${encodeURIComponent(code)}`),
  kline: (code: string, period: KLineData["period"], count = 250, refresh = false) =>
    get<KLineData>(`/kline/chart?code=${encodeURIComponent(code)}&period=${period}&count=${count}${refresh ? "&refresh=true" : ""}`),
  percentile: (code: string, refresh = false) => get<ValPercentile>(`/valuation/percentile?code=${code}${refresh ? "&refresh=true" : ""}`),
  financials: (code: string, refresh = false) => get<Financials>(`/financials?code=${code}${refresh ? "&refresh=true" : ""}`),
  announcements: (code: string, refresh = false) => get<Announcement[]>(`/announcements?code=${code}${refresh ? "&refresh=true" : ""}`),
  quote: (codes: string) => get<Record<string, Quote>>(`/quote?codes=${codes}`),
  reports: (code: string, refresh = false) => get<Report[]>(`/reports?code=${code}${refresh ? "&refresh=true" : ""}`),
  news: (code: string, refresh = false) => get<NewsItem[]>(`/news?code=${code}${refresh ? "&refresh=true" : ""}`),
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
  sectorScoresLevel2Cache: () => get<SwLevel2Data | null>("/sector-scores/level2/cache"),
  sectorScoresLevel2: (refresh = false) =>
    get<SwLevel2Data>(`/sector-scores/level2${refresh ? "?refresh=true" : ""}`),
  plateScoresCache: () => get<PlateScoresData | null>("/plate-scores/cache"),
  plateScores: (refresh = false) =>
    get<PlateScoresData>(`/plate-scores${refresh ? "?refresh=true" : ""}`),
  industryChains: () => get<ChainCatalogData>("/industry-chains"),
  industryChain: (key: string, refresh = false) =>
    get<IndustryChainData>(`/industry-chain/${encodeURIComponent(key)}${refresh ? "?refresh=true" : ""}`),
  search: (q: string) => get<SearchResult[]>(`/search?q=${encodeURIComponent(q)}`),
  myReports: () => get<MyReport[]>("/myreports"),
  uploadReport: (name: string, contentB64: string) =>
    request<MyReport>("/myreports", "POST", { name, content_b64: contentB64 }),
  deleteReport: (id: string) => request<{ ok: boolean }>(`/myreports/${id}`, "DELETE"),
  // ---- 基金模块 ----
  fundSearch: (q: string, limit = 20) =>
    get<FundSearchResult[]>(`/funds/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  fundQuote: (codes: string[]) =>
    get<Record<string, FundQuote>>(`/funds/quote?codes=${encodeURIComponent(codes.join(","))}`),
  fundNav: (code: string, limit = 250) => get<FundNavHistory>(`/funds/nav/${code}?limit=${limit}`),
  fundMetrics: (code: string) => get<FundMetrics>(`/funds/metrics/${code}`),
  fundProfile: (code: string) => get<FundProfile>(`/funds/profile/${code}`),
  fundScreen: (params: { type?: string; r4433?: boolean; sort_by?: string; order?: string;
                         min_y1?: number; min_m6?: number; min_y3?: number; keyword?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params.type) qs.set("type", params.type);
    if (params.r4433) qs.set("r4433", "true");
    if (params.sort_by) qs.set("sort_by", params.sort_by);
    if (params.order) qs.set("order", params.order);
    if (params.min_y1 !== undefined) qs.set("min_y1", String(params.min_y1));
    if (params.min_m6 !== undefined) qs.set("min_m6", String(params.min_m6));
    if (params.min_y3 !== undefined) qs.set("min_y3", String(params.min_y3));
    if (params.keyword) qs.set("keyword", params.keyword);
    if (params.limit) qs.set("limit", String(params.limit));
    return get<FundScreenData>(`/funds/screen?${qs.toString()}`);
  },
  fundPfs: (refresh = false) => get<PFSData>(`/funds/pfs?limit=100${refresh ? "&refresh=true" : ""}`),
  fundPortfolio: (fresh = false) => get<FundPortfolioData>(`/fund-portfolio${fresh ? "?fresh=true" : ""}`),
  addFundHolding: (code: string, shares: number, cost: number, boughtDate?: string) =>
    request<FundPortfolioData>("/fund-portfolio/holding", "POST", { code, shares, cost, ...(boughtDate ? { bought_date: boughtDate } : {}) }),
  removeFundHolding: (code: string) =>
    request<FundPortfolioData>(`/fund-portfolio/holding?code=${code}`, "DELETE"),
  updateFundHolding: (code: string, shares: number, cost: number, boughtDate?: string) =>
    request<FundPortfolioData>("/fund-portfolio/holding", "PUT", { code, shares, cost, ...(boughtDate ? { bought_date: boughtDate } : {}) }),
  closeFundPosition: (code: string, date: string, nav: number, shares: number, cost?: number) =>
    request<FundPortfolioData>("/fund-portfolio/close", "POST", { code, date, nav, shares, ...(cost !== undefined ? { cost } : {}) }),
  removeFundClosed: (index: number) =>
    request<FundPortfolioData>(`/fund-portfolio/close?index=${index}`, "DELETE"),
  // —— 因子实验室 ——
  factorLab: () => get<FactorLabStatus>("/factor/lab"),
  factorBuild: () => request<{ building: boolean; started: boolean }>("/factor/build", "POST"),
  factorEvaluate: (factor: string, start?: string, end?: string) =>
    get<FactorEvaluateData>(`/factor/evaluate?factor=${encodeURIComponent(factor)}${start ? `&start=${start}` : ""}${end ? `&end=${end}` : ""}`, 300_000),
  factorBacktest: (params: { factor: string; top_n?: number; freq?: string; cost?: number; start?: string; end?: string }) => {
    const qs = new URLSearchParams({ factor: params.factor });
    if (params.top_n) qs.set("top_n", String(params.top_n));
    if (params.freq) qs.set("freq", params.freq);
    if (params.cost !== undefined) qs.set("cost", String(params.cost));
    if (params.start) qs.set("start", params.start);
    if (params.end) qs.set("end", params.end);
    return get<FactorBacktestData>(`/factor/backtest?${qs.toString()}`, 300_000);
  },
  factorFields: () => get<FactorFieldsDoc>("/factor/fields"),
  factorValidate: (id: string, name: string, expr: string) =>
    request<{ ok: boolean }>("/factor/validate", "POST", { id, name, expr }),
  factorCustomSave: (id: string, name: string, expr: string) =>
    request<CustomFactor>("/factor/custom", "POST", { id, name, expr }),
  factorCustomDelete: (id: string) =>
    request<{ deleted: string }>(`/factor/custom/${encodeURIComponent(id)}`, "DELETE"),
};

// —— 因子实验室类型 ——
export interface FactorCatalog {
  built_at: string;
  stocks: number;
  rows: number;
  date_min: string;
  date_max: string;
}

export interface FundamentalsStatus {
  has_data: boolean;
  built: {
    rows: number; stocks: number; report_dates: number;
    notice_date_min: string; notice_date_max: string;
  } | null;
  periods_fetched: number;
  building: boolean;
  progress: { done: number; total: number; rows: number; started_at: string | null; done_at: string | null; error: string | null };
  pit_note: string;
  biases: string[];
}

export interface FactorLabStatus {
  factors: { id: string; name: string }[];
  fin_factors?: { id: string; name: string }[];
  has_data: boolean;
  catalog: FactorCatalog | null;
  biases: string[];
  building: boolean;
  progress: { fetched: number; total: number; failed: number; started_at: string | null; done_at: string | null; error: string | null };
  fundamentals?: FundamentalsStatus;
}

export interface FactorEvaluateData {
  factor: string;
  factor_name: string;
  start: string;
  end: string;
  n_days: number;
  avg_coverage: number;
  ic: { ic_mean: number; rank_ic_mean: number; rank_ic_ir: number | null; rank_ic_positive_ratio: number; ic_std: number };
  ic_series: { dates: string[]; values: number[] };
  ic_decay: Record<string, { rank_ic_mean: number; rank_ic_ir: number | null }>;
  quantile_returns: Record<string, number | null>;
  quantile_turnover: Record<string, number | null>;
  long_excess_bp: number;
  long_short_bp: number;
  rank_autocorr: number | null;
  by_year: Record<string, { rank_ic_mean: number; rank_ic_ir: number | null; days: number }>;
  biases: string[];
  timing_note: string;
}

export interface FactorBacktestMetrics {
  total_return: number;
  ann_return: number;
  ann_vol: number | null;
  sharpe: number | null;
  max_drawdown: number;
  win_rate: number | null;
  ann_turnover: number;
  n_days: number;
}

export interface FactorBacktestData {
  factor: string;
  factor_name: string;
  params: { start: string; end: string; top_n: number; top_pct: number | null; freq: string; cost_multiplier: number };
  metrics: FactorBacktestMetrics;
  cost_stress: Record<string, FactorBacktestMetrics>;
  nav: { dates: string[]; strategy: number[]; benchmark: number[] };
  yearly_returns: Record<string, number>;
  biases: string[];
  timing_note: string;
}

export interface CustomFactor {
  id: string;
  name: string;
  expr: string;
  created_at?: string;
}

export interface FactorFieldsDoc {
  fields: { name: string; desc: string }[];
  ops: { name: string; desc: string }[];
  examples: { expr: string; desc: string }[];
  custom: CustomFactor[];
}
