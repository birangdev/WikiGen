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
        try:
            section_plan: dict[str, list[str]] = json.loads(plan_raw)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown fences
            import re
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", plan_raw, re.DOTALL)
            if m:
                section_plan = json.loads(m.group(1))
            else:
                click.echo(click.style("⚠  Could not parse section plan, using defaults.", fg="yellow"))
                section_plan = {s: [] for s in self.cfg.wiki.sections}

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
