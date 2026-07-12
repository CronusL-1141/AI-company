"""记忆系统 v2 P1 — 方向层激活单元测试.

覆盖：repository create_memory(kind/source_refs/supersedes)、list_memories
valid-only + kind 过滤、invalidate_memory、list_direction_memories 组装 +
kind 优先级排序、count_valid_memories，以及双 hook 的方向记忆渲染函数。
（pyproject asyncio_mode=auto，async 测试无需装饰器。）
"""

from __future__ import annotations

from aiteam.storage.repository import StorageRepository


async def test_create_memory_with_kind_and_source_refs(
    db_repository: StorageRepository,
) -> None:
    """create_memory 落 kind/source_refs，to_pydantic 往返一致."""
    m = await db_repository.create_memory(
        scope="global",
        scope_id="system",
        content="所有输出使用中文",
        kind="constraint",
        source_refs=["report#abc", "task#123"],
    )
    assert m.kind == "constraint"
    assert m.source_refs == ["report#abc", "task#123"]
    assert m.invalid_at is None

    fetched = await db_repository.get_memory(m.id)
    assert fetched is not None
    assert fetched.kind == "constraint"
    assert fetched.source_refs == ["report#abc", "task#123"]


async def test_supersedes_invalidates_old(db_repository: StorageRepository) -> None:
    """supersedes 置旧条 invalid_at + invalidated_by；默认列表排除失效条."""
    old = await db_repository.create_memory(
        scope="global", scope_id="system", content="偏好旧版", kind="preference"
    )
    new = await db_repository.create_memory(
        scope="global",
        scope_id="system",
        content="偏好新版",
        kind="preference",
        supersedes=old.id,
    )

    valid = await db_repository.list_memories("global", "system")
    assert [m.id for m in valid] == [new.id]

    all_m = await db_repository.list_memories(
        "global", "system", include_invalidated=True
    )
    old_row = next(m for m in all_m if m.id == old.id)
    assert old_row.invalid_at is not None
    assert old_row.invalidated_by == new.id


async def test_invalidate_memory(db_repository: StorageRepository) -> None:
    """invalidate_memory 显式失效不删除，再查有效列表消失."""
    m = await db_repository.create_memory(
        scope="user", scope_id="user", content="过时偏好", kind="directive"
    )
    out = await db_repository.invalidate_memory(m.id, invalidated_by="manual")
    assert out is not None
    assert out.invalid_at is not None
    assert out.invalidated_by == "manual"

    valid = await db_repository.list_memories("user", "user")
    assert m.id not in [x.id for x in valid]
    # 不存在的 id 返回 None
    assert await db_repository.invalidate_memory("nope") is None


async def test_list_memories_kind_filter(db_repository: StorageRepository) -> None:
    """list_memories 支持 kind 过滤."""
    await db_repository.create_memory(
        scope="global", scope_id="system", content="约束A", kind="constraint"
    )
    await db_repository.create_memory(
        scope="global", scope_id="system", content="偏好B", kind="preference"
    )
    only = await db_repository.list_memories("global", "system", kind="constraint")
    assert [m.content for m in only] == ["约束A"]


async def test_list_direction_memories_scope_and_priority(
    db_repository: StorageRepository,
) -> None:
    """方向层组装：global+user 全局 + 当前项目 project 级；按 kind 优先级排序."""
    proj = await db_repository.create_project("dir-proj", root_path="/tmp/dir-proj")
    other = await db_repository.create_project(
        "other-proj", root_path="/tmp/other-proj"
    )

    await db_repository.create_memory(
        scope="global", scope_id="system",
        content="pref-global", kind="preference",
    )
    await db_repository.create_memory(
        scope="global", scope_id="system", content="constraint-global", kind="constraint"
    )
    await db_repository.create_memory(
        scope="user", scope_id="user", content="design-user", kind="design"
    )
    await db_repository.create_memory(
        scope="project", scope_id=proj.id, content="directive-proj", kind="directive"
    )
    # 别的项目的 project 级条目不应进入本项目注入
    await db_repository.create_memory(
        scope="project", scope_id=other.id, content="other-proj-entry", kind="constraint"
    )

    items = await db_repository.list_direction_memories(project_id=proj.id)
    contents = [m.content for m in items]
    assert "other-proj-entry" not in contents
    assert set(contents) == {
        "pref-global", "constraint-global", "design-user", "directive-proj"
    }
    # kind 优先级：constraint(0) < design(1) < directive(2) < preference(3)
    kinds = [m.kind for m in items]
    assert kinds == sorted(kinds, key=lambda k: {
        "constraint": 0, "design": 1, "directive": 2, "preference": 3
    }[k])
    assert kinds[0] == "constraint"


async def test_list_direction_memories_no_project(
    db_repository: StorageRepository,
) -> None:
    """不传 project_id 时只含 global+user，project 级条目全排除."""
    proj = await db_repository.create_project("p1")
    await db_repository.create_memory(
        scope="global", scope_id="system", content="g", kind="constraint"
    )
    await db_repository.create_memory(
        scope="project", scope_id=proj.id, content="p", kind="constraint"
    )
    items = await db_repository.list_direction_memories(project_id=None)
    assert [m.content for m in items] == ["g"]


async def test_count_valid_memories(db_repository: StorageRepository) -> None:
    """count_valid_memories 只数有效条目（失效不计）."""
    a = await db_repository.create_memory(
        scope="global", scope_id="system", content="a", kind="preference"
    )
    await db_repository.create_memory(
        scope="global", scope_id="system", content="b", kind="preference"
    )
    assert await db_repository.count_valid_memories("global", "system") == 2
    await db_repository.invalidate_memory(a.id)
    assert await db_repository.count_valid_memories("global", "system") == 1


# ================================================================
# 双 hook 方向记忆渲染函数（stdlib only，直接单测渲染逻辑）
# ================================================================


def test_render_direction_memories_bootstrap_truncation() -> None:
    """session_bootstrap 渲染：超预算按顺序截断并注明剩余条数."""
    from aiteam.hooks.session_bootstrap import _render_direction_memories

    items = [
        {"kind": "constraint", "content": "约" * 50},
        {"kind": "design", "content": "计" * 50},
        {"kind": "preference", "content": "偏" * 50},
    ]
    lines = _render_direction_memories(items, budget=120)
    text = "\n".join(lines)
    assert "方向记忆" in text
    assert "约束/护栏" in text  # 高优先级先保留
    assert "另有" in text and "memory_list" in text  # 触发截断提示


def test_render_direction_memories_empty() -> None:
    """空列表渲染为空（不注入噪声）."""
    from aiteam.hooks.session_bootstrap import _render_direction_memories

    assert _render_direction_memories([]) == []


def test_render_direction_memories_subagent() -> None:
    """inject_subagent_context 渲染：kind 标签 + 内容都在，全量放得下不截断."""
    from aiteam.hooks.inject_subagent_context import _render_direction_memories

    items = [
        {"kind": "constraint", "content": "所有输出使用中文"},
        {"kind": "directive", "content": "完成即汇报"},
    ]
    lines = _render_direction_memories(items, budget=900)
    text = "\n".join(lines)
    assert "所有输出使用中文" in text
    assert "完成即汇报" in text
    assert "另有" not in text  # 未截断
