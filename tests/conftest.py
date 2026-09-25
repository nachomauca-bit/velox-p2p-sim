"""Shared test setup: isolated SQLite DB, fixture extractor, no API key. Set before app imports."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="velox-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["EXTRACTOR"] = "fixture"
os.environ["GEMINI_API_KEY"] = ""  # tests must never call the Gemini API
os.environ["GEMINI_BACKEND"] = "aistudio"  # a developer .env with Vertex settings must not leak into tests
os.environ["GOOGLE_CLOUD_PROJECT"] = ""
os.environ["APP_PASSWORD"] = ""  # basic auth off unless a test turns it on

import pytest  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.seed import seed_all  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_model_state(monkeypatch):
    """The extraction module remembers the working model, unavailable models and the model list per process."""
    from app import extract

    monkeypatch.setattr(extract, "_resolved_model", None)
    monkeypatch.setattr(extract, "_unavailable_models", set())
    monkeypatch.setattr(extract, "_flash_models", None)


@pytest.fixture()
def session():
    """A freshly seeded database (both scenarios, empty inboxes)."""
    init_db(drop=True)
    with SessionLocal() as s:
        seed_all(s)
        yield s


@pytest.fixture()
def tmp_cache_dir(tmp_path, monkeypatch):
    """Point the extraction cache at a temporary directory."""
    from app import config

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    return cache
