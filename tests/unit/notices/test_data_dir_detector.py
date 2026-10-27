"""E28 data_dir_legacy: found from the resolver's own answer (docs/config-dir-compat-design.md §5.3)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from aiteam.services.notices.catalog import CATALOG, render_entry
from aiteam.services.notices.detectors import REGISTRY, DetectContext
from aiteam.services.notices.detectors.data_dir import MIGRATE_COMMAND, DataDirDetector


def _ctx(host: str = "cc") -> DetectContext:
    return DetectContext(host=host, event="SessionStart", source="startup", session_id="s", cwd="/",
                         project_id="", facts={}, now=datetime.now(UTC), repo=None)


def _legacy(home):
    (home / ".aiteam").rmdir()
    legacy = home / ".claude" / "data" / "ai-team-os"
    legacy.mkdir(parents=True)
    with sqlite3.connect(legacy / "aiteam.db") as connection:
        connection.execute("CREATE TABLE t (x)")
    connection.close()


def test_registered_for_both_hosts_at_session_start():
    detector = next(d for d in REGISTRY if d.name == "data_dir")
    assert detector.catalog_ids == ("data_dir_legacy",) and detector.hosts == {"cc", "codex"}


async def test_canonical_data_directory_reports_nothing(isolated_home):
    assert await DataDirDetector().detect(_ctx()) == []


@pytest.mark.parametrize("host", ["cc", "codex"])
async def test_legacy_directory_reports_the_manual_command(isolated_home, host):
    _legacy(isolated_home)
    [finding] = await DataDirDetector().detect(_ctx(host))
    assert (finding.catalog_id, finding.key, finding.variant) == ("data_dir_legacy", "data_dir_legacy:pending", "")
    assert finding.params == {"reason": "", "command": MIGRATE_COMMAND}
    rendered = render_entry(CATALOG["data_dir_legacy"], language="zh", host=host, params=finding.params)
    assert MIGRATE_COMMAND in rendered.plain and "迁移 OS 数据" in rendered.plain
    assert "不带 --apply" in rendered.model and "--user-quote" in rendered.model


async def test_plugin_install_gets_the_plugin_variant(isolated_home):
    _legacy(isolated_home)
    plugins = isolated_home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"ai-team-os@ai-team-os": [{"scope": "user", "installPath": "/p"}]}}))
    [finding] = await DataDirDetector().detect(_ctx())
    assert finding.variant == "plugin" and finding.params["reason"] == "plugin_session_start"


def _attempt(data_dir, reason, *, also_after: bool = False):
    """Install records as an update leaves them (installs.json, see hook_core.append_install_record)."""
    records = [{"host": "cc", "kind": "source", "migration": {"ok": False, "reason": reason, "gate": 4}}]
    if also_after:
        records.append({"host": "cc", "kind": "source"})  # a later plain install tried nothing
    (data_dir / "installs.json").write_text(json.dumps(records))


@pytest.mark.parametrize("also_after", [False, True], ids=["last-record", "earlier-record"])
async def test_the_last_update_attempt_gives_the_reason(isolated_home, also_after):
    _legacy(isolated_home)
    _attempt(isolated_home / ".claude" / "data" / "ai-team-os", "db_in_use", also_after=also_after)
    [finding] = await DataDirDetector().detect(_ctx())
    assert (finding.variant, finding.key) == ("gate", "data_dir_legacy:db_in_use")
    assert finding.params["reason"] == "db_in_use"
    rendered = render_entry(CATALOG["data_dir_legacy"], language="zh", host="cc", params=finding.params,
                            variant=finding.variant)
    assert "db_in_use" in rendered.plain and "不带 --apply" in rendered.model


async def test_a_recorded_reason_outranks_the_plugin_variant(isolated_home):
    """A plugin user whose update (e.g. the Codex adapter's) tried and failed hears why."""
    _legacy(isolated_home)
    plugins = isolated_home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {"ai-team-os@m": [{}]}}))
    _attempt(isolated_home / ".claude" / "data" / "ai-team-os", "locked")
    [finding] = await DataDirDetector().detect(_ctx())
    assert (finding.variant, finding.params["reason"]) == ("gate", "locked")
