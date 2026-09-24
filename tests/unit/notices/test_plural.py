"""English count agreement: "1 copy is", "2 copies are" (API catalog and the hook's local copy)."""

from __future__ import annotations

import sys

import pytest

from aiteam.services.notices import render
from aiteam.services.notices.catalog import CATALOG, render_entry

# (entry, variant, extra params, text for n=1, text for n=2)
CASES = [
    ("decisions_pending", "", {"title": "t"}, "1 decision is waiting", "2 decisions are waiting"),
    ("installed_copy_stale", "", {}, "1 installed hook/skill copy is behind",
     "2 installed hook/skill copies are behind"),
    ("installed_copy_stale", "plugin_sync_failed", {}, "sync 1 outdated hook copy,", "sync 2 outdated hook copies,"),
    ("installed_copy_synced", "", {}, "Synced 1 outdated hook copy. It takes effect",
     "Synced 2 outdated hook copies. They take effect"),
    ("codex_copy_stale", "", {}, "1 Codex hook copy is behind the adapter and runs old rules",
     "2 Codex hook copies are behind the adapter and run old rules"),
    ("codex_copy_stale", "missing", {}, "1 registered Codex copy is missing",
     "2 registered Codex copies are missing"),
    ("codex_copy_stale", "modified", {}, "1 Codex copy differs from the install record",
     "2 Codex copies differ from the install record"),
    ("codex_copy_stale", "source_missing", {}, "missing 1 declared file", "missing 2 declared files"),
    ("codex_copy_stale", "retired", {}, "1 retired Codex entry remains", "2 retired Codex entries remain"),
    ("blocked_turn_end", "", {}, "1 task is still running", "2 tasks are still running"),
    ("more_pending", "", {}, "1 more item is pending", "2 more items are pending"),
]
IDS = [f"{entry}-{variant or 'default'}" for entry, variant, *_ in CASES]


@pytest.mark.parametrize(("catalog_id", "variant", "extra", "one", "two"), CASES, ids=IDS)
def test_api_catalog_agrees_in_number(catalog_id, variant, extra, one, two):
    entry = CATALOG[catalog_id]
    for n, expected in ((1, one), (2, two)):
        line = render_entry(entry, variant=variant, language="en", params={"n": n, **extra}).plain
        assert expected in line, line
        assert "{" not in line and "|" not in line  # no raw plural syntax left


@pytest.mark.parametrize(("catalog_id", "variant", "extra", "one", "two"),
                         [case for case in CASES if CATALOG[case[0]].local],
                         ids=[i for i, case in zip(IDS, CASES, strict=True) if CATALOG[case[0]].local])
def test_hook_local_copy_agrees_in_number(catalog_id, variant, extra, one, two):
    un = sys.modules["user_notice"]
    for n, expected in ((1, one), (2, two)):
        line, _note = un.render_local(catalog_id, {"n": str(n), **extra}, host="cc", language="en",
                                      variant=variant)
        assert expected in line, line


def test_every_english_count_line_uses_the_plural_form():
    """A new English line with {n} must pick its words with {n?one|other} (or not need them)."""
    no_agreement_needed = {("channel_mention", "")}  # "({n} new)" reads the same for 1 and 2
    for entry in CATALOG.values():
        for variant, texts in entry.variants.items():
            english = texts.user["en"]
            if "{n}" in english and (entry.id, variant) not in no_agreement_needed:
                assert "{n?" in english, (entry.id, variant)


def test_plural_form_rules():
    assert render.fill("{n} {n?copy|copies}", {"n": "1"}) == "1 copy"
    assert render.fill("{n} {n?copy|copies}", {"n": " 1 "}) == " 1  copy"
    assert render.fill("{n} {n?copy|copies}", {"n": "0"}) == "0 copies"
    assert render.fill("{n?copy|copies}", {}) == "copies"
