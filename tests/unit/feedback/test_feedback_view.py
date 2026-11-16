"""The read-only full-text page the confirmation dialog links to (clients fold a long dialog).

The page shows exactly the stored text, escaped, and nothing on it can run or send:
no script, form or button, GET only, and reading it changes no row.
"""

from __future__ import annotations

import html
import re

from aiteam.storage.feedback import FeedbackRepository

from .conftest import BUG

_PRE = re.compile(r"<pre>(.*)</pre>", re.DOTALL)


def _page_text(page: str) -> str:
    return html.unescape(_PRE.search(page).group(1))


async def test_the_page_shows_the_stored_text_byte_for_byte(stack) -> None:
    draft = await stack.draft(**{**BUG, "body": BUG["body"] + "\n  indented & <tagged> \"quoted\" 'text'\n\n中文行"})
    response = await stack.client.get(f"/api/feedback/drafts/{draft['draft_id']}/view")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    row = stack.ledger(draft["draft_id"])
    assert _page_text(response.text).encode("utf-8") == row["body"].encode("utf-8")
    assert html.escape(row["title"]) in response.text and row["content_hash"] in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_markup_in_the_text_is_escaped_and_never_runs(stack) -> None:
    # The API's input guard already refuses a <script> in a request, so the row is
    # written straight into the ledger: the page must be safe whatever the row holds.
    body = "Steps:\n<script>alert('x')</script>\n<img src=x onerror=alert(1)>"
    await FeedbackRepository(stack.repo.db_url).insert(
        id="d-markup", kind="bug", mode="new", target_repo="acme/os", title="Broken <b>bold</b> title",
        body=body, content_hash="a" * 64, status="draft", created_at=stack.clock())
    page = (await stack.client.get("/api/feedback/drafts/d-markup/view")).text
    assert "<script" not in page.lower() and "<img" not in page.lower() and "<b>" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    assert _page_text(page) == body


async def test_the_page_has_no_way_to_write(stack) -> None:
    draft = await stack.draft(**BUG)
    path = f"/api/feedback/drafts/{draft['draft_id']}/view"
    page = (await stack.client.get(path)).text.lower()
    assert not any(tag in page for tag in ("<form", "<button", "<input", "<script", "<a "))
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (await stack.client.request(method, path)).status_code == 405, method


async def test_a_missing_lapsed_or_refused_draft_is_not_found_and_reading_changes_nothing(stack) -> None:
    assert (await stack.client.get("/api/feedback/drafts/no-such-draft/view")).status_code == 404
    refused = await stack.draft(**{**BUG, "body": "token " + "gh" + "p_" + "Q7mZ2kX9pL4vR8nB3cT6wY1hJ5dF0gSabcdef"})
    assert (await stack.client.get(f"/api/feedback/drafts/{refused['draft_id']}/view")).status_code == 404
    draft = await stack.draft(**BUG)
    before = stack.ledger(draft["draft_id"])
    stack.clock.advance(minutes=30, seconds=1)
    assert (await stack.client.get(f"/api/feedback/drafts/{draft['draft_id']}/view")).status_code == 404
    assert stack.ledger(draft["draft_id"]) == before  # still a draft: the page never settles a row
