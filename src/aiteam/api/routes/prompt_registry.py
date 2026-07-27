"""Prompt Registry routes — Agent template effectiveness statistics.

Effectiveness is derived from AgentActivity rows plus failure-alchemy memories;
no new database tables are created. (Content-hash version tracking lived here too
until 2026-07-27 — retired unused, see the note above the surviving endpoint.)
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


# NOTE: POST /track and GET /versions retired 2026-07-27 (batch 8b) together with
# the `prompt_version_list` MCP tool. Version tracking only ever recorded a hash when
# something called /track — nothing ever did, so /versions was a permanently empty
# list dressed up as a feature. Effectiveness stats below stand on their own: they are
# computed from real AgentActivity rows and need no version records.

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
    # Two aggregate queries total (was 3 per team: agents + activities + knowledge —
    # 762 round-trips on a 254-team install, plus up to 2000 activity rows per team
    # dragged into Python purely to be counted).
    role_stats, error_samples = await repo.aggregate_activity_stats_by_role()

    all_template_names = _list_all_template_names()
    _match_cache: dict[str, str] = {}

    def _match_template(role: str) -> str:
        """Match a role string to the best-matching template name (memoised)."""
        if role in _match_cache:
            return _match_cache[role]
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
        matched = best if best_score > 0 else ""
        _match_cache[role] = matched
        return matched

    def _blank(name: str) -> dict[str, Any]:
        return {
            "template_name": name,
            "total_activities": 0,
            "success_count": 0,
            "failure_count": 0,
            "total_duration_ms": 0,
            "duration_samples": 0,
            "failure_reasons": [],
            "failure_lesson_count": 0,
        }

    stats: dict[str, dict[str, Any]] = {}
    for row in role_stats:
        matched = _match_template(row["role"])
        if not matched or (template_name and matched != template_name):
            continue
        s = stats.setdefault(matched, _blank(matched))
        s["total_activities"] += row["total"]
        s["success_count"] += row["success"]
        s["failure_count"] += row["failure"]
        s["total_duration_ms"] += row["total_duration_ms"]
        s["duration_samples"] += row["duration_samples"]

    for role, error in error_samples:
        matched = _match_template(role)
        if not matched or matched not in stats:
            continue
        reasons = stats[matched]["failure_reasons"]
        if len(reasons) < 50:
            reasons.append(error[:100])

    # Failure-alchemy lesson counts — one query for every scope at once.
    for mem in await repo.list_memories_by_metadata_type("failure_alchemy"):
        tname = (mem.metadata or {}).get("template_name", "")
        if not tname or (template_name and tname != template_name):
            continue
        stats.setdefault(tname, _blank(tname))["failure_lesson_count"] += 1

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
