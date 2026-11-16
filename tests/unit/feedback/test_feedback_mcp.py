"""The MCP tools as a client meets them: the confirmation dialog decides, and only it.

A real MCP client (FastMCP's, in memory) calls the tools; the tools call the real API
under uvicorn (port 0) over HTTP; gh is fake_gh.py, and github.com for OS's own App
token is fake_github_http.py. The client's dialog answer is the elicitation handler
below: no dialog support, decline, cancel, accept, no answer at all.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from mcp import types as mcp_types

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps, middleware
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.ws.manager import ConnectionManager
from aiteam.feedback import service as service_module
from aiteam.feedback.auth import AppAuth
from aiteam.feedback.service import FeedbackService
from aiteam.mcp.tools import feedback as feedback_tools
from aiteam.storage.connection import get_engine
from aiteam.storage.feedback import FeedbackRepository
from aiteam.storage.repository import StorageRepository

from .conftest import BUG, HOSTS, LOGIN, REPO, TIMEOUTS, Clock, DatabaseReads, FakeGh, app_auth
from .fake_github_http import HttpGitHub

CLIENT = mcp_types.Implementation(name="claude-code", version="2.1.325")
# What an agent passes: the host and its version are not among the tool's arguments.
TOOL_BUG = {key: value for key, value in BUG.items() if not key.startswith("host")}


class _Server:
    def __init__(self, app) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                                    lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> int:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        return self.server.servers[0].sockets[0].getsockname()[1]

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@dataclass
class Api(FakeGh, DatabaseReads):
    """The fake GitHub, plus the server's database, clock and App authorization; ``slept``: the tool's waits."""

    database: Path = Path()
    clock: Clock = field(default_factory=Clock)
    auth: AppAuth | None = None
    slept: list[float] = field(default_factory=list)


@pytest.fixture
def api(tmp_path, monkeypatch, fake_gh):
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.setattr(middleware, "_slow_hook_log", middleware._SlowHookLog())
    monkeypatch.setattr(service_module, "_login", lambda: LOGIN)
    monkeypatch.setattr(service_module, "_hostnames", lambda: HOSTS)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-mcp")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    url = f"sqlite+aiosqlite:///{data_dir / 'aiteam.db'}"
    app = app_module.create_app()
    world = Api(fake_gh.binary, fake_gh.state_path, fake_gh.log_path, database=data_dir / "aiteam.db")
    clock = world.clock
    world.auth = app_auth(data_dir, HttpGitHub(str(fake_gh.state_path), str(fake_gh.log_path), clock), clock)

    async def waiting(seconds: float) -> None:  # the tool's wait between polls, on the test clock
        world.slept.append(seconds)
        clock.advance(seconds=seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(feedback_tools, "_sleep", waiting)

    @asynccontextmanager
    async def lifespan(_app):
        repo = StorageRepository(db_url=url)  # on the server's own loop: aiosqlite is loop-bound
        await repo.init_db()
        bus = EventBus(repo=repo)

        async def projects() -> list[tuple[str, str]]:
            return []

        app.dependency_overrides.update({
            deps.get_repository: lambda: repo,
            deps.get_event_bus: lambda: bus,
            deps.get_feedback_auth: lambda: world.auth,
            deps.get_feedback_service: lambda: FeedbackService(
                FeedbackRepository(url), projects=projects, emit=bus.emit, data_dir=data_dir, target_repo=REPO,
                timeouts=TIMEOUTS, clock=clock, auth=world.auth),
        })
        try:
            yield
        finally:
            await get_engine(url).dispose()

    app.router.lifespan_context = lifespan
    with _Server(app) as port:
        monkeypatch.setenv("AITEAM_API_URL", f"http://127.0.0.1:{port}")
        yield world


def _server() -> FastMCP:
    server = FastMCP("feedback-test")
    feedback_tools.register(server)
    return server


async def _file(handler=None, **draft: Any) -> tuple[dict, dict, list]:
    """Draft through the tool, then submit through the tool; returns both answers and the dialogs shown."""
    shown: list = []

    async def recorded(message, response_type, params, context):
        shown.append((message, params.requestedSchema))
        return await handler(message, response_type, params, context)

    async with Client(_server(), client_info=CLIENT, elicitation_handler=recorded if handler else None) as client:
        drafted = (await client.call_tool("feedback_draft", {**TOOL_BUG, **draft})).data
        submitted = (await client.call_tool("feedback_submit", {"draft_id": drafted["draft_id"]})).data
    return drafted, submitted, shown


def _answer(value):
    async def handler(message, response_type, params, context):
        return value
    return handler


ACCEPT = ElicitResult(action="accept")


async def test_accepting_the_dialog_publishes_the_text_it_showed(api) -> None:
    drafted, submitted, shown = await _file(_answer(ACCEPT))
    assert submitted["status"] == "sent", submitted
    (issue,) = api.repo()["issues"]
    assert issue["body"] == drafted["body"]
    (message, _), = shown
    # The full-text page, where and hash, title: Claude Code shows three lines of the
    # message and folds the rest. What the buttons do is the last line.
    head = message.splitlines()[:3]
    assert head[0] == f"Full text / 全文: {feedback_tools.view_url(drafted['draft_id'])}"
    assert head[0].startswith("Full text / 全文: http://127.0.0.1:") and head[0].endswith("/view")
    assert head[1].startswith("To / 发往: ") and drafted["destination"] in head[1]
    assert head[1].endswith(f"Hash / 哈希: {drafted['content_hash'][:12]}")
    assert head[2] == f"Title / 标题: {drafted['title']}"
    assert drafted["body"] in message and message.splitlines()[-1] == feedback_tools.LAST_LINE
    # The client's own name and version are in the report, not anything the agent said.
    assert "Claude Code 2.1.325" in issue["body"]


async def test_the_dialog_asks_for_nothing_but_accept_or_decline(api) -> None:
    _, _, shown = await _file(_answer(ACCEPT))
    (_, schema), = shown
    assert schema == {"type": "object", "properties": {}}


async def test_declining_the_dialog_publishes_nothing_and_says_so(api) -> None:
    _, submitted, _ = await _file(_answer(ElicitResult(action="decline")))
    assert (submitted["status"], submitted["outcome"], submitted["success"]) == ("draft", "declined", False)
    assert "用户在弹框里拒绝了" in submitted["message"]
    assert api.repo()["issues"] == [] and not api.calls("create_issue")


async def test_a_client_without_a_dialog_gets_a_web_link_and_nothing_is_sent(api) -> None:
    drafted, submitted, shown = await _file(None)
    assert shown == [] and submitted["status"] == "handed_off" and "/issues/new?" in submitted["url"]
    assert "elicitation" in submitted["reason"]
    assert api.repo()["issues"] == [] and not api.calls("create_issue")


async def test_a_cancelled_dialog_goes_to_the_web(api) -> None:
    _, submitted, _ = await _file(_answer(ElicitResult(action="cancel")))
    assert submitted["status"] == "handed_off" and api.repo()["issues"] == []


async def test_an_unanswered_dialog_goes_to_the_web(api, monkeypatch) -> None:
    monkeypatch.setattr(feedback_tools, "ELICIT_TIMEOUT_SECONDS", 0.5)
    gave_up = asyncio.Event()
    hand_off = feedback_tools._hand_off

    async def handing_off(draft_id: str, reason: str) -> dict:
        gave_up.set()
        return await hand_off(draft_id, reason)

    monkeypatch.setattr(feedback_tools, "_hand_off", handing_off)

    async def handler(message, response_type, params, context):
        # Nobody answers until the tool has given up. (This client answers dialogs in
        # its receive loop, so it has to answer at some point to read the result.)
        await gave_up.wait()
        return ACCEPT

    _, submitted, _ = await _file(handler)
    assert submitted["status"] == "handed_off" and "5 分钟" in submitted["reason"]
    assert api.repo()["issues"] == [] and not api.calls("create_issue")


async def test_without_gh_a_client_without_a_dialog_gets_the_web_and_no_code(api) -> None:
    api.set(login=None)
    drafted, submitted, shown = await _file(None)
    assert drafted["route"] == "web" and shown == [] and submitted["status"] == "handed_off"
    assert api.repo()["issues"] == [] and not api.calls("oauth:code")


# -- OS's own GitHub authorization, when gh cannot publish -----------------------------


def _script(api: Api, *outcomes: str, interval: int = 5, code_seconds: int | None = None) -> None:
    world = api.world()
    world["app"].update(script=list(outcomes), interval=interval)
    if code_seconds is not None:
        world["app"]["code_seconds"] = code_seconds
    api.save(world)


class _Session:
    """One MCP client session: the dialogs it was shown, the completions it was told, the tools' answers."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.shown: list[Any] = []
        self.completed: list[str] = []
        self.returned: list[dict] = []

    async def _answer(self, message, response_type, params, context):
        self.shown.append(params)
        return ElicitResult(action=self.answers.pop(0))

    async def _notified(self, message) -> None:
        root = getattr(message, "root", None)
        if isinstance(root, mcp_types.ElicitCompleteNotification):
            self.completed.append(root.params.elicitationId)

    @asynccontextmanager
    async def open(self):
        async with Client(_server(), client_info=CLIENT, elicitation_handler=self._answer,
                          message_handler=self._notified) as client:
            self.client = client
            yield self

    async def call(self, tool: str, arguments: dict) -> dict:
        answer = (await self.client.call_tool(tool, arguments)).data
        self.returned.append(answer)
        return answer


def _without_gh(monkeypatch) -> None:
    monkeypatch.setenv("AITEAM_FEEDBACK_GH", "off")


def _form_only_client(monkeypatch) -> None:
    """The client declares form dialogs only (as clients before URL mode do)."""
    monkeypatch.setattr(mcp_types, "UrlElicitationCapability", lambda: None)


@pytest.mark.parametrize("mode", ["url", "form"])
async def test_without_gh_the_user_authorizes_in_a_dialog_and_the_post_goes_out_as_them(api, monkeypatch,
                                                                                       mode) -> None:
    _without_gh(monkeypatch)
    if mode == "form":
        _form_only_client(monkeypatch)
    _script(api, "pending", "pending", "ok")
    session = _Session("accept", "accept")
    async with session.open():
        drafted = await session.call("feedback_draft", TOOL_BUG)
        assert drafted["route"] == "auth", drafted
        submitted = await session.call("feedback_submit", {"draft_id": drafted["draft_id"]})
    assert submitted["status"] == "sent", submitted
    publish, authorize = session.shown
    assert isinstance(publish, mcp_types.ElicitRequestFormParams)
    code_line = authorize.message.splitlines()[0]
    code = code_line.removeprefix("GitHub code / 授权码:  【 ").removesuffix(" 】")
    assert code != code_line and len(code) == 9 and code[4] == "-"
    if mode == "url":
        assert isinstance(authorize, mcp_types.ElicitRequestURLParams)
        assert authorize.url == "https://github.com/login/device"
        assert session.completed == [authorize.elicitationId]
    else:
        assert isinstance(authorize, mcp_types.ElicitRequestFormParams)
        assert authorize.requestedSchema == {"type": "object", "properties": {}}
        assert "https://github.com/login/device" in authorize.message.splitlines()[1] and not session.completed
    assert api.slept == [5.0, 5.0] and len(api.calls("oauth:poll")) == 3
    (issue,) = api.repo()["issues"]
    assert (issue["user"]["login"], issue["body"]) == ("alice-app", drafted["body"])
    row = api.ledger(drafted["draft_id"])
    assert (row["route"], row["gh_login"], row["confirm_method"], row["status"]) == (
        "app", "alice-app", "elicitation", "sent")
    (event,) = api.events("feedback.submitted")
    assert event["auth"] == "app"
    _assert_no_secret(api, session.returned)


@pytest.mark.parametrize("which", ["publish", "authorize"])
async def test_declining_either_dialog_sends_nothing_and_asks_github_nothing_more(api, monkeypatch, which) -> None:
    _without_gh(monkeypatch)
    session = _Session("decline") if which == "publish" else _Session("accept", "decline")
    async with session.open():
        drafted = await session.call("feedback_draft", TOOL_BUG)
        submitted = await session.call("feedback_submit", {"draft_id": drafted["draft_id"]})
    outcome = "declined" if which == "publish" else "auth_declined"
    assert (submitted["status"], submitted["outcome"], submitted["success"]) == ("draft", outcome, False)
    assert api.repo()["issues"] == [] and not api.calls("oauth:poll") and not api.slept
    assert len(api.calls("oauth:code")) == (0 if which == "publish" else 1)  # a code only after Accept
    assert await api.auth.store.load() is None


async def test_slow_down_makes_the_tool_wait_longer_and_github_is_not_asked_early(api, monkeypatch) -> None:
    _without_gh(monkeypatch)
    _script(api, "slow_down", "ok", interval=5)
    session = _Session("accept", "accept")
    async with session.open():
        drafted = await session.call("feedback_draft", TOOL_BUG)
        submitted = await session.call("feedback_submit", {"draft_id": drafted["draft_id"]})
    assert submitted["status"] == "sent" and api.slept == [10.0]
    assert len(api.calls("oauth:poll")) == 2


@pytest.mark.parametrize(("script", "code_seconds", "status"), [(["denied"], None, "draft"),
                                                                (["pending"], 12, "handed_off")],
                         ids=["denied", "expired"])
async def test_a_denied_or_expired_code_sends_nothing(api, monkeypatch, script, code_seconds, status) -> None:
    _without_gh(monkeypatch)
    _script(api, *script, code_seconds=code_seconds)
    session = _Session("accept", "accept")
    async with session.open():
        drafted = await session.call("feedback_draft", TOOL_BUG)
        submitted = await session.call("feedback_submit", {"draft_id": drafted["draft_id"]})
    assert submitted["status"] == status and api.repo()["issues"] == []
    if status == "draft":
        assert submitted["outcome"] == "auth_denied"
    else:
        assert "授权没有完成" in submitted["reason"] and "/issues/new?" in submitted["url"]


async def test_an_unfinished_authorization_answers_authorizing_and_the_next_call_just_waits(api, monkeypatch) -> None:
    _without_gh(monkeypatch)
    _script(api, "pending")
    session = _Session("accept", "accept")
    async with session.open():
        drafted = await session.call("feedback_draft", TOOL_BUG)
        first = await session.call("feedback_submit", {"draft_id": drafted["draft_id"]})
        assert first["status"] == "authorizing" and sum(api.slept) <= feedback_tools.AUTH_WAIT_SECONDS
        assert first["user_code"] in session.shown[1].message
        world = api.world()
        for flow in world["app"]["flows"].values():
            flow["script"] = ["ok"]  # the user finishes on GitHub
        api.save(world)
        second = await session.call("feedback_submit", {"draft_id": drafted["draft_id"]})
    assert second["status"] == "sent", second
    assert len(session.shown) == 2  # no second question, no new code
    assert len(api.calls("oauth:code")) == 1
    _assert_no_secret(api, session.returned)


async def test_the_account_tool_says_who_and_signs_out(api, monkeypatch) -> None:
    session = _Session()
    async with session.open():
        assert (await session.call("feedback_account", {}))["data"]["gh_login"] == "alice-gh"
        _without_gh(monkeypatch)
        none = (await session.call("feedback_account", {}))["data"]
        assert none["route"] == "none" and "AITEAM_FEEDBACK_GH=off" in none["gh_reason"]
        start = await api.auth.start("setup")
        assert (await api.auth.poll(start.flow_id)).status == "ok"
        status = (await session.call("feedback_account", {"action": "status"}))["data"]
        assert (status["route"], status["app_login"]) == ("app", "alice-app") and status["app_expires_at"]
        out = (await session.call("feedback_account", {"action": "sign_out"}))["data"]
        assert out["signed_out"] and out["revoke_page"].startswith("https://github.com/settings/")
        assert (await session.call("feedback_account", {}))["data"]["route"] == "none"
        assert not (await session.call("feedback_account", {"action": "revoke"}))["success"]
    assert await api.auth.store.load() is None
    _assert_no_secret(api, session.returned)


def _assert_no_secret(api: Api, returned: list[dict]) -> None:
    """No access or refresh token, nor GitHub's device code, in what the tools returned."""
    issued = api.world()["app"]
    secrets = [*issued["tokens"], *issued["refresh"], *issued["flows"]]
    text = json.dumps(returned)
    assert secrets and not any(secret in text for secret in secrets)
    assert "ghu_" not in text and "ghr_" not in text and "device_code" not in text
