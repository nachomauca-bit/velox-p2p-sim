# Velox P2P control-gate simulator

A small, self-contained demo of the redesigned accounts payable (procure-to-pay) invoice process of a fictional retailer, Velox Retail, built for the case study's final deck. The same **12 case documents** go through two scenarios: **A (as-is)**, with duplicate vendor records, no PO discipline, two mailboxes and no gate, and **B (to-be)**, with a clean master, commitments by spend category, one intake address registered on arrival, and the **control gate**. Gemini reads and classifies each document (fields, a confidence per field, the document type); deterministic rules decide, and every decision ends in one of four outcomes: **Post · Exception · Block · Human review**.

It is **not an ERP**: the "ERP (mock, system of record)" is a handful of read-only tables. All companies, people and identifiers are fictional, and every duration is simulated (see [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md), also shown in the app).

## Quick start

Requires Python 3.11+ and, optionally, GNU make. On Windows, `make setup` uses the Python launcher `py -3`. Everything runs locally: one process, one SQLite file.

```
make setup      # create .venv and install requirements.txt
make seed       # generate the sample documents, (re)create the DB, load and run both scenarios (the demo start)
make run        # serve the app on http://127.0.0.1:8010
make test       # unit, golden and UI tests; they never call the Gemini API
make validate   # compare the cached Gemini readings with the ground truth and the goldens (no API call)
```

A fresh database starts demo-ready on its own, so `make run` alone is enough after `make setup`. `make pdfs` regenerates only the sample documents; `make run PORT=8080` changes the port.

Without make (Windows, from the repository folder):

```
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app.seed
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8010
.venv\Scripts\python.exe -m pytest
```

On Linux or macOS, use `python3 -m venv .venv` and `.venv/bin/python` instead.

## Gemini and the cache

The repository ships the real Gemini readings of every sample PDF (`data/cache/<sha256 of the PDF>.json`, read by `gemini-3.8-flash`) and the owner-message drafts of the demo (`data/cache/drafts/`). The demo therefore runs **with real model output and without a key**, and never calls the network: *Reset demo*, *Run scenario* and *Run the control gate* read the cache only. Each invoice page says where its fields come from in one line, for example "Read by gemini-3.8-flash (cached)".

- To read new documents, copy `.env.example` to `.env`, set `GEMINI_API_KEY` (Google AI Studio) and run `make extract`; it reads every PDF not yet in the cache. `GEMINI_MODEL` defaults to `gemini-2.5-flash` and falls back automatically to the newest generally available Flash model the key can use. The app reads `.env` at start-up.
- `EXTRACTOR=fixture` uses the ground-truth JSON in `tests/fixtures/` instead of Gemini; the UI then says "FIXTURE — ground-truth test data, not a Gemini output". The tests always run in this mode.

## Using the app

- The header has **Run scenario** (re-runs the scenario on screen, offline) and **Reset demo** (both scenarios back to the start of the demo). The six stages of the deck run across the top of every page: Buy · Set up the supplier · Receive or confirm · Invoice arrives · Control gate · Resolve, post and pay.
- **Inbox**: to-be has one intake address, and each card shows "Registered B-05 · timestamp · ap@velox.com · clock started". *Receive next email* delivers the demo's held-back invoice.
- **Invoice page**: the received document, the **Gemini output** (fields with confidence, document type), the **rule log** (`Rule → result — reason`, one line per rule) and the **outcome** (type, owner, SLA). For an Exception or a Human review, *Draft message to owner* shows two sentences drafted by Gemini, labelled "Draft by Gemini — reviewed by AP"; nothing is sent.
- **Rule log**: the whole run, line by line. **Exception cockpit**: open exceptions by type, with owner, SLA, days open and a past-SLA flag; blocked duplicates apart. **Metrics** and **Compare**: the four metrics of the deck, A vs B.
- ERP (mock): **Posted invoices**, **Vendor master**, **Purchase orders & receipts**, **Contracts & catalogues**.
- Any page accepts `?scenario=asis` or `?scenario=tobe`.
- The four-minute demo script is in [docs/DEMO.md](docs/DEMO.md). To let someone else click through it, see [docs/SHARE_DEMO.md](docs/SHARE_DEMO.md).

## Results (the 12 case documents, `tests/golden.yaml`)

| Metric | A — As-is | B — To-be |
|---|---|---|
| First-pass match rate | 27.3% | 72.7% |
| Accounts per supplier | 2.33 | 1.17 |
| Touchless rate | 0% | 72.7% |
| Invoice cycle time (business days, median, simulated) | 15 | 0 (same day) |
| Registered same day | 0% | 100% |

In B, 8 invoices are posted with no human step, 2 are exceptions with a named owner and an SLA (one of them past its SLA in the cockpit), 1 is a Human review (above the approval limit) and 1 is blocked as a duplicate. In A, AP keys every invoice: 4 are posted and 8 go into the untracked email loop, where the reminder is posted a second time, the credit note stays unapplied and one invoice is posted to the wrong legal entity. The values are for all twelve, after the held-back invoice has been received (docs/DEMO.md).

## Status

- **Built**: seed data for both scenarios (to-be: 14 vendor records for 12 suppliers, 12 POs, 11 receipt lines, 4 contract rows, 1 catalogue; as-is derived by rules D1–D6: 28 records, 6 of 12 POs); the 12 case PDFs, generated deterministically; Gemini reading with structured output, per-field confidence and a disk cache; the control gate for both scenarios with the deck's exception types (A3), four outcomes and the rule log; the exception cockpit, metrics (A6) and comparison; the demo's Reset and Receive next email.
- **Tested**: 1,400+ unit, golden and UI tests that never call the API. The cached real readings are checked on every test run (`tests/test_validate_live.py`): all fields of the case documents match the ground truth, and the gate gives the expected result for all 24 case-document results. Report: [docs/LIVE_VALIDATION.md](docs/LIVE_VALIDATION.md).
- **Test set v2** (26 harder documents in four languages, scans, a UBL e-invoice, an email-body invoice; [docs/TEST_SET_V2.md](docs/TEST_SET_V2.md)) stays available from the command line (`python -m app.seed --dataset v2`) and in the tests.
- **Phase 3** (Google Cloud, a real mailbox) follows after the upload: the code is built and tested offline with fakes; see [docs/PHASE3.md](docs/PHASE3.md) and [docs/DEPLOY_GCP.md](docs/DEPLOY_GCP.md).

## Project layout

```
velox-p2p-sim/
  app/
    main.py            FastAPI app and routes
    config.py          settings from .env / environment
    db.py, models.py   SQLite engine and SQLAlchemy models: mock ERP tables + simulator tables
    world.py           single source of truth: entities, suppliers, records, POs, receipts, contracts, catalogue,
                       approval matrix, the 12 case documents
    world_v2.py        test set v2: 26 documents
    seed.py            seeds to-be from world.py, derives as-is with rules D1-D6, Reset demo
    normalize.py       name / tax ID / IBAN / invoice-number normalisation and name similarity
    invoices_gen.py    generates the sample documents (reportlab; scans rasterised with pypdfium2)
    extract.py         Gemini reading, disk cache, fixture mode, CLI
    sim.py             business-day calendar and simulated durations
    gate.py            control gate: scenario-aware rules, rule log, postings
    taxonomy.py        exception types, owners, SLAs (deck A3)
    metrics.py         the four metrics (deck A6) and the A vs B comparison
    drafts.py          Gemini-drafted two-sentence messages to exception owners (cached)
    validate_live.py   report on the cached real readings (docs/LIVE_VALIDATION.md)
    ubl.py, intake_imap.py, auth.py, export_bq.py    phase 3: e-invoices, mailbox intake, basic auth, export
    templates/, static/
  data/
    invoices/          the 12 case PDFs
    invoices_v2/       test set v2
    cache/             Gemini readings (one JSON per file hash) and drafts/
  tests/               pytest suite; fixtures/ = ground truth per PDF; golden*.yaml = expected results
  docs/
    ASSUMPTIONS.md     every simulated number, with the reason (also shown in the app)
    DEMO.md            four-minute demo script
    CHECKPOINT_V2.md   alignment with the final deck: outcomes, metrics, wording, open points
    TEST_SET_V2.md     the 26 documents of test set v2
    PHASE3.md          cloud-ready build and live validation
    DEPLOY_GCP.md      Google Cloud deployment (phase 3)
    LIVE_VALIDATION.md generated by make validate
    SHARE_DEMO.md      sharing the demo through a tunnel
    img/               screenshots
```

## Disclaimer

Every page shows this footer:

> Velox P2P control-gate simulator. ERP (mock, system of record): read-only tables, not a real ERP. Gemini reads and classifies; rules decide. Durations are simulated (see Assumptions).
