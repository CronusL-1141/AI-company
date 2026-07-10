"""End-to-end integration test for the v1.5.0-B Stage 0 shallow-scan flow.

Walks a brand-new repo through the full chain:

  1. Scanner discovers a new repo and persists the profile, triggering
     the on_new_profile hook (Scanner -> Worker bridge).
  2. The hook calls EcosystemShallowQueueWorker.enqueue_repo, which
     creates a deep_review row in RUNNING + builds a dispatch prompt.
  3. We simulate the ai-engineer sub-agent finishing by directly
     calling the same code path the apply_summary MCP tool uses
     (update_profile_shallow_summary + update_deep_review_stage).
  4. Verify stage_status flips to SHALLOW_DONE and the summary is
     persisted.

No real CC sub-agent is spawned; the test exercises every layer except
the actual Agent tool call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio

from aiteam.services.ecosystem_scanner import (
    EcosystemScanner,
    FilterConfig,
)
from aiteam.services.ecosystem_shallow_queue import (
    DispatchIntent,
    EcosystemShallowQueueWorker,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReviewStatus,
    EcosystemProjectSettings,
    EcosystemScanStrategy,
    EcosystemStageStatus,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


def _make_repo_data(
    full_name: str,
    *,
    stars: int,
    description: str,
    topics: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "repo_full_name": full_name,
        "name": full_name.split("/")[-1],
        "owner": full_name.split("/")[0],
        "description": description,
        "stars": stars,
        "language": "Python",
        "topics": topics or ["claude-code", "mcp"],
        "homepage": None,
        "last_commit_at": datetime.now(tz=UTC),
        "pushed_at": datetime.now(tz=UTC),
        "needs_deep_review": stars < 15000,
        "relevance_category": "skill-system",
        "relevance_score": 8,
        "one_line_summary": description[:200],
    }


def _gh_search_factory(items: list[dict[str, Any]]):
    async def _gh_search(
        keyword: str, min_stars: int, topics: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return list(items)

    return _gh_search


# ============================================================
# Tests
# ============================================================


async def test_stage0_e2e_new_repo_to_summary_completed(
    repo: StorageRepository,
) -> None:
    """Scanner -> queue dispatch -> apply summary -> stage_status=shallow_done."""
    project_id = "proj-e2e"
    # Seed the project's ecosystem settings.
    await repo.upsert_ecosystem_project_settings(
        EcosystemProjectSettings(
            project_id=project_id,
            min_stars=1000,
            top_n=50,
            shallow_concurrency=5,
        )
    )

    captured_intents: list[DispatchIntent] = []
    worker = EcosystemShallowQueueWorker(repo, project_id=project_id)

    async def on_new_profile(profile) -> None:
        intent = await worker.enqueue_repo(profile.id)
        if intent is not None:
            captured_intents.append(intent)

    new_item = _make_repo_data(
        "anthropics/claude-code",
        stars=10000,
        description="Claude AI coding assistant with MCP support",
    )
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_gh_search_factory([new_item]),
        config=FilterConfig(min_stars=500, refresh_window_days=7),
        project_id=project_id,
        on_new_profile=on_new_profile,
    )

    # 1. Scanner runs, persists profile, fires hook -> worker.enqueue_repo.
    scan_result = await scanner.scan(
        strategy=EcosystemScanStrategy.FULL,
        queries=(("claude", ("mcp",)),),
        triggered_by="test",
    )
    assert scan_result.new_profiles == 1
    assert len(captured_intents) == 1
    intent = captured_intents[0]
    assert intent.repo_full_name == "anthropics/claude-code"

    # 2. The deep_review row should be in-flight with the dispatch prompt.
    #    D5: status is a derived read-only view (stays 'queued'); in-flight
    #    is expressed by stage=queued + the tick claim held atomically.
    review = await repo.get_deep_review(
        intent.deep_review_id, project_id=project_id
    )
    assert review is not None
    assert review.status == EcosystemDeepReviewStatus.QUEUED
    assert review.stage_status == EcosystemStageStatus.QUEUED  # not yet SHALLOW_DONE
    assert review.claimed_by is not None
    assert review.claimed_by.startswith("tick:")
    assert "anthropics/claude-code" in review.dispatch_prompt
    assert "200-400" in review.dispatch_prompt

    # 3. Simulate the ai-engineer sub-agent producing a summary; this is
    #    the exact code path the apply_summary MCP tool / API endpoint
    #    invokes. We avoid spinning a TestClient by hitting the layer
    #    underneath (storage helpers + stage advance).
    summary = (
        "## Claude Code\n"
        "核心功能: Anthropic 官方 Claude AI 编程助手, 通过 MCP 协议扩展工具能力。\n"
        "定位: agent-framework 生态中的官方实现, 与 IDE / 终端深度集成。\n"
        "适用场景: AI Team OS 想升级 sub-agent 调度时可直接对标 / 借鉴 prompt 模板。"
    )
    profile = await repo.update_profile_shallow_summary(
        intent.repo_id,
        shallow_summary=summary,
        project_id=project_id,
    )
    assert profile is not None
    assert profile.shallow_summary.startswith("## Claude Code")
    assert profile.last_shallow_refreshed_at is not None

    final = await repo.update_deep_review_stage(
        intent.deep_review_id,
        EcosystemStageStatus.SHALLOW_DONE,
        project_id=project_id,
    )
    assert final is not None
    assert final.stage_status == EcosystemStageStatus.SHALLOW_DONE
    assert final.shallow_completed_at is not None

    # 4. Worker should now report 0 pending shallow scans.
    status = await worker.queue_status()
    assert status["pending_shallow"] == 0
    assert status["active_total"] >= 1


async def test_stage0_e2e_failure_classification_and_recovery(
    repo: StorageRepository,
) -> None:
    """A 404 marks deleted; later revive returns repo to active set."""
    project_id = "proj-e2e-fail"
    await repo.upsert_ecosystem_project_settings(
        EcosystemProjectSettings(project_id=project_id, min_stars=1000)
    )

    # Seed the profile manually (skip scanner for this scenario).
    item = _make_repo_data(
        "ghosts/abandoned",
        stars=2000,
        description="Will go 404",
    )
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_gh_search_factory([item]),
        config=FilterConfig(min_stars=500, refresh_window_days=7),
        project_id=project_id,
    )
    await scanner.scan(
        strategy=EcosystemScanStrategy.FULL,
        queries=(("ghosts", ()),),
    )

    profile = await repo.get_ecosystem_profile(
        "ghosts/abandoned", project_id=project_id
    )
    assert profile is not None

    revive_status = {"status": 404}

    async def gh_fetch(repo_full_name: str) -> dict[str, Any]:
        return {"http_status": revive_status["status"]}

    worker = EcosystemShallowQueueWorker(
        repo, project_id=project_id, gh_fetcher=gh_fetch
    )

    # 1. Agent reports 404 -> profile marked deleted, no retry.
    decision = await worker.report_failure(
        profile.id, error_kind="http", http_status=404
    )
    assert decision.failure_class == "deleted"
    assert decision.immediate_retry is False

    flagged = await repo.get_ecosystem_profile_by_id(
        profile.id, project_id=project_id
    )
    assert flagged is not None
    assert flagged.is_deleted is True
    assert flagged.is_active is False

    # 2. tick() should now skip the deleted repo.
    await worker.tick()
    deleted_review_rows = await repo.list_deep_reviews(
        repo_id=profile.id, project_id=project_id
    )
    # No new in-flight runs were queued.
    inflight = [
        r
        for r in deleted_review_rows
        if r.status
        in (
            EcosystemDeepReviewStatus.QUEUED,
            EcosystemDeepReviewStatus.RUNNING,
        )
    ]
    assert inflight == []

    # 3. GitHub recovers — revive_check_one clears the flag.
    revive_status["status"] = 200
    revived = await worker.revive_check_one(profile.id)
    assert revived is True

    restored = await repo.get_ecosystem_profile_by_id(
        profile.id, project_id=project_id
    )
    assert restored is not None
    assert restored.is_deleted is False
    assert restored.is_active is True

    # 4. Next tick now picks the revived repo for shallow scan.
    result = await worker.tick()
    assert result.dispatched == 1
    assert result.intents[0].repo_id == profile.id
