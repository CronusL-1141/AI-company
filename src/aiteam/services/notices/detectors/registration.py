"""E07 unregistered_dir: the session's folder is not a registered project.

Same judgement the session-start briefing used (exact or longest-prefix
``root_path`` match, the user's dismiss list in ``dismissed_projects.json``),
moved API-side. The notice row is scoped to ``dir:<realpath>`` so it is only
offered to sessions started in that folder.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from aiteam.clock import utc_now
from aiteam.services.notices.detectors import DetectContext, Finding, Scope
from aiteam.services.notices.install_kind import os_data_dir


def dismissed_file() -> Path:
    return os_data_dir() / "dismissed_projects.json"


def real_dir(cwd: str) -> str:
    """Resolved folder path with forward slashes (blocking; use a thread)."""
    try:
        return str(Path(cwd).expanduser().resolve()).replace("\\", "/").rstrip("/") or "/"
    except (OSError, RuntimeError):
        return cwd.replace("\\", "/").rstrip("/") or "/"


def dir_scope(real: str) -> str:
    """Pseudo project id for notices bound to an unregistered folder."""
    return f"dir:{real}"


def match_project(real: str, projects: list) -> str:
    """Project id whose root_path equals or contains ``real`` (longest wins)."""
    target = real.lower()
    best, best_len = "", -1
    for project in projects:
        root = (getattr(project, "root_path", "") or "").replace("\\", "/").rstrip("/")
        if not root:
            continue
        lowered = root.lower()
        if (target == lowered or target.startswith(lowered + "/")) and len(lowered) > best_len:
            best, best_len = project.id, len(lowered)
    return best


def _normalize_roots(projects: list) -> list:
    class _P:
        __slots__ = ("id", "root_path")

        def __init__(self, pid: str, root: str) -> None:
            self.id, self.root_path = pid, root

    return [_P(p.id, real_dir(p.root_path)) for p in projects if getattr(p, "root_path", "")]


async def resolve_project_id(repo, cwd: str) -> tuple[str, str]:
    """(project_id or "", resolved folder) for a session folder."""
    if not cwd:
        return "", ""
    projects = await repo.list_projects()
    real, roots = await asyncio.to_thread(lambda: (real_dir(cwd), _normalize_roots(projects)))
    return match_project(real, roots), real


def _load_dismissed() -> list[str]:
    try:
        data = json.loads(dismissed_file().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    values = data.get("dismissed") if isinstance(data, dict) else None
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def is_dismissed_sync(real: str) -> bool:
    return real.lower() in _load_dismissed()


def dismiss_dir_sync(cwd: str) -> dict:
    """Add a folder to the dismiss list (same file and format as before)."""
    normalized = real_dir(cwd).lower()
    path = dismissed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    dismissed = _load_dismissed()
    if normalized not in dismissed:
        dismissed.append(normalized)
    data = {"dismissed": dismissed, "updated_at": utc_now().isoformat()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"success": True, "dismissed_count": len(dismissed), "cwd": normalized}


async def dismiss_dir(cwd: str) -> dict:
    return await asyncio.to_thread(dismiss_dir_sync, cwd)


class RegistrationDetector:
    """Offer registration once per new session in an unregistered folder."""

    name = "registration"
    catalog_ids = ("unregistered_dir",)
    timing = frozenset({"session_start"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return bool(ctx.cwd)

    def scope(self, ctx: DetectContext) -> Scope:
        real = str(ctx.facts.get("real_dir") or ctx.cwd)
        return Scope(keys=(f"unregistered_dir:{real}",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        real = str(ctx.facts.get("real_dir") or "") or await asyncio.to_thread(real_dir, ctx.cwd)
        if ctx.project_id:
            return []
        project_id, _ = await resolve_project_id(ctx.repo, ctx.cwd)
        if project_id or await asyncio.to_thread(is_dismissed_sync, real):
            return []
        return [Finding(
            catalog_id="unregistered_dir",
            key=f"unregistered_dir:{real}",
            project_id=dir_scope(real),
        )]
