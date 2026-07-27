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
