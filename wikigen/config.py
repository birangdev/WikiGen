"""wikigen configuration — loads, validates, and saves wikigen.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BackendConfig:
    name: str = "claude"
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""          # for Ollama or custom OpenAI-compat endpoints
    max_tokens: int = 4096
    temperature: float = 0.2

    def resolve_api_key(self) -> str | None:
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        # Sensible defaults
        defaults = {
            "claude": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "ollama": None,
        }
        env = defaults.get(self.name)
        return os.environ.get(env) if env else None


@dataclass
class IngestionConfig:
    include_patterns: list[str] = field(default_factory=lambda: ["**/*"])
    exclude_patterns: list[str] = field(default_factory=lambda: [
        "**/.git/**",
        "**/node_modules/**",
        "**/__pycache__/**",
        "**/.venv/**",
        "**/venv/**",
        "**/.env",
        "**/dist/**",
        "**/build/**",
        "**/*.pyc",
        "**/*.egg-info/**",
        "**/wiki/**",
    ])
    max_file_size_kb: int = 256
    chunk_size_tokens: int = 6000      # ~24k chars, safe for most models
    chunk_overlap_tokens: int = 200


@dataclass
class WikiConfig:
    sections: list[str] = field(default_factory=lambda: [
        "Overview",
        "Architecture",
        "Modules",
        "Data Models",
        "API Reference",
        "Configuration",
        "Development Guide",
    ])
    index_page: str = "Home"
    link_style: str = "wikilink"       # "wikilink" ([[Page]]) or "markdown" ([Page](Page.md))
    front_matter: bool = True          # add YAML front matter to each page


@dataclass
class WikigenConfig:
    project_name: str = ""
    backend: BackendConfig = field(default_factory=BackendConfig)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    wiki: WikiConfig = field(default_factory=WikiConfig)

    @classmethod
    def default(cls, project_dir: Path) -> "WikigenConfig":
        cfg = cls()
        cfg.project_name = project_dir.name
        cfg.backend = BackendConfig(
            name="claude",
            model="claude-sonnet-4-20250514",
            api_key_env="ANTHROPIC_API_KEY",
        )
        return cfg


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses to plain dicts."""
    import dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    return obj


def save_config(cfg: WikigenConfig, path: Path) -> None:
    data = _to_dict(cfg)
    # Add helpful comments by writing a template
    template = f"""\
# wikigen configuration
# https://github.com/your-org/wikigen

project_name: {cfg.project_name!r}

backend:
  # Supported: claude | openai | ollama
  name: {cfg.backend.name!r}
  # Model name — leave empty to use the backend default
  model: {cfg.backend.model!r}
  # Environment variable that holds your API key
  api_key_env: {cfg.backend.api_key_env!r}
  # base_url: "http://localhost:11434"  # Uncomment for Ollama
  max_tokens: {cfg.backend.max_tokens}
  temperature: {cfg.backend.temperature}

ingestion:
  include_patterns: {yaml.dump(cfg.ingestion.include_patterns, default_flow_style=True).strip()}
  exclude_patterns:
{chr(10).join('    - ' + repr(p) for p in cfg.ingestion.exclude_patterns)}
  max_file_size_kb: {cfg.ingestion.max_file_size_kb}
  chunk_size_tokens: {cfg.ingestion.chunk_size_tokens}
  chunk_overlap_tokens: {cfg.ingestion.chunk_overlap_tokens}

wiki:
  sections: {yaml.dump(cfg.wiki.sections, default_flow_style=True).strip()}
  index_page: {cfg.wiki.index_page!r}
  # link_style: wikilink  (use [[PageName]]) or markdown (use [PageName](PageName.md))
  link_style: {cfg.wiki.link_style!r}
  front_matter: {str(cfg.wiki.front_matter).lower()}
"""
    path.write_text(template, encoding="utf-8")


def load_config(path: Path) -> WikigenConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    backend_raw = raw.get("backend", {})
    backend = BackendConfig(
        name=backend_raw.get("name", "claude"),
        model=backend_raw.get("model", ""),
        api_key_env=backend_raw.get("api_key_env", "ANTHROPIC_API_KEY"),
        base_url=backend_raw.get("base_url", ""),
        max_tokens=backend_raw.get("max_tokens", 4096),
        temperature=backend_raw.get("temperature", 0.2),
    )

    ing_raw = raw.get("ingestion", {})
    ingestion = IngestionConfig(
        include_patterns=ing_raw.get("include_patterns", ["**/*"]),
        exclude_patterns=ing_raw.get("exclude_patterns", IngestionConfig().exclude_patterns),
        max_file_size_kb=ing_raw.get("max_file_size_kb", 256),
        chunk_size_tokens=ing_raw.get("chunk_size_tokens", 6000),
        chunk_overlap_tokens=ing_raw.get("chunk_overlap_tokens", 200),
    )

    wiki_raw = raw.get("wiki", {})
    wiki = WikiConfig(
        sections=wiki_raw.get("sections", WikiConfig().sections),
        index_page=wiki_raw.get("index_page", "Home"),
        link_style=wiki_raw.get("link_style", "wikilink"),
        front_matter=wiki_raw.get("front_matter", True),
    )

    return WikigenConfig(
        project_name=raw.get("project_name", path.parent.name),
        backend=backend,
        ingestion=ingestion,
        wiki=wiki,
    )
