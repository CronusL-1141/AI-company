"""On an edge of the multi-host sync the workflow backup pauses until its cross-machine stage.

The asset tables are shared: their rows belong to the center, and an edge writes the
shared face only through operations. The backup writes them directly, so on an edge
it would leave writes outside any operation (orphans the sync reports and an
operator's revert would undo). Until the backup's cross-machine stage it pauses on an
edge: hook events are not queued, enrolling, running and answering a scan hit are
refused with the reason, and the status and the E30 notice say so. Read back from
the database through fresh connections.
"""

from __future__ import annotations

from aiteam.assets.machine import MachineKey
from aiteam.assets.service import EDGE_PAUSE, PAUSED_EDGE, AssetBackup, project_roots
from aiteam.clock import utc_now
from aiteam.services.notices.detectors import DetectContext
from aiteam.services.notices.detectors.asset_backup import AssetBackupDetector
from aiteam.storage.assets import AssetRepository

from .conftest import MACHINE, NODE, PROJECT_SLUG

ASSET_TABLES = ("asset_profiles", "asset_roots", "asset_items", "asset_versions", "asset_blobs",
                "asset_scan_findings", "asset_root_state")


def _counts(stack) -> dict[str, int]:
    return {table: stack.rows(f"SELECT count(*) FROM {table}")[0][0] for table in ASSET_TABLES}


async def _as_edge(stack, monkeypatch, machine: MachineKey = MACHINE) -> AssetBackup:
    """The same node restarted as an edge: a paused backup over the same database."""
    from aiteam.api import deps

    paused = AssetBackup(repo=AssetRepository(stack.repo.db_url), projects=lambda: project_roots(stack.repo),
                         data_dir=stack.data_dir, node_id=NODE, machine=lambda: machine, paused=EDGE_PAUSE)
    await paused.start()
    stack.client._transport.app.dependency_overrides[deps.get_asset_backup] = lambda: paused  # noqa: SLF001
    monkeypatch.setattr(deps, "_asset_backup", paused)
    stack.backup = paused
    return paused


async def test_an_edge_writes_no_asset_row_and_refuses_asset_writes_with_the_reason(stack, monkeypatch) -> None:
    root = await stack.enroll()
    await _as_edge(stack, monkeypatch)
    before = _counts(stack)
    memory = stack.folder / "projects" / PROJECT_SLUG / "memory" / "notes.md"
    memory.write_text("Changed while the node is an edge.\n")
    await stack.hook("PostToolUse", tool_name="Write", tool_input={"file_path": str(memory)})
    await stack.hook("Stop")
    response = await stack.client.post("/api/hooks/compact-checkpoint", json={"session_id": "s1", "cwd": "/work"})
    assert response.status_code == 200  # PreCompact, the third way in
    await stack.settle()
    assert _counts(stack) == before
    for path, body in (("/api/assets/roots", {"profile": "rec", "path": str(stack.folder)}),
                       ("/api/assets/run", {"root_id": root["id"]}),
                       ("/api/assets/findings/resolve", {"finding_id": "f-1", "action": "allow",
                                                         "user_quote": "yes"})):
        response = await stack.client.post(path, json=body)
        assert response.status_code == 409, (path, response.text)
        assert "跨机阶段未上线" in response.json()["detail"]
    assert _counts(stack) == before
    status = (await stack.client.get("/api/assets/status")).json()["data"]
    assert "跨机阶段未上线" in status["paused"]


async def test_the_notice_names_every_paused_folder_of_this_machine(stack, monkeypatch) -> None:
    root = await stack.enroll()
    paused = await _as_edge(stack, monkeypatch)
    [item] = await paused.attention()
    assert (item.root.id, item.status) == (root["id"], PAUSED_EDGE)
    from aiteam.api import deps

    monkeypatch.setattr(deps, "asset_backup_if_started", lambda: paused)
    context = DetectContext(host="cc", event="session_start", source="test", session_id="s1", cwd="",
                            project_id="", facts={}, now=utc_now(), repo=stack.repo)
    [finding] = await AssetBackupDetector().detect(context)
    assert finding.variant == PAUSED_EDGE and finding.params == {"n": 1, "file": str(stack.folder)}


async def test_a_backup_that_is_not_paused_is_unchanged(stack) -> None:
    await stack.enroll()
    assert stack.backup.paused == "" and await stack.backup.attention() == []
    before = _counts(stack)["asset_versions"]
    memory = stack.folder / "projects" / PROJECT_SLUG / "memory" / "notes.md"
    memory.write_text("Changed on a center or a standalone node.\n")
    await stack.hook("PostToolUse", tool_name="Write", tool_input={"file_path": str(memory)})
    await stack.settle()
    assert _counts(stack)["asset_versions"] == before + 1
