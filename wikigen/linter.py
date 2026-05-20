"""wikigen linter — broken links, orphaned pages, malformed front matter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Issue types
# ---------------------------------------------------------------------------

@dataclass
class LintIssue:
    kind: str          # "broken_link" | "orphan" | "missing_front_matter"
    page: Path
    detail: str

    def __str__(self) -> str:
        icon = {
            "broken_link": "🔗",
            "orphan": "👻",
            "missing_front_matter": "📋",
        }.get(self.kind, "⚠")
        return f"  {icon}  {self.page.name} — {self.detail}"


# ---------------------------------------------------------------------------
# Linter
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MDLINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


class Linter:
    def __init__(self, wiki_dir: Path) -> None:
        self.wiki_dir = wiki_dir

    def run(self, fix: bool = False) -> list[LintIssue]:
        pages = list(self.wiki_dir.rglob("*.md"))
        pages = [p for p in pages if not p.name.startswith(".")]

        if not pages:
            click.echo(click.style("✗ No wiki pages found.", fg="red"))
            return []

        # Build a set of all page slugs (without extension, lowercase)
        page_slugs: set[str] = {p.stem.lower() for p in pages}
        # Also index by relative path
        page_rel_paths: set[str] = {str(p.relative_to(self.wiki_dir)).lower() for p in pages}

        # Build link graph: which pages link to which
        incoming: dict[str, set[str]] = {p.stem.lower(): set() for p in pages}

        issues: list[LintIssue] = []

        for page in pages:
            content = page.read_text(encoding="utf-8", errors="replace")
            page_slug = page.stem.lower()

            # ── Check front matter ──
            if not _FRONT_MATTER_RE.search(content):
                issues.append(LintIssue("missing_front_matter", page, "No YAML front matter block found."))

            # ── Check [[WikiLinks]] ──
            for m in _WIKILINK_RE.finditer(content):
                target = m.group(1).strip()
                target_slug = _slugify(target)
                if target_slug in incoming:
                    incoming[target_slug].add(page_slug)
                if target_slug not in page_slugs:
                    issues.append(
                        LintIssue("broken_link", page, f"[[{target}]] → no page '{target_slug}' found")
                    )

            # ── Check [text](path) Markdown links (local only) ──
            for m in _MDLINK_RE.finditer(content):
                href = m.group(2).strip()
                if href.startswith("http://") or href.startswith("https://"):
                    continue  # external links are fine
                if href.startswith("#"):
                    continue  # anchor links

                # Resolve relative to this page's directory
                resolved = (page.parent / href).resolve()
                if not resolved.exists():
                    # Check if it's just a slug reference
                    stem = Path(href).stem.lower()
                    if stem not in page_slugs:
                        issues.append(
                            LintIssue("broken_link", page, f"[{m.group(1)}]({href}) → file not found")
                        )
                    else:
                        if stem in incoming:
                            incoming[stem].add(page_slug)

        # ── Check for orphans (no incoming links) ──
        for page in pages:
            slug = page.stem.lower()
            # Home page is exempt
            if slug in ("home", "index", "readme"):
                continue
            if not incoming.get(slug):
                issues.append(LintIssue("orphan", page, "No other page links to this page."))

        # ── Report ──
        if not issues:
            click.echo(click.style(f"✓ No issues found in {len(pages)} wiki pages.", fg="green"))
            return []

        broken = [i for i in issues if i.kind == "broken_link"]
        orphans = [i for i in issues if i.kind == "orphan"]
        fm = [i for i in issues if i.kind == "missing_front_matter"]

        click.echo(click.style(f"Found {len(issues)} issue(s) across {len(pages)} pages:\n", fg="yellow"))

        if broken:
            click.echo(click.style(f"  Broken links ({len(broken)})", bold=True))
            for issue in broken:
                click.echo(str(issue))
            click.echo("")

        if orphans:
            click.echo(click.style(f"  Orphaned pages ({len(orphans)})", bold=True))
            for issue in orphans:
                click.echo(str(issue))
            click.echo("")

        if fm:
            click.echo(click.style(f"  Missing front matter ({len(fm)})", bold=True))
            for issue in fm:
                click.echo(str(issue))
            click.echo("")

        if fix:
            _auto_fix(issues, wiki_dir=self.wiki_dir)

        return issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(title: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", title)
    s = s.replace(" ", "-")
    s = re.sub(r"[^a-zA-Z0-9\-]", "", s)
    return s.lower()


def _auto_fix(issues: list[LintIssue], wiki_dir: Path) -> None:
    """Attempt trivial fixes: add stub front matter to pages that lack it."""
    fm_issues = [i for i in issues if i.kind == "missing_front_matter"]
    if not fm_issues:
        return

    click.echo(click.style(f"\n[--fix] Adding front matter to {len(fm_issues)} page(s)…", fg="cyan"))
    for issue in fm_issues:
        page = issue.page
        content = page.read_text(encoding="utf-8", errors="replace")
        title = page.stem.replace("-", " ").title()
        stub = f"---\ntitle: {title}\ndescription: ''\ntags: []\nrelated: []\n---\n\n"
        page.write_text(stub + content, encoding="utf-8")
        click.echo(f"  {click.style('✓', fg='green')} {page.name}")
