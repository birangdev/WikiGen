"""wikigen ingester — full pipeline: collect → plan → generate → write."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from .backends import get_backend
from .cache import HashCache
from .collector import FileChunk, FileCollector, SourceFile, chunk_file
from .config import WikigenConfig
from .prompts import (
    build_context_summary_prompt,
    build_home_page_prompt,
    build_page_prompt,
    build_section_plan_prompt,
)
from .writer import WikiWriter, page_filename


def _file_tree(files: list[SourceFile], max_lines: int = 200) -> str:
    lines = [str(sf.rel_path) for sf in files]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more files)"]
    return "\n".join(lines)


def _find_relevant_chunks(
    page_title: str,
    section: str,
    chunks: list[FileChunk],
    max_chunks: int = 6,
) -> list[str]:
    """Heuristically select the most relevant chunks for a given page title."""
    keywords = set(page_title.lower().split()) | {section.lower()}

    def score(chunk: FileChunk) -> int:
        text_lower = chunk.text.lower()
        path_lower = str(chunk.source_file.rel_path).lower()
        s = 0
        for kw in keywords:
            s += text_lower.count(kw) * 2
            s += path_lower.count(kw) * 5
        if chunk.source_file.is_priority:
            s += 10
        return s

    ranked = sorted(chunks, key=score, reverse=True)
    return [f"{c.label}\n\n{c.text}" for c in ranked[:max_chunks]]


def _append_log(wiki_dir: Path, message: str) -> None:
    """Append a timestamped entry to wiki/log.md if it exists."""
    from datetime import date
    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        return
    today = date.today().isoformat()
    entry = f"\n## {today} — {message}\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry)


class Ingester:
    def __init__(
        self,
        cfg: WikigenConfig,
        project_dir: Path,
        wiki_dir: Path,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.project_dir = project_dir
        self.wiki_dir = wiki_dir
        self.dry_run = dry_run
        self.backend = get_backend(cfg.backend)
        self.writer = WikiWriter(wiki_dir, cfg.wiki)

    def run(self, force: bool = False, concurrency: int = 4) -> None:
        click.echo(click.style("● Collecting files…", fg="cyan"))
        collector = FileCollector(self.project_dir, self.cfg.ingestion)
        files = collector.collect()

        if not files:
            click.echo(click.style("✗ No files found. Check your include/exclude patterns.", fg="red"))
            sys.exit(1)

        click.echo(f"  Found {len(files)} source files.")

        # ── 1. Build project context from priority files ──
        click.echo(click.style("● Building project context…", fg="cyan"))
        priority_pairs = [
            (str(sf.rel_path), sf.content)
            for sf in files
            if sf.is_priority
        ]
        if not priority_pairs:
            # Fall back to first few files
            priority_pairs = [(str(sf.rel_path), sf.content) for sf in files[:5]]

        context_prompt = build_context_summary_prompt(self.cfg.project_name, priority_pairs)
        context_summary = self.backend.complete(
            system="You are an expert technical writer.",
            user=context_prompt,
        )
        click.echo(f"  Context summary: {len(context_summary)} chars")

        # ── 2. Chunk all files ──
        click.echo(click.style("● Chunking source files…", fg="cyan"))
        all_chunks: list[FileChunk] = []
        for sf in files:
            all_chunks.extend(
                chunk_file(sf, self.cfg.ingestion.chunk_size_tokens, self.cfg.ingestion.chunk_overlap_tokens)
            )
        click.echo(f"  {len(all_chunks)} chunks from {len(files)} files.")

        # ── 3. Plan wiki structure ──
        click.echo(click.style("● Planning wiki structure…", fg="cyan"))
        tree = _file_tree(files)
        plan_prompt = build_section_plan_prompt(
            self.cfg.project_name,
            context_summary,
            self.cfg.wiki.sections,
            tree,
        )
        plan_raw = self.backend.complete(
            system="You are an expert technical writer. Respond only with valid JSON.",
            user=plan_prompt,
        )
        import re as _re
        click.echo(f"  Section plan raw (first 200 chars): {plan_raw[:200]!r}")
        section_plan = _parse_section_plan(plan_raw, self.cfg.wiki.sections)
        click.echo(f"  Section plan parsed: { {k: len(v) for k, v in section_plan.items()} }")

        # If plan is empty, derive pages directly from the file tree
        if not any(section_plan.values()):
            click.echo(click.style("⚠  Section plan empty — deriving pages from source files.", fg="yellow"))
            section_plan = _derive_plan_from_files(files, self.cfg.wiki.sections)

        all_page_titles = [t for pages in section_plan.values() for t in pages]
        total_pages = len(all_page_titles) + 1  # +1 for Home
        click.echo(f"  Planned {total_pages} pages across {len(section_plan)} sections.")

        if self.dry_run:
            click.echo(click.style("\n[DRY RUN] Would generate:", fg="yellow"))
            for section, pages in section_plan.items():
                click.echo(f"  {section}/")
                for p in pages:
                    click.echo(f"    {page_filename(p)}")
            click.echo(f"  {page_filename(self.cfg.wiki.index_page)}")
            return

        # ── 4. Prepare output directories ──
        self.writer.ensure_dirs(list(section_plan.keys()))

        cache = HashCache(self.wiki_dir)

        # ── 5. Generate pages in parallel ──
        click.echo(click.style(f"● Generating {total_pages} pages (concurrency={concurrency})…", fg="cyan"))

        tasks: list[tuple[str, str]] = []  # (section, title)
        for section, pages in section_plan.items():
            for title in pages:
                tasks.append((section, title))

        generated = 0
        skipped = 0

        def generate_page(section: str, title: str) -> tuple[str, str, str]:
            """Returns (section, title, filepath)."""
            cache_key = f"{section}/{title}"
            if not force and cache.get(cache_key) == context_summary[:64]:
                return section, title, "SKIP"

            source_chunks = _find_relevant_chunks(title, section, all_chunks)
            sys_prompt, user_prompt = build_page_prompt(
                page_title=title,
                section=section,
                project_name=self.cfg.project_name,
                context_summary=context_summary,
                source_chunks=source_chunks,
                all_page_titles=all_page_titles,
                link_style=self.cfg.wiki.link_style,
            )
            content = self.backend.complete(sys_prompt, user_prompt)
            path = self.writer.write_page(section, title, content)
            return section, title, str(path)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(generate_page, s, t): (s, t) for s, t in tasks}
            for future in as_completed(futures):
                section, title, result = future.result()
                if result == "SKIP":
                    skipped += 1
                    click.echo(f"  {click.style('–', fg='yellow')} {section}/{title} (cached)")
                else:
                    generated += 1
                    cache.set(f"{section}/{title}", context_summary[:64])
                    click.echo(f"  {click.style('✓', fg='green')} {section}/{title}")

        # ── 6. Generate Home page ──
        click.echo(click.style("● Generating Home page…", fg="cyan"))
        sys_prompt, user_prompt = build_home_page_prompt(
            self.cfg.project_name,
            context_summary,
            section_plan,
            self.cfg.wiki.link_style,
        )
        home_content = self.backend.complete(sys_prompt, user_prompt)
        home_path = self.writer.write_home(home_content)
        generated += 1

        cache.save()

        click.echo("")
        click.echo(click.style("✓ Done!", fg="green", bold=True))
        click.echo(f"  Generated : {generated} pages")
        if skipped:
            click.echo(f"  Cached    : {skipped} pages")
        click.echo(f"  Wiki dir  : {self.wiki_dir}")

        # Append to log.md if it exists
        _append_log(self.wiki_dir, f"wikigen ingest — generated {generated} pages, {skipped} cached.")


# ---------------------------------------------------------------------------
# Section plan parsing — robust against malformed LLM output
# ---------------------------------------------------------------------------

def _parse_section_plan(
    raw: str,
    fallback_sections: list[str],
) -> dict[str, list[str]]:
    """Parse the LLM's section plan JSON, handling common failure modes.

    The LLM is asked to return:
        {"Architecture": ["SystemOverview", "RequestLifecycle"], ...}

    In practice it sometimes returns:
        - JSON wrapped in ```json fences
        - A flat list ["SystemOverview", "RequestLifecycle"]
        - A dict where values are strings instead of lists
        - A dict where ALL pages are keys with empty values (flat map)
        - Slugified names instead of PascalCase
        - Valid JSON but every section has an empty list (parse failure fallback)
    """
    import json, re

    # 1. Strip markdown fences
    cleaned = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)

    # 2. Try to parse JSON
    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find any JSON object in the response
        obj = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if obj:
            try:
                parsed = json.loads(obj.group(0))
            except json.JSONDecodeError:
                pass

    if parsed is None:
        click.echo(click.style("⚠  Could not parse section plan JSON — using file-based fallback.", fg="yellow"))
        return {}  # will trigger file-based fallback below

    # 3. Handle flat list: ["PageA", "PageB"] — no sections
    if isinstance(parsed, list):
        click.echo(click.style("⚠  Section plan was a flat list — grouping under single section.", fg="yellow"))
        return {"Overview": [str(p) for p in parsed if p]}

    # 4. Handle dict — normalise values to list[str]
    if isinstance(parsed, dict):
        result: dict[str, list[str]] = {}
        for section, pages in parsed.items():
            if isinstance(pages, list) and pages:
                # Normal case — filter empty strings
                result[section] = [str(p) for p in pages if p]
            elif isinstance(pages, str) and pages:
                # Value was a single string
                result[section] = [pages]
            elif isinstance(pages, dict):
                # Nested dict — extract keys as page titles
                result[section] = [str(k) for k in pages.keys() if k]
            # Skip sections with empty page lists

        if result:
            return result

    # 5. Last resort — detect if the dict is a flat {pageName: ""} map
    # (LLM returned pages as keys with empty/null values)
    if isinstance(parsed, dict):
        all_values_empty = all(
            not v or v == {} or v == []
            for v in parsed.values()
        )
        if all_values_empty:
            click.echo(click.style("⚠  Section plan was a flat key map — grouping pages by name prefix.", fg="yellow"))
            pages = list(parsed.keys())
            return _group_pages_by_prefix(pages, fallback_sections)

    click.echo(click.style("⚠  Section plan unrecognised format — using file-based fallback.", fg="yellow"))
    return {}


def _group_pages_by_prefix(
    page_titles: list[str],
    sections: list[str],
) -> dict[str, list[str]]:
    """Best-effort: assign flat page list to sections by keyword matching."""
    section_keywords: dict[str, list[str]] = {
        "Architecture":     ["architecture", "overview", "structure", "design", "system"],
        "Modules":          ["module", "service", "manager", "handler", "controller", "player", "monitor"],
        "API Reference":    ["api", "cli", "command", "endpoint", "interface", "notification"],
        "Data Models":      ["model", "schema", "settings", "config", "prefs", "defaults", "data"],
        "Configuration":    ["config", "setting", "pref", "option", "launch", "startup"],
        "Development Guide":["guide", "test", "build", "deploy", "contribute", "log"],
    }

    result: dict[str, list[str]] = {s: [] for s in sections}
    unassigned: list[str] = []

    for page in page_titles:
        slug = page.lower().replace("-", " ").replace("_", " ")
        assigned = False
        for section in sections:
            keywords = section_keywords.get(section, [section.lower()])
            if any(kw in slug for kw in keywords):
                result[section].append(page)
                assigned = True
                break
        if not assigned:
            unassigned.append(page)

    # Put unassigned pages in Overview or first section
    catch_all = "Overview" if "Overview" in result else sections[0] if sections else "General"
    if catch_all not in result:
        result[catch_all] = []
    result[catch_all].extend(unassigned)

    # Remove empty sections
    return {s: pages for s, pages in result.items() if pages}


def _derive_plan_from_files(
    files: list,
    sections: list[str],
) -> dict[str, list[str]]:
    """Last-resort plan: turn source filenames into wiki page titles,
    grouped into sections by keyword matching."""
    import re

    def to_pascal(slug: str) -> str:
        """Convert file stem to PascalCase page title."""
        return "".join(w.capitalize() for w in re.split(r"[-_\s]+", slug))

    page_titles = [
        to_pascal(f.path.stem)
        for f in files
        if not f.is_priority and f.path.suffix in {".py", ".swift", ".ts", ".js", ".go", ".rs", ".kt"}
    ][:40]  # cap at 40 pages

    # Reuse keyword grouping
    return _group_pages_by_prefix(page_titles, sections)
