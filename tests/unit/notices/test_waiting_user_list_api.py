"""GET /api/task-wall/waiting-user: every task waiting on the user, across projects.

The list the Dashboard's pending page shows. It must agree with the sidebar badge
(GET /api/notices/summary ``tasks``: the same predicate) and, narrowed to one
project, with that project's digest ``waiting_user`` (the same rows). Seeded over
HTTP where the managed API can write the shape, through the repository where only
legacy data has it; read back by a new app on the same SQLite file.
"""

from __future__ import annotations

import pytest

from aiteam.storage.connection import close_db
from aiteam.types import TaskStatus

from .test_config_change import _client

WAITING = "/api/task-wall/waiting-user"
# Measured against the request's own clock: two requests never agree on these.
_CLOCK_FIELDS = ("idle_days", "wall_days", "blocked_days")


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_db()


def _user(reason: str) -> dict:
    return {"kind": "user", "reason": reason}


async def _project(client, name: str, root) -> str:
    response = await client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]["id"]


async def _task(client, project_id: str, title: str, **extra) -> str:
    response = await client.post(f"/api/projects/{project_id}/tasks", json={"title": title, **extra})
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def _seed(client, repo, tmp_path) -> dict[str, str]:
    """Two projects, each with a waiting top-level task and a waiting subtask of a parent that is not waiting."""
    ids: dict[str, str] = {}
    for key in ("alpha", "beta"):
        project = ids[key] = await _project(client, key, tmp_path / key)
        parent = ids[f"{key}_parent"] = await _task(client, project, f"{key} 父任务")
        ids[f"{key}_top"] = await _task(client, project, f"{key} 顶层等用户", waiting_on=_user("拍板"))
        ids[f"{key}_child"] = await _task(client, project, f"{key} 子任务等用户",
                                          parent_id=parent, waiting_on=_user("选方案"))
        # Waiting on an agent is not the user's.
        await repo.create_task(None, f"{key} 等 agent", project_id=project, config={
            "task_management": {"waiting_on": {"kind": "agent", "target_id": "someone", "reason": "等执行"}},
        })
    # Work recorded on a waiting task: the digest reads it for its other lists.
    memo = await client.post(f"/api/tasks/{ids['alpha_top']}/memo",
                             json={"content": "已整理选项", "author": "builder", "type": "progress"})
    assert memo.status_code == 200, memo.text
    # Legacy rows the managed API refuses to write: completed yet still recording a
    # user wait, and a user wait outside every project.
    done = await repo.create_task(None, "已完成仍记等待", project_id=ids["alpha"],
                                  config={"task_management": {"waiting_on": _user("已决定")}})
    await repo.update_task(done.id, status=TaskStatus.COMPLETED)
    ids["done"] = done.id
    loose = await repo.create_task(None, "无项目等用户", config={"task_management": {"waiting_on": _user("确认")}})
    ids["loose"] = loose.id
    return ids


def _without_clock(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in _CLOCK_FIELDS}


async def test_waiting_list_spans_projects_and_matches_badge_and_digest(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'w.db'}"
    async with _client(db_url) as (client, repo):
        ids = await _seed(client, repo, tmp_path)
    await close_db()
    async with _client(db_url) as (client, _fresh):  # a new app and repository read it back
        everything = (await client.get(WAITING)).json()
        listed = {item["id"]: item for item in everything["data"]}
        assert set(listed) == {ids["alpha_top"], ids["alpha_child"], ids["beta_top"], ids["beta_child"], ids["loose"]}
        summary = (await client.get("/api/notices/summary")).json()
        assert everything["total"] == len(everything["data"]) == summary["tasks"] == 5
        assert ids["done"] not in listed

        for key in ("alpha", "beta"):
            child, top = listed[ids[f"{key}_child"]], listed[ids[f"{key}_top"]]
            assert (child["parent_id"], child["parent_title"]) == (ids[f"{key}_parent"], f"{key} 父任务")
            assert top["parent_id"] is None and top["parent_title"] is None
            assert child["project_id"] == top["project_id"] == ids[key]
            assert child["waiting_on"]["reason"] == "选方案" and top["waiting_on"]["reason"] == "拍板"
        assert listed[ids["loose"]]["project_id"] is None

        for key in ("alpha", "beta"):
            narrowed = (await client.get(WAITING, params={"project_id": ids[key]})).json()
            assert [item["id"] for item in narrowed["data"]] == [
                item["id"] for item in everything["data"] if item["project_id"] == ids[key]
            ]
            assert {item["id"] for item in narrowed["data"]} == {ids[f"{key}_top"], ids[f"{key}_child"]}
            # One row construction: the digest's waiting list is this list, field for field,
            # from both of its outlets (the digest alone and the wall that embeds it).
            digest = (await client.get(f"/api/projects/{ids[key]}/task-wall/digest")).json()
            wall = (await client.get(f"/api/projects/{ids[key]}/task-wall")).json()
            expected = [_without_clock(item) for item in narrowed["data"]]
            assert [_without_clock(item) for item in digest["waiting_user"]] == expected
            assert [_without_clock(item) for item in wall["digest"]["waiting_user"]] == expected

        # The user settles one in the dialog: the next read drops it, badge and list together.
        cleared = await client.put(f"/api/tasks/{ids['alpha_top']}", json={"waiting_on": None})
        assert cleared.status_code == 200, cleared.text
        after = (await client.get(WAITING)).json()
        assert ids["alpha_top"] not in {item["id"] for item in after["data"]}
        assert after["total"] == (await client.get("/api/notices/summary")).json()["tasks"] == 4


async def test_project_filter_is_a_plain_filter(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'f.db'}"
    async with _client(db_url) as (client, _repo):
        response = await client.get(WAITING, params={"project_id": "no-such-project"})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == [] and response.json()["total"] == 0
