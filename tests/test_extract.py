"""Extraction tests. None of them touches the network: the SDK client is always a fake or runs on an
in-process httpx.MockTransport, and a real client creation fails the test."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import ValidationError
from sqlalchemy import select

from app import config, extract, seed, world
from app.extract import (
    FIELDS,
    FIXTURE_MODEL,
    SYSTEM_INSTRUCTION,
    USER_INSTRUCTION,
    ExtractionFailed,
    ExtractionResult,
    ExtractionUnavailable,
    InvoiceExtraction,
)
from app.models import Extraction, InboundDocument

GEN = ["generateContent", "countTokens"]


# --------------------------------------------------------------------------------------------
# Helpers and fixtures
# --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_extractor(monkeypatch) -> list[float]:
    """Every test: no real SDK client, fresh model resolution, default model, and time.sleep recorded
    instead of slept (returns the list of requested pauses)."""
    def refuse_real_client():
        raise AssertionError("tests must never create a real Gemini client")

    pauses: list[float] = []
    monkeypatch.setattr(extract, "_client", refuse_real_client)
    monkeypatch.setattr(extract, "_resolved_model", None)
    monkeypatch.setattr(extract.time, "sleep", pauses.append)
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    return pauses


@pytest.fixture()
def sleeps(isolated_extractor) -> list[float]:
    """The pauses (seconds) the code under test asked time.sleep for."""
    return isolated_extractor


@pytest.fixture()
def gemini_mode(monkeypatch, tmp_cache_dir):
    """EXTRACTOR=gemini on the AI Studio backend with a dummy key and an empty temporary cache."""
    monkeypatch.setattr(config, "EXTRACTOR", "gemini")
    monkeypatch.setattr(config, "GEMINI_BACKEND", "aistudio")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    return tmp_cache_dir


def fixture_extraction(stem: str) -> dict:
    record = json.loads((config.FIXTURES_DIR / f"{stem}.json").read_text(encoding="utf-8"))
    return record["extraction"]


def fake_pdf(directory: Path, sample_no: int) -> Path:
    """A small stand-in file named like the sample PDF (only its bytes and hash matter here)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / world.DOCUMENT_BY_NO[sample_no].filename
    path.write_bytes(f"%PDF-1.4 fake sample {sample_no}\n".encode())
    return path


def fake_response(parsed=None, text=None, tokens_in=1834, tokens_out=612, thinking=None):
    usage = SimpleNamespace(prompt_token_count=tokens_in, candidates_token_count=tokens_out,
                            thoughts_token_count=thinking)
    return SimpleNamespace(parsed=parsed, text=text, usage_metadata=usage, candidates=None)


def not_found(model: str) -> genai_errors.ClientError:
    message = f"models/{model} is not found for API version v1beta, or is not supported for generateContent."
    return genai_errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": message}})


def api_error(code: int, status: str, message: str) -> genai_errors.APIError:
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


def quota_body(message: str, retry_delay=None) -> dict:
    """A 429 body shaped like the Gemini API's, optionally with a RetryInfo detail."""
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []}]
    if retry_delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": message, "details": details}}


def quota_error(message: str = "You exceeded your current quota.", retry_delay=None) -> genai_errors.ClientError:
    return genai_errors.ClientError(429, quota_body(message, retry_delay))


class FakeModels:
    def __init__(self, respond, listed=()):
        self.respond = respond  # callable(model) -> response, or raises
        self.listed = list(listed)
        self.calls: list[str] = []
        self.list_calls = 0
        self.last_request = None

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        self.last_request = (contents, config)
        return self.respond(model)

    def list(self):
        self.list_calls += 1
        return iter(self.listed)


class FakeClient:
    def __init__(self, respond, listed=()):
        self.models = FakeModels(respond, listed)


def use_client(monkeypatch, client) -> None:
    monkeypatch.setattr(extract, "_client", lambda: client)


def listed_models(*entries) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=f"models/{name}", supported_actions=actions) for name, actions in entries]


def use_sdk_on_mock_transport(monkeypatch, handler) -> None:
    """The installed google-genai client, but every HTTP request goes to `handler` (never the network)."""
    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(extract, "_client", lambda: genai.Client(
        api_key="test-key", http_options=genai_types.HttpOptions(httpx_client=http)))


def generate_ok(extraction_json: str, thoughts=None) -> httpx.Response:
    """A 200 generateContent response carrying the extraction JSON as text."""
    usage = {"promptTokenCount": 1834, "candidatesTokenCount": 612, "totalTokenCount": 2446}
    if thoughts is not None:
        usage["thoughtsTokenCount"] = thoughts
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": extraction_json}]}, "finishReason": "STOP"}],
        "usageMetadata": usage,
    })


def validation_error() -> ValidationError:
    try:
        InvoiceExtraction.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("an empty extraction must not validate")


FALLBACK_LIST = listed_models(
    ("gemini-2.0-flash", GEN), ("gemini-3.5-flash", GEN), ("gemini-3.8-flash", GEN),
    ("gemini-3.8-flash-preview-09-2026", GEN), ("gemini-3.5-flash-lite", GEN), ("gemini-3.8-flash-tts", GEN),
    ("gemini-3.1-pro-preview", GEN), ("gemini-4.0-flash", ["countTokens"]),  # no generateContent -> skipped
)


def make_doc(session, file_path: str, sample_no: int = 4, scenario: str = "tobe",
             file_hash: str = "0" * 64) -> InboundDocument:
    spec = world.DOCUMENT_BY_NO[sample_no]
    doc = InboundDocument(
        doc_id=seed.doc_id_for(scenario, sample_no), scenario=scenario, sample_no=sample_no,
        channel=spec.channel, mailbox=spec.mailbox, received_on=spec.received_on, file_path=file_path,
        file_hash=file_hash, sender_email=spec.sender_email, subject=spec.subject, registered=True,
        registered_on=spec.received_on, doc_type="unknown",
    )
    session.add(doc)
    session.commit()
    return doc


# --------------------------------------------------------------------------------------------
# (a) Fixture mode
# --------------------------------------------------------------------------------------------


def test_every_fixture_file_validates_against_the_strict_schema():
    paths = sorted(config.FIXTURES_DIR.glob("*.json"))
    assert len(paths) == len(world.DOCUMENTS)
    for path in paths:
        InvoiceExtraction.model_validate(json.loads(path.read_text(encoding="utf-8"))["extraction"])


def test_fixture_mode_returns_every_document():
    assert config.EXTRACTOR == "fixture"
    for spec in world.DOCUMENTS:
        result = extract.extract_file(config.INVOICES_DIR / spec.filename)
        InvoiceExtraction.model_validate(result.data)
        assert set(result.data) == set(FIELDS)
        assert result.is_fixture and result.model == FIXTURE_MODEL
        assert result.source == "fixture" and result.from_cache is False
        assert result.data["doc_type"]["value"] == spec.true_doc_type
        assert result.data["invoice_number"]["value"] == spec.invoice_number


# --------------------------------------------------------------------------------------------
# Output schema: every key required, every value nullable
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIELDS)
def test_every_field_requires_value_and_confidence(name):
    field_cls = InvoiceExtraction.model_fields[name].annotation
    assert field_cls.model_json_schema()["required"] == ["value", "confidence"]
    field_cls.model_validate({"value": None, "confidence": 0})  # null stays allowed
    with pytest.raises(ValidationError):
        field_cls.model_validate({"value": None})
    with pytest.raises(ValidationError):
        field_cls.model_validate({"confidence": 0.5})


def test_line_keys_are_required_but_nullable():
    keys = ("description", "quantity", "unit_price", "amount")
    extract.Line.model_validate(dict.fromkeys(keys))
    for missing in keys:
        with pytest.raises(ValidationError):
            extract.Line.model_validate({k: None for k in keys if k != missing})


def test_confidence_is_not_bounded_in_the_schema():
    """Out-of-range confidences must still validate: _postprocess clamps them (tested below)."""
    field = extract.TextField.model_validate({"value": "x", "confidence": 1.7})
    assert field.confidence == 1.7
    assert "maximum" not in extract.TextField.model_json_schema()["properties"]["confidence"]


# --------------------------------------------------------------------------------------------
# (b) Disk cache, (c) unavailable paths
# --------------------------------------------------------------------------------------------


def _counting_fake(calls: list[str]):
    data = InvoiceExtraction.model_validate(fixture_extraction("04_bright_agency_credit_note")).model_dump(mode="json")

    def fake_call_gemini(pdf_bytes: bytes, file_name: str) -> ExtractionResult:
        calls.append(file_name)
        return ExtractionResult(model="gemini-2.5-flash", data=data, from_cache=False, source="gemini",
                                created_on=datetime(2026, 10, 2, 11, 45), latency_ms=4210,
                                input_tokens=1834, output_tokens=812, thinking_tokens=200)
    return fake_call_gemini


def test_cache_is_written_then_hit_and_force_bypasses_it(gemini_mode, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(extract, "call_gemini", _counting_fake(calls))
    pdf = fake_pdf(tmp_path, 4)
    sha = extract.file_sha256(pdf)

    first = extract.extract_file(pdf)
    assert calls == [pdf.name] and first.source == "gemini" and first.from_cache is False
    cache_file = gemini_mode / f"{sha}.json"
    assert cache_file.exists() and extract.cache_path(sha) == cache_file
    record = json.loads(cache_file.read_text(encoding="utf-8"))
    assert set(record) == {"file_name", "file_sha256", "model", "backend", "created_on", "latency_ms", "usage",
                           "extraction"}
    assert record["file_name"] == pdf.name and record["file_sha256"] == sha
    assert record["model"] == "gemini-2.5-flash" and record["latency_ms"] == 4210
    assert record["usage"] == {"input_tokens": 1834, "output_tokens": 812, "thinking_tokens": 200}
    InvoiceExtraction.model_validate(record["extraction"])

    second = extract.extract_file(pdf)
    assert calls == [pdf.name]  # cache hit: the API is not called again
    assert second.from_cache is True and second.source == "cache"
    assert second.data == first.data and second.model == "gemini-2.5-flash"
    assert (second.input_tokens, second.output_tokens, second.thinking_tokens) == (1834, 812, 200)

    forced = extract.extract_file(pdf, force=True)
    assert calls == [pdf.name, pdf.name] and forced.source == "gemini"


def test_old_cache_record_without_thinking_tokens_still_reads(gemini_mode):
    record = {"file_name": "x.pdf", "file_sha256": "a" * 64, "model": "gemini-2.5-flash",
              "created_on": "2026-10-02T11:45:00", "latency_ms": 4210,
              "usage": {"input_tokens": 1834, "output_tokens": 612},
              "extraction": fixture_extraction("04_bright_agency_credit_note")}
    (gemini_mode / f"{'a' * 64}.json").write_text(json.dumps(record), encoding="utf-8")

    cached = extract.read_cache("a" * 64)
    assert cached is not None and cached.source == "cache"
    assert (cached.input_tokens, cached.output_tokens, cached.thinking_tokens) == (1834, 612, None)


def test_write_cache_is_atomic(gemini_mode, monkeypatch):
    sha = "b" * 64
    result = _counting_fake([])(b"", "x.pdf")
    replaced: list[tuple[str, str]] = []
    real_replace = extract.os.replace

    def spy_replace(src, dst):
        replaced.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(extract.os, "replace", spy_replace)
    path = extract.write_cache(sha, result, "x.pdf")
    assert replaced == [(f"{sha}.json.tmp", f"{sha}.json")]
    assert [p.name for p in gemini_mode.iterdir()] == [f"{sha}.json"]  # no temp file left behind
    assert extract.read_cache(sha).data == result.data

    def failing_replace(src, dst):
        raise OSError("disk full")

    before = path.read_bytes()
    monkeypatch.setattr(extract.os, "replace", failing_replace)
    with pytest.raises(OSError):
        extract.write_cache(sha, _counting_fake([])(b"", "other.pdf"), "other.pdf")
    assert path.read_bytes() == before  # an interrupted write never damages the existing entry
    assert [p.name for p in gemini_mode.iterdir()] == [f"{sha}.json"]  # the temp file is cleaned up


def test_failed_cache_write_keeps_the_api_result(gemini_mode, tmp_path, monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(extract, "call_gemini", _counting_fake(calls))

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(extract.os, "replace", failing_replace)
    pdf = fake_pdf(tmp_path, 4)
    result = extract.extract_file(pdf)
    assert calls == [pdf.name] and result.source == "gemini"
    assert f"[extract] WARNING file={pdf.name} cache not written: disk full" in capsys.readouterr().out
    assert list(gemini_mode.iterdir()) == []


def test_missing_pdf_is_unavailable(gemini_mode, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(extract, "call_gemini", _counting_fake(calls))
    with pytest.raises(ExtractionUnavailable, match="04_bright_agency_credit_note.pdf not found"):
        extract.extract_file(tmp_path / "04_bright_agency_credit_note.pdf")
    assert calls == []


@pytest.mark.parametrize("content", [
    "",
    "{not json",
    '{"model": "gemini-2.5-flash"}',  # KeyError: no extraction / created_on
    "[]",
    json.dumps({"model": "m", "created_on": "yesterday", "extraction": {}}),
    json.dumps({"model": "m", "created_on": "2026-10-02T11:45:00",
                "extraction": {"doc_type": {"value": "invoice"}}}),  # ValidationError
])
def test_corrupt_cache_file_is_ignored_as_a_miss(gemini_mode, tmp_path, monkeypatch, capsys, content):
    calls: list[str] = []
    monkeypatch.setattr(extract, "call_gemini", _counting_fake(calls))
    pdf = fake_pdf(tmp_path, 4)
    sha = extract.file_sha256(pdf)
    (gemini_mode / f"{sha}.json").write_text(content, encoding="utf-8")

    assert extract.read_cache(sha) is None
    assert f"[extract] corrupt cache file {sha}.json ignored" in capsys.readouterr().out
    with pytest.raises(ExtractionUnavailable, match="not in the cache"):
        extract.extract_file(pdf, allow_api=False)

    result = extract.extract_file(pdf)  # a miss: the API is called and the entry is rewritten
    assert calls == [pdf.name] and result.source == "gemini"
    assert extract.read_cache(sha).data == result.data


def test_cache_miss_with_api_disabled_is_unavailable(gemini_mode, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(extract, "call_gemini", _counting_fake(calls))
    with pytest.raises(ExtractionUnavailable, match="not in the cache"):
        extract.extract_file(fake_pdf(tmp_path, 4), allow_api=False)
    assert calls == []


def test_missing_api_key_is_unavailable(gemini_mode, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(extract, "call_gemini", _counting_fake(calls))
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(ExtractionUnavailable, match="GEMINI_API_KEY"):
        extract.extract_file(fake_pdf(tmp_path, 4))
    assert calls == []


# --------------------------------------------------------------------------------------------
# (d) call_gemini with an injected fake client
# --------------------------------------------------------------------------------------------


def test_call_gemini_postprocesses_the_parsed_response(monkeypatch, capsys):
    data = fixture_extraction("04_bright_agency_credit_note")
    data["supplier_name"]["confidence"] = 1.7
    data["invoice_number"]["confidence"] = -0.2
    data["due_date"] = {"value": None, "confidence": 0.8}
    data["notes"] = {"value": "   ", "confidence": 0.5}
    data["po_numbers"] = {"value": [], "confidence": 0.6}
    client = FakeClient(lambda model: fake_response(parsed=InvoiceExtraction.model_validate(data)))
    use_client(monkeypatch, client)

    result = extract.call_gemini(b"%PDF-1.4 fake", "04_bright_agency_credit_note.pdf")

    out = result.data
    assert out["supplier_name"]["confidence"] == 1.0
    assert out["invoice_number"]["confidence"] == 0.0
    assert out["due_date"] == {"value": None, "confidence": 0.0}
    assert out["notes"] == {"value": None, "confidence": 0.0}
    assert out["po_numbers"] == {"value": None, "confidence": 0.0}
    assert out["gross_total"] == {"value": -1800.0, "confidence": 0.99}
    assert all(0.0 <= out[name]["confidence"] <= 1.0 for name in FIELDS)
    assert result.model == "gemini-2.5-flash" and client.models.calls == ["gemini-2.5-flash"]
    assert result.source == "gemini" and result.from_cache is False and not result.is_fixture
    assert result.backend == config.GEMINI_BACKEND and not result.is_ubl
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    assert (result.input_tokens, result.output_tokens, result.thinking_tokens) == (1834, 612, None)

    contents, gen_config = client.models.last_request
    assert contents[0].inline_data.mime_type == "application/pdf"
    assert contents[0].inline_data.data == b"%PDF-1.4 fake"
    assert contents[1] == USER_INSTRUCTION
    assert gen_config.system_instruction == SYSTEM_INSTRUCTION
    assert gen_config.response_mime_type == "application/json"
    assert gen_config.response_schema is InvoiceExtraction
    assert gen_config.temperature == 0

    log = capsys.readouterr().out
    assert "[extract] file=04_bright_agency_credit_note.pdf model=gemini-2.5-flash latency_ms=" in log
    assert f"tokens_in=1834 tokens_out=612 (thinking=None) cache=miss backend={config.GEMINI_BACKEND}" in log


@pytest.mark.parametrize("candidates, thinking, tokens_out", [
    (612, 388, 1000), (612, None, 612), (612, 0, 612), (None, 388, 388), (None, None, None),
])
def test_output_tokens_include_the_thinking_tokens(candidates, thinking, tokens_out):
    usage = SimpleNamespace(prompt_token_count=1834, candidates_token_count=candidates,
                            thoughts_token_count=thinking)
    assert extract._token_counts(usage) == (1834, tokens_out, thinking)
    assert extract._token_counts(None) == (None, None, None)


def test_thinking_tokens_are_logged_and_stored_in_the_cache(gemini_mode, tmp_path, monkeypatch, capsys):
    parsed = InvoiceExtraction.model_validate(fixture_extraction("04_bright_agency_credit_note"))
    use_client(monkeypatch, FakeClient(lambda model: fake_response(parsed=parsed, thinking=388)))
    pdf = fake_pdf(tmp_path, 4)

    result = extract.extract_file(pdf)

    assert (result.input_tokens, result.output_tokens, result.thinking_tokens) == (1834, 1000, 388)
    assert "tokens_in=1834 tokens_out=1000 (thinking=388) cache=miss" in capsys.readouterr().out
    record = json.loads(extract.cache_path(extract.file_sha256(pdf)).read_text(encoding="utf-8"))
    assert record["usage"] == {"input_tokens": 1834, "output_tokens": 1000, "thinking_tokens": 388}
    cached = extract.extract_file(pdf)
    assert cached.source == "cache" and (cached.output_tokens, cached.thinking_tokens) == (1000, 388)


def test_call_gemini_uses_response_text_when_nothing_is_parsed(monkeypatch):
    text = json.dumps(fixture_extraction("07_shopsys_invoice"))
    content = genai_types.Content(role="model", parts=[genai_types.Part(text=text)])
    response = genai_types.GenerateContentResponse(candidates=[genai_types.Candidate(content=content)],
                                                   usage_metadata=None)
    use_client(monkeypatch, FakeClient(lambda model: response))

    result = extract.call_gemini(b"%PDF-1.4 fake", "07_shopsys_invoice.pdf")

    assert result.data == InvoiceExtraction.model_validate_json(text).model_dump(mode="json")
    assert result.data["supplier_iban"]["value"] == "ABA 121000248 ACCT 4839201756"
    assert result.input_tokens is None and result.output_tokens is None


@pytest.mark.parametrize("response", [
    fake_response(text="this is not JSON"),
    fake_response(text=json.dumps({"doc_type": {"value": "receipt", "confidence": 1}})),
    genai_types.GenerateContentResponse(candidates=[]),
])
def test_unusable_response_raises_extraction_failed(monkeypatch, response):
    use_client(monkeypatch, FakeClient(lambda model: response))
    with pytest.raises(ExtractionFailed, match="no usable extraction"):
        extract.call_gemini(b"%PDF-1.4 fake", "x.pdf")


@pytest.mark.parametrize("make_error", [
    lambda: json.JSONDecodeError("Expecting value", "<html>proxy error</html>", 0),
    validation_error,
    lambda: ValueError("could not parse the response"),
])
def test_sdk_parse_error_on_a_200_raises_extraction_failed(monkeypatch, sleeps, make_error):
    """The SDK itself can raise while parsing a 200 response; that is a failed call, not a crash."""
    def respond(model):
        raise make_error()

    client = FakeClient(respond)
    use_client(monkeypatch, client)
    with pytest.raises(ExtractionFailed, match="unusable response from gemini-2.5-flash"):
        extract.call_gemini(b"%PDF", "x.pdf")
    assert client.models.calls == ["gemini-2.5-flash"] and sleeps == []  # not retried
    assert extract._resolved_model is None


def test_non_json_200_body_through_the_installed_sdk_raises_extraction_failed(monkeypatch):
    use_sdk_on_mock_transport(monkeypatch, lambda request: httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(ExtractionFailed, match="unusable response from gemini-2.5-flash"):
        extract.call_gemini(b"%PDF", "x.pdf")


# --------------------------------------------------------------------------------------------
# (e) Model fallback, retries, error classification
# --------------------------------------------------------------------------------------------


def test_unavailable_model_falls_back_to_newest_ga_flash_and_remembers_it(monkeypatch, capsys):
    parsed = InvoiceExtraction.model_validate(fixture_extraction("03_bright_agency_invoice"))

    def respond(model):
        if model == "gemini-2.5-flash":
            raise not_found(model)
        return fake_response(parsed=parsed)

    client = FakeClient(respond, FALLBACK_LIST)
    use_client(monkeypatch, client)

    result = extract.call_gemini(b"%PDF-1.4 fake", "03_bright_agency_invoice.pdf")

    assert result.model == "gemini-3.8-flash"
    assert client.models.calls == ["gemini-2.5-flash", "gemini-3.8-flash"]
    assert extract._resolved_model == "gemini-3.8-flash"
    log = capsys.readouterr().out
    assert "[extract] WARNING model=gemini-2.5-flash unavailable (404 NOT_FOUND" in log
    assert "-> falling back to gemini-3.8-flash" in log

    again = extract.call_gemini(b"%PDF-1.4 fake", "03_bright_agency_invoice.pdf")
    assert again.model == "gemini-3.8-flash"
    assert client.models.calls == ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.8-flash"]
    assert client.models.list_calls == 1
    # Gemini 3+: temperature left at the API default (Google advises against lowering it).
    assert client.models.last_request[1].temperature is None


@pytest.mark.parametrize("model, expected", [
    ("gemini-2.5-flash", 0.0), ("gemini-2.0-flash", 0.0), ("gemini-3.8-flash", None), ("gemini-10.1-flash", None),
])
def test_temperature_per_model_generation(model, expected):
    assert extract._temperature_for(model) == expected


def test_discovered_fallbacks_are_ga_flash_only_newest_first():
    client = FakeClient(lambda model: None, FALLBACK_LIST)
    assert extract._discover_flash_models(client) == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.0-flash"]


def test_fallback_skips_a_model_without_access(monkeypatch):
    parsed = InvoiceExtraction.model_validate(fixture_extraction("03_bright_agency_invoice"))

    def respond(model):
        if model == "gemini-2.5-flash":
            raise not_found(model)
        if model == "gemini-3.8-flash":
            raise api_error(403, "PERMISSION_DENIED", "Your project does not have access to model gemini-3.8-flash.")
        return fake_response(parsed=parsed)

    client = FakeClient(respond, FALLBACK_LIST)
    use_client(monkeypatch, client)
    assert extract.call_gemini(b"%PDF", "x.pdf").model == "gemini-3.5-flash"
    assert client.models.calls == ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.5-flash"]


def test_no_fallback_available_raises_extraction_failed(monkeypatch):
    def respond(model):
        raise not_found(model)

    use_client(monkeypatch, FakeClient(respond, listed_models(("gemini-3.8-flash-preview-09-2026", GEN))))
    with pytest.raises(ExtractionFailed, match="no other GA Flash model is available"):
        extract.call_gemini(b"%PDF", "x.pdf")
    assert extract._resolved_model is None


@pytest.mark.parametrize("error, unavailable", [
    (not_found("gemini-2.5-flash"), True),
    (api_error(403, "PERMISSION_DENIED", "Permission denied on model gemini-2.5-flash."), True),
    (api_error(400, "FAILED_PRECONDITION", "This model is no longer available to new users."), True),
    (api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded for metric: x, limit: 0, model: gemini-2.5-flash"), True),
    (api_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."), False),
    (api_error(429, "RESOURCE_EXHAUSTED", "Resource has been exhausted (e.g. check quota)."), False),
    (api_error(503, "UNAVAILABLE", "The model is overloaded. Please try again later."), False),
])
def test_model_unavailable_classification(error, unavailable):
    assert extract._is_model_unavailable(error) is unavailable


def test_transient_error_is_retried_once(monkeypatch, sleeps, capsys):
    parsed = InvoiceExtraction.model_validate(fixture_extraction("08_quickprint_invoice"))
    failures = [api_error(503, "UNAVAILABLE", "The model is overloaded. Please try again later.")]

    def respond(model):
        if failures:
            raise failures.pop()
        return fake_response(parsed=parsed)

    client = FakeClient(respond)
    use_client(monkeypatch, client)
    result = extract.call_gemini(b"%PDF", "08_quickprint_invoice.pdf")
    assert result.model == "gemini-2.5-flash"
    assert client.models.calls == ["gemini-2.5-flash", "gemini-2.5-flash"]
    assert sleeps == [extract.RETRY_BACKOFF_S]
    assert "transient error (503 UNAVAILABLE" in capsys.readouterr().out


@pytest.mark.parametrize("error, message", [
    (api_error(429, "RESOURCE_EXHAUSTED", "Resource has been exhausted."), "429 RESOURCE_EXHAUSTED"),
    (httpx.ConnectError("connection refused"), "network error"),
])
def test_persistent_transient_error_fails_after_the_retries(monkeypatch, sleeps, error, message):
    """TRANSIENT_ATTEMPTS calls with growing pauses (2 s, 4 s); with no other GA Flash model listed, it fails."""
    def respond(model):
        raise error

    client = FakeClient(respond)
    use_client(monkeypatch, client)
    with pytest.raises(ExtractionFailed, match=message):
        extract.call_gemini(b"%PDF", "x.pdf")
    assert len(client.models.calls) == extract.TRANSIENT_ATTEMPTS == 3
    assert sleeps == [extract.RETRY_BACKOFF_S, 2 * extract.RETRY_BACKOFF_S]


def test_overloaded_model_falls_back_to_the_next_ga_flash_and_remembers_it(monkeypatch, sleeps, capsys):
    """The case seen live: 2.5 no longer available (404), 3.8 answering 503 "high demand" on every retry."""
    parsed = InvoiceExtraction.model_validate(fixture_extraction("08_quickprint_invoice"))
    busy = api_error(503, "UNAVAILABLE", "This model is currently experiencing high demand. Please try again later.")

    def respond(model):
        if model == "gemini-2.5-flash":
            raise not_found(model)
        if model == "gemini-3.8-flash":
            raise busy
        return fake_response(parsed=parsed)

    client = FakeClient(respond, FALLBACK_LIST)
    use_client(monkeypatch, client)
    assert extract.call_gemini(b"%PDF", "x.pdf").model == "gemini-3.5-flash"
    assert client.models.calls == ["gemini-2.5-flash"] + ["gemini-3.8-flash"] * 3 + ["gemini-3.5-flash"]
    log = capsys.readouterr().out
    assert "still overloaded after 3 attempts (503 UNAVAILABLE" in log and "-> trying gemini-3.5-flash" in log

    # The next document goes straight to the model that answered; the model list is not fetched again.
    assert extract.call_gemini(b"%PDF", "y.pdf").model == "gemini-3.5-flash"
    assert client.models.calls[-1] == "gemini-3.5-flash" and client.models.list_calls == 1


def test_an_unavailable_model_is_never_asked_again(monkeypatch, sleeps):
    """Even when no call succeeded yet, a model that answered 404 is skipped for the rest of the process."""
    busy = api_error(503, "UNAVAILABLE", "This model is currently experiencing high demand.")

    def respond(model):
        if model == "gemini-2.5-flash":
            raise not_found(model)
        raise busy

    client = FakeClient(respond, listed_models(("gemini-3.8-flash", GEN)))
    use_client(monkeypatch, client)
    for _ in range(2):
        with pytest.raises(ExtractionFailed, match="overloaded"):
            extract.call_gemini(b"%PDF", "x.pdf")
    assert client.models.calls.count("gemini-2.5-flash") == 1
    assert client.models.calls.count("gemini-3.8-flash") == 2 * extract.TRANSIENT_ATTEMPTS
    assert client.models.list_calls == 1


@pytest.mark.parametrize("error, pause", [
    (quota_error(retry_delay="37s"), 38.0),  # RetryInfo.retryDelay + 1 s
    (quota_error(retry_delay="0.5s"), 1.5),
    (quota_error("Quota exceeded for metric: requests per minute. Please retry in 12.5s."), 13.5),  # message only
    (quota_error("Please retry in 5s.", retry_delay="20s"), 21.0),  # RetryInfo wins over the message
    (quota_error(retry_delay="600s"), extract.MAX_RETRY_DELAY_S),  # capped at 60 s
    (quota_error(retry_delay="soon"), extract.RETRY_BACKOFF_S),  # unreadable delay
    (quota_error(), extract.RETRY_BACKOFF_S),  # no delay given
    (genai_errors.ClientError(429, [quota_body("Please retry in 3s.")]), 4.0),  # list-shaped body
    (api_error(503, "UNAVAILABLE", "The model is overloaded. Please retry in 30s."), extract.RETRY_BACKOFF_S),
    (httpx.ConnectTimeout("timed out"), extract.RETRY_BACKOFF_S),
])
def test_retry_pause_uses_the_server_delay_on_a_429(monkeypatch, sleeps, capsys, error, pause):
    parsed = InvoiceExtraction.model_validate(fixture_extraction("08_quickprint_invoice"))
    failures = [error]

    def respond(model):
        if failures:
            raise failures.pop()
        return fake_response(parsed=parsed)

    client = FakeClient(respond)
    use_client(monkeypatch, client)
    assert extract.call_gemini(b"%PDF", "x.pdf").model == "gemini-2.5-flash"
    assert client.models.calls == ["gemini-2.5-flash", "gemini-2.5-flash"]
    assert sleeps == [pause]
    assert f"-> retrying in {pause:g} s" in capsys.readouterr().out


def test_zero_quota_429_falls_back_without_waiting(monkeypatch, sleeps):
    """A 429 with "limit: 0" means no access to the model: fall back at once, ignore its retry delay."""
    parsed = InvoiceExtraction.model_validate(fixture_extraction("08_quickprint_invoice"))

    def respond(model):
        if model == "gemini-2.5-flash":
            raise quota_error("Quota exceeded for metric: generate_content_free_tier_requests, limit: 0, "
                              "model: gemini-2.5-flash. Please retry in 37s.", retry_delay="37s")
        return fake_response(parsed=parsed)

    client = FakeClient(respond, FALLBACK_LIST)
    use_client(monkeypatch, client)
    assert extract.call_gemini(b"%PDF", "x.pdf").model == "gemini-3.8-flash"
    assert client.models.calls == ["gemini-2.5-flash", "gemini-3.8-flash"]
    assert sleeps == []


def test_429_retry_delay_through_the_installed_sdk(monkeypatch, sleeps):
    """The real SDK error object carries the RetryInfo detail where _server_retry_delay looks for it."""
    extraction_json = json.dumps(fixture_extraction("08_quickprint_invoice"))
    responses = [httpx.Response(429, json=quota_body("You exceeded your current quota. Please retry in 7.2s.",
                                                     retry_delay="7s")),
                 generate_ok(extraction_json)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    use_sdk_on_mock_transport(monkeypatch, handler)
    result = extract.call_gemini(b"%PDF", "08_quickprint_invoice.pdf")
    assert result.model == "gemini-2.5-flash" and len(requests) == 2  # the SDK itself does not retry
    assert sleeps == [8.0]


def test_other_api_error_is_not_retried_and_is_readable(monkeypatch):
    def respond(model):
        raise api_error(400, "INVALID_ARGUMENT", "Request contains an invalid argument.")

    client = FakeClient(respond)
    use_client(monkeypatch, client)
    with pytest.raises(ExtractionUnavailable, match="400 INVALID_ARGUMENT: Request contains an invalid argument"):
        extract.call_gemini(b"%PDF", "x.pdf")
    assert client.models.calls == ["gemini-2.5-flash"]


def test_call_gemini_through_the_installed_sdk_on_a_mock_transport(monkeypatch):
    """The real google-genai client builds the request and parses the response; httpx never leaves the process."""
    extraction_json = json.dumps(fixture_extraction("03_bright_agency_invoice"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": GEN},
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": GEN},
                {"name": "models/gemini-3.8-flash-lite", "supportedGenerationMethods": GEN},
            ]})
        if path.endswith("/models/gemini-2.5-flash:generateContent"):
            return httpx.Response(404, json={"error": {
                "code": 404, "status": "NOT_FOUND",
                "message": "models/gemini-2.5-flash is not found for API version v1beta."}})
        if path.endswith("/models/gemini-3.8-flash:generateContent"):
            return generate_ok(extraction_json, thoughts=388)
        return httpx.Response(500, json={"error": {"code": 500, "status": "INTERNAL", "message": f"unexpected {path}"}})

    use_sdk_on_mock_transport(monkeypatch, handler)

    result = extract.call_gemini(b"%PDF-1.4 fake", "03_bright_agency_invoice.pdf")

    assert result.model == "gemini-3.8-flash"
    assert result.data["po_numbers"]["value"] == ["4500117"]
    assert (result.input_tokens, result.output_tokens, result.thinking_tokens) == (1834, 1000, 388)
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == [
        "gemini-2.5-flash:generateContent", "models", "gemini-3.8-flash:generateContent"]
    assert all(r.headers["x-goog-api-key"] == "test-key" for r in requests)

    body = json.loads(requests[-1].content)
    generation = body["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    # temperature 0 on the 2.x request; left at the API default on the Gemini 3 fallback
    assert json.loads(requests[0].content)["generationConfig"]["temperature"] == 0
    assert "temperature" not in generation
    assert set(generation["responseSchema"]["properties"]) == set(FIELDS)
    assert generation["responseSchema"]["required"] == list(FIELDS)
    assert body["systemInstruction"]["parts"][0]["text"] == SYSTEM_INSTRUCTION
    pdf_part, text_part = body["contents"][0]["parts"]
    inline = pdf_part["inlineData"]
    assert inline.get("mimeType", inline.get("mime_type")) == "application/pdf"
    assert base64.b64decode(inline["data"]) == b"%PDF-1.4 fake"
    assert text_part["text"] == USER_INSTRUCTION


def test_response_schema_sent_by_the_sdk_requires_every_key_and_keeps_null(monkeypatch):
    """As the installed SDK serialises it: value and confidence required, value nullable, no bounds."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return generate_ok(json.dumps(fixture_extraction("04_bright_agency_credit_note")))

    use_sdk_on_mock_transport(monkeypatch, handler)
    extract.call_gemini(b"%PDF", "x.pdf")

    schema = json.loads(requests[0].content)["generationConfig"]["responseSchema"]
    for name in FIELDS:
        field = schema["properties"][name]
        assert field["required"] == ["value", "confidence"], name
        assert field["properties"]["value"]["nullable"] is True, name
        confidence = field["properties"]["confidence"]
        assert confidence["type"] == "NUMBER" and "nullable" not in confidence, name
        assert "minimum" not in confidence and "maximum" not in confidence, name
    line = schema["properties"]["lines"]["properties"]["value"]["items"]
    assert line["required"] == ["description", "quantity", "unit_price", "amount"]
    assert all(line["properties"][key]["nullable"] is True for key in line["required"])
    assert schema["properties"]["invoice_date"]["properties"]["value"]["description"] == "ISO date YYYY-MM-DD"


# --------------------------------------------------------------------------------------------
# (f) extract_document / extract_documents: upsert + doc_type, None when unavailable, ExtractionFailed
#     re-raised (batch: counted, never raised), sibling refresh after a fresh API result
# --------------------------------------------------------------------------------------------


def doc_for_file(session, pdf: Path, sample_no: int, scenario: str = "tobe") -> InboundDocument:
    """An inbound document for a stand-in PDF (absolute path: BASE_DIR / path keeps it)."""
    return make_doc(session, str(pdf), sample_no=sample_no, scenario=scenario, file_hash=extract.file_sha256(pdf))


def ok_client(monkeypatch, stem: str = "04_bright_agency_credit_note") -> FakeClient:
    parsed = InvoiceExtraction.model_validate(fixture_extraction(stem))
    client = FakeClient(lambda model: fake_response(parsed=parsed))
    use_client(monkeypatch, client)
    return client


def failing_client(monkeypatch, error: Exception) -> FakeClient:
    def respond(model):
        raise error

    client = FakeClient(respond)
    use_client(monkeypatch, client)
    return client


def bad_key() -> genai_errors.APIError:
    return api_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.")


def spy_extract_document(monkeypatch) -> list[tuple[str, bool]]:
    """Record (doc_id, allow_api) of every extract_document call, including the sibling refreshes."""
    calls: list[tuple[str, bool]] = []
    real = extract.extract_document

    def spy(session, doc, **kwargs):
        calls.append((doc.doc_id, kwargs.get("allow_api", True)))
        return real(session, doc, **kwargs)

    monkeypatch.setattr(extract, "extract_document", spy)
    return calls


def test_extract_document_upserts_row_and_sets_doc_type(session):
    doc = make_doc(session, "data/invoices/04_bright_agency_credit_note.pdf")

    row = extract.extract_document(session, doc)
    assert row is not None and row.model == FIXTURE_MODEL and row.from_cache is False
    assert row.json["invoice_number"]["value"] == "CN-2026-0031"
    assert doc.doc_type == "credit_note" and doc.extraction is row

    again = extract.extract_document(session, doc)
    assert again.id == row.id
    rows = session.scalars(select(Extraction).where(Extraction.doc_id == doc.doc_id)).all()
    assert len(rows) == 1


def test_extract_document_returns_none_when_unavailable(session, gemini_mode, tmp_path, monkeypatch, capsys):
    doc = doc_for_file(session, fake_pdf(tmp_path, 5), 5)
    client = ok_client(monkeypatch)

    assert extract.extract_document(session, doc, allow_api=False) is None  # not cached, API disabled
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert extract.extract_document(session, doc) is None  # no key
    assert client.models.calls == []
    assert doc.extraction is None and doc.doc_type == "unknown"
    log = capsys.readouterr().out
    assert f"doc={doc.doc_id} unavailable: 05_fitout_partners_invoice.pdf is not in the cache" in log
    assert f"doc={doc.doc_id} unavailable: GEMINI_API_KEY is not set" in log


def test_extract_document_reraises_extraction_failed(session, gemini_mode, tmp_path, monkeypatch):
    doc = doc_for_file(session, fake_pdf(tmp_path, 5), 5)
    client = failing_client(monkeypatch, bad_key())

    with pytest.raises(ExtractionFailed, match="400 INVALID_ARGUMENT: API key not valid"):
        extract.extract_document(session, doc)
    assert client.models.calls == ["gemini-2.5-flash"]
    assert doc.extraction is None and doc.doc_type == "unknown"


def test_failed_forced_re_extraction_keeps_the_existing_row(session, gemini_mode, tmp_path, monkeypatch):
    pdf = fake_pdf(tmp_path, 4)
    doc = doc_for_file(session, pdf, 4)
    extract.write_cache(extract.file_sha256(pdf), _counting_fake([])(b"", pdf.name), pdf.name)
    row = extract.extract_document(session, doc)
    assert row is not None and row.from_cache is True and doc.doc_type == "credit_note"
    before = (row.id, row.model, row.created_on, row.json)

    failing_client(monkeypatch, api_error(503, "UNAVAILABLE", "The model is overloaded."))
    with pytest.raises(ExtractionFailed, match="503 UNAVAILABLE"):
        extract.extract_document(session, doc, force=True)
    session.expire_all()
    assert (doc.extraction.id, doc.extraction.model, doc.extraction.created_on, doc.extraction.json) == before
    assert doc.doc_type == "credit_note"


def test_extract_documents_counts_every_outcome(session, gemini_mode, tmp_path, monkeypatch):
    cached_pdf, fresh_pdf, uncached_pdf = fake_pdf(tmp_path, 4), fake_pdf(tmp_path, 9), fake_pdf(tmp_path, 5)
    extract.write_cache(extract.file_sha256(cached_pdf), _counting_fake([])(b"", cached_pdf.name), cached_pdf.name)
    docs = [doc_for_file(session, cached_pdf, 4), doc_for_file(session, fresh_pdf, 9)]
    ok_client(monkeypatch, "08_quickprint_invoice")

    summary = extract.extract_documents(session, docs)
    assert summary == {"extracted": 2, "from_cache": 1, "unavailable": 0, "failed": 0, "error": None}

    uncached = doc_for_file(session, uncached_pdf, 5)
    summary = extract.extract_documents(session, docs + [uncached], allow_api=False)
    assert summary == {"extracted": 2, "from_cache": 2, "unavailable": 1, "failed": 0, "error": None}


@pytest.mark.parametrize("error, api_calls, pauses", [
    (bad_key(), 1, []),  # a broken key: one call, no retry
    (httpx.ConnectTimeout("timed out"), 3, [extract.RETRY_BACKOFF_S, 2 * extract.RETRY_BACKOFF_S]),  # 1 call + 2 retries
])
def test_extract_documents_uses_the_cache_only_after_the_first_failure(
        session, gemini_mode, tmp_path, monkeypatch, sleeps, capsys, error, api_calls, pauses):
    pdfs = {no: fake_pdf(tmp_path, no) for no in (1, 4, 5, 9)}
    extract.write_cache(extract.file_sha256(pdfs[4]), _counting_fake([])(b"", pdfs[4].name), pdfs[4].name)
    docs = [doc_for_file(session, pdfs[no], no) for no in (1, 4, 5, 9)]  # only sample 4 is cached
    client = failing_client(monkeypatch, error)

    summary = extract.extract_documents(session, docs)

    assert client.models.calls == ["gemini-2.5-flash"] * api_calls  # samples 5 and 9 never reach the API
    assert sleeps == pauses
    assert summary["failed"] == 1 and summary["error"].startswith(("Gemini API error", "network error"))
    assert {k: summary[k] for k in ("extracted", "from_cache", "unavailable")} == {
        "extracted": 1, "from_cache": 1, "unavailable": 2}
    assert docs[1].extraction is not None and docs[0].extraction is None
    log = capsys.readouterr().out
    assert f"doc={docs[0].doc_id} FAILED: " in log
    assert "the remaining documents use the cache only" in log


def test_extract_documents_reports_the_first_error_only(session, gemini_mode, tmp_path, monkeypatch):
    docs = [doc_for_file(session, fake_pdf(tmp_path, no), no) for no in (1, 5)]
    failing_client(monkeypatch, bad_key())
    # Make the cache-only run of the second document fail too, so there are two failures.
    real_extract_file = extract.extract_file

    def extract_file(path, *, force=False, allow_api=True):
        if not allow_api:
            raise ExtractionFailed(f"second failure for {Path(path).name}")
        return real_extract_file(path, force=force, allow_api=allow_api)

    monkeypatch.setattr(extract, "extract_file", extract_file)
    summary = extract.extract_documents(session, docs)
    assert summary["failed"] == 2 and summary["error"].startswith("Gemini API error on gemini-2.5-flash")


def test_fresh_api_result_refreshes_the_other_copies_of_the_same_file(session, gemini_mode, tmp_path,
                                                                     monkeypatch, capsys):
    pdf = fake_pdf(tmp_path, 4)
    tobe, asis = (doc_for_file(session, pdf, 4, scenario=s) for s in ("tobe", "asis"))
    other = doc_for_file(session, fake_pdf(tmp_path, 9), 9)
    client = ok_client(monkeypatch)
    calls = spy_extract_document(monkeypatch)

    row = extract.extract_document(session, tobe)

    assert client.models.calls == ["gemini-2.5-flash"]  # one API call for both copies
    assert calls == [(tobe.doc_id, True), (asis.doc_id, False)]  # the sibling: cache only, no recursion
    assert row.from_cache is False and row.model == "gemini-2.5-flash"
    assert asis.extraction is not None and asis.extraction.id != row.id
    assert asis.extraction.json == row.json and asis.extraction.from_cache is True
    assert asis.extraction.model == "gemini-2.5-flash" and asis.doc_type == "credit_note"
    assert other.extraction is None  # a different file is not touched
    assert f"doc={asis.doc_id} refreshed from the new result of doc={tobe.doc_id}" in capsys.readouterr().out

    calls.clear()
    extract.extract_document(session, asis)  # a cache hit: nothing new to share
    assert calls == [(asis.doc_id, True)] and client.models.calls == ["gemini-2.5-flash"]

    calls.clear()
    extract.extract_document(session, asis, force=True)  # a forced re-extraction refreshes the other copy
    assert calls == [(asis.doc_id, True), (tobe.doc_id, False)]
    assert client.models.calls == ["gemini-2.5-flash"] * 2


def test_fixture_result_does_not_refresh_siblings(session, monkeypatch):
    tobe, asis = (make_doc(session, "data/invoices/04_bright_agency_credit_note.pdf", scenario=s)
                  for s in ("tobe", "asis"))  # same default file_hash
    calls = spy_extract_document(monkeypatch)
    assert extract.extract_document(session, tobe).model == FIXTURE_MODEL
    assert calls == [(tobe.doc_id, True)] and asis.extraction is None


def test_sibling_with_a_missing_file_does_not_undo_the_fresh_result(session, gemini_mode, tmp_path,
                                                                   monkeypatch, capsys):
    pdf = fake_pdf(tmp_path, 4)
    doc = doc_for_file(session, pdf, 4)
    sibling = make_doc(session, str(tmp_path / "gone" / pdf.name), sample_no=4, scenario="asis",
                       file_hash=doc.file_hash)
    ok_client(monkeypatch)

    row = extract.extract_document(session, doc)

    assert row is not None and doc.extraction is row and doc.doc_type == "credit_note"
    assert sibling.extraction is None
    assert f"doc={sibling.doc_id} unavailable: {pdf.name} not found" in capsys.readouterr().out


def test_sibling_read_error_does_not_undo_the_fresh_result(session, gemini_mode, tmp_path, monkeypatch, capsys):
    pdf = fake_pdf(tmp_path, 4)
    doc = doc_for_file(session, pdf, 4)
    sibling_pdf = fake_pdf(tmp_path / "asis", 4)
    sibling = doc_for_file(session, sibling_pdf, 4, scenario="asis")
    ok_client(monkeypatch)
    real_sha = extract.file_sha256

    def file_sha256(path):
        if Path(path) == sibling_pdf:
            raise PermissionError("access denied")
        return real_sha(path)

    monkeypatch.setattr(extract, "file_sha256", file_sha256)
    row = extract.extract_document(session, doc)

    assert row is not None and doc.extraction is row and sibling.extraction is None
    assert f"doc={sibling.doc_id} not refreshed: access denied" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


@pytest.fixture()
def cli_invoices(tmp_path, monkeypatch):
    """Stand-in PDFs in a temporary invoices folder; the PDF generator is not run."""
    invoices = tmp_path / "invoices"
    for spec in world.DOCUMENTS:
        fake_pdf(invoices, spec.no)
    monkeypatch.setattr(config, "INVOICES_DIR", invoices)
    monkeypatch.setattr(seed, "ensure_pdfs", lambda: None)
    return invoices


def test_cli_without_key_and_cache_misses_exits_2(cli_invoices, gemini_mode, monkeypatch, capsys):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert extract.main(["--only", "1,5", "--no-db"]) == 2
    out = capsys.readouterr().out
    assert "GEMINI_API_KEY" in out and ".env.example" in out
    assert "01_nordwind_invoice.pdf" in out and "05_fitout_partners_invoice.pdf" in out


def test_cli_works_offline_from_the_cache(cli_invoices, gemini_mode, monkeypatch, capsys):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    pdf = cli_invoices / world.DOCUMENT_BY_NO[4].filename
    cached = _counting_fake([])(b"", pdf.name)
    extract.write_cache(extract.file_sha256(pdf), cached, pdf.name)

    assert extract.main(["--only", "4", "--no-db"]) == 0
    out = capsys.readouterr().out
    assert "cache=hit" in out and "CN-2026-0031" in out and "-1,800.00" in out


def test_cli_reports_a_failed_file_and_continues_with_the_rest(cli_invoices, gemini_mode, monkeypatch, capsys):
    calls: list[str] = []
    succeed = _counting_fake(calls)

    def call_gemini(pdf_bytes: bytes, file_name: str) -> ExtractionResult:
        if file_name.startswith("05_"):
            raise RuntimeError("unexpected SDK bug")  # not an ExtractionUnavailable: must not stop the batch
        if file_name.startswith("08_"):
            raise ExtractionFailed("Gemini API error on gemini-2.5-flash: 500 INTERNAL: boom")
        return succeed(pdf_bytes, file_name)

    monkeypatch.setattr(extract, "call_gemini", call_gemini)
    assert extract.main(["--only", "4,5,8,12", "--no-db"]) == 1

    out = capsys.readouterr().out
    assert calls == ["04_bright_agency_credit_note.pdf", "12_harbor_freight_invoice.pdf"]  # 12 still ran
    assert "[extract] file=05_fitout_partners_invoice.pdf FAILED: RuntimeError: unexpected SDK bug" in out
    assert "[extract] file=08_quickprint_invoice.pdf FAILED: Gemini API error on gemini-2.5-flash" in out
    table = {line.split()[1]: line for line in out.splitlines() if line[:3].strip().isdigit()}
    assert table["05_fitout_partners_invoice.pdf"].rstrip().endswith("FAILED")
    assert table["08_quickprint_invoice.pdf"].rstrip().endswith("FAILED")
    assert "CN-2026-0031" in table["12_harbor_freight_invoice.pdf"]
    assert sorted(p.name for p in gemini_mode.iterdir()) == sorted(
        f"{extract.file_sha256(cli_invoices / world.DOCUMENT_BY_NO[no].filename)}.json" for no in (4, 12))


def test_cli_fixture_mode_updates_both_scenarios_in_the_database(cli_invoices, session, capsys):
    pdf = cli_invoices / world.DOCUMENT_BY_NO[4].filename
    sha = extract.file_sha256(pdf)
    docs = [make_doc(session, pdf.name, scenario=s, file_hash=sha) for s in config.SCENARIOS]

    assert extract.main(["--only", "4"]) == 0

    out = capsys.readouterr().out
    assert "fixture" in out and "2 inbound document(s) updated" in out
    session.expire_all()
    for doc in docs:
        refreshed = session.scalars(select(InboundDocument).where(InboundDocument.doc_id == doc.doc_id)).one()
        assert refreshed.doc_type == "credit_note"
        assert refreshed.extraction is not None and refreshed.extraction.model == FIXTURE_MODEL


def test_cli_rejects_unknown_sample_numbers(cli_invoices):
    with pytest.raises(SystemExit):
        extract.main(["--only", str(len(world.DOCUMENTS) + 1), "--no-db"])


# --------------------------------------------------------------------------------------------
# CLI: test set v2 (UBL e-invoice parsed, email body skipped, only PDFs need the model)
# --------------------------------------------------------------------------------------------


class _FakeUbl:
    UBL_MODEL = "UBL e-invoice (parsed, no model call)"

    @staticmethod
    def parse_ubl(data: bytes) -> dict:
        return fixture_extraction("03_bright_agency_invoice")


@pytest.fixture()
def cli_invoices_v2(tmp_path, monkeypatch):
    """Stand-in files for test set v2 in a temporary folder (the generator is not run); UBL parsing is faked."""
    folder = tmp_path / "invoices_v2"
    folder.mkdir()
    for spec in world.documents_for("v2"):
        (folder / spec.filename).write_bytes(f"%PDF-1.4 fake v2 sample {spec.no}\n".encode())
    monkeypatch.setattr(config, "INVOICES_V2_DIR", folder)
    monkeypatch.setattr(extract, "_ubl_module", lambda: _FakeUbl)
    return folder


def v2_file(no: int) -> str:
    return next(spec.filename for spec in world.documents_for("v2") if spec.no == no)


def test_cli_v2_parses_the_ubl_and_skips_the_email_body_without_a_key(cli_invoices_v2, gemini_mode, monkeypatch,
                                                                      capsys):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(extract, "call_gemini", lambda *a: pytest.fail("no model call for UBL or email body"))
    assert v2_file(11).endswith(".xml") and v2_file(14).endswith(".txt")

    assert extract.main(["--dataset", "v2", "--only", "11,14", "--no-db"]) == 0

    out = capsys.readouterr().out
    table = {line.split()[1]: line for line in out.splitlines() if line[:3].strip().isdigit()}
    assert _FakeUbl.UBL_MODEL.split()[0] in table[v2_file(11)] and "ubl" in table[v2_file(11)]
    assert table[v2_file(14)].rstrip().endswith("email body only")
    assert list(gemini_mode.iterdir()) == []  # nothing cached


def test_cli_v2_without_key_names_only_the_pdfs_that_need_the_model(cli_invoices_v2, gemini_mode, monkeypatch,
                                                                    capsys):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert extract.main(["--dataset", "v2", "--no-db"]) == 2
    out = capsys.readouterr().out
    assert v2_file(1) in out and v2_file(11) not in out and v2_file(14) not in out
    assert "GEMINI_BACKEND=vertex" in out and ".env.example" in out


def test_cli_v2_fixture_mode_reads_the_v2_fixtures(cli_invoices_v2, tmp_path, monkeypatch, capsys):
    fixtures_v2 = tmp_path / "fixtures_v2"
    fixtures_v2.mkdir()
    record = json.loads((config.FIXTURES_DIR / "03_bright_agency_invoice.json").read_text(encoding="utf-8"))
    (fixtures_v2 / f"{Path(v2_file(21)).stem}.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(config, "FIXTURES_V2_DIR", fixtures_v2)
    assert extract.main(["--dataset", "v2", "--only", "21", "--no-db"]) == 0
    assert "INV-2026-0457" in capsys.readouterr().out


def test_cli_v2_rejects_unknown_numbers(cli_invoices_v2):
    with pytest.raises(SystemExit):
        extract.main(["--dataset", "v2", "--only", str(len(world.documents_for("v2")) + 1), "--no-db"])


def test_cli_v2_generates_missing_files(tmp_path, monkeypatch, gemini_mode):
    from app import invoices_gen

    generated: list[Path] = []
    monkeypatch.setattr(config, "INVOICES_V2_DIR", tmp_path / "empty")
    monkeypatch.setattr(invoices_gen, "generate_all_v2", lambda out_dir: generated.append(out_dir) or [])
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(extract, "_ubl_module", lambda: _FakeUbl)
    extract.main(["--dataset", "v2", "--only", "14", "--no-db"])
    assert generated == [tmp_path / "empty"]
