"""Unregistered HTTP MCP folders can register/decline without borrowing scope."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

import httpx

from .test_mcp_http_sharing import _call, _connect, _project
from .test_mcp_process_groups import _isolated_mcp


def _content(response: dict) -> dict:
    result = response["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def _rejected(response: dict, message: str = "") -> None:
    result = response["result"]
    if result.get("isError"):
        assert message in json.dumps(result, ensure_ascii=False), result
    else:
        content = result["structuredContent"]
        assert content.get("success") is False, result
        assert message in json.dumps(content, ensure_ascii=False), result


def _stored_projects(database: Path) -> list[tuple]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        return connection.execute("SELECT id, name, root_path FROM projects ORDER BY name").fetchall()


def test_unregistered_http_folder_registers_then_resolves_on_the_next_request(tmp_path):
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, _, port, _ = runtime
        directory = tmp_path / "新项目"
        directory.mkdir()
        other = _project(port, tmp_path / "other", "Other project")
        database = tmp_path / "data/aiteam.db"

        async def exercise():
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                await _connect(client, directory)
                for index, tool in enumerate(("context_resolve", "project_list", "task_create"), 2):
                    args = {"title": "must not write", "project_id": other} if tool == "task_create" else {}
                    _rejected(await _call(client, index, tool, args), "not a registered project")
                created = _content(await _call(client, 5, "project_create", {"name": "Bootstrap"}))
                assert created["success"] is True
                project_id = created["data"]["id"]
                assert created["data"]["root_path"] == str(directory.resolve())
                assert (project_id, "Bootstrap", str(directory.resolve())) in _stored_projects(database)
                context = _content(await _call(client, 6, "context_resolve"))
                assert context["project"]["id"] == project_id
                task = _content(await _call(client, 7, "task_create", {"title": "In new project"}))
                assert task["success"] is True, task
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                    stored = connection.execute(
                        "SELECT project_id FROM tasks WHERE id = ?", (task["data"]["id"],),
                    ).fetchone()
                    assert stored == (project_id,)
                    blocked = connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE title = 'must not write'",
                    ).fetchone()
                    assert blocked == (0,)
                # Same transport, new request: neither previous project nor a
                # failed request may supply a missing connection directory.
                del client.headers["X-Aiteam-Project-Dir"]
                _rejected(await _call(client, 8, "project_create", {"name": "No header"}), "absolute working directory")
                client.headers["X-Aiteam-Project-Dir"] = quote(str(tmp_path / "other"), safe="/:.-_")
                assert _content(await _call(client, 9, "context_resolve"))["project"]["id"] == other
                assert (await client.delete("/mcp/")).status_code == 200

        asyncio.run(exercise())
        assert len(_stored_projects(database)) == 2


def test_unregistered_http_folder_can_decline_only_its_own_registration(tmp_path):
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, _, port, _ = runtime
        directory = tmp_path / "declined"
        directory.mkdir()
        other_dir = tmp_path / "other"
        _project(port, other_dir, "Other project")
        dismissed = tmp_path / "home/.aiteam/dismissed_projects.json"

        async def exercise():
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                await _connect(client, directory)
                _rejected(
                    await _call(client, 2, "dismiss_project_registration", {"cwd": str(other_dir)}),
                    "working directory",
                )
                assert not dismissed.exists()
                result = _content(await _call(client, 3, "dismiss_project_registration"))
                assert result["success"] is True
                assert result["cwd"] == str(directory.resolve()).lower()
                assert result["notice"] == "none"
                assert json.loads(dismissed.read_text())["dismissed"] == [str(directory.resolve()).lower()]
                again = _content(await _call(client, 4, "dismiss_project_registration", {"cwd": str(directory)}))
                assert again["dismissed_count"] == 1
                _rejected(await _call(client, 5, "context_resolve"), "not a registered project")
                assert (await client.delete("/mcp/")).status_code == 200

        asyncio.run(exercise())
        assert len(_stored_projects(tmp_path / "data/aiteam.db")) == 1


def test_http_bootstrap_rejects_invalid_and_cross_project_paths(tmp_path):
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, _, port, _ = runtime
        directory = tmp_path / "home/work"
        directory.mkdir()
        other_dir = tmp_path / "other"
        _project(port, other_dir, "Other project")
        link = directory / "other-link"
        link.symlink_to(other_dir, target_is_directory=True)
        own_link = tmp_path / "own-link"
        own_link.symlink_to(directory, target_is_directory=True)
        escape = other_dir / "escape"
        escape.symlink_to(directory, target_is_directory=True)
        regular_file = tmp_path / "file"
        regular_file.write_text("not a directory")

        async def exercise():
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                await _connect(client, directory)
                wrong_roots = (str(other_dir), str(link), str(tmp_path / "home/wor"), str(tmp_path), "/")
                for index, root in enumerate(wrong_roots, 2):
                    _rejected(await _call(client, index, "project_create", {"name": "Wrong root", "root_path": root}))
                # A registered project's lexical child may resolve into a
                # different project; resolve the actual directory first.
                client.headers["X-Aiteam-Project-Dir"] = quote(str(link), safe="/:.-_")
                context = _content(await _call(client, 8, "context_resolve"))
                assert context["project"]["name"] == "Other project"
                client.headers["X-Aiteam-Project-Dir"] = quote(str(escape), safe="/:.-_")
                _rejected(await _call(client, 20, "context_resolve"), "not a registered project")
                _rejected(await _call(client, 21, "project_create", {"name": "Escape", "root_path": str(other_dir)}))
                for index, invalid in enumerate(("relative", str(tmp_path / "missing"), str(regular_file), "%00"), 9):
                    client.headers["X-Aiteam-Project-Dir"] = invalid
                    _rejected(await _call(client, index, "project_create", {"name": "Invalid cwd"}))
                client.headers["X-Aiteam-Project-Dir"] = quote(str(own_link), safe="/:.-_")
                created = _content(await _call(client, 14, "project_create", {
                    "name": "Canonical", "root_path": str(own_link),
                }))
                assert created["data"]["root_path"] == str(directory.resolve())
                assert _content(await _call(client, 15, "context_resolve"))["project"]["id"] == created["data"]["id"]
                assert (await client.delete("/mcp/")).status_code == 200

        asyncio.run(exercise())
        rows = _stored_projects(tmp_path / "data/aiteam.db")
        assert len(rows) == 2
        assert {row[1] for row in rows} == {"Canonical", "Other project"}
