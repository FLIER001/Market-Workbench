#!/usr/bin/env bash
# 发布打包：源码 → .publish/app（前端构建产物 + 后端精简副本）。
#
# 只做本地打包与自检；上传/部署由 WorkBuddy 发布工具完成（或走 deploy/ 那套自建主机）。
# 云服务配置在**构建时**注入：未注入的构建自动关闭云登录与云数据层。
#
# 用法：
#   scripts/publish.sh                # 要求工作区干净
#   scripts/publish.sh --allow-dirty   # 本地试打包
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ALLOW_DIRTY=0
[[ "${1:-}" == "--allow-dirty" ]] && ALLOW_DIRTY=1

# 云服务构建期配置（可选）。值属于可公开下发的 publicConfig，不是密钥。
if [[ -f .env.publish ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env.publish
  set +a
fi

if [[ $ALLOW_DIRTY -eq 0 ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "工作区有未提交改动，先提交或用 --allow-dirty 强制打包：" >&2
    git status --short >&2
    exit 1
  fi
fi

COMMIT="$(git rev-parse --short HEAD)"
TAG="$(git describe --tags --always 2>/dev/null || echo "$COMMIT")"

echo "==> 构建前端 (${TAG} / ${COMMIT})"
(
  cd frontend
  VITE_CLOUD_ENDPOINT="${VITE_CLOUD_ENDPOINT:-}" \
  VITE_CLOUD_PUBLISHABLE_KEY="${VITE_CLOUD_PUBLISHABLE_KEY:-}" \
  VITE_CLOUD_OAUTH_RELAY="${VITE_CLOUD_OAUTH_RELAY:-}" \
  npm run build
)

# 注入自检：声明了 endpoint 却没进产物，说明 VITE_* 没被 Vite 读到。
if [[ -n "${VITE_CLOUD_ENDPOINT:-}" ]]; then
  if ! grep -rq "$VITE_CLOUD_ENDPOINT" frontend/dist/assets/*.js; then
    echo "构建产物中未找到云服务 endpoint，VITE_CLOUD_* 注入失败" >&2
    exit 1
  fi
fi

echo "==> 同步 .publish/app"
mkdir -p .publish/app/backend .publish/app/frontend
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude 'tests' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '*.pyc' \
  backend/ .publish/app/backend/
rm -rf .publish/app/backend/.cache .publish/app/frontend/web
cp -R frontend/dist .publish/app/frontend/web
cp frontend/package.json .publish/app/frontend/package.json

echo
echo "产物  : .publish/app ($(du -sh .publish/app | cut -f1 | tr -d ' '))"
echo "版本  : $TAG ($COMMIT)"
if [[ -n "${VITE_CLOUD_ENDPOINT:-}" ]]; then
  echo "云配置: $VITE_CLOUD_ENDPOINT"
else
  echo "云配置: 未注入（云登录与云数据层关闭）"
fi
echo
echo "部署：上传 .publish/app 后，启动命令带 MW_BUILD_COMMIT=$COMMIT"
echo "      MW_CLOUD_ENDPOINT / MW_CLOUD_PUBLISHABLE_KEY 供后端校验云会话使用。"
