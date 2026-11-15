"""The node identity file and what it lets this process do.

``node.json`` sits next to the database it describes (the OS data directory, or the
directory of an explicit ``AITEAM_DB_PATH``). Run time only reads it: install or an
explicit enable step writes it. No file means ``standalone``, the default, which
behaves exactly as a single machine always has.

The file names the hardware it was written on. A copy that reaches another machine
(a cloned disk, a restored backup, a sync tool) is refused as edge or center: the
process runs standalone and says why. Two machines both believing they are the same
node, or both believing they are the center, is the failure this prevents. The node
id is never the host name: cloned machines share host names.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from aiteam.types import GovernanceScope, NodeIdentity, NodeRole, NodeStatus

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


def load_node_status(path: Path) -> NodeStatus:
    """Read ``node.json`` and check it against this machine (blocking, see hardware_uuid)."""
    if not path.exists():
        return NodeStatus()
    try:
        identity = NodeIdentity.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        return NodeStatus(refusal=f"{path} 无法读取或格式不符，按 standalone 运行：{exc}")
    if identity.role is NodeRole.STANDALONE:
        return NodeStatus(identity=identity)
    actual = hardware_uuid()
    if not actual:
        refusal = f"读不到本机硬件标识，无法确认 {path} 属于本机，拒绝以 {identity.role} 运行"
    elif actual != identity.hardware_uuid.upper():
        refusal = (f"{path} 写于另一台机器（硬件标识 {identity.hardware_uuid}，本机 {actual}），"
                   f"拒绝以 {identity.role} 运行：节点身份文件不可随拷贝或克隆带到别的机器")
    else:
        return NodeStatus(identity=identity, role=identity.role)
    return NodeStatus(identity=identity, refusal=refusal)


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
    """Standalone governs everything; a syncing node only its own session rows.

    Session governance (liveness, resetting stuck agents, closing stale session
    teams, running wakes) belongs to the node the session runs on: only it sees the
    transcripts and processes. Rows with an empty origin predate the sync; they come
    from the baseline copy of the edge's database, so the edge governs them and the
    center leaves them alone. Shared-face governance runs on the center (and
    standalone) only.
    """
    if status.role is NodeRole.STANDALONE:
        return GovernanceScope()
    if status.role is NodeRole.CENTER:
        return GovernanceScope(origins=(status.node_id,), shared=True)
    return GovernanceScope(origins=("", status.node_id), shared=False)


def log_node_status(status: NodeStatus) -> None:
    if status.refusal:
        logger.warning("Multi-host sync: %s", status.refusal)
    elif status.role is not NodeRole.STANDALONE:
        logger.info("Multi-host sync: node %s runs as %s", status.node_id, status.role)
