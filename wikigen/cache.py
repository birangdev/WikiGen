"""wikigen cache — SHA-256 hash tracking for incremental updates."""

from __future__ import annotations

import json
from pathlib import Path


CACHE_FILE = ".wikigen_cache.json"


class HashCache:
    """Persistent map of relative-path → sha256, stored in the wiki directory."""

    def __init__(self, wiki_dir: Path) -> None:
        self.wiki_dir = wiki_dir
        self._path = wiki_dir / CACHE_FILE
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def save(self) -> None:
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get(self, rel_path: str) -> str | None:
        return self._data.get(rel_path)

    def set(self, rel_path: str, sha256: str) -> None:
        self._data[rel_path] = sha256

    def remove(self, rel_path: str) -> None:
        self._data.pop(rel_path, None)

    def all_keys(self) -> set[str]:
        return set(self._data.keys())

    def is_changed(self, rel_path: str, sha256: str) -> bool:
        return self._data.get(rel_path) != sha256
