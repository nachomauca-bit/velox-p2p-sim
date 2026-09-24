"""Web UI and intake tests (TestClient). conftest.py provides a temp DB, EXTRACTOR=fixture and no API key."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config, extract, main, seed, sim, world
from app.models import InboundDocument

NAV_PATHS = [path for _, items in main.NAV_GROUPS for path, _ in items]
MINIMAL_PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.fixture()
def client(session):
    """A TestClient created after the `session` fixture has seeded the temp DB."""
    with TestClient(main.app) as c:
        yield c


def use_scenario(client: TestClient, scenario: str) -> None:
    r = client.post(f"/scenario/{scenario}", follow_redirects=False)
    assert r.status_code == 303


def load_documents(client: TestClient, scenario: str) -> str:
    use_scenario(client, scenario)
    r = client.post("/documents/load")  # redirects to /inbox, which shows the flash message
    assert r.status_code == 200
    assert r.url.path == "/inbox"
    return r.text


def mailbox_section(html: str, channel: str) -> str:
    """HTML of one mailbox column of the inbox."""
    start = html.index(f'id="mailbox-{channel}"')
    end = html.find('<section class="mailbox"', start)
    return html[start:end if end != -1 else len(html)]


def count_docs(session, scenario: str) -> int:
    return session.scalar(select(func.count()).select_from(InboundDocument)
                          .where(InboundDocument.scenario == scenario))


def get_doc(session, doc_id: str) -> InboundDocument:
    return session.scalar(select(InboundDocument).where(InboundDocument.doc_id == doc_id))


def use_gemini(monkeypatch, key: str = "test-key") -> None:
    """Gemini mode with a fake key; tests must still replace extract.call_gemini (never the real API)."""
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", key)


def fake_gemini(model: str, **overrides):
    """Stand-in for extract.call_gemini: the ground-truth fixture relabelled as a Gemini output."""
    def call(pdf_bytes: bytes, file_name: str) -> extract.ExtractionResult:
        record = json.loads((config.FIXTURES_DIR / f"{Path(file_name).stem}.json").read_text(encoding="utf-8"))
        data = record["extraction"]
        for name, value in overrides.items():
            data[name] = {"value": value, "confidence": 0.9}
        return extract.ExtractionResult(model=model, data=data, from_cache=False, source="gemini",
                                        created_on=datetime(2026, 10, 1, 12, 0), latency_ms=1200,
                                        input_tokens=1000, output_tokens=200)
    return call


def failing_gemini(message: str, calls: list[str]):
    """Stand-in for extract.call_gemini that always fails like a broken key or network."""
    def call(pdf_bytes: bytes, file_name: str) -> extract.ExtractionResult:
        calls.append(file_name)
        raise extract.ExtractionFailed(message)
    return call


# --------------------------------------------------------------------------------------------
# Layout, navigation, scenario switch
# --------------------------------------------------------------------------------------------


def test_health_and_root_redirect(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and r.headers["location"] == "/inbox"


@pytest.mark.parametrize("scenario", config.SCENARIOS)
def test_every_nav_page_returns_200(client, scenario):
    use_scenario(client, scenario)
    for path in NAV_PATHS:
        r = client.get(path)
        assert r.status_code == 200, path
        assert config.SCENARIO_LABELS[scenario] in r.text


def test_layout_footer_nav_groups_and_header(client):
    html = client.get("/inbox").text
    assert config.FOOTER_TEXT in html
    assert "Velox ERP (mock, system of record)" in html
    assert "Control gate (new)" in html
    assert main.APP_TITLE in html
    for label in config.SCENARIO_LABELS.values():
        assert label in html
    assert "Load sample documents" in html
    assert 'title="Phase 2"><button type="button" disabled>Run scenario' in html
    assert 'action="/reset"' in html and "confirm(" in html


def test_scenario_switch_sets_cookie_and_redirects_back(client):
    r = client.post("/scenario/tobe", headers={"referer": "http://testserver/erp/vendors?view=asis"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/erp/vendors"
    assert "scenario=tobe" in r.headers["set-cookie"]
    assert client.cookies.get("scenario") == "tobe"
    assert 'switch-tobe active' in client.get("/inbox").text


def test_scenario_switch_ignores_foreign_referer_and_rejects_bad_values(client):
    r = client.post("/scenario/asis", headers={"referer": "https://evil.example/phish"}, follow_redirects=False)
    assert r.headers["location"] == "/inbox"
    assert client.post("/scenario/prod", follow_redirects=False).status_code == 400


def test_back_path_only_returns_same_origin_paths():
    class FakeRequest:
        def __init__(self, referer):
            self.headers = {"referer": referer} if referer else {}
            self.url = type("U", (), {"netloc": "testserver"})()

    assert main.back_path(FakeRequest("http://testserver/erp/contracts")) == "/erp/contracts"
    assert main.back_path(FakeRequest("http://other/erp/contracts")) == "/inbox"
    assert main.back_path(FakeRequest("//evil.example/x")) == "/inbox"
    assert main.back_path(FakeRequest(None)) == "/inbox"


# --------------------------------------------------------------------------------------------
# Error pages: no Swagger, HTML with the layout for browsers, JSON for API clients and the webhook
# --------------------------------------------------------------------------------------------


def test_no_swagger_redoc_or_openapi_pages(client):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("path, detail", [
    ("/invoice/B-99", "Document B-99 not found."),
    ("/files/B-99.pdf", "Document B-99 not found."),
    ("/no-such-page", None),
])
def test_browser_404_is_an_html_page_with_layout_and_footer(client, path, detail):
    r = client.get(path, headers=BROWSER)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert config.FOOTER_TEXT in html and main.APP_TITLE in html
    assert "Velox ERP (mock, system of record)" in html and "Control gate (new)" in html
    assert "404 · Not Found" in html and 'href="/inbox">Back to the inbox' in html
    if detail:
        assert detail in html


def test_browser_400_and_405_pages_keep_status_and_headers(client):
    r = client.post("/scenario/prod", headers=BROWSER, follow_redirects=False)
    assert r.status_code == 400 and "Unknown scenario" in r.text and config.FOOTER_TEXT in r.text
    r = client.get("/reset", headers=BROWSER)
    assert r.status_code == 405 and r.headers["allow"] == "POST" and "Back to the inbox" in r.text


def test_api_clients_and_the_webhook_get_json_errors(client, tmp_data_dir):
    r = client.get("/invoice/B-99")  # TestClient sends Accept: */*
    assert r.status_code == 404 and r.json() == {"detail": "Document B-99 not found"}
    assert client.get("/no-such-page").json() == {"detail": "Not Found"}
    r = post_webhook(client, channel="fax", headers=BROWSER)  # even when it accepts HTML
    assert r.status_code == 400
    assert r.json() == {"detail": "channel must be ap_mailbox or store_mailbox"}


# --------------------------------------------------------------------------------------------
# Inbox and "Load sample documents"
# --------------------------------------------------------------------------------------------


def test_inbox_empty_state_offers_loading(client):
    html = client.get("/inbox").text
    assert "No documents in the mailboxes" in html
    assert 'action="/documents/load"' in html


@pytest.mark.parametrize("scenario", config.SCENARIOS)
def test_load_lists_documents_split_by_channel(client, session, scenario):
    html = load_documents(client, scenario)
    n = len(world.DOCUMENTS)
    assert f"{n} documents loaded" in html
    assert f"{n} extracted" in html
    assert count_docs(session, scenario) == n
    for channel, mailbox in world.MAILBOX_BY_CHANNEL.items():
        expected = [seed.doc_id_for(scenario, d.no) for d in world.DOCUMENTS if d.channel == channel]
        others = [seed.doc_id_for(scenario, d.no) for d in world.DOCUMENTS if d.channel != channel]
        section = mailbox_section(html, channel)
        assert f'data-count="{len(expected)}"' in section
        assert mailbox in section
        for doc_id in expected:
            assert f'id="doc-{doc_id}"' in section
        for doc_id in others:
            assert f'id="doc-{doc_id}"' not in section
    assert html.count("Extraction pending") == 0
    assert html.count(">Extracted<") == n


def test_asis_inbox_shows_not_registered_with_expected_dates(client):
    html = load_documents(client, "asis")
    assert html.count('<strong class="not-registered">Not registered yet</strong>') == len(world.DOCUMENTS)
    for d in world.DOCUMENTS:
        assert main.fmt_date(sim.registration_date("asis", d.channel, d.received_on)) in html
    ap_days = sim.registration_delay_days("asis", "ap_mailbox")
    store_days = sim.registration_delay_days("asis", "store_mailbox")
    assert f"AP opens ap@ after {main.plural(ap_days, 'business day')}" in html
    assert f"Forwarded by the store after {main.plural(store_days, 'business day')}" in html


def test_tobe_inbox_shows_registration_dates(client):
    html = load_documents(client, "tobe")
    assert "Not registered yet" not in html
    for d in world.DOCUMENTS:
        # received and registered on the same timestamp
        assert html.count(main.fmt_datetime(d.received_on)) >= 2
    assert html.count("Registered on arrival") == len(world.DOCUMENTS)


def test_doc_type_chips_after_extraction(client):
    html = load_documents(client, "tobe")
    n_credit = sum(1 for d in world.DOCUMENTS if d.doc_type == "credit_note")
    assert html.count(">Credit note<") == n_credit
    assert html.count(">Invoice<") == len(world.DOCUMENTS) - n_credit


def test_load_without_cache_or_key_reports_pending_extraction(client, monkeypatch, tmp_cache_dir):
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    html = load_documents(client, "asis")
    n = len(world.DOCUMENTS)
    assert f"Extraction pending for {n} documents: set GEMINI_API_KEY in .env and run make extract" in html
    assert "failed" not in html
    assert html.count(">Extraction pending<") == n
    page = client.get("/invoice/A-05")
    assert page.status_code == 200
    assert "No extraction yet" in page.text and "Extract now" in page.text
    # the running server does not re-read .env: the hint must not say "just click Extract now"
    assert ("add the key to .env and run make extract (it updates the database directly), "
            "or restart the app and click Extract now.") in page.text
    assert "afterwards" not in page.text


def test_load_reports_failed_documents_without_the_key_hint(client, monkeypatch):
    use_gemini(monkeypatch)
    summary = {"extracted": 0, "from_cache": 0, "unavailable": 11, "failed": 1,
               "error": "401 UNAUTHENTICATED: API key not valid."}
    monkeypatch.setattr(extract, "extract_documents", lambda *args, **kwargs: dict(summary))
    html = load_documents(client, "asis")
    assert "1 document failed: 401 UNAUTHENTICATED: API key not valid." in html
    assert "flash-error" in html
    assert "set GEMINI_API_KEY" not in html  # the key is set: the hint would be wrong
    assert "Extraction pending for 11 documents (retry with Extract now" in html


def test_load_message_kind_and_key_hint(monkeypatch):
    base = {"extracted": 12, "from_cache": 12, "unavailable": 0, "failed": 0, "error": None}
    assert main.load_message(12, "asis", base)[0] == "info"
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "EXTRACTOR", "fixture")  # no key needed in fixture mode: no key hint
    kind, text = main.load_message(12, "asis", base | {"extracted": 11, "unavailable": 1})
    assert kind == "warn" and text.endswith("Extraction pending for 1 document.") and "GEMINI" not in text
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    kind, text = main.load_message(12, "asis", base | {"extracted": 0, "unavailable": 12})
    assert kind == "warn" and "set GEMINI_API_KEY in .env and run make extract." in text
    kind, text = main.load_message(12, "asis", base | {"extracted": 0, "failed": 2, "error": "x" * 1000})
    assert kind == "error" and "2 documents failed: " + "x" * 300 + "." in text and "x" * 301 not in text


def test_load_with_a_broken_api_key_calls_the_api_once(client, monkeypatch, tmp_cache_dir):
    """End to end with extract.py (SHARED CONTRACT): after the first ExtractionFailed, no more API calls."""
    use_gemini(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(extract, "call_gemini", failing_gemini("401 UNAUTHENTICATED: API key not valid", calls))
    html = load_documents(client, "asis")
    n = len(world.DOCUMENTS)
    assert len(calls) == 1
    assert "1 document failed: 401 UNAUTHENTICATED: API key not valid." in html
    assert f"Extraction pending for {n - 1} documents" in html and "set GEMINI_API_KEY" not in html


# --------------------------------------------------------------------------------------------
# Invoice page, extraction panel, PDF files
# --------------------------------------------------------------------------------------------


def test_invoice_page_shows_fields_confidence_and_fixture_badge(client):
    load_documents(client, "tobe")
    r = client.get("/invoice/B-03")
    assert r.status_code == 200
    html = r.text
    assert "FIXTURE — not a Gemini output" in html
    assert 'src="/files/B-03.pdf#' in html
    for label in main.extract.FIELD_LABELS.values():
        assert label in html
    spec = world.DOCUMENT_BY_NO[3]
    assert spec.invoice_number in html and spec.printed_supplier_name in html
    assert spec.po_numbers[0] in html
    assert "99%" in html and 'style="width: 99%"' in html
    assert "The control gate runs in phase 2" in html
    assert "Raw extraction JSON" in html
    assert "Re-run gate" in html and "Force re-extract" in html


def test_low_confidence_is_flagged(client, session):
    load_documents(client, "tobe")
    doc = session.scalar(select(InboundDocument).where(InboundDocument.doc_id == "B-03"))
    data = dict(doc.extraction.json)
    data["invoice_number"] = {"value": "INV-2026-0457", "confidence": 0.42}
    doc.extraction.json = data
    session.commit()
    html = client.get("/invoice/B-03").text
    assert "42%" in html and "below 80%" in html and "row-low" in html


def test_force_reextract_htmx_partial_and_no_js_fallback(client):
    load_documents(client, "tobe")
    r = client.post("/invoice/B-03/extract?force=1", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="extraction-panel"' in r.text and "<footer" not in r.text
    assert "ground-truth fixture" in r.text
    r = client.post("/invoice/B-03/extract?force=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/invoice/B-03"


def test_force_reextract_without_api_key_keeps_extraction(client, monkeypatch):
    load_documents(client, "tobe")
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    r = client.post("/invoice/B-07/extract?force=1", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "GEMINI_API_KEY is not set" in r.text
    assert "The current extraction was kept" in r.text
    assert "Shopsys Software Inc." in r.text  # the previous extraction is still shown


def raise_on_extract(exc: Exception):
    def extract_document(*args, **kwargs):
        raise exc
    return extract_document


def test_force_reextract_api_error_keeps_extraction(client, monkeypatch):
    load_documents(client, "tobe")
    use_gemini(monkeypatch)
    monkeypatch.setattr(extract, "extract_document",
                        raise_on_extract(extract.ExtractionFailed("503 UNAVAILABLE: model overloaded")))
    r = client.post("/invoice/B-07/extract?force=1", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Gemini API error: 503 UNAVAILABLE: model overloaded. The current extraction was kept." in r.text
    assert "flash-error" in r.text and "Shopsys Software Inc." in r.text
    r = client.post("/invoice/B-07/extract?force=1")  # no JS: redirect + flash on the invoice page
    assert r.status_code == 200 and r.url.path == "/invoice/B-07"
    assert "Gemini API error: 503 UNAVAILABLE: model overloaded." in r.text


def test_extract_now_api_error_without_previous_extraction(client, monkeypatch, tmp_cache_dir):
    use_gemini(monkeypatch, key="")
    load_documents(client, "asis")  # no cache and no key: nothing extracted
    use_gemini(monkeypatch)
    monkeypatch.setattr(extract, "extract_document",
                        raise_on_extract(extract.ExtractionFailed("Gemini API error on m: 429 RESOURCE_EXHAUSTED")))
    r = client.post("/invoice/A-05/extract", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Gemini API error on m: 429 RESOURCE_EXHAUSTED." in r.text  # prefix not doubled
    assert "Gemini API error: Gemini" not in r.text and "was kept" not in r.text
    assert "No extraction yet" in r.text


def test_extract_unexpected_error_is_reported_not_500(client, monkeypatch):
    load_documents(client, "tobe")
    monkeypatch.setattr(extract, "extract_document", raise_on_extract(RuntimeError("disk full")))
    r = client.post("/invoice/B-07/extract?force=1", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Extraction failed: disk full" in r.text and "Shopsys Software Inc." in r.text


def test_force_reextract_refreshes_the_other_scenarios_copy(client, session, monkeypatch, tmp_cache_dir):
    """GEM-2: a fresh API result for B-03 also updates A-03 (same PDF, same file hash)."""
    use_gemini(monkeypatch)
    monkeypatch.setattr(extract, "call_gemini", fake_gemini("gemini-test-1"))
    load_documents(client, "asis")
    html = load_documents(client, "tobe")  # same PDFs: served from the cache written above
    n = len(world.DOCUMENTS)
    assert f"{n} extracted ({n} from cache)" in html
    monkeypatch.setattr(extract, "call_gemini", fake_gemini("gemini-test-2", invoice_number="INV-NEW-0003"))
    r = client.post("/invoice/B-03/extract?force=1", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "gemini-test-2" in r.text and "INV-NEW-0003" in r.text
    session.expire_all()  # the app wrote through its own sessions
    for doc_id in ("B-03", "A-03"):
        ex = get_doc(session, doc_id).extraction
        assert ex.model == "gemini-test-2", doc_id
        assert ex.json["invoice_number"]["value"] == "INV-NEW-0003", doc_id
    assert get_doc(session, "A-05").extraction.model == "gemini-test-1"  # other documents untouched
    assert "INV-NEW-0003" in client.get("/invoice/A-03").text


def test_unknown_invoice_is_404(client):
    assert client.get("/invoice/B-99").status_code == 404


def test_pdf_is_served_inline(client):
    load_documents(client, "tobe")
    r = client.get("/files/B-01.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.content.startswith(b"%PDF-")


def test_path_traversal_is_refused(client, session):
    load_documents(client, "tobe")
    assert client.get("/files/..%2F..%2Fapp%2Fconfig.pdf").status_code in (400, 404)
    assert client.get("/files/B-99.pdf").status_code == 404
    template = session.scalar(select(InboundDocument).where(InboundDocument.doc_id == "B-01"))
    for i, bad_path in enumerate(["app/config.py", "data/../app/main.py", "../outside.pdf"], start=1):
        session.add(InboundDocument(
            doc_id=f"B-X{i:02d}", scenario="tobe", sample_no=0, channel="ap_mailbox", mailbox=world.AP_MAILBOX,
            received_on=template.received_on, file_path=bad_path, file_hash="0" * 64,
            sender_email="x@example.com", subject="x", registered=True, registered_on=template.received_on))
        session.commit()
        assert client.get(f"/files/B-X{i:02d}.pdf").status_code == 404, bad_path


# --------------------------------------------------------------------------------------------
# Mock ERP pages
# --------------------------------------------------------------------------------------------


def test_vendor_master_ratio_per_scenario(client):
    use_scenario(client, "asis")
    asis = client.get("/erp/vendors").text
    assert "28 accounts / 12 suppliers = 2.3" in asis
    assert "Possible duplicate of" in asis and "Not linked to a party" in asis
    assert 'class="rule"' in asis and "D1 spelling duplicate" in asis
    use_scenario(client, "tobe")
    tobe = client.get("/erp/vendors").text
    assert "16 accounts / 12 suppliers = 1.3" in tobe
    assert "Possible duplicate of" not in tobe and "Not linked to a party" not in tobe
    # the toggle shows the other view without switching the scenario
    assert "28 accounts / 12 suppliers = 2.3" in client.get("/erp/vendors?view=asis").text
    assert client.get("/erp/vendors?view=bogus").status_code == 400


def test_vendor_rows_flags_terms_and_duplicates(client):
    use_scenario(client, "asis")
    html = client.get("/erp/vendors").text
    # D1 duplicate of Nordwind, missing identifiers (D4), terms drift (D3), inactive leftovers (D5)
    assert "V-000117 (VDE, similar name)" in html
    assert "missing VAT ID" in html and "missing IBAN" in html
    assert "terms ≠ agreed" in html and "CT-2025-001" in html
    assert html.count('class="chip chip-neutral">inactive<') == len(seed.D5_INACTIVE)


def test_purchase_orders_page(client):
    use_scenario(client, "asis")
    asis = client.get("/erp/purchase-orders").text
    assert f"({len(seed.purchase_orders_for('asis'))} of {len(world.PURCHASE_ORDERS)})" in asis
    assert "Rule D6" in asis
    use_scenario(client, "tobe")
    tobe = client.get("/erp/purchase-orders").text
    assert "Rule D6" not in tobe
    for po in world.PURCHASE_ORDERS:
        assert f'id="po-{po.po_number}"' in tobe
    assert "Partially received" in tobe  # Lumen PO 4500112, line 2: 20 of 40
    assert "Not confirmed" in tobe  # FitOut milestone 2, PO 4500123
    assert "Fully received" in tobe and "Service confirmed" in tobe


def test_receipt_status_rules():
    assert main.receipt_status("goods", 40, 40, True) == ("Fully received", "ok")
    assert main.receipt_status("goods", 40, 20, True) == ("Partially received", "warn")
    assert main.receipt_status("goods", 40, 0, True) == ("Not received", "bad")
    assert main.receipt_status("service", 1, 1, True) == ("Service confirmed", "ok")
    assert main.receipt_status("service", 1, 0, True) == ("Not confirmed", "bad")


def test_contracts_page_lists_all_contracts(client):
    html = client.get("/erp/contracts").text
    for c in world.CONTRACTS:
        assert c.contract_id in html
        assert world.PARTY_BY_ID[c.party_id].canonical_name in html


def test_pending_invoices_empty_state(client):
    assert "No invoices posted yet — the control gate posts here in phase 2." in client.get("/erp/pending-invoices").text


# --------------------------------------------------------------------------------------------
# Reset, placeholders, assumptions
# --------------------------------------------------------------------------------------------


def test_reset_empties_only_the_active_scenario(client, session):
    load_documents(client, "tobe")
    load_documents(client, "asis")
    r = client.post("/reset", headers={"referer": "http://testserver/inbox"})
    assert r.status_code == 200
    assert "No documents in the mailboxes" in r.text
    assert count_docs(session, "asis") == 0
    assert count_docs(session, "tobe") == len(world.DOCUMENTS)


def test_reset_never_redirects_to_a_deleted_document(client):
    load_documents(client, "asis")
    for referer in ("http://testserver/invoice/A-01", "http://testserver/files/A-01.pdf"):
        r = client.post("/reset", headers={"referer": referer}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/inbox", referer
    r = client.post("/reset", headers={"referer": "http://testserver/erp/vendors"}, follow_redirects=False)
    assert r.headers["location"] == "/erp/vendors"  # other pages still redirect back


def test_placeholder_pages_mention_phase_2(client):
    for path in main.PHASE2_PATHS:
        html = client.get(path).text
        assert main.PHASE2_MESSAGE in html
        assert "phase 2" in html.lower()


def test_assumptions_page_renders_markdown_or_explains_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOCS_DIR", tmp_path)
    assert "does not exist yet" in client.get("/assumptions").text
    (tmp_path / "ASSUMPTIONS.md").write_text("# Assumptions\n\n| rule | value |\n|---|---|\n| D1 | 7 |\n",
                                             encoding="utf-8")
    html = client.get("/assumptions").text
    assert "<table>" in html and "<td>D1</td>" in html


# --------------------------------------------------------------------------------------------
# Intake webhook hook
# --------------------------------------------------------------------------------------------


@pytest.fixture()
def tmp_data_dir(tmp_path, monkeypatch):
    """Keep uploaded PDFs out of the repository's data/ directory."""
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "INVOICES_DIR", tmp_path / "data" / "invoices")
    return tmp_path


def post_webhook(client, content=MINIMAL_PDF, headers=None, **data):
    fields = {"channel": "ap_mailbox", "sender": "billing@example.com"} | data
    return client.post("/intake/webhook", files={"file": ("invoice.pdf", content, "application/pdf")}, data=fields,
                       headers=headers)


def test_webhook_accepts_pdf_and_registers_per_scenario(client, tmp_data_dir):
    r = post_webhook(client, scenario="tobe", subject="Invoice 42")
    assert r.status_code == 201, r.text
    assert r.json() == {"doc_id": "B-W01", "scenario": "tobe", "registered": True, "extracted": False}
    assert post_webhook(client, scenario="tobe").json()["doc_id"] == "B-W02"
    use_scenario(client, "asis")
    r = post_webhook(client, channel="store_mailbox")  # scenario from the cookie
    assert r.json() == {"doc_id": "A-W01", "scenario": "asis", "registered": False, "extracted": False}
    assert list((tmp_data_dir / "data" / "invoices" / "inbound").glob("*.pdf"))
    pdf = client.get("/files/B-W01.pdf")
    assert pdf.status_code == 200 and pdf.content == MINIMAL_PDF
    use_scenario(client, "tobe")
    assert "Invoice 42" in client.get("/inbox").text


def test_webhook_rejects_invalid_input(client, tmp_data_dir):
    assert post_webhook(client, content=b"hello, not a pdf").status_code == 400
    assert post_webhook(client, channel="fax").status_code == 400
    assert post_webhook(client, sender="not-an-email").status_code == 400
    assert post_webhook(client, scenario="prod").status_code == 400
    too_big = MINIMAL_PDF + b"0" * (main.MAX_UPLOAD_BYTES + 1)
    r = post_webhook(client, content=too_big)  # within the multipart allowance: the endpoint's own check
    assert r.status_code == 413 and r.json() == {"detail": "PDF larger than 10 MB"}


def test_webhook_checks_content_length_before_parsing(client, tmp_data_dir):
    multipart = {"content-type": "multipart/form-data; boundary=x"}
    r = client.post("/intake/webhook", content=iter([b"--x--\r\n"]), headers=multipart)  # chunked, no length
    assert r.status_code == 411 and r.json() == {"detail": "Content-Length header required"}
    r = client.post("/intake/webhook", content=b"--x--\r\n", headers=multipart | {"content-length": "abc"})
    assert r.status_code == 411
    limit = main.MAX_UPLOAD_BYTES + main.MULTIPART_OVERHEAD_BYTES
    # a declared length over the limit is refused before the body is read
    r = client.post("/intake/webhook", content=b"--x--\r\n", headers=multipart | {"content-length": str(limit + 1)})
    assert r.status_code == 413 and "at most 10 MB" in r.json()["detail"]
    r = post_webhook(client, content=MINIMAL_PDF + b"0" * limit)
    assert r.status_code == 413 and r.json()["detail"] != "PDF larger than 10 MB"  # the middleware, not the endpoint
    assert not (tmp_data_dir / "data" / "invoices" / "inbound").exists()
    assert post_webhook(client, scenario="tobe").status_code == 201  # normal uploads still pass


def test_concurrent_webhook_uploads_get_distinct_ids(client, session, tmp_data_dir, monkeypatch):
    allocate = main.next_webhook_doc_id

    def slow_allocate(db_session, scenario):
        doc_id = allocate(db_session, scenario)
        time.sleep(0.1)  # widen the gap between id allocation and insert
        return doc_id

    monkeypatch.setattr(main, "next_webhook_doc_id", slow_allocate)
    uploads = [MINIMAL_PDF + f"% upload {i}\n".encode() for i in range(3)]
    with ThreadPoolExecutor(max_workers=len(uploads)) as pool:
        responses = list(pool.map(lambda content: post_webhook(client, content=content, scenario="tobe"), uploads))
    assert [r.status_code for r in responses] == [201] * len(uploads)
    assert sorted(r.json()["doc_id"] for r in responses) == ["B-W01", "B-W02", "B-W03"]
    assert count_docs(session, "tobe") == len(uploads)
