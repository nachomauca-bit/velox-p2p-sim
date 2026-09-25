#!/usr/bin/env bash
# Phase 3, step 4 (optional): KPIs outside the app. Creates the BigQuery dataset and switches the service's
# export on: after every scenario run, every document received through the intake webhook, a re-run of one
# document and a reset, the app writes NDJSON to the bucket and loads each table into ${BQ_DATASET}
# (WRITE_TRUNCATE: each export replaces the tables). Loading a set alone does not export. Looker Studio reads
# those tables (docs/DEPLOY_GCP.md, "Looker Studio"). The setting survives later runs of 02_deploy_app.sh
# (BQ_EXPORT=0 there switches it off again).
#
#   PROJECT_ID=velox-demo-123 bash deploy/04_bigquery.sh
#
# Options (environment):
#   LOAD_LOCAL_EXPORT=1   also load a local export (make export -> data/export/*.ndjson) with `bq load`,
#                         without the app, e.g. to build the report before the service is deployed
#   SKIP_SERVICE_UPDATE=1 only create the dataset (the service update below restarts the instance: with the
#                         database in /tmp, the app starts again from a freshly seeded ERP)
#
# NOT TESTED against Google Cloud (written offline).
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "${REPO_ROOT}"

echo "== Dataset ${PROJECT_ID}:${BQ_DATASET} (${BQ_LOCATION})"
if ! bq --project_id="${PROJECT_ID}" show --dataset "${PROJECT_ID}:${BQ_DATASET}" >/dev/null 2>&1; then
  bq --project_id="${PROJECT_ID}" --location="${BQ_LOCATION}" mk --dataset \
    --description="Velox P2P simulator: gate decisions and mock ERP tables (replaced on every export)" \
    "${PROJECT_ID}:${BQ_DATASET}"
fi

if [ "${LOAD_LOCAL_EXPORT:-0}" = "1" ]; then
  echo "== Load the local export (data/export) into ${BQ_DATASET}"
  for file in data/export/*.ndjson; do
    table="$(basename "${file}" .ndjson)"
    if [ ! -s "${file}" ]; then
      echo "   ${table}: empty, skipped"
      continue
    fi
    bq --project_id="${PROJECT_ID}" load --replace --source_format=NEWLINE_DELIMITED_JSON \
      "${PROJECT_ID}:${BQ_DATASET}.${table}" "${file}" "data/export/${table}.schema.json"
  done
fi

if [ "${SKIP_SERVICE_UPDATE:-0}" != "1" ]; then
  echo "== Switch the export on in the service ${SERVICE} (restarts it: a new revision)"
  gcloud run services update "${SERVICE}" --region="${REGION}" \
    --update-env-vars="BQ_EXPORT=1,BQ_PROJECT=${PROJECT_ID},BQ_DATASET=${BQ_DATASET}"
fi

echo
echo "Done. After the next scenario run:"
echo "  bq query --use_legacy_sql=false 'SELECT scenario, outcome, COUNT(*) AS n FROM \`${PROJECT_ID}.${BQ_DATASET}.gate_decision\` GROUP BY 1, 2 ORDER BY 1, 2'"
