# Build from the repository root:  docker build -f backend/Dockerfile -t warren-api .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* && useradd -m -u 10001 warren
WORKDIR /srv
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/ .
RUN mkdir -p /data && chown -R warren /data
USER warren
ENV WARREN_ENV=production SQLITE_PATH=/data/warren.db CHROMA_PATH=/data/chroma CHECKPOINT_PATH=/data/checkpoints.db PORT=5000
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"
# One worker on purpose; concurrency comes from threads (SSE streams + background jobs).
CMD ["sh", "-c", "gunicorn wsgi:app --bind 0.0.0.0:${PORT:-5000} --worker-class gthread --workers 1 --threads 32 --timeout 120 --graceful-timeout 30"]
