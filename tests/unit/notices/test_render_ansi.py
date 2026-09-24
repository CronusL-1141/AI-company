"""ANSI colouring rules (docs/user-notice-design.md §4.3), pinned byte for byte."""

from __future__ import annotations

import pytest

from aiteam.services.notices import render
from aiteam.services.notices.catalog import CATALOG, CATALOG_ENTRIES, render_entry

COLOURED = [entry for entry in CATALOG_ENTRIES if render.COLOR_SEQUENCE[entry.color]]


@pytest.mark.parametrize("entry", COLOURED, ids=[entry.id for entry in COLOURED])
def test_cc_cli_colours_body_only_and_resets_foreground_at_line_end(entry):
    params = {name: "p" for name in entry.params}
    result = render_entry(entry, language="en", host="cc", params=params, entrypoint="cli")
    sequence = render.COLOR_SEQUENCE[entry.color]
    assert result.line.startswith(render.PREFIX + sequence)
    assert result.line.endswith(render.RESET_FG)
    assert "\x1b[0m" not in result.line
    # Nothing uncoloured after the coloured run; exactly one opening sequence.
    assert result.line.count("\x1b[") == 2
    assert render.strip_ansi(result.line) == result.plain


@pytest.mark.parametrize(("host", "entrypoint"), [
    ("cc", ""), ("cc", "sdk-ts"), ("cc", "claude-desktop"), ("codex", "cli"), ("codex", ""),
])
def test_no_colour_outside_cc_cli(host, entrypoint):
    result = render_entry(CATALOG["api_down"], language="zh", host=host, entrypoint=entrypoint)
    assert "\x1b" not in result.line
    assert result.line == result.plain


def test_status_lines_are_never_coloured():
    result = render_entry(CATALOG["release_available"], variant="cc_plugin", language="en", host="cc",
                          params={"ver": "v2", "old": "v1", "url": "u"}, entrypoint="cli")
    assert "\x1b" not in result.line


@pytest.mark.parametrize("hostile", [
    "evil\x1b[31mred", "a\x1b]8;;http://x\x07b", "line\nbreak", "tab\tand\rreturn",
    "rtl‮override", "zero​width", "\x00nul\x7fdel",
], ids=["csi", "osc", "newline", "tab-cr", "bidi", "zero-width", "nul-del"])
def test_parameters_cannot_inject_sequences_or_lines(hostile):
    result = render_entry(CATALOG["blocked_foreign_branch"], language="en", host="cc",
                          params={"branch": hostile}, entrypoint="cli")
    body = result.line[len(render.PREFIX):]
    assert body.count("\x1b") == 2  # only our own colour start and reset
    assert "\n" not in result.line and "\r" not in result.line and "\t" not in result.line
    for char in ("‮", "​", "\x00", "\x7f", "\x07"):
        assert char not in result.line
    assert "\n" not in result.model.split("\n")[0]


def test_truncation_keeps_head_or_tail():
    long_branch = "feature/" + "x" * 40
    head = render_entry(CATALOG["blocked_foreign_branch"], language="en", params={"branch": long_branch})
    assert "feature/" in head.plain and render.ELLIPSIS in head.plain
    long_file = "/very/long/path/" + "d" * 40 + "/secret.env"
    tail = render_entry(CATALOG["blocked_secret_add"], language="en", params={"file": long_file})
    assert "secret.env" in tail.plain and render.ELLIPSIS + "d" in tail.plain


def test_width_counts_wide_characters_twice_and_ignores_ansi():
    assert render.display_width("ab") == 2
    assert render.display_width("中文") == 4
    assert render.display_width("\x1b[33m中a\x1b[39m") == 3


def test_wide_parameters_shrink_until_the_line_fits():
    entry = CATALOG["branch_switched"]
    params = {name: "中" * limit for name, limit in entry.params.items()}
    result = render_entry(entry, language="en", params=params)
    assert render.display_width(result.plain) <= render.MAX_WIDTH
    assert "switched from" in result.plain and 'Tell Claude "check branch change"' in result.plain
