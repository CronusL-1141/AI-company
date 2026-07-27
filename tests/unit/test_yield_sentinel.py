"""AI Team OS — plugin backup-chain yield sentinel tests.

_yield_if_superseded() is prepended (byte-identically) to every business hook so
that, when AI Team OS is installed both as a marketplace plugin and via the source
installer, only one chain speaks. The correctness core (2026-07-22 design): the
*plugin-mode* copy yields, but the *runtime main-chain* copy and any direct/repo/
test run must NEVER yield — otherwise a covered event silently drops.

We exercise the sentinel end-to-end through the real send_event.py: when it runs it
emits "[aiteam-hook]" on stderr (API unreachable); when it yields it exits 0 with no
such marker. AITEAM_API_URL points at a dead port so the "ran" signal is deterministic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_HOOKS = REPO_ROOT / "plugin" / "hooks"
SRC_HOOKS = REPO_ROOT / "src" / "aiteam" / "hooks"

BUSINESS_HOOKS = [
    "send_event", "workflow_reminder", "session_bootstrap",
    "inject_subagent_context", "deep_review_link",
    "meeting_ecosystem_writeback", "cc_task_bridge",
    "context_tracker", "permission_denied_recovery",
    "pre_compact_save",
]

DEAD_API = "http://127.0.0.1:9"  # discard port — POST fails fast → deterministic "ran"


def _settings_registering(*script_names: str) -> dict:
    """A settings.json dict whose SessionStart registers the given ai-team-os scripts."""
    home = Path.home()  # patched per-test via HOME env in the subprocess, not here
    hooks = [
        {
            "type": "command",
            "command": f'"/usr/bin/python3" "{home}/.claude/hooks/ai-team-os/{n}" SessionStart',
        }
        for n in script_names
    ]
    return {"hooks": {"SessionStart": [{"hooks": hooks}]}}


def _run_hook(hook_path: Path, home: Path, plugin_root: Path | None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "AITEAM_API_URL": DEAD_API,
    }
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run(
        [sys.executable, str(hook_path), "SessionStart"],
        input='{"session_id":"t"}',
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


def _ran(result: subprocess.CompletedProcess) -> bool:
    return "[aiteam-hook]" in result.stderr


def _yielded(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == 0 and "[aiteam-hook]" not in result.stderr


@pytest.fixture()
def env_dirs(tmp_path):
    """Build a fake HOME with a runtime main-chain dir and a fake plugin dir.

    Both get a real copy of send_event.py so the sentinel resolves __file__ under
    the right root. settings.json is written per-test by writing into home.
    """
    home = tmp_path / "home"
    runtime = home / ".claude" / "hooks" / "ai-team-os"
    runtime.mkdir(parents=True)
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)

    src = PLUGIN_HOOKS / "send_event.py"
    shutil.copy2(src, runtime / "send_event.py")
    shutil.copy2(src, plugin / "hooks" / "send_event.py")

    def write_settings(cfg: dict) -> None:
        # Rewrite the HOME token in commands to the fake home so the marker matches.
        text = json.dumps(cfg).replace(str(Path.home()), str(home))
        (home / ".claude" / "settings.json").write_text(text, encoding="utf-8")

    return {
        "home": home,
        "runtime_hook": runtime / "send_event.py",
        "plugin_root": plugin,
        "plugin_hook": plugin / "hooks" / "send_event.py",
        "write_settings": write_settings,
    }


# ---------------------------------------------------------------------------
# runtime main-chain copy — must NEVER yield
# ---------------------------------------------------------------------------

class TestRuntimeCopyNeverYields:
    def test_runtime_copy_runs_without_plugin_env(self, env_dirs):
        env_dirs["write_settings"](_settings_registering("send_event.py"))
        r = _run_hook(env_dirs["runtime_hook"], env_dirs["home"], plugin_root=None)
        assert _ran(r), f"runtime copy yielded unexpectedly: {r.stderr!r}"

    def test_runtime_copy_runs_even_if_plugin_env_leaks(self, env_dirs):
        """Even if CLAUDE_PLUGIN_ROOT is set, the runtime file is not under it → runs."""
        env_dirs["write_settings"](_settings_registering("send_event.py"))
        r = _run_hook(env_dirs["runtime_hook"], env_dirs["home"],
                      plugin_root=env_dirs["plugin_root"])
        assert _ran(r), f"runtime copy yielded with leaked plugin env: {r.stderr!r}"


# ---------------------------------------------------------------------------
# plugin-mode copy — yields only when the main chain covers this hook
# ---------------------------------------------------------------------------

class TestPluginCopyYield:
    def test_yields_when_main_chain_covers_it(self, env_dirs):
        env_dirs["write_settings"](_settings_registering("send_event.py"))
        r = _run_hook(env_dirs["plugin_hook"], env_dirs["home"],
                      plugin_root=env_dirs["plugin_root"])
        assert _yielded(r), f"plugin copy did not yield: rc={r.returncode} err={r.stderr!r}"

    def test_runs_when_no_main_chain(self, env_dirs):
        env_dirs["write_settings"]({"hooks": {}})
        r = _run_hook(env_dirs["plugin_hook"], env_dirs["home"],
                      plugin_root=env_dirs["plugin_root"])
        assert _ran(r), f"plugin copy yielded without a main chain: {r.stderr!r}"

    def test_runs_when_settings_absent(self, env_dirs):
        # no settings.json at all
        r = _run_hook(env_dirs["plugin_hook"], env_dirs["home"],
                      plugin_root=env_dirs["plugin_root"])
        assert _ran(r), f"plugin copy yielded with no settings.json: {r.stderr!r}"

    def test_runs_when_main_chain_covers_other_hook_only(self, env_dirs):
        """No coverage gap: main chain registers a *different* script → plugin still runs."""
        env_dirs["write_settings"](_settings_registering("workflow_reminder.py"))
        r = _run_hook(env_dirs["plugin_hook"], env_dirs["home"],
                      plugin_root=env_dirs["plugin_root"])
        assert _ran(r), f"plugin copy wrongly yielded for uncovered hook: {r.stderr!r}"

    def test_runs_when_file_not_under_plugin_root(self, env_dirs, tmp_path):
        """CLAUDE_PLUGIN_ROOT set but the hook file lives elsewhere → not the plugin copy."""
        env_dirs["write_settings"](_settings_registering("send_event.py"))
        unrelated = tmp_path / "elsewhere"
        r = _run_hook(env_dirs["plugin_hook"], env_dirs["home"], plugin_root=unrelated)
        assert _ran(r), f"plugin copy yielded despite file outside plugin root: {r.stderr!r}"


# ---------------------------------------------------------------------------
# static guards — sentinel is present, identical, and absent from auto_install
# ---------------------------------------------------------------------------

class TestSentinelStaticGuards:
    def _extract(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        start = text.index("def _yield_if_superseded")
        end = text.index("raise SystemExit(0)", start) + len("raise SystemExit(0)")
        return text[start:end]

    def test_every_business_hook_has_sentinel(self):
        for name in BUSINESS_HOOKS:
            assert "_yield_if_superseded" in (PLUGIN_HOOKS / f"{name}.py").read_text("utf-8")

    def test_sentinel_identical_across_hooks(self):
        bodies = {self._extract(PLUGIN_HOOKS / f"{name}.py") for name in BUSINESS_HOOKS}
        assert len(bodies) == 1, "sentinel body drifted between hooks"

    def test_plugin_and_src_twins_identical(self):
        for name in BUSINESS_HOOKS:
            p = (PLUGIN_HOOKS / f"{name}.py").read_bytes()
            s = (SRC_HOOKS / f"{name}.py").read_bytes()
            assert p == s, f"{name}: plugin/src twin drift (I1)"

    def test_auto_install_has_no_sentinel(self):
        """auto_install is the self-heal entry — it must never yield."""
        assert "_yield_if_superseded" not in (PLUGIN_HOOKS / "auto_install.py").read_text("utf-8")

    def test_call_is_first_statement_in_main(self):
        for name in BUSINESS_HOOKS:
            text = (PLUGIN_HOOKS / f"{name}.py").read_text("utf-8")
            guard = text.index('if __name__ == "__main__":')
            after = text[guard:]
            first_stmt = after.split("\n")[1].strip()
            assert first_stmt == "_yield_if_superseded()", f"{name}: {first_stmt!r}"
