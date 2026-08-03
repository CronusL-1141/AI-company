"""launch_call guard: every spawn plan checks its subagent_type - and only warns.

Debate 503e07f1 action item 2, as amended 2026-08-03: **templates are an accelerator,
not a whitelist.** The legal set of subagent_types is CC's real registration surface
(three-source templates plus CC built-ins, which cannot be enumerated and shift with
CC versions). So an unknown type is *reported, never rewritten*: the plan still spawns
what the caller asked for, with a prominent top-level warning for the human reading it.

Two layers of guarding live here:

1. Runtime - each dispatch builder surfaces unknown types at the top level of its plan
   while leaving ``launch_call.params.subagent_type`` untouched.
2. Static - every launch_call construction point in the repo goes through the shared
   checker, and every subagent_type this repo *hardcodes* really exists. Names that
   arrive from a caller are deliberately not asserted: they may legitimately be a CC
   built-in this repo knows nothing about.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from testlib import shipped_template_names, spawnable_subagent_types

from aiteam.services import agent_template_registry as registry
from aiteam.services.agent_template_registry import FALLBACK_SUBAGENT_TYPE

REPO_ROOT = Path(__file__).resolve().parents[2]
# Production trees that may build a spawn plan. Tests are excluded on purpose: they
# assert on plans, they do not construct them.
SCANNED_TREES = ("src", "plugin", "scripts")

CHECKER_NAME = "check_subagent_types"


# ============================================================
# Static: discovery of every launch_call construction point
# ============================================================


def _python_sources() -> list[Path]:
    files: list[Path] = []
    for tree in SCANNED_TREES:
        root = REPO_ROOT / tree
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _dict_literals_with_subagent_type(tree: ast.AST) -> list[ast.Dict]:
    """Dict literals carrying a ``subagent_type`` key - i.e. real spawn params."""
    found: list[ast.Dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "subagent_type" for k in node.keys
        ):
            found.append(node)
    return found


def _module_level_str_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for resolving named defaults."""
    consts: dict[str, str] = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.target is not None:
            targets = [node.target]
        value = getattr(node, "value", None)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for t in targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = value.value
    return consts


def _hardcoded_subagent_types(node: ast.AST, module_consts: dict[str, str]) -> list[str]:
    """String subagent_type values this repo pins itself.

    Covers plain literals, ``caller_value or "fallback"`` and ``A if c else B``, plus
    module-level constants referenced by name. Non-constant expressions (a caller's
    variable) are skipped - those are precisely the names we refuse to assert on.
    """
    values: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        values.append(node.value)
    elif isinstance(node, ast.Name) and node.id in module_consts:
        values.append(module_consts[node.id])
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            values.extend(_hardcoded_subagent_types(v, module_consts))
    elif isinstance(node, ast.IfExp):
        for v in (node.body, node.orelse):
            values.extend(_hardcoded_subagent_types(v, module_consts))
    return values


def _launch_call_construction_points() -> dict[Path, tuple[str, ast.Module]]:
    """{path: (source, ast)} for every module that builds spawn params."""
    points: dict[Path, tuple[str, ast.Module]] = {}
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        if "subagent_type" not in source:
            continue
        tree = ast.parse(source)
        if _dict_literals_with_subagent_type(tree):
            points[path] = (source, tree)
    return points


def test_construction_point_discovery_is_not_empty() -> None:
    """The discovery itself must find something, or every guard below is vacuous."""
    points = _launch_call_construction_points()
    assert points, "found no launch_call construction point - the AST probe is broken"


def test_every_launch_call_site_runs_the_shared_check() -> None:
    """A new spawn-plan builder must not ship without the template check."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path, (source, _tree) in _launch_call_construction_points().items()
        if CHECKER_NAME not in source
    ]
    assert not offenders, (
        f"these modules build launch_call params without calling {CHECKER_NAME}(): {offenders}"
    )


def test_hardcoded_subagent_types_all_exist() -> None:
    """Repo-pinned names (defaults/fallbacks) must be real; caller names are not asserted."""
    allowed = spawnable_subagent_types()
    bad: list[str] = []
    for path, (_source, tree) in _launch_call_construction_points().items():
        consts = _module_level_str_constants(tree)
        for d in _dict_literals_with_subagent_type(tree):
            for key, value in zip(d.keys, d.values, strict=False):
                if not (isinstance(key, ast.Constant) and key.value == "subagent_type"):
                    continue
                for name in _hardcoded_subagent_types(value, consts):
                    if name not in allowed:
                        bad.append(f"{path.relative_to(REPO_ROOT)}: {name!r}")
    assert not bad, f"hardcoded subagent_type that no template provides: {bad}"


def test_shipped_template_guard_counts_every_file() -> None:
    """The helper must see one name per shipped file - no silent shrinkage.

    A regex-scraped guard used to answer this question; it skipped templates without a
    frontmatter name and matched body text instead, so the set could shrink without a
    single test going red.
    """
    from aiteam.services.agent_template_registry import PLUGIN_AGENTS_DIR

    names = shipped_template_names()
    assert len(names) == len(list(PLUGIN_AGENTS_DIR.glob("*.md")))
    assert FALLBACK_SUBAGENT_TYPE in spawnable_subagent_types()


# ============================================================
# Runtime: check_subagent_types reports, never rewrites
# ============================================================


@pytest.fixture
def only_known_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Catalogue reduced to a single template, so 'unknown' is deterministic."""
    plugin = tmp_path / "plugin-agents"
    plugin.mkdir()
    (plugin / "engineering-code-reviewer.md").write_text(
        "---\nname: code-reviewer\ndescription: reviews code\nmodel: opus\n---\n\nbody\n",
        encoding="utf-8",
    )
    empty_user = tmp_path / "empty-user"
    empty_user.mkdir()
    monkeypatch.setattr(registry, "PLUGIN_AGENTS_DIR", plugin)
    monkeypatch.setattr(registry, "AGENTS_DIR", empty_user)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return "code-reviewer"


def test_known_template_produces_no_warning(only_known_template: str) -> None:
    assert registry.check_subagent_types([(only_known_template, "reviewer")]) == []


def test_sanctioned_fallback_is_never_flagged(only_known_template: str) -> None:
    """general-purpose is a CC built-in absent from the catalogue - warning it on every
    default dispatch would train readers to ignore the warning."""
    assert registry.check_subagent_types([(FALLBACK_SUBAGENT_TYPE, "default")]) == []


def test_unknown_type_is_reported_with_a_suggestion(only_known_template: str) -> None:
    warnings = registry.check_subagent_types([("code-reviwer", "reviewer")])

    assert len(warnings) == 1
    entry = warnings[0]
    assert entry["subagent_type"] == "code-reviwer"
    assert entry["used_by"] == ["reviewer"]
    assert entry["closest_templates"] == ["code-reviewer"], entry
    assert "不在模板库" in entry["message"]
    assert FALLBACK_SUBAGENT_TYPE in entry["message"]


def test_unknown_type_with_no_near_match_still_reports(only_known_template: str) -> None:
    warnings = registry.check_subagent_types([("zzzz-nothing-like-it", "x")])

    assert len(warnings) == 1
    assert warnings[0]["closest_templates"] == []
    assert "无相近模板" in warnings[0]["message"]


def test_repeated_unknown_type_collapses_to_one_entry(only_known_template: str) -> None:
    warnings = registry.check_subagent_types([("ghost", "a"), ("ghost", "b"), ("ghost", "a")])

    assert len(warnings) == 1
    assert warnings[0]["used_by"] == ["a", "b"]


def test_empty_type_is_not_reported(only_known_template: str) -> None:
    """An empty slot means 'use the builder's default', not a bogus type."""
    assert registry.check_subagent_types([("", "x"), ("   ", "y")]) == []


# ============================================================
# Runtime: the two dispatch builders
# ============================================================


def _participant(**overrides) -> dict:
    base = {
        "name": "reviewer",
        "agent_template": "code-reviewer",
        "role": "评审",
        "context_files": [],
        "expected_output": "三段式",
    }
    base.update(overrides)
    return base


def _build_meeting_plan(participants: list) -> tuple[list, list]:
    from aiteam.mcp.tools.meeting import _build_dispatch_plan

    plan, _expected, _legacy, template_warnings = _build_dispatch_plan(
        meeting_id="mtg-1",
        title="T",
        participants_raw=participants,
        rounds=[],
        materials=[],
        team_name="team",
    )
    return plan, template_warnings


def test_meeting_known_template_has_no_warning(only_known_template: str) -> None:
    plan, warnings = _build_meeting_plan([_participant()])

    assert warnings == []
    assert plan[0]["launch_call"]["params"]["subagent_type"] == "code-reviewer"


def test_meeting_unknown_template_passes_through_untouched(only_known_template: str) -> None:
    """The whole point of the amendment: warn loudly, spawn exactly what was asked."""
    plan, warnings = _build_meeting_plan([_participant(agent_template="my-custom-agent")])

    assert plan[0]["launch_call"]["params"]["subagent_type"] == "my-custom-agent", (
        "the caller's subagent_type was rewritten - silent downgrade is forbidden"
    )
    assert plan[0]["ready_to_paste"] is True, "an unknown type must not block the dispatch"
    assert len(warnings) == 1
    assert warnings[0]["subagent_type"] == "my-custom-agent"
    assert warnings[0]["used_by"] == ["reviewer"]


def test_meeting_missing_template_uses_the_sanctioned_fallback(only_known_template: str) -> None:
    plan, warnings = _build_meeting_plan([_participant(agent_template="")])

    assert plan[0]["launch_call"]["params"]["subagent_type"] == FALLBACK_SUBAGENT_TYPE
    assert warnings == []


def test_meeting_legacy_string_participant_is_not_checked(only_known_template: str) -> None:
    """Legacy strings carry no launch_call, so there is no type to check."""
    plan, warnings = _build_meeting_plan(["someone"])

    assert plan[0]["launch_call"] == {}
    assert warnings == []


def test_meeting_create_surfaces_warnings_at_the_top_level(
    only_known_template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning buried inside dispatch_plan[i] would be read by nobody."""
    from aiteam.mcp import _base
    from aiteam.mcp.tools import meeting as meeting_tools

    monkeypatch.setattr(_base, "_resolve_team_id", lambda team_id: "team-1")
    monkeypatch.setattr(meeting_tools, "_resolve_team_id", lambda team_id: "team-1")
    monkeypatch.setattr(
        meeting_tools, "_api_call", lambda *a, **kw: {"success": True, "data": {"id": "mtg-9"}}
    )

    captured: dict = {}

    class _Recorder:
        def tool(self, *_a, **_kw):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    meeting_tools.register(_Recorder())
    result = captured["meeting_create"](
        topic="T",
        team_id="team-1",
        participants=[_participant(agent_template="my-custom-agent")],
    )

    assert result["template_warnings"], "unknown template must surface at the plan's top level"
    assert result["template_warnings"][0]["subagent_type"] == "my-custom-agent"
    assert "my-custom-agent" in result["dispatch_instructions"]
    # And the plan still spawns what was asked for.
    assert result["dispatch_plan"][0]["launch_call"]["params"]["subagent_type"] == "my-custom-agent"


# ============================================================
# Runtime: ecosystem tagger Layer 3 dispatch
# ============================================================


async def _tagger_plan(agent_template: str | None = None) -> dict:
    from aiteam.services.ecosystem_tagger import EcosystemTagger
    from aiteam.storage.connection import close_db
    from aiteam.storage.repository import StorageRepository

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    try:
        tagger = EcosystemTagger(repo)
        repos = [
            {
                "id": "repo-1",
                "repo_full_name": "acme/thing",
                "description": "x",
                "topics": [],
                "language": "Python",
            }
        ]
        if agent_template is None:
            return await tagger.build_llm_dispatch_plan(repos)
        return await tagger.build_llm_dispatch_plan(repos, agent_template=agent_template)
    finally:
        await close_db()


async def test_tagger_default_template_raises_no_warning(only_known_template: str) -> None:
    plan = await _tagger_plan()

    assert plan["template_warnings"] == []
    assert plan["dispatch"][0]["launch_call"]["params"]["subagent_type"] == FALLBACK_SUBAGENT_TYPE


async def test_tagger_unknown_template_passes_through_untouched(only_known_template: str) -> None:
    plan = await _tagger_plan("code-reviwer")

    assert plan["dispatch"][0]["launch_call"]["params"]["subagent_type"] == "code-reviwer", (
        "the caller's subagent_type was rewritten - silent downgrade is forbidden"
    )
    assert plan["agent_template"] == "code-reviwer"
    assert len(plan["template_warnings"]) == 1
    assert plan["template_warnings"][0]["closest_templates"] == ["code-reviewer"]
    assert "⚠️" in plan["instructions"]
    assert "code-reviwer" in plan["instructions"]


async def test_tagger_known_template_raises_no_warning(only_known_template: str) -> None:
    plan = await _tagger_plan("code-reviewer")

    assert plan["template_warnings"] == []
    assert "⚠️" not in plan["instructions"]
