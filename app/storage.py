"""Prepare the folders the app writes to, before the web server starts (the Docker image runs this first).

- Creates STATE_DIR (when set), the webhook inbound folder, the cache, the export folder and the database folder.
- Copies the real Gemini extractions and drafts shipped with the app (config.BUNDLED_CACHE_DIR, data/cache in the
  repository and the image) into CACHE_DIR when the cache lives elsewhere: a container volume, a Cloud Storage
  bucket. Existing files are never overwritten, so extractions made on the server are kept.

A failure is reported but never stops the app: without the copy, loading a sample set calls the model instead
(or, with no key, says the document is not in the cache).

CLI:  python -m app.storage
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from app import config


def sqlite_folder(url: str) -> Optional[Path]:
    """The folder of a file-based SQLite database URL (None for other databases or in-memory SQLite)."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix):].split("?", 1)[0]
    if not path or path == ":memory:":
        return None
    return Path(path).parent


def seed_cache(source: Optional[Path] = None, target: Optional[Path] = None) -> int:
    """Copy the bundled cache records missing from the cache; returns how many files were copied."""
    source = source or config.BUNDLED_CACHE_DIR
    target = target or config.CACHE_DIR
    if not source.is_dir() or (target.exists() and source.resolve() == target.resolve()):
        return 0
    copied = 0
    for path in sorted(source.rglob("*.json")):
        destination = target / path.relative_to(source)
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied += 1
    return copied


def prepare() -> list[str]:
    """Create the writable folders and seed the cache; returns one log line per action or problem."""
    lines = []
    folders = [config.STATE_DIR, config.INBOUND_DIR, config.CACHE_DIR, config.EXPORT_DIR,
               sqlite_folder(config.DATABASE_URL)]
    for folder in folders:
        if folder is None:
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            lines.append(f"WARNING cannot create {folder}: {type(exc).__name__}: {exc}")
    try:
        copied = seed_cache()
    except OSError as exc:
        lines.append(f"WARNING cache not seeded into {config.CACHE_DIR}: {type(exc).__name__}: {exc}")
    else:
        if copied:
            lines.append(f"copied {copied} bundled cache record(s) into {config.CACHE_DIR}")
    return lines


def main() -> int:
    for line in prepare():
        print(f"[storage] {line}")
    return 0  # never block the start-up


if __name__ == "__main__":
    raise SystemExit(main())
