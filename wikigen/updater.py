"""wikigen updater — incremental re-processing of changed source files."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from .backends import get_backend
from .cache import HashCache
from .collector import FileCollector, SourceFile, chunk_file
from .config import WikigenConfig
from .prompts import build_context_summary_prompt, build_page_prompt
from .writer import WikiWriter


class Updater:
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

    def run(self, concurrency: int = 4) -> None:
        if not self.wiki_dir.exists():
            click.echo(
                click.style("✗ Wiki directory not found. Run: ", fg="red")
                + click.style("wikigen ingest", bold=True)
            )
            sys.exit(1)

        cache = HashCache(self.wiki_dir)

        click.echo(click.style("● Scanning for changes…", fg="cyan"))
        collector = FileCollector(self.project_dir, self.cfg.ingestion)
        current_files = collector.collect()

        current_paths = {str(sf.rel_path) for sf in current_files}
        cached_paths = cache.all_keys()

        # Detect changes
        changed: list[SourceFile] = []
        for sf in current_files:
            key = str(sf.rel_path)
            if cache.is_changed(key, sf.sha256):
                changed.append(sf)

        deleted = cached_paths - current_paths

        if not changed and not deleted:
            click.echo(click.style("✓ Everything up to date.", fg="green"))
            return

        click.echo(f"  Changed  : {len(changed)} file(s)")
        click.echo(f"  Deleted  : {len(deleted)} file(s)")

        if self.dry_run:
            click.echo(click.style("\n[DRY RUN] Would re-process:", fg="yellow"))
            for sf in changed:
                click.echo(f"  ~ {sf.rel_path}")
            for p in deleted:
                click.echo(f"  ✗ {p}")
            return

        # ── Remove wiki pages for deleted source files ──
        for rel_path in deleted:
            # Best-effort: find wiki pages that mention this file in their source
            cache.remove(rel_path)
            click.echo(f"  {click.style('✗', fg='red')} Removed cache entry: {rel_path}")

        # ── Re-generate context summary ──
        priority_pairs = [
            (str(sf.rel_path), sf.content)
            for sf in current_files
            if sf.is_priority
        ]
        if not priority_pairs:
            priority_pairs = [(str(sf.rel_path), sf.content) for sf in current_files[:5]]

        click.echo(click.style("● Refreshing project context…", fg="cyan"))
        context_summary = self.backend.complete(
            system="You are an expert technical writer.",
            user=build_context_summary_prompt(self.cfg.project_name, priority_pairs),
        )

        # ── Load existing section plan from wiki or re-use section structure ──
        # Discover all existing wiki page paths to know current section→page mapping
        existing_pages: dict[str, list[str]] = {}
        if self.wiki_dir.exists():
            for section_dir in self.wiki_dir.iterdir():
                if section_dir.is_dir() and not section_dir.name.startswith("."):
                    pages = [
                        p.stem for p in section_dir.glob("*.md")
                    ]
                    if pages:
                        existing_pages[section_dir.name] = pages

        # Build a reverse lookup: page_slug → (section, title)
        all_page_titles = [t for pages in existing_pages.values() for t in pages]

        # ── Chunk changed files only ──
        all_chunks = []
        for sf in changed:
            all_chunks.extend(
                chunk_file(sf, self.cfg.ingestion.chunk_size_tokens, self.cfg.ingestion.chunk_overlap_tokens)
            )

        # ── Find wiki pages that are "about" the changed source files ──
        # Strategy: look for wiki pages that mention the changed filenames
        pages_to_update: list[tuple[str, str]] = []
        changed_names = {sf.path.stem.lower() for sf in changed}

        for section_slug, page_slugs in existing_pages.items():
            for page_slug in page_slugs:
                wiki_path = self.wiki_dir / section_slug / (page_slug + ".md")
                if not wiki_path.exists():
                    continue
                content = wiki_path.read_text(encoding="utf-8", errors="replace").lower()
                # If any changed filename appears in the page, mark it for update
                if any(name in content for name in changed_names):
                    pages_to_update.append((section_slug, page_slug))

        if not pages_to_update:
            # Re-generate all pages in the first section as a safe fallback
            first_section = next(iter(existing_pages), None)
            if first_section:
                pages_to_update = [(first_section, p) for p in existing_pages[first_section]]

        click.echo(f"  {len(pages_to_update)} wiki page(s) need updating.")

        def update_page(section_slug: str, page_slug: str) -> tuple[str, str]:
            # Reconstruct a human-readable title from the slug
            title = page_slug.replace("-", " ").title()
            from .ingester import _find_relevant_chunks
            source_chunks_text = _find_relevant_chunks(title, section_slug, all_chunks)

            sys_prompt, user_prompt = build_page_prompt(
                page_title=title,
                section=section_slug,
                project_name=self.cfg.project_name,
                context_summary=context_summary,
                source_chunks=source_chunks_text,
                all_page_titles=all_page_titles,
                link_style=self.cfg.wiki.link_style,
            )
            content = self.backend.complete(sys_prompt, user_prompt)

            out_path = self.wiki_dir / section_slug / (page_slug + ".md")
            out_path.write_text(content, encoding="utf-8")
            return section_slug, page_slug

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(update_page, s, p): (s, p) for s, p in pages_to_update}
            for future in as_completed(futures):
                section_slug, page_slug = future.result()
                click.echo(f"  {click.style('✓', fg='green')} {section_slug}/{page_slug}")

        # Update cache for changed files
        for sf in changed:
            cache.set(str(sf.rel_path), sf.sha256)
        cache.save()

        click.echo("")
        click.echo(click.style("✓ Update complete.", fg="green", bold=True))
        click.echo(f"  Updated {len(pages_to_update)} wiki page(s).")

        # Append to log.md
        from .ingester import _append_log
        _append_log(self.wiki_dir, f"wikigen update — {len(changed)} source files changed, {len(pages_to_update)} wiki pages updated.")
