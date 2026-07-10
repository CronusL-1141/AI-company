"""Ecosystem tag rules — Layer 1 (topics) + Layer 2 (keywords / language / owner).

Used by ecosystem_tagger.py Layer 2 fallback when GitHub topics fail to match.

Each rule maps a tag name (must exist in EcosystemTag dictionary) to a list of
case-insensitive substrings. If ANY substring matches the combined text
(name + description + topics), the tag is applied with confidence=0.7 and
source=AUTO_RULE.

Tag names here MUST match those in api/deps.py::_DEFAULT_ECOSYSTEM_TAGS.

K4 edge-case fixes (2026-05-07):
- Removed bare "mcp" alias from mcp_framework — too generic, caused 99/265 false positives
- Added docs_only / claude_code / agent_harness / javascript / java tags
- Added language-field direct mapping for tech_stack tags
- Added docs-only detection (awesome-list / cookbook / tutorial / curriculum)
"""

from __future__ import annotations

import re
from typing import Final

# Layer 1: GitHub topic -> tag-name aliases.
# When EcosystemRepoProfile.topics contains any of these strings (case-insensitive),
# the corresponding tag is applied with confidence=0.95, source=GITHUB_TOPIC.
GITHUB_TOPIC_ALIASES: Final[dict[str, list[str]]] = {
    "memory_system": ["memory", "long-term-memory", "vector-memory", "rag", "retrieval-augmented-generation"],
    "skill_system": ["skill", "skills", "claude-skill", "ai-skill"],
    "agent_orchestration": ["agent-orchestration", "orchestration", "multi-agent-orchestration"],
    "mcp_server": ["mcp-server", "mcp-servers", "model-context-protocol-server"],
    # NOTE: bare "mcp" REMOVED — too generic, caused massive false positives (n8n, dify, open-webui).
    # mcp_framework now requires explicit framework/sdk indicators in topics.
    "mcp_framework": ["mcp-framework", "mcp-sdk", "model-context-protocol-framework", "model-context-protocol-sdk"],
    "tool_use": ["tool-use", "function-calling", "tool-calling"],
    "workflow_engine": ["workflow", "workflow-engine", "pipeline", "dag", "workflow-automation"],
    "multi_agent": ["multi-agent", "multiagent", "agent-collaboration", "swarm"],
    "single_agent": ["single-agent"],
    "python": ["python", "python3", "py"],
    "typescript": ["typescript", "ts"],
    "javascript": ["javascript", "js", "nodejs", "node-js"],
    "java": ["java"],
    "rust": ["rust", "rustlang"],
    "go": ["go", "golang"],
    "official_anthropic": ["anthropic-official"],
    "battle_tested": ["production-ready", "stable"],
    "experimental": ["experimental", "wip", "alpha", "beta"],
    "framework": ["framework"],
    "application": ["application", "app", "tool"],
    "library": ["library", "lib", "sdk"],
    "plugin": ["plugin", "plugins", "extension"],
    "template": ["template", "starter", "boilerplate", "scaffold"],
    # K4 NEW
    "claude_code": ["claude-code", "claude-code-skill", "anthropic-claude-code"],
    "agent_harness": ["agent-harness", "agent-scaffold", "agent-runtime"],
    "docs_only": [
        "awesome-list",
        "awesome",
        "tutorial",
        "tutorials",
        "curriculum",
        "cookbook",
        "examples",
        "documentation",
        "cheatsheet",
        "interview",
        "study-guide",
    ],
}


# Layer 2: regex patterns for description / name fallback when topics absent.
# Each entry: tag_name -> list of substrings (case-insensitive `in` match).
# Order matters only for resolving ties — the first matching rule per tag wins.
KEYWORD_RULES: Final[dict[str, list[str]]] = {
    # capability
    "memory_system": [
        "memory system", "long-term memory", "long term memory",
        "vector memory", "memory layer", "agent memory", "persistent memory",
        "rag ", "retrieval augmented", "retrieval-augmented",
    ],
    "skill_system": [
        "skill system", "skills system", "claude skill", "claude code skill",
        "plugin system", "skill plugin", "code skill", "claude-code skill",
        "claude code plugin", "ai skill", "claude-skill", "is a skill",
        "skills for claude", "agent skill",
    ],
    "agent_orchestration": [
        "orchestrat", "agent coordinator", "agent scheduling",
        "multi-agent system", "agent workflow", "agentic workflow",
    ],
    "mcp_server": [
        "mcp server", "model context protocol server",
        "mcp-server", "mcp tool",
    ],
    # K4: mcp_framework now needs strong "framework / sdk for building MCP" signals.
    # Plain "mcp" / "model context protocol" no longer triggers — those are too common.
    "mcp_framework": [
        "mcp framework", "mcp sdk", "framework for building mcp",
        "framework for mcp", "sdk for mcp", "build mcp servers",
        "build mcp clients", "mcp protocol implementation",
    ],
    "tool_use": [
        "tool use", "tool-use", "function calling", "function-calling",
        "tool calling", "tool invocation",
    ],
    "workflow_engine": [
        "workflow engine", "workflow orchestrator",
        "pipeline engine", "dag scheduler", "task pipeline",
        "workflow automation", "workflow automation platform",
    ],
    "multi_agent": [
        "multi-agent", "multi agent", "multiple agents",
        "agent team", "swarm", "agent collaboration",
    ],
    "single_agent": [
        "single agent", "single-agent", "autonomous agent",
    ],
    # tech_stack — keyword fallback for description; primary signal is now `language` field.
    "python": [
        "python implementation", "written in python", "pip install",
        "fastapi", "asyncio", "pydantic", "pyproject.toml",
    ],
    "typescript": [
        "written in typescript", "npm install", "tsconfig",
        "node.js", "nodejs",
    ],
    "javascript": [
        "javascript library", "javascript framework", "package.json",
    ],
    "java": [
        "java framework", "spring boot", "spring-boot", "maven",
        "is a java", "java implementation",
    ],
    "rust": [
        "rust implementation", "written in rust", "cargo install",
    ],
    "go": [
        "golang", "written in go", "go module",
    ],
    # maturity — K4: removed weak "by anthropic" / "anthropics/" patterns that caused
    # false positives on awesome-lists. Owner_rules now is the authoritative anthropic signal.
    "official_anthropic": [
        "anthropic official", "anthropic's official",
    ],
    "battle_tested": [
        "production ready", "production-ready", "battle tested",
        "battle-tested", "widely used", "stable release",
    ],
    "experimental": [
        "experimental", "work in progress", "wip ",
        "early stage", "preview release",
    ],
    # positioning
    "framework": [
        "framework for", "is a framework", "agent framework",
        "framework that",
    ],
    "application": [
        "application for", "end-user app", "desktop app",
        "cli application", "is an application", "all-in-one assistant",
        "ai interface", "ai platform",
    ],
    "library": [
        "library for", "is a library", "python library",
        "javascript library", "library that",
    ],
    "plugin": [
        "plugin for", "extension for", "is a plugin",
        "claude plugin", "code plugin",
    ],
    "template": [
        "template for", "starter template", "project template",
        "boilerplate", "scaffold for",
    ],
    # K4 NEW
    "claude_code": [
        "claude code", "claude-code", "claude code skill",
        "claude code plugin", "for claude code", "cc-",
    ],
    "agent_harness": [
        "agent harness", "agent runtime", "agent scaffold",
        "the agent that", "harness for", "agentic harness",
    ],
    # docs_only — strong signals: awesome-list / cookbook / tutorial / curriculum / interview-prep
    "docs_only": [
        "awesome list", "awesome-list", "curated list", "curated collection",
        "collection of mcp", "collection of notebooks", "collection of recipes",
        "cookbook", "interactive tutorial", "tutorial repository",
        "curriculum", "for beginners", "study guide", "interview guide",
        "interview & ", "cheatsheet", "knowledge base",
        "list of awesome", "awesome resources",
    ],
}


# Owner-based shortcut: if owner login matches, force-apply specific tags.
# Used to ensure Anthropic's official repos always get tagged correctly.
OWNER_RULES: Final[dict[str, list[str]]] = {
    "anthropics": ["official_anthropic"],
}


# Language field -> tech_stack tag. Confidence applied at the tagger level.
# Triggers when EcosystemRepoProfile.language matches (case-insensitive).
LANGUAGE_TAG_MAP: Final[dict[str, str]] = {
    "python": "python",
    "typescript": "typescript",
    "javascript": "javascript",
    "java": "java",
    "rust": "rust",
    "go": "go",
}


# Languages that signal a docs/curriculum/notebook repo rather than a code package.
# When the primary language is one of these, the repo is likely docs_only.
DOCS_ONLY_LANGUAGES: Final[set[str]] = {
    "jupyter notebook",
    "tex",
    "html",
    "markdown",
    "mdx",
}


# Repo name patterns that strongly indicate docs-only nature.
# Checked as substring (lower-case) against repo_full_name and name.
DOCS_ONLY_NAME_PATTERNS: Final[tuple[str, ...]] = (
    "awesome-",
    "-awesome",
    "cookbook",
    "tutorial",
    "tutorials",
    "guide",
    "for-beginners",
    "learn-",
    "interview",
    "study-",
    "cheatsheet",
    "examples",
    "notebooks",
    "curriculum",
    "howto",
    "how-to",
    "knowledge",
)


def _build_text(name: str, description: str | None, topics: list[str], owner: str) -> str:
    """Lowercase concatenation used by Layer 2 keyword matching."""
    parts = [name or "", description or "", " ".join(topics or []), owner or ""]
    return " ".join(parts).lower()


_WORD_RE = re.compile(r"[a-z0-9_-]+")


def _normalize_topic(topic: str) -> str:
    """Normalize a GitHub topic for alias matching: lower-case, strip whitespace."""
    return topic.strip().lower()


def match_github_topics(topics: list[str]) -> dict[str, str]:
    """Layer 1: match GitHub topics against GITHUB_TOPIC_ALIASES.

    Returns dict mapping tag_name -> matched_topic_string (for traceability).
    """
    if not topics:
        return {}
    normalized = {_normalize_topic(t) for t in topics if t}
    matched: dict[str, str] = {}
    for tag_name, aliases in GITHUB_TOPIC_ALIASES.items():
        for alias in aliases:
            if alias.lower() in normalized:
                matched[tag_name] = alias
                break
    return matched


def match_keyword_rules(
    name: str,
    description: str | None,
    topics: list[str],
    owner: str = "",
) -> dict[str, str]:
    """Layer 2: scan combined text against KEYWORD_RULES + OWNER_RULES + name patterns.

    Returns dict mapping tag_name -> matched_keyword (for traceability).
    """
    matched: dict[str, str] = {}

    if owner:
        owner_lower = owner.strip().lower()
        for owner_key, tags in OWNER_RULES.items():
            if owner_key.lower() == owner_lower:
                for tag_name in tags:
                    matched.setdefault(tag_name, f"owner:{owner_key}")

    # docs_only — name pattern shortcut (handles awesome-*/cookbook/tutorial/* names)
    name_lower = (name or "").strip().lower()
    for pattern in DOCS_ONLY_NAME_PATTERNS:
        if pattern in name_lower:
            matched.setdefault("docs_only", f"name:{pattern}")
            break

    text = _build_text(name, description, topics, owner)
    if not text.strip():
        return matched

    for tag_name, keywords in KEYWORD_RULES.items():
        if tag_name in matched:
            continue
        for kw in keywords:
            if kw.lower() in text:
                matched[tag_name] = kw
                break

    return matched


def match_language_tag(language: str | None) -> dict[str, str]:
    """Map EcosystemRepoProfile.language to a tech_stack tag.

    Returns dict mapping tag_name -> matched_language (for traceability).
    Empty dict if language is missing or not in LANGUAGE_TAG_MAP.
    """
    if not language:
        return {}
    lang_lower = language.strip().lower()
    tag = LANGUAGE_TAG_MAP.get(lang_lower)
    if tag:
        return {tag: f"language:{language}"}
    return {}


def match_docs_only_by_language(language: str | None) -> bool:
    """Return True if the primary language signals a docs-only repo
    (Jupyter Notebook, TeX, HTML, Markdown, MDX).
    """
    if not language:
        return False
    return language.strip().lower() in DOCS_ONLY_LANGUAGES
