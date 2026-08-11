# 架构说明

Vibe-Research 由一个 React 前端和一个 FastAPI 后端组成。前端负责界面、会话状态和 AI 配置入口；后端负责数据访问、用户本地存储、文件归档、AI 请求转发和 MCP 工具。

```text
Browser
  │  /api
  ▼
React + Vite (5899) ── development proxy ──► FastAPI (8900)
                                                   │
                 ┌─────────────────────────────────┼────────────────────────────┐
                 ▼                                 ▼                            ▼
          Public market data                 ~/.vibe-research/            AI providers / CLI
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

## 数据与状态

公开市场数据按来源和接口能力获取。接口可能使用缓存、最后一次可用快照或降级路径；页面应同时呈现更新时间和可用状态，而不是把缓存当成实时结论。

用户账户数据保存在本机 SQLite；持仓、已清仓记录和研报文件保存在用户数据目录。项目本身不提供远程数据托管服务。

## AI 接口

网页可使用本机 CLI 或 OpenAI 兼容 API；MCP server 供本地 Agent 调用数据工具。系统只传递数据和上下文，不内置股票推荐、目标价或交易指令。
