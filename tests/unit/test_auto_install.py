"""AI Team OS — auto_install version self-heal + main-chain convergence tests.

auto_install.py (plugin-only, no src twin) turned "import succeeds → short-circuit"
into a version comparison, and now converges every entry point onto one absolute-path
runtime chain in ~/.claude/settings.json (the '单运行时' core, also the Windows
self-heal). These tests pin: version compare, _sync_main_chain correctness +
idempotency (including against install.py's register_hooks), and the main() decision
flow (silent when current+registered, card + sync when behind).

_self_heal_interpreter rewrites CLAUDE_PLUGIN_ROOT/hooks/hooks.json, so main() tests
point CLAUDE_PLUGIN_ROOT at a throwaway fake plugin — never the real repo.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOKS_JSON = REPO_ROOT / "plugin" / "hooks" / "hooks.json"


def _auto_install_command() -> str:
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    for group in manifest["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            if "auto_install.py" in hook["command"]:
                return hook["command"]
    raise AssertionError("auto_install SessionStart entry not found")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ai():
    return _load(REPO_ROOT / "plugin" / "hooks" / "auto_install.py", "auto_install_under_test")


@pytest.fixture()
def install_mod():
    return _load(REPO_ROOT / "install.py", "install_for_auto_test")


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _make_fake_plugin(tmp_path: Path, version: str) -> Path:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "hooks").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "ai-team-os", "version": version}), encoding="utf-8"
    )
    hooks_json = {
        "hooks": {
            "SessionStart": [
                {"hooks": [
                    {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/auto_install.py"', "timeout": 30000},  # noqa: E501
                    {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/session_bootstrap.py"', "timeout": 5000},  # noqa: E501
                    {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/send_event.py" SessionStart', "timeout": 2000},  # noqa: E501
                ]},
            ],
            "PreToolUse": [
                {"matcher": "Agent|Bash|Edit|Write", "hooks": [
                    {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/send_event.py" PreToolUse', "timeout": 2000},  # noqa: E501
                ]},
            ],
            "TaskCreated": [
                {"hooks": [
                    {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/send_event.py" TaskCreated', "timeout": 5000},  # noqa: E501
                ]},
            ],
        }
    }
    (plugin / "hooks" / "hooks.json").write_text(json.dumps(hooks_json), encoding="utf-8")
    for name in ("auto_install.py", "session_bootstrap.py", "send_event.py"):
        (plugin / "hooks" / name).write_text("# dummy\n", encoding="utf-8")
    return plugin


def _all_commands(settings_path: Path) -> list[str]:
    cfg = json.loads(settings_path.read_text(encoding="utf-8"))
    cmds = []
    for groups in cfg.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmds.append(hook["command"])
    return cmds


# ---------------------------------------------------------------------------
# version compare
# ---------------------------------------------------------------------------

class TestVersionCompare:
    def test_behind_detected(self, ai):
        assert ai._version_tuple("1.10.2") > ai._version_tuple("1.2.0")

    def test_equal(self, ai):
        assert ai._version_tuple("1.10.2") == ai._version_tuple("1.10.2")

    def test_patch_ordering(self, ai):
        assert ai._version_tuple("1.10.10") > ai._version_tuple("1.10.9")

    def test_suffix_degrades(self, ai):
        assert ai._version_tuple("2.0.0rc1") == ai._version_tuple("2.0.0")


# ---------------------------------------------------------------------------
# _sync_main_chain
# ---------------------------------------------------------------------------

class TestSyncMainChain:
    def test_registers_absolute_paths_and_excludes_auto_install(self, ai, fake_home, tmp_path, monkeypatch):
        plugin = _make_fake_plugin(tmp_path, "1.10.2")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))

        added = ai._sync_main_chain()
        assert added > 0

        settings = fake_home / ".claude" / "settings.json"
        cmds = _all_commands(settings)
        # auto_install is the self-heal entry — never in the installed chain.
        assert not any("auto_install.py" in c for c in cmds)
        # every registered command points at the runtime dir with an absolute interpreter.
        runtime = str(fake_home / ".claude" / "hooks" / "ai-team-os").replace("\\", "/")
        assert all(runtime in c for c in cmds)
        assert not any("${CLAUDE_PLUGIN_ROOT}" in c for c in cmds)
        assert not any(c.startswith("python3 ") for c in cmds)
        # scripts were copied to the runtime dir
        assert (fake_home / ".claude" / "hooks" / "ai-team-os" / "send_event.py").exists()
        assert not (fake_home / ".claude" / "hooks" / "ai-team-os" / "auto_install.py").exists()

    def test_covers_events_beyond_core(self, ai, fake_home, tmp_path, monkeypatch):
        plugin = _make_fake_plugin(tmp_path, "1.10.2")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
        ai._sync_main_chain()
        cmds = _all_commands(fake_home / ".claude" / "settings.json")
        assert any(c.endswith('send_event.py" TaskCreated') for c in cmds), "TaskCreated hook not registered"

    def test_idempotent(self, ai, fake_home, tmp_path, monkeypatch):
        plugin = _make_fake_plugin(tmp_path, "1.10.2")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
        ai._sync_main_chain()
        first = _all_commands(fake_home / ".claude" / "settings.json")
        added_again = ai._sync_main_chain()
        second = _all_commands(fake_home / ".claude" / "settings.json")
        assert added_again == 0
        assert sorted(first) == sorted(second)
        assert len(second) == len(set(second)), "duplicate commands after re-sync"

    def test_no_plugin_root_is_noop(self, ai, fake_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert ai._sync_main_chain() == 0


# ---------------------------------------------------------------------------
# cross-idempotency with install.py register_hooks (both write the same chain)
# ---------------------------------------------------------------------------

class TestCrossIdempotencyWithInstaller:
    def _no_dupes(self, settings_path: Path):
        cmds = _all_commands(settings_path)
        assert len(cmds) == len(set(cmds)), f"duplicate commands: {cmds}"

    def test_installer_then_auto(self, ai, install_mod, fake_home, monkeypatch):
        # install.py reads project_root/plugin/hooks; auto reads CLAUDE_PLUGIN_ROOT.
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT / "plugin"))
        install_mod.register_hooks(REPO_ROOT)
        ai._sync_main_chain()
        settings = fake_home / ".claude" / "settings.json"
        self._no_dupes(settings)
        # send_event PreToolUse must appear exactly once despite differing matchers.
        cmds = _all_commands(settings)
        pre = [c for c in cmds if "send_event.py" in c and c.rstrip().endswith("PreToolUse")]
        assert len(pre) == 1, pre

    def test_auto_then_installer(self, ai, install_mod, fake_home, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT / "plugin"))
        ai._sync_main_chain()
        install_mod.register_hooks(REPO_ROOT)
        settings = fake_home / ".claude" / "settings.json"
        self._no_dupes(settings)
        cmds = _all_commands(settings)
        pre = [c for c in cmds if "send_event.py" in c and c.rstrip().endswith("PreToolUse")]
        assert len(pre) == 1, pre


# ---------------------------------------------------------------------------
# retirements reach plugin users: the self-heal sync drops what it no longer ships
# ---------------------------------------------------------------------------

# The three registrations the 2026-09-23 manifest dropped, in the shape the
# previous plugin manifest shipped them.
_PREVIOUS_RELEASE_ENTRIES = {
    "PostToolUse": [
        {"matcher": "Agent|Bash|Edit|Write|Workflow", "hooks": [
            {"type": "command", "timeout": 5,
             "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/workflow_reminder.py" PostToolUse'},
        ]},
        {"matcher": "mcp__ai-team-os__meeting_conclude", "hooks": [
            {"type": "command", "timeout": 5,
             "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/meeting_ecosystem_writeback.py"'},
        ]},
    ],
    "TaskCompleted": [
        {"hooks": [
            {"type": "command", "timeout": 5,
             "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cc_task_bridge.py"'},
        ]},
    ],
}
_RETIRED_ON_0923 = ("cc_task_bridge.py", "meeting_ecosystem_writeback.py")


def _previous_release_plugin(tmp_path: Path) -> Path:
    """The real plugin tree as the previous release shipped it: today's hooks plus
    the three registrations and two scripts retired on 2026-09-23."""
    import shutil

    plugin = tmp_path / "previous-plugin"
    shutil.copytree(REPO_ROOT / "plugin" / "hooks", plugin / "hooks",
                    ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    for event, groups in _PREVIOUS_RELEASE_ENTRIES.items():
        manifest["hooks"].setdefault(event, []).extend(json.loads(json.dumps(groups)))
    (plugin / "hooks" / "hooks.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name in _RETIRED_ON_0923:
        (plugin / "hooks" / name).write_text("# previous release\n", encoding="utf-8")
    return plugin


class TestRetirementsReachPluginUsers:
    FOREIGN_USER_HOOK = "/usr/local/bin/my-own-hook --flag"

    def _seed_previous_release(self, ai, fake_home, tmp_path, monkeypatch) -> tuple[Path, str]:
        """Run the self-heal as the previous release did: additive, no retirement list."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_previous_release_plugin(tmp_path)))
        with monkeypatch.context() as m:
            m.setattr(ai, "RETIRED_HOOK_SCRIPTS", (), raising=False)
            assert ai._sync_main_chain() > 0
        settings_path = fake_home / ".claude" / "settings.json"
        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        runtime = fake_home / ".claude" / "hooks" / "ai-team-os"
        # A hook the project installs into the runtime dir from elsewhere, and one the
        # user added by hand into a group we also populate: neither is ours to drop.
        foreign_runtime = f'"/py" "{str(runtime).replace(chr(92), "/")}/channel_listen.py" Stop'
        cfg["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": foreign_runtime}]})
        wr_group = next(g for g in cfg["hooks"]["PostToolUse"]
                        if g.get("matcher") == "Agent|Bash|Edit|Write|Workflow")
        wr_group["hooks"].append({"type": "command", "command": self.FOREIGN_USER_HOOK})
        settings_path.write_text(json.dumps(cfg), encoding="utf-8")
        cmds = _all_commands(settings_path)
        for name in _RETIRED_ON_0923:
            assert any(name in c for c in cmds), f"seed must register {name}"
            assert (runtime / name).exists(), f"seed must copy {name}"
        return runtime, foreign_runtime

    def _assert_healed(self, fake_home, runtime: Path, foreign_runtime: str) -> None:
        settings_path = fake_home / ".claude" / "settings.json"
        hooks = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]
        cmds = _all_commands(settings_path)
        for name in _RETIRED_ON_0923:
            assert not any(name in c for c in cmds), f"{name} still registered"
            assert not (runtime / name).exists(), f"{name} copy left in the runtime dir"
        assert not any("workflow_reminder.py" in c and c.endswith("PostToolUse") for c in cmds)
        assert sum(c.endswith('workflow_reminder.py" PreToolUse') for c in cmds) == 1
        post_matchers = {g.get("matcher", "") for g in hooks["PostToolUse"]}
        assert "mcp__ai-team-os__meeting_conclude" not in post_matchers, "empty group left behind"
        # Foreign hooks survive in place.
        assert self.FOREIGN_USER_HOOK in cmds
        assert foreign_runtime in cmds
        # Today's chain is complete and registered once.
        assert len(cmds) == len(set(cmds)), "duplicate commands after heal"
        completed = [h["command"] for g in hooks["TaskCompleted"] for h in g["hooks"]]
        assert len(completed) == 1 and completed[0].endswith('send_event.py" TaskCompleted')

    def test_previous_release_then_current_self_heal(self, ai, fake_home, tmp_path, monkeypatch):
        runtime, foreign_runtime = self._seed_previous_release(ai, fake_home, tmp_path, monkeypatch)

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT / "plugin"))
        assert ai._sync_main_chain() == 0, "nothing new to add, only retirements to drop"
        self._assert_healed(fake_home, runtime, foreign_runtime)

        # A further run is a byte-level no-op.
        settings_path = fake_home / ".claude" / "settings.json"
        before = settings_path.read_bytes()
        assert ai._sync_main_chain() == 0
        assert settings_path.read_bytes() == before

    def test_plugin_upgrade_session_start_heals(self, ai, fake_home, tmp_path, monkeypatch, capsys):
        """End to end through main(): a plugin upgrade is what triggers the sync."""
        runtime, foreign_runtime = self._seed_previous_release(ai, fake_home, tmp_path, monkeypatch)

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT / "plugin"))
        monkeypatch.setattr(ai, "_self_heal_interpreter", lambda: None)  # never rewrite the repo
        monkeypatch.setattr(ai, "_plugin_version", lambda: "9.9.9")
        monkeypatch.setattr(ai, "_installed_version", lambda: "1.0.0")
        monkeypatch.setattr(ai, "_pip_install", lambda upgrade: (True, None))
        ai.main()
        assert "已升级" in capsys.readouterr().out
        self._assert_healed(fake_home, runtime, foreign_runtime)

    def test_matches_source_install_result(self, ai, install_mod, fake_home, tmp_path, monkeypatch):
        """Plugin self-heal and source update leave the same set of hook commands."""
        self._seed_previous_release(ai, fake_home, tmp_path, monkeypatch)
        settings_path = fake_home / ".claude" / "settings.json"
        seeded = settings_path.read_text(encoding="utf-8")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT / "plugin"))
        ai._sync_main_chain()
        via_plugin = sorted(_all_commands(settings_path))

        settings_path.write_text(seeded, encoding="utf-8")
        install_mod.register_hooks(REPO_ROOT)
        via_source = sorted(_all_commands(settings_path))
        assert via_plugin == via_source

    def test_retired_list_mirrors_installer(self, ai, install_mod):
        assert set(ai.RETIRED_HOOK_SCRIPTS) == set(install_mod.RETIRED_HOOK_SCRIPTS)


# ---------------------------------------------------------------------------
# main() decision flow
# ---------------------------------------------------------------------------

class TestMainFlow:
    def test_silent_when_current_and_registered(self, ai, fake_home, tmp_path, monkeypatch, capsys):
        plugin = _make_fake_plugin(tmp_path, "1.10.2")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
        monkeypatch.setattr(ai, "_installed_version", lambda: "1.10.2")
        # pre-register a main chain so _main_chain_registered() is True
        (fake_home / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": '"/py" "/home/.claude/hooks/ai-team-os/send_event.py" SessionStart'}
            ]}]}}), encoding="utf-8"
        )
        # pip must never be called on the silent path
        monkeypatch.setattr(ai, "_pip_install", lambda upgrade: pytest.fail("pip called on silent path"))
        ai.main()
        assert capsys.readouterr().out == "", "expected zero output when fully ready"

    def test_behind_upgrades_and_emits_card(self, ai, fake_home, tmp_path, monkeypatch, capsys):
        plugin = _make_fake_plugin(tmp_path, "9.9.9")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
        monkeypatch.setattr(ai, "_installed_version", lambda: "1.2.0")
        calls = {}

        def fake_pip(upgrade):
            calls["upgrade"] = upgrade
            return True, None

        monkeypatch.setattr(ai, "_pip_install", fake_pip)
        ai.main()
        out = capsys.readouterr().out
        assert calls.get("upgrade") is True, "should upgrade (not fresh) when behind"
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "已升级" in ctx
        assert "重启" in ctx
        # main chain got registered
        assert ai._main_chain_registered() is True

    def test_fresh_install_when_not_importable(self, ai, fake_home, tmp_path, monkeypatch, capsys):
        plugin = _make_fake_plugin(tmp_path, "1.10.2")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
        monkeypatch.setattr(ai, "_installed_version", lambda: None)
        seen = {}

        def fake_pip(upgrade):
            seen["upgrade"] = upgrade
            return True, None

        monkeypatch.setattr(ai, "_pip_install", fake_pip)
        ai.main()
        out = capsys.readouterr().out
        assert seen.get("upgrade") is False, "fresh install must not pass --upgrade"
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "已安装" in ctx

    def test_pip_failure_is_non_blocking_with_clear_hint(self, ai, fake_home, tmp_path, monkeypatch, capsys):
        plugin = _make_fake_plugin(tmp_path, "9.9.9")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
        monkeypatch.setattr(ai, "_installed_version", lambda: "1.2.0")
        monkeypatch.setattr(ai, "_pip_install", lambda upgrade: (False, "network down"))
        ai.main()  # must not raise
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "失败" in ctx
        assert "pip install" in ctx  # actionable retry hint


# ---------------------------------------------------------------------------
# Windows-compat: the hooks.json auto_install launcher (item 5)
# ---------------------------------------------------------------------------

class TestWindowsLauncher:
    def test_launcher_is_cross_platform(self):
        cmd = _auto_install_command()
        # OS-branched: Windows (Git Bash) → py -3, everything else → python3.
        assert "uname" in cmd
        assert "py -3" in cmd
        assert "python3" in cmd
        assert "MINGW" in cmd

    def test_launcher_not_rewritten_by_self_heal(self, ai):
        """_self_heal_interpreter only rewrites commands starting with python3/python;
        the launcher must be left intact so the OS branch survives."""
        cmd = _auto_install_command()
        assert not cmd.startswith("python3 ")
        assert not cmd.startswith("python ")

    def test_auto_install_timeout_bumped(self):
        manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        for group in manifest["hooks"]["SessionStart"]:
            for hook in group["hooks"]:
                if "auto_install.py" in hook["command"]:
                    # timeout 单位是**秒**（CC 官方文档："Seconds before canceling"，
                    # command 类默认 600）。2026-07-27 修正：此前全仓按毫秒思维写
                    # 3000/5000/300000，等于把超时保护关掉（3000 秒 = 50 分钟）。
                    # auto_install 要跑 git+pip 安装，给 120-600 秒区间。
                    assert 120 <= hook["timeout"] <= 600
                    return

    @pytest.mark.skipif(sys.platform == "win32", reason="uses sh + fake interpreters")
    def test_launcher_branch_selection(self, tmp_path):
        """macOS/Linux pick python3; a faked MINGW uname picks py -3 (then python)."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        plugin = tmp_path / "plugin"
        (plugin / "hooks").mkdir(parents=True)
        (plugin / "hooks" / "auto_install.py").write_text("# dummy", encoding="utf-8")

        def _fake(name: str):
            p = bindir / name
            p.write_text(f'#!/bin/sh\necho "RAN:{name}"\n', encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        for name in ("py", "python3", "python"):
            _fake(name)

        cmd = _auto_install_command().replace("${CLAUDE_PLUGIN_ROOT}", str(plugin))
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

        # Real uname (Darwin/Linux) → python3
        out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env).stdout
        assert "RAN:python3" in out

        # Faked Windows uname → py -3
        (bindir / "uname").write_text('#!/bin/sh\necho MINGW64_NT-10.0\n', encoding="utf-8")
        (bindir / "uname").chmod(0o755)
        out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env).stdout
        assert "RAN:py" in out

        # Windows without py → falls back to python
        (bindir / "py").unlink()
        out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env).stdout
        assert "RAN:python" in out
