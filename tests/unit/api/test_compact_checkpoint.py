"""压缩检查点：PreCompact 定格作战态，压缩后的 SessionStart 递回。

Q6 裁定 A（2026-07-27）。改造前这个 hook 只往一个全仓没人读的 jsonl 追加时间
戳；本文件把"检查点真的存了、真的取得回来、内容真的是库里的实况"钉成契约。

刻意不存 CC 的 compact_summary 正文：那段摘要压缩后本来就在模型上下文里，
再存一份既重复、又会把大段对话灌进刚瘦身四成的 events 表。
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from aiteam.api import agent_context, compact_checkpoint
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
        assert snap["agents"][0]["ctx_window"] is None
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
                "agents": [{
                    "name": "batch9", "status": "busy", "current_task": "修桥",
                    "ctx_pct": 42.0, "ctx_window": 1_000_000,
                }],
                "open_tasks": [{"status": "running", "title": "在跑的活", "assigned_to": "batch9"}],
                "pending_briefings": [{"id": "abcdef1234", "title": "换判据?", "urgency": "low"}],
            }
        )
        assert "batch9" in text and "修桥" in text and "42%" in text
        assert "换判据?" in text and "abcdef12" in text
        # 明确告诉压缩后的 Leader 这是实况而非回忆，并指向现查的工具
        assert "不是回忆" in text and "task_memo_read" in text

    @pytest.mark.parametrize(
        "context",
        [
            pytest.param({"ctx_pct": 42.0}, id="legacy-without-window"),
            pytest.param({"ctx_pct": 42.0, "ctx_window": None}, id="unknown-window"),
            pytest.param({"ctx_pct": 42.0, "ctx_window": 0}, id="zero-window"),
            pytest.param({"ctx_pct": 42.0, "ctx_window": -1}, id="negative-window"),
            pytest.param({"ctx_pct": None, "ctx_window": 1_000_000}, id="unknown-pct"),
        ],
    )
    def test_unknown_context_does_not_invent_a_percentage(self, context):
        text = compact_checkpoint.render({"agents": [{"name": "batch9", "status": "busy", **context}]})
        assert "batch9" in text
        assert "上下文" not in text

    def test_open_tasks_are_left_to_the_briefing_digest(self):
        """The snapshot's open_tasks were the newest 10 running rows, shown as if the whole
        wall; the briefing injected with the checkpoint carries the live digest instead."""
        tasks_only = {"open_tasks": [{"status": "running", "title": "在跑的活", "assigned_to": "b"}]}
        assert compact_checkpoint.render(tasks_only) == ""
        text = compact_checkpoint.render({**tasks_only, "agents": [{"name": "b", "status": "busy"}]})
        assert "在跑的活" not in text and "未完成任务" not in text
        assert "任务墙摘要" in text and "task_list_project" in text
        # The tool the old closing line named never existed.
        assert "taskwall_view" not in text


class TestEndpoints:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "window,tokens,pct,rendered",
        [
            pytest.param(None, 420_000, 42.0, "42%", id="default-window"),
            pytest.param(200_000, 29_400, 14.7, "15%", id="reported-1470-percent"),
            pytest.param(200_000, 55_800, 27.9, "28%", id="reported-2790-percent"),
            pytest.param(200_000, 0, 0.0, "0%", id="measured-zero"),
            pytest.param(200_000, 250_000, 125.0, "125%", id="over-window-is-not-clamped"),
        ],
    )
    async def test_measured_context_survives_db_and_checkpoint_round_trip(
        self, client, repo, tmp_path, monkeypatch, window, tokens, pct, rendered
    ):
        if window is None:
            monkeypatch.delenv("CLAUDE_CONTEXT_SIZE", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", str(window))
        transcript = tmp_path / "agent-measured.jsonl"
        part = tokens // 4
        usage_rows = [
            {"input_tokens": 999_999},  # Only the last assistant usage is the numerator.
            {
                "input_tokens": part,
                "cache_creation_input_tokens": part,
                "cache_read_input_tokens": part,
                "output_tokens": tokens - 3 * part,
            },
        ]
        transcript.write_text(
            "\n".join(
                json.dumps({"type": "assistant", "message": {"usage": usage}})
                for usage in usage_rows
            ),
            encoding="utf-8",
        )
        measured = agent_context.measure(transcript)
        assert measured is not None
        assert (measured["ctx_tokens"], measured["ctx_window"], measured["ctx_pct"]) == (
            tokens, window or 1_000_000, pct
        )
        await _populate(repo)
        worker = next(a for a in await repo.find_agents_by_session(SESSION) if a.name == "batch9")
        await repo.update_agent(worker.id, **measured)
        stored = await repo.get_agent(worker.id)
        assert (stored.ctx_tokens, stored.ctx_window, stored.ctx_pct) == (
            tokens, window or 1_000_000, pct
        )

        saved = client.post(
            "/api/hooks/compact-checkpoint",
            json={"session_id": SESSION, "trigger": "auto", "cwd": "/tmp/os"},
        )
        assert saved.status_code == 200
        # Read the event back in a separate request; do not assert on the in-memory snapshot.
        got = client.get(f"/api/hooks/compact-checkpoint?session_id={SESSION}").json()
        context = got["data"]["agents"][0]
        assert context["ctx_pct"] == pct
        assert context["ctx_window"] == (window or 1_000_000)
        assert f"（上下文 {rendered}）" in got["text"]

    @pytest.mark.asyncio
    async def test_save_then_read_round_trip(self, client, repo):
        await _populate(repo)
        worker = next(a for a in await repo.find_agents_by_session(SESSION) if a.name == "batch9")
        # A legacy percentage without its denominator must stay unknown through both DB reads.
        await repo.update_agent(worker.id, ctx_pct=42.0)
        saved = client.post(
            "/api/hooks/compact-checkpoint",
            json={"session_id": SESSION, "trigger": "auto", "cwd": "/tmp/os"},
        ).json()
        assert saved["data"]["trigger"] == "auto"

        got = client.get(f"/api/hooks/compact-checkpoint?session_id={SESSION}").json()
        assert got["found"] is True
        assert "batch9" in got["text"]
        assert got["data"]["agents"][0]["ctx_window"] is None
        assert "上下文" not in got["text"]
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
