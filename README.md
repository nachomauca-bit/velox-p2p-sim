# Velox P2P control-gate simulator

A small, self-contained demo that simulates the redesigned Accounts Payable (procure-to-pay) invoice process of a fictional retailer, Velox Retail. It shows how the same 14 supplier documents (the brief's 12 plus two clean, everyday invoices) behave in two scenarios: **A (as-is)**, with a dirty vendor master, weak PO discipline and two intake mailboxes, and **B (to-be)**, with a clean master, POs or contracts for most spend, one intake channel and a control gate. A Gemini model reads the documents (structured output with per-field confidence); everything else is deterministic rules. It is **not an ERP**: the "ERP" is a handful of read-only mock tables modelled on Dynamics 365 Finance concepts. There are no ledgers, payments, users or currency conversion. All companies, people and identifiers are fictional, and durations are simulated (see [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md)).

## Quick start

Requires Python 3.11+ and, optionally, GNU make. On Windows, `make setup` uses the Python launcher `py -3`. Everything runs locally: one process, one SQLite file, no Docker.

```
make setup      # create .venv and install requirements.txt
make seed       # generate the sample documents, (re)create the DB, seed both scenarios, load both inboxes
make extract    # Gemini extraction of the 14 PDFs (cached; needs a key, see below; DATASET=v2 for test set v2)
make validate   # compare the cached Gemini extractions with the ground truth and the goldens (no API call)
make run        # serve the app on http://127.0.0.1:8010
make test       # unit tests; never call the Gemini API
```

`make pdfs` regenerates only the sample documents (the 14 case PDFs and test set v2); `make run PORT=8080` changes the port.

Without make (Windows, from the repository folder):

```
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app.invoices_gen --all
.venv\Scripts\python.exe -m app.seed
.venv\Scripts\python.exe -m app.extract
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --reload-dir app --port 8010
.venv\Scripts\python.exe -m pytest
```

On Linux or macOS, use `python3 -m venv .venv` and `.venv/bin/python` instead.

## Gemini setup

1. Copy `.env.example` to `.env`.
2. Set `GEMINI_API_KEY` to a Google AI Studio key (https://aistudio.google.com/apikey).
3. `GEMINI_MODEL` defaults to `gemini-2.5-flash`. Google now limits 2.5 Flash to existing users. If the configured model is unavailable, extraction automatically falls back to the newest GA (non-preview) Flash model your key can use, and logs which one it used. To pin another GA Flash model, change that one line.
4. Run `make extract` (or `python -m app.extract`). It extracts every PDF not yet in the cache and copies the results into both inboxes. `make extract FORCE=1` (`--force-extract`) calls the API again for every PDF. `--only 1,5,12` limits the run to some documents.

Results are cached in `data/cache/<sha256 of the PDF>.json`. The repository ships the real Gemini extractions of all 38 sample PDFs and the drafts of the four to-be owner messages, so the demo runs with real model output and without a key. `make seed` and re-runs read the cache and never call the API, so the demo works offline after the first extraction. "Load sample documents" calls the API only for PDFs not yet in the cache (and only when `GEMINI_API_KEY` or the Vertex backend is configured); "Force re-extract" on an invoice page always calls it. Latency and token usage are printed per call. The app reads `.env` at start-up: restart `make run` after adding the key.

### Without an API key: `EXTRACTOR=fixture`

Set `EXTRACTOR=fixture` in `.env` to use the ground-truth JSON in `tests/fixtures/` instead of Gemini. The UI labels these results "fixture (ground truth, no API call)", and they are never presented as Gemini output. The tests always run in this mode (`tests/conftest.py`). To rebuild the fixtures from `app/world.py`, run `python -m tests.make_fixtures`.

## Using the app

- **Load sample documents** offers two sets: the **14 case documents** (the demo) and **test set v2** (26 harder documents in four languages, see [docs/TEST_SET_V2.md](docs/TEST_SET_V2.md)). Load the same set in both scenarios before comparing.
- Pick a scenario in the header (**A — As-is** / **B — To-be**) and press **Run scenario**: every document goes through the gate, with a visible step log. **Reset** restores the scenario (documents loaded, nothing processed).
- **Exception cockpit**: to-be exceptions grouped by type, each with an owner and an SLA; as-is shows a single untracked "email loop" bucket.
- **KPIs**: upstream process health and downstream automation efficiency, with the formula of every KPI.
- **Compare**: A vs B for the same sample documents (14 case documents or the 26 of test set v2; documents received live by email are left out). **Run both scenarios** fills it in one click.
- Every invoice page shows the received document (PDF, UBL XML or email text), the extracted fields with confidence, the gate trace, the outcome and (to-be exceptions, with a key) a two-sentence message to the owner drafted by Gemini.
- Any page accepts `?scenario=asis` or `?scenario=tobe`, handy for links and screenshots.
- To let someone else click through the demo, open a Cloudflare Tunnel to your machine behind the app password: [docs/SHARE_DEMO.md](docs/SHARE_DEMO.md).
- The 3-minute demo script is in [docs/DEMO.md](docs/DEMO.md); screenshots are in [docs/img](docs/img) (taken with `EXTRACTOR=fixture`, because no Gemini key was available: the invoice page labels the fields as fixture data).

## Phase 3: real intake and Google Cloud

Built and tested offline; nothing has run on Google Cloud yet (no project was available). Step-by-step guide and the Phase-3 acceptance test: [docs/DEPLOY_GCP.md](docs/DEPLOY_GCP.md).

- **Real intake**: `POST /intake/webhook` accepts a PDF, a Peppol UBL e-invoice (XML, read by `app/ubl.py` without any model call) or an email whose invoice is only in the body (registered as unknown, routed to human review). It is idempotent by message ID and runs the gate on arrival, so the document shows in the cockpit at once. `make intake` (`python -m app.intake_imap --once`, or `--loop 60`) reads new messages from one or two IMAP mailboxes (`IMAP_*`, `IMAP2_*` in `.env`) and posts them to the webhook; on Cloud Run it is a job triggered every minute by Cloud Scheduler. A message is marked read only after the webhook accepted it; a permanent rejection (e.g. an attachment over 10 MB) is logged and not retried. `IMAP_SINCE` and `IMAP_ALLOWED_SENDERS` limit what is picked up. Files received by email are served back as sandboxed plain text (XML, email text) or as the PDF.
- **Vertex AI**: `GEMINI_BACKEND=vertex` with `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` switches the same SDK and the same extraction code to Vertex AI (service-account credentials on Cloud Run).
- **Cloud Run**: `Dockerfile` (one worker, single instance: SQLite), `make docker-build` / `make docker-run`, `deploy/01_setup.sh` … `04_bigquery.sh`. Inbound documents, the extraction cache and the export live on a mounted Cloud Storage bucket (`INBOUND_DIR`, `CACHE_DIR`, `EXPORT_DIR`). `APP_PASSWORD` puts basic auth in front of every page except `/health`.
- **KPIs outside the app**: `make export` writes one NDJSON file per table to `data/export/`; with `BQ_EXPORT=1` it also loads them into BigQuery (`requirements-gcp.txt`), after every scenario run as well. The guide explains a Looker Studio report on top.

## Project layout

```
velox-p2p-sim/
  app/
    main.py            FastAPI app and routes, incl. POST /intake/webhook (real intake)
    config.py          settings from .env / environment (model backend, paths, auth, BigQuery export)
    db.py              engine, session, init
    models.py          SQLAlchemy models: mock ERP tables + simulator tables
    world.py           single source of truth: entities, suppliers, accounts, contracts, POs, receipts, the 14 documents
    world_v2.py        test set v2: 26 documents (docs/TEST_SET_V2.md)
    seed.py            seeds to-be from world.py and derives as-is with rules D1-D6
    normalize.py       name / VAT / IBAN / invoice-number normalisation and name similarity
    invoices_gen.py    generates the sample documents (reportlab; scans rasterised with pypdfium2)
    extract.py         Gemini extraction, disk cache, fixture mode, CLI
    sim.py             business-day calendar and simulated durations
    gate.py            control gate: scenario-aware rules (SCENARIOS), step trace, postings
    taxonomy.py        exception types, owners, SLAs
    metrics.py         KPIs and the A vs B comparison
    drafts.py          Gemini-drafted two-sentence messages to exception owners (cached)
    ubl.py             Peppol UBL e-invoice parser and renderer (no model call)
    intake_imap.py     IMAP poller that posts new messages to the webhook
    auth.py            basic auth middleware (APP_PASSWORD)
    export_bq.py       NDJSON / BigQuery export of the tables and gate decisions
    templates/         Jinja2 pages
    static/
  data/
    invoices/          the 14 case PDFs; inbound/ = documents received by the webhook (local default)
    invoices_v2/       test set v2 (PDFs, scans, UBL XML, email body)
    cache/             extraction cache (one JSON per file hash)
    velox.db           SQLite database (created by make seed)
  tests/               pytest suite
    fixtures/          ground-truth extraction per PDF (used instead of the API); fixtures_v2/ for test set v2
    make_fixtures.py   rebuilds the fixtures from app/world.py
    golden.yaml        expected gate result per document and scenario, and the scenario KPIs; golden_v2.yaml for v2
  docs/
    ASSUMPTIONS.md     every simulated number, with the reason (also shown in the app)
    DEMO.md            3-minute demo script
    TEST_SET_V2.md     the 26 documents of test set v2 and their expected results
    DEPLOY_GCP.md      Google Cloud deployment and the Phase-3 acceptance test
    img/               screenshots
  deploy/              gcloud scripts: setup, Cloud Run app, intake job, BigQuery
  Dockerfile
  .env.example         every setting, documented
  Makefile
  requirements.txt     (requirements-gcp.txt: BigQuery client for the export)
```

## Status

**Phase 1** (brief build order, steps 1–6) and **Phase 2** (steps 7–9) are built; **Phase 3** is built and tested offline (see above).

- Seed data for both scenarios: the clean world (16 vendor accounts for 12 suppliers, 14 POs, 11 receipt lines, 4 contract rows) and the dirty world derived by rules D1–D6 (28 accounts for 12 suppliers, a ratio of 2.3; 6 of 14 POs).
- 14 sample PDFs, generated deterministically: the brief's 12 plus SecureNet (clean PO) and Harbor Freight (recurring contract), added so that the sample is not made of difficult cases only (docs/ASSUMPTIONS.md, section 11).
- Gemini extraction with structured output, per-field confidence, disk cache and automatic model fallback.
- Control gate for both scenarios (register, extract with confidence gate, resolve vendor, legal entity, duplicate, credit note, commitment match, terms, post), exception taxonomy with owners and SLAs, exception cockpit, KPIs, compare page, run log, reset.
- Results with the 14 documents (`tests/golden.yaml`): touchless 35.7% (A) vs 71.4% (B); 9 untracked email-loop documents vs 4 exceptions with an owner and an SLA; 2 duplicate postings of 1 invoice vs a blocked duplicate; 1 unapplied credit note vs an applied one; 2 wrong-entity postings vs 0; simulated cash leakage 29,646.00 EUR vs 0.00; cycle 13.1 vs 0.5 days on the sample average, 25 vs 3 days on the reference path "non-PO invoice sent to a store".
- Test set v2 (`tests/golden_v2.yaml`): touchless 23.1% (A) vs 53.8% (B); 19 untracked email-loop documents vs 12 exceptions or human reviews with an owner and an SLA; 4 wrong-entity postings vs 0; a statement posted as an invoice vs routed as "not an invoice"; cash leakage 56,765.00 EUR vs 0.00. It is a stress set: most documents are designed to hit a rule.
- Unit, golden and UI tests that never call the API or Google Cloud.

**Validated against the live Gemini API on 25 Sep 2026** ([docs/LIVE_VALIDATION.md](docs/LIVE_VALIDATION.md), `make validate`): gemini-3.8-flash read all 38 sample PDFs (German, French, Spanish and English, two scans included) with **684 of 684 fields correct**, no critical field below the 0.80 confidence threshold, a median of 5 s per document and a total cost of about 0.20 USD. Run on these real extractions, the control gate gives the expected result for all 28 case-document results and for 51 of the 52 test-set-v2 results; the one difference is explained in the report (the degraded scan, document 13, was read correctly and confidently, so to-be posts it instead of sending it to human review). `tests/test_validate_live.py` re-checks this on every test run, from the cache. The Gemini owner-message drafts were also checked live (B-05, B-06, B-08, B-12).

**Not yet run on Google Cloud** (no project yet): Vertex AI, Cloud Run, Cloud Storage, BigQuery and real mailboxes are covered by unit tests with fakes; `docs/DEPLOY_GCP.md` lists the steps and the acceptance test.

## Disclaimer

Every page shows this footer:

> Velox P2P control-gate simulator. Mock ERP tables modelled on Dynamics 365 Finance concepts. Document understanding by a Gemini model via the Google GenAI SDK. Durations are simulated (see Assumptions).
