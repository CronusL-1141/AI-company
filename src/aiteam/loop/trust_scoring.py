"""Agent trust scoring — updates and queries agent trust scores.

Trust score range: 0.0–1.0, default 0.5.
  task success  : +0.05 (cap at 1.0)
  task failure  : -0.10 (floor at 0.0)
  task timeout  : -0.05 (floor at 0.0)
"""

from __future__ import annotations

from typing import Literal

from aiteam.storage.repository import StorageRepository


async def update_trust_score(
    repo: StorageRepository,
    agent_id: str,
    task_result: Literal["success", "failure", "timeout"],
) -> float:
    """Adjust trust_score for agent_id based on task_result.

    Returns the updated trust_score, or -1.0 if agent not found.
    """
    agent = await repo.get_agent(agent_id)
    if agent is None:
        return -1.0

    delta = {"success": 0.05, "failure": -0.10, "timeout": -0.05}.get(task_result, 0.0)
    new_score = max(0.0, min(1.0, agent.trust_score + delta))

    await repo.update_agent(agent_id, trust_score=new_score)
    return new_score


async def get_agent_trust_scores(repo: StorageRepository) -> list[dict]:
    """Return all agents sorted by trust_score descending.

    Each entry: {agent_id, agent_name, team_id, trust_score}
    """
    teams = await repo.list_teams()
    records: list[dict] = []
    for team in teams:
        agents = await repo.list_agents(team.id)
        for agent in agents:
            records.append(
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "team_id": team.id,
                    "team_name": team.name,
                    "trust_score": agent.trust_score,
                }
            )
    records.sort(key=lambda r: r["trust_score"], reverse=True)
    return records
