"""Regression: template identity is the frontmatter ``name``, never the filename stem.

Defect (debate 503e07f1, action item 1): ``_collect_templates`` de-duplicated across
sources - and ``list_templates`` grouped - by ``path.stem``, while CC resolves
``subagent_type`` by the frontmatter ``name`` only. 15 of the 25 shipped templates
have ``stem != name`` (``engineering-security-engineer.md`` declares
``name: security-engineer``), so a user-level ``security-engineer.md`` and the
plugin-level ``engineering-security-engineer.md`` were listed as two distinct
templates while CC sees exactly one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiteam.api.routes import agent_templates as at
from aiteam.services import agent_template_registry as registry


def _write_template(directory: Path, stem: str, name: str | None, description: str = "x") -> Path:
    """Write a minimal agent template; ``name=None`` omits the frontmatter key."""
    directory.mkdir(parents=True, exist_ok=True)
    name_line = f"name: {name}\n" if name is not None else ""
    path = directory / f"{stem}.md"
    path.write_text(
        f"---\n{name_line}description: {description}\nmodel: opus\n---\n\nbody of {stem}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def template_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolate the three template sources under tmp_path."""
    project_root = tmp_path / "proj"
    project = project_root / ".claude" / "agents"
    user = tmp_path / "user-agents"
    plugin = tmp_path / "plugin-agents"
    for d in (project, user, plugin):
        d.mkdir(parents=True, exist_ok=True)
    # The catalogue lives in the registry; the route is a thin caller.
    monkeypatch.setattr(registry, "AGENTS_DIR", user)
    monkeypatch.setattr(registry, "PLUGIN_AGENTS_DIR", plugin)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return {
        "project_root": project_root,
        "project": project,
        "user": user,
        "plugin": plugin,
    }


def test_same_name_different_stem_is_one_template(template_dirs: dict[str, Path]) -> None:
    """The exact production collision: user `security-engineer.md` vs plugin
    `engineering-security-engineer.md`, both declaring `name: security-engineer`.

    CC resolves one template; the catalogue must report one, not two.
    """
    _write_template(template_dirs["user"], "security-engineer", "security-engineer")
    _write_template(template_dirs["plugin"], "engineering-security-engineer", "security-engineer")

    templates, _report = registry.collect_templates(None)

    names = [t.get("name") for t in templates]
    assert names == ["security-engineer"], f"stem-keyed de-dup leaked duplicate rows: {names}"
    assert templates[0]["source"] == "user", "higher-precedence source must win"


def test_highest_precedence_source_wins_across_all_three(template_dirs: dict[str, Path]) -> None:
    """project > user > plugin, matched on name even when every stem differs."""
    _write_template(template_dirs["project"], "local-sre", "sre", description="project copy")
    _write_template(template_dirs["user"], "sre", "sre", description="user copy")
    _write_template(template_dirs["plugin"], "engineering-sre", "sre", description="plugin copy")

    templates, report = registry.collect_templates(str(template_dirs["project_root"]))

    assert len(templates) == 1, [t.get("filename") for t in templates]
    assert templates[0]["source"] == "project"
    assert templates[0]["description"] == "project copy"
    # Per-source counts still report what each directory physically holds.
    assert report["project"]["found"] == 1
    assert report["user"]["found"] == 1
    assert report["plugin"]["found"] == 1


def test_distinct_names_sharing_no_stem_all_survive(template_dirs: dict[str, Path]) -> None:
    """De-dup must not over-merge: different names stay separate templates."""
    _write_template(template_dirs["plugin"], "engineering-sre", "sre")
    _write_template(template_dirs["plugin"], "testing-api-tester", "api-tester")
    _write_template(template_dirs["plugin"], "team-member", "team-member")

    templates, _report = registry.collect_templates(None)

    assert sorted(t["name"] for t in templates) == ["api-tester", "sre", "team-member"]


def test_missing_frontmatter_name_falls_back_to_stem(template_dirs: dict[str, Path]) -> None:
    """A template without `name:` is still catalogued, keyed by its stem."""
    _write_template(template_dirs["plugin"], "nameless-helper", None)

    templates, _report = registry.collect_templates(None)

    assert len(templates) == 1
    assert templates[0]["name"] == "nameless-helper"
    assert templates[0]["filename"] == "nameless-helper"


def test_blank_frontmatter_name_falls_back_to_stem(template_dirs: dict[str, Path]) -> None:
    """`name:` present but empty must not collapse every such template into one key."""
    _write_template(template_dirs["plugin"], "blank-one", '""')
    _write_template(template_dirs["plugin"], "blank-two", '""')

    templates, _report = registry.collect_templates(None)

    assert sorted(t["name"] for t in templates) == ["blank-one", "blank-two"]


def test_filename_field_still_reports_the_real_stem(template_dirs: dict[str, Path]) -> None:
    """`filename` keeps pointing at the file on disk - it is how /{name} finds it."""
    _write_template(template_dirs["plugin"], "engineering-security-engineer", "security-engineer")

    templates, _report = registry.collect_templates(None)

    assert templates[0]["name"] == "security-engineer"
    assert templates[0]["filename"] == "engineering-security-engineer"


async def test_grouped_categories_follow_the_resolved_name(template_dirs: dict[str, Path]) -> None:
    """Grouping keys off the same identity as de-dup, so a template cannot appear
    under a category derived from a stem CC never sees."""
    _write_template(template_dirs["plugin"], "engineering-security-engineer", "security-engineer")
    _write_template(template_dirs["plugin"], "testing-api-tester", "api-tester")
    _write_template(template_dirs["plugin"], "team-member", "team-member")

    payload = await at.list_templates(x_project_dir=None)

    assert payload["total"] == 3
    flattened = [t["name"] for group in payload["grouped"].values() for t in group]
    assert sorted(flattened) == ["api-tester", "security-engineer", "team-member"]
    assert set(payload["grouped"]) == {"security", "api", "team"}


async def test_grouped_has_no_duplicate_rows(template_dirs: dict[str, Path]) -> None:
    """The user-visible symptom: the same CC template listed twice."""
    _write_template(template_dirs["user"], "security-engineer", "security-engineer")
    _write_template(template_dirs["plugin"], "engineering-security-engineer", "security-engineer")

    payload = await at.list_templates(x_project_dir=None)

    flattened = [t["name"] for group in payload["grouped"].values() for t in group]
    assert flattened == ["security-engineer"], f"duplicate rows in grouped: {flattened}"
    assert payload["total"] == 1


async def test_detail_route_resolves_the_name_the_listing_reports(
    template_dirs: dict[str, Path],
) -> None:
    """Listing and detail must agree: a name shown in the catalogue must open."""
    _write_template(template_dirs["plugin"], "engineering-security-engineer", "security-engineer")

    by_name = await at.get_template("security-engineer", x_project_dir=None)
    assert "error" not in by_name, by_name
    assert by_name["meta"]["filename"] == "engineering-security-engineer"

    # The stem still resolves - existing callers keep working.
    by_stem = await at.get_template("engineering-security-engineer", x_project_dir=None)
    assert "error" not in by_stem, by_stem
    assert by_stem["meta"]["name"] == "security-engineer"

    missing = await at.get_template("no-such-template", x_project_dir=None)
    assert "error" in missing


async def test_shipped_templates_are_deduped_by_name() -> None:
    """End-to-end against the real plugin/ + ~/.claude copies: no name collides.

    The shipped set is where the defect bit - `engineering-*.md` files declare
    short names, so any stem-keyed catalogue double-counts installed copies.
    """
    templates, _report = registry.collect_templates(None)
    names = [t["name"] for t in templates]
    assert len(names) == len(set(names)), "duplicate template names in the live catalogue"
