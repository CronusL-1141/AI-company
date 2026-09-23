"""失败炼金与失败诊断的读写层归属。

三处实锤：

* process_failure 曾直接往方向层写一条 project 级记忆（kind 默认 preference），
  绕过记忆 API 的单条字数上限、桶配额与安全扫描；方向层会注入该项目的每个会话。
  教训现在作为 issue memo 追加到失败任务上，进情景层。
* diagnose_failure 读 ``task.config["memo"]``：get_task 会用 task_memos 表覆盖这个
  视图，但任务在表里没有有效 memo 时回退到冻结档案（真库有这样的任务），失效的旧
  memo 于是又被当成失败点。现在直接读表。
* prompt_effectiveness 的 failure_lesson_count 只数带 template_name 的教训，而没有
  任何调用方传过 template_name，恒为 0。现在按失败任务执行者的角色归到模板。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.routes import prompt_registry
from aiteam.loop.failure_alchemy import FailureAlchemist
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _failed_task(repo: StorageRepository, **task_kwargs):
    project = await repo.create_project(name="p", root_path="/tmp/failure-alchemy-proj")
    team = await repo.create_team(name="t", mode="coordinate", project_id=project.id)
    task = await repo.create_task(
        team.id, title="部署脚本失败", description="d", project_id=project.id, **task_kwargs
    )
    await repo.update_task(task.id, status="failed", result="connection refused")
    return project, team, task


async def _direction_rows(repo: StorageRepository, project_id: str, team_id: str) -> list:
    return (
        await repo.list_memories_by_metadata_type("failure_alchemy")
        + await repo.list_memories("project", project_id)
        + await repo.list_memories("team", team_id)
    )


# ---------------------------------------------------------------------------
# process_failure：教训进情景层，不碰方向层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_process_failure_writes_issue_memo_not_direction_memory(repo):
    project, team, task = await _failed_task(repo)

    result = await FailureAlchemist(repo).process_failure(task.id, team.id)

    assert await _direction_rows(repo, project.id, team.id) == []
    memos = await repo.list_task_memos(task.id)
    assert len(memos) == 1
    memo = memos[0]
    assert memo.id == result["memo_id"]
    assert memo.author == "failure_alchemist"
    assert memo.memo_type == "issue"
    assert memo.project_id == project.id
    assert memo.meta["type"] == "failure_alchemy"
    assert memo.meta["team_id"] == team.id
    assert "抗体:" in memo.content and "connection refused" in memo.content


@pytest.mark.asyncio()
async def test_process_failure_records_template_name(repo):
    _, team, task = await _failed_task(repo)
    await FailureAlchemist(repo).process_failure(
        task.id, team.id, template_name="engineering-backend-architect"
    )
    memo = (await repo.list_task_memos(task.id))[0]
    assert memo.meta["template_name"] == "engineering-backend-architect"


@pytest.mark.asyncio()
async def test_invisible_characters_skip_the_memo(repo):
    project, team, task = await _failed_task(repo)
    await repo.update_task(task.id, result="refused​")

    result = await FailureAlchemist(repo).process_failure(task.id, team.id)

    assert result["memo_id"] == ""
    assert result["memo_skipped"]
    assert await repo.list_task_memos(task.id) == []
    assert await _direction_rows(repo, project.id, team.id) == []


# ---------------------------------------------------------------------------
# 端到端：MCP failure_analysis 走的那条 HTTP 路由
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_and_repo():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    from aiteam.api.app import create_app
    from aiteam.api.event_bus import EventBus
    from aiteam.memory.store import MemoryStore
    from aiteam.orchestrator.team_manager import TeamManager

    memory = MemoryStore(repository=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = EventBus(repo=repo)
    deps._manager = TeamManager(repository=repo, memory=memory)
    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    yield TestClient(app), repo

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None


def test_failure_analysis_route_lands_in_task_memos(client_and_repo):
    client, repo = client_and_repo
    loop = asyncio.get_event_loop()
    project, team, task = loop.run_until_complete(_failed_task(repo))

    resp = client.post(f"/api/teams/{team.id}/failure-analysis", json={"task_id": task.id})
    assert resp.status_code == 200, resp.text

    memos = client.get(f"/api/tasks/{task.id}/memo").json()["data"]
    assert [(m["author"], m["type"]) for m in memos] == [("failure_alchemist", "issue")]
    assert loop.run_until_complete(_direction_rows(repo, project.id, team.id)) == []


# ---------------------------------------------------------------------------
# diagnose_failure：只读 task_memos 的有效条目
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_diagnose_ignores_frozen_config_memo(repo):
    """表里的 memo 全部失效时，不得回退到冻结的 config.memo。"""
    frozen = [{"timestamp": "2026-07-01T00:00:00", "author": "x", "content": "旧失败", "type": "issue"}]
    _, _, task = await _failed_task(repo, config={"memo": frozen})
    stale = await repo.add_task_memo(task.id, content="error: 已被纠正", memo_type="issue")
    await repo.invalidate_task_memo(stale.id)

    diagnosis = await FailureAlchemist(repo).diagnose_failure(task.id)

    assert diagnosis["failed_at"] == "未记录"
    assert not any("issue" in fix for fix in diagnosis["suggested_fixes"])


@pytest.mark.asyncio()
async def test_diagnose_reads_valid_issue_memo_from_table(repo):
    _, _, task = await _failed_task(repo)
    await repo.add_task_memo(task.id, content="进展正常", memo_type="progress")
    issue = await repo.add_task_memo(task.id, content="卡在依赖安装", memo_type="issue")

    diagnosis = await FailureAlchemist(repo).diagnose_failure(task.id)

    assert diagnosis["failed_at"] == issue.created_at.isoformat()
    assert any("1 条issue" in fix for fix in diagnosis["suggested_fixes"])


@pytest.mark.asyncio()
async def test_diagnose_skips_the_alchemy_lesson(repo):
    """事后写的教训 memo 既不是失败点，也不算待排查的 issue 记录。"""
    _, team, task = await _failed_task(repo)
    issue = await repo.add_task_memo(task.id, content="卡在依赖安装", memo_type="issue")
    await FailureAlchemist(repo).process_failure(task.id, team.id)

    diagnosis = await FailureAlchemist(repo).diagnose_failure(task.id)

    assert diagnosis["failed_at"] == issue.created_at.isoformat()
    assert any("1 条issue" in fix for fix in diagnosis["suggested_fixes"])


# ---------------------------------------------------------------------------
# failure_lesson_count：没有调用方传 template_name，按执行者角色归到模板
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_failure_lesson_count_attributes_by_assigned_role(repo, monkeypatch):
    monkeypatch.setattr(
        prompt_registry,
        "_list_all_template_names",
        lambda: ["engineering-backend-architect", "testing-qa-engineer"],
    )
    project = await repo.create_project(name="p", root_path="/tmp/failure-lesson-proj")
    team = await repo.create_team(name="t", mode="coordinate", project_id=project.id)
    agent = await repo.create_agent(team.id, name="api-worker", role="backend-architect")
    by_id = await repo.create_task(team.id, title="a", project_id=project.id, assigned_to=agent.id)
    by_name = await repo.create_task(
        team.id, title="b", project_id=project.id, assigned_to="qa-engineer-2"
    )
    for task in (by_id, by_name):
        await repo.update_task(task.id, status="failed", result="boom")
        await FailureAlchemist(repo).process_failure(task.id, team.id)

    result = await prompt_registry.prompt_effectiveness(template_name="", repo=repo)

    counts = {e["template_name"]: e["failure_lesson_count"] for e in result["effectiveness"]}
    assert counts == {"engineering-backend-architect": 1, "testing-qa-engineer": 1}
