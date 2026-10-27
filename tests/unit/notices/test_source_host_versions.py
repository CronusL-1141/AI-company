"""E15 reads only the selected CC installation, including source installs (5fb3622a)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from aiteam.clock import utc_now
from aiteam.services.notices import install_kind, ledger
from aiteam.services.notices.detectors import DetectContext, NoDataError
from aiteam.services.notices.detectors import host_versions as versions

from .conftest import request, write_json


@pytest.fixture(autouse=True)
def local_codex_home(isolated_home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / ".codex"))


def source_install(home: Path, root: Path, version: str = "1.9.0") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "ai-team-os"\nversion = "{version}"\n', encoding="utf-8",
    )
    package = root / "src/aiteam"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    pointer = home / ".aiteam/install_path.txt"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(root), encoding="utf-8")
    return root


def receipt(home: Path, version: str = "1.10.0") -> None:
    write_json(home / ".codex/hooks" / versions.CODEX_OBSERVER_DIRNAME / versions.CODEX_RECEIPT_NAME,
               {"schema": 1, "aiteam_version": version})


def plugin(home: Path, version: str = "1.8.0") -> None:
    write_json(home / ".claude/plugins/installed_plugins.json", {
        "plugins": {"ai-team-os@market": [{"version": version, "installPath": "/plugin"}]},
    })


def context(repo):
    return DetectContext(host="cc", event="SessionStart", source="startup", session_id="s1", cwd="",
                         project_id="", facts={}, now=utc_now(), repo=repo)


async def test_recorded_source_version_can_trigger_e15(repo, isolated_home, tmp_path):
    source_install(isolated_home, tmp_path / "installed source", "1.9.0")
    receipt(isolated_home, "1.10.0")
    [finding] = await versions.HostVersionsDetector().detect(context(repo))
    assert finding.host == "cc" and finding.variant == "cc_source"
    assert finding.key == "host_version_mismatch:1.9.0:1.10.0"
    assert finding.params == {"mine": "v1.9.0", "other": "v1.10.0"}


@pytest.mark.parametrize(("cc", "codex", "older"), [
    ("1.9.0", "1.10.0", "cc"),
    ("1.10.0", "1.9.0", "codex"),
    ("1.10.0", "1.10.0", None),
    ("1.10", "1.10.0", None),
    ("1.10.0rc1", "1.10.0", None),
    ("v1.9.0", "v1.10.0", "cc"),
], ids=["cc-older", "codex-older", "equal", "equal-release", "unordered-suffix", "v-prefix"])
async def test_source_comparison_and_variant(repo, isolated_home, tmp_path, cc, codex, older):
    source_install(isolated_home, tmp_path / "installed", cc)
    receipt(isolated_home, codex)
    findings = await versions.HostVersionsDetector().detect(context(repo))
    if older is None:
        assert findings == []
    else:
        [finding] = findings
        assert finding.host == older
        assert finding.variant == ("cc_source" if older == "cc" else "codex")


@pytest.mark.parametrize("enabled", [True, False], ids=["plugin-selected", "disabled-plugin-source"])
async def test_source_selection_agrees_with_e09(repo, isolated_home, tmp_path, enabled):
    source_install(isolated_home, tmp_path / "installed")
    plugin(isolated_home)
    write_json(isolated_home / ".claude/settings.json", {"enabledPlugins": {"ai-team-os@market": enabled}})
    receipt(isolated_home)
    kind = await install_kind.detect("cc")
    [finding] = await versions.HostVersionsDetector().detect(context(repo))
    assert finding.variant == kind.variant
    assert finding.params["mine"] == ("v1.8.0" if enabled else "v1.9.0")


@pytest.mark.parametrize("damage", [
    "no-install", "missing-root", "empty-path", "relative-path", "missing-project", "foreign-project",
    "broken-project", "missing-package", "broken-package", "dynamic-version", "conflicting-version",
    "bad-version", "numeric-version", "long-version", "oversized-project", "oversized-package",
], ids=str)
async def test_unknown_source_is_no_data_without_api_checkout_fallback(repo, isolated_home, tmp_path, damage):
    root = source_install(isolated_home, tmp_path / "installed")
    pointer = isolated_home / ".aiteam/install_path.txt"
    project = root / "pyproject.toml"
    package = root / "src/aiteam/__init__.py"
    if damage == "no-install":
        pointer.unlink()
    elif damage == "missing-root":
        pointer.write_text(str(tmp_path / "gone"))
    elif damage == "empty-path":
        pointer.write_text("")
    elif damage == "relative-path":
        pointer.write_text(".")  # must not use pytest's current checkout
    elif damage == "missing-project":
        project.unlink()
    elif damage == "foreign-project":
        project.write_text('[project]\nname = "other-project"\nversion = "1.9.0"\n')
    elif damage == "broken-project":
        project.write_text("[broken")
    elif damage == "missing-package":
        package.unlink()
    elif damage == "broken-package":
        package.write_text("__version__ = (")
    elif damage == "dynamic-version":
        package.write_text('raise RuntimeError("must never import source")\n__version__ = str("1.9.0")\n')
    elif damage == "conflicting-version":
        package.write_text('__version__ = "1.8.0"\n')
    elif damage == "bad-version":
        source_install(isolated_home, root, "not-a-version")
    elif damage == "numeric-version":
        project.write_text('[project]\nname = "ai-team-os"\nversion = 19\n')
    elif damage == "long-version":
        source_install(isolated_home, root, "1." + "9" * 65)
    elif damage == "oversized-project":
        project.write_text(project.read_text() + "#" * (256 * 1024))
    elif damage == "oversized-package":
        package.write_text(package.read_text() + "#" * (256 * 1024))
    receipt(isolated_home)
    with pytest.raises(NoDataError):
        await versions.HostVersionsDetector().detect(context(repo))


async def test_source_is_read_as_data_and_off_event_loop(repo, isolated_home, tmp_path, monkeypatch):
    root = source_install(isolated_home, tmp_path / "installed")
    package = root / "src/aiteam/__init__.py"
    package.write_text('raise RuntimeError("must never execute")\n__version__: str = "1.9.0"\n')
    receipt(isolated_home)
    detect = install_kind.detect_sync
    worker_threads = []

    def observed(host):
        worker_threads.append(threading.get_ident())
        return detect(host)

    monkeypatch.setattr(install_kind, "detect_sync", observed)
    [finding] = await versions.HostVersionsDetector().detect(context(repo))
    assert finding.variant == "cc_source"
    assert worker_threads and all(ident != threading.get_ident() for ident in worker_threads)


@pytest.mark.parametrize("missing", ["cc", "codex"], ids=["invalid-selected-plugin", "invalid-codex-receipt"])
async def test_invalid_selected_version_is_no_data(repo, isolated_home, tmp_path, missing):
    source_install(isolated_home, tmp_path / "installed")
    plugin(isolated_home, "not-a-version" if missing == "cc" else "1.9.0")
    receipt(isolated_home, "not-a-version" if missing == "codex" else "1.10.0")
    # A broken selected plugin must not silently borrow the valid source version.
    with pytest.raises(NoDataError):
        await versions.HostVersionsDetector().detect(context(repo))


async def test_source_notice_persists_uses_source_steps_and_clears_after_update(repo, isolated_home,
                                                                              tmp_path, monkeypatch):
    root = source_install(isolated_home, tmp_path / "installed")
    receipt(isolated_home)
    monkeypatch.setattr(versions.HostVersionsDetector, "timeout_s", 10.0)
    monkeypatch.setitem(ledger.DEADLINE_S, "SessionStart", 10.0)
    registry = [versions.HostVersionsDetector()]
    # Detection from the newer host persists the older host's notice without showing it there.
    codex = await ledger.pending(repo, request(host="codex", session="x1"), registry=registry)
    assert codex.user_text == "" and codex.delivery_ids == []
    [stored] = (await repo.list_notices(statuses=["active"], key_prefix="host_version_mismatch:"))[0]
    assert stored.host == "cc" and stored.variant == "cc_source"
    cc = await ledger.pending(repo, request(host="cc", session="c1"), registry=[])
    assert 'OS v1.9.0 in Claude Code is older than v1.10.0' in cc.user_text
    assert len(cc.delivery_ids) == 1
    assert 'python3 install.py --update' in cc.model_text
    assert 'claude plugin' not in cc.model_text
    # Missing source is an incomplete observation: it cannot clear the persisted mismatch.
    (root / "pyproject.toml").unlink()
    await ledger.pending(repo, request(host="cc", session="c2"), registry=registry)
    assert (await repo.get_notice(stored.key)).status.value == "active"
    source_install(isolated_home, root, "1.10.0")
    await ledger.pending(repo, request(host="cc", session="c3"), registry=registry)
    assert (await repo.get_notice(stored.key)).status.value == "cleared"
