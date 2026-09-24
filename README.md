# Velox P2P control-gate simulator

A small, self-contained demo that simulates the redesigned Accounts Payable (procure-to-pay) invoice process of a fictional retailer, Velox Retail. It shows how the same 12 supplier documents behave in two scenarios: **A (as-is)**, with a dirty vendor master, weak PO discipline and two intake mailboxes, and **B (to-be)**, with a clean master, POs or contracts for most spend, one intake channel and a control gate. A Gemini model reads the documents (structured output with per-field confidence); everything else is deterministic rules. It is **not an ERP**: the "ERP" is a handful of read-only mock tables modelled on Dynamics 365 Finance concepts. There are no ledgers, payments, users or currency conversion. All companies, people and identifiers are fictional, and durations are simulated (see [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md)).

## Quick start

Requires Python 3.11+ and, optionally, GNU make. On Windows, `make setup` uses the Python launcher `py -3`. Everything runs locally: one process, one SQLite file, no Docker.

```
make setup      # create .venv and install requirements.txt
make seed       # generate the 12 PDFs, (re)create the DB, seed both scenarios, load both inboxes
make extract    # Gemini extraction of the 12 PDFs (cached; needs GEMINI_API_KEY, see below)
make run        # serve the app on http://127.0.0.1:8010
make test       # unit tests; never call the Gemini API
```

`make pdfs` regenerates only the PDFs; `make run PORT=8080` changes the port.

Without make (Windows, from the repository folder):

```
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app.invoices_gen
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

Results are cached in `data/cache/<sha256 of the PDF>.json`. `make seed` and re-runs read the cache and never call the API, so the demo works offline after the first extraction. "Load sample documents" calls the API only for PDFs not yet in the cache (and only when `GEMINI_API_KEY` is set); "Force re-extract" on an invoice page always calls it. Latency and token usage are printed per call. The app reads `.env` at start-up: restart `make run` after adding the key.

### Without an API key: `EXTRACTOR=fixture`

Set `EXTRACTOR=fixture` in `.env` to use the ground-truth JSON in `tests/fixtures/` instead of Gemini. The UI labels these results "fixture (ground truth, no API call)", and they are never presented as Gemini output. The tests always run in this mode (`tests/conftest.py`). To rebuild the fixtures from `app/world.py`, run `python -m tests.make_fixtures`.

## Project layout

```
velox-p2p-sim/
  app/
    main.py            FastAPI app and routes; POST /intake/webhook is the hook for real email intake
    config.py          paths and .env settings (model, extractor, confidence threshold, footer)
    db.py              engine, session, init
    models.py          SQLAlchemy models: mock ERP tables + simulator tables
    world.py           single source of truth: entities, suppliers, accounts, contracts, POs, receipts, the 12 documents
    seed.py            seeds to-be from world.py and derives as-is with rules D1-D6
    normalize.py       name / VAT / IBAN / invoice-number normalisation and name similarity
    invoices_gen.py    generates the 12 PDFs (reportlab)
    extract.py         Gemini extraction, disk cache, fixture mode, CLI
    sim.py             business-day calendar and simulated durations
    gate.py            phase 2: control gate
    taxonomy.py        phase 2: exception types, owners, SLAs
    metrics.py         phase 2: KPIs
    templates/         Jinja2 pages
    static/
  data/
    invoices/          generated PDFs
    cache/             extraction cache (one JSON per file hash)
    velox.db           SQLite database (created by make seed)
  tests/               pytest suite
    fixtures/          ground-truth extraction per PDF (used instead of the API)
    make_fixtures.py   rebuilds the fixtures from app/world.py
  docs/
    ASSUMPTIONS.md     every simulated number, with the reason (also shown in the app)
    DEMO.md            phase 2: 3-minute demo script
    img/               screenshots
  .env.example         GEMINI_API_KEY, GEMINI_MODEL, EXTRACTOR
  Makefile
  requirements.txt
```

## Status

**Phase 1** (brief build order, steps 1–6):

- Repository skeleton, Makefile, `.env.example`, SQLite database.
- Seed data for both scenarios: the clean world (16 vendor accounts for 12 suppliers, 14 POs, 11 receipt lines, 4 contract rows) and the dirty world derived by rules D1–D6 (28 accounts for 12 suppliers, a ratio of 2.3; 6 of 14 POs).
- The 12 sample PDFs, generated deterministically.
- Gemini extraction with structured output, per-field confidence, disk cache and automatic model fallback. **Not yet run against the live API**: no key was available while building, so `data/cache/` is empty and the inbox shows "Extraction pending" until you add `GEMINI_API_KEY` to `.env` and run `make extract` (or use `EXTRACTOR=fixture`).
- Pages: Inbox (two mailboxes, registration behaviour per scenario), Invoice (PDF next to the extracted fields and confidence bars), Vendor master, Purchase orders & receipts, Contracts, Pending vendor invoices (empty until phase 2), Assumptions. The header has the scenario switch, "Load sample documents" and "Reset".
- Unit tests that never call the API.

**Phase 2** is pending until the user says "go phase 2". It covers the control gate, exception taxonomy, exception cockpit, KPIs, the compare page, "Run scenario", the golden test and `docs/DEMO.md`. Until then, those pages and buttons are placeholders marked "phase 2".

## Disclaimer

Every page shows this footer:

> Velox P2P control-gate simulator. Mock ERP tables modelled on Dynamics 365 Finance concepts. Document understanding by a Gemini model via the Google GenAI SDK. Durations are simulated (see Assumptions).
