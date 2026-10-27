"""AI Team OS — Health check endpoint."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Header, Request

import aiteam
from aiteam.api.language import resolve_language
from aiteam.api.release_updates import notice_language, release_checker
from aiteam.paths import own_cc_config_dir, resolve_data_dir
from aiteam.services.notices import install_kind
from aiteam.types import ReleaseUpdateStatus

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health_check(request: Request, task_count: bool = False) -> dict:
    """Status and version, plus where this process keeps its data.

    ``data_dir`` / ``data_dir_mode`` are what the API resolved at startup
    (aiteam.paths: env, canonical, legacy or conflict); ``cc_config_dir`` is the
    Claude Code config folder its own environment points at, the one it answers
    for when a request names no session folder (aiteam.api.cc_config_scope). A
    session start compares ``data_dir`` with its own resolution (data_dir_split).

    ``task_count=1`` adds ``task_count``, COUNT(*) over the default database's
    tasks: gate 7 of the data directory migration reconciles on it. It is opt-in
    so the liveness probe every starter polls never waits on the database.
    """
    resolution = getattr(request.app.state, "data_dir", None)
    if resolution is None:  # an app assembled without create_app (tests, tools)
        resolution = await asyncio.to_thread(resolve_data_dir)
    body: dict = {
        "status": "ok", "version": aiteam.__version__,
        "data_dir": str(resolution.path), "data_dir_mode": resolution.mode,
        "cc_config_dir": str(own_cc_config_dir()),
    }
    if task_count:
        from aiteam.api.deps import get_global_repository

        body["task_count"] = await get_global_repository().count_all_tasks()
    return body


@router.get("/api/releases/latest", response_model=ReleaseUpdateStatus)
async def latest_release(
    language: str | None = None, accept_language: str | None = Header(default=None),
    cwd: str | None = None, host: Literal["cc", "codex", "system"] = "system",
    fallback_language: str | None = None,
    installation: Literal["cc-plugin", "cc-source", "codex", "unknown"] = "cc-source",
) -> ReleaseUpdateStatus:
    """Check the public stable release without updating the installation.

    For ``host=cc`` the installation kind is detected here and the query value
    is ignored: hook copies guessed it from ``CLAUDE_PLUGIN_ROOT``, which the
    main-chain copy serving plugin users never has.
    """
    selected = await resolve_language(
        cwd=cwd, host=host,
        fallback_language=fallback_language or language or (
            notice_language(accept_language=accept_language) if accept_language else None
        ),
    )
    if host == "codex":
        installation = "codex"
    elif host == "cc":
        installation = (await install_kind.detect("cc")).kind
    return await release_checker.check(aiteam.__version__, selected["effective"], installation)
