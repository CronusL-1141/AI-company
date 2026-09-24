"""Local rendering rules of user_notice (design §4): prefix, width, colour, ANSI hygiene."""

from __future__ import annotations

import re
import sys

import pytest

ESC = "\x1b"
SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@pytest.fixture()
def un():
    return sys.modules["user_notice"]


def _all_cases(un):
    for catalog_id, entry in un.LOCAL_CATALOG.items():
        for variant in entry["variants"]:
            for language in ("zh", "en"):
                yield catalog_id, variant, language


def test_colour_only_on_the_cc_cli(un):
    for host, entrypoint, coloured in (("cc", "cli", True), ("cc", "", False), ("cc", "claude-desktop", False),
                                       ("codex", "cli", False)):
        line, _ = un.render_local("api_down", {}, host=host, language="zh", entrypoint=entrypoint)
        assert (ESC in line) is coloured, (host, entrypoint)


@pytest.mark.parametrize(("catalog_id", "sequence"), [
    ("api_down", "\x1b[33m"), ("install_done", "\x1b[32m"), ("blocked_secret_add", "\x1b[31m"),
])
def test_prefix_stays_host_grey_and_the_colour_runs_to_the_end(un, catalog_id, sequence):
    line, _ = un.render_local(catalog_id, {"ver": "v1.0", "file": ".env"}, host="cc", language="en",
                              entrypoint="cli")
    assert line.startswith(un.PREFIX + sequence)
    assert line.endswith("\x1b[39m")
    assert "\x1b[0m" not in line
    assert SEQUENCE.findall(line) == [sequence, "\x1b[39m"], "no text after the coloured run"


def test_status_lines_are_never_coloured(un):
    line, _ = un.render_local("install_in_progress", {"attempt": "2"}, host="cc", language="zh", entrypoint="cli")
    assert ESC not in line


def test_parameters_cannot_inject_sequences(un):
    hostile = f"{ESC}[31mred{ESC}[0m\n‮​x\x07.env"
    line, note = un.render_local("blocked_secret_add", {"file": hostile}, host="cc", language="zh",
                                 entrypoint="cli")
    assert SEQUENCE.findall(line) == ["\x1b[31m", "\x1b[39m"]
    assert not any(ch in line for ch in ("\n", "‮", "​", "\x07"))
    assert ESC not in note


def test_every_local_line_fits_160_columns_with_maximal_parameters(un):
    for catalog_id, variant, language in _all_cases(un):
        params = {name: "宽" * 200 for name in un.LOCAL_CATALOG[catalog_id]["params"]}
        for entrypoint in ("cli", ""):
            line, _ = un.render_local(catalog_id, params, host="cc", language=language, variant=variant,
                                      entrypoint=entrypoint)
            assert line.startswith(un.PREFIX)
            assert un.display_width(line) <= un.MAX_COLUMNS, (catalog_id, variant, language)


def test_paths_keep_their_tail(un):
    target = "/Users/someone/projects/very-long-repository-name/.worktrees/feature-branch-name"
    line, _ = un.render_local("blocked_teardown", {"target": target}, host="cc", language="en",
                              variant="unsaved")
    assert "…" in line and "feature-branch-name" in line


def test_english_texts_have_no_em_dash_and_no_markdown(un):
    for catalog_id, variant, language in _all_cases(un):
        texts = un.LOCAL_CATALOG[catalog_id]["variants"][variant]
        for part in ("user", "model"):
            text = texts[part][language]
            if language == "en":
                assert "—" not in text, (catalog_id, variant, part)
        assert not any(token in texts["user"][language] for token in ("**", "`", "](", "http"))


def test_model_note_frames(un):
    line, note = un.render_local("api_down", {}, host="cc", language="zh", entrypoint="cli")
    plain = un.strip_ansi(line)
    first, *_rest, last = note.split("\n")
    assert first.endswith("：" + plain), "the model is told the exact line the user saw"
    assert last == un.MODEL_CLOSING["zh"]
    _, unreliable = un.render_local("api_down", {}, host="cc", language="zh", reliable=False)
    assert unreliable.startswith("AI Team OS 尝试向用户显示以下提示")
    line, note = un.render_local("blocked_secret_add", {"file": ".env"}, host="cc", language="zh", entrypoint="cli")
    assert note == "用户界面已显示：" + un.strip_ansi(line)


def test_unknown_variant_falls_back_to_the_default(un):
    assert un.render_local("install_failed", {}, host="cc", language="en", variant="nope") == \
        un.render_local("install_failed", {}, host="cc", language="en", variant="")
