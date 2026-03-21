"""Unit tests for the find_skill progressive loading system.

Tests all 3 layers of skill discovery:
  Layer 1: Quick recommendation by task description
  Layer 2: Category browsing
  Layer 3: Full skill detail lookup
"""

from __future__ import annotations

import pytest

from aiteam.mcp.skill_registry import (
    _SKILL_INDEX,
    CATEGORIES,
    CATEGORY_LABELS,
    SKILLS,
    Skill,
    find_skill_category,
    find_skill_detail,
    find_skill_quick,
)

# ============================================================
# Skill data integrity
# ============================================================


class TestSkillDataIntegrity:
    """Ensure the skill catalog is well-formed."""

    def test_all_skills_have_required_fields(self):
        for s in SKILLS:
            assert s.id, f"Skill missing id: {s}"
            assert s.name, f"Skill {s.id} missing name"
            assert s.oneliner, f"Skill {s.id} missing oneliner"
            assert s.category, f"Skill {s.id} missing category"
            assert s.install_cmd, f"Skill {s.id} missing install_cmd"

    def test_unique_ids(self):
        ids = [s.id for s in SKILLS]
        assert len(ids) == len(set(ids)), f"Duplicate skill IDs found: {ids}"

    def test_all_categories_have_labels(self):
        for cat in CATEGORIES:
            assert cat in CATEGORY_LABELS, f"Category '{cat}' missing display label"

    def test_skill_index_matches_catalog(self):
        assert len(_SKILL_INDEX) == len(SKILLS)
        for s in SKILLS:
            assert s.id in _SKILL_INDEX


# ============================================================
# Skill serialization layers
# ============================================================


class TestSkillSerialization:
    """Test the 3-layer output format of individual skills."""

    @pytest.fixture()
    def sample_skill(self) -> Skill:
        return Skill(
            id="test-skill",
            name="Test Skill",
            oneliner="A test skill for unit testing",
            category="testing",
            install_cmd="/install test-skill",
            tags=["test", "unit"],
            github="test/test-skill",
            stars="100",
            features=["Feature A", "Feature B"],
            os_complement="Complements OS by testing things.",
            use_cases=["Unit testing"],
            variants=["test-skill-v2"],
            compatibility="All platforms",
        )

    def test_layer1_fields(self, sample_skill: Skill):
        data = sample_skill.to_layer1()
        assert set(data.keys()) == {"id", "name", "oneliner", "category", "install_cmd"}
        assert data["id"] == "test-skill"
        assert data["install_cmd"] == "/install test-skill"

    def test_layer2_extends_layer1(self, sample_skill: Skill):
        l1 = sample_skill.to_layer1()
        l2 = sample_skill.to_layer2()
        # Layer 2 must contain all Layer 1 fields
        for key in l1:
            assert key in l2
        # Layer 2 adds these fields
        assert "tags" in l2
        assert "features" in l2
        assert "use_cases" in l2
        assert "github" in l2

    def test_layer3_extends_layer2(self, sample_skill: Skill):
        l2 = sample_skill.to_layer2()
        l3 = sample_skill.to_layer3()
        for key in l2:
            assert key in l3
        assert "os_complement" in l3
        assert "variants" in l3
        assert "compatibility" in l3


# ============================================================
# Layer 1: Quick recommendation
# ============================================================


class TestLayer1QuickRecommend:
    """Test task-description-based skill recommendations."""

    def test_returns_level_1_metadata(self):
        result = find_skill_quick("build a frontend app")
        assert result["level"] == 1
        assert result["level_name"] == "quick_recommend"
        assert "results" in result
        assert "hint" in result

    def test_frontend_query_returns_frontend_design(self):
        result = find_skill_quick("frontend ui design")
        ids = [r["id"] for r in result["results"]]
        assert "frontend-design" in ids

    def test_security_query_returns_vibesec(self):
        result = find_skill_quick("security audit web application")
        ids = [r["id"] for r in result["results"]]
        assert "vibesec" in ids

    def test_data_science_query_returns_jupyter(self):
        result = find_skill_quick("data science jupyter notebook ml")
        ids = [r["id"] for r in result["results"]]
        assert "jupyter-notebook" in ids

    def test_code_review_query(self):
        result = find_skill_quick("code review PR quality")
        ids = [r["id"] for r in result["results"]]
        assert "code-review" in ids or "pr-review-toolkit" in ids

    def test_top_n_limit(self):
        result = find_skill_quick("build something", top_n=3)
        assert len(result["results"]) <= 3

    def test_results_have_match_score(self):
        result = find_skill_quick("frontend design")
        for r in result["results"]:
            assert "match_score" in r

    def test_results_sorted_by_score_descending(self):
        result = find_skill_quick("security testing code review")
        scores = [r["match_score"] for r in result["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_fallback_when_no_match(self):
        result = find_skill_quick("xyzzy_nonexistent_task_type")
        # Should still return results (fallback to first N skills)
        assert len(result["results"]) > 0

    def test_results_contain_layer1_fields(self):
        result = find_skill_quick("backend api development")
        for r in result["results"]:
            assert "id" in r
            assert "name" in r
            assert "oneliner" in r
            assert "install_cmd" in r


# ============================================================
# Layer 2: Category browsing
# ============================================================


class TestLayer2CategoryBrowse:
    """Test category-based skill browsing."""

    def test_all_categories_overview(self):
        result = find_skill_category()
        assert result["level"] == 2
        assert result["level_name"] == "category_browse"
        assert "categories" in result
        assert len(result["results"]) == len(CATEGORIES)

    def test_specific_category_filter(self):
        result = find_skill_category("security")
        assert result["level"] == 2
        assert len(result["results"]) > 0
        # All returned skills should be in security category
        for label, skills in result["results"].items():
            for s in skills:
                assert s["category"] == "security"

    def test_category_partial_match(self):
        result = find_skill_category("code")
        assert len(result["results"]) > 0

    def test_nonexistent_category_returns_error(self):
        result = find_skill_category("nonexistent_category_xyz")
        assert "error" in result
        assert "available_categories" in result

    def test_category_results_have_layer2_fields(self):
        result = find_skill_category("frontend")
        for label, skills in result["results"].items():
            for s in skills:
                assert "tags" in s
                assert "features" in s
                assert "use_cases" in s


# ============================================================
# Layer 3: Full detail
# ============================================================


class TestLayer3FullDetail:
    """Test individual skill full-detail lookups."""

    def test_exact_id_lookup(self):
        result = find_skill_detail("vibesec")
        assert result["level"] == 3
        assert result["level_name"] == "full_detail"
        assert "result" in result
        assert result["result"]["id"] == "vibesec"

    def test_full_detail_contains_all_fields(self):
        result = find_skill_detail("superpowers")
        skill_data = result["result"]
        assert "os_complement" in skill_data
        assert "variants" in skill_data
        assert "compatibility" in skill_data
        assert "features" in skill_data
        assert "install_cmd" in skill_data

    def test_fuzzy_match_by_name(self):
        result = find_skill_detail("Frontend-Design")
        assert "result" in result
        assert result["result"]["id"] == "frontend-design"

    def test_partial_id_match(self):
        result = find_skill_detail("vibe")
        assert "result" in result
        assert result["result"]["id"] == "vibesec"

    def test_nonexistent_skill_returns_error(self):
        result = find_skill_detail("nonexistent_skill_xyz")
        assert "error" in result
        assert "available_skills" in result

    def test_all_catalog_skills_are_findable(self):
        for skill in SKILLS:
            result = find_skill_detail(skill.id)
            assert "result" in result, f"Skill {skill.id} not found via find_skill_detail"
            assert result["result"]["id"] == skill.id


# ============================================================
# MCP tool integration (import-level check)
# ============================================================


class TestMCPToolRegistration:
    """Verify the find_skill MCP tool can be imported."""

    def test_find_skill_importable(self):
        from aiteam.mcp.server import find_skill
        assert callable(find_skill)

    def test_find_skill_level1_via_tool(self):
        from aiteam.mcp.server import find_skill
        result = find_skill(task_description="frontend design", level=1)
        assert result.get("level") == 1 or "error" not in result

    def test_find_skill_level2_via_tool(self):
        from aiteam.mcp.server import find_skill
        result = find_skill(level=2, category="security")
        assert result.get("level") == 2

    def test_find_skill_level3_via_tool(self):
        from aiteam.mcp.server import find_skill
        result = find_skill(level=3, skill_id="vibesec")
        assert result.get("level") == 3

    def test_find_skill_level1_missing_description(self):
        from aiteam.mcp.server import find_skill
        result = find_skill(level=1)
        assert "error" in result

    def test_find_skill_level3_missing_skill_id(self):
        from aiteam.mcp.server import find_skill
        result = find_skill(level=3)
        assert "error" in result
