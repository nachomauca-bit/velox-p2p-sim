"""FastAPI app: mock ERP pages (read-only), control-gate pages (phase 1: inbox and invoice) and intake.

Server-rendered with Jinja2 + HTMX partials, Pico.css from a CDN. The active scenario
("asis" | "tobe") lives in a cookie. The control gate itself, the exception cockpit, KPIs and the
comparison page are phase 2: they only have placeholders here.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Awaitable, Callable
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

from app import config, db, extract, normalize, seed, sim, world
from app.models import (
    Contract,
    InboundDocument,
    LegalEntity,
    Party,
    PendingVendorInvoice,
    ProductReceipt,
    PurchaseOrder,
    VendorAccount,
)

APP_TITLE = "Velox AP — control-gate simulator"
APP_DIR = Path(__file__).resolve().parent

SCENARIO_COOKIE = "scenario"
FLASH_COOKIE = "flash"
SCENARIO_SUMMARY = {
    "asis": "Dirty vendor master, weak PO discipline, two intake mailboxes, no control gate.",
    "tobe": "Clean vendor master, POs and contracts for most spend, registration on arrival, "
            "control gate (phase 2).",
}
PHASE2_MESSAGE = "Available in phase 2 — the control gate is not built yet"

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
PHASE2_PATHS = {"/gate/exceptions", "/gate/kpis", "/gate/compare"}

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


def active_scenario(request: Request) -> str:
    value = request.cookies.get(SCENARIO_COOKIE, "")
    return value if value in config.SCENARIOS else config.DEFAULT_SCENARIO


def check_scenario(value: str) -> str:
    if value not in config.SCENARIOS:
        raise HTTPException(status_code=400, detail=f"Unknown scenario {value!r}: use one of {config.SCENARIOS}")
    return value


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
        "phase2_paths": PHASE2_PATHS,
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
    response.set_cookie(SCENARIO_COOKIE, scenario, max_age=30 * 24 * 3600, httponly=True, samesite="lax")
    return response


def extraction_mode_note() -> str:
    if config.EXTRACTOR == "fixture":
        return " Fixture mode: ground-truth JSON, not a Gemini output."
    return ""


@app.post("/documents/load")
def load_documents(request: Request, session: Session = Depends(db.get_session)) -> RedirectResponse:
    """Load the 12 sample PDFs into both mailboxes of the active scenario and extract them."""
    scenario = active_scenario(request)
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
        return redirect("/inbox", ("error", f"{len(docs)} documents loaded, but extraction failed: {exc}"))
    print(f"[intake] scenario={scenario} loaded={len(docs)} extracted={summary['extracted']} "
          f"from_cache={summary['from_cache']} unavailable={summary['unavailable']} "
          f"failed={summary.get('failed', 0)}")
    return redirect("/inbox", load_message(len(docs), scenario, summary))


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


# Pages of a single document: a reset deletes the scenario's documents, so never redirect back there.
DOCUMENT_PAGE_PREFIXES = ("/invoice/", "/files/")


@app.post("/reset")
def reset(request: Request, session: Session = Depends(db.get_session)) -> RedirectResponse:
    scenario = active_scenario(request)
    seed.reset_scenario(session, scenario)
    print(f"[reset] scenario={scenario} ERP tables re-seeded, inbox emptied")
    path = back_path(request)
    if path.startswith(DOCUMENT_PAGE_PREFIXES):
        path = "/inbox"
    return redirect(path, ("info", f"{config.SCENARIO_LABELS[scenario]} reset: "
                                   "ERP tables re-seeded, inbox emptied."))


# --------------------------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------------------------


def registration_info(doc: InboundDocument) -> dict[str, Any]:
    """Registered date, or the expected one with the reason (brief sections 6 and 10)."""
    if doc.registered and doc.registered_on is not None:
        note = "Registered on arrival" if doc.registered_on == doc.received_on else "Registered"
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
    mailboxes = [
        {"channel": channel, "address": address, "label": CHANNEL_LABELS[channel],
         "docs": [(d, registration_info(d)) for d in docs if d.channel == channel]}
        for channel, address in world.MAILBOX_BY_CHANNEL.items()
    ]
    return render(request, "inbox.html", {"page_title": "Inbox", "mailboxes": mailboxes, "total": len(docs),
                                          "doc_type_labels": DOC_TYPE_LABELS})


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


@app.get("/invoice/{doc_id}")
def invoice_page(doc_id: str, request: Request, session: Session = Depends(db.get_session)) -> Response:
    doc = get_document(session, doc_id)
    ctx = extraction_context(doc)
    ctx.update(page_title=f"Invoice {doc.doc_id}", registration=registration_info(doc),
               channel_label=CHANNEL_LABELS.get(doc.channel, doc.channel),
               doc_scenario_label=config.SCENARIO_LABELS.get(doc.scenario, doc.scenario))
    return render(request, "invoice.html", ctx)


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


@app.get("/erp/pending-invoices")
def pending_invoices(request: Request, session: Session = Depends(db.get_session)) -> Response:
    scenario = active_scenario(request)
    rows = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.scenario == scenario)
                           .order_by(PendingVendorInvoice.invoice_id)).all()
    return render(request, "erp_pending_invoices.html", {"page_title": "Pending vendor invoices", "rows": rows})


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
# Control gate: phase-2 placeholders, assumptions
# --------------------------------------------------------------------------------------------


def phase2_page(request: Request, title: str, intro: str) -> Response:
    return render(request, "phase2.html", {"page_title": title, "intro": intro, "message": PHASE2_MESSAGE})


@app.get("/gate/exceptions")
def exception_cockpit(request: Request) -> Response:
    return phase2_page(request, "Exception cockpit",
                       "Queue of exceptions grouped by type, with owner, SLA due date, age and reason.")


@app.get("/gate/kpis")
def kpis(request: Request) -> Response:
    return phase2_page(request, "KPIs", "Process-health and automation-efficiency KPIs for the active scenario.")


@app.get("/gate/compare")
def compare(request: Request) -> Response:
    return phase2_page(request, "Compare", "Scenario A vs B side by side for the same 12 documents.")


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
            sample_no=0,  # 0 = not one of the 12 sample documents
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
