"""Task-Agent intelligent matching engine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aiteam.storage.repository import StorageRepository

# Agent templates directories — mirrors prompt_registry lookup order
_AGENTS_DIR = Path.home() / ".claude" / "agents"
_PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "plugin" / "agents"
)


def _list_all_template_names() -> list[str]:
    """Return sorted list of all available template name stems."""
    names: set[str] = set()
    for base in (_AGENTS_DIR, _PLUGIN_DIR):
        if base.exists():
            for f in base.glob("*.md"):
                if re.match(r"^[\w\-]+$", f.stem):
                    names.add(f.stem)
    return sorted(names)


def _match_template(role: str, template_names: list[str]) -> str:
    """Match a role string to the best-matching template name."""
    role_lower = role.lower()
    best: str = ""
    best_score = 0
    for tname in template_names:
        words = re.split(r"[\-_\s]+", tname.lower())
        score = sum(1 for w in words if w and w in role_lower)
        if score > best_score:
            best_score = score
            best = tname
    return best if best_score > 0 else ""


async def _get_template_completion_rates(
    repo: StorageRepository,
    template_names: list[str],
) -> dict[str, float]:
    """Compute completion rate per template from AgentActivity records.

    Returns a dict mapping template_name -> completion_rate (0.0–1.0).
    Templates with no activity records get 0.0.
    """
    teams = await repo.list_teams()
    # Map agent_id -> role across all teams
    agent_role_map: dict[str, str] = {}
    activities: list[Any] = []
    for team in teams:
        agents = await repo.list_agents(team.id)
        for ag in agents:
            agent_role_map[ag.id] = ag.role or ""
        team_activities = await repo.list_activities_by_team(team.id, limit=2000)
        activities.extend(team_activities)

    # Aggregate success/total counts per template
    counts: dict[str, dict[str, int]] = {}
    for act in activities:
        role = agent_role_map.get(act.agent_id, "")
        matched = _match_template(role, template_names)
        if not matched:
            continue
        if matched not in counts:
            counts[matched] = {"total": 0, "success": 0}
        counts[matched]["total"] += 1
        if act.status == "completed":
            counts[matched]["success"] += 1

    rates: dict[str, float] = {}
    for tname in template_names:
        c = counts.get(tname)
        if c and c["total"] > 0:
            rates[tname] = c["success"] / c["total"]
        else:
            rates[tname] = 0.0
    return rates


class TaskMatcher:
    def __init__(self, repo: StorageRepository):
        self._repo = repo

    async def find_matches(self, team_id: str) -> list[dict]:
        """Find matching suggestions between pending unassigned tasks and idle agents.

        Matching score = keyword_score * (1 + completion_rate_bonus).
        Agents whose template has a high historical completion rate are preferred.
        """
        agents = await self._repo.list_agents(team_id)
        idle_agents = [
            a for a in agents if a.status in ("waiting", "offline") and a.role != "leader"
        ]

        tasks = await self._repo.list_tasks(team_id)
        pending = [t for t in tasks if t.status in ("pending",) and not t.assigned_to]

        # Build completion rate map (gracefully ignore errors)
        template_names = _list_all_template_names()
        try:
            completion_rates = await _get_template_completion_rates(self._repo, template_names)
        except Exception:
            completion_rates = {}

        matches = []
        for task in pending:
            task_tags = set(t.lower() for t in (task.tags or []))
            best_agent = None
            best_score: float = 0.0
            for agent in idle_agents:
                role = (agent.role or agent.name or "").lower()
                # Base keyword match score
                keyword_score = sum(1 for tag in task_tags if tag in role or role in tag)
                if keyword_score == 0:
                    continue
                # Completion rate bonus: template with 80% rate adds 0.8 bonus
                matched_template = _match_template(role, template_names)
                rate = completion_rates.get(matched_template, 0.0)
                # Trust score bonus: trust=1.0 adds 0.5, trust=0.0 subtracts 0.5
                trust_bonus = (agent.trust_score - 0.5) * 1.0
                weighted_score = keyword_score * (1.0 + rate) + trust_bonus
                if weighted_score > best_score:
                    best_score = weighted_score
                    best_agent = agent
            if best_agent:
                matches.append(
                    {
                        "task_id": task.id,
                        "task_title": task.title,
                        "agent_id": best_agent.id,
                        "agent_name": best_agent.name,
                        "match_score": round(best_score, 3),
                    }
                )
        return matches
