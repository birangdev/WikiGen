"""wikigen writer — converts LLM output to wiki Markdown files."""

from __future__ import annotations

import re
from pathlib import Path

from .config import WikiConfig


def _slugify(title: str) -> str:
    """Convert a PascalCase or space-separated title to a safe filename."""
    # Insert hyphens before capital letters that follow lowercase letters
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", title)
    # Replace spaces with hyphens
    s = s.replace(" ", "-")
    # Remove anything that isn't alphanumeric or hyphen
    s = re.sub(r"[^a-zA-Z0-9\-]", "", s)
    return s.lower()


def page_filename(title: str) -> str:
    return _slugify(title) + ".md"


class WikiWriter:
    def __init__(self, wiki_dir: Path, cfg: WikiConfig) -> None:
        self.wiki_dir = wiki_dir
        self.cfg = cfg

    def ensure_dirs(self, sections: list[str]) -> None:
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        for section in sections:
            (self.wiki_dir / _slugify(section)).mkdir(parents=True, exist_ok=True)

    def write_page(self, section: str, title: str, content: str) -> Path:
        """Write a wiki page to wiki/<section>/<title>.md.
        Returns the written path."""
        section_dir = self.wiki_dir / _slugify(section)
        section_dir.mkdir(parents=True, exist_ok=True)

        # Convert [[WikiLink]] to [WikiLink](../section/wiki-link.md) if markdown style
        if self.cfg.link_style == "markdown":
            content = _convert_wikilinks_to_markdown(content)

        path = section_dir / page_filename(title)
        path.write_text(content, encoding="utf-8")
        return path

    def write_home(self, content: str) -> Path:
        if self.cfg.link_style == "markdown":
            content = _convert_wikilinks_to_markdown(content)
        path = self.wiki_dir / (page_filename(self.cfg.index_page))
        path.write_text(content, encoding="utf-8")
        return path


def _convert_wikilinks_to_markdown(text: str) -> str:
    """Best-effort conversion of [[PageTitle]] → [PageTitle](page-title.md)."""
    def replace(m: re.Match) -> str:
        title = m.group(1).strip()
        fname = page_filename(title)
        return f"[{title}]({fname})"

    return re.sub(r"\[\[([^\]]+)\]\]", replace, text)
