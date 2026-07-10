"""Tests for ecosystem_tag_rules — Layer 1 (topics) + Layer 2 (keywords)."""

from __future__ import annotations

from aiteam.services.ecosystem_tag_rules import (
    GITHUB_TOPIC_ALIASES,
    KEYWORD_RULES,
    OWNER_RULES,
    match_docs_only_by_language,
    match_github_topics,
    match_keyword_rules,
    match_language_tag,
)

# ============================================================
# Layer 1: GitHub topics
# ============================================================


class TestMatchGithubTopics:
    def test_empty_topics_returns_empty_dict(self) -> None:
        assert match_github_topics([]) == {}

    def test_direct_alias_match(self) -> None:
        result = match_github_topics(["mcp-server", "python"])
        assert "mcp_server" in result
        assert "python" in result

    def test_case_insensitive_alias(self) -> None:
        """Alias match should be case-insensitive."""
        result = match_github_topics(["MCP-Server", "Python"])
        assert "mcp_server" in result
        assert "python" in result

    def test_mcp_topic_alone_does_not_map_to_mcp_framework(self) -> None:
        """K4 fix: bare 'mcp' topic is too generic — must NOT auto-map to mcp_framework.
        n8n / dify / open-webui / Snailclimb-JavaGuide all have 'mcp' topic but are
        not MCP frameworks. Only 'mcp-framework' / 'mcp-sdk' should trigger the tag.
        """
        result = match_github_topics(["mcp"])
        assert "mcp_framework" not in result

    def test_explicit_mcp_framework_topic_still_works(self) -> None:
        """Explicit 'mcp-framework' or 'mcp-sdk' topic SHOULD map to mcp_framework."""
        assert match_github_topics(["mcp-framework"]).get("mcp_framework") is not None
        assert match_github_topics(["mcp-sdk"]).get("mcp_framework") is not None

    def test_multiple_topics_all_matched(self) -> None:
        result = match_github_topics(["claude-skill", "typescript", "experimental"])
        assert {"skill_system", "typescript", "experimental"} <= set(result)

    def test_unknown_topic_ignored(self) -> None:
        result = match_github_topics(["zzz-unknown-topic"])
        assert result == {}

    def test_returns_traceability_value(self) -> None:
        """Returned dict values should be the matched alias for traceability."""
        result = match_github_topics(["mcp-server"])
        assert result["mcp_server"] == "mcp-server"


# ============================================================
# Layer 2: keyword rules
# ============================================================


class TestMatchKeywordRules:
    def test_empty_input_returns_empty(self) -> None:
        assert match_keyword_rules(name="", description=None, topics=[], owner="") == {}

    def test_description_keyword_match(self) -> None:
        """Description containing 'long-term memory' should match memory_system."""
        result = match_keyword_rules(
            name="some-repo",
            description="Provides long-term memory for AI agents",
            topics=[],
        )
        assert "memory_system" in result

    def test_owner_rule_anthropics_official(self) -> None:
        """Anthropic owner repos should auto-tag as official_anthropic."""
        result = match_keyword_rules(
            name="claude-code",
            description="claude code agent",
            topics=[],
            owner="anthropics",
        )
        assert "official_anthropic" in result

    def test_owner_rule_case_insensitive(self) -> None:
        result = match_keyword_rules(
            name="x", description=None, topics=[], owner="ANTHROPICS"
        )
        assert "official_anthropic" in result

    def test_python_implementation_phrase(self) -> None:
        result = match_keyword_rules(
            name="agent-lib",
            description="A pure Python implementation of agent orchestration",
            topics=[],
        )
        assert "python" in result

    def test_typescript_via_npm_install(self) -> None:
        result = match_keyword_rules(
            name="x",
            description="Install with npm install some-package",
            topics=[],
        )
        assert "typescript" in result

    def test_framework_positioning(self) -> None:
        result = match_keyword_rules(
            name="x",
            description="It is a framework for building agents",
            topics=[],
        )
        assert "framework" in result

    def test_traceability_records_keyword(self) -> None:
        result = match_keyword_rules(
            name="x",
            description="Production-ready library",
            topics=[],
        )
        assert "battle_tested" in result
        assert "production-ready" in result["battle_tested"].lower()


class TestRulesDictionaryIntegrity:
    def test_all_topic_aliases_have_canonical_tag_in_keyword_rules_or_owner(self) -> None:
        """Every tag in GITHUB_TOPIC_ALIASES should also appear in KEYWORD_RULES
        or OWNER_RULES so Layer 2 can fall back when topics are missing."""
        all_layer2_tags = set(KEYWORD_RULES.keys())
        for owner_tags in OWNER_RULES.values():
            all_layer2_tags.update(owner_tags)

        # Some pure-topic tags can be topic-only (rare). Allow whitelisted exceptions.
        topic_only_allowed: set[str] = set()
        topic_tags = set(GITHUB_TOPIC_ALIASES.keys())
        missing = topic_tags - all_layer2_tags - topic_only_allowed
        assert missing == set(), f"Tags missing Layer 2 fallback: {missing}"

    def test_no_empty_alias_lists(self) -> None:
        for tag, aliases in GITHUB_TOPIC_ALIASES.items():
            assert aliases, f"Tag {tag} has no aliases"
        for tag, kws in KEYWORD_RULES.items():
            assert kws, f"Tag {tag} has no keywords"


# ============================================================
# K4 edge-case regression tests (real ecosystem repos)
# ============================================================


class TestK4EdgeCases:
    """Five+ regression tests built from actual mis-tagged repos discovered
    via dry-run on the 265-repo ecosystem dataset (see docs/ecosystem-tag-edge-cases.md).
    """

    def test_n8n_workflow_platform_not_mcp_framework(self) -> None:
        """n8n (186K stars) is a workflow_engine + application that integrates MCP,
        but not an mcp_framework. Bare 'mcp' topic must not trigger mcp_framework.
        """
        topics = ["mcp", "mcp-client", "mcp-server", "workflow",
                  "workflow-automation", "automation", "typescript", "low-code"]
        topic_tags = match_github_topics(topics)
        kw_tags = match_keyword_rules(
            name="n8n",
            description="Fair-code workflow automation platform with native AI capabilities.",
            topics=topics,
            owner="n8n-io",
        )
        all_tags = set(topic_tags) | set(kw_tags)
        assert "mcp_framework" not in all_tags, "n8n must not be tagged as mcp_framework"
        # but it should still pick up mcp_server / workflow / typescript / etc.
        assert "mcp_server" in topic_tags
        assert "workflow_engine" in topic_tags
        assert "typescript" in topic_tags

    def test_awesome_mcp_servers_is_docs_only(self) -> None:
        """punkpeye/awesome-mcp-servers (86K) is a markdown awesome-list, not code.
        Must be tagged docs_only via name pattern. mcp_framework must NOT trigger.
        """
        kw = match_keyword_rules(
            name="awesome-mcp-servers",
            description="A collection of MCP servers.",
            topics=["mcp", "ai"],
            owner="punkpeye",
        )
        topic = match_github_topics(["mcp", "ai"])
        all_tags = set(kw) | set(topic)
        assert "docs_only" in all_tags, "awesome-* repos must be flagged docs_only"
        assert "mcp_framework" not in all_tags

    def test_jupyter_curriculum_is_docs_only(self) -> None:
        """microsoft/mcp-for-beginners is a Jupyter-notebook curriculum.
        Name 'for-beginners' triggers docs_only via DOCS_ONLY_NAME_PATTERNS.
        """
        kw = match_keyword_rules(
            name="mcp-for-beginners",
            description="Curriculum introducing fundamentals of Model Context Protocol",
            topics=["mcp", "tutorial"],
            owner="microsoft",
        )
        assert "docs_only" in kw

    def test_jupyter_language_signals_docs_only(self) -> None:
        """When primary language is Jupyter Notebook / TeX / HTML, repo is docs."""
        assert match_docs_only_by_language("Jupyter Notebook") is True
        assert match_docs_only_by_language("TeX") is True
        assert match_docs_only_by_language("HTML") is True
        assert match_docs_only_by_language("Python") is False
        assert match_docs_only_by_language(None) is False

    def test_claude_code_topic_maps_to_claude_code_tag(self) -> None:
        """affaan-m/everything-claude-code etc. previously got 0 tags because
        'claude-code' topic had no mapping. Must now hit claude_code tag.
        """
        result = match_github_topics(["claude-code"])
        assert "claude_code" in result

    def test_language_field_maps_to_tech_stack(self) -> None:
        """Repo's primary language field should drive the tech_stack tag directly,
        independent of description keywords (fixes 0-tag Rust/Java/JS repos).
        """
        assert match_language_tag("Python") == {"python": "language:Python"}
        assert match_language_tag("Rust") == {"rust": "language:Rust"}
        assert match_language_tag("Java") == {"java": "language:Java"}
        assert match_language_tag("JavaScript") == {"javascript": "language:JavaScript"}
        assert match_language_tag(None) == {}
        assert match_language_tag("") == {}

    def test_anthropic_owner_rule_still_works_but_no_keyword_overreach(self) -> None:
        """Owner=anthropics still tags official_anthropic (via OWNER_RULES),
        but description 'by Anthropic' on awesome-* repos must NOT trigger it.
        """
        # owner-based: should match
        own = match_keyword_rules(
            name="claude-code", description="Agentic CLI", topics=[], owner="anthropics"
        )
        assert "official_anthropic" in own

        # awesome-list with 'by Anthropic' in description but different owner: must NOT match
        not_official = match_keyword_rules(
            name="awesome-claude-code",
            description="A curated list of skills, hooks, agents for Claude Code by Anthropic",
            topics=["awesome-list", "claude-code"],
            owner="hesreallyhim",
        )
        assert "official_anthropic" not in not_official, (
            "awesome-* repos describing 'by Anthropic' must not steal official_anthropic"
        )

    def test_docs_only_name_patterns_cover_common_cases(self) -> None:
        """Various docs-only name shapes should all hit the docs_only tag."""
        for name in ["awesome-claude-code", "mcp-for-beginners", "claude-cookbook",
                     "ai-tutorial", "java-interview-guide", "learn-rust",
                     "examples", "knowledge-base"]:
            tags = match_keyword_rules(name=name, description="", topics=[], owner="x")
            assert "docs_only" in tags, f"name={name!r} should map to docs_only"
