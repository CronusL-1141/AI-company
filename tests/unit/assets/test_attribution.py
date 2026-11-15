"""Project-memory folders to OS projects, by slug computed forward; never auto-registered."""

from __future__ import annotations

import json
from pathlib import Path

from aiteam.assets.attribution import ProjectRoot, attribute, attribute_path, slug
from aiteam.types import AssetOwnerKind

PROJECT, UNBOUND = AssetOwnerKind.PROJECT, AssetOwnerKind.UNBOUND


def test_slug_is_claude_codes_folder_name():
    assert slug("/home/me/AI team OS") == "-home-me-AI-team-OS"
    assert slug("/home/me/.local/share/x") == "-home-me--local-share-x"
    assert slug("/Volumes/盘/项目") == "-Volumes---" + "-" * 2  # each CJK character becomes one dash


def test_exact_and_subfolder_matches():
    projects = [ProjectRoot("A", "/w/app"), ProjectRoot("B", "/w/app/sub")]
    assert attribute("-w-app", projects) == attribute("-w-app", projects)
    assert attribute("-w-app", projects).ref == "A" and attribute("-w-app", projects).subpath == ""
    # The longest project wins; the rest is kept in slug form (stable whatever is mounted).
    owner = attribute("-w-app-sub-deep-er", projects)
    assert (owner.kind, owner.ref, owner.subpath) == (PROJECT, "B", "deep-er")
    owner = attribute("-w-app-other", projects)
    assert (owner.kind, owner.ref, owner.subpath) == (PROJECT, "A", "other")


def test_trailing_slash_in_root_path_is_ignored():
    assert attribute("-w-app", [ProjectRoot("A", "/w/app/")]).ref == "A"


def test_no_match_is_unbound_by_slug():
    owner = attribute("-home-me", [ProjectRoot("A", "/w/app")])
    assert (owner.kind, owner.ref) == (UNBOUND, "-home-me")


def test_colliding_slugs_are_decided_by_a_session_cwd(tmp_path: Path):
    projects = [ProjectRoot("ZH1", "/v/项目"), ProjectRoot("ZH2", "/v/文档")]  # both slug "-v---"
    folder = tmp_path / "-v---"
    folder.mkdir()
    assert attribute("-v---", projects, folder).kind == UNBOUND  # no session file to ask
    (folder / "s1.jsonl").write_text(json.dumps({"type": "summary"}) + "\n"
                                     + json.dumps({"cwd": "/v/文档", "type": "user"}) + "\n", encoding="utf-8")
    owner = attribute("-v---", projects, folder)
    assert (owner.kind, owner.ref, owner.subpath) == (PROJECT, "ZH2", "")


def test_per_project_entries_attribute_by_path():
    projects = [ProjectRoot("A", "/w/app")]
    assert attribute_path("/w/app/", projects).ref == "A"
    assert attribute_path("/w/app/pkg", projects).subpath == "pkg"
    assert attribute_path("/elsewhere", projects).kind == UNBOUND


def test_a_sibling_folder_is_not_a_subfolder_when_a_session_says_so(tmp_path: Path):
    projects = [ProjectRoot("A", "/w/app")]
    sibling = tmp_path / "-w-app-foo"
    sibling.mkdir()
    (sibling / "s.jsonl").write_text(json.dumps({"cwd": "/w/app-foo"}) + "\n", encoding="utf-8")
    assert attribute("-w-app-foo", projects, sibling).kind == UNBOUND
    (sibling / "s.jsonl").write_text(json.dumps({"cwd": "/w/app/foo"}) + "\n", encoding="utf-8")
    owner = attribute("-w-app-foo", projects, sibling)
    assert (owner.kind, owner.ref, owner.subpath) == (PROJECT, "A", "foo")
    # No session to ask: the slug's extension is taken as a subfolder.
    assert attribute("-w-app-foo", projects, tmp_path / "absent").ref == "A"
