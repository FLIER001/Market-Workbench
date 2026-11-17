# Market Workbench 云主机部署

单机 Docker Compose：`app`（FastAPI + 静态前端）+ `caddy`（自动 HTTPS 反代）。数据全部落在 `deploy/data/`。

## 0. 前置

| 项 | 要求 |
|---|---|
| 主机 | 2 核 4G 起，Ubuntu 22.04/24.04，磁盘 20G+ |
| 架构 | **必须 x86_64（amd64）**。akshare 在 Linux 依赖 `py-mini-racer`，该包无 arm64 轮子，ARM 实例装不上 |
| 地域 | **建议国内**（数据源是东财/新浪/腾讯，海外 IP 易被限流） |
| 域名 | A 记录指向主机公网 IP |
| 端口 | 放行 80 / 443（Caddy 自动签证书） |
| 内存 | 实测 footprint 峰值 1.24G；构建阶段建议加 2G swap |

## 1. 装 Docker

```bash
curl -fsSL https://get.docker.com | sh
docker compose version
```

## 2. 传代码

在**本机**执行（排除体积与大目录，约 40M）：

```bash
rsync -av --delete \
  --exclude '.git' --exclude 'docs' --exclude 'outputs' --exclude 'xhs-cards' \
  --exclude 'frontend/node_modules' --exclude 'frontend/dist' --exclude 'backend/.venv' \
  --exclude '__pycache__' --exclude '*.log' --exclude '.env.local' \
  /Users/k/Market-Workbench/ user@服务器:/opt/market-workbench/
```

## 3. 配置

```bash
cd /opt/market-workbench/deploy
cp .env.example .env && chmod 600 .env
vi .env          # MW_DOMAIN / MW_API_KEY / MW_ALLOW_ORIGINS / MW_DEEP_ANALYSIS_*
mkdir -p data && chmod 700 data
```

## 4. 构建启动

```bash
docker compose up -d --build     # 首次约 5-10 分钟（装 akshare/pandas）
docker compose ps                # app 应为 healthy
docker compose logs -f app
```

## 5. 建号

用户库为空时，第一个账号可以直接在网页注册（之后自动关闭注册）。想跳过这个窗口就用命令行：

```bash
docker compose exec app python add_user.py add <用户名>
docker compose exec app python add_user.py list
```

`MW_ALLOW_REGISTRATION=0`（默认）下，其余账号一律走上面的命令。

## 6. 验收

| 检查 | 期望 |
|---|---|
| `curl https://<域名>/api/health` | 200 + 版本号 JSON |
| 登录页 | 首次登录会要求填「访问密钥」= `.env` 里的 `MW_API_KEY`；填对才能登录 |
| 行情 / 个股 / 黄金页 | 数据正常返回 |

## 7. 日常运维

| 事项 | 命令 |
|---|---|
| 更新代码 | 本机重跑 rsync → `docker compose up -d --build` |
| 备份数据 | `tar czf mw-$(date +%F).tgz -C deploy data` |
| 看日志 | `docker compose logs -f --tail=200 app` |
| 重启 | `docker compose restart app` |
| 内存占用 | `docker stats --no-stream` |

**每周重启**（项目已知内存只涨不跌，容器内看门狗仅在 macOS 生效）：

```bash
# crontab -e
0 4 * * 1 cd /opt/market-workbench/deploy && docker compose restart app
```

## 8. 排错

| 现象 | 原因 / 处理 |
|---|---|
| 构建被 Killed | 内存不足，加 swap 或换 4G 机型 |
| 证书签不下来 | 域名未解析 / 80、443 未放行 / 套了 CDN 回源配置错 |
| 页面能开、接口 401 | 浏览器本地没有正确的访问密钥：重新登录并在登录页填 `MW_API_KEY` |
| 行情大面积超时 | 服务器在海外被数据源限流，换国内地域 |
| 首次打开黄金/油价页很慢 | 冷建 50s-2min，属预期；后续走缓存 |
| 容器反复重启 | `docker compose logs app` 看启动异常；`mem_limit` 是否过紧 |

## 说明

- 镜像不含 `.git`，网页上的「检查更新」不可用；更新走第 7 节的重新构建。
- 本机 CLI 订阅通道（Claude/Qwen/Codex）在云端无效，AI 只能用 `MW_DEEP_ANALYSIS_*` 的 API 接口。
- `deploy/data/` 存 `users.db`、自选/持仓、研报、各类快照；迁移主机时整目录带走即可。
- WorkBuddy 云服务（账号登录 + `user_data` 云表）只绑定发布域名，自建部署留空
  `MW_CLOUD_ENDPOINT` / `MW_CLOUD_PUBLISHABLE_KEY` 即可：登录页自动隐藏云登录入口，
  用户数据仍落在 `deploy/data/users.db`。云表 RLS 按 `owner_id = auth.uid()` 隔离，
  应用代码不传 `owner_id`、不传 token。
