"""Optional (brief section 13): a Gemini model drafts a two-sentence message to the owner of a to-be exception.

- The prompt holds only facts from the gate decision and the document (supplier, invoice number, amount,
  PO / contract, exception label, owner, next owner, the gate's reason, the requested action from the
  taxonomy and the SLA due date); the model is told to add nothing else.
- One call per prompt through the extraction plumbing (extract._client / extract._generate): same model
  fallback, per-model temperature and single retry on transient errors. Plain text, no schema.
- Cached on disk as data/cache/drafts/<sha256>.json (key: system instruction + prompt), written atomically;
  a corrupt file is a miss. force=True re-calls the API.
- Never faked: in fixture mode, without model access (config.gemini_configured()), for a non-draftable decision
  or when the API fails, get_draft raises DraftUnavailable with a readable reason. The UI shows the text under
  DRAFT_LABEL.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from google.genai import types as genai_types

from app import config, extract, sim, taxonomy
from app.models import GateDecision, InboundDocument

DRAFT_LABEL = "Draft by Gemini — reviewed by AP"
MAX_CHARS = 600

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December")

SYSTEM_INSTRUCTION = """\
You draft short internal messages for the accounts-payable (AP) team of Velox Retail. Each message goes to the
person who owns an invoice exception and asks them to resolve it.

Rules
- English, plain text, exactly two sentences. No greeting line, no signature, no subject line, no list, no markdown.
- Address the owner by first name at the start of the first sentence (for example "Alex, invoice ...").
- Sentence 1: what is wrong, naming the supplier, the invoice number, the amount with its currency, and the PO or
  contract when one is listed.
- Sentence 2: politely ask for the requested action, made specific to this case, by the due date listed.
- If a next owner is listed, ask the owner only for their own part of the standard resolution (what the gate
  finding and their role point to) and say that the next owner takes over after that.
- Use only the facts listed. Never invent numbers, dates, names, deliveries or reasons; leave out anything that
  is not listed. Mention a next owner only if one is listed.

Example of the tone only (fictional facts, never reuse them):
Alex, invoice 77-0412 from Example Supplies GmbH (1,250 EUR) references PO 4500999, but no goods receipt is
recorded. Please post the receipt or tell AP what was delivered by Monday 5 October 2026.
"""


class DraftUnavailable(RuntimeError):
    """No draft can be shown: fixture mode, no model access, not draftable, not cached with the API disabled,
    API failure."""


@dataclass
class Draft:
    text: str
    model: str
    from_cache: bool
    created_on: datetime
    latency_ms: Optional[int] = None  # of the Gemini call that produced the text (also for a cache hit)

    @property
    def label(self) -> str:
        return DRAFT_LABEL


# --------------------------------------------------------------------------------------------
# Which decisions get a draft
# --------------------------------------------------------------------------------------------


def _why_not_draftable(decision: Optional[GateDecision]) -> Optional[str]:
    """None when the decision can get a draft; otherwise the reason, as one readable sentence."""
    if decision is None:
        return "This document has no gate decision yet."
    if decision.scenario != "tobe":
        return "Drafts are for to-be exceptions only: the as-is process has no owner to write to."
    etype = taxonomy.BY_KEY.get(decision.exception_type or "")
    if etype is None:
        return "This document has no open exception to write about."
    if etype.key == "email_loop":
        return "The untracked email loop has no owner to write to."
    if etype.key == "duplicate_invoice":  # touchless: no human step, so no message to draft
        return "Handled automatically: the supplier gets a status reply."
    if not etype.blocking:
        return f"'{etype.label}' is an information flag, not an exception with an owner."
    if not decision.owner_name:
        return "This exception has no owner to write to."
    return None


def is_draftable(decision: Optional[GateDecision]) -> bool:
    """To-be decisions with a blocking exception type that has an owner (not email_loop, not info flags, not a
    blocked duplicate: the supplier gets an automatic status reply)."""
    return _why_not_draftable(decision) is None


# --------------------------------------------------------------------------------------------
# Prompt (facts only)
# --------------------------------------------------------------------------------------------


def format_day(d: date) -> str:
    """date(2026, 10, 1) -> 'Thursday 1 October 2026' (English names whatever the locale)."""
    return f"{_DAY_NAMES[d.weekday()]} {d.day} {_MONTH_NAMES[d.month - 1]} {d.year}"


def format_amount(amount: Optional[float], currency: Optional[str]) -> Optional[str]:
    """48000.0, 'EUR' -> '48,000 EUR'; cents only when there are any ('27,846.50 EUR')."""
    if amount is None:
        return None
    value = float(amount)
    text = f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
    return f"{text} {currency}" if currency else text


def sla_due_date(decision: GateDecision, doc: InboundDocument) -> Optional[date]:
    """Registration (the ageing clock starts there) + sla_days business days; None without an SLA."""
    start = doc.registered_on or doc.received_on
    if decision.sla_days is None or start is None:
        return None
    due = sim.add_business_days(start, int(decision.sla_days))
    return due.date() if isinstance(due, datetime) else due


def _person(name: Optional[str], role: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return f"{name} ({role})" if role else name


def _business_days(n: int) -> str:
    return f"{n} business day" if n == 1 else f"{n} business days"


def build_prompt(decision: GateDecision, doc: InboundDocument) -> str:
    """One line per fact known for this decision; missing facts are left out, never filled in.

    Only for draftable decisions (the exception type must exist in the taxonomy).
    """
    details: dict[str, Any] = decision.details or {}
    etype = taxonomy.get(decision.exception_type or "")
    doc_type = details.get("doc_type") or doc.doc_type
    is_credit = doc_type == "credit_note"
    due = sla_due_date(decision, doc)
    next_owner = _person(details.get("next_owner_name"), details.get("next_owner_role"))
    facts = [
        ("Document type", {"invoice": "invoice", "credit_note": "credit note"}.get(doc_type or "")),
        ("Supplier", details.get("supplier_name")),
        ("Credit note number" if is_credit else "Invoice number", details.get("invoice_number")),
        ("Amount", format_amount(details.get("gross_total"), details.get("currency"))),
        ("Purchase order", details.get("po_number")),
        ("Contract", details.get("contract_id")),
        ("Exception", etype.label),
        ("Gate finding", decision.reason),
        ("Owner", _person(decision.owner_name, decision.owner_role)),
        ("Owner first name", next(iter((decision.owner_name or "").split()), None)),
        ("Standard resolution (owner, then next owner)" if next_owner else "Requested action", etype.resolution),
        ("Due by", f"{format_day(due)} (SLA {_business_days(int(decision.sla_days))} from registration)"
                   if due else None),
        ("Next owner after this step", next_owner),
    ]
    lines = [f"- {name}: {value}" for name, value in facts if value not in (None, "")]
    return "Facts:\n" + "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Cache (data/cache/drafts/<sha256>.json)
# --------------------------------------------------------------------------------------------


def cache_key(prompt: str) -> str:
    """sha256 of the system instruction and the prompt: a changed instruction never reuses old drafts."""
    return hashlib.sha256(f"{SYSTEM_INSTRUCTION}\n\n{prompt}".encode("utf-8")).hexdigest()


def cache_path(prompt: str) -> Path:
    return config.CACHE_DIR / "drafts" / f"{cache_key(prompt)}.json"  # CACHE_DIR read at call time


def read_cache(path: Path) -> Optional[Draft]:
    """The cached draft, or None on a miss. A corrupt file is logged and treated as a miss."""
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        text, model = record["text"], record["model"]
        if not isinstance(text, str) or not text.strip() or not isinstance(model, str):
            raise ValueError("text or model missing")
        return Draft(text=text, model=model, from_cache=True,
                     created_on=datetime.fromisoformat(record["created_on"]), latency_ms=record.get("latency_ms"))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"[draft] corrupt cache file {path.name} ignored ({type(exc).__name__}: {str(exc)[:120]})")
        return None


def write_cache(path: Path, record: dict[str, Any]) -> None:
    """Atomic write (temp file + os.replace), like extract.write_cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------------------------
# Gemini call
# --------------------------------------------------------------------------------------------


def clean_text(text: Optional[str]) -> str:
    """Strip, collapse whitespace (one paragraph), cap at MAX_CHARS characters."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 1].rstrip() + "…"
    return text


def _call_gemini(prompt: str) -> tuple[str, str, int]:
    """(text, model, latency_ms). Any failure becomes DraftUnavailable."""
    gen_config = genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION,
                                                   automatic_function_calling=extract.NO_AFC)  # temperature: extract
    try:
        client = extract._client()
        response, model, latency_ms = extract._generate(client, [prompt], gen_config)
        text = clean_text(getattr(response, "text", None))
    except extract.ExtractionFailed as exc:
        raise DraftUnavailable(f"Gemini could not draft the message: {exc}") from exc
    except Exception as exc:  # SDK, network or response errors: the page must still render
        raise DraftUnavailable(f"Gemini could not draft the message: {extract._describe(exc)}") from exc
    if not text:
        raise DraftUnavailable(f"Gemini ({model}) returned an empty draft.")
    return text, model, latency_ms


def get_draft(decision: Optional[GateDecision], doc: InboundDocument, *, allow_api: bool = True,
              force: bool = False) -> Draft:
    """The draft message to the owner of a to-be exception: from the cache, else one Gemini call.

    Raises DraftUnavailable (readable reason) when the decision is not draftable, in fixture mode, when the
    draft is not cached and the API is disabled or not configured (no key / no Vertex project), or when the
    call fails.
    """
    reason = _why_not_draftable(decision)
    if reason:
        raise DraftUnavailable(reason)
    if config.EXTRACTOR == "fixture":
        raise DraftUnavailable("Drafts need the Gemini API (EXTRACTOR=fixture).")
    prompt = build_prompt(decision, doc)
    path = cache_path(prompt)
    doc_id = doc.doc_id or decision.doc_id
    if not force:
        cached = read_cache(path)
        if cached is not None:
            print(f"[draft] doc={doc_id} model={cached.model} latency_ms={cached.latency_ms} cache=hit")
            return cached
    if not config.gemini_configured():  # checked first: the most useful reason when both apply
        raise DraftUnavailable(f"Set {config.gemini_missing_setting()} in .env to draft messages.")
    if not allow_api:
        raise DraftUnavailable("No cached draft for this exception, and API calls are disabled here.")
    try:
        text, model, latency_ms = _call_gemini(prompt)
    except DraftUnavailable as exc:
        print(f"[draft] doc={doc_id} FAILED: {exc}")
        raise
    print(f"[draft] doc={doc_id} model={model} latency_ms={latency_ms} cache=miss")
    draft = Draft(text=text, model=model, from_cache=False, created_on=datetime.now().replace(microsecond=0),
                  latency_ms=latency_ms)
    record = {"doc_id": doc_id, "exception_type": decision.exception_type, "model": model,
              "created_on": draft.created_on.isoformat(timespec="seconds"), "latency_ms": latency_ms,
              "prompt": prompt, "text": text}
    try:
        write_cache(path, record)
    except OSError as exc:  # keep the (billed) text; the next request just misses the cache
        print(f"[draft] WARNING doc={doc_id} cache not written: {type(exc).__name__}: {str(exc)[:120]}")
    return draft
