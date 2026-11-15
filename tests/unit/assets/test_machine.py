"""A folder's identity is (machine, host, path); the machine part is this Mac's hardware, hashed."""

from __future__ import annotations

import pytest

from aiteam.assets.machine import MachineKey, read_machine_key
from aiteam.assets.service import AssetBackup, project_roots
from aiteam.storage.assets import AssetRepository, root_id
from aiteam.types import AssetHost

from .conftest import MACHINE, close_stack, open_stack


def test_the_key_is_a_hash_of_the_hardware_id():
    first = read_machine_key(platform="darwin", read_uuid=lambda: "6A1B2C3D-0000-1111-2222-333344445555")
    again = read_machine_key(platform="darwin", read_uuid=lambda: "6a1b2c3d-0000-1111-2222-333344445555")
    other = read_machine_key(platform="darwin", read_uuid=lambda: "7A1B2C3D-0000-1111-2222-333344445555")
    assert first == again and len(first.key) == 32 and not first.refusal
    assert other.key != first.key and "6A1B2C3D" not in first.key.upper()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_are_refused_with_the_reason(platform):
    key = read_machine_key(platform=platform, read_uuid=lambda: "anything")
    assert key.key == "" and "macOS" in key.refusal


def test_an_unreadable_hardware_id_is_refused_never_empty():
    key = read_machine_key(platform="darwin", read_uuid=lambda: None)
    assert key.key == "" and key.refusal


def test_same_path_on_two_machines_are_two_folders():
    assert root_id("a" * 32, AssetHost.CLAUDE, "/same/config") != root_id("b" * 32, AssetHost.CLAUDE, "/same/config")


@pytest.mark.asyncio
async def test_enrolling_without_a_machine_key_is_refused(tmp_path, monkeypatch):
    stack = await open_stack(tmp_path, monkeypatch)
    try:
        stack.backup.machine = MachineKey(refusal="工作流备份目前只支持 macOS")
        response = await stack.client.post("/api/assets/roots", json={"profile": "main"})
        assert response.status_code == 400 and "macOS" in response.text
        assert stack.rows("SELECT count(*) FROM asset_roots") == [(0,)]
    finally:
        await close_stack(stack)


@pytest.mark.asyncio
async def test_a_folder_survives_a_sync_node_id_and_is_this_machines_only(tmp_path, monkeypatch):
    stack = await open_stack(tmp_path, monkeypatch)
    try:
        root = await stack.enroll()
        # A trailing slash or another spelling of the same literal path is the same folder.
        response = await stack.client.post("/api/assets/roots", json={"profile": "main", "path": f"{stack.folder}/"})
        assert response.json()["data"]["id"] == root["id"]
        url = stack.repo._db_url  # noqa: SLF001

        async def loaded(machine: MachineKey, node: str) -> list[str]:
            backup = AssetBackup(repo=AssetRepository(url), projects=lambda: project_roots(stack.repo),
                                 data_dir=stack.data_dir, node_id=node, machine=lambda: machine)
            await backup.start()
            return [r.id for r in backup.enrolled]

        assert await loaded(MACHINE, "air-9999") == [root["id"]]  # the node joined a sync: same folder
        assert await loaded(MachineKey(key="f" * 32), "") == []   # another machine's copy of the database
        assert await loaded(MachineKey(refusal="no key"), "") == []
    finally:
        await close_stack(stack)
