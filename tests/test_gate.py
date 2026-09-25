"""Control gate (brief section 8; app/gate.py): pure rules, then single documents and whole runs on the seed.

Database tests use the conftest `session` fixture (fresh seed, empty inboxes) and the fixture extractor.
"""
from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, extract, gate, seed, sim, world
from app.models import (
    CreditNoteApplication,
    GateDecision,
    InboundDocument,
    PendingVendorInvoice,
    ProductReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    Run,
    VendorAccount,
)
from app.normalize import normalise_name

ENTITIES = [(e.code, e.name, e.vat_id) for e in world.LEGAL_ENTITIES]
STEP_RESULTS = {"ok", "info", "flag", "exception", "blocked", "skipped", "created", "applied", "unapplied"}


def quiet(_line: str) -> None:
    pass


def load(session: Session, scenario: str) -> dict[int, InboundDocument]:
    """Load the sample documents of a scenario and extract them (fixture mode). Returns {sample_no: doc}."""
    docs = seed.load_sample_documents(session, scenario)
    extract.extract_documents(session, docs, allow_api=False)
    return {d.sample_no: d for d in docs}


def patch(doc: InboundDocument, **fields: tuple[Any, float]) -> None:
    """Overwrite extracted fields: patch(doc, invoice_number=("X-1", 0.5))."""
    data = copy.deepcopy(doc.extraction.json)
    for name, (value, conf) in fields.items():
        data[name] = {"value": value, "confidence": conf}
    doc.extraction.json = data


def decision(session: Session, doc_id: str) -> GateDecision:
    return session.scalars(select(GateDecision).where(GateDecision.doc_id == doc_id)).one()


def count(session: Session, model, scenario: str) -> int:
    return session.scalar(select(func.count()).select_from(model).where(model.scenario == scenario))


# --------------------------------------------------------------------------------------------
# Scenario configuration
# --------------------------------------------------------------------------------------------


def test_both_scenarios_configure_the_same_flags() -> None:
    assert set(gate.SCENARIOS) == set(config.SCENARIOS)
    assert set(gate.SCENARIOS["asis"]) == set(gate.SCENARIOS["tobe"])
    assert gate.SCENARIOS["tobe"]["confidence_threshold"] == config.CONFIDENCE_THRESHOLD
    assert gate.SCENARIOS["asis"]["confidence_threshold"] is None
    assert gate.SCENARIOS["tobe"]["doa_auto_approve_limit"] == 500.0


# --------------------------------------------------------------------------------------------
# Bill-to mapping: legal suffixes are kept, so the three Velox entities stay distinct
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name, vat, expected", [
    ("Velox Retail GmbH", None, "VDE"),
    ("Velox Retail SAS", None, "VFR"),
    ("Velox Retail Inc.", None, "VUS"),
    ("VELOX RETAIL S.A.S.", None, "VFR"),  # case and punctuation
    ("Velox Retail Inc", None, "VUS"),
    ("Velox Retail GmbHH", None, "VDE"),  # typo: fuzzy ratio >= 95
    ("Velox Retail SAS", "DE 298 765 431", "VDE"),  # the VAT ID wins over the name
    (None, "84-2716453", "VUS"),  # EIN of the US entity
    ("Velox Retail", None, None),  # no legal form: could be any of the three
    ("Metro Media GmbH", None, None),  # not a Velox entity
    (None, None, None),
    # The entity name followed by an attention line (document 10 has no bill-to VAT ID): whole-word prefix.
    ("Velox Retail GmbH Store Berlin 01", None, "VDE"),
    ("Velox Retail GmbH, Attn. Store Berlin 01", None, "VDE"),
    ("Velox Retail Inc. - Accounts Payable", None, "VUS"),
    ("Velox Retail GmbHX Store", None, None),  # not a whole-word prefix
    ("Velox Retail Store Berlin 01", None, None),  # no legal form
])
def test_map_bill_to(name, vat, expected) -> None:
    assert gate.map_bill_to(name, vat, ENTITIES) == expected


def test_map_bill_to_prefix_must_be_unique() -> None:
    entities = ENTITIES + [("VD2", "Velox Retail GmbH", "DE000000000")]  # two entities with the same name
    assert gate.map_bill_to("Velox Retail GmbH Store Berlin 01", None, entities) is None


def test_bill_to_mapping_does_not_use_the_suffix_stripping_name_normalisation() -> None:
    names = [e.name for e in world.LEGAL_ENTITIES]
    assert len({normalise_name(n) for n in names}) == 1  # 'velox retail' three times
    assert [gate.map_bill_to(n, None, ENTITIES) for n in names] == ["VDE", "VFR", "VUS"]


# --------------------------------------------------------------------------------------------
# Duplicates, tolerance, line mapping, confidence
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("a, gross_a, b, gross_b, expected", [
    ("NWL-2026-00913", 27846.0, "NWL-2026-00913", 27846.0, True),
    ("NWL-2026-00913", 27846.0, "NWL 2026 913 (COPY)", 27846.0, True),  # spaces, zeros, copy wording
    ("NWL-2026-00913", 27846.0, "NWL-2026-00913", 28100.0, True),  # 0.9% apart
    ("NWL-2026-00913", 27846.0, "NWL-2026-00913", 28200.0, False),  # 1.3% apart
    ("NWL-2026-00913", 27846.0, "NWL-2026-00914", 27846.0, False),
    (None, 27846.0, None, 27846.0, False),  # no number, no duplicate
    ("CN-2026-0031", -1800.0, "CN-2026-0031", -1800.0, True),
])
def test_same_invoice(a, gross_a, b, gross_b, expected) -> None:
    assert gate.same_invoice(a, gross_a, b, gross_b) is expected


@pytest.mark.parametrize("expected, tolerance", [(1000.0, 50.0), (2500.0, 50.0), (5000.0, 100.0), (6300.0, 126.0)])
def test_price_tolerance_is_two_percent_or_fifty_whichever_is_larger(expected, tolerance) -> None:
    assert gate.price_tolerance(expected) == tolerance


@pytest.mark.parametrize("invoiced, expected, ok", [
    (1050.00, 1000.0, True),  # +50 on 1,000: the 50 EUR floor applies (2% would be 20)
    (1050.01, 1000.0, False),
    (949.99, 1000.0, False),  # below the PO counts too
    (5100.00, 5000.0, True),  # +100 = 2% of 5,000
    (5100.01, 5000.0, False),
    (6426.00, 6300.0, True),
    (6600.00, 6300.0, False),  # document 6: +300 on 6,300
])
def test_within_tolerance_edges(invoiced, expected, ok) -> None:
    assert gate.within_tolerance(invoiced, expected) is ok


LED_PO = ["LED track spotlight 30W", "LED panel 600x600 40W"]


def test_map_lines_maps_reordered_lines_by_description() -> None:
    assert gate.map_lines(["LED panel 600x600 40W", "LED track spotlight 30W"], LED_PO) == [1, 0]
    assert gate.map_lines(["LED panel 600x600", "Track spotlight LED 30W"], LED_PO) == [1, 0]


def test_map_lines_falls_back_to_position_only_when_no_description_matches() -> None:
    assert gate.map_lines(["anything", "else"], LED_PO) == [0, 1]
    assert gate.map_lines([None, None], LED_PO) == [0, 1]  # no descriptions read
    assert gate.map_lines(["Garbled", "LED panel 600x600 40W"], LED_PO) == [0, 1]  # its PO line is free
    assert gate.map_lines(["LED panel 600x600 40W", "Delivery"], LED_PO) == [1, None]  # its PO line is taken
    assert gate.map_lines(["LED panel 600x600 40W", "Delivery", "Pallet"], LED_PO) == [1, None, None]  # counts differ


def test_map_lines_by_description_when_counts_differ() -> None:
    assert gate.map_lines(["LED panel 600x600 40W"], LED_PO) == [1]
    assert gate.map_lines(["LED track spotlight 30W", "LED panel 600x600", "Delivery"], LED_PO) == [0, 1, None]


def test_map_lines_split_and_repeated_lines_share_their_po_line() -> None:
    assert gate.map_lines(["LED track spotlight 30W", "LED panel 600x600 40W", "LED panel 600x600 40W"],
                          LED_PO) == [0, 1, 1]
    assert gate.map_lines(["Managed firewall service Q3 2026"] * 2, ["Managed firewall service Q3 2026"]) == [0, 0]
    # Identical PO descriptions: one-to-one by position.
    assert gate.map_lines(["Poster A2", "Poster A2"], ["Poster A2", "Poster A2"]) == [0, 1]


@pytest.mark.parametrize("text, expected", [
    ("2026-09-30", date(2026, 9, 30)),
    ("30.09.2026", date(2026, 9, 30)),  # day first, as the extraction prompt asks for non-ISO dates
    ("30/09/2026", date(2026, 9, 30)),
    ("9/3/2026", date(2026, 3, 9)),
    (" 01.10.2026 ", date(2026, 10, 1)),
    ("30.09/2026", None),  # mixed separators
    ("31.02.2026", None),  # no such day
    ("2026-13-01", None),
    ("September 30, 2026", None),
    ("", None),
    (None, None),
])
def test_parse_date(text, expected) -> None:
    assert gate.parse_date(text) == expected


def _extraction(**fields: Any) -> dict[str, Any]:
    return {name: {"value": v, "confidence": 0.99 if v is not None else 0.0} for name, v in fields.items()}


def test_net_amount_is_read_or_derived() -> None:
    lines = [_line("Spot", 80, 40.0), _line("Panel", 40, 50.0)]
    assert gate.net_amount(_extraction(net_total=5200.0, gross_total=6188.0, tax_total=988.0)) == 5200.0
    assert gate.net_amount(_extraction(net_total=None, gross_total=6188.0, tax_total=988.0)) == 5200.0
    assert gate.net_amount(_extraction(net_total=None, gross_total=6188.0, tax_total=None, lines=lines)) == 5200.0
    assert gate.net_amount(_extraction(net_total=None, gross_total=6188.0, tax_total=None, lines=None)) is None
    lines[1]["amount"] = lines[1]["unit_price"] = None
    assert gate.net_amount(_extraction(net_total=None, gross_total=6188.0, lines=lines)) is None


def test_lines_total_mismatch() -> None:
    lines = [_line("Spot", 80, 40.0), _line("Panel", 40, 50.0)]
    assert gate.lines_total_mismatch(lines, 5200.0, "EUR") is None
    assert gate.lines_total_mismatch(lines, 5304.0, "EUR") is None  # +104.00 = 2% of 5,200
    why = gate.lines_total_mismatch(lines[:1], 5200.0, "EUR")
    assert why.startswith("the invoice lines do not add up to the net total") and "3,200.00 EUR" in why
    assert gate.lines_total_mismatch(lines, None, "EUR") is None  # nothing to compare with
    assert gate.lines_total_mismatch([{"description": "Spot", "quantity": 80, "unit_price": None,
                                       "amount": None}], 5200.0, "EUR") is None  # the line check reports it


def _fields(**overrides: tuple[Any, float]) -> dict[str, Any]:
    data = {name: {"value": "x", "confidence": 0.99} for name in extract.CRITICAL_FIELDS}
    data["gross_total"] = {"value": 100.0, "confidence": 0.99}
    for name, (value, conf) in overrides.items():
        data[name] = {"value": value, "confidence": conf}
    return data


def test_low_confidence_fields() -> None:
    assert gate.low_confidence_fields(_fields(), 0.8) == []
    assert gate.low_confidence_fields(_fields(invoice_number=("INV-1", 0.55)), 0.8) == ["invoice number"]
    assert gate.low_confidence_fields(_fields(bill_to_name=(None, 0.0)), 0.8) == ["bill-to name"]
    # Supplier identity: the best of VAT ID / IBAN / name counts; a missing identifier does not.
    assert gate.low_confidence_fields(_fields(supplier_vat_id=("DE1", 0.4), supplier_iban=(None, 0.0)), 0.8) == []
    low = gate.low_confidence_fields(_fields(supplier_vat_id=("DE1", 0.4), supplier_iban=(None, 0.0),
                                             supplier_name=("Acme", 0.7)), 0.8)
    assert low == ["supplier identity (VAT ID, IBAN or name)"]


# --------------------------------------------------------------------------------------------
# Vendor resolution, PO line checks, log format
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("printed, account, method", [
    ("NORDWIND LOGISTICS", "V-000117", "exact_name"),  # document 2
    ("  bright agency ", "V-000119", "exact_name"),  # document 4: trimmed, case-insensitive
    ("Nordwind Logistics", "V-000117", "exact_name"),  # suffixes are NOT stripped for the exact hit
    ("Cleanspace Facilities BV", "V-000104", "exact_name"),  # document 11: lowest account ID wins (VDE)
    ("Nordwind Logistik", "V-000101", "fuzzy_name"),
    ("Blue Ocean Supplies", None, None),
])
def test_naive_account(printed, account, method) -> None:
    active = [a for a in seed.accounts_for("asis") if a.status == "active"]
    hit, how = gate.naive_account(printed, active)
    assert (hit.account_id if hit else None, how) == (account, method)


def _po(category: str, lines: list[tuple[str, float, float]], currency: str = "EUR") -> PurchaseOrder:
    return PurchaseOrder(po_number="4509999", category=category, currency=currency, legal_entity_code="VDE",
                         lines=[PurchaseOrderLine(line_no=i, description=d, qty=q, unit_price=p, amount=q * p,
                                                  receipt_required=True) for i, (d, q, p) in enumerate(lines, 1)])


def _receipt(line_no: int, qty: float, kind: str = "product_receipt") -> ProductReceipt:
    return ProductReceipt(po_number="4509999", line_no=line_no, qty_received=qty, received_by="Tim Koch", kind=kind)


def _line(desc: str, qty: float, unit: float) -> dict[str, Any]:
    return {"description": desc, "quantity": qty, "unit_price": unit, "amount": round(qty * unit, 2)}


def test_check_po_lines_goods() -> None:
    po = _po("goods", [("Spot", 80, 40.0), ("Panel", 40, 50.0)])
    lines = [_line("Spot", 80, 40.0), _line("Panel", 40, 50.0)]
    checks, issues = gate.check_po_lines(lines, po, [_receipt(1, 80), _receipt(2, 40)], "EUR")
    assert issues == [] and [c["result"] for c in checks] == ["ok", "ok"]
    _, issues = gate.check_po_lines(lines, po, [_receipt(1, 80), _receipt(2, 20)], "EUR")
    assert [(i["kind"], i["receiver"]) for i in issues] == [("quantity", "Tim Koch")]
    _, issues = gate.check_po_lines(lines, po, [_receipt(1, 80)], "EUR")
    assert [i["kind"] for i in issues] == ["no_receipt"]
    _, issues = gate.check_po_lines([_line("Spot", 90, 40.0), _line("Panel", 40, 50.0)], po,
                                    [_receipt(1, 90), _receipt(2, 40)], "EUR")
    assert [(i["kind"], i["receiver"]) for i in issues] == [("quantity", None)]  # 90 > 80 ordered: to the buyer
    checks, issues = gate.check_po_lines([_line("Spot", 80, 41.0), _line("Panel", 40, 50.0)], po,
                                         [_receipt(1, 80), _receipt(2, 40)], "EUR")
    assert [i["kind"] for i in issues] == ["price"]  # +80.00 on 3,200.00, tolerance 64.00
    assert (checks[0]["variance"], checks[0]["tolerance"]) == (80.0, 64.0)
    _, issues = gate.check_po_lines(lines, po, [_receipt(1, 80), _receipt(2, 40)], "GBP")
    assert [i["kind"] for i in issues] == ["price"] and "GBP" in issues[0]["text"]


def test_check_po_lines_services_need_a_service_confirmation() -> None:
    po = _po("service", [("Campaign", 1, 12000.0)])
    lines = [_line("Campaign", 1, 12000.0)]
    assert gate.check_po_lines(lines, po, [_receipt(1, 1, "service_confirmation")], "EUR")[1] == []
    _, issues = gate.check_po_lines(lines, po, [], "EUR")
    assert [i["kind"] for i in issues] == ["no_receipt"]
    assert "service confirmation" in issues[0]["text"]


def test_check_po_lines_aggregates_split_lines_per_po_line() -> None:
    """Document 12 with its panel line split in two: 20 + 20 invoiced, 20 received -> quantity issue to the
    receiver (each half alone would pass)."""
    po = _po("goods", [("LED track spotlight 30W", 80, 40.0), ("LED panel 600x600 40W", 40, 50.0)])
    lines = [_line("LED track spotlight 30W", 80, 40.0), _line("LED panel 600x600 40W", 20, 50.0),
             _line("LED panel 600x600 40W", 20, 50.0)]
    checks, issues = gate.check_po_lines(lines, po, [_receipt(1, 80), _receipt(2, 20)], "EUR")
    assert [(i["kind"], i["receiver"]) for i in issues] == [("quantity", "Tim Koch")]
    assert issues[0]["text"] == "Lines 2 and 3 (PO line 2): 40 invoiced but only 20 received on PO 4509999"
    assert [(c["line"], c["po_line"], c["invoiced_qty"], c["result"]) for c in checks] == [
        (1, 1, 80.0, "ok"), (2, 2, 20.0, "issue"), (3, 2, 20.0, "issue")]


def test_check_po_lines_catches_a_repeated_line() -> None:
    """Document 13's single service line invoiced twice (2 x 4,800 on a PO for 1)."""
    po = _po("service", [("Managed firewall service Q3 2026", 1, 4800.0)])
    lines = [_line("Managed firewall service Q3 2026", 1, 4800.0)] * 2
    _, issues = gate.check_po_lines(lines, po, [_receipt(1, 1, "service_confirmation")], "EUR")
    assert [i["kind"] for i in issues] == ["quantity"]
    assert issues[0]["text"] == "Lines 1 and 2 (PO line 1): 2 invoiced but only 1 ordered on PO 4509999"
    # Two lines of 1 x 2,400: 2 invoiced on a PO for 1, and 4,800 against 2 x 4,800 at the PO price.
    _, issues = gate.check_po_lines([_line("Managed firewall service Q3 2026", 1, 2400.0)] * 2, po,
                                    [_receipt(1, 1, "service_confirmation")], "EUR")
    assert [i["kind"] for i in issues] == ["price", "quantity"]


def test_check_po_lines_stores_floats_whatever_the_seed_types() -> None:
    """Seed rows built in memory carry ints (80), rows read back from SQLite floats (80.0): the record must be the
    same either way (the root cause of a flaky idempotency test)."""
    as_ints = _po("goods", [("Spot", 80, 40)])
    as_floats = _po("goods", [("Spot", 80.0, 40.0)])
    lines = [{"description": "Spot", "quantity": 80, "unit_price": 40, "amount": 3200}]
    int_checks, _ = gate.check_po_lines(lines, as_ints, [_receipt(1, 80)], "EUR")
    float_checks, _ = gate.check_po_lines(lines, as_floats, [_receipt(1, 80.0)], "EUR")
    assert repr(int_checks) == repr(float_checks)
    numbers = {k: v for k, v in int_checks[0].items() if isinstance(v, (int, float)) and k not in ("line", "po_line")}
    assert numbers and all(type(v) is float for v in numbers.values()), numbers


def test_check_po_header_when_no_lines_were_read() -> None:
    po = _po("goods", [("Spot", 80, 40.0), ("Panel", 40, 50.0)])
    checks, issues, unreadable = gate.check_po_header(5200.0, po, [_receipt(1, 80), _receipt(2, 40)], "EUR")
    assert (issues, unreadable) == ([], None)
    assert [c["result"] for c in checks] == ["ok", "ok", "ok"] and checks[0]["expected"] == 5200.0
    _, issues, _ = gate.check_po_header(5200.0, po, [_receipt(1, 80), _receipt(2, 20)], "EUR")
    assert [(i["kind"], i["receiver"]) for i in issues] == [("quantity", "Tim Koch")]
    _, issues, unreadable = gate.check_po_header(3200.0, po, [_receipt(1, 80), _receipt(2, 40)], "EUR")
    assert "could not be read" in unreadable and "5,200.00 EUR" in unreadable  # a partial invoice: undecidable
    assert gate.check_po_header(None, po, [], "EUR")[2] == "neither the invoice lines nor the net total could be read"
    service = _po("service", [("Campaign", 1, 12000.0)])
    assert gate.check_po_header(12000.0, service, [_receipt(1, 1, "service_confirmation")], "EUR")[1:] == ([], None)
    assert [i["kind"] for i in gate.check_po_header(12000.0, service, [], "EUR")[1]] == ["no_receipt"]


def test_log_line_format() -> None:
    line = gate.log_line(7, "step=resolve_vendor result=ok", party="Shopsys", account="V-000105", method="vat_id",
                         skipped=None)
    assert line == "[doc 07] step=resolve_vendor result=ok party=Shopsys account=V-000105 method=vat_id"
    assert gate.log_line(5, "outcome=exception", owner="Jonas Weber") == '[doc 05] outcome=exception owner="Jonas Weber"'


# --------------------------------------------------------------------------------------------
# Whole runs: record shape, log, registration, idempotency, re-run
# --------------------------------------------------------------------------------------------


def test_every_decision_has_all_detail_keys_and_the_nine_steps(session: Session) -> None:
    for scenario in config.SCENARIOS:
        load(session, scenario)
        gate.run_scenario(session, scenario, log=quiet)
    decisions = list(session.scalars(select(GateDecision)))
    assert len(decisions) == 2 * len(world.DOCUMENTS)
    for d in decisions:
        assert set(d.details) == set(gate.DETAIL_KEYS), d.doc_id
        assert tuple(s["step"] for s in d.steps) == gate.STEP_NAMES, d.doc_id
        assert {s["result"] for s in d.steps} <= STEP_RESULTS, d.doc_id
        assert d.reason and d.decided_on is not None and d.details["path"] in sim.PATHS
        assert d.simulated_days == sum(d.details["cycle_breakdown"].values())
        det, spec = d.details, world.DOCUMENT_BY_NO[d.details["sample_no"]]
        assert det["invoice_date"] == spec.invoice_date.isoformat(), d.doc_id
        assert (det["posted_on"] is not None) is det["posted"], d.doc_id
        if det["posted"]:
            assert det["posted_on"] == d.decided_on.isoformat(), d.doc_id
        assert det["lookup_party_id"] == (spec.party_id if d.scenario == "tobe" else None), d.doc_id


def test_run_log_lines(session: Session) -> None:
    load(session, "tobe")
    lines: list[str] = []
    run = gate.run_scenario(session, "tobe", log=lines.append)
    assert run.summary_json["log"] == lines
    assert "[doc 07] step=resolve_vendor result=ok party=Shopsys account=V-000105 method=vat_id" in lines
    assert lines[-1].startswith("[run] scenario=tobe documents=14 touchless=10 exceptions=4 ")
    doc_line = re.compile(r'^\[doc \d{2}\] (step=[a-z_]+ result=[a-z]+|outcome=[a-z_]+)( [a-z_]+=("[^"]*"|\S+))*$')
    assert all(doc_line.match(line) for line in lines[:-1])
    outcome_lines = [line for line in lines if " outcome=" in line]
    assert len(outcome_lines) == 14
    assert "[doc 06] outcome=exception exception=price_qty_mismatch owner=\"Sofia Brandt\" sla=2 days=2 " \
           "touchless=no" in lines
    assert run.summary_json["outcomes"] == {"posted": 8, "exception": 4, "blocked_duplicate": 1, "applied_credit": 1}


def test_registration_per_scenario_and_clear_results(session: Session) -> None:
    asis, tobe = load(session, "asis"), load(session, "tobe")
    gate.run_scenario(session, "asis", log=quiet)
    gate.run_scenario(session, "tobe", log=quiet)
    for doc in asis.values():
        assert doc.registered and doc.registered_on == sim.registration_date("asis", doc.channel, doc.received_on)
    assert all(doc.registered_on == doc.received_on for doc in tobe.values())
    assert decision(session, "A-02").details["registration_lag_days"] == 7  # store mailbox
    assert decision(session, "A-01").details["registration_lag_days"] == 1
    gate.clear_results(session, "asis")
    assert not any(doc.registered or doc.registered_on for doc in asis.values())
    assert all(doc.registered for doc in tobe.values())
    for model in (GateDecision, PendingVendorInvoice, CreditNoteApplication, Run):
        assert count(session, model, "asis") == 0
        assert count(session, model, "tobe") > 0


def _snapshot(session: Session, scenario: str) -> dict[str, Any]:
    decisions = session.scalars(select(GateDecision).where(GateDecision.scenario == scenario))
    postings = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.scenario == scenario))
    credits = session.scalars(select(CreditNoteApplication).where(CreditNoteApplication.scenario == scenario))
    return {
        "decisions": sorted((d.doc_id, d.outcome, d.exception_type, d.owner_name, d.sla_days, d.reason,
                             d.simulated_days, repr(d.details), repr(d.steps), d.decided_on) for d in decisions),
        "postings": sorted((p.invoice_id, p.doc_id, p.vendor_account_id, p.total, p.status, p.due_date, p.posted_on,
                            repr(p.flags)) for p in postings),
        "credits": sorted((c.credit_note_id, c.doc_id, c.applied_to_invoice_id, c.status) for c in credits),
        "accounts": count(session, VendorAccount, scenario),
    }


@pytest.mark.parametrize("scenario", config.SCENARIOS)
def test_run_scenario_is_idempotent(session: Session, scenario: str) -> None:
    load(session, scenario)
    gate.run_scenario(session, scenario, log=quiet)
    first = _snapshot(session, scenario)
    gate.run_scenario(session, scenario, log=quiet)
    assert _snapshot(session, scenario) == first
    assert count(session, Run, scenario) == 1


@pytest.mark.parametrize("scenario", config.SCENARIOS)
def test_run_is_idempotent_whether_seed_rows_come_from_memory_or_the_database(session: Session,
                                                                              scenario: str) -> None:
    """Deterministic version of what made the idempotency test flaky: seed objects still in the identity map hold
    the ints of app/world.py (80), rows read back from SQLite hold floats (80.0). The first run uses in-memory ints
    (held on purpose), the second fresh rows after expunge_all(); the records must be identical."""
    load(session, scenario)
    held: list[Any] = list(session.scalars(select(PurchaseOrder))) + list(session.scalars(select(ProductReceipt)))
    for obj in held:
        if isinstance(obj, PurchaseOrder):
            for pl in obj.lines:
                pl.qty = int(pl.qty)
        else:
            obj.qty_received = int(obj.qty_received)
    gate.run_scenario(session, scenario, log=quiet)
    first = _snapshot(session, scenario)
    session.expunge_all()
    gate.run_scenario(session, scenario, log=quiet)
    assert _snapshot(session, scenario) == first
    assert held  # kept alive until here


def test_postings_and_credit_applications(session: Session) -> None:
    load(session, "tobe")
    gate.run_scenario(session, "tobe", log=quiet)
    postings = {p.doc_id: p for p in session.scalars(select(PendingVendorInvoice).where(
        PendingVendorInvoice.scenario == "tobe"))}
    assert sorted(p.invoice_id for p in postings.values()) == [f"PVI-B-{n:04d}" for n in range(1, 10)]
    assert postings["B-04"].total == -1800.0 and postings["B-04"].status == "credit_applied"
    assert postings["B-04"].terms_days is None and not postings["B-04"].terms_source  # a credit note has no terms
    assert postings["B-01"].terms_source == "master" and postings["B-01"].due_date == date(2026, 10, 30)
    assert postings["B-01"].flags == {"wrong_entity": False, "duplicate_of": None, "terms_variance": True,
                                      "doa_auto_approved": False}
    assert postings["B-10"].flags["doa_auto_approved"] is True
    credit = session.scalars(select(CreditNoteApplication).where(CreditNoteApplication.scenario == "tobe")).one()
    assert (credit.credit_note_id, credit.doc_id, credit.status) == ("CN-2026-0031", "B-04", "applied")
    assert credit.applied_to_invoice_id == postings["B-03"].invoice_id


def test_asis_duplicate_posting_and_unapplied_credit(session: Session) -> None:
    load(session, "asis")
    gate.run_scenario(session, "asis", log=quiet)
    doc2 = decision(session, "A-02").details
    assert (doc2["duplicate_posting"], doc2["duplicate_of"], doc2["true_party_id"]) == (True, "A-01", "P-0001")
    assert decision(session, "A-02").details["email_loop_days"] == sim.email_loop_days("02")
    credit = session.scalars(select(CreditNoteApplication).where(CreditNoteApplication.scenario == "asis")).one()
    assert (credit.status, credit.applied_to_invoice_id) == ("unapplied", None)
    posting = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.doc_id == "A-04")).one()
    assert (posting.vendor_account_id, posting.total, posting.status) == ("V-000119", -1800.0, "unapplied_credit")


def test_rerun_document_keeps_the_rest_of_the_run(session: Session) -> None:
    docs = load(session, "tobe")
    gate.run_scenario(session, "tobe", log=quiet)
    before = _snapshot(session, "tobe")
    # Document 1 comes before its resent copy (document 2): re-running it must not see the copy as the original.
    again = gate.rerun_document(session, docs[1], log=quiet)
    assert again.outcome == "posted" and again.details["duplicate_of"] is None
    assert gate.rerun_document(session, docs[2], log=quiet).details["duplicate_of"] == "B-01"
    gate.rerun_document(session, docs[3], log=quiet)  # the invoice the credit note is applied to
    assert _snapshot(session, "tobe") == before


def test_rerun_document_processes_earlier_unprocessed_documents_first(session: Session) -> None:
    """Running single documents out of order must not post the resend of an invoice: rerun_document first
    processes, in order, every earlier document of the scenario that has no decision yet."""
    docs = load(session, "tobe")
    lines: list[str] = []
    resend = gate.rerun_document(session, docs[2], log=lines.append)  # document 2 is the last one to arrive
    assert (resend.outcome, resend.details["duplicate_of"]) == ("blocked_duplicate", "B-01")
    assert count(session, GateDecision, "tobe") == 14
    order = [int(line[5:7]) for line in lines if " outcome=" in line]
    assert order == [7, 1, 3, 9, 10, 5, 14, 11, 6, 4, 8, 13, 12, 2]  # processing order, the document last


def test_rerun_document_leaves_later_documents_alone(session: Session) -> None:
    docs = load(session, "tobe")
    credit = gate.rerun_document(session, docs[4], log=quiet)  # the credit note: invoice 3 is processed first
    assert (credit.outcome, credit.details["credit_status"]) == ("applied_credit", "applied")
    assert credit.details["applied_to"] == decision(session, "B-03").details["invoice_id"]
    assert count(session, GateDecision, "tobe") == 10  # documents 7, 1, 3, 9, 10, 5, 14, 11, 6 and 4
    assert gate.rerun_document(session, docs[4], log=quiet).details["applied_to"] == credit.details["applied_to"]
    assert count(session, GateDecision, "tobe") == 10


@pytest.mark.parametrize("scenario", config.SCENARIOS)
def test_rerun_document_extracts_what_is_missing(session: Session, scenario: str) -> None:
    docs = {d.sample_no: d for d in seed.load_sample_documents(session, scenario)}  # not extracted
    d = gate.rerun_document(session, docs[3], log=quiet)
    assert d.details["invoice_number"] == "INV-2026-0457"
    assert all(docs[n].extraction is not None for n in (7, 1, 3))  # document 3 and the two before it
    assert docs[4].extraction is None


# --------------------------------------------------------------------------------------------
# Single documents with synthetic extractions
# --------------------------------------------------------------------------------------------


def test_low_confidence_goes_to_human_review_in_tobe_only(session: Session) -> None:
    for scenario in config.SCENARIOS:
        patch(load(session, scenario)[3], invoice_number=("INV-2026-0457", 0.55))
        gate.run_scenario(session, scenario, log=quiet)
    tobe = decision(session, "B-03")
    assert (tobe.outcome, tobe.exception_type, tobe.owner_name, tobe.sla_days, tobe.simulated_days) == (
        "human_review", "human_review", "Marco Ruiz", 1, 1)
    assert "invoice number" in tobe.reason and tobe.details["posted"] is False
    assert [s["result"] for s in tobe.steps][:2] == ["ok", "exception"]
    assert all(s["result"] == "skipped" for s in tobe.steps[2:])
    assert decision(session, "A-03").outcome == "posted"  # as-is has no confidence threshold


def test_missing_extraction(session: Session) -> None:
    tobe, asis = load(session, "tobe")[10], load(session, "asis")[10]
    for doc in (tobe, asis):
        doc.extraction = None
    session.commit()
    review = gate.process(session, tobe, log=quiet)
    assert (review.outcome, review.owner_name, review.sla_days) == ("human_review", "Marco Ruiz", 1)
    assert "No extraction" in review.reason
    loop = gate.process(session, asis, log=quiet)
    assert (loop.outcome, loop.exception_type, loop.owner_name, loop.details["posted"]) == (
        "exception", "email_loop", None, False)
    assert loop.details["email_loop_days"] == sim.email_loop_days("10")


UNKNOWN_SUPPLIER = dict(supplier_name=("Blue Ocean Supplies Ltd", 0.99), supplier_vat_id=("DE111222333", 0.99),
                        supplier_iban=("DE02100100100006820101", 0.99))


def test_unknown_vendor_is_routed_to_master_data_in_tobe(session: Session) -> None:
    doc = load(session, "tobe")[10]
    patch(doc, **UNKNOWN_SUPPLIER)
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.owner_name, d.sla_days) == ("exception", "unknown_vendor", "Lena Fischer", 2)
    assert d.details["account_id"] is None and d.details["posted"] is False
    assert count(session, VendorAccount, "tobe") == len(world.CLEAN_ACCOUNTS)


def test_unknown_vendor_gets_an_account_on_the_fly_in_asis(session: Session) -> None:
    doc = load(session, "asis")[10]
    patch(doc, **UNKNOWN_SUPPLIER)
    n_accounts = count(session, VendorAccount, "asis")
    d = gate.process(session, doc, log=quiet)
    assert (d.details["resolution_method"], d.details["account_id"], d.details["posted"]) == ("created", "V-000129", True)
    assert d.details["true_party_id"] is None and d.details["party_id"] is None
    created = session.scalars(select(VendorAccount).where(VendorAccount.scenario == "asis",
                                                          VendorAccount.account_id == "V-000129")).one()
    assert (created.created_by, created.display_name, created.legal_entity_code, created.payment_terms_days,
            created.corruption_rules, created.notes) == (
        gate.ON_THE_FLY_CREATOR, "Blue Ocean Supplies Ltd", "VDE", 14, ["gate"], gate.ON_THE_FLY_NOTE)
    assert gate.process(session, doc, log=quiet).details["account_id"] == "V-000129"  # re-run: no second account
    assert count(session, VendorAccount, "asis") == n_accounts + 1
    gate.clear_results(session, "asis")
    assert count(session, VendorAccount, "asis") == n_accounts


def test_credit_note_without_a_known_invoice(session: Session) -> None:
    tobe, asis = load(session, "tobe")[4], load(session, "asis")[4]
    d = gate.process(session, tobe, log=quiet)  # invoice 3 not processed yet
    assert (d.outcome, d.exception_type, d.owner_name, d.sla_days) == (
        "exception", "credit_note_without_invoice", "Marco Ruiz", 2)
    assert d.details["posted"] is False and count(session, PendingVendorInvoice, "tobe") == 0
    assert session.scalars(select(CreditNoteApplication.status).where(
        CreditNoteApplication.doc_id == "B-04")).one() == "unapplied"
    a = gate.process(session, asis, log=quiet)
    assert (a.outcome, a.details["credit_status"], a.details["touchless"]) == ("posted", "unapplied", True)


def test_resolution_by_name_when_identifiers_are_missing(session: Session) -> None:
    doc = load(session, "tobe")[1]
    patch(doc, supplier_vat_id=(None, 0.0), supplier_iban=(None, 0.0), supplier_name=("Nordwind Logistics", 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.details["resolution_method"], d.details["party_id"], d.details["account_id"]) == (
        "name", "P-0001", "V-000101")


def test_duplicate_vendor_account_is_an_info_flag_in_tobe(session: Session) -> None:
    doc = load(session, "tobe")[1]
    session.add(VendorAccount(scenario="tobe", account_id="V-000140", legal_entity_code="VDE", party_id=None,
                              display_name="NORDWIND LOGISTICS", vat_id="DE281947305", iban=None,
                              payment_terms_days=14, created_by="test", created_on=date(2026, 1, 1)))
    session.commit()
    d = gate.process(session, doc, log=quiet)
    assert d.outcome == "posted" and d.details["touchless"] is True and d.details["account_id"] == "V-000101"
    flags = {f["type"]: f for f in d.details["flags"]}
    assert set(flags) == {"duplicate_vendor_account", "terms_variance"}
    assert flags["duplicate_vendor_account"]["owner_name"] == "Lena Fischer"
    assert "V-000140" in flags["duplicate_vendor_account"]["detail"]


def test_doa_limit_is_strict(session: Session) -> None:
    doc = load(session, "tobe")[10]
    patch(doc, gross_total=(500.0, 0.99))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.owner_name, d.sla_days) == ("exception", "no_po", "Paul Neumann", 2)
    assert d.simulated_days == 3  # SLA 2 + workflow approval 1
    assert d.details["cycle_breakdown"]["workflow_approval"] == 1


def test_contract_period_is_matched_once(session: Session) -> None:
    docs = load(session, "tobe")
    patch(docs[2], invoice_number=("NWL-2026-00999", 0.99))  # a second Nordwind invoice for September
    gate.run_scenario(session, "tobe", log=quiet)
    d = decision(session, "B-02")
    assert (d.outcome, d.exception_type, d.owner_name) == ("exception", "no_po", "Nina Hoffmann")  # contract owner
    assert "already invoiced for 2026-09" in d.reason


def test_an_unreadable_invoice_date_never_claims_a_contract_period(session: Session) -> None:
    """Two Nordwind invoices whose date cannot be read: neither is matched to the contract (an unknown period is
    not 'already invoiced for None'); both go to human review."""
    docs = load(session, "tobe")
    patch(docs[1], invoice_date=("end of September", 0.9))
    patch(docs[2], invoice_date=("end of September", 0.9), invoice_number=("NWL-2026-00999", 0.99))
    gate.run_scenario(session, "tobe", log=quiet)
    for doc_id in ("B-01", "B-02"):
        d = decision(session, doc_id)
        assert (d.outcome, d.exception_type, d.owner_name, d.details["posted"]) == (
            "human_review", "human_review", "Marco Ruiz", False), doc_id
        assert "invoice date could not be read" in d.reason and d.details["contract_id"] is None
        assert d.details["invoice_date"] is None and d.details["contract_period"] is None


@pytest.mark.parametrize("printed", ["30.09.2026", "30/09/2026"])
def test_day_first_invoice_dates_are_read(session: Session, printed: str) -> None:
    doc = load(session, "tobe")[11]
    patch(doc, invoice_date=(printed, 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.details["contract_id"], d.details["invoice_date"], d.details["contract_period"]) == (
        "posted", "CT-2025-003", "2026-09-30", "2026-09")
    posting = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.doc_id == "B-11")).one()
    assert (posting.invoice_date, posting.due_date) == (date(2026, 9, 30), date(2026, 10, 30))


# --------------------------------------------------------------------------------------------
# Documents that stop early still count for later checks (party lookup)
# --------------------------------------------------------------------------------------------


def test_documents_in_human_review_still_count_for_duplicates_and_credit_notes(session: Session) -> None:
    """Document 1 and document 3 stop at extraction (gross total at 0.79): the resend (document 2) is still blocked
    as a duplicate of B-01, and the credit note (document 4) still finds invoice INV-2026-0457."""
    docs = load(session, "tobe")
    patch(docs[1], gross_total=(27846.0, 0.79))
    patch(docs[3], gross_total=(14400.0, 0.79))
    gate.run_scenario(session, "tobe", log=quiet)
    for doc_id, party in (("B-01", "P-0001"), ("B-03", "P-0002")):
        d = decision(session, doc_id)
        assert (d.outcome, d.details["party_id"], d.details["lookup_party_id"]) == ("human_review", None, party)
    resend = decision(session, "B-02")
    assert (resend.outcome, resend.details["duplicate_of"], resend.details["posted"]) == (
        "blocked_duplicate", "B-01", False)
    credit = decision(session, "B-04")
    assert (credit.outcome, credit.details["credit_status"], credit.details["applied_to"]) == (
        "applied_credit", "applied", "B-03")  # B-03 is not posted yet: the document itself


def test_a_document_in_human_review_claims_its_contract_period(session: Session) -> None:
    docs = load(session, "tobe")
    patch(docs[1], gross_total=(27846.0, 0.79))  # in human review, never reaches the contract match
    patch(docs[2], invoice_number=("NWL-2026-00999", 0.99))  # a second Nordwind invoice for September
    gate.run_scenario(session, "tobe", log=quiet)
    d = decision(session, "B-02")
    assert (d.outcome, d.exception_type, d.owner_name) == ("exception", "no_po", "Nina Hoffmann")
    assert "already invoiced for 2026-09 (B-01)" in d.reason


def test_the_lookup_party_is_to_be_only(session: Session) -> None:
    doc = load(session, "asis")[1]
    patch(doc, gross_total=(27846.0, 0.79))
    assert gate.process(session, doc, log=quiet).details["lookup_party_id"] is None


# --------------------------------------------------------------------------------------------
# Commitment match: line reconciliation, split / repeated / reordered / missing lines, PO numbers
# --------------------------------------------------------------------------------------------

LUMEN_LINES = [{"description": "LED track spotlight 30W", "quantity": 80, "unit_price": 40.0, "amount": 3200.0},
               {"description": "LED panel 600x600 40W", "quantity": 40, "unit_price": 50.0, "amount": 2000.0}]


def test_a_dropped_line_is_not_posted(session: Session) -> None:
    """Document 12 read without its panel line: the spotlight line alone would match and be received in full,
    but the lines no longer add up to the 5,200.00 net total."""
    doc = load(session, "tobe")[12]
    patch(doc, lines=(LUMEN_LINES[:1], 0.9))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.owner_name, d.details["posted"]) == (
        "human_review", "human_review", "Marco Ruiz", False)
    assert "the invoice lines do not add up to the net total" in d.reason
    assert d.details["line_checks"][0]["result"] == "ok"  # the line itself is fine


def test_a_dropped_line_is_an_email_loop_in_asis(session: Session) -> None:
    """PO 4500112 is not in the as-is ERP, so this uses document 9 (PO 4500114 exists in both scenarios)."""
    doc = load(session, "asis")[9]
    patch(doc, lines=([{"description": "A2 in-store posters, 4-colour", "quantity": 400, "unit_price": 3.5,
                        "amount": 1400.0}], 0.9))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.details["posted"], d.details["path"]) == (
        "exception", "email_loop", True, "email_loop")
    assert "the invoice lines do not add up to the net total" in d.reason


def test_split_lines_are_checked_together(session: Session) -> None:
    doc = load(session, "tobe")[12]
    half = dict(LUMEN_LINES[1], quantity=20, amount=1000.0)
    patch(doc, lines=([LUMEN_LINES[0], half, dict(half)], 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.owner_name, d.details["next_owner_name"]) == (
        "exception", "price_qty_mismatch", "Tim Koch", "Sofia Brandt")
    assert "Lines 2 and 3 (PO line 2): 40 invoiced but only 20 received" in d.reason


def test_a_repeated_line_is_caught(session: Session) -> None:
    """Document 13's line read twice. With the totals doubled it is a quantity issue for the buyer; with the printed
    totals, the lines do not add up."""
    firewall = {"description": "Managed firewall service Q3 2026", "quantity": 1, "unit_price": 4800.0,
                "amount": 4800.0}
    doc = load(session, "tobe")[13]
    patch(doc, lines=([firewall, dict(firewall)], 0.95), net_total=(9600.0, 0.95), gross_total=(9600.0, 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.owner_name) == ("exception", "price_qty_mismatch", "Sofia Brandt")
    assert "2 invoiced but only 1 ordered on PO 4500128" in d.reason
    patch(doc, net_total=(4800.0, 0.99), gross_total=(4800.0, 0.99))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.owner_name) == ("human_review", "Marco Ruiz") and "do not add up" in d.reason


def test_reordered_lines_are_mapped_by_description(session: Session) -> None:
    doc = load(session, "tobe")[9]
    patch(doc, lines=(list(reversed(doc.extraction.json["lines"]["value"])), 0.99))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.details["touchless"]) == ("posted", True)
    assert [(c["line"], c["po_line"], c["result"]) for c in d.details["line_checks"]] == [(1, 2, "ok"), (2, 1, "ok")]


@pytest.mark.parametrize("no, outcome, owner", [
    (9, "posted", None),  # goods: the net total is the PO total, received in full
    (3, "posted", None),  # service: confirmed
    (12, "exception", "Tim Koch"),  # the net total is the PO total, but PO line 2 is only half received
])
def test_no_lines_read_checks_the_po_at_header_level(session: Session, no: int, outcome: str, owner) -> None:
    doc = load(session, "tobe")[no]
    patch(doc, lines=(None, 0.0))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.owner_name) == (outcome, owner)
    assert "None" not in d.reason
    assert d.details["line_checks"][0]["description"].startswith("No invoice lines read")
    if outcome == "posted":
        assert "no invoice lines read, net total within tolerance of the PO total" in d.reason


def test_no_lines_and_a_partial_net_total_go_to_human_review(session: Session) -> None:
    doc = load(session, "tobe")[12]
    patch(doc, lines=(None, 0.0), net_total=(3200.0, 0.95), gross_total=(3200.0, 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.owner_name) == ("human_review", "Marco Ruiz")
    assert "the invoice lines could not be read" in d.reason and "None" not in d.reason


def test_received_in_full_only_when_the_whole_po_is_received(session: Session) -> None:
    """Document 12 invoicing only the spotlights (received in full); the panels are only half received."""
    docs = load(session, "tobe")
    patch(docs[12], lines=(LUMEN_LINES[:1], 0.95), net_total=(3200.0, 0.95), gross_total=(3200.0, 0.95))
    d = gate.process(session, docs[12], log=quiet)
    assert d.outcome == "posted"
    assert "received in full" not in d.reason and "the invoiced quantities received" in d.reason
    assert "and received in full" in gate.process(session, docs[9], log=quiet).reason


@pytest.mark.parametrize("printed", [["PO 4500117"], ["P.O. #4500117"], ["po-4500117"], ["4599999", "PO 4500117"]])
def test_po_numbers_are_normalised_and_each_is_tried(session: Session, printed: list[str]) -> None:
    doc = load(session, "tobe")[3]
    patch(doc, po_numbers=(printed, 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.details["commitment"], d.details["po_number"]) == ("posted", "po", "4500117")


def test_po_not_found_goes_to_ap_because_no_requester_can_be_derived(session: Session) -> None:
    doc = load(session, "tobe")[3]
    patch(doc, po_numbers=(["4599999", "4599998"], 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.owner_name, d.owner_role) == (
        "exception", "po_not_found", "Marco Ruiz", "AP specialist")
    assert "no requester can be derived: AP gets the correct PO from the supplier" in d.reason
    assert "4599999, 4599998" in d.reason


# --------------------------------------------------------------------------------------------
# Amounts, currency, bill-to, invoice numbers
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("printed", ["eur", "€", " EUR "])
def test_currency_is_normalised(session: Session, printed: str) -> None:
    doc = load(session, "tobe")[3]
    patch(doc, currency=(printed, 0.95))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.details["currency"]) == ("posted", "EUR")
    assert session.scalars(select(PendingVendorInvoice.currency).where(
        PendingVendorInvoice.doc_id == "B-03")).one() == "EUR"


def test_net_is_derived_when_missing(session: Session) -> None:
    doc = load(session, "tobe")[11]
    patch(doc, net_total=(None, 0.0))  # gross 3,200 - tax 0
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.details["net_total"], d.details["contract_id"]) == ("posted", 3200.0, "CT-2025-003")
    patch(doc, tax_total=(None, 0.0))  # the sum of the lines
    assert gate.process(session, doc, log=quiet).details["contract_id"] == "CT-2025-003"
    patch(doc, lines=(None, 0.0))  # nothing left to derive it from: no gross-vs-net-range comparison
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.owner_name, d.details["net_total"]) == ("human_review", "Marco Ruiz", None)
    assert "the net amount could not be read" in d.reason


def test_a_negative_invoice_is_a_probable_credit_note(session: Session) -> None:
    doc = load(session, "tobe")[10]
    patch(doc, gross_total=(-150.0, 0.99), net_total=(-126.05, 0.99))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.owner_name, d.details["doa_auto_approved"], d.details["posted"]) == (
        "human_review", "Marco Ruiz", False, False)
    assert "negative total, probable credit note" in d.reason


def test_doa_auto_approval_needs_a_positive_amount(session: Session) -> None:
    doc = load(session, "tobe")[10]
    patch(doc, gross_total=(0.0, 0.99))
    d = gate.process(session, doc, log=quiet)
    assert (d.outcome, d.exception_type, d.owner_name, d.details["doa_auto_approved"]) == (
        "exception", "no_po", "Paul Neumann", False)


def test_bill_to_with_an_attention_line_maps_to_its_entity(session: Session) -> None:
    """Document 10 prints no bill-to VAT ID; the model may append the attention line to the entity name."""
    for scenario in config.SCENARIOS:
        patch(load(session, scenario)[10], bill_to_name=("Velox Retail GmbH Store Berlin 01", 0.95))
        gate.run_scenario(session, scenario, log=quiet)
    tobe, asis = decision(session, "B-10"), decision(session, "A-10")
    assert (tobe.outcome, tobe.details["bill_to_entity"], tobe.details["doa_auto_approved"]) == ("posted", "VDE", True)
    assert (asis.details["bill_to_entity"], asis.details["wrong_entity_posting"]) == ("VDE", False)


def test_an_unmapped_bill_to_is_not_counted_as_a_wrong_entity_posting(session: Session) -> None:
    doc = load(session, "asis")[10]
    patch(doc, bill_to_name=("Velox Retail", 0.95))  # could be any of the three entities
    d = gate.process(session, doc, log=quiet)
    assert (d.details["posted"], d.details["bill_to_entity"], d.details["wrong_entity_posting"]) == (True, None, False)
    posting = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.doc_id == "A-10")).one()
    assert posting.flags["wrong_entity"] is False


@pytest.mark.parametrize("printed", ["COPIE NWL-2026-00913", "Invoice no. NWL-2026-00913", "NWL 2026 913 (COPIA)"])
def test_labelled_or_copy_invoice_numbers_are_still_duplicates(session: Session, printed: str) -> None:
    docs = load(session, "tobe")
    patch(docs[2], invoice_number=(printed, 0.95))
    gate.run_scenario(session, "tobe", log=quiet)
    d = decision(session, "B-02")
    assert (d.outcome, d.details["duplicate_of"]) == ("blocked_duplicate", "B-01")
