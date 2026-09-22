#!/usr/bin/env bash
# Market Workbench 一键启动（统一入口）。
# 前端构建产物由后端静态托管（app.py 里的 _FRONTEND_DIST），单进程对外服务。
# 开发模式（前端热更新）仍可另开 `npm run dev`，本脚本不影响。
set -euo pipefail
cd "$(dirname "$0")"

HOST="${MW_HOST:-127.0.0.1}"
PORT="${MW_PORT:-8900}"

# 前端：dist 缺失或源码比 dist 新时才构建（增量判断，省一次 vite）
if [ ! -f frontend/dist/index.html ] || \
   [ -n "$(find frontend/src frontend/package.json -newer frontend/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "[run] 构建前端..."
  (cd frontend && npm install --no-fund --no-audit >/dev/null && npm run build)
else
  echo "[run] 前端 dist 已是最新，跳过构建"
fi

echo "[run] 启动 http://${HOST}:${PORT}"
cd backend
exec .venv/bin/python -m uvicorn app:app --host "$HOST" --port "$PORT"
