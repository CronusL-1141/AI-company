"""The node identity at API startup: a copied node.json never makes a node edge or center.

Each case starts the real app (lifespan with init_db, startup migrations, dependency
wiring) in its own process over a temporary data directory, with this machine's
hardware identifier pinned, makes one write through HTTP, and stops. The database is
then read back through a fresh connection.

* No node.json: standalone, as every installation is by default.
* An edge or center identity written on this machine: capture on, the node id in the
  clock, governance narrowed, ids derived from natural keys; the startup migration's
  rewrite of an old agent status is not captured.
* The same file on another machine (a cloned disk, a restored backup): refused, the
  node runs standalone and the log says why.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.sync import node as node_module
from aiteam.sync.node import write_node_identity
from aiteam.sync.tables import SHARED_TABLES
from aiteam.types import NodeIdentity, NodeRole

ROOT = Path(__file__).resolve().parents[2]
MACHINE_A, MACHINE_B = "AAAAAAAA-0000-0000-0000-00000000000A", "BBBBBBBB-0000-0000-0000-00000000000B"

START = r"""
import asyncio, json, sys
sys.path.insert(0, sys.argv[2])
import sync_equivalence_driver as driver
from aiteam.sync import node as node_module

node_module.hardware_uuid = lambda: sys.argv[1]
driver.quiet_background()


async def main():
    import httpx
    from aiteam.api import deps
    from aiteam.api.app import create_app
    from aiteam import ids

    from aiteam.sync import context as sync_context

    app = create_app()
    async with app.router.lifespan_context(app):
        reaper, natural = deps.get_reaper(), ids.natural_ids_enabled()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            root = "/work/p-" + str(len(await deps.get_repository().list_projects()))  # one per start
            project = (await client.post("/api/projects", json={"name": "p", "root_path": root})).json()
            status = (await client.get("/api/sync/status")).json()
        trouble = sync_context.identity_trouble()
    print(json.dumps({"origins": reaper._scope.origins, "shared": reaper._scope.shared,
                      "natural_ids": natural, "project_id": project["data"]["id"],
                      "trouble": trouble, "healthy": status["healthy"], "problems": status["problems"]}))


asyncio.run(main())
"""


async def _seed(db: Path) -> str:
    """A database from an earlier version: one agent still carries the retired 'idle' status."""
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db}")
    await repo.init_db()
    team = await repo.create_team(name="old-team", mode="coordinate")
    agent = await repo.create_agent(team_id=team.id, name="old", role="member")
    await close_db()
    con = sqlite3.connect(db)
    con.execute("UPDATE agents SET status = 'idle', last_active_at = NULL WHERE id = ?", (agent.id,))
    con.commit()
    con.close()
    return agent.id


def _start(tmp_path: Path, machine: str) -> dict:
    data = tmp_path / "data"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "HOME": str(tmp_path / "home"),
           "AITEAM_DATA_DIR": str(data), "AITEAM_DB_PATH": str(data / "aiteam.db"),
           "AITEAM_DIAGNOSTICS_ENABLED": "0"}
    proc = subprocess.run([sys.executable, "-c", START, machine, str(Path(__file__).parent)],
                          capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _read(db: Path, agent_id: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            "state": con.execute("SELECT node_id, capture FROM sync_state").fetchone(),
            "changes": con.execute("SELECT table_name, pk, kind FROM sync_changes ORDER BY seq").fetchall(),
            "agent_status": con.execute("SELECT status FROM agents WHERE id = ?", (agent_id,)).fetchone()[0],
            "outbox": con.execute("SELECT state, staged FROM sync_outbox").fetchall(),
            "tentative": con.execute("SELECT table_name, pk FROM sync_tentative").fetchall(),
        }
    finally:
        con.close()


@pytest.fixture()
def node_dir(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "home" / ".aiteam").mkdir(parents=True)
    return tmp_path


async def test_without_node_json_the_node_is_standalone(node_dir: Path) -> None:
    db = node_dir / "data" / "aiteam.db"
    agent_id = await _seed(db)
    started = _start(node_dir, MACHINE_A)
    assert started["origins"] is None and started["shared"] and not started["natural_ids"]
    assert started["trouble"] is None and started["healthy"]
    stored = _read(db, agent_id)
    assert stored == {"state": ("", 0), "changes": [], "agent_status": "waiting", "outbox": [], "tentative": []}


@pytest.mark.parametrize("role", [NodeRole.EDGE, NodeRole.CENTER])
async def test_an_identity_written_here_runs_as_its_role(node_dir: Path, role: NodeRole) -> None:
    db = node_dir / "data" / "aiteam.db"
    agent_id = await _seed(db)
    write_node_identity(node_dir / "data" / "node.json",
                        NodeIdentity(node_id="air-3f9c", role=role, hardware_uuid=MACHINE_A))
    started = _start(node_dir, MACHINE_A)
    center = role is NodeRole.CENTER
    # The center governs its own sessions only; the edge also the baseline's (empty origin).
    assert started["origins"] == (["air-3f9c"] if center else ["", "air-3f9c"]) and started["natural_ids"]
    assert started["shared"] is center
    stored = _read(db, agent_id)
    # Both capture the shared face; the edge also its session face, which goes up to the center.
    assert stored["state"] == ("air-3f9c", 1 if center else 3)
    # The migration turned 'idle' into 'waiting' unrecorded.
    assert stored["agent_status"] == "waiting"
    assert ("agents", agent_id, "U") not in stored["changes"]
    if center:
        # The HTTP write is a change the edges will pull.
        assert ("projects", started["project_id"], "I") in stored["changes"]
    else:
        # No center to reach: the write was staged as an operation, its row is tentative;
        # what is left in the change log is the edge's session face waiting to go up.
        assert [change for change in stored["changes"] if change[0] in SHARED_TABLES] == []
        assert stored["outbox"] == [("pending", 1)]
        assert ("projects", started["project_id"]) in stored["tentative"]


@pytest.mark.parametrize("role", [NodeRole.EDGE, NodeRole.CENTER])
async def test_a_copied_identity_is_refused_on_another_machine(node_dir: Path, role: NodeRole) -> None:
    db = node_dir / "data" / "aiteam.db"
    agent_id = await _seed(db)
    write_node_identity(node_dir / "data" / "node.json",
                        NodeIdentity(node_id="air-3f9c", role=role, hardware_uuid=MACHINE_A))
    started = _start(node_dir, MACHINE_B)
    assert started["origins"] is None and started["shared"] and not started["natural_ids"]
    # Not silent: a pending notice names the refused role, the sync health check is red.
    assert started["trouble"] == ["standalone", role.value]
    assert not started["healthy"] and "节点身份被拒" in started["problems"][0]
    assert _read(db, agent_id) == {"state": ("", 0), "changes": [], "agent_status": "waiting", "outbox": [],
                                   "tentative": []}
    assert "写于另一台机器" in (node_dir / "data" / "debug.log").read_text(encoding="utf-8")



@pytest.mark.parametrize("role", [NodeRole.EDGE, NodeRole.CENTER])
async def test_a_sync_nodes_database_without_its_identity_file_keeps_its_node(node_dir: Path, role: NodeRole) -> None:
    """The first start records the node in the database; without node.json the next start
    keeps it (held): same origin and capture, governance as the role has it, red."""
    db = node_dir / "data" / "aiteam.db"
    agent_id = await _seed(db)
    identity_file = node_dir / "data" / "node.json"
    write_node_identity(identity_file, NodeIdentity(node_id="air-3f9c", role=role, hardware_uuid=MACHINE_A))
    first = _start(node_dir, MACHINE_A)
    identity_file.unlink()
    held = _start(node_dir, MACHINE_A)
    assert held["origins"] == first["origins"] and held["shared"] == first["shared"] and held["natural_ids"]
    assert held["trouble"] == ["held", role.value]
    assert not held["healthy"] and any("同步通道已停" in problem for problem in held["problems"])
    assert _read(db, agent_id)["state"] == ("air-3f9c", 1 if role is NodeRole.CENTER else 3)


async def test_a_database_cloned_to_other_hardware_writes_under_a_temporary_origin(node_dir: Path) -> None:
    db = node_dir / "data" / "aiteam.db"
    agent_id = await _seed(db)
    identity_file = node_dir / "data" / "node.json"
    write_node_identity(identity_file, NodeIdentity(node_id="air-3f9c", role=NodeRole.EDGE, hardware_uuid=MACHINE_A))
    _start(node_dir, MACHINE_A)
    identity_file.unlink()
    clone = _start(node_dir, MACHINE_B)
    origin = node_module.unbound_origin(MACHINE_B)
    assert clone["origins"] == [origin] and clone["shared"] and clone["trouble"] == ["unbound", "edge"]
    assert not clone["healthy"] and any("临时来源" in problem for problem in clone["problems"])
    assert _read(db, agent_id)["state"] == (origin, 0)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT origin_node FROM projects WHERE id = ?", (clone["project_id"],)).fetchone() == (
            origin,)
        assert con.execute("SELECT registered_node FROM sync_state").fetchone() == ("air-3f9c",)
    finally:
        con.close()
