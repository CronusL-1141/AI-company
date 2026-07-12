"""记忆系统 v2 P0 — task_memos 升表单元测试.

覆盖：repository memo CRUD、supersedes 置换失效、config→表回填幂等、
config['memo'] 视图 hydration、unified_search 过滤失效条目。
（pyproject asyncio_mode=auto，async 测试无需装饰器。）
"""

from __future__ import annotations

from aiteam.api.unified_search import unified_search
from aiteam.storage.repository import StorageRepository


async def test_add_and_list_task_memos(db_repository: StorageRepository) -> None:
    """add_task_memo 写表，list_task_memos 按时序返回有效条目."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "任务A")

    m1 = await db_repository.add_task_memo(task.id, "第一条", memo_type="progress")
    m2 = await db_repository.add_task_memo(
        task.id, "第二条", memo_type="summary", author="engineer-1"
    )

    memos = await db_repository.list_task_memos(task.id)
    assert [m.content for m in memos] == ["第一条", "第二条"]
    assert memos[0].id == m1.id
    assert memos[1].id == m2.id
    assert memos[1].memo_type == "summary"
    assert memos[1].author == "engineer-1"
    assert all(m.invalid_at is None for m in memos)


async def test_get_task_memo(db_repository: StorageRepository) -> None:
    """get_task_memo 按真 id 取单条."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "任务B")
    m = await db_repository.add_task_memo(task.id, "内容X")

    fetched = await db_repository.get_task_memo(m.id)
    assert fetched is not None
    assert fetched.content == "内容X"
    assert await db_repository.get_task_memo("nonexistent") is None


async def test_supersedes_invalidation(db_repository: StorageRepository) -> None:
    """supersedes 置旧条 invalid_at + invalidated_by；默认列表排除失效条."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "任务C")

    old = await db_repository.add_task_memo(task.id, "旧结论", memo_type="decision")
    new = await db_repository.add_task_memo(
        task.id, "新结论", memo_type="decision", supersedes=old.id
    )

    valid = await db_repository.list_task_memos(task.id)
    assert [m.id for m in valid] == [new.id]

    all_memos = await db_repository.list_task_memos(task.id, include_invalidated=True)
    assert len(all_memos) == 2
    old_row = next(m for m in all_memos if m.id == old.id)
    assert old_row.invalid_at is not None
    assert old_row.invalidated_by == new.id


async def test_hydrate_config_memo_view(db_repository: StorageRepository) -> None:
    """get_task 把有效 memo 拼回 config['memo'] 视图（旧读侧零改动兼容）."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "任务D")
    await db_repository.add_task_memo(task.id, "进展一")

    fetched = await db_repository.get_task(task.id)
    assert fetched is not None
    view = fetched.config.get("memo", [])
    assert len(view) == 1
    assert view[0]["content"] == "进展一"
    assert "id" in view[0]
    assert "timestamp" in view[0]


async def test_backfill_idempotent(db_repository: StorageRepository) -> None:
    """config.memo → 表回填幂等：跑两次不重复导入."""
    team = await db_repository.create_team("memo-team", "coordinate")
    legacy_config = {
        "memo": [
            {
                "timestamp": "2026-07-01T10:00:00",
                "author": "leader",
                "content": "历史memo1",
                "type": "progress",
            },
            {
                "timestamp": "2026-07-01T11:00:00",
                "author": "engineer-2",
                "content": "历史memo2",
                "type": "summary",
            },
        ]
    }
    task = await db_repository.create_task(team.id, "老任务", config=legacy_config)

    first = await db_repository.backfill_task_memos_from_config()
    assert first == 2

    second = await db_repository.backfill_task_memos_from_config()
    assert second == 0

    memos = await db_repository.list_task_memos(task.id)
    assert len(memos) == 2
    assert [m.content for m in memos] == ["历史memo1", "历史memo2"]
    assert memos[1].memo_type == "summary"
    assert memos[1].author == "engineer-2"


async def test_unified_search_filters_invalidated(
    db_repository: StorageRepository,
) -> None:
    """unified_search 直查表，失效 memo 不出现在结果中，取代者可被检索到."""
    project = await db_repository.create_project("proj-memo")
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(
        team.id, "检索任务", project_id=project.id
    )

    old = await db_repository.add_task_memo(
        task.id, "旧的独特词 zebra 结论", memo_type="decision"
    )
    new = await db_repository.add_task_memo(
        task.id, "新的独特词 zebra 结论", memo_type="decision", supersedes=old.id
    )

    results = await unified_search(db_repository, "zebra", project_id=project.id)
    memo_ids = {r["id"] for r in results if r["kind"] == "task_memo"}
    assert new.id in memo_ids
    assert old.id not in memo_ids
