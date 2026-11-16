"""Which requests an edge turns into operations (aiteam.sync.routing).

Matched against the real application's routes, in its router's order: a write to a
shared-face route becomes an operation; reads, session-face and local writes stay
on the node; resolving a folder to its project (on every hook call) is a read
unless it asks to create the project.
"""

from __future__ import annotations

import json

import pytest

from aiteam.sync.routing import RouteTable, writes
from aiteam.types import SyncFace


@pytest.fixture(scope="module")
def table() -> RouteTable:
    from aiteam.api.app import create_app

    return RouteTable(create_app().routes)


@pytest.mark.parametrize(("method", "path", "operation"), [
    ("POST", "/api/projects/p1/tasks", True),
    ("PUT", "/api/tasks/t1", True),
    ("PUT", "/api/tasks/t1/complete", True),
    ("POST", "/api/tasks/t1/memo", True),
    ("POST", "/api/memories", True),
    ("DELETE", "/api/reports/r1", True),
    ("GET", "/api/tasks/t1", False),
    ("POST", "/api/hooks/event", False),
    ("POST", "/api/notices/pending", False),
    ("POST", "/api/sync/ops", False),
], ids=lambda value: str(value))
def test_only_writes_to_the_shared_face_become_operations(table, method, path, operation) -> None:
    match = table.match(method, path)
    assert match is not None
    assert writes(match, method, b"{}") is operation


def test_resolving_a_folder_is_a_read_unless_it_creates_the_project(table) -> None:
    match = table.match("POST", "/api/context/resolve")
    assert match.surface.face is SyncFace.SHARED and match.surface.write_flag == "auto_create"
    assert not writes(match, "POST", json.dumps({"cwd": "/work", "auto_create": False}).encode())
    assert not writes(match, "POST", json.dumps({"cwd": "/work"}).encode())
    assert writes(match, "POST", json.dumps({"cwd": "/work", "auto_create": True}).encode())


def test_deletions_and_the_ecosystem_archive_are_online_only(table) -> None:
    for method, path in (("DELETE", "/api/projects/p1"), ("DELETE", "/api/scheduler/s1"),
                         ("POST", "/api/ecosystem/tags"), ("PUT", "/api/account-usage/k/plan-anchor")):
        surface = table.match(method, path).surface
        assert surface.face is SyncFace.SHARED and not surface.offline_ok, (method, path)


def test_a_plan_anchor_is_chosen_where_its_samples_are(table) -> None:
    # The reset reads this node's own samples; only the store goes to the center.
    assert table.match("POST", "/api/account-usage/k/plan-anchor/reset").surface.face is SyncFace.LOCAL


def test_path_parameters_name_the_row_a_merge_rule_edits(table) -> None:
    match = table.match("PUT", "/api/tasks/abc")
    assert match.template == "/api/tasks/{task_id}" and match.params == {"task_id": "abc"}
