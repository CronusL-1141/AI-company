"""E11 installed_copy_stale and E15 host_version_mismatch detectors (batch B)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.services.notices.detectors import DetectContext, NoDataError
from aiteam.services.notices.detectors import installed_copies as copies
from aiteam.services.notices.detectors.host_versions import HostVersionsDetector
from aiteam.services.notices.detectors.installed_copies import InstalledCopiesDetector
from aiteam.types import NoticeStatus

from .conftest import request, write_json

INSTALLER = '''
HOOK_SURFACE: list = [
    ("SessionStart", "", [("session_bootstrap.py", "", 15)]),
    ("PreToolUse", "Bash", [("workflow_reminder.py", "", 5), ("send_event.py", "PreToolUse", 5)]),
]
HOOK_SCRIPTS = tuple(s for _e, _m, entries in HOOK_SURFACE for s, _a, _t in entries)
HOOK_SUPPORT_MODULES: tuple[str, ...] = ("user_notice.py",)
'''


def make_source_tree(root: Path) -> Path:
    hooks = root / "plugin" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("session_bootstrap.py", "workflow_reminder.py", "send_event.py", "user_notice.py",
                 "auto_install.py", "uninstall_main_chain.py", "hook_core.py"):
        (hooks / name).write_text(f"# {name} v2\n", encoding="utf-8")
    (root / "plugin" / "skills" / "os-release").mkdir(parents=True)
    (root / "plugin" / "skills" / "os-release" / "SKILL.md").write_text("release v2\n", encoding="utf-8")
    (root / "plugin" / "commands").mkdir(parents=True)
    (root / "plugin" / "commands" / "os-doctor.md").write_text("doctor v2\n", encoding="utf-8")
    (root / "plugin" / "agents").mkdir(parents=True)
    (root / "plugin" / "agents" / "team-member.md").write_text("member v2\n", encoding="utf-8")
    (root / "install.py").write_text(INSTALLER, encoding="utf-8")
    (root / "src" / "aiteam").mkdir(parents=True)
    (root / "src" / "aiteam" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    return root


def install_copies(home: Path, root: Path) -> Path:
    """What a source install leaves behind, all current."""
    config = home / ".claude"
    hooks = config / "hooks" / "ai-team-os"
    hooks.mkdir(parents=True)
    for name in ("session_bootstrap.py", "workflow_reminder.py", "send_event.py", "user_notice.py"):
        (hooks / name).write_bytes((root / "plugin" / "hooks" / name).read_bytes())
    (hooks / "mail_reminder.py").write_text("# the user's own hook\n", encoding="utf-8")
    for relative in ("skills/os-release/SKILL.md", "commands/os-doctor.md", "agents/team-member.md"):
        target = config / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / "plugin" / relative).read_bytes())
    (config / "skills" / "mine").mkdir(parents=True)
    (config / "skills" / "mine" / "SKILL.md").write_text("user skill\n", encoding="utf-8")
    data = home / ".claude" / "data" / "ai-team-os"
    data.mkdir(parents=True, exist_ok=True)
    (data / "install_path.txt").write_text(str(root), encoding="utf-8")
    return config


@pytest.fixture()
def source_install(isolated_home, tmp_path, monkeypatch):
    root = make_source_tree(tmp_path / "checkout")
    monkeypatch.setattr(copies, "_api_source_root", lambda: root)
    copies._cache.clear()
    config = install_copies(isolated_home, root)
    return root, config


def _ctx(repo):
    return DetectContext(host="cc", event="SessionStart", source="startup", session_id="s1", cwd="",
                         project_id="", facts={}, now=utc_now(), repo=repo)


def _touch_newer(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))


async def test_current_install_reports_nothing(repo, source_install):
    assert await InstalledCopiesDetector().detect(_ctx(repo)) == []


async def test_changed_and_missing_copies_are_stale(repo, source_install):
    root, config = source_install
    changed = config / "hooks" / "ai-team-os" / "workflow_reminder.py"
    changed.write_text("# workflow_reminder.py v1\n", encoding="utf-8")
    (config / "hooks" / "ai-team-os" / "user_notice.py").unlink()
    (config / "skills" / "os-release" / "SKILL.md").write_text("release v1\n", encoding="utf-8")
    diff = copies.compare_sync()
    assert [(item.relative, item.missing) for item in diff.stale] == [
        ("hooks/ai-team-os/user_notice.py", True),
        ("hooks/ai-team-os/workflow_reminder.py", False),
        ("skills/os-release/SKILL.md", False),
    ]
    assert diff.baseline.branch == "master" and diff.baseline.version == "9.9.9"
    [hit] = await InstalledCopiesDetector().detect(_ctx(repo))
    assert hit.key == diff.key and hit.params == {"n": 3} and hit.variant == ""


async def test_not_distributed_hooks_and_user_files_never_count(repo, source_install):
    _root, config = source_install
    # hook_core.py and the plugin-only scripts are not copied by the source installer.
    assert not (config / "hooks" / "ai-team-os" / "hook_core.py").exists()
    (config / "skills" / "mine" / "SKILL.md").write_text("changed\n", encoding="utf-8")
    (config / "commands" / "os-doctor.md").unlink()  # a removed command is the user's choice
    assert copies.compare_sync().stale == ()


async def test_in_place_edit_invalidates_the_cache(repo, source_install):
    _root, config = source_install
    target = config / "hooks" / "ai-team-os" / "send_event.py"
    assert copies.compare_sync().stale == ()
    original = target.read_bytes()
    target.write_bytes(original.replace(b"v2", b"v3"))  # same size, same folder mtime
    _touch_newer(target)
    assert [item.relative for item in copies.compare_sync().stale] == ["hooks/ai-team-os/send_event.py"]


async def test_unparseable_installer_skips_missing_but_still_compares(repo, source_install):
    root, config = source_install
    (root / "install.py").write_text("HOOK_SURFACE = build()\n", encoding="utf-8")
    (config / "hooks" / "ai-team-os" / "user_notice.py").unlink()
    (config / "hooks" / "ai-team-os" / "send_event.py").write_text("old\n", encoding="utf-8")
    assert [item.relative for item in copies.compare_sync().stale] == ["hooks/ai-team-os/send_event.py"]


async def test_worktree_branch_is_read_from_the_gitdir_pointer(tmp_path):
    gitdir = tmp_path / "main" / ".git" / "worktrees" / "b"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/cc/notice-b\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert copies.git_branch(tree) == "cc/notice-b"
    (gitdir / "HEAD").write_text("0123456789abcdef\n", encoding="utf-8")
    assert copies.git_branch(tree) == "detached 01234567"


async def test_no_source_install_no_finding(repo, isolated_home, monkeypatch):
    monkeypatch.setattr(copies, "_api_source_root", lambda: None)
    assert await InstalledCopiesDetector().detect(_ctx(repo)) == []


async def test_source_install_without_baseline_is_no_data(repo, isolated_home, tmp_path, monkeypatch):
    monkeypatch.setattr(copies, "_api_source_root", lambda: None)
    write_json(isolated_home / ".claude" / "data" / "ai-team-os" / "install-state.json", {})
    (isolated_home / ".claude" / "data" / "ai-team-os" / "install_path.txt").write_text(
        str(tmp_path / "gone"), encoding="utf-8")
    with pytest.raises(NoDataError):
        await InstalledCopiesDetector().detect(_ctx(repo))


def _plugin(home: Path, version: str = "1.14.0") -> None:
    write_json(home / ".claude" / "plugins" / "installed_plugins.json", {
        "plugins": {"ai-team-os@market": [{"installPath": str(home / "p"), "version": version}]},
    })


async def test_plugin_sync_failure_variant(repo, isolated_home, monkeypatch):
    monkeypatch.setattr(copies, "_api_source_root", lambda: None)
    _plugin(isolated_home)
    state = isolated_home / ".claude" / "data" / "ai-team-os" / "install-state.json"
    write_json(state, {"phase": "installed"})
    assert await InstalledCopiesDetector().detect(_ctx(repo)) == []
    write_json(state, {"phase": "installed", "sync_failed": {"n": 2, "files": ["a.py", "b.py"]}})
    [hit] = await InstalledCopiesDetector().detect(_ctx(repo))
    assert hit.variant == "plugin_sync_failed" and hit.params == {"n": 2}
    assert hit.key.startswith("installed_copy_stale:cc:plugin:")


async def test_source_install_owns_copies_even_with_the_plugin_enabled(repo, source_install, isolated_home):
    _root, config = source_install
    _plugin(isolated_home)
    (config / "hooks" / "ai-team-os" / "send_event.py").write_text("old\n", encoding="utf-8")
    [hit] = await InstalledCopiesDetector().detect(_ctx(repo))
    assert hit.variant == "" and hit.params == {"n": 1}


async def test_stale_copies_reach_the_session_start_line_and_clear(repo, source_install):
    _root, config = source_install
    target = config / "hooks" / "ai-team-os" / "send_event.py"
    target.write_text("old\n", encoding="utf-8")
    shown = await ledger.pending(repo, request())
    assert "hook" in shown.user_text and "sync OS install" in shown.user_text
    assert 'os_config_change("sync_installed_copies")' in shown.model_text
    [row] = (await repo.list_notices(key_prefix="installed_copy_stale:"))[0]
    assert row.status == NoticeStatus.ACTIVE
    target.write_bytes((_root / "plugin" / "hooks" / "send_event.py").read_bytes())
    _touch_newer(target)
    await ledger.pending(repo, request(session="s2"))
    [row] = (await repo.list_notices(key_prefix="installed_copy_stale:"))[0]
    assert row.status == NoticeStatus.CLEARED


def _receipt(home: Path, **fields) -> None:
    write_json(home / ".codex" / "hooks" / "ai-team-os-observer" / ".aiteam-codex-install.json",
               {"schema": 1, **fields})


async def test_host_versions_needs_both_sides(repo, isolated_home, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    detector = HostVersionsDetector()
    with pytest.raises(NoDataError):
        await detector.detect(_ctx(repo))
    _plugin(isolated_home, "1.14.0")
    _receipt(isolated_home, repo_root="/x")  # today's receipt: no version field
    with pytest.raises(NoDataError):
        await detector.detect(_ctx(repo))
    _receipt(isolated_home, aiteam_version="1.14.0")
    assert await detector.detect(_ctx(repo)) == []
    _receipt(isolated_home, aiteam_version="1.13.1")
    [hit] = await detector.detect(_ctx(repo))
    assert hit.key == "host_version_mismatch:1.14.0:1.13.1"
    assert hit.params == {"cc": "v1.14.0", "cx": "v1.13.1"}


async def test_host_versions_ignores_a_source_install(repo, isolated_home, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (isolated_home / ".claude" / "data" / "ai-team-os").mkdir(parents=True)
    (isolated_home / ".claude" / "data" / "ai-team-os" / "install_path.txt").write_text("/src", encoding="utf-8")
    _receipt(isolated_home, aiteam_version="1.13.1")
    with pytest.raises(NoDataError):
        await HostVersionsDetector().detect(_ctx(repo))
