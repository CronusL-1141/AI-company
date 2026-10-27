"""Data directory resolver: the mode matrix and "resolve, never create" (U1, U2).

docs/config-dir-compat-design.md §5.1 and §7.2. The resolver lives in the path
section of ``hook_core.py``; ``aiteam.paths`` re-exports it. Every cell of the
matrix snapshots the test tree before and after and replaces every mkdir with a
failure: resolving must not create, move or touch anything (§2 principle 3).

Locations:
* canonical: the neutral data directory (absent / database / empty dir / stub)
* legacy: the old directory inside the Claude config folder (absent / zero-byte
  file / database / stub / link to canonical / link to elsewhere)
* ``AITEAM_DATA_DIR``: unset / empty string / set
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

CANONICAL_STATES = ("absent", "database", "empty", "stub")
LEGACY_STATES = ("absent", "zero_byte", "database", "stub", "link_canonical", "link_elsewhere")


def _make_db(path: Path, user_version: int = 7) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (x)")
        connection.execute(f"PRAGMA user_version = {user_version}")
    connection.close()


def _canonical(home: Path) -> Path:
    return home / ".aiteam"


def _legacy(home: Path) -> Path:
    return home / ".claude" / "data" / "ai-team-os"


def _build(home: Path, canonical: str, legacy: str) -> None:
    canon, old = _canonical(home), _legacy(home)
    if canonical == "database":
        _make_db(canon / "aiteam.db", user_version=9)
    elif canonical == "empty":
        canon.mkdir(parents=True)
    elif canonical == "stub":
        canon.mkdir(parents=True)
        (canon / "MOVED-TO").write_text(str(old), encoding="utf-8")
    old.parent.mkdir(parents=True, exist_ok=True)
    if legacy == "zero_byte":
        old.mkdir()
        (old / "aiteam.db").write_bytes(b"")
    elif legacy == "database":
        _make_db(old / "aiteam.db", user_version=8)
    elif legacy == "stub":
        old.mkdir()
        (old / "MOVED-TO").write_text(str(canon), encoding="utf-8")
    elif legacy == "link_canonical":
        old.symlink_to(canon, target_is_directory=True)
    elif legacy == "link_elsewhere":
        elsewhere = home / "elsewhere" / "ai-team-os"
        _make_db(elsewhere / "aiteam.db", user_version=6)
        old.symlink_to(elsewhere, target_is_directory=True)


def _expected(canonical: str, legacy: str) -> str:
    legacy_db = legacy in ("database", "link_elsewhere")
    if canonical == "database" and legacy_db:
        return "conflict"
    if canonical in ("database", "empty") and not legacy_db:
        return "canonical"
    if legacy_db:
        return "legacy"
    return "missing"


def _snapshot(root: Path) -> list[tuple]:
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            rows.append((str(path.relative_to(root)), path.is_symlink(), info.st_size, info.st_mtime_ns))
    return sorted(rows)


def _forbid_creation(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError(f"resolver tried to create a directory: {args!r}")

    monkeypatch.setattr(os, "mkdir", refuse)
    monkeypatch.setattr(os, "makedirs", refuse)
    monkeypatch.setattr(Path, "mkdir", refuse)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("AITEAM_DATA_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return root


MATRIX = [
    (env, canonical, legacy)
    for env in ("unset", "empty", "set")
    for canonical in CANONICAL_STATES
    for legacy in LEGACY_STATES
]


@pytest.mark.parametrize(("env", "canonical", "legacy"), MATRIX,
                         ids=[f"env-{e}/canon-{c}/legacy-{v}" for e, c, v in MATRIX])
def test_mode_matrix_never_creates(home, monkeypatch, env, canonical, legacy):
    from aiteam import paths

    _build(home, canonical, legacy)
    override = home / "override"
    if env == "empty":
        monkeypatch.setenv("AITEAM_DATA_DIR", "")
    elif env == "set":
        monkeypatch.setenv("AITEAM_DATA_DIR", str(override))
    before = _snapshot(home)
    _forbid_creation(monkeypatch)

    resolution = paths.resolve_data_dir()

    expected = "env" if env == "set" else _expected(canonical, legacy)
    assert resolution.mode == expected
    if expected == "env":
        assert resolution.path == override
    elif expected in ("canonical", "conflict"):
        assert resolution.path == _canonical(home)
    elif expected == "legacy":
        # Not resolved: a link stays the path the user sees.
        assert resolution.path == _legacy(home)
    if expected == "missing":
        with pytest.raises(paths.DataDirMissing):
            paths.os_data_dir()
    else:
        assert paths.os_data_dir() == resolution.path
    monkeypatch.undo()
    assert _snapshot(home) == before, "resolving changed the file tree"


def test_conflict_evidence_names_both_databases(home):
    from aiteam import paths

    _build(home, "database", "database")
    resolution = paths.resolve_data_dir()
    by_role = {candidate.role: candidate for candidate in resolution.evidence}
    assert by_role["canonical"].state == "database" and by_role["legacy"].state == "database"
    assert by_role["canonical"].user_version == 9 and by_role["legacy"].user_version == 8
    assert by_role["canonical"].db_size > 0 and by_role["legacy"].db_mtime > 0


def test_missing_evidence_includes_the_moved_config_folder(home, monkeypatch):
    """The incident shape: the config folder moved and took the data with it."""
    from aiteam import paths

    profile = home / "profiles" / "one" / "config"
    _make_db(profile / "data" / "ai-team-os" / "aiteam.db")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    resolution = paths.resolve_data_dir()
    assert resolution.mode == "missing"
    config = [c for c in resolution.evidence if c.role == "config"]
    assert config and config[0].path == str(profile / "data" / "ai-team-os")
    assert config[0].state == "database"
    with pytest.raises(paths.DataDirMissing) as raised:
        paths.os_data_dir()
    assert str(profile / "data" / "ai-team-os") in str(raised.value)


def test_env_without_database_records_the_others(home, monkeypatch):
    from aiteam import paths

    _build(home, "absent", "database")
    monkeypatch.setenv("AITEAM_DATA_DIR", str(home / "scratch"))
    resolution = paths.resolve_data_dir()
    assert resolution.mode == "env"
    states = {c.role: c.state for c in resolution.evidence}
    assert states["env"] == "absent" and states["legacy"] == "database"


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_config_homes_treat_blank_as_unset(home, monkeypatch, value):
    from aiteam import paths

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    monkeypatch.setenv("CODEX_HOME", value)
    assert paths.cc_config_dir() == home / ".claude"
    assert paths.cc_global_config() == home / ".claude.json"
    assert paths.cc_main_chain_dir() == home / ".claude" / "hooks" / "ai-team-os"
    assert paths.codex_home() == home / ".codex"


def test_config_homes_follow_the_environment_literally(home, monkeypatch):
    from aiteam import paths

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/profile")
    monkeypatch.setenv("CODEX_HOME", "~/codex-home")
    assert paths.cc_config_dir() == home / "profile"
    assert paths.cc_global_config() == home / "profile" / ".claude.json"
    assert paths.cc_main_chain_dir() == home / "profile" / "hooks" / "ai-team-os"
    assert paths.codex_home() == home / "codex-home"


def test_codex_home_self_locates_only_when_asked(home, tmp_path):
    from aiteam import paths

    script = tmp_path / "codex-install" / "hooks" / "ai-team-os-observer" / "hook_core.py"
    assert paths.codex_home(self_locate=True, script=script) == tmp_path / "codex-install"
    assert paths.codex_home(self_locate=False, script=script) == home / ".codex"


def test_package_reexports_are_the_hook_core_objects():
    """U2: one definition; the package must not grow a second resolver."""
    from aiteam import paths
    from aiteam.hooks import hook_core

    for name in paths.__all__:
        assert getattr(paths, name) is getattr(hook_core, name), name
    assert paths.os_data_dir is hook_core.os_data_dir
    assert {"resolve_data_dir", "os_data_dir", "cc_config_dir", "cc_global_config",
            "cc_main_chain_dir", "codex_home", "DataDirMissing"} <= set(paths.__all__)


# ── creation is the installers' alone, and only on an empty machine (§5.2) ──

CREATORS = {"install.py", "plugin/hooks/auto_install.py", "scripts/codex_adapter.py"}


def test_only_the_installers_call_the_creation_helper():
    """The list in aiteam.paths' docstring, pinned: a hook or service creating it is a regression."""
    root = Path(__file__).resolve().parents[2]
    callers = set()
    for folder in ("src", "plugin", "scripts"):
        for path in (root / folder).rglob("*.py"):
            if path.name == "hook_core.py" or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "create_data_dir_if_absent(" in text:
                callers.add(path.relative_to(root).as_posix())
    if "create_data_dir_if_absent(" in (root / "install.py").read_text(encoding="utf-8"):
        callers.add("install.py")
    assert callers - {"src/aiteam/paths.py"} == CREATORS


@pytest.mark.parametrize(("canonical", "legacy", "created", "mode"), [
    ("absent", "absent", True, "canonical"),
    ("empty", "absent", False, "canonical"),
    ("absent", "database", False, "legacy"),
    ("database", "database", False, "conflict"),
    ("absent", "stub", False, "missing"),
    ("absent", "link_canonical", False, "missing"),
], ids=["empty-machine", "existing", "legacy", "conflict", "stub-left", "dangling-link"])
def test_installers_create_only_when_nothing_exists(home, canonical, legacy, created, mode):
    from aiteam import paths

    _build(home, canonical, legacy)
    resolution, did_create = paths.create_data_dir_if_absent()
    assert (did_create, resolution.mode) == (created, mode)
    assert (home / ".aiteam").is_dir() == (mode in ("canonical", "conflict"))


@pytest.fixture()
def unreadable_legacy(home):
    """A legacy database the user cannot read (permissions): still the user's data."""
    database = _legacy(home) / "aiteam.db"
    _make_db(database)
    database.chmod(0)
    yield database
    database.chmod(0o600)


@pytest.mark.parametrize("canonical", ["absent", "empty"])
def test_an_unreadable_database_is_a_trace_never_shadowed(home, unreadable_legacy, canonical):
    """It is neither "empty" nor "absent": no installer creates a new directory beside it, and an
    empty canonical directory does not win over it (the API would start on an empty database)."""
    from aiteam import paths

    _build(home, canonical, "absent")
    before = _canonical(home).exists()
    resolution, did_create = paths.create_data_dir_if_absent()
    legacy = next(c for c in resolution.evidence if c.role == "legacy")
    assert legacy.state == "unreadable"
    assert (resolution.mode, did_create) == ("missing", False)
    assert _canonical(home).exists() == before


def test_installers_never_shadow_data_left_in_a_moved_config_folder(home, monkeypatch):
    from aiteam import paths

    profile = home / "profiles" / "one" / "config"
    _make_db(profile / "data" / "ai-team-os" / "aiteam.db")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    resolution, did_create = paths.create_data_dir_if_absent()
    assert (resolution.mode, did_create) == ("missing", False)
    assert not (home / ".aiteam").exists()


def test_an_explicit_override_may_be_created(home, monkeypatch):
    from aiteam import paths

    monkeypatch.setenv("AITEAM_DATA_DIR", str(home / "scratch" / "data"))
    resolution, did_create = paths.create_data_dir_if_absent()
    assert did_create and resolution.path.is_dir() and resolution.mode == "env"


def test_paths_report_and_locate(home, monkeypatch, capsys):
    """aiteam paths: one JSON report; --locate finds a database left in a moved config folder."""
    import json

    from aiteam.cli.commands import paths_cmd

    profile = home / ".profiles" / "one" / "config"
    _make_db(profile / "data" / "ai-team-os" / "aiteam.db")
    assert paths_cmd.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["data_dir"]["mode"] == "missing" and report["data_dir"]["database"] is None
    assert report["claude"]["config_dir"] == str(home / ".claude") and report["claude"]["source"] == "default"
    assert report["codex"]["home"] == str(home / ".codex")
    assert "found_elsewhere" not in report["data_dir"] and "api" not in report

    assert paths_cmd.main(["--locate"]) == 0
    located = json.loads(capsys.readouterr().out)["data_dir"]
    assert located["found_elsewhere"] == [str(profile / "data" / "ai-team-os")]

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    assert paths_cmd.main(["--locate"]) == 0
    moved = json.loads(capsys.readouterr().out)
    assert moved["claude"]["source"] == "env"
    config = [c for c in moved["data_dir"]["evidence"] if c["role"] == "config"]
    assert config and config[0]["state"] == "database"
    assert not (home / ".aiteam").exists()


def test_paths_report_shows_the_chain_owner_and_the_install_registry(home, monkeypatch, capsys):
    """aiteam paths names who installed this folder's main chain and where the OS is installed."""
    import json

    from aiteam.cli.commands import paths_cmd
    from aiteam.hooks import hook_core

    (home / ".aiteam").mkdir()
    profile = home / "profiles" / "one"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    (profile / "hooks" / "ai-team-os").mkdir(parents=True)
    hook_core.write_chain_owner("source", baseline="/src/checkout", version="1.2.3")
    hook_core.append_install_record({"host": "cc", "config_dir": str(home / ".claude"), "version": "1.0.0"})
    hook_core.append_install_record({"host": "cc", "config_dir": str(profile), "version": "1.2.2"})
    hook_core.append_install_record({"host": "cc", "config_dir": str(profile), "version": "1.2.3"})

    assert paths_cmd.main([]) == 0
    report = json.loads(capsys.readouterr().out)

    owner = report["claude"]["chain_owner"]
    assert owner["file"] == str(profile / "hooks" / "ai-team-os" / ".chain-owner.json")
    assert owner["installed_by"] == "source" and owner["record"]["baseline"] == "/src/checkout"
    assert report["installs"]["count"] == 3
    assert sorted((r["config_dir"], r["version"]) for r in report["installs"]["latest"]) == [
        (str(home / ".claude"), "1.0.0"), (str(profile), "1.2.3")]

    (profile / "hooks" / "ai-team-os" / ".chain-owner.json").unlink()
    (home / ".aiteam" / "install_path.txt").write_text("/src/checkout", encoding="utf-8")
    assert paths_cmd.main([]) == 0
    owner = json.loads(capsys.readouterr().out)["claude"]["chain_owner"]
    assert owner["record"] is None and owner["installed_by"] == "source"  # the older machine-wide rule


def _install_module():
    import importlib.util

    installer = Path(__file__).resolve().parents[2] / "install.py"
    spec = importlib.util.spec_from_file_location("_paths_test_install", installer)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_step_five_uses_a_legacy_directory_in_place(home, capsys):
    _build(home, "absent", "database")
    assert _install_module().prepare_data_dir() == home / ".claude" / "data" / "ai-team-os"
    assert "legacy location, used in place" in capsys.readouterr().out
    assert not (home / ".aiteam").exists()


def test_install_step_five_stops_when_the_data_was_left_elsewhere(home, monkeypatch, capsys):
    profile = home / "profiles" / "one" / "config"
    _make_db(profile / "data" / "ai-team-os" / "aiteam.db")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    assert _install_module().prepare_data_dir() is None
    out = capsys.readouterr().out
    assert "[FAIL]" in out and str(profile / "data" / "ai-team-os") in out
    assert not (home / ".aiteam").exists()


# ── a link between the two locations is one directory, judged by identity (samefile) ──


def test_a_link_from_the_neutral_directory_back_to_the_old_one_is_not_a_conflict(home):
    """Someone linked the new location to the old data by hand: one database, not two."""
    from aiteam import paths

    legacy = home / ".claude" / "data" / "ai-team-os"
    _make_db(legacy / "aiteam.db")
    (home / ".aiteam").symlink_to(legacy, target_is_directory=True)
    resolution = paths.resolve_data_dir()
    assert resolution.mode == "canonical" and resolution.path == home / ".aiteam"
    assert {c.role: c.state for c in resolution.evidence}["legacy"] == "linked"


def test_a_link_spelled_in_another_letter_case_is_still_the_same_directory(home, tmp_path):
    """On a case-insensitive disk a spelling comparison would see two databases."""
    from aiteam import paths

    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("case-sensitive file system")
    _make_db(home / ".aiteam" / "aiteam.db")
    legacy = home / ".claude" / "data" / "ai-team-os"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(home / ".AITEAM", target_is_directory=True)
    resolution = paths.resolve_data_dir()
    assert resolution.mode == "canonical", resolution.evidence
