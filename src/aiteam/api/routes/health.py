"""AI Team OS — Health check endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header

import aiteam
from aiteam.api.language import resolve_language
from aiteam.api.release_updates import notice_language, release_checker
from aiteam.services.notices import install_kind
from aiteam.types import ReleaseUpdateStatus

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health_check() -> dict:
    """Simple health check — returns status and version."""
    return {"status": "ok", "version": aiteam.__version__}


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
