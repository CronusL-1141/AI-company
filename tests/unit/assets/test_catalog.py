"""The classifier: deny table first, then categories as a whitelist; pure string work."""

from __future__ import annotations

import pytest

from aiteam.assets.catalog import catalog, classify, denied_by, deny_rules, worth_walking
from aiteam.assets.catalog_claude import GLOBAL_CONFIG_NAME, MAIN_CHAIN
from aiteam.types import AssetCategory, AssetHost

CLAUDE = AssetHost.CLAUDE
C = AssetCategory


@pytest.mark.parametrize(("path", "category"), [
    ("CLAUDE.md", C.GLOBAL_INSTRUCTIONS),
    ("loop.md", C.LOOP_PROMPT),
    ("projects/-home-me-proj/memory/MEMORY.md", C.PROJECT_MEMORY),
    ("projects/-home-me-proj/memory/archive/2026/old.md", C.PROJECT_MEMORY),
    ("skills/mine/SKILL.md", C.SKILLS),
    ("skills/mine/templates/a.txt", C.SKILLS),
    ("agents/reviewer.md", C.AGENTS),
    ("commands/deploy.md", C.COMMANDS),
    ("commands/group/sub.md", C.COMMANDS),
    ("hooks/guard.sh", C.HOOK_SCRIPTS),
    ("settings.json", C.HOOK_REGISTRATION),
    (GLOBAL_CONFIG_NAME, C.MCP_SERVERS),
    ("plugins/installed_plugins.json", C.PLUGIN_MANIFEST),
    ("plugins/known_marketplaces.json", C.PLUGIN_MANIFEST),
])
def test_assets_are_classified(path, category):
    assert classify(CLAUDE, path) == category


@pytest.mark.parametrize("path", [
    # Session records are an optional category of a later stage; caches and state never.
    "projects/-home-me-proj/0b1c.jsonl",
    "projects/-home-me-proj/0b1c/subagents/agent-1.jsonl",
    "projects/-home-me-proj/context-monitor.json",
    "history.jsonl",
    "skills/synced/account-skill/SKILL.md",  # comes back with the account
    "skills/README.md",                        # not inside a skill folder
    "agents/nested/a.md",
    f"{MAIN_CHAIN}/send_event.py",             # OS's hook chain
    "settings.json.bak-20261111",
    "CLAUDE.md.backup",
    "backups/settings.json",
    "sessions/42.json",
    ".credentials.json",
    "plugins/cache/p/plugin.json",
    "plugins/marketplaces/m/x.json",
    "usage-merge.json",
    "other-tool/sentinel.py",                  # anything no category names
    "",
    "/etc/passwd",
    "../outside/CLAUDE.md",
    "projects/../CLAUDE.md",
])
def test_everything_else_is_not_managed(path):
    assert classify(CLAUDE, path) is None


def test_deny_matches_without_case_categories_with_case():
    assert denied_by(CLAUDE, "Backups/Settings.JSON") is not None
    assert denied_by(CLAUDE, "SESSIONS/1.json") is not None
    assert classify(CLAUDE, "claude.md") is None  # categories are exact: the host's own spelling


def test_deny_beats_every_category():
    # A backup copy inside project memory matches the memory category and a deny rule.
    assert classify(CLAUDE, "projects/-p/memory/MEMORY.md.bak") is None
    assert classify(CLAUDE, "projects/-p/memory/old-backup/x.md") is None


def test_every_deny_sample_is_unmanaged_on_every_host():
    for host in AssetHost:
        rules = deny_rules(host)
        assert rules, host
        for rule in rules:
            assert rule.samples, rule.pattern
            for sample in rule.samples:
                assert classify(host, sample) is None, (host, sample)


def test_the_codex_catalog_is_a_skeleton_that_reads_nothing():
    assert catalog(AssetHost.CODEX).categories == ()
    for path in ("AGENTS.md", "rules/default.rules", "memories/MEMORY.md", "config.toml"):
        assert classify(AssetHost.CODEX, path) is None


@pytest.mark.parametrize(("folder", "expected"), [
    ("projects", True),
    ("projects/-p", True),
    ("projects/-p/memory", True),
    ("projects/-p/memory/archive", True),
    ("projects/-p/0b1c", False),        # a session's folder: never listed further
    ("projects/-p/0b1c/tool-results", False),
    ("skills", True),
    ("skills/synced", False),
    ("hooks", True),
    (MAIN_CHAIN, False),
    ("sessions", False),
    ("plugins", False),                 # the lists are stat'ed directly
    ("cache", False),
    ("file-history", False),
])
def test_the_walk_descends_only_where_an_asset_can_be(folder, expected):
    assert worth_walking(CLAUDE, folder, catalog(CLAUDE).default_categories) is expected


def test_a_deselected_category_is_not_walked():
    assert not worth_walking(CLAUDE, "projects", [C.SKILLS])


@pytest.mark.parametrize(("host", "path"), [
    (CLAUDE, ".credentials.json"), (CLAUDE, "sessions/1.json"), (CLAUDE, "usage-merge.json"),
    (CLAUDE, "plugins/cache/p/x.json"), (AssetHost.CODEX, "auth.json"), (AssetHost.CODEX, "state_5.sqlite"),
])
def test_credentials_and_state_are_denied_not_just_unclassified(host, path):
    """Unclassified is not enough: a later category must not be able to reach these."""
    assert denied_by(host, path) is not None
