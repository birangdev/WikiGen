# wikigen

> LLM-powered wiki generator for any codebase — structured, interlinked Markdown notes that survive context window limits.

[![PyPI](https://img.shields.io/pypi/v/wikigen)](https://pypi.org/project/wikigen/)
[![Python](https://img.shields.io/pypi/pyversions/wikigen)](https://pypi.org/project/wikigen/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Inspired by [Karpathy's LLM Wiki](https://x.com/karpathy) concept — wikigen is a **general-purpose CLI tool** that points at any project directory and generates a rich, interlinked Markdown wiki from your codebase.

---

## Architecture

```
wikigen/
│
├── cli.py              ← Click entry point — routes all 4 commands
│   │
│   ├── config.py       ← WikigenConfig dataclasses, YAML load/save
│   │
│   ├── ingester.py     ← Full ingest pipeline orchestrator
│   │   ├── collector.py    walk · chunk · prioritise source files
│   │   ├── cache.py        SHA-256 hash store (.wikigen_cache.json)
│   │   ├── writer.py       Markdown output + [[wikilink]] conversion
│   │   ├── backends/       LLM abstraction layer
│   │   │   ├── Claude      Anthropic SDK
│   │   │   ├── OpenAI      openai SDK (or any compatible endpoint)
│   │   │   └── Ollama      local via httpx REST
│   │   └── prompts/        all system + user prompt builders
│   │
│   ├── updater.py      ← Incremental re-processing (changed files only)
│   │   └── (reuses collector, cache, backends, prompts)
│   │
│   └── linter.py       ← Broken [[WikiLinks]], orphan detection, CI exit code
│
tests/
└── test_wikigen.py     22 tests, zero LLM calls required
```

**Data flow during `wikigen ingest`:**

```
project/          collector.py         ingester.py           backends/
source files  →   walk + chunk    →    context summary   →   LLM call
                  SHA-256 hash         section plan           (parallel)
                                       page generation
                                            ↓
                                       writer.py  →  wiki/*.md
                                       cache.py   →  .wikigen_cache.json
```

---

## Use with AI coding agents (Claude Code, Cursor, Copilot, etc.)

wikigen is designed to be invoked directly by coding agents that have shell access. No interactive prompts, no confirmations — every command is fully scriptable.

**Claude Code** already has `ANTHROPIC_API_KEY` in its environment (the same key it uses for its own reasoning). This means wikigen's Claude backend picks it up automatically — no separate key setup needed inside a Claude Code session.

A Claude Code agent can self-document any project it's working in with a single tool call:

```bash
# Agent drops this into the project shell — zero config required
cd /path/to/project
pip install "wikigen-cli[claude]" -q
wikigen init
wikigen ingest
```

After that, the agent (or you) can run `wikigen update` after every significant change, keeping the wiki in sync as the codebase evolves. The wiki then becomes persistent structured context the agent can read back in future sessions — surviving the context window limit that would otherwise force it to re-read the whole codebase each time.

**Other agents** (Cursor, Aider, Copilot Workspace, etc.) work the same way as long as they have an `OPENAI_API_KEY` or can reach a local Ollama instance:

```bash
# OpenAI-backed agent
wikigen --backend openai ingest

# Fully local, no keys
wikigen --backend ollama ingest
```

All commands exit with code 0 on success and non-zero on error, making them composable in agent tool-call loops and CI pipelines.

---

## Why wikigen?

Large codebases exceed the context window of any LLM. Wikigen solves this by:

1. **Chunking** your entire codebase into LLM-sized windows.
2. Using an LLM to **synthesise structured wiki pages** — not just summaries, but architecture notes, module docs, data-model refs, and more.
3. Writing **interlinked Markdown** so you can navigate your knowledge graph.
4. Tracking **file hashes** so only changed files are re-processed on `wikigen update`.

The resulting wiki lives next to your code, is committed to git, and stays fresh automatically.

---

## Installation

```bash
# Core (no backend pre-installed)
pip install wikigen-cli

# With Claude (Anthropic) support
pip install "wikigen-cli[claude]"

# With OpenAI support
pip install "wikigen-cli[openai]"

# Everything
pip install "wikigen-cli[all]"
```

Requires Python ≥ 3.11.

### Use without PyPI (local / development)

You don't need to publish to PyPI to use wikigen as a real CLI tool. An editable install reads directly from your local source:

```bash
git clone https://github.com/your-org/wikigen
cd wikigen
pip install -e ".[claude]"   # registers the `wikigen` command system-wide
wikigen --version             # works immediately
```

Any edits to the source are reflected instantly — no reinstall needed.

---

## Quick start

```bash
cd my-project

# 1. Scaffold config
wikigen init

# 2. Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Generate wiki
wikigen ingest

# 4. Browse your wiki
ls wiki/
```

---

## Commands

### `wikigen init`

Creates a `wikigen.yaml` in the project root. Edit it to configure:

- Which LLM backend to use (`claude`, `openai`, or `ollama`)
- Which files to include/exclude
- What wiki sections to generate

```bash
wikigen init
```

### `wikigen ingest`

Reads the entire codebase and generates the wiki from scratch.

```bash
wikigen ingest                  # normal run
wikigen ingest --force          # regenerate even cached pages
wikigen ingest --dry-run        # preview what would be generated
wikigen ingest --concurrency 8  # parallel LLM requests
```

Pipeline:
1. Walk project tree → collect source files
2. Read priority files (CLAUDE.md, README, schema) → build project context summary
3. Ask LLM to plan wiki structure (sections → page titles)
4. Generate each page in parallel, injecting relevant source chunks as context
5. Write interlinked Markdown to `wiki/`
6. Store SHA-256 hashes in `wiki/.wikigen_cache.json`

### `wikigen update`

Re-processes only files that changed since the last run.

```bash
wikigen update
wikigen update --dry-run
```

Detects:
- **Changed files** (hash mismatch) → re-generates affected wiki pages
- **Deleted files** → removes cache entries

### `wikigen lint`

Validates all wiki pages for consistency.

```bash
wikigen lint          # report issues, exit 1 if any found
wikigen lint --fix    # auto-fix trivial issues (e.g. add missing front matter stubs)
```

Checks:
- `[[WikiLinks]]` that don't resolve to an existing page
- `[text](path.md)` links pointing to missing files
- Pages that are never linked from anywhere (**orphans**)
- Missing YAML front matter

Useful in CI:
```yaml
# .github/workflows/wiki.yml
- run: wikigen lint
```

---

## Configuration reference (`wikigen.yaml`)

```yaml
project_name: "my-project"

backend:
  name: "claude"           # claude | openai | ollama
  model: "claude-sonnet-4-20250514"
  api_key_env: "ANTHROPIC_API_KEY"
  # base_url: "http://localhost:11434"  # for Ollama
  max_tokens: 4096
  temperature: 0.2

ingestion:
  include_patterns: ["**/*"]
  exclude_patterns:
    - "**/.git/**"
    - "**/node_modules/**"
    - "**/__pycache__/**"
  max_file_size_kb: 256
  chunk_size_tokens: 6000
  chunk_overlap_tokens: 200

wiki:
  sections:
    - Overview
    - Architecture
    - Modules
    - Data Models
    - API Reference
    - Configuration
    - Development Guide
  index_page: "Home"
  link_style: "wikilink"   # wikilink ([[Page]]) or markdown ([Page](Page.md))
  front_matter: true
```

---

## Backends

### Claude (Anthropic)

```bash
pip install "wikigen-cli[claude]"
export ANTHROPIC_API_KEY=sk-ant-...
```

```yaml
backend:
  name: claude
  model: claude-sonnet-4-20250514
  api_key_env: ANTHROPIC_API_KEY
```

### OpenAI

```bash
pip install "wikigen-cli[openai]"
export OPENAI_API_KEY=sk-...
```

```yaml
backend:
  name: openai
  model: gpt-4o
  api_key_env: OPENAI_API_KEY
```

Also works with any OpenAI-compatible API (Together, Groq, Azure, etc.) by setting `base_url`.

### Ollama (local)

```bash
ollama pull llama3
```

```yaml
backend:
  name: ollama
  model: llama3
  base_url: "http://localhost:11434"
```

No API key required. All processing stays on your machine.

---

## Wiki structure

After `wikigen ingest`, your wiki looks like:

```
wiki/
├── home.md                    # index page with full ToC
├── .wikigen_cache.json        # hash cache (commit this)
├── architecture/
│   ├── system-overview.md
│   ├── request-lifecycle.md
│   └── data-flow.md
├── modules/
│   ├── auth-module.md
│   └── payment-module.md
├── data-models/
│   ├── user-model.md
│   └── order-model.md
└── ...
```

Each page has YAML front matter:

```yaml
---
title: RequestLifecycle
description: How HTTP requests flow through the system.
tags: [architecture, http, middleware]
related: [SystemOverview, AuthModule]
---
```

And uses `[[WikiLinks]]` for cross-references (compatible with Obsidian, Foam, Logseq, etc.).

---

## Global options

```
wikigen --project-dir /path/to/project  ingest
wikigen --wiki-dir /custom/wiki/path    ingest
wikigen --backend openai                ingest   # override config backend
```

---

## Development

```bash
git clone https://github.com/your-org/wikigen
cd wikigen
pip install -e ".[dev]"

# Run tests (no API key needed — all LLM calls are unit-tested without network)
pytest

# Lint
ruff check wikigen/
mypy wikigen/
```

---

## Roadmap

- [ ] `wikigen serve` — local web UI for browsing the wiki
- [ ] GitHub Actions integration template
- [ ] Embeddings-based chunk retrieval for better relevance
- [ ] Support for multi-modal (diagrams via GPT-4V / Claude Vision)
- [ ] `wikigen diff` — show what changed between two wiki generations
- [ ] MkDocs / Docusaurus export

---

## License

MIT © wikigen contributors
