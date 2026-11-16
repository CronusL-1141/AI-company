"""Drafting and publishing through the API, through gh (a real child process, fake_gh.py) and,
where marked ``any_stack``, again through OS's GitHub App token (fake_github_http.py).

Every outcome is read back from the database and from the stand-in's GitHub, not
from the response alone.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import httpx

from aiteam.feedback import forms

from .conftest import APP_LOGIN, BUG, GH_LOGIN, IDEA, IDEAS_ID, REPO, REPO_ID, world_state


async def test_a_new_bug_is_published_exactly_as_previewed_and_recorded(any_stack) -> None:
    stack = any_stack
    draft = await stack.draft(**BUG)
    via = draft["route"]
    assert draft["status"] == "draft" and via in ("gh", "app"), draft
    response = await stack.submit(draft["draft_id"], draft["content_hash"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    (issue,) = stack.gh.repo()["issues"]
    assert issue["title"] == draft["title"] and issue["body"] == draft["body"]
    login = GH_LOGIN if via == "gh" else APP_LOGIN
    assert issue["user"]["login"] == login
    (create,) = stack.gh.calls("create_issue")
    assert create["via"] == via
    if via == "gh":
        assert json.loads(create["stdin"]) == {"title": draft["title"], "body": draft["body"], "labels": ["bug"]}
        assert draft["body"] not in " ".join(create["argv"])  # the text goes on standard input only
    row = stack.ledger(draft["draft_id"])
    assert (row["status"], row["remote_number"], row["remote_url"]) == ("sent", issue["number"], issue["html_url"])
    assert (row["gh_login"], row["confirm_method"], row["session_id"], row["route"]) == (
        login, "elicitation", "session-1", via)
    assert row["confirmed_at"] and row["sent_at"]
    (event,) = stack.events("feedback.submitted")
    assert (event["draft_id"], event["remote_url"], event["confirm_method"], event["gh_login"], event["auth"]) == (
        draft["draft_id"], issue["html_url"], "elicitation", login, via)


async def test_the_bug_label_is_dropped_for_a_non_collaborator_and_kept_for_one(stack) -> None:
    first = await stack.draft(**BUG)
    assert (await stack.submit(first["draft_id"], first["content_hash"])).json()["labels"] == []
    stack.gh.set(collaborator=True)
    second = await stack.draft(**{**BUG, "title": "Another broken button"})
    assert (await stack.submit(second["draft_id"], second["content_hash"])).json()["labels"] == ["bug"]
    # Both carry the marker the repository's workflow labels by.
    assert all("<!-- aiteam-feedback:v1 form=bug_report " in issue["body"] for issue in stack.gh.repo()["issues"])


async def test_a_new_idea_goes_to_ideas_with_the_ids_found_when_drafting(any_stack) -> None:
    stack = any_stack
    draft = await stack.draft(**IDEA)
    row = stack.ledger(draft["draft_id"])
    assert (row["repository_id"], row["category_id"]) == (REPO_ID, IDEAS_ID)
    assert (await stack.submit(draft["draft_id"], draft["content_hash"])).json()["status"] == "sent"
    (mutation,) = stack.gh.calls("CreateDiscussion")
    if mutation["via"] == "gh":
        sent = json.loads(mutation["stdin"])["variables"]["input"]
        assert (sent["repositoryId"], sent["categoryId"], sent["body"]) == (REPO_ID, IDEAS_ID, draft["body"])
    (discussion,) = stack.gh.repo()["discussions"]
    assert (discussion["category_id"], discussion["body"]) == (IDEAS_ID, draft["body"])
    assert stack.ledger(draft["draft_id"])["remote_url"] == discussion["url"]
    assert not stack.gh.calls("create_issue")


async def test_a_comment_on_an_issue_or_a_discussion_creates_nothing_new(any_stack) -> None:
    stack = any_stack
    issue = stack.gh.add_issue("Old report")
    discussion = stack.gh.add_discussion("Old idea")
    for target, op in ((f"issue:{issue['number']}", "comment_issue"),
                       (f"discussion:{discussion['number']}", "AddDiscussionComment")):
        draft = await stack.draft(kind="bug", body=f"Same here ({target}).", comment_on=target)
        assert "### " not in draft["body"] and "form=comment" in draft["body"]
        assert (await stack.submit(draft["draft_id"], draft["content_hash"])).json()["status"] == "sent"
        assert len(stack.gh.calls(op)) == 1
    repo = stack.gh.repo()
    assert len(repo["issues"]) == 1 and len(repo["discussions"]) == 1
    assert len(repo["issues"][0]["comments"]) == 1 and len(repo["discussions"][0]["comments"]) == 1
    assert not stack.gh.calls("create_issue") and not stack.gh.calls("CreateDiscussion")


async def test_a_comment_on_a_post_that_does_not_exist_is_refused(stack) -> None:
    response = await stack.client.post("/api/feedback/drafts", json={"kind": "bug", "body": "x",
                                                                     "comment_on": "issue:99"})
    assert response.status_code == 404


async def test_without_gh_the_draft_goes_to_the_web_and_gh_is_never_run(stack, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AITEAM_GH_BIN")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("aiteam.executables.PATH_EXTRA_DIRS", ())
    draft = await stack.draft(**BUG)
    assert draft["route"] == "web" and "gh" in draft["route_reason"]
    response = await stack.submit(draft["draft_id"], draft["content_hash"])
    assert response.status_code == 409  # only the web can publish it
    handed = (await stack.client.post(f"/api/feedback/drafts/{draft['draft_id']}/handoff",
                                      json={"reason": "gh 不可用"})).json()
    url = urllib.parse.urlsplit(handed["url"])
    query = dict(urllib.parse.parse_qsl(url.query))
    assert url.path == f"/{REPO}/issues/new" and query["template"] == "bug_report.yml"
    assert "labels" not in query  # without the permission GitHub answers the whole page with 404
    assert (query["title"], query["what-happened"], query["repro"]) == (BUG["title"], BUG["body"], BUG["steps"])
    assert stack.ledger(draft["draft_id"])["status"] == "handed_off"
    assert stack.gh.calls() == []


async def test_an_idea_handed_off_prefills_the_ideas_form(stack, monkeypatch) -> None:
    stack.gh.set(login=None)
    draft = await stack.draft(**IDEA)
    assert draft["route"] == "web" and "unauthenticated" in draft["route_reason"]
    handed = (await stack.client.post(f"/api/feedback/drafts/{draft['draft_id']}/handoff",
                                      json={"reason": "not logged in"})).json()
    url = urllib.parse.urlsplit(handed["url"])
    query = dict(urllib.parse.parse_qsl(url.query))
    assert url.path == f"/{REPO}/discussions/new" and query["category"] == "ideas"
    assert (query["problem"], query["proposal"]) == (IDEA["body"], IDEA["proposal"])


async def test_a_bad_token_goes_to_the_web_with_the_reason(stack) -> None:
    stack.gh.set(token="invalid")
    draft = await stack.draft(**BUG)
    assert draft["route"] == "web" and "unauthenticated" in draft["route_reason"]


async def test_a_token_without_write_access_fails_the_send_and_points_to_the_web(stack) -> None:
    stack.gh.set(token="forbidden")
    draft = await stack.draft(**BUG)
    assert draft["route"] == "gh"  # reading works
    answer = (await stack.submit(draft["draft_id"], draft["content_hash"])).json()
    assert (answer["status"], answer["error_kind"], answer["fallback"]) == ("failed", "forbidden", "web")
    assert stack.ledger(draft["draft_id"])["claim_hash"] == ""
    handed = await stack.client.post(f"/api/feedback/drafts/{draft['draft_id']}/handoff", json={"reason": "403"})
    assert handed.json()["status"] == "handed_off"
    assert stack.gh.repo()["issues"] == []


async def test_a_page_too_long_to_prefill_gets_the_text_in_a_local_file(stack) -> None:
    long_body = "The button stays grey.\n" * 400  # ~9,200 characters: over the URL limit once encoded
    stack.gh.set(login=None)
    draft = await stack.draft(**{**BUG, "body": long_body})
    handed = (await stack.client.post(f"/api/feedback/drafts/{draft['draft_id']}/handoff",
                                      json={"reason": "web"})).json()
    assert handed.get("url"), handed
    assert forms.fits(handed["url"]) and "what-happened" not in handed["url"]
    path = Path(handed["file"])
    assert path.parent == stack.data_dir / "feedback" and oct(path.stat().st_mode & 0o777) == "0o600"
    text = path.read_text()
    assert long_body.strip() in text and "Steps to reproduce:" in text


async def test_a_lookup_that_fails_still_previews_and_says_it_could_not_check(stack) -> None:
    stack.gh.set(search_down=True)
    draft = await stack.draft(**BUG)
    assert draft["status"] == "draft" and draft["search_failed"] is True and draft["route"] == "web"
    assert draft["body"]


async def test_similar_posts_are_listed_from_github_and_from_this_ledger(stack) -> None:
    stack.gh.add_issue("Task wall button broken again")
    first = await stack.draft(**BUG)
    assert [item["title"] for item in first["similar"]] == ["Task wall button broken again"]
    assert "OR" in first["searched_github_for"]
    await stack.submit(first["draft_id"], first["content_hash"])
    second = await stack.draft(**{**BUG, "title": "Task wall button still broken"})
    assert [item["title"] for item in second["similar_sent_here"]] == [BUG["title"]]


async def test_without_an_ideas_category_an_idea_can_only_go_to_the_web(stack) -> None:
    world = world_state()
    world["repos"][REPO]["categories"] = [c for c in world["repos"][REPO]["categories"] if c["slug"] != "ideas"]
    stack.gh.save(world)
    draft = await stack.draft(**IDEA)
    assert draft["route"] == "web" and "Ideas" in draft["route_reason"]
    assert (await stack.submit(draft["draft_id"], draft["content_hash"])).status_code == 409


async def test_security_reports_and_questions_are_never_filed(stack) -> None:
    security = await stack.draft(kind="security", body="Bypass of the guard.")
    assert security["status"] == "not_filed" and security["url"].endswith(f"/{REPO}/security/advisories/new")
    question = await stack.draft(kind="question", body="How do I use it?")
    assert question["status"] == "not_filed" and question["url"].endswith("/discussions/categories/q-a")
    assert stack.rows("SELECT count(*) FROM feedback_submissions") == [(0,)]
    assert stack.gh.calls() == []


async def test_only_the_dialog_confirmation_is_accepted(stack) -> None:
    draft = await stack.draft(**BUG)
    for body in ({"confirm_method": "user_quote", "action": "accept", "content_hash": draft["content_hash"]},
                 {"confirm_method": "elicitation", "action": "accept", "content_hash": draft["content_hash"],
                  "user_quote": "the user said yes"},
                 {"confirm_method": "elicitation", "action": "decline", "content_hash": draft["content_hash"]}):
        response = await stack.client.post(f"/api/feedback/drafts/{draft['draft_id']}/submit", json=body)
        assert response.status_code == 422, body
    assert stack.ledger(draft["draft_id"])["status"] == "draft" and not stack.gh.calls("create_issue")


async def test_the_list_shows_summaries_without_text(stack) -> None:
    draft = await stack.draft(**BUG)
    await stack.submit(draft["draft_id"], draft["content_hash"])
    (row,) = (await stack.client.get("/api/feedback")).json()["data"]
    assert (row["id"], row["status"], row["title"]) == (draft["draft_id"], "sent", BUG["title"])
    assert "body" not in row
    assert (await stack.client.get("/api/feedback", params={"status": "draft"})).json()["data"] == []
    assert (await stack.client.get("/api/feedback", params={"limit": 101})).status_code == 422


async def test_the_production_wiring_builds_the_service_over_the_requests_database(stack, monkeypatch) -> None:
    """Only the repository and the event bus are replaced: the service, its target and its
    folder come from the production dependency (AITEAM_FEEDBACK_REPO, next to the database)."""
    from aiteam.api import app as app_module
    from aiteam.api import deps

    monkeypatch.setenv("AITEAM_FEEDBACK_REPO", REPO)
    stack.gh.set(login=None)
    app = app_module.create_app()
    app.dependency_overrides.update({deps.get_repository: lambda: stack.repo, deps.get_event_bus: lambda: stack.bus})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        draft = (await client.post("/api/feedback/drafts", json={**BUG, "body": "Grey.\n" * 1500})).json()
        handed = (await client.post(f"/api/feedback/drafts/{draft['draft_id']}/handoff",
                                    json={"reason": "web"})).json()
    assert draft["destination"].startswith(REPO) and stack.ledger(draft["draft_id"])["status"] == "handed_off"
    assert Path(handed["file"]).parent == stack.database.parent / "feedback"


async def test_a_lookup_that_times_out_still_previews_on_the_web_route(tmp_path, monkeypatch, fake_gh) -> None:
    from aiteam.feedback.github import Timeouts

    from .conftest import close_stack, open_stack

    stack = await open_stack(tmp_path, monkeypatch, fake_gh, timeouts=Timeouts(login=20, lookup=1, search=20, send=20))
    try:
        fake_gh.set(hang={"FeedbackLookup": "before"})
        draft = await stack.draft(**BUG)
        assert (draft["status"], draft["search_failed"], draft["route"]) == ("draft", True, "web")
        assert "timeout" in draft["route_reason"] and draft["body"]
    finally:
        await close_stack(stack)
