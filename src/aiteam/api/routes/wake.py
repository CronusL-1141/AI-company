"""AI Team OS — Wake actionable route.

唤醒体系 v2（docs/wake-loop-v2-design.md §7.1）的单一只读判据端点。
事件 watcher（scripts/os-watch.sh）轮询它、turn-end guard（hooks/turn_end_guard.py）
停机时查它，共用同一判据。判据逻辑集中在 wake_actionable.py（可单测），本文件仅做薄
HTTP 包装。

用 get_repository（不带项目 scope）：判据模块内部按 team_id/session_id/project_id 显式
过滤，避免 ambient _project_scope 造成的双重过滤。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aiteam.api import wake_actionable
from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["wake"])


@router.get("/api/wake/actionable")
async def get_wake_actionable(
    session_id: str = "",
    team_id: str = "",
    project_id: str = "",
    since: str = "",
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """判断 Leader 会话当前是否有"值得被唤醒处理"的事件。

    Query params（全部可选）：
    - session_id: 归属 workflow_runs 的启动会话
    - team_id: 归属 agents 的团队
    - project_id: 归属 task_memos/briefings 的项目（缺省从 team 解析）
    - since: ISO8601 时间水位（naive-local 或带 tz 均可），只统计其后的增量事件

    返回见 wake_actionable.compute_actionable。绝不 500：内部任何失败降级为保守值。
    """
    return await wake_actionable.compute_actionable(
        repo,
        session_id=session_id,
        team_id=team_id,
        project_id=project_id,
        since_raw=since or None,
    )
