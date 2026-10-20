"""Watchdog 巡检事件必须通过生产枚举校验，并能跨数据库连接读回。"""

from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.config import settings
from aiteam.loop.watchdog import WatchdogChecker, WatchdogRunner
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, Event, TaskStatus


@pytest_asyncio.fixture
async def watchdog_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "")
    url = f"sqlite+aiosqlite:///{tmp_path / 'watchdog.sqlite'}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    try:
        yield repo, url
    finally:
        await get_engine(url).dispose()


async def _patrol_and_read(repo: StorageRepository, url: str) -> tuple[StorageRepository, list[Event]]:
    runner = WatchdogRunner(WatchdogChecker(repo), EventBus(repo))
    # This is the real per-repository entry called by each patrol cycle. Keeping
    # the exception visible makes an unknown event fail at its production source.
    await runner._run_cycle_for_repo(repo)
    await get_engine(url).dispose()
    reader = StorageRepository(db_url=url)
    return reader, await reader.list_events(type_prefix="watchdog.")


@pytest.mark.asyncio
async def test_alerts_persist_and_patrol_reaches_the_next_team(watchdog_repo):
    repo, url = watchdog_repo
    task_ids = set()
    team_sources = set()
    for name in ("first", "second"):
        team = await repo.create_team(name, "coordinate")
        dependency = await repo.create_task(team.id, "已完成的依赖", status="completed")
        task = await repo.create_task(
            team.id, "可解除阻塞", status="blocked", depends_on=[dependency.id],
        )
        task_ids.add(task.id)
        team_sources.add(f"team:{team.id}")
        alerts = await WatchdogChecker(repo).run_all_checks(team.id)
        assert len(alerts) == 1
        assert alerts[0]["task_id"] == task.id

    _, events = await _patrol_and_read(repo, url)

    assert len(events) == 2
    assert {event.type.value for event in events} == {"watchdog.alert"}
    assert {event.source for event in events} == team_sources
    assert {event.data["task_id"] for event in events} == task_ids
    assert all(event.data["category"] == "task" for event in events)


@pytest.mark.asyncio
async def test_stuck_agent_recovery_persists_its_event_and_state(watchdog_repo):
    repo, url = watchdog_repo
    team = await repo.create_team("stuck-agent", "coordinate")
    agent = await repo.create_agent(team.id, "worker", "developer")
    task = await repo.create_task(team.id, "运行中的任务", status="running", assigned_to=agent.id)
    await repo.update_agent(
        agent.id, status="busy", current_task=task.id,
        last_active_at=utc_now() - timedelta(minutes=31),
    )

    reader, events = await _patrol_and_read(repo, url)

    assert len(events) == 1
    assert events[0].type.value == "watchdog.agent_recovered"
    assert events[0].source == f"team:{team.id}"
    assert events[0].data["agent_id"] == agent.id
    assert events[0].data["reset_tasks"] == [task.id]
    recovered = await reader.get_agent(agent.id)
    assert recovered.status == AgentStatus.WAITING
    assert recovered.current_task is None
    reset_task = await reader.get_task(task.id)
    assert reset_task.status == TaskStatus.PENDING
    assert reset_task.assigned_to is None


@pytest.mark.asyncio
async def test_failed_task_retry_persists_its_event_and_state(watchdog_repo):
    repo, url = watchdog_repo
    team = await repo.create_team("retry", "coordinate")
    task = await repo.create_task(team.id, "失败的任务", status="failed")

    reader, events = await _patrol_and_read(repo, url)

    assert len(events) == 1
    assert events[0].type.value == "watchdog.task_retried"
    assert events[0].source == f"team:{team.id}"
    assert events[0].data["task_id"] == task.id
    assert events[0].data["retry_count"] == 1
    retried = await reader.get_task(task.id)
    assert retried.status == TaskStatus.PENDING
    assert retried.config["retry_count"] == 1


@pytest.mark.asyncio
async def test_retry_limit_persists_alert_and_failure_analysis(watchdog_repo):
    repo, url = watchdog_repo
    team = await repo.create_team("retry-limit", "coordinate")
    task = await repo.create_task(team.id, "反复失败的任务", status="failed", config={"retry_count": 2})

    reader, events = await _patrol_and_read(repo, url)

    assert len(events) == 1
    assert events[0].type.value == "watchdog.task_failed_permanently"
    assert events[0].source == f"task:{task.id}"
    assert events[0].data["task_id"] == task.id
    assert events[0].data["team_id"] == team.id
    assert events[0].data["retry_count"] == 2
    failed = await reader.get_task(task.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.config["retry_count"] == 2
    memos = await reader.list_task_memos(task.id)
    assert len(memos) == 1
    assert memos[0].meta["type"] == "failure_alchemy"
