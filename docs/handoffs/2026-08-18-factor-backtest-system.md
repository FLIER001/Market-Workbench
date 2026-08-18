# Market Workbench 因子与回测系统：会话交接

## 用户目标

在 `/Users/k/Vibe-Research` 的 Market Workbench 基础上设计并后续落地一个因子研究与回测系统。用户特别要求借鉴优秀开源项目，“站在巨人的肩膀上”，不能闭门自研。

本轮只完成只读审计、开源调研和方案设计，未修改仓库文件。

## 当前建议结论

系统定位为“研究型因子实验室”，默认首期范围：

- A 股；
- 日频；
- 横截面选股因子；
- 先做价量因子与单因子检验；
- 第二阶段做只多、等权、周/月调仓的组合回测；
- 不做自动交易、今日股票排名、任意 Python 策略或通用策略 DSL。

不要整体嵌入一个重型量化平台。建议采用组合式方案：

1. Qlib 的数据集、信号、组合、执行和实验记录分层；
2. Alphalens 的 Rank IC、分组收益、换手、秩自相关和衰减口径；
3. RQAlpha/LEAN 的订单、撮合、交易约束和公司行为真实性清单；
4. A Share Quant Research Workspace 的本地研究工件、Parquet 目录和统一 score 契约；
5. DuckDB + Parquet 作为大规模历史数据层；
6. exchange_calendars 直接提供 XSHG 交易日历。

## 当前项目事实

- 架构：React 19 + TypeScript + Vite 前端，FastAPI 后端，本地自托管。见 `/Users/k/Vibe-Research/docs/architecture.md`。
- 后端入口：`/Users/k/Vibe-Research/backend/app.py`。
- 当前数据层：`backend/astock.py` 提供行情、K 线、财务、公告等；大量模块使用 JSON/SQLite/内存 last-good 缓存。
- 现有评分：`backend/plate_scores.py`、`sector_scores.py`、`sw_level2_scores.py`、`market.py`。
- 现有专用回测：`/Users/k/Vibe-Research/research/A股宏观面总分模块_回测与权重设计.md` 和 `research/output/macro_backtest/`，但不是通用引擎。
- 当前路线图在 `/Users/k/Vibe-Research/ROADMAP.md:88` 明确写着不做通用回测平台/量化因子工厂。新功能落地前必须先确认并修改这一产品边界。
- 本地 Python 环境已验证：Pandas `3.0.5`、NumPy `2.5.1`；未安装 DuckDB、PyArrow、SciPy。
- Git 状态（2026-08-17）：`main...upstream/main [ahead 29, behind 25]`，仅有未跟踪 `.zcode/`。不要擅自处理该目录或同步分支。

## 数据真实性边界

当前页面 K 线主要是腾讯前复权数据，适合展示，不足以单独支撑严谨回测。严谨版本必须具有：

- point-in-time 股票池；
- 上市日、退市日；
- 历史 ST、停牌、涨跌停状态；
- 不复权成交价和复权因子；
- 公司行为；
- 财务数据实际公告可得日及修订版本；
- 历史行业分类或明确标注静态分类偏差；
- 每次运行固定数据版本、配置版本、因子版本和代码 SHA。

没有上述字段时，可以提供“探索性回测”，但必须显示 `survivorship_bias`、缺失字段和覆盖率，不能标为研究级结果。

## 开源项目调研结果

### 建议重点采用

- Microsoft Qlib，MIT  
  https://github.com/microsoft/qlib  
  借鉴工作流、Alpha158、score 到策略/执行的分层和 recorder。不要整包依赖：其运行依赖含 MLflow、Redis、MongoDB、CVXPY、Jupyter、PyArrow 等，和当前轻量项目不匹配。

- Alphalens Reloaded，Apache-2.0  
  https://github.com/stefan-jansen/alphalens-reloaded  
  借鉴 `factor_information_coefficient`、`mean_return_by_quantile`、`quantile_turnover`、`factor_rank_autocorrelation` 等输入输出口径。不要直接安装：其 `pyproject.toml` 要求 `pandas<3.0`，与当前 Pandas 3.0.5 冲突。建议用现有 Pandas/NumPy实现薄兼容层，并用其公开测试样本做 golden test。

- A Share Quant Research Workspace，MIT  
  https://github.com/cyecho-io/ashare-lowfreq-research  
  借鉴 `catalog.json`、Parquet 数据目录、`scores.parquet` 上下游契约、运行工件与 Web-first 工作流。不要盲抄：其 `is_st`、行业字段存在静态化问题，税费和换手指标也需按本项目口径重做。

- exchange_calendars，Apache-2.0  
  https://github.com/gerrymanoim/exchange_calendars  
  可直接依赖，仓库含 `exchange_calendar_xshg.py` 和对应测试。仍需用实际行情交易日做一致性核验。

- DuckDB，MIT  
  https://github.com/duckdb/duckdb  
  可直接依赖，用于查询和生成分区 Parquet；SQLite继续只管用户、任务和运行元数据。

### 只借鉴、不使用代码

- RQAlpha  
  https://github.com/ricequant/rqalpha  
  A股撮合和税费设计很有参考价值，但许可证限制商业用途，且完整 point-in-time 数据依赖 RQData。不能把其代码作为本项目基础。

- QuantConnect LEAN，Apache-2.0  
  https://github.com/QuantConnect/Lean  
  借鉴订单生命周期、公司行为、退市、数据归一化和回放语义；不嵌入大型 C# 引擎。

- vectorbt  
  https://github.com/polakowo/vectorbt  
  借鉴矩阵化参数扫描。当前为 Apache 2.0 + Commons Clause 的 fair-code 许可，且依赖 NumPy/Numba/SciPy/Plotly/Widgets 等，不建议直接依赖。

- Zipline Reloaded / Backtrader  
  前者重型且要求 `pandas<3.0`，后者 GPL-3.0；均不适合直接嵌入当前 MIT 项目。

## 建议技术结构

### 存储

```text
~/.vibe-research/factor-data/
├── catalog.json
├── bars/year=YYYY/*.parquet
├── instruments/*.parquet
├── calendars/*.parquet
├── universes/*.parquet
├── fundamentals/year=YYYY/*.parquet
├── factors/<factor_id>/<factor_version>/*.parquet
└── runs/<run_id>/
```

- DuckDB：大表导入、质量检查、窗口和横截面查询；
- Parquet：行情、主数据、股票池、因子面板、曲线和结果；
- SQLite：用户隔离、任务、配置、状态和结果索引；
- JSON：小型 catalog、数据来源和 schema 清单。

### 统一 score 契约

```text
trade_date
instrument
factor_id
factor_version
score
direction
available_at
universe_id
data_version
```

所有原生因子、现有板块评分和未来可选 Qlib 模型都输出这套契约。回测引擎只消费 score，不依赖信号来源。

### 因子检验

- IC、Rank IC、ICIR、正 IC 比例；
- 1/5/10/20 日前瞻收益和衰减；
- 五分组收益及单调性；
- 多头组相对基准；
- 分组换手；
- 因子秩自相关；
- 按行业、年份、市场状态分组；
- 覆盖率、缺失率和样本数；
- 滚动样本外和块自助置信区间。

首期价量因子建议：跳过最近 5 日的 20/60/120 日动量、5 日反转、20/60 日波动、下行波动、Amihud 非流动性、成交额/换手稳定性、趋势路径质量。

### 组合回测

事件链：

```text
T 日数据可得
→ T 日收盘后产生信号
→ 目标权重
→ T+1 订单
→ 检查停牌/涨跌停/ST/上市天数
→ 撮合
→ 扣佣金/经手费/印花税/滑点
→ 更新现金、持仓、净值和归因
```

首期：只多、等权、Top 20% 或 Top N、周/月调仓；输出无成本和 1x/2x/3x 成本压力测试。

### 依赖策略

第一批建议仅新增：

```text
duckdb
exchange-calendars
```

不新增 Qlib、Alphalens、Zipline、vectorbt、QuantStats、MLflow、Redis、Celery、Plotly。

如复用任何 MIT/Apache 源码，增加 `THIRD_PARTY.md`，记录仓库、固定 commit SHA、许可证、具体复用文件/函数、修改说明，并保留 NOTICE/版权要求。不得复制 RQAlpha、vectorbt、Backtrader 的受限代码。

## 建议实施顺序

1. 产品边界确认：用户确认“研究型因子实验室”，并同意调整 ROADMAP。
2. 开源基准原型：建立 `THIRD_PARTY.md` 草案、固定 upstream SHA、准备 Alphalens golden fixtures。
3. DuckDB + 分区 Parquet 小型性能原型，先用可公开取得的小样本验证。
4. 数据质量与 point-in-time 审计；输出可得、代理、缺失字段清单，不静默替代。
5. Alphalens 兼容单因子检验。
6. Qlib 风格统一 score 契约和实验 lineage。
7. A股事件式组合回测。
8. 前端新增“因子研究”页面，使用现有 FastAPI、React、ECharts。
9. 用现有板块/行业评分作为第一批真实验证对象，补滚动样本外 Rank IC、分组收益、回撤和换手。
10. 稳定后再提供可选 Qlib score 适配，不把 Qlib变成核心运行依赖。

## 未决问题

下一会话应先向用户确认以下至少一项，不要直接大规模实现：

1. 首期对象是否确定为“A股日频横截面选股因子”，还是 ETF/行业轮动；
2. 数据源选择：纯公开数据、Tushare Pro、恒生聚源、Wind 或其他已有授权源；
3. 是否允许“探索性回测”在点时字段不完整时运行，还是数据不达标就完全阻止；
4. 用户是否接受新增 DuckDB 与 exchange_calendars 两个运行依赖；
5. 是否先做数据/性能 PoC，再进入正式页面开发。

## Suggested skills

- `ponytail`：任何实现或架构修改都应使用；控制依赖、避免把项目改造成重型平台。
- `agent-reach`：需要刷新 GitHub 项目、许可证、版本或源码时使用；当前 GitHub 后端为 `gh CLI`。
- `data-analytics:analyze-data-quality`：做 point-in-time 数据覆盖、来源冲突、缺失和代理边界审计时使用。
- `data-analytics:validate-data`：核对 IC、分组收益、成本、回撤和样本外结论是否可发布时使用。
- `data-analytics:visualize-data`：设计或 QA 因子 IC、分层收益、净值和回撤图时使用。
- `spreadsheets:Spreadsheets`：若用户要求输出数据可得性清单或开源项目比较表，默认交付 Excel，而非 CSV。

## 交接注意

- 用户偏好先从本地真实代码和数据出发，严格区分真实数据、代理和缺失。
- 复杂架构变更应先只读审计，再逐批确认；不要一次性广泛改动。
- 若后续用户要求“推送”，需遵守项目发布纪律：版本、CHANGELOG、README、README_en 同步，提交并推送后核对远端 SHA。
- 不处理当前未跟踪 `.zcode/`，不覆盖用户文件，不执行破坏性 Git 操作。
