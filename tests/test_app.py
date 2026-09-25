"""Web UI and intake tests (TestClient). conftest.py provides a temp DB, EXTRACTOR=fixture and no API key.

Phase-2 pages run the real gate / metrics / drafts modules on the fixture extraction; the expected
outcomes come from tests/golden.yaml.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config, drafts, extract, gate, main, metrics, seed, sim, world
from app.models import GateDecision, InboundDocument, PendingVendorInvoice, Run

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


def count_rows(session, model, scenario: str) -> int:
    return session.scalar(select(func.count()).select_from(model).where(model.scenario == scenario))


def run_scenario(client: TestClient, scenario: str):
    """Header "Run scenario" (loads the sample documents first when the inbox is empty); lands on /run."""
    use_scenario(client, scenario)
    r = client.post("/run")
    assert r.status_code == 200 and r.url.path == "/run", r.text[:500]
    return r


def page_text(html: str) -> str:
    """Visible text of a page: tags removed, entities decoded (for strings split by markup)."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", html))


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


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
    assert 'action="/run"' in html and ">Run scenario</button>" in html
    assert "disabled>Run scenario" not in html
    assert 'action="/reset"' in html and "confirm(" in html
    assert "phase 2" not in html.lower()  # no "phase 2" markers left in the navigation


def test_scenario_switch_sets_cookie_and_redirects_back(client):
    r = client.post("/scenario/tobe", headers={"referer": "http://testserver/erp/vendors?view=asis"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/erp/vendors"
    assert "scenario=tobe" in r.headers["set-cookie"]
    assert client.cookies.get("scenario") == "tobe"
    assert 'switch-tobe active' in client.get("/inbox").text


def form_html(html: str, action: str) -> str:
    """HTML of the first form posting to `action`."""
    start = html.index(f'action="{action}"')
    return html[start:html.index("</form>", start)]


def test_header_actions_act_on_the_scenario_the_page_shows(client, session):
    """A second tab that switched the cookie must not make this page's Run / Reset / Load hit the other scenario."""
    use_scenario(client, "asis")
    html = client.get("/inbox").text  # rendered for as-is (header forms and the empty-state Load form)
    for action in ("/documents/load", "/run", "/reset"):
        assert '<input type="hidden" name="scenario" value="asis">' in form_html(html, action), action
    assert html.count('<input type="hidden" name="scenario" value="asis">') == 4
    use_scenario(client, "tobe")  # the other tab
    r = client.post("/run", data={"scenario": "asis"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/run"
    assert "scenario=asis" in r.headers["set-cookie"]  # the step log opens on the scenario that ran
    assert count_rows(session, GateDecision, "asis") == len(world.DOCUMENTS)
    assert count_rows(session, GateDecision, "tobe") == 0
    assert client.cookies.get("scenario") == "asis"
    use_scenario(client, "tobe")
    r = client.post("/reset", data={"scenario": "asis"}, follow_redirects=False)
    assert r.status_code == 303 and client.cookies.get("scenario") == "asis"
    session.expire_all()
    assert count_rows(session, GateDecision, "asis") == 0 and count_docs(session, "tobe") == 0
    use_scenario(client, "asis")
    r = client.post("/documents/load", data={"scenario": "tobe"}, follow_redirects=False)
    assert r.status_code == 303 and client.cookies.get("scenario") == "tobe"
    assert count_docs(session, "tobe") == len(world.DOCUMENTS)
    for action in ("/run", "/reset", "/documents/load"):
        assert client.post(action, data={"scenario": "prod"}, follow_redirects=False).status_code == 400, action


def test_run_buttons_of_empty_states_carry_the_scenario(client):
    use_scenario(client, "tobe")
    for path in ("/run", "/gate/exceptions", "/gate/kpis", "/erp/pending-invoices"):
        html = client.get(path).text
        body = html[html.index('<main class="content">'):]  # skip the header forms
        assert '<input type="hidden" name="scenario" value="tobe">' in form_html(body, "/run"), path


def test_load_extracts_inside_the_gate_lock(client, monkeypatch):
    held = []

    def extract_documents(session, docs, allow_api=False):
        held.append(main._gate_lock.locked())
        return {"extracted": 0, "from_cache": 0, "unavailable": len(docs), "failed": 0}
    monkeypatch.setattr(extract, "extract_documents", extract_documents)
    load_documents(client, "asis")
    assert held == [True]  # a Run clicked meanwhile waits for the extraction


def test_load_extraction_crash_is_a_short_message(client, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(extract, "extract_documents", boom)
    html = load_documents(client, "asis")
    assert f"{len(world.DOCUMENTS)} documents loaded, but the extraction failed; see the server log." in html
    assert "database is locked" not in html
    assert "extraction error: RuntimeError('database is locked')" in capsys.readouterr().out


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
    assert "Not processed by the gate yet" in html and "Run gate on this document" in html
    assert "Raw extraction JSON" in html
    assert "Force re-extract" in html


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
    html = client.get("/erp/pending-invoices").text
    assert "No invoices posted yet in A — As-is." in html
    assert "The control gate posts here when the scenario runs." in html and 'action="/run"' in html


# --------------------------------------------------------------------------------------------
# Reset, assumptions
# --------------------------------------------------------------------------------------------


def test_reset_restores_only_the_active_scenario(client, session):
    run_scenario(client, "tobe")
    run_scenario(client, "asis")
    r = client.post("/reset", headers={"referer": "http://testserver/inbox"})
    assert r.status_code == 200 and r.url.path == "/inbox"
    n = len(world.DOCUMENTS)
    assert f"{n} sample documents reloaded and not processed yet; {n} extracted." in r.text
    assert "Not processed yet" in r.text and "outcome-chip" not in r.text
    session.expire_all()
    assert count_docs(session, "asis") == n
    assert count_rows(session, GateDecision, "asis") == 0 and count_rows(session, Run, "asis") == 0
    assert count_rows(session, PendingVendorInvoice, "asis") == 0
    assert all(d.extraction is not None and not d.registered
               for d in session.scalars(select(InboundDocument).where(InboundDocument.scenario == "asis")))
    assert count_rows(session, GateDecision, "tobe") == n  # the other scenario is untouched


def test_reset_never_redirects_to_a_deleted_document(client):
    load_documents(client, "asis")
    for referer in ("http://testserver/invoice/A-01", "http://testserver/files/A-01.pdf"):
        r = client.post("/reset", headers={"referer": referer}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/inbox", referer
    r = client.post("/reset", headers={"referer": "http://testserver/erp/vendors"}, follow_redirects=False)
    assert r.headers["location"] == "/erp/vendors"  # other pages still redirect back


def test_assumptions_page_renders_markdown_or_explains_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOCS_DIR", tmp_path)
    assert "does not exist yet" in client.get("/assumptions").text
    (tmp_path / "ASSUMPTIONS.md").write_text("# Assumptions\n\n| rule | value |\n|---|---|\n| D1 | 7 |\n",
                                             encoding="utf-8")
    html = client.get("/assumptions").text
    assert "<table>" in html and "<td>D1</td>" in html


# --------------------------------------------------------------------------------------------
# Intake webhook (C6): PDF, UBL e-invoice, email body only, forwarded comment, idempotency, gate run
# --------------------------------------------------------------------------------------------


@pytest.fixture()
def tmp_data_dir(tmp_path, monkeypatch):
    """Keep uploaded files out of the repository's data/ directory (project root moved to a temp folder)."""
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "INVOICES_DIR", tmp_path / "data" / "invoices")
    monkeypatch.setattr(config, "INBOUND_DIR", tmp_path / "data" / "invoices" / "inbound")
    return tmp_path


@pytest.fixture()
def bucket_dir(tmp_path, monkeypatch):
    """INBOUND_DIR outside the project (as a mounted Cloud Storage bucket on Cloud Run); the project stays put."""
    inbound = tmp_path / "bucket" / "inbound"
    monkeypatch.setattr(config, "INBOUND_DIR", inbound)
    return inbound


def post_webhook(client, content=MINIMAL_PDF, headers=None, filename="invoice.pdf", **data):
    fields = {"channel": "ap_mailbox", "sender": "billing@example.com"} | data
    files = None if content is None else {"file": (filename, content, "application/octet-stream")}
    return client.post("/intake/webhook", files=files, data=fields, headers=headers)


def ubl_invoice(no: int = 11) -> bytes:
    """A Peppol UBL e-invoice of test set v2 (document 11: Metro Media, PO 4500126)."""
    ubl = pytest.importorskip("app.ubl")
    return ubl.render_ubl(world.document_for("v2", no))


def webhook_doc(session, doc_id: str) -> InboundDocument:
    session.expire_all()  # the app wrote through its own sessions
    return get_doc(session, doc_id)


def test_webhook_accepts_pdf_registers_per_scenario_and_runs_the_gate(client, session, tmp_data_dir):
    r = post_webhook(client, scenario="tobe", subject="Invoice 42")
    assert r.status_code == 201, r.text
    assert r.json() == {"doc_id": "B-W01", "scenario": "tobe", "registered": True, "extracted": False,
                        "outcome": "human_review", "exception_type": "human_review", "owner_name": "Marco Ruiz"}
    assert post_webhook(client, scenario="tobe").json()["doc_id"] == "B-W02"
    use_scenario(client, "asis")
    r = post_webhook(client, channel="store_mailbox")  # scenario from the cookie
    assert r.json() == {"doc_id": "A-W01", "scenario": "asis", "registered": False, "extracted": False,
                        "outcome": "exception", "exception_type": "email_loop", "owner_name": None}
    assert list((tmp_data_dir / "data" / "invoices" / "inbound").glob("*.pdf"))
    pdf = client.get("/files/B-W01.pdf")
    assert pdf.status_code == 200 and pdf.content == MINIMAL_PDF
    doc = webhook_doc(session, "B-W01")
    assert (doc.dataset, doc.content_type, doc.sample_no, doc.email_body) == ("live", "pdf", 0, None)
    assert count_rows(session, GateDecision, "tobe") == 2  # the gate ran on each upload
    use_scenario(client, "tobe")
    html = client.get("/inbox").text
    assert "Invoice 42" in html and "Live intake (webhook)" in html
    assert ">Human review<" in mailbox_section(html, "ap_mailbox")


def test_webhook_scenario_defaults_to_tobe_without_a_cookie(client, bucket_dir):
    assert client.cookies.get("scenario") is None
    assert post_webhook(client).json()["scenario"] == "tobe"


def test_webhook_rejects_invalid_input(client, session, tmp_data_dir):
    assert post_webhook(client, content=b"hello, not a pdf").status_code == 400
    r = post_webhook(client, content=b"<?xml version='1.0'?><note>not an invoice</note>", filename="note.xml")
    assert r.status_code == 400 and r.json() == {"detail": "file is not a PDF or a UBL e-invoice (XML)"}
    r = post_webhook(client, content=None)  # neither a file nor an email body
    assert r.status_code == 400 and r.json() == {"detail": "email_body is required when no file is attached"}
    assert post_webhook(client, content=None, email_body="   ").status_code == 400  # a blank body is no body
    assert post_webhook(client, channel="fax").status_code == 400
    assert post_webhook(client, sender="not-an-email").status_code == 400
    assert post_webhook(client, scenario="prod").status_code == 400
    too_big = MINIMAL_PDF + b"0" * (main.MAX_UPLOAD_BYTES + 1)
    r = post_webhook(client, content=too_big)  # within the multipart allowance: the endpoint's own check
    assert r.status_code == 413 and r.json() == {"detail": "file larger than 10 MB"}
    assert count_docs(session, "tobe") == count_docs(session, "asis") == 0


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
    assert r.status_code == 413 and r.json()["detail"] != "file larger than 10 MB"  # the middleware, not the endpoint
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


def test_webhook_ubl_e_invoice_is_parsed_without_a_model_and_posted(client, session, bucket_dir):
    ubl = pytest.importorskip("app.ubl")
    content = ubl_invoice(11)
    r = post_webhook(client, content=content, filename="MM-2026-248.xml", scenario="tobe",
                     sender="einvoice@metromedia.de")
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["extracted"], body["outcome"], body["exception_type"]) == (True, "posted", None)
    doc = webhook_doc(session, "B-W01")
    assert (doc.content_type, doc.source_name, doc.extraction.model) == ("ubl_xml", "MM-2026-248.xml", ubl.UBL_MODEL)
    assert list(bucket_dir.glob("*.xml"))
    xml = client.get("/files/B-W01.xml")
    assert xml.status_code == 200 and xml.headers["content-type"].startswith("application/xml")
    assert xml.content == content
    html = client.get("/invoice/B-W01").text
    assert "<iframe" not in html and 'class="doc-text xml-text"' in html
    assert "&lt;cbc:ID&gt;MM-2026-248&lt;/cbc:ID&gt;" in html  # escaped, never rendered as markup
    assert "UBL e-invoice — parsed, no model call" in html and ">UBL e-invoice<" in gate_panel(html)


def test_webhook_email_body_only_goes_to_human_review(client, session, bucket_dir):
    text = "Guten Tag, anbei unsere Rechnung 2026/140 über 58,31 EUR. Mit freundlichen Grüßen, Kaffee & Co OHG"
    r = post_webhook(client, content=None, email_body=text, channel="store_mailbox", scenario="tobe",
                     sender="info@kaffee-und-co.de", subject="Rechnung 2026/140")
    assert r.status_code == 201, r.text
    assert r.json() == {"doc_id": "B-W01", "scenario": "tobe", "registered": True, "extracted": False,
                        "outcome": "human_review", "exception_type": "human_review", "owner_name": "Marco Ruiz"}
    doc = webhook_doc(session, "B-W01")
    assert (doc.content_type, doc.email_body, doc.source_name) == ("email_body", text, None)
    stored = list(bucket_dir.glob("*.txt"))
    assert len(stored) == 1 and stored[0].read_text(encoding="utf-8") == text
    decision = session.scalars(select(GateDecision).where(GateDecision.doc_id == "B-W01")).one()
    assert "the invoice is only in the email body" in decision.reason
    html = client.get("/invoice/B-W01").text
    assert "<iframe" not in html and 'class="doc-text email-text"' in html
    assert html_lib.escape(text, quote=False) in html
    assert "Extract now" not in html and "there is no document to extract" in html
    assert ">email body only<" in gate_panel(html)
    use_scenario(client, "tobe")
    assert ">Nothing to extract<" in client.get("/inbox").text


def test_webhook_keeps_the_forwarding_comment_of_an_attachment(client, session, bucket_dir):
    comment = "Bonjour, facture reçue au magasin la semaine dernière. Merci de la régler. Luc"
    r = post_webhook(client, email_body=comment, sender="luc.bernard@velox.com", subject="TR: Facture QP-26-1107",
                     scenario="tobe")
    assert r.status_code == 201
    doc = webhook_doc(session, "B-W01")
    assert (doc.content_type, doc.email_body) == ("pdf", comment)
    html = client.get("/invoice/B-W01").text
    assert "<iframe" in html and "Email text" in html and html_lib.escape(comment, quote=False) in html


def test_webhook_message_id_makes_a_repeated_delivery_a_no_op(client, session, bucket_dir):
    first = post_webhook(client, scenario="tobe", message_id="<msg-1@example.com>")
    assert first.status_code == 201 and first.json()["doc_id"] == "B-W01"
    again = post_webhook(client, scenario="tobe", message_id="<msg-1@example.com>")
    assert again.status_code == 200
    assert again.json() == {**first.json(), "existing": True}
    # a second attachment of the same email, and the same email for the other scenario, are new documents
    assert post_webhook(client, scenario="tobe", message_id="<msg-1@example.com>",
                        filename="annex.pdf").json()["doc_id"] == "B-W02"
    assert post_webhook(client, scenario="asis", message_id="<msg-1@example.com>").status_code == 201
    body = {"content": None, "email_body": "Invoice 77 for 120.00 EUR", "scenario": "tobe",
            "message_id": "<msg-2@example.com>"}
    assert post_webhook(client, **body).status_code == 201
    repeat = post_webhook(client, **body)
    assert repeat.status_code == 200 and repeat.json()["doc_id"] == "B-W03" and repeat.json()["existing"] is True
    assert post_webhook(client, scenario="tobe").json()["doc_id"] == "B-W04"  # no message id: always new
    assert count_docs(session, "tobe") == 4 and count_docs(session, "asis") == 1
    assert count_rows(session, GateDecision, "tobe") == 4  # the gate ran once per document


def test_webhook_document_shows_in_the_cockpit_after_a_run(client, session, bucket_dir):
    run_scenario(client, "tobe")
    assert 'id="bucket-human_review"' not in client.get("/gate/exceptions").text
    r = post_webhook(client, scenario="tobe", subject="Scan from the store")
    assert r.json()["outcome"] == "human_review"
    html = client.get("/gate/exceptions").text
    assert "5 blocking exceptions" in page_text(html)
    assert 'href="/invoice/B-W01"' in section(html, "bucket-human_review")
    assert count_rows(session, GateDecision, "tobe") == len(world.DOCUMENTS) + 1


def test_files_of_an_inbound_dir_outside_the_project_are_served(client, session, bucket_dir):
    assert not bucket_dir.resolve().is_relative_to(config.BASE_DIR.resolve())
    post_webhook(client, scenario="tobe")
    doc = webhook_doc(session, "B-W01")
    stored = Path(doc.file_path)
    assert stored.is_absolute() and stored.parent == bucket_dir.resolve()
    for url in ("/files/B-W01.pdf", "/files/B-W01"):
        r = client.get(url)
        assert r.status_code == 200 and r.content == MINIMAL_PDF, url
    assert client.get("/files/B-W01.xml").status_code == 404  # the extension must be the file's
    doc.file_path = (bucket_dir.parent / "elsewhere.pdf").as_posix()  # outside INBOUND_DIR and DATA_DIR
    (bucket_dir.parent / "elsewhere.pdf").write_bytes(MINIMAL_PDF)
    session.commit()
    assert client.get("/files/B-W01.pdf").status_code == 404


def test_basic_auth_is_off_without_a_password_and_guards_everything_with_one(client, session, bucket_dir,
                                                                             monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    assert client.get("/inbox").status_code == 200
    monkeypatch.setattr(config, "APP_USERNAME", "velox")
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret")
    r = client.get("/inbox")
    assert r.status_code == 401 and 'realm="Velox"' in r.headers["www-authenticate"]
    assert client.get("/static/app.css").status_code == 401
    assert client.get("/inbox", auth=("velox", "wrong")).status_code == 401
    assert client.get("/inbox", auth=("velox", "s3cret")).status_code == 200
    assert client.get("/health").status_code == 200  # uptime checks stay open
    assert post_webhook(client, scenario="tobe").status_code == 401 and count_docs(session, "tobe") == 0
    authorized = {"authorization": _basic("velox", "s3cret")}
    assert post_webhook(client, scenario="tobe", headers=authorized).status_code == 201
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    assert client.get("/inbox").status_code == 200


def _basic(user: str, password: str) -> str:
    import base64

    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --------------------------------------------------------------------------------------------
# Datasets: case documents (14) or test set v2 (26)
# --------------------------------------------------------------------------------------------


def test_header_offers_both_datasets(client):
    html = form_html(client.get("/inbox").text, "/documents/load")
    for key, label in main.DATASET_CHOICES.items():
        assert f'name="dataset" value="{key}"' in html and label in html
    assert main.DATASET_CHOICES == {"v1": "Case documents (14)", "v2": "Test set v2 (26)"}


def test_load_test_set_v2_and_reset_keeps_it(client, session):
    use_scenario(client, "tobe")
    r = client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"})
    assert r.status_code == 200 and r.url.path == "/inbox"
    n = len(world.documents_for("v2"))
    assert n == 26
    assert f"{n} documents loaded into the mailboxes of B — To-be (test set v2)" in r.text
    assert "1 email with the invoice only in the body" in r.text and "Extraction pending" not in r.text
    assert "Test set v2 (26)" in r.text and 'id="doc-B2-01"' in r.text and 'id="doc-B2-26"' in r.text
    assert ">UBL e-invoice (XML)<" in r.text and ">Email body, no attachment<" in r.text
    assert ">Not an invoice<" in r.text  # document 3, the statement
    assert count_docs(session, "tobe") == n
    r = client.post("/reset", data={"scenario": "tobe"})
    assert "Documents: Test set v2 (26)." in r.text
    session.expire_all()
    assert count_docs(session, "tobe") == n and seed.loaded_dataset(session, "tobe") == "v2"
    r = client.post("/documents/load", data={"scenario": "tobe", "dataset": "v1"})
    assert count_docs(session, "tobe") == len(world.DOCUMENTS) and "(case documents)" in r.text
    assert client.post("/documents/load", data={"scenario": "tobe", "dataset": "v9"},
                       follow_redirects=False).status_code == 400


def test_invoice_pages_of_the_v2_formats(client):
    client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"})
    ubl = client.get("/invoice/B2-11").text
    assert "<iframe" not in ubl and 'class="doc-text xml-text"' in ubl and "&lt;cbc:ID&gt;MM-2026-248" in ubl
    assert "Peppol BIS Billing 3.0" in ubl and 'href="/files/B2-11.xml"' in ubl
    email = client.get("/invoice/B2-14").text
    assert "<iframe" not in email and 'class="doc-text email-text"' in email and "Rechnung 2026/140" in email
    assert "Extract now" not in email
    forwarded = client.get("/invoice/B2-08").text
    assert 'src="/files/B2-08.pdf#' in forwarded and "Email text" in forwarded and "Bonjour" in forwarded
    r = client.get("/files/B2-11.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    r = client.get("/files/B2-14.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")


def test_compare_warns_when_the_scenarios_hold_different_datasets(client):
    client.post("/documents/load", data={"scenario": "asis", "dataset": "v1"})
    client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"})
    html = squash(page_text(client.get("/gate/compare").text))
    assert ("The two scenarios hold different sample documents (A — As-is the case documents, B — To-be the test "
            "set v2)") in html
    client.post("/documents/load", data={"scenario": "asis", "dataset": "v2"})
    html = squash(page_text(client.get("/gate/compare").text))
    assert "hold different sample documents" not in html and "same 26 documents (test set v2)" in html


def test_run_after_a_scenario_exports_when_enabled_and_never_fails_on_export(client, monkeypatch, capsys):
    export_bq = pytest.importorskip("app.export_bq")
    calls = []
    monkeypatch.setattr(export_bq, "export_all", lambda db_session: calls.append(1) or {"gate_decision": 14})
    monkeypatch.setattr(config, "BQ_EXPORT", False)
    run_scenario(client, "tobe")
    assert calls == []
    monkeypatch.setattr(config, "BQ_EXPORT", True)
    run_scenario(client, "tobe")
    assert calls == [1] and "[export] after the tobe run: 14 rows in 1 tables" in capsys.readouterr().out

    def broken(db_session):
        raise RuntimeError("BigQuery unreachable")
    monkeypatch.setattr(export_bq, "export_all", broken)
    r = run_scenario(client, "tobe")  # the run still succeeds
    assert "documents processed" in r.text
    assert "[export] after the tobe run FAILED: RuntimeError('BigQuery unreachable')" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# Phase 2: run, inbox outcomes, invoice gate panel, cockpit, KPIs, compare, drafts, ?scenario=
# --------------------------------------------------------------------------------------------

GATE_PAGES = NAV_PATHS + ["/run"]


def gate_panel(html: str) -> str:
    """HTML of the gate panel of an invoice page."""
    start = html.index('id="gate-panel"')
    return html[start:html.index('id="extraction-panel"', start)]


def section(html: str, element_id: str) -> str:
    """HTML from the element with this id to the next <section (the cockpit buckets)."""
    start = html.index(f'id="{element_id}"')
    end = html.find("<section", start)
    return html[start:end if end != -1 else len(html)]


@pytest.mark.parametrize("scenario", config.SCENARIOS)
def test_every_page_returns_200_before_and_after_a_run(client, scenario):
    letter = seed.SCENARIO_LETTER[scenario]
    use_scenario(client, scenario)
    for path in GATE_PAGES:  # empty inbox, no run
        assert client.get(path).status_code == 200, path
    load_documents(client, scenario)
    doc_pages = [f"/invoice/{letter}-{d.no:02d}" for d in world.DOCUMENTS]
    for path in GATE_PAGES + doc_pages:  # documents loaded, not processed
        assert client.get(path).status_code == 200, path
    run_scenario(client, scenario)
    for path in GATE_PAGES + doc_pages:
        r = client.get(path)
        assert r.status_code == 200, path
        assert config.FOOTER_TEXT in r.text, path


def test_run_page_shows_the_step_log_and_every_document(client, session):
    r = run_scenario(client, "tobe")
    html, text = r.text, page_text(r.text)
    n = len(world.DOCUMENTS)
    assert f"{n} documents processed, 10 touchless, 4 exceptions." in html  # flash
    run = session.scalar(select(Run).where(Run.scenario == "tobe"))
    log = run.summary_json["log"]
    step = re.compile(r"^\[doc \d{2}\] step=(%s) result=\w+" % "|".join(gate.STEP_NAMES))
    outcome = re.compile(r"^\[doc \d{2}\] outcome=\w+")
    assert all(step.match(line) or outcome.match(line) or line.startswith("[run] ") for line in log)
    for line in log:
        assert line in text, line
    # the brief's example line (section 13)
    assert "[doc 07] step=resolve_vendor result=ok party=Shopsys account=V-000105 method=vat_id" in text
    assert f"[run] scenario=tobe documents={n} touchless=10 exceptions=4" in text
    assert html.count('class="log-line') == len(log)
    assert "animation-delay:" in html and 'href="#run-log"' in html and "Show all" in html
    assert html.count('id="row-B-') == n
    for link in ('href="/gate/exceptions"', 'href="/gate/kpis"', 'href="/gate/compare"'):
        assert link in html


def test_run_page_before_a_run_offers_the_button(client):
    html = client.get("/run").text
    assert "has not been run yet" in html and 'action="/run"' in html


def test_run_loads_the_sample_documents_when_the_inbox_is_empty(client, session):
    r = run_scenario(client, "asis")
    n = len(world.DOCUMENTS)
    assert f"{n} sample documents loaded first." in r.text
    assert count_docs(session, "asis") == n
    assert count_rows(session, GateDecision, "asis") == n


def test_run_page_warns_about_documents_without_extraction(client, monkeypatch, tmp_cache_dir):
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    load_documents(client, "tobe")  # no cache and no key: nothing extracted
    html = run_scenario(client, "tobe").text
    assert f"No extraction for {len(world.DOCUMENTS)} documents" in html
    assert "B-01" in html and "flash-warn" in html


def test_run_failure_is_reported_not_500(client, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("rule table broken")
    monkeypatch.setattr(gate, "run_scenario", boom)
    use_scenario(client, "tobe")
    r = client.post("/run")
    assert r.status_code == 200 and r.url.path == "/inbox"
    assert "The gate run of B — To-be failed; see the server log." in r.text
    assert "rule table broken" not in r.text  # the raw error goes to the log, not the page
    assert "[run] scenario=tobe FAILED: RuntimeError('rule table broken')" in capsys.readouterr().out


def test_log_line_split_and_tone():
    line = main.log_line('[doc 12] step=commitment_match result=exception owner="Tim Koch"')
    assert (line["prefix"], line["token"], line["tone"], line["kind"]) == (
        "[doc 12]", "result=exception", "bad", "step")
    assert line["before"] + line["token"] + line["after"] == 'step=commitment_match result=exception owner="Tim Koch"'
    assert main.log_line("[doc 01] outcome=posted days=0")["kind"] == "outcome"
    assert main.log_line("[run] scenario=tobe documents=14")["kind"] == "run"


def test_inbox_shows_outcome_chips_and_owners_after_a_run(client):
    html = load_documents(client, "tobe")
    assert "Not processed yet" in html and "outcome-chip" not in html
    run_scenario(client, "tobe")
    html = client.get("/inbox").text
    assert html.count("outcome-chip") == len(world.DOCUMENTS)
    assert 'data-count="11"' in mailbox_section(html, "ap_mailbox")
    assert 'data-count="3"' in mailbox_section(html, "store_mailbox")
    for label in ("Posted", "Blocked duplicate", "Credit applied", "Exception: Price or quantity outside tolerance",
                  "Exception: PO exists, no receipt or service confirmation",
                  "Exception: Billed to the wrong Velox entity"):
        assert f">{label}<" in html, label
    assert "<strong>Sofia Brandt</strong>" in html and "<strong>Tim Koch</strong>, then Sofia Brandt" in html
    assert "14 of 14 documents processed" in html


def test_asis_inbox_after_a_run_shows_registration_and_the_email_loop(client):
    run_scenario(client, "asis")
    html = client.get("/inbox").text
    assert "Not registered yet" not in html
    assert html.count(">Email loop — untracked<") == 9
    assert "Forwarded by the store after 7 business days" in html
    assert "AP opened ap@ after 1 business day" in html
    for d in world.DOCUMENTS:
        assert main.fmt_datetime(sim.registration_date("asis", d.channel, d.received_on)) in html


def test_invoice_b06_price_mismatch_routed_to_the_buyer(client):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-06").text)
    text = squash(page_text(panel))
    assert "Exception: Price or quantity outside tolerance" in panel
    assert "<strong>Sofia Brandt</strong>" in panel and "Buyer" in panel
    assert "2 business days · due Tue 2026-10-06" in text  # received Fri 2 Oct + 2 business days
    assert "Agree correction with supplier or approve variance" in panel
    assert panel.count('class="trace-step') == len(gate.STEP_NAMES)
    for label in ("Register", "Resolve vendor", "Legal entity check", "Duplicate check", "Commitment match", "Post"):
        assert f'<span class="trace-name">{label}</span>' in panel
    assert "Line checks" in panel and "price" in panel
    assert "Simulated cycle: 2 business days" in text and "Owner resolves within the SLA" in text
    assert "Draft message to owner" in panel and "Re-run gate" in panel


def test_invoice_b12_routed_to_the_receiver_then_the_buyer(client):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-12").text)
    assert "<strong>Tim Koch</strong>" in panel
    assert "then <strong>Sofia Brandt</strong>" in panel
    assert "due <strong>Wed 2026-10-07</strong>" in squash(panel)


def test_invoice_b02_blocked_as_a_duplicate_of_b01(client):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-02").text)
    assert ">Blocked duplicate<" in panel
    assert "Duplicate of" in panel and '<a href="/invoice/B-01">B-01</a>' in panel
    assert "<strong>Marco Ruiz</strong>" in panel and "blocked on arrival" in panel
    assert "Reply to supplier with status" in panel
    assert "Draft message to owner" not in panel  # touchless: the supplier gets an automatic status reply
    r = client.post("/invoice/B-02/draft", headers={"HX-Request": "true"})
    assert "Handled automatically: the supplier gets a status reply." in r.text


def test_invoice_b01_posted_on_master_terms_with_an_info_flag(client, session):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-01").text)
    text = squash(page_text(panel))
    assert ">Posted<" in panel and "contract match" in panel
    pvi = session.scalar(select(PendingVendorInvoice).where(PendingVendorInvoice.doc_id == "B-01"))
    assert f'href="/erp/pending-invoices#{pvi.invoice_id}"' in panel
    assert "30 days from the master" in text and "invoice 14 days · agreed 30 days" in text
    assert f"<dt>Due date</dt><dd>{main.fmt_date(pvi.due_date)}</dd>" in panel
    assert "Contract CT-2025-001" in text
    assert "Info tasks (non-blocking)" in panel and "Marco Ruiz" in panel
    assert "Draft message to owner" not in panel


def test_invoice_a02_duplicate_posting_and_asis_badges(client):
    run_scenario(client, "asis")
    panel = gate_panel(client.get("/invoice/A-02").text)
    assert ">Email loop — untracked<" in panel and ">duplicate posting<" in panel
    assert "Untracked manual follow-up by email" in panel
    assert "V-000117" in panel and "from the invoice" in panel
    assert "Draft message to owner" not in panel
    # the quick-fix tool keys the document once AP opens ap@: no claim that AP keyed it by hand
    assert "AP opens ap@ and the quick-fix tool keys it" in panel and "AP opens and keys" not in panel
    assert ">wrong entity<" in gate_panel(client.get("/invoice/A-08").text)
    assert ">unapplied credit<" in gate_panel(client.get("/invoice/A-04").text)


def test_cockpit_tobe_groups_by_type_with_owner_sla_and_age(client):
    run_scenario(client, "tobe")
    html = client.get("/gate/exceptions").text
    assert "4 blocking exceptions" in page_text(html)
    for key, docs in (("po_no_receipt", ["B-05"]), ("price_qty_mismatch", ["B-06", "B-12"]),
                      ("wrong_legal_entity", ["B-08"])):
        bucket = section(html, f"bucket-{key}")
        for doc_id in docs:
            assert f'href="/invoice/{doc_id}"' in bucket, (key, doc_id)
        assert f'<span class="count">{len(docs)}</span>' in bucket
    bucket = section(html, "bucket-price_qty_mismatch")
    as_of = sim.cockpit_as_of(world.DOCUMENT_BY_NO[n].received_on for n in (5, 6, 8, 12))
    assert as_of == datetime(2026, 10, 5, 17, 0)  # close of the day B-12 (the last exception) arrived
    age = sim.business_days_between(world.DOCUMENT_BY_NO[6].received_on, as_of)
    assert age == 1 and "Tue 2026-10-06" in bucket and f"{age} d" in bucket
    info = section(html, "info-tasks")
    assert 'href="/invoice/B-01"' in info and "Invoice payment terms differ from the master (info)" in info
    assert "Listed from the whole run." in info
    blocked = squash(page_text(section(html, "blocked-duplicates")))
    assert 'href="/invoice/B-02"' in section(html, "blocked-duplicates")
    assert ("Not posted. The supplier gets an automatic reply with the status of the original invoice; "
            f"{world.AP_SPECIALIST.name} is informed. Listed from the whole run.") in blocked
    lead = squash(page_text(html[html.index('class="lead"'):html.index("</p>", html.index('class="lead"'))]))
    assert ("The blocking queue is a snapshot at the close of Mon 2026-10-05, the day the last open exception "
            "arrived") in lead and "Info tasks and blocked duplicates are listed from the whole run." in lead
    assert "Email loop" not in html


def test_cockpit_owner_filter(client):
    run_scenario(client, "tobe")
    html = client.get("/gate/exceptions").text
    assert 'href="/gate/exceptions?owner=Sofia%20Brandt"' in html
    html = client.get("/gate/exceptions", params={"owner": "Sofia Brandt"}).text
    queue = html[html.index('class="queue-total"'):html.index('id="info-tasks"')]
    assert "2 blocking exceptions for Sofia Brandt" in page_text(queue)
    assert 'href="/invoice/B-06"' in queue and 'href="/invoice/B-12"' in queue  # owner, and next owner
    assert 'href="/invoice/B-05"' not in queue and 'href="/invoice/B-08"' not in queue
    html = client.get("/gate/exceptions", params={"owner": "Jonas Weber"}).text
    queue = html[html.index('class="queue-total"'):html.index('id="info-tasks"')]
    assert 'href="/invoice/B-05"' in queue and 'href="/invoice/B-06"' not in queue
    assert "No info tasks for Jonas Weber" in html


def test_cockpit_asis_is_a_single_email_loop_bucket(client):
    run_scenario(client, "asis")
    html = client.get("/gate/exceptions").text
    assert html.count('<section class="bucket') == 1
    bucket = section(html, "bucket-email_loop")
    assert "Email loop — untracked" in bucket and '<span class="count">9</span>' in bucket
    assert bucket.count('class="doc-id"') == 9
    assert "owner-filter" not in html


def test_cockpit_asis_ages_stop_at_the_posting_day(client, session):
    """The as-is email loop ends in a posting: a document posted by the snapshot is marked so, and its age is
    counted to its posting day, not to the snapshot."""
    run_scenario(client, "asis")
    html = client.get("/gate/exceptions").text
    bucket = section(html, "bucket-email_loop")
    assert "<th>Posted on</th>" in bucket and "Days in loop" in bucket
    loop = session.scalars(select(GateDecision).where(GateDecision.scenario == "asis",
                                                      GateDecision.exception_type == "email_loop")).all()
    registered = {d.doc_id: get_doc(session, d.doc_id).registered_on for d in loop}
    as_of = sim.cockpit_as_of(registered.values())
    closed = 0
    for d in loop:
        row = bucket[bucket.index(f'id="loop-{d.doc_id}"'):]
        row = squash(page_text(row[:row.index("</tr>")]))
        age = sim.business_days_between(registered[d.doc_id], min(as_of, d.decided_on))
        assert f" {age} d " in f" {row} ", (d.doc_id, row)
        marker = f"posted {main.fmt_date(d.decided_on)}"
        if d.decided_on <= as_of:
            closed += 1
            assert marker in row, d.doc_id
        else:
            assert marker not in row and f"{main.fmt_date(d.decided_on)} after the snapshot" in row, d.doc_id
    assert 0 < closed < len(loop)  # the snapshot shows both kinds
    lead = squash(page_text(html[html.index('class="lead"'):html.index("</p>", html.index('class="lead"'))]))
    assert f"All {len(loop)} documents of the run that went through it are listed." in lead
    assert (f"Snapshot at the close of {main.fmt_date(as_of)}, when the last of them was registered: "
            f"{len(loop) - closed} still in the loop that evening, {closed} already posted.") in lead


def test_cockpit_before_a_run_offers_the_button(client):
    use_scenario(client, "tobe")
    html = client.get("/gate/exceptions").text
    assert "has not been run yet" in html and 'action="/run"' in html


def test_kpis_show_values_and_formulas(client, session):
    use_scenario(client, "tobe")
    html = client.get("/gate/kpis").text  # before a run: upstream tiles, downstream asks for a run
    assert "Upstream — process health" in html and "Downstream — automation efficiency" in html
    before = metrics.compute(session, "tobe")["kpis"].values()
    need_run = [k for k in before if k["group"] == "downstream" or k["value"] is None]
    assert len(need_run) < len(before) and html.count(">Run the scenario<") == len(need_run)
    assert 'id="kpi-accounts_per_supplier"' in html and ">1.3<" in html
    assert "chart.js@4" not in html
    run_scenario(client, "tobe")
    html = client.get("/gate/kpis").text
    text = squash(page_text(html))
    tile = html[html.index('id="kpi-touchless_rate"'):]
    assert '<span class="kpi-value">71.4%</span>' in tile[:600]
    kpis = metrics.compute(session, "tobe")["kpis"]
    for i, kpi in enumerate(kpis.values(), start=1):
        assert f'id="note-{i}"' in html
        assert squash(f"{kpi['label']}. {kpi['formula']}") in text, kpi["key"]  # numbered footnote
    assert len(re.findall(r'class="kpi-tile[ "]', html)) == len(kpis)
    assert html.count('" title="') >= len(kpis)  # formula on hover
    assert "https://cdn.jsdelivr.net/npm/chart.js@4" in html
    raw = re.search(r'id="chart-data" type="application/json">(.*?)</script>', html).group(1)
    data = json.loads(raw)
    assert data["outcomes"]["labels"][0] == "Posted" and sum(data["outcomes"]["values"]) == len(world.DOCUMENTS)
    assert len(data["cycle"]["labels"]) == len(world.DOCUMENTS)


def test_kpis_and_compare_survive_a_metrics_error(client, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("division by zero somewhere")
    monkeypatch.setattr(metrics, "compute", boom)
    monkeypatch.setattr(metrics, "compare", boom)
    for path, what in (("/gate/kpis", "KPIs"), ("/gate/compare", "comparison")):
        r = client.get(path)
        assert r.status_code == 200 and f"The {what} could not be computed: division by zero somewhere" in r.text


def test_compare_offers_run_both_then_shows_both_columns(client):
    html = client.get("/gate/compare").text
    assert "Neither scenario has been run yet" in html and 'action="/run-both"' in html
    run_scenario(client, "tobe")
    html = client.get("/gate/compare").text
    assert "Scenario A — As-is has not been run yet" in html and ">Not run<" in html
    r = client.post("/run-both")
    assert r.status_code == 200 and r.url.path == "/gate/compare"
    html = r.text
    assert 'action="/run-both"' not in html
    for d in world.DOCUMENTS:
        row = html[html.index(f'id="cmp-doc-{d.no}"'):]
        row = row[:row.index("</tbody>")]
        assert f'href="/invoice/A-{d.no:02d}"' in row and f'href="/invoice/B-{d.no:02d}"' in row
        assert "Designed to show:" in row
    assert ">duplicate posting<" in html and ">blocked duplicate<" in html
    tile = html[html.index('id="cmp-touchless_rate"'):]
    tile = tile[:tile.index('class="kpi-tile')]
    assert "35.7%" in tile and "71.4%" in tile and "side-tobe better" in tile
    assert "Cycle time, shown both ways" in html and "Reference path: non-PO invoice sent to a store" in html


def test_rerun_gate_htmx_partial_and_no_js_fallback(client, session):
    load_documents(client, "tobe")
    r = client.post("/invoice/B-06/rerun", headers={"HX-Request": "true"})  # not processed yet: runs it
    assert r.status_code == 200 and 'id="gate-panel"' in r.text and "<footer" not in r.text
    assert "Gate re-run: Exception: Price or quantity outside tolerance, owner Sofia Brandt." in r.text
    r = client.post("/invoice/B-06/rerun", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/invoice/B-06"
    session.expire_all()
    rows = session.scalars(select(GateDecision).where(GateDecision.doc_id == "B-06")).all()
    assert len(rows) == 1  # the old decision is replaced, not duplicated


def test_rerun_error_is_reported_not_500(client, monkeypatch, capsys):
    run_scenario(client, "tobe")

    def boom(*args, **kwargs):
        raise RuntimeError("PO table locked")
    monkeypatch.setattr(gate, "rerun_document", boom)
    r = client.post("/invoice/B-06/rerun", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "The gate could not process B-06; see the server log." in r.text
    assert "PO table locked" not in r.text
    assert "[rerun] doc=B-06 FAILED: RuntimeError('PO table locked')" in capsys.readouterr().out


def test_rerun_processes_the_earlier_unprocessed_documents_first(client, session):
    load_documents(client, "tobe")
    r = client.post("/invoice/B-06/rerun", headers={"HX-Request": "true"})
    assert r.status_code == 200
    session.expire_all()
    docs = sorted(session.scalars(select(InboundDocument).where(InboundDocument.scenario == "tobe")),
                  key=gate.order_key)
    ids = [d.doc_id for d in docs]
    earlier = ids[:ids.index("B-06")]
    processed = set(session.scalars(select(GateDecision.doc_id).where(GateDecision.scenario == "tobe")))
    assert earlier and processed == set(earlier) | {"B-06"}  # the earlier ones, then B-06; nothing later
    text = squash(page_text(r.text))
    assert (f"{len(earlier)} earlier documents not processed yet went first, in processing order: "
            f"{', '.join(earlier)}.") in text
    assert "Exception: Price or quantity outside tolerance" in text


def test_kpis_and_compare_warn_when_only_some_documents_are_processed(client, session):
    load_documents(client, "tobe")
    client.post("/invoice/B-06/rerun")
    session.expire_all()
    done, n = count_rows(session, GateDecision, "tobe"), len(world.DOCUMENTS)
    assert 0 < done < n
    warning = f"Only {done} of {n} documents processed — run the scenario"
    use_scenario(client, "tobe")
    html = client.get("/gate/kpis").text
    assert warning in squash(page_text(html))
    kpi_warning = html[html.index("partial-run"):]
    assert '<input type="hidden" name="scenario" value="tobe">' in kpi_warning[:kpi_warning.index("</form>")]
    assert f"B — To-be: {warning}" in squash(page_text(client.get("/gate/compare").text))
    run_scenario(client, "tobe")
    assert "documents processed — run the scenario" not in client.get("/gate/kpis").text
    assert "documents processed — run the scenario" not in client.get("/gate/compare").text


def test_draft_button_shows_the_unavailable_reason_in_fixture_mode(client):
    run_scenario(client, "tobe")
    assert "Draft message to owner" not in gate_panel(client.get("/invoice/B-03").text)  # posted: no exception
    r = client.post("/invoice/B-06/draft", headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="draft-box"' in r.text and "<footer" not in r.text
    assert "Drafts need the Gemini API (EXTRACTOR=fixture)." in r.text
    assert drafts.DRAFT_LABEL not in r.text
    r = client.post("/invoice/B-06/draft")  # no JS: redirect + flash
    assert r.status_code == 200 and r.url.path == "/invoice/B-06"
    assert "No draft: Drafts need the Gemini API (EXTRACTOR=fixture)." in r.text


def test_draft_text_is_shown_under_the_exact_label(client, monkeypatch):
    run_scenario(client, "tobe")
    calls = []

    def fake_draft(decision, doc, *, allow_api=True, force=False):
        calls.append((decision.doc_id, force))
        return drafts.Draft(text="Sofia, invoice AD-2026/0788 is 300.00 EUR above PO 4500109.", model="gemini-test",
                            from_cache=True, created_on=datetime(2026, 10, 2, 12, 0))
    monkeypatch.setattr(drafts, "get_draft", fake_draft)
    r = client.post("/invoice/B-06/draft?force=1", headers={"HX-Request": "true"})
    assert f'<p class="draft-label">{drafts.DRAFT_LABEL}</p>' in r.text
    assert drafts.DRAFT_LABEL == "Draft by Gemini — reviewed by AP"
    assert "Sofia, invoice AD-2026/0788 is 300.00 EUR above PO 4500109." in r.text and "from cache" in r.text
    assert calls == [("B-06", True)]


def test_scenario_query_parameter_sets_the_cookie(client):
    use_scenario(client, "asis")
    r = client.get("/inbox?scenario=tobe")
    assert r.status_code == 200 and "switch-tobe active" in r.text
    assert client.cookies.get("scenario") == "tobe"
    assert "switch-tobe active" in client.get("/gate/kpis").text  # sticks without the parameter
    r = client.get("/gate/exceptions?scenario=bogus")  # an invalid value is ignored
    assert r.status_code == 200 and "switch-tobe active" in r.text
    r = client.post("/scenario/asis", headers={"referer": "http://testserver/gate/kpis?scenario=tobe"},
                    follow_redirects=False)
    assert r.headers["location"] == "/gate/kpis"  # the switch wins: the parameter is not carried back


def test_pending_invoices_show_flag_chips_and_totals(client, session):
    run_scenario(client, "asis")
    html = client.get("/erp/pending-invoices").text
    assert '<a href="/invoice/A-01" class="chip chip-bad">duplicate of A-01</a>' in html
    assert ">wrong entity<" in html and ">unapplied credit<" in html and ">terms: invoice<" in html
    rows = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.scenario == "asis")).all()
    for currency in {r.currency for r in rows}:
        total = sum(r.total for r in rows if r.currency == currency)
        assert f"{main.fmt_money(total)} {currency}" in html, currency
    run_scenario(client, "tobe")
    html = client.get("/erp/pending-invoices").text
    assert ">DoA auto-approved<" in html and ">terms: master<" in html and ">credit applied<" in html
    assert ">terms: invoice<" not in html and "duplicate of" not in html


@pytest.mark.parametrize("scenario", config.SCENARIOS)
def test_pending_invoices_terms_chip_only_on_rows_with_terms(client, session, scenario):
    run_scenario(client, scenario)
    html = client.get("/erp/pending-invoices").text
    rows = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.scenario == scenario)).all()
    assert any(r.terms_days is None for r in rows)  # the credit note
    for r in rows:
        row = html[html.index(f'<tr id="{r.invoice_id}">'):]
        row = row[:row.index("</tr>")]
        assert ('class="chip chip-ok">terms: ' in row or 'class="chip chip-warn">terms: ' in row) == (
            r.terms_days is not None), r.invoice_id


def test_exceptions_chart_labels_are_wrapped(client):
    run_scenario(client, "tobe")
    html = client.get("/gate/kpis").text
    data = json.loads(re.search(r'id="chart-data" type="application/json">(.*?)</script>', html).group(1))
    exceptions = data["exceptions"]
    assert len(exceptions["lines"]) == len(exceptions["labels"]) > 0
    for label, lines in zip(exceptions["labels"], exceptions["lines"]):
        assert " ".join(lines) == label
        assert all(len(line) <= main.CHART_LABEL_WIDTH for line in lines), lines
    assert any(len(lines) > 1 for lines in exceptions["lines"])  # e.g. "PO exists, no receipt or service ..."
    assert "labels: data.exceptions.lines" in html
    assert main.label_lines("Supercalifragilisticexpialidocious-and-more") == [
        "Supercalifragilisticexpialidocious-and-more"]  # a single long word is never split


def test_load_documents_clears_previous_gate_results(client, session):
    run_scenario(client, "tobe")
    load_documents(client, "tobe")
    session.expire_all()
    assert count_rows(session, GateDecision, "tobe") == 0 and count_rows(session, Run, "tobe") == 0
