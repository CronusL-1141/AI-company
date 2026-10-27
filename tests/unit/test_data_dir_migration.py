"""Data directory migration: the seven gates, both move paths, rollback, lock, CLI (design §4.1, §7 U4).

Every scenario runs in a fake home under tmp_path with all collaborators injected
(resolver, API stop/start, holder probe, gate 7 check); real processes, lsof and
the old-path link are exercised in tests/integration/test_data_dir_migration_processes.py.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from aiteam import data_dir_migration as mig
from aiteam.data_dir_migration import ApiStop, HolderProbe, Postcheck

from ._data_dir_migration_fixtures import (
    DB,
    FakeApi,
    FilePostcheck,
    canonical_of,
    make_db,
    make_legacy,
    no_holders,
    snapshot,
    spec_resolver,
    task_count,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def run(home: Path, *, apply: bool = True, api: FakeApi | None = None, **overrides):
    """migrate() with the standard doubles for *home*; *overrides* replace any of them."""
    resolve = overrides.pop("resolve", spec_resolver(home))
    api = api or FakeApi(running=False)
    kwargs = dict(apply=apply, legacy_dir=home / ".claude" / "data" / "ai-team-os", target_dir=canonical_of(home),
                  resolve=resolve, stop_api=api.stop, start_api=api.start, db_holders=no_holders,
                  postcheck=FilePostcheck(resolve))
    kwargs.update(overrides)
    return mig.migrate(**kwargs)


def run_rollback(home: Path, *, api: FakeApi | None = None, **overrides):
    resolve = overrides.pop("resolve", spec_resolver(home))
    api = api or FakeApi(running=False)
    kwargs = dict(legacy_dir=home / ".claude" / "data" / "ai-team-os", target_dir=canonical_of(home),
                  resolve=resolve, stop_api=api.stop, start_api=api.start, db_holders=no_holders,
                  postcheck=FilePostcheck(resolve))
    kwargs.update(overrides)
    return mig.rollback(**kwargs)


def statuses(result) -> list[tuple[int, str, str | None]]:
    return [(gate.gate, gate.status, gate.reason) for gate in result.gates]


def assert_untouched(home: Path, legacy: Path, before: dict) -> None:
    """The refusal contract: old directory as it was, nothing new beside it, no lock left."""
    assert not legacy.is_symlink()
    assert snapshot(legacy) == before
    assert not os.path.lexists(canonical_of(home))
    assert sorted(p.name for p in legacy.parent.iterdir()) == ["ai-team-os"]
    assert sorted(p.name for p in home.iterdir()) == [".claude"]


def cross_device(tree: Path):
    """device_of double: *tree* and everything under it on device 1, the rest on device 2."""
    root = os.path.realpath(tree)

    def device_of(path: Path) -> int:
        resolved = os.path.realpath(path)
        return 1 if resolved == root or resolved.startswith(root + os.sep) else 2

    return device_of


# ============================================================
# Import surface
# ============================================================


def test_import_needs_neither_the_resolver_nor_the_mcp_package():
    code = ("import json, sys, aiteam.data_dir_migration; "
            "print(json.dumps(sorted(n for n in sys.modules if n.startswith(('aiteam.paths', 'aiteam.mcp')))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=ROOT)
    assert json.loads(out.stdout) == []


# ============================================================
# Dry run and gate 1
# ============================================================


def test_dry_run_judges_gates_1_to_5_and_writes_nothing(home):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    api = FakeApi(running=True)
    result = run(home, apply=False, api=api)
    assert result.action == "dry_run" and result.ok and result.reason is None
    assert statuses(result) == [(1, "pass", None), (2, "pass", None), (3, "pass", None), (4, "pass", None),
                                (5, "pass", None), (6, "skip", None), (7, "skip", None)]
    assert result.gates[0].detail["legacy"]["task_count"] == 3
    assert result.gates[2].detail["would_stop"] == [424242]
    assert api.calls == [("stop", legacy, True)]
    assert_untouched(home, legacy, before)


def test_dry_run_does_not_count_the_api_it_would_stop_as_a_holder(home):
    legacy = make_legacy(home)

    def holders(paths):
        return HolderProbe(True, "fake", [{"pid": 424242, "command": "python -m uvicorn"}])

    before = snapshot(legacy)
    assert run(home, apply=False, api=FakeApi(running=True), db_holders=holders).ok
    # Applying stops the API first; if that same process still holds the database, it is a holder.
    applied = run(home, api=FakeApi(running=True), db_holders=holders)
    assert applied.reason == mig.DB_IN_USE
    assert_untouched(home, legacy, before)


def test_conflict_refuses_and_reports_both_sides(home):
    legacy = make_legacy(home, user_version=1)
    canonical_of(home).mkdir()
    make_db(canonical_of(home) / DB, tasks=1, user_version=7)
    before = snapshot(legacy)
    result = run(home)
    assert result.reason == mig.CONFLICT and not result.ok
    detail = result.gates[0].detail
    assert (detail["legacy"]["user_version"], detail["canonical"]["user_version"]) == (1, 7)
    assert {"size", "mtime"} <= detail["legacy"].keys() and {"size", "mtime"} <= detail["canonical"].keys()
    assert snapshot(legacy) == before
    assert sorted(p.name for p in canonical_of(home).iterdir()) == [DB]


@pytest.mark.parametrize("mode", ["canonical", "missing", "env"])
def test_anything_but_legacy_is_not_legacy(home, mode):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    path = {"canonical": canonical_of(home), "missing": canonical_of(home), "env": home / "elsewhere"}[mode]
    api = FakeApi(running=True)
    result = run(home, api=api, resolve=lambda: type("R", (), {"path": path, "mode": mode, "evidence": {}})())
    assert result.reason == mig.NOT_LEGACY
    assert api.calls == []
    assert_untouched(home, legacy, before)


def test_explicit_source_is_accepted_when_the_resolver_finds_nothing(home):
    source = home / "profile" / "data" / "ai-team-os"
    source.mkdir(parents=True)
    make_db(source / DB, tasks=2)
    resolve = spec_resolver(home)
    assert resolve().mode == "missing"
    refused = run(home, legacy_dir=source)
    assert refused.reason == mig.NOT_LEGACY
    result = run(home, legacy_dir=source, explicit_source=True)
    assert result.ok and result.legacy_marker == "symlink"
    assert source.is_symlink() and os.path.samefile(source, canonical_of(home))
    assert "--from" in result.command


def test_unreadable_database_is_not_migrated(home):
    legacy = make_legacy(home)
    with closing(sqlite3.connect(legacy / DB)) as con:
        con.execute("DROP TABLE tasks")
        con.commit()
    before = snapshot(legacy)
    result = run(home)
    assert result.reason == mig.BASELINE_UNREADABLE
    assert_untouched(home, legacy, before)


# ============================================================
# Gate 2: the lock
# ============================================================


def write_lock(data_dir: Path, *, pid: int, started_at: float, host: str | None = None) -> None:
    """A lock taken at *started_at* and not refreshed since (its mtime is the same moment)."""
    import socket

    lock = data_dir / mig.LOCK_NAME
    lock.write_text(json.dumps({"pid": pid, "host": host or socket.gethostname(), "started_at": started_at}),
                    encoding="utf-8")
    os.utime(lock, (started_at, started_at))


def test_live_lock_refuses_and_is_left_alone(home):
    legacy = make_legacy(home)
    write_lock(legacy, pid=os.getppid(), started_at=time.time())
    before = snapshot(legacy)
    api = FakeApi(running=True)
    result = run(home, api=api)
    assert result.reason == mig.LOCKED and result.gates[1].detail["holder"]["pid"] == os.getppid()
    assert api.calls == []
    assert snapshot(legacy) == before


def test_expired_lock_is_replaced_and_released(home):
    legacy = make_legacy(home)
    write_lock(legacy, pid=os.getppid(), started_at=time.time() - mig.LOCK_TTL_SECONDS - 1)
    result = run(home)
    assert result.ok, result.gates
    assert result.gates[1].detail["replaced_stale"]["pid"] == os.getppid()
    assert not (canonical_of(home) / mig.LOCK_NAME).exists()


def test_migration_lock_active_semantics(tmp_path):
    now = time.time()
    assert mig.migration_lock_active(tmp_path) is False
    write_lock(tmp_path, pid=os.getppid(), started_at=now)
    assert mig.migration_lock_active(tmp_path) is True
    assert mig.migration_lock_active(tmp_path, now=now + mig.LOCK_TTL_SECONDS) is False
    os.utime(tmp_path / mig.LOCK_NAME)  # refreshed by a long copy
    assert mig.migration_lock_active(tmp_path, now=now + mig.LOCK_TTL_SECONDS) is True
    write_lock(tmp_path, pid=os.getpid(), started_at=now)
    assert mig.migration_lock_active(tmp_path) is False  # the holder restarts the API itself
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_lock(tmp_path, pid=dead.pid, started_at=now)
    assert mig.migration_lock_active(tmp_path) is False
    write_lock(tmp_path, pid=dead.pid, started_at=now, host="another-host")
    assert mig.migration_lock_active(tmp_path) is True
    (tmp_path / mig.LOCK_NAME).write_text("{half", encoding="utf-8")
    assert mig.migration_lock_active(tmp_path) is True  # fresh mtime counts


def test_a_long_copy_keeps_its_lock_fresh(tmp_path):
    lock = tmp_path / mig.LOCK_NAME
    lock.write_text("{}", encoding="utf-8")
    os.utime(lock, (1_000, 1_000))
    with mig._lock_kept_fresh([tmp_path], every=0.001):
        deadline = time.monotonic() + 10
        while lock.stat().st_mtime == 1_000 and time.monotonic() < deadline:
            time.sleep(0.001)
    assert lock.stat().st_mtime > 1_000


def test_only_one_of_many_concurrent_takers_gets_the_lock(tmp_path):
    takers = 48
    barrier = threading.Barrier(takers)
    outcomes: list[bool] = []

    def take():
        barrier.wait()
        outcomes.append(mig._acquire_lock(tmp_path, time.time())[0])

    threads = [threading.Thread(target=take) for _ in range(takers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(True) == 1 and len(outcomes) == takers


# ============================================================
# Gates 3-5: refusals restore the API and leave the directory alone
# ============================================================


@pytest.mark.parametrize("reason", [mig.API_STILL_RUNNING, mig.PID_MISMATCH])
def test_api_that_cannot_be_stopped_blocks_the_move(home, reason):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    calls = []

    def stop(data_dir, dry_run=False):
        calls.append(dry_run)
        return ApiStop(False, 48123, reason, was_running=True, pids=[7], detail={"pid": 7})

    started = []
    result = run(home, stop_api=stop, start_api=lambda port: started.append(port) or {"success": True})
    assert result.reason == reason and statuses(result)[2] == (3, "fail", reason)
    assert started == []  # nothing was stopped, so nothing is restarted
    assert_untouched(home, legacy, before)


def test_db_in_use_names_the_holder_and_restarts_the_api(home):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    api = FakeApi(running=True)

    def holders(paths):
        assert [p.name for p in paths] == [DB, DB + "-wal", DB + "-shm"]
        return HolderProbe(True, "fake", [{"pid": 999, "command": "sqlite3 aiteam.db"}])

    result = run(home, api=api, db_holders=holders)
    assert result.reason == mig.DB_IN_USE
    assert result.gates[3].detail["holders"] == [{"pid": 999, "command": "sqlite3 aiteam.db"}]
    assert api.names() == ["stop", "start"] and result.api["restart"]["success"]
    assert_untouched(home, legacy, before)


def test_unusable_probe_counts_as_in_use(home):
    legacy = make_legacy(home)
    result = run(home, db_holders=lambda paths: HolderProbe(False, "unavailable", error="no probe"))
    assert result.reason == mig.DB_IN_USE and result.gates[3].detail["error"] == "no probe"
    assert not os.path.lexists(canonical_of(home)) and not legacy.is_symlink()


READER = r"""
import sqlite3, sys
db = sys.argv[1]
writer = sqlite3.connect(db, isolation_level=None)
writer.execute("INSERT INTO tasks VALUES ('w1', 'in the WAL')")
reader = sqlite3.connect(db, isolation_level=None)
reader.execute("BEGIN")
reader.execute("SELECT COUNT(*) FROM tasks").fetchone()
writer.execute("INSERT INTO tasks VALUES ('w2', 'after the reader')")
print("ready", flush=True)
sys.stdin.readline()
"""


def test_wal_that_cannot_be_truncated_blocks_the_move(home):
    """A holder the probe cannot see keeps a read transaction open: the checkpoint is busy."""
    legacy = make_legacy(home)
    child = subprocess.Popen([sys.executable, "-c", READER, str(legacy / DB)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        wal = legacy / (DB + "-wal")
        assert wal.stat().st_size > 0
        api = FakeApi(running=True)
        result = run(home, api=api, checkpoint_timeout=0.05)
        assert result.reason == mig.WAL_NOT_TRUNCATED
        assert result.gates[4].detail["busy"] == 1 and result.gates[4].detail["wal_bytes"] > 0
        assert api.names() == ["stop", "start"]
        assert not os.path.lexists(canonical_of(home)) and not legacy.is_symlink()
        assert not (legacy / mig.LOCK_NAME).exists()
    finally:
        child.stdin.write("\n")
        child.stdin.close()
        child.wait(timeout=30)
    assert task_count(legacy / DB) == 5


# ============================================================
# Gate 6: the two ways of moving
# ============================================================


def test_same_filesystem_renames_and_leaves_a_link(home):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    inode = (legacy / DB).stat().st_ino
    api = FakeApi(running=True)
    result = run(home, api=api)
    target = canonical_of(home)
    assert result.ok and result.method == "rename" and result.legacy_marker == "symlink"
    assert statuses(result) == [(n, "pass", None) for n in range(1, 8)]
    assert (target / DB).stat().st_ino == inode
    assert legacy.is_symlink() and os.readlink(legacy) == str(target)
    record = json.loads((target / mig.RECORD_NAME).read_text(encoding="utf-8"))
    assert (record["method"], record["legacy_marker"], record["task_count"]) == ("rename", "symlink", 3)
    after = snapshot(target)
    del after[mig.RECORD_NAME]
    assert after == before
    assert not (target / mig.LOCK_NAME).exists()
    assert api.names() == ["stop", "start"] and api.calls[1] == ("start", 48123)
    assert spec_resolver(home)().mode == "canonical"
    assert result.migrated_at


def test_a_process_that_opens_the_database_during_the_copy_stops_the_move(home):
    """Gate 4 passed, then the copy took a while and a process opened the old database: renaming
    the old directory aside would leave its writes behind, so the copy is dropped."""
    legacy = make_legacy(home)
    before = snapshot(legacy)
    probes = []

    def holders(paths):
        probes.append([Path(path).parent for path in paths])
        late = len(probes) > 1  # the probe after the copy
        return mig.HolderProbe(True, "fake", [{"pid": 4242, "command": "old MCP"}] if late else [])

    result = run(home, device_of=cross_device(legacy), db_holders=holders)
    assert not result.ok and result.reason == mig.DB_IN_USE and result.failed_gate.gate == 6
    assert result.failed_gate.detail["when"] == "after the copy"
    assert probes == [[legacy] * len(probes[0])] * 2
    assert_untouched(home, legacy, before)


def test_cross_filesystem_copies_verifies_and_keeps_the_original(home):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    copied_into = []

    def copy_tree(src, dst):
        copied_into.append(Path(dst).name)
        shutil.copytree(src, dst, symlinks=True)

    result = run(home, device_of=cross_device(legacy), copy_tree=copy_tree)
    target = canonical_of(home)
    assert result.ok and result.method == "copy" and result.legacy_marker == "symlink"
    assert copied_into == [".aiteam.partial"]
    assert not os.path.lexists(target.with_name(".aiteam.partial"))
    backup = Path(result.backup_dir)
    assert backup.name.startswith("ai-team-os.migrated-") and snapshot(backup) == before
    after = snapshot(target)
    del after[mig.RECORD_NAME]
    assert after == before
    assert result.gates[5].detail["sha256"] == after[DB][2]
    assert legacy.is_symlink() and os.path.samefile(legacy, target)


def _hooks_write_during(copy_into_partial: bool, old_dir: Path):
    """copy_tree double: copies, then does what hooks in other sessions do while a long copy runs.

    They append to the old directory (they still resolve it); with *copy_into_partial*
    the new side also gets writes of its own (made once it is in place).
    """

    def copy_tree(src, dst):
        shutil.copytree(src, dst, symlinks=True)
        with open(old_dir / "notice-local.cc.jsonl", "a", encoding="utf-8") as handle:
            handle.write('{"id": "written during the copy"}\n')
        with open(old_dir / "debug.log", "a", encoding="utf-8") as handle:
            handle.write("late line\n")
        (old_dir / "hook-delivery").mkdir(exist_ok=True)
        (old_dir / "hook-delivery" / "late.jsonl").write_text('{"id": "d1"}\n', encoding="utf-8")
        (old_dir / "wake-state" / "w.json").write_text('{"side": "old"}', encoding="utf-8")
        if copy_into_partial:
            with open(Path(dst) / "notice-local.cc.jsonl", "a", encoding="utf-8") as handle:
                handle.write('{"id": "written after the switch"}\n')
            (Path(dst) / "wake-state" / "w.json").write_text('{"side": "new"}', encoding="utf-8")

    return copy_tree


def test_cross_filesystem_carries_over_what_hooks_wrote_during_the_copy(home):
    """Hooks keep writing to the old directory while a long copy runs; the new directory gets it all."""
    legacy = make_legacy(home)
    result = run(home, device_of=cross_device(legacy), copy_tree=_hooks_write_during(True, legacy))
    target = canonical_of(home)
    assert result.ok and result.method == "copy"
    notices = (target / "notice-local.cc.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(notices) == sorted(['{"id": "n1"}', '{"id": "written after the switch"}',
                                      '{"id": "written during the copy"}'])
    assert (target / "debug.log").read_text(encoding="utf-8") == "line\nlate line\n"
    assert (target / "hook-delivery" / "late.jsonl").read_text(encoding="utf-8") == '{"id": "d1"}\n'
    assert (target / "wake-state" / "w.json").read_text(encoding="utf-8") == '{"side": "new"}'
    carried = result.gates[5].detail["carried"]
    assert set(carried) == {"copied", "appended", "kept_live"}
    assert carried["copied"] == ["hook-delivery/late.jsonl"]
    assert sorted(carried["appended"]) == ["debug.log", "notice-local.cc.jsonl"]
    assert carried["kept_live"] == ["wake-state/w.json"]
    backup = Path(result.backup_dir)
    assert '"written during the copy"' in (backup / "notice-local.cc.jsonl").read_text(encoding="utf-8")


def test_cross_filesystem_rollback_carries_over_too(home):
    legacy = make_legacy(home)
    assert run(home, device_of=cross_device(legacy)).ok
    target = canonical_of(home)
    back = run_rollback(home, device_of=cross_device(target), copy_tree=_hooks_write_during(False, target))
    assert back.ok and back.method == "copy", back.gates
    assert '{"id": "written during the copy"}' in (legacy / "notice-local.cc.jsonl").read_text(encoding="utf-8")
    assert (legacy / "hook-delivery" / "late.jsonl").is_file()


def test_checksum_mismatch_discards_the_copy(home):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    api = FakeApi(running=True)

    def corrupting_copy(src, dst):
        shutil.copytree(src, dst, symlinks=True)
        with open(Path(dst) / DB, "r+b") as handle:
            handle.seek(200)
            byte = handle.read(1)
            handle.seek(200)
            handle.write(bytes([byte[0] ^ 0xFF]))

    result = run(home, api=api, device_of=cross_device(legacy), copy_tree=corrupting_copy)
    assert result.reason == mig.CHECKSUM_MISMATCH
    detail = result.gates[5].detail
    assert detail["source_sha256"] != detail["copy_sha256"]
    assert api.names() == ["stop", "start"]
    assert_untouched(home, legacy, before)


def test_stub_directory_when_no_link_can_be_made(home):
    legacy = make_legacy(home)
    before = snapshot(legacy)

    def no_links(target, link):
        raise OSError("links are not available here")

    result = run(home, make_symlink=no_links)
    target = canonical_of(home)
    assert result.ok and result.legacy_marker == "stub"
    assert sorted(p.name for p in legacy.iterdir()) == sorted([mig.MOVED_TO_NAME, mig.RECORD_NAME])
    assert (legacy / mig.MOVED_TO_NAME).read_text(encoding="utf-8").strip() == str(target)
    assert spec_resolver(home)().mode == "canonical"
    back = run_rollback(home, make_symlink=no_links)
    assert back.ok and back.failed_gate is None, back.gates
    assert snapshot(legacy) == before


# ============================================================
# Gate 7 and rollback
# ============================================================


def mismatch(port, expected_dir, expected_mode, baseline):
    return Postcheck(False, {"error": "task_count_mismatch", "task_count": baseline["task_count"] + 1})


@pytest.mark.parametrize("cross", [False, True], ids=["rename", "copy"])
def test_failed_reconciliation_rolls_back_to_the_original_tree(home, cross):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    api = FakeApi(running=True)
    extra = {"device_of": cross_device(legacy)} if cross else {}
    result = run(home, api=api, postcheck=mismatch, **extra)
    assert not result.ok and result.reason == mig.POSTCHECK_FAILED and result.rolled_back
    assert statuses(result)[-1] == (7, "fail", mig.POSTCHECK_FAILED)
    assert not legacy.is_symlink() and snapshot(legacy) == before
    assert not os.path.lexists(canonical_of(home))
    # stop (gate 3), start (gate 7), stop on the new dir, restart on the old one
    assert api.names() == ["stop", "start", "stop", "start"]
    assert api.calls[2] == ("stop", canonical_of(home), False)
    assert sorted(p.name for p in legacy.parent.iterdir()) == ["ai-team-os"]
    leftovers = sorted(p.name for p in home.iterdir())
    if cross:
        # The copy the API ran on during gate 7 is kept aside, not deleted.
        assert len(leftovers) == 2 and leftovers[1] == ".claude"
        assert leftovers[0].startswith(".aiteam.rolled-back-") and Path(result.backup_dir).name == leftovers[0]
    else:
        assert leftovers == [".claude"]
    assert spec_resolver(home)().mode == "legacy"


@pytest.mark.parametrize("cross", [False, True], ids=["rename", "copy"])
def test_rollback_restores_the_tree_byte_for_byte(home, cross):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    assert run(home, **({"device_of": cross_device(legacy)} if cross else {})).ok
    api = FakeApi(running=True)
    postcheck = FilePostcheck(spec_resolver(home))
    back = run_rollback(home, api=api, postcheck=postcheck,
                        **({"device_of": cross_device(canonical_of(home))} if cross else {}))
    assert back.ok and back.failed_gate is None and back.rolled_back, back.gates
    assert back.method == ("copy" if cross else "rename")
    assert not legacy.is_symlink() and snapshot(legacy) == before
    assert not os.path.lexists(canonical_of(home))
    assert api.names() == ["stop", "start"]
    assert postcheck.calls[0][1:3] == (legacy, "legacy")
    assert spec_resolver(home)().mode == "legacy"
    if cross:
        names = sorted(p.name for p in home.iterdir())
        assert names[0].startswith(".aiteam.rolled-back-")


def test_rollback_refuses_a_directory_this_module_did_not_migrate(home):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    result = run_rollback(home)
    assert result.reason == mig.NOT_MIGRATED
    assert snapshot(legacy) == before


def test_rollback_refuses_a_stub_that_old_code_wrote_into(home):
    legacy = make_legacy(home)

    def no_links(target, link):
        raise OSError("links are not available here")

    assert run(home, make_symlink=no_links).ok
    (legacy / "notice-local.cc.jsonl").write_text("written by old code\n", encoding="utf-8")
    result = run_rollback(home)
    assert result.reason == mig.NOT_MIGRATED
    assert result.gates[0].detail["entries"] == ["notice-local.cc.jsonl"]
    assert (canonical_of(home) / DB).exists()


# ============================================================
# Gate 3 command check and gate 7 HTTP check
# ============================================================


def test_command_matches_the_restart_command_shape_only():
    from aiteam.mcp.tools.infra import _restart_command

    reference = _restart_command(8123)
    ok = mig.command_matches_api
    assert ok(list(reference), 8123)
    assert ok(["/Applications/My Python/python3", *reference[1:]], 8123)  # argv as psutil reads it
    assert ok(["python", "-m", "uvicorn", "aiteam.api.app:create_app", "--factory", "--fd", "4"], 8123)
    assert ok(["python", "-m", "uvicorn", "aiteam.api.app:create_app", "--factory", "--port=8123"], 8123)
    assert not ok(list(reference), 8000)
    assert not ok(["python", "-m", "uvicorn", "other.app:create_app", "--factory"], 8123)
    assert not ok(["python", "-c", "print(1)"], 8123)


def test_our_api_is_what_autostart_calls_our_api():
    """One definition (_autostart._is_api_command) plus the port: the console-script uvicorn
    autostart reuses as ours must not read as pid_mismatch to the migration."""
    from aiteam.mcp._autostart import _is_api_command

    ok = mig.command_matches_api
    shapes = [
        ["/usr/local/bin/uvicorn", "aiteam.api.app:create_app", "--host", "127.0.0.1", "--port", "8123", "--factory"],
        ["/usr/bin/python3", "/usr/local/bin/uvicorn", "aiteam.api.app:create_app", "--factory", "--port", "8123"],
        ["/usr/bin/python3", "/usr/local/bin/aiteam", "up", "--api-port", "8123"],
        ["/usr/local/bin/aiteam", "up", "--api-port=8123", "--reload"],
        ["python", "-m", "aiteam.cli.app", "up", "--api-port", "8123", "--no-dashboard"],
    ]
    for shape in shapes:
        assert _is_api_command(shape) and ok(shape, 8123), shape
        assert not ok(shape, 8124), shape
    assert ok(["python", "-m", "aiteam.cli.app", "up"], mig.UP_DEFAULT_PORT)
    assert not ok(["python", "-m", "aiteam.cli.app", "up"], 8123)
    assert not ok(["/usr/local/bin/aiteam", "status"], 8123)
    assert not ok(["/usr/local/bin/uvicorn", "other.app:create_app", "--port", "8123"], 8123)


class _Api(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        body = self.routes.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(json.dumps(body or {}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture()
def http_api():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("health,total,error", [
    ({"data_dir_mode": "canonical"}, 3, None),
    ({"data_dir_mode": "legacy"}, 3, "data_dir_mismatch"),
    ({}, 3, "health_missing_data_dir"),
    ({"data_dir_mode": "canonical"}, 2, "task_count_mismatch"),
    ({"data_dir_mode": "canonical"}, None, "task_count_mismatch"),
], ids=["match", "wrong-mode", "no-fields", "lost-task", "no-count"])
def test_postcheck_over_http(tmp_path, http_api, health, total, error):
    """The health document's shape is the production one (see the contract test below)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    make_db(data_dir / DB, tasks=3, user_version=4)
    body = {"status": "ok", **({"data_dir": str(data_dir)} if health else {}), **health,
            **({"task_count": total} if total is not None else {})}
    _Api.routes = {mig.HEALTH_COUNT_PATH: body}
    check = mig.postcheck_api(http_api.server_address[1], data_dir, "canonical",
                              {"task_count": 3, "user_version": 4}, polls=1, sleep=lambda s: None)
    assert check.ok is (error is None) and check.detail.get("error") == error


def test_postcheck_over_http_checks_user_version(tmp_path, http_api):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    make_db(data_dir / DB, tasks=3, user_version=5)
    _Api.routes = {mig.HEALTH_COUNT_PATH: {"data_dir": str(data_dir), "data_dir_mode": "canonical", "task_count": 3}}
    check = mig.postcheck_api(http_api.server_address[1], data_dir, "canonical",
                              {"task_count": 3, "user_version": 4}, polls=1, sleep=lambda s: None)
    assert check.detail["error"] == "user_version_mismatch"


async def test_the_real_health_route_reports_what_gate_7_reads(tmp_path, monkeypatch):
    """Contract: the keys gate 7 reads are the ones /api/health returns, task_count only on request."""
    import httpx
    from fastapi import FastAPI

    from aiteam.api import deps
    from aiteam.api.routes.health import router
    from aiteam.storage.repository import StorageRepository

    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}")
    await repo.init_db()
    monkeypatch.setattr(deps, "_repository", repo)
    project = await repo.create_project(name="p", root_path=str(tmp_path))
    for title in ("a", "b"):
        await repo.create_task(None, title, project_id=project.id)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        plain = (await client.get("/api/health")).json()
        counted = (await client.get(mig.HEALTH_COUNT_PATH)).json()
    assert {mig.HEALTH_DATA_DIR_KEY, mig.HEALTH_MODE_KEY} <= set(plain)
    assert mig.HEALTH_TASK_COUNT_KEY not in plain, "the liveness probe never touches the database"
    assert counted[mig.HEALTH_TASK_COUNT_KEY] == 2


# ============================================================
# CLI
# ============================================================


def cli_overrides(home: Path, **extra):
    resolve = spec_resolver(home)
    api = FakeApi(running=False)
    overrides = dict(legacy_dir=home / ".claude" / "data" / "ai-team-os", target_dir=canonical_of(home),
                     resolve=resolve, stop_api=api.stop, start_api=api.start, db_holders=no_holders,
                     postcheck=FilePostcheck(resolve))
    return {**overrides, **extra}


def test_cli_prints_one_line_per_gate_and_sets_the_exit_code(home, capsys):
    legacy = make_legacy(home)
    assert mig.main([], **cli_overrides(home)) == 0
    out = capsys.readouterr().out
    assert "[1] 先看两处: 通过" in out and "[5] WAL 归零: 通过" in out and "[6] 搬: 跳过" in out
    assert mig.MANUAL_COMMAND in out and not legacy.is_symlink()

    busy = HolderProbe(True, "fake", [{"pid": 31337, "command": "sqlite3 aiteam.db"}])
    assert mig.main(["--apply"], **cli_overrides(home, db_holders=lambda paths: busy)) == 1
    out = capsys.readouterr().out
    assert "[4] 无人开库: 不过 (db_in_use)" in out and "31337" in out and "数据留在旧位置" in out

    assert mig.main(["--apply", "--json"], **cli_overrides(home)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and data["legacy_marker"] == "symlink" and len(data["gates"]) == 7

    assert mig.main([], **cli_overrides(home)) == 1
    assert "解析器报 canonical" in capsys.readouterr().out

    assert mig.main(["--rollback"], **cli_overrides(home)) == 0
    assert "已搬回旧位置" in capsys.readouterr().out and not legacy.is_symlink()


@pytest.mark.parametrize("entry", [["scripts/migrate_data_dir.py"], ["-m", "aiteam.data_dir_migration"]],
                         ids=["script", "module"])
def test_entries_start_without_the_resolver(entry):
    out = subprocess.run([sys.executable, *entry, "--help"], capture_output=True, text=True, timeout=60,
                         check=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert "--apply" in out.stdout and "--rollback" in out.stdout and "--from" in out.stdout


# ============================================================
# Contract with the real resolver (active once aiteam.paths exists)
# ============================================================


@pytest.mark.skipif(os.name == "nt", reason="the fake home is set through HOME")
def test_real_resolver_reads_the_left_link_as_migrated(tmp_path, monkeypatch):
    paths = pytest.importorskip("aiteam.paths")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AITEAM_DATA_DIR", raising=False)
    make_legacy(home)
    before = paths.resolve_data_dir()
    assert before.mode == "legacy"
    result = run(home, resolve=paths.resolve_data_dir, postcheck=FilePostcheck(paths.resolve_data_dir))
    assert result.ok and result.legacy_marker == "symlink", result.gates
    after = paths.resolve_data_dir()
    assert after.mode == "canonical" and os.path.samefile(after.path, canonical_of(home))


# ============================================================
# --user-quote: the conversation entry records the user's own words
# ============================================================


def test_user_quote_is_refused_on_a_dry_run(home, capsys):
    make_legacy(home)
    with pytest.raises(SystemExit) as raised:
        mig.main(["--user-quote", "迁吧"], **cli_overrides(home))
    assert raised.value.code == 2
    assert "--user-quote" in capsys.readouterr().err


def test_user_quote_is_recorded_as_consent_in_the_new_data_directory(home, capsys, monkeypatch):
    """No API listening: the record goes to the local file the next hook fetch imports."""
    import json as _json

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    make_legacy(home)
    assert mig.main(["--apply", "--json", "--user-quote", "好，迁移 OS 数据"], **cli_overrides(home)) == 0
    document = _json.loads(capsys.readouterr().out)
    assert document["ok"] and document["consent_recorded"] == "local"
    lines = (canonical_of(home) / "notice-local.cc.jsonl").read_text(encoding="utf-8").splitlines()
    record = _json.loads(lines[-1])
    assert record["kind"] == "consent" and record["change"] == "migrate_data_dir"
    assert record["user_quote"] == "好，迁移 OS 数据" and record["status"] == "ok"
    assert record["notice_key"] == "data_dir_legacy" and "host" not in record


def test_user_quote_of_a_refused_run_records_the_reason(home, capsys):
    import json as _json

    make_legacy(home)
    overrides = cli_overrides(home, db_holders=lambda paths: mig.HolderProbe(
        ok=True, method="lsof", holders=[{"pid": 4242, "command": "sqlite3 aiteam.db"}]))
    assert mig.main(["--apply", "--json", "--user-quote", "迁"], **overrides) == 1
    assert _json.loads(capsys.readouterr().out)["consent_recorded"] == "local"
    legacy = home / ".claude" / "data" / "ai-team-os"
    record = _json.loads((legacy / "notice-local.codex.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (record["status"], record["error"]) == ("failed", mig.DB_IN_USE)



# ============================================================
# Gate 3: the restarted API must run this code (an --offline update, a checkout run by hand)
# ============================================================


def test_an_api_that_would_come_back_on_other_code_stops_the_run_before_anything(home, tmp_path):
    legacy = make_legacy(home)
    before = snapshot(legacy)
    api = FakeApi(running=True)
    elsewhere = tmp_path / "installed" / "aiteam"
    result = run(home, api=api, api_package=lambda: elsewhere)
    assert not result.ok and result.reason == mig.CODE_MISMATCH and result.failed_gate.gate == 3
    assert result.failed_gate.detail["api_would_run"] == str(elsewhere)
    assert api.names() == [] and api.running, "the API is not even stopped"
    assert_untouched(home, legacy, before)


def test_the_default_probe_asks_the_interpreter_and_environment_the_api_starts_with(tmp_path, monkeypatch):
    """Another aiteam first on the import path (what an install without this checkout leaves) is seen."""
    other = tmp_path / "other"
    (other / "aiteam").mkdir(parents=True)
    (other / "aiteam" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(other))
    assert mig.default_api_package() == Path(os.path.realpath(other / "aiteam"))
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    assert mig.default_api_package() == mig.THIS_PACKAGE


# ============================================================
# Gate 6: older code recreates the old directory between the move and the link
# ============================================================


def _recreating_symlink(legacy: Path, files: dict[str, bytes]):
    """make_symlink double: the first call finds what an older hook just recreated (as the real
    os.symlink would: FileExistsError), later calls link for real."""
    calls = []

    def make(target, link):
        calls.append(link)
        if len(calls) == 1:
            legacy.mkdir()
            for name, data in files.items():
                (legacy / name).write_bytes(data)
        mig._default_symlink(target, link)

    return make


def test_notes_an_older_hook_wrote_into_a_recreated_old_directory_are_carried_and_the_link_placed(home):
    legacy = make_legacy(home)
    note = b'{"kind": "local_notice", "uuid": "late"}\n'
    result = run(home, make_symlink=_recreating_symlink(legacy, {"notice-local.codex.jsonl": note}))
    target = canonical_of(home)
    assert result.ok and result.legacy_marker == "symlink", result.gates
    assert legacy.is_symlink() and os.readlink(legacy) == str(target)
    assert (target / "notice-local.codex.jsonl").read_bytes() == note
    assert result.gates[5].detail["recreated_and_carried"] == {"copied": ["notice-local.codex.jsonl"]}
    assert "已迁移" in mig.format_result(result)


def test_a_database_in_a_recreated_old_directory_is_not_merged_and_the_text_says_where_the_data_is(home):
    """An older service started on an empty database there: nothing is guessed, and the conclusion
    does not claim the data stayed in the old place: it is in the new one."""
    legacy = make_legacy(home)
    result = run(home, make_symlink=_recreating_symlink(legacy, {"aiteam.db": b"SQLite format 3\x00" + b"\0" * 84}))
    target = canonical_of(home)
    assert not result.ok and result.failed_gate.gate == 6
    assert "error" in result.failed_gate.detail["undo"]
    assert task_count(target / DB) == 3, "the real database is in the new place"
    text = mig.format_result(result)
    assert f"数据在新位置 {target}" in text and "数据留在旧位置" not in text
