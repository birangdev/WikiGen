"""wikigen file collection — walks a project tree and chunks files for LLM consumption."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path


from .config import IngestionConfig


def _matches_glob(path_str: str, pattern: str) -> bool:
    """Match a path string against a glob pattern supporting ** wildcards."""
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            regex += "(.+/)?"
            i += 3
        elif pattern[i : i + 2] == "**":
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            regex += "[^/]"
            i += 1
        elif pattern[i] in r".^$+{}[]|()":
            regex += re.escape(pattern[i])
            i += 1
        else:
            regex += pattern[i]
            i += 1
    return bool(re.fullmatch(regex, path_str))


# ---------------------------------------------------------------------------
# Source file representation
# ---------------------------------------------------------------------------

# File extensions we'll read as text
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".scala",
    ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php",
    ".swift", ".m", ".r", ".jl", ".lua", ".ex", ".exs", ".clj", ".hs",
    ".elm", ".vue", ".svelte", ".dart",
    # Markup / config
    ".md", ".mdx", ".rst", ".txt", ".yaml", ".yml", ".toml", ".json",
    ".xml", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # Shell / infra
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".tf", ".hcl",
    ".dockerfile", ".Dockerfile", ".env.example",
    # Misc
    ".sql", ".graphql", ".proto", ".thrift",
}

# Context files that are always ingested first (high-priority)
PRIORITY_FILENAMES = {
    "CLAUDE.md", "README.md", "README.rst", "README.txt",
    "ARCHITECTURE.md", "DESIGN.md", "CONTRIBUTING.md",
    "schema.yaml", "schema.yml", "schema.json",
    "openapi.yaml", "openapi.yml", "swagger.yaml", "swagger.yml",
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "Makefile", "Dockerfile",
}


@dataclass
class SourceFile:
    path: Path              # absolute
    rel_path: Path          # relative to project root
    content: str
    sha256: str
    size_bytes: int
    is_priority: bool = False

    @classmethod
    def read(cls, path: Path, project_root: Path) -> "SourceFile":
        content = path.read_text(encoding="utf-8", errors="replace")
        sha = hashlib.sha256(content.encode()).hexdigest()
        return cls(
            path=path,
            rel_path=path.relative_to(project_root),
            content=content,
            sha256=sha,
            size_bytes=path.stat().st_size,
            is_priority=path.name in PRIORITY_FILENAMES,
        )


@dataclass
class FileChunk:
    source_file: SourceFile
    chunk_index: int
    total_chunks: int
    text: str
    token_estimate: int = 0

    @property
    def label(self) -> str:
        if self.total_chunks == 1:
            return str(self.source_file.rel_path)
        return f"{self.source_file.rel_path} (part {self.chunk_index + 1}/{self.total_chunks})"


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class FileCollector:
    """Walk a project directory and return SourceFile objects."""

    def __init__(self, project_root: Path, cfg: IngestionConfig) -> None:
        self.root = project_root
        self.cfg = cfg

    def collect(self) -> list[SourceFile]:
        files: list[SourceFile] = []
        max_bytes = self.cfg.max_file_size_kb * 1024

        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if self._is_excluded(path):
                continue
            if not self._is_included(path):
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                # Allow extensionless files with known priority names
                if path.name not in PRIORITY_FILENAMES:
                    continue
            if path.stat().st_size > max_bytes:
                continue

            try:
                sf = SourceFile.read(path, self.root)
                files.append(sf)
            except Exception:
                # Binary or unreadable — skip silently
                pass

        # Sort: priority files first, then alphabetically
        files.sort(key=lambda f: (not f.is_priority, f.rel_path))
        return files

    def _is_excluded(self, path: Path) -> bool:
        rel = str(path.relative_to(self.root)).replace("\\", "/")
        for pattern in self.cfg.exclude_patterns:
            if _matches_glob(rel, pattern) or _matches_glob(path.name, pattern):
                return True
        return False

    def _is_included(self, path: Path) -> bool:
        if not self.cfg.include_patterns:
            return True
        rel = str(path.relative_to(self.root)).replace("\\", "/")
        for pattern in self.cfg.include_patterns:
            if _matches_glob(rel, pattern):
                return True
        return False


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def chunk_file(sf: SourceFile, chunk_size_tokens: int, overlap_tokens: int) -> list[FileChunk]:
    """Split a SourceFile into overlapping chunks suitable for LLM context windows."""
    text = sf.content
    chunk_size_chars = chunk_size_tokens * 4
    overlap_chars = overlap_tokens * 4

    if _estimate_tokens(text) <= chunk_size_tokens:
        return [FileChunk(sf, 0, 1, text, _estimate_tokens(text))]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size_chars
        chunk = text[start:end]

        # Try to break at a newline boundary
        if end < len(text):
            last_nl = chunk.rfind("\n")
            if last_nl > chunk_size_chars // 2:
                chunk = chunk[:last_nl + 1]
                end = start + last_nl + 1

        chunks.append(chunk)
        next_start = end - overlap_chars
        if next_start <= start:  # guard against infinite loop if overlap >= chunk size
            next_start = end
        start = next_start
        if start >= len(text):
            break

    total = len(chunks)
    return [
        FileChunk(sf, i, total, c, _estimate_tokens(c))
        for i, c in enumerate(chunks)
    ]
