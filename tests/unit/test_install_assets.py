"""AI Team OS — installer asset-distribution tests.

Covers the skills/commands/agents distribution added so the independent
(source) install path ships the same assets as the marketplace plugin.
Regression guard for the 2026-07-22 gap: uninstalling the plugin dropped the
/os-* skills and commands entirely because install.py never distributed them,
and agents came from the 22-file .claude/agents instead of the 25-file
plugin/agents superset.

Home is faked by monkeypatching pathlib.Path.home so the installer/uninstaller
functions write into tmp_path instead of the real ~/.claude.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_SKILLS = REPO_ROOT / "plugin" / "skills"
PLUGIN_COMMANDS = REPO_ROOT / "plugin" / "commands"
PLUGIN_AGENTS = REPO_ROOT / "plugin" / "agents"
PLUGIN_LOOP_MD = REPO_ROOT / "plugin" / "loop.md"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def install_mod():
    return _load(REPO_ROOT / "install.py", "install_under_test")


@pytest.fixture()
def uninstall_mod():
    return _load(REPO_ROOT / "scripts" / "uninstall.py", "uninstall_under_test")


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _skill_names() -> list[str]:
    return sorted(p.name for p in PLUGIN_SKILLS.iterdir() if p.is_dir())


def _command_names() -> list[str]:
    return sorted(p.name for p in PLUGIN_COMMANDS.glob("*.md"))


def _agent_names() -> list[str]:
    return sorted(p.name for p in PLUGIN_AGENTS.glob("*.md"))


# ---------------------------------------------------------------------------
# copy_skills
# ---------------------------------------------------------------------------

class TestCopySkills:
    def test_copies_every_skill_dir(self, install_mod, fake_home):
        install_mod.copy_skills(REPO_ROOT)
        dst = fake_home / ".claude" / "skills"
        for name in _skill_names():
            assert (dst / name / "SKILL.md").exists(), f"{name}/SKILL.md missing"

    def test_copies_nested_resources(self, install_mod, fake_home):
        """Nested resources (meeting-facilitate/templates/*) must survive."""
        install_mod.copy_skills(REPO_ROOT)
        dst = fake_home / ".claude" / "skills"
        templates = dst / "meeting-facilitate" / "templates"
        assert templates.is_dir()
        assert any(templates.glob("*.md")), "meeting-facilitate templates not copied"

    def test_fresh_install_skips_existing(self, install_mod, fake_home):
        dst = fake_home / ".claude" / "skills" / "os-workflow"
        dst.mkdir(parents=True)
        sentinel = dst / "SKILL.md"
        sentinel.write_text("USER CUSTOM", encoding="utf-8")

        install_mod.copy_skills(REPO_ROOT, overwrite=False)

        assert sentinel.read_text(encoding="utf-8") == "USER CUSTOM"

    def test_update_overwrites(self, install_mod, fake_home):
        dst = fake_home / ".claude" / "skills" / "os-workflow"
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text("STALE", encoding="utf-8")

        install_mod.copy_skills(REPO_ROOT, overwrite=True)

        assert (dst / "SKILL.md").read_text(encoding="utf-8") != "STALE"


# ---------------------------------------------------------------------------
# copy_commands
# ---------------------------------------------------------------------------

class TestCopyCommands:
    def test_copies_every_command(self, install_mod, fake_home):
        install_mod.copy_commands(REPO_ROOT)
        dst = fake_home / ".claude" / "commands"
        for name in _command_names():
            assert (dst / name).exists(), f"command {name} missing"

    def test_fresh_install_skips_existing(self, install_mod, fake_home):
        dst = fake_home / ".claude" / "commands"
        dst.mkdir(parents=True)
        (dst / "os-status.md").write_text("USER CUSTOM", encoding="utf-8")

        install_mod.copy_commands(REPO_ROOT, overwrite=False)

        assert (dst / "os-status.md").read_text(encoding="utf-8") == "USER CUSTOM"

    def test_update_overwrites(self, install_mod, fake_home):
        dst = fake_home / ".claude" / "commands"
        dst.mkdir(parents=True)
        (dst / "os-status.md").write_text("STALE", encoding="utf-8")

        install_mod.copy_commands(REPO_ROOT, overwrite=True)

        assert (dst / "os-status.md").read_text(encoding="utf-8") != "STALE"


# ---------------------------------------------------------------------------
# copy_agent_templates — must use plugin/agents (25 superset)
# ---------------------------------------------------------------------------

class TestCopyAgentTemplates:
    def test_uses_plugin_agents_superset(self, install_mod, fake_home):
        install_mod.copy_agent_templates(REPO_ROOT)
        dst = fake_home / ".claude" / "agents"
        installed = sorted(p.name for p in dst.glob("*.md"))
        assert installed == _agent_names()

    def test_includes_the_three_plugin_only_agents(self, install_mod, fake_home):
        install_mod.copy_agent_templates(REPO_ROOT)
        dst = fake_home / ".claude" / "agents"
        for name in ("debate-advocate.md", "debate-critic.md", "team-member.md"):
            assert (dst / name).exists(), f"{name} (plugin-only) not installed"


# ---------------------------------------------------------------------------
# verify_installation — skills/commands checks
# ---------------------------------------------------------------------------

class TestVerifyInstallation:
    def test_verify_passes_after_full_asset_copy(self, install_mod, fake_home, capsys):
        install_mod.copy_agent_templates(REPO_ROOT)
        install_mod.copy_skills(REPO_ROOT)
        install_mod.copy_commands(REPO_ROOT)

        install_mod.verify_installation(REPO_ROOT)
        out = capsys.readouterr().out
        # Skills / commands lines are reported and marked OK when all present.
        assert "~/.claude/skills/" in out
        assert "~/.claude/commands/" in out
        assert "[FAIL] ~/.claude/skills/" not in out
        assert "[FAIL] ~/.claude/commands/" not in out

    def test_verify_flags_missing_skills(self, install_mod, fake_home, capsys):
        # Copy commands but NOT skills → skills check must not be OK.
        install_mod.copy_commands(REPO_ROOT)
        install_mod.verify_installation(REPO_ROOT)
        out = capsys.readouterr().out
        assert "[OK] ~/.claude/skills/" not in out

    def test_verify_assets_helper_counts(self, install_mod, fake_home):
        install_mod.copy_skills(REPO_ROOT)
        ok, detail = install_mod._verify_assets_present(
            PLUGIN_SKILLS,
            fake_home / ".claude" / "skills",
            lambda p: [d.name for d in p.iterdir() if d.is_dir()],
            lambda dst, name: (dst / name / "SKILL.md").exists(),
        )
        assert ok is True
        assert detail == f"{len(_skill_names())}/{len(_skill_names())} present"


# ---------------------------------------------------------------------------
# uninstall — removal + drift guard
# ---------------------------------------------------------------------------

class TestUninstall:
    def test_remove_skills(self, uninstall_mod, fake_home):
        skills = fake_home / ".claude" / "skills"
        for name in uninstall_mod.SKILL_NAMES:
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text("x", encoding="utf-8")
        # An unrelated third-party skill must be preserved.
        (skills / "lark-doc").mkdir(parents=True)

        uninstall_mod.remove_skills(dry_run=False)

        for name in uninstall_mod.SKILL_NAMES:
            assert not (skills / name).exists()
        assert (skills / "lark-doc").exists(), "unrelated skill wrongly removed"

    def test_remove_commands(self, uninstall_mod, fake_home):
        commands = fake_home / ".claude" / "commands"
        commands.mkdir(parents=True)
        for name in uninstall_mod.COMMAND_FILES:
            (commands / name).write_text("x", encoding="utf-8")
        (commands / "user-custom.md").write_text("keep", encoding="utf-8")

        uninstall_mod.remove_commands(dry_run=False)

        for name in uninstall_mod.COMMAND_FILES:
            assert not (commands / name).exists()
        assert (commands / "user-custom.md").exists()

    def test_dry_run_removes_nothing(self, uninstall_mod, fake_home):
        skills = fake_home / ".claude" / "skills"
        (skills / "os-workflow").mkdir(parents=True)
        commands = fake_home / ".claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "os-status.md").write_text("x", encoding="utf-8")

        uninstall_mod.remove_skills(dry_run=True)
        uninstall_mod.remove_commands(dry_run=True)

        assert (skills / "os-workflow").exists()
        assert (commands / "os-status.md").exists()


# ---------------------------------------------------------------------------
# drift guards — uninstaller lists must mirror the plugin source
# ---------------------------------------------------------------------------

class TestDriftGuards:
    def test_agent_list_matches_source(self, uninstall_mod):
        assert sorted(uninstall_mod.AGENT_TEMPLATES) == _agent_names()

    def test_skill_list_matches_source(self, uninstall_mod):
        assert sorted(uninstall_mod.SKILL_NAMES) == _skill_names()

    def test_command_list_matches_source(self, uninstall_mod):
        assert sorted(uninstall_mod.COMMAND_FILES) == _command_names()


# ---------------------------------------------------------------------------
# install_loop_md — lives in the root install.py (the single install path)
# ---------------------------------------------------------------------------

class TestLoopMd:
    def test_source_template_carries_sentinel(self, install_mod):
        # The overwrite/skip decision hinges on this marker existing in the template.
        assert install_mod.LOOP_TEMPLATE_SENTINEL in PLUGIN_LOOP_MD.read_text("utf-8")

    def test_installs_when_absent(self, install_mod, fake_home):
        install_mod.install_loop_md(REPO_ROOT)
        dst = fake_home / ".claude" / "loop.md"
        assert dst.exists()
        assert install_mod.LOOP_TEMPLATE_SENTINEL in dst.read_text("utf-8")

    def test_overwrites_our_template(self, install_mod, fake_home):
        dst = fake_home / ".claude" / "loop.md"
        dst.write_text(f"{install_mod.LOOP_TEMPLATE_SENTINEL}\nSTALE", encoding="utf-8")
        install_mod.install_loop_md(REPO_ROOT)
        assert "STALE" not in dst.read_text("utf-8")

    def test_preserves_user_customized(self, install_mod, fake_home):
        dst = fake_home / ".claude" / "loop.md"
        dst.write_text("MY OWN LOOP PROMPT", encoding="utf-8")  # no sentinel
        install_mod.install_loop_md(REPO_ROOT)
        assert dst.read_text("utf-8") == "MY OWN LOOP PROMPT"

    def test_uninstall_removes_our_template(self, uninstall_mod, fake_home):
        dst = fake_home / ".claude" / "loop.md"
        dst.write_text("ai-team-os-loop-template v1\nx", encoding="utf-8")
        uninstall_mod.remove_loop_md(dry_run=False)
        assert not dst.exists()

    def test_uninstall_preserves_user_customized(self, uninstall_mod, fake_home):
        dst = fake_home / ".claude" / "loop.md"
        dst.write_text("MY OWN LOOP PROMPT", encoding="utf-8")
        uninstall_mod.remove_loop_md(dry_run=False)
        assert dst.exists()


# ---------------------------------------------------------------------------
# register_hooks — rebuilds our chain, never touches anybody else's
# ---------------------------------------------------------------------------

def _settings(home: Path) -> dict:
    import json
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _commands(home: Path) -> list[str]:
    return [
        hook.get("command", "")
        for groups in _settings(home).get("hooks", {}).values()
        for group in groups
        for hook in group.get("hooks", [])
    ]


class TestRegisterHooks:
    def test_registers_the_full_surface(self, install_mod, fake_home):
        install_mod.register_hooks(REPO_ROOT)
        registered = _settings(fake_home)["hooks"]
        expected_events = {event for event, _m, _e in install_mod.HOOK_SURFACE}
        assert set(registered) == expected_events
        # The four events the source install used to be missing entirely. The CC
        # task hook moved from TaskCreated to TaskCompleted in v1.11 (the bridge
        # became a completion-time ledger) — the point of the assertion is that
        # the source path still carries the task hook, whatever it is hung on.
        for event in ("TaskCompleted", "UserPromptSubmit", "PermissionDenied", "PreCompact"):
            assert event in registered

    def test_matchers_match_the_manifest_split(self, install_mod, fake_home):
        install_mod.register_hooks(REPO_ROOT)
        groups = _settings(fake_home)["hooks"]["PreToolUse"]
        by_matcher = {g.get("matcher", ""): g for g in groups}
        # send_event keeps full telemetry; workflow_reminder stays on its own tools.
        assert any("send_event.py" in h["command"] for h in by_matcher["*"]["hooks"])
        narrow = by_matcher["Agent|Bash|Edit|Write|Workflow"]["hooks"]
        assert any("workflow_reminder.py" in h["command"] for h in narrow)

    def test_idempotent(self, install_mod, fake_home):
        install_mod.register_hooks(REPO_ROOT)
        first = _commands(fake_home)
        install_mod.register_hooks(REPO_ROOT)
        second = _commands(fake_home)
        assert sorted(first) == sorted(second)
        assert len(second) == len(set(second)), "duplicate hook commands after re-run"

    def test_preserves_foreign_hooks(self, install_mod, fake_home):
        """Third-party hooks — including ones sitting in our runtime dir — survive.

        The runtime dir also hosts hooks from other branches of this project and
        the user's own guards; a path-substring purge would delete them silently.
        """
        import json

        runtime = fake_home / ".claude" / "hooks" / "ai-team-os"
        settings_path = fake_home / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "Bash",
            "hooks": [
                {"type": "command", "command": "/Users/x/.claude/hooks/prod-guard.sh"},
                {"type": "command", "command": f'"/py" "{runtime}/mail_reminder.py" PreToolUse'},
            ],
        }]}}), encoding="utf-8")

        install_mod.register_hooks(REPO_ROOT)

        commands = _commands(fake_home)
        assert any("prod-guard.sh" in c for c in commands)
        assert any("mail_reminder.py" in c for c in commands)

    def test_drops_stale_entries_of_our_own(self, install_mod, fake_home):
        """A previous install's wrong matcher/timeout is replaced, not duplicated."""
        import json

        runtime = fake_home / ".claude" / "hooks" / "ai-team-os"
        settings_path = fake_home / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "*",
            "hooks": [
                {"type": "command",
                 "command": f'"/py" "{runtime}/workflow_reminder.py" PreToolUse',
                 "timeout": 3},
                {"type": "command", "command": f'"/py" "{runtime}/task_completed_gate.py"'},
            ],
        }]}}), encoding="utf-8")

        install_mod.register_hooks(REPO_ROOT)

        commands = _commands(fake_home)
        assert not any("task_completed_gate.py" in c for c in commands), "retired hook survived"
        wf = [c for c in commands if "workflow_reminder.py" in c and c.endswith("PreToolUse")]
        assert len(wf) == 1
        groups = _settings(fake_home)["hooks"]["PreToolUse"]
        stale = [g for g in groups if g.get("matcher") == "*"
                 and any("workflow_reminder" in h["command"] for h in g["hooks"])]
        assert not stale, "workflow_reminder still registered under the '*' matcher"

    def test_removes_retired_runtime_copies(self, install_mod, fake_home):
        runtime = fake_home / ".claude" / "hooks" / "ai-team-os"
        runtime.mkdir(parents=True)
        stale = runtime / "task_completed_gate.py"
        stale.write_text("# retired", encoding="utf-8")

        install_mod.copy_hook_scripts(REPO_ROOT)

        assert not stale.exists()
        for name in install_mod.HOOK_SCRIPTS:
            assert (runtime / name).exists(), f"{name} not distributed"


# ---------------------------------------------------------------------------
# single install path — the retired scripts/install.py must stay buried
# ---------------------------------------------------------------------------

class TestSingleInstallPath:
    def test_scripts_installer_is_gone(self):
        """Deleted 2026-07-27 (batch 5).

        It had been a no-op redirect since 2026-07-22, but its HOOK_EVENTS table
        was still a second, silently drifting registration surface — it described
        an OS with different events and matchers than plugin/hooks/hooks.json.
        Resurrecting it re-opens exactly that drift.
        """
        assert not (REPO_ROOT / "scripts" / "install.py").exists()

    def test_updater_delegates_hook_copy_to_installer(self):
        """scripts/update.py must not keep its own hook-file list (it fell behind once)."""
        text = (REPO_ROOT / "scripts" / "update.py").read_text(encoding="utf-8")
        assert "copy_hook_scripts" in text
        assert "send_event.py" not in text, "update.py re-grew a hardcoded hook list"
