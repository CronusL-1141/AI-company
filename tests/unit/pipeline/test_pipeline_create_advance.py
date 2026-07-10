"""Unit tests for pipeline_create / pipeline_advance redesign (Phase 1.3).

These tests exercise the new stateful pipeline via the API routes directly,
verifying that:
  - No ceremonial subtasks are generated
  - PipelineState is written correctly into task.config
  - stage_history is appended correctly
  - force / triggered_by semantics work
  - Last-stage / completed-pipeline behaviour is well-defined
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture()
def app_client():
    """Test client with in-memory SQLite, mirrors existing test_pipeline.py pattern."""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


@pytest.fixture()
def repo():
    """Standalone repo for direct storage assertions."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(r.init_db())
    yield r
    asyncio.get_event_loop().run_until_complete(close_db())


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_task(client: TestClient) -> tuple[str, str]:
    """Create team + task, return (team_id, task_id)."""
    resp = client.post("/api/teams", json={"name": "test-team", "mode": "coordinate"})
    team_id = resp.json()["data"]["id"]
    resp = client.post(
        f"/api/teams/{team_id}/tasks/run",
        json={"title": "Test task", "description": "for pipeline tests"},
    )
    task_id = resp.json()["data"]["id"]
    return team_id, task_id


# ============================================================
# TC-CREATE-01: pipeline_create does NOT generate subtasks
# ============================================================


def test_create_does_not_generate_subtasks(app_client, repo):
    """TC-CREATE-01: pipeline_create('feature') must not create any new subtasks."""
    _, task_id = _make_task(app_client)

    subtasks_before = _run(repo.list_subtasks(task_id))

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2",
        json={"task_type": "feature"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    subtasks_after = _run(repo.list_subtasks(task_id))
    assert len(subtasks_before) == len(subtasks_after), (
        f"Expected no new subtasks but found {len(subtasks_after) - len(subtasks_before)} new ones"
    )


# ============================================================
# TC-CREATE-02: correct PipelineState written for 'feature'
# ============================================================


def test_create_feature_pipeline_state(app_client, repo):
    """TC-CREATE-02: pipeline_create writes correct PipelineState for 'feature' template."""
    _, task_id = _make_task(app_client)

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2",
        json={"task_type": "feature"},
    )
    data = resp.json()
    assert data["success"] is True
    payload = data["data"]

    assert payload["template"] == "feature"
    assert payload["current_stage"] == "research"
    assert payload["current_stage_class"] == "Plan"
    assert payload["autopilot_active"] is False
    assert payload["stage_started_at"] is not None

    # Verify persisted to DB
    state = _run(repo.get_pipeline_state(task_id))
    assert state is not None
    assert state.template == "feature"
    assert state.current_stage == "research"
    assert state.current_stage_class == "Plan"


# ============================================================
# TC-CREATE-03: stage_history has initial entry from_stage=None
# ============================================================


def test_create_writes_initial_stage_history(app_client, repo):
    """TC-CREATE-03: pipeline_create writes one history entry with from_stage=None."""
    _, task_id = _make_task(app_client)

    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "research"})

    history = _run(repo.read_stage_history(task_id, limit=10))
    assert len(history) == 1
    assert history[0].from_stage is None
    assert history[0].to_stage == "research"
    assert history[0].triggered_by == "system"
    assert history[0].reason == "pipeline_create"


# ============================================================
# TC-CREATE-04: hotfix first stage is 'diagnose'
# ============================================================


def test_create_hotfix_first_stage_is_diagnose(app_client, repo):
    """TC-CREATE-04: pipeline_create('hotfix') sets first stage to 'diagnose'."""
    _, task_id = _make_task(app_client)

    resp = app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "hotfix"})
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["current_stage"] == "diagnose"
    assert data["data"]["current_stage_class"] == "Plan"

    state = _run(repo.get_pipeline_state(task_id))
    assert state is not None
    assert state.current_stage == "diagnose"


# ============================================================
# TC-ADVANCE-05: advance without target auto-picks next stage
# ============================================================


@pytest.mark.skip(
    reason="v1.7.0 pipeline 退役：自动推进停用、无 force 的 advance 被出口条件拒绝"
    "（CHANGELOG Changed — auto phase progression is stopped）；本测试锁定的是退役前语义"
)
def test_advance_auto_next_stage(app_client, repo):
    """TC-ADVANCE-05: pipeline_advance without target_stage selects next in sequence."""
    _, task_id = _make_task(app_client)
    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "research"})

    # research → report (auto)
    resp = app_client.post(f"/api/tasks/{task_id}/pipeline/v2/advance", json={})
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["from_stage"] == "research"
    assert data["data"]["to_stage"] == "report"
    assert data["data"]["current_stage_class"] == "Plan"

    state = _run(repo.get_pipeline_state(task_id))
    assert state is not None
    assert state.current_stage == "report"


# ============================================================
# TC-ADVANCE-06: force=True skips exit condition check
# ============================================================


def test_advance_force_skips_exit_check(app_client, repo):
    """TC-ADVANCE-06: pipeline_advance(force=True) succeeds even if exit check would block."""
    _, task_id = _make_task(app_client)
    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "spike"})

    # The placeholder exit check always returns True, but force flag should be
    # reflected in the response and should not raise any error.
    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2/advance",
        json={"force": True, "triggered_by": "force"},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["force"] is True
    assert data["data"]["triggered_by"] == "force"
    assert data["data"]["to_stage"] == "implement"


# ============================================================
# TC-ADVANCE-07: triggered_by stored in stage_history
# ============================================================


def test_advance_triggered_by_stored_in_history(app_client, repo):
    """TC-ADVANCE-07: triggered_by='auto' is written into stage_history."""
    _, task_id = _make_task(app_client)
    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "hotfix"})

    # v1.7.0 后无 force 的 advance 被出口条件拒绝；本测试标的是 history 记录逻辑
    app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2/advance",
        json={"triggered_by": "auto", "force": True},
    )

    history = _run(repo.read_stage_history(task_id, limit=10))
    advance_entry = [h for h in history if h.from_stage is not None]
    assert len(advance_entry) == 1
    assert advance_entry[0].triggered_by == "auto"
    assert advance_entry[0].from_stage == "diagnose"
    assert advance_entry[0].to_stage == "fix"


# ============================================================
# TC-ADVANCE-08: advance on last stage returns pipeline_completed error
# ============================================================


def test_advance_on_last_stage_returns_completed_signal(app_client):
    """TC-ADVANCE-08: advancing past the last stage returns pipeline_completed=True error.

    Design decision: we return success=False with pipeline_completed=True to signal
    the caller that the pipeline is done, not broken. Leader should treat this as
    completion, not a failure.
    """
    _, task_id = _make_task(app_client)
    # quick-fix: fix → test (2 stages)
    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "quick-fix"})

    # fix → test（v1.7.0 后无 force 会被出口条件拒绝；本测试标的是 last-stage 信号）
    app_client.post(f"/api/tasks/{task_id}/pipeline/v2/advance", json={"force": True})

    # test → ??? (last stage)
    resp = app_client.post(f"/api/tasks/{task_id}/pipeline/v2/advance", json={"force": True})
    data = resp.json()
    assert data["success"] is False
    assert data.get("pipeline_completed") is True
    assert "最后阶段" in data["error"] or "已完成" in data["error"]


# ============================================================
# TC-AUTOPILOT-01..03: autopilot endpoint
# ============================================================


def test_autopilot_enable(app_client, repo):
    """TC-AUTOPILOT-01: POST autopilot active=True sets autopilot_active and records started_at."""
    _, task_id = _make_task(app_client)

    # Create pipeline first
    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "feature"})

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2/autopilot",
        json={"active": True},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["autopilot_active"] is True
    assert data["data"]["autopilot_started_at"] is not None

    # Verify persisted
    state = _run(repo.get_pipeline_state(task_id))
    assert state is not None
    assert state.autopilot_active is True


def test_autopilot_disable(app_client, repo):
    """TC-AUTOPILOT-02: POST autopilot active=False sets autopilot_active=False."""
    _, task_id = _make_task(app_client)

    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "feature"})
    # Enable first
    app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2/autopilot",
        json={"active": True},
    )

    # Now disable
    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2/autopilot",
        json={"active": False},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["autopilot_active"] is False

    state = _run(repo.get_pipeline_state(task_id))
    assert state is not None
    assert state.autopilot_active is False


def test_autopilot_enable_with_duration(app_client, repo):
    """TC-AUTOPILOT-03: POST autopilot with max_duration_minutes persists the value."""
    _, task_id = _make_task(app_client)

    app_client.post(f"/api/tasks/{task_id}/pipeline/v2", json={"task_type": "feature"})

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/v2/autopilot",
        json={"active": True, "max_duration_minutes": 240},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["autopilot_max_duration_minutes"] == 240
