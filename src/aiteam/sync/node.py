"""The node identity file, the database's record of its node, and what they let this process do.

``node.json`` sits next to the database it describes (the OS data directory, or the
directory of an explicit ``AITEAM_DB_PATH``). Run time only reads it: install or an
explicit enable step writes it. No file and a database that never belonged to a sync
node means ``standalone``, the default, which behaves exactly as a single machine
always has.

The file names the hardware it was written on, and so does the database: each start
with an identity that holds records the node in ``sync_state`` (``registered_node``).
A database that knows it is a sync node's never falls back to standalone, which would
let it write rows nobody owns and govern rows of other machines (``decide``):

* the file holds on this machine: the node syncs (``syncing``);
* the file is missing, unreadable, from another machine, or the hardware cannot be
  read, and the database is registered to this machine (or the hardware cannot be
  read): the node keeps its registered id and role for its rows, capture and
  governance, its sync channel stops and its status is red (``held``);
* the database is registered to other hardware (a cloned disk or a copied database):
  writing under that node's id would give its rows a second writer, so rows get a
  temporary origin derived from this machine, nothing is captured or synced, and the
  status is red (``unbound``) until the user enables this machine as a node of its
  own or leaves the sync (``python3 -m aiteam.sync enable`` / ``leave``);
* otherwise: standalone, with the reason when a file was refused.

The node id is never the host name: cloned machines share host names.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.engine import Connection

from aiteam.storage.partition import edge_origins
from aiteam.types import GovernanceScope, NodeIdentity, NodeMode, NodeRegistration, NodeRole, NodeStatus

logger = logging.getLogger(__name__)

NODE_FILE = "node.json"

_IOREG_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')


def node_file(db_path: Path) -> Path:
    """Where the identity of the node owning ``db_path`` is kept."""
    return db_path.parent / NODE_FILE


def hardware_uuid() -> str | None:
    """This machine's hardware identifier, or None when it cannot be read.

    Only macOS reads a value bound to the hardware (IOPlatformUUID), which a cloned
    disk does not carry. Elsewhere a normal user usually gets ``/etc/machine-id``
    (Linux) or ``MachineGuid`` (Windows), both of which travel with a disk image: the
    copy guard holds against moved files there, not against cloned machines.

    Blocking (macOS asks ``ioreg``): call it at startup or from a worker thread.
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout
            match = _IOREG_UUID.search(out)
            return match.group(1).upper() if match else None
        if sys.platform.startswith("linux"):
            for source in ("/sys/class/dmi/id/product_uuid", "/etc/machine-id"):
                try:
                    value = Path(source).read_text(encoding="ascii").strip()
                except OSError:
                    continue
                if value:
                    return value.upper()
            return None
        if os.name == "nt":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0]).upper()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def read_identity(path: Path) -> tuple[NodeIdentity | None, str]:
    """The identity in ``node.json`` (None when there is none), and why a file there is unusable."""
    if not path.exists():
        return None, ""
    try:
        return NodeIdentity.model_validate_json(path.read_bytes()), ""
    except (OSError, ValidationError) as exc:
        return None, f"{path} 无法读取或格式不符：{exc}"


def registered_node(conn: Connection) -> NodeRegistration | None:
    """The sync node this database belongs to, or None: whether it is a sync node's database."""
    row = conn.exec_driver_sql(
        "SELECT registered_node, registered_role, hardware_uuid FROM sync_state WHERE id = 1").first()
    if row is None or not row[0] or row[1] not in (NodeRole.EDGE.value, NodeRole.CENTER.value):
        return None
    return NodeRegistration(row[0], NodeRole(row[1]), row[2] or "")


def register(conn: Connection, identity: NodeIdentity) -> None:
    """Record in the database the node it belongs to (an identity that holds here)."""
    conn.exec_driver_sql("INSERT OR IGNORE INTO sync_state (id, node_id, capture) VALUES (1, '', 0)")
    conn.exec_driver_sql(
        "UPDATE sync_state SET registered_node = ?, registered_role = ?, hardware_uuid = ? WHERE id = 1",
        (identity.node_id, identity.role.value, identity.hardware_uuid.upper()))


def forget_registration(conn: Connection) -> None:
    """The database leaves the sync (``python3 -m aiteam.sync leave``)."""
    conn.exec_driver_sql(
        "UPDATE sync_state SET registered_node = '', registered_role = '', hardware_uuid = '' WHERE id = 1")


def unbound_origin(hardware: str) -> str:
    """The temporary origin of rows written on a machine the database does not belong to."""
    return "unbound-" + hashlib.sha256(hardware.encode()).hexdigest()[:8]


def _not_syncing_because(identity: NodeIdentity | None, file_problem: str, hardware: str | None) -> str:
    """Why the identity file does not make this process sync ("" when there is no file at all)."""
    if identity is None:
        return file_problem
    if identity.role is NodeRole.STANDALONE:
        return "node.json 声明为 standalone"
    if not hardware:
        return f"读不到本机硬件标识，无法确认 node.json（{identity.role}）属于本机"
    return f"node.json（{identity.role}）写于另一台机器（硬件标识 {identity.hardware_uuid}，本机 {hardware}）"


def decide(identity: NodeIdentity | None, file_problem: str, registration: NodeRegistration | None,
           hardware: str | None) -> NodeStatus:
    """How this process takes part in the sync (see the module docstring). Pure."""
    declared = identity is not None and identity.role is not NodeRole.STANDALONE
    if identity is not None and declared and hardware and hardware == identity.hardware_uuid.upper():
        return NodeStatus(identity=identity, role=identity.role, mode=NodeMode.SYNCING, origin=identity.node_id)
    why = _not_syncing_because(identity, file_problem, hardware)
    if registration is None:
        refused = declared or bool(file_problem)  # a standalone file, or none, is no refusal
        return NodeStatus(identity=identity, refusal=f"{why}，本库不属于同步节点，按单机运行" if refused else "")
    if hardware and hardware != registration.hardware_uuid.upper():
        origin = unbound_origin(hardware)
        return NodeStatus(identity=identity, mode=NodeMode.UNBOUND, origin=origin, refusal=(
            f"本库登记为另一台机器的 {registration.node_id}（硬件标识 {registration.hardware_uuid}，本机 {hardware}）"
            f"{'；' + why if why else ''}。为免同一来源有两个写者，本机以临时来源 {origin} 记录、不同步"))
    reasons = [why or "node.json 不存在"]
    if not hardware and not declared:
        reasons.append("读不到本机硬件标识，无法确认本库属于本机")
    return NodeStatus(identity=identity, role=registration.role, mode=NodeMode.HELD, origin=registration.node_id,
                      refusal=f"{'；'.join(reasons)}：本库登记为 {registration.node_id}（{registration.role}），"
                              "继续以这个身份记录本机的行，同步通道已停")


def load_node_status(path: Path, registration: NodeRegistration | None = None) -> NodeStatus:
    """Read ``node.json`` and decide against this machine and the database's record
    (blocking: reads the hardware, see hardware_uuid)."""
    identity, problem = read_identity(path)
    needs_hardware = registration is not None or (identity is not None and identity.role is not NodeRole.STANDALONE)
    return decide(identity, problem, registration, hardware_uuid() if needs_hardware else None)


def new_node_identity(name: str, role: NodeRole, *, center_url: str = "") -> NodeIdentity:
    """A fresh identity for this machine: ``<name>-<4 hex>``, bound to its hardware."""
    machine = hardware_uuid()
    if role is not NodeRole.STANDALONE and not machine:
        raise RuntimeError(f"读不到本机硬件标识，不能创建 {role} 节点")
    return NodeIdentity(
        node_id=f"{name}-{secrets.token_hex(2)}", role=role, center_url=center_url,
        hardware_uuid=machine or "",
    )


def write_node_identity(path: Path, identity: NodeIdentity) -> None:
    """Write ``node.json`` atomically. For install and explicit enable steps only."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(identity.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def governance_scope(status: NodeStatus) -> GovernanceScope:
    """Standalone governs everything; a sync node only its own session rows.

    Session governance (liveness, resetting stuck agents, closing stale session
    teams, running wakes) belongs to the node the session runs on: only it sees the
    transcripts and processes. Rows with an empty origin predate the sync; they come
    from the baseline copy of the edge's database, so the edge governs them and the
    center leaves them alone. Shared-face governance runs on the center (and
    standalone) only. A node whose identity is held keeps its scope; an unbound one
    governs only the rows of its temporary origin.
    """
    if status.mode is NodeMode.UNBOUND:
        return GovernanceScope(origins=(status.origin,), shared=True)
    if status.role is NodeRole.STANDALONE:
        return GovernanceScope()
    if status.role is NodeRole.CENTER:
        return GovernanceScope(origins=(status.node_id,), shared=True)
    return GovernanceScope(origins=edge_origins(status.node_id), shared=False)


def log_node_status(status: NodeStatus) -> None:
    if status.refusal:
        logger.warning("Multi-host sync: %s", status.refusal)
    elif status.role is not NodeRole.STANDALONE:
        logger.info("Multi-host sync: node %s runs as %s", status.node_id, status.role)
