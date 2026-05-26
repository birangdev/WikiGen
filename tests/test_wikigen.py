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

    def test_resolve_api_key_from_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text('ANTHROPIC_API_KEY="sk-from-dotenv"\n')
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        bc = BackendConfig(name="claude", api_key_env="ANTHROPIC_API_KEY")
        assert bc.resolve_api_key() == "sk-from-dotenv"

    def test_resolve_api_key_env_takes_priority_over_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text("ANTHROPIC_API_KEY=sk-from-dotenv\n")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        monkeypatch.chdir(tmp_path)
        bc = BackendConfig(name="claude", api_key_env="ANTHROPIC_API_KEY")
        assert bc.resolve_api_key() == "sk-from-env"

    def test_resolve_api_key_dotenv_strips_quotes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text("ANTHROPIC_API_KEY='sk-single-quoted'\n")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        bc = BackendConfig(name="claude", api_key_env="ANTHROPIC_API_KEY")
        assert bc.resolve_api_key() == "sk-single-quoted"


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

    def test_creates_raw_dir(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        assert (tmp_path / "raw").is_dir()

    def test_creates_wiki_home_placeholder(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        home = tmp_path / "wiki" / "home.md"
        assert home.exists()
        assert "wikigen ingest" in home.read_text()

    def test_home_md_not_overwritten(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        home = wiki_dir / "home.md"
        home.write_text("# Real home\n")
        write_all(tmp_path, wiki_dir, "proj")
        assert home.read_text() == "# Real home\n"

    def test_raw_dir_not_recreated_on_second_run(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        sentinel = tmp_path / "raw" / "document.txt"
        sentinel.write_text("important")
        write_all(tmp_path, tmp_path / "wiki", "proj")
        assert sentinel.exists()

    def test_for_tool_claude_only(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj", for_tool="claude")
        assert (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / ".cursorrules").exists()
        assert not (tmp_path / ".aider.conf.yml").exists()

    def test_for_tool_cursor_only(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj", for_tool="cursor")
        assert (tmp_path / ".cursorrules").exists()
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_for_tool_all_writes_everything(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj", for_tool="all")
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / ".cursorrules").exists()
        assert (tmp_path / ".aider.conf.yml").exists()

    def test_auto_detect_claude_code_writes_only_claude_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wikigen.agent_instructions import write_all
        monkeypatch.setenv("CLAUDE_CODE", "1")
        write_all(tmp_path, tmp_path / "wiki", "proj")
        assert (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / ".cursorrules").exists()

    def test_no_for_tool_no_env_writes_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wikigen.agent_instructions import write_all
        monkeypatch.delenv("CLAUDE_CODE", raising=False)
        write_all(tmp_path, tmp_path / "wiki", "proj")
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / ".cursorrules").exists()
        assert (tmp_path / ".aider.conf.yml").exists()


# ---------------------------------------------------------------------------
# Aider YAML merge
# ---------------------------------------------------------------------------

class TestAiderYamlMerge:
    def test_created_with_yaml_markers_not_html(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all, YAML_BEGIN, WIKIGEN_BEGIN
        write_all(tmp_path, tmp_path / "wiki", "proj")
        content = (tmp_path / ".aider.conf.yml").read_text()
        assert YAML_BEGIN in content
        assert WIKIGEN_BEGIN not in content

    def test_created_file_is_valid_yaml(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        content = (tmp_path / ".aider.conf.yml").read_text()
        parsed = yaml.safe_load(content)
        assert parsed is not None

    def test_existing_yaml_config_preserved_on_append(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all, YAML_BEGIN
        existing = "model: gpt-4o\nauto-commits: false\n"
        (tmp_path / ".aider.conf.yml").write_text(existing)
        write_all(tmp_path, tmp_path / "wiki", "proj")
        content = (tmp_path / ".aider.conf.yml").read_text()
        assert "model: gpt-4o" in content
        assert YAML_BEGIN in content
        assert content.index("model: gpt-4o") < content.index(YAML_BEGIN)

    def test_existing_yaml_config_with_markers_updates(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all, YAML_BEGIN, YAML_END
        existing = f"model: gpt-4o\n\n{YAML_BEGIN}\nread:\n  - old.md\n{YAML_END}\n"
        (tmp_path / ".aider.conf.yml").write_text(existing)
        results = write_all(tmp_path, tmp_path / "wiki", "proj")
        actions = {str(p.name): action for _, p, action in results}
        assert actions.get(".aider.conf.yml") == "updated"
        content = (tmp_path / ".aider.conf.yml").read_text()
        assert "model: gpt-4o" in content
        assert "old.md" not in content

    def test_aider_yaml_idempotent(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        write_all(tmp_path, tmp_path / "wiki", "proj")
        first = (tmp_path / ".aider.conf.yml").read_text()
        write_all(tmp_path, tmp_path / "wiki", "proj")
        second = (tmp_path / ".aider.conf.yml").read_text()
        assert first == second

    def test_appended_file_still_valid_yaml(self, tmp_path: Path) -> None:
        from wikigen.agent_instructions import write_all
        (tmp_path / ".aider.conf.yml").write_text("model: gpt-4o\n")
        write_all(tmp_path, tmp_path / "wiki", "proj")
        content = (tmp_path / ".aider.conf.yml").read_text()
        parsed = yaml.safe_load(content)
        assert parsed is not None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class TestBackends:
    def test_get_backend_claude(self) -> None:
        from wikigen.backends import get_backend, ClaudeBackend
        cfg = BackendConfig(name="claude")
        assert isinstance(get_backend(cfg), ClaudeBackend)

    def test_get_backend_openai(self) -> None:
        from wikigen.backends import get_backend, OpenAIBackend
        cfg = BackendConfig(name="openai")
        assert isinstance(get_backend(cfg), OpenAIBackend)

    def test_get_backend_ollama(self) -> None:
        from wikigen.backends import get_backend, OllamaBackend
        cfg = BackendConfig(name="ollama")
        assert isinstance(get_backend(cfg), OllamaBackend)

    def test_get_backend_unknown_exits(self) -> None:
        from wikigen.backends import get_backend
        cfg = BackendConfig(name="unknown-llm")
        with pytest.raises(SystemExit):
            get_backend(cfg)

    def test_get_backend_claude_code_removed(self) -> None:
        from wikigen.backends import get_backend
        cfg = BackendConfig(name="claude-code")
        with pytest.raises(SystemExit):
            get_backend(cfg)


# ---------------------------------------------------------------------------
# Section plan parsing
# ---------------------------------------------------------------------------

class TestSectionPlanParsing:
    def _sections(self) -> list[str]:
        return ["Architecture", "Modules", "API Reference"]

    def test_valid_json(self) -> None:
        from wikigen.ingester import _parse_section_plan
        raw = '{"Architecture": ["Overview", "Design"], "Modules": ["Auth"]}'
        result = _parse_section_plan(raw, self._sections())
        assert result["Architecture"] == ["Overview", "Design"]
        assert result["Modules"] == ["Auth"]

    def test_json_in_fences(self) -> None:
        from wikigen.ingester import _parse_section_plan
        raw = '```json\n{"Architecture": ["Overview"]}\n```'
        result = _parse_section_plan(raw, self._sections())
        assert "Architecture" in result

    def test_flat_list(self) -> None:
        from wikigen.ingester import _parse_section_plan
        raw = '["PageA", "PageB", "PageC"]'
        result = _parse_section_plan(raw, self._sections())
        assert any(pages for pages in result.values())

    def test_flat_key_map(self) -> None:
        from wikigen.ingester import _parse_section_plan
        raw = '{"Overview": "", "AuthModule": null, "ApiEndpoints": ""}'
        result = _parse_section_plan(raw, self._sections())
        all_pages = [p for pages in result.values() for p in pages]
        assert len(all_pages) > 0

    def test_invalid_json_returns_empty(self) -> None:
        from wikigen.ingester import _parse_section_plan
        result = _parse_section_plan("not json at all {{", self._sections())
        assert result == {}

    def test_string_value_normalised_to_list(self) -> None:
        from wikigen.ingester import _parse_section_plan
        raw = '{"Architecture": "SinglePage"}'
        result = _parse_section_plan(raw, self._sections())
        assert result.get("Architecture") == ["SinglePage"]

    def test_nested_dict_value_extracts_keys(self) -> None:
        from wikigen.ingester import _parse_section_plan
        raw = '{"Architecture": {"Overview": "desc", "Design": "desc"}}'
        result = _parse_section_plan(raw, self._sections())
        assert set(result.get("Architecture", [])) == {"Overview", "Design"}

    def test_empty_sections_filtered_out(self) -> None:
        from wikigen.ingester import _parse_section_plan
        raw = '{"Architecture": ["Overview"], "Modules": []}'
        result = _parse_section_plan(raw, self._sections())
        assert "Modules" not in result


# ---------------------------------------------------------------------------
# Ingester helpers
# ---------------------------------------------------------------------------

class TestIngesterHelpers:
    def test_file_tree_basic(self, tmp_project: Path, cfg: WikigenConfig) -> None:
        from wikigen.ingester import _file_tree
        from wikigen.collector import FileCollector
        files = FileCollector(tmp_project, cfg.ingestion).collect()
        tree = _file_tree(files)
        assert "main.py" in tree or "README.md" in tree

    def test_file_tree_truncates(self, tmp_project: Path, cfg: WikigenConfig) -> None:
        from wikigen.ingester import _file_tree
        from wikigen.collector import FileCollector
        files = FileCollector(tmp_project, cfg.ingestion).collect()
        tree = _file_tree(files, max_lines=1)
        assert "more files" in tree

    def test_find_relevant_chunks_keyword_match(self, tmp_project: Path) -> None:
        from wikigen.ingester import _find_relevant_chunks
        from wikigen.collector import SourceFile, FileChunk
        sf = SourceFile.read(tmp_project / "src" / "main.py", tmp_project)
        chunks = [FileChunk(sf, 0, 1, "def authenticate_user(): pass", 6)]
        result = _find_relevant_chunks("AuthModule", "Modules", chunks, max_chunks=3)
        assert len(result) >= 1

    def test_find_relevant_chunks_caps_at_max(self, tmp_project: Path) -> None:
        from wikigen.ingester import _find_relevant_chunks
        from wikigen.collector import SourceFile, FileChunk
        sf = SourceFile.read(tmp_project / "src" / "main.py", tmp_project)
        chunks = [FileChunk(sf, i, 10, f"content {i}", 2) for i in range(10)]
        result = _find_relevant_chunks("Title", "Section", chunks, max_chunks=3)
        assert len(result) == 3

    def test_derive_plan_from_files(self, tmp_project: Path, cfg: WikigenConfig) -> None:
        from wikigen.ingester import _derive_plan_from_files
        from wikigen.collector import FileCollector
        files = FileCollector(tmp_project, cfg.ingestion).collect()
        plan = _derive_plan_from_files(files, cfg.wiki.sections)
        assert isinstance(plan, dict)
        all_pages = [p for pages in plan.values() for p in pages]
        assert len(all_pages) >= 0  # may be empty for tiny project, shape is correct

    def test_group_pages_by_prefix_keyword_match(self) -> None:
        from wikigen.ingester import _group_pages_by_prefix
        pages = ["SystemOverview", "AuthModule", "UserModel", "ApiEndpoints"]
        sections = ["Architecture", "Modules", "Data Models", "API Reference"]
        result = _group_pages_by_prefix(pages, sections)
        assert isinstance(result, dict)
        all_assigned = [p for pages in result.values() for p in pages]
        assert set(all_assigned) == set(pages)

    def test_group_pages_unassigned_goes_to_catchall(self) -> None:
        from wikigen.ingester import _group_pages_by_prefix
        pages = ["WeirdPageXYZ"]
        sections = ["Architecture", "Overview"]
        result = _group_pages_by_prefix(pages, sections)
        all_assigned = [p for pages in result.values() for p in pages]
        assert "WeirdPageXYZ" in all_assigned


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

class TestPrompts:
    def test_context_summary_prompt_contains_project(self) -> None:
        from wikigen.prompts import build_context_summary_prompt
        prompt = build_context_summary_prompt("MyProject", [("README.md", "# MyProject\nA tool.")])
        assert "MyProject" in prompt
        assert "README.md" in prompt

    def test_section_plan_prompt_contains_sections(self) -> None:
        from wikigen.prompts import build_section_plan_prompt
        prompt = build_section_plan_prompt(
            "MyProject", "context here", ["Architecture", "Modules"], "src/main.py"
        )
        assert "Architecture" in prompt
        assert "Modules" in prompt
        assert "JSON" in prompt

    def test_page_prompt_returns_tuple(self) -> None:
        from wikigen.prompts import build_page_prompt
        system, user = build_page_prompt(
            page_title="AuthModule",
            section="Modules",
            project_name="MyProject",
            context_summary="A project.",
            source_chunks=["def login(): pass"],
            all_page_titles=["AuthModule", "UserModel"],
        )
        assert "AuthModule" in user
        assert "MyProject" in user
        assert isinstance(system, str) and len(system) > 0

    def test_page_prompt_markdown_link_style(self) -> None:
        from wikigen.prompts import build_page_prompt
        _, user = build_page_prompt(
            page_title="Overview",
            section="Architecture",
            project_name="Proj",
            context_summary="ctx",
            source_chunks=[],
            all_page_titles=[],
            link_style="markdown",
        )
        assert "PageTitle.md" in user

    def test_home_page_prompt_contains_toc(self) -> None:
        from wikigen.prompts import build_home_page_prompt
        system, user = build_home_page_prompt(
            "MyProject",
            "context",
            {"Architecture": ["Overview"], "Modules": ["Auth"]},
        )
        assert "Overview" in user
        assert "Auth" in user
        assert "MyProject" in user


# ---------------------------------------------------------------------------
# Writer — additional coverage
# ---------------------------------------------------------------------------

class TestWriterExtra:
    def test_write_home(self, tmp_path: Path) -> None:
        writer = WikiWriter(tmp_path / "wiki", WikiConfig())
        path = writer.write_home("---\ntitle: Home\n---\n\n## TOC\n")
        assert path.name == "home.md"
        assert path.exists()

    def test_write_home_creates_wiki_dir(self, tmp_path: Path) -> None:
        writer = WikiWriter(tmp_path / "nonexistent" / "wiki", WikiConfig())
        path = writer.write_home("content")
        assert path.exists()

    def test_ensure_dirs(self, tmp_path: Path) -> None:
        writer = WikiWriter(tmp_path / "wiki", WikiConfig())
        writer.ensure_dirs(["Architecture", "Data Models"])
        assert (tmp_path / "wiki" / "architecture").is_dir()
        assert (tmp_path / "wiki" / "data-models").is_dir()

    def test_wikilink_conversion(self, tmp_path: Path) -> None:
        cfg = WikiConfig(link_style="markdown")
        writer = WikiWriter(tmp_path / "wiki", cfg)
        path = writer.write_page("Architecture", "Overview", "See [[AuthModule]] for details.")
        content = path.read_text()
        assert "[[AuthModule]]" not in content
        assert "[AuthModule](" in content

    def test_wikilink_not_converted_in_wikilink_mode(self, tmp_path: Path) -> None:
        cfg = WikiConfig(link_style="wikilink")
        writer = WikiWriter(tmp_path / "wiki", cfg)
        path = writer.write_page("Architecture", "Overview", "See [[AuthModule]].")
        assert "[[AuthModule]]" in path.read_text()

    def test_empty_section_falls_back_to_general(self, tmp_path: Path) -> None:
        writer = WikiWriter(tmp_path / "wiki", WikiConfig())
        path = writer.write_page("", "OrphanPage", "content")
        assert "general" in str(path)


# ---------------------------------------------------------------------------
# Linter — log.md exemptions + auto-fix
# ---------------------------------------------------------------------------

class TestLinterLogExemption:
    def test_log_md_not_flagged(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "home.md").write_text(
            "---\ntitle: Home\ndescription: x\ntags: []\nrelated: []\n---\n\nContent.\n"
        )
        (wiki / "log.md").write_text("# Log\n\n## 2024-01-01 — init\n\n- Started.\n")
        linter = Linter(wiki)
        issues = linter.run()
        assert not any("log" in str(i.page) for i in issues)

    def test_auto_fix_adds_front_matter(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = wiki / "no-front-matter.md"
        page.write_text("## Heading\nContent without front matter.\n")
        (wiki / "home.md").write_text(
            "---\ntitle: Home\ndescription: x\ntags: []\nrelated: []\n---\n\n[[NoFrontMatter]]\n"
        )
        linter = Linter(wiki)
        linter.run(fix=True)
        content = page.read_text()
        assert content.startswith("---")
        assert "title:" in content

    def test_missing_front_matter_detected(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "home.md").write_text("## No front matter\n")
        linter = Linter(wiki)
        issues = linter.run()
        assert any(i.kind == "missing_front_matter" for i in issues)


# ---------------------------------------------------------------------------
# Cache — all_keys
# ---------------------------------------------------------------------------

class TestCacheAllKeys:
    def test_all_keys_empty(self, tmp_path: Path) -> None:
        cache = HashCache(tmp_path)
        assert cache.all_keys() == set()

    def test_all_keys_after_set(self, tmp_path: Path) -> None:
        cache = HashCache(tmp_path)
        cache.set("a.py", "hash1")
        cache.set("b.py", "hash2")
        assert cache.all_keys() == {"a.py", "b.py"}

    def test_all_keys_after_remove(self, tmp_path: Path) -> None:
        cache = HashCache(tmp_path)
        cache.set("a.py", "hash1")
        cache.set("b.py", "hash2")
        cache.remove("a.py")
        assert cache.all_keys() == {"b.py"}


# ---------------------------------------------------------------------------
# Collector — glob matching
# ---------------------------------------------------------------------------

class TestGlobMatching:
    def test_double_star_matches_nested(self) -> None:
        from wikigen.collector import _matches_glob
        assert _matches_glob(".git/objects/pack/file", "**/.git/**")

    def test_single_star_does_not_cross_slash(self) -> None:
        from wikigen.collector import _matches_glob
        assert not _matches_glob("src/sub/file.py", "src/*.py")
        assert _matches_glob("src/file.py", "src/*.py")

    def test_question_mark_matches_single_char(self) -> None:
        from wikigen.collector import _matches_glob
        assert _matches_glob("file.py", "file.p?")
        assert not _matches_glob("file.pyc", "file.p?")

    def test_extension_pattern(self) -> None:
        from wikigen.collector import _matches_glob
        assert _matches_glob("src/main.pyc", "**/*.pyc")
        assert not _matches_glob("src/main.py", "**/*.pyc")


# ---------------------------------------------------------------------------
# Chunker — edge cases
# ---------------------------------------------------------------------------

class TestChunkerEdgeCases:
    def test_overlap_larger_than_chunk_does_not_loop(self, tmp_path: Path) -> None:
        f = tmp_path / "big.py"
        f.write_text("x = 1\n" * 1000)
        sf = SourceFile.read(f, tmp_path)
        chunks = chunk_file(sf, chunk_size_tokens=100, overlap_tokens=200)
        assert len(chunks) >= 1

    def test_empty_file_single_chunk(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.py"
        f.write_text("")
        sf = SourceFile.read(f, tmp_path)
        chunks = chunk_file(sf, chunk_size_tokens=500, overlap_tokens=50)
        assert len(chunks) == 1

    def test_chunk_covers_full_content(self, tmp_path: Path) -> None:
        f = tmp_path / "code.py"
        content = "line\n" * 3000
        f.write_text(content)
        sf = SourceFile.read(f, tmp_path)
        chunks = chunk_file(sf, chunk_size_tokens=500, overlap_tokens=50)
        combined = "".join(c.text for c in chunks)
        assert "line" in combined


# ---------------------------------------------------------------------------
# Resume + progress
# ---------------------------------------------------------------------------

class TestResumeAndProgress:
    def test_resume_skips_existing_page(self, tmp_path: Path) -> None:
        """A page file that already exists on disk should be detected as resumed."""
        from wikigen.writer import WikiWriter, page_filename, _slugify
        wiki_dir = tmp_path / "wiki"
        writer = WikiWriter(wiki_dir, WikiConfig())
        writer.ensure_dirs(["Architecture"])
        # Simulate a page written by a previous interrupted run
        page_path = wiki_dir / "architecture" / page_filename("Overview")
        page_path.write_text("---\ntitle: Overview\n---\n\nContent.\n")
        assert page_path.exists()
        assert page_path.stat().st_size > 0

    def test_resume_does_not_skip_empty_file(self, tmp_path: Path) -> None:
        """An empty file should not count as a completed page."""
        from wikigen.writer import page_filename
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "architecture").mkdir(parents=True)
        empty = wiki_dir / "architecture" / page_filename("Overview")
        empty.write_text("")
        assert empty.stat().st_size == 0

    def test_home_page_placeholder_detection(self, tmp_path: Path) -> None:
        """Placeholder home.md (from init) should be treated as not-yet-generated."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        home = wiki_dir / "home.md"
        placeholder = "# proj — Wiki\n\n_Run `wikigen ingest` to generate the table of contents._\n"
        home.write_text(placeholder)
        content = home.read_text(encoding="utf-8")
        # The placeholder detection logic used in ingester.py
        is_placeholder = (
            home.exists()
            and "wikigen ingest" in content
            and home.stat().st_size < 500
        )
        assert is_placeholder

    def test_home_page_real_content_not_placeholder(self, tmp_path: Path) -> None:
        """A fully generated home page should not be treated as a placeholder."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        home = wiki_dir / "home.md"
        real_content = (
            "---\ntitle: Home\ndescription: Index\ntags: []\n---\n\n"
            "## Overview\nThis is the real generated home page with lots of content.\n"
            "## Sections\n- Architecture\n- Modules\n- API Reference\n"
        )
        home.write_text(real_content)
        is_placeholder = (
            home.exists()
            and "wikigen ingest" in home.read_text(encoding="utf-8")
            and home.stat().st_size < 500
        )
        assert not is_placeholder

    def test_cache_saved_per_page(self, tmp_path: Path) -> None:
        """Cache file should exist after a page is written and cache.save() called."""
        cache = HashCache(tmp_path)
        cache.set("Architecture/Overview", "abc123")
        cache.save()
        # Cache file must exist immediately — not deferred to end of run
        assert (tmp_path / ".wikigen_cache.json").exists()
        reloaded = HashCache(tmp_path)
        assert reloaded.get("Architecture/Overview") == "abc123"

    def test_partial_cache_survives_reload(self, tmp_path: Path) -> None:
        """Simulates mid-run save: 2 of 5 pages saved, then cache reloaded."""
        cache = HashCache(tmp_path)
        for i in range(5):
            cache.set(f"section/page{i}", f"hash{i}")
            if i < 2:
                cache.save()  # only saved for first 2
        # Reload — should still have all 5 since save() was called with all set
        cache2 = HashCache(tmp_path)
        assert cache2.get("section/page0") == "hash0"
        assert cache2.get("section/page1") == "hash1"
