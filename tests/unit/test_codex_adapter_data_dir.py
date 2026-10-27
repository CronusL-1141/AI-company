"""codex_adapter.py update moves the shared data directory first, on the plain CLI path only (design §4.1).

The seven gates themselves are exercised on real processes through install.py
(tests/integration/test_config_dir_update.py); here the migration entry is
replaced by a double that returns the production result type, to pin where the
adapter calls it, what it records, and which paths never call it (the JSON
paths the API runs itself through config_change).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from aiteam import data_dir_migration as mig

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "codex_adapter.py"


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    """A Codex install over a machine whose data is still in the legacy location."""
    home = tmp_path / "home"
    legacy = home / ".claude" / "data" / "ai-team-os"
    legacy.mkdir(parents=True)
    with sqlite3.connect(legacy / "aiteam.db") as connection:
        connection.execute("CREATE TABLE tasks (id TEXT)")
    connection.close()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("AITEAM_DATA_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("codex_adapter_data_dir", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (home / ".codex").mkdir()
    assert module.install(ROOT, home / ".codex", Path(sys.executable), dry_run=False) == 0
    calls: list[bool] = []

    def migrate(*, apply: bool = False, **_kwargs) -> mig.MigrationResult:
        calls.append(apply)
        gates = [mig.GateResult(1, "two_sides", "pass"), mig.GateResult(2, "lock", "pass"),
                 mig.GateResult(3, "stop_api", "pass"),
                 mig.GateResult(4, "db_holders", "fail", mig.DB_IN_USE, {"holders": [{"pid": 4242}]})]
        return mig.MigrationResult(action="apply" if apply else "dry_run", ok=False, reason=mig.DB_IN_USE,
                                   legacy_dir=str(legacy), target_dir=str(home / ".aiteam"), gates=gates)

    monkeypatch.setattr(mig, "migrate", migrate)
    module.calls, module.legacy, module.codex_home = calls, legacy, home / ".codex"
    return module


def _records(legacy: Path) -> list[dict]:
    path = legacy / "installs.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def test_plain_update_tries_the_migration_and_records_its_reason(adapter, capsys):
    assert adapter.main(["update", "--codex-home", str(adapter.codex_home)]) == 0
    assert adapter.calls == [True]
    out = capsys.readouterr().out
    assert "[4] 无人开库: 不过 (db_in_use)" in out
    [record] = _records(adapter.legacy)
    assert (record["host"], record["kind"], record["config_dir"]) == ("codex", "codex", str(adapter.codex_home))
    assert (record["migration"]["reason"], record["migration"]["gate"]) == ("db_in_use", 4)
    assert record["migration"]["detail"] == {"holders": [{"pid": 4242}]}


def test_plain_dry_run_previews_the_gates_and_records_nothing(adapter):
    assert adapter.main(["update", "--codex-home", str(adapter.codex_home), "--dry-run"]) == 0
    assert adapter.calls == [False] and _records(adapter.legacy) == []


@pytest.mark.parametrize("extra", [["--json", "--dry-run"], ["--json"], ["--no-migrate-data"]],
                         ids=["json-preview", "json-apply", "no-migrate-data"])
def test_json_paths_and_the_opt_out_never_move_data(adapter, capsys, extra):
    assert adapter.main(["update", "--codex-home", str(adapter.codex_home), *extra]) == 0
    assert adapter.calls == []
    if "--json" in extra:
        json.loads(capsys.readouterr().out)  # stdout stays one JSON document
    else:
        assert "migration" not in _records(adapter.legacy)[-1]
