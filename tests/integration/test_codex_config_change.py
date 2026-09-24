"""Real MCP calls, adapter writes and HTTP consent survive a complete DB reopen."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from fastmcp import Client, FastMCP

from aiteam.mcp.tools import infra
from aiteam.services import config_change
from tests.integration.test_user_notice_hooks_live import LiveAPI
from tests.unit.notices.test_codex_config_change import CHANGE, make_installation, snapshot


async def test_mcp_update_records_real_backups_and_persistent_consent(tmp_path, monkeypatch):
    install = make_installation(tmp_path, monkeypatch)
    api = LiveAPI(tmp_path / "api")
    api.env.update(HOME=str(install.home), CODEX_HOME=str(install.codex),
                   AITEAM_CODEX_STATE_DIR=str(tmp_path / "codex-state"),
                   AITEAM_STATE_DIR=str(tmp_path / "state"))
    monkeypatch.setenv("AITEAM_API_URL", api.base)  # no discovery/fallback to port 8000
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        api.env.pop(key, None)
    before_config = snapshot(install.codex)
    approval = "确认，按上述预览更新 Codex 适配器。" + "同意本次预览。" * 100
    await asyncio.to_thread(api.start)
    try:
        mcp = FastMCP("isolated-codex-config-change")
        infra.register(mcp)
        async with Client(mcp) as client:
            shown = (await client.call_tool("os_config_change", {"change": CHANGE})).data
            assert shown["baseline"]["root"] == str(install.source)
            assert shown["baseline"]["codex_home"] == str(install.codex)
            assert snapshot(install.codex) == before_config
            refused = (await client.call_tool("os_config_change", {
                "change": CHANGE, "confirm_token": shown["confirm_token"], "user_quote": "",
            })).data
            assert refused["success"] is False
            assert snapshot(install.codex) == before_config
            issued, signature = shown["confirm_token"].split(".")
            expired = f"{int(issued) - config_change.TOKEN_TTL_S - 1}.{signature}"
            refused = (await client.call_tool("os_config_change", {
                "change": CHANGE, "confirm_token": expired, "user_quote": "确认",
            })).data
            assert refused["success"] is False and "expired" in refused["error"]
            assert snapshot(install.codex) == before_config

            manifest = install.codex / "hooks.json"
            manifest.write_bytes(manifest.read_bytes() + b"\n")
            after_drift = snapshot(install.codex)
            refused = (await client.call_tool("os_config_change", {
                "change": CHANGE, "confirm_token": shown["confirm_token"], "user_quote": "确认",
            })).data
            assert refused["success"] is False and "changed since the preview" in refused["error"]
            assert snapshot(install.codex) == after_drift
            async with httpx.AsyncClient(base_url=api.base, trust_env=False) as observer:
                pending = await observer.get("/api/events", params={"type": "decision.user_config_write"})
                assert pending.json()["data"] == []

            shown = (await client.call_tool("os_config_change", {"change": CHANGE})).data
            originals = {row["path"]: Path(row["path"]).read_bytes() for row in shown["targets"]}
            result = (await client.call_tool("os_config_change", {
                "change": CHANGE, "confirm_token": shown["confirm_token"],
                "user_quote": approval,
            })).data
            assert result["success"] and result["event"]["recorded"] == "api"
            assert result["host"] == "codex"
            for target in result["targets"]:
                assert Path(target["backup"]).read_bytes() == originals[target["path"]]
                assert hashlib.sha256(Path(target["path"]).read_bytes()).hexdigest() == target["after_sha256"]
            assert install.installed.read_bytes() == install.script.read_bytes()
            assert not (install.codex / "config.toml").exists()  # hooks-only survives the MCP path
            assert not (install.codex / "bin").exists()

        # Terminating this fixture's API closes its SQLite connection/engine.
        # A new process and new HTTP client must recover the consent from disk.
        await asyncio.to_thread(api.restart)
        async with httpx.AsyncClient(base_url=api.base, trust_env=False) as fresh:
            response = await fresh.get("/api/events", params={"type": "decision.user_config_write"})
            response.raise_for_status()
            [event] = response.json()["data"]
            assert event["data"]["change"] == CHANGE
            assert event["data"]["user_quote"] == approval  # online keeps the full long Chinese quote
            assert event["data"]["host"] == "codex"
            assert event["data"]["tool"] == "os_config_change"
            assert event["data"]["targets"] == result["targets"]
            assert event["data"]["baseline"] == shown["baseline"]
    finally:
        await asyncio.to_thread(api.stop)
        api.client.close()


async def test_successful_update_does_not_clear_an_unresolved_retired_handler(tmp_path, monkeypatch):
    install = make_installation(tmp_path, monkeypatch)
    surface = install.source / "plugin/harness/codex/surface.py"
    retired_row = ('    ("UserPromptSubmit", "", KIND_OBSERVE, [\n'
                   '        ("channel_unread_codex.py", "leader-codex", 3, 0),\n    ]),\n')
    assert retired_row in surface.read_text()
    surface.write_text(surface.read_text().replace(retired_row, ""))
    install.script.unlink()
    manifest = install.codex / "hooks.json"
    retained = install.installed.read_bytes(), manifest.read_bytes()
    api = LiveAPI(tmp_path / "api")
    api.env.update(HOME=str(install.home), CODEX_HOME=str(install.codex))
    monkeypatch.setenv("AITEAM_API_URL", api.base)
    await asyncio.to_thread(api.start)
    try:
        async with httpx.AsyncClient(base_url=api.base, trust_env=False) as observer:
            response = await observer.get("/api/notices", params={"fresh": 1, "host": "codex", "status": "all"})
            response.raise_for_status()
            [unresolved] = [row for row in response.json()["items"] if row["catalog_id"] == "codex_copy_stale"]
            assert unresolved["status"] == "active"
            key = unresolved["key"]
            mcp = FastMCP("retired-codex-handler-update")
            infra.register(mcp)
            async with Client(mcp) as client:
                shown = (await client.call_tool("os_config_change", {"change": CHANGE})).data
                assert shown["notice_key"] == "" and shown["confirm_token"]
                assert any(row["path"] == str(install.receipt) for row in shown["targets"])
                result = (await client.call_tool("os_config_change", {
                    "change": CHANGE, "confirm_token": shown["confirm_token"], "user_quote": "确认更新保留遗留声明",
                })).data
                assert result["success"] and result["notice_key"] == ""
            # Read without a refresh first: eager clear must never have happened.
            detail = await observer.get(f"/api/notices/{key}")
            assert detail.json()["notice"]["status"] == "active"
            assert detail.json()["notice"]["cleared_at"] is None
            assert (install.installed.read_bytes(), manifest.read_bytes()) == retained
            response = await observer.get("/api/notices", params={"fresh": 1, "host": "codex", "status": "all"})
            [after] = [row for row in response.json()["items"] if row["catalog_id"] == "codex_copy_stale"]
            assert after["key"] == key and after["status"] == "active" and after["cleared_at"] is None
    finally:
        await asyncio.to_thread(api.stop)
        api.client.close()


async def test_offline_modified_copy_records_bounded_consent_and_imports_once(tmp_path, monkeypatch):
    long_root = tmp_path / ("中文安装目录" * 10) / ("中文来源目录" * 10)
    install = make_installation(long_root, monkeypatch)
    install.installed.write_bytes(install.installed.read_bytes() + b"\n# custom local content\n")
    customized = install.installed.read_bytes()
    api = LiveAPI(tmp_path / "api")
    api.env.update(HOME=str(install.home), CODEX_HOME=str(install.codex))
    monkeypatch.setenv("AITEAM_API_URL", api.base)
    approval = "确认覆盖本地修改并保留备份。" * 100
    try:
        mcp = FastMCP("offline-codex-update")
        infra.register(mcp)
        async with Client(mcp) as client:
            shown = (await client.call_tool("os_config_change", {"change": CHANGE})).data
            assert shown["warnings"]  # long local-edit warnings remain visible in the approval screen
            result = (await client.call_tool("os_config_change", {
                "change": CHANGE, "confirm_token": shown["confirm_token"], "user_quote": approval,
            })).data
        assert result["success"] and result["event"]["recorded"] == "local"
        assert "warnings" not in result and "options" not in result
        target = next(row for row in result["targets"] if row["path"] == str(install.installed))
        assert Path(target["backup"]).read_bytes() == customized
        assert install.installed.read_bytes() == install.script.read_bytes()
        [line] = infra._local_record_path("codex").read_bytes().splitlines(keepends=True)
        assert line.endswith(b"\n") and len(line) <= 1025
        record = json.loads(line)
        assert record["kind"] == "consent" and record["change"] == CHANGE and record["host"] == "codex"
        assert record["tool"] == "os_config_change" and record["session_id"] == ""
        assert record["user_quote"] and approval.startswith(record["user_quote"])
        assert record["target_count"] == len(result["targets"])
        target_digest = hashlib.sha256(json.dumps(result["targets"], sort_keys=True).encode()).hexdigest()
        assert record["targets_sha256"] == target_digest

        await asyncio.to_thread(api.start)
        body = {"host": "codex", "event": "UserPromptSubmit", "session_id": "offline-recovery",
                "facts": {"local_records": [record]}}
        async with httpx.AsyncClient(base_url=api.base, trust_env=False) as recovery:
            for _attempt in range(2):
                imported = await recovery.post("/api/notices/pending", json=body)
                imported.raise_for_status()
        await asyncio.to_thread(api.restart)
        async with httpx.AsyncClient(base_url=api.base, trust_env=False) as fresh:
            (await fresh.post("/api/notices/pending", json=body)).raise_for_status()
            response = await fresh.get("/api/events", params={"type": "decision.user_config_write"})
            response.raise_for_status()
            [event] = response.json()["data"]
            for field in ("change", "host", "session_id", "tool", "user_quote", "target_count", "targets_sha256"):
                assert event["data"][field] == record[field]
    finally:
        await asyncio.to_thread(api.stop)
        api.client.close()
