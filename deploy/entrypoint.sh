#!/bin/sh
# 容器入口：准备数据目录 → 降权到 app 用户 → 启动 uvicorn
set -eu

DATA_DIR="${MW_DATA_DIR:-/data}"
REPORTS_DIR="${MW_REPORTS_DIR:-$DATA_DIR/myreports}"

mkdir -p "$DATA_DIR" "$REPORTS_DIR"

if [ "$(id -u)" = "0" ]; then
  # 绑定挂载的宿主机目录通常属于 root；只在启动时纠正一次归属
  chown -R app:app "$DATA_DIR" 2>/dev/null || true
  exec gosu app "$@"
fi

exec "$@"
