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
    # brief v2 section 7: no Load menu in the header; Run scenario (offline) and one Reset demo with a confirm
    assert "Load sample documents" not in html and 'action="/documents/load"' not in html
    assert 'action="/run"' in html and "Run scenario</button>" in html
    assert re.search(r"<button[^>]*\bdisabled\b[^>]*>[^<]*(<svg.*?</svg>)?Run scenario", html) is None
    assert 'action="/reset"' in html and "confirm(" in html and "Reset demo</button>" in html
    assert "phase 2" not in html.lower()  # no "phase 2" markers left in the navigation
    assert "webhook" not in html.lower()  # live intake is phase 3: not advertised in the UI
    # the six stages of the deck, in order, on every page
    strip = html[html.index('class="stage-strip"'):html.index("</nav>", html.index('class="stage-strip"'))]
    labels = re.findall(r'class="stage-label">([^<]+)<', strip)
    assert labels == ["Buy", "Set up the supplier", "Receive or confirm", "Invoice arrives", "Control gate",
                      "Resolve, post and pay"]
    assert current_stages(html) == ["Invoice arrives"]


def current_stages(html: str) -> list[str]:
    """Labels of the highlighted stages of the stage strip."""
    strip = html[html.index('class="stage-strip"'):html.index("</nav>", html.index('class="stage-strip"'))]
    return re.findall(r'aria-current="step">\s*<span class="stage-no">\d+</span><span class="stage-label">([^<]+)<',
                      strip)


@pytest.mark.parametrize("path, stages", [
    ("/erp/contracts", ["Buy"]), ("/erp/vendors", ["Set up the supplier"]),
    ("/erp/purchase-orders", ["Buy", "Receive or confirm"]),  # purchase orders and their receipts on one page
    ("/inbox", ["Invoice arrives"]), ("/run", ["Control gate"]), ("/gate/exceptions", ["Resolve, post and pay"]),
    ("/erp/pending-invoices", ["Resolve, post and pay"]), ("/gate/kpis", []), ("/gate/compare", []),
    ("/assumptions", [])])
def test_stage_strip_highlights_the_page_stage(client, path, stages):
    assert current_stages(client.get(path).text) == stages, path


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
    """A second tab that switched the cookie must not make this page's Run hit the other scenario; Reset demo resets
    both scenarios whatever the page shows."""
    use_scenario(client, "asis")
    html = client.get("/inbox").text  # rendered for as-is
    assert '<input type="hidden" name="scenario" value="asis">' in form_html(html, "/run")
    assert '<input type="hidden" name="scenario"' not in form_html(html, "/reset")  # Reset demo: both scenarios
    assert html.count('<input type="hidden" name="scenario" value="asis">') == 1
    use_scenario(client, "tobe")  # the other tab
    r = client.post("/run", data={"scenario": "asis"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/run"
    assert "scenario=asis" in r.headers["set-cookie"]  # the rule log opens on the scenario that ran
    assert count_rows(session, GateDecision, "asis") == len(world.DOCUMENTS)
    assert count_rows(session, GateDecision, "tobe") == 0
    assert client.cookies.get("scenario") == "asis"
    r = client.post("/reset", data={"scenario": "asis"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    assert client.cookies.get("scenario") == "tobe"  # the demo starts on the to-be inbox
    session.expire_all()
    assert count_rows(session, GateDecision, "asis") == len(world.DOCUMENTS)
    assert count_rows(session, GateDecision, "tobe") == len(world.DOCUMENTS) - 1  # the next email is held back
    # the unadvertised load route (tests, CLI parity) still honours the scenario the page sent
    use_scenario(client, "asis")
    r = client.post("/documents/load", data={"scenario": "tobe"}, follow_redirects=False)
    assert r.status_code == 303 and client.cookies.get("scenario") == "tobe"
    session.expire_all()
    assert count_docs(session, "tobe") == len(world.DOCUMENTS)
    for action in ("/run", "/documents/load"):
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
    heading = "Document not found" if detail else "Page not found"
    assert "Error 404" in html and f"<h1>{heading}</h1>" in html and 'href="/inbox">Back to the inbox' in html
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
# Inbox, Reset demo and Receive next email (the sample-document load route is kept for tests and parity)
# --------------------------------------------------------------------------------------------


def test_inbox_empty_state_offers_the_demo_reset(client):
    html = client.get("/inbox").text
    body = html[html.index('<main class="content">'):]
    assert "No documents in the mailboxes" in body
    assert 'action="/reset"' in body and "Reset demo</button>" in body
    assert 'action="/documents/load"' not in html  # no Load menu (brief v2 section 7)


def test_load_lists_asis_documents_split_by_channel(client, session):
    html = load_documents(client, "asis")
    n = len(world.DOCUMENTS)
    assert f"{n} documents loaded" in html
    assert f"{n} extracted" in html
    assert count_docs(session, "asis") == n
    for channel, mailbox in world.MAILBOX_BY_CHANNEL.items():
        expected = [seed.doc_id_for("asis", d.no) for d in world.DOCUMENTS if d.channel == channel]
        others = [seed.doc_id_for("asis", d.no) for d in world.DOCUMENTS if d.channel != channel]
        section = mailbox_section(html, channel)
        assert f'data-count="{len(expected)}"' in section
        assert mailbox in section
        for doc_id in expected:
            assert f'id="doc-{doc_id}"' in section
        for doc_id in others:
            assert f'id="doc-{doc_id}"' not in section
    # scenario A has no model: AP keys the documents, the cards name no reading
    assert "Read with Gemini" not in html and "Not read yet" not in html


def test_load_lists_tobe_documents_on_the_one_intake_address(client, session):
    html = load_documents(client, "tobe")
    n = len(world.DOCUMENTS)
    assert f"{n} documents loaded" in html and f"{n} extracted" in html
    assert count_docs(session, "tobe") == n
    assert 'id="mailbox-store_mailbox"' not in html  # one intake address in to-be
    section = mailbox_section(html, "ap_mailbox")
    assert f'data-count="{n}"' in section and world.AP_MAILBOX in section
    for d in world.DOCUMENTS:
        assert f'id="doc-{seed.doc_id_for("tobe", d.no)}"' in section
    assert html.count(">Read with Gemini<") == n and "Not read yet" not in html


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
        # received and registered on the same timestamp: "Registered B-05 · Fri 2026-10-02 14:02 · ap@velox.com"
        assert html.count(main.fmt_datetime(d.received_on)) >= 2
        assert f"Registered <strong>{seed.doc_id_for('tobe', d.no)}</strong>" in html
    assert html.count("clock started") == len(world.DOCUMENTS)


def test_doc_type_chips_after_extraction(client):
    html = load_documents(client, "tobe")
    n_credit = sum(1 for d in world.DOCUMENTS if d.true_doc_type == "credit_note")
    n_reminder = sum(1 for d in world.DOCUMENTS if d.true_doc_type == "reminder")
    assert (n_credit, n_reminder) == (1, 1)  # case documents 4 and 2
    assert html.count(">Credit note<") == n_credit
    assert html.count('<span class="chip chip-warn">Payment reminder</span>') == n_reminder
    assert html.count(">Invoice<") == len(world.DOCUMENTS) - n_credit - n_reminder


def test_load_without_cache_or_key_reports_pending_extraction(client, monkeypatch, tmp_cache_dir):
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    html = load_documents(client, "tobe")
    n = len(world.DOCUMENTS)
    assert f"Extraction pending for {n} documents: set GEMINI_API_KEY in .env and run make extract" in html
    assert "failed" not in html
    assert html.count(">Not read yet<") == n
    page = client.get("/invoice/B-05")
    assert page.status_code == 200
    assert "Not read yet." in page.text and "Extract now" not in page.text  # no extract button (brief v2)
    # the running server does not re-read .env: the hint names the restart
    assert ("Running the control gate reads it from the extraction cache; if the file was never read, "
            "GEMINI_API_KEY must be set in .env (then restart the app, or run make extract).") in page.text
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
    assert "Extraction pending for 11 documents (retry with make extract)." in html


def test_load_message_kind_and_key_hint(monkeypatch):
    base = {"extracted": 12, "from_cache": 12, "unavailable": 0, "failed": 0, "error": None}
    assert main.load_message(12, "asis", base)[0] == "ok"
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
    # one provenance line (brief v2): the fixture disclaimer here, "Read by <model> (cached)" with the cache
    assert html.count('class="provenance') == 1
    assert "FIXTURE — ground-truth test data, not a Gemini output (EXTRACTOR=fixture)." in html
    assert 'src="/files/B-03.pdf#' in html
    for name, label in main.extract.FIELD_LABELS.items():
        assert (label in html) == (name != "notes"), name  # fields + confidence, no free-text notes
    assert "Supplier tax ID (VAT)" in html and "Supplier VAT ID" not in html
    spec = world.DOCUMENT_BY_NO[3]
    assert spec.invoice_number in html and spec.printed_supplier_name in html
    assert spec.po_numbers[0] in html
    assert "99%" in html and 'style="width: 99%"' in html
    assert "Not processed yet." in html and "Run the control gate</button>" in html
    # no latency, tokens, created-on, raw JSON, re-extract button or critical-field chips
    for gone in ("Raw extraction JSON", "Force re-extract", "Extract now", "Latency", "Tokens", "Created on",
                 'class="crit', "critical field missing"):
        assert gone not in html, gone
    # the order of the page: Document -> Gemini output -> rule log and outcome
    assert html.index(">Document</h2>") < html.index(">Gemini output</h2>") < html.index('id="gate-panel"')


def test_low_confidence_is_flagged(client, session):
    load_documents(client, "tobe")
    doc = session.scalar(select(InboundDocument).where(InboundDocument.doc_id == "B-03"))
    data = dict(doc.extraction.json)
    data["invoice_number"] = {"value": "INV-2026-0457", "confidence": 0.42}
    doc.extraction.json = data
    session.commit()
    html = client.get("/invoice/B-03").text
    assert "42%" in html and html.count('class="row-low"') == 1 and html.count('class="conf conf-low"') == 1
    assert "below 80%" in html  # the legend names the threshold once, no per-field chip
    assert "chip-bad\">below" not in html


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
    assert "Not read yet." in r.text


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
    # no content sniffing; no CSP sandbox, which would break the browser's PDF viewer in the invoice-page frame
    assert r.headers["x-content-type-options"] == "nosniff" and "content-security-policy" not in r.headers


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
    # deck A6: all records (inactive included) / unique suppliers by tax ID, two decimals
    assert "28 accounts / 12 suppliers = 2.33" in asis
    assert 'class="dup-of"' in asis and "Duplicate of 4 accounts" in asis and "Not linked to a supplier" in asis
    assert 'class="rule"' in asis and "D1 spelling duplicate" in asis
    use_scenario(client, "tobe")
    tobe = client.get("/erp/vendors").text
    assert "14 accounts / 12 suppliers = 1.17" in tobe
    assert 'class="dup-of"' not in tobe and not re.search(r"Duplicate of \d+ account", tobe)
    assert "Not linked to a supplier" not in tobe
    assert "party" not in page_text(tobe[tobe.index('<main class="content">'):]).lower()  # "supplier" (brief v2)
    # the toggle shows the other view without switching the scenario
    assert "28 accounts / 12 suppliers = 2.33" in client.get("/erp/vendors?view=asis").text
    assert client.get("/erp/vendors?view=bogus").status_code == 400


def test_vendor_rows_flags_terms_and_duplicates(client):
    use_scenario(client, "asis")
    html = client.get("/erp/vendors").text
    # D1 duplicate of Nordwind, missing identifiers (D4), terms drift (D3), inactive leftovers (D5)
    assert "V-000117 (VDE, similar name)" in html
    assert "missing tax ID" in html and "missing IBAN" in html and "VAT ID" not in html
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
    assert "Contracts &amp; catalogues" in html
    for c in world.CONTRACTS:
        assert c.contract_id in html
        assert world.PARTY_BY_ID[c.party_id].canonical_name in html


def test_contracts_page_lists_the_catalogue_in_tobe_only(client):
    cat = world.CATALOGUES[0]
    use_scenario(client, "tobe")
    tobe = client.get("/erp/contracts").text
    row = tobe[tobe.index(f'id="{cat.catalogue_id}"'):]
    row = row[:row.index("</tr>")]
    assert html_lib.escape(world.PARTY_BY_ID[cat.party_id].canonical_name) in row and cat.legal_entity_code in row
    assert "500.00" in row and "CHF" in row  # the per-invoice limit in the group currency
    use_scenario(client, "asis")
    asis = client.get("/erp/contracts").text
    assert cat.catalogue_id not in asis and "No card or catalogue in this scenario" in asis


def test_pending_invoices_empty_state(client):
    use_scenario(client, "asis")
    html = client.get("/erp/pending-invoices").text
    assert "<h2>No invoices posted yet in A — As-is</h2>" in html and "<h1>Posted invoices" in html
    assert "AP posts here when the scenario runs." in html and 'action="/run"' in html
    assert "control gate" not in page_text(html[html.index('<main class="content">'):]).lower()  # no gate in A
    use_scenario(client, "tobe")
    html = client.get("/erp/pending-invoices").text
    assert "The control gate posts here when the scenario runs." in html


# --------------------------------------------------------------------------------------------
# Reset, assumptions
# --------------------------------------------------------------------------------------------


def test_reset_demo_runs_both_scenarios_and_holds_back_the_next_email(client, session):
    """Brief v2 section 7: both scenarios re-seeded, the 12 case documents loaded and run offline, case document 5
    (FitOut) not arrived yet in to-be; the to-be inbox opens with the Receive next email button."""
    load_documents(client, "tobe")
    client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"})  # anything loaded before is replaced
    r = client.post("/reset")
    assert r.status_code == 200 and r.url.path == "/inbox" and client.cookies.get("scenario") == "tobe"
    n = len(world.DOCUMENTS)
    held = world.DOCUMENT_BY_NO[seed.DEMO_HELD_BACK]
    assert "Demo reset: both scenarios loaded and run (offline)." in r.text
    assert "Receive next email</button>" in r.text and held.sender_email in r.text
    assert f"{n - 1} of {n - 1} documents processed" in page_text(r.text)
    session.expire_all()
    assert count_docs(session, "asis") == n and count_rows(session, GateDecision, "asis") == n
    assert count_docs(session, "tobe") == n - 1 and count_rows(session, GateDecision, "tobe") == n - 1
    assert get_doc(session, seed.doc_id_for("tobe", held.no)) is None
    assert count_rows(session, Run, "asis") == 1 and count_rows(session, Run, "tobe") == 1
    assert {d.dataset for d in session.scalars(select(InboundDocument))} == {"v1"}


def test_receive_next_email_registers_without_running_the_gate(client, session):
    client.post("/reset")
    held = world.DOCUMENT_BY_NO[seed.DEMO_HELD_BACK]
    doc_id = seed.doc_id_for("tobe", held.no)
    r = client.post("/inbox/receive")
    assert r.status_code == 200 and r.url.path == "/inbox"
    stamp = held.received_on.strftime("%a %d %b %Y %H:%M")
    assert f"Email received at {world.AP_MAILBOX}: registered as {doc_id} on {stamp}." in r.text
    card = r.text[r.text.index(f'id="doc-{doc_id}"'):]
    card = card[:card.index("</article>")]
    assert squash(page_text(card)).count(f"Registered {doc_id} · {main.fmt_datetime(held.received_on)} · "
                                         f"{world.AP_MAILBOX} · clock started") == 1
    assert "Not read yet" in card and "Open it to run the control gate" in card and "outcome-chip" not in card
    assert "Receive next email</button>" not in r.text  # nothing else is waiting
    session.expire_all()
    doc = get_doc(session, doc_id)
    assert doc.registered and doc.registered_on == held.received_on and doc.channel == "ap_mailbox"
    assert doc.extraction is None
    assert session.scalar(select(GateDecision).where(GateDecision.doc_id == doc_id)) is None
    # its page offers the gate; running it reads the cache (or fixture) only and shows the outcome
    page = client.get(f"/invoice/{doc_id}").text
    assert "Run the control gate</button>" in page and "Not read yet." in page
    r = client.post(f"/invoice/{doc_id}/rerun")
    assert r.status_code == 200 and r.url.path == f"/invoice/{doc_id}"
    text = squash(page_text(r.text))
    assert "Outcome → Exception" in text and "Exception: PO exists, no receipt or confirmation" in text
    assert "Jonas Weber · Receiver / requester" in text and "2 business days" in text
    # a second press has nothing to receive
    r = client.post("/inbox/receive")
    assert "No email waiting: every case document has arrived." in r.text


def test_reset_always_opens_the_inbox(client):
    load_documents(client, "asis")
    for referer in ("http://testserver/invoice/A-01", "http://testserver/files/A-01.pdf",
                    "http://testserver/erp/vendors"):
        r = client.post("/reset", headers={"referer": referer}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/inbox", referer


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
    assert ">Human review: Low extraction confidence<" in mailbox_section(html, "ap_mailbox")


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
    assert xml.status_code == 200 and xml.headers["content-type"] == "text/plain; charset=utf-8"
    assert xml.content == content
    html = client.get("/invoice/B-W01").text
    assert "<iframe" not in html and 'class="doc-text xml-text"' in html
    assert "&lt;cbc:ID&gt;MM-2026-248&lt;/cbc:ID&gt;" in html  # escaped, never rendered as markup
    assert "Parsed from the UBL e-invoice: structured XML, no model call." in html
    assert ">UBL e-invoice<" in gate_panel(html)


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
    assert "Extract now" not in html and "there is no document to read" in html
    assert ">email body only<" in gate_panel(html)
    use_scenario(client, "tobe")
    assert ">Nothing to read<" in client.get("/inbox").text


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
    assert "4 open" in squash(page_text(html))  # B-01, B-05, B-06 and the scan
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
# Datasets: the case documents (12) in the UI; test set v2 (26) from the CLI and the tests only
# --------------------------------------------------------------------------------------------


def test_datasets_are_named_and_test_set_v2_stays_out_of_the_ui(client):
    assert main.DATASET_CHOICES == {"v1": "Case documents (12)", "v2": "Test set v2 (26)"}
    html = client.get("/inbox").text
    assert 'name="dataset"' not in html and "Test set v2" not in html


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
    assert '<span class="chip chip-warn">Statement</span>' in r.text  # document 3, the account statement
    assert count_docs(session, "tobe") == n
    client.post("/reset")  # Reset demo always returns to the case documents
    session.expire_all()
    assert seed.loaded_dataset(session, "tobe") == "v1" and count_docs(session, "tobe") == len(world.DOCUMENTS) - 1
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
    for url in ("/files/B2-11.xml", "/files/B2-14.txt"):  # received content is never rendered as markup
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "text/plain; charset=utf-8", url
        assert r.headers["x-content-type-options"] == "nosniff", url
        assert r.headers["content-security-policy"] == "default-src 'none'; sandbox", url


def test_b2_14_email_body_presentation(client):
    """Redesign review: an invoice that arrives only in the email body is shown as the email text (no frame, no
    PDF link), the Gemini panel says there is nothing to read, and the gate sends it to Human review under its own
    name, not "low extraction confidence"."""
    client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"})
    client.post("/run", data={"scenario": "tobe"})
    html = client.get("/invoice/B2-14").text
    body = html[html.index('<main class="content">'):]
    assert "<iframe" not in body and "Open PDF" not in body and 'href="/files/B2-14' not in body
    assert '<span class="chip chip-neutral">Email body, no attachment</span>' in body
    assert html.count('class="doc-text email-text"') == 1 and 'class="email-comment"' not in body
    assert "The invoice is only in the text of the email" in body
    assert "Not read yet." in body and "there is no document to read" in body
    panel = squash(page_text(gate_panel(html)))
    assert "Human review: invoice only in the email body" in panel and "Low extraction confidence" not in panel
    assert "Key the invoice from the email text" in panel
    assert "Marco Ruiz · AP review" in panel and "1 business day" in panel


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
    """HTML of the gate panel of an invoice page (rule log, outcome card, info tasks, cycle): the last panel."""
    start = html.index('id="gate-panel"')
    return html[start:html.index("</article>", start)]


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


def test_run_page_shows_the_rule_log_and_every_document(client, session):
    r = run_scenario(client, "tobe")
    html, text = r.text, squash(page_text(r.text))
    n = len(world.DOCUMENTS)
    assert (f"B — To-be: {n} documents processed · Post 8 · Exception 2 · Block 1 · Human review 1 · posted with no "
            "human touch 8.") in text  # flash
    run = session.scalar(select(Run).where(Run.scenario == "tobe"))
    log = run.summary_json["log"]
    # "[B-05] Rule → result — reason" with the deck's words; one Outcome line per document; the run summary last
    rule = re.compile(r"^\[B-\d{2}\] (?P<rule>[^→]+?) → (?P<result>.+?)(?: — .*)?$")
    rules = set(gate.STEP_LABELS["tobe"].values()) | {"Outcome"}
    for line in log[:-1]:
        m = rule.match(line)
        assert m and m.group("rule") in rules, line
    assert sum(1 for line in log if " Outcome → " in line) == n
    assert {rule.match(line).group("result") for line in log if " Outcome → " in line} == {
        "Post", "Exception", "Block", "Human review"}
    assert log[-1].startswith(f"[run] B: {n} documents · Post 8 · Exception 2 · Block 1 · Human review 1")
    for line in log:
        assert squash(line) in text, line
    assert ("[B-07] Supplier (tax ID) → pass — Resolved to Shopsys Software Inc. by tax ID at supplier level; its "
            "record in VUS is V-000105.") in text
    # no code identifiers in the visible log
    assert "[doc " not in text and "step=" not in text and "result=" not in text
    for code in ("po_no_receipt", "price_qty_mismatch", "commitment_match", "resolve_vendor", "blocked_duplicate"):
        assert code not in text, code
    assert html.count('class="log-line') == len(log)
    assert "<h1>Rule log" in html and "Posted with no human touch" in html
    assert "animation-delay:" in html and 'href="#run-log"' in html and "Show all" in html
    assert html.count('id="row-B-') == n
    for link in ('href="/gate/exceptions"', 'href="/gate/kpis"', 'href="/gate/compare"'):
        assert link in html


def test_run_page_before_a_run_offers_the_button(client):
    html = client.get("/run").text
    assert "has not been run yet" in html and 'action="/run"' in html


def test_asis_run_page_never_says_gate(client):
    html = run_scenario(client, "asis").text
    body = squash(page_text(html[html.index('<main class="content">'):html.index("<footer")]))
    assert "Processing run (no gate)" in body and "Processing log" in body
    assert "[A-05] Keyed by AP → pass" in body and "[A-05] Outcome → Email loop — untracked" in body
    for word in ("gate run", "Gate run", "Rule log", "skipped", "Not reached"):
        assert word not in body, word


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
    assert "Not read: B-06, B-07, B-01" in html  # every document, in processing order
    assert "B-11" in html and "flash-warn" in html


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
    line = main.log_line("[B-06] Tolerances → Exception — Line 1: 150 x 44.00 vs PO 4500109 at 42.00 (+300.00)")
    assert (line["prefix"], line["rule"], line["result"], line["tone"], line["kind"]) == (
        "[B-06]", "Tolerances", "Exception", "bad", "step")
    assert line["reason"] == "Line 1: 150 x 44.00 vs PO 4500109 at 42.00 (+300.00)"
    outcome = main.log_line("[B-02] Outcome → Block — Duplicate invoice · Marco Ruiz · SLA — · 0 simulated business days")
    assert (outcome["kind"], outcome["result"], outcome["tone"]) == ("outcome", "Block", "blocked")
    assert main.log_line("[B-01] Registered on arrival → pass — B-01 · Thu 01 Oct 2026")["tone"] == "ok"
    assert main.log_line("[B-01] Approval limit → Human review — above the limit")["tone"] == "warn"
    assert main.log_line("[A-05] PO lookup → email loop — untracked")["tone"] == "loop"
    assert main.log_line("[run] B: 12 documents · Post 8")["kind"] == "run"
    plain = main.log_line("[B-01] a line without an arrow")
    assert (plain["rule"], plain["result"], plain["reason"]) == ("", "", "a line without an arrow")


def test_inbox_shows_outcome_chips_and_owners_after_a_run(client):
    html = load_documents(client, "tobe")
    assert "Not processed yet" in html and "outcome-chip" not in html
    run_scenario(client, "tobe")
    html = client.get("/inbox").text
    n = len(world.DOCUMENTS)
    assert html.count("outcome-chip") == n
    assert f'data-count="{n}"' in mailbox_section(html, "ap_mailbox") and 'id="mailbox-store_mailbox"' not in html
    for label in ("Post", "Block", "Exception: Price or quantity mismatch",
                  "Exception: PO exists, no receipt or confirmation", "Human review: Amount above approval limit"):
        assert f">{label}<" in html, label
    for gone in ("Posted<", "Blocked duplicate", "Credit applied", "Tim Koch"):
        assert gone not in html, gone
    assert "<strong>Sofia Brandt</strong> · Buyer" in html
    assert "<strong>Stefan Keller</strong> · Next approver in the matrix" in html
    assert f"{n} of {n} documents processed" in html


def test_asis_inbox_after_a_run_shows_registration_and_the_email_loop(client):
    run_scenario(client, "asis")
    html = client.get("/inbox").text
    assert "Not registered yet" not in html
    assert html.count(">Email loop — untracked<") == 8
    assert "Forwarded by the store after 7 business days" in html
    assert "AP opened ap@ after 1 business day" in html
    for d in world.DOCUMENTS:
        assert main.fmt_datetime(sim.registration_date("asis", d.channel, d.received_on)) in html


def test_invoice_b06_price_mismatch_routed_to_the_buyer(client):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-06").text)
    text = squash(page_text(panel))
    assert "Exception: Price or quantity mismatch" in panel
    assert "Sofia Brandt · Buyer" in text
    assert "2 business days · due Wed 2026-09-30" in text  # registered Mon 28 Sep + 2 business days
    assert "Agree a correction with the supplier or approve the variance" in panel
    # the rule log: one line per rule that ran, in the deck's words, then the outcome
    rules = re.findall(r'<span class="rl-rule">([^<]+)</span>', panel)
    assert rules == ["Registered on arrival", "Screened", "Read with Gemini", "Confidence", "Document type",
                     "Supplier (tax ID)", "Legal entity", "Duplicate", "Commitment", "Terms", "Tolerances", "Outcome"]
    assert "Not reached" not in panel and "skipped" not in text  # a rule that did not run is not a line
    assert "Tolerances → Exception — Line 1: 150 x 44.00 vs PO 4500109 at 42.00" in text
    assert "Terms → pass — Master terms 60 days apply; the invoice states 30 days, which is ignored." in text
    assert "Outcome → Exception" in text
    assert panel.index('class="rule-log"') < panel.index('id="outcome"')  # rule log, then the outcome card
    assert "Line checks" in panel and "price" in panel
    assert "Simulated cycle: 5 business days" in text and "Resolved past the SLA" in text  # the one past SLA
    assert "Draft message to owner" in panel and "Run again</button>" in panel


def test_invoice_b01_above_the_approval_limit_goes_to_the_next_approver(client):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-01").text)
    text = squash(page_text(panel))
    assert "Human review: Amount above approval limit" in panel
    assert "Stefan Keller · Next approver in the matrix (Finance director DE)" in text
    assert ("Approval limit → Human review — 27,846.00 EUR ≈ CHF 26,175 (simulated rate 0.94) is above the CHF "
            "25,000 approval limit of VDE") in text
    assert "Contract CT-2025-001" in text and "2 business days · due" in text
    assert "Draft message to owner" in panel


def test_invoice_b02_blocked_as_a_duplicate_of_b01(client):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-02").text)
    text = squash(page_text(panel))
    assert ">Block<" in panel and "Payment reminder" in client.get("/invoice/B-02").text
    assert "Duplicate of" in panel and '<a class="doc-id" href="/invoice/B-01">B-01</a>' in panel
    assert "Blocked before posting; AP replies to the supplier with the status of the original invoice." in text
    assert "<dt>SLA</dt><dd>—</dd>" in panel and "automatic" not in text.lower()
    assert "Duplicate → Block — Invoice NWL-2026-00913" in text and "Outcome → Block" in text
    assert "Draft message to owner" not in panel  # drafts exist for Exception and Human review only
    r = client.post("/invoice/B-02/draft", headers={"HX-Request": "true"})
    assert "Block: AP replies to the supplier with the status of the original invoice; there is no owner to write to." in r.text


def test_invoice_b10_posted_on_master_terms(client, session):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-10").text)
    text = squash(page_text(panel))
    assert ">Post<" in panel and "contract match" in panel
    pvi = session.scalar(select(PendingVendorInvoice).where(PendingVendorInvoice.doc_id == "B-10"))
    assert f'href="/erp/pending-invoices#{pvi.invoice_id}"' in panel
    # the terms come from the master; the invoice's own terms are only named in the rule line (no task, no flag)
    assert f"Payment terms {pvi.terms_days} days from the master" in text and pvi.terms_source == "master"
    assert f"Terms → pass — Master terms: {pvi.terms_days} days." in text
    assert f"<dt>Due date</dt><dd>{main.fmt_date(pvi.due_date)}</dd>" in panel
    contract = session.scalar(select(GateDecision).where(GateDecision.doc_id == "B-10")).details["contract_id"]
    assert f"Contract {contract}" in text
    assert "Info tasks (non-blocking)" not in panel and "Draft message to owner" not in panel


def test_invoice_b09_matches_the_card_catalogue_commitment(client):
    run_scenario(client, "tobe")
    panel = gate_panel(client.get("/invoice/B-09").text)
    text = squash(page_text(panel))
    assert ">Post<" in panel and "catalogue match" in panel
    assert "Card / catalogue CAT-2026-001 small store purchase" in text
    assert "Commitment → pass — Small store purchase → card / catalogue: CAT-2026-001" in text
    assert "Approval limit → pass" in text and "CHF" in text


def test_invoice_a02_duplicate_posting_and_asis_badges(client):
    run_scenario(client, "asis")
    panel = gate_panel(client.get("/invoice/A-02").text)
    assert ">Email loop — untracked<" in panel and ">duplicate posting<" in panel
    assert "Untracked manual follow-up by email" in panel
    assert "V-000117" in panel and "from the invoice" in panel
    assert "Draft message to owner" not in panel
    # scenario A: AP keys every document (deck slide 4); the halted quick-fix tool is history only
    text = squash(page_text(panel))
    assert "Keyed by AP → pass" in text and "Processing log" in text and "quick-fix" not in text.lower()
    assert "Today's processing (AP, no gate)" in text and "control gate" not in text.lower()
    assert "Outcome → Email loop — untracked" in text
    assert ">wrong entity<" in gate_panel(client.get("/invoice/A-10").text)
    assert ">unapplied credit<" in gate_panel(client.get("/invoice/A-04").text)


def queue_row(html: str, doc_id: str) -> str:
    """Visible text of one row of the cockpit queue."""
    row = html[html.index(">", html.index(f'id="queue-{doc_id}"')) + 1:]
    return squash(page_text(row[:row.index("</tr>")])).strip()


def test_cockpit_tobe_groups_by_type_with_owner_sla_and_days_open(client):
    run_scenario(client, "tobe")
    html = client.get("/gate/exceptions").text
    assert "3 open · 1 past SLA" in squash(page_text(html))
    for key, docs in (("po_no_receipt", ["B-05"]), ("price_qty_mismatch", ["B-06"]),
                      ("amount_above_approval_limit", ["B-01"])):
        bucket = section(html, f"bucket-{key}")
        for doc_id in docs:
            assert f'href="/invoice/{doc_id}"' in bucket, (key, doc_id)
        assert f'<span class="count">{len(docs)}</span>' in bucket
    for column in ("Document", "Supplier", "Type", "Owner", "SLA (days)", "Days open", "Past SLA"):
        assert f"{column}</th>" in html, column
    assert ">Age<" not in html and "SLA due" not in html
    as_of = sim.cockpit_as_of(world.DOCUMENT_BY_NO[n].received_on for n in (1, 5, 6))
    assert as_of == datetime(2026, 10, 2, 17, 0)  # close of the day B-05 (the last open exception) arrived
    # exactly one open exception is past its SLA at the snapshot: B-06, with the buyer
    days = sim.business_days_between(world.DOCUMENT_BY_NO[6].received_on, as_of)
    assert days == 4
    b06 = queue_row(html, "B-06")
    assert b06.startswith("B-06 Atlas Displays SL Price or quantity mismatch Line 1: 150 x 44.00")
    assert "Sofia Brandt · Buyer" in b06 and b06.endswith(f" 2 {days} past SLA")
    assert "Stefan Keller · Next approver in the matrix" in queue_row(html, "B-01")
    assert queue_row(html, "B-01").endswith("2 1 within SLA")
    assert queue_row(html, "B-05").endswith("2 0 within SLA")
    assert "Jonas Weber · Receiver / requester (Store development manager DE)" in queue_row(html, "B-05")
    assert html.count('class="chip chip-bad">past SLA<') == 1
    info = section(html, "info-tasks")
    assert "No info tasks." in info and "Listed from the whole run." in info
    blocked = squash(page_text(section(html, "blocked-duplicates")))
    assert 'href="/invoice/B-02"' in section(html, "blocked-duplicates")
    assert "Block — duplicate invoices · AP replies with status" in blocked
    assert (f"Blocked before posting: no owner task and no SLA. AP ({world.AP_SPECIALIST.name}) replies to the "
            "supplier with the status of the original invoice. Listed from the whole run.") in blocked
    start = html.index('class="snapshot-note"')
    note = squash(page_text(html[start:html.index("</p>", start)]))
    assert ("The queue is a snapshot at the close of Fri 2026-10-02, the day the last open exception arrived: days "
            "open are business days from registration to it; past SLA = still open after its SLA.") in note
    assert "every SLA is met" not in html and "Email loop" not in html


def test_cockpit_owner_filter(client):
    run_scenario(client, "tobe")
    html = client.get("/gate/exceptions").text
    assert 'href="/gate/exceptions?owner=Sofia%20Brandt"' in html
    html = client.get("/gate/exceptions", params={"owner": "Sofia Brandt"}).text
    queue = html[html.index('class="queue-total"'):html.index('id="info-tasks"')]
    assert "1 open · 1 past SLA for Sofia Brandt (as owner or next owner)" in squash(page_text(queue))
    assert 'href="/invoice/B-06"' in queue
    assert 'href="/invoice/B-05"' not in queue and 'href="/invoice/B-01"' not in queue
    html = client.get("/gate/exceptions", params={"owner": "Jonas Weber"}).text
    queue = html[html.index('class="queue-total"'):html.index('id="info-tasks"')]
    assert 'href="/invoice/B-05"' in queue and 'href="/invoice/B-06"' not in queue
    assert "No info tasks for Jonas Weber" in html


def test_cockpit_asis_is_a_single_email_loop_bucket(client):
    run_scenario(client, "asis")
    html = client.get("/gate/exceptions").text
    assert html.count('<section class="bucket') == 1
    bucket = section(html, "bucket-email_loop")
    assert "Email loop — untracked" in bucket and '<span class="count">8</span>' in bucket
    assert bucket.count('class="doc-id"') == 8
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
        raw = bucket[bucket.index(f'id="loop-{d.doc_id}"'):]
        raw = raw[:raw.index("</tr>")]
        row = squash(page_text(raw))
        age = sim.business_days_between(registered[d.doc_id], min(as_of, d.decided_on))
        assert f'<td class="num nowrap">{age}</td>' in raw, (d.doc_id, row)  # the Days open column
        marker = f"posted {main.fmt_date(d.decided_on)}"
        if d.decided_on <= as_of:
            closed += 1
            assert marker in row, d.doc_id
        else:
            assert marker not in row and f"{main.fmt_date(d.decided_on)} after the snapshot" in row, d.doc_id
    # the twelve case documents: the loop outlasts the last registration, so every one is still open that evening
    assert closed == 0 and len(loop) == 8
    start = html.index('class="snapshot-note"')
    note = squash(page_text(html[start:html.index("</p>", start)]))
    assert f"All {len(loop)} documents of the run that went through it are listed." in note
    assert (f"Snapshot at the close of {main.fmt_date(as_of)}, when the last of them was registered: "
            f"{len(loop)} still in the loop that evening, 0 already posted.") in note
    # a document posted by the snapshot is marked so, and its days open stop at the posting day
    d = loop[0]
    late = d.decided_on + (d.decided_on - registered[d.doc_id])
    view = main.gate_view(d, get_doc(session, d.doc_id), late)
    assert view["posted_by_snapshot"] and not view["past_sla"]
    assert view["age_days"] == sim.business_days_between(registered[d.doc_id], d.decided_on)


def test_cockpit_before_a_run_offers_the_button(client):
    use_scenario(client, "tobe")
    html = client.get("/gate/exceptions").text
    assert "has not been run yet" in html and 'action="/run"' in html


def test_kpis_show_values_and_formulas(client, session):
    use_scenario(client, "tobe")
    html = client.get("/gate/kpis").text  # before a run: accounts per supplier; the others ask for a run
    # the four metrics of the deck's slide 11 with their tags, then the small indicator
    assert html.count(">Upstream · process health<") == 2 and html.count(">Downstream · automation efficiency<") == 2
    assert html.count(">Indicator<") == 1
    before = metrics.compute(session, "tobe")["kpis"].values()
    need_run = [k for k in before if k["value"] is None]
    assert len(need_run) == 4 and html.count(">needs a run<") == len(need_run)
    assert 'id="kpi-accounts_per_supplier"' in html and ">1.17<" in html
    assert "chart.js@4" not in html
    run_scenario(client, "tobe")
    html = client.get("/gate/kpis").text
    text = squash(page_text(html))
    tile = html[html.index('id="kpi-touchless_rate"'):]
    assert '<span class="kpi-value">72.7%</span>' in tile[:800]
    cycle = html[html.index('id="kpi-cycle_time_median"'):]
    assert '<small class="sim-label">simulated</small>' in cycle[:800]
    assert html.count("kpi-tile kpi-hero") == 4 and html.count("kpi-tile kpi-mini") == 1
    for gone in ("Cash leakage", "Exception rate", "Registration lag", "Reference path", "DoA", "Designed to show",
                 "same definitions as the case deck", "KPIs"):
        assert gone not in html, gone
    kpis = metrics.compute(session, "tobe")["kpis"]
    for i, kpi in enumerate(kpis.values(), start=1):
        assert f'id="note-{i}"' in html
        assert squash(f"{kpi['label']}. {kpi['formula']}") in text, kpi["key"]  # numbered footnote
    assert len(re.findall(r'class="kpi-tile[ "]', html)) == len(kpis)
    assert html.count('class="kpi-formula" role="tooltip"') == len(kpis)  # formula on hover and focus
    for kpi in kpis.values():
        start = html.index(f'id="f-{kpi["key"]}"')
        assert squash(kpi["formula"]) in squash(page_text(html[start:html.index("</div>", start)])), kpi["key"]
    assert len(re.findall(r'class="kpi-tile[^"]*" id="kpi-\w+" tabindex="0"', html)) == len(kpis)
    assert "https://cdn.jsdelivr.net/npm/chart.js@4" in html
    raw = re.search(r'id="chart-data" type="application/json">(.*?)</script>', html).group(1)
    data = json.loads(raw)
    assert data["outcomes"]["labels"] == ["Post", "Exception", "Block", "Human review"]
    assert sum(data["outcomes"]["values"]) == len(world.DOCUMENTS)
    assert len(data["cycle"]["labels"]) == len(world.DOCUMENTS)
    paths = dict(zip(data["cycle"]["labels"], data["cycle"]["paths"]))
    assert paths["B-02"] == "Blocked, not posted" and paths["B-03"] == "Touchless"  # a Block is never touchless
    assert paths["B-06"] == "Exception with SLA"


def test_kpi_layout_places_every_kpi_once(session):
    """The four headline metrics in the deck's order + the small ones: none dropped or shown twice."""
    kpis = main.ordered_kpis(metrics.compute(session, "tobe")["kpis"])
    headline, small = main.kpi_layout(kpis)
    placed = [t["key"] for t in headline] + [t["key"] for t in small]
    assert sorted(placed) == sorted(k["key"] for k in kpis)
    assert [t["key"] for t in headline] == list(metrics.HEADLINE) == [
        "first_pass_match_rate", "accounts_per_supplier", "touchless_rate", "cycle_time_median"]
    assert [t["key"] for t in small] == ["registered_same_day"]
    extra = kpis + [{"key": "new_kpi", "group": "downstream"}]  # a metric the layout does not know yet
    assert main.kpi_layout(extra)[1][-1]["key"] == "new_kpi"


def test_presentation_helpers():
    assert main.value_parts("29,646.00 EUR") == [("29,646.00", "EUR")]
    assert main.value_parts("1,800.00 EUR + 500.00 USD") == [("1,800.00", "EUR"), ("500.00", "USD")]
    assert main.value_parts("0.5 days") == [("0.5", "days")] and main.value_parts("71.4%") is None
    assert main.fmt_signed(300) == "+300.00" and main.fmt_signed(-12.5) == "-12.50"
    assert main.nav_active("/run") == "/inbox" and main.nav_active("/invoice/B-01") == "/inbox"
    # a badge that repeats the outcome pill next to it is dropped; the others stay
    assert main.badge_chips(["credit note linked", "contract match"], "Credit note linked") == [("contract match", "ok")]
    # the four outcomes of the gate; the as-is words
    assert main.outcome_chip("posted", None) == ("Post", "ok")
    assert main.outcome_chip("applied_credit", None) == ("Post", "ok")
    assert main.outcome_chip("blocked_duplicate", "duplicate_invoice") == ("Block", "blocked")
    assert main.outcome_chip("exception", "no_po") == ("Exception: No PO", "bad")
    assert main.outcome_chip("human_review", "amount_above_approval_limit") == (
        "Human review: Amount above approval limit", "warn")
    assert main.outcome_chip("posted", None, "asis") == ("Posted by AP", "ok")
    assert main.outcome_chip("exception", "email_loop", "asis") == ("Email loop — untracked", "loop")
    # line-check headers: never "Line None"
    assert main.line_label({"line": 2}) == "Line 2"
    assert main.line_label({"line": None, "po_line": None}) == "Net total"
    assert main.line_label({"line": None, "po_line": 1}) == "PO line 1"
    assert main.line_label({"line": None, "po_line": 1, "po_number": "4500123"}) == "PO 4500123 line 1"


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
    assert "Designed to show" not in html  # brief v2: no design notes on the comparison
    assert ">duplicate posting<" in html and ">Block<" in html and ">credit note linked<" in html
    # the four metrics, A vs B, each with its deck tag; the definition with both scenarios' own numbers
    assert html.count("kpi-tile ab-tile kpi-hero") == 4 and html.count("kpi-tile ab-tile kpi-mini") == 1
    tile = html[html.index('id="cmp-touchless_rate"'):]
    tile = tile[:tile.index('class="kpi-tile')]
    assert "0.0%" in tile and "72.7%" in tile and "side-tobe better" in tile
    assert ">Downstream · automation efficiency<" in tile and "A: 0 of 12 · B: 8 of 11." in tile
    first_pass = html[html.index('id="cmp-first_pass_match_rate"'):]
    first_pass = first_pass[:first_pass.index('class="kpi-tile')]
    assert "27.3%" in first_pass and "72.7%" in first_pass and "A: 3 of 11 · B: 8 of 11." in first_pass
    accounts = html[html.index('id="cmp-accounts_per_supplier"'):]
    assert "2.33" in accounts[:1500] and "1.17" in accounts[:1500] and "A: 28 ÷ 12 · B: 14 ÷ 12." in accounts
    assert "Cycle time, shown both ways" not in html and "Reference path" not in html


def test_rerun_gate_htmx_partial_and_no_js_fallback(client, session):
    load_documents(client, "tobe")
    r = client.post("/invoice/B-06/rerun", headers={"HX-Request": "true"})  # not processed yet: runs it
    assert r.status_code == 200 and 'id="gate-panel"' in r.text and "<footer" not in r.text
    # the Gemini output panel above is refreshed too (out of band): the run may have read the document first
    assert '<article class="panel" id="extraction-panel" hx-swap-oob="true">' in r.text
    assert r.text.index('id="gate-panel"') < r.text.index('id="extraction-panel"') and "FIXTURE" in r.text
    assert "Control gate: Exception: Price or quantity mismatch, owner Sofia Brandt, SLA 2 days." in r.text
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
    r = client.post("/invoice/B-05/rerun", headers={"HX-Request": "true"})
    assert r.status_code == 200
    session.expire_all()
    docs = sorted(session.scalars(select(InboundDocument).where(InboundDocument.scenario == "tobe")),
                  key=gate.order_key)
    ids = [d.doc_id for d in docs]
    earlier = ids[:ids.index("B-05")]
    processed = set(session.scalars(select(GateDecision.doc_id).where(GateDecision.scenario == "tobe")))
    assert earlier and processed == set(earlier) | {"B-05"}  # the earlier ones, then B-05; nothing later
    assert "B-11" not in processed  # SecureNet arrives after FitOut
    text = squash(page_text(r.text))
    assert (f"{len(earlier)} earlier documents not processed yet went first, in processing order: "
            f"{', '.join(earlier)}.") in text
    assert "Exception: PO exists, no receipt or confirmation" in text


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


def test_pending_invoices_show_flag_chips_and_no_totals(client, session):
    run_scenario(client, "asis")
    html = client.get("/erp/pending-invoices").text
    assert '<a href="/invoice/A-01" class="chip chip-bad">duplicate of A-01</a>' in html
    assert ">wrong entity<" in html and ">unapplied credit<" in html and ">terms: invoice<" in html
    # brief v2: no value aggregates (no totals per currency), the document amounts stay
    assert "pvi-totals" not in html and "<tfoot>" not in html and "Totals per currency" not in html
    rows = session.scalars(select(PendingVendorInvoice).where(PendingVendorInvoice.scenario == "asis")).all()
    for r in rows:
        assert main.fmt_money(r.total) in html, r.invoice_id
    run_scenario(client, "tobe")
    html = client.get("/erp/pending-invoices").text
    assert ">card / catalogue<" in html
    assert ">terms: master<" in html and ">credit applied<" in html
    assert ">terms: invoice<" not in html and "duplicate of" not in html and "DoA" not in html


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
    assert any(len(lines) > 1 for lines in exceptions["lines"])  # e.g. "PO exists, no receipt or confirmation"
    assert "labels: data.exceptions.lines" in html
    assert main.label_lines("Supercalifragilisticexpialidocious-and-more") == [
        "Supercalifragilisticexpialidocious-and-more"]  # a single long word is never split


def test_load_documents_clears_previous_gate_results(client, session):
    run_scenario(client, "tobe")
    load_documents(client, "tobe")
    session.expire_all()
    assert count_rows(session, GateDecision, "tobe") == 0 and count_rows(session, Run, "tobe") == 0


def test_live_resend_of_a_sample_invoice_is_blocked_and_aged_to_today(client, session, bucket_dir):
    """A live document (real date) is processed after the loaded sample set (simulated dates): resending the v2
    UBL invoice 11 through the webhook is caught as a duplicate of B2-11, and live rows are aged to today."""
    use_scenario(client, "tobe")
    assert client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"}).status_code == 200
    assert client.post("/run", data={"scenario": "tobe"}).status_code == 200
    r = post_webhook(client, content=ubl_invoice(11), filename="einvoice.xml", scenario="tobe",
                     message_id="<resend-11@test>")
    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "blocked_duplicate"
    decision = session.scalar(select(GateDecision).where(GateDecision.doc_id == r.json()["doc_id"]))
    assert decision.details["duplicate_of"] == "B2-11"

    body_only = post_webhook(client, content=None, scenario="tobe", email_body="Invoice 77 for 120.00 EUR",
                             message_id="<body-77@test>")
    assert body_only.json()["outcome"] == "human_review"
    html = client.get("/gate/exceptions").text
    assert "aged to today" in " ".join(page_text(html).split())


# --------------------------------------------------------------------------------------------
# Review round 3: received files, live documents, exports, messages
# --------------------------------------------------------------------------------------------

XHTML_SCRIPT = b'<h:script xmlns:h="http://www.w3.org/1999/xhtml">alert(document.domain)</h:script>'


def test_a_ubl_carrying_an_xhtml_script_is_never_served_as_xml_or_html(client, session, bucket_dir):
    """Stored XSS: an emailed e-invoice with an XHTML <script> must not run in the app's origin. The file is served
    as plain text in a CSP sandbox with no content sniffing (or the webhook refuses it outright)."""
    content = ubl_invoice(11)
    evil = content.replace(b"<cbc:ID>", XHTML_SCRIPT + b"<cbc:ID>", 1)
    assert evil != content
    r = post_webhook(client, content=evil, filename="evil.xml", scenario="tobe")
    assert r.status_code in (201, 400), r.text
    if r.status_code == 400:
        return  # refused as not a UBL e-invoice: nothing stored, nothing served
    doc_id = r.json()["doc_id"]
    for url in (f"/files/{doc_id}.xml", f"/files/{doc_id}"):
        served = client.get(url)
        assert served.status_code == 200, url
        content_type = served.headers["content-type"]
        assert content_type == "text/plain; charset=utf-8", (url, content_type)
        assert "xml" not in content_type and "html" not in content_type
        assert served.headers["x-content-type-options"] == "nosniff"
        csp = served.headers["content-security-policy"]
        assert "sandbox" in csp and "default-src 'none'" in csp
    html = client.get(f"/invoice/{doc_id}").text  # the invoice page shows the XML escaped, never as markup
    assert "<h:script" not in html and "&lt;h:script" in html


def test_live_resend_is_blocked_even_when_the_sample_set_was_not_run(client, session, bucket_dir, capsys):
    """K1 without a run: the loaded sample documents without a decision are processed first, in processing order,
    so resending the v2 UBL invoice 11 is caught as a duplicate of B2-11 (never posted a second time)."""
    use_scenario(client, "tobe")
    assert client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"}).status_code == 200
    assert count_rows(session, GateDecision, "tobe") == 0
    capsys.readouterr()
    r = post_webhook(client, content=ubl_invoice(11), filename="einvoice.xml", scenario="tobe",
                     message_id="<resend-11-norun@test>")
    assert r.status_code == 201, r.text
    assert (r.json()["outcome"], r.json()["exception_type"]) == ("blocked_duplicate", "duplicate_invoice")
    decision = session.scalar(select(GateDecision).where(GateDecision.doc_id == r.json()["doc_id"]))
    assert decision.details["duplicate_of"] == "B2-11"
    n = len(world.documents_for("v2"))
    assert count_rows(session, GateDecision, "tobe") == n + 1  # every sample document was decided first
    postings = session.scalars(select(PendingVendorInvoice.doc_id).where(
        PendingVendorInvoice.scenario == "tobe", PendingVendorInvoice.invoice_number == "MM-2026-248")).all()
    assert postings == ["B2-11"]
    assert f"[intake] {n} earlier documents not processed yet went first, in processing order: B2-" in (
        capsys.readouterr().out)


def test_asis_live_documents_are_not_registered_yet_and_reported_apart_in_the_cockpit(client, session,
                                                                                         bucket_dir):
    """As-is live documents: registration day ahead (shown as not registered yet), their own seeded email-loop
    length, and not counted in the simulated snapshot of the cockpit."""
    run_scenario(client, "asis")
    lead = squash(page_text(client.get("/gate/exceptions").text))
    before = re.search(r"(\d+) still in the loop that evening, (\d+) already posted", lead).groups()
    ids = [post_webhook(client, scenario="asis", subject=f"Scan {i}").json()["doc_id"] for i in (1, 2)]
    assert ids == ["A-W01", "A-W02"]
    session.expire_all()
    for doc_id in ids:
        decision = session.scalar(select(GateDecision).where(GateDecision.doc_id == doc_id))
        assert decision.exception_type == "email_loop"
        assert decision.details["email_loop_days"] == sim.email_loop_days(doc_id)  # seeded by its own id
        assert decision.steps[0]["detail"].startswith("Not registered yet: expected 1 business day after receipt")
        assert get_doc(session, doc_id).registered_on > datetime.now()
        page = squash(page_text(client.get(f"/invoice/{doc_id}").text))
        assert "Registered Not registered yet — expected" in page
    inbox = client.get("/inbox").text
    for doc_id in ids:
        start = inbox.index(f'id="doc-{doc_id}"')
        assert "Not registered yet" in inbox[start:inbox.index("</article>", start)]
    lead = squash(page_text(client.get("/gate/exceptions").text))
    after = re.search(r"(\d+) still in the loop that evening, (\d+) already posted", lead).groups()
    assert after == before  # the snapshot counts the sample documents only
    assert ("2 of them were received live through the intake webhook: they carry real dates, are not part of the "
            "snapshot and are aged to today.") in lead


def test_every_change_of_the_gate_results_is_exported(client, session, bucket_dir, monkeypatch, capsys):
    export_bq = pytest.importorskip("app.export_bq")
    calls = []
    monkeypatch.setattr(export_bq, "export_all", lambda db_session: calls.append(1) or {"gate_decision": 1})
    monkeypatch.setattr(config, "BQ_EXPORT", True)
    run_scenario(client, "tobe")
    assert post_webhook(client, scenario="tobe").status_code == 201
    assert client.post("/invoice/B-03/rerun", data={}).status_code == 200
    assert client.post("/reset", data={"scenario": "tobe"}).status_code == 200
    out = capsys.readouterr().out
    for event in ("run", "intake of B-W01", "re-run of B-03", "reset"):
        assert f"[export] after the tobe {event}: 1 rows in 1 tables" in out, event
    assert "[export] after the asis reset: 1 rows in 1 tables" in out  # Reset demo re-runs both scenarios
    assert len(calls) == 5

    def broken(db_session):
        raise RuntimeError("BigQuery unreachable")
    monkeypatch.setattr(export_bq, "export_all", broken)
    r = post_webhook(client, scenario="tobe")  # never fatal: the document is still stored and gated
    assert r.status_code == 201 and r.json()["outcome"] == "human_review"
    assert client.post("/invoice/B-03/rerun", data={}).status_code == 200
    assert client.post("/reset", data={"scenario": "tobe"}).status_code == 200
    assert "[export] after the tobe reset FAILED: RuntimeError('BigQuery unreachable')" in capsys.readouterr().out


def test_extraction_messages_name_the_ubl_parser_and_never_none_ms(client, session, monkeypatch):
    client.post("/documents/load", data={"scenario": "tobe", "dataset": "v2"})
    for force in (0, 1):
        r = client.post(f"/invoice/B2-11/extract?force={force}", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "Parsed the UBL e-invoice (structured XML, no model call)." in r.text
        assert "None ms" not in r.text and "via the Gemini API" not in r.text
    doc = get_doc(session, "B2-01")

    def fake_row(latency_ms, from_cache=False):
        return lambda *args, **kwargs: type("Row", (), {"model": "gemini-test", "from_cache": from_cache,
                                                        "latency_ms": latency_ms})()
    monkeypatch.setattr(extract, "extract_document", fake_row(None))
    assert main.run_extraction(session, doc, force=False) == (
        "info", "Extracted with gemini-test (via the Gemini API).")
    monkeypatch.setattr(extract, "extract_document", fake_row(850))
    assert main.run_extraction(session, doc, force=False) == (
        "info", "Extracted with gemini-test (via the Gemini API in 850 ms).")
    monkeypatch.setattr(extract, "extract_document", fake_row(None, from_cache=True))
    assert main.run_extraction(session, doc, force=False) == ("info", "Extracted with gemini-test (from cache).")


def test_reset_confirm_says_what_the_demo_reset_does(client):
    html = client.get("/inbox").text
    reset = html[html.index('action="/reset"'):]
    reset = html_lib.unescape(reset[:reset.index(">")])
    assert ("Reset the demo? Both scenarios are re-seeded, the 12 case documents are loaded and run offline, and "
            "one email waits to be received.") in reset
    assert "live intake" not in html.lower() and "received by email" not in html.lower()


def test_log_lines_print_on_a_legacy_code_page(monkeypatch):
    """Found in the demo walkthrough: with the server output redirected on Windows (cp1252), printing a rule-log line
    with "→" raised UnicodeEncodeError and "Run the control gate" returned a 500. config.utf8_output fixes the
    streams at start-up."""
    import io

    raw = io.BytesIO()
    legacy = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    with pytest.raises(UnicodeEncodeError):
        print("[B-05] Commitment → Exception — ≈ CHF", file=legacy)
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    config.utf8_output(legacy)
    print("[B-05] Commitment → Exception — ≈ CHF", file=legacy)
    legacy.flush()
    assert legacy.buffer.getvalue().decode("utf-8").splitlines() == ["[B-05] Commitment → Exception — ≈ CHF"]
    config.utf8_output(object())  # anything that is not a text stream is left alone


def test_startup_prints_the_columns_an_older_database_gets(session, monkeypatch, capsys):
    """db.init_db returns the columns it added to an older database; the startup prints them once."""
    from app import db, models

    calls = []

    def add_missing_columns(engine):
        calls.append(engine)
        return ["inbound_document.source_name"]
    monkeypatch.setattr(models, "add_missing_columns", add_missing_columns)
    assert db.init_db() == ["inbound_document.source_name"]
    capsys.readouterr()
    with TestClient(main.app):
        pass
    out = capsys.readouterr().out
    assert out.count("[startup] database upgraded: added inbound_document.source_name") == 1
    assert len(calls) == 2  # once per init_db: the startup does not add the columns a second time
