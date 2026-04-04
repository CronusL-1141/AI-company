"""Prompt Registry routes — Agent template version tracking and effectiveness statistics.

Uses MemoryModel with category="prompt_version" in metadata as source of truth.
No new database tables are created.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/prompt-registry", tags=["prompt-registry"])

# Agent templates directory (user-level)
_AGENTS_DIR = Path.home() / ".claude" / "agents"
# Plugin agents directory (project-level)
_PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "plugin" / "agents"
)

_SCOPE = "global"
_SCOPE_ID = "prompt_registry"
_CATEGORY = "prompt_version"


def _compute_hash(content: str) -> str:
    """Return first 12 chars of SHA-256 hex digest for template content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _find_template_path(template_name: str) -> Path | None:
    """Locate a template .md file by name (without extension).

    Searches user-level agents dir first, then project plugin agents dir.
    """
    for base in (_AGENTS_DIR, _PLUGIN_DIR):
        if not base.exists():
            continue
        candidate = base / f"{template_name}.md"
        if candidate.exists():
            return candidate
    return None


def _read_template_content(template_name: str) -> str | None:
    """Read raw content of a template file, return None if not found."""
    path = _find_template_path(template_name)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _list_all_template_names() -> list[str]:
    """Return sorted list of all available template names (stems)."""
    names: set[str] = set()
    for base in (_AGENTS_DIR, _PLUGIN_DIR):
        if base.exists():
            for f in base.glob("*.md"):
                # Validate name is safe (letters, digits, hyphens, underscores)
                if re.match(r"^[\w\-]+$", f.stem):
                    names.add(f.stem)
    return sorted(names)


@router.post("/track")
async def track_template_usage(
    template_name: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Record usage of an Agent template — compute content hash and upsert version record.

    Call this whenever an Agent based on a template starts a task.
    Uses MemoryModel with metadata.category='prompt_version' for storage.

    Args:
        template_name: Template filename stem, e.g. "engineering-backend-architect"

    Returns:
        Version record info including content_hash and whether this is a new version.
    """
    if not re.match(r"^[\w\-]+$", template_name):
        return {"success": False, "error": "Invalid template_name format"}

    content = _read_template_content(template_name)
    if content is None:
        return {"success": False, "error": f"Template '{template_name}' not found"}

    content_hash = _compute_hash(content)

    # Check if this version is already tracked
    existing = await repo.list_memories(_SCOPE, _SCOPE_ID)
    for mem in existing:
        meta = mem.metadata or {}
        if (
            meta.get("category") == _CATEGORY
            and meta.get("template_name") == template_name
            and meta.get("content_hash") == content_hash
        ):
            # Increment usage_count on existing record — delete old then create new
            updated_meta = dict(meta)
            updated_meta["usage_count"] = meta.get("usage_count", 0) + 1
            await repo.delete_memory(mem.id)
            await repo.create_memory(
                scope=_SCOPE,
                scope_id=_SCOPE_ID,
                content=f"[prompt_version] {template_name}@{content_hash}",
                metadata=updated_meta,
            )
            return {
                "success": True,
                "is_new_version": False,
                "template_name": template_name,
                "content_hash": content_hash,
                "usage_count": updated_meta["usage_count"],
            }

    # New version — create record
    from datetime import datetime

    new_meta = {
        "category": _CATEGORY,
        "template_name": template_name,
        "content_hash": content_hash,
        "first_used_at": datetime.now().isoformat(),
        "usage_count": 1,
    }
    await repo.create_memory(
        scope=_SCOPE,
        scope_id=_SCOPE_ID,
        content=f"[prompt_version] {template_name}@{content_hash}",
        metadata=new_meta,
    )
    return {
        "success": True,
        "is_new_version": True,
        "template_name": template_name,
        "content_hash": content_hash,
        "usage_count": 1,
    }


@router.get("/versions")
async def list_prompt_versions(
    template_name: str = Query("", description="Filter by template name; empty = all"),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """List tracked template versions.

    Args:
        template_name: Optional filter. Empty returns all tracked templates.

    Returns:
        List of version records with hash, usage count, and first_used_at.
    """
    all_memories = await repo.list_memories(_SCOPE, _SCOPE_ID)

    versions: list[dict[str, Any]] = []
    for mem in all_memories:
        meta = mem.metadata or {}
        if meta.get("category") != _CATEGORY:
            continue
        if template_name and meta.get("template_name") != template_name:
            continue
        versions.append(
            {
                "memory_id": mem.id,
                "template_name": meta.get("template_name", ""),
                "content_hash": meta.get("content_hash", ""),
                "first_used_at": meta.get("first_used_at", ""),
                "usage_count": meta.get("usage_count", 0),
                "recorded_at": mem.created_at.isoformat() if mem.created_at else "",
            }
        )

    # Group by template_name, collapse multiple entries (sum usage_count)
    grouped: dict[str, dict[str, Any]] = {}
    for v in versions:
        name = v["template_name"]
        if name not in grouped:
            grouped[name] = {
                "template_name": name,
                "versions": [],
                "total_usage": 0,
            }
        grouped[name]["versions"].append(
            {
                "content_hash": v["content_hash"],
                "first_used_at": v["first_used_at"],
                "usage_count": v["usage_count"],
            }
        )
        grouped[name]["total_usage"] += v["usage_count"]

    result = sorted(grouped.values(), key=lambda x: x["total_usage"], reverse=True)
    return {"success": True, "templates": result, "total": len(result)}


@router.get("/effectiveness")
async def prompt_effectiveness(
    template_name: str = Query("", description="Filter by template; empty = all templates"),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Return effectiveness statistics for Agent templates.

    Aggregates AgentActivity records grouped by agent role (template_name),
    computing success rate, average duration, and failure reasons.

    The agent's role field is matched against template names to link activities.
    Additionally, failure alchemy memories with template_name metadata are included
    to show which templates have the most associated failure lessons.

    Args:
        template_name: Optional filter by template name stem.

    Returns:
        List of effectiveness records per template.
    """
    # Query all agent activities via per-team queries
    teams = await repo.list_teams()

    # Build agent_id -> role map from all teams
    agent_role_map: dict[str, str] = {}
    activities = []
    for team in teams:
        agents = await repo.list_agents(team.id)
        for ag in agents:
            agent_role_map[ag.id] = ag.role or ""
        team_activities = await repo.list_activities_by_team(team.id, limit=2000)
        activities.extend(team_activities)

    # Build template -> activities mapping using role text matching
    all_template_names = _list_all_template_names()

    def _match_template(role: str) -> str:
        """Match a role string to the best-matching template name."""
        role_lower = role.lower()
        best: str = ""
        best_score = 0
        for tname in all_template_names:
            # Score: count matching words
            words = re.split(r"[\-_\s]+", tname.lower())
            score = sum(1 for w in words if w and w in role_lower)
            if score > best_score:
                best_score = score
                best = tname
        return best if best_score > 0 else ""

    # Aggregate per template
    stats: dict[str, dict[str, Any]] = {}

    for act in activities:
        role = agent_role_map.get(act.agent_id, "")
        matched = _match_template(role)
        if not matched:
            continue
        if template_name and matched != template_name:
            continue

        if matched not in stats:
            stats[matched] = {
                "template_name": matched,
                "total_activities": 0,
                "success_count": 0,
                "failure_count": 0,
                "total_duration_ms": 0,
                "duration_samples": 0,
                "failure_reasons": [],
                "failure_lesson_count": 0,
            }

        s = stats[matched]
        s["total_activities"] += 1
        if act.status == "completed":
            s["success_count"] += 1
        elif act.status in ("failed", "error"):
            s["failure_count"] += 1
            if act.error:
                s["failure_reasons"].append(act.error[:100])
        if act.duration_ms is not None:
            s["total_duration_ms"] += act.duration_ms
            s["duration_samples"] += 1

    # Attach failure alchemy lesson counts from team memories
    all_team_memories = []
    for team in teams:
        mems = await repo.list_team_knowledge(team.id, memory_type="failure_alchemy", limit=500)
        all_team_memories.extend(mems)

    for mem in all_team_memories:
        meta = mem.metadata or {}
        tname = meta.get("template_name", "")
        if tname and tname in stats:
            stats[tname]["failure_lesson_count"] += 1
        elif tname and (not template_name or tname == template_name):
            # Template has failure lessons but no activity records yet
            if tname not in stats:
                stats[tname] = {
                    "template_name": tname,
                    "total_activities": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "total_duration_ms": 0,
                    "duration_samples": 0,
                    "failure_reasons": [],
                    "failure_lesson_count": 0,
                }
            stats[tname]["failure_lesson_count"] += 1

    # Build result list
    result_list: list[dict[str, Any]] = []
    for s in stats.values():
        total = s["total_activities"]
        success_rate = round(s["success_count"] / total * 100, 1) if total > 0 else None
        avg_duration = (
            round(s["total_duration_ms"] / s["duration_samples"])
            if s["duration_samples"] > 0
            else None
        )
        # Deduplicate and truncate failure reasons
        seen: set[str] = set()
        unique_reasons: list[str] = []
        for r in s["failure_reasons"]:
            if r not in seen:
                seen.add(r)
                unique_reasons.append(r)
            if len(unique_reasons) >= 5:
                break

        result_list.append(
            {
                "template_name": s["template_name"],
                "total_activities": total,
                "success_count": s["success_count"],
                "failure_count": s["failure_count"],
                "success_rate_pct": success_rate,
                "avg_duration_ms": avg_duration,
                "top_failure_reasons": unique_reasons,
                "failure_lesson_count": s["failure_lesson_count"],
            }
        )

    result_list.sort(key=lambda x: x["total_activities"], reverse=True)
    return {"success": True, "effectiveness": result_list, "total": len(result_list)}
