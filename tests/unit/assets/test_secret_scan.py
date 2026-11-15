"""The secret scan's rules against their corpus: production rules, no stub.

Removing a rule turns its corpus entry red (the entry names the rule), and a rule
without corpus entries fails on its own.
"""

from __future__ import annotations

import time

import pytest

from aiteam.assets.secret_scan import (
    CORPUS,
    LONG_LINE,
    LONG_TEXT,
    MAX_LINE_CHARS,
    MAX_SCAN_CHARS,
    RULES,
    Hit,
    scan_bytes,
    scan_text,
)

RULES_BY_ID = {rule.id: rule for rule in RULES}
HITS = [(entry.rule, text) for entry in CORPUS for text in entry.must_hit]
MISSES = [(entry.rule, text) for entry in CORPUS for text in entry.must_not_hit]


def test_every_rule_has_corpus_entries_both_ways():
    covered = {entry.rule for entry in CORPUS if entry.must_hit and entry.must_not_hit}
    assert covered == set(RULES_BY_ID)
    assert len(RULES_BY_ID) == len(RULES)  # ids are unique


@pytest.mark.parametrize(("rule", "text"), HITS, ids=[f"{rule}-{i}" for i, (rule, _) in enumerate(HITS)])
def test_rule_catches_its_samples(rule, text):
    assert rule in RULES_BY_ID, f"rule {rule} is gone but its corpus remains"
    assert RULES_BY_ID[rule].hits(text)
    assert Hit(rule, 1) in scan_text(text)


@pytest.mark.parametrize(("rule", "text"), MISSES, ids=[f"{rule}-{i}" for i, (rule, _) in enumerate(MISSES)])
def test_rule_leaves_its_lookalikes_alone(rule, text):
    assert rule in RULES_BY_ID
    assert not RULES_BY_ID[rule].hits(text)


def test_hits_name_rule_and_line_never_the_text():
    token = next(text for entry in CORPUS if entry.rule == "github_token" for text in entry.must_hit)
    hits = scan_bytes(f"# notes\nnothing here\n{token}\n".encode())
    assert Hit("github_token", 3) in hits
    assert {hit.line for hit in hits} == {3}
    assert all(set(vars(hit)) == {"rule", "line"} for hit in hits)


def test_ordinary_memory_text_is_clean():
    text = (
        "# Project memory\n"
        "- The API listens on port 8000; tokens are counted per session.\n"
        "- password policy: see the team handbook\n"
        "- Run `python3 -m pytest tests/ -n 4 --dist loadfile` before merging.\n"
        "- Commit 6767bfe0123456789abcdef added the sync groundwork.\n"
    )
    assert scan_text(text) == []


def test_undecodable_bytes_do_not_stop_the_scan():
    token = next(text for entry in CORPUS if entry.rule == "private_key" for text in entry.must_hit)
    assert Hit("private_key", 2) in scan_bytes(b"\xff\xfe binary\n" + token.encode() + b"\n")


def test_a_line_over_the_limit_is_a_suspected_secret_without_matching():
    hex_line = "0123456789abcdef" * 375  # 6000 characters
    assert scan_text(f"intro\n{hex_line}\n") == [Hit(LONG_LINE, 2)]
    assert scan_text("x" * MAX_LINE_CHARS) == []


def test_a_text_over_the_scan_budget_stops_with_a_suspected_secret():
    line = "plain words " * 100
    text = (line + "\n") * (MAX_SCAN_CHARS // len(line) + 5)
    hits = scan_text(text)
    assert hits[-1].rule == LONG_TEXT and len(hits) == 1


@pytest.mark.parametrize("text", [
    "a" * MAX_LINE_CHARS, "a1" * (MAX_LINE_CHARS // 2), "x." * (MAX_LINE_CHARS // 2), "sk-" * (MAX_LINE_CHARS // 3),
    "a://" * (MAX_LINE_CHARS // 4), "api_key" * (MAX_LINE_CHARS // 7), "eyJ" * (MAX_LINE_CHARS // 3),
    "api_key=a " * (MAX_LINE_CHARS // 10), "-----BEGIN " + "AAAA " * 800,
], ids=["letters", "alnum", "dots", "sk-prefixes", "schemes", "key-names", "jwt-prefixes", "assignments", "pem"])
def test_the_longest_scanned_line_costs_linear_time(text):
    """Every pattern is bounded: the worst line at the limit scans in milliseconds (was seconds)."""
    started = time.perf_counter()
    scan_text(text)
    assert time.perf_counter() - started < 0.25
