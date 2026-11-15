"""Which machine a config folder is on: the key enrolled folders are told apart by.

A folder's identity is (machine, host, path). The machine part is a hash of this
Mac's hardware identifier (``aiteam.sync.node.hardware_uuid``): stable across
reinstalls and node roles, different on a cloned disk. It is never the sync node
id (empty until a node joins a sync, then changing) nor the host name (cloned
machines share it).

macOS only: elsewhere the readable identifiers (``/etc/machine-id``, ``MachineGuid``)
travel with a disk image, so two cloned machines would enroll the same folder id
and their histories would merge. Enrolling is refused there, and when the
identifier cannot be read, rather than falling back to an empty key.

Blocking (asks ``ioreg``): read once at startup, off the event loop.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from dataclasses import dataclass

from aiteam.sync.node import hardware_uuid

_SALT = "aiteam-asset-machine"


@dataclass(frozen=True)
class MachineKey:
    """This machine's key, or why there is none (then nothing may be enrolled)."""

    key: str = ""
    refusal: str = ""


def read_machine_key(*, platform: str | None = None,
                     read_uuid: Callable[[], str | None] = hardware_uuid) -> MachineKey:
    """This machine's key (``platform`` and ``read_uuid`` default to the real ones)."""
    if (platform or sys.platform) != "darwin":
        return MachineKey(refusal=(
            "工作流备份目前只支持 macOS：其他平台读得到的机器标识会随磁盘镜像复制，"
            "克隆出的机器会得到同一个配置目录 ID，两台机器的历史会混在一起"))
    uuid = read_uuid()
    if not uuid:
        return MachineKey(refusal="读不到本机硬件标识（IOPlatformUUID），无法区分本机的配置目录，拒绝登记")
    return MachineKey(key=hashlib.sha256(f"{_SALT}\x1f{uuid.upper()}".encode()).hexdigest()[:32])

