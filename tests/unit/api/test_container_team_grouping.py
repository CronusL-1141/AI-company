"""同一个 CC 进程的多支容器队要能在展示层归成一组。

CC 的队目录在建队那一刻盖下 ``leadSessionId`` 后**永不更新**，而同一个进程换会话
（结束后重开、resume）时只有 ``~/.claude/sessions/<pid>.json`` 跟着改 ``sessionId``。
于是一个活进程在 OS 侧会留下多支容器队：老的 completed、新的 active。用户裁定
（2026-07-27）归属逻辑一律不动，只在展示层把它们合成一组。

合并只认**能证明的**关系，绝不认亲：
- 档① 建队时从注册表按 session_id 精确匹配盖下的 ``config.cc_pid``；
- 档② 现读注册表，owner_session_id 恰是某进程的当前会话；
- 都不成立 → 不知道，独立成行。

刻意不做 cwd + 启动时间窗匹配：本机实测 pid 32147 与 32220 在**同一个 cwd** 下
相隔 1.0 秒启动，队目录 createdAt 正好落在两者之间，近似匹配在真实数据上就是
二义的。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps, session_registry
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

LIVE_PID = 32220
SESSION_NOW = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
SESSION_OLD = "0def8f84-3b72-4b09-ae42-b13365ffeb65"


def _write_registry(tmp_path, monkeypatch, records: list[dict]) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for record in records:
        (sessions / f"{record['pid']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    monkeypatch.setattr(session_registry, "_sessions_dir", lambda: sessions)


def _record(pid: int, session_id: str) -> dict:
    return {
        "pid": pid,
        "sessionId": session_id,
        "cwd": "/Users/dev/Desktop/AI team OS",
        "startedAt": 1785116991072,
        "version": "2.1.219",
        "kind": "interactive",
        "entrypoint": "cli",
        "name": "ai-team-os-18",
        "status": "busy",
    }


@pytest.fixture()
def client():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = EventBus(repo=repo)
    deps._manager = TeamManager(repository=repo, memory=memory)

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    yield TestClient(app), repo

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None


class TestBirthStamp:
    """建容器队时把当时的 pid 记下来——注册表只答得出当前会话，事后再问就晚了。"""

    @pytest.mark.asyncio
    async def test_container_team_records_the_owning_process(
        self, db_repository, tmp_path, monkeypatch
    ):
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        translator = HookTranslator(repo=db_repository, event_bus=EventBus(repo=db_repository))

        team = await translator._find_or_create_session_team(SESSION_NOW, {})

        assert team.config["cc_pid"] == LIVE_PID
        assert team.config["owner_session_id"] == SESSION_NOW

    @pytest.mark.asyncio
    async def test_unregistered_session_is_left_blank_never_guessed(
        self, db_repository, tmp_path, monkeypatch
    ):
        """注册表里没有这条会话（老会话/测试/workflow 注入）就留空，不许猜。"""
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        translator = HookTranslator(repo=db_repository, event_bus=EventBus(repo=db_repository))

        team = await translator._find_or_create_session_team(SESSION_OLD, {})

        assert "cc_pid" not in team.config

    @pytest.mark.asyncio
    async def test_registry_failure_does_not_block_team_creation(
        self, db_repository, monkeypatch
    ):
        def boom():
            raise OSError("registry unreadable")

        monkeypatch.setattr(session_registry, "read_sessions", boom)
        translator = HookTranslator(repo=db_repository, event_bus=EventBus(repo=db_repository))

        team = await translator._find_or_create_session_team(SESSION_NOW, {})

        assert team.name == "session-80d0cc5e"
        assert "cc_pid" not in team.config


class TestListResolution:
    def test_stamped_pid_is_reported(self, client, tmp_path, monkeypatch):
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [])  # 注册表已无此进程，靠盖的章
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-0def8f84",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_OLD, "cc_pid": LIVE_PID},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] == LIVE_PID

    def test_live_session_resolves_through_the_registry(
        self, client, tmp_path, monkeypatch
    ):
        """存量队没盖过章，但它的会话正是某进程的当前会话——这条也算证据。"""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-80d0cc5e",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_NOW},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] == LIVE_PID

    def test_two_containers_of_one_process_share_a_pid(
        self, client, tmp_path, monkeypatch
    ):
        """正是要修的现象：一个活进程，两支容器队，合并后同组。"""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            repo.create_team(
                name="session-0def8f84",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_OLD, "cc_pid": LIVE_PID},
            )
        )
        loop.run_until_complete(
            repo.create_team(
                name="session-80d0cc5e",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_NOW},
            )
        )

        teams = client.get("/api/teams").json()["data"]

        assert {t["cc_pid"] for t in teams} == {LIVE_PID}

    def test_unprovable_container_stands_alone(
        self, client, tmp_path, monkeypatch
    ):
        """历史会话在注册表里查不到（进程换会话时原地改写，旧值不留痕）——留空。"""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-0def8f84",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_OLD},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] is None

    def test_non_container_teams_are_not_resolved(
        self, client, tmp_path, monkeypatch
    ):
        """workflow 队每运行一支，自有合集，不参与进程合并。"""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="workflow-wf_374d39b3",
                mode="coordinate",
                config={"kind": "workflow", "owner_session_id": SESSION_NOW},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] is None

    def test_registry_failure_still_lists_teams(self, client, monkeypatch):
        client, repo = client
        def boom():
            raise OSError("registry unreadable")

        monkeypatch.setattr(session_registry, "read_sessions", boom)
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-80d0cc5e",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_NOW},
            )
        )

        resp = client.get("/api/teams")

        assert resp.status_code == 200
        assert resp.json()["data"][0]["cc_pid"] is None
