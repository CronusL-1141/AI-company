"""Tests for meeting template loader (templates.py)."""

from __future__ import annotations

import sys

import pytest

TEMPLATE_NAMES = [
    "brainstorm",
    "decision",
    "review",
    "retrospective",
    "standup",
    "debate",
    "lean_coffee",
    "council",
]


def _fresh_module():
    """Import templates with a clean cache (invalidate module-level _cache)."""
    mod_name = "aiteam.meeting.templates"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import aiteam.meeting.templates as m
    return m


def test_all_templates_load():
    """All 8 templates must be loadable."""
    m = _fresh_module()
    loaded = set(m.TEMPLATE_ROUNDS.keys())
    assert loaded == set(TEMPLATE_NAMES), f"Missing: {set(TEMPLATE_NAMES) - loaded}"


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_structure(name):
    """Each template must have total_rounds, description, and non-empty rounds list."""
    m = _fresh_module()
    tpl = m.TEMPLATE_ROUNDS[name]
    assert isinstance(tpl["total_rounds"], int) and tpl["total_rounds"] > 0
    assert isinstance(tpl["description"], str) and tpl["description"]
    assert isinstance(tpl["rounds"], list) and len(tpl["rounds"]) > 0
    for r in tpl["rounds"]:
        assert "number" in r
        assert "name" in r
        assert "rule" in r


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_rounds_count_matches_total(name):
    """len(rounds) must equal total_rounds declared in frontmatter."""
    m = _fresh_module()
    tpl = m.TEMPLATE_ROUNDS[name]
    assert len(tpl["rounds"]) == tpl["total_rounds"]


def test_recommend_debate():
    """Keyword 'debate' in topic should return 'debate' template."""
    m = _fresh_module()
    name, reason = m.recommend_template("请组织一个 debate")
    assert name == "debate"


def test_recommend_brainstorm_fallback():
    """No keyword match should fall back to 'brainstorm'."""
    m = _fresh_module()
    name, reason = m.recommend_template("随便聊聊xyz123无关词")
    assert name == "brainstorm"
    assert "no keyword match" in reason


def test_custom_template_loaded(tmp_path, monkeypatch):
    """Drop a custom template file and verify it gets loaded."""
    import aiteam.meeting.templates as base_mod

    custom_dir = tmp_path / "tpl"
    custom_dir.mkdir()
    custom_file = custom_dir / "custom_test.md"
    custom_file.write_text(
        "---\n"
        "template_name: custom_test\n"
        "description: Custom template for testing\n"
        "total_rounds: 1\n"
        "rounds:\n"
        "  - number: 1\n"
        "    name: Test round\n"
        "    rule: Just test\n"
        "keywords: [custom_test_keyword]\n"
        "---\n\n# Custom\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(base_mod, "_TEMPLATE_DIR", custom_dir)
    monkeypatch.setattr(base_mod, "_cache", None)

    assert "custom_test" in base_mod.TEMPLATE_ROUNDS
    assert base_mod.TEMPLATE_ROUNDS["custom_test"]["total_rounds"] == 1


def test_missing_template_dir_returns_empty(tmp_path, monkeypatch):
    """If template directory does not exist, should return empty dicts without crashing."""
    import aiteam.meeting.templates as base_mod

    nonexistent = tmp_path / "nonexistent_dir"
    monkeypatch.setattr(base_mod, "_TEMPLATE_DIR", nonexistent)
    monkeypatch.setattr(base_mod, "_cache", None)

    assert len(list(base_mod.TEMPLATE_ROUNDS.keys())) == 0
    assert len(list(base_mod.TEMPLATE_KEYWORDS.keys())) == 0
