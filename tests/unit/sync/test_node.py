"""Reading node.json against this machine, and the governance scope each role gets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aiteam.sync import node
from aiteam.types import GovernanceScope, NodeIdentity, NodeRole

HERE, ELSEWHERE = "AAAAAAAA-0000-0000-0000-00000000000A", "BBBBBBBB-0000-0000-0000-00000000000B"


@pytest.fixture()
def machine(monkeypatch):
    def on(hardware: str | None) -> None:
        monkeypatch.setattr(node, "hardware_uuid", lambda: hardware)

    on(HERE)
    return on


def _write(tmp_path: Path, role: NodeRole, hardware: str = HERE) -> Path:
    path = node.node_file(tmp_path / "aiteam.db")
    node.write_node_identity(path, NodeIdentity(node_id="air-3f9c", role=role, hardware_uuid=hardware))
    return path


def test_no_file_is_standalone(tmp_path: Path, machine) -> None:
    status = node.load_node_status(node.node_file(tmp_path / "aiteam.db"))
    assert status.role is NodeRole.STANDALONE and status.node_id == "" and not status.refusal
    assert node.governance_scope(status) == GovernanceScope(origins=None, shared=True)


@pytest.mark.parametrize("role", [NodeRole.EDGE, NodeRole.CENTER])
def test_a_role_holds_on_the_machine_it_was_written_on(tmp_path: Path, machine, role: NodeRole) -> None:
    status = node.load_node_status(_write(tmp_path, role))
    assert status.role is role and status.node_id == "air-3f9c" and not status.refusal
    assert node.governance_scope(status) == GovernanceScope(("", "air-3f9c"), role is NodeRole.CENTER)


@pytest.mark.parametrize("role", [NodeRole.EDGE, NodeRole.CENTER])
@pytest.mark.parametrize("hardware", [ELSEWHERE, None], ids=["other-machine", "unreadable"])
def test_a_role_is_refused_where_the_hardware_does_not_match(
    tmp_path: Path, machine, role: NodeRole, hardware: str | None,
) -> None:
    path = _write(tmp_path, role)
    machine(hardware)
    status = node.load_node_status(path)
    assert status.role is NodeRole.STANDALONE and status.node_id == ""
    assert role.value in status.refusal and ("另一台机器" in status.refusal or "读不到" in status.refusal)
    assert node.governance_scope(status) == GovernanceScope()


def test_a_standalone_identity_travels_freely(tmp_path: Path, machine) -> None:
    path = _write(tmp_path, NodeRole.STANDALONE)
    machine(ELSEWHERE)
    status = node.load_node_status(path)
    assert status.role is NodeRole.STANDALONE and not status.refusal


@pytest.mark.parametrize("content", ["{not json", json.dumps({"node_id": "Air Book", "role": "edge"}),
                                     json.dumps({"node_id": "air-1", "role": "leader"})],
                         ids=["broken-json", "bad-node-id", "unknown-role"])
def test_an_unreadable_file_runs_standalone_and_says_so(tmp_path: Path, machine, content: str) -> None:
    path = node.node_file(tmp_path / "aiteam.db")
    path.write_text(content, encoding="utf-8")
    status = node.load_node_status(path)
    assert status.role is NodeRole.STANDALONE and str(path) in status.refusal


def test_a_new_identity_is_bound_to_this_machine(tmp_path: Path, machine) -> None:
    identity = node.new_node_identity("air", NodeRole.EDGE, center_url="http://127.0.0.1:18000")
    assert identity.node_id.startswith("air-") and len(identity.node_id) == 8
    assert identity.hardware_uuid == HERE
    machine(None)
    with pytest.raises(RuntimeError):
        node.new_node_identity("air", NodeRole.CENTER)


def test_the_hardware_identifier_is_stable() -> None:
    first = node.hardware_uuid()
    assert first == node.hardware_uuid()
    if sys.platform == "darwin":  # elsewhere a container may have no machine id at all
        assert first and len(first) == 36
