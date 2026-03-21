"""Task-Agent intelligent matching engine."""
from __future__ import annotations

from aiteam.storage.repository import StorageRepository


class TaskMatcher:
    def __init__(self, repo: StorageRepository):
        self._repo = repo

    async def find_matches(self, team_id: str) -> list[dict]:
        """Find matching suggestions between pending unassigned tasks and idle agents."""
        agents = await self._repo.list_agents(team_id)
        idle_agents = [a for a in agents if a.status in ("waiting", "offline") and a.role != "leader"]

        tasks = await self._repo.list_tasks(team_id)
        pending = [t for t in tasks if t.status in ("pending",) and not t.assigned_to]

        matches = []
        for task in pending:
            task_tags = set(t.lower() for t in (task.tags or []))
            best_agent = None
            best_score = 0
            for agent in idle_agents:
                role = (agent.role or agent.name or "").lower()
                # Match: intersection of agent role and task tags
                score = sum(1 for tag in task_tags if tag in role or role in tag)
                if score > best_score:
                    best_score = score
                    best_agent = agent
            if best_agent:
                matches.append({
                    "task_id": task.id,
                    "task_title": task.title,
                    "agent_id": best_agent.id,
                    "agent_name": best_agent.name,
                    "match_score": best_score,
                })
        return matches
