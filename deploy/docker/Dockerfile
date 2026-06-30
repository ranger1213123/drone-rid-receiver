# Drone RID 中心服务器 — 生产级镜像 (多阶段构建)
# 构建: docker build -f deploy/docker/Dockerfile -t drone-rid-center:VERSION .

# ── Stage 1: 前端构建 ──
FROM node:20-slim AS frontend-builder

WORKDIR /build

# 安装前端依赖
COPY package.json package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && npm ci --production=false 2>/dev/null || npm install

# 复制 Vite 配置和源码
COPY vite.config.js ./
COPY app/server/static/ app/server/static/

# 构建生产包
RUN npm run build

# ── Stage 2: Python 运行镜像 ──
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# 从构建阶段复制前端产物
COPY --from=frontend-builder /build/app/server/static/dist/ app/server/static/dist/

# 复制应用代码
COPY . .

# 创建非 root 用户
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/health')" || exit 1

CMD ["gunicorn", "-c", "deploy/docker/gunicorn.conf.py", "wsgi:app"]
