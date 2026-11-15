"""What the backup must never do: read through links, take a failed look for a deletion,
keep a credential, create the data folder, or enroll a folder that is not a config folder.

Every check reads the database file or the content store.
"""

from __future__ import annotations

import json
import os
import shutil
import stat

import pytest

from aiteam.assets.blob_store import BlobStore
from aiteam.assets.os_manifest import build_claude_manifest, source_root
from aiteam.assets.secret_scan import CORPUS
from aiteam.assets.service import FOLDER_MISSING, STORE_MISSING, UNREADABLE_DIR, AssetBackup, project_roots
from aiteam.clock import utc_now
from aiteam.services.notices.catalog import CATALOG, render_entry
from aiteam.services.notices.detectors import DetectContext
from aiteam.services.notices.detectors.asset_backup import AssetBackupDetector
from aiteam.storage.assets import AssetRepository

from .conftest import MACHINE, OS_HOOK, PROJECT_ROOT, PROJECT_SLUG, SECRETS, dump_database, read_store

MEMORY = f"projects/{PROJECT_SLUG}/memory"
pytestmark = pytest.mark.asyncio


def _versions(stack, rel_like: str = "%") -> list[tuple]:
    return stack.rows("SELECT i.rel_path, v.content_hash, v.source FROM asset_versions v "
                      "JOIN asset_items i ON i.id = v.item_id WHERE i.rel_path LIKE ? ORDER BY v.hlc", rel_like)


def _tombstones(stack) -> int:
    return stack.rows("SELECT count(*) FROM asset_versions WHERE content_hash IS NULL")[0][0]


def _state(stack) -> dict[str, str]:
    return dict(stack.rows("SELECT rel_path, status FROM asset_root_state"))


async def _notice(stack) -> list:
    ctx = DetectContext(host="cc", event="SessionStart", source="startup", session_id="s1", cwd=PROJECT_ROOT,
                        project_id=stack.project_id, facts={}, now=utc_now(), repo=stack.repo)
    return await AssetBackupDetector().detect(ctx)


def _everything(stack) -> str:
    """The database dump and every stored content, as text."""
    return dump_database(stack.database) + "".join(c.decode("utf-8", "replace") for c in read_store(stack.data_dir))


def _github_token() -> str:
    return next(entry for entry in CORPUS if entry.rule == "github_token").must_hit[0].split()[-1]


def _random(n: int) -> str:
    alphabet = "Qz7Xk2Lp9Rm4Tv8Wn3Bc6Hd1Fg5Js0"
    return "".join(alphabet[(i * 11 + 5) % len(alphabet)] for i in range(n))


# -- B2: a failed look is not a deletion --------------------------------------------


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads through chmod 000")
async def test_an_unreadable_folder_records_no_deletion(stack):
    await stack.enroll()
    before, state_before = len(_versions(stack)), _state(stack)
    projects = stack.folder / "projects"
    projects.chmod(0)
    try:
        response = await stack.client.post("/api/assets/run", json={})
        assert response.json()["data"]["versions"] == 0 and response.json()["data"]["unreadable"] == ["projects"]
        assert _tombstones(stack) == 0 and _state(stack) == state_before
        [root] = (await stack.client.get("/api/assets/status")).json()["data"]["roots"]
        assert root["not_backed_up"][UNREADABLE_DIR]["items"][0]["rel_path"] == "projects"
        assert [f.params["file"] for f in await _notice(stack)] == ["projects"]
    finally:
        projects.chmod(0o755)
    response = await stack.client.post("/api/assets/run", json={})
    assert response.json()["data"]["versions"] == 0 and len(_versions(stack)) == before
    assert await _notice(stack) == []


async def test_a_moved_config_folder_pauses_and_records_nothing(stack):
    await stack.enroll()
    before, state_before = len(_versions(stack)), _state(stack)
    moved = stack.folder.with_name("moved-away")
    stack.folder.rename(moved)
    try:
        response = await stack.client.post("/api/assets/run", json={})
        assert response.json()["success"] is False and "配置目录" in response.json()["data"]["error"]
        assert len(_versions(stack)) == before and _tombstones(stack) == 0 and _state(stack) == state_before
        [finding] = await _notice(stack)
        assert finding.variant == FOLDER_MISSING and finding.params["file"] == str(stack.folder)
        line = render_entry(CATALOG[finding.catalog_id], variant=finding.variant, language="zh", host="cc",
                            params=finding.params).plain
        assert "已暂停" in line
    finally:
        moved.rename(stack.folder)
    response = await stack.client.post("/api/assets/run", json={})
    assert response.json()["success"] is True and response.json()["data"]["versions"] == 0
    assert await _notice(stack) == []


async def test_a_folder_really_deleted_is_still_a_deletion(stack):
    await stack.enroll()
    shutil.rmtree(stack.folder / "skills" / "my-skill")
    await stack.client.post("/api/assets/run", json={})
    assert sorted(rel for rel, content_hash, _ in _versions(stack, "skills/%") if content_hash is None) == [
        "skills/my-skill/SKILL.md", "skills/my-skill/templates/a.txt"]


async def test_a_vanished_written_path_waits_for_the_comparison(stack):
    """The fast path never records a deletion: it asks for a comparison, which lists the folder."""
    await stack.enroll()
    path = stack.folder / MEMORY / "notes.md"
    path.unlink()
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-gone",
                     tool_input={"file_path": str(path), "content": "x"})
    await stack.settle()
    [(_, tombstone, source)] = [v for v in _versions(stack, "notes.md") if v[1] is None]
    assert tombstone is None and source == "reconcile"


async def test_a_config_file_caught_mid_write_removes_nothing(stack):
    await stack.enroll()
    (stack.folder / ".claude.json").write_text('{"mcpServers": {"remote": {"type": "ht', encoding="utf-8")
    response = await stack.client.post("/api/assets/run", json={})
    assert response.json()["data"]["versions"] == 0 and _tombstones(stack) == 0
    assert _state(stack)["mcp/remote.json"] == "synced"


# -- B5: links are never followed ----------------------------------------------------


async def test_links_are_listed_and_never_read(stack, tmp_path):
    outside = tmp_path / "dotfiles"
    (outside / "linked-skill").mkdir(parents=True)
    (outside / "linked-skill" / "SKILL.md").write_text("from a dotfiles repo\n", encoding="utf-8")
    (outside / "outside.md").write_text("outside the folder\n", encoding="utf-8")
    memory = stack.folder / MEMORY
    os.symlink(outside / "linked-skill", stack.folder / "skills" / "linked-skill")
    os.symlink(stack.folder / ".credentials.json", memory / "cred.md")
    os.symlink(stack.folder / "settings.json", memory / "settings.md")
    os.symlink(outside / "outside.md", memory / "outside.md")
    await stack.enroll()
    state = _state(stack)
    for rel in ("skills/linked-skill", f"{MEMORY}/cred.md", f"{MEMORY}/settings.md", f"{MEMORY}/outside.md"):
        assert state[rel] == "symlink", rel
    everything = _everything(stack)
    for leaked in (SECRETS[4], SECRETS[1], "from a dotfiles repo", "outside the folder"):
        assert leaked not in everything
    # The fast path decides a written path below a link the same way: nothing recorded, then or later.
    before = len(_versions(stack))
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-link",
                     tool_input={"file_path": str(stack.folder / "skills/linked-skill/SKILL.md"), "content": "x"})
    await stack.settle()
    for _ in range(2):
        await stack.client.post("/api/assets/run", json={})
    assert len(_versions(stack)) == before and _tombstones(stack) == 0
    notices = await _notice(stack)
    assert notices and notices[0].params["n"] == 4


async def test_a_linked_settings_file_is_not_followed_and_removes_nothing(stack, tmp_path):
    await stack.enroll()
    real = tmp_path / "settings-real.json"
    shutil.move(stack.folder / "settings.json", real)
    os.symlink(real, stack.folder / "settings.json")
    await stack.client.post("/api/assets/run", json={})
    assert _state(stack)["settings.json"] == "symlink"
    assert _tombstones(stack) == 0 and _state(stack)["hooks/registration.json"] == "synced"


# -- B3: credentials inside definitions ----------------------------------------------


async def test_mcp_and_hook_definitions_keep_no_credential(stack):
    tokens = [_random(40 + n) for n in range(6)]
    config = json.loads((stack.folder / ".claude.json").read_text())
    config["mcpServers"].update({
        "bridge": {"command": "npx", "args": ["mcp-remote", "https://mcp.example.com/sse", "--header",
                                               f"Authorization: Bearer {tokens[0]}"]},
        "search": {"command": "search-mcp", "args": ["--api-key", tokens[1], f"--token={tokens[2]}"]},
        "pathkey": {"type": "http", "url": f"https://mcp.example.com/k/{tokens[3]}/sse?key={tokens[4]}&mode=fast"},
    })
    (stack.folder / ".claude.json").write_text(json.dumps(config))
    settings = json.loads((stack.folder / "settings.json").read_text())
    settings["hooks"]["Stop"][0]["hooks"].append({"type": "http", "url": "https://hooks.example.com/notify",
                                                  "headers": {"Authorization": f"Bearer {tokens[5]}"}})
    (stack.folder / "settings.json").write_text(json.dumps(settings))
    await stack.enroll()
    stored = {rel: json.loads(BlobStore(stack.data_dir).get(h)) for rel, h in stack.rows(
        "SELECT rel_path, content_hash FROM asset_root_state WHERE status = 'synced' "
        "AND (rel_path LIKE 'mcp/%' OR rel_path = 'hooks/registration.json')")}
    assert stored["mcp/bridge.json"]["args"] == ["mcp-remote", "https://mcp.example.com/sse", "--header",
                                                 "<redacted>"]
    assert stored["mcp/search.json"]["args"] == ["--api-key", "<redacted>", "--token=<redacted>"]
    assert stored["mcp/pathkey.json"]["url"] == "https://mcp.example.com/k/<redacted>/sse?key=<redacted>&mode=<redacted>"
    everything = _everything(stack)
    assert [token for token in tokens if token in everything] == []


async def test_a_definition_still_holding_a_credential_is_refused(stack):
    token = _github_token()
    config = json.loads((stack.folder / ".claude.json").read_text())
    config["mcpServers"]["gh"] = {"command": "gh-mcp", "args": ["serve", token]}
    (stack.folder / ".claude.json").write_text(json.dumps(config))
    await stack.enroll()
    assert _state(stack)["mcp/gh.json"] == "blocked"
    assert token not in dump_database(stack.database)


# -- B4: the data folder is never created ---------------------------------------------


async def test_a_missing_data_folder_stops_the_backup_and_is_not_recreated(stack, tmp_path):
    root = await stack.enroll()
    gone = tmp_path / "gone-data"
    backup = AssetBackup(repo=AssetRepository(stack.repo._db_url), projects=lambda: project_roots(stack.repo),  # noqa: SLF001
                         data_dir=gone, machine=lambda: MACHINE)
    await backup.start()
    (stack.folder / "CLAUDE.md").write_text("changed\n", encoding="utf-8")
    summary = await backup.run_now(root["id"], timeout=30)
    assert summary.error and not gone.exists()
    [item] = await backup.attention()
    assert item.status == STORE_MISSING
    await backup.drain(5)


async def test_the_store_refuses_a_missing_data_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        BlobStore(tmp_path / "missing").put(b"x")
    assert not (tmp_path / "missing").exists()


async def test_store_files_are_readable_by_the_owner_only(stack):
    await stack.enroll()
    store = stack.data_dir / "asset-store"
    for path in [store, *store.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), (path, oct(mode))


# -- S1: only config folders, and an audited answer ---------------------------------


async def test_only_a_known_config_folder_can_be_enrolled(stack, tmp_path):
    repo_dir = tmp_path / "some-repo"
    (repo_dir / "commands").mkdir(parents=True)
    (repo_dir / "commands" / "deploy.md").write_text("deploy\n", encoding="utf-8")
    response = await stack.client.post("/api/assets/roots", json={"profile": "main", "path": str(repo_dir)})
    assert response.status_code == 400 and "配置目录" in response.text
    assert stack.rows("SELECT count(*) FROM asset_roots") == [(0,)]


async def test_answering_a_scan_hit_is_audited_with_the_users_words(stack):
    await stack.enroll()
    (stack.folder / "CLAUDE.md").write_text(f"Demo token {_github_token()}\n", encoding="utf-8")
    await stack.client.post("/api/assets/run", json={})
    hit = (await stack.client.get("/api/assets/status")).json()["data"]["pending_findings"]["items"][0]
    refused = await stack.client.post("/api/assets/findings/resolve",
                                      json={"finding_id": hit["finding_id"], "action": "allow"})
    assert refused.status_code == 422  # the user's words are required
    await stack.client.post("/api/assets/findings/resolve", headers={"X-CC-Session-Id": "s-user"},
                            json={"finding_id": hit["finding_id"], "action": "allow", "user_quote": "是假的，放行"})
    [(data,)] = stack.rows("SELECT data FROM events WHERE type = 'asset.scan_resolved'")
    audit = json.loads(data)
    assert audit["user_quote"] == "是假的，放行" and audit["session_id"] == "s-user"
    assert audit["rel_path"] == "CLAUDE.md" and len(audit["content_hash"]) == 64 and audit["disposition"] == "allowed"


# -- S6: guards that must keep holding -------------------------------------------------


async def test_os_hook_registrations_are_never_stored(stack):
    await stack.enroll()
    [(content_hash,)] = stack.rows("SELECT content_hash FROM asset_root_state WHERE rel_path = ?",
                                   "hooks/registration.json")
    stored = BlobStore(stack.data_dir).get(content_hash).decode()
    assert "notify.sh" in stored and "ai-team-os/send_event.py" not in stored and OS_HOOK not in stored


async def test_codex_events_do_not_touch_a_claude_folder(stack):
    await stack.enroll()
    path = stack.folder / MEMORY / "notes.md"
    path.write_text("changed by a codex session\n", encoding="utf-8")
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-codex", harness="codex",
                     tool_input={"file_path": str(path), "content": "x"})
    await stack.settle()
    assert len(_versions(stack, "notes.md")) == 1


async def test_a_deselected_category_is_not_backed_up_by_the_fast_path(stack):
    await stack.enroll(categories=["project_memory", "global_instructions"])
    path = stack.folder / "agents" / "my-writer.md"
    path.write_text("changed\n", encoding="utf-8")
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-desel",
                     tool_input={"file_path": str(path), "content": "x"})
    await stack.settle()
    assert _versions(stack, "agents/%") == []


# -- S7: OS templates are judged by the current release, every time ---------------------


async def test_an_os_template_back_at_the_release_is_not_backed_up_again(stack):
    await stack.enroll()
    manifest = build_claude_manifest()
    [(rel,)] = stack.rows("SELECT rel_path FROM asset_items WHERE os_template = 1")
    template = stack.folder / rel
    template.write_bytes((source_root() / "plugin" / rel).read_bytes())  # the user installed the update
    assert manifest.is_release(rel, template.read_bytes())
    await stack.client.post("/api/assets/run", json={})
    assert _state(stack)[rel] == "os_asset" and len(_versions(stack, rel)) == 1
    template.write_bytes(template.read_bytes() + b"\nchanged again\n")
    await stack.client.post("/api/assets/run", json={})
    assert _state(stack)[rel] == "synced" and len(_versions(stack, rel)) == 2
