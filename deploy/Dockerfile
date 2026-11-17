# syntax=docker/dockerfile:1

# ---------- 前端构建 ----------
FROM node:22-alpine AS web
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# build = tsc -b && vite build；类型错误会直接让镜像构建失败
RUN npm run build

# ---------- 运行时 ----------
FROM python:3.14-slim AS runtime

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
