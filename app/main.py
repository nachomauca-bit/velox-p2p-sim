"""FastAPI app: mock ERP pages (read-only), control-gate pages and intake.

Server-rendered with Jinja2 + HTMX partials and app/static/app.css; htmx and Chart.js from a CDN. The active
scenario ("asis" | "tobe") lives in a cookie; any GET also accepts ?scenario=asis|tobe (sets the cookie).
The gate rules live in gate.py, the KPIs in metrics.py and the owner drafts in drafts.py: this module
only calls them and renders the results.
"""
from __future__ import annotations

import hashlib
import json
import re
import textwrap
import threading
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit
from xml.dom import minidom

import jinja2
import markdown
from markupsafe import Markup
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config, db, drafts, extract, gate, metrics, normalize, seed, sim, taxonomy, world
from app.models import (
    Contract,
    GateDecision,
    InboundDocument,
    LegalEntity,
    Party,
    PendingVendorInvoice,
    ProductReceipt,
    PurchaseOrder,
    Run,
    VendorAccount,
)

APP_TITLE = "Velox AP — control-gate simulator"
APP_DIR = Path(__file__).resolve().parent

SCENARIO_COOKIE = "scenario"
FLASH_COOKIE = "flash"
SCENARIO_SUMMARY = {
    "asis": "Duplicate vendor records, no PO discipline, two mailboxes, no control gate.",
    "tobe": "Clean master (one record per supplier and legal entity, terms from contract), commitments by spend "
            "category, one intake registered on arrival, the control gate.",
}

# Left navigation: two visibly separate groups (brief section 11). The split is the architecture
# message: the ERP stays the system of record, the gate sits in front of it.
NAV_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Velox ERP (mock, system of record)", [
        ("/erp/pending-invoices", "Posted invoices"),
        ("/erp/vendors", "Vendor master"),
        ("/erp/purchase-orders", "Purchase orders & receipts"),
        ("/erp/contracts", "Contracts & catalogues"),
    ]),
    ("Control gate (new)", [
        ("/inbox", "Inbox"),
        ("/gate/exceptions", "Exception cockpit"),
        ("/gate/kpis", "Metrics"),
        ("/gate/compare", "Compare"),
        ("/assumptions", "Assumptions"),
    ]),
]

# The six stages of the deck (slides 4 and 9), in this order, for the stage strip on top of every page:
# (key, label, link). Pages map to the stages they show (brief v2 section 1).
STAGES = [
    ("buy", "Buy", "/erp/purchase-orders"),
    ("supplier", "Set up the supplier", "/erp/vendors"),
    ("receive", "Receive or confirm", "/erp/purchase-orders#receipts"),
    ("arrives", "Invoice arrives", "/inbox"),
    ("gate", "Control gate", "/run"),
    ("resolve", "Resolve, post and pay", "/gate/exceptions"),
]
STAGE_PAGES = {"/erp/purchase-orders": {"buy", "receive"}, "/erp/contracts": {"buy"}, "/erp/vendors": {"supplier"},
               "/inbox": {"arrives"}, "/run": {"gate"}, "/gate/exceptions": {"resolve"},
               "/erp/pending-invoices": {"resolve"}}

DOC_TYPE_LABELS = {"invoice": "Invoice", "credit_note": "Credit note", "reminder": "Payment reminder",
                   "statement": "Statement", "other": "Not an invoice", "unknown": "Type unknown"}
CHANNEL_LABELS = {"ap_mailbox": "AP shared mailbox", "store_mailbox": "Store mailbox (Berlin 01)"}
CONTENT_LABELS = {"pdf": "PDF", "ubl_xml": "UBL e-invoice (XML)", "email_body": "Email body, no attachment"}
# Sample document sets: the 12 case documents (the demo) or test set v2 (docs/TEST_SET_V2.md; CLI and tests only).
DATASET_CHOICES = {"v1": "Case documents (12)", "v2": "Test set v2 (26)"}
DATASET_NAMES = {**DATASET_CHOICES, seed.LIVE_DATASET: "Live intake (webhook)"}


# --------------------------------------------------------------------------------------------
# App, templates, formatting filters
# --------------------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create tables (never drop), add the columns an older database lacks, and seed the mock ERP if it is empty.
    An empty database comes up demo-ready (config.DEMO_AUTOLOAD): both scenarios loaded and run offline."""
    added = db.init_db()
    if added:
        print(f"[startup] database upgraded: added {', '.join(added)}")
    with db.SessionLocal() as session:
        if session.scalar(select(func.count()).select_from(LegalEntity)) == 0:
            seed.seed_all(session)
            if config.DEMO_AUTOLOAD:
                seed.reset_demo(session)
                print("[startup] empty database: demo ready (both scenarios loaded and run; one email to receive)")
            else:
                print("[startup] empty database: seeded both scenarios (run `make seed` to load the mailboxes)")
    yield


# No Swagger / ReDoc / OpenAPI pages: every page of the app carries the layout and its footer.
app = FastAPI(title=APP_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

templates = Jinja2Templates(env=jinja2.Environment(
    loader=jinja2.FileSystemLoader(APP_DIR / "templates"), autoescape=True))


def fmt_money(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.2f}"


def fmt_qty(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.2f}"


def fmt_date(value: Optional[date | datetime]) -> str:
    """Weekday + ISO date, so business-day delays are easy to follow: 'Thu 2026-10-01'."""
    return "—" if value is None else value.strftime("%a %Y-%m-%d")


def fmt_date_wd(value: Optional[date | datetime]) -> Markup:
    """fmt_date with the weekday in its own span, so a narrow table can hide it: 'Thu 2026-10-01'."""
    if value is None:
        return Markup("—")
    return Markup('<span class="wd">{}</span> {}').format(value.strftime("%a"), value.strftime("%Y-%m-%d"))


def fmt_datetime(value: Optional[datetime]) -> str:
    return "—" if value is None else value.strftime("%a %Y-%m-%d %H:%M")


def fmt_bank(value: Optional[str]) -> str:
    return world.format_iban(value) if value else "—"


_VALUE_UNIT = re.compile(r"^([-+]?[\d,]+(?:\.\d+)?) ([A-Z]{3}|days?)$")


def value_parts(value: Any) -> Optional[list[tuple[str, str]]]:
    """A KPI value split into number and unit, so a tile can show the unit smaller and never break the number:
    '29,646.00 EUR' -> [('29,646.00', 'EUR')], '1,800.00 EUR + 500.00 USD' -> two parts, '0.5 days' ->
    [('0.5', 'days')]; None for any other text (a percentage, a ratio, a dash)."""
    parts = [_VALUE_UNIT.match(p.strip()) for p in str(value or "").split(" + ")]
    return [(m.group(1), m.group(2)) for m in parts] if parts and all(parts) else None


def fmt_signed(value: Optional[float]) -> str:
    """Money with an explicit sign for a variance: '+300.00', '-12.50', '0.00'."""
    if value is None:
        return "—"
    return f"+{value:,.2f}" if value > 0 else f"{value:,.2f}"


templates.env.filters.update(money=fmt_money, qty=fmt_qty, d=fmt_date, dwd=fmt_date_wd, dt=fmt_datetime,
                             bank=fmt_bank, value_parts=value_parts, signed=fmt_signed)


# --------------------------------------------------------------------------------------------
# Scenario cookie, flash messages, rendering
# --------------------------------------------------------------------------------------------


def scenario_param(request: Request) -> Optional[str]:
    """A valid ?scenario= on a GET (links in the demo script and screenshots), else None."""
    value = request.query_params.get("scenario") if request.method == "GET" else None
    return value if value in config.SCENARIOS else None


def active_scenario(request: Request) -> str:
    value = scenario_param(request) or request.cookies.get(SCENARIO_COOKIE, "")
    return value if value in config.SCENARIOS else config.DEFAULT_SCENARIO


def set_scenario_cookie(response: Response, scenario: str) -> None:
    response.set_cookie(SCENARIO_COOKIE, scenario, max_age=30 * 24 * 3600, httponly=True, samesite="lax")


@app.middleware("http")
async def scenario_from_query(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """GET ...?scenario=asis|tobe renders that scenario and makes it the active one (cookie)."""
    response = await call_next(request)
    scenario = scenario_param(request)
    if scenario:
        set_scenario_cookie(response, scenario)
    return response


def check_scenario(value: str) -> str:
    if value not in config.SCENARIOS:
        raise HTTPException(status_code=400, detail=f"Unknown scenario {value!r}: use one of {config.SCENARIOS}")
    return value


def form_scenario(request: Request, value: Optional[str]) -> str:
    """Scenario of a header action (Load / Run / Reset): the hidden field of the form, i.e. the scenario the page
    was rendered for, so a second tab that switched the cookie never runs or resets the wrong one. Without the
    field (older page, API client) the cookie decides."""
    return check_scenario(value) if value else active_scenario(request)


def back_path(request: Request, default: str = "/inbox") -> str:
    """Path of the Referer when it is same-origin, else `default` (never an external redirect)."""
    referer = request.headers.get("referer")
    if not referer:
        return default
    parts = urlsplit(referer)
    if parts.netloc and parts.netloc != request.url.netloc:
        return default
    path = parts.path
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    return path


def redirect(path: str, flash: Optional[tuple[str, str]] = None) -> RedirectResponse:
    """303 redirect, optionally carrying a one-shot flash message (kind, text) in a cookie."""
    response = RedirectResponse(path, status_code=303)
    if flash:
        kind, text = flash
        response.set_cookie(FLASH_COOKIE, quote(f"{kind}|{text}"), max_age=60, httponly=True, samesite="lax")
    return response


def read_flash(request: Request) -> Optional[dict[str, str]]:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    kind, _, text = unquote(raw).partition("|")
    return {"kind": kind if kind in ("ok", "info", "warn", "error") else "info", "text": text}


def nav_active(path: str) -> str:
    """Sidebar item to highlight: a document page and the step log of a run belong to the inbox."""
    return "/inbox" if path.startswith("/invoice/") or path == "/run" else path


def active_stages(path: str) -> set[str]:
    """The deck stages a page shows: a document page is the control gate (the rule log of one invoice)."""
    return {"gate"} if path.startswith("/invoice/") else STAGE_PAGES.get(path, set())


def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> Response:
    """Render a full page with the shared layout context (scenario, nav, footer, flash)."""
    scenario = active_scenario(request)
    flash = read_flash(request)
    ctx = {
        "app_title": APP_TITLE,
        "scenario": scenario,
        "scenario_label": config.SCENARIO_LABELS[scenario],
        "scenario_summary": SCENARIO_SUMMARY[scenario],
        "scenarios": config.SCENARIOS,
        "scenario_labels": config.SCENARIO_LABELS,
        "nav_groups": NAV_GROUPS,
        "nav_active": nav_active(request.url.path),
        "stages": STAGES,
        "active_stages": active_stages(request.url.path),
        "footer_text": config.FOOTER_TEXT,
        "flash": flash,
        "dataset_choices": DATASET_CHOICES,
        **context,
    }
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if flash:
        response.delete_cookie(FLASH_COOKIE)
    return response


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --------------------------------------------------------------------------------------------
# Error pages: HTML (with the layout and footer) for browsers, the default JSON otherwise
# --------------------------------------------------------------------------------------------

WEBHOOK_PATH = "/intake/webhook"
ERROR_MESSAGES = {
    404: "This page does not exist, or the document was removed (for example by a reset).",
    405: "This address does not accept this kind of request.",
}


def wants_html(request: Request) -> bool:
    """Browser requests get an HTML error page; the intake webhook and API clients get JSON."""
    return request.url.path != WEBHOOK_PATH and "text/html" in request.headers.get("accept", "")


def status_phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if not wants_html(request):
        return await http_exception_handler(request, exc)  # FastAPI's default: {"detail": ...}
    phrase = status_phrase(exc.status_code)
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != phrase else None
    response = render(request, "error.html", {
        "page_title": f"{exc.status_code} {phrase}", "status_code": exc.status_code, "phrase": phrase,
        "detail": detail, "message": ERROR_MESSAGES.get(exc.status_code),
    }, status_code=exc.status_code)
    response.headers.update(exc.headers or {})  # e.g. Allow on 405
    return response


# --------------------------------------------------------------------------------------------
# Basic routes, scenario switch, header actions
# --------------------------------------------------------------------------------------------


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/inbox", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scenario/{scenario}")
def switch_scenario(scenario: str, request: Request) -> RedirectResponse:
    check_scenario(scenario)
    response = redirect(back_path(request))
    set_scenario_cookie(response, scenario)
    return response


def extraction_mode_note() -> str:
    if config.EXTRACTOR == "fixture":
        return " Fixture mode: ground-truth JSON, not a Gemini output."
    return ""


def form_dataset(value: Optional[str]) -> str:
    """Dataset of a sample load: v1 (the 12 case documents, the default) or v2 (test set v2)."""
    if not value:
        return "v1"
    if value not in seed.DATASETS:
        raise HTTPException(status_code=400, detail=f"Unknown dataset {value!r}: use one of {seed.DATASETS}")
    return value


@app.post("/documents/load")
def load_documents(request: Request, scenario: Optional[str] = Form(None), dataset: Optional[str] = Form(None),
                   session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Load a set of sample documents (case documents or test set v2) into both mailboxes of the scenario and
    extract them.

    The previous gate results of the scenario are cleared first: they belong to the old documents. The extraction
    runs inside the gate lock too, so a Run clicked meanwhile waits instead of reading half-written rows.
    """
    scenario = form_scenario(request, scenario)
    dataset = form_dataset(dataset)
    with _gate_lock:
        gate.clear_results(session, scenario)
        docs = seed.load_sample_documents(session, scenario, dataset)
        for doc in docs:
            reg = registration_info(doc)
            print(f"[doc {doc.doc_id}] step=intake result={'registered' if doc.registered else 'waiting'} "
                  f"mailbox={doc.mailbox} registration={reg['date'].date().isoformat()}")
        try:
            summary = extract.extract_documents(session, docs, allow_api=config.gemini_configured())
        except Exception as exc:  # per-document failures are counted; anything else must not break the intake
            session.rollback()
            print(f"[intake] scenario={scenario} loaded={len(docs)} extraction error: {exc!r}")
            summary = None
    if summary is None:
        response = redirect("/inbox", ("error", f"{len(docs)} documents loaded, but the extraction failed; "
                                                "see the server log."))
    else:
        print(f"[intake] scenario={scenario} dataset={dataset} loaded={len(docs)} extracted={summary['extracted']} "
              f"from_cache={summary['from_cache']} unavailable={summary['unavailable']} "
              f"failed={summary.get('failed', 0)}")
        response = redirect("/inbox", load_message(len(docs), scenario, summary, dataset, docs))
    set_scenario_cookie(response, scenario)
    return response


def load_message(loaded: int, scenario: str, summary: dict[str, Any], dataset: str = "v1",
                 docs: Sequence[InboundDocument] = ()) -> tuple[str, str]:
    """Flash (kind, text) for "Load sample documents" from the extract_documents summary. An email that carries
    the invoice only in its body has nothing to extract: it is counted apart, not as pending."""
    failed, unavailable = summary.get("failed", 0), summary["unavailable"]
    body_only = sum(1 for d in docs if d.content_type == "email_body" and d.extraction is None)
    unavailable = max(unavailable - body_only, 0)
    text = (f"{loaded} documents loaded into the mailboxes of {config.SCENARIO_LABELS[scenario]} "
            f"({seed.DATASET_LABELS[dataset]}); {summary['extracted']} extracted ({summary['from_cache']} from cache)."
            f"{extraction_mode_note()}")
    if body_only:
        text += (f" {plural(body_only, 'email')} with the invoice only in the body (nothing to extract: AP keys "
                 f"{'it' if body_only == 1 else 'them'}).")
    if failed:
        error = str(summary.get("error") or "unknown error")[:300].rstrip(".")  # the flash lives in a cookie
        text += f" {plural(failed, 'document')} failed: {error}."
    if unavailable:
        text += f" Extraction pending for {plural(unavailable, 'document')}"
        if config.EXTRACTOR != "fixture" and not config.gemini_configured():
            text += f": set {config.gemini_missing_setting()} in .env and run make extract."
        elif config.gemini_configured():
            text += " (retry with make extract)."
        else:
            text += "."
    if failed:
        return ("error", text)
    return ("warn", text) if unavailable else ("ok", text)


# Pages of a single document: a reset deletes the scenario's documents (webhook uploads are not
# restored), so never redirect back there.
DOCUMENT_PAGE_PREFIXES = ("/invoice/", "/files/")

# One process (brief): runs, resets and re-runs of the gate are serialised so two clicks never
# interleave their deletes and inserts.
_gate_lock = threading.Lock()


@app.post("/reset")
def reset_demo(session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Header "Reset demo" (brief v2 section 7): both scenarios re-seeded, the 12 case documents loaded and run
    offline (extraction cache or fixtures, never the API), the demo's next email held back; opens the to-be inbox."""
    with _gate_lock:
        try:
            seed.reset_demo(session)
        except Exception as exc:  # a broken rule or data problem must not end in a 500
            session.rollback()
            print(f"[reset] demo reset FAILED: {exc!r}")
            return redirect("/inbox", ("error", "The demo reset failed; see the server log."))
    for scenario in config.SCENARIOS:
        export_after_run(session, scenario, "reset")
    held = seed.held_back_documents(session)
    print(f"[reset] demo: both scenarios re-seeded, loaded and run; {len(held)} email(s) to receive")
    text = "Demo reset: both scenarios loaded and run (offline)."
    if held:
        text += f" One email has not arrived yet: press Receive next email ({held[0].party.canonical_name})."
    response = redirect("/inbox", ("ok", text))
    set_scenario_cookie(response, "tobe")
    return response


@app.post("/inbox/receive")
def receive_next_email(session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Demo step 1: the next held-back case document arrives at ap@ and is registered on arrival (to-be). Nothing is
    read or decided yet: its page offers "Run the control gate"."""
    with _gate_lock:
        held = seed.held_back_documents(session)
        if not held:
            response = redirect("/inbox", ("info", "No email waiting: every case document has arrived."))
            set_scenario_cookie(response, "tobe")
            return response
        doc = seed.receive_document(session, "tobe", held[0])
    print(f"[intake] {doc.doc_id} received on {doc.mailbox}, registered on arrival {doc.registered_on:%Y-%m-%d %H:%M}")
    response = redirect("/inbox", ("ok", f"Email received at {doc.mailbox}: registered as {doc.doc_id} on "
                                         f"{doc.registered_on:%a %d %b %Y %H:%M}. The clock runs from this minute."))
    set_scenario_cookie(response, "tobe")
    return response


def run_gate(session: Session, scenario: str) -> tuple[Optional[Run], str]:
    """Run the gate on every document of a scenario; an empty inbox is loaded first. Never raises.

    Returns (run, message); run is None when the run failed (message = a short text; the error is in the log).
    """
    with _gate_lock:
        try:
            loaded = ""
            if count_documents(session, scenario) == 0:
                docs = seed.load_sample_documents(session, scenario)
                loaded = f"{len(docs)} sample documents loaded first. "
            run = gate.run_scenario(session, scenario, allow_api=False)  # offline: cache or fixtures only
        except Exception as exc:  # a broken rule or data problem must not end in a 500
            session.rollback()
            print(f"[run] scenario={scenario} FAILED: {exc!r}")
            return None, f"The gate run of {config.SCENARIO_LABELS[scenario]} failed; see the server log."
    export_after_run(session, scenario)
    s = run.summary_json or {}
    words = metrics.outcome_counts_from(scenario, s.get("outcomes") or {})
    return run, (f"{loaded}{config.SCENARIO_LABELS[scenario]}: {s.get('documents', 0)} documents processed · "
                 + " · ".join(f"{w} {n}" for w, n in words.items())
                 + f" · posted with no human touch {s.get('touchless', 0)}.")


def export_after_run(session: Session, scenario: str, event: str = "run") -> None:
    """With BQ_EXPORT on, export the decisions and mock tables (app/export_bq.py: NDJSON files, then BigQuery)
    after anything that changes the gate results: a run, a reset, a re-run of one document, a document received
    through the intake webhook (`event` names it in the log). Imported lazily; a failure is logged and never fails
    the request."""
    if not config.BQ_EXPORT:
        return
    try:
        from app import export_bq

        tables = export_bq.export_all(session) or {}
        rows = sum(v if isinstance(v, int) else len(v) for v in tables.values())
        print(f"[export] after the {scenario} {event}: {rows} rows in {len(tables)} tables")
    except Exception as exc:  # the export is optional: the demo keeps working without it
        session.rollback()
        print(f"[export] after the {scenario} {event} FAILED: {exc!r}")


def count_documents(session: Session, scenario: str) -> int:
    return session.scalar(select(func.count()).select_from(InboundDocument)
                          .where(InboundDocument.scenario == scenario)) or 0


@app.post("/run")
def run_scenario(request: Request, scenario: Optional[str] = Form(None),
                 session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Header "Run scenario": process every document of the page's scenario, then show the step log."""
    scenario = form_scenario(request, scenario)
    run, message = run_gate(session, scenario)
    response = redirect("/inbox", ("error", message)) if run is None else redirect("/run", ("ok", message))
    set_scenario_cookie(response, scenario)
    return response


@app.post("/run-both")
def run_both(session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Run as-is, then to-be, and open the comparison page."""
    messages = []
    for scenario in config.SCENARIOS:
        run, message = run_gate(session, scenario)
        if run is None:
            return redirect("/gate/compare", ("error", message))
        messages.append(message)
    return redirect("/gate/compare", ("ok", " ".join(messages)))


# --------------------------------------------------------------------------------------------
# Gate results as the pages show them: outcome chips, badges, owner, SLA due date, age
# --------------------------------------------------------------------------------------------

EMAIL_LOOP_LABEL = "Email loop — untracked"
# The four outcomes of the gate (brief v2): Post · Exception · Block · Human review; the as-is has no gate.
OUTCOME_LABELS = gate.OUTCOME_WORDS["tobe"]
# One colour per outcome, the same on every page (pills, inbox card stripes, outcome cards, charts):
# Post = emerald, Block = slate, Human review = amber, Exception = red, email loop = orange. Violet marks a credit
# note linked to its invoice (a badge next to Post). Sky ("info") is kept for info tasks and info flags only.
OUTCOME_TONES = {"posted": "ok", "applied_credit": "ok", "blocked_duplicate": "blocked", "human_review": "warn",
                 "exception": "bad"}
# Tone of a step result in the gate trace (css class chip-<tone>).
RESULT_TONES = {"ok": "ok", "applied": "vio", "info": "info", "blocked": "blocked", "flag": "warn",
                "created": "warn", "exception": "bad", "human_review": "warn", "email_loop": "loop",
                "unapplied": "bad", "skipped": "neutral", **OUTCOME_TONES}
# Tone of a result word in the rule log ("[B-05] Commitment → Exception — ...").
LOG_WORD_TONES = {"pass": "ok", "info": "info", "flag": "warn", "account created": "warn", "skipped": "neutral",
                  "Exception": "bad", "Human review": "warn", "Block": "blocked", "credit note linked": "vio",
                  "unapplied": "bad", "email loop": "loop", "Post": "ok", "Posted by AP": "ok",
                  "Email loop — untracked": "loop", "Blocked (same account)": "blocked"}
# Short badges (same strings as metrics.compare cells) and their tone.
BADGE_TONES = {"duplicate posting": "bad", "wrong entity": "bad", "unapplied credit": "bad",
               "vendor account created": "bad", "terms paid early": "warn", "terms paid late": "warn",
               "terms variance": "warn", "duplicate record flagged": "info", "contract match": "ok",
               "catalogue match": "ok", "3-way match": "ok", "credit note linked": "vio",
               "statement posted as invoice": "bad", "UBL e-invoice": "info", "email body only": "warn"}
CYCLE_LABELS = {"store_forwarding": "Store mailbox forwarding",
                "ap_open_and_key": "AP opens ap@ and keys the invoice",
                "email_loop": "Email loop (untracked)", "email_approval": "Approval by email",
                "posting": "Posting", "registration": "Registration on arrival",
                "extraction_and_gate": "Reading and rules", "exception_sla": "Owner resolves within the SLA",
                "past_sla": "Resolved past the SLA", "workflow_approval": "Workflow approval"}
RESOLUTION_LABELS = {"vat_id": "tax ID", "iban": "IBAN", "name": "name similarity",
                     "exact_name": "exact display name (first hit)", "fuzzy_name": "similar display name (first hit)",
                     "created": "nothing: AP opened a new account"}
PATH_LABELS = {"matched": "Matched", "email_loop": "Email loop", "touchless": "Touchless",
               "exception": "Exception with SLA"}
BLOCKED_PATH_LABEL = "Blocked, not posted"  # a blocked duplicate is never posted: not touchless


def path_label(path: Optional[str], outcome: Optional[str]) -> Optional[str]:
    """How a document went through (cycle chart, outcome card): a blocked duplicate is named apart."""
    return BLOCKED_PATH_LABEL if outcome == "blocked_duplicate" else PATH_LABELS.get(path or "", path)


def outcome_chip(outcome: Optional[str], exception_type: Optional[str], scenario: str = "tobe") -> tuple[str, str]:
    """(label, tone) of the outcome chip: Post / Exception: <A3 label> / Block / Human review: <A3 label>; as-is:
    Posted by AP / Email loop — untracked."""
    if exception_type == "email_loop":
        return EMAIL_LOOP_LABEL, "loop"
    word = gate.OUTCOME_WORDS.get(scenario, OUTCOME_LABELS).get(outcome or "", outcome or "—")
    if outcome in ("exception", "human_review") and exception_type:
        return f"{word}: {taxonomy.label(exception_type)}", OUTCOME_TONES[outcome]
    return word, OUTCOME_TONES.get(outcome or "", "neutral")


def badge_chips(badges: list[str], *shown: Optional[str]) -> list[tuple[str, str]]:
    """(label, tone) of the short badges, without the ones that repeat a label already shown next to them (an
    outcome pill that already says what the badge says)."""
    seen = [s.lower() for s in shown if s]
    return [(b, BADGE_TONES.get(b, "neutral")) for b in badges if not any(b.lower() in s for s in seen)]


# Badge that repeats the content-type chip of an inbox card ("UBL e-invoice (XML)", "Email body, no attachment").
CONTENT_BADGES = {"ubl_xml": "UBL e-invoice", "email_body": "email body only"}
# An email that carries the invoice only in its body goes to human review, but not for a low extraction
# confidence: there was nothing to extract. The pages say so instead of the taxonomy label.
EMAIL_BODY_REVIEW_LABEL = "Human review: invoice only in the email body"
EMAIL_BODY_REVIEW_RESOLUTION = "Key the invoice from the email text"


def is_email_body_review(decision: GateDecision, doc: Optional[InboundDocument]) -> bool:
    return doc is not None and doc.content_type == "email_body" and decision.exception_type == "human_review"


def sla_due(decision: GateDecision, doc: Optional[InboundDocument]) -> Optional[datetime]:
    """Registration (the ageing clock starts there) + SLA business days; None without an SLA."""
    start = doc and (doc.registered_on or doc.received_on)
    if decision.sla_days is None or start is None:
        return None
    return sim.add_business_days(start, int(decision.sla_days))


def age_days(doc: Optional[InboundDocument], as_of: Optional[datetime]) -> Optional[int]:
    """Days open: business days from registration to the cockpit snapshot (sim.cockpit_as_of) or an earlier end."""
    start = doc and (doc.registered_on or doc.received_on)
    return None if start is None or as_of is None else sim.business_days_between(start, as_of)


def parse_iso(value: Any) -> Optional[datetime]:
    """An ISO date or datetime string of the decision details as a datetime; None when missing or unreadable."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def posted_on(decision: GateDecision) -> Optional[datetime]:
    """Simulated posting datetime of a posted document (details 'posted_on', else the decision date, which is the
    posting date for a posting); None when the document was not posted."""
    det = decision.details or {}
    if not det.get("posted"):
        return None
    return parse_iso(det.get("posted_on")) or decision.decided_on


def gate_view(decision: GateDecision, doc: Optional[InboundDocument] = None,
              as_of: Optional[datetime] = None) -> dict[str, Any]:
    """Everything a page shows about one decision: details (always present keys) + chips, owner, SLA, age.

    The age runs from registration to the snapshot `as_of`, or to the posting day when the document was already
    posted by then (as-is: the email loop ends in a posting).
    """
    det = dict(decision.details or {})
    label, tone = outcome_chip(decision.outcome, decision.exception_type, decision.scenario)
    spec = seed.spec_for(doc.dataset if doc else seed.dataset_of_doc_id(decision.doc_id),
                         det.get("sample_no") or (doc.sample_no if doc else 0))
    posted = posted_on(decision)
    posted_by_snapshot = bool(as_of and posted and posted <= as_of)
    email_body_review = is_email_body_review(decision, doc)
    type_label = taxonomy.label(decision.exception_type) if decision.exception_type else None
    days_open = age_days(doc, posted if posted_by_snapshot else as_of)
    open_at_snapshot = as_of is not None and not posted_by_snapshot
    return {
        **det,
        "doc_id": decision.doc_id,
        "outcome": decision.outcome,
        "exception_type": decision.exception_type,
        "is_email_loop": decision.exception_type == "email_loop",
        "type_label": EMAIL_BODY_REVIEW_LABEL if email_body_review else type_label,
        "type_info": taxonomy.BY_KEY.get(decision.exception_type or ""),
        "type_resolution": EMAIL_BODY_REVIEW_RESOLUTION if email_body_review else None,
        "chip_label": EMAIL_BODY_REVIEW_LABEL if email_body_review else label,
        "chip_tone": tone,
        "owner_name": decision.owner_name,
        "owner_role": decision.owner_role,
        "sla_days": decision.sla_days,
        "sla_due": sla_due(decision, doc),
        "age_days": days_open,
        # Past its SLA at the cockpit snapshot: still open and open for more business days than its SLA.
        "past_sla": bool(open_at_snapshot and decision.sla_days is not None and days_open is not None
                         and days_open > decision.sla_days),
        "owner_title": det.get("owner_title"),
        "posted_on": posted,
        "posted_by_snapshot": posted_by_snapshot,
        "reason": decision.reason,
        "days": None if decision.simulated_days is None else int(decision.simulated_days),
        "badges": badge_chips(metrics.badges(decision), label),
        "flags": det.get("flags") or [],
        "supplier": det.get("supplier_name") or (spec.printed_supplier_name if spec else None) or "—",
        "path_label": path_label(det.get("path"), decision.outcome),
    }


def decisions_by_doc(session: Session, scenario: str) -> dict[str, GateDecision]:
    rows = session.scalars(select(GateDecision).where(GateDecision.scenario == scenario)
                           .order_by(GateDecision.id)).all()
    return {d.doc_id: d for d in rows}  # the latest decision wins if a document has several


def scenario_documents(session: Session, scenario: str) -> list[InboundDocument]:
    """Documents in the gate's processing order: registration datetime (per scenario rule), then doc_id."""
    docs = session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)).all()
    return sorted(docs, key=gate.order_key)


def latest_run(session: Session, scenario: str) -> Optional[Run]:
    return session.scalar(select(Run).where(Run.scenario == scenario)
                          .order_by(Run.started_on.desc(), Run.id.desc()).limit(1))



# --------------------------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------------------------


def registration_info(doc: InboundDocument) -> dict[str, Any]:
    """Registered date, or the expected one with the reason (brief sections 6 and 10). A document received live
    (real dates) whose registration day is still ahead is shown as not registered yet, even once the gate ran."""
    ahead = doc.dataset == seed.LIVE_DATASET and doc.registered_on is not None and doc.registered_on > datetime.now()
    if doc.registered and doc.registered_on is not None and not ahead:
        days = sim.business_days_between(doc.received_on, doc.registered_on)
        if days == 0:
            note = "Registered on arrival"
        elif doc.channel == "store_mailbox":
            note = f"Forwarded by the store after {plural(days, 'business day')}"
        else:
            note = f"AP opened ap@ after {plural(days, 'business day')}"
        return {"registered": True, "date": doc.registered_on, "reason": note}
    days = sim.registration_delay_days(doc.scenario, doc.channel)
    if days == 0:
        reason = "Registered on arrival"
    elif doc.channel == "store_mailbox":
        reason = f"Forwarded by the store after {plural(days, 'business day')}"
    else:
        reason = f"AP opens ap@ after {plural(days, 'business day')}"
    return {"registered": False, "date": sim.registration_date(doc.scenario, doc.channel, doc.received_on),
            "reason": reason}


def doc_type_key(doc: InboundDocument) -> str:
    """The document type as read (invoice, credit_note, reminder, statement, other), "unknown" before it is read."""
    return doc.doc_type if doc.doc_type in DOC_TYPE_LABELS else "unknown"


def dataset_summary(docs: Sequence[InboundDocument]) -> list[dict[str, Any]]:
    """The document sets in a mailbox view, e.g. [{"key": "v2", "label": "Test set v2 (26)", "count": 26}]."""
    counts: dict[str, int] = {}
    for d in docs:
        counts[d.dataset or "v1"] = counts.get(d.dataset or "v1", 0) + 1
    order = [*seed.DATASETS, seed.LIVE_DATASET]
    return [{"key": k, "label": DATASET_NAMES.get(k, k), "count": counts[k]}
            for k in sorted(counts, key=lambda k: (order.index(k) if k in order else len(order), k))]


@app.get("/inbox")
def inbox(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    docs = session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)
                           .order_by(InboundDocument.received_on, InboundDocument.doc_id)).all()
    decisions = decisions_by_doc(session, scenario)
    views = {d.doc_id: gate_view(decisions[d.doc_id], d) for d in docs if d.doc_id in decisions}
    for d in docs:  # an inbox card already names the content type in a chip
        if d.doc_id in views and d.content_type in CONTENT_BADGES:
            views[d.doc_id]["badges"] = [b for b in views[d.doc_id]["badges"]
                                         if b[0] != CONTENT_BADGES[d.content_type]]
    # to-be: one intake address (ap@), registered on arrival; as-is: the two mailboxes suppliers use today.
    channels = {"ap_mailbox": world.AP_MAILBOX} if scenario == "tobe" else world.MAILBOX_BY_CHANNEL
    mailboxes = [
        {"channel": channel, "address": address,
         "label": "One intake address" if scenario == "tobe" else CHANNEL_LABELS[channel],
         "docs": [(d, registration_info(d), views.get(d.doc_id), doc_type_key(d)) for d in docs
                  if d.channel == channel or scenario == "tobe"]}
        for channel, address in channels.items()
    ]
    held = seed.held_back_documents(session, scenario) if scenario == "tobe" else []
    return render(request, "inbox.html", {"page_title": "Inbox", "mailboxes": mailboxes, "total": len(docs),
                                          "processed": len(views), "doc_type_labels": DOC_TYPE_LABELS,
                                          "content_labels": CONTENT_LABELS, "datasets": dataset_summary(docs),
                                          "run": latest_run(session, scenario), "next_email": held[0] if held else None,
                                          "unprocessed": [d.doc_id for d in docs if d.doc_id not in views]})


# --------------------------------------------------------------------------------------------
# Run page: the visible step log of the latest run and the outcome per document
# --------------------------------------------------------------------------------------------

_LOG_PREFIX = re.compile(r"^(\[[^\]]*\])\s?(.*)$")
_LOG_RULE = re.compile(r"^(?P<rule>.+?) → (?P<result>.+?)(?: — (?P<reason>.*))?$")
LOG_REVEAL_MS = 9000  # the whole log is revealed line by line in about this many milliseconds


def log_line(text: str) -> dict[str, str]:
    """One rule-log line split for display: "[B-05]", the rule, the result word (with its tone), the reason."""
    m = _LOG_PREFIX.match(text)
    prefix, body = (m.group(1), m.group(2)) if m else ("", text)
    if prefix == "[run]":
        return {"prefix": prefix, "rule": "", "result": "", "reason": body, "tone": "neutral", "kind": "run"}
    r = _LOG_RULE.match(body)
    if r is None:
        return {"prefix": prefix, "rule": "", "result": "", "reason": body, "tone": "neutral", "kind": "step"}
    kind = "outcome" if r.group("rule") == "Outcome" else "step"
    return {"prefix": prefix, "rule": r.group("rule"), "result": r.group("result"), "reason": r.group("reason") or "",
            "tone": LOG_WORD_TONES.get(r.group("result"), "neutral"), "kind": kind}


@app.get("/run")
def run_page(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    run = latest_run(session, scenario)
    summary = (run.summary_json or {}) if run else {}
    lines = [log_line(str(text)) for text in summary.get("log") or []]
    docs = scenario_documents(session, scenario)
    decisions = decisions_by_doc(session, scenario)
    in_log = {line["prefix"][1:-1] for line in lines}
    return render(request, "run.html", {
        "page_title": "Rule log" if scenario == "tobe" else "Processing log", "run": run, "summary": summary,
        "lines": lines, "outcome_counts": metrics.outcome_counts_from(scenario, summary.get("outcomes") or {}),
        "outcome_tones": LOG_WORD_TONES,
        # processed on its own after the run (e.g. the demo's next email): its lines are on its invoice page
        "processed_after": [d.doc_id for d in docs if d.doc_id in decisions and d.doc_id not in in_log] if run else [],
        "step_ms": max(12, min(90, LOG_REVEAL_MS // max(len(lines), 1))),
        "rows": [gate_view(decisions[d.doc_id], d) for d in docs if d.doc_id in decisions],
        # an email that carries the invoice only in its body has nothing to extract: named apart, not as missing
        "no_extraction": [d.doc_id for d in docs if d.extraction is None and d.content_type != "email_body"],
        "body_only": [d.doc_id for d in docs if d.extraction is None and d.content_type == "email_body"],
        "no_extraction_reason": no_extraction_reason(),
        "unprocessed": [d.doc_id for d in docs if d.doc_id not in decisions],
        "extraction": summary.get("extraction") or {},
    })


# --------------------------------------------------------------------------------------------
# Invoice page, extraction panel (HTMX partial), PDF files
# --------------------------------------------------------------------------------------------


def get_document(session: Session, doc_id: str) -> InboundDocument:
    doc = session.scalar(select(InboundDocument).where(InboundDocument.doc_id == doc_id))
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    return doc


def field_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per extraction field, in schema order, with confidence and flags."""
    rows = []
    for name in extract.FIELDS:
        item = data.get(name) or {}
        value = item.get("value")
        confidence = float(item.get("confidence") or 0.0)
        rows.append({
            "name": name,
            "label": extract.FIELD_LABELS.get(name, name),
            "value": value,
            "confidence": confidence,
            "pct": round(confidence * 100),
            "low": value is not None and confidence < config.CONFIDENCE_THRESHOLD,
            "critical": name in extract.CRITICAL_FIELDS,
            "identity": name in extract.SUPPLIER_IDENTITY_FIELDS,  # any one of them identifies the supplier
        })
    return rows


def no_extraction_reason(doc: Optional[InboundDocument] = None) -> str:
    if doc is not None and doc.content_type == "email_body":
        return ("The invoice is only in the email body: there is no document to read. AP keys it from the email "
                "text shown on this page.")
    if doc is not None and doc.scenario == "asis":
        return "Processing this document keys its fields (scenario A has no model: AP types them)."
    if doc is not None and reading_saved(doc):  # the demo's next email: its reading is in the cache already
        return "Run the control gate: it reads the document first, from the saved reading (no model call now)."
    if config.EXTRACTOR == "fixture":
        return "Fixture mode is on (EXTRACTOR=fixture) and there is no ground-truth fixture for this PDF."
    if not config.gemini_configured():
        # the running server does not re-read .env: a new key only works after a restart
        return (f"Running the control gate reads it from the extraction cache; if the file was never read, "
                f"{config.gemini_missing_setting()} must be set in .env (then restart the app, or run make extract).")
    return ("Running the control gate reads it first: from the extraction cache when this file was read before, "
            "else with one Gemini API call.")


def reading_saved(doc: InboundDocument) -> bool:
    """A reading of this file exists without calling the API: its cached Gemini reading, or (EXTRACTOR=fixture) its
    ground-truth fixture."""
    if config.EXTRACTOR == "fixture":
        path = resolve_document_file(doc.file_path)
        return path is not None and extract.fixture_path(path).exists()
    return bool(doc.file_hash) and extract.cache_path(doc.file_hash).exists()


def is_ubl_extraction(ex: Any) -> bool:
    """The extraction is a UBL e-invoice parsed directly (metrics.ubl_model), not a model output."""
    return bool(ex) and ex.model == metrics.ubl_model()


def provenance(ex: Any) -> tuple[str, str]:
    """(kind, the one provenance line) of an extraction: who read the fields and from where."""
    if extract.is_fixture_model(ex.model):
        return ("fixture", "FIXTURE — ground-truth test data, not a Gemini output (EXTRACTOR=fixture).")
    if is_ubl_extraction(ex):
        return ("ubl", "Parsed from the UBL e-invoice: structured XML, no model call.")
    return ("cache", f"Read by {ex.model} (cached).") if ex.from_cache else ("api", f"Read by {ex.model} (Gemini API).")


def extraction_context(doc: InboundDocument, message: Optional[tuple[str, str]] = None) -> dict[str, Any]:
    ex = doc.extraction
    return {
        "doc": doc,
        "ex": ex,
        # fields + confidence, nothing else (brief v2): the model's free-text notes are not shown
        "fields": [f for f in field_rows(ex.json) if f["name"] != "notes"] if ex else [],
        "provenance": provenance(ex) if ex else None,
        "is_fixture": bool(ex and extract.is_fixture_model(ex.model)),
        "is_ubl": is_ubl_extraction(ex),
        "no_extraction_reason": no_extraction_reason(doc),
        "threshold_pct": round(config.CONFIDENCE_THRESHOLD * 100),
        "panel_message": message,
        "doc_type_labels": DOC_TYPE_LABELS,
        "doc_type": doc_type_key(doc),
    }


def latest_decision(session: Session, doc_id: str) -> Optional[GateDecision]:
    return session.scalar(select(GateDecision).where(GateDecision.doc_id == doc_id)
                          .order_by(GateDecision.id.desc()).limit(1))


def trace_rows(steps: list[dict[str, Any]], scenario: str = "tobe") -> list[dict[str, str]]:
    """The rule log of one document, the same lines as the run log: rule (deck words), result word and tone,
    reason. Quiet steps (not reached, not applicable) are left out."""
    labels = gate.STEP_LABELS.get(scenario, {})
    return [{"label": labels.get(s.get("step", ""), str(s.get("step", "")).replace("_", " ").capitalize()),
             "result": gate.RESULT_WORDS.get(s.get("result") or "", s.get("result") or "—"),
             "tone": RESULT_TONES.get(s.get("result") or "", "neutral"),
             "detail": s.get("detail") or ""} for s in steps or [] if not s.get("quiet")]


def cycle_rows(breakdown: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Key, label and business days per activity of the simulated cycle (sim.cycle_breakdown order)."""
    return [{"key": k, "label": CYCLE_LABELS.get(k, k.replace("_", " ").capitalize()), "days": int(v or 0)}
            for k, v in (breakdown or {}).items()]


def line_label(row: dict[str, Any]) -> str:
    """Column header of one line check: "Line 2"; a check without an invoice line (no lines read) is the net
    total against the PO ("Net total") or one PO line ("PO line 1", "PO 4500123 line 1")."""
    if row.get("line") is not None:
        return f"Line {row['line']}"
    if row.get("po_line") is None:
        return "Net total"
    return f"PO {row['po_number']} line {row['po_line']}" if row.get("po_number") else f"PO line {row['po_line']}"


# Columns of the line-check table, in this order, when the gate provides them (else every key it provides).
# po_number: only on the rows of a multi-PO invoice.
LINE_CHECK_COLUMNS = ("line", "po_number", "description", "kind", "invoiced_qty", "received_qty", "unit_price",
                      "po_unit_price", "variance", "tolerance", "issues", "result")


def line_columns(checks: list[dict[str, Any]]) -> list[str]:
    keys = list(dict.fromkeys(k for c in checks for k in c))
    preferred = [k for k in LINE_CHECK_COLUMNS if k in keys]
    return preferred or keys


def doc_link(ref: Optional[str], session: Session) -> Optional[dict[str, str]]:
    """Link for a document id (A-01) or a pending vendor invoice id (PVI-A-0001)."""
    if not ref:
        return None
    if ref.startswith("PVI-"):
        pvi = session.scalar(select(PendingVendorInvoice).where(PendingVendorInvoice.invoice_id == ref))
        label = f"{ref} ({pvi.doc_id})" if pvi and pvi.doc_id else ref
        return {"href": f"/erp/pending-invoices#{ref}", "label": label}
    return {"href": f"/invoice/{ref}", "label": ref}


def gate_context(session: Session, doc: InboundDocument, message: Optional[tuple[str, str]] = None) -> dict[str, Any]:
    """Context of the gate panel (trace, outcome card, flags, cycle, badges): page and HTMX partial."""
    decision = latest_decision(session, doc.doc_id)
    gv = gate_view(decision, doc) if decision else None
    pvi = None
    if gv and gv.get("invoice_id"):
        pvi = session.scalar(select(PendingVendorInvoice).where(PendingVendorInvoice.invoice_id == gv["invoice_id"]))
    checks = [{**c, "label": line_label(c)} for c in (gv or {}).get("line_checks") or [] if isinstance(c, dict)]
    return {
        "doc": doc, "gv": gv, "pvi": pvi, "gate_message": message,
        "trace": trace_rows(decision.steps, doc.scenario) if decision else [],
        "outcome_words": gate.OUTCOME_WORDS.get(doc.scenario, OUTCOME_LABELS),
        "line_checks": checks, "line_columns": [c for c in line_columns(checks) if c != "label"],
        "resolution_labels": RESOLUTION_LABELS,
        "cycle": cycle_rows(gv.get("cycle_breakdown")) if gv else [],
        "duplicate_link": doc_link(gv.get("duplicate_of"), session) if gv else None,
        "applied_link": doc_link(gv.get("applied_to"), session) if gv else None,
        "draftable": bool(decision) and drafts.is_draftable(decision),
        "draft_label": drafts.DRAFT_LABEL,
    }


@app.get("/invoice/{doc_id}")
def invoice_page(doc_id: str, request: Request, session: Session = Depends(db.get_session)) -> Response:
    doc = get_document(session, doc_id)
    ctx = extraction_context(doc)
    ctx.update(gate_context(session, doc))
    ctx.update(page_title=f"Invoice {doc.doc_id}", registration=registration_info(doc),
               channel_label=CHANNEL_LABELS.get(doc.channel, doc.channel),
               doc_scenario_label=config.SCENARIO_LABELS.get(doc.scenario, doc.scenario),
               content=document_content(doc), dataset_label=DATASET_NAMES.get(doc.dataset, doc.dataset))
    return render(request, "invoice.html", ctx)


# A received file comes from any email sender, so it is never rendered as markup in the app's origin: a UBL
# e-invoice may carry an XHTML <script>, so XML is served as plain text, like an email body, in a CSP sandbox with no
# content sniffing. A PDF keeps the browser's viewer (a CSP sandbox breaks Chrome's viewer in the invoice-page frame).
FILE_TYPES = {".pdf": "application/pdf", ".xml": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
FILE_HEADERS = {"X-Content-Type-Options": "nosniff"}
TEXT_FILE_HEADERS = {**FILE_HEADERS, "Content-Security-Policy": "default-src 'none'; sandbox"}


def file_url(doc: InboundDocument) -> str:
    """/files/B-01.pdf, /files/B2-11.xml, /files/B2-14.txt: the document id plus its file's extension."""
    suffix = Path(doc.file_path).suffix.lower()
    return f"/files/{doc.doc_id}{suffix if suffix in FILE_TYPES else ''}"


def pretty_xml(data: bytes) -> str:
    """Indented XML text for display (escaped by the template); the raw text if it cannot be parsed or declares a
    DTD (never expanded)."""
    raw = data.decode("utf-8", errors="replace")
    if b"<!DOCTYPE" in data.upper():
        return raw
    try:
        pretty = minidom.parseString(data).toprettyxml(indent="  ")
    except Exception:  # not well-formed: show it as received
        return raw
    return "\n".join(line for line in pretty.splitlines() if line.strip())


def document_content(doc: InboundDocument) -> dict[str, Any]:
    """How the invoice page shows the received document: a PDF in a frame, a UBL e-invoice as indented XML text, or
    the email body; plus the email text that came with an attachment (e.g. a store manager's forwarding comment)."""
    kind = doc.content_type or "pdf"
    content: dict[str, Any] = {"kind": kind, "label": CONTENT_LABELS.get(kind, kind), "url": file_url(doc),
                               "text": None, "comment": None}
    if kind == "email_body":
        path = resolve_document_file(doc.file_path)
        content["text"] = doc.email_body or (path.read_text(encoding="utf-8", errors="replace") if path else None)
        return content
    if kind == "ubl_xml":
        path = resolve_document_file(doc.file_path)
        content["text"] = pretty_xml(path.read_bytes()) if path else None
    content["comment"] = doc.email_body
    return content


def rerun_message(decision: GateDecision, processed_first: Sequence[str] = ()) -> tuple[str, str]:
    label, _ = outcome_chip(decision.outcome, decision.exception_type, decision.scenario)
    owner = f", owner {decision.owner_name}" if decision.owner_name else ""
    sla = "" if decision.sla_days is None else f", SLA {plural(int(decision.sla_days), 'day')}"
    text = (f"{'Control gate' if decision.scenario == 'tobe' else 'Processing'}: {label}{owner}"
            f"{sla if decision.owner_name else ''}.")
    if processed_first:
        text += (f" {plural(len(processed_first), 'earlier document')} not processed yet went first, in processing "
                 f"order: {', '.join(processed_first)}.")
    return ("info", text)


def processed_doc_ids(session: Session, scenario: str) -> set[str]:
    return set(session.scalars(select(GateDecision.doc_id).where(GateDecision.scenario == scenario)))


@app.post("/invoice/{doc_id}/rerun")
def rerun_gate(doc_id: str, request: Request, session: Session = Depends(db.get_session)) -> Response:
    """Re-run the gate on one document. The gate first processes every earlier document of the scenario that has
    no decision yet (duplicate and credit-note checks look back), then this one. HTMX gets the gate panel
    partial; without JS, a redirect."""
    doc = get_document(session, doc_id)
    scenario = doc.scenario
    with _gate_lock:
        try:
            before = processed_doc_ids(session, doc.scenario)
            decision = gate.rerun_document(session, doc, allow_api=False)  # offline: cache or fixtures only
            new = processed_doc_ids(session, doc.scenario) - before - {doc_id}
            message = rerun_message(decision, [d.doc_id for d in scenario_documents(session, doc.scenario)
                                               if d.doc_id in new])
        except Exception as exc:  # never a 500: the panel shows a short message, the log has the error
            session.rollback()
            print(f"[rerun] doc={doc_id} FAILED: {exc!r}")
            message = ("error", f"The gate could not process {doc_id}; see the server log.")
    export_after_run(session, scenario, f"re-run of {doc_id}")
    session.expire_all()
    doc = get_document(session, doc_id)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_gate_rerun.html",
                                          {**extraction_context(doc), **gate_context(session, doc, message)})
    return redirect(f"/invoice/{doc.doc_id}", message)


@app.post("/invoice/{doc_id}/draft")
def draft_message(doc_id: str, request: Request, force: int = 0,
                  session: Session = Depends(db.get_session)) -> Response:
    """Draft a message to the owner of a to-be exception (drafts.py). Never raises: shows the reason."""
    doc = get_document(session, doc_id)
    decision = latest_decision(session, doc_id)
    draft, error = None, None
    try:
        draft = drafts.get_draft(decision, doc, allow_api=config.gemini_configured(), force=bool(force))
    except drafts.DraftUnavailable as exc:
        error = str(exc)
    except Exception as exc:  # anything unexpected: readable, not a 500
        print(f"[draft] doc={doc_id} error: {exc!r}")
        error = f"The draft could not be produced: {exc}"
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_draft.html", {
            "doc": doc, "draft": draft, "draft_error": error, "draft_label": drafts.DRAFT_LABEL,
            # a new draft needs the API: without a key, "Draft again" would only replace the cached text with an error
            "can_redraft": config.gemini_configured()})
    if draft:
        return redirect(f"/invoice/{doc_id}", ("info", f"{drafts.DRAFT_LABEL}: {draft.text}"))
    return redirect(f"/invoice/{doc_id}", ("warn", f"No draft: {error}"))


def api_error_text(exc: Exception) -> str:
    """'Gemini API error: <message>.' without doubling a prefix the message already has."""
    message = str(exc).rstrip(".")
    return f"{message}." if message.startswith("Gemini API error") else f"Gemini API error: {message}."


def run_extraction(session: Session, doc: InboundDocument, force: bool) -> tuple[str, str]:
    """Extract (or re-extract) one document. Never raises: returns a (kind, message) pair."""
    needs_model = (doc.content_type or "pdf") == "pdf"  # a UBL file is parsed; an email body has nothing to read
    if force and needs_model and config.EXTRACTOR != "fixture" and not config.gemini_configured():
        return ("warn", f"Force re-extract needs the Gemini API and {config.gemini_missing_setting()} is not set "
                        "in .env. The current extraction was kept.")
    had_extraction = doc.extraction is not None
    try:
        row = extract.extract_document(session, doc, force=force, allow_api=config.gemini_configured())
    except extract.ExtractionFailed as exc:  # the API was called but failed: the old row stays
        session.rollback()
        print(f"[extract] doc={doc.doc_id} API error: {exc}")
        return ("error", api_error_text(exc) + (" The current extraction was kept." if had_extraction else ""))
    except Exception as exc:  # anything else (bad cache file, DB error): report it, never 500
        session.rollback()
        print(f"[extract] doc={doc.doc_id} error: {exc!r}")
        return ("error", f"Extraction failed: {exc}")
    if row is None:
        return ("warn", "Extraction unavailable: nothing was extracted (see the reason below).")
    if extract.is_fixture_model(row.model):
        return ("info", "Loaded the ground-truth fixture (no API call; not a Gemini output).")
    if is_ubl_model(row.model):
        return ("info", "Parsed the UBL e-invoice (structured XML, no model call).")
    if row.from_cache:
        source = "from cache"
    elif row.latency_ms is not None:
        source = f"via the Gemini API in {row.latency_ms} ms"
    else:
        source = "via the Gemini API"
    return ("info", f"Extracted with {row.model} ({source}).")


def is_ubl_model(model: Optional[str]) -> bool:
    """The extraction came from the UBL parser (app/ubl.py, imported lazily), not from a model."""
    try:
        from app import ubl
    except ImportError:
        return False
    return model == ubl.UBL_MODEL


@app.post("/invoice/{doc_id}/extract")
def extract_now(doc_id: str, request: Request, force: int = 0,
                session: Session = Depends(db.get_session)) -> Response:
    """Extract now / force re-extract. HTMX gets the panel partial; without JS, a redirect."""
    doc = get_document(session, doc_id)
    message = run_extraction(session, doc, force=bool(force))
    session.refresh(doc)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_extraction_panel.html", extraction_context(doc, message))
    return redirect(f"/invoice/{doc.doc_id}", message)


def resolve_document_file(stored: str) -> Optional[Path]:
    """Absolute path of a stored document (relative to the project root, or absolute for INBOUND_DIR outside it),
    or None if it lies outside DATA_DIR and INBOUND_DIR or does not exist."""
    path = (Path(config.BASE_DIR) / stored).resolve()
    roots = [Path(config.DATA_DIR).resolve(), Path(config.INBOUND_DIR).resolve()]
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        return None
    return path


@app.get("/files/{name}")
def document_file(name: str, session: Session = Depends(db.get_session)) -> FileResponse:
    """The received file of a document: /files/B-01.pdf, /files/B2-11.xml, /files/B2-14.txt (or without the
    extension). Only files under DATA_DIR or INBOUND_DIR are served; XML and email text as sandboxed plain text."""
    stem, suffix = (name[: -len(ext)], ext) if (ext := Path(name).suffix.lower()) in FILE_TYPES else (name, "")
    doc = get_document(session, stem)
    path = resolve_document_file(doc.file_path)
    actual = path.suffix.lower() if path else ""
    if path is None or actual not in FILE_TYPES or (suffix and suffix != actual):
        raise HTTPException(status_code=404, detail="File not available")
    return FileResponse(path, media_type=FILE_TYPES[actual], filename=f"{doc.doc_id}{actual}",
                        content_disposition_type="inline",
                        headers=FILE_HEADERS if actual == ".pdf" else TEXT_FILE_HEADERS)


# --------------------------------------------------------------------------------------------
# ERP (mock, system of record): posted invoices, vendor master, purchase orders & receipts, contracts & catalogues
# --------------------------------------------------------------------------------------------


PVI_STATUS = {"pending_payment": ("pending payment", "neutral"), "credit_applied": ("credit applied", "vio"),
              "unapplied_credit": ("unapplied credit", "bad")}


def pvi_chips(row: PendingVendorInvoice) -> list[dict[str, Optional[str]]]:
    """Flag chips of a posted invoice: terms source (only when the row has terms: not for a credit note), wrong
    entity, duplicate of, credit, card / catalogue."""
    flags = row.flags or {}
    chips = []
    if row.terms_days is not None and row.terms_source:
        chips.append({"label": f"terms: {row.terms_source}", "tone": "ok" if row.terms_source == "master" else "warn",
                      "href": None})
    if flags.get("terms_variance"):
        chips.append({"label": "terms variance", "tone": "warn", "href": None})
    if flags.get("wrong_entity"):
        chips.append({"label": "wrong entity", "tone": "bad", "href": None})
    if flags.get("duplicate_of"):
        chips.append({"label": f"duplicate of {flags['duplicate_of']}", "tone": "bad",
                      "href": f"/invoice/{flags['duplicate_of']}"})
    if row.status == "unapplied_credit":
        chips.append({"label": "unapplied credit", "tone": "bad", "href": None})
    if flags.get("catalogue"):
        chips.append({"label": "card / catalogue", "tone": "ok", "href": None})
    return chips


@app.get("/erp/pending-invoices")
def pending_invoices(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    rows = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.scenario == scenario)
                           .order_by(PendingVendorInvoice.invoice_id)).all()
    return render(request, "erp_pending_invoices.html", {
        "page_title": "Posted invoices",
        "rows": [{"row": r, "chips": pvi_chips(r), "status": PVI_STATUS.get(r.status, (r.status, "neutral"))}
                 for r in rows],
        "run": latest_run(session, scenario),
    })


@dataclass
class VendorRow:
    account: VendorAccount
    party: Optional[Party]  # linked supplier, or the one found by tax ID / name for unlinked accounts
    party_found_by: Optional[str]  # None when linked; "tax ID" | "name" when inferred
    agreed_terms: Optional[int]
    agreed_source: Optional[str]  # contract id or "supplier agreement"
    duplicates: list[str] = field(default_factory=list)  # "V-000117 (VDE, similar name)"

    @property
    def terms_differ(self) -> bool:
        return self.agreed_terms is not None and self.account.payment_terms_days != self.agreed_terms

    @property
    def flags(self) -> list[tuple[str, str]]:
        """(label, css kind) pairs shown as highlight chips."""
        acc, out = self.account, []
        if self.duplicates:
            out.append(("possible duplicate", "dup"))
        if not acc.vat_id:
            out.append(("missing tax ID", "missing"))
        if not acc.iban:
            out.append(("missing IBAN", "missing"))
        if self.terms_differ:
            out.append(("terms ≠ agreed", "terms"))
        if acc.status == "inactive":
            out.append(("inactive", "inactive"))
        return out


def find_party(acc: VendorAccount, parties: list[Party]) -> tuple[Optional[Party], Optional[str]]:
    """Linked supplier; for unlinked accounts the supplier with the same tax ID, else a matching name."""
    if acc.party_id:
        return next((p for p in parties if p.party_id == acc.party_id), None), None
    vat = normalize.normalise_vat(acc.vat_id)
    if vat:
        for p in parties:
            if normalize.normalise_vat(p.vat_id) == vat:
                return p, "tax ID"
    for p in parties:
        if normalize.names_match(acc.display_name, p.canonical_name):
            return p, "name"
    return None, None


def agreed_terms(party: Optional[Party], entity: str, contracts: list[Contract]) -> tuple[Optional[int], Optional[str]]:
    """Terms of the recurring contract for party + entity, else the party's agreed terms."""
    if party is None:
        return None, None
    for c in contracts:
        if c.party_id == party.party_id and c.legal_entity_code == entity and c.recurring:
            return c.payment_terms_days, c.contract_id
    return party.agreed_terms_days, "supplier agreement"


def duplicate_reason(a: VendorAccount, b: VendorAccount) -> Optional[str]:
    """Why b looks like a duplicate of a, or None. One party with one account per legal entity is by design."""
    if a.party_id and a.party_id == b.party_id and a.legal_entity_code != b.legal_entity_code:
        return None
    vat_a = normalize.normalise_vat(a.vat_id)
    if vat_a and vat_a == normalize.normalise_vat(b.vat_id):
        return "same tax ID"
    if normalize.names_match(a.display_name, b.display_name):
        return "similar name"
    return None


def vendor_rows(accounts: list[VendorAccount], parties: list[Party], contracts: list[Contract]) -> list[VendorRow]:
    rows = []
    for acc in accounts:
        party, found_by = find_party(acc, parties)
        terms, source = agreed_terms(party, acc.legal_entity_code, contracts)
        dups = [f"{other.account_id} ({other.legal_entity_code}, {reason})" for other in accounts
                if other is not acc and (reason := duplicate_reason(acc, other))]
        rows.append(VendorRow(acc, party, found_by, terms, source, dups))
    return rows


def group_vendor_rows(rows: list[VendorRow], parties: list[Party]) -> list[tuple[str, list[VendorRow]]]:
    """Groups by linked supplier (in supplier order); unlinked accounts last."""
    groups = []
    for p in parties:
        linked = [r for r in rows if r.account.party_id == p.party_id]
        if linked:
            groups.append((p.canonical_name, linked))
    unlinked = [r for r in rows if not r.account.party_id]
    if unlinked:
        groups.append(("Not linked to a supplier", unlinked))
    return groups


def vendor_stats(rows: list[VendorRow], n_parties: int) -> dict[str, Any]:
    """Tiles of the vendor master page; n_parties = unique suppliers as the metric counts them."""
    n = len(rows)
    complete = sum(1 for r in rows if r.account.vat_id and r.account.iban)
    return {
        "accounts": n,
        "suppliers": n_parties,
        "ratio": f"{n / n_parties:.2f}" if n_parties else "—",
        "active": sum(1 for r in rows if r.account.status == "active"),
        "pct_complete": round(100 * complete / n) if n else 0,
        "terms_differ": sum(1 for r in rows if r.terms_differ),
        "duplicates": sum(1 for r in rows if r.duplicates),
    }


@app.get("/erp/vendors")
def vendor_master(request: Request, view: Optional[str] = None,
                  session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    view = check_scenario(view) if view else scenario
    accounts = session.scalars(select(VendorAccount).where(VendorAccount.scenario == view)
                               .order_by(VendorAccount.account_id)).all()
    parties = session.scalars(select(Party).where(Party.scenario == view).order_by(Party.party_id)).all()
    contracts = session.scalars(select(Contract).where(Contract.scenario == view)).all()
    rows = vendor_rows(list(accounts), list(parties), list(contracts))
    suppliers = metrics.vendor_master_stats(list(accounts), list(parties), list(contracts))["parties"]
    return render(request, "erp_vendors.html", {
        "page_title": "Vendor master", "view": view, "view_label": config.SCENARIO_LABELS[view],
        "groups": group_vendor_rows(rows, list(parties)), "stats": vendor_stats(rows, suppliers),
        "rule_help": seed.RULE_DESCRIPTIONS,
    })


def receipt_status(category: str, ordered: float, received: float, receipt_required: bool) -> tuple[str, str]:
    """(label, tone) for a PO line: goods fully/partially/not received; services confirmed or not."""
    if not receipt_required:
        return "No receipt required", "neutral"
    if category == "service":
        return ("Service confirmed", "ok") if received > 0 else ("Not confirmed", "bad")
    if received >= ordered:
        return "Fully received", "ok"
    return ("Partially received", "warn") if received > 0 else ("Not received", "bad")


@app.get("/erp/purchase-orders")
def purchase_orders(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    pos = session.scalars(select(PurchaseOrder).where(PurchaseOrder.scenario == scenario)
                          .order_by(PurchaseOrder.po_number)).all()
    accounts = {a.account_id: a for a in session.scalars(
        select(VendorAccount).where(VendorAccount.scenario == scenario))}
    receipts: dict[tuple[str, int], list[ProductReceipt]] = {}
    for r in session.scalars(select(ProductReceipt).where(ProductReceipt.scenario == scenario)
                             .order_by(ProductReceipt.received_on)):
        receipts.setdefault((r.po_number, r.line_no), []).append(r)
    views = []
    for po in pos:
        lines = []
        for ln in po.lines:
            rows = receipts.get((po.po_number, ln.line_no), [])
            received = sum(r.qty_received for r in rows)
            label, tone = receipt_status(po.category, ln.qty, received, ln.receipt_required)
            lines.append({"line": ln, "received": received, "status": label, "tone": tone, "receipts": rows})
        views.append({"po": po, "account": accounts.get(po.vendor_account_id), "lines": lines})
    return render(request, "erp_purchase_orders.html", {
        "page_title": "Purchase orders & receipts", "pos": views,
        "total_pos": len(world.PURCHASE_ORDERS),
    })


@app.get("/erp/contracts")
def contracts(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    rows = session.scalars(select(Contract).where(Contract.scenario == scenario)
                           .order_by(Contract.contract_id)).all()
    parties = {p.party_id: p for p in session.scalars(select(Party).where(Party.scenario == scenario))}
    return render(request, "erp_contracts.html", {
        "page_title": "Contracts & catalogues", "parties": parties,
        "rows": [r for r in rows if r.category != "catalogue"],
        "catalogues": [r for r in rows if r.category == "catalogue"],
    })


# --------------------------------------------------------------------------------------------
# Control gate: exception cockpit, KPIs, compare, assumptions
# --------------------------------------------------------------------------------------------


def blocking_exception(v: dict[str, Any]) -> bool:
    """Routed, blocking exception (the cockpit queue): not posted, has a taxonomy type, not an info flag."""
    return (v["outcome"] in ("exception", "human_review") and bool(v["exception_type"])
            and v["exception_type"] != "email_loop" and v["exception_type"] not in taxonomy.INFO_TYPES)


def owner_counts(queue: list[dict[str, Any]], info: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Owners (current or next) with the number of items that mention them."""
    counts: dict[str, int] = {}
    for v in queue:
        for name in {v["owner_name"], v.get("next_owner_name")} - {None, ""}:
            counts[name] = counts.get(name, 0) + 1
    for item in info:
        if item["flag"].get("owner_name"):
            counts[item["flag"]["owner_name"]] = counts.get(item["flag"]["owner_name"], 0) + 1
    return sorted(counts.items())


def group_by_type(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exception groups in taxonomy order: type, label, owner role, SLA, standard resolution, rows."""
    groups = []
    for t in taxonomy.EXCEPTION_TYPES:
        rows = [v for v in queue if v["exception_type"] == t.key]
        if rows:
            groups.append({"type": t, "rows": rows})
    known = {t.key for t in taxonomy.EXCEPTION_TYPES}
    for key in sorted({v["exception_type"] for v in queue} - known):  # never drop an unknown type
        groups.append({"type": None, "key": key, "rows": [v for v in queue if v["exception_type"] == key]})
    return groups


def info_task(view: dict[str, Any], flag: dict[str, Any], doc: Optional[InboundDocument],
              as_of: Optional[datetime]) -> dict[str, Any]:
    """One non-blocking info task of the cockpit (a duplicate vendor record): its SLA from the taxonomy and its days
    open from registration to the snapshot (the document itself was posted)."""
    t = taxonomy.BY_KEY.get(flag.get("type") or "")
    sla = t.sla_days if t else None
    days = age_days(doc, as_of)
    days = None if days is None else max(days, 0)
    return {"view": view, "flag": flag, "sla_days": sla, "days_open": days,
            "past_sla": sla is not None and days is not None and days > sla}


@app.get("/gate/exceptions")
def exception_cockpit(request: Request, owner: Optional[str] = None,
                      session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    docs = {d.doc_id: d for d in scenario_documents(session, scenario)}
    decisions = decisions_by_doc(session, scenario)
    routing = bool(gate.SCENARIOS[scenario]["exception_routing"])
    plain = [gate_view(decisions[i], d) for i, d in docs.items() if i in decisions]
    queued = [v["doc_id"] for v in plain if (blocking_exception(v) if routing else v["is_email_loop"])]
    live = {i for i, d in docs.items() if d.dataset == seed.LIVE_DATASET}
    # Sample documents live in the simulated calendar: aged to the snapshot of their run. Documents received live
    # through the intake webhook carry real dates: aged to today, and never counted in the snapshot.
    as_of = sim.cockpit_as_of(docs[i].registered_on or docs[i].received_on for i in queued if i not in live)
    now = datetime.now()
    views = [gate_view(decisions[i], d, now if i in live else as_of) for i, d in docs.items() if i in decisions]
    queue = [v for v in views if blocking_exception(v)]
    info = [info_task(v, f, docs.get(v["doc_id"]), now if v["doc_id"] in live else as_of)
            for v in views for f in v["flags"] if isinstance(f, dict)]
    owners = owner_counts(queue, info)
    owner = owner or None
    if owner:
        queue = [v for v in queue if owner in (v["owner_name"], v.get("next_owner_name"))]
        info = [item for item in info if item["flag"].get("owner_name") == owner]
    loop = [v for v in views if v["is_email_loop"]]
    loop_sample = [v for v in loop if v["doc_id"] not in live]
    return render(request, "exceptions.html", {
        "page_title": "Exception cockpit", "has_run": bool(decisions),
        "routing": routing,
        "groups": group_by_type(queue), "queue_count": len(queue), "info": info,
        "past_sla_count": sum(1 for v in queue if v["past_sla"]),
        # as-is: every document that went through the loop; the snapshot tells which sample documents were still in
        # it (live documents are reported apart)
        "loop": loop, "loop_sample": len(loop_sample),
        "loop_open": sum(1 for v in loop_sample if not v["posted_by_snapshot"]),
        "loop_live": len(loop) - len(loop_sample),
        "blocked": [v for v in views if v["outcome"] == "blocked_duplicate"],
        "owners": owners, "owner": owner, "as_of": as_of, "ap_specialist": world.AP_SPECIALIST.name,
        "has_live": any(i in live for i in queued),
    })


# Direction of "better" for the comparison page: +1 higher is better, -1 lower is better.
KPI_BETTER = {"first_pass_match_rate": 1, "accounts_per_supplier": -1, "touchless_rate": 1, "cycle_time_median": -1,
              "registered_same_day": 1}


def kpi_layout(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the four metrics of the deck's slide 11 in its order, the small indicators) for metric tiles or compare rows
    (dicts with "key"). Every item is placed exactly once."""
    by_key = {t["key"]: t for t in items}
    headline = [by_key[k] for k in metrics.HEADLINE if k in by_key]
    return headline, [t for t in items if t["key"] not in metrics.HEADLINE]


# Chart colours: the outcome colours of the pills (OUTCOME_TONES, app/static/app.css), so a colour means the same
# thing on every page: Post = emerald, Block = slate, Human review = amber, Exception = red, email loop = orange.
# Cycle bars: no human step = emerald, exception with SLA = red, email loop = orange. Legends and data tables name
# every colour.
CHART_COLOURS = {"Post": "#059669", "Exception": "#dc2626", "Block": "#475569", "Human review": "#f59e0b",
                 "Posted by AP": "#059669", "Email loop — untracked": "#ea580c", "Blocked (same account)": "#475569"}
CHART_NEUTRAL = "#64748b"
EXCEPTION_BAR_COLOURS = {"email_loop": "#ea580c", "human_review": "#f59e0b", "amount_above_approval_limit": "#f59e0b"}
PATH_COLOURS = {"matched": "#059669", "touchless": "#059669", "email_loop": "#ea580c", "exception": "#dc2626"}


def ordered_kpis(kpis: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Metrics in metrics' display order, each with its footnote number."""
    return [{**kpi, "key": key, "note": i} for i, (key, kpi) in enumerate(kpis.items(), start=1)]


def kpi_display(kpi: Optional[dict[str, Any]], available: bool) -> str:
    """Display text of one metric tile; every metric but accounts per supplier needs a run."""
    if kpi is None:
        return "—"
    if not available and kpi.get("value") is None:
        return "Run the scenario"
    return str(kpi.get("display") if kpi.get("display") not in (None, "") else "—")


def split_formula(formula: Optional[str]) -> tuple[str, str]:
    """(definition, numbers) of a metric's formula text: '... Here: 8 of 11.' -> ('...', '8 of 11.')."""
    definition, _, numbers = (formula or "").partition(" Here: ")
    return definition, numbers


CHART_LABEL_WIDTH = 22  # characters per line of a category label on a chart axis


def label_lines(label: str, width: int = CHART_LABEL_WIDTH) -> list[str]:
    """A long category label as lines of about `width` characters (Chart.js draws an array as several lines),
    so the axis never cuts it off. Words are never split."""
    return textwrap.wrap(label, width=width, break_long_words=False, break_on_hyphens=False) or [label]


def chart_data(data: dict[str, Any]) -> dict[str, Any]:
    """Chart.js inputs: outcomes (doughnut), exceptions by type (bar), cycle days per document (bar)."""
    outcomes = data.get("outcomes") or {}
    by_type = data.get("exceptions_by_type") or {}
    cycle = data.get("cycle_by_doc") or []
    type_labels = [EMAIL_LOOP_LABEL if k == "email_loop" else taxonomy.label(k) for k in by_type]  # deck A3 words
    return {
        "outcomes": {"labels": list(outcomes), "values": list(outcomes.values()),
                     "colours": [CHART_COLOURS.get(k, CHART_NEUTRAL) for k in outcomes]},
        "exceptions": {"labels": type_labels, "lines": [label_lines(label) for label in type_labels],
                       "values": list(by_type.values()),
                       "colours": [EXCEPTION_BAR_COLOURS.get(k, CHART_COLOURS["Exception"]) for k in by_type]},
        "cycle": {"labels": [c.get("doc_id") for c in cycle], "values": [c.get("days") for c in cycle],
                  "paths": [path_label(c.get("path"), c.get("outcome")) for c in cycle],
                  "colours": [CHART_COLOURS["Block"] if c.get("outcome") == "blocked_duplicate"
                              else PATH_COLOURS.get(c.get("path") or "", CHART_NEUTRAL) for c in cycle]},
    }


def safe_metrics(call: Callable[[], dict[str, Any]], what: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """metrics.* result, or (None, error): a metrics bug must not turn a page into a 500."""
    try:
        return call(), None
    except Exception as exc:
        print(f"[metrics] {what} FAILED: {exc!r}")
        return None, f"The {what} could not be computed: {exc}"


def partial_run_warning(data: Optional[dict[str, Any]]) -> Optional[str]:
    """'Only N of M documents processed — run the scenario' when the KPIs cover part of the documents only
    (e.g. after re-running single documents); None when every document has a decision or nothing ran."""
    if not data or not data.get("available"):
        return None
    done, total = int(data.get("documents") or 0), int(data.get("documents_total") or 0)
    if done >= total:
        return None
    return f"Only {done} of {total} documents processed — run the scenario"


@app.get("/gate/kpis")
def kpis(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    data, error = safe_metrics(lambda: metrics.compute(session, scenario), "KPIs")
    tiles = ordered_kpis(data.get("kpis") or {}) if data else []
    available = bool(data and data.get("available"))
    for t in tiles:
        t["shown"] = kpi_display(t, available)
    headline, small = kpi_layout(tiles)
    return render(request, "kpis.html", {
        "page_title": "Metrics", "data": data, "error": error, "available": available, "small": small,
        "headline": headline,
        "tiles": tiles, "charts": chart_data(data) if data and available else None,
        "partial_warning": partial_run_warning(data),
    })


def better_side(key: str, a: Optional[dict[str, Any]], b: Optional[dict[str, Any]]) -> Optional[str]:
    """'asis' | 'tobe' for the better value, None when equal, unknown or not comparable."""
    direction = KPI_BETTER.get(key)
    va, vb = (a or {}).get("value"), (b or {}).get("value")
    if not direction or not isinstance(va, (int, float)) or not isinstance(vb, (int, float)) or va == vb:
        return None
    return "asis" if (va - vb) * direction > 0 else "tobe"


def compare_kpi_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per metric, A vs B; the tooltip is the definition with each scenario's own numbers."""
    a_data, b_data = data.get("asis") or {}, data.get("tobe") or {}
    a_kpis, b_kpis = a_data.get("kpis") or {}, b_data.get("kpis") or {}
    rows = []
    for tile in ordered_kpis(a_kpis or b_kpis):  # same keys in both scenarios
        key = tile["key"]
        a, b = a_kpis.get(key), b_kpis.get(key)
        comparable = key == "accounts_per_supplier" or data.get("both_available")
        definition, a_numbers = split_formula((a or {}).get("formula"))
        b_numbers = split_formula((b or {}).get("formula"))[1]
        numbers = " · ".join(f"{side}: {n.rstrip('.')}" for side, n in (("A", a_numbers), ("B", b_numbers)) if n)
        rows.append({**tile, "formula": definition + (f" {numbers}." if numbers else ""),
                     "asis": kpi_display(a, bool(a_data.get("available"))),
                     "tobe": kpi_display(b, bool(b_data.get("available"))),
                     "better": better_side(key, a, b) if comparable else None})
    return rows


def compare_cell(cell: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not cell:
        return None
    scenario = "asis" if str(cell.get("doc_id") or "").startswith("A") else "tobe"
    label, tone = outcome_chip(cell.get("outcome"), cell.get("exception_type"), scenario)
    return {**cell, "chip_label": label, "chip_tone": tone,
            "badge_chips": badge_chips(cell.get("badges") or [], label)}


@app.get("/gate/compare")
def compare(request: Request, session: Session = Depends(db.get_session)) -> Response:
    data, error = safe_metrics(lambda: metrics.compare(session), "comparison")
    kpi_rows = compare_kpi_rows(data) if data else []
    headline, small = kpi_layout(kpi_rows)
    rows = [{**r, "a": compare_cell(r.get("asis")), "b": compare_cell(r.get("tobe"))}
            for r in (data or {}).get("rows") or []]
    partial = [(config.SCENARIO_LABELS[s], warning) for s in config.SCENARIOS
               if (warning := partial_run_warning((data or {}).get(s)))]
    return render(request, "compare.html", {
        "page_title": "Compare", "data": data, "error": error, "rows": rows, "partial_warnings": partial,
        "both_available": bool(data and data.get("both_available")),
        "available": {s: bool(data and (data.get(s) or {}).get("available")) for s in config.SCENARIOS},
        "headline": headline, "small": small,
        "loaded": {s: (data or {}).get("datasets", {}).get(s) for s in config.SCENARIOS},
    })


@app.get("/assumptions")
def assumptions(request: Request) -> Response:
    path = config.DOCS_DIR / "ASSUMPTIONS.md"
    html = None
    if path.is_file():
        html = markdown.markdown(path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
    return render(request, "assumptions.html", {"page_title": "Assumptions", "html": html})


# ============================================================================================
# Real intake: the IMAP poller (app/imap_poll.py) or an n8n IMAP trigger posts every email here.
#
# POST /intake/webhook  (multipart/form-data; basic auth applies when APP_PASSWORD is set)
#   file        optional: a PDF (%PDF- magic bytes) or a UBL e-invoice (XML), at most 10 MB
#               Content-Length is required (411 without it; 413 before parsing if clearly too large).
#   email_body  optional text; required without a file (the invoice is then only in the email body); with a
#               file it is the email text, e.g. a store manager's forwarding comment
#   channel     ap_mailbox | store_mailbox
#   sender      sender email address
#   subject     optional
#   scenario    optional: asis | tobe (default: the active scenario cookie, else tobe)
#   message_id  optional: the email's Message-ID; the same message and file name (or body) again -> 200 with the
#               existing doc_id, nothing created
# Saves INBOUND_DIR/<sha256[:16]>.pdf|.xml|.txt, registers an InboundDocument ("B-W01", dataset "live") per the
# scenario's registration rule, extracts it (a model call only when Gemini is configured; UBL is parsed) and runs
# the gate on it, so it shows in the inbox and the exception cockpit. Returns 201 with the gate's outcome.
# ============================================================================================

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 64 * 1024  # form fields and multipart boundaries around the file
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INBOUND_SUFFIX = {"pdf": ".pdf", "ubl_xml": ".xml", "email_body": ".txt"}

# One process (brief): a lock is enough to keep doc_id allocation + insert atomic across the
# threadpool, so two concurrent uploads never get the same id (IntegrityError -> 500).
_webhook_lock = threading.Lock()


@app.middleware("http")
async def limit_webhook_upload(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Reject oversized webhook uploads from the Content-Length header, before multipart parsing."""
    if request.url.path == WEBHOOK_PATH and request.method == "POST":
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return JSONResponse({"detail": "Content-Length header required"}, status_code=411)
        if int(length) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES:
            return JSONResponse({"detail": "request too large: the file must be at most 10 MB"}, status_code=413)
    return await call_next(request)


def next_webhook_doc_id(session: Session, scenario: str) -> str:
    """Next free id for a webhook document: A-W01, A-W02, ... / B-W01, ..."""
    letter = seed.SCENARIO_LETTER[scenario]
    prefix = f"{letter}-W"
    existing = session.scalars(select(InboundDocument.doc_id).where(InboundDocument.doc_id.like(f"{prefix}%")))
    numbers = [int(i[len(prefix):]) for i in existing if i[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:02d}"


def webhook_scenario(request: Request, value: Optional[str]) -> str:
    """The form's scenario, else the active scenario cookie, else to-be (a mail poller sends no cookie)."""
    if value:
        return check_scenario(value)
    cookie = request.cookies.get(SCENARIO_COOKIE, "")
    return cookie if cookie in config.SCENARIOS else "tobe"


def looks_like_ubl(content: bytes) -> bool:
    """A UBL Invoice or CreditNote (app/ubl.py, imported lazily); False when the parser is not available."""
    try:
        from app import ubl
    except ImportError:
        return False
    return ubl.is_ubl(content)


def attachment_kind(content: bytes) -> Optional[str]:
    """content_type of an attached file: "pdf" (%PDF- magic bytes) or "ubl_xml"; None for anything else."""
    if content.startswith(b"%PDF-"):
        return "pdf"
    return "ubl_xml" if looks_like_ubl(content) else None


def read_attachment(file: Optional[UploadFile]) -> tuple[Optional[bytes], Optional[str]]:
    """(content, file name) of the uploaded file; (None, None) when no file (or an empty one) was sent."""
    if file is None:
        return None, None
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file larger than 10 MB")
    return (content, (file.filename or "").strip()[:200] or None) if content else (None, None)


def existing_webhook_document(session: Session, scenario: str, message_id: str, kind: str,
                              source_name: Optional[str], sha: str) -> Optional[InboundDocument]:
    """A document of the scenario already stored from the same email: same Message-ID and the same file name, or,
    for a body-only email, the same body."""
    for doc in session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario,
                                                             InboundDocument.message_id == message_id)):
        if kind == "email_body" and doc.content_type == "email_body" and doc.file_hash == sha:
            return doc
        if kind != "email_body" and doc.content_type != "email_body" and doc.source_name == source_name:
            return doc
    return None


def webhook_result(doc: InboundDocument, decision: Optional[GateDecision], registered: bool) -> dict[str, Any]:
    return {"doc_id": doc.doc_id, "scenario": doc.scenario, "registered": registered,
            "extracted": doc.extraction is not None, "outcome": decision.outcome if decision else None,
            "exception_type": decision.exception_type if decision else None,
            "owner_name": decision.owner_name if decision else None}


def gate_one_document(session: Session, doc: InboundDocument) -> Optional[GateDecision]:
    """Run the gate on a newly received document, as "Re-run gate" does: every earlier document of the scenario
    without a decision (e.g. a sample set loaded but not run yet) is processed first, in processing order, so the
    duplicate, credit-note and contract checks see them. Never raises.

    After a failed model call on this document the earlier ones use the cache only (one failed call is enough)."""
    allow_api = config.gemini_configured() and (doc.extraction is not None or doc.content_type == "email_body")
    with _gate_lock:
        try:
            before = processed_doc_ids(session, doc.scenario)
            decision = gate.rerun_document(session, doc, allow_api=allow_api)
            first = [d.doc_id for d in scenario_documents(session, doc.scenario)
                     if d.doc_id not in before and d.doc_id != doc.doc_id]
            if first:
                print(f"[intake] {plural(len(first), 'earlier document')} not processed yet went first, in processing "
                      f"order: {', '.join(first)}")
            return decision
        except Exception as exc:  # the document is stored; Run scenario or Re-run gate can process it later
            session.rollback()
            print(f"[intake] gate on {doc.doc_id} FAILED: {exc!r}")
            return None


@app.post(WEBHOOK_PATH, status_code=201)
def intake_webhook(
    request: Request,
    file: Optional[UploadFile] = File(None),
    email_body: Optional[str] = Form(None),
    channel: str = Form(...),
    sender: str = Form(...),
    subject: Optional[str] = Form(None),
    scenario: Optional[str] = Form(None),
    message_id: Optional[str] = Form(None),
    session: Session = Depends(db.get_session),
) -> JSONResponse:
    scenario = webhook_scenario(request, scenario)
    if channel not in world.MAILBOX_BY_CHANNEL:
        raise HTTPException(status_code=400, detail="channel must be ap_mailbox or store_mailbox")
    if not _EMAIL_RE.match(sender.strip()):
        raise HTTPException(status_code=400, detail="sender must be an email address")
    body = (email_body or "").strip() or None
    content, source_name = read_attachment(file)
    if content is None and body is None:
        raise HTTPException(status_code=400, detail="email_body is required when no file is attached")
    kind = attachment_kind(content) if content is not None else "email_body"
    if kind is None:
        raise HTTPException(status_code=400, detail="file is not a PDF or a UBL e-invoice (XML)")

    data = content if content is not None else body.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    inbound_dir = Path(config.INBOUND_DIR)
    path = inbound_dir / f"{sha[:16]}{INBOUND_SUFFIX[kind]}"
    msg_id = (message_id or "").strip()[:250] or None
    received_on = datetime.now().replace(microsecond=0)
    registered = sim.registration_delay_days(scenario, channel) == 0  # to-be: registered on arrival
    with _webhook_lock:  # idempotency check, file write, id allocation and insert: one upload at a time
        known = existing_webhook_document(session, scenario, msg_id, kind, source_name, sha) if msg_id else None
        if known is not None:
            print(f"[intake] webhook message_id={msg_id} already stored as {known.doc_id}: nothing created")
            on_arrival = sim.registration_delay_days(known.scenario, known.channel) == 0
            result = webhook_result(known, latest_decision(session, known.doc_id), on_arrival)
            return JSONResponse({**result, "existing": True}, status_code=200)
        inbound_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        doc = InboundDocument(
            doc_id=next_webhook_doc_id(session, scenario), scenario=scenario,
            sample_no=0,  # 0 = not one of the sample documents
            channel=channel, mailbox=world.MAILBOX_BY_CHANNEL[channel], received_on=received_on,
            file_path=seed.stored_path(path), file_hash=sha, sender_email=sender.strip(),
            subject=((subject or "").strip() or f"(no subject) {source_name or ''}".strip())[:200],
            registered=registered, registered_on=received_on if registered else None, doc_type="unknown",
            dataset=seed.LIVE_DATASET, content_type=kind, email_body=body, message_id=msg_id,
            source_name=source_name,
        )
        session.add(doc)
        session.commit()
    if kind == "email_body":  # nothing to extract: AP keys it from the email (the gate routes it)
        extraction = "no document attached"
    else:
        extraction = ": ".join(run_extraction(session, doc, force=False))
    decision = gate_one_document(session, doc)
    session.refresh(doc)
    print(f"[intake] webhook doc={doc.doc_id} scenario={scenario} channel={channel} content={kind} "
          f"registered={registered} extracted={doc.extraction is not None} ({extraction}) "
          f"outcome={decision.outcome if decision else 'not processed'}")
    result = webhook_result(doc, decision, registered)
    export_after_run(session, scenario, f"intake of {doc.doc_id}")
    return JSONResponse(result, status_code=201)


# --------------------------------------------------------------------------------------------
# Basic auth (brief section 17): one shared password in front of every page when APP_PASSWORD is set
# --------------------------------------------------------------------------------------------


class OptionalBasicAuth:
    """Outermost ASGI layer: app/auth.py's BasicAuthMiddleware when config.APP_PASSWORD is set, else a pass-through
    (the local default). The settings are read per request, so both ways can be tested without a restart; the guard
    is rebuilt only when they change."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app
        self._guard: Optional[Callable[..., Awaitable[None]]] = None
        self._credentials: Optional[tuple[str, str]] = None

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http" or not config.APP_PASSWORD:
            await self.app(scope, receive, send)
            return
        credentials = (config.APP_USERNAME, config.APP_PASSWORD)
        if self._guard is None or self._credentials != credentials:
            from app import auth

            self._guard = auth.BasicAuthMiddleware(self.app, username=credentials[0], password=credentials[1])
            self._credentials = credentials
        await self._guard(scope, receive, send)


app.add_middleware(OptionalBasicAuth)  # added last: the outermost layer, before the other middleware
