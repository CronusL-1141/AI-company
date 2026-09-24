"""Conversation-authorised config writes (docs/user-notice-design.md §5.9) and the notice tools."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.services import config_change
from aiteam.services.config_change import ConfigChangeError
from aiteam.services.notices import ledger
from aiteam.services.notices.detectors import installed_copies as copies
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

from .test_installed_copies import install_copies, make_source_tree

CONFIG_WRITE = EventType.DECISION_USER_CONFIG_WRITE.value


@pytest.fixture()
def stale_install(isolated_home, tmp_path, monkeypatch):
    root = make_source_tree(tmp_path / "checkout")
    monkeypatch.setattr(copies, "_api_source_root", lambda: root)
    copies._cache.clear()
    config = install_copies(isolated_home, root)
    hooks = config / "hooks" / "ai-team-os"
    (hooks / "send_event.py").write_text("old send_event\n", encoding="utf-8")
    (hooks / "user_notice.py").unlink()
    (config / "commands" / "os-doctor.md").write_text("old doctor\n", encoding="utf-8")
    return root, config


def _md5(path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_preview_lists_every_file_and_apply_writes_with_backups(stale_install):
    root, config = stale_install
    shown = config_change.preview("sync_installed_copies", now=1000)
    assert shown["baseline"] == {"root": str(root), "branch": "master", "version": "9.9.9"}
    assert shown["warnings"] == [] and shown["confirm_token"].startswith("1000.")
    actions = {target["path"].split("/.claude/", 1)[1]: target["action"] for target in shown["targets"]}
    assert actions == {"commands/os-doctor.md": "write", "hooks/ai-team-os/send_event.py": "write",
                       "hooks/ai-team-os/user_notice.py": "create"}
    assert "restart" in shown["summary"] and "No file is deleted" in shown["summary"]

    data = config_change.apply("sync_installed_copies", shown["confirm_token"], "好，同步吧", now=1100)
    for name in ("send_event.py", "user_notice.py"):
        assert _md5(config / "hooks" / "ai-team-os" / name) == _md5(root / "plugin" / "hooks" / name)
    backup = config / "hooks" / "ai-team-os" / ("send_event.py" + data["backup_suffix"])
    assert backup.read_text(encoding="utf-8") == "old send_event\n"
    created = next(item for item in data["targets"] if item["action"] == "create")
    assert created["backup"] == "" and created["before_sha256"] == ""
    assert data["user_quote"] == "好，同步吧" and data["notice_key"].startswith("installed_copy_stale:cc:")
    assert copies.compare_sync().stale == ()
    assert (config / "skills" / "mine" / "SKILL.md").exists()  # nothing outside the plan touched


def test_apply_refuses_when_files_changed_after_the_preview(stale_install):
    _root, config = stale_install
    token = config_change.preview("sync_installed_copies", now=1000)["confirm_token"]
    target = config / "hooks" / "ai-team-os" / "send_event.py"
    target.write_text("edited meanwhile\n", encoding="utf-8")
    with pytest.raises(ConfigChangeError, match="changed since the preview"):
        config_change.apply("sync_installed_copies", token, "yes", now=1010)
    assert target.read_text(encoding="utf-8") == "edited meanwhile\n"


@pytest.mark.parametrize(("quote", "delay", "match"), [
    ("", 10, "user_quote is required"),
    ("   ", 10, "user_quote is required"),
    ("yes", 601, "expired"),
    ("yes", -5, "expired"),
], ids=["empty-quote", "blank-quote", "expired", "future"])
def test_apply_needs_the_quote_and_a_fresh_token(stale_install, quote, delay, match):
    _root, config = stale_install
    token = config_change.preview("sync_installed_copies", now=1000)["confirm_token"]
    with pytest.raises(ConfigChangeError, match=match):
        config_change.apply("sync_installed_copies", token, quote, now=1000 + delay)
    assert (config / "hooks" / "ai-team-os" / "send_event.py").read_text(encoding="utf-8") == "old send_event\n"


def test_token_from_another_process_or_forged_is_refused(stale_install, monkeypatch):
    token = config_change.preview("sync_installed_copies", now=1000)["confirm_token"]
    monkeypatch.setattr(config_change, "_KEY", b"another process" * 2)
    with pytest.raises(ConfigChangeError, match="changed since the preview"):
        config_change.apply("sync_installed_copies", token, "yes", now=1010)
    with pytest.raises(ConfigChangeError, match="malformed"):
        config_change.apply("sync_installed_copies", "abc", "yes", now=1010)


def test_non_master_baseline_warns(stale_install):
    root, _config = stale_install
    (root / ".git" / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    shown = config_change.preview("sync_installed_copies")
    assert shown["baseline"]["branch"] == "feature" and "not master" in shown["warnings"][0]


def test_nothing_to_do_has_no_token(isolated_home, tmp_path, monkeypatch):
    root = make_source_tree(tmp_path / "checkout")
    monkeypatch.setattr(copies, "_api_source_root", lambda: root)
    copies._cache.clear()
    install_copies(isolated_home, root)
    shown = config_change.preview("sync_installed_copies")
    assert shown["nothing_to_do"] and shown["confirm_token"] == ""


def test_plugin_install_and_unknown_changes_are_refused(isolated_home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / ".codex"))
    before = sorted(path.relative_to(isolated_home).as_posix() for path in isolated_home.rglob("*"))
    with pytest.raises(ConfigChangeError, match="No source install"):
        config_change.preview("sync_installed_copies")
    with pytest.raises(ConfigChangeError, match="No valid Codex install receipt"):
        config_change.preview("update_codex_adapter")
    with pytest.raises(ConfigChangeError, match="Unknown change"):
        config_change.preview("rm_rf")
    assert sorted(path.relative_to(isolated_home).as_posix() for path in isolated_home.rglob("*")) == before


def test_compact_local_record_fits_one_line():
    data = {"change": "sync_installed_copies", "user_quote": "好" * 900,
            "targets": [{"path": f"/x/{index}", "before_sha256": "a" * 64} for index in range(40)],
            "baseline": {"root": "/r"}}
    slim = config_change.compact_for_local_record(data)
    assert slim["target_count"] == 40 and len(slim["targets_sha256"]) == 64
    assert len(json.dumps(slim, ensure_ascii=False).encode("utf-8")) < 1024


# ---------------------------------------------------------------------------
# Recording the decision event: API endpoint and local fallback, one event per uuid
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _client(db_url: str):
    repository = StorageRepository(db_url=db_url)
    await repository.init_db()
    deps._repository = repository
    deps._event_bus = EventBus(repo=repository)
    app = create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, repository
    finally:
        deps._repository = None
        deps._event_bus = None


async def _config_events(db_url):
    await close_db()
    fresh = StorageRepository(db_url=db_url)
    return await fresh.list_events(event_type=CONFIG_WRITE, limit=50)


async def test_consent_endpoint_is_idempotent_across_the_api_and_the_local_file(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
    record = {"uuid": "abcdef0123456789", "kind": "consent", "at": "2026-09-24T01:00:00Z",
              "change": "sync_installed_copies", "user_quote": "同步", "host": "cc",
              "targets": [{"path": "/x", "action": "write"}]}
    async with _client(db_url) as (client, _repo):
        assert (await client.post("/api/notices/consent", json=record)).json()["success"]
        assert (await client.post("/api/notices/consent", json=record)).status_code == 200
        bad = await client.post("/api/notices/consent", json={**record, "kind": "local_notice"})
        assert bad.status_code == 400
        smuggled = {**record, "uuid": "fedcba9876543210", "anything": "x" * 1000}
        assert (await client.post("/api/notices/consent", json=smuggled)).status_code == 200
    events = await _config_events(db_url)
    assert len(events) == 2 and all("anything" not in event.data for event in events)
    events = [event for event in events if event.data.get("user_quote") == "同步"]
    assert events[0].data["targets"] == [{"path": "/x", "action": "write"}]
    # The same record arriving later from a hook's local file lands on the same event.
    repo = StorageRepository(db_url=db_url)
    await ledger.import_local_records(repo, "cc", [record], events[0].timestamp)
    assert len(await _config_events(db_url)) == 2
    await close_db()


def _tools(module):
    collected = {}

    class _Collector:
        def tool(self, *args, **kwargs):
            def decorate(fn):
                collected[fn.__name__] = fn
                return fn
            return decorate

    module.register(_Collector())
    return collected


async def test_os_config_change_falls_back_to_the_local_record_when_the_api_is_down(stale_install, monkeypatch):
    from aiteam.mcp.tools import infra

    calls = []

    def api_down(method, path, body=None, **_kwargs):
        calls.append((method, path))
        return {"success": False, "error": "unreachable"}

    monkeypatch.setattr(infra, "_api_call", api_down)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    tool = _tools(infra)["os_config_change"]
    shown = await tool("sync_installed_copies")
    assert shown["mode"] == "preview" and ("POST", "/api/notices/consent") not in calls
    refused = await tool("sync_installed_copies", shown["confirm_token"], "")
    assert refused["success"] is False and "user_quote" in refused["error"]
    applied = await tool("sync_installed_copies", shown["confirm_token"], "同步 OS 装机面，确认")
    assert applied["success"] and applied["event"]["recorded"] == "local"
    assert ("POST", "/api/notices/consent") in calls
    lines = infra._local_record_path("cc").read_text(encoding="utf-8").splitlines()
    [record] = [json.loads(line) for line in lines]
    assert record["kind"] == "consent" and record["session_id"] == "sess-1"
    assert record["tool"] == "os_config_change" and record["user_quote"] == "同步 OS 装机面，确认"
    assert all(len(line.encode("utf-8")) <= 1024 for line in lines)


async def test_local_consent_record_becomes_one_event(stale_install, monkeypatch, repo):
    from aiteam.mcp.tools import infra

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-consent-import-test")
    monkeypatch.setattr(infra, "_api_call", lambda *a, **k: {"success": False})
    tool = _tools(infra)["os_config_change"]
    shown = await tool("sync_installed_copies")
    await tool("sync_installed_copies", shown["confirm_token"], "yes")
    [record] = [json.loads(line) for line in infra._local_record_path("cc").read_text().splitlines()]
    await ledger.import_local_records(repo, "cc", [record], ledger.utc_now())
    await ledger.import_local_records(repo, "cc", [record], ledger.utc_now())
    events = await repo.list_events(event_type=CONFIG_WRITE)
    assert len(events) == 1 and events[0].data["change"] == "sync_installed_copies"


def test_notice_tools_are_layered(monkeypatch):
    from aiteam.mcp.tools import notices

    seen = []

    def fake(method, path, body=None, **_kwargs):
        seen.append((method, path))
        if path.startswith("/api/notices?"):
            return {"items": [{"key": "k", "user_line": "l", "action": "a",
                               "last_delivery": {"event": "UserPromptSubmit", "emitted_at": "2026-09-24T01:00:00Z"},
                               "kind": "action", "status": "active"}], "total": 7, "language": "zh"}
        return {"success": True}

    monkeypatch.setattr(notices, "_api_call", fake)
    tools = _tools(notices)
    listed = tools["notice_list"](limit=500)
    assert listed["total"] == 7 and listed["limit"] == 100 and "last_delivery" not in listed["items"][0]
    assert listed["items"][0]["last_shown"] == "UserPromptSubmit 2026-09-24T01:00:00Z"
    assert seen[-1] == ("GET", "/api/notices?status=active&limit=100&fresh=1")
    assert tools["notice_list"](status="bogus")["success"] is False
    tools["notice_list"](key="unregistered_dir:/a/b")
    assert seen[-1] == ("GET", "/api/notices/unregistered_dir:%2Fa%2Fb")
    tools["notice_dismiss"]("k:1", hours=24)
    assert seen[-1] == ("POST", "/api/notices/k:1/snooze?hours=24.0")
    tools["notice_dismiss"]("k:1")
    assert seen[-1] == ("POST", "/api/notices/k:1/dismiss")


def test_apply_plan_executor_owns_the_write_set(monkeypatch):
    """The Codex batch hands the whole write to its adapter; token and quote stay here."""
    state = {"sha": "a" * 64}
    calls = []

    def plan():
        return config_change.Plan(
            change="fake_adapter",
            targets=(config_change.Target("/x/hook.py", "write", state["sha"], "b" * 64, "replace hook.py"),),
            baseline={"root": "/repo", "branch": "master"},
            payload={"schema": 1, "baseline": {"state_sha256": state["sha"]}},
        )

    def apply_plan(current):
        calls.append(current.payload)
        return {"targets": [{"path": "/x/hook.py", "action": "write", "backup": "/x/hook.py.bak"}]}

    monkeypatch.setitem(config_change.CHANGES, "fake_adapter", config_change.ChangeSpec(
        name="fake_adapter", description="test", plan=plan, apply_plan=apply_plan,
    ))
    token = config_change.preview("fake_adapter", now=1000)["confirm_token"]
    state["sha"] = "c" * 64  # the adapter's state drifted after the preview
    with pytest.raises(ConfigChangeError, match="changed since the preview"):
        config_change.apply("fake_adapter", token, "yes", now=1010)
    assert calls == []
    token = config_change.preview("fake_adapter", now=1000)["confirm_token"]
    data = config_change.apply("fake_adapter", token, "更新 Codex 适配器", now=1010)
    assert calls == [{"schema": 1, "baseline": {"state_sha256": "c" * 64}}]
    assert data["targets"][0]["backup"] == "/x/hook.py.bak" and data["user_quote"] == "更新 Codex 适配器"
    assert data["change"] == "fake_adapter" and data["baseline"]["branch"] == "master"


def test_register_change_fills_the_reserved_slot(monkeypatch):
    monkeypatch.setattr(config_change, "CHANGES", dict(config_change.CHANGES))
    monkeypatch.setattr(config_change, "RESERVED", dict(config_change.RESERVED))
    spec = config_change.ChangeSpec(name="update_codex_adapter", description="d",
                                    plan=lambda: config_change.Plan(change="update_codex_adapter", targets=()),
                                    apply_plan=lambda plan: {"targets": []})
    config_change.register_change(spec)
    assert "update_codex_adapter" not in config_change.RESERVED
    assert config_change.preview("update_codex_adapter")["nothing_to_do"]


def _fail_on_second_write(monkeypatch):
    real = config_change.CHANGES["sync_installed_copies"]
    count = {"n": 0}

    def write(target):
        count["n"] += 1
        if count["n"] == 2:
            raise OSError("disk full")
        real.write(target)

    monkeypatch.setitem(config_change.CHANGES, "sync_installed_copies",
                        config_change.ChangeSpec(real.name, real.description, real.plan, write=write))


def test_a_failed_write_reports_what_was_already_done(stale_install, monkeypatch):
    _fail_on_second_write(monkeypatch)
    token = config_change.preview("sync_installed_copies", now=1000)["confirm_token"]
    with pytest.raises(ConfigChangeError, match="disk full") as caught:
        config_change.apply("sync_installed_copies", token, "yes", now=1010)
    partial = caught.value.partial
    assert partial["status"] == "partial" and len(partial["targets"]) == 1
    assert partial["user_quote"] == "yes" and "disk full" in partial["error"]
    from pathlib import Path

    done = partial["targets"][0]
    assert Path(done["path"]).exists() and (done["backup"] == "" or Path(done["backup"]).exists())
    failed = partial["failed_target"]
    assert failed["action"] == "create" and failed["backup"] == ""  # a missing copy has nothing to back up


async def test_os_config_change_records_a_partial_write_and_the_codex_host(stale_install, monkeypatch):
    from aiteam.mcp.tools import infra

    monkeypatch.setattr(infra, "_api_call", lambda *a, **k: {"success": False})
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    _fail_on_second_write(monkeypatch)
    tool = _tools(infra)["os_config_change"]
    shown = await tool("sync_installed_copies")
    result = await tool("sync_installed_copies", shown["confirm_token"], "yes")
    assert result["success"] is False and result["status"] == "partial" and len(result["targets"]) == 1
    assert result["event"]["recorded"] == "local"
    [record] = [json.loads(line) for line in infra._local_record_path("codex").read_text().splitlines()]
    assert record["status"] == "partial" and record["host"] == "codex" and record["target_count"] == 1
    assert record["failed_path"].endswith("user_notice.py")


def test_preview_flags_copies_edited_on_this_machine(stale_install):
    import os

    root, config = stale_install
    edited = config / "commands" / "os-doctor.md"
    source = root / "plugin" / "commands" / "os-doctor.md"
    stat = source.stat()
    os.utime(source, (stat.st_atime, stat.st_mtime - 3600))  # the user's copy is newer
    shown = config_change.preview("sync_installed_copies")
    assert "modified on this machine" in shown["warnings"][0] and "commands/os-doctor.md" in shown["warnings"][0]
    summaries = {target["path"].rsplit("/", 1)[1]: target["summary"] for target in shown["targets"]}
    assert summaries["os-doctor.md"].startswith("replace locally modified")
    os.utime(edited, (stat.st_atime, stat.st_mtime - 7200))  # now the source is newer: plain lag
    shown = config_change.preview("sync_installed_copies")
    assert not any("modified on this machine" in warning for warning in shown["warnings"])



async def test_os_config_change_never_blocks_the_event_loop(stale_install, monkeypatch):
    """Planning may run git (the Codex adapter); the MCP server keeps serving meanwhile."""
    import asyncio
    import time

    from aiteam.mcp.tools import infra

    monkeypatch.setattr(infra, "_api_call", lambda *a, **k: {"success": False})
    real = config_change.CHANGES["sync_installed_copies"]

    def slow_plan():
        time.sleep(0.4)  # a blocking subprocess, as the adapter's preview does
        return real.plan()

    def slow_write(target):
        time.sleep(0.2)
        real.write(target)

    monkeypatch.setitem(config_change.CHANGES, "sync_installed_copies",
                        config_change.ChangeSpec(real.name, real.description, slow_plan, write=slow_write))
    tool = _tools(infra)["os_config_change"]
    beats = 0
    stop = False

    async def heartbeat():
        nonlocal beats
        while not stop:
            beats += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(heartbeat())
    try:
        shown = await tool("sync_installed_copies")
        during_preview = beats
        applied = await tool("sync_installed_copies", shown["confirm_token"], "yes")
    finally:
        stop = True
        await task
    assert applied["success"]
    # A blocked loop would leave the heartbeat at 0-1 beats across 0.4s of planning.
    assert during_preview > 50 and beats > during_preview + 50
