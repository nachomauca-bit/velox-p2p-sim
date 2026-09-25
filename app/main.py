"""FastAPI app: mock ERP pages (read-only), control-gate pages and intake.

Server-rendered with Jinja2 + HTMX partials, Pico.css and Chart.js from a CDN. The active scenario
("asis" | "tobe") lives in a cookie; any GET also accepts ?scenario=asis|tobe (sets the cookie).
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

import jinja2
import markdown
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
    "asis": "Dirty vendor master, weak PO discipline, two intake mailboxes, no control gate.",
    "tobe": "Clean vendor master, POs and contracts for most spend, registration on arrival, "
            "control gate with an exception cockpit.",
}

# Left navigation: two visibly separate groups (brief section 11). The split is the architecture
# message: the ERP stays the system of record, the gate sits in front of it.
NAV_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Velox ERP (mock, system of record)", [
        ("/erp/pending-invoices", "Pending vendor invoices"),
        ("/erp/vendors", "Vendor master"),
        ("/erp/purchase-orders", "Purchase orders & receipts"),
        ("/erp/contracts", "Contracts"),
    ]),
    ("Control gate (new)", [
        ("/inbox", "Inbox"),
        ("/gate/exceptions", "Exception cockpit"),
        ("/gate/kpis", "KPIs"),
        ("/gate/compare", "Compare"),
        ("/assumptions", "Assumptions"),
    ]),
]

DOC_TYPE_LABELS = {"invoice": "Invoice", "credit_note": "Credit note", "unknown": "Type unknown"}
CHANNEL_LABELS = {"ap_mailbox": "AP shared mailbox", "store_mailbox": "Store mailbox (Berlin 01)"}


# --------------------------------------------------------------------------------------------
# App, templates, formatting filters
# --------------------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create tables (never drop) and seed the mock ERP if the database is empty."""
    db.init_db()
    with db.SessionLocal() as session:
        if session.scalar(select(func.count()).select_from(LegalEntity)) == 0:
            seed.seed_all(session)
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


def fmt_datetime(value: Optional[datetime]) -> str:
    return "—" if value is None else value.strftime("%a %Y-%m-%d %H:%M")


def fmt_bank(value: Optional[str]) -> str:
    return world.format_iban(value) if value else "—"


templates.env.filters.update(money=fmt_money, qty=fmt_qty, d=fmt_date, dt=fmt_datetime, bank=fmt_bank)


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
    return {"kind": kind if kind in ("info", "warn", "error") else "info", "text": text}


def nav_active(path: str) -> str:
    return "/inbox" if path.startswith("/invoice/") else path


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
        "footer_text": config.FOOTER_TEXT,
        "flash": flash,
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


@app.post("/documents/load")
def load_documents(request: Request, scenario: Optional[str] = Form(None),
                   session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Load the sample PDFs into both mailboxes of the scenario and extract them.

    The previous gate results of the scenario are cleared first: they belong to the old documents. The extraction
    runs inside the gate lock too, so a Run clicked meanwhile waits instead of reading half-written rows.
    """
    scenario = form_scenario(request, scenario)
    with _gate_lock:
        gate.clear_results(session, scenario)
        docs = seed.load_sample_documents(session, scenario)
        for doc in docs:
            reg = registration_info(doc)
            print(f"[doc {doc.doc_id}] step=intake result={'registered' if doc.registered else 'waiting'} "
                  f"mailbox={doc.mailbox} registration={reg['date'].date().isoformat()}")
        try:
            summary = extract.extract_documents(session, docs, allow_api=bool(config.GEMINI_API_KEY))
        except Exception as exc:  # per-document failures are counted; anything else must not break the intake
            session.rollback()
            print(f"[intake] scenario={scenario} loaded={len(docs)} extraction error: {exc!r}")
            summary = None
    if summary is None:
        response = redirect("/inbox", ("error", f"{len(docs)} documents loaded, but the extraction failed; "
                                                "see the server log."))
    else:
        print(f"[intake] scenario={scenario} loaded={len(docs)} extracted={summary['extracted']} "
              f"from_cache={summary['from_cache']} unavailable={summary['unavailable']} "
              f"failed={summary.get('failed', 0)}")
        response = redirect("/inbox", load_message(len(docs), scenario, summary))
    set_scenario_cookie(response, scenario)
    return response


def load_message(loaded: int, scenario: str, summary: dict[str, Any]) -> tuple[str, str]:
    """Flash (kind, text) for "Load sample documents" from the extract_documents summary."""
    failed, unavailable = summary.get("failed", 0), summary["unavailable"]
    text = (f"{loaded} documents loaded into the mailboxes of {config.SCENARIO_LABELS[scenario]}; "
            f"{summary['extracted']} extracted ({summary['from_cache']} from cache).{extraction_mode_note()}")
    if failed:
        error = str(summary.get("error") or "unknown error")[:300].rstrip(".")  # the flash lives in a cookie
        text += f" {plural(failed, 'document')} failed: {error}."
    if unavailable:
        text += f" Extraction pending for {plural(unavailable, 'document')}"
        if config.EXTRACTOR != "fixture" and not config.GEMINI_API_KEY:
            text += ": set GEMINI_API_KEY in .env and run make extract."
        elif config.GEMINI_API_KEY:
            text += " (retry with Extract now on the invoice page, or run make extract)."
        else:
            text += "."
    if failed:
        return ("error", text)
    return ("warn", text) if unavailable else ("info", text)


# Pages of a single document: a reset deletes the scenario's documents (webhook uploads are not
# restored), so never redirect back there.
DOCUMENT_PAGE_PREFIXES = ("/invoice/", "/files/")

# One process (brief): runs, resets and re-runs of the gate are serialised so two clicks never
# interleave their deletes and inserts.
_gate_lock = threading.Lock()


@app.post("/reset")
def reset(request: Request, scenario: Optional[str] = Form(None),
          session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Restore the scenario for the demo: ERP re-seeded, gate results cleared, sample documents reloaded
    (unprocessed) and extracted from the cache or fixtures only (never the API)."""
    scenario = form_scenario(request, scenario)
    with _gate_lock:
        gate.clear_results(session, scenario)
        seed.reset_scenario(session, scenario)
        docs = seed.load_sample_documents(session, scenario)
        try:
            summary = extract.extract_documents(session, docs, allow_api=False)
        except Exception as exc:  # the reset itself succeeded; report the extraction problem
            session.rollback()
            summary = {"extracted": 0, "from_cache": 0, "unavailable": len(docs), "failed": 0, "error": str(exc)}
    print(f"[reset] scenario={scenario} ERP tables re-seeded, gate results cleared, {len(docs)} documents "
          f"reloaded, extracted={summary['extracted']} unavailable={summary['unavailable']}")
    path = back_path(request)
    if path.startswith(DOCUMENT_PAGE_PREFIXES):
        path = "/inbox"
    text = (f"{config.SCENARIO_LABELS[scenario]} reset: ERP tables re-seeded, gate results cleared, "
            f"{len(docs)} sample documents reloaded and not processed yet; {summary['extracted']} extracted.")
    if summary["unavailable"]:
        text += f" Extraction pending for {plural(summary['unavailable'], 'document')} (no cache entry)."
    response = redirect(path, ("warn" if summary["unavailable"] else "info", text))
    set_scenario_cookie(response, scenario)
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
            run = gate.run_scenario(session, scenario, allow_api=bool(config.GEMINI_API_KEY))
        except Exception as exc:  # a broken rule or data problem must not end in a 500
            session.rollback()
            print(f"[run] scenario={scenario} FAILED: {exc!r}")
            return None, f"The gate run of {config.SCENARIO_LABELS[scenario]} failed; see the server log."
    s = run.summary_json or {}
    return run, (f"{loaded}{config.SCENARIO_LABELS[scenario]}: {s.get('documents', 0)} documents processed, "
                 f"{s.get('touchless', 0)} touchless, {s.get('exceptions', 0)} exceptions.")


def count_documents(session: Session, scenario: str) -> int:
    return session.scalar(select(func.count()).select_from(InboundDocument)
                          .where(InboundDocument.scenario == scenario)) or 0


@app.post("/run")
def run_scenario(request: Request, scenario: Optional[str] = Form(None),
                 session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Header "Run scenario": process every document of the page's scenario, then show the step log."""
    scenario = form_scenario(request, scenario)
    run, message = run_gate(session, scenario)
    response = redirect("/inbox", ("error", message)) if run is None else redirect("/run", ("info", message))
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
    return redirect("/gate/compare", ("info", " ".join(messages)))


# --------------------------------------------------------------------------------------------
# Gate results as the pages show them: outcome chips, badges, owner, SLA due date, age
# --------------------------------------------------------------------------------------------

EMAIL_LOOP_LABEL = "Email loop — untracked"
OUTCOME_LABELS = {"posted": "Posted", "exception": "Exception", "blocked_duplicate": "Blocked duplicate",
                  "applied_credit": "Credit applied", "human_review": "Human review"}
OUTCOME_TONES = {"posted": "ok", "applied_credit": "ok", "blocked_duplicate": "info", "human_review": "warn",
                 "exception": "bad"}
# Tone of a step result in the gate trace and the run log (css class chip-<tone>).
RESULT_TONES = {"ok": "ok", "applied": "ok", "info": "info", "blocked": "info", "flag": "warn", "created": "warn",
                "exception": "bad", "unapplied": "bad", "skipped": "neutral", **OUTCOME_TONES}
# Short badges (same strings as metrics.compare cells) and their tone.
BADGE_TONES = {"duplicate posting": "bad", "wrong entity": "bad", "unapplied credit": "bad",
               "vendor account created": "bad", "terms paid early": "warn", "terms paid late": "warn",
               "terms variance": "warn", "terms variance flagged": "info", "duplicate account flagged": "info",
               "DoA auto-approved": "info", "blocked duplicate": "info", "contract match": "ok",
               "3-way match": "ok", "credit applied": "ok"}
STEP_LABELS = {"register": "Register", "extract": "Extract", "resolve_vendor": "Resolve vendor",
               "legal_entity": "Legal entity check", "duplicate_check": "Duplicate check",
               "credit_note": "Credit note", "commitment_match": "Commitment match", "terms": "Payment terms",
               "post": "Post"}
CYCLE_LABELS = {"store_forwarding": "Store mailbox forwarding",
                "ap_open_and_key": "AP opens ap@ and the quick-fix tool keys it",
                "email_loop": "Email loop (untracked)", "email_approval": "Approval by email",
                "posting": "Posting", "registration": "Registration on arrival",
                "extraction_and_gate": "Extraction and gate", "exception_sla": "Owner resolves within the SLA",
                "workflow_approval": "Workflow approval"}
RESOLUTION_LABELS = {"vat_id": "VAT ID", "iban": "IBAN", "name": "name similarity",
                     "exact_name": "exact display name (first hit)", "fuzzy_name": "similar display name (first hit)",
                     "created": "nothing: created on the fly"}
PATH_LABELS = {"matched": "Matched", "email_loop": "Email loop", "touchless": "Touchless",
               "exception": "Exception with SLA"}


def outcome_chip(outcome: Optional[str], exception_type: Optional[str]) -> tuple[str, str]:
    """(label, tone) of the outcome chip: Posted / Exception: <label> / Blocked duplicate / ..."""
    if exception_type == "email_loop":
        return EMAIL_LOOP_LABEL, "loop"
    if outcome == "exception":
        return f"Exception: {taxonomy.label(exception_type)}", "bad"
    return OUTCOME_LABELS.get(outcome or "", outcome or "—"), OUTCOME_TONES.get(outcome or "", "neutral")


def badge_chips(badges: list[str]) -> list[tuple[str, str]]:
    return [(b, BADGE_TONES.get(b, "neutral")) for b in badges]


def sla_due(decision: GateDecision, doc: Optional[InboundDocument]) -> Optional[datetime]:
    """Registration (the ageing clock starts there) + SLA business days; None without an SLA."""
    start = doc and (doc.registered_on or doc.received_on)
    if decision.sla_days is None or start is None:
        return None
    return sim.add_business_days(start, int(decision.sla_days))


def age_days(doc: Optional[InboundDocument], as_of: Optional[datetime]) -> Optional[int]:
    """Business days from registration to the cockpit snapshot date (sim.cockpit_as_of) or an earlier end."""
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
    label, tone = outcome_chip(decision.outcome, decision.exception_type)
    spec = world.DOCUMENT_BY_NO.get(det.get("sample_no") or (doc.sample_no if doc else 0))
    posted = posted_on(decision)
    posted_by_snapshot = bool(as_of and posted and posted <= as_of)
    return {
        **det,
        "doc_id": decision.doc_id,
        "outcome": decision.outcome,
        "exception_type": decision.exception_type,
        "is_email_loop": decision.exception_type == "email_loop",
        "type_label": taxonomy.label(decision.exception_type) if decision.exception_type else None,
        "type_info": taxonomy.BY_KEY.get(decision.exception_type or ""),
        "chip_label": label,
        "chip_tone": tone,
        "owner_name": decision.owner_name,
        "owner_role": decision.owner_role,
        "sla_days": decision.sla_days,
        "sla_due": sla_due(decision, doc),
        "age_days": age_days(doc, posted if posted_by_snapshot else as_of),
        "posted_on": posted,
        "posted_by_snapshot": posted_by_snapshot,
        "reason": decision.reason,
        "days": None if decision.simulated_days is None else int(decision.simulated_days),
        "badges": badge_chips(metrics.badges(decision)),
        "flags": det.get("flags") or [],
        "supplier": det.get("supplier_name") or (spec.printed_supplier_name if spec else None) or "—",
        "path_label": PATH_LABELS.get(det.get("path") or "", det.get("path")),
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
    """Registered date, or the expected one with the reason (brief sections 6 and 10)."""
    if doc.registered and doc.registered_on is not None:
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


@app.get("/inbox")
def inbox(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    docs = session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)
                           .order_by(InboundDocument.received_on, InboundDocument.doc_id)).all()
    decisions = decisions_by_doc(session, scenario)
    views = {d.doc_id: gate_view(decisions[d.doc_id], d) for d in docs if d.doc_id in decisions}
    mailboxes = [
        {"channel": channel, "address": address, "label": CHANNEL_LABELS[channel],
         "docs": [(d, registration_info(d), views.get(d.doc_id)) for d in docs if d.channel == channel]}
        for channel, address in world.MAILBOX_BY_CHANNEL.items()
    ]
    return render(request, "inbox.html", {"page_title": "Inbox", "mailboxes": mailboxes, "total": len(docs),
                                          "processed": len(views), "doc_type_labels": DOC_TYPE_LABELS,
                                          "run": latest_run(session, scenario)})


# --------------------------------------------------------------------------------------------
# Run page: the visible step log of the latest run and the outcome per document
# --------------------------------------------------------------------------------------------

_LOG_PREFIX = re.compile(r"^(\[[^\]]*\])\s?(.*)$")
_LOG_RESULT = re.compile(r"\b(result|outcome)=(\S+)")
LOG_REVEAL_MS = 9000  # the whole log is revealed line by line in about this many milliseconds


def log_line(text: str) -> dict[str, str]:
    """One log line split for display: "[doc 07]", the text around the result/outcome token, its tone."""
    m = _LOG_PREFIX.match(text)
    prefix, body = (m.group(1), m.group(2)) if m else ("", text)
    kind = "run" if prefix == "[run]" else "step"
    r = _LOG_RESULT.search(body)
    if r is None:
        return {"prefix": prefix, "before": body, "token": "", "after": "", "tone": "neutral", "kind": kind}
    if kind == "step" and r.group(1) == "outcome":
        kind = "outcome"
    return {"prefix": prefix, "before": body[:r.start()], "token": r.group(0), "after": body[r.end():],
            "tone": RESULT_TONES.get(r.group(2), "neutral"), "kind": kind}


@app.get("/run")
def run_page(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    run = latest_run(session, scenario)
    summary = (run.summary_json or {}) if run else {}
    lines = [log_line(str(text)) for text in summary.get("log") or []]
    docs = scenario_documents(session, scenario)
    decisions = decisions_by_doc(session, scenario)
    return render(request, "run.html", {
        "page_title": "Run", "run": run, "summary": summary, "lines": lines,
        "step_ms": max(12, min(90, LOG_REVEAL_MS // max(len(lines), 1))),
        "rows": [gate_view(decisions[d.doc_id], d) for d in docs if d.doc_id in decisions],
        "no_extraction": [d.doc_id for d in docs if d.extraction is None],
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


def no_extraction_reason() -> str:
    if config.EXTRACTOR == "fixture":
        return "Fixture mode is on (EXTRACTOR=fixture) and there is no ground-truth fixture for this PDF."
    if not config.GEMINI_API_KEY:
        # the running server does not re-read .env: Extract now only works after a restart
        return ("This PDF is not in the extraction cache and GEMINI_API_KEY is not set: add the key to .env "
                "and run make extract (it updates the database directly), or restart the app and click "
                "Extract now.")
    return "Not extracted yet. Extract now calls the Gemini API once; the result is cached on disk."


def extraction_context(doc: InboundDocument, message: Optional[tuple[str, str]] = None) -> dict[str, Any]:
    ex = doc.extraction
    return {
        "doc": doc,
        "ex": ex,
        "fields": field_rows(ex.json) if ex else [],
        "raw_json": json.dumps(ex.json, indent=2, ensure_ascii=False) if ex else "",
        "is_fixture": bool(ex and ex.model == extract.FIXTURE_MODEL),
        "no_extraction_reason": no_extraction_reason(),
        "threshold_pct": round(config.CONFIDENCE_THRESHOLD * 100),
        "panel_message": message,
        "doc_type_labels": DOC_TYPE_LABELS,
    }


def latest_decision(session: Session, doc_id: str) -> Optional[GateDecision]:
    return session.scalar(select(GateDecision).where(GateDecision.doc_id == doc_id)
                          .order_by(GateDecision.id.desc()).limit(1))


def trace_rows(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"label": STEP_LABELS.get(s.get("step", ""), str(s.get("step", "")).replace("_", " ").capitalize()),
             "result": s.get("result") or "—", "tone": RESULT_TONES.get(s.get("result") or "", "neutral"),
             "detail": s.get("detail") or ""} for s in steps or []]


def cycle_rows(breakdown: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Key, label and business days per activity of the simulated cycle (sim.cycle_breakdown order)."""
    return [{"key": k, "label": CYCLE_LABELS.get(k, k.replace("_", " ").capitalize()), "days": int(v or 0)}
            for k, v in (breakdown or {}).items()]


# Columns of the line-check table, in this order, when the gate provides them (else every key it provides).
LINE_CHECK_COLUMNS = ("line", "description", "kind", "invoiced_qty", "received_qty", "unit_price", "po_unit_price",
                      "variance", "tolerance", "issues", "result")


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
    checks = [c for c in (gv or {}).get("line_checks") or [] if isinstance(c, dict)]
    return {
        "doc": doc, "gv": gv, "pvi": pvi, "gate_message": message,
        "trace": trace_rows(decision.steps) if decision else [],
        "line_checks": checks, "line_columns": line_columns(checks),
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
               doc_scenario_label=config.SCENARIO_LABELS.get(doc.scenario, doc.scenario))
    return render(request, "invoice.html", ctx)


def rerun_message(decision: GateDecision, processed_first: Sequence[str] = ()) -> tuple[str, str]:
    label, _ = outcome_chip(decision.outcome, decision.exception_type)
    owner = f", owner {decision.owner_name}" if decision.owner_name else ""
    text = f"Gate re-run: {label}{owner}."
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
    with _gate_lock:
        try:
            before = processed_doc_ids(session, doc.scenario)
            decision = gate.rerun_document(session, doc, allow_api=bool(config.GEMINI_API_KEY))
            new = processed_doc_ids(session, doc.scenario) - before - {doc_id}
            message = rerun_message(decision, [d.doc_id for d in scenario_documents(session, doc.scenario)
                                               if d.doc_id in new])
        except Exception as exc:  # never a 500: the panel shows a short message, the log has the error
            session.rollback()
            print(f"[rerun] doc={doc_id} FAILED: {exc!r}")
            message = ("error", f"The gate could not process {doc_id}; see the server log.")
    session.expire_all()
    doc = get_document(session, doc_id)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_gate_panel.html", gate_context(session, doc, message))
    return redirect(f"/invoice/{doc.doc_id}", message)


@app.post("/invoice/{doc_id}/draft")
def draft_message(doc_id: str, request: Request, force: int = 0,
                  session: Session = Depends(db.get_session)) -> Response:
    """Draft a message to the owner of a to-be exception (drafts.py). Never raises: shows the reason."""
    doc = get_document(session, doc_id)
    decision = latest_decision(session, doc_id)
    draft, error = None, None
    try:
        draft = drafts.get_draft(decision, doc, allow_api=bool(config.GEMINI_API_KEY), force=bool(force))
    except drafts.DraftUnavailable as exc:
        error = str(exc)
    except Exception as exc:  # anything unexpected: readable, not a 500
        print(f"[draft] doc={doc_id} error: {exc!r}")
        error = f"The draft could not be produced: {exc}"
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_draft.html", {
            "doc": doc, "draft": draft, "draft_error": error, "draft_label": drafts.DRAFT_LABEL})
    if draft:
        return redirect(f"/invoice/{doc_id}", ("info", f"{drafts.DRAFT_LABEL}: {draft.text}"))
    return redirect(f"/invoice/{doc_id}", ("warn", f"No draft: {error}"))


def api_error_text(exc: Exception) -> str:
    """'Gemini API error: <message>.' without doubling a prefix the message already has."""
    message = str(exc).rstrip(".")
    return f"{message}." if message.startswith("Gemini API error") else f"Gemini API error: {message}."


def run_extraction(session: Session, doc: InboundDocument, force: bool) -> tuple[str, str]:
    """Extract (or re-extract) one document. Never raises: returns a (kind, message) pair."""
    if force and config.EXTRACTOR != "fixture" and not config.GEMINI_API_KEY:
        return ("warn", "Force re-extract needs the Gemini API and GEMINI_API_KEY is not set in .env. "
                        "The current extraction was kept.")
    had_extraction = doc.extraction is not None
    try:
        row = extract.extract_document(session, doc, force=force, allow_api=bool(config.GEMINI_API_KEY))
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
    if row.model == extract.FIXTURE_MODEL:
        return ("info", "Loaded the ground-truth fixture (no API call; not a Gemini output).")
    source = "from cache" if row.from_cache else f"via the Gemini API in {row.latency_ms} ms"
    return ("info", f"Extracted with {row.model} ({source}).")


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


def resolve_data_file(relative: str) -> Optional[Path]:
    """Absolute path of a stored document, or None if it escapes data/ or does not exist."""
    path = (config.BASE_DIR / relative).resolve()
    if not path.is_relative_to(config.DATA_DIR.resolve()) or not path.is_file():
        return None
    return path


@app.get("/files/{doc_id}.pdf")
def document_file(doc_id: str, session: Session = Depends(db.get_session)) -> FileResponse:
    doc = get_document(session, doc_id)
    path = resolve_data_file(doc.file_path)
    if path is None:
        raise HTTPException(status_code=404, detail="File not available")
    return FileResponse(path, media_type="application/pdf", filename=f"{doc.doc_id}.pdf",
                        content_disposition_type="inline")


# --------------------------------------------------------------------------------------------
# Mock ERP: pending vendor invoices, vendor master, purchase orders & receipts, contracts
# --------------------------------------------------------------------------------------------


PVI_STATUS = {"pending_payment": ("pending payment", "neutral"), "credit_applied": ("credit applied", "ok"),
              "unapplied_credit": ("unapplied credit", "bad")}


def pvi_chips(row: PendingVendorInvoice) -> list[dict[str, Optional[str]]]:
    """Flag chips of a posted invoice: terms source (only when the row has terms: not for a credit note), wrong
    entity, duplicate of, credit, DoA."""
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
    if flags.get("doa_auto_approved"):
        chips.append({"label": "DoA auto-approved", "tone": "info", "href": None})
    return chips


def totals_by_currency(rows: list[PendingVendorInvoice]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = totals.setdefault(r.currency, {"currency": r.currency, "count": 0, "total": 0.0})
        t["count"] += 1
        t["total"] = round(t["total"] + (r.total or 0.0), 2)
    return sorted(totals.values(), key=lambda t: t["currency"])


@app.get("/erp/pending-invoices")
def pending_invoices(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    rows = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.scenario == scenario)
                           .order_by(PendingVendorInvoice.invoice_id)).all()
    return render(request, "erp_pending_invoices.html", {
        "page_title": "Pending vendor invoices",
        "rows": [{"row": r, "chips": pvi_chips(r), "status": PVI_STATUS.get(r.status, (r.status, "neutral"))}
                 for r in rows],
        "totals": totals_by_currency(list(rows)), "run": latest_run(session, scenario),
    })


@dataclass
class VendorRow:
    account: VendorAccount
    party: Optional[Party]  # linked party, or the one found by VAT ID / name for unlinked accounts
    party_found_by: Optional[str]  # None when linked; "VAT ID" | "name" when inferred
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
            out.append(("missing VAT ID", "missing"))
        if not acc.iban:
            out.append(("missing IBAN", "missing"))
        if self.terms_differ:
            out.append(("terms ≠ agreed", "terms"))
        if acc.status == "inactive":
            out.append(("inactive", "inactive"))
        return out


def find_party(acc: VendorAccount, parties: list[Party]) -> tuple[Optional[Party], Optional[str]]:
    """Linked party; for unlinked accounts the party with the same VAT ID, else a matching name."""
    if acc.party_id:
        return next((p for p in parties if p.party_id == acc.party_id), None), None
    vat = normalize.normalise_vat(acc.vat_id)
    if vat:
        for p in parties:
            if normalize.normalise_vat(p.vat_id) == vat:
                return p, "VAT ID"
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
        return "same VAT ID"
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
    """Groups by linked party (in party order); unlinked accounts last."""
    groups = []
    for p in parties:
        linked = [r for r in rows if r.account.party_id == p.party_id]
        if linked:
            groups.append((p.canonical_name, linked))
    unlinked = [r for r in rows if not r.account.party_id]
    if unlinked:
        groups.append(("Not linked to a party", unlinked))
    return groups


def vendor_stats(rows: list[VendorRow], n_parties: int) -> dict[str, Any]:
    n = len(rows)
    complete = sum(1 for r in rows if r.account.vat_id and r.account.iban)
    return {
        "accounts": n,
        "suppliers": n_parties,
        "ratio": f"{n / n_parties:.1f}" if n_parties else "—",
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
    return render(request, "erp_vendors.html", {
        "page_title": "Vendor master", "view": view, "view_label": config.SCENARIO_LABELS[view],
        "groups": group_vendor_rows(rows, list(parties)), "stats": vendor_stats(rows, len(parties)),
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
    return render(request, "erp_contracts.html", {"page_title": "Contracts", "rows": rows, "parties": parties})


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


@app.get("/gate/exceptions")
def exception_cockpit(request: Request, owner: Optional[str] = None,
                      session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    docs = {d.doc_id: d for d in scenario_documents(session, scenario)}
    decisions = decisions_by_doc(session, scenario)
    routing = bool(gate.SCENARIOS[scenario]["exception_routing"])
    plain = [gate_view(decisions[i], d) for i, d in docs.items() if i in decisions]
    queued = [v["doc_id"] for v in plain if (blocking_exception(v) if routing else v["is_email_loop"])]
    as_of = sim.cockpit_as_of(docs[i].registered_on or docs[i].received_on for i in queued)
    views = [gate_view(decisions[i], d, as_of) for i, d in docs.items() if i in decisions]
    queue = [v for v in views if blocking_exception(v)]
    info = [{"view": v, "flag": f} for v in views for f in v["flags"] if isinstance(f, dict)]
    owners = owner_counts(queue, info)
    owner = owner or None
    if owner:
        queue = [v for v in queue if owner in (v["owner_name"], v.get("next_owner_name"))]
        info = [item for item in info if item["flag"].get("owner_name") == owner]
    loop = [v for v in views if v["is_email_loop"]]
    return render(request, "exceptions.html", {
        "page_title": "Exception cockpit", "has_run": bool(decisions),
        "routing": routing,
        "groups": group_by_type(queue), "queue_count": len(queue), "info": info,
        # as-is: every document of the run that went through the loop; the snapshot tells which were still in it
        "loop": loop, "loop_open": sum(1 for v in loop if not v["posted_by_snapshot"]),
        "blocked": [v for v in views if v["outcome"] == "blocked_duplicate"],
        "owners": owners, "owner": owner, "as_of": as_of, "ap_specialist": world.AP_SPECIALIST.name,
    })


# Direction of "better" for the comparison page: +1 higher is better, -1 lower is better.
KPI_BETTER = {"po_contract_coverage": 1, "accounts_per_supplier": -1, "pct_accounts_vat_iban": 1,
              "pct_accounts_terms_ok": 1, "registration_lag_days": -1, "touchless": 1, "touchless_rate": 1,
              "exceptions": -1, "exception_rate": -1, "duplicates_blocked": 1, "duplicate_postings": -1,
              "duplicate_invoices": -1, "credit_notes_applied": 1, "credit_notes_unapplied": -1,
              "wrong_entity_postings": -1, "terms_variance_paid": -1, "cash_leakage_amount": -1,
              "avg_cycle_days": -1, "avg_cycle_followup_days": -1, "reference_nonpo_store_days": -1}
CYCLE_KPIS = ("avg_cycle_days", "avg_cycle_followup_days", "reference_nonpo_store_days")
KPI_GROUPS = (("upstream", "Upstream — process health"), ("downstream", "Downstream — automation efficiency"))
# Chart colours (light surface): outcomes use the status steps where the outcome is a state (posted = good,
# human review = warning, exception = critical) and fixed categorical slots otherwise; cycle bars are blue
# for "no human step" paths and orange for paths with a human step. Labels and legends always name them.
CHART_COLOURS = {"posted": "#0ca30c", "applied_credit": "#4a3aa7", "blocked_duplicate": "#2a78d6",
                 "human_review": "#fab219", "exception": "#d03b3b"}
PATH_COLOURS = {"matched": "#2a78d6", "touchless": "#2a78d6", "email_loop": "#eb6834", "exception": "#eb6834"}


def ordered_kpis(kpis: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """KPIs in metrics' display order, each with its footnote number."""
    return [{**kpi, "key": key, "note": i} for i, (key, kpi) in enumerate(kpis.items(), start=1)]


def kpi_display(kpi: Optional[dict[str, Any]], available: bool) -> str:
    """Display text of one KPI tile; downstream KPIs (and upstream ones computed from decisions) need a run."""
    if kpi is None:
        return "—"
    if not available and (kpi.get("group") == "downstream" or kpi.get("value") is None):
        return "Run the scenario"
    return str(kpi.get("display") if kpi.get("display") not in (None, "") else "—")


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
    type_labels = [EMAIL_LOOP_LABEL if k == "email_loop" else taxonomy.label(k) for k in by_type]
    return {
        "outcomes": {"labels": [OUTCOME_LABELS.get(k, k) for k in outcomes], "values": list(outcomes.values()),
                     "colours": [CHART_COLOURS.get(k, "#6b7280") for k in outcomes]},
        "exceptions": {"labels": type_labels, "lines": [label_lines(label) for label in type_labels],
                       "values": list(by_type.values()),
                       "colours": ["#ec835a" if k == "email_loop" else "#d03b3b" for k in by_type]},
        "cycle": {"labels": [c.get("doc_id") for c in cycle], "values": [c.get("days") for c in cycle],
                  "paths": [PATH_LABELS.get(c.get("path") or "", c.get("path")) for c in cycle],
                  "colours": [PATH_COLOURS.get(c.get("path") or "", "#6b7280") for c in cycle]},
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
    groups = [{"key": key, "title": title, "tiles": [t for t in tiles if t.get("group") == key]}
              for key, title in KPI_GROUPS]
    return render(request, "kpis.html", {
        "page_title": "KPIs", "data": data, "error": error, "available": available, "groups": groups,
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
    a_data, b_data = data.get("asis") or {}, data.get("tobe") or {}
    a_kpis, b_kpis = a_data.get("kpis") or {}, b_data.get("kpis") or {}
    rows = []
    for tile in ordered_kpis(a_kpis or b_kpis):  # same keys in both scenarios
        key = tile["key"]
        a, b = a_kpis.get(key), b_kpis.get(key)
        comparable = tile.get("group") != "downstream" or data.get("both_available")
        rows.append({**tile, "asis": kpi_display(a, bool(a_data.get("available"))),
                     "tobe": kpi_display(b, bool(b_data.get("available"))),
                     "better": better_side(key, a, b) if comparable else None})
    return rows


def compare_cell(cell: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not cell:
        return None
    label, tone = outcome_chip(cell.get("outcome"), cell.get("exception_type"))
    return {**cell, "chip_label": label, "chip_tone": tone, "badge_chips": badge_chips(cell.get("badges") or [])}


@app.get("/gate/compare")
def compare(request: Request, session: Session = Depends(db.get_session)) -> Response:
    data, error = safe_metrics(lambda: metrics.compare(session), "comparison")
    kpi_rows = compare_kpi_rows(data) if data else []
    rows = [{**r, "a": compare_cell(r.get("asis")), "b": compare_cell(r.get("tobe"))}
            for r in (data or {}).get("rows") or []]
    partial = [(config.SCENARIO_LABELS[s], warning) for s in config.SCENARIOS
               if (warning := partial_run_warning((data or {}).get(s)))]
    return render(request, "compare.html", {
        "page_title": "Compare", "data": data, "error": error, "rows": rows, "partial_warnings": partial,
        "both_available": bool(data and data.get("both_available")),
        "available": {s: bool(data and (data.get(s) or {}).get("available")) for s in config.SCENARIOS},
        "groups": [{"key": key, "title": title, "rows": [r for r in kpi_rows if r.get("group") == key]}
                   for key, title in KPI_GROUPS],
        "cycle_rows": [r for r in kpi_rows if r["key"] in CYCLE_KPIS],
    })


@app.get("/assumptions")
def assumptions(request: Request) -> Response:
    path = config.DOCS_DIR / "ASSUMPTIONS.md"
    html = None
    if path.is_file():
        html = markdown.markdown(path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
    return render(request, "assumptions.html", {"page_title": "Assumptions", "html": html})


# ============================================================================================
# HOOK: real intake (n8n IMAP trigger / Gmail) — out of scope for now; wire it here later.
#
# POST /intake/webhook  (multipart/form-data)
#   file      the PDF (checked: %PDF- magic bytes, at most 10 MB)
#             Content-Length is required (411 without it; 413 before parsing if clearly too large).
#   channel   ap_mailbox | store_mailbox
#   sender    sender email address
#   subject   optional
#   scenario  optional: asis | tobe (default: the active scenario cookie)
# Saves data/invoices/inbound/<sha256[:16]>.pdf, registers an InboundDocument ("B-W01", ...)
# following the scenario's registration rules, and tries extraction (API only if a key is set).
# ============================================================================================

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 64 * 1024  # form fields and multipart boundaries around the PDF
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
            return JSONResponse({"detail": "request too large: the PDF must be at most 10 MB"}, status_code=413)
    return await call_next(request)


def next_webhook_doc_id(session: Session, scenario: str) -> str:
    """Next free id for a webhook document: A-W01, A-W02, ... / B-W01, ..."""
    letter = seed.SCENARIO_LETTER[scenario]
    prefix = f"{letter}-W"
    existing = session.scalars(select(InboundDocument.doc_id).where(InboundDocument.doc_id.like(f"{prefix}%")))
    numbers = [int(i[len(prefix):]) for i in existing if i[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:02d}"


@app.post("/intake/webhook", status_code=201)
def intake_webhook(
    request: Request,
    file: UploadFile = File(...),
    channel: str = Form(...),
    sender: str = Form(...),
    subject: Optional[str] = Form(None),
    scenario: Optional[str] = Form(None),
    session: Session = Depends(db.get_session),
) -> JSONResponse:
    scenario = check_scenario(scenario) if scenario else active_scenario(request)
    if channel not in world.MAILBOX_BY_CHANNEL:
        raise HTTPException(status_code=400, detail="channel must be ap_mailbox or store_mailbox")
    if not _EMAIL_RE.match(sender.strip()):
        raise HTTPException(status_code=400, detail="sender must be an email address")
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="PDF larger than 10 MB")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="file is not a PDF")

    sha = hashlib.sha256(content).hexdigest()
    inbound_dir = config.INVOICES_DIR / "inbound"
    path = inbound_dir / f"{sha[:16]}.pdf"
    received_on = datetime.now().replace(microsecond=0)
    registered = sim.registration_delay_days(scenario, channel) == 0  # to-be: registered on arrival
    with _webhook_lock:  # file write, id allocation and insert: one upload at a time
        inbound_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        doc = InboundDocument(
            doc_id=next_webhook_doc_id(session, scenario), scenario=scenario,
            sample_no=0,  # 0 = not one of the sample documents
            channel=channel, mailbox=world.MAILBOX_BY_CHANNEL[channel], received_on=received_on,
            file_path=path.relative_to(config.BASE_DIR).as_posix(), file_hash=sha,
            sender_email=sender.strip(),
            subject=(subject or "").strip() or f"(no subject) {file.filename or ''}".strip(),
            registered=registered, registered_on=received_on if registered else None, doc_type="unknown",
        )
        session.add(doc)
        session.commit()
    kind, message = run_extraction(session, doc, force=False)
    extracted = doc.extraction is not None
    print(f"[intake] webhook doc={doc.doc_id} scenario={scenario} channel={channel} "
          f"registered={registered} extracted={extracted} ({kind}: {message})")
    return JSONResponse({"doc_id": doc.doc_id, "scenario": scenario, "registered": registered,
                         "extracted": extracted}, status_code=201)
