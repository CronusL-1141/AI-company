"""What-If analyzer — multi-approach generation and comparison for task planning."""
from __future__ import annotations

from datetime import datetime

from aiteam.storage.repository import StorageRepository


class WhatIfAnalyzer:
    def __init__(self, repo: StorageRepository):
        self._repo = repo

    async def analyze_task(self, task_id: str, team_id: str) -> dict:
        """Generate multi-approach analysis for a task."""
        task = await self._repo.get_task(task_id)
        if not task:
            return {"error": "task not found"}

        # Get available agents for the team
        agents = await self._repo.list_agents(team_id)
        idle_agents = [a for a in agents if a.status in ("waiting", "offline") and a.role != "leader"]

        # Get team historical knowledge (failure alchemy outputs)
        knowledge = await self._repo.search_memories("team", team_id, task.title[:20])

        # Generate approaches
        approaches = []

        # Approach A: Assign by best match
        best_match = self._find_best_agent(task, idle_agents)
        approaches.append({
            "name": "方案A：最佳匹配",
            "description": f"分配给 {best_match.name if best_match else '待定'}（角色匹配度最高）",
            "agent": best_match.name if best_match else None,
            "estimated_risk": "低",
            "rationale": "按角色匹配度分配，风险最小",
        })

        # Approach B: Parallel split
        if len(idle_agents) >= 2:
            approaches.append({
                "name": "方案B：并行拆分",
                "description": f"拆分为{min(len(idle_agents), 3)}个子任务并行执行",
                "agents": [a.name for a in idle_agents[:3]],
                "estimated_risk": "中",
                "rationale": "速度更快，但需要协调成本",
            })

        # Approach C: Experience-driven
        if knowledge:
            approaches.append({
                "name": "方案C：经验驱动",
                "description": "基于团队历史经验调整策略",
                "knowledge_refs": [k.content[:100] for k in knowledge[:2]],
                "estimated_risk": "低",
                "rationale": f"参考{len(knowledge)}条历史经验",
            })

        result = {
            "task_id": task.id,
            "task_title": task.title,
            "approaches": approaches,
            "recommendation": approaches[0]["name"] if approaches else "无可用方案",
            "analyzed_at": datetime.now().isoformat(),
        }

        # Save analysis results to memory
        await self._repo.create_memory(
            scope="team", scope_id=team_id,
            content=f"What-If分析: {task.title}\n方案数: {len(approaches)}\n推荐: {result['recommendation']}",
            metadata={"type": "what_if_analysis", "task_id": task.id, "approaches": len(approaches)},
        )

        return result

    def _find_best_agent(self, task, agents):
        """Find the best-matching agent for a task."""
        if not agents:
            return None
        task_tags = set(t.lower() for t in (task.tags or []))
        best = None
        best_score = -1
        for agent in agents:
            role = (agent.role or agent.name or "").lower()
            score = sum(1 for tag in task_tags if tag in role or role in tag)
            if score > best_score:
                best_score = score
                best = agent
        return best or agents[0]
