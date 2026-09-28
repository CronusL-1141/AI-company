"""Reconcile lease over native HTTP MCP (the Codex path), end to end.

HTTP MCP hosts send no CC session id, so before the connection's own MCP session id
was carried to the API a Codex session that lost its lease_id (context compaction)
could neither renew, apply nor release its own lease and every session waited out
the TTL. Two real HTTP MCP connections against an isolated runtime check that the
holder is recognised by its connection alone, that another connection is refused
but can still peek, and that an empty batch from the holder frees the lease.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from .test_mcp_http_sharing import _call, _connect, _project
from .test_mcp_process_groups import _isolated_mcp


def _tool(response: dict) -> dict:
    return response["result"]["structuredContent"]


def _stored_lease(database, project_id: str) -> dict | None:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        (raw,) = con.execute(
            "SELECT json_extract(config, '$.memory.reconcile_lease') FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    finally:
        con.close()
    return json.loads(raw) if raw else None


def test_http_mcp_connection_holds_and_frees_the_lease_without_lease_id(tmp_path):
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, _, port, _ = runtime
        directory = tmp_path / "project-reconcile"
        project_id = _project(port, directory, "HTTP reconcile")
        database = tmp_path / "data" / "aiteam.db"
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as rest:
            task = rest.post(f"/api/projects/{project_id}/tasks", json={"title": "t"}).json()["data"]
            for text in ("部署 API 到生产环境使用 docker compose 命令", "生产环境部署 API 用 docker compose 命令启动"):
                rest.post(f"/api/tasks/{task['id']}/memo", json={"content": text},
                          headers={"X-Project-Id": project_id}).raise_for_status()

        async def exercise() -> dict:
            async with (
                # Generous: other suites share the machine, and a slow tool call is not the subject.
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=60) as first,
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=60) as second,
            ):
                await _connect(first, directory)
                await _connect(second, directory)
                seen: dict = {}
                seen["first"] = _tool(await _call(first, 2, "memory_reconcile_candidates"))
                seen["stored"] = _stored_lease(database, project_id)
                seen["second"] = _tool(await _call(second, 2, "memory_reconcile_candidates"))
                seen["second_peek"] = _tool(await _call(second, 3, "memory_reconcile_candidates", {"peek": True}))
                # The first connection never passes its lease_id back: it is known by its connection.
                seen["first_again"] = _tool(await _call(first, 3, "memory_reconcile_candidates"))
                seen["released"] = _tool(await _call(first, 4, "memory_reconcile_apply", {"operations": []}))
                seen["second_after"] = _tool(await _call(second, 4, "memory_reconcile_candidates"))
                for client in (first, second):
                    assert (await client.delete("/mcp/")).status_code == 200
                return seen

        seen = asyncio.run(exercise())

    first = seen["first"]["data"]["reconcile_lease"]
    assert seen["first"]["success"] is True and first["status"] == "acquired", seen["first"]
    assert seen["stored"]["holder_kind"] == "mcp_connection", seen["stored"]
    assert first["lease_id"] not in json.dumps(seen["stored"])  # only its hash is stored
    assert seen["second"]["success"] is False
    assert seen["second"]["reconcile_lease"]["holder_kind"] == "mcp_connection"
    assert seen["second_peek"]["success"] is True
    assert seen["second_peek"]["data"]["reconcile_lease"]["status"] == "peek"
    assert seen["second_peek"]["data"]["stats"]["total_valid_memos"] == 2
    renewed = seen["first_again"]["data"]["reconcile_lease"]
    assert renewed["status"] == "renewed" and "lease_id" not in renewed
    assert seen["released"]["data"]["reconcile_lease"] == {"status": "released"}, seen["released"]
    assert seen["second_after"]["success"] is True
    assert seen["second_after"]["data"]["reconcile_lease"]["status"] == "acquired"
