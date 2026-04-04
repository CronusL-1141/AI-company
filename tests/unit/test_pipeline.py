"""AI Team OS — Pipeline unit tests.

Tests PipelineManager create/advance/fail/skip/status operations
using in-memory SQLite via the API test client.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.loop.pipeline import (
    MAX_ROLLBACK_COUNT,
    PIPELINE_TEMPLATES,
    SHORTCUT_PIPELINES,
    PipelineManager,
)
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def app_client():
    """Create test client with in-memory SQLite."""
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
def repo_and_loop():
    """Create a standalone repo for direct PipelineManager tests."""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    yield repo
    asyncio.get_event_loop().run_until_complete(close_db())


def _run(coro):
    """Helper to run async in sync tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# Template definition tests
# ============================================================


def test_pipeline_templates_defined():
    """All 4 standard pipeline templates exist."""
    assert "feature" in PIPELINE_TEMPLATES
    assert "bugfix" in PIPELINE_TEMPLATES
    assert "research" in PIPELINE_TEMPLATES
    assert "refactor" in PIPELINE_TEMPLATES


def test_shortcut_templates_defined():
    """All 3 shortcut pipelines exist."""
    assert "quick-fix" in SHORTCUT_PIPELINES
    assert "spike" in SHORTCUT_PIPELINES
    assert "hotfix" in SHORTCUT_PIPELINES


def test_pipeline_stages_have_required_fields():
    """Each stage in every template has name and agent_template."""
    for name, stages in {**PIPELINE_TEMPLATES, **SHORTCUT_PIPELINES}.items():
        for stage in stages:
            assert "name" in stage, f"Template '{name}' has stage without name"
            assert "agent_template" in stage, f"Template '{name}' stage '{stage['name']}' missing agent_template"


# ============================================================
# API route tests
# ============================================================


def _create_team_and_task(client: TestClient) -> tuple[str, str]:
    """Helper: create a team and a task, return (team_id, task_id)."""
    resp = client.post("/api/teams", json={"name": "test-team", "mode": "coordinate"})
    team_id = resp.json()["data"]["id"]

    resp = client.post(
        f"/api/teams/{team_id}/tasks/run",
        json={"description": "Test task for pipeline", "title": "Test Feature"},
    )
    task_id = resp.json()["data"]["id"]
    return team_id, task_id


def test_create_pipeline_via_api(app_client):
    """Create a feature pipeline via API."""
    _, task_id = _create_team_and_task(app_client)

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "feature"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["pipeline_type"] == "feature"
    assert data["data"]["total_stages"] == 6
    assert data["data"]["active_stages"] == 6
    assert data["data"]["current_stage"] == "research"
    assert data["data"]["next_agent_template"] == "explore-agent"
    # Verify stages
    stages = data["data"]["stages"]
    assert len(stages) == 6
    assert stages[0]["name"] == "research"
    assert stages[0]["status"] == "pending"
    assert stages[0]["subtask_id"] is not None


def test_create_pipeline_with_skip(app_client):
    """Create a feature pipeline with skipped stages."""
    _, task_id = _create_team_and_task(app_client)

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "feature", "skip_stages": ["deploy"]},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["active_stages"] == 5  # 6 - 1 skipped
    stages = data["data"]["stages"]
    deploy_stage = [s for s in stages if s["name"] == "deploy"][0]
    assert deploy_stage["status"] == "skipped"
    assert deploy_stage["subtask_id"] is None


def test_create_pipeline_invalid_type(app_client):
    """Reject unknown pipeline type."""
    _, task_id = _create_team_and_task(app_client)

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "nonexistent"},
    )
    data = resp.json()
    assert data["success"] is False
    assert "未知" in data["error"]


def test_create_pipeline_duplicate(app_client):
    """Reject duplicate pipeline creation."""
    _, task_id = _create_team_and_task(app_client)

    app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "bugfix"},
    )
    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "bugfix"},
    )
    data = resp.json()
    assert data["success"] is False
    assert "已有" in data["error"]


def test_advance_pipeline_via_api(app_client):
    """Advance pipeline through stages."""
    _, task_id = _create_team_and_task(app_client)

    # Create bugfix pipeline (5 stages)
    app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "bugfix"},
    )

    # Advance: reproduce → diagnose
    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/advance",
        json={"result_summary": "Bug reproduced on test env"},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["completed_stage"] == "reproduce"
    assert data["data"]["current_stage"] == "diagnose"
    assert data["data"]["agent_template"] == "backend-architect"

    # Advance: diagnose → fix
    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/advance",
        json={"result_summary": "Root cause: null check missing"},
    )
    data = resp.json()
    assert data["data"]["completed_stage"] == "diagnose"
    assert data["data"]["current_stage"] == "fix"


def test_advance_pipeline_to_completion(app_client):
    """Advance pipeline all the way to completion."""
    _, task_id = _create_team_and_task(app_client)

    # Create spike pipeline (2 stages: research → report)
    app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "spike"},
    )

    # Advance: research → report
    resp = app_client.post(f"/api/tasks/{task_id}/pipeline/advance", json={})
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["current_stage"] == "report"

    # Advance: report → done
    resp = app_client.post(f"/api/tasks/{task_id}/pipeline/advance", json={})
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["pipeline_completed"] is True


def test_pipeline_status_via_api(app_client):
    """Query pipeline status."""
    _, task_id = _create_team_and_task(app_client)

    app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "research"},
    )

    # Advance one stage
    app_client.post(f"/api/tasks/{task_id}/pipeline/advance", json={})

    resp = app_client.get(f"/api/tasks/{task_id}/pipeline")
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["pipeline_type"] == "research"
    assert data["data"]["progress"] == "1/4"
    assert data["data"]["progress_pct"] == 25
    assert data["data"]["current_stage"] == "analyze"
    assert data["data"]["stats"]["completed"] == 1
    assert data["data"]["stats"]["active"] == 4


def test_pipeline_status_no_pipeline(app_client):
    """Query status for task without pipeline."""
    _, task_id = _create_team_and_task(app_client)

    resp = app_client.get(f"/api/tasks/{task_id}/pipeline")
    data = resp.json()
    assert data["success"] is False
    assert "没有 pipeline" in data["error"]


def test_fail_stage_with_rollback(app_client):
    """Fail a review stage triggers rollback to implement."""
    _, task_id = _create_team_and_task(app_client)

    # Create feature pipeline
    app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "feature"},
    )

    # Advance through research → design → implement → review
    for _ in range(3):
        app_client.post(f"/api/tasks/{task_id}/pipeline/advance", json={})

    # Now at review stage — fail it
    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/fail",
        json={"reason": "Code quality issues found"},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["action"] == "rollback"
    assert data["data"]["rollback_to"] == "implement"
    assert data["data"]["rollback_count"] == 1


def test_fail_stage_max_rollback_escalation(app_client):
    """Exceeding max rollbacks triggers escalation."""
    _, task_id = _create_team_and_task(app_client)

    # Create hotfix pipeline (fix → test)
    app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "hotfix"},
    )

    for i in range(MAX_ROLLBACK_COUNT + 1):
        # Advance fix → test
        app_client.post(f"/api/tasks/{task_id}/pipeline/advance", json={})

        # Fail test (triggers rollback to fix)
        resp = app_client.post(
            f"/api/tasks/{task_id}/pipeline/fail",
            json={"reason": f"Test failure #{i + 1}"},
        )
        data = resp.json()

        if i < MAX_ROLLBACK_COUNT:
            assert data["data"]["action"] == "rollback"
        else:
            assert data["data"]["action"] == "escalate"
            assert data["data"]["rollback_exceeded"] is True


def test_skip_stage_via_api(app_client):
    """Skip a pending stage."""
    _, task_id = _create_team_and_task(app_client)

    app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "feature"},
    )

    # Skip review stage (which is pending)
    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline/skip",
        json={"stage_name": "review"},
    )
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["skipped_stage"] == "review"


def test_list_pipeline_templates(app_client):
    """List all available pipeline templates."""
    resp = app_client.get("/api/pipeline/templates")
    data = resp.json()
    assert data["success"] is True
    assert data["total"] == 7  # 4 standard + 3 shortcut
    assert "feature" in data["data"]
    assert "hotfix" in data["data"]
    assert data["data"]["feature"]["type"] == "standard"
    assert data["data"]["hotfix"]["type"] == "shortcut"


def test_bugfix_pipeline_stages(app_client):
    """Verify bugfix pipeline has correct stages."""
    _, task_id = _create_team_and_task(app_client)

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "bugfix"},
    )
    data = resp.json()
    stage_names = [s["name"] for s in data["data"]["stages"]]
    assert stage_names == ["reproduce", "diagnose", "fix", "review", "test"]


def test_subtasks_created_with_dependencies(app_client):
    """Subtasks are created with correct parent_id and chained depends_on."""
    _, task_id = _create_team_and_task(app_client)

    resp = app_client.post(
        f"/api/tasks/{task_id}/pipeline",
        json={"pipeline_type": "spike"},
    )
    data = resp.json()
    stages = data["data"]["stages"]

    # Get subtask details
    sub0_id = stages[0]["subtask_id"]
    sub1_id = stages[1]["subtask_id"]
    assert sub0_id is not None
    assert sub1_id is not None

    # Check subtasks via API
    resp0 = app_client.get(f"/api/tasks/{sub0_id}")
    sub0 = resp0.json()["data"]
    assert sub0["parent_id"] == task_id
    assert sub0["depends_on"] == []  # first stage has no deps

    resp1 = app_client.get(f"/api/tasks/{sub1_id}")
    sub1 = resp1.json()["data"]
    assert sub1["parent_id"] == task_id
    assert sub1["depends_on"] == [sub0_id]  # second stage depends on first


# ============================================================
# Direct PipelineManager tests
# ============================================================


def test_pipeline_manager_create_direct(repo_and_loop):
    """Test PipelineManager.create_pipeline directly."""
    repo = repo_and_loop
    team = _run(repo.create_team("direct-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Direct test task"))

    mgr = PipelineManager(repo)
    result = _run(mgr.create_pipeline(task.id, "refactor"))

    assert result["success"] is True
    assert result["data"]["pipeline_type"] == "refactor"
    assert result["data"]["total_stages"] == 5

    # Verify config was updated
    updated_task = _run(repo.get_task(task.id))
    assert "pipeline" in updated_task.config
    assert updated_task.config["pipeline"]["type"] == "refactor"


def test_pipeline_manager_nonexistent_task(repo_and_loop):
    """PipelineManager handles missing task gracefully."""
    repo = repo_and_loop
    mgr = PipelineManager(repo)
    result = _run(mgr.create_pipeline("nonexistent-id", "feature"))
    assert result["success"] is False
    assert "不存在" in result["error"]


def test_pipeline_manager_status(repo_and_loop):
    """Test get_pipeline_status directly."""
    repo = repo_and_loop
    team = _run(repo.create_team("status-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Status test"))

    mgr = PipelineManager(repo)
    _run(mgr.create_pipeline(task.id, "research"))

    status = _run(mgr.get_pipeline_status(task.id))
    assert status["success"] is True
    assert status["data"]["pipeline_type"] == "research"
    assert status["data"]["progress"] == "0/4"
    assert status["data"]["progress_pct"] == 0
    assert status["data"]["pipeline_completed"] is False


def test_pipeline_advance_updates_subtask_status(repo_and_loop):
    """Advancing a stage marks its subtask as completed."""
    repo = repo_and_loop
    team = _run(repo.create_team("advance-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Advance test"))

    mgr = PipelineManager(repo)
    result = _run(mgr.create_pipeline(task.id, "spike"))
    sub0_id = result["data"]["stages"][0]["subtask_id"]

    # Advance first stage
    _run(mgr.advance_stage(task.id, "Done"))

    # Check subtask is completed
    sub0 = _run(repo.get_task(sub0_id))
    assert sub0.status.value == "completed"


def test_pipeline_completion_auto_marks_parent_completed(repo_and_loop):
    """When all pipeline stages complete, parent task is auto-marked completed."""
    repo = repo_and_loop
    team = _run(repo.create_team("auto-complete-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Auto complete test"))

    mgr = PipelineManager(repo)
    _run(mgr.create_pipeline(task.id, "spike"))  # 2 stages: research → report

    # Advance through both stages
    _run(mgr.advance_stage(task.id, "research done"))
    result = _run(mgr.advance_stage(task.id, "report done"))

    assert result["success"] is True
    assert result["data"]["pipeline_completed"] is True
    assert result["data"].get("parent_task_completed") is True

    # Verify parent task status in DB
    updated = _run(repo.get_task(task.id))
    assert updated.status.value == "completed"


def test_task_wall_excludes_subtasks(repo_and_loop):
    """Task wall should not include tasks with a parent_id (pipeline subtasks)."""
    from aiteam.loop.engine import LoopEngine

    repo = repo_and_loop
    team = _run(repo.create_team("wall-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Parent Task"))

    # Create pipeline — this generates subtasks with parent_id set
    mgr = PipelineManager(repo)
    _run(mgr.create_pipeline(task.id, "spike"))

    # Query task wall via LoopEngine directly
    engine = LoopEngine(repo)
    wall_data = _run(engine.get_task_wall(team.id))

    # Collect all task IDs from the wall
    all_wall_ids = []
    for bucket in wall_data["wall"].values():
        all_wall_ids.extend(item["id"] for item in bucket)
    all_wall_ids.extend(item["id"] for item in wall_data.get("completed", []))

    # The parent task should be on the wall
    assert task.id in all_wall_ids

    # No subtask (which has parent_id set) should appear on the wall
    pipeline_status = _run(mgr.get_pipeline_status(task.id))
    subtask_ids = {s["subtask_id"] for s in pipeline_status["data"]["stages"] if s["subtask_id"]}
    for sub_id in subtask_ids:
        assert sub_id not in all_wall_ids, f"Subtask {sub_id} should not appear on task wall"


def test_task_wall_shows_pipeline_progress(repo_and_loop):
    """Tasks with a pipeline should include pipeline_progress fields on the wall."""
    from aiteam.loop.engine import LoopEngine

    repo = repo_and_loop
    team = _run(repo.create_team("progress-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Progress Task"))

    # Create spike pipeline (2 stages) and advance one stage
    mgr = PipelineManager(repo)
    _run(mgr.create_pipeline(task.id, "spike"))
    _run(mgr.advance_stage(task.id, "research done"))

    # Query task wall via LoopEngine
    engine = LoopEngine(repo)
    wall_data = _run(engine.get_task_wall(team.id))

    # Find the parent task in the wall (it's still in-progress after 1/2 stages)
    parent_item = None
    for bucket in wall_data["wall"].values():
        for item in bucket:
            if item["id"] == task.id:
                parent_item = item
    assert parent_item is not None, "Parent task not found on wall"
    assert parent_item["pipeline_progress"] == "1/2"
    assert parent_item["pipeline_current_stage"] == "report"
    assert parent_item["pipeline_pct"] == 50


# ============================================================
# Parallel execution tests
# ============================================================


def test_parallel_group_detection(repo_and_loop):
    """_get_parallel_group returns all members of a parallel group."""
    from aiteam.loop.pipeline import PipelineManager

    stages = [
        {"name": "fix", "status": "running", "parallel_with": []},
        {"name": "test", "status": "running", "parallel_with": ["fix"]},
        {"name": "deploy", "status": "pending", "parallel_with": []},
    ]
    # test and fix are in the same group
    group = PipelineManager._get_parallel_group(stages, "fix")
    group_names = {s["name"] for s in group}
    assert group_names == {"fix", "test"}

    # deploy has no parallel peers
    solo = PipelineManager._get_parallel_group(stages, "deploy")
    assert [s["name"] for s in solo] == ["deploy"]


def test_bugfix_parallel_hold(repo_and_loop):
    """Completing fix while test is still pending returns parallel_waiting."""
    repo = repo_and_loop
    team = _run(repo.create_team("par-hold-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Parallel hold test"))

    mgr = PipelineManager(repo)
    # bugfix: reproduce → diagnose → fix / test(parallel) → (done)
    _run(mgr.create_pipeline(task.id, "bugfix"))

    # Advance reproduce → diagnose → fix
    _run(mgr.advance_stage(task.id))   # reproduce done
    _run(mgr.advance_stage(task.id))   # diagnose done
    _run(mgr.advance_stage(task.id))   # fix done → triggers parallel unlock of test

    # At this point fix completed; test should be in RUNNING (parallel-started).
    # Completing fix again would be an error, so we inspect status directly.
    status = _run(mgr.get_pipeline_status(task.id))
    stage_map = {s["name"]: s for s in status["data"]["stages"]}

    # fix completed, test should now be running (unlocked as parallel peer)
    assert stage_map["fix"]["status"] == "completed"
    assert stage_map["test"]["status"] == "running"


def test_bugfix_parallel_completes_pipeline(repo_and_loop):
    """After both fix and test complete, pipeline finishes."""
    repo = repo_and_loop
    team = _run(repo.create_team("par-done-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Parallel done test"))

    mgr = PipelineManager(repo)
    _run(mgr.create_pipeline(task.id, "bugfix"))

    # Advance serially through reproduce, diagnose, fix
    _run(mgr.advance_stage(task.id))   # reproduce → diagnose
    _run(mgr.advance_stage(task.id))   # diagnose → fix
    result = _run(mgr.advance_stage(task.id))  # fix done → parallel unlocks test

    # fix is done; test is now running in parallel
    assert "parallel_stages_started" in result["data"] or result["data"].get("completed_stage") == "fix"

    # Now advance test (the parallel peer) — pipeline should complete
    result2 = _run(mgr.advance_stage(task.id))
    assert result2["success"] is True
    assert result2["data"].get("pipeline_completed") is True

    updated = _run(repo.get_task(task.id))
    assert updated.status.value == "completed"


def test_parallel_status_shows_parallel_with(repo_and_loop):
    """Pipeline status exposes parallel_with field on stages."""
    repo = repo_and_loop
    team = _run(repo.create_team("par-status-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Parallel status test"))

    mgr = PipelineManager(repo)
    _run(mgr.create_pipeline(task.id, "bugfix"))

    status = _run(mgr.get_pipeline_status(task.id))
    stage_map = {s["name"]: s for s in status["data"]["stages"]}
    # test stage in bugfix template declares parallel_with = ["fix"]
    assert stage_map["test"]["parallel_with"] == ["fix"]
    # fix stage has no parallel_with
    assert stage_map["fix"]["parallel_with"] == []
