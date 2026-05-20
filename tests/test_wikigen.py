"""wikigen test suite."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from wikigen.cache import HashCache
from wikigen.collector import FileCollector, SourceFile, chunk_file
from wikigen.config import (
    BackendConfig,
    IngestionConfig,
    WikiConfig,
    WikigenConfig,
    load_config,
    save_config,
)
from wikigen.linter import Linter, _slugify
from wikigen.writer import WikiWriter, page_filename


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal fake project directory."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main entry\ndef main(): pass\n")
    (tmp_path / "src" / "utils.py").write_text("def helper(): return 42\n")
    (tmp_path / "README.md").write_text("# MyProject\nA cool tool.\n")
    (tmp_path / "CLAUDE.md").write_text("Use Python 3.11. Prefer dataclasses.\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'myproject'\nversion = '0.1'\n")
    return tmp_path


@pytest.fixture
def cfg() -> WikigenConfig:
    return WikigenConfig.default(Path("."))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_round_trip(self, tmp_path: Path, cfg: WikigenConfig) -> None:
        path = tmp_path / "wikigen.yaml"
        save_config(cfg, path)
        loaded = load_config(path)
        assert loaded.project_name == cfg.project_name
        assert loaded.backend.name == cfg.backend.name
        assert loaded.wiki.link_style == cfg.wiki.link_style

    def test_default_excludes_git(self, cfg: WikigenConfig) -> None:
        assert "**/.git/**" in cfg.ingestion.exclude_patterns

    def test_resolve_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        bc = BackendConfig(name="claude", api_key_env="ANTHROPIC_API_KEY")
        assert bc.resolve_api_key() == "sk-test"

    def test_resolve_api_key_missing(self) -> None:
        bc = BackendConfig(name="claude", api_key_env="MISSING_KEY_XYZ")
        assert bc.resolve_api_key() is None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class TestCollector:
    def test_collects_py_files(self, tmp_project: Path, cfg: WikigenConfig) -> None:
        collector = FileCollector(tmp_project, cfg.ingestion)
        files = collector.collect()
        paths = {str(sf.rel_path) for sf in files}
        assert "src/main.py" in paths
        assert "src/utils.py" in paths

    def test_priority_files_first(self, tmp_project: Path, cfg: WikigenConfig) -> None:
        collector = FileCollector(tmp_project, cfg.ingestion)
        files = collector.collect()
        priority_indices = [i for i, f in enumerate(files) if f.is_priority]
        non_priority_indices = [i for i, f in enumerate(files) if not f.is_priority]
        assert priority_indices, "Expected at least one priority file"
        if non_priority_indices:
            assert min(priority_indices) < max(non_priority_indices)

    def test_excludes_git_dir(self, tmp_project: Path, cfg: WikigenConfig) -> None:
        git_file = tmp_project / ".git" / "config"
        git_file.parent.mkdir()
        git_file.write_text("[core]\n")
        collector = FileCollector(tmp_project, cfg.ingestion)
        files = collector.collect()
        paths = {str(sf.rel_path) for sf in files}
        assert ".git/config" not in paths

    def test_skips_large_files(self, tmp_project: Path, cfg: WikigenConfig) -> None:
        big = tmp_project / "big.py"
        big.write_text("x = 1\n" * 100_000)
        cfg.ingestion.max_file_size_kb = 1
        collector = FileCollector(tmp_project, cfg.ingestion)
        files = collector.collect()
        paths = {str(sf.rel_path) for sf in files}
        assert "big.py" not in paths


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

class TestChunker:
    def test_small_file_single_chunk(self, tmp_project: Path) -> None:
        sf = SourceFile.read(tmp_project / "src" / "main.py", tmp_project)
        chunks = chunk_file(sf, chunk_size_tokens=2000, overlap_tokens=50)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].total_chunks == 1

    def test_large_file_multiple_chunks(self, tmp_path: Path) -> None:
        big_py = tmp_path / "big.py"
        big_py.write_text("x = 1\n" * 5000)
        sf = SourceFile.read(big_py, tmp_path)
        chunks = chunk_file(sf, chunk_size_tokens=500, overlap_tokens=50)
        assert len(chunks) > 1
        for i, c in enumerate(chunks):
            assert c.chunk_index == i
            assert c.total_chunks == len(chunks)

    def test_chunk_labels(self, tmp_path: Path) -> None:
        f = tmp_path / "module.py"
        f.write_text("a" * 10000)
        sf = SourceFile.read(f, tmp_path)
        chunks = chunk_file(sf, chunk_size_tokens=200, overlap_tokens=20)
        if len(chunks) > 1:
            assert "part 1/" in chunks[0].label


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_set_get(self, tmp_path: Path) -> None:
        cache = HashCache(tmp_path)
        cache.set("src/main.py", "abc123")
        assert cache.get("src/main.py") == "abc123"

    def test_persist_and_reload(self, tmp_path: Path) -> None:
        c1 = HashCache(tmp_path)
        c1.set("file.py", "deadbeef")
        c1.save()

        c2 = HashCache(tmp_path)
        assert c2.get("file.py") == "deadbeef"

    def test_is_changed(self, tmp_path: Path) -> None:
        cache = HashCache(tmp_path)
        assert cache.is_changed("new.py", "sha1") is True
        cache.set("new.py", "sha1")
        assert cache.is_changed("new.py", "sha1") is False
        assert cache.is_changed("new.py", "sha2") is True

    def test_remove(self, tmp_path: Path) -> None:
        cache = HashCache(tmp_path)
        cache.set("x.py", "aaa")
        cache.remove("x.py")
        assert cache.get("x.py") is None


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

class TestWriter:
    def test_write_page(self, tmp_path: Path) -> None:
        writer = WikiWriter(tmp_path / "wiki", WikiConfig())
        content = "---\ntitle: TestPage\n---\n\n## Heading\nContent here.\n"
        path = writer.write_page("Architecture", "TestPage", content)
        assert path.exists()
        assert path.read_text() == content

    def test_slugify_section(self, tmp_path: Path) -> None:
        writer = WikiWriter(tmp_path / "wiki", WikiConfig())
        path = writer.write_page("Data Models", "UserModel", "# User\n")
        assert "data-models" in str(path)

    def test_page_filename(self) -> None:
        assert page_filename("RequestLifecycle") == "request-lifecycle.md"
        assert page_filename("Home") == "home.md"


# ---------------------------------------------------------------------------
# Linter
# ---------------------------------------------------------------------------

class TestLinter:
    def _make_wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        arch = wiki / "architecture"
        arch.mkdir()

        (wiki / "home.md").write_text(
            "---\ntitle: Home\ndescription: Index\ntags: []\nrelated: []\n---\n\n"
            "See [[Overview]] and [[MissingPage]].\n"
        )
        (arch / "overview.md").write_text(
            "---\ntitle: Overview\ndescription: x\ntags: []\nrelated: []\n---\n\n## Intro\nHello.\n"
        )
        # Orphan: never linked
        (arch / "orphan-page.md").write_text(
            "---\ntitle: OrphanPage\ndescription: x\ntags: []\nrelated: []\n---\n\n## Orphan\n"
        )
        return wiki

    def test_detects_broken_link(self, tmp_path: Path) -> None:
        wiki = self._make_wiki(tmp_path)
        linter = Linter(wiki)
        issues = linter.run()
        kinds = {i.kind for i in issues}
        assert "broken_link" in kinds

    def test_detects_orphan(self, tmp_path: Path) -> None:
        wiki = self._make_wiki(tmp_path)
        linter = Linter(wiki)
        issues = linter.run()
        orphan_issues = [i for i in issues if i.kind == "orphan"]
        assert any("orphan-page" in str(i.page) for i in orphan_issues)

    def test_clean_wiki(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "home.md").write_text(
            "---\ntitle: Home\ndescription: x\ntags: []\nrelated: []\n---\n\n[[About]]\n"
        )
        (wiki / "about.md").write_text(
            "---\ntitle: About\ndescription: x\ntags: []\nrelated: []\n---\n\nContent.\n"
        )
        linter = Linter(wiki)
        issues = linter.run()
        # home links to about, about is not orphaned, no broken links
        broken = [i for i in issues if i.kind == "broken_link"]
        assert not broken

    def test_slugify(self) -> None:
        assert _slugify("RequestLifecycle") == "request-lifecycle"
        assert _slugify("API Reference") == "api-reference"


# ---------------------------------------------------------------------------
# Agent instructions
# ---------------------------------------------------------------------------

class TestAgentInstructions:
    def test_writes_all_files(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        wiki_dir = tmp_path / "wiki"
        results = write_all(tmp_path, wiki_dir, "testproject")
        paths = [str(p) for _, p, _ in results]
        assert any("CLAUDE.md" in p for p in paths)
        assert any(".cursorrules" in p for p in paths)
        assert any("copilot-instructions.md" in p for p in paths)
        assert any(".aider.conf.yml" in p for p in paths)
        assert any("rules" in p for p in paths)

    def test_fresh_files_action_is_created(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        results = write_all(tmp_path, tmp_path / "wiki", "proj")
        actions = {str(p.name): action for _, p, action in results}
        assert actions.get("CLAUDE.md") == "created"
        assert actions.get(".cursorrules") == "created"

    def test_existing_file_no_wikigen_section_appends(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all, WIKIGEN_BEGIN
        # Simulate a handcrafted CLAUDE.md with no wikigen section
        existing = "# My Project\n\nUse Python 3.11. Prefer dataclasses.\n"
        (tmp_path / "CLAUDE.md").write_text(existing)
        write_all(tmp_path, tmp_path / "wiki", "proj")
        result = (tmp_path / "CLAUDE.md").read_text()
        # Original content preserved
        assert "Use Python 3.11" in result
        # wikigen section appended
        assert WIKIGEN_BEGIN in result
        # Original content comes BEFORE wikigen section
        assert result.index("Use Python 3.11") < result.index(WIKIGEN_BEGIN)

    def test_existing_file_with_wikigen_section_updates_only_that_section(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all, WIKIGEN_BEGIN, WIKIGEN_END
        # Simulate file that already has a wikigen block from a previous run
        existing = (
            "# My Project\n\nTeam rules here.\n\n"
            f"{WIKIGEN_BEGIN}\n# old wikigen content\n{WIKIGEN_END}\n"
        )
        (tmp_path / "CLAUDE.md").write_text(existing)
        results = write_all(tmp_path, tmp_path / "wiki", "proj")
        actions = {str(p.name): action for _, p, action in results}
        assert actions.get("CLAUDE.md") == "updated"
        content = (tmp_path / "CLAUDE.md").read_text()
        # Team rules preserved
        assert "Team rules here" in content
        # Old wikigen content replaced
        assert "old wikigen content" not in content
        # New wikigen content present
        assert "wikigen ingest" in content or "wikigen lint" in content

    def test_second_run_is_idempotent(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        content_after_first = (tmp_path / "CLAUDE.md").read_text()
        results = write_all(tmp_path, tmp_path / "wiki", "proj")
        actions = {str(p.name): action for _, p, action in results}
        content_after_second = (tmp_path / "CLAUDE.md").read_text()
        assert actions.get("CLAUDE.md") == "updated"
        # Content should be identical after two runs
        assert content_after_first == content_after_second

    def test_log_md_not_overwritten(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        log = wiki_dir / "log.md"
        log.write_text("# existing log\n")
        write_all(tmp_path, wiki_dir, "proj")
        assert log.read_text() == "# existing log\n"

    def test_claude_md_contains_key_sections(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "myproject")
        claude = (tmp_path / "CLAUDE.md").read_text()
        assert "myproject" in claude
        assert "raw/" in claude
        assert "wiki/home.md" in claude
        assert "wikigen lint" in claude
        assert "wikigen ingest" in claude

    def test_cursorrules_mentions_wikigen(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        cr = (tmp_path / ".cursorrules").read_text()
        assert "wikigen" in cr
        assert "home.md" in cr

    def test_copilot_instructions_in_github_dir(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        p = tmp_path / ".github" / "copilot-instructions.md"
        assert p.exists()
        assert "wiki" in p.read_text()
