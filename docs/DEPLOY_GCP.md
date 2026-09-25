# Running the simulator on Google Cloud (phase 3)

The same app, with the same logic, on Google Cloud: a Gemini model on **Vertex AI**, the app on **Cloud Run**, inbound
documents and the extraction cache on **Cloud Storage**, passwords in **Secret Manager**, a real mailbox polled every
minute, and (optionally) the KPIs in **BigQuery** and **Looker Studio**. It does not include Gemini Enterprise, agents
or a Dynamics 365 connector (brief section 17).

> **Status: written and tested offline only.** Nothing on this page has been run against Google Cloud yet (there was
> no GCP project or key when it was written). What was checked offline, and what was not, is listed at the end
> ([What was and was not tested](#what-was-and-was-not-tested)). Expect to fix small things on the first real deploy.

## What runs where

```
 Gmail (ap@ / store mailbox)                         Google Cloud project
 ─────────────────────────                           ───────────────────────────────────────────────────────────────
  email + PDF / UBL XML  ──IMAP──►  Cloud Run job "velox-intake"   (python -m app.intake_imap --once)
                                    ▲ started every minute by Cloud Scheduler
                                    │ POST /intake/webhook (basic auth)
                                    ▼
  browser ──https + basic auth──►  Cloud Run service "velox-p2p-sim"  (one container, one uvicorn worker,
                                    min = max = 1 instance, SQLite in /tmp)
                                    ├─ Gemini on Vertex AI (service account credentials, no API key)
                                    ├─ /mnt/gcs = Cloud Storage bucket: inbound/, cache/, export/
                                    ├─ Secret Manager: APP_PASSWORD (IMAP passwords go to the job)
                                    └─ after each run, emailed document, re-run, reset (optional):
                                       NDJSON → BigQuery dataset velox_p2p ──► Looker Studio report
```

- **One config switch for the model**: `GEMINI_BACKEND=vertex` with `GOOGLE_CLOUD_PROJECT` and
  `GOOGLE_CLOUD_LOCATION`. The extraction code is unchanged: `extract._client()` creates
  `genai.Client(vertexai=True, project=..., location=...)` instead of `genai.Client(api_key=...)`. Same schema,
  cache, retry and GA-Flash fallback (the fallback also reads Vertex AI's `publishers/google/models/...` names).
- **UBL e-invoices** (`.xml`) are parsed by `app/ubl.py` with no model call; **emails without an attachment** are
  registered as `unknown` and routed to human review.
- **Basic auth** (`app/auth.py`) protects every page and the webhook when `APP_PASSWORD` is set; `/health` stays open.

## Prerequisites

From the brief (section 17), to be provided by you:

1. A **GCP project with billing enabled** (the free-trial credits are enough) and an account with **Owner** on it.
   Without Owner: Editor + Project IAM Admin + Secret Manager Admin + **Cloud Run Admin** (`roles/run.admin`);
   Editor alone cannot make the service public (`--allow-unauthenticated`) or let the scheduler start the job.
2. The **Vertex AI API** enabled — `deploy/01_setup.sh` enables it with the other APIs.
3. **Two mailboxes with IMAP**: one plays `ap@velox.com`, one plays `store.berlin01@velox.com` (optional). For Gmail:
   turn on 2-Step Verification, create an **app password** (Google Account → Security → App passwords), and check
   that IMAP is enabled (Gmail settings → Forwarding and POP/IMAP).

On your machine: the `gcloud` CLI (includes `bq`), logged in with `gcloud auth login`; bash (Git Bash on Windows);
the project's Python environment (`make setup`) to generate and pre-extract the documents; Docker only if you want
to try the image locally (`make docker-build docker-run`).

**Windows (Git Bash):** Git Bash rewrites arguments that look like POSIX paths when it starts a Windows program such
as gcloud (`mount-path=/mnt/gcs` would arrive as `C:/Program Files/Git/mnt/gcs`). `deploy/env.sh` exempts the
affected gcloud flags (`MSYS2_ARG_CONV_EXCL`), so the scripts work unchanged. Do not set `MSYS_NO_PATHCONV=1`
yourself: it also stops the one conversion a gcloud shell launcher can need (the path of its own script). Cloud
Shell, Linux and macOS need nothing.

## Step by step

All scripts read their settings from the environment (`deploy/env.sh` lists them with their defaults: region
`europe-west1`, Vertex location `global`, bucket `<project>-velox-p2p`, service `velox-p2p-sim`, ...). They print
every step and are safe to re-run.

```bash
# 0. Locally: tests green, documents generated (the image ships them; they are never regenerated in the cloud)
make setup && make test && make pdfs

export PROJECT_ID=velox-demo-123        # your project id
export REGION=europe-west1              # Cloud Run / Scheduler / bucket region

# 1. APIs, bucket, service account + roles, Artifact Registry, secrets (asks for the passwords, hidden)
bash deploy/01_setup.sh

# 2a. Required: pre-extract the sample documents on your machine, so that no model call happens inside a web
#     request during the demo (.env settings: see "Pre-extracting the sample documents" below)
make extract && make extract DATASET=v2

# 2b. Copy the cache to the bucket, build the image with Cloud Build, deploy the Cloud Run service; prints the URL
UPLOAD_CACHE=1 bash deploy/02_deploy_app.sh   # other options: DB_ON_BUCKET=1, SKIP_BUILD=1, BQ_EXPORT=1|0

# 3. The IMAP poller as a Cloud Run job, started every minute by Cloud Scheduler
#    (first mark the messages already in the mailboxes as read: see the acceptance run)
IMAP_USER=velox.ap.demo@gmail.com IMAP2_USER=velox.store.demo@gmail.com bash deploy/03_deploy_intake_job.sh

# 4. Optional: BigQuery dataset + switch the KPI export on (restarts the service)
bash deploy/04_bigquery.sh
```

Open the URL, log in as `velox` with the `APP_PASSWORD` you chose. On an empty database the app seeds the mock ERP
at startup; load the documents from the Inbox page as usual.

**Redeploying.** `02_deploy_app.sh` applies its settings with `--update-env-vars`: the variables it does not set
keep their value on the service. So `BQ_EXPORT=1` from `04_bigquery.sh` survives a later redeploy; pass
`BQ_EXPORT=0` (or `1`) to 02 to change it explicitly. Without `BQ_EXPORT`, 02 says it keeps the current setting.

**Intake filters (optional, 03).** `IMAP_SINCE` (YYYY-MM-DD) makes the poller consider only messages received on or
after that day; `IMAP_ALLOWED_SENDERS` (comma-separated emails or @domains, e.g. `you@gmail.com,@velox.com`)
makes it ignore mail from anyone else (such messages stay unread). `IMAP2_SINCE` / `IMAP2_ALLOWED_SENDERS` do the
same for the store mailbox. 03 passes them to the job only when they are set:

```bash
IMAP_USER=velox.ap.demo@gmail.com IMAP_SINCE=2026-09-25 IMAP_ALLOWED_SENDERS=you@gmail.com \
  bash deploy/03_deploy_intake_job.sh
```

**Roles of the service account** (`velox-p2p-sim@<project>.iam.gserviceaccount.com`): `roles/aiplatform.user`
(Gemini), `roles/storage.objectAdmin` on the bucket only, `roles/secretmanager.secretAccessor` on each secret,
`roles/bigquery.dataEditor` + `roles/bigquery.jobUser` (export), `roles/run.invoker` on the intake job (Cloud
Scheduler starts the job as this account). A new service account takes a minute or more to be usable in IAM
policies: `01_setup.sh` waits for it and retries every role binding (up to 6 times, 10 s apart:
`RETRY_ATTEMPTS`, `RETRY_DELAY_S`).

### Pre-extracting the sample documents (required)

Loading a set extracts its documents inside the web request that loads it. With an empty cache that is one model
call per PDF, one after another: 14 for the case set, 24 for test set v2 (a few minutes). The service allows
900 s per request (`--timeout=900`), but a slow model or a 429 retry can still exceed it: the browser then gets a
504 while the request goes on in the background and holds the gate lock (a Run clicked meanwhile waits). So
extract the documents on your machine first and upload the cache; loading and running then make no model calls.

With **Vertex AI** (recommended: the cache records then show `"backend": "vertex"`; needs step 1, which enables
the API):

```bash
gcloud auth application-default login           # once; your user needs roles/aiplatform.user (Owner has it)
# .env: GEMINI_BACKEND=vertex, GOOGLE_CLOUD_PROJECT=velox-demo-123, GOOGLE_CLOUD_LOCATION=global
make extract                                    # the 14 case PDFs
make extract DATASET=v2                         # 24 PDFs to Gemini; the UBL XML is parsed, the email body skipped
```

Or with an **AI Studio key** (`GEMINI_API_KEY` in `.env`): the same `make extract` commands; the records then say
`"backend": "aistudio"`. Without `gcloud auth application-default login`, a Vertex AI extraction fails with
`Vertex AI credentials: DefaultCredentialsError: ...` (the rest of the batch then uses the cache only).

Step 2b (`UPLOAD_CACHE=1`) copies `data/cache` to `gs://<bucket>/cache`; 02 warns when `data/cache` is empty. To
upload a cache extracted later: `UPLOAD_CACHE=1 SKIP_BUILD=1 bash deploy/02_deploy_app.sh` (a redeploy: load the
sets again afterwards). Each cache record stores `"backend": "vertex"` (or `"aistudio"`), so you can always tell
where an extraction ran. Remove the Vertex lines from `.env` before running `make test` (the tests expect the AI
Studio default).

## Settings on Cloud Run

| Variable | Value set by `02_deploy_app.sh` | Why |
|---|---|---|
| `GEMINI_BACKEND` | `vertex` | Gemini through Vertex AI, authenticated as the service account |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | the project, `global` | where the model is called (`VERTEX_LOCATION=europe-west1` to pin a region, if the model is offered there) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | one-line change; unavailable models fall back to the newest GA Flash |
| `INBOUND_DIR`, `CACHE_DIR`, `EXPORT_DIR` | `/mnt/gcs/inbound`, `/mnt/gcs/cache`, `/mnt/gcs/export` | on the bucket: survive restarts and redeploys |
| `DATABASE_URL` | `sqlite:////tmp/velox.db` (`DB_ON_BUCKET=1`: `sqlite:////mnt/gcs/velox.db`) | see the next section |
| `APP_USERNAME` / `APP_PASSWORD` | `velox` / secret `APP_PASSWORD` | basic auth; the job sends the same pair to the webhook |
| `BQ_PROJECT`, `BQ_DATASET` | the project, `velox_p2p` | where the KPI export loads the tables |
| `BQ_EXPORT` | only when passed to 02 (`BQ_EXPORT=1` or `0`); `04_bigquery.sh` sets `1`; otherwise the current value stays (off on a first deploy) | KPI export after every scenario run, every document received through the webhook, a re-run of one document and a reset (not after loading a set alone) |

Service flags: `--execution-environment gen2` (needed for the bucket mount), `--min-instances 1 --max-instances 1`,
`--timeout 900` (the webhook waits for the model and the gate; loading a set whose PDFs are not in the cache makes
all its model calls in one request), `--allow-unauthenticated` (the URL is public; basic auth protects it). The
bucket is mounted with `uid=10001;gid=10001`, the image's non-root user.

## SQLite on Cloud Run: the caveat

The brief keeps SQLite (no Cloud SQL), so the demo is **single-instance by design**.

- **Default: the database in `/tmp`.** Cloud Run's `/tmp` is in memory: fast and safe, but it lives only as long
  as the instance. `--min-instances 1` keeps the instance (and the state) alive between requests during a demo.
  A **redeploy, a settings change (`gcloud run services update`, `04_bigquery.sh`) or a restart** starts a new
  instance from a freshly seeded ERP with empty mailboxes: load the documents again. Inbound documents, the
  extraction cache and the export are on the bucket and survive; re-running a scenario then costs no model calls.
- **Option `DB_ON_BUCKET=1`: the database on the Cloud Storage FUSE mount.** It survives restarts, but Cloud Storage
  FUSE has **no file locking** (SQLite's locks are not honoured) and rewrites the whole object on every change, so it
  is slow and safe only with exactly one writer. Cloud Run briefly runs the old and the new revision side by side
  during a deploy: do not use the app while deploying, or the file can be corrupted. Use it only if losing the state
  on a restart is a real problem.

## Phase-3 acceptance run

The acceptance criterion from the brief, step by step. The real email comes **last**: loading a set or resetting a
scenario replaces all of that scenario's documents, **including the ones received by email**, and the poller does
not bring them back (the message is already marked read).

**Before you start**

- Steps 0 to 3 are done, including the pre-extraction (2a) and `UPLOAD_CACHE=1` (2b): loading and running the sets
  then make no model calls.
- **Mark every message already in the AP mailbox (and the store mailbox) as read** (Gmail: select all, then *Mark
  as read*), ideally before step 3 starts the poller. The poller registers every *unread* message as a document, so
  old mail would otherwise appear in the Inbox (if it already did, loading the set in step 1 clears it). From then
  on **nobody opens mail in these mailboxes**: a message read before the poller sees it is never ingested. To
  narrow the poller further, deploy the job with `IMAP_SINCE` (YYYY-MM-DD, e.g. today) and `IMAP_ALLOWED_SENDERS`
  (comma-separated emails or @domains: the accounts you will send from); see step 3.
- Switch the header to **B — To-be**: the poller registers in `tobe` by default (`INTAKE_SCENARIO`).

1. **Test set v2 in both scenarios.** Load *Test set v2* into A and B from the Inbox and run both scenarios. With the
   pre-extracted cache on the bucket, loading makes no model calls (without it, loading sends the 24 v2 PDFs to
   Gemini in one request; later loads and runs use the cache). Expected results: `docs/TEST_SET_V2.md` and
   `tests/golden_v2.yaml`. Those were computed with the ground-truth fixtures: real model output can differ,
   especially on the two scans (their fixture confidences are simulated). Note the differences rather than tuning
   the prompt: the confidence gate exists for them.
2. **Comparison page** (`/gate/compare`): both scenarios side by side for the v2 set.
3. **KPI export** (if built): after each run the BigQuery tables are replaced;
   `bq query --use_legacy_sql=false 'SELECT scenario, outcome, COUNT(*) n FROM velox_p2p.gate_decision GROUP BY 1, 2'`
   matches the app's KPI page. `make export` writes the same NDJSON locally. The export also runs after every
   document received by email, a re-run of one document and a reset, so the emails of step 4 reach BigQuery too
   (with `sample_no = 0`).
4. **A real email, end to end (last).** From one of your own accounts (one listed in `IMAP_ALLOWED_SENDERS`, if you
   set it), send an email with a PDF invoice attached to the AP mailbox. Use a PDF that is **not in the cache**, so
   that the extraction really runs on Vertex AI in the cloud: any invoice PDF that is not one of the sample files
   (for example one of your own). Within about two minutes (the scheduler starts the job every minute):
   - the **Inbox** of B lists a new document `B-W..`, registered on arrival;
   - its page shows the extracted fields with confidence and the model name, the gate trace and the outcome;
   - the **Exception cockpit** lists it if it stopped for a person (an invoice from a supplier that is not in the
     vendor master does).

   Evidence that the extraction ran on Vertex AI: the service log line
   `[extract] file=... model=gemini-... cache=miss backend=vertex`, and `"backend": "vertex"` in its cache record
   `gs://<bucket>/cache/<sha256>.json`:

   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"backend=vertex"' --limit=5
   gcloud run jobs executions list --job=velox-intake --region=$REGION --limit=5      # the poller's runs
   ```

   **Duplicate block.** Then send, unchanged, a sample PDF that scenario B has already posted, for example
   `data/invoices_v2/01_nordwind_rechnung.pdf`. The new `B-W..` document is **blocked as a duplicate** of `B2-01`.
   That file is a cache hit, so its log line reads `cache=hit` and names no backend; its cache record shows where
   it was extracted (step 2a):

   ```bash
   sha=$(sha256sum data/invoices_v2/01_nordwind_rechnung.pdf | cut -d' ' -f1)
   gcloud storage cat "gs://$PROJECT_ID-velox-p2p/cache/$sha.json" | grep '"backend"'
   ```

   An email **without an attachment** (invoice text in the body) is registered as `unknown` and routed to human
   review. A **UBL e-invoice** (`.xml`) is parsed without a model call.

## Looker Studio (Data Studio) on the BigQuery tables

The export writes one table per app table (`gate_decision`, `pending_vendor_invoice`, `credit_note_application`,
`inbound_document`, `vendor_account`, `purchase_order`, `purchase_order_line`, `product_receipt`, `contract`,
`party`, `legal_entity`, `run`), both scenarios in each (column `scenario`). JSON columns (`steps`, `details`, ...)
are JSON text; `gate_decision` also carries flat columns taken from `details` (`touchless`, `supplier_name`,
`gross_total`, `currency`, `wrong_entity_posting`, `duplicate_posting`, `credit_status`, `terms_variance_paid`, ...)
and the document's `dataset` (`v1` / `v2`), so the report needs no JSON parsing. `inbound_document` also holds the
sender and the email text of messages received by the real mailbox: keep the dataset private to the demo team.

1. Create a KPI view once (BigQuery console → SQL workspace). The definitions are those of `app/metrics.py`:

   ```sql
   CREATE OR REPLACE VIEW `velox-demo-123.velox_p2p.kpi_by_scenario` AS
   SELECT
     scenario,
     dataset,
     COUNT(*)                                                  AS documents,
     COUNTIF(touchless)                                        AS touchless,
     ROUND(100 * SAFE_DIVIDE(COUNTIF(touchless), COUNT(*)), 1) AS touchless_rate_pct,
     COUNTIF(outcome IN ('exception', 'human_review'))         AS exceptions,
     COUNTIF(outcome = 'blocked_duplicate')                    AS duplicates_blocked,
     COUNTIF(credit_status = 'applied')                        AS credit_notes_applied,
     COUNTIF(credit_status = 'unapplied')                      AS credit_notes_unapplied,
     COUNTIF(wrong_entity_posting)                             AS wrong_entity_postings,
     COUNTIF(terms_variance_paid)                              AS terms_variance_paid,
     ROUND(AVG(simulated_days), 1)                             AS avg_cycle_days
   FROM `velox-demo-123.velox_p2p.gate_decision`
   GROUP BY scenario, dataset;
   ```

   The view survives every export (the tables are replaced, not renamed). Documents received by email have
   `sample_no = 0`; add `WHERE sample_no > 0` to compare with the sample runs only.
2. [lookerstudio.google.com](https://lookerstudio.google.com) → **Create → Report** → **Add data → BigQuery** →
   your project → `velox_p2p` → `kpi_by_scenario` → **Add**. Add `gate_decision` as a second data source.
3. **Scorecards** from `kpi_by_scenario`: touchless rate, exceptions, duplicates blocked, unapplied credit notes,
   wrong-entity postings, average cycle days; a **drop-down control** on `scenario` and one on `dataset`.
4. **Bar chart** from `gate_decision`: dimension `exception_type`, metric *Record Count*, filter
   `outcome` in (`exception`, `human_review`) — exceptions by type. A **table** by `owner_name` with `sla_days` gives
   the owners' queue.
5. Vendor-master quality from `vendor_account` and `party` (per scenario): accounts ÷ parties, share of accounts with
   `vat_id` and `iban`.
6. Share the report with viewers of the dataset (they need BigQuery read access, or use the owner's credentials in
   the data source settings).

## Costs (rough, check the current price lists)

With the free-trial credits none of this is a concern; for a longer-running project:

- **Cloud Run service**: `--min-instances 1` keeps one 1 vCPU / 1 GiB instance idle all the time — roughly a couple of
  US cents per hour, on the order of US$ 10–20 a month. Set `--min-instances 0` or delete the service after the demo.
- **Intake job + Cloud Scheduler**: 1,440 short job runs a day, a few US$ a month; Cloud Scheduler gives a few jobs
  free per billing account. **Pause the scheduler between demos** (`gcloud scheduler jobs pause
  velox-intake-every-minute --location=$REGION`).
- **Gemini on Vertex AI** (Flash): a fraction of a US cent per document; the whole v2 set well under US$ 1. The cache
  means each file is paid once.
- **Cloud Storage, BigQuery, Secret Manager, Artifact Registry, Cloud Build**: a few MB / a few versions / one small
  image — cents, mostly within the free tiers.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Every page answers 401 | Wrong password: the browser asks for user `velox` (or `APP_USERNAME`) and the `APP_PASSWORD` secret. `/health` needs none. |
| Extraction fails with `Vertex AI credentials: DefaultCredentialsError ...` (or `RefreshError`) | No usable Application Default Credentials. On your machine (pre-extraction on Vertex AI): run `gcloud auth application-default login`. On Cloud Run the service runs as the service account (02 sets `--service-account`): a `RefreshError` there is usually a passing metadata-server problem: *Force re-extract* on the document's page. Nothing is lost: the rest of the batch uses the cache and the run goes on. |
| Extraction fails with `403 PERMISSION_DENIED ... aiplatform.endpoints.predict` | The service account lacks `roles/aiplatform.user`, or the Vertex AI API is off: re-run `01_setup.sh`. The document stays unextracted; *Force re-extract* on its page once fixed. |
| `404 ... Publisher Model ... was not found` in the log | The model is not offered in `GOOGLE_CLOUD_LOCATION`: keep `global`, or set `GEMINI_MODEL` to a model listed for your region. The app already falls back to the newest GA Flash model it can list. |
| `gcloud builds submit` fails to push the image | New projects run Cloud Build as the Compute Engine default service account: grant it `roles/artifactregistry.writer`, `roles/logging.logWriter` and `roles/storage.objectViewer`. |
| `gcloud run deploy ... --allow-unauthenticated` is refused | An organisation policy (domain-restricted sharing) blocks public services. Use a project outside the organisation, or open the app with `gcloud run services proxy velox-p2p-sim --region=$REGION`. |
| `PermissionError` writing `/mnt/gcs/...` | The mount is not owned by the container user: check the volume's `mount-options` (`uid=10001;gid=10001`) in `gcloud run services describe`. The mount needs `--execution-environment gen2`. |
| Email sent, nothing in the app | The message was opened (read) in the mailbox before the poller's pass: the poller only takes unread mail; mark it unread. The sender is not in `IMAP_ALLOWED_SENDERS`, or the mail is older than `IMAP_SINCE`. Otherwise the job log (`gcloud logging read 'resource.type="cloud_run_job"' --limit=50`): IMAP login (Gmail needs an app password and IMAP on), webhook 401 (the job's `APP_PASSWORD` differs), `REJECTED` (the webhook refused it for good, e.g. a bad sender or file: the message is marked read and not retried). The header must show scenario B (the poller posts to `INTAKE_SCENARIO`). |
| The Inbox fills with old emails | The mailbox had unread messages when the poller first ran. Mark them as read before deploying 03, or deploy it with `IMAP_SINCE` / `IMAP_ALLOWED_SENDERS`; *Load* or *Reset* the scenario to clear the documents. |
| Loading a set ends in a `504` / `upstream request timeout` | The documents were not pre-extracted, so every model call ran inside that request (step 2a). The request may go on in the background and holds the gate lock until it ends. Pre-extract, upload the cache (`UPLOAD_CACHE=1 SKIP_BUILD=1 bash deploy/02_deploy_app.sh`) and load again. |
| Scheduler runs fail with 401/403 | The scheduler must call the job with an **OAuth** token of the service account, which needs `roles/run.invoker` on the job (03 grants it). |
| The app forgot the loaded documents | A new revision or a restart with the database in `/tmp` (see the SQLite caveat). Load and run again: the cache on the bucket avoids new model calls. |
| `[export] FAILED: BigQuery load of ...` in the log | Dataset missing (`04_bigquery.sh`), or the service account lacks `roles/bigquery.dataEditor` / `jobUser`. The NDJSON files are in `gs://<bucket>/export/` anyway. |
| The BigQuery tables are behind the app | The export runs after a scenario run, an emailed document, a re-run and a reset, not after loading a set alone. It needs `BQ_EXPORT=1` on the service: `bash deploy/04_bigquery.sh` sets it, and later runs of 02 keep it unless you pass `BQ_EXPORT=0`. |

## Pausing and removing everything

These commands delete data and cannot be undone; run them yourself when the demo is over (or delete the project):

```bash
gcloud scheduler jobs delete velox-intake-every-minute --location=$REGION
gcloud run jobs delete velox-intake --region=$REGION
gcloud run services delete velox-p2p-sim --region=$REGION
bq rm -r -d $PROJECT_ID:velox_p2p
gcloud storage rm -r gs://$PROJECT_ID-velox-p2p
gcloud secrets delete APP_PASSWORD; gcloud secrets delete IMAP_PASSWORD; gcloud secrets delete IMAP2_PASSWORD
gcloud artifacts repositories delete velox --location=$REGION
```

## Alternative intake: a local n8n workflow

Instead of the job and the scheduler (brief option b): an **IMAP Email** trigger node (the mailbox, *Download
attachments* on) followed by an **HTTP Request** node: `POST https://<service-url>/intake/webhook`, basic auth
(`velox` / `APP_PASSWORD`), body *multipart/form-data* with `file` (the binary attachment; omit it when there is none),
`email_body` (the email text; required without a file), `channel` (`ap_mailbox` or `store_mailbox`), `sender`,
`subject`, `message_id` (the webhook ignores a message it already has) and optionally `scenario`.

## What was and was not tested

**Tested offline, part of `make test`** (`tests/test_cloud.py`, `tests/test_export_bq.py`, `tests/test_extract.py`,
`tests/test_deploy.py`):

- settings: defaults, environment overrides (read in a child process), `gemini_configured()` for both backends;
- the Vertex AI client: arguments (`vertexai=True`, project, location, no API key), a real SDK client object, and a
  full fallback round trip through the installed google-genai SDK on an in-process mock transport (request path
  `projects/<p>/locations/<l>/publishers/google/models/...`, bearer token, the publisher model listing);
- Vertex AI credentials: missing ADC, a token that cannot be refreshed and a malformed key file end as a failed
  extraction (`Vertex AI credentials: ...`), and a batch then goes on from the cache;
- routing: `.xml` → UBL parser (no model, no cache, also in fixture mode), `.txt` → "no document attached", v2 fixtures;
- basic auth on a small FastAPI app (401 / 200, realm, `/health` exempt, constant-time compare of both parts);
- the export: NDJSON of a seeded database after gate runs (types, JSON columns, flat fields), and the BigQuery load
  path with a fake client and module (WRITE_TRUNCATE, schemas, empty tables, errors);
- the deploy scripts (`tests/test_deploy.py`; skipped where no bash is found, on Windows it uses Git for Windows'
  bash): `bash -n` on every script, LF line endings, and a dry run of 01 to 04 with stand-in `gcloud` / `bq` /
  `gsutil` commands that record their arguments and stdin. The stand-ins start a native Python the way a gcloud
  shell launcher can, so under Git Bash they get the arguments gcloud would get: the tests check the key flags (`--timeout=900`,
  the volume and its mount path, `--update-env-vars` and the `BQ_EXPORT` rule, the job's `^|^` variable list with
  `IMAP_ALLOWED_SENDERS`, the scheduler's URI and OAuth account), that `/mnt/gcs` and `sqlite:////tmp/velox.db`
  arrive unconverted, the wait for a new service account and the retried role bindings, and that the secrets go
  on stdin without a trailing newline and never on a command line.

**Checked by hand once while writing, not part of `make test`:** the Dockerfile with `docker build --check` (no
warnings) and a build of the same steps without `pip install` (build context without `.env`, tests, database or
cache; files owned by the non-root user).

**Not tested** (needs Google Cloud or the network): the full image build with `pip install` and running the container;
Cloud Build, Artifact Registry, Cloud Run deploy (volume mount and its `uid`/`gid` options, secrets, min/max
instances); the real gcloud / bq launchers under Git Bash (the stand-ins imitate them); real Gemini calls on Vertex
AI, real Application Default Credentials and the real `models.list` response on Vertex; Cloud Scheduler starting
the job; IMAP against Gmail; BigQuery loads; Looker Studio; the costs above.
