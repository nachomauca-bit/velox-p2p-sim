# Velox P2P control-gate simulator: one container, one process, one uvicorn worker.
# The app keeps its state in one SQLite file, so it runs as a single instance (brief sections 3 and 17).
#
#   make pdfs && make docker-build          (the sample documents are copied in, never regenerated here:
#                                            their SHA-256 is the extraction cache key)
#   make docker-run                         (http://127.0.0.1:8080, settings from .env)
#
# Not in the image: tests, .env, the local database, the extraction cache, webhook uploads (.dockerignore).
FROM python:3.12-slim

# 1 = also install the optional Google Cloud libraries (BigQuery export); 0 = the plain app.
ARG INSTALL_GCP=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Dependencies first: this layer is reused while only the code changes.
COPY requirements.txt requirements-gcp.txt ./
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_GCP" = "1" ]; then pip install -r requirements-gcp.txt; fi

# Non-root user (uid 10001; deploy/02_deploy_app.sh mounts the bucket with this uid). It owns /app/data, the
# local default for the database, the cache, the export and the webhook uploads.
RUN useradd --create-home --uid 10001 --user-group --shell /usr/sbin/nologin velox \
    && mkdir -p /app/data \
    && chown velox:velox /app/data

COPY --chown=velox:velox app/ ./app/
COPY --chown=velox:velox docs/ ./docs/
COPY --chown=velox:velox data/invoices/ ./data/invoices/
COPY --chown=velox:velox data/invoices_v2/ ./data/invoices_v2/

USER velox
EXPOSE 8080

# ONE worker (single instance, SQLite). Cloud Run sets PORT; the proxy headers give the app the https scheme.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
