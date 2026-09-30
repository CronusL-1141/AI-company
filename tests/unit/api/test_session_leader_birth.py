"""A CC session's Leader row is born with its first activity, not with an empty SessionStart.

`claude` followed by /resume first starts a throwaway session: SessionStart
(source=startup) under a one-off id, then - once the user has picked a
conversation, 5-15s later - SessionEnd for the one-off id and SessionStart
(source=resume) under the picked id. Nothing else is ever sent under the one-off
id (production, 09-23..09-30: 25 such sessions, session_start/session_end only,
no transcript). Creating the Leader at that SessionStart left a Leader row and a
session-<sid8> container team behind on every resume: 26 of the 101 CC Leader rows,
never measurable, and the rows the MCP server's stale session id bound task edges to.

At SessionStart a throwaway session and a fresh session nobody has typed into yet
look the same, so an empty conversation (source startup or clear) gets its row
from its first activity - any CC hook but SessionStart/SessionEnd - or, if it wrote
a transcript without sending one, at its SessionEnd.

Every assertion reads the rows back through the repository.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

ROOT = "/work/ai-team-os"
ONE_OFF = "91ca1de1-162f-47c5-a9e3-1510f4f7a667"
RESUMED = "39c7d5a3-52c6-4735-b866-1fa68229709a"
FRESH = "a1a1a1a1-0000-4000-8000-00000000000a"


@pytest_asyncio.fixture()
async def repo(tmp_path):
    # A file database: the concurrency case needs real connection interleaving.
    r = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'birth.sqlite'}")
    await r.init_db()
    await r.create_project(name="AI Team OS", root_path=ROOT)
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    t = HookTranslator(repo=repo, event_bus=EventBus(repo=repo))
    yield t
    await t.drain(5)


async def _hook(translator, event: str, session_id: str, **extra) -> dict:
    return await translator.handle_event(
        {"hook_event_name": event, "session_id": session_id, "cwd": ROOT, **extra}
    )


async def _session_rows(repo, session_id: str) -> tuple[list, list]:
    """(agents of the session, teams the session owns), both read from the database."""
    agents = await repo.find_agents_by_session(session_id)
    teams = [t for t in await repo.list_teams() if (t.config or {}).get("owner_session_id") == session_id]
    return agents, teams


def _tool(tool_use_id: str, **extra) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": "pwd", "description": "pwd"},
            "tool_use_id": tool_use_id, **extra}


# ------------------------------------------------------------------
# The throwaway session of `claude` + /resume leaves nothing behind
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_leaves_no_row_for_the_throwaway_session(translator, repo):
    await _hook(translator, "SessionStart", ONE_OFF, source="startup")
    await _hook(translator, "SessionEnd", ONE_OFF, reason="other",
                transcript_path=f"/nonexistent/{ONE_OFF}.jsonl")
    await _hook(translator, "SessionStart", RESUMED, source="resume")

    assert await _session_rows(repo, ONE_OFF) == ([], [])
    agents, teams = await _session_rows(repo, RESUMED)
    assert [(a.role, a.status.value, a.project_id is not None) for a in agents] == [("leader", "busy", True)]
    assert [(t.name, t.status) for t in teams] == [(f"session-{RESUMED[:8]}", "active")]


@pytest.mark.parametrize("source", ["startup", "clear"])
@pytest.mark.asyncio
async def test_empty_session_opened_and_closed_leaves_no_row(translator, repo, source):
    await _hook(translator, "SessionStart", FRESH, source=source)
    assert await _session_rows(repo, FRESH) == ([], [])

    await _hook(translator, "SessionEnd", FRESH, reason="prompt_input_exit",
                transcript_path=f"/nonexistent/{FRESH}.jsonl")

    assert await _session_rows(repo, FRESH) == ([], [])


@pytest.mark.asyncio
async def test_startup_of_a_session_that_has_a_leader_reuses_it(translator, repo):
    """An empty-conversation start defers only a Leader that does not exist yet.

    A session id can start again with source=startup (``claude --session-id``, a
    redelivered SessionStart): its existing row is revived, not left offline in a
    closed team, and no second row is born.
    """
    await _hook(translator, "SessionStart", FRESH, source="startup")
    await _hook(translator, "Stop", FRESH, stop_hook_active=False)
    await _hook(translator, "SessionEnd", FRESH, reason="prompt_input_exit",
                transcript_path=f"/nonexistent/{FRESH}.jsonl")
    (leader,), (team,) = await _session_rows(repo, FRESH)
    assert (leader.status.value, team.status) == ("offline", "completed")

    result = await _hook(translator, "SessionStart", FRESH, source="startup")

    agents, teams = await _session_rows(repo, FRESH)
    assert [(a.id, a.role, a.status.value) for a in agents] == [(leader.id, "leader", "busy")]
    assert [(t.id, t.status) for t in teams] == [(team.id, "active")]
    assert result["leader"] == "Leader" and result["leader_usage_skip"] != "leader-deferred"


@pytest.mark.parametrize("source", ["resume", "compact", "fork", None])
@pytest.mark.asyncio
async def test_session_with_a_conversation_gets_its_leader_at_session_start(translator, repo, source):
    await _hook(translator, "SessionStart", FRESH, **({"source": source} if source else {}))

    agents, teams = await _session_rows(repo, FRESH)
    assert [a.role for a in agents] == ["leader"]
    assert len(teams) == 1


# ------------------------------------------------------------------
# The first activity brings the Leader in
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, extra",
    [
        ("PreToolUse", _tool("toolu_first")),
        ("PostToolUse", _tool("toolu_first", tool_response={"stdout": "/work"})),
        ("Stop", {"stop_hook_active": False}),
        ("PostCompact", {"trigger": "manual"}),
        ("TaskCreated", {"task_id": "1", "task_subject": "first"}),
    ],
    ids=["pre-tool", "post-tool", "stop", "post-compact", "task-created"],
)
@pytest.mark.asyncio
async def test_first_activity_creates_the_leader_bound_and_housed(translator, repo, event, extra):
    await _hook(translator, "SessionStart", FRESH, source="startup")
    assert await _session_rows(repo, FRESH) == ([], [])

    await _hook(translator, event, FRESH, **extra)

    agents, teams = await _session_rows(repo, FRESH)
    assert [(a.role, a.status.value) for a in agents] == [("leader", "busy")]
    leader = agents[0]
    project = (await repo.list_projects())[0]
    assert leader.project_id == project.id and leader.last_active_at is not None
    assert [(t.name, t.status, t.leader_agent_id) for t in teams] == [
        (f"session-{FRESH[:8]}", "active", leader.id)]
    assert leader.team_id == teams[0].id


@pytest.mark.asyncio
async def test_a_subagent_as_first_activity_lands_in_the_new_leaders_team(translator, repo):
    await _hook(translator, "SessionStart", FRESH, source="startup")

    await _hook(translator, "SubagentStart", FRESH, agent_id="toolu_sub", agent_type="attr-sub")

    agents, teams = await _session_rows(repo, FRESH)
    assert sorted((a.role, a.name) for a in agents) == [("attr-sub", "attr-sub"), ("leader", "Leader")]
    assert {a.team_id for a in agents} == {teams[0].id}


@pytest.mark.asyncio
async def test_main_session_tool_calls_are_the_leaders(translator, repo):
    """Without the Leader, the first tool call would be attributed to no one."""
    await _hook(translator, "SessionStart", FRESH, source="startup")

    await _hook(translator, "PreToolUse", FRESH, **_tool("toolu_main"))

    (leader,) = (await _session_rows(repo, FRESH))[0]
    activities = await repo.list_activities(leader.id)
    assert [a.tool_name for a in activities] == ["Bash"]


@pytest.mark.asyncio
async def test_parallel_first_tool_calls_create_one_leader_and_one_team(translator, repo):
    """A first turn often runs tools in parallel: each hook is the first activity."""
    await _hook(translator, "SessionStart", FRESH, source="startup")

    await asyncio.gather(*(
        _hook(translator, "PreToolUse", FRESH, **_tool(f"toolu_{i}")) for i in range(48)
    ))

    agents, teams = await _session_rows(repo, FRESH)
    assert [a.role for a in agents] == ["leader"]
    assert len(teams) == 1


@pytest.mark.asyncio
async def test_a_redelivered_first_activity_does_not_claim_the_session_is_live(translator, repo):
    await _hook(translator, "SessionStart", FRESH, source="startup")
    origin = (utc_now().replace(microsecond=0)).isoformat()

    await _hook(translator, "PreToolUse", FRESH, **_tool("toolu_old"),
                _hook_replay={"origin_at": origin, "attempt": 1})

    (leader,) = (await _session_rows(repo, FRESH))[0]
    assert (leader.status.value, leader.last_active_at) == ("waiting", None)

    await _hook(translator, "PreToolUse", FRESH, **_tool("toolu_live"))

    (leader,) = (await _session_rows(repo, FRESH))[0]
    assert leader.status.value == "busy"


@pytest.mark.asyncio
async def test_activity_outside_every_project_creates_nothing(translator, repo):
    await translator.handle_event({"hook_event_name": "SessionStart", "session_id": FRESH,
                                   "cwd": "/elsewhere", "source": "startup"})
    await translator.handle_event({"hook_event_name": "PreToolUse", "session_id": FRESH,
                                   "cwd": "/elsewhere", **_tool("toolu_x")})

    assert await _session_rows(repo, FRESH) == ([], [])


# ------------------------------------------------------------------
# A conversation that sent no activity hook still gets its row at SessionEnd
# ------------------------------------------------------------------


def _transcript(tmp_path: Path, session_id: str) -> Path:
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(json.dumps({"type": "user", "sessionId": session_id,
                                "message": {"role": "user", "content": "hello"}}) + "\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_interrupted_only_turn_gets_its_leader_at_session_end(translator, repo, tmp_path):
    await _hook(translator, "SessionStart", FRESH, source="startup")
    transcript = _transcript(tmp_path, FRESH)

    result = await _hook(translator, "SessionEnd", FRESH, reason="prompt_input_exit",
                         transcript_path=str(transcript))

    agents, teams = await _session_rows(repo, FRESH)
    assert [(a.role, a.status.value) for a in agents] == [("leader", "offline")]
    assert [t.status for t in teams] == ["completed"]
    assert result["leader_usage_skip"] != "no-leader-row"


# ------------------------------------------------------------------
# Codex keeps its own path
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_events_never_create_a_cc_leader(translator, repo, tmp_path):
    rollout = _transcript(tmp_path, FRESH)
    await _hook(translator, "PreToolUse", FRESH, harness="codex", **_tool("call_1"))
    await _hook(translator, "Stop", FRESH, harness="codex")
    await _hook(translator, "SessionEnd", FRESH, harness="codex", transcript_path=str(rollout))

    assert [a for a in (await _session_rows(repo, FRESH))[0] if a.role == "leader"] == []
