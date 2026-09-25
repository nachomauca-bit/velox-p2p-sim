"""Paths and environment settings. Everything configurable lives in .env (on Cloud Run: environment variables).

Every setting has a safe local default, so the app runs offline without a .env file. Phase 3 (GCP) changes
environment variables only: the model backend (AI Studio key or Vertex AI), the storage folders (a mounted
Cloud Storage bucket), basic auth and the BigQuery export. See .env.example and docs/DEPLOY_GCP.md.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    """The stripped value of an environment variable; an empty value counts as unset."""
    return os.getenv(name, "").strip() or default


def _env_path(name: str, default: Path) -> Path:
    """A folder from the environment; a relative value is relative to the project root."""
    value = _env(name)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _env_bool(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------------------------
# Storage. The sample PDFs ship with the app under DATA_DIR; INBOUND_DIR, CACHE_DIR and EXPORT_DIR may
# live outside the project (on Cloud Run: folders of a mounted Cloud Storage bucket).
# --------------------------------------------------------------------------------------------

DATA_DIR = _env_path("DATA_DIR", BASE_DIR / "data")
INVOICES_DIR = DATA_DIR / "invoices"  # the 14 case documents (v1)
INVOICES_V2_DIR = DATA_DIR / "invoices_v2"  # test set v2 (26 documents, docs/TEST_SET_V2.md)
INBOUND_DIR = _env_path("INBOUND_DIR", INVOICES_DIR / "inbound")  # documents received by the intake webhook
CACHE_DIR = _env_path("CACHE_DIR", DATA_DIR / "cache")  # extraction and draft cache (never re-call the API)
EXPORT_DIR = _env_path("EXPORT_DIR", DATA_DIR / "export")  # NDJSON export for BigQuery (app/export_bq.py)
DOCS_DIR = BASE_DIR / "docs"
FIXTURES_DIR = BASE_DIR / "tests" / "fixtures"
FIXTURES_V2_DIR = BASE_DIR / "tests" / "fixtures_v2"

DATABASE_URL = _env("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'velox.db').as_posix()}")

# --------------------------------------------------------------------------------------------
# Gemini model access. Same google-genai SDK and the same extraction code for both backends; only the
# client differs (extract._client):
#   aistudio (default)  an API key from Google AI Studio (GEMINI_API_KEY)
#   vertex              Vertex AI in a Google Cloud project (GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION),
#                       authenticated with Application Default Credentials (on Cloud Run: the service account)
# --------------------------------------------------------------------------------------------

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-2.5-flash")

GEMINI_BACKENDS = ("aistudio", "vertex")
GEMINI_BACKEND = _env("GEMINI_BACKEND", "aistudio").lower()
if GEMINI_BACKEND not in GEMINI_BACKENDS:
    print(f"[config] WARNING unknown GEMINI_BACKEND={GEMINI_BACKEND!r}: using 'aistudio' "
          f"(valid: {', '.join(GEMINI_BACKENDS)})")
    GEMINI_BACKEND = "aistudio"
GOOGLE_CLOUD_PROJECT = _env("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = _env("GOOGLE_CLOUD_LOCATION", "global")


def gemini_configured() -> bool:
    """True when the Gemini API can be called: the Vertex backend with a project, or an API key.

    (Vertex without a project but with a key uses Vertex AI express mode.) Reads the settings at call time,
    so tests can monkeypatch them.
    """
    return bool((GEMINI_BACKEND == "vertex" and GOOGLE_CLOUD_PROJECT) or GEMINI_API_KEY)


def gemini_missing_setting() -> str:
    """The variable to set when gemini_configured() is False, for readable messages."""
    return "GOOGLE_CLOUD_PROJECT" if GEMINI_BACKEND == "vertex" else "GEMINI_API_KEY"


def gemini_backend_label() -> str:
    """'Vertex AI (project p, location global)' or 'Google AI Studio' (logs, UI)."""
    if GEMINI_BACKEND == "vertex" and GOOGLE_CLOUD_PROJECT:
        return f"Vertex AI (project {GOOGLE_CLOUD_PROJECT}, location {GOOGLE_CLOUD_LOCATION})"
    if GEMINI_BACKEND == "vertex":
        return "Vertex AI (express mode, API key)"
    return "Google AI Studio"


# "gemini" (default) calls the Gemini API (with disk cache).
# "fixture" reads hand-written ground-truth JSON from tests/fixtures/ and never calls the API;
# the UI labels it clearly as not being a Gemini output. Used by tests and for offline UI work.
EXTRACTOR = _env("EXTRACTOR", "gemini").lower()

# --------------------------------------------------------------------------------------------
# Basic auth in front of the whole app (app/auth.py), one shared password. Empty password = off.
# --------------------------------------------------------------------------------------------

APP_USERNAME = _env("APP_USERNAME", "velox")
APP_PASSWORD = _env("APP_PASSWORD")

# --------------------------------------------------------------------------------------------
# KPI export (app/export_bq.py): NDJSON files in EXPORT_DIR always; loaded into BigQuery when BQ_EXPORT is on.
# --------------------------------------------------------------------------------------------

BQ_EXPORT = _env_bool("BQ_EXPORT")
BQ_PROJECT = _env("BQ_PROJECT", GOOGLE_CLOUD_PROJECT)
BQ_DATASET = _env("BQ_DATASET", "velox_p2p")

SCENARIOS = ("asis", "tobe")
SCENARIO_LABELS = {"asis": "A — As-is", "tobe": "B — To-be"}
DEFAULT_SCENARIO = "asis"

# Confidence threshold for critical fields (used by the phase-2 gate; shown in the UI now).
CONFIDENCE_THRESHOLD = 0.80

FOOTER_TEXT = (
    "Velox P2P control-gate simulator. Mock ERP tables modelled on Dynamics 365 Finance concepts. "
    "Document understanding by a Gemini model via the Google GenAI SDK. "
    "Durations are simulated (see Assumptions)."
)
