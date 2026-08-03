"""Shared test helpers (importable — `tests` is on pythonpath)."""

from __future__ import annotations

import asyncio


def make_team(payload: dict | None = None, **overrides) -> dict:
    """Create a team for test setup, straight through the injected repository.

    ``POST /api/teams`` was retired 2026-07-27 (batch 7): in production a team is
    only ever minted by the hook chain (SubagentStart auto-enrolment / workflow
    ingest), never over HTTP. Tests therefore mint theirs the same way — through
    the repository the app is already wired to (``deps._repository``), which the
    ``app_client`` / ``integration_client`` fixtures set up.

    Takes the same body dict the retired route accepted, so call sites read the
    same as before. Returns the created team as a plain dict.
    """
    from aiteam.api import deps

    body = dict(payload or {})
    body.update(overrides)
    name = body.pop("name")
    mode = body.pop("mode", "coordinate")
    config = body.pop("config", None)
    repo = deps._repository
    if repo is None:  # pragma: no cover — misuse guard
        raise RuntimeError("make_team() requires an app fixture that sets deps._repository")
    team = asyncio.get_event_loop().run_until_complete(
        repo.create_team(name=name, mode=mode, config=config, **body)
    )
    return team.model_dump()


# ============================================================
# Agent template guard - shared by every test that asserts a
# hardcoded subagent_type is real.
# ============================================================


def shipped_template_names() -> set[str]:
    """Frontmatter names of the templates shipped in ``plugin/agents``.

    Deliberately the shipped directory only, not the live three-source catalogue:
    the guard has to give the same answer on a CI box with no ``~/.claude/agents``.

    Parsing goes through the production parser (``parse_template``), never a regex.
    A regex such as ``^name:\\s*(\\S+)`` skips a template that declares no name and
    then happily matches a ``name:`` line inside another template's body - the guard
    set silently shrinks and gains a name nobody can spawn. The count assertion below
    is what makes that failure loud.
    """
    from aiteam.services.agent_template_registry import PLUGIN_AGENTS_DIR, parse_template

    files = sorted(PLUGIN_AGENTS_DIR.glob("*.md"))
    assert files, f"no agent templates found under {PLUGIN_AGENTS_DIR}"
    parsed = [(f, parse_template(f)) for f in files]
    unparsed = [f.name for f, meta in parsed if not meta]
    assert not unparsed, f"templates that failed to parse (catalogue would drop them): {unparsed}"
    names = {str(meta["name"]) for _f, meta in parsed if meta}
    assert len(names) == len(files), (
        f"parsed {len(names)} distinct template names from {len(files)} *.md files - "
        "a name collision or a parse fallback is hiding a template"
    )
    return names


def spawnable_subagent_types() -> set[str]:
    """What a subagent_type hardcoded *in this repo* is allowed to be.

    Shipped templates plus the single sanctioned fallback constant. CC's own built-in
    types are NOT enumerated here: the list is unknowable and moves with every CC
    release, which is exactly why the runtime check only warns. This set governs
    static assertions about repo-hardcoded names, nothing else.
    """
    from aiteam.services.agent_template_registry import FALLBACK_SUBAGENT_TYPE

    return shipped_template_names() | {FALLBACK_SUBAGENT_TYPE}
