"""Ecosystem archive ingest end to end: what GitHub returns is cleaned before it is stored.

Nobody is present to refuse a GitHub repo description, so fetched text is cleaned,
never refused: single-line fields (full name, name, owner, language, homepage,
topics) with the single-line rule, the description with ``strip_invisible`` (drops
what ``scan_invisible`` refuses, keeps newlines, emoji ZWJ sequences and flags).
A real repo prompted this: evidentlyai/evidently's description carries two U+200B,
and it reached the archive and a deep-review dispatch prompt untouched.

Production code throughout: the MCP tools called through FastMCP (they reach the
API over HTTP), a real uvicorn on a temporary SQLite file, and the real subprocess
calls to ``gh``, answered by a stand-in ``gh`` placed first on PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from aiteam.mcp import _base
from aiteam.mcp.tools import ecosystem as ecosystem_tools
from aiteam.text_safety import scan_invisible
from tests.unit.api.test_hook_ingest_preread import hook_server  # noqa: F401 - pytest fixture
from tests.unit.api.test_write_side_text_safety_e2e import _http

RLO, ZWSP, ESC = chr(0x202E), chr(0x200B), chr(0x1B)
FAMILY = chr(0x1F468) + chr(0x200D) + chr(0x1F469)  # emoji ZWJ sequence
ENGLAND = chr(0x1F3F4) + "".join(chr(0xE0000 + ord(c)) for c in "gbeng") + chr(0xE007F)

SEARCH_HIT = {
    "fullName": f"acme/agentkit{ZWSP}",
    "name": f"agentkit{ZWSP}",
    "owner": {"login": f"acme{RLO}"},
    "description": f"Claude agent kit{RLO}evil{ZWSP}\nsecond line {FAMILY} {ENGLAND}{ESC}[31m",
    "stargazersCount": 9000,
    "language": f"Python{ESC}",
    "homepage": f"https://acme.dev{ZWSP}",
    "pushedAt": "2026-09-01T00:00:00Z",
}
TOPIC_LINES = [f"claude{ZWSP}code", "mcp"]
API_ERROR = f"gh: Not Found (HTTP 404){ESC}[0m{RLO}\n"

CLEAN_DESCRIPTION = f"Claude agent kitevil\nsecond line {FAMILY} {ENGLAND}[31m"
CLEAN_FIELDS = {
    "repo_full_name": "acme/agentkit", "name": "agentkit", "owner": "acme",
    "language": "Python", "homepage": "https://acme.dev",
}

# Stands in for the gh CLI: search, per-repo topics (--jq), a failing repo probe.
FAKE_GH = """#!{python}
import json, os, sys
args = sys.argv[1:]
data = json.load(open(os.environ["FAKE_GH_DATA"], encoding="utf-8"))
if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[:2] == ["search", "repos"]:
    print(json.dumps(data["search"]))
    sys.exit(0)
if args[:1] == ["api"] and "--jq" in args:
    print("\\n".join(data["topics"]))
    sys.exit(0)
if args[:1] == ["api"]:
    sys.stderr.write(data["api_error"])
    sys.exit(1)
sys.exit(2)
"""


def _call(name: str, args: dict) -> dict:
    mcp = FastMCP("ecosystem-ingest")
    ecosystem_tools.register(mcp)
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _rows(database: Path, sql: str, *args) -> list[tuple]:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _invisible_anywhere(database: Path) -> list[str]:
    """Every text cell in every ecosystem table that scan_invisible would refuse."""
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        hits = []
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'ecosystem%'")]
        for table in tables:
            columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            for row in con.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                hits += [f"{table}.{column}: {value!a}" for column, value in zip(columns, row)
                         if isinstance(value, str) and scan_invisible(value) is not None]
        return hits
    finally:
        con.close()


@pytest.fixture()
def eco(hook_server, tmp_path, monkeypatch):  # noqa: F811
    port, database, _ = hook_server
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AITEAM_API_URL", url)
    data = tmp_path / "fake_gh.json"
    data.write_text(json.dumps({"search": [SEARCH_HIT], "topics": TOPIC_LINES, "api_error": API_ERROR}),
                    encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.replace("{python}", sys.executable), encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_DATA", str(data))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    work = tmp_path / "work"
    work.mkdir()
    _, project = _http(url, "POST", "/api/projects", {"name": "eco", "root_path": str(work)})
    pid = project["data"]["id"]
    monkeypatch.setattr(_base, "_session_project_id", pid)
    status, saved = _http(url, "PUT", f"/api/ecosystem/projects/{pid}/settings",
                          {"min_stars": 1000, "focus_topics": ["claude-code"]})
    assert status == 200, saved
    return {"url": url, "database": database, "pid": pid}


def test_index_update_stores_github_text_cleaned_and_reads_it_back_clean(eco):
    """ecosystem_index_update: gh search result with RLO / ZWSP / ESC, read back via ecosystem_repo_get."""
    result = _call("ecosystem_index_update", {"dry_run": False})
    assert result.get("success") is True and result["diff"]["new_count"] == 1, result

    got = _call("ecosystem_repo_get", {"repo_full_name": "acme/agentkit"})
    profile = got["profile"]
    assert {key: profile[key] for key in CLEAN_FIELDS} == CLEAN_FIELDS, profile
    assert profile["description"] == CLEAN_DESCRIPTION, ascii(profile["description"])
    assert profile["topics"] == ["claude code", "mcp"], profile["topics"]

    # Across the persistence boundary: the stored row, and every text cell the scan wrote.
    row = _rows(eco["database"], "SELECT repo_full_name, name, owner, language, homepage, description, "
                "description_excerpt, one_line_summary, topics FROM ecosystem_repo_profiles")
    assert len(row) == 1, row
    *fields, description, excerpt, summary, topics = row[0]
    assert dict(zip(CLEAN_FIELDS, fields)) == CLEAN_FIELDS, row
    assert description == excerpt == summary == CLEAN_DESCRIPTION, ascii(row)
    assert json.loads(topics) == ["claude code", "mcp"], topics
    assert _invisible_anywhere(eco["database"]) == []

    # The cleaned full name is the lookup key: the same raw hit next time is the same row.
    again = _call("ecosystem_index_update", {"dry_run": False})
    assert again.get("success") is True and again["diff"]["new_count"] == 0, again
    assert _rows(eco["database"], "SELECT COUNT(*) FROM ecosystem_repo_profiles")[0][0] == 1


def test_legacy_scan_tool_and_its_profile_route_store_github_text_cleaned(eco):
    """ecosystem_scan posts each gh hit to POST /api/ecosystem/profiles; the route cleans too."""
    result = _call("ecosystem_scan", {"min_stars": 1000})
    assert result.get("new_profiles") == 1 and not result.get("errors"), result
    row = _rows(eco["database"], "SELECT repo_full_name, owner, description FROM ecosystem_repo_profiles")
    assert row == [("acme/agentkit", "acme", CLEAN_DESCRIPTION)], ascii(row)

    # The route is the write entry: a relayed body is cleaned there, not only by the tool.
    body = {"repo_full_name": f"acme/other{ZWSP}", "name": f"other{RLO}", "owner": f"acme{ESC}",
            "description": SEARCH_HIT["description"], "language": f"Go{ZWSP}",
            "topics": [f"agent{RLO}s"], "homepage": f"https://x.dev{ESC}",
            "one_line_summary": f"one{RLO}line", "description_excerpt": f"ex{ZWSP}cerpt"}
    request = urllib.request.Request(eco["url"] + "/api/ecosystem/profiles", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Project-Id": eco["pid"]})
    with urllib.request.urlopen(request, timeout=15) as response:
        assert response.status == 200
    row = _rows(eco["database"], "SELECT repo_full_name, name, owner, language, topics, homepage, description, "
                "one_line_summary, description_excerpt FROM ecosystem_repo_profiles WHERE name LIKE 'other%'")
    assert row == [("acme/other", "other", "acme", "Go", json.dumps(["agent s"]), "https://x.dev",
                    CLEAN_DESCRIPTION, "oneline", "excerpt")], ascii(row)
    assert _invisible_anywhere(eco["database"]) == []


def test_refresh_stores_the_gh_error_text_cleaned(eco):
    """ecosystem_refresh: gh's stderr for a failed probe becomes last_fetch_error, one clean line."""
    assert _call("ecosystem_index_update", {"dry_run": False}).get("success") is True
    result = _call("ecosystem_refresh", {})
    assert result.get("success") is True and result["marked_deleted"] == 1, result
    error, deleted = _rows(eco["database"], "SELECT last_fetch_error, is_deleted FROM ecosystem_repo_profiles")[0]
    assert deleted == 1 and error == "gh: Not Found (HTTP 404) [0m", ascii(error)
    assert _invisible_anywhere(eco["database"]) == []


# What an agent writes back through the ecosystem tools: the author is present, so an
# invisible character is refused (200, success false, a safety block) and the tool
# hands that answer back unchanged; nothing is stored.
AUTHORED = [
    ("ecosystem_apply_shallow_summary", lambda ids, v: {"repo_id": ids["rid"], "shallow_summary": v}),
    ("ecosystem_apply_architecture_md", lambda ids, v: {"deep_review_id": ids["dr"], "architecture_md": v}),
    ("ecosystem_apply_debate_result", lambda ids, v: {"deep_review_id": ids["dr"], "risks_md": v}),
    ("ecosystem_apply_quality_review",
     lambda ids, v: {"dr_id": ids["dr"], "quality_score": 70, "quality_notes": v}),
    ("ecosystem_repo_manual_status", lambda ids, v: {"repo_id": ids["rid"], "status": "pinned", "reason": v}),
]


def _cells_with(database: Path, token: str) -> list[str]:
    """Every text cell of every ecosystem table that contains the token."""
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        found = []
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'ecosystem%'")]
        for table in tables:
            for column in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
                found += [v for (v,) in con.execute(f"SELECT {column} FROM {table} WHERE instr({column}, ?) > 0",
                                                    (token,))]
        return found
    finally:
        con.close()


def _queued_review(eco: dict) -> dict:
    """A profile and a queued deep review for it, created the way the tools do."""
    body = {"repo_full_name": "acme/reviewed", "name": "reviewed", "owner": "acme"}
    request = urllib.request.Request(eco["url"] + "/api/ecosystem/profiles", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Project-Id": eco["pid"]})
    with urllib.request.urlopen(request, timeout=15) as response:
        rid = json.loads(response.read())["id"]
    return {"rid": rid, "dr": _call("ecosystem_deep_review_request", {"repo_id": rid})["id"]}


@pytest.mark.parametrize(("tool", "args"), AUTHORED, ids=[tool for tool, _ in AUTHORED])
def test_an_agent_write_with_an_invisible_character_comes_back_refused(eco, tool, args):
    ids = _queued_review(eco)
    token = f"TOK-{tool}"

    refused = _call(tool, args(ids, f"{token} text{RLO}hidden"))
    assert refused.get("success") is False, refused
    assert refused.get("safety", {}).get("category") == "invisible_unicode" and "U+202E" in refused["error"], refused
    assert _cells_with(eco["database"], token) == []

    accepted = _call(tool, args(ids, f"{token} text hidden"))
    assert accepted.get("success") is True, accepted
    assert any(f"{token} text hidden" in cell for cell in _cells_with(eco["database"], token))



# A worker's failure report relays failure text (gh stderr, an HTTP error, a quoted
# fragment). It is cleaned, never refused: a refused failure report answers
# success false just like a recorded one, so the worker would move on while the
# failure went unrecorded and its claim stayed held.
FAILURE = f"gh: HTTP 500{ESC}[0m while reading{RLO} README{ZWSP}\nline two"
FAILURE_CLEAN = "gh: HTTP 500[0m while reading README\nline two"


def _claimed(eco: dict) -> dict:
    """A queued review; the request creates it already claimed by its dispatch."""
    ids = _queued_review(eco)
    assert _review_row(eco, ids["dr"])[0], "the requested review should start claimed"
    return ids


def _review_row(eco: dict, dr: str) -> tuple:
    return _rows(eco["database"], "SELECT claimed_by, stage_status, risks_md, quality_notes "
                 "FROM ecosystem_deep_reviews WHERE id = ?", dr)[0]


def test_a_shallow_failure_report_with_invisible_characters_is_recorded_and_frees_the_claim(eco):
    """ecosystem_apply_shallow_summary(error_kind=...): every report counts; the budget frees the claim."""
    ids = _claimed(eco)
    for attempt in range(1, 6):  # MAX_RETRY_BUDGET failures escalate to shallow_failed
        result = _call("ecosystem_apply_shallow_summary", {
            "repo_id": ids["rid"], "deep_review_id": ids["dr"], "error_kind": "http", "http_status": 500,
            "error_message": FAILURE})
        assert "safety" not in result and result.get("failure_class"), result
        count, error = _rows(eco["database"], "SELECT fetch_failure_count, last_fetch_error "
                             "FROM ecosystem_repo_profiles WHERE id = ?", ids["rid"])[0]
        assert count == attempt and error == FAILURE_CLEAN, ascii((count, error))
    claimed_by, stage, _, _ = _review_row(eco, ids["dr"])
    assert claimed_by is None and stage == "shallow_failed", (claimed_by, stage)


def test_an_architecture_failure_with_invisible_characters_frees_the_claim(eco):
    """ecosystem_apply_architecture_md(error_message=...): the failure lands in risks_md, the claim is freed."""
    ids = _claimed(eco)
    result = _call("ecosystem_apply_architecture_md", {"deep_review_id": ids["dr"], "error_message": FAILURE})
    assert "safety" not in result and result.get("stage_status") == "architecture_failed", result
    claimed_by, stage, risks, _ = _review_row(eco, ids["dr"])
    assert claimed_by is None and stage == "architecture_failed", (claimed_by, stage)
    assert risks.endswith(f"[architecture failed] {FAILURE_CLEAN}"), ascii(risks)


def test_a_claim_released_with_invisible_characters_in_the_reason_is_freed(eco):
    """ecosystem_release_claim(reason=...): the worker gives up; the claim is always released."""
    ids = _claimed(eco)
    result = _call("ecosystem_release_claim", {"dr_id": ids["dr"], "reason": FAILURE})
    assert result.get("success") is True and "safety" not in result, result
    claimed_by, _, _, notes = _review_row(eco, ids["dr"])
    assert claimed_by is None and FAILURE_CLEAN in notes, ascii((claimed_by, notes))
