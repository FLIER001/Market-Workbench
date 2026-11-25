# 安全说明

## 支持范围

只支持 `main` 分支的最新版本。安全问题修复不单独向后移植。

## 报告漏洞

请勿在公开 Issue 中披露可被利用的细节。通过仓库的 GitHub 私密漏洞报告（Security → Report a vulnerability）提交，说明受影响版本、复现步骤与影响范围。

## 安全边界

| 层 | 作用 | 配置 |
|---|---|---|
| 访问密钥 `MW_API_KEY` | 所有用户共享的网关门槛，挡住匿名读取；设了即关闭 `/docs`、`/redoc`、`/openapi.json` | 环境变量；前端登录页填写后存浏览器 localStorage |
| 登录会话 | 真正按用户隔离数据的凭据。库里只存 token 的 SHA-256，有效期 30 天 | `MW_ALLOW_REGISTRATION` 控制能否网页注册；默认只能用 `backend/add_user.py` 建号 |
| 登录限流 | 同一来源 IP 对同一用户名 5 分钟内失败 10 次后返回 429 | 进程内计数，单实例部署有效 |
| 默认拒绝写操作 | 所有 POST / PUT / PATCH / DELETE 在解析请求体之前校验会话，只有登录、注册、云身份登录、退出登录例外 | 代码内白名单，`tests/test_auth_boundary.py` 守住 |
| 强制刷新与 LLM | `refresh=true` / `force=true`、对话、反思、深度分析、AI 解读、研报、自定义因子、数据构建均要求登录 | 同上 |
| 请求体上限 | 单个请求 40MB；单个用户数据 key 2MB；研报单文件 25MB | `app._MAX_BODY_BYTES`、`users.MAX_VALUE_BYTES`、`myreports.MAX_BYTES` |

### 本机 CLI（订阅接入）

「订阅接入」会调用本机已登录的 AI CLI，提示词里带着页面上的新闻、公告、资讯正文。这些正文来自外部，可能包含诱导性内容。

| CLI | 运行方式 | 默认 |
|---|---|---|
| Claude Code | 禁用全部工具，只输出文字 | 可用 |
| Codex / Qwen / DeepSeek | 全自动模式，可读写本机文件、执行命令 | 关闭，需 `MW_CLI_AGENTS=1` 显式开启 |

开启前请确认：只有你本人使用这台机器，且接受页面内容被当作指令执行的风险。

### 用户自带模型端点

对话允许用户自带 OpenAI 兼容 `baseURL`。后端在请求前校验目标：始终拒绝云元数据地址；设了 `MW_API_KEY` 时额外拒绝内网地址并解析域名核对；不跟随重定向。

## 已知取舍

- 会话 token 与访问密钥保存在浏览器 localStorage，页面若有 XSS 即可被读取。项目未使用 `dangerouslySetInnerHTML` 与原始 HTML 渲染；接入第三方脚本前请评估。
- `MW_API_KEY` 泄露后只能整体更换，所有用户需重新填写。
- 登录限流在进程内计数，多实例部署时各实例独立。
- 内置的「程序更新」会执行 `git pull --ff-only` 并重启进程，任何登录用户都能触发；仅在单人自托管时使用。
