<p align="center"><b>简体中文</b> | <a href="README_en.md">English</a></p>

# Market Workbench

[![Version](https://img.shields.io/badge/version-1.8.0-1f6feb)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61dafb?logo=react&logoColor=white)](https://react.dev/)
[![License](https://img.shields.io/badge/license-MIT-2ea44f)](LICENSE)

本地自托管的 A 股市场研究工作台，兼顾港美市场。行情、财务、资金面、宏观、板块、债市、黄金、油价、因子检验与公开资讯聚合于单一界面；数据留在本机，AI 连接由你提供，产品不输出荐股或交易指令。

**为什么选它**

- **全本地**：FastAPI + React，账号、持仓、研报存于本机 `~/.vibe-research/`，无云端托管、无遥测。
- **数据零门槛**：东方财富、腾讯、新浪、AkShare、FRED、EIA、IMF、Polymarket/Kalshi 等公开接口，绝大多数无需 API key；核心依赖轻量秒装，重型数据源可选（未安装时对应端点返回 501 并附安装提示，不影响其余功能）。
- **AI 自带（BYO）**：接入你自己的 OpenAI 兼容 API 或本机 CLI（Claude Code / Codex / Qwen / Gemini / DeepSeek）；密钥只存浏览器本地。
- **Agent 友好**：内置零第三方依赖的 MCP Server，向本地 Agent 暴露 41 个数据工具（行情、估值、财务、资金流、宏观/流动性合成分、行业链、债市、因子回测等）。
- **研究边界清晰**：每个指标标注数据来源与更新时间；不给个股推荐、目标价或择时指令。

> 本项目基于 [simonlin1212/Vibe-Research](https://github.com/simonlin1212/Vibe-Research) 二次开发，保留其公开数据与可插拔 AI 接入基础，主要增加宏观与资金面、债市/黄金/油价、择时配置、因子研究、行业产业链、基金工作流、账号隔离数据同步及数据来源时效展示。

![市场全景](docs/screenshots/daily-review.png)

<details>
<summary>更多页面截图</summary>

| 宏观面 | 因子研究 |
|---|---|
| ![宏观面](docs/screenshots/macro.png) | ![因子研究](docs/screenshots/factors.png) |

| 债市 | 择时配置 |
|---|---|
| ![债市](docs/screenshots/bonds.png) | ![择时配置](docs/screenshots/allocation.png) |

| 黄金 | 油价 |
|---|---|
| ![黄金](docs/screenshots/gold.png) | ![油价](docs/screenshots/oil.png) |

</details>

## 目录

- [功能总览](#功能总览)
- [架构](#架构)
- [快速开始](#快速开始)
- [接入 AI 与 Agent](#接入-ai-与-agent)
- [配置与数据](#配置与数据)
- [开发](#开发)
- [文档](#文档)
- [边界与免责声明](#边界与免责声明)

## 功能总览

| 模块 | 内容 |
|---|---|
| 市场全景 | A 股指数、全球市场、市场情绪、成交额、板块资金与每日复盘 |
| 全球预期 | Polymarket 与 Kalshi 公开概率数据、来源、刷新时间、历史走势与 AI 研判卡 |
| 宏观面 | 宏观指标、模块评分与总分卡（回测权重）、数据来源与更新时间 |
| 资金面 | 中美流动性综合得分、流动性信号与市场资金指标 |
| 择时配置 | 宏观 × 流动性 × 市场确认合成择时分（5 档风险等级 + 风险预算倍率 + 现金底仓），输出股/债/商品/现金目标权重与调仓建议 |
| 个股数据 | A 股、港股、美股行情；K 线、估值、财务、公告、研报与资金面 |
| 自选 / 持仓 | 股票与 ETF 分组自选、实时行情；股票及基金持仓、已清仓记录与收益跟踪 |
| 行业研究 | 产业链纵深（环节图谱 / 利润分布 / 瓶颈传导）、申万行业与主题板块评分 |
| 债市 | 收益率曲线与期限/信用利差、Shibor、LPR、中美利差、八状态框架与分品种评分 |
| 黄金 / 油价 | 黄金多维评分、PAXG 国内折算；油价 5 维 8 指标评分（EIA/CFTC/GPR）与裂解价差代理 |
| 标的筛选 | Manager-First 基金筛选；自然语言个股筛选（AI 逐项核验，不内置候选池） |
| 因子研究 | 价量因子检验（RankIC / 五分组 / 换手，Alphalens 口径）、公式引擎自建因子（价量 25 字段 + PIT 财务字段，公告日对齐）与探索性组合回测 |
| 资讯 / 研究 | 公开资讯聚合与公告跟踪；个人研报归档、研究笔记与反思审计 |

## 架构

```text
Browser ──/api──► React + Vite (5899) ──► FastAPI (8900) ──┬─ 公开市场数据源（行情/财务/宏观/资讯）
                                                            ├─ ~/.vibe-research/（账号 SQLite、持仓、研报）
                                                            └─ AI：用户自配 API 或本机 CLI
```

- **前端**：React 19、TypeScript、Vite、Tailwind、ECharts
- **后端**：FastAPI、SQLite（用户/会话）、按主题拆分的数据模块（`backend/` 下 30+ Python 模块）
- **MCP Server**：纯标准库 JSON-RPC over stdio，与 HTTP API 共用同一数据工具层

详见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

要求：Python 3.10+、Node.js 20+。

```bash
git clone https://github.com/FLIER001/Market-Workbench.git
cd Market-Workbench
```

```bash
# 终端一：后端（http://127.0.0.1:8900）
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8900
```

```bash
# 终端二：前端（http://127.0.0.1:5899）
cd frontend
npm install
npm run dev
```

浏览器打开 <http://127.0.0.1:5899>。首次启动（用户库为空）可在网页注册主账号；此后注册默认关闭，账号由后台脚本管理：

```bash
cd backend && .venv/bin/python add_user.py add <用户名>   # 另有 list / passwd / remove
```

验证服务：`curl -fsS http://127.0.0.1:8900/api/health`。常见问题（后端连不上、数据缺失、升级后数据位置）见 [docs/getting-started.md](docs/getting-started.md)。

## 接入 AI 与 Agent

1. **网页端**：在「接入 AI」页选择本机 CLI，或填入 OpenAI 兼容 API 地址、模型名与 key。配置只存浏览器本地，仅调用时提交给本机后端。
2. **Agent 端**：注册 MCP Server（与 HTTP API 同源同版本，本地无需 key）：

```bash
cd backend
claude mcp add market-workbench -- "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py"
```

HTTP API 分组与全部 MCP 工具清单见 [backend/README.md](backend/README.md)。

## 配置与数据

本地开发零配置：前端默认将 `/api` 代理至 `http://127.0.0.1:8900`。对外部署时须设置安全项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `VR_ALLOW_ORIGINS` | `*` | CORS 白名单；公网部署收紧为前端域名 |
| `VR_API_KEY` | 空 | 设置后所有 `/api/*`（健康检查除外）要求 Bearer 鉴权 |
| `VR_ALLOW_REGISTRATION` | 关 | 置 1 重开网页注册 |
| `VR_DATA_DIR` / `VR_REPORTS_DIR` | `~/.vibe-research/` | 账号/持仓/研报存储位置 |
| `VR_DATA_PROXY` | 关 | 仅当机器必须走系统代理出网时置 1 |

完整变量（含 `VR_EIA_API_KEY`、深度分析模型覆盖等）见 [docs/configuration.md](docs/configuration.md)，示例见 [backend/.env.example](backend/.env.example)。

## 开发

```bash
# 后端测试（pytest）
cd backend && .venv/bin/python -m pytest tests -q

# 前端测试与构建（node --test / tsc + vite）
cd frontend && npm test && npm run build
```

版本号唯一来源为 `frontend/package.json`，后端 API、MCP Server 与界面均从它读取。变更记录见 [CHANGELOG.md](CHANGELOG.md)，后续计划见 [ROADMAP.md](ROADMAP.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | 本地运行、验证与常见问题 |
| [docs/configuration.md](docs/configuration.md) | 环境变量、数据目录与公网部署 |
| [docs/architecture.md](docs/architecture.md) | 前后端结构、数据源与 AI 接口 |
| [backend/README.md](backend/README.md) | HTTP API 分组与 MCP 工具清单 |
| [CHANGELOG.md](CHANGELOG.md) · [ROADMAP.md](ROADMAP.md) | 版本历史与路线图 |

## 边界与免责声明

Market Workbench 只整理公开数据与用户自行录入的信息。指标、榜单与 AI 输出均为研究参考，不构成投资建议；项目不提供个股推荐、涨跌预测、交易时机或收益承诺。数据来自公开接口，可能存在延迟、缺失或限流；请核对来源并独立决策。

## License

[MIT](LICENSE)
