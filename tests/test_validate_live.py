"""The real Gemini extractions committed in data/cache must keep matching the ground truth and the goldens.

Runs `python -m app.validate_live --no-report` in a subprocess (the module sets its own database and extractor
before the app reads the settings) with no API key: it reads the cache only, so it never calls Gemini.
"""
import os
import subprocess
import sys

import pytest

from app import config


def _cached_extractions() -> int:
    return len(list(config.CACHE_DIR.glob("*.json"))) if config.CACHE_DIR.exists() else 0


@pytest.mark.skipif(_cached_extractions() == 0, reason="no real Gemini extractions in data/cache")
def test_the_cached_gemini_extractions_still_give_the_expected_results():
    env = {**os.environ, "GEMINI_API_KEY": "", "GOOGLE_CLOUD_PROJECT": "", "PYTHONPATH": str(config.BASE_DIR),
           "PYTHONIOENCODING": "utf-8"}
    env.pop("DATABASE_URL", None)
    result = subprocess.run([sys.executable, "-m", "app.validate_live", "--no-report"], cwd=config.BASE_DIR,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-3000:]
    assert "Case documents (v1)**: 28 of 28" in output
