"""OS's own files come from plugin/: release content is OS's, changed content is an OS template."""

from __future__ import annotations

import shutil
from pathlib import Path

from aiteam.assets.os_manifest import build_claude_manifest, source_root

ROOT = source_root()


def test_every_template_install_py_copies_is_listed():
    manifest = build_claude_manifest()
    plugin = ROOT / "plugin"
    for agent in (plugin / "agents").glob("*.md"):
        assert f"agents/{agent.name}" in manifest.files
    for command in (plugin / "commands").glob("*.md"):
        assert f"commands/{command.name}" in manifest.files
    assert any(path.startswith("skills/") for path in manifest.files)
    assert "loop.md" in manifest.files


def test_release_content_is_os_changed_content_is_not():
    manifest = build_claude_manifest()
    agent = sorted((ROOT / "plugin" / "agents").glob("*.md"))[0]
    rel = f"agents/{agent.name}"
    assert manifest.is_release(rel, agent.read_bytes())
    assert manifest.os_name(rel) and not manifest.is_release(rel, agent.read_bytes() + b"\nmy edit\n")
    assert not manifest.os_name("agents/my-own.md")


def test_retired_names_are_os_whatever_their_content():
    manifest = build_claude_manifest()
    for rel in manifest.retired:
        assert manifest.is_release(rel, b"anything")


def test_loop_prompt_with_the_marker_is_os():
    manifest = build_claude_manifest()
    assert manifest.is_release("loop.md", f"<!-- {manifest.loop_sentinel} v0 -->\nold text\n".encode())
    assert not manifest.is_release("loop.md", b"my own loop prompt\n")


def test_the_list_follows_plugin(tmp_path: Path):
    """A template added to plugin/ is OS's at once; a removed one is not (the list is derived)."""
    shutil.copy2(ROOT / "install.py", tmp_path / "install.py")
    shutil.copytree(ROOT / "plugin" / "agents", tmp_path / "plugin" / "agents")
    removed = sorted((tmp_path / "plugin" / "agents").glob("*.md"))[0]
    removed.unlink()
    (tmp_path / "plugin" / "agents" / "brand-new.md").write_text("new\n", encoding="utf-8")
    manifest = build_claude_manifest(tmp_path)
    assert "agents/brand-new.md" in manifest.files
    assert f"agents/{removed.name}" not in manifest.files


def test_round_trip_for_the_child_process():
    manifest = build_claude_manifest()
    assert type(manifest).from_json(manifest.to_json()) == manifest
