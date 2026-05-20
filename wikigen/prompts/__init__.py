"""wikigen prompts — all LLM system and user prompts."""

from __future__ import annotations

from ..config import WikigenConfig


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are wikigen, an expert technical writer and software architect.
Your job is to generate high-quality, structured Markdown wiki pages for software projects.

Rules:
- Write in clear, concise technical prose aimed at developers.
- Every page must have a YAML front matter block with: title, description, tags, and related pages.
- Use [[WikiLink]] syntax when referencing other wiki pages by name.
- Organise content with ## headings (never use # — that is the page title in front matter).
- Include code blocks with language tags wherever helpful.
- Do not hallucinate — if information is absent from the provided source, say so explicitly.
- Be thorough but focused: prefer depth on specific topics over superficial breadth.
- When you see CLAUDE.md or ARCHITECTURE.md content, treat it as authoritative ground truth.
"""


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_context_summary_prompt(project_name: str, priority_files: list[tuple[str, str]]) -> str:
    """Build the prompt that distils priority files into a project context summary."""
    parts = [
        f"Project: {project_name}\n\n",
        "The following high-priority files describe this project:\n\n",
    ]
    for rel_path, content in priority_files:
        parts.append(f"### {rel_path}\n```\n{content[:8000]}\n```\n\n")

    parts.append(
        "Produce a concise PROJECT CONTEXT SUMMARY (≤500 words) covering:\n"
        "1. What the project does and who it is for\n"
        "2. Primary programming language(s) and key dependencies\n"
        "3. High-level architecture / main components\n"
        "4. Any explicit conventions or constraints from CLAUDE.md\n\n"
        "Output ONLY the summary text, no headings or preamble."
    )
    return "".join(parts)


def build_section_plan_prompt(
    project_name: str,
    context_summary: str,
    sections: list[str],
    file_tree: str,
) -> str:
    """Ask the LLM to plan which wiki pages to create."""
    return (
        f"Project: {project_name}\n\n"
        f"## Project Context\n{context_summary}\n\n"
        f"## File Tree\n```\n{file_tree}\n```\n\n"
        f"## Requested Wiki Sections\n"
        + "\n".join(f"- {s}" for s in sections)
        + "\n\n"
        "Based on the project context and file tree, produce a JSON object mapping each section "
        "to a list of wiki page titles to generate under that section.\n"
        "Example:\n"
        '{\n  "Architecture": ["System Overview", "Request Lifecycle", "Data Flow"],\n'
        '  "Modules": ["Auth Module", "Payment Module"]\n}\n\n'
        "Rules:\n"
        "- Page titles must be PascalCase, no spaces (e.g. RequestLifecycle).\n"
        "- Maximum 5 pages per section.\n"
        "- Only include pages that can actually be inferred from the codebase.\n"
        "- Output ONLY valid JSON, no markdown fences."
    )


def build_page_prompt(
    page_title: str,
    section: str,
    project_name: str,
    context_summary: str,
    source_chunks: list[str],
    all_page_titles: list[str],
    link_style: str = "wikilink",
) -> tuple[str, str]:
    """Return (system, user) prompts for generating a single wiki page."""
    link_note = (
        "Use [[PageTitle]] to link to other wiki pages."
        if link_style == "wikilink"
        else "Use [PageTitle](PageTitle.md) to link to other wiki pages."
    )

    known_pages = ", ".join(all_page_titles[:60])  # cap to avoid blowing context

    source_text = "\n\n---\n\n".join(
        f"**Source: {chunk[:80]}**\n{chunk}" for chunk in source_chunks
    ) if source_chunks else "(No specific source files provided for this page.)"

    system = SYSTEM_PROMPT

    user = (
        f"Generate the wiki page **{page_title}** for project **{project_name}**.\n\n"
        f"## Project Context\n{context_summary}\n\n"
        f"## Section\n{section}\n\n"
        f"## Known Wiki Pages (for cross-linking)\n{known_pages}\n\n"
        f"## Linking Convention\n{link_note}\n\n"
        f"## Relevant Source Material\n{source_text}\n\n"
        "Output a complete Markdown file starting with YAML front matter:\n"
        "```yaml\n"
        "---\n"
        f"title: {page_title}\n"
        "description: <one sentence>\n"
        "tags: [<comma separated>]\n"
        "related: [<other page titles>]\n"
        "---\n"
        "```\n"
        "Then the page content with ## headings. Do not include a top-level # heading."
    )

    return system, user


def build_home_page_prompt(
    project_name: str,
    context_summary: str,
    all_pages: dict[str, list[str]],  # section -> [page titles]
    link_style: str = "wikilink",
) -> tuple[str, str]:
    """Prompt for generating the Home/index wiki page."""
    link_note = (
        "Use [[PageTitle]] syntax."
        if link_style == "wikilink"
        else "Use [PageTitle](PageTitle.md) syntax."
    )

    toc = "\n".join(
        f"**{section}**\n" + "\n".join(f"  - {p}" for p in pages)
        for section, pages in all_pages.items()
    )

    system = SYSTEM_PROMPT
    user = (
        f"Generate the Home page for the **{project_name}** wiki.\n\n"
        f"## Project Context\n{context_summary}\n\n"
        f"## Pages in this wiki\n{toc}\n\n"
        f"## Linking Convention\n{link_note}\n\n"
        "The Home page should:\n"
        "1. Open with a one-paragraph project description.\n"
        "2. Include a Table of Contents linking to every section and page.\n"
        "3. Include a Quick Start section with the 3-5 most important pages.\n"
        "4. Have a Getting Started section if applicable.\n\n"
        "Output full Markdown with YAML front matter. No top-level # heading."
    )
    return system, user
