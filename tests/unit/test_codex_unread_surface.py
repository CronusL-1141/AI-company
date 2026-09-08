"""Append-only unread registration and declaration-level trust regression tests."""

from __future__ import annotations

import copy
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODEX_DIR = ROOT / "plugin" / "harness" / "codex"
UNREAD_SCRIPT = "channel_unread_codex.py"

# Captured before appending the unread handler, from the core baseline lock.
LEGACY_DIGESTS = (
    (
        "pre_tool_use:0:0",
        "a2f3a9eb8fb119589b021e7a249defeef4dfafee28b94b8a6a8eb5a0dde69dee",
        "8fd0b7edf6ba437b2844798684d826ade19a70969c4609e263f5450f15cf6c4e",
    ),
    (
        "pre_tool_use:1:0",
        "df20e7e43350c49240a20152ac3b8f32bbe6f8ebcac2801e351318f38a178986",
        "16646671c0f6ed943ca7cc56cf8c279f1b06fd9c535165065ef0adab5269825e",
    ),
    (
        "post_tool_use:0:0",
        "5e1e4eaa26e953de733d8acdaced48c6185db59f74e04ee76215df34476a1808",
        "4bf7d2636837f10b4bcba186dcb0ccaa852e2698920d31eb42d49819ea006419",
    ),
    (
        "post_tool_use:1:0",
        "0f19bd6153bc1c42886fd2bebae1cc73e3d60cc53a5b2f9fe0205298f56c7010",
        "c2ff1575c21e7207184f3d80e091e7202d5fd6dcc2f32bf2a793aa6c4b617659",
    ),
    (
        "post_tool_use:2:0",
        "b744fa86856e880e5ec2b05746b1945a6fba7352bf30376dba8103561740e84f",
        "e1c3babb829d523dbe9ad9956ac233fa930456da9779315c2d613466e4f08b85",
    ),
    (
        "post_tool_use:3:0",
        "683680f3c6cafcdc2411bc8f1b77d93b6664a9477acc82a963a004e8204418cb",
        "8ca787c270c7f891f264e9c995b6ce603af10f8cf466f029c29426bd3bdac6e3",
    ),
    (
        "session_start:0:0",
        "0a712ae096267c9fbbafc297d575f2573adaa66f3070d207bdc4eef151e30b1f",
        "c29deb455a9399e58bea50bf5928a8e6b8d11c26cb385f121588cf0610668f14",
    ),
    (
        "session_start:0:1",
        "48829a380372c7c5c2c5d5dc6eaaeced8f710a2ebf87257d7f1cb1ae9923830d",
        "423df814bd34cc4be8b56251f814162b5255c4d4b66d9cf3583150935cbc5ab9",
    ),
    (
        "subagent_start:0:0",
        "3f4bedd92be26948ebc21475bd6971bf162b4ecf0ddf75ba75b6eaaa60d2c5c2",
        "8cdbaffca4832a46f4fda3345caaa9a855b8969a9d74a5af6bfca7dd76ccff59",
    ),
    (
        "subagent_start:0:1",
        "7131d60a4ce00ef262fce358c1a6665d7d5462d691ac87b22f9855e79aa6eec5",
        "1c52f693d512f49d70837a80b814a2c4b98afb854fe8beef3d27a6d71b177624",
    ),
    (
        "subagent_stop:0:0",
        "fa9c5de57d712307665694c8757fb8aaaed022af3c6cb6c8245b8f39d59a7fb6",
        "287040b6f47800e458c8aa889ac2cfbce457e92f6979c12c310f22e14916b65d",
    ),
    (
        "stop:0:0",
        "c3a1a1f842c85febcc4959d510b75640e26a2209da550c3d9f9b32d5c70160f6",
        "735cd7fb076c6708a5746d6ad50045c94dcc81fb236a4d8f53166d97afd68dc6",
    ),
)


def load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"unread_test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def surface() -> ModuleType:
    return load_module(CODEX_DIR / "surface.py")


def test_legacy_registration_digests_and_order_are_unchanged(surface: ModuleType) -> None:
    entries = surface.trust_lock_entries(surface.render_manifest())
    assert len(entries) == len(LEGACY_DIGESTS) + 1
    assert entries[:-1] == [
        {
            "key": key,
            "command_sha256": f"sha256:{unix_digest}",
            "command_windows_sha256": f"sha256:{windows_digest}",
        }
        for key, unix_digest, windows_digest in LEGACY_DIGESTS
    ]
    assert entries[-1]["key"] == "user_prompt_submit:0:0"


def test_unread_handler_is_observe_only_and_appended(surface: ModuleType) -> None:
    assert surface.CODEX_HOOK_SURFACE[-1] == (
        "UserPromptSubmit", "", surface.KIND_OBSERVE,
        [(UNREAD_SCRIPT, "leader-codex", 3, 0)],
    )
    manifest = surface.render_manifest()
    assert list(manifest["hooks"])[-1] == "UserPromptSubmit"
    assert len(manifest["hooks"]["UserPromptSubmit"]) == 1
    group = manifest["hooks"]["UserPromptSubmit"][0]
    assert "matcher" not in group
    assert len(group["hooks"]) == 1
    handler = group["hooks"][0]
    assert handler["additionalContextLimit"] == 0
    assert handler["timeout"] == 3
    assert handler["type"] == "command"
    assert "async" not in handler
    for field in ("command", "command_windows"):
        assert shlex.split(handler[field]) == [
            "{{PY}}", f"{{{{CODEX_HOOKS_DIR}}}}/{UNREAD_SCRIPT}", "leader-codex",
        ]


def test_generated_files_match_renderers(surface: ModuleType) -> None:
    assert (CODEX_DIR / "hooks.json").read_text() == surface.render()
    manifest = json.loads((CODEX_DIR / "hooks.json").read_text())
    assert (CODEX_DIR / "hook-trust.lock").read_text() == surface.render_trust_lock(manifest)


def test_unread_script_is_registered_in_both_sets(surface: ModuleType) -> None:
    assert UNREAD_SCRIPT in surface.CODEX_INJECTION_SCRIPTS
    assert surface.CODEX_HOOK_SCRIPTS[-1] == UNREAD_SCRIPT
    assert surface.CODEX_HOOK_SCRIPTS.count(UNREAD_SCRIPT) == 1
    assert "channel_unread.py" not in surface.CODEX_HOOK_SCRIPTS


def test_i15_rejects_missing_injection_registration(surface: ModuleType, monkeypatch) -> None:
    checker = load_module(ROOT / "scripts" / "check_codex_hook_surface.py")
    monkeypatch.setattr(
        surface, "CODEX_INJECTION_SCRIPTS", surface.CODEX_INJECTION_SCRIPTS - {UNREAD_SCRIPT},
    )
    errors: list[str] = []
    checker.check_handlers(surface, surface.render_manifest(), errors)
    assert any(UNREAD_SCRIPT in error and "additionalContextLimit" in error for error in errors)


@pytest.mark.parametrize("invalid_limit", [None, -1, True])
def test_i15_rejects_invalid_injection_limit(surface: ModuleType, invalid_limit) -> None:
    checker = load_module(ROOT / "scripts" / "check_codex_hook_surface.py")
    manifest = surface.render_manifest()
    handler = manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    handler["additionalContextLimit"] = invalid_limit
    errors: list[str] = []
    checker.check_handlers(surface, manifest, errors)
    assert any(UNREAD_SCRIPT in error and "additionalContextLimit" in error for error in errors)


def test_i17_detects_changed_reader_without_disturbing_old_keys(surface: ModuleType) -> None:
    checker = load_module(ROOT / "scripts" / "check_codex_trust_lock.py")
    manifest = surface.render_manifest()
    changed = copy.deepcopy(manifest)
    handler = changed["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    for field in ("command", "command_windows"):
        handler[field] = handler[field].replace("leader-codex", "another-reader")
    errors = checker._diff(
        surface.trust_lock_entries(manifest), surface.trust_lock_entries(changed),
    )
    assert len(errors) == 2
    assert all(error.startswith("user_prompt_submit:0:0:") for error in errors)


def test_i20_rejects_cc_script_name_registration(surface: ModuleType) -> None:
    checker = load_module(ROOT / "scripts" / "check_codex_isolation.py")
    errors: list[str] = []
    checker.check_same_name_ban(surface, errors)
    assert not errors
    checker.check_same_name_ban(SimpleNamespace(CODEX_HOOK_SCRIPTS=("channel_unread.py",)), errors)
    assert any("channel_unread.py" in error for error in errors)


@pytest.mark.parametrize("script", [
    "check_codex_hook_surface.py", "check_codex_trust_lock.py", "check_codex_isolation.py",
])
def test_codex_registration_machine_checks_pass(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script)],
        cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
