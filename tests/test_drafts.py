"""Draft messages to exception owners (app/drafts.py). None of these tests touches the network: the SDK
client is a fake or the installed SDK on an in-process httpx.MockTransport, and creating a real client is
recorded (and fails the call)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app import config, drafts, extract, taxonomy, world
from app.drafts import DRAFT_LABEL, Draft, DraftUnavailable
from app.models import GateDecision, InboundDocument

GOOD_TEXT = ("Jonas, invoice 2026-091 from FitOut Partners Ltd (48,000 EUR) references PO 4500123, but no service "
             "confirmation is recorded. Please confirm the service delivery by Monday 5 October 2026.")

# Every key of GateDecision.details (shared contract); None / False / [] when not applicable.
DETAIL_KEYS = (
    "sample_no", "supplier_name", "invoice_number", "invoice_number_norm", "gross_total", "net_total", "currency",
    "doc_type", "party_id", "true_party_id", "account_id", "resolution_method", "bill_to_entity", "posted_entity",
    "posted", "invoice_id", "wrong_entity_posting", "duplicate_posting", "duplicate_of", "commitment", "po_number",
    "contract_id", "contract_period", "doa_auto_approved", "requester_name", "next_owner_name", "next_owner_role",
    "credit_status", "applied_to", "flags", "terms_days", "terms_source", "invoice_terms_days", "agreed_terms_days",
    "terms_variance_paid", "touchless", "path", "cycle_breakdown", "registration_lag_days", "email_loop_days",
    "line_checks", "lookup_party_id", "invoice_date", "posted_on",
)

# Labels build_prompt may use; anything else in a prompt would be a fact the contract does not provide.
PROMPT_LABELS = {"Document type", "Supplier", "Invoice number", "Credit note number", "Amount", "Purchase order",
                 "Contract", "Exception", "Gate finding", "Owner", "Owner first name", "Requested action",
                 "Standard resolution (owner, then next owner)", "Due by", "Next owner after this step"}


# --------------------------------------------------------------------------------------------
# Helpers and fixtures
# --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def client_creations(monkeypatch) -> list[str]:
    """Every test: no real SDK client (an attempt is recorded, then fails), fresh model resolution, default
    model, no real pauses. Returns the list of attempted real client creations (must stay empty)."""
    attempts: list[str] = []

    def refuse_real_client():
        attempts.append("real client")
        raise AssertionError("tests must never create a real Gemini client")

    monkeypatch.setattr(extract, "_client", refuse_real_client)
    monkeypatch.setattr(extract, "_resolved_model", None)
    monkeypatch.setattr(extract.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    return attempts


@pytest.fixture()
def gemini_mode(monkeypatch, tmp_cache_dir):
    """EXTRACTOR=gemini on the AI Studio backend with a dummy key and an empty temporary cache (the drafts go
    to <cache>/drafts)."""
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_BACKEND", "aistudio")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    return tmp_cache_dir


class FakeModels:
    def __init__(self, respond, listed=()):
        self.respond = respond  # callable(model) -> response, or raises
        self.listed = list(listed)
        self.calls: list[str] = []
        self.requests: list[tuple] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        self.requests.append((contents, config))
        return self.respond(model)

    def list(self):
        return iter(self.listed)


class FakeClient:
    def __init__(self, respond, listed=()):
        self.models = FakeModels(respond, listed)


def text_response(text):
    return SimpleNamespace(text=text, parsed=None, usage_metadata=None, candidates=None)


def use_fake(monkeypatch, respond=None, listed=()) -> FakeModels:
    client = FakeClient(respond or (lambda model: text_response(GOOD_TEXT)), listed)
    monkeypatch.setattr(extract, "_client", lambda: client)
    return client.models


def make_details(**overrides) -> dict:
    """Details of the to-be decision on sample 5 (FitOut Partners, PO without service confirmation)."""
    spec = world.DOCUMENT_BY_NO[5]
    details = {key: None for key in DETAIL_KEYS}
    details.update(
        sample_no=5, supplier_name=spec.printed_supplier_name, invoice_number=spec.invoice_number,
        invoice_number_norm="2026091", gross_total=spec.gross_total, net_total=spec.net_total,
        currency=spec.currency, doc_type="invoice", party_id="P-0003", true_party_id="P-0003",
        account_id="V-000103", resolution_method="vat_id", bill_to_entity="VDE", posted=False,
        wrong_entity_posting=False, duplicate_posting=False, commitment="po", po_number="4500123",
        doa_auto_approved=False, requester_name="Jonas Weber", flags=[], terms_days=None,
        terms_variance_paid=False, touchless=False, path="exception",
        cycle_breakdown={"registration": 0, "extraction_and_gate": 0, "exception_sla": 2, "posting": 0},
        registration_lag_days=0, line_checks=[{"line_no": 1, "result": "no_confirmation"}],
    )
    details.update(overrides)
    return details


def make_decision(details: dict | None = None, **fields) -> GateDecision:
    values = dict(doc_id="B-05", scenario="tobe", outcome="exception", exception_type="po_no_receipt",
                  owner_role="Store development lead DE", owner_name="Jonas Weber", sla_days=2,
                  reason="PO 4500123 exists but no service confirmation is recorded for the invoiced milestone.",
                  simulated_days=2, decided_on=datetime(2026, 10, 1, 14, 2))
    values.update(fields)
    return GateDecision(steps=[], details=make_details() if details is None else details, **values)


def make_doc(sample_no: int = 5, scenario: str = "tobe", **fields) -> InboundDocument:
    spec = world.DOCUMENT_BY_NO[sample_no]
    registered = scenario == "tobe"
    values = dict(
        doc_id=f"{'B' if registered else 'A'}-{sample_no:02d}", scenario=scenario, sample_no=sample_no,
        channel=spec.channel, mailbox=spec.mailbox, received_on=spec.received_on,
        file_path=f"data/invoices/{spec.filename}", file_hash="0" * 64, sender_email=spec.sender_email,
        subject=spec.subject, registered=registered, registered_on=spec.received_on if registered else None,
        doc_type=spec.doc_type,
    )
    values.update(fields)
    return InboundDocument(**values)


def prompt_facts(prompt: str) -> dict[str, str]:
    """'- Label: value' lines -> {label: value}."""
    facts = {}
    for line in prompt.splitlines()[1:]:
        match = re.fullmatch(r"- ([^:]+): (.+)", line)
        assert match, f"unexpected prompt line {line!r}"
        facts[match.group(1)] = match.group(2)
    return facts


# --------------------------------------------------------------------------------------------
# Label, formatting, draftable decisions
# --------------------------------------------------------------------------------------------


def test_label_constant_is_exact():
    assert DRAFT_LABEL == "Draft by Gemini — reviewed by AP"
    assert Draft("x", "m", False, datetime(2026, 10, 1)).label == DRAFT_LABEL


@pytest.mark.parametrize("day, expected", [
    (date(2026, 10, 1), "Thursday 1 October 2026"),
    (date(2026, 10, 5), "Monday 5 October 2026"),
    (date(2027, 1, 31), "Sunday 31 January 2027"),
])
def test_format_day(day, expected):
    assert drafts.format_day(day) == expected


@pytest.mark.parametrize("amount, currency, expected", [
    (48000.0, "EUR", "48,000 EUR"),
    (27846.5, "EUR", "27,846.50 EUR"),
    (-1800.0, "EUR", "-1,800 EUR"),
    (9600, "USD", "9,600 USD"),
    (180.0, None, "180"),
    (None, "EUR", None),
])
def test_format_amount(amount, currency, expected):
    assert drafts.format_amount(amount, currency) == expected


@pytest.mark.parametrize("exception_type", ["po_no_receipt", "price_qty_mismatch", "wrong_legal_entity",
                                            "human_review", "unknown_vendor", "no_po",
                                            "credit_note_without_invoice", "po_not_found"])
def test_to_be_blocking_exceptions_with_an_owner_are_draftable(exception_type):
    assert drafts.is_draftable(make_decision(exception_type=exception_type))


@pytest.mark.parametrize("decision", [
    pytest.param(None, id="no decision"),
    pytest.param(make_decision(scenario="asis", outcome="exception", exception_type="email_loop", owner_role=None,
                               owner_name=None, sla_days=None), id="as-is email loop"),
    pytest.param(make_decision(scenario="asis", exception_type="po_no_receipt"), id="as-is with an owner"),
    pytest.param(make_decision(outcome="posted", exception_type=None, owner_role=None, owner_name=None,
                               sla_days=None, details=make_details(flags=[{"type": "terms_variance"}])),
                 id="to-be posted with an info flag"),
    pytest.param(make_decision(outcome="posted", exception_type="terms_variance", sla_days=None),
                 id="terms_variance info type"),
    pytest.param(make_decision(outcome="posted", exception_type="duplicate_vendor_account", sla_days=5),
                 id="duplicate_vendor_account info type"),
    pytest.param(make_decision(exception_type="email_loop", owner_name=None, sla_days=None), id="to-be email loop"),
    pytest.param(make_decision(outcome="blocked_duplicate", exception_type="duplicate_invoice",
                               owner_name="Marco Ruiz", owner_role="AP specialist", sla_days=0),
                 id="blocked duplicate (automatic status reply)"),
    pytest.param(make_decision(owner_name=None), id="no owner"),
    pytest.param(make_decision(exception_type="not_a_type"), id="unknown type"),
])
def test_other_decisions_are_not_draftable(decision):
    assert not drafts.is_draftable(decision)


# --------------------------------------------------------------------------------------------
# Prompt: the facts of the decision, nothing else
# --------------------------------------------------------------------------------------------


def test_prompt_contains_the_facts():
    decision, doc = make_decision(), make_doc()
    facts = prompt_facts(drafts.build_prompt(decision, doc))
    assert facts == {
        "Document type": "invoice",
        "Supplier": "FitOut Partners Ltd",
        "Invoice number": "2026-091",
        "Amount": "48,000 EUR",
        "Purchase order": "4500123",
        "Exception": taxonomy.label("po_no_receipt"),
        "Gate finding": decision.reason,
        "Owner": "Jonas Weber (Store development lead DE)",
        "Owner first name": "Jonas",
        "Requested action": taxonomy.get("po_no_receipt").resolution,
        # received (and registered on arrival) Thursday 1 October 2026 + 2 business days
        "Due by": "Monday 5 October 2026 (SLA 2 business days from registration)",
    }


def test_prompt_leaves_out_facts_the_decision_does_not_have():
    decision = make_decision(exception_type="wrong_legal_entity", owner_name="Marco Ruiz", owner_role="AP specialist",
                             sla_days=1, reason="Billed to Velox Retail SAS (VFR) but PO 4500126 belongs to VDE.",
                             details=make_details(invoice_number=None, po_number=None, currency=None))
    prompt = drafts.build_prompt(decision, make_doc(8))
    facts = prompt_facts(prompt)
    assert set(facts) <= PROMPT_LABELS
    assert {"Invoice number", "Purchase order", "Contract", "Next owner after this step"}.isdisjoint(facts)
    assert facts["Amount"] == "48,000"  # no currency given: none is added
    assert "None" not in prompt
    # received Friday 2 October 2026 + 1 business day
    assert facts["Due by"] == "Monday 5 October 2026 (SLA 1 business day from registration)"


def test_prompt_never_carries_internal_fields():
    prompt = drafts.build_prompt(make_decision(), make_doc())
    assert set(prompt_facts(prompt)) <= PROMPT_LABELS
    for internal in ("V-000103", "P-0003", "vat_id", "2026091", "no_confirmation", "exception_sla", "VDE"):
        assert internal not in prompt


def test_prompt_names_the_next_owner_and_the_contract_when_given():
    decision = make_decision(
        exception_type="price_qty_mismatch", owner_name="Tim Koch", owner_role="Warehouse lead, Berlin DC",
        details=make_details(po_number="4500112", contract_id="CT-2025-001", next_owner_name="Sofia Brandt",
                             next_owner_role="Buyer, Procurement DE"))
    facts = prompt_facts(drafts.build_prompt(decision, make_doc(12)))
    assert facts["Next owner after this step"] == "Sofia Brandt (Buyer, Procurement DE)"
    # the taxonomy resolution is the buyer's; the receiver is asked for their own part only
    assert facts["Standard resolution (owner, then next owner)"] == taxonomy.get("price_qty_mismatch").resolution
    assert "Requested action" not in facts
    assert facts["Contract"] == "CT-2025-001"
    assert facts["Owner first name"] == "Tim"
    # received Monday 5 October 2026 + 2 business days
    assert facts["Due by"].startswith("Wednesday 7 October 2026")


def test_prompt_for_a_credit_note_and_a_same_day_sla():
    decision = make_decision(exception_type="credit_note_without_invoice", owner_name="Marco Ruiz",
                             owner_role="AP specialist", sla_days=0, reason="No posted invoice INV-2026-9999.",
                             details=make_details(doc_type="credit_note", supplier_name="Bright Agency",
                                                  invoice_number="CN-2026-0031", gross_total=-1800.0,
                                                  po_number=None))
    facts = prompt_facts(drafts.build_prompt(decision, make_doc(4)))
    assert facts["Document type"] == "credit note"
    assert facts["Credit note number"] == "CN-2026-0031"
    assert "Invoice number" not in facts
    assert facts["Amount"] == "-1,800 EUR"
    # SLA 0: due on the registration day (Friday 2 October 2026)
    assert facts["Due by"] == "Friday 2 October 2026 (SLA 0 business days from registration)"


def test_due_date_counts_from_registration_not_from_receipt():
    doc = make_doc(registered_on=datetime(2026, 10, 9, 9, 0))  # a Friday
    assert drafts.sla_due_date(make_decision(sla_days=2), doc) == date(2026, 10, 13)
    assert drafts.sla_due_date(make_decision(sla_days=None), doc) is None


def test_unknown_document_type_is_left_out():
    facts = prompt_facts(drafts.build_prompt(make_decision(exception_type="human_review",
                                                           details=make_details(doc_type=None)),
                                             make_doc(doc_type="unknown")))
    assert "Document type" not in facts


# --------------------------------------------------------------------------------------------
# get_draft: reasons it is unavailable (never a fake text)
# --------------------------------------------------------------------------------------------


def test_fixture_mode_is_unavailable_even_with_a_cached_draft(gemini_mode, monkeypatch, client_creations):
    models = use_fake(monkeypatch)
    drafts.get_draft(make_decision(), make_doc())  # cached now
    monkeypatch.setattr(config, "EXTRACTOR", "fixture")
    with pytest.raises(DraftUnavailable) as info:
        drafts.get_draft(make_decision(), make_doc())
    assert str(info.value) == "Drafts need the Gemini API (EXTRACTOR=fixture)."
    assert len(models.calls) == 1 and client_creations == []


def test_fixture_mode_from_the_test_environment_never_creates_a_client(client_creations):
    assert config.EXTRACTOR == "fixture"  # tests/conftest.py
    with pytest.raises(DraftUnavailable, match=r"EXTRACTOR=fixture"):
        drafts.get_draft(make_decision(), make_doc())
    assert client_creations == []


def test_no_key_is_unavailable(gemini_mode, monkeypatch, client_creations):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(DraftUnavailable) as info:
        drafts.get_draft(make_decision(), make_doc())
    assert str(info.value) == "Set GEMINI_API_KEY in .env to draft messages."
    assert client_creations == []
    assert not (gemini_mode / "drafts").exists()


@pytest.mark.parametrize("decision, reason", [
    (make_decision(scenario="asis", exception_type="email_loop", owner_name=None, sla_days=None), "as-is"),
    (make_decision(outcome="posted", exception_type=None, owner_name=None, sla_days=None), "no open exception"),
    (make_decision(outcome="posted", exception_type="terms_variance", sla_days=None), "information flag"),
    (make_decision(outcome="blocked_duplicate", exception_type="duplicate_invoice", owner_name="Marco Ruiz",
                   sla_days=0), "Handled automatically: the supplier gets a status reply"),
    (make_decision(owner_name=None), "no owner"),
    (None, "no gate decision"),
])
def test_not_draftable_is_unavailable_with_a_reason(gemini_mode, monkeypatch, decision, reason):
    models = use_fake(monkeypatch)
    with pytest.raises(DraftUnavailable, match=reason):
        drafts.get_draft(decision, make_doc())
    assert models.calls == []


def test_vertex_without_project_names_the_missing_setting(gemini_mode, monkeypatch, client_creations):
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(DraftUnavailable) as info:
        drafts.get_draft(make_decision(), make_doc())
    assert str(info.value) == "Set GOOGLE_CLOUD_PROJECT in .env to draft messages."
    assert client_creations == []


def test_vertex_with_a_project_drafts_without_an_api_key(gemini_mode, monkeypatch):
    """config.gemini_configured(), not GEMINI_API_KEY, decides whether the API can be called."""
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "velox-demo")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    models = use_fake(monkeypatch)
    draft = drafts.get_draft(make_decision(), make_doc())
    assert draft.text == GOOD_TEXT and draft.from_cache is False and models.calls == ["gemini-2.5-flash"]


def test_no_key_reason_wins_over_api_disabled(gemini_mode, monkeypatch, client_creations):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(DraftUnavailable, match="Set GEMINI_API_KEY"):
        drafts.get_draft(make_decision(), make_doc(), allow_api=False)
    assert client_creations == []


def test_cache_miss_with_api_disabled_is_unavailable(gemini_mode, monkeypatch):
    models = use_fake(monkeypatch)
    with pytest.raises(DraftUnavailable, match="API calls are disabled"):
        drafts.get_draft(make_decision(), make_doc(), allow_api=False)
    assert models.calls == []


@pytest.mark.parametrize("error, fragment", [
    (genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad"}}),
     "400 INVALID_ARGUMENT"),
    (genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}}),
     "503 UNAVAILABLE"),
    (httpx.ConnectError("no route to host"), "network error"),
    (RuntimeError("unexpected SDK failure"), "RuntimeError: unexpected SDK failure"),
])
def test_api_failure_is_unavailable_and_not_cached(gemini_mode, monkeypatch, capsys, error, fragment):
    def fail(model):
        raise error

    use_fake(monkeypatch, fail)
    with pytest.raises(DraftUnavailable, match="Gemini could not draft the message") as info:
        drafts.get_draft(make_decision(), make_doc())
    assert fragment in str(info.value)
    assert "[draft] doc=B-05 FAILED" in capsys.readouterr().out
    assert not list(gemini_mode.rglob("*.json"))


def test_client_creation_failure_is_unavailable(gemini_mode, monkeypatch):
    def broken_client():
        raise ValueError("invalid client options")

    monkeypatch.setattr(extract, "_client", broken_client)
    with pytest.raises(DraftUnavailable, match="ValueError: invalid client options"):
        drafts.get_draft(make_decision(), make_doc())


@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_empty_response_is_unavailable_and_not_cached(gemini_mode, monkeypatch, text):
    use_fake(monkeypatch, lambda model: text_response(text))
    with pytest.raises(DraftUnavailable, match="empty draft"):
        drafts.get_draft(make_decision(), make_doc())
    assert not drafts.cache_path(drafts.build_prompt(make_decision(), make_doc())).exists()


# --------------------------------------------------------------------------------------------
# get_draft: the call, post-processing and the cache
# --------------------------------------------------------------------------------------------


def test_request_is_plain_text_with_the_system_instruction(gemini_mode, monkeypatch):
    models = use_fake(monkeypatch)
    decision, doc = make_decision(), make_doc()
    draft = drafts.get_draft(decision, doc)
    assert (draft.text, draft.model, draft.from_cache) == (GOOD_TEXT, "gemini-2.5-flash", False)
    contents, gen_config = models.requests[0]
    assert contents == [drafts.build_prompt(decision, doc)]
    assert gen_config.system_instruction == drafts.SYSTEM_INSTRUCTION
    assert gen_config.response_schema is None and gen_config.response_mime_type is None
    assert gen_config.temperature == 0.0  # per-model temperature from the extraction plumbing (Gemini 2.x)


def test_system_instruction_asks_for_two_facts_only_sentences():
    instruction = drafts.SYSTEM_INSTRUCTION
    for rule in ("English", "exactly two sentences", "first name", "No greeting line", "no signature",
                 "Use only the facts listed", "Never invent", "If a next owner is listed"):
        assert rule in instruction


def test_text_is_stripped_collapsed_and_capped(gemini_mode, monkeypatch):
    use_fake(monkeypatch, lambda model: text_response("  Jonas,   invoice 2026-091\n\n is blocked.\tPlease  act. \n"))
    assert drafts.get_draft(make_decision(), make_doc()).text == "Jonas, invoice 2026-091 is blocked. Please act."

    use_fake(monkeypatch, lambda model: text_response("word " * 300))
    text = drafts.get_draft(make_decision(), make_doc(), force=True).text
    assert len(text) <= drafts.MAX_CHARS and text.endswith("…")


def test_cache_miss_then_hit_then_force(gemini_mode, monkeypatch, capsys):
    replies = iter([GOOD_TEXT, "Jonas, second draft. Please act by Monday 5 October 2026."])
    models = use_fake(monkeypatch, lambda model: text_response(next(replies)))
    decision, doc = make_decision(), make_doc()

    first = drafts.get_draft(decision, doc)
    path = gemini_mode / "drafts" / f"{drafts.cache_key(drafts.build_prompt(decision, doc))}.json"
    assert path.exists() and not list(path.parent.glob("*.tmp"))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert (record["text"], record["model"], record["doc_id"]) == (GOOD_TEXT, "gemini-2.5-flash", "B-05")
    assert record["prompt"] == drafts.build_prompt(decision, doc)
    assert re.search(r"\[draft\] doc=B-05 model=gemini-2\.5-flash latency_ms=\d+ cache=miss", capsys.readouterr().out)

    second = drafts.get_draft(decision, doc)
    assert (second.text, second.from_cache, second.created_on) == (GOOD_TEXT, True, first.created_on)
    assert second.latency_ms == first.latency_ms
    assert re.search(r"\[draft\] doc=B-05 model=gemini-2\.5-flash latency_ms=\d+ cache=hit", capsys.readouterr().out)
    assert len(models.calls) == 1

    forced = drafts.get_draft(decision, doc, force=True)
    assert (forced.text, forced.from_cache) == ("Jonas, second draft. Please act by Monday 5 October 2026.", False)
    assert len(models.calls) == 2
    assert json.loads(path.read_text(encoding="utf-8"))["text"] == forced.text


def test_cached_draft_works_offline(gemini_mode, monkeypatch, client_creations):
    use_fake(monkeypatch)
    drafts.get_draft(make_decision(), make_doc())
    monkeypatch.setattr(extract, "_client", lambda: pytest.fail("a cached draft must not create a client"))
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert drafts.get_draft(make_decision(), make_doc(), allow_api=False).from_cache


def test_forced_draft_with_api_disabled_is_unavailable(gemini_mode, monkeypatch):
    models = use_fake(monkeypatch)
    drafts.get_draft(make_decision(), make_doc())
    with pytest.raises(DraftUnavailable, match="API calls are disabled"):
        drafts.get_draft(make_decision(), make_doc(), force=True, allow_api=False)
    assert len(models.calls) == 1


def test_different_facts_use_different_cache_entries(gemini_mode, monkeypatch):
    models = use_fake(monkeypatch)
    drafts.get_draft(make_decision(), make_doc())
    drafts.get_draft(make_decision(sla_days=1), make_doc())
    assert len(models.calls) == 2
    assert len(list((gemini_mode / "drafts").glob("*.json"))) == 2


def test_cache_key_covers_the_system_instruction(monkeypatch):
    prompt = drafts.build_prompt(make_decision(), make_doc())
    before = drafts.cache_key(prompt)
    monkeypatch.setattr(drafts, "SYSTEM_INSTRUCTION", drafts.SYSTEM_INSTRUCTION + "\n- One more rule.")
    assert drafts.cache_key(prompt) != before


@pytest.mark.parametrize("content", ["{not json", "[]", '{"text": "", "model": "m", "created_on": "2026-10-01"}',
                                     '{"text": "Hi.", "model": "m"}',
                                     '{"text": "Hi.", "model": "m", "created_on": "yesterday"}'])
def test_corrupt_cache_file_is_a_miss(gemini_mode, monkeypatch, capsys, content):
    models = use_fake(monkeypatch)
    path = drafts.cache_path(drafts.build_prompt(make_decision(), make_doc()))
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    draft = drafts.get_draft(make_decision(), make_doc())
    assert (draft.text, draft.from_cache, len(models.calls)) == (GOOD_TEXT, False, 1)
    assert "corrupt cache file" in capsys.readouterr().out
    assert json.loads(path.read_text(encoding="utf-8"))["text"] == GOOD_TEXT  # repaired


def test_failed_cache_write_keeps_the_draft(gemini_mode, monkeypatch, capsys):
    use_fake(monkeypatch)

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(drafts.os, "replace", failing_replace)
    draft = drafts.get_draft(make_decision(), make_doc())
    assert draft.text == GOOD_TEXT
    assert "cache not written" in capsys.readouterr().out
    assert not list((gemini_mode / "drafts").iterdir())  # no half-written temp file left


def test_model_fallback_of_the_extraction_plumbing_applies(gemini_mode, monkeypatch):
    def respond(model):
        if model == "gemini-2.5-flash":
            raise genai_errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND",
                                                           "message": "models/gemini-2.5-flash is not found."}})
        return text_response(GOOD_TEXT)

    listed = [SimpleNamespace(name=f"models/{name}", supported_actions=["generateContent"])
              for name in ("gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.8-flash-lite")]
    models = use_fake(monkeypatch, respond, listed)
    draft = drafts.get_draft(make_decision(), make_doc())
    assert draft.model == "gemini-3.8-flash"
    assert models.calls == ["gemini-2.5-flash", "gemini-3.8-flash"]
    assert models.requests[-1][1].temperature is None  # Gemini 3+: API default temperature


def test_draft_through_the_installed_sdk_on_a_mock_transport(gemini_mode, monkeypatch):
    """The real google-genai client builds the request and parses the text; httpx never leaves the process."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": f"  {GOOD_TEXT}\n"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 310, "candidatesTokenCount": 48, "totalTokenCount": 358},
        })

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(extract, "_client", lambda: genai.Client(
        api_key="test-key", http_options=genai_types.HttpOptions(httpx_client=http)))

    decision, doc = make_decision(), make_doc()
    draft = drafts.get_draft(decision, doc)

    assert draft.text == GOOD_TEXT
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == ["gemini-2.5-flash:generateContent"]
    body = json.loads(requests[0].content)
    assert body["systemInstruction"]["parts"][0]["text"] == drafts.SYSTEM_INSTRUCTION
    assert body["contents"][0]["parts"] == [{"text": drafts.build_prompt(decision, doc)}]
    generation = body["generationConfig"]
    assert generation["temperature"] == 0
    assert "responseSchema" not in generation and "responseMimeType" not in generation


def test_draft_for_rows_stored_in_the_database(session, gemini_mode, monkeypatch):
    """Works on persisted ORM rows (JSON details round-trip), not only on objects built in memory."""
    use_fake(monkeypatch)
    session.add_all([make_doc(), make_decision()])
    session.commit()
    session.expire_all()
    doc = session.query(InboundDocument).filter_by(doc_id="B-05").one()
    decision = session.query(GateDecision).filter_by(doc_id="B-05").one()
    assert drafts.is_draftable(decision)
    assert prompt_facts(drafts.build_prompt(decision, doc))["Due by"].startswith("Monday 5 October 2026")
    assert drafts.get_draft(decision, doc).text == GOOD_TEXT
