"""CC 会话注册表桥接 + 存活判据双轨观察（C13）。

``~/.claude/sessions/<pid>.json`` 是 CC 自己维护的进程级会话登记，比 OS 现有
的两路信号都更直接：带真实 pid 与 idle/busy 状态，且同一进程换会话时只有它
跟着改 sessionId（队目录的 leadSessionId 在建队那刻盖章后不再变）。

本批只把它接进来**并行观察**，不参与任何下线决策——切换主判据是另一个需要
证据支撑的决定。这些用例把"只观察"这条纪律钉死。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.api import session_registry
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import from_timestamp
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

LIVE_PID = 32220
DEAD_PID = 999_999
SESSION_LIVE = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
SESSION_DEAD = "0def8f84-3b72-4b09-ae42-b13365ffeb65"


def _write_registry(tmp_path, monkeypatch, records: list[dict]):
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for record in records:
        (sessions / f"{record['pid']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    monkeypatch.setattr(session_registry, "_sessions_dir", lambda: sessions)
    return sessions


def _record(pid: int, session_id: str, **extra) -> dict:
    base = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": "/Users/dev/Desktop/AI team OS",
        "startedAt": 1785116991072,
        "procStart": "Mon Jul 27 01:49:50 2026",
        "version": "2.1.219",
        "kind": "interactive",
        "entrypoint": "cli",
        "name": "ai-team-os-18",
        "status": "busy",
        "updatedAt": 1785143850087,
        "statusUpdatedAt": 1785143850087,
    }
    base.update(extra)
    return base


class TestRegistryReading:
    def test_reads_the_shape_cc_actually_writes(self, tmp_path, monkeypatch):
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_LIVE)])
        (record,) = session_registry.read_sessions()
        assert record.pid == LIVE_PID
        assert record.session_id == SESSION_LIVE
        assert record.status == "busy"
        assert record.kind == "interactive"
        assert record.name == "ai-team-os-18"
        # startedAt is epoch ms and agrees with `ps` local time (09:49:51),
        # unlike procStart which CC renders in UTC.
        assert record.started_at == from_timestamp(1785116991072 / 1000)

    def test_accepts_iso_timestamps_too(self, tmp_path, monkeypatch):
        """CC has two code paths: Date.now() epoch ms and toISOString()."""
        _write_registry(
            tmp_path,
            monkeypatch,
            [_record(LIVE_PID, SESSION_LIVE, updatedAt="2026-07-27T09:17:30.087Z")],
        )
        (record,) = session_registry.read_sessions()
        assert record.updated_at == datetime(2026, 7, 27, 9, 17, 30, 87000, tzinfo=UTC)

    def test_corrupt_file_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        sessions = _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_LIVE)])
        (sessions / "666.json").write_text("{not json", encoding="utf-8")
        assert [r.pid for r in session_registry.read_sessions()] == [LIVE_PID]

    def test_missing_directory_reads_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_registry, "_sessions_dir", lambda: tmp_path / "nope")
        assert session_registry.read_sessions() == []

    def test_cwd_filter_respects_path_boundaries(self, tmp_path, monkeypatch):
        _write_registry(
            tmp_path,
            monkeypatch,
            [
                _record(1, "a", cwd="/Users/dev/Desktop/AI team OS"),
                _record(2, "b", cwd="/Users/dev/Desktop/AI team OS/dashboard"),
                # Same prefix, different project — must not be swept in.
                _record(3, "c", cwd="/Users/dev/Desktop/AI team OS-backup"),
            ],
        )
        found = session_registry.sessions_for_cwd("/Users/dev/Desktop/AI team OS")
        assert {r.session_id for r in found} == {"a", "b"}


class TestLivenessVerdict:
    def test_unregistered_session_is_unknown_not_dead(self, tmp_path, monkeypatch):
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_LIVE)])
        assert session_registry.session_alive(SESSION_DEAD) is None

    def test_live_and_dead_pids_are_distinguished(self, tmp_path, monkeypatch):
        _write_registry(
            tmp_path,
            monkeypatch,
            [_record(LIVE_PID, SESSION_LIVE), _record(DEAD_PID, SESSION_DEAD)],
        )
        monkeypatch.setattr(
            session_registry, "process_alive", lambda pid: pid == LIVE_PID
        )
        assert session_registry.session_alive(SESSION_LIVE) is True
        assert session_registry.session_alive(SESSION_DEAD) is False

    def test_duplicate_session_id_prefers_the_live_process(self, tmp_path, monkeypatch):
        _write_registry(
            tmp_path,
            monkeypatch,
            [_record(DEAD_PID, SESSION_LIVE), _record(LIVE_PID, SESSION_LIVE)],
        )
        monkeypatch.setattr(
            session_registry, "process_alive", lambda pid: pid == LIVE_PID
        )
        assert session_registry.find_session(SESSION_LIVE).pid == LIVE_PID

    def test_our_own_process_reads_as_alive(self):
        import os

        assert session_registry.process_alive(os.getpid()) is True
        assert session_registry.process_alive(0) is False


class _RecordingBus:
    """Stub bus that keeps the real bus's type contract.

    The first version of this stub accepted any string, so the unit tests went
    green while the live API rejected the new events with
    "'cc.teammate_idle' is not a valid EventType" — the enum entry was missing.
    Validating here means a stub can never be more permissive than production.
    """

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event_type, source, data, **_kwargs):
        EventType(event_type)
        self.events.append((event_type, source, data))


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


class TestDualTrackObservation:
    async def _agent(self, repo, session_id: str):
        project = await repo.create_project(
            name="AI team OS", root_path="/Users/dev/Desktop/AI team OS"
        )
        team = await repo.create_team(name="session-x", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", session_id=session_id,
        )
        await repo.update_agent(agent.id, project_id=project.id)
        return await repo.get_agent(agent.id)

    @pytest.mark.asyncio
    async def test_divergence_is_recorded_but_never_decides(self, repo, monkeypatch):
        """Registry says alive, mtime says dead — the mtime verdict still stands."""
        bus = _RecordingBus()
        reaper = StateReaper(repo=repo, event_bus=bus)
        agent = await self._agent(repo, SESSION_LIVE)
        monkeypatch.setattr(session_registry, "session_alive", lambda _sid: True)

        verdict = await reaper._agent_session_live(agent, repo)

        assert verdict is False  # no transcript on disk -> mtime track says dead
        (event_type, _source, data), = bus.events
        assert event_type == "session.liveness_divergence"
        assert data["mtime_live"] is False
        assert data["registry_live"] is True
        assert data["decided_by"] == "mtime"

    @pytest.mark.asyncio
    async def test_agreement_emits_nothing(self, repo, monkeypatch):
        bus = _RecordingBus()
        reaper = StateReaper(repo=repo, event_bus=bus)
        agent = await self._agent(repo, SESSION_LIVE)
        monkeypatch.setattr(session_registry, "session_alive", lambda _sid: False)

        await reaper._agent_session_live(agent, repo)

        assert bus.events == []

    @pytest.mark.asyncio
    async def test_unregistered_session_is_not_a_disagreement(self, repo, monkeypatch):
        bus = _RecordingBus()
        reaper = StateReaper(repo=repo, event_bus=bus)
        agent = await self._agent(repo, SESSION_LIVE)
        monkeypatch.setattr(session_registry, "session_alive", lambda _sid: None)

        await reaper._agent_session_live(agent, repo)

        assert bus.events == []

    @pytest.mark.asyncio
    async def test_divergence_is_throttled_per_session(self, repo, monkeypatch):
        """Observation must not become the event flood this batch is removing."""
        bus = _RecordingBus()
        reaper = StateReaper(repo=repo, event_bus=bus)
        agent = await self._agent(repo, SESSION_LIVE)
        monkeypatch.setattr(session_registry, "session_alive", lambda _sid: True)

        for _ in range(5):
            await reaper._agent_session_live(agent, repo)

        assert len(bus.events) == 1


class TestTeammateIdleIsObservationOnly:
    @pytest.mark.asyncio
    async def test_idle_signal_records_both_views_and_changes_nothing(self, repo):
        bus = _RecordingBus()
        translator = HookTranslator(repo=repo, event_bus=bus)
        team = await repo.create_team(name="session-x", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", session_id=SESSION_LIVE,
        )
        await repo.update_agent(agent.id, status="busy")

        result = await translator.handle_event(
            {
                "hook_event_name": "TeammateIdle",
                "session_id": SESSION_LIVE,
                "teammate_name": "worker",
                "team_name": "session-0def8f84",
            }
        )

        assert result["status"] == "observed"
        # CC's "idle" is "turn finished", not "gone" — status must be untouched.
        assert (await repo.get_agent(agent.id)).status.value == "busy"
        (event_type, _source, data), = bus.events
        assert event_type == "cc.teammate_idle"
        assert data["teammate_name"] == "worker"
        assert data["agent_id"] == agent.id
        assert data["os_status"] == "busy"
        assert data["observed_only"] is True

    @pytest.mark.asyncio
    async def test_idle_signal_is_never_pinned_on_the_leader(self, repo):
        """Live check caught this: a teammate's idle signal landed on the Leader row."""
        bus = _RecordingBus()
        translator = HookTranslator(repo=repo, event_bus=bus)
        team = await repo.create_team(name="session-x", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="hook", session_id=SESSION_LIVE,
        )

        await translator.handle_event(
            {
                "hook_event_name": "TeammateIdle",
                "session_id": SESSION_LIVE,
                "teammate_name": "some-worker",
            }
        )

        (_type, _source, data), = bus.events
        assert data["agent_id"] != leader.id
        assert data["agent_id"] == ""

    @pytest.mark.asyncio
    async def test_unknown_teammate_still_records_the_signal(self, repo):
        bus = _RecordingBus()
        translator = HookTranslator(repo=repo, event_bus=bus)

        await translator.handle_event(
            {
                "hook_event_name": "TeammateIdle",
                "session_id": SESSION_LIVE,
                "teammate_name": "never-registered",
            }
        )

        (_type, _source, data), = bus.events
        assert data["agent_id"] == ""
        assert data["os_status"] == ""


def test_real_registry_matches_the_live_machine():
    """Smoke: whatever this machine has must parse without exploding."""
    for record in session_registry.read_sessions():
        assert record.pid > 0
        assert isinstance(session_registry.process_alive(record.pid), bool)
