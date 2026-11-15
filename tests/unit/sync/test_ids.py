"""Row ids two nodes can reach independently: operation contexts and natural keys."""

from __future__ import annotations

import sqlite3
import unicodedata
import uuid
from pathlib import Path

import pytest

from aiteam import ids
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import Report, Task, WorkflowAgent, WorkflowRun


@pytest.fixture()
def natural_ids():
    ids.set_natural_ids(True)
    try:
        yield
    finally:
        ids.set_natural_ids(False)


def test_ids_are_random_outside_an_operation() -> None:
    first, second = ids.new_id(), ids.new_id()
    assert first != second and uuid.UUID(first).version == 4


def test_an_operation_reproduces_its_ids_in_order() -> None:
    op_id = str(uuid.uuid4())
    with ids.operation_ids(op_id):
        staged = [Task(title="a").id, Report(author="x", topic="t", report_type="r", date="d", content="c").id,
                  ids.new_id()]
    with ids.operation_ids(op_id):
        replayed = [Task(title="a").id, Report(author="x", topic="t", report_type="r", date="d", content="c").id,
                    ids.new_id()]
    assert staged == replayed and len(set(staged)) == 3
    assert staged[0] == str(uuid.uuid5(uuid.UUID(op_id), "1"))
    with ids.operation_ids(str(uuid.uuid4())):
        assert Task(title="a").id not in staged
    assert uuid.UUID(Task(title="a").id).version == 4  # the context is gone again


def test_nested_operations_restore_the_outer_sequence() -> None:
    outer, inner = str(uuid.uuid4()), str(uuid.uuid4())
    with ids.operation_ids(outer):
        a = ids.new_id()
        with ids.operation_ids(inner):
            ids.new_id()
        b = ids.new_id()
    assert (a, b) == (str(uuid.uuid5(uuid.UUID(outer), "1")), str(uuid.uuid5(uuid.UUID(outer), "2")))


def test_natural_ids_are_off_for_a_standalone_node() -> None:
    assert not ids.natural_ids_enabled()
    assert ids.derived_id(ids.PROJECT_ROOT, "/Users/x/proj") is None


def test_natural_ids_derive_from_the_literal_key(natural_ids) -> None:
    root = "/Volumes/外置盘/café"
    nfc, nfd = unicodedata.normalize("NFC", root), unicodedata.normalize("NFD", root)
    assert nfc != nfd
    assert ids.derived_id(ids.PROJECT_ROOT, nfc) == ids.derived_id(ids.PROJECT_ROOT, nfd + "/")
    assert ids.derived_id(ids.PROJECT_ROOT, "/a/b") != ids.derived_id(ids.PROJECT_ROOT, "/a/b/c")
    assert ids.derived_id(ids.TEAM_NAME, "session-1234") != ids.derived_id(ids.WORKFLOW_RUN, "session-1234")
    assert ids.derived_id(ids.WORKFLOW_AGENT, "wf", "") is None  # an empty key part keeps a random id
    assert ids.normalize_root_path("/") == "/"
    with pytest.raises(ValueError):
        ids.derived_id("projects.name", "x")


async def _new_machine(path: Path) -> StorageRepository:
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repo.init_db()
    return repo


async def _create_everything(repo: StorageRepository) -> dict[str, str]:
    project = await repo.create_project(name="p", root_path="/Users/x/proj/")
    team = await repo.create_team(name="session-abcd1234", mode="coordinate")
    run = (await repo.upsert_workflow_run(WorkflowRun(wf_id="wf-1", status="running"))).run
    agent = await repo.upsert_workflow_agent(WorkflowAgent(wf_id="wf-1", run_id=run.id, cc_agent_id="cc-1"))
    return {"projects": project.id, "teams": team.id, "workflow_runs": run.id, "workflow_agents": agent.id}


def _stored_ids(path: Path) -> dict[str, str]:
    con = sqlite3.connect(path)
    try:
        return {table: con.execute(f"SELECT id FROM {table}").fetchone()[0]
                for table in ("projects", "teams", "workflow_runs", "workflow_agents")}
    finally:
        con.close()


async def test_two_machines_creating_the_same_objects_agree_on_ids(tmp_path: Path, natural_ids) -> None:
    created = [await _create_everything(await _new_machine(tmp_path / f"{name}.db")) for name in ("air", "mini")]
    await close_db()
    stored = [_stored_ids(tmp_path / f"{name}.db") for name in ("air", "mini")]
    assert created[0] == created[1] == stored[0] == stored[1]


async def test_a_standalone_node_keeps_random_ids(tmp_path: Path) -> None:
    created = [await _create_everything(await _new_machine(tmp_path / f"{name}.db")) for name in ("one", "two")]
    await close_db()
    stored = [_stored_ids(tmp_path / f"{name}.db") for name in ("one", "two")]
    assert created[0] == stored[0] and created[1] == stored[1]
    assert all(created[0][table] != created[1][table] for table in created[0])


def test_the_id_module_is_a_leaf_below_the_shared_types() -> None:
    """aiteam.types builds _new_id on aiteam.ids, so ids may import nothing of the package."""
    import ast
    import sys

    tree = ast.parse(Path(ids.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module and node.level == 0}
    assert imported - {"__future__"} <= set(sys.stdlib_module_names)
    types_source = (Path(ids.__file__).parent / "types.py").read_text(encoding="utf-8")
    assert "aiteam.sync" not in types_source  # the shared types depend on no feature package
