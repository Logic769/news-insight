# 分析服务镜像：只跑 news-insight，不碰爬虫进程，也不共享 SQLite 文件
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY fixtures ./fixtures

RUN useradd --create-home --uid 10001 insight \
 && mkdir -p /app/data \
 && chown -R insight:insight /app/data
USER insight

EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8020/health',timeout=3).status==200 else 1)"

# 读接口是 CPU/IO 混合型，多 worker 才能把并发 p95 压到 3 秒内；
# 分析批次本身由跨进程文件锁保护，多个 worker 不会重复跑 analyze。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8020 --workers ${INSIGHT_API_WORKERS:-1}"]
