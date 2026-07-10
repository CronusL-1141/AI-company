"""End-to-end integration test for the deep-review workflow.

Exercises the full chain:
  service.request -> embed prompt -> simulate agent saving a deep-review
  report -> hook payload -> service.link_report -> review.completed

No real CC sub-agent is launched; instead we synthesize the
PostToolUse payload that the deep_review_link hook would receive and feed
it through the same regex-extract path. The repository is in-memory SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.hooks.deep_review_link import _extract_anchors
from aiteam.services.ecosystem_deep_reviewer import EcosystemDeepReviewer
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def test_deep_review_e2e_flow(repo: StorageRepository) -> None:
    """Walk a real PrefectHQ/fastmcp-shaped row through the full lifecycle."""
    profile = EcosystemRepoProfile(
        repo_full_name="PrefectHQ/fastmcp",
        name="fastmcp",
        owner="PrefectHQ",
        stars=25000,
        language="Python",
        description="Fast, Pythonic way to build MCP servers.",
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("PrefectHQ/fastmcp")
    assert fetched is not None
    repo_id = fetched.id

    # 1. Service queues a review and returns the dispatch prompt.
    reviewer = EcosystemDeepReviewer(repo)
    review = await reviewer.request(repo_id=repo_id, timeout_minutes=45)
    # D5: status is derived from stage_status (queued); no more RUNNING writes.
    assert review.status == EcosystemDeepReviewStatus.QUEUED
    # K5: dispatch prompt now lives in dispatch_prompt, not demo_log_excerpt.
    assert "PrefectHQ/fastmcp" in review.dispatch_prompt
    assert review.demo_log_excerpt == ""

    # 2. Simulate a sub-agent saving a 5-section report. We embed the two
    #    machine-readable anchors required by the hook.
    report_id = "report-uuid-fastmcp-001"
    report_content = f"""# PrefectHQ/fastmcp 深度审查报告

repo_id={repo_id}
deep_review_id={review.id}

## 1. 真实定位与成熟度
Pythonic MCP server framework. README claim 与实际相符。25K stars。

## 2. 架构概览
- src/fastmcp/server.py — 核心入口
- src/fastmcp/tools.py — 工具装饰器
- 目录树（深度 2）：
  ```
  fastmcp/
  ├── src/fastmcp/
  └── tests/
  ```

## 3. 我们能借鉴的点
- 装饰器风格的 tool 注册（src/fastmcp/tools.py:42-89）
- 集成可能性：reference

## 4. 风险/不可取
- 依赖较重：依赖 anyio
- 许可证：Apache-2.0（兼容）

## 5. 集成建议
- 推荐动作：reference
- 理由：API 风格成熟可借鉴，但本项目已自建 MCP 服务器，无需直接集成。

## 元数据
- demo_result: skipped
- demo_log_excerpt: 由代码评审完成，未运行 demo。
"""

    # 3. Build the PostToolUse payload exactly as Claude Code would emit it.
    hook_payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {
            "author": "deepdive-agent",
            "topic": "deep-review-PrefectHQ-fastmcp",
            "content": report_content,
            "report_type": "deep-review",
        },
        "tool_response": {"id": report_id, "report_type": "deep-review"},
    }

    # K5: hook signature now returns (deep_review_id, repo_id, report_id, content).
    deep_review_id, anchored_repo_id, extracted_report_id, _content = (
        _extract_anchors(hook_payload)
    )
    assert deep_review_id == review.id
    assert anchored_repo_id == repo_id
    assert extracted_report_id == report_id

    # 4. The hook would now POST to link_report; here we call the service
    #    directly (REST-equivalent) since the API endpoint already wraps it.
    linked = await reviewer.link_report(deep_review_id, extracted_report_id)
    assert linked is not None
    assert linked.status == EcosystemDeepReviewStatus.COMPLETED
    assert linked.report_id == report_id
    assert linked.completed_at is not None
    assert linked.duration_seconds >= 0.0

    # 5. status() now returns the completed row.
    final = await reviewer.status(repo_id=repo_id)
    assert final is not None
    assert final.status == EcosystemDeepReviewStatus.COMPLETED
    assert final.report_id == report_id
