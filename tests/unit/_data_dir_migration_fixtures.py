"""Shared helpers for the data directory migration tests.

The layout matches production inside a fake home: the legacy directory is
``<home>/.claude/data/ai-team-os``, the canonical one ``<home>/.aiteam``.
``spec_resolver`` follows the resolver contract of docs/config-dir-compat-design.md
§5.1 (env / canonical / legacy / conflict / missing, SQLite header check, a
legacy path that is our link counts as migrated); the contract test in
test_data_dir_migration.py runs the same scenario against ``aiteam.paths`` once
it exists. The API doubles return the production result types.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from aiteam.data_dir_migration import ApiStop, HolderProbe, Postcheck, db_facts, is_sqlite_file

DB = "aiteam.db"


def legacy_of(home: Path) -> Path:
    return home / ".claude" / "data" / "ai-team-os"


def canonical_of(home: Path) -> Path:
    return home / ".aiteam"


def make_db(path: Path, *, tasks: int = 3, user_version: int = 1) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
        con.executemany("INSERT INTO tasks VALUES (?, ?)", [(f"t{i}", f"task {i}") for i in range(tasks)])
        con.execute(f"PRAGMA user_version = {user_version}")
        con.commit()


def make_legacy(home: Path, *, tasks: int = 3, user_version: int = 1, port: int | None = None) -> Path:
    """A legacy data directory shaped like a real one: database, runtime files, a subdirectory."""
    legacy = legacy_of(home)
    legacy.mkdir(parents=True)
    make_db(legacy / DB, tasks=tasks, user_version=user_version)
    (legacy / "notice-local.cc.jsonl").write_text('{"id": "n1"}\n', encoding="utf-8")
    (legacy / "debug.log").write_text("line\n", encoding="utf-8")
    os.chmod(legacy / "debug.log", 0o600)
    (legacy / "wake-state").mkdir()
    (legacy / "wake-state" / "w.json").write_text("{}", encoding="utf-8")
    (legacy / "diagnostics").mkdir()
    os.symlink("debug.log", legacy / "latest.log")
    if port is not None:
        (legacy / "api_port.txt").write_text(str(port), encoding="utf-8")
    return legacy


def task_count(db: Path) -> int:
    with closing(sqlite3.connect(db)) as con:
        return con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def snapshot(root: Path, *, volatile: frozenset[str] = frozenset()) -> dict[str, tuple]:
    """Every entry under *root* with its type, mode and content hash (links by target, not followed).

    Names in *volatile* are recorded as present without their content (the WAL
    index that every reader of a live database updates).
    """
    entries: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ("link", os.readlink(full))
            elif stat.S_ISDIR(info.st_mode):
                entries[rel] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                digest = None if name in volatile else hashlib.sha256(Path(full).read_bytes()).hexdigest()
                entries[rel] = ("file", stat.S_IMODE(info.st_mode), digest)
    return entries


def databases_under(root: Path) -> list[Path]:
    """Real files named aiteam.db under *root*, not following links."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        found += [Path(dirpath) / name for name in filenames if name == DB]
    return sorted(found)


@dataclass
class Resolution:
    path: Path
    mode: str
    evidence: dict = field(default_factory=dict)


def spec_resolver(home: Path, *, env: str | None = None):
    """Resolver following the §5.1 contract for *home*; never creates anything."""

    def resolve() -> Resolution:
        if env:
            return Resolution(Path(env).expanduser(), "env")
        legacy, canonical = legacy_of(home), canonical_of(home)
        legacy_db, canonical_db = legacy / DB, canonical / DB
        links_here = legacy.is_symlink() and canonical.exists() and os.path.samefile(legacy, canonical)
        legacy_is_ours = links_here or (legacy / "MOVED-TO").exists()
        legacy_has_db = is_sqlite_file(legacy_db) and not legacy_is_ours
        if os.path.lexists(canonical):
            if legacy_has_db and is_sqlite_file(canonical_db):
                return Resolution(canonical, "conflict", {"legacy": str(legacy_db), "canonical": str(canonical_db)})
            return Resolution(canonical, "canonical")
        if legacy_has_db:
            return Resolution(legacy, "legacy")
        return Resolution(canonical, "missing", {"checked": [str(canonical), str(legacy)]})

    return resolve


class FakeApi:
    """stop_api / start_api doubles with the production shapes; records every call."""

    def __init__(self, *, running: bool = True, port: int = 48123) -> None:
        self.running = running
        self.port = port
        self.calls: list[tuple] = []

    def stop(self, data_dir: Path, dry_run: bool = False) -> ApiStop:
        self.calls.append(("stop", Path(data_dir), dry_run))
        if not self.running:
            return ApiStop(True, self.port, detail={"port": self.port})
        pids = [424242]
        detail = {"port": self.port, "pids": pids}
        if dry_run:
            return ApiStop(True, self.port, was_running=True, pids=pids, detail=detail)
        self.running = False
        return ApiStop(True, self.port, was_running=True, pids=pids, stopped=pids, detail=detail)

    def start(self, port: int) -> dict:
        self.calls.append(("start", port))
        self.running = True
        return {"success": True, "new_pid": 434343}

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def no_holders(paths: list[Path]) -> HolderProbe:
    return HolderProbe(True, "fake")


class FilePostcheck:
    """Gate 7 double as strict as production: resolver mode and path, task count, user_version.

    Production asks the API over HTTP; this reads the same facts from the files and
    the resolver, so a wrong directory or a lost row fails here as it would there.
    """

    def __init__(self, resolve) -> None:
        self.resolve = resolve
        self.calls: list[tuple] = []

    def __call__(self, port: int, expected_dir: Path, expected_mode: str, baseline: dict) -> Postcheck:
        self.calls.append((port, Path(expected_dir), expected_mode, dict(baseline)))
        resolution = self.resolve()
        facts = db_facts(Path(expected_dir) / DB)
        detail = {"mode": resolution.mode, "data_dir": str(resolution.path),
                  "task_count": facts.get("task_count"), "user_version": facts.get("user_version")}
        same_dir = os.path.lexists(resolution.path) and os.path.samefile(resolution.path, expected_dir)
        ok = (resolution.mode == expected_mode and same_dir
              and facts.get("task_count") == baseline.get("task_count")
              and facts.get("user_version") == baseline.get("user_version"))
        return Postcheck(ok, detail)
