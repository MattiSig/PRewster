"""Which files of a pull request you have read to the end, kept across restarts."""

from __future__ import annotations

import hashlib
import json
import os

from .config import CACHE_DIR
from .github import ChangedFile

VIEWED_FILE = CACHE_DIR / "viewed.json"
KEEP = 300  # pull requests remembered, least recently touched dropped first


def fingerprint(f: ChangedFile) -> str:
    """Changes whenever the file's diff does, so a new push to it clears the tick."""
    content = f.patch if f.patch is not None else f"{f.status}:{f.sha}:{f.additions}:{f.deletions}"
    return hashlib.sha1(f"{f.previous_filename or ''}\0{content}".encode()).hexdigest()[:16]


class ViewedStore:
    def __init__(self):
        try:
            data = json.loads(VIEWED_FILE.read_text())
            self._data: dict[str, dict[str, str]] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    def is_viewed(self, key: str, f: ChangedFile) -> bool:
        return self._data.get(key, {}).get(f.filename) == fingerprint(f)

    def mark(self, key: str, f: ChangedFile) -> None:
        if self.is_viewed(key, f):
            return
        files = self._data.pop(key, {})
        files[f.filename] = fingerprint(f)
        self._data[key] = files  # re-inserted: most recently touched last
        while len(self._data) > KEEP:
            del self._data[next(iter(self._data))]
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = VIEWED_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data))
            os.replace(tmp, VIEWED_FILE)
        except OSError:
            pass
