"""The default database path: resolved through aiteam.paths, never created at run time.

docs/config-dir-compat-design.md §2, §5.1, §5.2, §10 item 1. The database lives
in the OS data directory (``aiteam.db``); ``AITEAM_DB_PATH`` names the file
exactly and may have its parent created. Resolving the default creates nothing,
no longer copies an ``aiteam.db`` it finds in the working directory, and is not
done at import time.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aiteam.paths import DataDirMissing
from aiteam.storage.connection import _default_db_url, default_db_path

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("AITEAM_DB_PATH", "AITEAM_DATA_DIR", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class TestDefaultLocation:
    def test_canonical_data_directory(self, home: Path) -> None:
        (home / ".aiteam").mkdir()
        assert _default_db_url() == f"sqlite+aiosqlite:///{home / '.aiteam' / 'aiteam.db'}"

    def test_legacy_data_directory_is_used_in_place(self, home: Path) -> None:
        legacy = home / ".claude" / "data" / "ai-team-os"
        legacy.mkdir(parents=True)
        with sqlite3.connect(legacy / "aiteam.db") as connection:
            connection.execute("CREATE TABLE t (x)")
        connection.close()
        assert default_db_path() == legacy / "aiteam.db"

    def test_no_data_directory_raises_and_creates_nothing(self, home: Path) -> None:
        with pytest.raises(DataDirMissing) as raised:
            _default_db_url()
        assert str(home / ".aiteam") in str(raised.value)
        assert _tree(home) == []

    def test_a_database_in_the_working_directory_is_never_copied(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removed start-up copy (>10KB aiteam.db in the cwd) stays removed."""
        (home / ".aiteam").mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / "aiteam.db").write_bytes(b"x" * 20000)
        monkeypatch.chdir(cwd)
        _default_db_url()
        assert not (home / ".aiteam" / "aiteam.db").exists()
        assert _tree(cwd) == ["aiteam.db"]


class TestDbPathEnvOverride:
    def test_env_override_used_and_its_parent_created(self, home: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "custom" / "demo.db"
        monkeypatch.setenv("AITEAM_DB_PATH", str(target))
        assert _default_db_url() == f"sqlite+aiosqlite:///{target}"
        assert target.parent.is_dir()
        assert _tree(home) == [], "an explicit database needs no data directory"

    def test_env_override_expands_user(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AITEAM_DB_PATH", "~/demo-data/aiteam.db")
        expected = home / "demo-data" / "aiteam.db"
        assert _default_db_url() == f"sqlite+aiosqlite:///{expected}"
        assert expected.parent.is_dir()

    def test_empty_env_override_falls_back_to_the_data_directory(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AITEAM_DB_PATH", "")
        (home / ".aiteam").mkdir()
        assert default_db_path() == home / ".aiteam" / "aiteam.db"


def test_import_resolves_nothing(tmp_path: Path) -> None:
    """Importing the storage, API and MCP modules on a machine without a data directory works and creates nothing."""
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items()
           if key not in ("AITEAM_DB_PATH", "AITEAM_DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME")}
    env.update(HOME=str(home), PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", "import aiteam.storage.connection, aiteam.api.app, aiteam.mcp.server"],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert _tree(home) == []
