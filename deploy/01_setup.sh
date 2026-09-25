#!/usr/bin/env bash
# Phase 3, step 1 (once per project): APIs, the Cloud Storage bucket, the service account and its roles, the
# Artifact Registry repository and the secrets.
#
#   PROJECT_ID=velox-demo-123 bash deploy/01_setup.sh
#
# Secrets: APP_PASSWORD (basic auth), IMAP_PASSWORD (AP mailbox) and IMAP2_PASSWORD (store mailbox, optional)
# are taken from the environment when set, otherwise asked for without echo. An empty IMAP2_PASSWORD skips the
# second mailbox. Re-running is safe: existing resources are kept and a secret gets a new version.
#
# NOT TESTED against Google Cloud (written offline). Needs: gcloud CLI logged in (`gcloud auth login`) as a
# project Owner (or Editor + Project IAM Admin + Secret Manager Admin), billing enabled on the project.
set -euo pipefail
source "$(dirname "$0")/env.sh"

echo "== 1/5 Enable the APIs"
gcloud services enable \
  run.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  bigquery.googleapis.com \
  iam.googleapis.com

echo "== 2/5 Bucket gs://${BUCKET} (inbound documents, extraction cache, KPI export)"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

echo "== 3/5 Service account ${SA_EMAIL} and its roles"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" --display-name="Velox P2P simulator (Cloud Run)"
fi
member="serviceAccount:${SA_EMAIL}"
# Gemini on Vertex AI (generateContent, models.list).
gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${member}" \
  --role="roles/aiplatform.user" --condition=None >/dev/null
# BigQuery export: replace the tables (dataEditor) and run the load jobs (jobUser). Tighter alternative:
# grant dataEditor on the dataset only, after 04_bigquery.sh has created it.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${member}" \
  --role="roles/bigquery.dataEditor" --condition=None >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${member}" \
  --role="roles/bigquery.jobUser" --condition=None >/dev/null
# The bucket only (not the whole project): read and write objects through the volume mount.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="${member}" \
  --role="roles/storage.objectAdmin" >/dev/null

echo "== 4/5 Artifact Registry repository ${AR_REPO} (${REGION})"
if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Velox P2P simulator images"
fi

echo "== 5/5 Secrets (Secret Manager); the service account may read each one"

ask() {  # ask NAME: the value of $NAME if set (even empty), else a hidden prompt on the terminal
  local name="$1"
  if [ -n "${!name+x}" ]; then
    printf '%s' "${!name}"
  else
    local value
    read -r -s -p "${name}: " value </dev/tty
    echo >&2
    printf '%s' "${value}"
  fi
}

put_secret() {  # put_secret NAME VALUE: create the secret if needed, add VALUE as a new version, grant access
  local name="$1" value="$2"
  if ! gcloud secrets describe "${name}" >/dev/null 2>&1; then
    gcloud secrets create "${name}" --replication-policy=automatic
  fi
  # printf, not echo: no trailing newline in the secret.
  printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=-
  gcloud secrets add-iam-policy-binding "${name}" --member="${member}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}

app_password="$(ask APP_PASSWORD)"
[ -n "${app_password}" ] || { echo "APP_PASSWORD must not be empty (it protects the public URL)" >&2; exit 1; }
put_secret APP_PASSWORD "${app_password}"

imap_password="$(ask IMAP_PASSWORD)"
[ -n "${imap_password}" ] || { echo "IMAP_PASSWORD must not be empty (the AP mailbox's app password)" >&2; exit 1; }
put_secret IMAP_PASSWORD "${imap_password}"

imap2_password="$(ask IMAP2_PASSWORD)"
if [ -n "${imap2_password}" ]; then
  put_secret IMAP2_PASSWORD "${imap2_password}"
else
  echo "IMAP2_PASSWORD empty: no second (store) mailbox"
fi

echo
echo "Done. Next: bash deploy/02_deploy_app.sh"
