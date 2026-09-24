"""Paths and environment settings. Everything configurable lives in .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
INVOICES_DIR = DATA_DIR / "invoices"
CACHE_DIR = DATA_DIR / "cache"
DOCS_DIR = BASE_DIR / "docs"
FIXTURES_DIR = BASE_DIR / "tests" / "fixtures"

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'velox.db').as_posix()}")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

# "gemini" (default) calls the Gemini API (with disk cache).
# "fixture" reads hand-written ground-truth JSON from tests/fixtures/ and never calls the API;
# the UI labels it clearly as not being a Gemini output. Used by tests and for offline UI work.
EXTRACTOR = os.getenv("EXTRACTOR", "gemini").strip().lower() or "gemini"

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
