"""AI Team OS — Fleet dispatch route (fleet-layer design §4, P3 down-channel).

The fleet down-channel drives an existing ship (a CC main session) to run one turn via
headless ``claude -p --resume <session_id>``. This route is the safety gate + the entry
into the shared wake machine (WakeAgentManager); the actual subprocess spawn, concurrency
control, circuit breaker and ledger all live in wake_manager.

Safety (design §4.3), enforced here BEFORE any subprocess is spawned:
- Only a *resumable* ship may be targeted (its transcript file exists).
- Only a ship that is *not user-live* may be targeted: its file mtime must be older than
  FLEET_DISPATCH_MIN_IDLE_SECONDS (deliberately more conservative than the 15min live
  window) so a dispatch never competes with a user typing in that session.
- The instruction is operational-only (constrained by the wake_manager preamble); tool
  permissions never exceed the requested preset; every dispatch is ledgered.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from aiteam.api import session_probe
from aiteam.api.deps import get_reaper, get_repository
from aiteam.config import settings
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


class FleetDispatchRequest(BaseModel):
    """Body for POST /api/fleet/dispatch."""

    target_session_id: str
    instruction: str
    project_id: str = ""
    tools_level: str = "safe"
    max_turns: int | None = None


def evaluate_dispatch_target(
    root_path: str,
    session_id: str,
    min_idle_seconds: int,
    now: datetime | None = None,
) -> dict:
    """Reachability gate for a fleet dispatch (pure, unit-testable).

    Reads the ship's transcript file mtime (file truth source) and decides whether it may
    be dispatched to. Never spawns anything.

    Returns a dict:
        {"allowed": bool, "availability": <idle_resumable|live|expired>,
         "reason": str, "last_active_at": iso|None, "idle_seconds": int|None}
    """
    _now = now or datetime.now()
    last_active = session_probe.session_last_active(root_path, session_id)
    if last_active is None:
        return {
            "allowed": False,
            "availability": "expired",
            "reason": "session transcript not found (expired or never existed); not resumable",
            "last_active_at": None,
            "idle_seconds": None,
        }
    idle_seconds = int((_now - last_active).total_seconds())
    if idle_seconds < min_idle_seconds:
        return {
            "allowed": False,
            "availability": "live",
            "reason": (
                f"session active {idle_seconds}s ago (< {min_idle_seconds}s guard); "
                "may be user-live, refusing to compete with an interactive turn"
            ),
            "last_active_at": last_active.isoformat(),
            "idle_seconds": idle_seconds,
        }
    return {
        "allowed": True,
        "availability": "idle_resumable",
        "reason": "idle and resumable",
        "last_active_at": last_active.isoformat(),
        "idle_seconds": idle_seconds,
    }


async def _resolve_session_root(
    repo: StorageRepository, session_id: str, project_id: str
) -> tuple[str, str]:
    """Resolve (project_id, root_path) for a target session.

    Prefer an explicit project_id; otherwise infer it from an agent belonging to the
    session (its project_id). Returns ("", "") when unresolvable.
    """
    pid = project_id
    if not pid:
        try:
            agents = await repo.find_agents_by_session(session_id)
        except Exception:  # noqa: BLE001
            agents = []
        for a in agents:
            if getattr(a, "project_id", None):
                pid = a.project_id
                break
    if not pid:
        return "", ""
    proj = await repo.get_project(pid)
    return (pid, (proj.root_path or "") if proj else "")


@router.post("/dispatch")
async def fleet_dispatch(
    body: FleetDispatchRequest,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Dispatch an operational instruction to an existing idle ship via headless resume.

    Returns {"success": bool, "status": ..., ...}. Never 500s on a refusal — a blocked
    dispatch is a normal, informative result (allowed=False with a reason).
    """
    session_id = (body.target_session_id or "").strip()
    if not session_id:
        return {"success": False, "status": "error_config", "reason": "missing target_session_id"}
    if not (body.instruction or "").strip():
        return {"success": False, "status": "error_config", "reason": "empty instruction"}

    project_id, root_path = await _resolve_session_root(repo, session_id, body.project_id)
    if not root_path:
        return {
            "success": False,
            "status": "unresolved_project",
            "reason": "cannot resolve the project/root_path for this session; pass project_id",
        }

    gate = evaluate_dispatch_target(
        root_path, session_id, settings.FLEET_DISPATCH_MIN_IDLE_SECONDS
    )
    if not gate["allowed"]:
        return {"success": False, "status": "refused", **gate}

    reaper = get_reaper()
    if reaper is None:
        return {
            "success": False,
            "status": "unavailable",
            "reason": "fleet dispatch machine not ready (reaper not started)",
        }

    result = await reaper.wake_manager.dispatch_to_session(
        target_session_id=session_id,
        instruction=body.instruction,
        cwd=root_path,
        max_turns=body.max_turns,
        tools_level=body.tools_level or "safe",
    )
    started = result.get("status") == "started"
    return {"success": started, **result, "availability": gate["availability"]}
