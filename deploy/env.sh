#!/usr/bin/env bash
# Shared settings of the deploy scripts (sourced by 01..04; not run on its own).
#
# Every value can be overridden from the environment, e.g.
#   PROJECT_ID=velox-demo-123 REGION=europe-west1 bash deploy/01_setup.sh
# Nothing here is secret: passwords go to Secret Manager (01_setup.sh), never into files or command lines.
# NOT TESTED against Google Cloud (written offline; see docs/DEPLOY_GCP.md).

: "${PROJECT_ID:?set PROJECT_ID to your Google Cloud project id, e.g. PROJECT_ID=velox-demo-123}"

# Every gcloud command of these scripts uses this project, without changing your gcloud default.
export CLOUDSDK_CORE_PROJECT="$PROJECT_ID"

REGION="${REGION:-europe-west1}"               # Cloud Run, Artifact Registry, Cloud Scheduler and the bucket
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"   # GOOGLE_CLOUD_LOCATION for Gemini on Vertex AI (or europe-west1)
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

SERVICE="${SERVICE:-velox-p2p-sim}"            # Cloud Run service (the app)
JOB="${JOB:-velox-intake}"                     # Cloud Run job (the IMAP poller)
SCHEDULER_JOB="${SCHEDULER_JOB:-velox-intake-every-minute}"
AR_REPO="${AR_REPO:-velox}"                    # Artifact Registry (Docker) repository
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}:${IMAGE_TAG}}"

SA_NAME="${SA_NAME:-velox-p2p-sim}"            # one service account for the service, the job and the scheduler
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

BUCKET="${BUCKET:-${PROJECT_ID}-velox-p2p}"    # inbound documents, extraction cache, KPI export
MOUNT_PATH="${MOUNT_PATH:-/mnt/gcs}"           # where the bucket is mounted in the container
APP_UID="${APP_UID:-10001}"                    # the Dockerfile's non-root user; the mount is owned by it

APP_USERNAME="${APP_USERNAME:-velox}"          # basic auth user (the password is the APP_PASSWORD secret)

BQ_DATASET="${BQ_DATASET:-velox_p2p}"
BQ_LOCATION="${BQ_LOCATION:-EU}"               # BigQuery dataset location (EU multi-region)

# The repository root (the scripts may be called from anywhere).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
