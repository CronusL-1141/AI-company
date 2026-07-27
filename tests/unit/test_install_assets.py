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
import subprocess
import sys
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
# install_loop_md — ported into root install.py (deprecated scripts/install.py path)
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
# scripts/install.py — deprecated: every invocation exits 1 with a redirect
# ---------------------------------------------------------------------------

class TestDeprecatedScriptsInstaller:
    @pytest.mark.parametrize("flag", [[], ["--check"], ["--uninstall"]])
    def test_exits_1_with_redirect(self, flag):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "install.py"), *flag],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 1
        out = proc.stdout + proc.stderr
        assert "DEPRECATED" in out or "弃用" in out
        assert "install.py" in out  # points at the surviving installer
