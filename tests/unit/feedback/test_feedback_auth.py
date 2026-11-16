"""OS's own GitHub authorization (a GitHub App user token from the device flow), through the API.

github.com and api.github.com are the fake of fake_github_http.py, on the test clock;
the token is kept by the production AppAuth in a 0600 file. gh is switched off with
AITEAM_FEEDBACK_GH=off where the token path is under test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import sys
from datetime import timedelta

import pytest

from aiteam.feedback import auth as auth_module
from aiteam.feedback.auth import Credentials, KeychainStore, SecretToolStore
from aiteam.feedback.github import GitHub, GitHubError, Timeouts

from .conftest import APP_LOGIN, BUG, GH_LOGIN, IDEA, REPO, authorize, without_gh

DIALOG = {**BUG, "dialog": True}


def _app(stack) -> dict:
    return stack.gh.world()["app"]


def _script(stack, *outcomes: str, interval: int | None = None) -> None:
    world = stack.gh.world()
    world["app"]["script"] = list(outcomes)
    if interval is not None:
        world["app"]["interval"] = interval
    stack.gh.save(world)


async def _confirm(stack, draft: dict) -> None:
    body = {"confirm_method": "elicitation", "action": "accept", "content_hash": draft["content_hash"]}
    response = await stack.client.post(f"/api/feedback/drafts/{draft['draft_id']}/confirm", json=body)
    assert response.status_code == 200, response.text


async def _start(stack, draft_id: str) -> dict:
    response = await stack.client.post("/api/feedback/github/device", json={"draft_id": draft_id})
    assert response.status_code == 200, response.text
    return response.json()


async def _poll(stack, flow_id: str) -> dict:
    return (await stack.client.post(f"/api/feedback/github/device/{flow_id}/poll")).json()


# -- which way a draft goes -------------------------------------------------------


async def test_gh_comes_first_even_with_a_token(stack) -> None:
    await authorize(stack.auth)
    assert (await stack.draft(**DIALOG))["route"] == "gh"


async def test_without_gh_a_stored_token_publishes(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    await authorize(stack.auth)
    draft = await stack.draft(**DIALOG)
    assert draft["route"] == "app"
    assert (await stack.submit(draft["draft_id"], draft["content_hash"])).json()["status"] == "sent"
    (issue,) = stack.gh.repo()["issues"]
    assert issue["user"]["login"] == APP_LOGIN
    assert all(call["via"] != "gh" for call in stack.gh.calls())  # gh never ran


@pytest.mark.parametrize(("dialog", "route"), [(True, "auth"), (False, "web")], ids=["dialog", "no-dialog"])
async def test_with_neither_a_dialog_client_authorizes_and_another_goes_to_the_web(stack, monkeypatch,
                                                                                  dialog, route) -> None:
    without_gh(monkeypatch)
    draft = await stack.draft(**{**BUG, "dialog": dialog})
    # Before authorizing nothing could be searched: the agent says so instead of listing similar posts.
    assert draft["route"] == route and draft["search_failed"] is (route == "auth")
    if route == "web":
        assert (await stack.client.post("/api/feedback/github/device", json={"draft_id": draft["draft_id"]})
                ).status_code == 200  # the code can be asked for, but submit refuses a web draft
        assert (await stack.submit(draft["draft_id"], draft["content_hash"])).status_code == 409


async def test_the_app_reaches_only_the_repositories_it_is_installed_on(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    await authorize(stack.auth)
    world = stack.gh.world()
    world["app"]["installed"] = []
    stack.gh.save(world)
    response = await stack.client.post("/api/feedback/drafts", json=DIALOG)
    assert response.status_code == 404 and REPO in response.json()["detail"]


async def test_a_revoked_token_is_dropped_and_the_next_draft_asks_again(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    await authorize(stack.auth)
    world = stack.gh.world()
    world["app"]["tokens"] = {}  # the user revoked the App on GitHub
    stack.gh.save(world)
    assert (await stack.draft(**DIALOG))["route"] == "auth"
    assert await stack.auth.store.load() is None


# -- the device flow ----------------------------------------------------------------


async def test_authorizing_then_submitting_publishes_as_the_user(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    _script(stack, "pending", "ok")
    draft = await stack.draft(**DIALOG)
    assert draft["route"] == "auth"
    await _confirm(stack, draft)
    assert stack.ledger(draft["draft_id"])["confirm_method"] == "elicitation"
    start = await _start(stack, draft["draft_id"])
    assert set(start) == {"success", "flow_id", "user_code", "verification_uri", "expires_in", "interval",
                          "existing"}  # never the device code
    assert start["verification_uri"] == "https://github.com/login/device" and not start["existing"]
    again = await _start(stack, draft["draft_id"])
    assert (again["existing"], again["user_code"], again["flow_id"]) == (True, start["user_code"], start["flow_id"])
    assert (await _poll(stack, start["flow_id"]))["status"] == "pending"
    ok = await _poll(stack, start["flow_id"])
    assert (ok["status"], ok["login"]) == ("ok", APP_LOGIN)
    sent = (await stack.submit(draft["draft_id"], draft["content_hash"])).json()
    assert sent["status"] == "sent", sent
    row = stack.ledger(draft["draft_id"])
    assert (row["route"], row["gh_login"], row["repository_id"]) == ("app", APP_LOGIN, "R_acme")
    assert stack.gh.repo()["issues"][0]["user"]["login"] == APP_LOGIN
    (event,) = stack.events("feedback.submitted")
    assert event["auth"] == "app"


async def test_submitting_before_authorizing_is_refused_and_sends_nothing(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    draft = await stack.draft(**DIALOG)
    assert (await stack.submit(draft["draft_id"], draft["content_hash"])).status_code == 409
    assert stack.gh.repo()["issues"] == [] and stack.ledger(draft["draft_id"])["status"] == "draft"


async def test_slow_down_lengthens_the_wait_and_github_is_never_asked_early(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    _script(stack, "slow_down", "ok", interval=5)
    draft = await stack.draft(**DIALOG)
    start = await _start(stack, draft["draft_id"])
    assert start["interval"] == 5
    first = await _poll(stack, start["flow_id"])
    assert (first["status"], first["interval"]) == ("pending", 10)
    for seconds in (0, 9):  # too early: answered here, GitHub is not asked
        stack.clock.advance(seconds=seconds)
        assert (await _poll(stack, start["flow_id"]))["status"] == "pending"
    assert len(stack.gh.calls("oauth:poll")) == 1
    stack.clock.advance(seconds=1)
    assert (await _poll(stack, start["flow_id"]))["status"] == "ok"
    assert len(stack.gh.calls("oauth:poll")) == 2  # only the polls at or after the interval reached GitHub


@pytest.mark.parametrize(("script", "advance", "status"), [(["denied"], 0, "denied"), (["pending"], 900, "expired")],
                         ids=["denied", "expired"])
async def test_a_denied_or_expired_authorization_leaves_the_draft(stack, monkeypatch, script, advance,
                                                                  status) -> None:
    without_gh(monkeypatch)
    _script(stack, *script)
    draft = await stack.draft(**DIALOG)
    start = await _start(stack, draft["draft_id"])
    stack.clock.advance(seconds=advance)
    assert (await _poll(stack, start["flow_id"]))["status"] == status
    assert stack.ledger(draft["draft_id"])["status"] == "draft" and stack.gh.repo()["issues"] == []
    assert await stack.auth.store.load() is None


# -- the token ---------------------------------------------------------------------


async def test_a_token_near_its_end_is_refreshed_once_and_the_old_pair_retires(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    await authorize(stack.auth)
    before = await stack.auth.store.load()
    stack.clock.advance(hours=8)
    draft = await stack.draft(**IDEA)
    assert (await stack.submit(draft["draft_id"], draft["content_hash"])).json()["status"] == "sent"
    after = await stack.auth.store.load()
    assert after.access_token != before.access_token and after.refresh_token != before.refresh_token
    assert len(stack.gh.calls("oauth:refresh")) == 1
    app = _app(stack)
    assert app["refresh"][before.refresh_token]["used"] and before.access_token not in app["tokens"]


async def test_concurrent_requests_spend_one_refresh_token_once(stack) -> None:
    await authorize(stack.auth)
    stack.clock.advance(hours=8)
    tokens = await asyncio.gather(*(stack.auth.token() for _ in range(16)))
    assert all(t is not None for t in tokens) and len({t.access_token for t in tokens}) == 1
    assert len(stack.gh.calls("oauth:refresh")) == 1
    assert (await stack.auth.store.load()).access_token == tokens[0].access_token


async def test_a_spent_refresh_token_means_authorizing_again(stack) -> None:
    await authorize(stack.auth)
    world = stack.gh.world()
    for held in world["app"]["refresh"].values():
        held["used"] = True
    stack.gh.save(world)
    stack.clock.advance(hours=8)
    assert await stack.auth.token() is None and await stack.auth.store.load() is None


async def test_the_file_store_is_private(stack) -> None:
    await authorize(stack.auth)
    path = stack.data_dir / "feedback" / "github-token.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    credentials = await stack.auth.store.load()
    assert credentials.access_token not in repr(credentials) and credentials.refresh_token not in repr(credentials)


# -- nothing leaks -------------------------------------------------------------------


async def test_no_token_or_device_code_reaches_an_answer_a_log_or_the_database(stack, monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    without_gh(monkeypatch)
    answers: list[str] = []
    draft = await stack.draft(**DIALOG)
    await _confirm(stack, draft)
    start = await _start(stack, draft["draft_id"])
    answers.append(json.dumps(start))
    answers.append(json.dumps(await _poll(stack, start["flow_id"])))
    answers.append((await stack.submit(draft["draft_id"], draft["content_hash"])).text)
    answers.append((await stack.client.get("/api/feedback/github/account")).text)
    answers.append((await stack.client.get(f"/api/feedback/drafts/{draft['draft_id']}")).text)
    answers.append((await stack.client.get("/api/feedback")).text)
    credentials = await stack.auth.store.load()
    connection = sqlite3.connect(stack.database)
    dump = "\n".join(connection.iterdump())
    connection.close()
    secrets = [credentials.access_token, credentials.refresh_token]
    for text in [*answers, caplog.text, dump]:
        assert not any(secret in text for secret in secrets)
        assert "ghu_" not in text and "ghr_" not in text and "device_code" not in text


async def test_the_account_says_who_and_signs_out_without_revealing_the_token(stack, monkeypatch) -> None:
    without_gh(monkeypatch)
    assert (await stack.client.get("/api/feedback/github/account")).json()["data"]["route"] == "none"
    await authorize(stack.auth)
    account = (await stack.client.get("/api/feedback/github/account")).json()["data"]
    assert (account["route"], account["app_login"], account["store"]) == ("app", APP_LOGIN, "file")
    assert "AITEAM_FEEDBACK_GH=off" in account["gh_reason"] and "ghu_" not in json.dumps(account)
    out = (await stack.client.delete("/api/feedback/github/account")).json()["data"]
    assert out["signed_out"] is True and out["revoke_page"] == auth_module.REVOKE_PAGE
    assert (await stack.client.get("/api/feedback/github/account")).json()["data"]["route"] == "none"


async def test_with_gh_logged_in_the_account_names_it(stack) -> None:
    assert (await stack.client.get("/api/feedback/github/account")).json()["data"]["gh_login"] == GH_LOGIN


async def test_a_gh_path_that_is_not_there_counts_as_no_gh(stack, monkeypatch, tmp_path) -> None:
    """AITEAM_GH_BIN at a missing file: no gh (the API keeps running), the reason names the variable."""
    monkeypatch.setenv("AITEAM_GH_BIN", str(tmp_path / "nowhere" / "gh"))
    await authorize(stack.auth)
    assert (await stack.draft(**DIALOG))["route"] == "app"
    account = (await stack.client.get("/api/feedback/github/account")).json()["data"]
    assert account["route"] == "app" and "AITEAM_GH_BIN" in account["gh_reason"]


async def test_issue_search_must_say_is_issue(stack) -> None:
    await authorize(stack.auth)
    github = GitHub(REPO, stack.auth.transport(await stack.auth.token()), Timeouts())
    with pytest.raises(GitHubError) as refused:
        await github._rest("GET", "search/issues?q=repo%3Aacme%2Fos+button", None, 5)
    assert refused.value.kind == "invalid"
    assert (await github._rest("GET", "search/issues?q=repo%3Aacme%2Fos+is%3Aissue+button", None, 5))[
        "total_count"] == 0
    assert await github.search_marker("h=000000000000") is None  # the marker search says is:issue


# -- the keyrings, through stand-in programs ------------------------------------------

_KEYRING = '''#!/bin/sh
exec "{python}" -c '
import json, os, sys
log, store = os.environ["FAKE_KEYRING_LOG"], os.environ["FAKE_KEYRING_STORE"]
data = sys.stdin.read() if not sys.stdin.isatty() else ""
with open(log, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "stdin": data}}) + "\\n")
args = sys.argv[1:]
if args[:1] == ["-i"]:
    words = data.split()
    secret = words[words.index("-w") + 1]
elif args[:1] == ["store"]:
    secret = data
elif args[:1] in (["find-generic-password"], ["lookup"]):
    if not os.path.exists(store):
        sys.exit(44)
    sys.stdout.write(open(store).read() + "\\n")
    sys.exit(0)
else:
    if not os.path.exists(store):
        sys.exit(44)
    os.remove(store)
    sys.exit(0)
open(store, "w").write(secret)
' "$@"
'''


@pytest.mark.parametrize("kind", ["keychain", "secret-tool"])
async def test_keyring_stores_take_the_secret_on_standard_input_only(tmp_path, monkeypatch, kind) -> None:
    program = tmp_path / kind
    program.write_text(_KEYRING.format(python=sys.executable))
    program.chmod(0o755)
    monkeypatch.setenv("FAKE_KEYRING_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("FAKE_KEYRING_STORE", str(tmp_path / "secret"))
    store = KeychainStore(security=str(program)) if kind == "keychain" else SecretToolStore(str(program))
    credentials = Credentials("ghu_" + "a" * 36, "ghr_" + "b" * 76, auth_module.utc_now() + timedelta(hours=8),
                              None, APP_LOGIN, "client")
    await store.save(credentials)
    assert await store.load() == credentials
    calls = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert not any("ghu_" in " ".join(c["argv"]) or "ghr_" in " ".join(c["argv"]) for c in calls)
    assert await store.delete() is True and await store.load() is None
    assert os.path.exists(tmp_path / "secret") is False
