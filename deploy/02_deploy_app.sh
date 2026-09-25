#!/usr/bin/env bash
# Phase 3, step 2: build the image with Cloud Build (pushed to Artifact Registry) and deploy the app as a
# Cloud Run service: Gemini on Vertex AI, the bucket mounted for inbound documents / cache / export, basic auth
# from Secret Manager, exactly one instance.
#
#   PROJECT_ID=velox-demo-123 bash deploy/02_deploy_app.sh
#
# Options (environment):
#   DB_ON_BUCKET=1   keep the SQLite file on the mounted bucket instead of /tmp (read the caveat below first)
#   UPLOAD_CACHE=1   copy the local extraction cache (data/cache) to the bucket first, so documents already
#                    extracted are not sent to the model again. Pre-extract first (make extract; make extract
#                    DATASET=v2): loading a set with an empty cache makes every model call inside one web request.
#   SKIP_BUILD=1     deploy the existing image without rebuilding it
#   BQ_EXPORT=1|0    switch the BigQuery export on or off (on: the dataset must exist, 04_bigquery.sh). Not set:
#                    the service keeps its current setting (off on the first deploy; 04 switches it on), because
#                    the settings are applied with --update-env-vars and never replace the others.
#
# Every deploy starts a new revision: with the database in /tmp the app starts from a freshly seeded ERP (empty
# mailboxes). Deploy before the demo, not during it.
#
# NOT TESTED against Google Cloud (written offline; see docs/DEPLOY_GCP.md).
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "${REPO_ROOT}"

if [ ! -d data/invoices_v2 ] || [ -z "$(ls -A data/invoices_v2 2>/dev/null)" ]; then
  echo "data/invoices_v2 is missing: run 'make pdfs' first (the image ships the generated documents)" >&2
  exit 1
fi

if [ "${UPLOAD_CACHE:-0}" = "1" ]; then
  if [ -z "$(ls -A data/cache 2>/dev/null)" ]; then
    echo "WARNING: data/cache is empty, nothing to upload. Pre-extract first (make extract; make extract" >&2
    echo "         DATASET=v2), or the first load of a set sends every PDF to the model inside one request." >&2
  else
    echo "== Copy the local extraction cache to gs://${BUCKET}/cache"
    gcloud storage rsync data/cache "gs://${BUCKET}/cache" --recursive
  fi
fi

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  echo "== Build ${IMAGE} with Cloud Build"
  # The upload skips secrets, the local DB and the cache (deploy/.gcloudignore); the build uses the Dockerfile.
  gcloud builds submit --region="${REGION}" --tag="${IMAGE}" --ignore-file=deploy/.gcloudignore .
fi

# SQLite location.
# Default: /tmp inside the container (an in-memory file system). Fast and safe, but the data lives only as long
#   as the instance: min-instances=1 keeps it alive during a demo; a redeploy or a restart starts from a
#   freshly seeded ERP. The inbound documents, the cache and the export survive on the bucket anyway.
# DB_ON_BUCKET=1: the file sits on the Cloud Storage FUSE mount and survives restarts. Caveat: GCS FUSE has no
#   file locking (SQLite's locks are not honoured) and rewrites the whole object on every change, so it is slow
#   and only safe with exactly one writer. Cloud Run briefly runs the old and the new revision side by side
#   during a deploy: stop using the app while deploying, or the file can be corrupted.
if [ "${DB_ON_BUCKET:-0}" = "1" ]; then
  DATABASE_URL="sqlite:///${MOUNT_PATH}/velox.db"
else
  DATABASE_URL="sqlite:////tmp/velox.db"
fi

# Plain settings. The password comes from Secret Manager (--set-secrets); Vertex AI uses the service account's
# credentials, so there is no API key anywhere. Applied with --update-env-vars: variables not listed here keep
# their current value on the service (BQ_EXPORT=1 from 04_bigquery.sh survives a redeploy).
env_vars=(
  "GEMINI_BACKEND=vertex"
  "GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
  "GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION}"
  "GEMINI_MODEL=${GEMINI_MODEL}"
  "EXTRACTOR=gemini"
  "APP_USERNAME=${APP_USERNAME}"
  "INBOUND_DIR=${MOUNT_PATH}/inbound"
  "CACHE_DIR=${MOUNT_PATH}/cache"
  "EXPORT_DIR=${MOUNT_PATH}/export"
  "DATABASE_URL=${DATABASE_URL}"
  "BQ_PROJECT=${PROJECT_ID}"
  "BQ_DATASET=${BQ_DATASET}"
)
if [ -n "${BQ_EXPORT:-}" ]; then
  env_vars+=("BQ_EXPORT=${BQ_EXPORT}")
else
  echo "BQ_EXPORT not set: the service keeps its current BigQuery export setting (off unless 04_bigquery.sh ran)"
fi
env_list="$(IFS=,; echo "${env_vars[*]}")"

echo "== Deploy the Cloud Run service ${SERVICE} (${REGION})"
# --execution-environment gen2   needed for Cloud Storage volume mounts
# --add-volume / -mount          the bucket at ${MOUNT_PATH}, owned by the image's non-root user (uid/gid)
# --min-instances 1              the SQLite state survives between requests during a demo (costs an idle
#                                instance; set it back to 0 or delete the service afterwards)
# --max-instances 1              SQLite + one uvicorn worker: never two instances writing
# --timeout 900                  the intake webhook extracts (a model call) and runs the gate before it answers;
#                                loading a set whose PDFs are not in the cache makes all its model calls in one
#                                request (pre-extract, see UPLOAD_CACHE). Cloud Run allows up to 3600.
# --allow-unauthenticated        the URL is public; basic auth (APP_PASSWORD) protects every page except /health
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --execution-environment=gen2 \
  --add-volume="name=data,type=cloud-storage,bucket=${BUCKET},mount-options=uid=${APP_UID};gid=${APP_UID}" \
  --add-volume-mount="volume=data,mount-path=${MOUNT_PATH}" \
  --update-env-vars="${env_list}" \
  --set-secrets="APP_PASSWORD=APP_PASSWORD:latest" \
  --min-instances=1 \
  --max-instances=1 \
  --concurrency=20 \
  --cpu=1 \
  --memory=1Gi \
  --cpu-boost \
  --timeout=900 \
  --port=8080 \
  --allow-unauthenticated

url="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"
echo
echo "Service URL: ${url}   (user ${APP_USERNAME}, password: the APP_PASSWORD secret)"
echo "Health check (no password): curl -s ${url}/health"
echo "Next: bash deploy/03_deploy_intake_job.sh   (optional: bash deploy/04_bigquery.sh)"
