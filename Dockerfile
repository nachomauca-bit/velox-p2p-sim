# Velox P2P control-gate simulator: one container, one process, one uvicorn worker.
# The app keeps its state in one SQLite file, so it runs as a single instance (brief sections 3 and 17).
#
#   make pdfs && make docker-build          (the sample documents are copied in, never regenerated here:
#                                            their SHA-256 is the extraction cache key)
#   make docker-run                         (http://127.0.0.1:8080, settings from .env, state in a volume)
#
# In the image: the app, the docs, the sample documents and the real Gemini extractions and drafts (data/cache).
# Not in the image: tests, .env, the local database, webhook uploads, the export (.dockerignore).
# Everything the app writes goes to STATE_DIR=/data: mount a volume there to keep it across redeploys
# (docs/DEPLOY_EASYPANEL.md). On start, app.storage copies the shipped cache into /data/cache (never overwriting).
FROM python:3.12-slim

# 1 = also install the optional Google Cloud libraries (BigQuery export); 0 = the plain app.
ARG INSTALL_GCP=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    STATE_DIR=/data

WORKDIR /app

# Dependencies first: this layer is reused while only the code changes.
COPY requirements.txt requirements-gcp.txt ./
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_GCP" = "1" ]; then pip install -r requirements-gcp.txt; fi

# Non-root user (uid 10001; deploy/02_deploy_app.sh mounts the bucket with this uid). It owns /data (STATE_DIR:
# database, cache, export, webhook uploads); a new Docker volume mounted there inherits that owner.
RUN useradd --create-home --uid 10001 --user-group --shell /usr/sbin/nologin velox \
    && mkdir -p /app/data /data \
    && chown velox:velox /app/data /data

COPY --chown=velox:velox app/ ./app/
COPY --chown=velox:velox docs/ ./docs/
COPY --chown=velox:velox data/invoices/ ./data/invoices/
COPY --chown=velox:velox data/invoices_v2/ ./data/invoices_v2/
COPY --chown=velox:velox data/cache/ ./data/cache/

USER velox
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)"

# Folders and cache first (never blocks the start), then ONE worker (single instance, SQLite). Cloud Run sets PORT;
# the proxy headers give the app the https scheme behind a reverse proxy (Cloud Run, EasyPanel's Traefik).
CMD ["sh", "-c", "python -m app.storage; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
