# syntax=docker/dockerfile:1

# ---------- 前端构建 ----------
# 基础镜像钉到 digest（多架构索引）：同一个 Dockerfile 永远构建出同样的基础层。
# 需要跟进上游安全更新时手动更新 digest：
#   docker buildx imagetools inspect node:22-alpine
FROM node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a AS web
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# build = tsc -b && vite build；类型错误会直接让镜像构建失败
RUN npm run build

# ---------- 运行时 ----------
FROM python:3.14-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HOME=/data \
    MW_DATA_DIR=/data \
    MW_HOST=0.0.0.0 \
    MW_PORT=8900 \
    PORT=8900

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata gosu \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --home-dir /data --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /app

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
COPY deploy/constraints.txt constraints.txt
RUN python -m pip install --upgrade pip \
 && python -m pip install -c constraints.txt -r backend/requirements.txt

# 后端（app.py 静态托管 ../frontend/dist；version.py 读 ../frontend/package.json）
COPY backend/ backend/
COPY --from=web /build/dist /app/frontend/dist
COPY frontend/package.json /app/frontend/package.json
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh && chown -R app:app /app

WORKDIR /app/backend
VOLUME ["/data"]
EXPOSE 8900

HEALTHCHECK --interval=30s --timeout=6s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/api/health', timeout=4)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8900", "--proxy-headers", "--forwarded-allow-ips", "*"]
