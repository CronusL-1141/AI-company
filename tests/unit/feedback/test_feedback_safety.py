"""What never leaves the machine: secrets (every production rule) and private terms, end to end.

A refused draft keeps no text in the ledger and never runs gh. The samples are the
secret scan's own corpus, so a rule added there is tested here too.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from aiteam.assets.secret_scan import CORPUS
from aiteam.feedback import redact

from .conftest import BUG, IDEA

_FIELDS = ("title", "body", "steps", "original")
_MUST_HIT = [pytest.param(sample, field, id=f"{entry.rule}-{n}-{field}")
             for entry in CORPUS for n, sample in enumerate(entry.must_hit) for field in _FIELDS]
_MUST_NOT_HIT = [pytest.param(sample, id=f"{entry.rule}-{n}")
                 for entry in CORPUS for n, sample in enumerate(entry.must_not_hit)]


def _with(field: str, sample: str) -> dict:
    body = dict(BUG)
    body[field] = f"{body.get(field, '')}\n{sample}".strip() if field != "title" else f"{body['title']} {sample}"
    return body


@pytest.mark.parametrize(("sample", "field"), _MUST_HIT)
async def test_every_secret_sample_in_every_field_is_refused_without_a_trace(stack, sample, field) -> None:
    answer = await stack.draft(**_with(field, sample))
    assert answer["status"] == "refused", answer
    assert {r["field"] for r in answer["refusal"]} == {field}
    row = stack.ledger(answer["draft_id"])
    assert (row["status"], row["title"], row["body"], json.loads(row["fields"]), json.loads(row["diagnostics"])) \
        == ("refused", "", "", {}, {})
    assert sample not in json.dumps(row, default=str)
    (event,) = stack.events("feedback.refused")
    assert sample not in json.dumps(event) and event["refusal"] == json.loads(row["refusal"])
    assert stack.gh.calls() == []


@pytest.mark.parametrize("sample", _MUST_NOT_HIT)
async def test_text_the_scan_must_leave_alone_is_drafted(stack, sample) -> None:
    answer = await stack.draft(**{**BUG, "body": f"{BUG['body']}\n{sample}"})
    assert answer["status"] == "draft", answer


async def test_the_whole_text_is_scanned_again_after_the_diagnostics_are_added(stack) -> None:
    # A value from the client's handshake is not one of the user's fields: only the
    # second scan, over the text as published, sees it.
    token = "gh" + "p_" + "Q7mZ2kX9pL4vR8nB3cT6wY1hJ5dF0gSabcdef"
    answer = await stack.draft(**{**BUG, "host_version": token})
    assert answer["status"] == "refused" and answer["refusal"][0]["rule"] == "github_token"
    assert stack.ledger(answer["draft_id"])["body"] == "" and stack.gh.calls() == []


async def test_a_private_term_refuses_and_the_list_never_reaches_github(stack) -> None:
    terms = ["Zephyr-Protocol", "星图"]
    (stack.data_dir / redact.TERMS_FILE).write_text("# mine\n" + "\n".join(terms) + "\n", encoding="utf-8")
    refused = await stack.draft(**{**IDEA, "body": "Make zephyr-protocol work.\n用于星图计划"})
    assert refused["status"] == "refused"
    assert [(r["field"], r["line"], r["term"]) for r in refused["refusal"]] == [
        ("body", 1, "zephyr-protocol"), ("body", 2, "星图")]
    row = stack.ledger(refused["draft_id"])
    assert row["body"] == "" and stack.gh.calls() == []
    assert all("term" not in stored for stored in json.loads(row["refusal"]))
    assert "zephyr" not in row["refusal"].lower() and "星图" not in row["refusal"]
    drafted = await stack.draft(**IDEA)
    await stack.submit(drafted["draft_id"], drafted["content_hash"])
    sent = json.dumps(stack.gh.calls()) + json.dumps(stack.gh.world())
    assert stack.gh.repo()["discussions"] and not any(term.lower() in sent.lower() for term in terms)


async def test_an_unusable_term_list_stops_drafting(stack) -> None:
    (stack.data_dir / redact.TERMS_FILE).write_text(
        "\n".join(f"t{n}" for n in range(redact.MAX_TERMS + 1)), encoding="utf-8")
    response = await stack.client.post("/api/feedback/drafts", json=BUG)
    assert response.status_code == 400 and "500" in response.json()["detail"]


async def test_personal_details_are_rewritten_before_the_preview(stack) -> None:
    project = "/work/secret-client/app"
    body = (f"Log in {stack.data_dir}/debug.log, user alice on alice-mbp.local, mail alice@example.com, "
            f"ran in {project}/src")
    response = await stack.client.post("/api/feedback/drafts", json={**BUG, "body": body},
                                       headers={"X-Project-Dir": urllib.parse.quote(project)})
    answer = response.json()
    assert ("Log in <aiteam-data>/debug.log, user <user> on <host>, mail <email>, ran in <project>/src"
            in answer["body"])
    assert answer["rewrites"] == {"aiteam-data": 1, "email": 1, "host": 1, "project": 1, "user": 1}
    assert "secret-client" not in json.dumps(stack.ledger(answer["draft_id"]), default=str)


async def test_invisible_characters_are_refused_as_input(stack) -> None:
    response = await stack.client.post("/api/feedback/drafts", json={**BUG, "body": "hidden​word"})
    assert response.status_code == 200 and response.json()["success"] is False
    assert stack.rows("SELECT count(*) FROM feedback_submissions") == [(0,)]
