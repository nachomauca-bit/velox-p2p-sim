# Velox P2P control-gate simulator

A small, self-contained demo that simulates the redesigned Accounts Payable (procure-to-pay) invoice process of a fictional retailer, Velox Retail. It shows how the same 14 supplier documents (the brief's 12 plus two clean, everyday invoices) behave in two scenarios: **A (as-is)**, with a dirty vendor master, weak PO discipline and two intake mailboxes, and **B (to-be)**, with a clean master, POs or contracts for most spend, one intake channel and a control gate. A Gemini model reads the documents (structured output with per-field confidence); everything else is deterministic rules. It is **not an ERP**: the "ERP" is a handful of read-only mock tables modelled on Dynamics 365 Finance concepts. There are no ledgers, payments, users or currency conversion. All companies, people and identifiers are fictional, and durations are simulated (see [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md)).

## Quick start

Requires Python 3.11+ and, optionally, GNU make. On Windows, `make setup` uses the Python launcher `py -3`. Everything runs locally: one process, one SQLite file, no Docker.

```
make setup      # create .venv and install requirements.txt
make seed       # generate the 14 PDFs, (re)create the DB, seed both scenarios, load both inboxes
make extract    # Gemini extraction of the 14 PDFs (cached; needs GEMINI_API_KEY, see below)
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

## Using the app

- Pick a scenario in the header (**A — As-is** / **B — To-be**) and press **Run scenario**: every document goes through the gate, with a visible step log. **Reset** restores the scenario (documents loaded, nothing processed).
- **Exception cockpit**: to-be exceptions grouped by type, each with an owner and an SLA; as-is shows a single untracked "email loop" bucket.
- **KPIs**: upstream process health and downstream automation efficiency, with the formula of every KPI.
- **Compare**: A vs B for the same 14 documents (**Run both scenarios** fills it in one click).
- Every invoice page shows the PDF, the extracted fields with confidence, the gate trace, the outcome and (to-be exceptions, with a key) a two-sentence message to the owner drafted by Gemini.
- Any page accepts `?scenario=asis` or `?scenario=tobe`, handy for links and screenshots.
- The 3-minute demo script is in [docs/DEMO.md](docs/DEMO.md); screenshots are in [docs/img](docs/img) (taken with `EXTRACTOR=fixture`, because no Gemini key was available: the invoice page labels the fields as fixture data).

## Project layout

```
velox-p2p-sim/
  app/
    main.py            FastAPI app and routes; POST /intake/webhook is the hook for real email intake
    config.py          paths and .env settings (model, extractor, confidence threshold, footer)
    db.py              engine, session, init
    models.py          SQLAlchemy models: mock ERP tables + simulator tables
    world.py           single source of truth: entities, suppliers, accounts, contracts, POs, receipts, the 14 documents
    seed.py            seeds to-be from world.py and derives as-is with rules D1-D6
    normalize.py       name / VAT / IBAN / invoice-number normalisation and name similarity
    invoices_gen.py    generates the 14 PDFs (reportlab)
    extract.py         Gemini extraction, disk cache, fixture mode, CLI
    sim.py             business-day calendar and simulated durations
    gate.py            control gate: scenario-aware rules (SCENARIOS), step trace, postings
    taxonomy.py        exception types, owners, SLAs
    metrics.py         KPIs and the A vs B comparison
    drafts.py          Gemini-drafted two-sentence messages to exception owners (cached)
    templates/         Jinja2 pages
    static/
  data/
    invoices/          generated PDFs
    cache/             extraction cache (one JSON per file hash)
    velox.db           SQLite database (created by make seed)
  tests/               pytest suite
    fixtures/          ground-truth extraction per PDF (used instead of the API)
    make_fixtures.py   rebuilds the fixtures from app/world.py
    golden.yaml        expected gate result per document and scenario, and the scenario KPIs
  docs/
    ASSUMPTIONS.md     every simulated number, with the reason (also shown in the app)
    DEMO.md            3-minute demo script
    img/               screenshots
  .env.example         GEMINI_API_KEY, GEMINI_MODEL, EXTRACTOR
  Makefile
  requirements.txt
```

## Status

**Phase 1** (brief build order, steps 1–6) and **Phase 2** (steps 7–9) are built.

- Seed data for both scenarios: the clean world (16 vendor accounts for 12 suppliers, 14 POs, 11 receipt lines, 4 contract rows) and the dirty world derived by rules D1–D6 (28 accounts for 12 suppliers, a ratio of 2.3; 6 of 14 POs).
- 14 sample PDFs, generated deterministically: the brief's 12 plus SecureNet (clean PO) and Harbor Freight (recurring contract), added so that the sample is not made of difficult cases only (docs/ASSUMPTIONS.md, section 11).
- Gemini extraction with structured output, per-field confidence, disk cache and automatic model fallback.
- Control gate for both scenarios (register, extract with confidence gate, resolve vendor, legal entity, duplicate, credit note, commitment match, terms, post), exception taxonomy with owners and SLAs, exception cockpit, KPIs, compare page, run log, reset.
- Results with the 14 documents (`tests/golden.yaml`): touchless 35.7% (A) vs 71.4% (B); 9 untracked email-loop documents vs 4 exceptions with an owner and an SLA; 2 duplicate postings of 1 invoice vs a blocked duplicate; 1 unapplied credit note vs an applied one; 2 wrong-entity postings vs 0; simulated cash leakage 29,646.00 EUR vs 0.00; cycle 13.1 vs 0.5 days on the sample average, 25 vs 3 days on the reference path "non-PO invoice sent to a store".
- Unit, golden and UI tests that never call the API.

**Not yet run against the live Gemini API**: no key was available while building, so `data/cache/` is empty. Add `GEMINI_API_KEY` to `.env` and run `make extract` (or use `EXTRACTOR=fixture`). The optional 13th "low-quality scan" document of the brief was not built.

## Disclaimer

Every page shows this footer:

> Velox P2P control-gate simulator. Mock ERP tables modelled on Dynamics 365 Finance concepts. Document understanding by a Gemini model via the Google GenAI SDK. Durations are simulated (see Assumptions).
