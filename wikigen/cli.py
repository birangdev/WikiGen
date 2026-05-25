"""wikigen CLI — entry point for all commands."""

import sys
from pathlib import Path

import click

from .config import load_config, save_config, WikigenConfig
from .agent_instructions import write_all as write_agent_files
from .ingester import Ingester
from .updater import Updater
from .linter import Linter
from . import __version__


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, "-V", "--version")
@click.option(
    "--project-dir",
    "-p",
    default=".",
    show_default=True,
    help="Root directory of the project to analyse.",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--wiki-dir",
    "-w",
    default=None,
    help="Output directory for wiki files (default: <project-dir>/wiki).",
    type=click.Path(file_okay=False, resolve_path=True),
)
@click.option(
    "--backend",
    "-b",
    default=None,
    type=click.Choice(["claude", "claude-code", "openai", "ollama"], case_sensitive=False),
    help="LLM backend to use. Overrides value in wikigen.yaml.",
)
@click.pass_context
def cli(ctx: click.Context, project_dir: str, wiki_dir: str | None, backend: str | None) -> None:
    """
    wikigen — generate and maintain a structured Markdown wiki for any codebase.

    Point it at a project directory and it reads your code, docs, and schemas,
    then uses an LLM to synthesise interlinked wiki notes that survive context
    window limits.

    \b
    Quick start:
      wikigen init          # scaffold wikigen.yaml inside your project
      wikigen ingest        # read codebase → generate wiki
      wikigen update        # re-process only changed files
      wikigen lint          # check for broken/orphaned wiki links
    """
    ctx.ensure_object(dict)
    project_path = Path(project_dir)
    wiki_path = Path(wiki_dir) if wiki_dir else project_path / "wiki"

    ctx.obj["project_dir"] = project_path
    ctx.obj["wiki_dir"] = wiki_path
    ctx.obj["backend_override"] = backend


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--no-agent-files",
    is_flag=True,
    default=False,
    help="Skip generating CLAUDE.md / .cursorrules / copilot-instructions etc.",
)
@click.option(
    "--for",
    "for_tool",
    default=None,
    type=click.Choice(["claude", "cursor", "copilot", "aider", "windsurf", "all"], case_sensitive=False),
    help="Only write the instruction file for this tool (auto-detected if omitted).",
)
@click.pass_context
def init(ctx: click.Context, no_agent_files: bool, for_tool: str | None) -> None:
    """Scaffold wikigen.yaml and multi-tool AI instruction files.

    By default this writes:

    \b
      wikigen.yaml                         main config
      CLAUDE.md                            Claude Code / claude.ai projects
      .cursorrules                         Cursor
      .github/copilot-instructions.md      GitHub Copilot / Copilot Workspace
      .aider.conf.yml                      Aider
      .windsurf/rules                      Windsurf / Codeium
      wiki/log.md                          append-only operations log

    All instruction files teach the agent how to navigate the wiki,
    cite sources, run lint, and keep the knowledge base consistent --
    following Andrej Karpathy's LLM Wiki pattern extended to every
    major coding agent.

    Use --no-agent-files to skip everything except wikigen.yaml.
    """
    project_dir: Path = ctx.obj["project_dir"]
    wiki_dir: Path = ctx.obj["wiki_dir"]
    config_path = project_dir / "wikigen.yaml"

    if config_path.exists():
        click.echo(click.style("⚠  wikigen.yaml already exists.", fg="yellow"))
        if not click.confirm("Overwrite?", default=False):
            sys.exit(0)

    cfg = WikigenConfig.default(project_dir)
    save_config(cfg, config_path)
    click.echo(click.style("✓ ", fg="green") + "wikigen.yaml")

    if not no_agent_files:
        results = write_agent_files(project_dir, wiki_dir, cfg.project_name, for_tool=for_tool)
        action_colours = {"created": "green", "appended": "cyan", "updated": "yellow"}
        action_icons   = {"created": "✓", "appended": "+", "updated": "↻"}
        for label, path, action in results:
            try:
                rel = path.relative_to(project_dir)
            except ValueError:
                rel = path
            tool   = label.split("(")[-1].rstrip(")")
            colour = action_colours.get(action, "green")
            icon   = action_icons.get(action, "✓")
            click.echo(
                click.style(f"{icon} ", fg=colour)
                + f"{rel}  "
                + click.style(f"[{tool}] {action}", dim=True)
            )

    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Edit wikigen.yaml — set your backend and API key env var.")
    click.echo("  2. Run:  " + click.style("wikigen ingest", bold=True))


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--dry-run", is_flag=True, help="Print what would be generated without writing files.")
@click.option("--force", "-f", is_flag=True, help="Regenerate all pages even if unchanged.")
@click.option(
    "--concurrency",
    "-c",
    default=4,
    show_default=True,
    help="Number of parallel LLM requests.",
)
@click.pass_context
def ingest(ctx: click.Context, dry_run: bool, force: bool, concurrency: int) -> None:
    """Read the codebase and generate the wiki from scratch.

    wikigen will:
      1. Walk the project directory and collect source files.
      2. Read CLAUDE.md / README.md / schema files for project context.
      3. Chunk files into LLM-sized windows.
      4. Call your configured LLM backend to generate Markdown wiki pages.
      5. Write interlinked pages to the wiki/ directory.

    Use --force to regenerate everything even if the hash cache says files
    haven't changed.
    """
    project_dir: Path = ctx.obj["project_dir"]
    wiki_dir: Path = ctx.obj["wiki_dir"]
    backend_override: str | None = ctx.obj["backend_override"]

    config_path = project_dir / "wikigen.yaml"
    if not config_path.exists():
        click.echo(
            click.style("✗ wikigen.yaml not found. Run: ", fg="red")
            + click.style("wikigen init", bold=True)
        )
        sys.exit(1)

    cfg = load_config(config_path)
    if backend_override:
        cfg.backend.name = backend_override

    ingester = Ingester(cfg, project_dir, wiki_dir, dry_run=dry_run)
    ingester.run(force=force, concurrency=concurrency)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--dry-run", is_flag=True, help="Show which files would be re-processed.")
@click.option(
    "--concurrency",
    "-c",
    default=4,
    show_default=True,
)
@click.pass_context
def update(ctx: click.Context, dry_run: bool, concurrency: int) -> None:
    """Re-process only files that changed since the last ingest/update.

    wikigen stores a SHA-256 hash of every processed file in
    wiki/.wikigen_cache.json. Only files whose hash has changed (or which
    are new) will be sent to the LLM.  Deleted source files cause their
    corresponding wiki pages to be removed.
    """
    project_dir: Path = ctx.obj["project_dir"]
    wiki_dir: Path = ctx.obj["wiki_dir"]
    backend_override: str | None = ctx.obj["backend_override"]

    config_path = project_dir / "wikigen.yaml"
    if not config_path.exists():
        click.echo(click.style("✗ wikigen.yaml not found. Run: ", fg="red") + "wikigen init")
        sys.exit(1)

    cfg = load_config(config_path)
    if backend_override:
        cfg.backend.name = backend_override

    updater = Updater(cfg, project_dir, wiki_dir, dry_run=dry_run)
    updater.run(concurrency=concurrency)


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--fix", is_flag=True, help="Attempt to auto-fix trivial broken links.")
@click.pass_context
def lint(ctx: click.Context, fix: bool) -> None:
    """Check all wiki pages for broken or orphaned Markdown links.

    Reports:
      - Internal [[WikiLinks]] that don't resolve to an existing page.
      - Standard Markdown links [text](path) pointing to missing files.
      - Wiki pages that are never linked from anywhere (orphans).

    Exit code is non-zero if any issues are found (useful in CI).
    """
    wiki_dir: Path = ctx.obj["wiki_dir"]

    if not wiki_dir.exists():
        click.echo(click.style("✗ Wiki directory not found: ", fg="red") + str(wiki_dir))
        click.echo("Run:  wikigen ingest")
        sys.exit(1)

    linter = Linter(wiki_dir)
    issues = linter.run(fix=fix)

    if issues:
        sys.exit(1)


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
