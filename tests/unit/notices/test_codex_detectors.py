"""Codex E13/E16: isolated homes, native trust semantics and persisted recovery."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.services.notices import detectors, ledger
from aiteam.services.notices.detectors import DetectContext, NoDataError, run_detectors, selected
from aiteam.services.notices.detectors import codex_copies as copies
from aiteam.services.notices.detectors import codex_trust as trust
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import EventModel
from aiteam.storage.repository import StorageRepository

from .conftest import write_json


@pytest.fixture(autouse=True)
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    copies._receipt_at.cache_clear()
    copies._distributed_names.cache_clear()
    yield home
    copies._receipt_at.cache_clear()
    copies._distributed_names.cache_clear()


def _context(repo, **updates):
    ctx = DetectContext(host="cc", event="SessionStart", source="startup", session_id="s1", cwd="",
                        project_id="", facts={}, now=utc_now(), repo=repo)
    return replace(ctx, **updates)


def _install(home, root, content=b"original\n"):
    source = root / copies.ADAPTER_PATH
    source.mkdir(parents=True)
    installed = home / "hooks" / copies.OBSERVER_DIRNAME
    installed.mkdir(parents=True)
    names = ["hook.py", "hook_core.py", "user_notice.py"]
    for directory in (source, installed):
        for name in names:
            (directory / name).write_bytes(content)
    _surface(source)
    write_json(installed / copies.RECEIPT_NAME, {
        "schema": 1, "repo_root": str(root), "files": names,
        "sha256": {name: hashlib.sha256(content).hexdigest() for name in names},
    })
    _manifest(home)
    return source, installed


def _surface(source, scripts=("hook.py",), companions=()):
    # Same declarations/derivation as the actual distribution surface. The
    # undefined KIND proves parsing these statements never executes Python.
    rows = ", ".join(f'("{name}", "", 5, None)' for name in scripts)
    (source.parent / "surface.py").write_text(
        f'CODEX_HOOK_SURFACE = [("SessionStart", "", KIND_OBSERVE, [{rows}])]\n'
        'CODEX_HOOK_SCRIPTS = tuple(dict.fromkeys(script for _e, _m, _k, entries in CODEX_HOOK_SURFACE '
        'for script, _a, _t, _l in entries))\n'
        f'CODEX_SUPPORT_MODULES = {companions!r}\n', encoding="utf-8",
    )


def _manifest(home, **fields):
    handler = {"type": "command", "command": f'python3 "{home}/hooks/{copies.OBSERVER_DIRNAME}/hook.py"',
               "timeout": 5, **fields}
    group = {"matcher": "startup", "hooks": [handler]}
    write_json(home / "hooks.json", {"hooks": {"SessionStart": [group]}})
    (home / "config.toml").write_text("", encoding="utf-8")
    return group, handler


def _save_trust(home, group, handler, *, saved=None, enabled=True, index=0):
    key = f"{home / 'hooks.json'}:session_start:0:{index}"
    value = trust.trusted_hash("SessionStart", group, handler) if saved is None else saved
    (home / "config.toml").write_text(
        f"[hooks.state.{json.dumps(key)}]\ntrusted_hash = {json.dumps(value)}\n"
        f"enabled = {str(enabled).lower()}\n", encoding="utf-8",
    )


async def test_receipt_baseline_distinguishes_old_modified_missing_and_current(repo, codex_home, tmp_path):
    source, installed = _install(codex_home, tmp_path / "adapter")
    detector = copies.CodexCopiesDetector()
    assert await detector.detect(_context(repo)) == []
    (source / "hook.py").write_bytes(b"upstream\n")
    [old] = await detector.detect(_context(repo))
    assert old.variant == "" and old.params == {"n": 1}
    # Even if the same-named CC hook matches, only the adapter is authoritative.
    cc = tmp_path / "adapter/src/aiteam/hooks"
    cc.mkdir(parents=True)
    (cc / "hook.py").write_bytes(b"original\n")
    assert await detector.detect(_context(repo)) == [old]
    (installed / "hook.py").write_bytes(b"customized\n")
    [modified] = await detector.detect(_context(repo))
    assert modified.variant == "modified" and modified.key != old.key
    (installed / "hook.py").unlink()
    [missing] = await detector.detect(_context(repo))
    assert missing.variant == "missing" and missing.key != modified.key
    # A successful manual copy can clear even before its receipt is rewritten.
    (installed / "hook.py").write_bytes(b"upstream\n")
    assert await detector.detect(_context(repo)) == []
    (source / "new_companion.py").write_bytes(b"new\n")
    _surface(source, companions=("new_companion.py",))
    [added] = await detector.detect(_context(repo))
    assert added.variant == "missing"
    (installed / "new_companion.py").write_bytes(b"new\n")
    (installed / "personal.py").write_bytes(b"user-owned\n")
    assert await detector.detect(_context(repo)) == []


async def test_file_io_is_threaded_and_cache_detects_preserved_mtime(repo, codex_home, tmp_path, monkeypatch):
    source, installed = _install(codex_home, tmp_path / "adapter")
    original = copies.read_bounded
    calls = []

    def read(path, limit):
        assert threading.current_thread() is not threading.main_thread()
        calls.append(path)
        return original(path, limit)

    monkeypatch.setattr(copies, "read_bounded", read)
    detector = copies.CodexCopiesDetector()
    await detector.detect(_context(repo))
    first = len(calls)
    await detector.detect(_context(repo))
    assert len(calls) == first
    path = installed / "hook.py"
    before = path.stat()
    path.write_bytes(b"Modified\n")  # same length and restored mtime, different ctime
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    [hit] = await detector.detect(_context(repo))
    assert hit.variant == "modified" and len(calls) > first
    assert (source / "hook.py").read_bytes() == b"original\n"


@pytest.mark.parametrize("retired", [True, False], ids=["retired-from-surface", "declared-source-missing"])
async def test_deleted_source_does_not_hide_other_stale_copies(repo, codex_home, tmp_path, retired):
    source, installed = _install(codex_home, tmp_path / "adapter")
    (source / "hook.py").unlink()
    if retired:
        _surface(source, scripts=())
    (source / "hook_core.py").write_bytes(b"upgrade\n")
    detector = copies.CodexCopiesDetector()
    runs = await run_detectors(_context(repo), [detector])
    assert runs[0].findings is not None
    expected = {"": 1, "retired": 1} if retired else {"": 1, "source_missing": 1}
    assert {hit.variant: hit.params["n"] for hit in runs[0].findings} == expected
    await ledger.apply_runs(repo, runs, utc_now(), host="cc")
    persisted, _ = await repo.list_notices(statuses=("active",), catalog_ids=detector.catalog_ids)
    assert {row.variant: row.params["n"] for row in persisted} == expected
    if retired:
        (installed / "hook.py").write_bytes(b"custom\n")
        assert {hit.variant: hit.params["n"] for hit in await detector.detect(_context(repo))} == {
            "": 1, "modified": 1,
        }
        (installed / "hook.py").unlink()
        assert "retired" in {hit.variant for hit in await detector.detect(_context(repo))}
        write_json(codex_home / "hooks.json", {"hooks": {}})
    else:
        (source / "hook.py").write_bytes(b"original\n")
    (installed / "hook_core.py").write_bytes(b"upgrade\n")
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    assert (await repo.list_notices(statuses=("active",), catalog_ids=detector.catalog_ids))[1] == 0


async def test_only_declared_distribution_files_can_be_missing(repo, codex_home, tmp_path):
    source, _ = _install(codex_home, tmp_path / "adapter")
    detector = copies.CodexCopiesDetector()
    assert await detector.detect(_context(repo)) == []
    (source / "developer_helper.py").write_bytes(b"not distributed\n")
    assert await detector.detect(_context(repo)) == []
    _surface(source, companions=("developer_helper.py",))
    [hit] = await detector.detect(_context(repo))
    assert hit.variant == "missing" and hit.params == {"n": 1}


def test_distribution_ast_matches_the_actual_installer_set():
    root = Path(__file__).resolve().parents[3]
    surface_path = root / "plugin/harness/codex/surface.py"
    spec = importlib.util.spec_from_file_location("notice_surface_parity", surface_path)
    surface = importlib.util.module_from_spec(spec)
    # The known checkout fixture is safe to load in this parity test; the
    # production detector only parses the receipt-selected repo using AST.
    exec(compile(surface_path.read_bytes(), str(surface_path), "exec"), surface.__dict__)
    expected = frozenset((*surface.CODEX_HOOK_SCRIPTS, *surface.CODEX_SUPPORT_MODULES,
                          "hook_core.py", "user_notice.py"))
    assert copies._distributed_names(surface_path, copies.file_signature(surface_path)) == expected


@pytest.mark.parametrize("customized", [False, True], ids=["retired-original", "retired-customized"])
async def test_real_adapter_updates_keep_retired_notice_tracked(repo, codex_home, tmp_path, customized):
    import ast
    import asyncio

    root = Path(__file__).resolve().parents[3]
    source = tmp_path / "real-adapter-source"
    shutil.copytree(root / "plugin/harness/codex", source / "plugin/harness/codex",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
    spec = importlib.util.spec_from_file_location("notice_retired_adapter", root / "scripts/codex_adapter.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    await asyncio.to_thread(adapter.install, source, codex_home, Path(sys.executable), dry_run=False, hooks_only=True)
    installed = codex_home / "hooks" / copies.OBSERVER_DIRNAME
    name = "channel_unread_codex.py"
    old_digest = hashlib.sha256((installed / name).read_bytes()).hexdigest()
    manifest_before = (codex_home / "hooks.json").read_bytes()

    # Retire the final real handler from the source surface without renumbering
    # the other events. This mutates only the temporary installation source.
    surface_path = source / "plugin/harness/codex/surface.py"
    source_text = surface_path.read_text()
    tree = ast.parse(source_text)
    surface = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                   and isinstance(node.target, ast.Name) and node.target.id == "CODEX_HOOK_SURFACE")
    retired = next(row for row in surface.elts if ast.literal_eval(row.elts[0]) == "UserPromptSubmit")
    lines = source_text.splitlines(keepends=True)
    surface_path.write_text("".join(lines[:retired.lineno - 1] + lines[retired.end_lineno:]))
    (source / copies.ADAPTER_PATH / name).unlink()
    if customized:
        (installed / name).write_bytes(b"# user customization that an update must keep\n")
    actual_bytes = (installed / name).read_bytes()
    detector = copies.CodexCopiesDetector()
    [before] = await detector.detect(_context(repo))
    assert before.variant == ("modified" if customized else "retired")
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")

    for _ in range(2):
        preview = await asyncio.to_thread(adapter.preview_update, source, codex_home)
        assert not any(item["path"] == str(installed / name) for item in preview["targets"])
        await asyncio.to_thread(adapter.apply_update, source, codex_home, expected_preview=preview)
        assert await detector.detect(_context(repo)) == [before]
        receipt = json.loads((installed / copies.RECEIPT_NAME).read_text())
        assert name not in receipt["files"]
        assert receipt["retired_files"] == [name]
        assert receipt["retired_sha256"] == {name: old_digest}
        assert (installed / name).read_bytes() == actual_bytes
        assert (codex_home / "hooks.json").read_bytes() == manifest_before
        await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
        assert (await repo.get_notice(before.key)).status == "active"

    # Deleting only the obsolete script leaves an executable declaration.
    (installed / name).unlink()
    [declaration] = await detector.detect(_context(repo))
    assert declaration.variant == "retired"
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    assert (await repo.get_notice(declaration.key)).status == "active"
    (codex_home / "hooks.json").unlink()
    [unknown] = await run_detectors(_context(repo), [detector])
    assert unknown.findings is None
    await ledger.apply_runs(repo, [unknown], utc_now(), host="cc")
    assert (await repo.get_notice(declaration.key)).status == "active"
    manifest = json.loads(manifest_before)
    manifest["hooks"].pop("UserPromptSubmit")
    write_json(codex_home / "hooks.json", manifest)
    assert await detector.detect(_context(repo)) == []
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    assert (await repo.get_notice(declaration.key)).status == "cleared"


@pytest.mark.parametrize("damage", ["bad_hash", "unsafe_name", "overlap", "wrong_type"], ids=str)
async def test_invalid_retired_receipt_is_no_data(repo, codex_home, tmp_path, damage):
    _, installed = _install(codex_home, tmp_path / "adapter")
    path = installed / copies.RECEIPT_NAME
    receipt = json.loads(path.read_text())
    receipt.update(retired_files=["old.py"], retired_sha256={"old.py": "1" * 64})
    if damage == "bad_hash":
        receipt["retired_sha256"] = {}
    elif damage == "unsafe_name":
        receipt["retired_files"] = ["../old.py"]
    elif damage == "overlap":
        receipt["retired_files"] = ["hook.py"]
        receipt["retired_sha256"] = {"hook.py": receipt["sha256"]["hook.py"]}
    else:
        receipt["retired_files"] = [{}]
    write_json(path, receipt)
    with pytest.raises(NoDataError):
        await copies.CodexCopiesDetector().detect(_context(repo))


@pytest.mark.parametrize("shape", ["relative", "env-wrapper"], ids=str)
async def test_uncertain_retired_registration_cannot_clear_ledger(repo, codex_home, tmp_path, shape):
    source, installed = _install(codex_home, tmp_path / "adapter")
    (source / "hook.py").unlink()
    _surface(source, scripts=())
    detector = copies.CodexCopiesDetector()
    [before] = await detector.detect(_context(repo))
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    (installed / "hook.py").unlink()
    command = "python3 hook.py" if shape == "relative" else f'env python3 "{installed / "hook.py"}"'
    write_json(codex_home / "hooks.json", {"hooks": {"SessionStart": [{
        "hooks": [{"type": "command", "command": command}],
    }]}})
    [run] = await run_detectors(_context(repo), [detector])
    assert run.findings is None
    await ledger.apply_runs(repo, [run], utc_now(), host="cc")
    assert (await repo.get_notice(before.key)).status == "active"


@pytest.mark.parametrize("damage", ["receipt_missing", "receipt_json", "receipt_schema", "traversal",
                                    "source_empty", "installed_symlink"], ids=str)
async def test_missing_or_unsafe_evidence_never_means_healthy(repo, codex_home, tmp_path, damage):
    source, installed = _install(codex_home, tmp_path / "adapter")
    detector = copies.CodexCopiesDetector()
    receipt_path = installed / copies.RECEIPT_NAME
    if damage == "receipt_missing":
        receipt_path.unlink()
    elif damage == "receipt_json":
        receipt_path.write_text("{")
    elif damage in {"receipt_schema", "traversal"}:
        receipt = json.loads(receipt_path.read_text())
        if damage == "receipt_schema":
            receipt["schema"] = 2
        else:
            receipt["files"] = ["../private.py"]
        write_json(receipt_path, receipt)
    elif damage == "source_empty":
        for path in source.iterdir():
            path.unlink()
    else:
        (installed / "hook.py").unlink()
        (installed / "hook.py").symlink_to(source / "hook.py")
    [run] = await run_detectors(_context(repo), [detector])
    assert run.findings is None


def test_trust_hash_normalizes_host_defaults_without_hashing_script_body():
    command = {"type": "command", "command": "python3 /tmp/hook.py"}
    group = {"hooks": [command]}
    baseline = trust.trusted_hash("SessionStart", group, command)
    explicit = {**command, "timeout": 600, "async": False, "additionalContextLimit": 2500}
    assert trust.trusted_hash("SessionStart", {"matcher": None}, explicit) == baseline
    assert trust.trusted_hash("SessionStart", {}, {**command, "commandWindows": "other"}) == baseline
    assert trust.trusted_hash("SessionStart", {}, {**command, "timeout": 5}) != baseline
    assert trust.trusted_hash("SessionStart", {"matcher": "startup"}, command) != baseline
    assert trust.trusted_hash("Stop", {"matcher": "ignored"}, command) == trust.trusted_hash("Stop", {}, command)
    assert trust.trusted_hash("SessionEnd", {}, {**command, "timeout": 99}) == trust.trusted_hash(
        "SessionEnd", {}, {**command, "timeout": 3})
    assert trust.trusted_hash("SessionEnd", {}, command) == trust.trusted_hash(
        "SessionEnd", {}, {**command, "timeout": 1})
    assert trust.trusted_hash("SessionStart", {}, {**command, "additionalContextLimit": 0}) != baseline
    assert trust.trusted_hash("Stop", {}, {**command, "additionalContextLimit": 0}) == trust.trusted_hash(
        "Stop", {}, command)
    assert trust.trusted_hash("SessionStart", {}, {**command, "commandWindows": "windows"}, windows=True) != baseline


def test_trust_hash_matches_independent_native_rust_golden():
    # Generated offline by running the official Rust serde definitions,
    # hook_hash and version_for_toml at 0a2eb4696c26ac33204bcd255721ab30220a4774.
    # toml 0.9.11 / serde 1.0.228 / serde_json 1.0.149 / sha2 0.10.9.
    # This literal must not be regenerated by the Python implementation.
    handler = {"type": "command", "command": "python3 /tmp/hook.py", "timeout": 5}
    assert trust.trusted_hash("SessionStart", {"matcher": "startup"}, handler) == (
        "sha256:6bbce890235652dabdab4794e855e084dd2c0fa632f0d959d760b479d32cf23a"
    )
    assert trust.trusted_hash("SessionStart", {}, {
        "type": "command", "command": "python3 /tmp/hook.py", "commandWindows": "ignored",
    }) == "sha256:a077d8e9f6b59dd3d156ad5bb95f6f4d7bc5d58f78c1b6c931b30a1d9a697760"
    assert trust.trusted_hash("UserPromptSubmit", {"matcher": "ignored"}, {
        **handler, "timeout": 0, "additionalContextLimit": 0,
    }) == "sha256:985cec642a76371242186b4c2057d480fedd0f866e287bcb2e39da5cefa9f59f"
    assert trust.trusted_hash("SessionStart", {"matcher": "startup"}, {
        **handler, "command": "python3 /tmp/提示.py", "statusMessage": "检查中",
    }) == "sha256:f1504f5ae6f6ad5fc613e95f41a2083720a5de35f162e67d3d9c52f7af8577ac"


async def test_trust_wildcard_in_real_adapter_shape_is_supported(repo, codex_home):
    group, handler = _manifest(codex_home)
    group["matcher"] = "*"
    write_json(codex_home / "hooks.json", {"hooks": {"PreToolUse": [group]}})
    key = f"{codex_home / 'hooks.json'}:pre_tool_use:0:0"
    digest = trust.trusted_hash("PreToolUse", group, handler)
    (codex_home / "config.toml").write_text(
        f"[hooks.state.{json.dumps(key)}]\ntrusted_hash = {json.dumps(digest)}\n", encoding="utf-8",
    )
    assert await trust.CodexTrustDetector().detect(_context(repo)) == []


async def test_trust_missing_modified_trusted_and_positional_identity(repo, codex_home):
    group, handler = _manifest(codex_home)
    detector = trust.CodexTrustDetector()
    [untrusted] = await detector.detect(_context(repo))
    assert untrusted.variant == ""
    _save_trust(codex_home, group, handler, saved="sha256:old")
    assert await detector.detect(_context(repo)) == [untrusted]
    _save_trust(codex_home, group, handler)
    assert await detector.detect(_context(repo)) == []
    # Moving the handler invalidates its positional key, even with identical command.
    unrelated = {"type": "command", "command": "python3 /tmp/personal.py"}
    group["hooks"].insert(0, unrelated)
    write_json(codex_home / "hooks.json", {"hooks": {"SessionStart": [group]}})
    [moved] = await detector.detect(_context(repo))
    assert moved.key != untrusted.key
    _save_trust(codex_home, group, handler, index=1)
    assert await detector.detect(_context(repo)) == []


@pytest.mark.parametrize(("canonical", "legacy", "disabled"), [
    (False, None, True), (False, True, True), (True, False, False), (None, False, True), (None, True, False),
], ids=["current-off", "current-off-overrides-alias-on", "current-on-overrides-alias-off", "legacy-off", "legacy-on"])
async def test_hooks_feature_toggle_precedence_preserves_existing_notice(repo, codex_home, canonical, legacy, disabled):
    _manifest(codex_home)
    detector = trust.CodexTrustDetector()
    [strong] = await detector.detect(_context(repo))
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    fields = "[features]\n"
    for name, value in (("hooks", canonical), ("codex_hooks", legacy)):
        if value is not None:
            fields += f"{name} = {str(value).lower()}\n"
    (codex_home / "config.toml").write_text(fields, encoding="utf-8")
    runs = await run_detectors(_context(repo), [detector])
    assert (runs[0].findings is None) is disabled
    if not disabled:
        assert runs[0].findings == [strong]
    await ledger.apply_runs(repo, runs, utc_now(), host="cc")
    assert (await repo.get_notice(strong.key)).status == "active"


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-alias", "relative-alias"])
async def test_explicit_home_alias_uses_canonical_trust_keys(repo, codex_home, tmp_path, monkeypatch, relative):
    alias = tmp_path / "codex-alias"
    alias.symlink_to(codex_home, target_is_directory=True)
    group, handler = _manifest(alias)
    canonical = codex_home.resolve()
    _save_trust(canonical, group, handler)  # native key is canonical; command still uses the alias
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", alias.name if relative else str(alias))
    assert await trust.CodexTrustDetector().detect(_context(repo)) == []
    assert copies.codex_home() == canonical


async def test_default_home_keeps_native_lexical_identity(repo, codex_home, tmp_path, monkeypatch):
    home = tmp_path / "native-user-home"
    home.mkdir()
    alias = home / ".codex"
    alias.symlink_to(codex_home, target_is_directory=True)
    group, handler = _manifest(alias)
    _save_trust(alias, group, handler)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME")
    assert copies.codex_home() == alias and copies.codex_home() != codex_home.resolve()
    assert await trust.CodexTrustDetector().detect(_context(repo)) == []


@pytest.mark.parametrize("kind", ["missing", "file", "literal-tilde"], ids=str)
def test_invalid_explicit_home_is_no_data(tmp_path, monkeypatch, kind):
    path = tmp_path / "invalid-home"
    if kind == "file":
        path.write_text("not a directory")
    monkeypatch.setenv("CODEX_HOME", "~" if kind == "literal-tilde" else str(path))
    with pytest.raises(NoDataError):
        copies.codex_home()


async def test_other_homes_and_disabled_hooks_are_not_missing_trust(repo, codex_home):
    group, handler = _manifest(codex_home)
    _save_trust(codex_home, group, handler, saved="sha256:old", enabled=False)
    assert await trust.CodexTrustDetector().detect(_context(repo)) == []
    handler["command"] = "python3 /another/codex/hooks/ai-team-os-observer/hook.py"
    write_json(codex_home / "hooks.json", {"hooks": {"SessionStart": [group]}})
    with pytest.raises(NoDataError):
        await trust.CodexTrustDetector().detect(_context(repo))


def test_ownership_is_the_executed_script_not_an_arbitrary_argument(codex_home):
    installed = codex_home / "hooks" / copies.OBSERVER_DIRNAME
    script = str(installed / "hook.py")
    assert trust._owned(f'python3 "{script}" --optional arg', installed)
    assert not trust._owned(f'echo "{script}"', installed)
    assert not trust._owned(f'python3 /other/program.py --input "{script}"', installed)
    with pytest.raises(trust.UnsupportedDeclarationError):
        trust._owned(f'env python3 "{script}"', installed)


@pytest.mark.parametrize("suffix", ["; echo done", ">/tmp/output"], ids=["attached-semicolon", "redirect"])
async def test_mixed_trusted_and_unverified_shell_handlers_preserve_confirmed_ledger_issue(
    repo, codex_home, suffix,
):
    group, handler = _manifest(codex_home)
    _save_trust(codex_home, group, handler)
    other = {"type": "command", "command": f"python3 {codex_home}/hooks/{copies.OBSERVER_DIRNAME}/b.py"}
    group["hooks"].append(other)
    write_json(codex_home / "hooks.json", {"hooks": {"SessionStart": [group]}})
    detector = trust.CodexTrustDetector()
    [strong] = await detector.detect(_context(repo))
    assert strong.variant == ""
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    other["command"] += suffix
    write_json(codex_home / "hooks.json", {"hooks": {"SessionStart": [group]}})
    [run] = await run_detectors(_context(repo), [detector])
    assert run.findings is None  # the trusted first handler cannot hide the unknown second one
    await ledger.apply_runs(repo, [run], utc_now(), host="cc")
    persisted = await repo.get_notice(strong.key)
    assert persisted.status == "active" and persisted.variant == ""


@pytest.mark.parametrize("damage", ["config_missing", "config_bad", "manifest_missing", "manifest_bad"], ids=str)
async def test_unreadable_trust_files_produce_no_data(repo, codex_home, damage):
    _manifest(codex_home)
    path = codex_home / ("config.toml" if damage.startswith("config") else "hooks.json")
    if damage.endswith("missing"):
        path.unlink()
    else:
        path.write_text("not a document")
    [run] = await run_detectors(_context(repo), [trust.CodexTrustDetector()])
    assert run.findings is None


async def test_unverified_uses_recent_persisted_codex_events_without_declaring_health(repo, codex_home):
    _manifest(codex_home, futureField=True)
    detector = trust.CodexTrustDetector()
    [weak] = await detector.detect(_context(repo))
    assert weak.variant == "unverified"
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    team = await repo.create_team("native", mode="coordinate")
    agent = await repo.create_agent(team_id=team.id, name="Codex", role="leader", session_id="test")
    await repo.update_agent(agent.id, harness="codex")
    # Production tool completion events have no data.harness: the persisted
    # session's agent identifies the host (hook_translator.py completion_data).
    event = await repo.create_event("cc.tool_complete", "session:test", {
        "session_id": "test", "tool_name": "exec_command", "tool_call_id": "call1",
    })
    [run] = await run_detectors(_context(repo), [detector])
    assert run.findings is None
    await ledger.apply_runs(repo, [run], utc_now(), host="cc")
    assert (await repo.get_notice(weak.key)).status == "active"
    async with get_session(repo._db_url) as session:
        await session.execute(update(EventModel).where(EventModel.id == event.id).values(
            timestamp=utc_now() - timedelta(days=8)))
    assert await detector.detect(_context(repo)) == [weak]


async def test_unverified_cannot_replace_confirmed_issue_even_after_manifest_changes(repo, codex_home):
    group, handler = _manifest(codex_home)
    detector = trust.CodexTrustDetector()
    [strong] = await detector.detect(_context(repo))
    await ledger.apply_runs(repo, await run_detectors(_context(repo), [detector]), utc_now(), host="cc")
    handler["futureField"] = True
    write_json(codex_home / "hooks.json", {"hooks": {"SessionStart": [group]}})
    [run] = await run_detectors(_context(repo), [detector])
    assert run.findings is None
    await ledger.apply_runs(repo, [run], utc_now(), host="cc")
    persisted = await repo.get_notice(strong.key)
    assert persisted.status == "active" and persisted.variant == ""


def test_registry_host_and_timing_scope(repo):
    registry = [copies.CodexCopiesDetector(), trust.CodexTrustDetector()]
    ctx = _context(repo)
    assert [d.name for d in selected(ctx, "session_start", fresh=False, registry=registry)] == [
        "codex_copies", "codex_trust"]
    assert [d.name for d in selected(replace(ctx, host="codex"), "session_start", fresh=False,
                                     registry=registry)] == ["codex_copies"]
    assert selected(ctx, "prompt", fresh=False, registry=registry) == []
    assert len(selected(ctx, None, fresh=True, registry=registry)) == 2
    assert {d.name for d in detectors.REGISTRY} >= {"codex_copies", "codex_trust"}


@asynccontextmanager
async def _client(db_url, monkeypatch, registry):
    repository = StorageRepository(db_url=db_url)
    await repository.init_db()
    monkeypatch.setattr(deps, "_repository", repository)
    monkeypatch.setattr(deps, "_event_bus", EventBus(repo=repository))
    monkeypatch.setattr(detectors, "REGISTRY", tuple(registry))
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, repository


@pytest.mark.parametrize("case", ["old", "modified", "missing", "untrusted", "unverified"], ids=str)
async def test_api_persists_findings_preserves_missing_evidence_and_clears_only_verified_recovery(
    codex_home, tmp_path, monkeypatch, case,
):
    source, installed = _install(codex_home, tmp_path / "adapter")
    group, handler = _manifest(codex_home, **({"futureField": True} if case == "unverified" else {}))
    if case == "old":
        (source / "hook.py").write_bytes(b"upgrade\n")
    elif case == "modified":
        (installed / "hook.py").write_bytes(b"user edit\n")
    elif case == "missing":
        (installed / "hook.py").unlink()
    is_copies = case in {"old", "modified", "missing"}
    detector = copies.CodexCopiesDetector() if is_copies else trust.CodexTrustDetector()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}"
    body = {"host": "cc", "event": "SessionStart", "source": "startup", "session_id": "s1",
            "facts": {"fallback_language": "en", "entrypoint": "cli"}}
    try:
        async with _client(db_url, monkeypatch, [detector]) as (client, first):
            response = await client.post("/api/notices/pending", json=body)
            assert response.status_code == 200
            assert response.json()["user_text"] and len(response.json()["delivery_ids"]) == 1
            items = (await client.get("/api/notices?status=active")).json()["items"]
            assert len(items) == 1
            key = items[0]["key"]
        await close_db()
        broken = installed / copies.RECEIPT_NAME if is_copies else codex_home / "config.toml"
        saved = broken.read_bytes()
        broken.unlink()
        async with _client(db_url, monkeypatch, [detector]) as (client, second):
            assert second is not first
            response = await client.post("/api/notices/pending", json={**body, "session_id": "s2"})
            assert response.status_code == 200
            detail = (await client.get(f"/api/notices/{key}")).json()
            expected_variant = case if case in {"modified", "missing", "unverified"} else ""
            assert detail["notice"]["status"] == "active"
            assert detail["notice"]["variant"] == expected_variant
            assert len(detail["deliveries"]) == 2
            broken.write_bytes(saved)
            if is_copies:
                (installed / "hook.py").write_bytes((source / "hook.py").read_bytes())
            else:
                handler.pop("futureField", None)
                write_json(codex_home / "hooks.json", {"hooks": {"SessionStart": [group]}})
                _save_trust(codex_home, group, handler)
            # A third HTTP request must clear through the real detector + catalog + ledger.
            await client.post("/api/notices/pending", json={**body, "session_id": "s3"})
        await close_db()
        async with _client(db_url, monkeypatch, [detector]) as (client, _):
            detail = (await client.get(f"/api/notices/{key}")).json()
            assert detail["notice"]["status"] == "cleared"
            assert len(detail["deliveries"]) == 2
    finally:
        await close_db()
