<p align="center"><b>简体中文</b> | <a href="README_en.md">English</a></p>

# Market Workbench

[![Version](https://img.shields.io/badge/version-1.1.0-1f6feb)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61dafb?logo=react&logoColor=white)](https://react.dev/)
[![License](https://img.shields.io/badge/license-MIT-2ea44f)](LICENSE)

面向 A 股研究、兼顾港美市场的本地自托管市场研究工作台。它把行情、财务、公告、资金面、宏观、板块和公开资讯放到一个界面；AI 连接由用户自行配置，产品不提供荐股或交易指令。

> **项目沿革**：本项目基于 [simonlin1212/Vibe-Research](https://github.com/simonlin1212/Vibe-Research) 二次开发，保留其公开数据与可插拔 AI 接入基础。当前版本主要增加宏观与资金面、黄金与基金、基金持仓与观察列表、行业及主题板块研究和评分、账号隔离的数据同步，以及数据来源和时效状态展示。

![市场全景](docs/screenshots/daily-review.png)

## 目录

- [功能](#功能)
- [快速开始](#快速开始)
- [使用](#使用)
- [配置与数据](#配置与数据)
- [开发](#开发)
- [文档](#文档)
- [边界与免责声明](#边界与免责声明)

## 功能

| 模块 | 内容 |
|---|---|
| 市场全景 | A 股指数、全球市场、市场情绪、成交额、板块资金与每日复盘 |
| 宏观与资金面 | 宏观指标、流动性信号、数据来源、更新时间与可用状态 |
| 个股与自选 | A 股、港股、美股行情；K 线、估值、财务、公告、研报、资金面和自选分组 |
| 板块与黄金 | 产业链研究、申万行业与主题板块观察、黄金多维指标 |
| 持仓与基金 | 股票及基金持仓、已清仓记录、基金搜索、净值、筛选和观察列表 |
| 资讯与研究 | 公开资讯聚合、个人研报归档、研究笔记、反思审计 |
| AI 与 MCP | OpenAI 兼容 API、本机 CLI 与 MCP 数据工具；模型和密钥由用户自行提供 |

## 快速开始

要求：Python 3.10+、Node.js 20+、npm。

```bash
git clone https://github.com/FLIER001/Vibe-Research.git
cd Vibe-Research

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

浏览器打开 <http://127.0.0.1:5899>，注册本地账号即可使用。先确认服务正常：

```bash
curl -fsS http://127.0.0.1:8900/api/health
```

更多启动方式见 [docs/getting-started.md](docs/getting-started.md)。

## 使用

1. 在“接入 AI”中选择本机 CLI 或填入自己的 OpenAI 兼容 API 配置。
2. 在“自选”或“持仓”录入自己的标的；持仓成本和已清仓记录均由你维护。
3. 从市场、宏观、板块或个股页面查看原始数据和来源状态，再按需要发起研究请求。

后端同时提供 HTTP API 与 MCP Server。MCP 安装示例和主要 API 分组见 [backend/README.md](backend/README.md)。

## 配置与数据

- 本地开发无需环境变量。前端默认将 `/api` 代理至 `http://127.0.0.1:8900`。
- 对外部署时应设置 `VR_ALLOW_ORIGINS` 和 `VR_API_KEY`；示例见 [backend/.env.example](backend/.env.example)。
- 用户账号数据库、持仓和研报默认保存在运行机器的 `~/.vibe-research/`，不写入仓库。`VR_DATA_DIR` 与 `VR_REPORTS_DIR` 可调整持仓和研报路径。
- API key 存在浏览器本地存储，并仅随请求发送到你部署的后端。

完整变量、部署注意事项和备份位置见 [docs/configuration.md](docs/configuration.md)。

## 开发

```bash
# 后端测试
cd backend
.venv/bin/python -m pytest tests -q

# 前端测试与构建
cd frontend
npm test
npm run build
```

项目版本的唯一来源是 `frontend/package.json`；后端 API、MCP Server 和界面展示都由此读取。变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 文档

- [快速开始](docs/getting-started.md)：本地运行、验证与常见问题
- [配置说明](docs/configuration.md)：环境变量、数据目录和对外部署
- [架构说明](docs/architecture.md)：前端、后端、数据源和 AI 接口
- [后端 API 与 MCP](backend/README.md)
- [路线图](ROADMAP.md)

## 边界与免责声明

Market Workbench 只整理公开数据和用户自行录入的信息，不提供个股推荐、涨跌预测、交易时机或收益承诺。页面中的指标、榜单和 AI 输出都不构成投资建议；请核对来源并独立决策。

## License

[MIT](LICENSE)
