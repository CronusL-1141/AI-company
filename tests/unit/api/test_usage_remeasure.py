"""The reaper's second chance to measure CC sub-agent usage (P-5; aiteam.api.usage_remeasure).

SubagentStop was the only measurement: a row whose Stop never came (killed
workflow, crashed session) stayed unmeasured, and a parse that raced CC's last
write stayed short. Each reaper cycle now lands the previous parse batch and starts
the next one; the parse runs in a child interpreter.

Every assertion reads the row back through the repository; transcripts are real
files in the shape CC writes (several lines per requestId, output growing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import psutil
import pytest
import pytest_asyncio

from aiteam.api import agent_context, usage_remeasure
from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import utc_now
from aiteam.services.token_attribution import parse_transcript_usage, unmeasurable_reason
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

ROOT = "/work/ai-team-os"
SESSION = "39c7d5a3-52c6-4735-b866-1fa68229709a"


@pytest.fixture()
def projects(tmp_path, monkeypatch):
    base = tmp_path / "projects"
    base.mkdir()
    monkeypatch.setattr(agent_context, "_projects_dir", lambda: base)
    return base


@pytest_asyncio.fixture()
async def repo(tmp_path):
    r = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'remeasure.sqlite'}")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def reaper(repo):
    r = StateReaper(repo, EventBus(repo=repo))
    yield r
    await r.stop()


def _turn(request_id: str, output_tokens: int) -> list[dict]:
    """One API call as CC writes it: two content-block lines, output growing."""
    lines = []
    for output in (1, output_tokens):
        lines.append({"type": "assistant", "requestId": request_id, "timestamp": "2026-09-30T05:00:00.000Z",
                      "message": {"id": f"msg-{request_id}", "role": "assistant", "model": "claude-opus-5-5",
                                  "content": [{"type": "text", "text": "x"}],
                                  "usage": {"input_tokens": 10, "output_tokens": output,
                                            "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100}}})
    return lines


def _write(path: Path, *turns: tuple[str, int], append: bool = False, age: float = 60.0) -> Path:
    """Write turns and date the file ``age`` seconds back (past the quiet window)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        for request_id, output in turns:
            for line in _turn(request_id, output):
                fh.write(json.dumps(line) + "\n")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


async def _subagent(repo, projects, cc_id: str, *, status: str = "offline", role: str = "workflow-subagent",
                    harness: str | None = None, store_path: bool = True, turns=(("r1", 100), ("r2", 200))):
    team = await repo.get_team_by_name("workflow-wf_test") or await repo.create_team(
        name="workflow-wf_test", mode="coordinate")
    path = projects / "-work-ai-team-os" / SESSION / "subagents" / "workflows" / "wf_test" / f"agent-{cc_id}.jsonl"
    if turns:
        _write(path, *turns)
    agent = await repo.create_agent(team_id=team.id, name=f"wf-{cc_id}", role=role, source="hook",
                                    session_id=SESSION, cc_tool_use_id=cc_id,
                                    transcript_path=str(path) if store_path else None)
    fields = {"status": status, "last_active_at": utc_now() - timedelta(hours=1)}
    if harness:
        fields["harness"] = harness
    await repo.update_agent(agent.id, **fields)
    return agent.id, path


async def _cycle(reaper: StateReaper, repo) -> None:
    """One reaper cycle's re-measure step, then wait for the batch it started."""
    await reaper._remeasure_subagent_usage(repo)
    if reaper._usage_batch is not None:
        _, batch = reaper._usage_batch
        await asyncio.wait({batch}, timeout=120)  # its outcome is the next cycle's to land


def _layers(agent) -> tuple:
    return (agent.input_tokens, agent.output_tokens, agent.cache_creation_tokens, agent.cache_read_tokens)


# ------------------------------------------------------------------
# What gets measured
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_whose_stop_never_came_is_measured_in_two_cycles(reaper, repo, projects):
    agent_id, path = await _subagent(repo, projects, "cc-killed", store_path=False)
    expected = parse_transcript_usage(path)

    await _cycle(reaper, repo)  # picks it and parses in the child
    assert (await repo.get_agent(agent_id)).tokens_measured_at is None
    await _cycle(reaper, repo)  # lands

    row = await repo.get_agent(agent_id)
    assert _layers(row) == (expected["input_tokens"], expected["output_tokens"],
                            expected["cache_creation_tokens"], expected["cache_read_tokens"])
    assert (row.tokens_source, row.model, row.transcript_path) == ("transcript", "claude-opus-5-5", str(path))
    assert row.tokens_measured_at is not None
    assert reaper._usage_batch is None  # measured and up to date: nothing more to parse


@pytest.mark.asyncio
async def test_killed_workflow_agent_is_measured_by_full_reaper_cycles(reaper, repo, projects):
    """The whole cycle: the heartbeat check offlines the agent, the re-measure step picks it up."""
    agent_id, path = await _subagent(repo, projects, "cc-wf-killed", status="busy")
    await repo.update_agent(agent_id, last_active_at=utc_now() - timedelta(hours=2))  # killed 2 h ago

    for _ in range(2):
        await reaper._reap_cycle_for_repo(repo)
        if reaper._usage_batch is not None:
            await asyncio.wait({reaper._usage_batch[1]}, timeout=120)

    row = await repo.get_agent(agent_id)
    assert row.status == "offline"
    assert row.output_tokens == parse_transcript_usage(path)["output_tokens"] == 300


@pytest.mark.asyncio
async def test_short_measurement_is_corrected_when_the_file_grew_after_it(reaper, repo, projects):
    agent_id, path = await _subagent(repo, projects, "cc-short", turns=(("r1", 22),))
    await repo.update_agent(agent_id, input_tokens=10, output_tokens=22, cache_creation_tokens=5,
                            cache_read_tokens=100, tokens_measured_at=utc_now() - timedelta(minutes=30),
                            tokens_source="transcript")
    _write(path, ("r1", 9424), ("r2", 50), append=True)  # the rest landed after the parse

    await _cycle(reaper, repo)
    await _cycle(reaper, repo)

    row = await repo.get_agent(agent_id)
    assert row.output_tokens == 9424 + 50 == parse_transcript_usage(path)["output_tokens"]


def _write_lines(path: Path, *lines: dict) -> Path:
    """A transcript of the given lines as CC writes them (compact JSON), quiet for a minute."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines), encoding="utf-8")
    stamp = time.time() - 60
    os.utime(path, (stamp, stamp))
    return path


_USER = {"type": "user", "timestamp": "2026-09-30T05:00:00.000Z", "message": {"role": "user", "content": "go"}}
# What CC writes when a call is rate limited: a local line, no billed call behind it.
_SYNTHETIC = {"type": "assistant", "requestId": "", "isApiErrorMessage": True, "error": "rate_limit",
              "timestamp": "2026-09-30T05:00:01.000Z",
              "message": {"id": "msg-synthetic", "role": "assistant", "model": "<synthetic>",
                          "content": [{"type": "text", "text": "API Error: rate limited"}],
                          "usage": {"input_tokens": 0, "output_tokens": 0,
                                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}


@pytest.mark.asyncio
async def test_a_transcript_of_synthetic_lines_only_is_not_measured_as_zero(reaper, repo, projects):
    """P-3: a rate-limited agent did not "run and spend nothing". The row stays unmeasured."""
    agent_id, path = await _subagent(repo, projects, "cc-rate-limited", turns=())
    _write_lines(path, _USER, _SYNTHETIC)

    await _cycle(reaper, repo)
    await _cycle(reaper, repo)

    row = await repo.get_agent(agent_id)
    assert (row.tokens_measured_at, row.output_tokens, row.tokens_source) == (None, None, None)
    assert row.usage_unmeasurable == "api_error_only"


@pytest.mark.asyncio
async def test_a_zero_parse_is_not_written_whatever_the_parser_returns(reaper, repo, projects, monkeypatch):
    """The second check: even a parser that turns synthetic lines into one call of zeros
    (as before batch 2a) does not get a row written as measured 0."""
    agent_id, path = await _subagent(repo, projects, "cc-zero-parse")
    zeros = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0,
             "api_calls": 1, "total_tokens": 0, "model": "", "model_source": "unknown"}

    async def run_batch(jobs):
        return [usage_remeasure.RemeasureResult(job=job, usage=zeros, read_at=time.time(),
                                                signature=usage_remeasure.file_signature(job.path)) for job in jobs]

    monkeypatch.setattr(usage_remeasure, "run_batch", run_batch)
    await _cycle(reaper, repo)
    await _cycle(reaper, repo)

    row = await repo.get_agent(agent_id)
    assert (row.tokens_measured_at, row.output_tokens) == (None, None)


@pytest.mark.parametrize(
    "lines, verdict",
    [((_USER, _SYNTHETIC), "api_error_only"), ((_USER,), "no_usage_lines")],
    ids=["synthetic-only", "no-assistant-line"],
)
@pytest.mark.asyncio
async def test_unbilled_transcripts_get_the_verdict_subagent_stop_gives(reaper, repo, projects, lines, verdict):
    agent_id, path = await _subagent(repo, projects, f"cc-{verdict}", turns=())
    _write_lines(path, *lines)

    await _cycle(reaper, repo)
    await _cycle(reaper, repo)

    row = await repo.get_agent(agent_id)
    assert row.usage_unmeasurable == verdict == unmeasurable_reason(path)
    assert row.tokens_measured_at is None
    events = await repo.list_events(event_type="agent.updated", entity_id=agent_id, limit=50)

    await reaper._remeasure_subagent_usage(repo)  # the same content again after a restart
    reaper._usage_ledger = usage_remeasure.RemeasureLedger()
    await _cycle(reaper, repo)
    await _cycle(reaper, repo)
    assert len(await repo.list_events(event_type="agent.updated", entity_id=agent_id, limit=50)) == len(events)


@pytest.mark.asyncio
async def test_billed_usage_clears_an_earlier_verdict(reaper, repo, projects):
    """The agent was woken and made real calls: "nothing to count" no longer holds."""
    agent_id, path = await _subagent(repo, projects, "cc-woke-up", turns=())
    _write_lines(path, _USER)
    await repo.update_agent(agent_id, usage_unmeasurable="no_usage_lines")
    _write(path, ("r1", 120), append=True)

    await _cycle(reaper, repo)
    await _cycle(reaper, repo)

    row = await repo.get_agent(agent_id)
    assert (row.output_tokens, row.usage_unmeasurable) == (120, None)


@pytest.mark.asyncio
async def test_rows_in_flight_are_not_picked(reaper, repo, projects):
    """Offline but active in the last 30 minutes (usage_coverage.is_in_flight): no conclusion yet."""
    agent_id, _ = await _subagent(repo, projects, "cc-just-stopped")
    await repo.update_agent(agent_id, last_active_at=utc_now() - timedelta(minutes=5))

    await reaper._remeasure_subagent_usage(repo)
    assert reaper._usage_batch is None

    await repo.update_agent(agent_id, last_active_at=utc_now() - timedelta(minutes=31))
    await _cycle(reaper, repo)
    await _cycle(reaper, repo)
    assert (await repo.get_agent(agent_id)).output_tokens == 300


@pytest.mark.asyncio
async def test_up_to_date_rows_are_not_parsed_again(reaper, repo, projects):
    agent_id, _ = await _subagent(repo, projects, "cc-done")
    await repo.update_agent(agent_id, output_tokens=300, tokens_measured_at=utc_now(), tokens_source="transcript")

    await reaper._remeasure_subagent_usage(repo)

    assert reaper._usage_batch is None


@pytest.mark.asyncio
async def test_candidates_keep_rows_without_a_harness(repo, projects):
    """Most CC rows carry harness NULL: "not codex" must keep them (NULL != 'codex' is not true in SQL)."""
    unlabelled, _ = await _subagent(repo, projects, "cc-null-harness")
    labelled, _ = await _subagent(repo, projects, "cc-claude-code", harness="claude-code")
    await _subagent(repo, projects, "cc-codex", harness="codex")

    candidates = await repo.list_usage_remeasure_candidates(
        since=utc_now() - usage_remeasure.REMEASURE_WINDOW, limit=100,
    )

    assert sorted(c.agent_id for c in candidates) == sorted([unlabelled, labelled])


@pytest.mark.parametrize(
    "kind", ["busy", "waiting", "codex", "leader", "no-cc-id", "out-of-window", "still-writing", "no-file"],
)
@pytest.mark.asyncio
async def test_rows_left_to_other_paths(reaper, repo, projects, kind):
    """Live rows stay SubagentStop's; Codex usage comes from its rollout path and a Leader's from its own."""
    agent_id, path = await _subagent(
        repo, projects, f"cc-{kind}",
        status={"busy": "busy", "waiting": "waiting"}.get(kind, "offline"),
        harness="codex" if kind == "codex" else None,
        role="leader" if kind == "leader" else "workflow-subagent",
        turns=() if kind == "no-file" else (("r1", 100),),
    )
    if kind == "no-cc-id":
        await repo.update_agent(agent_id, cc_tool_use_id=None)
    if kind == "out-of-window":
        await repo.update_agent(agent_id, last_active_at=utc_now() - timedelta(days=31))
    if kind == "still-writing":
        _write(path, ("r2", 5), append=True, age=0.0)

    await reaper._remeasure_subagent_usage(repo)

    assert reaper._usage_batch is None
    assert (await repo.get_agent(agent_id)).tokens_measured_at is None


# ------------------------------------------------------------------
# A result is written only over the state it was taken from
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_measurement_landing_meanwhile_is_not_overwritten(reaper, repo, projects):
    agent_id, _ = await _subagent(repo, projects, "cc-raced")
    await _cycle(reaper, repo)
    measured_by_stop = utc_now()
    await repo.update_agent(agent_id, output_tokens=777, tokens_measured_at=measured_by_stop)

    await reaper._remeasure_subagent_usage(repo)

    row = await repo.get_agent(agent_id)
    assert (row.output_tokens, row.tokens_measured_at) == (777, measured_by_stop)


@pytest.mark.asyncio
async def test_a_woken_row_is_not_written(reaper, repo, projects):
    agent_id, _ = await _subagent(repo, projects, "cc-woken")
    await _cycle(reaper, repo)
    await repo.update_agent(agent_id, status="busy")

    await reaper._remeasure_subagent_usage(repo)

    assert (await repo.get_agent(agent_id)).tokens_measured_at is None


@pytest.mark.asyncio
async def test_a_file_that_changed_after_the_parse_is_measured_again(reaper, repo, projects):
    agent_id, path = await _subagent(repo, projects, "cc-grew")
    await _cycle(reaper, repo)
    _write(path, ("r3", 1000), append=True)

    await _cycle(reaper, repo)  # the stale result is dropped, the new content is picked
    assert (await repo.get_agent(agent_id)).tokens_measured_at is None
    await _cycle(reaper, repo)

    assert (await repo.get_agent(agent_id)).output_tokens == parse_transcript_usage(path)["output_tokens"]


@pytest.mark.asyncio
async def test_a_file_without_usage_is_parsed_once_until_it_changes(reaper, repo, projects):
    agent_id, path = await _subagent(repo, projects, "cc-empty", turns=())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    stamp = time.time() - 60
    os.utime(path, (stamp, stamp))

    await _cycle(reaper, repo)
    await _cycle(reaper, repo)  # lands "nothing to write"; the same content is not picked again
    assert reaper._usage_batch is None
    assert (await repo.get_agent(agent_id)).tokens_measured_at is None  # no data is not zero

    _write(path, ("r1", 40), append=True)
    await _cycle(reaper, repo)
    await _cycle(reaper, repo)
    assert (await repo.get_agent(agent_id)).output_tokens == 40


# ------------------------------------------------------------------
# Budget, failure, shutdown
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backlog_drains_across_cycles_within_the_batch_cap(reaper, repo, projects, monkeypatch):
    monkeypatch.setattr(usage_remeasure, "REMEASURE_MAX_FILES", 2)
    ids = [(await _subagent(repo, projects, f"cc-backlog-{i}"))[0] for i in range(5)]

    sizes = []
    for _ in range(6):
        await reaper._remeasure_subagent_usage(repo)
        if reaper._usage_batch is None:
            break
        jobs, batch = reaper._usage_batch
        sizes.append(len(jobs))
        await asyncio.wait({batch}, timeout=120)

    assert sizes == [2, 2, 1]
    assert [(await repo.get_agent(i)).output_tokens for i in ids] == [300] * 5


@pytest.mark.asyncio
async def test_a_stop_measurement_between_the_check_and_the_write_wins(reaper, repo, projects, monkeypatch):
    """The write is conditional in SQL: a measurement landing after the row was read still wins."""
    agent_id, _ = await _subagent(repo, projects, "cc-cas")
    await _cycle(reaper, repo)
    real_get_agent = repo.get_agent

    async def get_agent_then_stop_lands(agent):
        row = await real_get_agent(agent)
        await repo.update_agent(agent, output_tokens=777, tokens_measured_at=utc_now())
        return row

    monkeypatch.setattr(repo, "get_agent", get_agent_then_stop_lands)
    await reaper._remeasure_subagent_usage(repo)

    assert (await real_get_agent(agent_id)).output_tokens == 777


@pytest.mark.asyncio
async def test_a_lower_remeasure_is_written_and_logged(reaper, repo, projects, caplog):
    agent_id, _ = await _subagent(repo, projects, "cc-lower")
    grew_id, _ = await _subagent(repo, projects, "cc-grew-normally")
    for row_id, output in ((agent_id, 99_999), (grew_id, 22)):
        await repo.update_agent(row_id, input_tokens=10, output_tokens=output, cache_creation_tokens=5,
                                cache_read_tokens=100, tokens_measured_at=utc_now() - timedelta(hours=2),
                                tokens_source="transcript")

    with caplog.at_level(logging.WARNING, logger="aiteam.api.usage_remeasure"):
        await _cycle(reaper, repo)
        await _cycle(reaper, repo)

    assert [(await repo.get_agent(i)).output_tokens for i in (agent_id, grew_id)] == [300, 300]
    lower = [r.getMessage() for r in caplog.records if "lower than stored" in r.getMessage()]
    assert len(lower) == 1 and agent_id in lower[0] and "output_tokens" in lower[0]


@pytest.mark.asyncio
async def test_a_stale_stored_path_is_replaced_by_the_file_found_by_cc_id(reaper, repo, projects):
    agent_id, path = await _subagent(repo, projects, "cc-moved")
    await repo.update_agent(agent_id, transcript_path=str(projects / "gone" / "agent-cc-moved.jsonl"))

    await _cycle(reaper, repo)
    await _cycle(reaper, repo)

    row = await repo.get_agent(agent_id)
    assert (row.transcript_path, row.output_tokens) == (str(path), 300)


def test_the_parse_child_imports_the_parser_and_nothing_heavier():
    """The child boot, run for real: no repository, SQLAlchemy or FastAPI among its imports."""
    root = str(Path(usage_remeasure.__file__).resolve().parents[2])
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", usage_remeasure._CHILD_BOOT, root],
        input=b'{"paths": []}', capture_output=True, check=True, timeout=120,
    )
    assert proc.stdout == b"[]"
    imported = {line.rsplit("|", 1)[-1].strip() for line in proc.stderr.decode().splitlines() if "|" in line}
    assert "aiteam.services.token_attribution" in imported
    heavy = sorted(m for m in imported if m.split(".")[0] in {"sqlalchemy", "fastapi", "starlette", "pydantic"}
                   or m.startswith(("aiteam.storage", "aiteam.api.agent_context", "aiteam.api.usage_remeasure")))
    assert heavy == []


async def _drain(reaper, repo, *, cycles: int = 20) -> list[int]:
    """Run cycles until no batch is started; returns each started batch's size."""
    sizes = []
    for _ in range(cycles):
        await reaper._remeasure_subagent_usage(repo)
        if reaper._usage_batch is None:
            break
        jobs, batch = reaper._usage_batch
        sizes.append(len(jobs))
        await asyncio.wait({batch}, timeout=120)
    return sizes


@pytest.mark.asyncio
async def test_a_failed_batch_is_retried_at_half_size_and_holds_nothing_against_its_rows(
    reaper, repo, projects, monkeypatch,
):
    """A timeout or crash says nothing about the files: none is held back, the batch shrinks."""
    monkeypatch.setattr(usage_remeasure, "REMEASURE_MAX_FILES", 4)
    ids = [(await _subagent(repo, projects, f"cc-flaky-{i}"))[0] for i in range(5)]
    real_boot = usage_remeasure._CHILD_BOOT
    monkeypatch.setattr(usage_remeasure, "_CHILD_BOOT", "import sys; sys.exit(3)")  # the child dies

    await _cycle(reaper, repo)
    monkeypatch.setattr(usage_remeasure, "_CHILD_BOOT", real_boot)
    sizes = await _drain(reaper, repo)

    assert sizes == [2, 3]  # half the failed batch, then back up to what is left
    assert [(await repo.get_agent(i)).output_tokens for i in ids] == [300] * 5
    assert reaper._usage_ledger.failures == {}


@pytest.mark.asyncio
async def test_a_file_that_keeps_failing_alone_is_left_until_it_changes(reaper, repo, projects, monkeypatch):
    """Halving isolates the file that kills the child; it gets REMEASURE_MAX_FAILURES tries alone."""
    good = [(await _subagent(repo, projects, f"cc-good-{i}"))[0] for i in range(3)]
    poison_id, poison = await _subagent(repo, projects, "cc-poison")
    real_run_batch = usage_remeasure.run_batch
    alone = []

    async def run_batch(jobs):
        if any(job.path == str(poison) for job in jobs):
            if len(jobs) == 1:
                alone.append(1)
            raise usage_remeasure.RemeasureChildError("child killed")
        return await real_run_batch(jobs)

    monkeypatch.setattr(usage_remeasure, "run_batch", run_batch)
    await _drain(reaper, repo)

    assert [(await repo.get_agent(i)).output_tokens for i in good] == [300] * 3
    assert (await repo.get_agent(poison_id)).tokens_measured_at is None
    assert len(alone) == usage_remeasure.REMEASURE_MAX_FAILURES
    assert reaper._usage_batch is None  # left alone now

    _write(poison, ("r3", 5), append=True)  # new content: tried again
    await reaper._remeasure_subagent_usage(repo)
    assert reaper._usage_batch is not None and reaper._usage_batch[0][0].path == str(poison)


@pytest.mark.asyncio
async def test_a_file_the_parser_raises_on_is_retried_before_it_is_left_alone(reaper, repo, projects, monkeypatch):
    agent_id, path = await _subagent(repo, projects, "cc-raises")

    async def run_batch(jobs):
        return [usage_remeasure.RemeasureResult(job=job, usage=None, signature=usage_remeasure.file_signature(job.path),
                                                read_at=time.time(), error="ValueError: bad line") for job in jobs]

    monkeypatch.setattr(usage_remeasure, "run_batch", run_batch)
    sizes = await _drain(reaper, repo)

    assert sizes == [1] * usage_remeasure.REMEASURE_MAX_FAILURES
    assert (await repo.get_agent(agent_id)).tokens_measured_at is None


@pytest.mark.asyncio
async def test_stopping_the_reaper_kills_the_parse_child(reaper, repo, projects, monkeypatch):
    await _subagent(repo, projects, "cc-slow")
    monkeypatch.setattr(usage_remeasure, "_CHILD_BOOT", "import time; time.sleep(300)")
    before = {p.pid for p in psutil.Process().children()}

    await reaper._remeasure_subagent_usage(repo)
    await asyncio.sleep(0.5)
    children = [p for p in psutil.Process().children() if p.pid not in before]
    assert len(children) == 1

    started = time.monotonic()
    await reaper.stop()
    assert time.monotonic() - started < 5  # the child is killed, not waited out (120 s)

    assert not children[0].is_running() or children[0].status() == psutil.STATUS_ZOMBIE
    assert reaper._usage_batch is None


# ------------------------------------------------------------------
# The event loop stays free while a batch is parsed
# ------------------------------------------------------------------


class _LagProbe:
    def __init__(self) -> None:
        self.worst = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            before = loop.time()
            await asyncio.sleep(0.005)
            self.worst = max(self.worst, loop.time() - before - 0.005)


LAG_FLOOR = 0.05  # scheduling noise a loaded machine adds to a 5 ms sleep


@pytest.mark.asyncio
async def test_a_batch_does_not_stall_the_event_loop(reaper, repo, projects):
    """16 transcripts of about 1.3 MB: parsed on the loop they take about as long as
    ``in_loop`` below. The probe on the same loop bounds what the re-measure step
    and its batch do to it, relative to that cost, so the bound holds on a fast
    idle machine and on a loaded runner alike."""
    paths = []
    for i in range(16):
        turns = [(f"r{k}", 50 + k) for k in range(2000)]
        _, path = await _subagent(repo, projects, f"cc-load-{i}", turns=turns)
        paths.append(path)
    started = time.perf_counter()
    for path in paths[:4]:
        parse_transcript_usage(path)
    in_loop = (time.perf_counter() - started) * 4  # what all 16 cost on the loop

    stop = asyncio.Event()
    probe = _LagProbe()
    task = asyncio.create_task(probe.run(stop))
    try:
        await _cycle(reaper, repo)
        await _cycle(reaper, repo)
    finally:
        stop.set()
        await task

    measured = [await repo.get_agent(a.id) for a in await repo.list_agents(
        (await repo.get_team_by_name("workflow-wf_test")).id)]
    assert all(a.tokens_measured_at is not None for a in measured)
    assert in_loop > 4 * LAG_FLOOR, f"fixture too small to tell ({in_loop:.3f}s)"
    assert probe.worst < max(LAG_FLOOR, in_loop / 4), (probe.worst, in_loop)
