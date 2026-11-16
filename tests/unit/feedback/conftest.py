"""The production HTTP stack over a temporary database, with the fake GitHub as gh and as github.com.

The service is the production one; only its clock, its time limits, the target
repository and the machine's login and host names are chosen here. Every request
goes through the app (routes, validation, error handlers); gh is a real child process
(fake_gh.py), and OS's GitHub App token reaches the same fake world over HTTP
(fake_github_http.py, through the production AppAuth with a 0600 file store).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps, middleware
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.ws.manager import ConnectionManager
from aiteam.clock import utc_now
from aiteam.feedback import service as service_module
from aiteam.feedback.auth import AppAuth, FileStore
from aiteam.feedback.github import GH_SWITCH_ENV, Timeouts
from aiteam.feedback.service import FeedbackService
from aiteam.storage.connection import get_engine
from aiteam.storage.feedback import FeedbackRepository
from aiteam.storage.repository import StorageRepository

from .fake_github_http import HttpGitHub, app_state

STUB = Path(__file__).with_name("fake_gh.py")
REPO = "acme/os"
REPO_ID = "R_acme"
IDEAS_ID = "DIC_ideas"
LOGIN = "alice"
HOSTS = ("alice-mbp.local", "alice-mbp")
GH_LOGIN = "alice-gh"
APP_LOGIN = "alice-app"
CLIENT_ID = "Iv23-test-client"
TIMEOUTS = Timeouts(login=20, lookup=20, search=20, send=20)


def world_state(**changes: Any) -> dict:
    return {
        "login": GH_LOGIN, "token": "ok", "collaborator": False,
        "repos": {REPO: {
            "id": REPO_ID, "labels": ["bug", "enhancement", "question"], "next_number": 1,
            "categories": [{"id": "DIC_general", "name": "General", "slug": "general"},
                           {"id": IDEAS_ID, "name": "Ideas", "slug": "ideas"},
                           {"id": "DIC_qa", "name": "Q&A", "slug": "q-a"}],
            "issues": [], "discussions": [],
        }},
        "app": app_state(CLIENT_ID, [REPO], APP_LOGIN),
        **changes,
    }


@dataclass
class FakeGh:
    """The stand-in's world and call log."""

    binary: Path
    state_path: Path
    log_path: Path

    def world(self) -> dict:
        return json.loads(self.state_path.read_text())

    def save(self, world: dict) -> None:
        self.state_path.write_text(json.dumps(world))

    def set(self, **changes: Any) -> None:
        self.save({**self.world(), **changes})

    def repo(self) -> dict:
        return self.world()["repos"][REPO]

    def calls(self, op: str | None = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        entries = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [entry for entry in entries if op is None or entry["op"] == op]

    def add_issue(self, title: str, body: str = "body") -> dict:
        world = self.world()
        repo = world["repos"][REPO]
        number = repo["next_number"]
        repo["next_number"] += 1
        stamp = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        issue = {"number": number, "node_id": f"I_{number}", "title": title, "body": body, "state": "open",
                 "html_url": f"https://github.com/{REPO}/issues/{number}", "labels": [],
                 "user": {"login": "someone"}, "created_at": stamp, "updated_at": stamp, "comments": []}
        repo["issues"].append(issue)
        self.save(world)
        return issue

    def add_discussion(self, title: str, body: str = "body") -> dict:
        world = self.world()
        repo = world["repos"][REPO]
        number = repo["next_number"]
        repo["next_number"] += 1
        discussion = {"id": f"D_{number}", "number": number, "title": title, "body": body,
                      "url": f"https://github.com/{REPO}/discussions/{number}", "closed": False,
                      "category_id": IDEAS_ID, "author": "someone", "comments": []}
        repo["discussions"].append(discussion)
        self.save(world)
        return discussion


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir()
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" "$@"\n')
    binary.chmod(0o755)
    gh = FakeGh(binary, tmp_path / "gh-world.json", tmp_path / "gh-calls.jsonl")
    gh.save(world_state())
    monkeypatch.setenv("AITEAM_GH_BIN", str(binary))
    monkeypatch.setenv("FAKE_GH_STATE", str(gh.state_path))
    monkeypatch.setenv("FAKE_GH_LOG", str(gh.log_path))
    return gh


class Clock:
    """The service's clock: real time at the start, moved on by the tests."""

    def __init__(self) -> None:
        self.now = utc_now()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class DatabaseReads:
    """Reading the API's database file directly (across the persistence boundary)."""

    database: Path

    def rows(self, sql: str, *args: Any) -> list[tuple]:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def ledger(self, draft_id: str) -> dict:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM feedback_submissions WHERE id = ?", (draft_id,)).fetchone()
            return dict(row) if row is not None else {}
        finally:
            connection.close()

    def events(self, event_type: str) -> list[dict]:
        return [json.loads(data) for (data,) in self.rows(
            "SELECT data FROM events WHERE type = ? ORDER BY timestamp", event_type)]


@dataclass
class Stack(DatabaseReads):
    client: httpx.AsyncClient
    repo: StorageRepository
    bus: EventBus
    database: Path
    data_dir: Path
    clock: Clock
    gh: FakeGh
    timeouts: Timeouts
    auth: AppAuth
    github: HttpGitHub

    def service(self, **changes: Any) -> FeedbackService:
        """A service over this database, as the app builds one per request."""

        async def projects() -> list[tuple[str, str]]:
            return [(p.name, p.root_path or "") for p in await self.repo.list_projects()]

        options = {"projects": projects, "emit": self.bus.emit, "data_dir": self.data_dir, "target_repo": REPO,
                   "timeouts": self.timeouts, "clock": self.clock, "auth": self.auth, **changes}
        return FeedbackService(FeedbackRepository(self.repo.db_url), **options)

    async def draft(self, **body: Any) -> dict:
        response = await self.client.post("/api/feedback/drafts", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    async def submit(self, draft_id: str, content_hash: str, **body: Any) -> httpx.Response:
        return await self.client.post(f"/api/feedback/drafts/{draft_id}/submit", json={
            "confirm_method": "elicitation", "action": "accept", "content_hash": content_hash, **body})


BUG = {"kind": "bug", "title": "Task wall create button stays disabled",
       "body": "Expected the button to work; it stays grey.", "steps": "1. Open /tasks\n2. Click create",
       "host": "cc", "host_label": "Claude Code", "host_version": "2.1.325", "author": "tester"}
IDEA = {"kind": "idea", "title": "Group feedback by component", "body": "Hard to see which part a report is about.",
        "proposal": "A component field.", "host": "cc", "host_label": "Claude Code", "host_version": "2.1.325"}


async def open_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: FakeGh, *,
                     timeouts: Timeouts = TIMEOUTS, database: Path | None = None) -> Stack:
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.setattr(middleware, "_slow_hook_log", middleware._SlowHookLog())
    monkeypatch.setattr(service_module, "_login", lambda: LOGIN)
    monkeypatch.setattr(service_module, "_hostnames", lambda: HOSTS)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    database = database or data_dir / "aiteam.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{database}")
    await repo.init_db()
    bus = EventBus(repo=repo)
    clock = Clock()
    github = HttpGitHub(str(gh.state_path), str(gh.log_path), clock)
    auth = app_auth(data_dir, github, clock)
    stack = Stack(None, repo, bus, database, data_dir, clock, gh, timeouts, auth, github)  # type: ignore[arg-type]
    app = app_module.create_app()
    app.dependency_overrides.update({
        deps.get_repository: lambda: repo,
        deps.get_event_bus: lambda: bus,
        deps.get_feedback_auth: lambda: stack.auth,
        deps.get_feedback_service: lambda: stack.service(),
    })
    stack.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"X-CC-Session-Id": "session-1"}, timeout=120)
    return stack


async def close_stack(stack: Stack) -> None:
    await stack.client.aclose()
    await get_engine(stack.repo.db_url).dispose()


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: FakeGh):
    opened = await open_stack(tmp_path, monkeypatch, fake_gh)
    try:
        yield opened
    finally:
        await close_stack(opened)


def app_auth(data_dir: Path, github: HttpGitHub, clock: Clock) -> AppAuth:
    """The production AppAuth on the fake github.com, keeping its token in a 0600 file."""
    return AppAuth(FileStore(data_dir / "feedback" / "github-token.json"), client_id=CLIENT_ID, http=github.client,
                   clock=clock)


async def authorize(auth: AppAuth, draft_id: str = "setup") -> None:
    """Have the fake user authorize the App through the real device flow (its script answers ok)."""
    start = await auth.start(draft_id)
    assert (await auth.poll(start.flow_id)).status == "ok"


def without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feedback acts as if there were no gh (the switch live trials of the token path use too)."""
    monkeypatch.setenv(GH_SWITCH_ENV, "off")


@pytest_asyncio.fixture(params=["gh", "app"])
async def any_stack(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: FakeGh):
    """The stack publishing through gh, then again through OS's App token (gh switched off):
    dedupe, marker search, reconciling and the rate limit must not care which."""
    opened = await open_stack(tmp_path, monkeypatch, fake_gh)
    if request.param == "app":
        without_gh(monkeypatch)
        await authorize(opened.auth)
    try:
        yield opened
    finally:
        await close_stack(opened)
