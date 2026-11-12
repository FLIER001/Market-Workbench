<div align="center">

# Market Workbench

[简体中文](README.md) · [English](README_en.md) · [更新记录](CHANGELOG.md) · [路线图](ROADMAP.md)

[![Version](https://img.shields.io/badge/version-1.8.2-1f6feb)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61dafb?logo=react&logoColor=white)](https://react.dev/)
[![License](https://img.shields.io/badge/license-MIT-2ea44f)](LICENSE)

</div>

本地自托管的 A 股研究工作台，兼顾港股和美股。提供行情、财务、宏观、行业、债市、黄金、油价和因子研究，也可记录自选、持仓、研报与笔记。AI 使用你自己配置的接口或本机 CLI。

## 主要特点

- **本地数据**：账号、持仓和研报默认保存在 `~/.market-workbench/`。
- **公开数据源**：使用东方财富、腾讯、新浪、AkShare、FRED、EIA、IMF、Polymarket、Kalshi 等接口；多数数据源无需 API key。
- **AI 可插拔**：支持 OpenAI 兼容 API，以及 Claude Code、Codex、Qwen、Gemini、DeepSeek 等本机 CLI；浏览器保存 AI 配置。
- **网页与 Agent 共用数据层**：FastAPI HTTP API 和零第三方依赖的 MCP Server 共用 43 个数据工具。
- **数据状态可见**：页面标注来源、更新时间和回退状态；缓存快照不被表述为实时数据。

![Market Workbench 市场全景](docs/screenshots/daily-review.png)

<details>
<summary>查看其他页面</summary>

| 宏观面 | 因子研究 |
|---|---|
| ![宏观面](docs/screenshots/macro.png) | ![因子研究](docs/screenshots/factors.png) |

| 债市 | 择时配置 |
|---|---|
| ![债市](docs/screenshots/bonds.png) | ![择时配置](docs/screenshots/allocation.png) |

| 黄金 | 油价 |
|---|---|
| ![黄金](docs/screenshots/gold.png) | ![油价](docs/screenshots/oil.png) |

| 资讯 | 个股数据 |
|---|---|
| ![资讯](docs/screenshots/intel.png) | ![个股数据](docs/screenshots/stock-detail.png) |

</details>

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 20+
- macOS 或 Linux；`run.sh` 使用 Bash

### 安装并启动

```bash
git clone https://github.com/FLIER001/Market-Workbench.git
cd Market-Workbench

cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..

./run.sh
```

打开 <http://127.0.0.1:8900>。`run.sh` 会在前端产物缺失或过期时构建前端，然后由同一个 FastAPI 进程提供网页和 API。

首次启动且用户库为空时，可以在网页注册主账号。后续账号通过命令行管理：

以下命令都在 `backend/` 目录执行。省略密码参数时会进入交互式输入，输入内容不会显示在终端中。

```bash
cd backend

# 添加账号：按提示输入至少 6 位的密码
.venv/bin/python add_user.py add <用户名>

# 列出全部账号
.venv/bin/python add_user.py list

# 重置密码：按提示输入新密码，同时注销该账号的所有登录会话
.venv/bin/python add_user.py passwd <用户名>

# 删除账号：同时删除该账号的用户数据、持仓记录和登录会话，且不可恢复
.venv/bin/python add_user.py remove <用户名>
```

安装验证与常见问题见 [快速上手](docs/getting-started.md)。

## 功能地图

| 研究范围 | 主要功能 |
|---|---|
| **市场与组合** | A 股与全球市场全景、指数和估值百分位、市场情绪、资金轮动、公开资讯；分组自选、股票与基金持仓、收益跟踪、持仓体检与历史情景回放；未来 7 / 30 / 90 天事件日历 |
| **宏观与配置** | 宏观模块评分与总分卡、中美流动性合成分、择时配置与历史回放、Polymarket / Kalshi 概率面板、美联储 ZQ 期货概率与预测市场对照 |
| **证券研究** | A 股、港股、美股行情，K 线、估值、财务、公告、研报、资金面；A 股财报前瞻与事件回看；高管和股东增持分析 |
| **行业与筛选** | 申万行业与主题板块评分、产业链环节与利润分布、Manager-First 基金筛选、自然语言与规则核对个股筛选 |
| **因子研究** | RankIC、五分组、换手与组合回测；价量及 PIT 财务因子、自定义公式、研究版本、对照实验、回测报表与邻参敏感度 |
| **其他资产** | 债市收益率曲线、持有与压力情景、期限候选先验；黄金环境与波动研究；油价评分与 WTI 暗盘行情 |
| **个人研究库** | 研报归档、研究笔记、反思审计 |
| **运行状态** | 上游接口探针、数据集更新时间与健康状态 |

其中宏观评分、择时配置、债券先验、黄金与油价指标、因子回测属于研究工具；部分实验指标尚未经过独立样本外验证。公开数据可能延迟、缺失或被限流。

## 接入 AI 与 MCP

**网页中使用 AI**：在「设置 → 接入 AI」中选择本机 CLI，或填写 OpenAI 兼容 API 的地址、模型与密钥。配置保存在浏览器本地，调用模型时才发送给本机后端。

**本地 Agent 调用数据工具**：以 Claude Code 为例，在仓库根目录执行：

```bash
claude mcp add market-workbench -- \
  "$(pwd)/backend/.venv/bin/python" "$(pwd)/backend/mcp_server.py"
```

MCP Server 使用与网页相同的数据工具层，本地调用不需要 API key。HTTP API 分组和工具说明见 [后端文档](backend/README.md)。

## 配置与部署

本地运行无需额外配置。需要修改端口、数据目录或对外提供服务时，再设置环境变量：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `MW_HOST` / `MW_PORT` | `127.0.0.1` / `8900` | 服务监听地址与端口 |
| `MW_DATA_DIR` / `MW_REPORTS_DIR` | `~/.market-workbench/` | 用户数据与研报目录 |
| `MW_ENV_FILE` | `backend/.env.local` | `run.sh` 加载的私有配置文件 |
| `MW_ALLOW_ORIGINS` | `*` | CORS 来源白名单 |
| `MW_API_KEY` | 空 | 设置后为 API 启用 Bearer 鉴权，健康检查除外 |
| `MW_ALLOW_REGISTRATION` | 关闭 | 设为 `1` 时允许网页注册 |
| `MW_DATA_PROXY` | 关闭 | 设为 `1` 时数据请求使用系统代理 |

完整配置、可选数据源密钥和公网部署要求见 [配置说明](docs/configuration.md) 与 [环境变量示例](backend/.env.example)。公开部署前，至少收紧 CORS、设置强 API key，并在反向代理上启用 HTTPS。

## 开发与架构

```text
React 19 + TypeScript + Vite
              │
              ▼
FastAPI :8900 ── 公开市场数据 / 本地 SQLite 与文件 / 自有 AI 或 CLI
     │
     ├── 静态托管 frontend/dist
     └── MCP Server（stdio，共用数据工具层）
```

前端开发需要热更新时，另运行 `cd frontend && npm run dev`；Vite 使用 5899 端口，并将 `/api` 代理到 8900。

```bash
# 后端测试
cd backend && .venv/bin/python -m pytest tests -q

# 前端测试与构建
cd frontend && npm test && npm run build
```

后端按主题拆分模块，评分重算使用短命子进程，版本号以 `frontend/package.json` 为准。模块划分与数据流见 [架构说明](docs/architecture.md)。

## 项目边界

Market Workbench 整理公开数据和用户自行录入的信息，输出仅供研究参考，不构成投资建议或收益承诺。使用前应核对数据来源与时点，并独立判断。

债市、黄金与因子研究的方法和限制分别见 [债市 V2](research/bonds_methodology_v2.md)、[黄金 V3](research/gold_methodology_v3.md) 与 [因子实验台 API](docs/research/factor-lab-api.md)。

## 致谢与许可

本项目基于 [simonlin1212/Vibe-Research](https://github.com/simonlin1212/Vibe-Research) 二次开发，保留了公开数据接入与可插拔 AI 的基础，并扩展了宏观、资产配置、行业、因子和跨资产研究。

[MIT License](LICENSE)
