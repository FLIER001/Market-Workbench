# 快速开始

本指南用于在一台本地机器上运行 Market Workbench。前端和后端需要同时启动。

## 前置条件

- Python 3.10 或更高版本
- Node.js 20 或更高版本
- npm

## 启动后端

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8900
```

另开一个终端确认健康检查：

```bash
curl -fsS http://127.0.0.1:8900/api/health
```

预期返回包含 `"ok": true` 和当前版本号的 JSON。

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

访问 <http://127.0.0.1:5899>。首次使用请注册账号；账号数据库只保存在这台机器上。

## 首次配置 AI

打开“接入 AI”页面，任选一种方式：

- 使用已经登录的本机 CLI；
- 填写自己的 OpenAI 兼容 API 地址、模型名和 key；
- 在外部 Agent 中注册后端 MCP server。

模型配置不由项目提供或托管。只有在需要调用模型时，浏览器才会将相应配置提交给本机后端。

## 常见问题

### 前端提示无法连接后端

确认后端监听在 `127.0.0.1:8900`，再运行健康检查。开发环境默认将 `/api` 代理至该地址；如后端在其他地址，设置前端的 `VITE_API_URL` 后重启前端。

### 个别数据为空或刷新失败

数据来自公开服务，可能有交易时段、延迟、限流或临时不可用等限制。页面会保留可用数据及其时间状态；不要把缺失值解释为市场结论。

### 更新项目后数据是否还在

默认数据目录位于 `~/.vibe-research/`，不在项目目录内。升级前仍建议备份该目录；路径配置见 [configuration.md](configuration.md)。
