"""Shared test setup: isolated SQLite DB, fixture extractor, no API key. Set before app imports."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="velox-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["EXTRACTOR"] = "fixture"
os.environ["GEMINI_API_KEY"] = ""  # tests must never call the Gemini API

import pytest  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.seed import seed_all  # noqa: E402


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
