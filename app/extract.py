"""Document understanding: a Gemini model with structured output and per-field confidence.

- Routed by file suffix: ".xml" is a UBL e-invoice, parsed directly by app/ubl.py (no model call, no cache,
  also in fixture mode; labelled ubl.UBL_MODEL); ".txt" is an email body without attachment (nothing to
  extract: ExtractionUnavailable); anything else is a PDF for the model.
- One call per PDF (document part + system instruction), JSON schema = InvoiceExtraction.
- Cached on disk as <CACHE_DIR>/<sha256>.json; re-runs never re-call the API unless forced. Cache files are
  written atomically; a corrupt one is logged and treated as a miss.
- EXTRACTOR=fixture reads ground-truth JSON from tests/fixtures/ (tests/fixtures_v2/ for test set v2) instead
  (tests / offline UI work); such results are labelled FIXTURE_MODEL so they can never be mistaken for Gemini output.
- Backend: GEMINI_BACKEND=aistudio (API key) or vertex (Vertex AI project + location, service account
  credentials). Only _client() differs; the request, schema, cache and fallback are the same.
- Model: GEMINI_MODEL (default gemini-2.5-flash). If the API rejects it as unavailable, the newest GA
  Flash model the backend offers (found with models.list, never hard-coded) is used for the rest of the process.

CLI:  python -m app.extract [--dataset v1|v2] [--force-extract] [--only 1,5,12] [--no-db]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, world
from app.models import Extraction, InboundDocument

try:  # google-auth comes with google-genai; guarded so that this module still imports without it
    from google.auth import exceptions as google_auth_errors

    # Vertex AI credentials: DefaultCredentialsError, RefreshError, TransportError, ... all derive from it.
    _CREDENTIAL_ERRORS: tuple[type[Exception], ...] = (google_auth_errors.GoogleAuthError,)
except ImportError:  # pragma: no cover
    _CREDENTIAL_ERRORS = ()

FIXTURE_MODEL = "fixture (ground truth, no API call)"


def is_fixture_model(model: Optional[str]) -> bool:
    """Every fixture label starts with "fixture" (incl. the scans' simulated-confidence label): never Gemini output."""
    return bool(model) and model.startswith("fixture")


# --------------------------------------------------------------------------------------------
# Output schema (brief section 7): every key required (so the model always answers it), every value
# nullable, each with a confidence 0..1. No ge/le bounds: _postprocess clamps the confidences.
# --------------------------------------------------------------------------------------------


class DocType(str, Enum):
    invoice = "invoice"
    credit_note = "credit_note"
    reminder = "reminder"
    statement = "statement"
    other = "other"


class TextField(BaseModel):
    value: Optional[str] = Field(...)
    confidence: float = Field(..., description="0..1; 0 when the value is null")


class NumberField(BaseModel):
    value: Optional[float] = Field(...)
    confidence: float = Field(..., description="0..1; 0 when the value is null")


class IntegerField(BaseModel):
    value: Optional[int] = Field(...)
    confidence: float = Field(..., description="0..1; 0 when the value is null")


class DateField(BaseModel):
    value: Optional[str] = Field(..., description="ISO date YYYY-MM-DD")
    confidence: float = Field(..., description="0..1; 0 when the value is null")


class DocTypeField(BaseModel):
    value: Optional[DocType] = Field(...)
    confidence: float = Field(..., description="0..1; 0 when the value is null")


class TextListField(BaseModel):
    value: Optional[list[str]] = Field(...)
    confidence: float = Field(..., description="0..1; 0 when the value is null")


class Line(BaseModel):
    description: Optional[str] = Field(...)
    quantity: Optional[float] = Field(...)
    unit_price: Optional[float] = Field(...)
    amount: Optional[float] = Field(...)


class LinesField(BaseModel):
    value: Optional[list[Line]] = Field(...)
    confidence: float = Field(..., description="0..1; 0 when the value is null")


class InvoiceExtraction(BaseModel):
    doc_type: DocTypeField
    supplier_name: TextField
    supplier_vat_id: TextField
    supplier_iban: TextField
    supplier_country: TextField
    bill_to_name: TextField
    bill_to_vat_id: TextField
    invoice_number: TextField
    invoice_date: DateField
    due_date: DateField
    payment_terms_days: IntegerField
    currency: TextField
    net_total: NumberField
    tax_total: NumberField
    gross_total: NumberField
    po_numbers: TextListField
    referenced_invoice_number: TextField
    lines: LinesField
    notes: TextField


FIELDS: tuple[str, ...] = tuple(InvoiceExtraction.model_fields)

FIELD_LABELS: dict[str, str] = {
    "doc_type": "Document type",
    "supplier_name": "Supplier name",
    "supplier_vat_id": "Supplier tax ID (VAT)",
    "supplier_iban": "Supplier IBAN / bank account",
    "supplier_country": "Supplier country",
    "bill_to_name": "Bill-to name",
    "bill_to_vat_id": "Bill-to tax ID (VAT)",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "due_date": "Due date",
    "payment_terms_days": "Payment terms (days)",
    "currency": "Currency",
    "net_total": "Net total",
    "tax_total": "Tax total",
    "gross_total": "Gross total",
    "po_numbers": "PO numbers",
    "referenced_invoice_number": "Referenced invoice",
    "lines": "Lines",
    "notes": "Notes",
}

# Critical fields for the confidence gate (brief section 8, step 2). Supplier identity is satisfied
# by any one of VAT ID, IBAN or name.
SUPPLIER_IDENTITY_FIELDS = ("supplier_vat_id", "supplier_iban", "supplier_name")
CRITICAL_FIELDS = SUPPLIER_IDENTITY_FIELDS + ("invoice_number", "gross_total", "bill_to_name")


# --------------------------------------------------------------------------------------------
# Result type, errors, cache
# --------------------------------------------------------------------------------------------


class ExtractionUnavailable(RuntimeError):
    """No extraction can be produced now (no cache entry and no API access, no fixture, no file, no attachment)."""


class ExtractionFailed(ExtractionUnavailable):
    """The Gemini API was called but returned an error or an unusable response."""


def _short_error(exc: Exception) -> str:
    """One readable line for an error; for a pydantic ValidationError, the count and the first failing field."""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "(root)"
        return f"{exc.error_count()} validation error(s), first at {where}: {first['msg']}"[:200]
    return next(iter(str(exc).strip().splitlines()), type(exc).__name__)[:200]


@dataclass
class ExtractionResult:
    model: str
    data: dict[str, Any]  # InvoiceExtraction.model_dump(mode="json")
    from_cache: bool
    source: str  # "gemini" | "cache" | "fixture" | "ubl"
    created_on: datetime
    latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None  # includes the thinking tokens (billed as output)
    thinking_tokens: Optional[int] = None
    backend: Optional[str] = None  # "aistudio" | "vertex" for a model call (None: unknown, fixture or UBL)

    @property
    def is_fixture(self) -> bool:
        return is_fixture_model(self.model)

    @property
    def is_ubl(self) -> bool:
        return self.source == "ubl"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cache_path(sha256: str) -> Path:
    return config.CACHE_DIR / f"{sha256}.json"


def _result_from_record(record: dict[str, Any], *, from_cache: bool, source: str) -> ExtractionResult:
    usage = record.get("usage") or {}
    data = InvoiceExtraction.model_validate(record["extraction"]).model_dump(mode="json")
    return ExtractionResult(
        model=record["model"],
        data=data,
        from_cache=from_cache,
        source=source,
        created_on=datetime.fromisoformat(record["created_on"]),
        latency_ms=record.get("latency_ms"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        thinking_tokens=usage.get("thinking_tokens"),  # absent in records written before it was stored
        backend=record.get("backend"),  # absent in records written before phase 3
    )


def read_cache(sha256: str) -> Optional[ExtractionResult]:
    """The cached result, or None on a miss. A corrupt cache file is logged and treated as a miss."""
    path = cache_path(sha256)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return _result_from_record(record, from_cache=True, source="cache")
    # ValueError covers json.JSONDecodeError, pydantic.ValidationError and a bad created_on;
    # TypeError / AttributeError: the JSON is not the expected object at all.
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"[extract] corrupt cache file {path.name} ignored ({_short_error(exc)})")
        return None


def write_cache(sha256: str, result: ExtractionResult, file_name: str) -> Path:
    """Write the cache record atomically (temp file + os.replace): an interrupted write leaves no half file."""
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "file_name": file_name,
        "file_sha256": sha256,
        "model": result.model,
        "backend": result.backend,
        "created_on": result.created_on.isoformat(timespec="seconds"),
        "latency_ms": result.latency_ms,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                  "thinking_tokens": result.thinking_tokens},
        "extraction": result.data,
    }
    path = cache_path(sha256)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)  # never leave a half-written temp file behind
        raise
    return path


def _is_under(path: Path, folder: Path) -> bool:
    """True when path lies inside folder (a relative path is taken from the project root, like doc.file_path)."""
    path = path if path.is_absolute() else config.BASE_DIR / path
    return path.resolve().is_relative_to(Path(folder).resolve())


def fixture_path(pdf_path: Path) -> Path:
    """Ground-truth JSON of a sample file: tests/fixtures_v2/ for test set v2, else tests/fixtures/."""
    pdf_path = Path(pdf_path)
    folder = config.FIXTURES_V2_DIR if _is_under(pdf_path, config.INVOICES_V2_DIR) else config.FIXTURES_DIR
    return folder / f"{pdf_path.stem}.json"


def extract_from_fixture(pdf_path: Path) -> ExtractionResult:
    path = fixture_path(pdf_path)
    if not path.exists():
        raise ExtractionUnavailable(f"no fixture for {Path(pdf_path).name} (EXTRACTOR=fixture)")
    record = json.loads(path.read_text(encoding="utf-8"))
    return _result_from_record(record, from_cache=False, source="fixture")


# --------------------------------------------------------------------------------------------
# Files that never go to the model: UBL e-invoices and email bodies
# --------------------------------------------------------------------------------------------

EMAIL_BODY_ONLY = "no document attached: the invoice is only in the email body"


def _ubl_module() -> Any:
    """app.ubl, imported on first use (a seam for tests)."""
    from app import ubl

    return ubl


def extract_ubl(path: Path) -> ExtractionResult:
    """Parse a UBL e-invoice directly: no model call and no cache (parsing is instant and deterministic)."""
    path = Path(path)
    if not path.is_file():
        raise ExtractionUnavailable(f"{path.name} not found at {path}")
    ubl = _ubl_module()
    try:
        data = _postprocess(InvoiceExtraction.model_validate(ubl.parse_ubl(path.read_bytes())))
    except (ValueError, SyntaxError) as exc:  # ValidationError, a malformed XML (ParseError is a SyntaxError)
        raise ExtractionUnavailable(f"{path.name} is not a readable UBL e-invoice: {_short_error(exc)}") from exc
    print(f"[extract] file={path.name} parsed as a UBL e-invoice (no model call)")
    return ExtractionResult(model=ubl.UBL_MODEL, data=data, from_cache=False, source="ubl",
                            created_on=datetime.now().replace(microsecond=0))


# --------------------------------------------------------------------------------------------
# Prompt (brief section 7). Two iterations maximum (brief section 18); the confidence gate is
# there for the fields the model still gets wrong.
# --------------------------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """\
You extract accounts-payable data from one supplier document (an invoice, a credit note or another document).
Answer with JSON that matches the response schema. Every field is an object with "value" and "confidence".

General rules
- Extract only what is printed on the document. Never guess and never fill in typical or inferred values.
- If a field is not on the document, return value null with confidence 0. A printed zero (e.g. "VAT 0%:
  0,00 EUR") is a value: return 0.
- confidence is a number from 0 to 1: 0.95 or higher only when the value is clearly printed and unambiguous;
  lower when you had to infer it, choose between several candidates, or the text is partially legible.

Field rules
- doc_type: "invoice", "credit_note", "reminder" (a payment reminder, also when it reproduces an invoice),
  "statement" (a statement of account that lists open items) or "other" (e.g. an order confirmation). A plain
  copy of an invoice without a reminder is "invoice".
- supplier_name: the issuing company's name as printed on this document.
- supplier_country: ISO 3166-1 alpha-2 code of the supplier's address (e.g. DE, FR, GB, NL, ES, CH, US).
- supplier_vat_id and bill_to_vat_id: the tax identifier (VAT ID, UID, EIN, ...) without the label in front
  and without spaces; keep country prefixes, dots and hyphens ("VAT ID: DE 281 947 305" -> "DE281947305",
  "EIN 47-3829105" -> "47-3829105", "CHE-419.287.563" stays as is).
- supplier_iban: the supplier's IBAN without spaces. For bank details without an IBAN (e.g. US routing and
  account numbers) return "ABA <routing number> ACCT <account number>".
- bill_to_name: the name of the legal entity that is billed (the customer company), not an attention line,
  a store or a person.
- invoice_number: the document's own number (for a credit note, the credit note number).
- invoice_date and due_date: ISO format YYYY-MM-DD. due_date only if a due date is printed. Numeric dates
  are day first unless the document is clearly US-formatted ("30.09.2026" and "02/10/2026" are 30 September
  and 2 October 2026); lower the confidence if the order is ambiguous.
- payment_terms_days: an integer when the terms are stated as a number of days (e.g. "30 days net" -> 30);
  otherwise null. Do not compute it from dates.
- currency: ISO 4217 code (EUR, USD, GBP, ...).
- net_total, tax_total, gross_total and the line quantity, unit_price and amount: plain numbers with a dot as
  decimal separator and no thousands separator ("23.400,00" -> 23400.00, "9,600.00" -> 9600.00).
  For a credit note, all amounts (totals, unit prices, line amounts) are negative; quantities stay positive.
- po_numbers: only numbers explicitly labelled as a purchase order ("PO", "Your PO", "P.O.", "purchase
  order", "order no."). A contract reference, invoice number, customer number or project code is NOT a PO
  number. Never guess a PO number. If there is none, return null.
- referenced_invoice_number: for a credit note, the number of the invoice being credited; otherwise null.
- lines: one entry per invoice line with description, quantity, unit_price and amount as printed.
- notes: a short text with anything a reviewer should know: words such as "copy", "reminder" or "duplicate"
  (including watermarks), contract references, and anything odd or inconsistent. null if there is nothing.
"""

USER_INSTRUCTION = "Extract the fields of this document according to the schema and the rules."

REQUEST_TIMEOUT_MS = 120_000  # a hung request must not block the UI forever
# No tools are passed, so automatic function calling is off; saying so also silences the SDK's AFC warning.
NO_AFC = genai_types.AutomaticFunctionCallingConfig(disable=True)
RETRY_BACKOFF_S = 2.0  # first pause on 5xx / network errors (and a 429 without a server delay); doubles each retry
TRANSIENT_ATTEMPTS = 3  # calls per model on a transient error (the first call + 2 retries)
OVERLOADED_CODES = frozenset({429, 500, 503, 504})  # still failing after the retries: try the next GA Flash model
MAX_RETRY_DELAY_S = 60.0  # cap on the pause a 429 asks for (its retry delay + 1 s)
_RETRY_IN = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)  # "Please retry in 37.8s." in a 429 message
_DURATION = re.compile(r"(\d+(?:\.\d+)?)s")  # RetryInfo.retryDelay, e.g. "37s"

# GA Flash model names only: excludes preview, exp, lite, tts, image and live variants.
GA_FLASH_PATTERN = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-flash$")

# The model that worked in this process (None until the first successful call).
_resolved_model: Optional[str] = None
# Models the API rejected as unavailable in this process (404, no access): never asked again.
_unavailable_models: set[str] = set()
# The GA Flash models found with models.list, newest first (listed once per process).
_flash_models: Optional[list[str]] = None


# --------------------------------------------------------------------------------------------
# Gemini call
# --------------------------------------------------------------------------------------------


def _client() -> genai.Client:
    """Create the SDK client for the configured backend. Tests replace this seam with a fake, so they never
    touch the network.

    vertex: Vertex AI in GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION with Application Default Credentials
    (on Cloud Run, the service account; locally, `gcloud auth application-default login`). Without a project
    but with a key: Vertex AI express mode. aistudio: the Gemini Developer API with GEMINI_API_KEY.
    """
    http_options = genai_types.HttpOptions(timeout=REQUEST_TIMEOUT_MS)
    if config.GEMINI_BACKEND == "vertex":
        if config.GOOGLE_CLOUD_PROJECT:
            return genai.Client(vertexai=True, project=config.GOOGLE_CLOUD_PROJECT,
                                location=config.GOOGLE_CLOUD_LOCATION, http_options=http_options)
        return genai.Client(vertexai=True, api_key=config.GEMINI_API_KEY, http_options=http_options)
    return genai.Client(api_key=config.GEMINI_API_KEY, http_options=http_options)


def _describe(exc: Exception) -> str:
    """Short, readable form of an SDK or network error for logs and the UI."""
    if isinstance(exc, genai_errors.APIError):
        first_line = next(iter((exc.message or "").strip().splitlines()), "")
        return f"{exc.code} {exc.status or 'ERROR'}: {first_line[:200]}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _credentials_failed(exc: Exception) -> ExtractionFailed:
    """Vertex AI: no Application Default Credentials, or the access token could not be obtained. The SDK loads
    them at the first request, so this surfaces from generate_content / models.list, not from genai.Client().
    An ExtractionFailed like any API error: a batch then degrades to cache-only instead of aborting."""
    return ExtractionFailed(f"Vertex AI credentials: {_describe(exc)}")


def _is_model_unavailable(exc: Exception) -> bool:
    """True when the API rejects the model itself (not found, no access, zero quota), not the request."""
    if not isinstance(exc, genai_errors.ClientError):
        return False
    message = (exc.message or str(exc)).lower()
    if exc.code == 404:
        return True
    if exc.code == 403 and "aiplatform." in message:
        return False  # Vertex AI: a missing IAM role or a disabled API, not a model problem; falling back won't help
    if exc.code in (400, 403):
        return "model" in message
    if exc.code == 429:
        return "limit: 0" in message  # a quota of zero for this model means no access
    return False


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, genai_errors.APIError) and not _is_model_unavailable(exc):
        return exc.code == 429 or (exc.code or 0) >= 500
    return False


def _server_retry_delay(exc: genai_errors.APIError) -> Optional[float]:
    """Seconds a 429 asks us to wait: RetryInfo.retryDelay in the error details, else "retry in Ns" in the message."""
    body = exc.details.get("error", exc.details) if isinstance(exc.details, dict) else {}
    entries = body.get("details") if isinstance(body, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and str(entry.get("@type", "")).endswith("RetryInfo"):
            match = _DURATION.fullmatch(str(entry.get("retryDelay", "")).strip())
            if match:
                return float(match.group(1))
    match = _RETRY_IN.search(exc.message or "")
    return float(match.group(1)) if match else None


def _retry_delay(exc: Exception, attempt: int = 1) -> float:
    """Pause before retry number `attempt`: on a 429 the server's delay + 1 s (at most 60 s), otherwise
    RETRY_BACKOFF_S doubled at each retry (2 s, 4 s)."""
    if isinstance(exc, genai_errors.APIError) and exc.code == 429:
        delay = _server_retry_delay(exc)
        if delay is not None:
            return min(delay + 1, MAX_RETRY_DELAY_S)
    return RETRY_BACKOFF_S * 2 ** (attempt - 1)


def _is_overloaded(exc: Exception) -> bool:
    """A model that is still busy after the retries (503 high demand, 429 rate limit, 5xx): another GA Flash
    model may answer. Network errors are not model-specific and do not count."""
    return (isinstance(exc, genai_errors.APIError) and not _is_model_unavailable(exc)
            and exc.code in OVERLOADED_CODES)


def _temperature_for(model: str) -> Optional[float]:
    """0 for Gemini 1.x/2.x (repeatable output). None (API default 1.0) for Gemini 3+, where Google strongly
    recommends not lowering the temperature (risk of looping or degraded output)."""
    match = re.match(r"^gemini-(\d+)", model)
    return 0.0 if match and int(match.group(1)) < 3 else None


def _timed_generate(client: Any, model: str, contents: list[Any],
                    gen_config: genai_types.GenerateContentConfig) -> tuple[Any, int]:
    gen_config = gen_config.model_copy(update={"temperature": _temperature_for(model)})
    start = time.perf_counter()
    response = client.models.generate_content(model=model, contents=contents, config=gen_config)
    return response, round((time.perf_counter() - start) * 1000)


def _generate_once(client: Any, model: str, contents: list[Any],
                   gen_config: genai_types.GenerateContentConfig) -> tuple[Any, int]:
    """generate_content on one model, retried after a growing pause on a transient error (TRANSIENT_ATTEMPTS
    calls at most); returns latency. The last transient error is raised."""
    for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
        try:
            return _timed_generate(client, model, contents, gen_config)
        except (genai_errors.APIError, httpx.HTTPError) as exc:
            if not _is_transient(exc) or attempt == TRANSIENT_ATTEMPTS:
                raise
            delay = _retry_delay(exc, attempt)
            print(f"[extract] WARNING model={model} transient error ({_describe(exc)}) -> retrying in {delay:g} s")
            time.sleep(delay)
    raise AssertionError("unreachable")  # the loop returns or raises


def _supports_generate(model: Any) -> bool:
    """AI Studio lists each model's actions; Vertex AI publisher models carry none (None): accept those."""
    actions = getattr(model, "supported_actions", None)
    return actions is None or "generateContent" in actions


def _flash_candidates(client: Any) -> list[str]:
    """_discover_flash_models, listed once per process."""
    global _flash_models
    if _flash_models is None:
        _flash_models = _discover_flash_models(client)
    return _flash_models


def _discover_flash_models(client: Any) -> list[str]:
    """GA Flash models this key or project can use for generateContent, newest version first."""
    try:
        listed = list(client.models.list())
    except (genai_errors.APIError, httpx.HTTPError) as exc:
        raise ExtractionFailed(f"could not list the available Gemini models: {_describe(exc)}") from exc
    found = {}
    for m in listed:
        # "models/gemini-3.8-flash" (AI Studio) or "publishers/google/models/gemini-3.8-flash" (Vertex AI)
        name = (m.name or "").rsplit("/", 1)[-1]
        match = GA_FLASH_PATTERN.match(name)
        if match and _supports_generate(m):
            found[name] = (int(match.group(1)), int(match.group(2) or 0))
    return sorted(found, key=lambda name: (found[name], name), reverse=True)


def _generate(client: Any, contents: list[Any],
              gen_config: genai_types.GenerateContentConfig) -> tuple[Any, str, int]:
    """Call the working model. If the API rejects it as unavailable (404, no access), or it is still overloaded
    after the retries (503 high demand, 429, 5xx), try the next GA Flash model, newest first. Unavailable models
    are remembered for the rest of the process; the model that answered is used first from then on."""
    global _resolved_model
    model = _resolved_model or config.GEMINI_MODEL
    tried: list[str] = []
    if model in _unavailable_models:
        candidates = [m for m in _flash_candidates(client) if m not in _unavailable_models]
        if not candidates:
            raise ExtractionFailed(f"model {model} is unavailable and no GA Flash model is available to fall back to")
        model = candidates[0]
    while True:
        try:
            response, latency_ms = _generate_once(client, model, contents, gen_config)
        except _CREDENTIAL_ERRORS as exc:  # first: some of them are also ValueErrors (a malformed key file)
            raise _credentials_failed(exc) from exc
        except httpx.HTTPError as exc:
            raise ExtractionFailed(f"network error calling {model}: {_describe(exc)}") from exc
        except genai_errors.APIError as exc:
            unavailable, overloaded = _is_model_unavailable(exc), _is_overloaded(exc)
            if not (unavailable or overloaded):
                raise ExtractionFailed(f"Gemini API error on {model}: {_describe(exc)}") from exc
            tried.append(model)
            if unavailable:
                _unavailable_models.add(model)
            remaining = [m for m in _flash_candidates(client) if m not in tried and m not in _unavailable_models]
            if not remaining:
                why = "is unavailable" if unavailable else "is overloaded"
                raise ExtractionFailed(f"model {model} {why} ({_describe(exc)}) and no other GA Flash model is "
                                       f"available (tried: {', '.join(tried)})") from exc
            if unavailable:
                print(f"[extract] WARNING model={model} unavailable ({_describe(exc)}) -> falling back to {remaining[0]}")
            else:
                print(f"[extract] WARNING model={model} still overloaded after {TRANSIENT_ATTEMPTS} attempts "
                      f"({_describe(exc)}) -> trying {remaining[0]}")
            model = remaining[0]
            continue
        except ValueError as exc:  # the SDK could not parse a 200 response (JSONDecodeError, ValidationError)
            raise ExtractionFailed(f"unusable response from {model}: {_short_error(exc)}") from exc
        _resolved_model = model
        return response, model, latency_ms


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    return str(getattr(reason, "value", reason))


def _parse_response(response: Any) -> InvoiceExtraction:
    """Prefer the SDK's parsed object; else validate the JSON text. Raises ValueError if unusable."""
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, InvoiceExtraction):
        return parsed
    if isinstance(parsed, dict):
        return InvoiceExtraction.model_validate(parsed)
    text = getattr(response, "text", None)
    if not text:
        raise ValueError(f"empty response (finish_reason={_finish_reason(response)})")
    return InvoiceExtraction.model_validate_json(text)


def _is_empty(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value is None or value == []


def _postprocess(extraction: InvoiceExtraction) -> dict[str, Any]:
    """Clamp every confidence to [0, 1]; an empty value becomes null with confidence 0."""
    data = extraction.model_dump(mode="json")
    for name in FIELDS:
        field = data[name]
        if _is_empty(field["value"]):
            field["value"], field["confidence"] = None, 0.0
        else:
            field["confidence"] = min(1.0, max(0.0, float(field["confidence"])))
    return data


def _token_counts(usage: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """(input, output, thinking) tokens. Output includes the thinking tokens: both are billed as output."""
    tokens_in = getattr(usage, "prompt_token_count", None)
    candidates = getattr(usage, "candidates_token_count", None)
    thinking = getattr(usage, "thoughts_token_count", None)
    tokens_out = None if candidates is None and thinking is None else (candidates or 0) + (thinking or 0)
    return tokens_in, tokens_out, thinking


def call_gemini(pdf_bytes: bytes, file_name: str) -> ExtractionResult:
    """Call the configured Gemini model once for one PDF and return a validated result.

    Raises ExtractionFailed (an ExtractionUnavailable) on API errors, missing or unusable Vertex AI credentials,
    or an unusable response.
    """
    contents = [genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), USER_INSTRUCTION]
    gen_config = genai_types.GenerateContentConfig(
        automatic_function_calling=NO_AFC,
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=InvoiceExtraction,  # the SDK converts the pydantic model to its Schema
    )  # temperature is set per model in _timed_generate
    try:
        client = _client()
        response, model, latency_ms = _generate(client, contents, gen_config)
    except _CREDENTIAL_ERRORS as exc:  # from genai.Client() (ADC lookup) or models.list during a fallback
        raise _credentials_failed(exc) from exc
    try:
        extraction = _parse_response(response)
    except ValueError as exc:  # includes pydantic.ValidationError and JSON decode errors
        raise ExtractionFailed(f"{model} returned no usable extraction for {file_name}: "
                               f"{_short_error(exc)}") from exc
    tokens_in, tokens_out, thinking = _token_counts(getattr(response, "usage_metadata", None))
    print(f"[extract] file={file_name} model={model} latency_ms={latency_ms} "
          f"tokens_in={tokens_in} tokens_out={tokens_out} (thinking={thinking}) cache=miss "
          f"backend={config.GEMINI_BACKEND}")
    return ExtractionResult(
        model=model,
        data=_postprocess(extraction),
        from_cache=False,
        source="gemini",
        created_on=datetime.now().replace(microsecond=0),
        latency_ms=latency_ms,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        thinking_tokens=thinking,
        backend=config.GEMINI_BACKEND,
    )


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------


def not_configured_message() -> str:
    """Why the API cannot be called (config.gemini_configured() is False), with the variable to set."""
    where = " (GEMINI_BACKEND=vertex)" if config.GEMINI_BACKEND == "vertex" else ""
    return f"{config.gemini_missing_setting()} is not set{where}: add it to .env (see .env.example)"


def extract_file(pdf_path: Path, *, force: bool = False, allow_api: bool = True) -> ExtractionResult:
    """Extract one document file, routed by its suffix.

    .xml   UBL e-invoice: parsed directly (no model call, no cache; also in fixture mode).
    .txt   email body without attachment: always ExtractionUnavailable (nothing to extract).
    other  a PDF: fixture mode, else cache (unless force), else the Gemini API (if allowed and configured).
    """
    pdf_path = Path(pdf_path)
    suffix = pdf_path.suffix.lower()
    if suffix == ".txt":
        raise ExtractionUnavailable(EMAIL_BODY_ONLY)
    if suffix == ".xml":
        return extract_ubl(pdf_path)
    if config.EXTRACTOR == "fixture":
        return extract_from_fixture(pdf_path)
    if not pdf_path.is_file():
        raise ExtractionUnavailable(f"{pdf_path.name} not found at {pdf_path}")
    sha = file_sha256(pdf_path)
    if not force:
        cached = read_cache(sha)
        if cached is not None:
            print(f"[extract] file={pdf_path.name} cache=hit model={cached.model}")
            return cached
    if not allow_api:
        raise ExtractionUnavailable(f"{pdf_path.name} is not in the cache and API calls are disabled here")
    if not config.gemini_configured():
        raise ExtractionUnavailable(not_configured_message())
    result = call_gemini(pdf_path.read_bytes(), pdf_path.name)
    try:
        write_cache(sha, result, pdf_path.name)
    except OSError as exc:  # keep the (billed) result; the next run just misses the cache
        print(f"[extract] WARNING file={pdf_path.name} cache not written: {_short_error(exc)}")
    return result


DOC_TYPES = tuple(t.value for t in DocType)  # invoice | credit_note | reminder | statement | other


def _doc_type_from(data: dict[str, Any]) -> str:
    """The document type as read; "unknown" when nothing usable was read."""
    value = (data.get("doc_type") or {}).get("value")
    return value if value in DOC_TYPES else "unknown"


def _save_result(session: Session, doc: InboundDocument, result: ExtractionResult) -> Extraction:
    """Upsert the document's Extraction row from a result and set its doc_type."""
    row = doc.extraction or Extraction(doc_id=doc.doc_id)
    row.model = result.model
    row.json = result.data
    row.created_on = result.created_on
    row.from_cache = result.from_cache
    row.latency_ms = result.latency_ms
    row.input_tokens = result.input_tokens
    row.output_tokens = result.output_tokens
    doc.extraction = row
    doc.doc_type = _doc_type_from(result.data)
    session.commit()
    return row


def _refresh_siblings(session: Session, doc: InboundDocument) -> None:
    """Copy a fresh API result onto every other inbound document with the same file (e.g. the other
    scenario's copy). Cache only (allow_api=False), so it never calls the API and never recurses."""
    siblings = list(session.scalars(select(InboundDocument).where(
        InboundDocument.file_hash == doc.file_hash, InboundDocument.doc_id != doc.doc_id)))
    for sibling in siblings:
        try:
            if extract_document(session, sibling, allow_api=False) is not None:
                print(f"[extract] doc={sibling.doc_id} refreshed from the new result of doc={doc.doc_id}")
        except OSError as exc:  # e.g. the sibling's file cannot be read; doc's own result is already saved
            print(f"[extract] doc={sibling.doc_id} not refreshed: {_short_error(exc)}")


def extract_document(session: Session, doc: InboundDocument, *, force: bool = False,
                     allow_api: bool = True) -> Optional[Extraction]:
    """Extract one inbound document and upsert its Extraction row.

    Returns None when no extraction is available (no model access, not cached, API disabled, no fixture, no file,
    or an email body without attachment).
    Raises ExtractionFailed when the API was called and failed; the existing row is then left unchanged.
    After a fresh API result, the other inbound documents with the same file are refreshed from the cache.
    """
    try:
        result = extract_file(config.BASE_DIR / doc.file_path, force=force, allow_api=allow_api)
    except ExtractionFailed:
        raise  # the caller reports it (a failed API call is not "unavailable")
    except ExtractionUnavailable as exc:
        print(f"[extract] doc={doc.doc_id} unavailable: {exc}")
        return None
    row = _save_result(session, doc, result)
    if result.source == "gemini":
        _refresh_siblings(session, doc)
    return row


def extract_documents(session: Session, docs: list[InboundDocument], *, force: bool = False,
                      allow_api: bool = True) -> dict[str, Any]:
    """Extract several documents; a per-document failure never raises.

    Returns the counts extracted, from_cache, unavailable and failed, and error: the first ExtractionFailed
    message (None if there was none). After the first failure the remaining documents run with
    allow_api=False (cache only), so a broken key or network costs one failed call, not one per document.
    """
    summary: dict[str, Any] = {"extracted": 0, "from_cache": 0, "unavailable": 0, "failed": 0, "error": None}
    for doc in docs:
        try:
            row = extract_document(session, doc, force=force, allow_api=allow_api)
        except ExtractionFailed as exc:
            print(f"[extract] doc={doc.doc_id} FAILED: {exc}")
            summary["failed"] += 1
            summary["error"] = summary["error"] or str(exc)
            if allow_api:
                print("[extract] WARNING the API call failed: the remaining documents use the cache only")
                allow_api = False
            continue
        if row is None:
            summary["unavailable"] += 1
        else:
            summary["extracted"] += 1
            summary["from_cache"] += int(row.from_cache)
    return summary


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def _sample_numbers(text: str) -> list[int]:
    """argparse type for --only: "1,5,12" -> [1, 5, 12] (checked against the dataset's numbers in main)."""
    try:
        numbers = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated numbers, got {text!r}") from None
    if not numbers:
        raise argparse.ArgumentTypeError("expected at least one sample number")
    return numbers


def _dataset_documents(dataset: str) -> tuple[list[world.DocumentSpec], Path]:
    """The sample documents of a dataset and their folder: v1 = the 12 case documents, v2 = test set v2."""
    if dataset == "v2":
        return list(world.documents_for("v2")), config.INVOICES_V2_DIR
    return list(world.DOCUMENTS), config.INVOICES_DIR


def _ensure_files(dataset: str, paths: list[Path]) -> None:
    """Generate the sample files if some are missing (existing files are never rewritten)."""
    if dataset == "v1":
        from app import seed  # lazy: generates the PDFs (app.invoices_gen) only if some are missing

        seed.ensure_pdfs()
    elif any(not path.exists() for path in paths):
        from app import invoices_gen

        invoices_gen.generate_all_v2(config.INVOICES_V2_DIR)


def _needs_model(path: Path) -> bool:
    """Only PDFs go to the model: a UBL e-invoice is parsed, an email body has nothing to extract."""
    return path.suffix.lower() not in (".xml", ".txt")


def _min_critical_confidence(data: dict[str, Any]) -> Optional[float]:
    """Lowest confidence among the critical fields that have a value (None if none has one)."""
    values = [data[name]["confidence"] for name in CRITICAL_FIELDS if data[name]["value"] is not None]
    return min(values) if values else None


def _summary_row(no: int, file_name: str, result: Optional[ExtractionResult], note: str = "FAILED") -> list[str]:
    if result is None:
        return [str(no), file_name, note, "", "", "", "", ""]
    data = result.data
    gross = data["gross_total"]["value"]
    min_conf = _min_critical_confidence(data)
    return [str(no), file_name, str(data["doc_type"]["value"] or ""), str(data["invoice_number"]["value"] or ""),
            f"{gross:,.2f}" if gross is not None else "", f"{min_conf:.2f}" if min_conf is not None else "",
            result.model, result.source]


def _print_table(header: list[str], rows: list[list[str]], right: tuple[int, ...] = ()) -> None:
    """Plain fixed-width table; columns listed in `right` (numbers) are right-aligned."""
    widths = [max(len(row[i]) for row in [header] + rows) for i in range(len(header))]
    for row in [header] + rows:
        cells = [cell.rjust(w) if i in right else cell.ljust(w) for i, (cell, w) in enumerate(zip(row, widths))]
        print("  ".join(cells).rstrip())


def _update_database(paths: list[Path]) -> None:
    """Copy the (cached) results onto every inbound document, both scenarios, whose file hash matches."""
    from app.db import SessionLocal, init_db

    init_db()
    hashes = {file_sha256(path) for path in paths}
    with SessionLocal() as session:
        docs = list(session.scalars(select(InboundDocument).where(InboundDocument.file_hash.in_(hashes))))
        if not docs:
            print("[extract] database: no inbound documents match these files (load the sample documents first)")
            return
        summary = extract_documents(session, docs, allow_api=False)
    print(f"[extract] database: {summary['extracted']} inbound document(s) updated (both scenarios), "
          f"{summary['unavailable']} unavailable")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract the sample documents with a Gemini model "
                                                 "(cached on disk; UBL e-invoices are parsed without a model).")
    parser.add_argument("--dataset", choices=("v1", "v2"), default="v1",
                        help="v1 = the 12 case documents (default), v2 = test set v2 (26 documents)")
    parser.add_argument("--force-extract", action="store_true", help="call the API even if a cached result exists")
    parser.add_argument("--only", type=_sample_numbers, metavar="1,5,12",
                        help="comma-separated sample document numbers (default: all of the dataset)")
    parser.add_argument("--no-db", action="store_true", help="do not copy the results into the database")
    args = parser.parse_args(argv)

    documents, folder = _dataset_documents(args.dataset)
    by_no = {spec.no: spec for spec in documents}
    unknown = [no for no in args.only or () if no not in by_no]
    if unknown:
        parser.error(f"unknown sample number(s) {unknown} for dataset {args.dataset}; valid: 1..{len(documents)}")
    specs = [by_no[no] for no in (args.only or sorted(by_no))]
    paths = [folder / spec.filename for spec in specs]
    _ensure_files(args.dataset, paths)

    if config.EXTRACTOR != "fixture" and not config.gemini_configured():
        misses = [p.name for p in paths if _needs_model(p)
                  and (args.force_extract or not cache_path(file_sha256(p)).exists())]
        if misses:
            print(f"[extract] {len(misses)} document(s) are not in the cache and "
                  f"{config.gemini_missing_setting()} is not set: {', '.join(misses)}")
            print("[extract] Add your Google AI Studio key to .env as GEMINI_API_KEY=..., or use Vertex AI with "
                  "GEMINI_BACKEND=vertex and GOOGLE_CLOUD_PROJECT=... (see .env.example), then run this again.")
            return 2

    rows: list[list[str]] = []
    extracted: list[Path] = []
    for spec, path in zip(specs, paths):
        if path.suffix.lower() == ".txt":  # the email itself: nothing to extract, not a failure
            rows.append(_summary_row(spec.no, path.name, None, note="email body only"))
            continue
        try:
            result: Optional[ExtractionResult] = extract_file(path, force=args.force_extract)
            extracted.append(path)
        except Exception as exc:  # one bad file must not stop the batch
            detail = str(exc) if isinstance(exc, ExtractionUnavailable) else _describe(exc)
            print(f"[extract] file={path.name} FAILED: {detail}")
            result = None
        rows.append(_summary_row(spec.no, path.name, result))

    print()
    _print_table(["no", "file", "doc_type", "invoice_number", "gross_total", "min_conf", "model", "source"], rows,
                 right=(0, 4, 5))
    print()
    if extracted and not args.no_db:
        _update_database(extracted)
    expected = [path for path in paths if path.suffix.lower() != ".txt"]
    return 0 if len(extracted) == len(expected) else 1


if __name__ == "__main__":
    sys.exit(main())
