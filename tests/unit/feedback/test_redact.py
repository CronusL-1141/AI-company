"""The checks and rewrites feedback text goes through (aiteam.feedback.redact), one by one."""

from __future__ import annotations

import time

import pytest

from aiteam.feedback import redact

CONTEXT = redact.Context(
    roots=(("/work/acme/app", "<project>"), ("/Users/alice/.claude", "<claude-config>"),
           ("/Users/alice/.codex", "<codex-home>"), ("/Users/alice/.claude/data/os", "<aiteam-data>")),
    home="/Users/alice", login="alice", hostnames=("alice-mbp.local", "alice-mbp"),
    project_names=("app", "zeta-ledger", "AI Team OS"),
)


def clean(text: str, context: redact.Context = CONTEXT) -> redact.Cleaned:
    return redact.clean({"body": text}, context)


@pytest.mark.parametrize(("text", "expected", "kind"), [
    ("see /work/acme/app/src/x.py", "see <project>/src/x.py", "project"),
    ("in /Users/alice/.claude/settings.json", "in <claude-config>/settings.json", "claude-config"),
    ("db /Users/alice/.claude/data/os/aiteam.db", "db <aiteam-data>/aiteam.db", "aiteam-data"),
    ("CODEX at /Users/alice/.codex/hooks", "CODEX at <codex-home>/hooks", "codex-home"),
    ("cd /Users/alice/Desktop", "cd ~/Desktop", "home"),
    ("file:///Users/alice/notes.txt", "file://~/notes.txt", "home"),
    ("other /Users/bob/x and /home/carol/y", "other ~/x and ~/y", "home"),
    ("C:\\Users\\Dave\\proj", "~\\proj", "home"),
    ("I am alice, on alice-mbp.local", "I am <user>, on <host>", "user"),
    ("mail alice@example.com please", "mail <email> please", "email"),
    ("Authorization: Bearer Q7mZ2kX9pL4vR8nB3cT6", "Authorization: Bearer <redacted>", "credential"),
    ("see https://example.com/cb?code=abc", "see https://example.com/cb?code=<redacted>", "url"),
], ids=["project", "claude-config", "data", "codex-home", "home", "file-url", "other-homes", "windows-home",
        "user-host", "email", "bearer", "url-query"])
def test_each_kind_of_rewrite(text: str, expected: str, kind: str) -> None:
    cleaned = clean(text)
    assert cleaned.fields["body"] == expected
    assert cleaned.rewrites.get(kind, 0) >= 1


def test_ordinary_prose_is_left_alone() -> None:
    text = "The token usage was high; basic idea: git@github.com:acme/app.git is the remote."
    assert clean(text).fields["body"] == text


def test_a_short_or_common_login_is_named_not_rewritten() -> None:
    context = redact.Context(login="me", hostnames=("mac",))
    cleaned = clean("tell me about the mac", context)
    assert cleaned.fields["body"] == "tell me about the mac"
    assert cleaned.notes["unreplaced_user"] == ["me"] and cleaned.notes["unreplaced_host"] == ["mac"]


def test_registered_project_names_and_leftover_paths_are_listed_not_refused() -> None:
    cleaned = clean("zeta-ledger broke under /opt/tools/bin and in AI Team OS")
    assert cleaned.notes["possible_private_names"] == ["zeta-ledger"]
    assert cleaned.notes["residual_paths"] == ["/opt/tools/bin"]


def test_private_terms_match_whole_ascii_words_ignoring_case_and_cjk_anywhere() -> None:
    context = redact.Context(terms=("contact", "星图"))
    with pytest.raises(redact.Refused) as refused:
        redact.clean({"title": "About Contact", "body": "line one\n这是星图计划"}, context)
    assert [(r.field, r.line, r.term) for r in refused.value.refusals] == [("title", 1, "Contact"),
                                                                          ("body", 2, "星图")]
    assert all("term" not in r.stored() for r in refused.value.refusals)
    clean("contactless payments", context)  # not the word


def test_a_secret_refuses_before_anything_is_rewritten() -> None:
    token = "gh" + "p_" + "Q7mZ2kX9pL4vR8nB3cT6wY1hJ5dF0gSabcdef"
    with pytest.raises(redact.Refused) as refused:
        redact.clean({"title": "ok", "body": f"one\n{token}"}, CONTEXT)
    assert [(r.rule, r.field, r.line) for r in refused.value.refusals] == [("github_token", "body", 2)]


def test_the_term_file(tmp_path) -> None:
    path = tmp_path / redact.TERMS_FILE
    assert redact.load_terms(path) == ()
    path.write_text("# private words\n\nalpha\n  beta  \nalpha\n", encoding="utf-8")
    assert redact.load_terms(path) == ("alpha", "beta")
    path.write_text("\n".join(f"term{n}" for n in range(redact.MAX_TERMS + 1)), encoding="utf-8")
    with pytest.raises(redact.TermListError):
        redact.load_terms(path)


def test_the_largest_input_is_checked_in_milliseconds() -> None:
    """20,000 characters against 500 terms: the bounds that keep this in a worker thread.

    If a bound is raised (text, terms, line length), this is the test that should go red.
    """
    terms = tuple(f"secretword{n}" for n in range(redact.MAX_TERMS))
    context = redact.Context(roots=CONTEXT.roots, home=CONTEXT.home, login="alice",
                             hostnames=CONTEXT.hostnames, project_names=tuple(f"p{n}" for n in range(50)),
                             terms=terms)
    line = "word /Users/alice/x https://example.com/a?b=c alice@example.com " * 60
    text = "\n".join([line[:4000]] * 5)[:20_000]
    started = time.perf_counter()
    cleaned = redact.clean({"body": text}, context)
    redact.secret_hits({"body": cleaned.fields["body"]})
    assert time.perf_counter() - started < 1.0
