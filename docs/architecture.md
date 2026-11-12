# 架构说明

Market Workbench 由一个 React 前端和一个 FastAPI 后端组成。前端负责界面、会话状态和 AI 配置入口；后端负责数据访问、用户本地存储、文件归档、AI 请求转发和 MCP 工具。

```text
Browser
  │  /api
  ▼
React + Vite (5899) ── development proxy ──► FastAPI (8900)
                                                   │
                 ┌─────────────────────────────────┼────────────────────────────┐
                 ▼                                 ▼                            ▼
          Public market data                 ~/.market-workbench/            AI providers / CLI
       quotes, filings, news, funds       accounts, portfolio, reports    API or local runtime
```

## 前端

`frontend/` 使用 React、TypeScript、Vite 和 Tailwind。路由覆盖市场全景、宏观、资金面、资讯、板块、黄金、自选、持仓、基金、研究和设置。开发服务器运行在 `5899` 端口，并将 API 请求代理至后端。

## 后端

`backend/app.py` 是 HTTP 入口。数据能力按主题拆分：

- `astock.py`、`gstock.py`：A 股及全球市场数据；
- `market.py`、`macro_fetch.py`：市场、资金面和宏观数据；
- `sector_scores.py`、`plate_scores.py`、`sw_level2_scores.py`：行业和主题板块观察；
- `fund.py`、`fund_portfolio.py`、`gold_score.py`：基金和黄金模块；
- `portfolio.py`、`myreports.py`、`users.py`：本地用户数据、持仓和研报；
- `chat.py`、`reflection.py`、`tools.py`、`mcp_server.py`：AI 请求、推理审计和 MCP 工具。

`backend/version.py` 从 `frontend/package.json` 读取版本号，避免各入口出现不同版本。

## 因子研究接口契约

既有 `/api/factor/*` 接口保持兼容。新增的 `/api/factor/research/*` 需要已登录的 user-token，统一返回 `{data: ...}`，记录按账户隔离。实现分布在 `factor_lab_routes.py`（路由）、`factor_lab.py`（持久化与任务）、`factor_lab_worker.py`（执行）、`factor_analysis.py`（纯函数计算）、`factor_data.py` 与 `factor_pit.py`（数据与点位）、`factor_expr.py`（表达式沙箱）。

### 数据结构

```ts
type Phase = 'development' | 'validation' | 'holdout';
type Definition = { factor?: string; expr?: string; direction: 'high' | 'low' };
type Period = {start: string; end: string};
type StudyConfig = {
  horizon: 1 | 5 | 10 | 20;
  min_days_listed: number;
  development: Period; validation: Period; holdout: Period;
};
type InputManifest = {id: string; data_version: string; files: Record<string, unknown>; code: Record<string, string>};
type Freeze = {id: string; run_ids: string[]; baseline_id: string; created_at: number};
type Decision = {decision: '继续验证' | '保留候选' | '暂缓' | '淘汰'; note: string; run_ids: string[]; created_at: number};
type Study = {
  id: string; name: string; description: string; config: StudyConfig;
  input_manifest: InputManifest; baseline_id: string | null; archived: boolean;
  freezes: Freeze[]; holdout_used_at: number | null; decisions: Decision[];
  created_at: number; updated_at: number;
};
type Run = {
  id: string; study_id: string; kind: 'evaluate' | 'compare' | 'backtest';
  name: string; definition: Definition | null; phase: Phase;
  source_run_ids: string[]; params: Record<string, unknown>;
  input_manifest: InputManifest;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  error: string | null; result: unknown | null;
  created_at: number; started_at: number | null; finished_at: number | null;
};
```

因子定义中内置的 `factor` 与沙箱 `expr` 只能二选一，新的执行路径不再使用临时共享因子保存。`direction` 在统计和组合排序之前只应用一次。原始定义和研究配置不可覆盖。配置里的日期是 ISO 日期，三段区间有序且不重叠。建研究可以先于数据集构建，执行时必须校验输入可用性与版本。导出元数据里的输入路径是逻辑标签，不是本机绝对路径。

### 路由

- `GET /api/factor/research` → `Study[]`
- `POST /api/factor/research`，body `{name, description?, config}` → `Study`，同时捕获当前输入清单
- `GET /api/factor/research/{id}` → `{study: Study, runs: Run[]}`，含全部任务类型，按账户隔离
- `PUT /api/factor/research/{id}`，body `{name?, description?, archived?, baseline_id?}` → `Study`，不改配置；基线必须是本研究中已完成的发展期 evaluate。
- `POST /api/factor/research/{id}/runs`，body `{name, definition?, phase?, source_run_id?}` → 排队中的 `Run`。发展期必须带定义；验证期与留出期复用冻结选择中的已完成发展期结果，客户端不能改它的定义或配置。请求校验通过后使用留出期会永久写入 `holdout_used_at`，界面须二次确认并标注「留出期已使用」。
- `GET /api/factor/research/{id}/runs/{run_id}` → `Run`
- `POST /api/factor/research/{id}/runs/{run_id}/cancel` → `Run`；取消后不得再被完成状态覆盖。
- `POST /api/factor/research/{id}/compare`，body `{run_ids: string[]}` → 排队中的比较 `Run`。取 2—4 个互不相同的已完成 evaluate，须同研究、同配置、同阶段、同输入清单；公共样本必须重算，不能拼接各自的汇总数字。
- `POST /api/factor/research/{id}/backtest`，body `{run_id, top_n?, freq?, cost?}` → 排队中的回测 `Run`。复用已存的因子定义、方向、阶段区间和研究输入，不写共享的历史产物。
- `POST /api/factor/research/{id}/freeze`，body `{run_ids: string[]}` → `Study`。为 1—4 个已完成的发展期 evaluate 追加不可变的冻结记录，必要时把研究基线一并加入。
- `POST /api/factor/research/{id}/decisions`，body `{decision, note, run_ids?: string[]}` → `Study`。备注不能为空；追加而不覆盖；须校验引用存在。
- `GET /api/factor/research/{id}/export` → `{format: 'market-workbench-factor-study', version: 1, study, runs}`，不含 auth token，也不含其他账户的数据。

失败返回：配置非法或比较不受支持 400，缺少会话 401，记录不可访问 404，冲突操作 409。给用户看的消息短而具体。显式失败的任务记录保留可见，重试新建一个 run。

### 计算模块 backend/factor_analysis.py

以下纯函数由持久化任务和测试共用：

- `input_manifest() -> dict`：输入文件指纹（bars/catalog/instruments/calendar/fundamentals）与计算代码指纹，执行前后各校验一次。大文件按 stat 缓存增量哈希，不要为了求哈希整份读入。
- `validate_definition(definition: dict) -> dict`：校验内置因子或编译表达式，返回归一化后的可序列化定义。
- `validate_config(config: dict) -> dict`：归一化主预测期、上市天数过滤与有序三段区间。不允许未来函数或编造股票池。
- `execute(definitions: list[dict], config: dict, phase: str, expected_manifest: dict, cancelled=None) -> dict`：面板只加载一次，保留历史预热，把结束交易日超出阶段末的标签掩掉。返回每个定义的原生统计，以及 1/5/10/20 四个预测期重算的公共样本统计。`cancelled` 是可选回调，在工作单元之间检查；取消时抛出异常，绝不返回一份成功证据。
- `backtest(definition: dict, config: dict, phase: str, params: dict, expected_manifest: dict, cancelled=None) -> dict`：复用既有组合模拟器，用快照值、安全时点，不落全局产物。

执行结果按传入定义的顺序排列：

```ts
type HorizonStats = {
  n_days: number; avg_coverage: number;
  sample: {input_rows:number; eligible_rows:number; factor_rows:number; paired_rows:number; used_rows:number; loss_ratio:number|null; skipped_days:number};
  ic: {ic_mean:number|null; rank_ic_mean:number|null; rank_ic_ir:number|null; rank_ic_positive_ratio:number|null};
  ic_series: {dates:string[]; values:number[]};
  quantile_returns: Record<string, number|null>;
  quantile_turnover: Record<string, number|null>;
  long_short_bp: number|null; rank_autocorr:number|null;
  by_year: Record<string, {rank_ic_mean:number|null; rank_ic_ir:number|null; days:number}>;
};
type AnalysisResult = {
  phase: Phase; start:string; end:string; input_manifest:InputManifest;
  horizons: number[];
  native: Array<{definition:Definition; by_horizon:Record<string,HorizonStats>}>;
  common: {by_horizon:Record<string,{results:HorizonStats[]; paired_rows:number}>} | null;
  correlation: {matrix:Array<Array<number|null>>; n_days:number; sample_rows:number} | null;
  biases:string[]; timing_note:string;
};
```

单个 evaluate 存的结果 `native.length===1`；比较存 `native.length===N` 加上 `common`，相关性按因子值、股票、日期三者的实际共同样本逐日重算横截面秩相关。因子值相关不等于 IC 序列相关。没有有效证据时显示为空值，不填 0。上市天数代理与财务数据局限仍须披露，不输出自动的「有效因子」评分。

### 持久化与任务

复用既有的用户 SQLite 连接和库，新增按账户隔离的研究与任务表；大块分析结果不要放进无关页面也会取的通用用户数据里。不引入新依赖。全市场计算单通道串行，限制内存峰值。若取消、重启与测试能干净实现，优先用独立子进程计算（代码库已有这种模式）；绝不并发加载多份全市场面板。服务重启后遗留的排队中／运行中任务转为显式失败，保留原始配置。导入模块时不跑计算。

### 前端职责

`frontend/src/lib/factorLab.ts`：上述类型的 HTTP 客户端，复用既有 `authHeaders` 与 401 处理，除后续统一受理外不改动通用 `api.ts`。

`frontend/src/pages/Factors.tsx`：真实的研究列表与配置新建／复制，按内置因子、表达式或历史自定义快照创建实验，从服务端轮询任务，分期明细与比较／相关性，冻结、验证／留出期与决策、导出，以及按需查看数据状态与构建。不出现演示数值，标签简短。编辑新 run 时已完成的结果继续可见，完整计算留在服务端完成。使用系统原生对话框与既有图表组件。前端因子测试跟着新页面与真实接口走，不再对着旧的本地主线程处理名。

## 数据与状态

公开市场数据按来源和接口能力获取。接口可能使用缓存、最后一次可用快照或降级路径；页面应同时呈现更新时间和可用状态，而不是把缓存当成实时结论。

用户账户数据保存在本机 SQLite；持仓、已清仓记录和研报文件保存在用户数据目录。项目本身不提供远程数据托管服务。

## AI 接口

网页可使用本机 CLI 或 OpenAI 兼容 API；MCP server 供本地 Agent 调用数据工具。系统只传递数据和上下文，不内置股票推荐、目标价或交易指令。
