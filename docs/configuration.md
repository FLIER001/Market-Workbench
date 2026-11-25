# 配置说明

本地运行无需配置。只有更改数据目录、使用特定数据服务或将服务暴露给其他设备时，才需要设置环境变量。

后端示例文件为 [`backend/.env.example`](../backend/.env.example)。可在启动前导出变量，或复制为 `backend/.env.local`；`./run.sh` 会自动加载此文件。用 `MW_ENV_FILE=/绝对路径/配置文件` 可改用版本库外的私有文件。文件使用 shell 变量赋值格式，由启动脚本加载；文件中显式设置的变量优先于启动环境。建议将权限设为 `600`，不要把凭证写入 Git 跟踪的 launchd plist。

`MW_MEM_WATCHDOG` 默认关闭，普通 `./run.sh` 不会因运行满 8 小时而停止服务。macOS 上需配置 launchd KeepAlive 等进程守护后再设为 `1`（仓库的 launchd 模板已显式开启）；Linux / 容器上读 cgroup 内存用量，需配合 `restart` 策略，`deploy/compose.yaml` 已默认开启。阈值默认 macOS 为 500MB，容器为 cgroup 上限的 80%，均可用 `MW_MEM_WATCHDOG_MAX_MB` 覆盖。

## 访问与跨域

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MW_ALLOW_ORIGINS` | `*` | 逗号分隔的 CORS 来源。仅限本地开发时保留默认值。 |
| `MW_API_KEY` | 空 | 设置后，除健康检查与登录注册入口外，API 都要求 `Authorization: Bearer <key>`（或 `X-VR-Access-Key`）；同时关闭 `/docs`、`/redoc` 与 `/openapi.json`。登录页会要求先填该密钥。 |
| `MW_CLI_AGENTS` | 关闭 | 设为 `1` 才允许 Codex / Qwen / DeepSeek 订阅接入。这三个 CLI 以全自动模式运行，可读写本机文件；Claude Code 已禁用全部工具，不受此开关影响。 |
| `MW_USERS_DB` | `$MW_DATA_DIR/users.db` | 账号库路径。 |
| `MW_MEM_WATCHDOG_MAX_MB` | 500 / cgroup 上限的 80% | 内存看门狗的触发阈值。 |
| `MW_FRONTEND_DIST` | `frontend/dist` | 后端静态托管的目录；托管平台过滤掉 `dist` 时换名。 |
| `MW_DEEP_ANALYSIS_TIMEOUT` / `MW_DEEP_ANALYSIS_SLOTS` | 2400 / 4 | 深度分析的单次超时（秒）与并发上限，超出立即返回 429。 |
| `MW_PULSE_LLM_BASE_URL` / `MW_PULSE_LLM_API_KEY` / `MW_PULSE_LLM_MODEL` | 空 | 「全球预期」页 AI 研判卡片的模型（OpenAI 兼容）。 |

公网部署至少应：

1. 将 `MW_ALLOW_ORIGINS` 设为实际前端域名；
2. 为 `MW_API_KEY` 设置随机强密钥；
3. 在反向代理上配置 HTTPS；
4. 限制运行主机和数据目录的访问权限；
5. 保持 `MW_ALLOW_REGISTRATION` 关闭（默认）：账号用 `backend/add_user.py` 添加。

无论是否设置 `MW_API_KEY`，下列请求都要求登录：所有写操作（POST / PUT / PATCH / DELETE）、带 `refresh=true` / `force=true` 的强制刷新、会调用 LLM 或本机 CLI 的端点。`MW_API_KEY` 是所有用户共享的网关门槛，真正按用户隔离的是登录会话。

前端“接入 AI”页可以填写与 `MW_API_KEY` 相同的访问密钥。该密钥只用于浏览器到你后端的请求鉴权。

## 数据目录

| 变量 | 默认位置 | 说明 |
|---|---|---|
| `MW_DATA_DIR` | `~/.market-workbench/` | 股票持仓及默认研报目录的根目录。 |
| `MW_REPORTS_DIR` | `$MW_DATA_DIR/myreports/` | 单独覆盖研报存储位置。 |

账号 SQLite 数据库默认保存在 `$MW_DATA_DIR/users.db`（即 `~/.market-workbench/users.db`）。所有数据路径由 `backend/paths.py` 统一解析。建议定期备份该目录，以及通过变量指定的持仓和研报目录；不要把它们加入 Git 仓库或公开同步目录。

## 数据源与网络

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MW_DATA_PROXY` | 未设置 | 设为 `1` 时强制数据请求使用系统代理。国内财经数据通常应保持直连。 |
| `IWENCAI_API_KEY` | 未设置 | 启用 iWenCai 研报语义搜索时使用。 |
| `IWENCAI_BASE_URL` | 服务默认地址 | 覆盖 iWenCai 服务地址。 |

公开数据源的覆盖范围、延迟和可用性不同。应用将缓存和最后一次可用结果用于展示，但缓存不是实时数据的替代品。

美联储页区分最近刷新尝试、最近成功抓取和行情数据时点；抓取失败时保留旧快照并标记，旧值不会当作新观测写入概率变化历史。v1.8.1 起可信历史写入 `fedwatch_history_v2.jsonl`，原历史文件保留；24h/7d 变化需要重新积累覆盖。

## 前端代理

开发服务器默认把 `/api` 代理到 `http://127.0.0.1:8900`。后端地址变更时，在启动前端前设置：

```bash
cd frontend
VITE_API_URL=http://127.0.0.1:9000 npm run dev
```

## AI 配置

模型提供商的 API key 不写入仓库。浏览器把配置保存在本地存储，并在请求时交给当前后端；部署在共享服务器时，应自行评估浏览器、日志和反向代理的安全边界。
