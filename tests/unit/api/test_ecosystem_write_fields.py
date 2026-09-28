"""Every ecosystem write entry that stores text, field by field, over the real app.

The write-side rules of the rest of the API, applied to the ecosystem routes:
single-line fields (names, tags, topics, labels) are stored cleaned; long text an
agent or a user writes (summaries, review notes, reasons) with an invisible
character is refused the memo way (200, success false, a safety block) and nothing
is stored; long text a hook relays from a report it parsed has no author present
to refuse, so it is stored with the invisible characters dropped and the layout
kept. Each value carries a unique token so the check reads the database file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.routes import ecosystem as ecosystem_routes
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from tests.unit.api.test_write_side_fields import _clean, _dirty, _stored, _token

RLO, ZWSP, ESC = chr(0x202E), chr(0x200B), chr(0x1B)
FAMILY = chr(0x1F468) + chr(0x200D) + chr(0x1F469)


@pytest.fixture()
def eco(tmp_path, monkeypatch):
    loop = asyncio.get_event_loop()
    database = tmp_path / "eco-fields.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{database}")
    loop.run_until_complete(repo.init_db())
    deps._repository = repo

    async def no_search(*_args, **_kwargs):
        return []

    async def no_fetch(_full_name):
        return {"http_status": 0, "error_message": "offline"}

    # Scan and refresh routes stay off the network; what they store is the point.
    monkeypatch.setattr(ecosystem_routes, "default_gh_search", no_search)
    monkeypatch.setattr(ecosystem_routes, "_default_repo_fetcher", no_fetch)
    app = create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "eco-fields", "root_path": str(tmp_path)}).json()["data"]["id"]
    client.headers.update({"X-Project-Id": pid})
    rid, reviewed = (client.post("/api/ecosystem/profiles", json={"repo_full_name": f"o/{n}", "name": n, "owner": "o"})
                     .json()["id"] for n in ("r", "reviewed"))
    ids = {
        "pid": pid, "rid": rid,
        # On its own repo: a queued review would keep the lifecycle batch off "r".
        "dr": client.post("/api/ecosystem/deep_reviews", json={"repo_id": reviewed}).json()["id"],
        "run": client.post("/api/ecosystem/scan-runs", json={}).json()["id"],
        "batch": client.post("/api/ecosystem/shallow_batches", json={}).json()["batch_id"],
        "ds": client.post("/api/ecosystem/data_sources",
                          json={"kind": "github", "name": "ds", "config": {}}).json()["data_source"]["id"],
    }
    assert client.post("/api/ecosystem/tags", json={"name": "mcp-server", "category": "capability"}).status_code == 200
    # A tagged, shallow-scanned repo: a lifecycle batch has a candidate to write a prompt for.
    assert client.post("/api/ecosystem/tags/manual", json={"repo_id": rid, "tag_name": "mcp-server"}).status_code == 200
    assert client.post("/api/ecosystem/shallow_queue/apply_summary",
                       json={"repo_id": rid, "shallow_summary": "s"}).json()["success"] is True
    yield client, database, ids
    loop.run_until_complete(close_db())
    deps._repository = None


# (label, method, path template, body builder taking the dirty value)
SINGLE_LINE = [
    ("settings.focus_topics", "PUT", "/api/ecosystem/projects/{pid}/settings", lambda v: {"focus_topics": [v]}),
    ("settings.focus_languages", "PUT", "/api/ecosystem/projects/{pid}/settings",
     lambda v: {"focus_languages": [v]}),
    ("data_source.name", "POST", "/api/ecosystem/data_sources",
     lambda v: {"kind": "github", "name": v, "config": {}}),
    ("data_source_update.name", "PUT", "/api/ecosystem/data_sources/{ds}", lambda v: {"name": v}),
    ("tag.name", "POST", "/api/ecosystem/tags", lambda v: {"name": v, "category": "capability"}),
    ("tag.aliases", "POST", "/api/ecosystem/tags",
     lambda v: {"name": "t-" + v[3:7], "category": "capability", "aliases": [v]}),
    # Beyond the reported list, same class: labels and one-line goals that are stored.
    ("manual_status.set_by", "POST", "/api/ecosystem/repos/{rid}/manual_status",
     lambda v: {"status": "pinned", "set_by": v}),
    ("request_batch.research_goal", "POST", "/api/ecosystem/lifecycle/request_batch",
     lambda v: {"tags": ["mcp-server"], "min_stars": 0, "research_goal": v}),
    ("scan_run.triggered_by", "POST", "/api/ecosystem/scan-runs", lambda v: {"triggered_by": v}),
    ("scan_execute.triggered_by", "POST", "/api/ecosystem/scan-runs/execute", lambda v: {"triggered_by": v}),
    ("refresh.triggered_by", "POST", "/api/ecosystem/refresh", lambda v: {"triggered_by": v}),
    ("shallow_batch.triggered_by", "POST", "/api/ecosystem/shallow_batches", lambda v: {"triggered_by": v}),
    ("shallow_batch_approve.approved_by", "POST", "/api/ecosystem/shallow_batches/{batch}/approve",
     lambda v: {"approved_by": v}),
]

LONG_TEXT = [
    ("apply_summary.shallow_summary", "POST", "/api/ecosystem/shallow_queue/apply_summary",
     lambda v: {"repo_id": "{rid}", "shallow_summary": v}),
    ("architecture.architecture_md", "POST", "/api/ecosystem/lifecycle/apply_architecture_md",
     lambda v: {"deep_review_id": "{dr}", "architecture_md": v}),
    ("debate.risks_md", "POST", "/api/ecosystem/lifecycle/apply_debate_result",
     lambda v: {"deep_review_id": "{dr}", "risks_md": v}),
    ("debate.learnings_md", "POST", "/api/ecosystem/lifecycle/apply_debate_result",
     lambda v: {"deep_review_id": "{dr}", "learnings_md": v}),
    ("debate.integration_md", "POST", "/api/ecosystem/lifecycle/apply_debate_result",
     lambda v: {"deep_review_id": "{dr}", "integration_md": v}),
    ("quality_review.quality_notes", "POST", "/api/ecosystem/review_queue/apply",
     lambda v: {"dr_id": "{dr}", "quality_score": 80, "quality_notes": v}),
    ("manual_status.reason", "POST", "/api/ecosystem/repos/{rid}/manual_status",
     lambda v: {"status": "pinned", "reason": v}),
    ("tag.description", "POST", "/api/ecosystem/tags",
     lambda v: {"name": "d-" + v[3:7], "category": "capability", "description": v}),
    # Beyond the reported list, same class: reasons and notes an author writes.
    ("scan_run.notes", "POST", "/api/ecosystem/scan-runs", lambda v: {"notes": v}),
    ("scan_run_complete.notes", "POST", "/api/ecosystem/scan-runs/{run}/complete", lambda v: {"notes": v}),
    ("scan_execute.notes", "POST", "/api/ecosystem/scan-runs/execute", lambda v: {"notes": v}),
    ("refresh.notes", "POST", "/api/ecosystem/refresh", lambda v: {"notes": v}),
    ("shallow_batch.trigger_reason", "POST", "/api/ecosystem/shallow_batches", lambda v: {"trigger_reason": v}),
]

# A refresh writes its counts over the notes when it finishes, so the layout check
# (which reads the finished row) does not apply; the refusal check does.
OVERWRITTEN = {"refresh.notes"}
LONG_TEXT_KEPT = [case for case in LONG_TEXT if case[0] not in OVERWRITTEN]

# Relayed text, no author present to refuse: hook-relayed report sections (and the
# backfill script that replays them), the failure text a worker reports (refusing it
# would leave the failure unrecorded and the claim held), and the error lines
# ecosystem_scan collected.
RELAYED = [
    (f"{route}.{field}", "POST", f"/api/ecosystem/deep_reviews/{{dr}}/{route}",
     (lambda f: lambda v: {"report_id": "rep-1", f: v})(field))
    for route in ("link_report", "backfill")
    for field in ("summary_md", "architecture_md", "risks_md", "learnings_md", "integration_md",
                  "demo_log_excerpt")
] + [
    ("apply_summary.error_message", "POST", "/api/ecosystem/shallow_queue/apply_summary",
     lambda v: {"repo_id": "{rid}", "error_kind": "http", "http_status": 500, "error_message": v}),
    ("architecture.error_message", "POST", "/api/ecosystem/lifecycle/apply_architecture_md",
     lambda v: {"deep_review_id": "{dr}", "error_message": v}),
    ("release_claim.reason", "POST", "/api/ecosystem/claims/release", lambda v: {"dr_id": "{dr}", "reason": v}),
    ("scan_run_complete.errors", "POST", "/api/ecosystem/scan-runs/{run}/complete", lambda v: {"errors": [v]}),
]


def _send(client, ids, method, path, body):
    body = json.loads(json.dumps(body).replace('"{rid}"', json.dumps(ids["rid"]))
                      .replace('"{dr}"', json.dumps(ids["dr"])))
    return client.request(method, path.format(**ids), json=body)


@pytest.mark.parametrize("case", SINGLE_LINE, ids=[c[0] for c in SINGLE_LINE])
def test_single_line_fields_are_stored_cleaned(case, eco):
    client, database, ids = eco
    label, method, path, body = case
    token = _token()
    resp = _send(client, ids, method, path, body(_dirty(token)))
    assert resp.status_code < 300 and resp.json().get("success") is not False, f"{label}: {resp.text[:200]}"
    stored = _stored(database, token)
    assert stored, f"{label}: nothing stored"
    for text in stored:
        assert _clean(token) in text, f"{label}: stored {text[:160]!a}"
        assert RLO not in text and ZWSP not in text and ESC not in text


@pytest.mark.parametrize("case", LONG_TEXT, ids=[c[0] for c in LONG_TEXT])
def test_long_text_with_an_invisible_character_is_refused_the_memo_way(case, eco):
    client, database, ids = eco
    label, method, path, body = case
    token = _token()
    resp = _send(client, ids, method, path, body(f"{token} body{RLO}hidden"))
    assert resp.status_code == 200, f"{label}: {resp.status_code} {resp.text[:200]}"
    answer = resp.json()
    assert answer.get("success") is False and answer.get("safety", {}).get("category") == "invisible_unicode", \
        f"{label}: {answer!a}"[:300]
    assert "U+202E" in answer["error"], answer
    assert _stored(database, token) == [], f"{label}: a refused body was stored"


@pytest.mark.parametrize("case", LONG_TEXT_KEPT, ids=[c[0] for c in LONG_TEXT_KEPT])
def test_long_text_keeps_its_layout_and_emoji_sequences(case, eco):
    client, database, ids = eco
    label, method, path, body = case
    token = _token()
    text = f"{token} line\tone\r\nline two {FAMILY} {chr(0x2028)} end"
    resp = _send(client, ids, method, path, body(text))
    assert resp.status_code < 300 and "safety" not in resp.json(), f"{label}: {resp.text[:200]}"
    assert any(text in stored for stored in _stored(database, token)), f"{label}: not stored as sent"


@pytest.mark.parametrize("case", RELAYED, ids=[c[0] for c in RELAYED])
def test_relayed_text_is_stored_without_invisible_characters(case, eco):
    client, database, ids = eco
    label, method, path, body = case
    token = _token()
    resp = _send(client, ids, method, path, body(f"{token} a{RLO}b{ZWSP}\n## next {FAMILY}{ESC}[0m"))
    # A failure report answers success false by design; what matters is no refusal.
    assert resp.status_code == 200 and "safety" not in resp.json(), f"{label}: {resp.text[:200]}"
    stored = _stored(database, token)
    assert stored and all(f"{token} ab\n## next {FAMILY}[0m" in text for text in stored), f"{label}: {stored!a}"


def test_a_dirty_tag_name_from_an_agent_finds_the_clean_tag(eco):
    """tags/manual and tags/llm/result look names up in the tag dictionary: cleaned first."""
    client, _, ids = eco
    resp = client.post("/api/ecosystem/tags/manual", json={"repo_id": ids["rid"], "tag_name": f"mcp-server{ZWSP}"})
    assert resp.status_code == 200 and resp.json()["tag_name"] == "mcp-server", resp.text[:200]
    resp = client.post("/api/ecosystem/tags/llm/result",
                       json={"repo_id": ids["rid"], "tags": [{"name": f"{RLO}mcp-server", "confidence": 0.9}]})
    assert resp.status_code == 200, resp.text[:200]
    assert resp.json()["layer3_tags"] == ["mcp-server"] and not resp.json()["skipped_unknown"], resp.json()


def test_a_body_with_another_error_keeps_the_422(eco):
    client, _, _ = eco
    resp = client.post("/api/ecosystem/lifecycle/apply_architecture_md",
                       json={"architecture_md": f"x{RLO}"})  # no deep_review_id
    assert resp.status_code == 422, resp.text[:200]


def test_a_topic_that_cleans_to_nothing_is_dropped(eco):
    """POST /api/ecosystem/profiles: the same rule _fetch_repo_topics applies at the gh exit."""
    client, database, _ = eco
    resp = client.post("/api/ecosystem/profiles", json={"repo_full_name": "o/topics", "name": "topics",
                                                        "owner": "o", "topics": [ZWSP, f"mcp{RLO}"]})
    assert resp.status_code == 200, resp.text[:200]
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        (topics,) = con.execute("SELECT topics FROM ecosystem_repo_profiles WHERE name = 'topics'").fetchone()
    finally:
        con.close()
    assert json.loads(topics) == ["mcp"], topics
