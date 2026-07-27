"""压缩检查点：PreCompact 定格作战态，压缩后的 SessionStart 递回。

Q6 裁定 A（2026-07-27）。改造前这个 hook 只往一个全仓没人读的 jsonl 追加时间
戳；本文件把"检查点真的存了、真的取得回来、内容真的是库里的实况"钉成契约。

刻意不存 CC 的 compact_summary 正文：那段摘要压缩后本来就在模型上下文里，
再存一份既重复、又会把大段对话灌进刚瘦身四成的 events 表。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.api import compact_checkpoint
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def client(repo):
    from fastapi.testclient import TestClient

    from aiteam.api import deps
    from aiteam.api.app import create_app
    from aiteam.api.event_bus import EventBus
    from aiteam.api.hook_translator import HookTranslator

    # get_event_bus / get_hook_translator hold their own repository, so
    # overriding get_repository alone would send the writes to the real DB while
    # the reads look at the in-memory one — the round-trip would fail for a
    # reason that has nothing to do with the code under test.
    bus = EventBus(repo=repo)
    app = create_app()
    app.dependency_overrides[deps.get_repository] = lambda: repo
    app.dependency_overrides[deps.get_scoped_repository] = lambda: repo
    app.dependency_overrides[deps.get_event_bus] = lambda: bus
    app.dependency_overrides[deps.get_hook_translator] = lambda: HookTranslator(
        repo=repo, event_bus=bus
    )
    with TestClient(app) as c:
        yield c


async def _populate(repo):
    project = await repo.create_project(name="AI team OS", root_path="/tmp/os")
    team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
    leader = await repo.create_agent(
        team_id=team.id, name="Leader", role="leader", source="hook", session_id=SESSION
    )
    await repo.update_agent(leader.id, project_id=project.id, model="claude-opus-5")
    worker = await repo.create_agent(
        team_id=team.id, name="batch9", role="worker", source="hook", session_id=SESSION
    )
    await repo.update_agent(worker.id, status="busy", current_task="修 cc_task_bridge")
    gone = await repo.create_agent(
        team_id=team.id, name="old-worker", role="worker", source="hook", session_id=SESSION
    )
    await repo.update_agent(gone.id, status="offline")
    await repo.create_task(
        team_id=None, title="在跑的活", project_id=project.id, status="running"
    )
    await repo.create_task(team_id=None, title="待办", project_id=project.id)
    await repo.create_briefing(title="要不要换存活判据", description="", project_id=project.id)
    return project


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_is_the_live_db_state(self, repo):
        project = await _populate(repo)
        snap = await compact_checkpoint.build_snapshot(repo, SESSION, "/tmp/os")

        assert snap["leader"]["name"] == "Leader"
        assert snap["project_id"] == project.id
        assert [a["name"] for a in snap["agents"]] == ["batch9"]  # offline 的不算在飞
        assert snap["agents"][0]["current_task"] == "修 cc_task_bridge"
        assert {t["title"] for t in snap["open_tasks"]} == {"在跑的活", "待办"}
        assert [b["title"] for b in snap["pending_briefings"]] == ["要不要换存活判据"]

    @pytest.mark.asyncio
    async def test_unknown_session_yields_an_empty_but_valid_snapshot(self, repo):
        snap = await compact_checkpoint.build_snapshot(repo, "nobody", "")
        assert snap["agents"] == []
        assert snap["open_tasks"] == []
        assert snap["leader"] is None


class TestRender:
    def test_empty_snapshot_injects_nothing(self):
        assert compact_checkpoint.render({"agents": [], "open_tasks": []}) == ""

    def test_rendered_block_names_what_was_in_flight(self):
        text = compact_checkpoint.render(
            {
                "agents": [{"name": "batch9", "status": "busy", "current_task": "修桥", "ctx_pct": 0.42}],
                "open_tasks": [{"status": "running", "title": "在跑的活", "assigned_to": "batch9"}],
                "pending_briefings": [{"id": "abcdef1234", "title": "换判据?", "urgency": "low"}],
            }
        )
        assert "batch9" in text and "修桥" in text and "42%" in text
        assert "在跑的活" in text and "@batch9" in text
        assert "换判据?" in text and "abcdef12" in text
        # 明确告诉压缩后的 Leader 这是实况而非回忆，并指向现查的工具
        assert "不是回忆" in text and "task_memo_read" in text


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_save_then_read_round_trip(self, client, repo):
        await _populate(repo)
        saved = client.post(
            "/api/hooks/compact-checkpoint",
            json={"session_id": SESSION, "trigger": "auto", "cwd": "/tmp/os"},
        ).json()
        assert saved["data"]["trigger"] == "auto"

        got = client.get(f"/api/hooks/compact-checkpoint?session_id={SESSION}").json()
        assert got["found"] is True
        assert "batch9" in got["text"]
        assert got["saved_at"]

    @pytest.mark.asyncio
    async def test_reading_without_a_checkpoint_is_found_false(self, client):
        got = client.get("/api/hooks/compact-checkpoint?session_id=never").json()
        assert got["found"] is False
        assert got["text"] == ""

    @pytest.mark.asyncio
    async def test_latest_checkpoint_wins(self, client, repo):
        await _populate(repo)
        client.post(
            "/api/hooks/compact-checkpoint",
            json={"session_id": SESSION, "trigger": "manual", "cwd": "/tmp/os"},
        )
        client.post(
            "/api/hooks/compact-checkpoint",
            json={"session_id": SESSION, "trigger": "auto", "cwd": "/tmp/os"},
        )
        got = client.get(f"/api/hooks/compact-checkpoint?session_id={SESSION}").json()
        assert got["data"]["trigger"] == "auto"


class TestPostCompact:
    @pytest.mark.asyncio
    async def test_records_that_compaction_really_happened(self, client, repo):
        client.post(
            "/api/hooks/event",
            json={
                "hook_event_name": "PostCompact",
                "session_id": SESSION,
                "trigger": "auto",
                "compact_summary": "x" * 4321,
            },
        )
        events = await repo.list_events(event_type="session.compact_completed", limit=1)
        assert len(events) == 1
        assert events[0].data["summary_chars"] == 4321
        # 摘要正文不入库：压缩后它本来就在模型上下文里，存一份是重复也是膨胀
        assert "compact_summary" not in events[0].data
        assert "xxxx" not in str(events[0].data)
