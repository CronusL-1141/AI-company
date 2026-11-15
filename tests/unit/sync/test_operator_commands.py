"""The operator's side of multi-host sync: commands, the center's service, the refusal notice.

* ``python3 -m aiteam.sync``: every command that changes something only prints its
  plan unless given --apply; ``update-center`` runs its steps over ssh in order and
  stops at the first failure (a stand-in ssh records what it was asked to run).
* The launchd job of a center only keeps the API alive: it schedules nothing, and
  is refused on a machine whose node is not the center.
* An identity refused at startup surfaces as a pending notice.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import plistlib
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.sync import cli
from aiteam.sync import context as sync_context
from aiteam.sync import node as node_module
from aiteam.types import NodeIdentity, NodeRole

ROOT = Path(__file__).resolve().parents[3]
MACHINE = "AAAAAAAA-0000-0000-0000-00000000000A"


def _database(path: Path) -> Path:
    async def build() -> None:
        await StorageRepository(db_url=f"sqlite+aiosqlite:///{path}").init_db()
        await close_db()

    asyncio.run(build())
    return path


@pytest.fixture()
def machine(monkeypatch):
    monkeypatch.setattr(node_module, "hardware_uuid", lambda: MACHINE)


def test_enable_prints_its_plan_and_writes_only_when_applied(tmp_path: Path, machine, capsys) -> None:
    db = _database(tmp_path / "aiteam.db")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO sync_changes (table_name, pk, kind, hlc) VALUES ('tasks', 't', 'I', 'h')")
    con.commit()
    con.close()
    assert cli.main(["enable", "--role", "center", "--name", "mini", "--db", str(db)]) == 0
    assert "baseline_seq = 1" in capsys.readouterr().out and not (tmp_path / "node.json").exists()
    assert cli.main(["enable", "--role", "center", "--name", "mini", "--db", str(db), "--apply"]) == 0
    identity = NodeIdentity.model_validate_json((tmp_path / "node.json").read_text())
    assert identity.role is NodeRole.CENTER and identity.hardware_uuid == MACHINE
    assert identity.node_id.startswith("mini-")
    assert sqlite3.connect(db).execute("SELECT baseline_seq FROM sync_state").fetchone() == (1,)
    assert node_module.load_node_status(tmp_path / "node.json").role is NodeRole.CENTER


def test_an_edge_points_at_the_tunnel_by_default(tmp_path: Path, machine) -> None:
    db = _database(tmp_path / "aiteam.db")
    assert cli.main(["enable", "--role", "edge", "--name", "air", "--db", str(db), "--apply"]) == 0
    identity = NodeIdentity.model_validate_json((tmp_path / "node.json").read_text())
    assert identity.role is NodeRole.EDGE and identity.center_url == "http://127.0.0.1:18000"


def test_a_baseline_with_two_clock_conventions_is_refused(tmp_path: Path, capsys) -> None:
    a, b = _database(tmp_path / "a.db"), _database(tmp_path / "b.db")
    assert cli.main(["check-baseline", str(a), str(b)]) == 0
    con = sqlite3.connect(b)
    con.execute("PRAGMA user_version = 1")
    con.close()
    assert cli.main(["check-baseline", str(a), str(b)]) == 1
    assert "user_version" in capsys.readouterr().out


def test_compare_finds_a_row_that_differs_and_ignores_node_local_columns(tmp_path: Path) -> None:
    a, b = _database(tmp_path / "a.db"), _database(tmp_path / "b.db")
    for db, link_id, accessed in ((a, 1, "2026-01-01"), (b, 7, "2026-02-02")):
        con = sqlite3.connect(db)
        con.execute("INSERT INTO knowledge_links (id, from_kind, from_id, to_kind, to_id, link_type, link_source, "
                    "context, project_id, created_at) VALUES (?, 'task', 't', 'report', 'r', 'cites', 'auto', '{}', "
                    "'', '2026-01-01')", (link_id,))
        con.execute("INSERT INTO memories (id, scope, scope_id, content, kind, metadata, source_refs, created_at, "
                    "accessed_at) VALUES ('m', 'global', 'system', 'c', 'preference', '{}', '[]', '2026-01-01', ?)",
                    (accessed,))
        con.commit()
        con.close()
    assert cli.shared_face_differences(a, b) == []
    con = sqlite3.connect(b)
    con.execute("UPDATE memories SET content = 'changed'")
    con.commit()
    con.close()
    assert cli.shared_face_differences(a, b) == ["memories m: 列 ['content'] 不同"]


def _fake_ssh(tmp_path: Path, fail_on: str = "") -> Path:
    log = tmp_path / "ssh.log"
    script = tmp_path / "bin" / "ssh"
    script.parent.mkdir()
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\ncase "$*" in *"{fail_on or "@@never@@"}"*) exit 3;; esac\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return log


def test_update_center_plans_then_runs_its_steps_in_order_and_stops_at_a_failure(tmp_path, monkeypatch) -> None:
    log = _fake_ssh(tmp_path, fail_on="install.py")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    assert cli.main(["update-center", "--host", "mini", "--repo", "/srv/ai team os"]) == 0
    assert not log.exists()  # a dry run asks ssh for nothing
    assert cli.main(["update-center", "--host", "mini", "--repo", "/srv/ai team os", "--apply"]) == 3
    ran = log.read_text().splitlines()
    assert [line.split(" ", 1)[1] for line in ran] == cli.center_update_steps("/srv/ai team os")[:3]
    assert all(line.startswith("mini ") for line in ran)
    assert "git pull --ff-only" in ran[1] and "'/srv/ai team os'" in ran[1]


def _launchd_module():
    script = ROOT / "scripts" / "install_center_launchd.py"
    spec = importlib.util.spec_from_file_location("install_center_launchd", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_center_service_only_keeps_the_api_alive(tmp_path: Path) -> None:
    launchd = _launchd_module()
    plist = plistlib.loads(launchd.render(python=sys.executable, repo=tmp_path, port=8000, log=tmp_path / "x.log",
                                          path="/opt/homebrew/bin:/usr/bin:/home/u/.local/bin").encode())
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    # launchd's bare PATH would hide gh, claude and codex from the API.
    assert plist["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin:/home/u/.local/bin"
    assert not set(plist) & set(launchd.SCHEDULING_KEYS)
    assert plist["ProgramArguments"][-5:] == ["--host", "127.0.0.1", "--port", "8000", "--factory"]


def test_the_center_service_is_refused_on_a_node_that_is_not_the_center(tmp_path: Path, machine) -> None:
    launchd = _launchd_module()
    db = tmp_path / "aiteam.db"
    assert "standalone" in launchd.center_refusal(db)
    node_module.write_node_identity(tmp_path / "node.json", NodeIdentity(
        node_id="air-3f9c", role=NodeRole.EDGE, hardware_uuid=MACHINE))
    assert "edge" in launchd.center_refusal(db)
    node_module.write_node_identity(tmp_path / "node.json", NodeIdentity(
        node_id="mini-c0de", role=NodeRole.CENTER, hardware_uuid=MACHINE))
    assert launchd.center_refusal(db) == ""


async def test_a_refused_identity_is_a_pending_notice() -> None:
    from aiteam.services.notices.detectors.sync_identity import SyncIdentityDetector

    detector = SyncIdentityDetector()
    try:
        assert await detector.detect(None) == []  # type: ignore[arg-type]
        sync_context.set_process_node("", "standalone", refused_role="edge")
        findings = await detector.detect(None)  # type: ignore[arg-type]
        assert [(f.catalog_id, f.params) for f in findings] == [("sync_identity_refused", {"role": "edge"})]
    finally:
        sync_context.set_process_node("", "standalone")


def test_enable_keeps_an_identity_that_holds_here_and_replaces_one_from_elsewhere(tmp_path, machine, capsys) -> None:
    db = _database(tmp_path / "aiteam.db")
    assert cli.main(["enable", "--role", "center", "--name", "mini", "--db", str(db), "--apply"]) == 0
    first = NodeIdentity.model_validate_json((tmp_path / "node.json").read_text())
    assert cli.main(["enable", "--role", "center", "--name", "mini", "--db", str(db), "--apply"]) == 0
    assert NodeIdentity.model_validate_json((tmp_path / "node.json").read_text()).node_id == first.node_id
    assert cli.main(["enable", "--role", "edge", "--name", "mini", "--db", str(db), "--apply"]) == 2  # role change
    node_module.write_node_identity(tmp_path / "node.json", first.model_copy(update={"hardware_uuid": "OTHER"}))
    capsys.readouterr()
    assert cli.main(["enable", "--role", "center", "--name", "mini", "--db", str(db), "--apply"]) == 0
    assert "不属于本机" in capsys.readouterr().out
    replaced = NodeIdentity.model_validate_json((tmp_path / "node.json").read_text())
    assert replaced.node_id != first.node_id and replaced.hardware_uuid == MACHINE


def test_repin_forgets_the_center_and_can_ask_for_a_rebuild(tmp_path: Path) -> None:
    from aiteam.sync import ledger
    from aiteam.sync.worker import open_engine

    db = _database(tmp_path / "aiteam.db")
    engine = open_engine(str(db))
    with engine.begin() as conn:
        ledger.ensure_peer(conn)
        conn.exec_driver_sql("UPDATE sync_peers SET center_node_id = 'mini-c0de', cursor = 40, cursor_hlc = 'h', "
                             "link = 'attached', handshake_error = 'center changed'")
    assert cli.main(["repin", "--db", str(db)]) == 0  # a dry run changes nothing
    with engine.connect() as conn:
        assert ledger.read_peer(conn).center_node_id == "mini-c0de"
    assert cli.main(["repin", "--db", str(db), "--apply"]) == 0
    with engine.connect() as conn:
        peer = ledger.read_peer(conn)
    assert (peer.center_node_id, peer.cursor, peer.handshake_error, peer.link.value) == ("", 40, "", "detached")
    assert cli.main(["repin", "--db", str(db), "--rebuild", "--apply"]) == 0
    with engine.connect() as conn:
        assert ledger.read_peer(conn).cursor == -1  # the worker rebuilds the replica on the next attach
    engine.dispose()
