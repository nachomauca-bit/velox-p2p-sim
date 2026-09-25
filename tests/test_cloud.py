"""Phase 3 (GCP) plumbing, tested offline: settings, the Vertex AI client and model discovery, routing of
UBL e-invoices and email bodies, the v2 fixture folder and basic auth. Nothing here reaches Google Cloud: the
SDK client is a recorder or runs on an in-process httpx.MockTransport, and settings from the environment are
read in a child process so this process's modules are never reloaded."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from google.oauth2.credentials import Credentials

from app import auth, config, extract, world
from app.auth import BasicAuthMiddleware, parse_basic
from app.extract import FIXTURE_MODEL, ExtractionUnavailable, InvoiceExtraction
from app.models import InboundDocument

GEN = ["generateContent", "countTokens"]

# Every setting read from the environment by app/config.py (cleared in the child process unless overridden).
CONFIG_VARS = (
    "DATA_DIR", "INBOUND_DIR", "CACHE_DIR", "EXPORT_DIR", "DATABASE_URL", "GEMINI_API_KEY", "GEMINI_MODEL",
    "GEMINI_BACKEND", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "EXTRACTOR", "APP_USERNAME", "APP_PASSWORD",
    "BQ_EXPORT", "BQ_PROJECT", "BQ_DATASET",
)

_DUMP_CONFIG = """
import json
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False  # a developer's .env must not leak into the test
from app import config
paths = ("DATA_DIR", "INVOICES_DIR", "INVOICES_V2_DIR", "INBOUND_DIR", "CACHE_DIR", "EXPORT_DIR", "FIXTURES_DIR",
         "FIXTURES_V2_DIR")
out = {name: str(getattr(config, name)) for name in paths}
for name in ("DATABASE_URL", "GEMINI_BACKEND", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GEMINI_MODEL",
             "APP_USERNAME", "APP_PASSWORD", "BQ_EXPORT", "BQ_PROJECT", "BQ_DATASET", "EXTRACTOR"):
    out[name] = getattr(config, name)
out["configured"] = config.gemini_configured()
out["missing"] = config.gemini_missing_setting()
print(json.dumps(out))
"""


def config_in_child(**env: str) -> dict:
    """app.config as a fresh process sees it with only these variables set (plus the OS basics)."""
    child_env = {k: v for k, v in os.environ.items() if k not in CONFIG_VARS}
    child_env.update(env)
    done = subprocess.run([sys.executable, "-c", _DUMP_CONFIG], cwd=config.BASE_DIR, env=child_env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------------------------
# C1 config: defaults and environment overrides
# --------------------------------------------------------------------------------------------


def test_config_defaults_are_local_and_offline():
    cfg = config_in_child()
    base = config.BASE_DIR
    assert cfg["DATA_DIR"] == str(base / "data")
    assert cfg["INVOICES_DIR"] == str(base / "data" / "invoices")
    assert cfg["INVOICES_V2_DIR"] == str(base / "data" / "invoices_v2")
    assert cfg["INBOUND_DIR"] == str(base / "data" / "invoices" / "inbound")
    assert cfg["CACHE_DIR"] == str(base / "data" / "cache")
    assert cfg["EXPORT_DIR"] == str(base / "data" / "export")
    assert cfg["FIXTURES_DIR"] == str(base / "tests" / "fixtures")
    assert cfg["FIXTURES_V2_DIR"] == str(base / "tests" / "fixtures_v2")
    assert cfg["DATABASE_URL"] == f"sqlite:///{(base / 'data' / 'velox.db').as_posix()}"
    assert (cfg["GEMINI_BACKEND"], cfg["GOOGLE_CLOUD_PROJECT"], cfg["GOOGLE_CLOUD_LOCATION"]) == ("aistudio", "", "global")
    assert cfg["GEMINI_MODEL"] == "gemini-2.5-flash" and cfg["EXTRACTOR"] == "gemini"
    assert cfg["configured"] is False and cfg["missing"] == "GEMINI_API_KEY"
    assert (cfg["APP_USERNAME"], cfg["APP_PASSWORD"]) == ("velox", "")  # basic auth off
    assert (cfg["BQ_EXPORT"], cfg["BQ_PROJECT"], cfg["BQ_DATASET"]) == (False, "", "velox_p2p")


def test_config_reads_the_cloud_run_environment(tmp_path):
    mount = tmp_path / "gcs"
    cfg = config_in_child(
        INBOUND_DIR=str(mount / "inbound"), CACHE_DIR=str(mount / "cache"), EXPORT_DIR=str(mount / "export"),
        DATABASE_URL="sqlite:////tmp/velox.db", GEMINI_BACKEND=" Vertex ", GOOGLE_CLOUD_PROJECT="velox-demo",
        GOOGLE_CLOUD_LOCATION="europe-west1", APP_USERNAME="demo", APP_PASSWORD="s3cret\n", BQ_EXPORT="true",
    )
    assert cfg["INBOUND_DIR"] == str(mount / "inbound")  # outside the project: a mounted bucket
    assert cfg["CACHE_DIR"] == str(mount / "cache") and cfg["EXPORT_DIR"] == str(mount / "export")
    assert cfg["INVOICES_DIR"] == str(config.BASE_DIR / "data" / "invoices")  # the samples ship with the app
    assert cfg["DATABASE_URL"] == "sqlite:////tmp/velox.db"
    assert (cfg["GEMINI_BACKEND"], cfg["GOOGLE_CLOUD_PROJECT"], cfg["GOOGLE_CLOUD_LOCATION"]) == (
        "vertex", "velox-demo", "europe-west1")
    assert cfg["configured"] is True  # Vertex AI with a project: no API key needed
    assert (cfg["APP_USERNAME"], cfg["APP_PASSWORD"]) == ("demo", "s3cret")  # a secret's trailing newline is dropped
    assert (cfg["BQ_EXPORT"], cfg["BQ_PROJECT"], cfg["BQ_DATASET"]) == (True, "velox-demo", "velox_p2p")


def test_config_relative_paths_are_taken_from_the_project_root():
    cfg = config_in_child(DATA_DIR="alt-data", CACHE_DIR="alt-cache", BQ_PROJECT="kpi-project", BQ_DATASET="kpis",
                          BQ_EXPORT="0")
    assert cfg["DATA_DIR"] == str(config.BASE_DIR / "alt-data")
    assert cfg["INVOICES_V2_DIR"] == str(config.BASE_DIR / "alt-data" / "invoices_v2")
    assert cfg["EXPORT_DIR"] == str(config.BASE_DIR / "alt-data" / "export")
    assert cfg["CACHE_DIR"] == str(config.BASE_DIR / "alt-cache")
    assert (cfg["BQ_EXPORT"], cfg["BQ_PROJECT"], cfg["BQ_DATASET"]) == (False, "kpi-project", "kpis")


def test_unknown_backend_falls_back_to_ai_studio_with_a_warning():
    child_env = {k: v for k, v in os.environ.items() if k not in CONFIG_VARS}
    child_env["GEMINI_BACKEND"] = "openai"
    done = subprocess.run([sys.executable, "-c", _DUMP_CONFIG], cwd=config.BASE_DIR, env=child_env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "unknown GEMINI_BACKEND='openai'" in done.stdout
    assert json.loads(done.stdout.strip().splitlines()[-1])["GEMINI_BACKEND"] == "aistudio"


@pytest.mark.parametrize("backend, project, key, configured, missing", [
    ("aistudio", "", "", False, "GEMINI_API_KEY"),
    ("aistudio", "", "k", True, "GEMINI_API_KEY"),
    ("aistudio", "velox-demo", "", False, "GEMINI_API_KEY"),  # a project alone is not AI Studio access
    ("vertex", "", "", False, "GOOGLE_CLOUD_PROJECT"),
    ("vertex", "velox-demo", "", True, "GOOGLE_CLOUD_PROJECT"),
    ("vertex", "", "k", True, "GOOGLE_CLOUD_PROJECT"),  # Vertex AI express mode (API key)
])
def test_gemini_configured_for_both_backends(monkeypatch, backend, project, key, configured, missing):
    monkeypatch.setattr(config, "GEMINI_BACKEND", backend)
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", project)
    monkeypatch.setattr(config, "GEMINI_API_KEY", key)
    assert config.gemini_configured() is configured
    assert config.gemini_missing_setting() == missing


def test_backend_label(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "velox-demo")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_LOCATION", "global")
    assert config.gemini_backend_label() == "Vertex AI (project velox-demo, location global)"
    monkeypatch.setattr(config, "GEMINI_BACKEND", "aistudio")
    assert config.gemini_backend_label() == "Google AI Studio"


# --------------------------------------------------------------------------------------------
# C2 extraction: Vertex AI client, model discovery, error classification
# --------------------------------------------------------------------------------------------


class RecordingClient:
    """Stands in for genai.Client: records the constructor arguments, never talks to the network."""

    created: list[dict] = []

    def __init__(self, **kwargs):
        RecordingClient.created.append(kwargs)


@pytest.fixture()
def recorded_clients(monkeypatch) -> list[dict]:
    RecordingClient.created = []
    monkeypatch.setattr(extract.genai, "Client", RecordingClient)
    return RecordingClient.created


def test_vertex_client_uses_project_and_location(monkeypatch, recorded_clients):
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "velox-demo")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_LOCATION", "europe-west1")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "ignored-on-vertex")
    extract._client()
    (kwargs,) = recorded_clients
    assert kwargs["vertexai"] is True
    assert (kwargs["project"], kwargs["location"]) == ("velox-demo", "europe-west1")
    assert "api_key" not in kwargs  # service-account credentials (ADC), never the AI Studio key
    assert kwargs["http_options"].timeout == extract.REQUEST_TIMEOUT_MS


def test_vertex_without_project_uses_express_mode(monkeypatch, recorded_clients):
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "vertex-key")
    extract._client()
    (kwargs,) = recorded_clients
    assert kwargs["vertexai"] is True and kwargs["api_key"] == "vertex-key"
    assert "project" not in kwargs and "location" not in kwargs


def test_ai_studio_client_uses_the_api_key(monkeypatch, recorded_clients):
    monkeypatch.setattr(config, "GEMINI_BACKEND", "aistudio")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "velox-demo")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "studio-key")
    extract._client()
    (kwargs,) = recorded_clients
    assert kwargs["api_key"] == "studio-key" and "vertexai" not in kwargs and "project" not in kwargs


def test_the_installed_sdk_accepts_the_vertex_arguments(monkeypatch):
    """A real client object (no request is made): project/location are valid for vertexai=True."""
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "velox-demo")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_LOCATION", "global")
    client = extract._client()
    assert client.vertexai is True
    assert client._api_client.project == "velox-demo" and client._api_client.location == "global"


def vertex_models(*entries) -> list[SimpleNamespace]:
    """models.list() entries as Vertex AI returns them: publisher names, no supported actions."""
    return [SimpleNamespace(name=f"publishers/google/models/{name}", supported_actions=None) for name in entries]


class ListingClient:
    def __init__(self, listed):
        self.models = SimpleNamespace(list=lambda: iter(listed))


def test_model_discovery_accepts_vertex_publisher_names():
    listed = vertex_models("gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
                           "gemini-3.8-flash-preview-09-2026", "gemini-3.5-flash", "imagen-4.0-generate-001")
    assert extract._discover_flash_models(ListingClient(listed)) == [
        "gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.0-flash"]


def test_model_discovery_still_filters_ai_studio_actions():
    listed = [SimpleNamespace(name="models/gemini-3.5-flash", supported_actions=GEN),
              SimpleNamespace(name="models/gemini-4.0-flash", supported_actions=["countTokens"]),
              SimpleNamespace(name="publishers/google/models/gemini-3.5-flash", supported_actions=None)]  # dedup
    assert extract._discover_flash_models(ListingClient(listed)) == ["gemini-3.5-flash"]


def vertex_error(code: int, status: str, message: str) -> genai_errors.APIError:
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


@pytest.mark.parametrize("error, unavailable", [
    (vertex_error(404, "NOT_FOUND", "Publisher Model `projects/velox-demo/locations/global/publishers/google/"
                                    "models/gemini-9.9-flash` was not found or your project does not have access "
                                    "to it."), True),
    (vertex_error(403, "PERMISSION_DENIED", "Permission 'aiplatform.endpoints.predict' denied on resource "
                                            "'//aiplatform.googleapis.com/projects/velox-demo/locations/global/"
                                            "publishers/google/models/gemini-2.5-flash' (or it may not exist)."),
     False),  # a missing IAM role: falling back to another model cannot help
    (vertex_error(403, "PERMISSION_DENIED", "Vertex AI API has not been used in project velox-demo before or it "
                                            "is disabled. Enable it by visiting https://console.developers.google"
                                            ".com/apis/api/aiplatform.googleapis.com/overview"), False),
    (vertex_error(403, "PERMISSION_DENIED", "Permission denied on model gemini-2.5-flash."), True),  # AI Studio
])
def test_vertex_errors_are_classified(error, unavailable):
    assert extract._is_model_unavailable(error) is unavailable


def test_vertex_fallback_through_the_installed_sdk_on_a_mock_transport(monkeypatch, capsys):
    """The real google-genai client in Vertex AI mode (static test credentials, in-process transport): the
    configured model is rejected, the publisher model list is read, and the newest GA Flash model is used."""
    record = json.loads((config.FIXTURES_DIR / "03_bright_agency_invoice.json").read_text(encoding="utf-8"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/publishers/google/models"):
            return httpx.Response(200, json={"publisherModels": [
                {"name": "publishers/google/models/gemini-3.5-flash"},
                {"name": "publishers/google/models/gemini-3.8-flash"},
                {"name": "publishers/google/models/gemini-3.8-flash-lite"}]})
        if path.endswith("/models/gemini-2.5-flash:generateContent"):
            return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND", "message": (
                "Publisher Model `projects/velox-demo/locations/global/publishers/google/models/gemini-2.5-flash` "
                "was not found or your project does not have access to it.")}})
        if path.endswith("/models/gemini-3.8-flash:generateContent"):
            return httpx.Response(200, json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(record["extraction"])}]},
                                "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1834, "candidatesTokenCount": 612}})
        return httpx.Response(500, json={"error": {"code": 500, "status": "INTERNAL", "message": f"unexpected {path}"}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(extract, "_client", lambda: genai.Client(
        vertexai=True, project="velox-demo", location="global", credentials=Credentials(token="test-token"),
        http_options=genai_types.HttpOptions(httpx_client=http)))
    monkeypatch.setattr(extract, "_resolved_model", None)
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")

    result = extract.call_gemini(b"%PDF-1.4 fake", "03_bright_agency_invoice.pdf")

    assert result.model == "gemini-3.8-flash" and result.backend == "vertex"
    assert result.data["po_numbers"]["value"] == ["4500117"]
    first = requests[0].url.path
    assert "/projects/velox-demo/locations/global/publishers/google/models/gemini-2.5-flash:generateContent" in first
    assert requests[1].method == "GET" and requests[1].url.path.endswith("/publishers/google/models")
    assert all(r.headers["authorization"] == "Bearer test-token" for r in requests)
    assert all("x-goog-api-key" not in r.headers for r in requests)
    assert "backend=vertex" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# C2 extraction: routing by file suffix, v2 fixtures
# --------------------------------------------------------------------------------------------


def fixture_record(stem: str = "03_bright_agency_invoice") -> dict:
    return json.loads((config.FIXTURES_DIR / f"{stem}.json").read_text(encoding="utf-8"))


class FakeUbl:
    """Stands in for app/ubl.py: records the bytes it parsed and returns a ground-truth extraction."""

    UBL_MODEL = "UBL e-invoice (parsed, no model call)"

    def __init__(self, data: dict | None = None, error: Exception | None = None):
        self.data = data if data is not None else fixture_record()["extraction"]
        self.error = error
        self.parsed: list[bytes] = []

    def parse_ubl(self, data: bytes) -> dict:
        self.parsed.append(data)
        if self.error:
            raise self.error
        return json.loads(json.dumps(self.data))


@pytest.fixture()
def fake_ubl(monkeypatch) -> FakeUbl:
    ubl = FakeUbl()
    monkeypatch.setattr(extract, "_ubl_module", lambda: ubl)
    return ubl


@pytest.fixture()
def no_api(monkeypatch):
    """A model call fails the test (the routed files must never reach it)."""
    def refuse(*args, **kwargs):
        raise AssertionError("this file must not go to the model")

    monkeypatch.setattr(extract, "call_gemini", refuse)
    monkeypatch.setattr(extract, "_client", refuse)


@pytest.mark.parametrize("mode", ["fixture", "gemini"])
def test_xml_is_parsed_as_ubl_without_model_or_cache(tmp_path, monkeypatch, tmp_cache_dir, fake_ubl, no_api, mode):
    monkeypatch.setattr(config, "EXTRACTOR", mode)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    xml = tmp_path / "11_metro_media_einvoice.xml"
    xml.write_bytes(b"<Invoice>fake</Invoice>")

    result = extract.extract_file(xml)

    assert fake_ubl.parsed == [b"<Invoice>fake</Invoice>"]
    assert result.model == FakeUbl.UBL_MODEL and result.source == "ubl" and result.is_ubl
    assert result.from_cache is False and not result.is_fixture and result.backend is None
    assert result.data == InvoiceExtraction.model_validate(fake_ubl.data).model_dump(mode="json")
    assert list(tmp_cache_dir.iterdir()) == []  # never cached
    assert extract.extract_file(xml, force=True, allow_api=False).source == "ubl"  # no API needed at all


def test_ubl_result_is_postprocessed(tmp_path, monkeypatch, fake_ubl):
    fake_ubl.data["notes"] = {"value": "  ", "confidence": 1.0}
    fake_ubl.data["supplier_name"]["confidence"] = 1.4
    xml = tmp_path / "x.xml"
    xml.write_bytes(b"<Invoice/>")
    data = extract.extract_file(xml).data
    assert data["notes"] == {"value": None, "confidence": 0.0}
    assert data["supplier_name"]["confidence"] == 1.0


@pytest.mark.parametrize("error", [ValueError("not well-formed XML: syntax error: line 1, column 0"),
                                   SyntaxError("parse error")])
def test_unreadable_ubl_is_unavailable(tmp_path, monkeypatch, error):
    monkeypatch.setattr(extract, "_ubl_module", lambda: FakeUbl(error=error))
    xml = tmp_path / "broken.xml"
    xml.write_bytes(b"<Invoice")
    with pytest.raises(ExtractionUnavailable, match="broken.xml is not a readable UBL e-invoice"):
        extract.extract_file(xml)


def test_ubl_not_matching_the_schema_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "_ubl_module", lambda: FakeUbl(data={"doc_type": {"value": "invoice"}}))
    xml = tmp_path / "odd.xml"
    xml.write_bytes(b"<Invoice/>")
    with pytest.raises(ExtractionUnavailable, match="not a readable UBL e-invoice: .*validation error"):
        extract.extract_file(xml)


def test_missing_xml_is_unavailable(tmp_path, fake_ubl):
    with pytest.raises(ExtractionUnavailable, match="gone.xml not found"):
        extract.extract_file(tmp_path / "gone.xml")
    assert fake_ubl.parsed == []


@pytest.mark.parametrize("mode", ["fixture", "gemini"])
def test_email_body_is_unavailable_in_every_mode(tmp_path, monkeypatch, no_api, mode):
    monkeypatch.setattr(config, "EXTRACTOR", mode)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    body = tmp_path / "14_kaffee_email_body.txt"
    body.write_text("Guten Tag, anbei unsere Rechnung 2026/140 ...", encoding="utf-8")
    with pytest.raises(ExtractionUnavailable) as info:
        extract.extract_file(body)
    assert str(info.value) == "no document attached: the invoice is only in the email body"


def make_doc(session, file_path: str, doc_id: str = "B-W01", file_hash: str = "0" * 64) -> InboundDocument:
    spec = world.DOCUMENT_BY_NO[3]
    doc = InboundDocument(
        doc_id=doc_id, scenario="tobe", sample_no=0, channel="ap_mailbox", mailbox=spec.mailbox,
        received_on=spec.received_on, file_path=file_path, file_hash=file_hash, sender_email=spec.sender_email,
        subject="test", registered=True, registered_on=spec.received_on, doc_type="unknown")
    session.add(doc)
    session.commit()
    return doc


def test_extract_document_stores_a_ubl_result(session, tmp_path, fake_ubl, no_api):
    xml = tmp_path / "abc.xml"
    xml.write_bytes(b"<Invoice/>")
    doc = make_doc(session, str(xml))  # absolute path: BASE_DIR / path keeps it (INBOUND_DIR outside the project)
    row = extract.extract_document(session, doc)
    assert row is not None and row.model == FakeUbl.UBL_MODEL and row.from_cache is False
    assert row.latency_ms is None and row.input_tokens is None  # no model call, no tokens
    assert doc.doc_type == "invoice" and row.json["invoice_number"]["value"] == "INV-2026-0457"


def test_extract_document_on_an_email_body_returns_none(session, tmp_path, no_api, capsys):
    body = tmp_path / "abc.txt"
    body.write_text("invoice in the text", encoding="utf-8")
    doc = make_doc(session, str(body))
    assert extract.extract_document(session, doc) is None
    assert doc.extraction is None and doc.doc_type == "unknown"
    assert f"doc={doc.doc_id} unavailable: no document attached" in capsys.readouterr().out


def test_real_ubl_module_round_trip_through_extract_file(tmp_path, no_api):
    """With the INTAKE agent's app/ubl.py and the DATASET agent's world_v2: document 11 is parsed, not modelled."""
    ubl = pytest.importorskip("app.ubl")
    world_v2 = pytest.importorskip("app.world_v2")
    spec = world_v2.DOCUMENT_V2_BY_NO[11]
    xml = tmp_path / spec.filename
    xml.write_bytes(ubl.render_ubl(spec))
    result = extract.extract_file(xml)
    assert result.source == "ubl" and result.model == ubl.UBL_MODEL
    assert result.data["invoice_number"]["value"] == spec.invoice_number
    assert result.data["gross_total"] == {"value": spec.gross_total, "confidence": 1.0}


def test_fixture_path_uses_the_v2_folder_for_test_set_v2():
    v2 = config.INVOICES_V2_DIR / "12_fitout_scan.pdf"
    assert extract.fixture_path(v2) == config.FIXTURES_V2_DIR / "12_fitout_scan.json"
    assert extract.fixture_path(Path("data") / "invoices_v2" / "12_fitout_scan.pdf") == (
        config.FIXTURES_V2_DIR / "12_fitout_scan.json")  # relative to the project root, like doc.file_path
    v1 = config.INVOICES_DIR / "03_bright_agency_invoice.pdf"
    assert extract.fixture_path(v1) == config.FIXTURES_DIR / "03_bright_agency_invoice.json"
    assert extract.fixture_path(config.INBOUND_DIR / "0123456789abcdef.pdf").parent == config.FIXTURES_DIR


def test_fixture_mode_reads_v2_fixtures(tmp_path, monkeypatch):
    invoices_v2, fixtures_v2 = tmp_path / "invoices_v2", tmp_path / "fixtures_v2"
    fixtures_v2.mkdir()
    record = fixture_record()
    record["extraction"]["invoice_number"] = {"value": "V2-TEST-1", "confidence": 0.93}
    (fixtures_v2 / "12_fitout_scan.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(config, "INVOICES_V2_DIR", invoices_v2)
    monkeypatch.setattr(config, "FIXTURES_V2_DIR", fixtures_v2)

    result = extract.extract_file(invoices_v2 / "12_fitout_scan.pdf")
    assert result.is_fixture and result.model == FIXTURE_MODEL
    assert result.data["invoice_number"] == {"value": "V2-TEST-1", "confidence": 0.93}
    with pytest.raises(ExtractionUnavailable, match="no fixture for 99_missing.pdf"):
        extract.extract_file(invoices_v2 / "99_missing.pdf")


def test_missing_vertex_project_is_reported_by_name(tmp_path, monkeypatch, tmp_cache_dir, no_api):
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ExtractionUnavailable) as info:
        extract.extract_file(pdf)
    assert str(info.value) == "GOOGLE_CLOUD_PROJECT is not set (GEMINI_BACKEND=vertex): add it to .env (see .env.example)"


def test_vertex_with_a_project_calls_the_model_without_a_key(tmp_path, monkeypatch, tmp_cache_dir):
    """config.gemini_configured(), not GEMINI_API_KEY, decides whether extract_file may call the API."""
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "velox-demo")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    calls: list[str] = []

    def fake_call(pdf_bytes: bytes, file_name: str) -> extract.ExtractionResult:
        calls.append(file_name)
        return extract._result_from_record(fixture_record(), from_cache=False, source="gemini")

    monkeypatch.setattr(extract, "call_gemini", fake_call)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    assert extract.extract_file(pdf).source == "gemini" and calls == ["x.pdf"]


# --------------------------------------------------------------------------------------------
# C3 basic auth
# --------------------------------------------------------------------------------------------


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def protected() -> TestClient:
    tiny = FastAPI()

    @tiny.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @tiny.get("/inbox")
    def inbox() -> dict[str, str]:
        return {"page": "inbox"}

    @tiny.post("/intake/webhook")
    def webhook() -> dict[str, str]:
        return {"doc_id": "B-W01"}

    tiny.add_middleware(BasicAuthMiddleware, username="velox", password="pässwort 1")  # as main.py registers it
    return TestClient(tiny)


def test_request_without_credentials_gets_401_with_the_velox_realm(protected):
    r = protected.get("/inbox")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith('Basic realm="Velox"')
    assert r.headers["cache-control"] == "no-store"
    assert r.text == "Authentication required.\n"


@pytest.mark.parametrize("headers", [
    basic("velox", "wrong"),
    basic("admin", "pässwort 1"),
    basic("velox", "pässwort 1 "),
    basic("velox", ""),
    {"Authorization": "Bearer some-token"},
    {"Authorization": "Basic not-base64!!"},
    {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
    {"Authorization": "Basic"},
])
def test_wrong_or_malformed_credentials_get_401(protected, headers):
    assert protected.get("/inbox", headers=headers).status_code == 401


def test_correct_credentials_pass(protected):
    r = protected.get("/inbox", headers=basic("velox", "pässwort 1"))
    assert r.status_code == 200 and r.json() == {"page": "inbox"}
    token = base64.b64encode("velox:pässwort 1".encode()).decode()
    assert protected.get("/inbox", headers={"Authorization": f"basic  {token}"}).status_code == 200  # scheme case


def test_webhook_needs_credentials_too(protected):
    assert protected.post("/intake/webhook").status_code == 401
    assert protected.post("/intake/webhook", headers=basic("velox", "pässwort 1")).status_code == 200


def test_health_is_exempt(protected):
    r = protected.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert protected.get("/health/extra").status_code == 401  # exact paths only


def test_both_parts_are_compared_in_constant_time(protected, monkeypatch):
    compared: list[tuple[bytes, bytes]] = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert protected.get("/inbox", headers=basic("admin", "pässwort 1")).status_code == 401
    assert compared == [(b"admin", b"velox"), ("pässwort 1".encode(), "pässwort 1".encode())]  # no early exit


def test_middleware_refuses_an_empty_password():
    with pytest.raises(ValueError, match="needs a password"):
        BasicAuthMiddleware(FastAPI(), username="velox", password="")


def test_custom_exempt_paths_and_realm():
    tiny = FastAPI()

    @tiny.get("/open")
    def open_page() -> dict[str, str]:
        return {"ok": "yes"}

    tiny.add_middleware(BasicAuthMiddleware, username="u", password="p", realm="Demo", exempt_paths=("/open",))
    client = TestClient(tiny)
    assert client.get("/open").status_code == 200
    assert client.get("/health").headers["www-authenticate"].startswith('Basic realm="Demo"')


@pytest.mark.parametrize("header, expected", [
    (None, None),
    (b"", None),
    (b"Basic " + base64.b64encode(b"velox:a:b"), (b"velox", b"a:b")),  # the password may contain ':'
    (b"Basic " + base64.b64encode(b":"), (b"", b"")),
    (b"Digest abc", None),
])
def test_parse_basic(header, expected):
    assert parse_basic(header) == expected
