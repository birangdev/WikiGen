"""wikigen agent_instructions — generates multi-tool AI instruction files."""

from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# CLAUDE.md  (Claude Code / claude.ai projects)
# ---------------------------------------------------------------------------

def claude_md(project_name: str, wiki_dir_rel: str = "wiki") -> str:
    return f"""\
# {project_name} — wikigen knowledge base

A structured, interlinked Markdown wiki for **{project_name}**, generated and
maintained by [wikigen](https://github.com/your-org/wikigen).
Based on Andrej Karpathy's LLM Wiki pattern.

## Purpose

This wiki is the persistent knowledge layer for `{project_name}`.  It survives
context-window resets by externalising structured knowledge into Markdown files
that any agent or human can read incrementally.

## Folder structure

```
raw/          -- immutable source documents (never modify)
{wiki_dir_rel}/         -- wiki pages (agent's working memory)
{wiki_dir_rel}/home.md  -- table of contents / index
{wiki_dir_rel}/log.md   -- append-only operations log
```

## How to use this wiki (for agents)

**Before answering any question about the project:**
1. Read `{wiki_dir_rel}/home.md` to orient yourself — it lists every page.
2. Read only the pages relevant to the question.
3. Synthesise your answer from those pages and cite them explicitly.
4. If the answer is not in the wiki, say so and offer to create a page.

**Never read the entire `{wiki_dir_rel}/` directory at once.**  Use `home.md`
as your map.  Good answers get filed back into the wiki so knowledge compounds.

## Ingest workflow

When asked to ingest a new source from `raw/`:

1. Read the full source document.
2. Discuss key takeaways with the user before writing.
3. Create a summary page in `{wiki_dir_rel}/` named after the source.
4. Create or update concept pages for each major idea or entity.
5. Add `[[wiki-links]]` throughout to connect related pages.
6. Update `{wiki_dir_rel}/home.md` — add new pages with one-line descriptions.
7. Append to `{wiki_dir_rel}/log.md`: date, source name, what changed.

A single source may touch 10–15 pages.  That is normal and expected.

## Page format

Every wiki page must follow this exact structure:

```markdown
---
title: PageTitle
description: One sentence describing this page.
tags: [tag1, tag2]
related: [OtherPage, AnotherPage]
sources: [raw/source.pdf]
updated: YYYY-MM-DD
---

Main content here. Use ## headings and short paragraphs.

Link to related concepts using [[WikiLinks]] throughout.

## Related pages

- [[related-concept-1]]
- [[related-concept-2]]
```

## Citation rules

- Every factual claim references its source: `(source: filename.pdf)`
- If two sources disagree, note the contradiction explicitly.
- Claims with no source are marked `[needs verification]`.
- Prefer primary sources over summaries.

## Lint / audit

When asked to lint the wiki:

- Check for contradictions between pages.
- Find orphan pages (no inbound `[[links]]` from other pages).
- Identify concepts mentioned but lacking their own page.
- Flag claims that may be outdated given newer sources.
- Verify all pages follow the page format above.
- Report findings as a numbered list with suggested fixes.

Or run:  `wikigen lint`

## wikigen CLI

```bash
wikigen ingest          # re-generate wiki from codebase
wikigen update          # re-process only changed files
wikigen lint            # check for broken links and orphans
```

## Rules

- **Never** modify anything in `raw/`.
- Always update `{wiki_dir_rel}/home.md` and `{wiki_dir_rel}/log.md` after changes.
- Page filenames: lowercase, hyphens (e.g. `auth-module.md`).
- Write in clear, plain language aimed at developers.
- When uncertain how to categorise something, ask the user.
"""


# ---------------------------------------------------------------------------
# .cursorrules  (Cursor)
# ---------------------------------------------------------------------------

def cursorrules(project_name: str, wiki_dir_rel: str = "wiki") -> str:
    return f"""\
# {project_name} — Cursor AI rules

This project uses wikigen to maintain a structured wiki in `{wiki_dir_rel}/`.

## Before answering questions about this codebase

1. Read `{wiki_dir_rel}/home.md` — it is the index of all knowledge pages.
2. Read only the pages relevant to the question.
3. Cite pages explicitly in your response.
4. Never read the entire `{wiki_dir_rel}/` directory at once.

## When editing code

After any significant change, note which wiki pages may be stale and
offer to update them or run `wikigen update`.

## Page format

All pages in `{wiki_dir_rel}/` follow this schema:

```
---
title: PageTitle
description: <one sentence>
tags: [...]
related: [...]
sources: [...]
updated: YYYY-MM-DD
---
```

Links between pages use `[[PageTitle]]` syntax.

## Wiki CLI

```bash
wikigen ingest    # full regeneration
wikigen update    # incremental update
wikigen lint      # audit links and orphans
```

## Rules

- Never modify files in `raw/` — they are immutable source documents.
- Always update `{wiki_dir_rel}/home.md` and `{wiki_dir_rel}/log.md` when adding pages.
- Page filenames: lowercase-with-hyphens.md
"""


# ---------------------------------------------------------------------------
# .github/copilot-instructions.md  (GitHub Copilot / Copilot Workspace)
# ---------------------------------------------------------------------------

def copilot_instructions(project_name: str, wiki_dir_rel: str = "wiki") -> str:
    return f"""\
# Copilot instructions for {project_name}

This project maintains a structured wiki in `{wiki_dir_rel}/` using wikigen.

## Using the wiki

Read `{wiki_dir_rel}/home.md` before answering questions about this project.
It contains the index of all wiki pages.  Fetch only the pages relevant to
the current task — do not load the entire directory.

## Wiki page schema

```yaml
---
title: PageTitle
description: one sentence
tags: [tag1, tag2]
related: [RelatedPage]
sources: [raw/doc.pdf]
updated: YYYY-MM-DD
---
```

Pages link to each other with `[[PageTitle]]` syntax.

## Keeping the wiki current

After making significant code changes, run `wikigen update` to refresh
affected wiki pages, then commit the updated Markdown files alongside
your code changes.

## Constraints

- `raw/` is read-only — never suggest edits to files there.
- `{wiki_dir_rel}/log.md` is append-only.
- `{wiki_dir_rel}/home.md` must be updated whenever pages are added or removed.
"""


# ---------------------------------------------------------------------------
# .aider.conf.yml  (Aider)
# ---------------------------------------------------------------------------

def aider_conf(project_name: str, wiki_dir_rel: str = "wiki") -> str:
    return f"""\
# Aider configuration for {project_name}
# https://aider.chat/docs/config/aider_conf.html

# Auto-read wiki index on session start so Aider knows the project structure
read:
  - {wiki_dir_rel}/home.md

# Treat wiki pages as context files, not editable code
# (Aider will read but not auto-commit them unless explicitly asked)
auto-commits: true

# Instructions appended to every session
system-prompt: |
  This project uses a structured wiki in `{wiki_dir_rel}/`.
  Read `{wiki_dir_rel}/home.md` first to orient yourself.
  When you make significant changes, note which wiki pages may need updating.
  Run `wikigen update` to regenerate stale pages.
  Never modify files in `raw/` — they are immutable source documents.
  Wiki pages use [[WikiLink]] syntax for cross-references.
  Page schema: title, description, tags, related, sources, updated in YAML front matter.
"""


# ---------------------------------------------------------------------------
# .windsurf/rules  (Windsurf / Codeium)
# ---------------------------------------------------------------------------

def windsurf_rules(project_name: str, wiki_dir_rel: str = "wiki") -> str:
    return f"""\
# Windsurf rules for {project_name}

## Knowledge base

This project has a structured wiki in `{wiki_dir_rel}/` maintained by wikigen.

- Always read `{wiki_dir_rel}/home.md` before answering questions about the codebase.
- Read only the relevant pages — never load the whole directory.
- Cite wiki pages when giving answers.

## Wiki page format

Pages use YAML front matter (title, description, tags, related, sources, updated)
and `[[WikiLink]]` syntax for internal links.

## Keeping the wiki fresh

Run `wikigen update` after significant code changes.
Run `wikigen lint` to check for broken links and orphaned pages.

## Constraints

- Never modify files in `raw/`.
- `{wiki_dir_rel}/log.md` is append-only.
- Always update `{wiki_dir_rel}/home.md` when adding or removing pages.
"""


# ---------------------------------------------------------------------------
# log.md  (empty append-only log)
# ---------------------------------------------------------------------------

def log_md(project_name: str) -> str:
    from datetime import date
    today = date.today().isoformat()
    return f"""\
# Wiki operations log

Append-only record of all wiki operations.  Never edit past entries.

---

## {today} — wikigen init

- Initialised wikigen for project `{project_name}`.
- Created `CLAUDE.md`, `.cursorrules`, `.github/copilot-instructions.md`,
  `.aider.conf.yml`, `.windsurf/rules`.
- Created `wiki/home.md` and `wiki/log.md`.
"""


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

WIKIGEN_BEGIN = "<!-- wikigen:begin -->"
WIKIGEN_END   = "<!-- wikigen:end -->"

# YAML files need comment-style markers so the file stays valid YAML
YAML_BEGIN = "# wikigen:begin"
YAML_END   = "# wikigen:end"


def _merge_into_file(path: Path, new_section: str) -> tuple[str, str]:
    """Merge wikigen's section into an existing Markdown/text file.

    Returns (action, final_content) where action is one of:
      "created"  -- file did not exist, written fresh
      "appended" -- file existed with no wikigen section, section appended
      "updated"  -- file existed, wikigen section replaced in place
    """
    fenced = WIKIGEN_BEGIN + "\n" + new_section.strip() + "\n" + WIKIGEN_END + "\n"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fenced, encoding="utf-8")
        return "created", fenced

    existing = path.read_text(encoding="utf-8")

    if WIKIGEN_BEGIN in existing:
        pattern = re.compile(
            re.escape(WIKIGEN_BEGIN) + r".*?" + re.escape(WIKIGEN_END),
            re.DOTALL,
        )
        updated = pattern.sub(fenced.strip(), existing)
        path.write_text(updated, encoding="utf-8")
        return "updated", updated
    else:
        sep = "\n\n---\n\n" if not existing.endswith("\n\n") else "\n---\n\n"
        final = existing.rstrip("\n") + sep + fenced
        path.write_text(final, encoding="utf-8")
        return "appended", final


def _merge_into_yaml_file(path: Path, new_section: str) -> tuple[str, str]:
    """Merge wikigen's section into a YAML file using # comment markers.

    HTML comment markers (used by _merge_into_file) would produce invalid YAML.
    This variant uses '# wikigen:begin' / '# wikigen:end' so the file stays
    parseable by YAML tools regardless of whether a wikigen section is present.
    """
    fenced = YAML_BEGIN + "\n" + new_section.strip() + "\n" + YAML_END + "\n"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fenced, encoding="utf-8")
        return "created", fenced

    existing = path.read_text(encoding="utf-8")

    if YAML_BEGIN in existing:
        pattern = re.compile(
            re.escape(YAML_BEGIN) + r".*?" + re.escape(YAML_END),
            re.DOTALL,
        )
        updated = pattern.sub(fenced.strip(), existing)
        path.write_text(updated, encoding="utf-8")
        return "updated", updated
    else:
        sep = "\n\n" if not existing.endswith("\n\n") else "\n"
        final = existing.rstrip("\n") + sep + fenced
        path.write_text(final, encoding="utf-8")
        return "appended", final



# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

# Maps --for tool names to the instruction file(s) each tool needs.
TOOL_FILES: dict[str, list[str]] = {
    "claude":   ["CLAUDE.md"],
    "cursor":   [".cursorrules"],
    "copilot":  [".github/copilot-instructions.md"],
    "aider":    [".aider.conf.yml"],
    "windsurf": [".windsurf/rules"],
}

# Auto-detect: env vars that indicate a specific tool environment.
_ENV_TOOL_MAP: list[tuple[str, str]] = [
    ("CLAUDE_CODE", "claude"),
]


def _detect_tool() -> str | None:
    """Return the tool name if a known environment variable is set."""
    import os
    for env_var, tool in _ENV_TOOL_MAP:
        if os.environ.get(env_var):
            return tool
    return None


def write_all(
    project_dir: Path,
    wiki_dir: Path,
    project_name: str,
    for_tool: str | None = None,
) -> list[tuple[str, Path, str]]:
    """Write/merge agent instruction files.

    If *for_tool* is given (or auto-detected), only the file(s) relevant to
    that tool are written.  Pass ``for_tool="all"`` or leave it ``None`` to
    write every file (original behaviour).

    Returns list of (label, path, action) where action is
    'created' | 'appended' | 'updated'.
    """
    if for_tool is None:
        for_tool = _detect_tool()  # may still be None → write all

    wiki_dir_rel = wiki_dir.relative_to(project_dir) if wiki_dir.is_relative_to(project_dir) else wiki_dir
    wiki_rel = str(wiki_dir_rel)

    results: list[tuple[str, Path, str]] = []

    def _merge(rel: str, section_content: str, label: str) -> None:
        if for_tool and for_tool != "all" and rel not in TOOL_FILES.get(for_tool, []):
            return
        p = project_dir / rel
        action, _ = _merge_into_file(p, section_content)
        results.append((label, p, action))

    def _merge_yaml(rel: str, section_content: str, label: str) -> None:
        if for_tool and for_tool != "all" and rel not in TOOL_FILES.get(for_tool, []):
            return
        p = project_dir / rel
        action, _ = _merge_into_yaml_file(p, section_content)
        results.append((label, p, action))

    _merge("CLAUDE.md",
           claude_md(project_name, wiki_rel),
           "CLAUDE.md (Claude Code)")

    _merge(".cursorrules",
           cursorrules(project_name, wiki_rel),
           ".cursorrules (Cursor)")

    _merge(".github/copilot-instructions.md",
           copilot_instructions(project_name, wiki_rel),
           ".github/copilot-instructions.md (GitHub Copilot)")

    _merge_yaml(".aider.conf.yml",
                aider_conf(project_name, wiki_rel),
                ".aider.conf.yml (Aider)")

    _merge(".windsurf/rules",
           windsurf_rules(project_name, wiki_rel),
           ".windsurf/rules (Windsurf)")

    # wiki/ + wiki/log.md — append-only, never overwrite
    wiki_dir.mkdir(parents=True, exist_ok=True)
    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        log_path.write_text(log_md(project_name), encoding="utf-8")
        results.append(("wiki/log.md", log_path, "created"))

    # wiki/home.md — placeholder; overwritten by `wikigen ingest`
    home_path = wiki_dir / "home.md"
    if not home_path.exists():
        home_path.write_text(
            f"# {project_name} — Wiki\n\n"
            "_Run `wikigen ingest` to generate the table of contents._\n",
            encoding="utf-8",
        )
        results.append(("wiki/home.md", home_path, "created"))

    # raw/ — immutable source documents, never modified by wikigen
    raw_dir = project_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    gitkeep = raw_dir / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.touch()
        results.append(("raw/", raw_dir, "created"))

    return results
