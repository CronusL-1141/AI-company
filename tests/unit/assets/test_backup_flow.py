"""The workflow backup end to end through the API: enroll, first backup, fast path, comparison.

Every assertion reads the database file or the content store, never the objects a
request built.
"""

from __future__ import annotations

import json
import os

import pytest

from aiteam.assets import process, service
from aiteam.assets.blob_store import BlobStore
from aiteam.assets.os_manifest import build_claude_manifest
from aiteam.assets.secret_scan import CORPUS
from aiteam.clock import utc_now
from aiteam.services.notices.catalog import CATALOG, render_entry
from aiteam.services.notices.detectors import DetectContext
from aiteam.services.notices.detectors.asset_backup import AssetBackupDetector
from aiteam.types import AssetHost

from .conftest import (
    MACHINE,
    NODE,
    PROJECT_ROOT,
    PROJECT_SLUG,
    SECRETS,
    UNBOUND_SLUG,
    close_stack,
    dump_database,
    open_stack,
    read_store,
)

MEMORY = f"projects/{PROJECT_SLUG}/memory"


async def _notice(stack) -> list:
    """What the session-start notice check finds (the detector, through its registry contract)."""
    ctx = DetectContext(host="cc", event="SessionStart", source="startup", session_id="s1", cwd=PROJECT_ROOT,
                        project_id=stack.project_id, facts={}, now=utc_now(), repo=stack.repo)
    detector = AssetBackupDetector()
    assert detector.applies(ctx) and detector.scope(ctx).prefixes == ("asset_backup_attention:",)
    return await detector.detect(ctx)


def _token() -> str:
    return next(text for entry in CORPUS if entry.rule == "github_token" for text in entry.must_hit)


def _state(stack, rel: str) -> tuple | None:
    rows = stack.rows("SELECT status, item_id, content_hash FROM asset_root_state WHERE rel_path = ?", rel)
    return rows[0] if rows else None


def _versions(stack, rel_like: str = "%") -> list[tuple]:
    return stack.rows(
        "SELECT i.category, i.owner_kind, i.owner_ref, i.owner_subpath, i.rel_path, v.content_hash, v.source, "
        "v.session_id, v.source_node, v.root_id, v.parent_hash, v.executable, i.os_template "
        "FROM asset_versions v JOIN asset_items i ON i.id = v.item_id WHERE i.rel_path LIKE ? ORDER BY v.hlc",
        rel_like)


@pytest.mark.asyncio
async def test_first_backup_keeps_the_users_assets_and_nothing_of_os(stack):
    root = await stack.enroll()
    assert root["status"] == "enrolled" and root["machine_key"] == MACHINE.key and root["profile_name"] == "rec"
    stored = {(row[0], row[1]) for row in stack.rows(
        "SELECT category, rel_path FROM asset_root_state WHERE status = 'synced'")}
    expected = {(category, rel) for category, rels in stack.expected.items() for rel in rels}
    assert stored == expected
    # One version per asset, all of this folder, this node, recorded as an explicit run.
    versions = _versions(stack)
    assert len(versions) == len(expected)
    assert {v[6] for v in versions} == {"manual"} and {v[8] for v in versions} == {NODE}
    assert {v[9] for v in versions} == {root["id"]}
    # OS's own files: none became an item, except the template the user changed.
    manifest = build_claude_manifest()
    marks = ",".join("?" * len(manifest.files))
    os_items = stack.rows(f"SELECT rel_path, os_template FROM asset_items WHERE rel_path IN ({marks})",
                          *manifest.files)
    assert len(os_items) == 1 and os_items[0][1] == 1
    assert _state(stack, "loop.md")[0] == "os_asset"


@pytest.mark.asyncio
async def test_project_memory_belongs_to_the_project_not_the_path(stack):
    await stack.enroll()
    rows = {(v[1], v[2], v[3], v[4]) for v in _versions(stack) if v[0] == "project_memory"}
    assert rows == {("project", stack.project_id, "", "MEMORY.md"), ("project", stack.project_id, "", "notes.md"),
                    ("unbound", UNBOUND_SLUG, "", "home.md")}


@pytest.mark.asyncio
async def test_executable_bit_is_kept(stack):
    await stack.enroll()
    [version] = _versions(stack, "hooks/guard.sh")
    assert version[11] == 1


@pytest.mark.asyncio
async def test_a_write_tool_is_backed_up_within_the_turn(stack):
    await stack.enroll()
    path = stack.folder / MEMORY / "notes.md"
    path.write_text("The API listens on port 8000.\nNew fact.\n", encoding="utf-8")
    await stack.hook("PostToolUse", session_id="s-edit", tool_name="Edit", tool_use_id="t-1",
                     tool_input={"file_path": str(path), "old_string": "x", "new_string": "y"})
    await stack.settle()  # no Stop, no SessionEnd: the write alone triggered it
    first, latest = _versions(stack, "notes.md")
    assert latest[6] == "event" and latest[7] == "s-edit" and latest[8] == NODE
    assert latest[10] == first[5]  # the folder's previous content is the parent
    assert BlobStore(stack.data_dir).get(latest[5]) == path.read_bytes()


@pytest.mark.asyncio
async def test_a_write_outside_the_folder_or_to_a_non_asset_is_ignored(stack):
    await stack.enroll()
    before = len(_versions(stack))
    outside = stack.folder.parent / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")
    cache = stack.folder / "cache" / "x.json"
    for index, target in enumerate((outside, cache)):
        await stack.hook("PostToolUse", tool_name="Write", tool_use_id=f"t-o{index}",
                         tool_input={"file_path": str(target), "content": "x"})
    await stack.settle()
    assert len(_versions(stack)) == before


@pytest.mark.asyncio
async def test_a_shell_change_is_caught_at_the_end_of_the_turn(stack):
    await stack.enroll()
    await stack.hook("Stop")
    await stack.settle()
    claude = stack.folder / "CLAUDE.md"
    claude.write_text("# Global rules\nChanged by a shell command.\n", encoding="utf-8")
    # Right after the last comparison, a turn end without a shell command is throttled...
    await stack.hook("Stop")
    await stack.settle()
    assert len(_versions(stack, "CLAUDE.md")) == 1
    # ...but a shell command in the turn makes the next turn end compare at once.
    await stack.hook("PostToolUse", tool_name="Bash", tool_use_id="t-b", tool_input={"command": "echo > CLAUDE.md"})
    await stack.hook("Stop", session_id="s1")
    await stack.settle()
    latest = _versions(stack, "CLAUDE.md")[-1]
    assert latest[6] == "reconcile" and BlobStore(stack.data_dir).get(latest[5]) == claude.read_bytes()


@pytest.mark.asyncio
async def test_precompact_checkpoint_compares_the_folder(stack, monkeypatch):
    await stack.enroll()
    monkeypatch.setattr(service, "RECONCILE_MIN_INTERVAL_S", 0.0)
    # A tool event (no comparison) is how the API learns the session's folder.
    await stack.hook("PostToolUse", session_id="s-pc", tool_name="Read", tool_use_id="t-r",
                     tool_input={"file_path": str(stack.folder / "CLAUDE.md")})
    await stack.settle()
    (stack.folder / "agents" / "my-writer.md").write_text("changed outside any tool\n", encoding="utf-8")
    response = await stack.client.post("/api/hooks/compact-checkpoint",
                                       json={"session_id": "s-pc", "cwd": PROJECT_ROOT})
    assert response.status_code == 200
    await stack.settle()
    assert _versions(stack, "agents/my-writer.md")[-1][6] == "reconcile"


@pytest.mark.asyncio
async def test_deleted_file_gets_a_tombstone_and_history_stays(stack):
    await stack.enroll()
    (stack.folder / "agents" / "my-writer.md").unlink()
    await stack.client.post("/api/assets/run", json={})
    await stack.settle()
    first, tombstone = _versions(stack, "agents/my-writer.md")
    assert first[5] and tombstone[5] is None and tombstone[10] == first[5]
    assert _state(stack, "agents/my-writer.md") is None
    assert BlobStore(stack.data_dir).has(first[5])  # the old content is kept


@pytest.mark.asyncio
async def test_unchanged_folder_adds_nothing(stack):
    await stack.enroll()
    before = len(_versions(stack))
    for _ in range(2):
        response = await stack.client.post("/api/assets/run", json={})
        assert response.json()["data"]["versions"] == 0
    assert len(_versions(stack)) == before


@pytest.mark.asyncio
async def test_a_touched_but_unchanged_file_adds_no_version(stack):
    """A new mtime makes the comparison read the file; the same content is not a new version."""
    await stack.enroll()
    path = stack.folder / "CLAUDE.md"
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    response = await stack.client.post("/api/assets/run", json={})
    assert response.json()["data"]["files"] == 1 and response.json()["data"]["versions"] == 0
    assert len(_versions(stack, "CLAUDE.md")) == 1
    # The comparison list took the new mtime, so the next run does not read it again.
    assert (await stack.client.post("/api/assets/run", json={})).json()["data"]["files"] == 0


@pytest.mark.asyncio
async def test_removed_mcp_server_and_changed_hook_registration(stack):
    await stack.enroll()
    config = json.loads((stack.folder / ".claude.json").read_text())
    config["mcpServers"].pop("remote")
    (stack.folder / ".claude.json").write_text(json.dumps(config))
    settings = json.loads((stack.folder / "settings.json").read_text())
    settings["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "bash ~/bin/second.sh"})
    (stack.folder / "settings.json").write_text(json.dumps(settings))
    await stack.client.post("/api/assets/run", json={})
    await stack.settle()
    assert _versions(stack, "mcp/remote.json")[-1][5] is None
    latest = _versions(stack, "hooks/registration.json")[-1]
    assert b"second.sh" in BlobStore(stack.data_dir).get(latest[5])


@pytest.mark.asyncio
async def test_nothing_outside_the_whitelist_is_touched_or_kept(stack, tmp_path, monkeypatch):
    settings_before = (stack.folder / "settings.json").read_bytes()
    global_before = (stack.folder / ".claude.json").read_bytes()
    await stack.enroll()
    await stack.hook("Stop")
    await stack.settle()
    assert (stack.folder / "settings.json").read_bytes() == settings_before
    assert (stack.folder / ".claude.json").read_bytes() == global_before

    def leaked() -> list[str]:
        everything = dump_database(stack.database) + "\n".join(
            content.decode("utf-8", "replace") for content in read_store(stack.data_dir))
        return [secret for secret in SECRETS if secret in everything]

    assert leaked() == []
    # Across a restart: a new process (engine, service) over the same files says the same.
    await close_stack(stack)
    reopened = await open_stack(tmp_path, monkeypatch, build=False)
    try:
        assert [root.id for root in reopened.backup.enrolled]
        response = await reopened.client.get("/api/assets/status", params={"host": "claude"})
        [root] = response.json()["data"]["roots"]
        assert sum(root["backed_up"].values()) == sum(len(v) for v in stack.expected.values())
        assert leaked() == []
    finally:
        await close_stack(reopened)


@pytest.mark.asyncio
async def test_a_suspected_secret_is_held_until_the_user_allows_it(stack):
    await stack.enroll()
    path = stack.folder / MEMORY / "notes.md"
    path.write_text(f"Token for the demo: {_token()}\n", encoding="utf-8")
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-s1",
                     tool_input={"file_path": str(path), "content": "..."})
    await stack.settle()
    status, _item, held_hash = _state(stack, f"{MEMORY}/notes.md")
    assert status == "blocked" and len(_versions(stack, "notes.md")) == 1
    assert not BlobStore(stack.data_dir).has(held_hash)
    # The notice names the file; the status lists the hit without its text.
    [finding] = await _notice(stack)
    line = render_entry(CATALOG[finding.catalog_id], language="zh", host="cc", params=finding.params).plain
    assert "notes.md" in line
    data = (await stack.client.get("/api/assets/status")).json()["data"]
    hits = [f for f in data["pending_findings"]["items"] if f["rel_path"].endswith("notes.md")]
    assert {f["rule"] for f in hits} >= {"github_token"} and {f["line"] for f in hits} == {1}
    hit = hits[0]  # one answer covers every hit on that content
    assert _token() not in json.dumps(data) and _token() not in dump_database(stack.database)
    # Allowed: that exact content uploads.
    response = await stack.client.post("/api/assets/findings/resolve",
                                       json={"finding_id": hit["finding_id"], "action": "allow",
                                             "user_quote": "这是演示用的假令牌，放行"})
    assert response.json()["data"]["disposition"] == "allowed"
    await stack.settle()
    latest = _versions(stack, "notes.md")[-1]
    assert latest[5] == held_hash and BlobStore(stack.data_dir).get(held_hash) == path.read_bytes()
    assert await _notice(stack) == []
    # A new content with a hit is scanned again.
    path.write_text(f"Another one: {_token()}x\n", encoding="utf-8")
    await stack.hook("PostToolUse", tool_name="Edit", tool_use_id="t-s2",
                     tool_input={"file_path": str(path), "old_string": "a", "new_string": "b"})
    await stack.settle()
    assert _state(stack, f"{MEMORY}/notes.md")[0] == "blocked"
    assert _versions(stack, "notes.md")[-1][5] == held_hash


@pytest.mark.asyncio
async def test_excluding_a_path_stops_its_backup(stack):
    await stack.enroll()
    path = stack.folder / "CLAUDE.md"
    path.write_text(f"key: {_token()}\n", encoding="utf-8")
    await stack.client.post("/api/assets/run", json={})
    hit = (await stack.client.get("/api/assets/status")).json()["data"]["pending_findings"]["items"][0]
    await stack.client.post("/api/assets/findings/resolve",
                            json={"finding_id": hit["finding_id"], "action": "exclude", "user_quote": "别备份它"})
    await stack.settle()
    assert _state(stack, "CLAUDE.md")[0] == "excluded"
    path.write_text("clean now\n", encoding="utf-8")
    await stack.client.post("/api/assets/run", json={})
    await stack.settle()
    assert _state(stack, "CLAUDE.md")[0] == "excluded" and len(_versions(stack, "CLAUDE.md")) == 1
    assert await _notice(stack) == []


def test_oversize_files_are_listed_not_read(tmp_path):
    path = tmp_path / "big.md"
    path.write_text("x" * 100, encoding="utf-8")
    job = process.Job(data_dir=str(tmp_path / "data"), max_bytes=64,
                      files=[process.FileJob("project_memory", f"{MEMORY}/big.md", str(path))])
    [result] = process.run(job).assets
    assert result.status == "oversize" and result.hash == "" and result.blob is None
    assert not (tmp_path / "data").exists()


@pytest.mark.asyncio
async def test_registering_the_project_later_moves_unbound_memory(stack):
    await stack.enroll()
    await stack.repo.create_project(name="home", root_path="/home/someone")
    (stack.folder / "projects" / UNBOUND_SLUG / "memory" / "home.md").write_text("Home notes, edited.\n")
    await stack.client.post("/api/assets/run", json={})
    await stack.settle()
    rows = _versions(stack, "home.md")
    old = [r for r in rows if r[1] == "unbound"]
    new = [r for r in rows if r[1] == "project"]
    assert [r[6] for r in old] == ["manual", "rebound"] and old[-1][5] is None
    assert len(new) == 1 and new[0][5]


@pytest.mark.asyncio
async def test_enroll_validation(stack):
    bad = [({"path": "relative/dir"}, 400), ({"path": str(stack.folder / "missing")}, 400),
           ({"host": "codex"}, 400), ({"profile": "", "path": str(stack.folder)}, 400)]
    for body, code in bad:
        response = await stack.client.post("/api/assets/roots", json={"profile": "rec", **body})
        assert response.status_code == code, (body, response.text)
    # The session's own folder is the default, and its profile is remembered.
    response = await stack.client.post("/api/assets/roots", json={"profile": "rec"})
    assert response.json()["data"]["path"] == str(stack.folder)
    response = await stack.client.post("/api/assets/roots", json={"action": "ignore"})
    assert response.json()["data"]["status"] == "ignored" and response.json()["data"]["profile_name"] == "rec"
    await stack.settle()
    assert stack.backup.enrolled == []


@pytest.mark.asyncio
async def test_ignored_folder_is_not_read_again(stack):
    await stack.enroll()
    await stack.client.post("/api/assets/roots", json={"action": "ignore"})
    before = len(_versions(stack))
    (stack.folder / "CLAUDE.md").write_text("changed\n")
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-i",
                     tool_input={"file_path": str(stack.folder / "CLAUDE.md"), "content": "changed"})
    await stack.hook("SessionEnd")
    await stack.settle()
    assert len(_versions(stack)) == before


@pytest.mark.asyncio
async def test_status_lists_folders_counts_and_problems(stack):
    root = await stack.enroll()
    data = (await stack.client.get("/api/assets/status", params={"host": AssetHost.CLAUDE})).json()["data"]
    [listed] = data["roots"]
    assert listed["id"] == root["id"] and listed["this_machine"] and listed["last_backup_at"]
    assert listed["backed_up"]["project_memory"] == 3 and listed["backed_up"]["agents"] == 3
    assert data["session_root_id"] == root["id"] and data["stored_bytes"] > 0
    assert (await stack.client.get("/api/assets/status", params={"host": "codex"})).json()["data"]["roots"] == []


@pytest.mark.asyncio
async def test_a_new_empty_folder_records_no_deletions(tmp_path, monkeypatch):
    stack = await open_stack(tmp_path, monkeypatch, build=False)
    try:
        await stack.enroll()
        assert stack.rows("SELECT count(*) FROM asset_versions") == [(0,)]
    finally:
        await close_stack(stack)


@pytest.mark.asyncio
async def test_without_an_enrolled_folder_hook_events_write_no_asset_rows(stack):
    path = stack.folder / MEMORY / "notes.md"
    await stack.hook("SessionStart")
    await stack.hook("PostToolUse", tool_name="Edit", tool_use_id="t-n",
                     tool_input={"file_path": str(path), "old_string": "a", "new_string": "b"})
    await stack.hook("Stop")
    await stack.hook("SessionEnd")
    await stack.settle()
    for table in ("asset_profiles", "asset_roots", "asset_items", "asset_versions", "asset_blobs",
                  "asset_scan_findings", "asset_root_state", "asset_restore_plans"):
        assert stack.rows(f"SELECT count(*) FROM {table}") == [(0,)], table
    assert not (stack.data_dir / "asset-store").exists()
