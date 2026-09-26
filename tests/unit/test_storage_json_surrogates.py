"""The storage layer's single gate: every JSON value is written with lone surrogates as U+FFFD.

These writes never pass the API middleware: they go straight through the
repository, the way internal writers do (file ingest, hook translation, reapers,
migrations). Before the gate such a write landed as an ASCII escape and every later
read that listed the row failed; now the stored text holds U+FFFD and the reads
stay 200.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from aiteam.api import workflow_ingest
from aiteam.storage.connection import get_session
from aiteam.types import EcosystemRepoEvent
from tests.unit.test_meeting_security import _make_client, _teardown

LONE = "x" + chr(0xD800) + "y"
FFFD = chr(0xFFFD)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _stored(repo, sql: str) -> list[str]:
    async with get_session(repo._db_url) as session:
        return [row[0] for row in (await session.execute(text(sql))).all()]


def test_repository_writes_store_the_replacement_and_reads_stay_200():
    client, repo, _ = _make_client()
    try:
        memory = _run(repo.create_memory("global", "system", "fine", source_refs=[LONE]))
        task = _run(repo.create_task(None, "t", tags=[LONE]))
        _run(repo.create_event("team.created", "test", {LONE: LONE}))
        _run(repo.create_repo_event(EcosystemRepoEvent(repo_id="r1", event_type="discovered",
                                                       payload_json={"v": LONE})))  # a TEXT column

        stored = (_run(_stored(repo, "SELECT source_refs FROM memories"))
                  + _run(_stored(repo, "SELECT tags FROM tasks"))
                  + _run(_stored(repo, "SELECT data FROM events WHERE type = 'team.created'"))
                  + _run(_stored(repo, "SELECT payload_json FROM ecosystem_repo_events")))
        assert all("\\ud800" not in cell for cell in stored), stored
        assert all("\\ufffd" in cell for cell in stored), stored

        resp = client.get("/api/memories")
        assert resp.status_code == 200, resp.text[:160]
        refs = next(m["source_refs"] for m in resp.json()["data"] if m["id"] == memory.id)
        assert refs == ["x" + FFFD + "y"]
        resp = client.get(f"/api/tasks/{task.id}")
        assert resp.status_code == 200, resp.text[:160]
        assert resp.json()["data"]["tags"] == ["x" + FFFD + "y"]
    finally:
        _teardown()


def test_a_workflow_file_with_a_lone_surrogate_is_ingested_and_stays_readable(tmp_path):
    """The reviewer's case: a host-written workflow JSON carrying the escape, ingested from disk."""
    client, repo, event_bus = _make_client()
    try:
        wf = tmp_path / "session-1" / "workflows" / "wf_lone.json"
        wf.parent.mkdir(parents=True)
        # Written as JSON text, the way Node's JSON.stringify writes a lone surrogate.
        wf.write_text(json.dumps({
            "runId": "wf-lone", "workflowName": "lone", "status": "completed",
            "phases": [{"title": LONE, "status": "completed"}],
            "result": {"summary": LONE},
        }), encoding="utf-8")
        assert "\\ud800" in wf.read_text(encoding="utf-8")

        outcome = _run(workflow_ingest.ingest_run_from_file(repo, event_bus, Path(wf)))
        assert outcome.get("ok") is True, outcome
        resp = client.get("/api/workflows")
        assert resp.status_code == 200, resp.text[:160]
        run = next(r for r in resp.json()["data"] if r["wf_id"] == "wf-lone")
        assert FFFD in json.dumps(run, ensure_ascii=False)
    finally:
        _teardown()
