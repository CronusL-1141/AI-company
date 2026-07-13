"""记忆系统 v2 P2 — 按需整理 memory_reconcile 单元测试.

覆盖：reconcile 粗筛聚簇 + BM25 配对（build_candidate_groups）、repository
新增方法（list_project_task_memos / invalidate_task_memo 幂等 / score_task_memo /
count_valid_task_memos_since / get+set_last_reconcile_at）。
（pyproject asyncio_mode=auto，async 测试无需装饰器。）
"""

from __future__ import annotations

from datetime import datetime, timedelta

from aiteam.memory.reconcile import build_candidate_groups
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskMemo

# ================================================================
# 粗筛：聚簇 + BM25 两两配对（纯函数，不依赖 DB）
# ================================================================


def _memo(content: str, task_id: str = "t1", scope_path: str = "") -> TaskMemo:
    return TaskMemo(task_id=task_id, content=content, scope_path=scope_path)


def test_similar_memos_pair_into_group() -> None:
    """同 scope_path 内高相似两条 → 配成候选组，含全文."""
    memos = [
        _memo("部署 API 到生产环境使用 docker compose 命令", scope_path="/deploy"),
        _memo("生产环境部署 API 用 docker compose 命令启动", scope_path="/deploy"),
        _memo("前端 React 组件的样式微调和颜色替换", scope_path="/deploy"),
    ]
    groups = build_candidate_groups(memos, threshold=0.45)
    assert len(groups) == 1
    group = groups[0]
    assert group["member_count"] == 2
    contents = {m["content"] for m in group["members"]}
    assert any("部署 API" in c for c in contents)
    # 不相关的前端 memo 不应进组
    assert all("前端 React" not in c for c in contents)


def test_dissimilar_memos_no_group() -> None:
    """簇内两两都不相似 → 不产出候选组."""
    memos = [
        _memo("数据库索引优化查询性能", scope_path="/db"),
        _memo("撰写用户使用手册文档", scope_path="/db"),
    ]
    assert build_candidate_groups(memos, threshold=0.45) == []


def test_empty_scope_clusters_by_task_not_globally() -> None:
    """无 scope_path 的相似 memo 分属不同任务 → 不跨任务聚簇成组."""
    memos = [
        _memo("修复登录接口的空指针异常", task_id="task-A"),
        _memo("修复登录接口的空指针异常", task_id="task-B"),
    ]
    # 不同任务 + 空 scope_path → cluster_key 不同 → 不成组
    assert build_candidate_groups(memos, threshold=0.3) == []


def test_promotion_candidate_flagged_across_tasks() -> None:
    """同 scope_path 跨任务反复出现 → 标记 promotion_candidate."""
    memos = [
        _memo("用户要求所有输出使用中文", task_id="task-A", scope_path="/pref"),
        _memo("用户要求所有输出都用中文回复", task_id="task-B", scope_path="/pref"),
    ]
    groups = build_candidate_groups(memos, threshold=0.4)
    assert len(groups) == 1
    assert groups[0]["promotion_candidate"] is True
    assert groups[0]["distinct_tasks"] == 2


# ================================================================
# repository 新增方法
# ================================================================


async def test_list_project_task_memos(db_repository: StorageRepository) -> None:
    """按 project 列有效 memo，失效/他项目条目排除."""
    await db_repository.add_task_memo("t1", content="a", project_id="P1")
    m2 = await db_repository.add_task_memo("t1", content="b", project_id="P1")
    await db_repository.add_task_memo("t2", content="c", project_id="P2")
    await db_repository.invalidate_task_memo(m2.id)

    listed = await db_repository.list_project_task_memos("P1")
    contents = [m.content for m in listed]
    assert contents == ["a"]  # b 已失效、c 属 P2


async def test_list_project_task_memos_scope_path(
    db_repository: StorageRepository,
) -> None:
    """scope_path 给定 → 仅取该作用域."""
    await db_repository.add_task_memo(
        "t1", content="x", project_id="P1", scope_path="/deploy"
    )
    await db_repository.add_task_memo(
        "t1", content="y", project_id="P1", scope_path="/db"
    )
    only = await db_repository.list_project_task_memos("P1", scope_path="/deploy")
    assert [m.content for m in only] == ["x"]


async def test_invalidate_task_memo_idempotent(
    db_repository: StorageRepository,
) -> None:
    """重复失效不改写首次的 invalidated_by（幂等底座）."""
    m = await db_repository.add_task_memo("t1", content="a", project_id="P1")
    first = await db_repository.invalidate_task_memo(m.id, invalidated_by="new-1")
    assert first is not None and first.invalidated_by == "new-1"
    at1 = first.invalid_at
    # 二次失效：不覆盖
    second = await db_repository.invalidate_task_memo(m.id, invalidated_by="new-2")
    assert second is not None
    assert second.invalidated_by == "new-1"
    assert second.invalid_at == at1
    # 不存在 → None
    assert await db_repository.invalidate_task_memo("nope") is None


async def test_score_task_memo(db_repository: StorageRepository) -> None:
    """打分入列、reason 入 meta；id 不存在 → None."""
    m = await db_repository.add_task_memo("t1", content="总结", project_id="P1")
    scored = await db_repository.score_task_memo(m.id, 8, "信息密度高")
    assert scored is not None
    assert scored.quality_score == 8
    assert scored.meta.get("quality_reason") == "信息密度高"
    assert await db_repository.score_task_memo("nope", 5) is None


async def test_count_valid_task_memos_since(
    db_repository: StorageRepository,
) -> None:
    """since 后的有效计数；None 计全部."""
    old = await db_repository.add_task_memo("t1", content="old", project_id="P1")
    # 手动把 old 的 created_at 拨到过去，模拟"上次整理前"
    cutoff = datetime.now()
    await db_repository.add_task_memo("t1", content="new", project_id="P1")

    total = await db_repository.count_valid_task_memos_since("P1", None)
    assert total == 2
    since = await db_repository.count_valid_task_memos_since("P1", cutoff)
    assert since == 1  # 仅 cutoff 之后的 new
    assert old.content == "old"


async def test_last_reconcile_at_roundtrip(
    db_repository: StorageRepository,
) -> None:
    """set/get last_reconcile_at 走 project.config 往返一致."""
    project = await db_repository.create_project(name="P")
    assert await db_repository.get_last_reconcile_at(project.id) is None
    when = datetime.now() - timedelta(minutes=5)
    out = await db_repository.set_last_reconcile_at(project.id, when)
    assert out == when
    got = await db_repository.get_last_reconcile_at(project.id)
    assert got is not None
    assert abs((got - when).total_seconds()) < 1
    # 项目不存在 → None
    assert await db_repository.set_last_reconcile_at("nope") is None
