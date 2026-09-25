#!/usr/bin/env bash
# Phase 3, step 3: real intake. A Cloud Run job (same image) runs the IMAP poller once
# (python -m app.intake_imap --once), and Cloud Scheduler starts it every minute: a new email is registered
# within about two minutes. The poller posts each unseen message to POST <service>/intake/webhook with the
# basic-auth credentials; the webhook deduplicates on the message id, so overlapping runs are harmless.
#
#   PROJECT_ID=velox-demo-123 IMAP_USER=velox.ap.demo@gmail.com bash deploy/03_deploy_intake_job.sh
#   (second mailbox, optional: IMAP2_USER=velox.store.demo@gmail.com; needs the IMAP2_PASSWORD secret)
#
# Options (environment): IMAP_HOST / IMAP2_HOST (default imap.gmail.com), IMAP_FOLDER / IMAP2_FOLDER (INBOX),
#   INTAKE_SCENARIO (tobe | asis, default tobe), SCHEDULE (cron, default every minute).
#
# Pause the polling between demos (a run every minute is billed):
#   gcloud scheduler jobs pause  velox-intake-every-minute --location=<REGION>
#   gcloud scheduler jobs resume velox-intake-every-minute --location=<REGION>
# Run one pass by hand:
#   gcloud run jobs execute velox-intake --region=<REGION> --wait
#
# Alternative without the job and the scheduler: a local n8n workflow (IMAP Email trigger -> HTTP Request,
# POST multipart/form-data to <service>/intake/webhook with basic auth and the fields file (binary),
# email_body, channel (ap_mailbox | store_mailbox), sender, subject, message_id, scenario). See
# docs/DEPLOY_GCP.md.
#
# NOT TESTED against Google Cloud (written offline).
set -euo pipefail
source "$(dirname "$0")/env.sh"

: "${IMAP_USER:?set IMAP_USER to the address of the mailbox that plays ap@velox.com}"
IMAP_HOST="${IMAP_HOST:-imap.gmail.com}"
IMAP_FOLDER="${IMAP_FOLDER:-INBOX}"
IMAP2_HOST="${IMAP2_HOST:-imap.gmail.com}"
IMAP2_FOLDER="${IMAP2_FOLDER:-INBOX}"
INTAKE_SCENARIO="${INTAKE_SCENARIO:-tobe}"
SCHEDULE="${SCHEDULE:-* * * * *}"

service_url="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"
[ -n "${service_url}" ] || { echo "service ${SERVICE} not found: run 02_deploy_app.sh first" >&2; exit 1; }

env_vars=(
  "INTAKE_WEBHOOK_URL=${service_url}/intake/webhook"
  "INTAKE_SCENARIO=${INTAKE_SCENARIO}"
  "APP_USERNAME=${APP_USERNAME}"
  "IMAP_HOST=${IMAP_HOST}" "IMAP_PORT=993" "IMAP_USER=${IMAP_USER}" "IMAP_FOLDER=${IMAP_FOLDER}"
  "IMAP_CHANNEL=ap_mailbox"
)
secrets=("APP_PASSWORD=APP_PASSWORD:latest" "IMAP_PASSWORD=IMAP_PASSWORD:latest")
if [ -n "${IMAP2_USER:-}" ]; then
  env_vars+=("IMAP2_HOST=${IMAP2_HOST}" "IMAP2_PORT=993" "IMAP2_USER=${IMAP2_USER}" "IMAP2_FOLDER=${IMAP2_FOLDER}"
             "IMAP2_CHANNEL=store_mailbox")
  secrets+=("IMAP2_PASSWORD=IMAP2_PASSWORD:latest")
fi
env_list="$(IFS=,; echo "${env_vars[*]}")"
secret_list="$(IFS=,; echo "${secrets[*]}")"

echo "== Cloud Run job ${JOB}: one IMAP pass per execution"
# --task-timeout 600s   the webhook may take up to ~3 minutes per message (model call + gate)
# --max-retries 0       a failed message stays unseen and is retried by the next scheduled run
gcloud run jobs deploy "${JOB}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --command=python \
  --args="-m,app.intake_imap,--once" \
  --set-env-vars="${env_list}" \
  --set-secrets="${secret_list}" \
  --tasks=1 \
  --max-retries=0 \
  --task-timeout=600s \
  --cpu=1 \
  --memory=512Mi

echo "== Allow the service account to start the job (Cloud Scheduler calls the Cloud Run Admin API as it)"
gcloud run jobs add-iam-policy-binding "${JOB}" --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/run.invoker" >/dev/null

echo "== Cloud Scheduler ${SCHEDULER_JOB}: '${SCHEDULE}'"
run_uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "${SCHEDULER_JOB}" --location="${REGION}" >/dev/null 2>&1; then
  action=update
else
  action=create
fi
gcloud scheduler jobs "${action}" http "${SCHEDULER_JOB}" \
  --location="${REGION}" \
  --schedule="${SCHEDULE}" \
  --time-zone="Etc/UTC" \
  --uri="${run_uri}" \
  --http-method=POST \
  --oauth-service-account-email="${SA_EMAIL}"

echo
echo "Done. Send an email with a PDF to ${IMAP_USER}; within about two minutes it is in the inbox of the app."
echo "Job runs:  gcloud run jobs executions list --job=${JOB} --region=${REGION} --limit=5"
echo "Job logs:  gcloud logging read 'resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB}\"' --limit=50"
