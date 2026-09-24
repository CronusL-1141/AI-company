"""E07 lifecycle across the persistence boundary (defect D1, design §14).

A folder nobody returns to kept its "not a registered project" notice open for
good: on the Dashboard banner and badge, and in /os-doctor as the one decision
waiting. Each case writes through one app and repository, drops the pooled
engines, and reads back through a second app with a new repository.
"""

from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.services.notices.detectors import registration
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@asynccontextmanager
async def _client(db_url: str):
    repository = StorageRepository(db_url=db_url)
    await repository.init_db()
    deps._repository = repository
    deps._event_bus = EventBus(repo=repository)
    app = create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, repository
    finally:
        deps._repository = None
        deps._event_bus = None
        await close_db()  # the next client must read from disk


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"


def _start(cwd, session="s1", source="startup", **facts):
    return {"host": "cc", "event": "SessionStart", "source": source, "session_id": session, "cwd": str(cwd),
            "facts": {"entrypoint": "cli", "fallback_language": "en", **facts}}


def _key(folder) -> str:
    return registration.notice_key(registration.real_dir(str(folder)))


async def _statuses(db_url, *folders) -> list[str | None]:
    async with _client(db_url) as (_client_, repository):
        rows = [await repository.get_notice(_key(folder)) for folder in folders]
    return [row.status.value if row else None for row in rows]


async def _asked(db_url, *folders) -> None:
    async with _client(db_url) as (client, _repository):
        for index, folder in enumerate(folders):
            answer = (await client.post("/api/notices/pending", json=_start(folder, session=f"s{index}"))).json()
            assert "not a registered project" in answer["user_text"], folder


@pytest.fixture()
def folders(tmp_path):
    root = tmp_path / "root"
    sub = root / "deep" / "sub"
    other = tmp_path / "rooted"  # shares a name prefix with root, is not below it
    for path in (sub, other):
        path.mkdir(parents=True)
    return root, sub, other


# ---------------------------------------------------------------------------
# Registering clears the folder and everything below it
# ---------------------------------------------------------------------------


async def test_project_create_clears_the_root_and_its_subfolders(db_url, folders):
    root, sub, other = folders
    await _asked(db_url, root, sub, other)
    async with _client(db_url) as (client, _repository):
        created = await client.post("/api/projects", json={"name": "root", "root_path": str(root)})
        assert created.status_code == 201, created.text
    assert await _statuses(db_url, root, sub, other) == ["cleared", "cleared", "active"]
    async with _client(db_url) as (client, _repository):
        summary = (await client.get("/api/notices/summary")).json()
        assert summary["notices"] == 1 and summary["top"]["key"] == _key(other)


async def test_project_re_rooted_over_a_folder_clears_it(db_url, folders, tmp_path):
    root, sub, _other = folders
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    await _asked(db_url, sub)
    async with _client(db_url) as (client, _repository):
        project = (await client.post("/api/projects", json={"name": "p", "root_path": str(elsewhere)})).json()
        moved = await client.put(f"/api/projects/{project['data']['id']}", json={"root_path": str(root)})
        assert moved.status_code == 200, moved.text
    assert await _statuses(db_url, sub) == ["cleared"]


async def test_context_auto_registration_clears_it(db_url, folders):
    root, _sub, _other = folders
    await _asked(db_url, root)
    async with _client(db_url) as (client, _repository):
        resolved = (await client.post("/api/context/resolve", json={"cwd": str(root), "auto_create": True})).json()
        assert resolved["created"] is True
    assert await _statuses(db_url, root) == ["cleared"]


async def test_a_dismissed_folder_stays_dismissed_when_registered(db_url, folders):
    root, _sub, _other = folders
    await _asked(db_url, root)
    async with _client(db_url) as (client, _repository):
        assert (await client.post(f"/api/notices/{_key(root)}/dismiss")).status_code == 200
        await client.post("/api/projects", json={"name": "root", "root_path": str(root)})
    assert await _statuses(db_url, root) == ["dismissed"]


def test_every_project_writer_clears_the_notice():
    """A new route that creates or re-roots a project must clear E07 too."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[3] / "src" / "aiteam"
    writers = [path for path in source.rglob("*.py")
               if ".create_project(" in path.read_text(encoding="utf-8")
               or ".update_project(" in path.read_text(encoding="utf-8")]
    writers = [path for path in writers if path.name != "repository.py"]
    assert writers, "no project writer found: the scan is broken"
    for path in writers:
        assert "clear_registered(" in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------------------
# The on-demand sweep: vanished folders clear, unanswered ones expire
# ---------------------------------------------------------------------------


async def test_summary_sweeps_vanished_and_stale_folders(db_url, tmp_path):
    gone, stale, live = tmp_path / "gone", tmp_path / "stale", tmp_path / "live"
    for path in (gone, stale, live):
        path.mkdir()
    await _asked(db_url, gone, stale, live)
    async with _client(db_url) as (_client_, repository):
        notice = await repository.get_notice(_key(stale))
        await repository.upsert_notice(key=notice.key, catalog_id=notice.catalog_id, project_id=notice.project_id,
                                       now=utc_now() - timedelta(days=8))
    shutil.rmtree(gone)
    registration._last_sweep.clear()
    async with _client(db_url) as (client, _repository):
        summary = (await client.get("/api/notices/summary")).json()
        assert summary["notices"] == 1 and summary["top"]["key"] == _key(live)
    assert await _statuses(db_url, gone, stale, live) == ["cleared", "expired", "active"]
    async with _client(db_url) as (client, _repository):
        expired = (await client.get("/api/notices", params={"status": "expired"})).json()
        assert [row["key"] for row in expired["items"]] == [_key(stale)]


async def test_an_expired_folder_is_asked_again_when_a_session_returns(db_url, tmp_path):
    stale = tmp_path / "stale"
    stale.mkdir()
    await _asked(db_url, stale)
    async with _client(db_url) as (_client_, repository):
        notice = await repository.get_notice(_key(stale))
        await repository.upsert_notice(key=notice.key, catalog_id=notice.catalog_id, project_id=notice.project_id,
                                       now=utc_now() - timedelta(days=8))
    registration._last_sweep.clear()
    async with _client(db_url) as (client, _repository):
        answer = (await client.post("/api/notices/pending", json=_start(stale, session="back"))).json()
        assert "not a registered project" in answer["user_text"]
    assert await _statuses(db_url, stale) == ["active"]


async def test_the_pending_fetch_sweeps_too_and_is_throttled(db_url, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for path in (first, second):
        path.mkdir()
    await _asked(db_url, first, second)
    registration._last_sweep.clear()
    shutil.rmtree(first)
    async with _client(db_url) as (client, _repository):
        await client.post("/api/notices/pending", json=_start(tmp_path, session="x", source="resume"))
        shutil.rmtree(second)
        await client.get("/api/notices/summary")  # within the throttle window: no second sweep
    assert await _statuses(db_url, first, second) == ["cleared", "active"]


async def test_the_sweep_checks_a_bounded_number_of_folders(db_url, tmp_path, monkeypatch):
    monkeypatch.setattr(registration, "SWEEP_LIMIT", 2)
    paths = [tmp_path / f"f{index}" for index in range(3)]
    for path in paths:
        path.mkdir()
    await _asked(db_url, *paths)
    checked = []
    real_missing = registration._missing
    monkeypatch.setattr(registration, "_missing", lambda folders: checked.extend(folders) or real_missing(folders))
    registration._last_sweep.clear()
    async with _client(db_url) as (client, _repository):
        await client.get("/api/notices/summary")
    assert len(checked) == 2, "oldest first, at most SWEEP_LIMIT folders per sweep"


# ---------------------------------------------------------------------------
# "Skip" said while the API was down arrives through the local record file
# ---------------------------------------------------------------------------


async def test_a_queued_dismiss_is_imported_once_and_only_for_e07(db_url, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    await _asked(db_url, work)
    records = [
        {"uuid": "d" * 32, "kind": "notice_dismiss", "key": _key(work), "at": "2026-09-24T00:00:00Z"},
        {"uuid": "e" * 32, "kind": "notice_dismiss", "key": "branch_switched:x:a:b"},
    ]
    async with _client(db_url) as (client, repository):
        await repository.upsert_notice(key="branch_switched:x:a:b", catalog_id="branch_switched",
                                       params={"repo": "r", "ob": "a", "nb": "b"}, now=utc_now())
        for _ in range(2):
            response = await client.post("/api/notices/pending", json={
                "host": "cc", "event": "UserPromptSubmit", "session_id": "s1", "cwd": str(tmp_path),
                "facts": {"local_records": records}})
            assert response.status_code == 200, response.text
    async with _client(db_url) as (_client_, repository):
        assert (await repository.get_notice(_key(work))).status.value == "dismissed"
        assert (await repository.get_notice("branch_switched:x:a:b")).status.value == "active"
