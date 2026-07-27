"""批 3 ⑥：delete_project 级联漏了 scope='team' 的记忆（125 条孤儿记忆的直接成因）。

团队知识（failure_alchemy / lesson_learned / loop_review）以 scope='team' +
scope_id=<team_id> 落库。删项目时 teams 行被删掉，这些记忆却没人管，从此
scope_id 指向一个不存在的团队 —— 既查不出来也删不掉。

顺带锁住另外两条同类的"子查询在父行删除后失效"隐患：agents 与 events 都按
team_ids 子查询清理，而 team_ids 必须在 teams 行删除**之前**取实。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aiteam.storage.models import AgentModel, EventModel, MemoryModel


async def _count(repo, model, **where) -> int:
    from aiteam.storage.connection import get_session

    async with get_session(repo._db_url) as session:
        stmt = select(func.count()).select_from(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return (await session.execute(stmt)).scalar_one()


@pytest.mark.asyncio
async def test_team_scoped_memories_are_cascaded(db_repository):
    project = await db_repository.create_project(name="doomed", root_path="/tmp/doomed")
    keep = await db_repository.create_project(name="keeper", root_path="/tmp/keeper")

    team = await db_repository.create_team(name="doomed-team", mode="coordinate", project_id=project.id)
    other_team = await db_repository.create_team(name="keeper-team", mode="coordinate", project_id=keep.id)

    await db_repository.create_memory(scope="team", scope_id=team.id, content="团队知识")
    await db_repository.create_memory(scope="team", scope_id=other_team.id, content="别项目的")
    await db_repository.create_memory(scope="project", scope_id=project.id, content="项目记忆")
    await db_repository.create_memory(scope="global", scope_id="global", content="全局的")

    assert await db_repository.delete_project(project.id) is True

    assert await _count(db_repository, MemoryModel, scope="team", scope_id=team.id) == 0
    # 只清本项目的：别项目的团队记忆 + 全局记忆必须原封不动
    assert await _count(db_repository, MemoryModel, scope="team", scope_id=other_team.id) == 1
    assert await _count(db_repository, MemoryModel, scope="global") == 1
    assert await _count(db_repository, MemoryModel, scope="project", scope_id=project.id) == 0


@pytest.mark.asyncio
async def test_team_agents_and_events_are_cascaded(db_repository):
    """team_ids 子查询必须在 teams 行删除前取实，否则按它清理的表全都清了个寂寞。"""
    project = await db_repository.create_project(name="doomed2", root_path="/tmp/doomed2")
    team = await db_repository.create_team(name="doomed-team-2", mode="coordinate", project_id=project.id)
    await db_repository.create_agent(team_id=team.id, name="worker", role="dev")
    await db_repository.create_event(
        event_type="team.created", source=f"team:{team.id}", data={}, entity_id=team.id
    )

    assert await _count(db_repository, AgentModel, team_id=team.id) == 1
    assert await _count(db_repository, EventModel, entity_id=team.id) == 1

    assert await db_repository.delete_project(project.id) is True

    assert await _count(db_repository, AgentModel, team_id=team.id) == 0
    assert await _count(db_repository, EventModel, entity_id=team.id) == 0
