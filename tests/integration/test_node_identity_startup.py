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
from aiteam.sync.node import write_node_identity
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
            project = (await client.post("/api/projects", json={"name": "p", "root_path": "/work/p"})).json()
            status = (await client.get("/api/sync/status")).json()
        refused = sync_context.refused_role()
    print(json.dumps({"origins": reaper._scope.origins, "shared": reaper._scope.shared,
                      "natural_ids": natural, "project_id": project["data"]["id"],
                      "refused_role": refused, "healthy": status["healthy"], "problems": status["problems"]}))


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
    assert started["refused_role"] == "" and started["healthy"]
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
    assert stored["state"] == ("air-3f9c", 1)  # the shared face is captured
    # The migration turned 'idle' into 'waiting' unrecorded.
    assert stored["agent_status"] == "waiting"
    assert ("agents", agent_id, "U") not in stored["changes"]
    if center:
        # The HTTP write is a change the edges will pull.
        assert ("projects", started["project_id"], "I") in stored["changes"]
    else:
        # No center to reach: the write was staged as an operation, its row is tentative.
        assert stored["changes"] == []
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
    assert started["refused_role"] == role.value
    assert not started["healthy"] and "节点身份被拒" in started["problems"][0]
    assert _read(db, agent_id) == {"state": ("", 0), "changes": [], "agent_status": "waiting", "outbox": [],
                                   "tentative": []}
    assert "写于另一台机器" in (node_dir / "data" / "debug.log").read_text(encoding="utf-8")
