"""The API renders, the hook writes: every rendered line must pass the hook's checks.

``user_notice.emit`` drops a user line that contains Markdown, a backtick or
``http`` (design §5.7). Parameters are external text (briefing titles, channel
senders, branch and file names), so a renderer that passed them through would
produce lines the hook silently drops: the notice never shows, its delivery is
never reported as written, and the ledger keeps re-claiming it. Both renderers
(the API catalog and the hook's local copy) therefore neutralise those tokens
inside parameters, identically.
"""

from __future__ import annotations

import json
import sys

import pytest

from aiteam.services.notices.catalog import CATALOG, CATALOG_ENTRIES, render_entry

MARKUP = "**bold** `tick` [a](b) see https://x.invalid/p and fix/http-retry HTTP://Y"


def _params(entry) -> dict:
    return {name: MARKUP for name in entry.params}


CASES = [
    pytest.param(entry.id, variant, id=f"{entry.id}-{variant or 'default'}")
    for entry in CATALOG_ENTRIES
    for variant in entry.variants
]


@pytest.fixture()
def un():
    return sys.modules["user_notice"]


@pytest.mark.parametrize(("catalog_id", "variant"), CASES)
def test_api_rendered_line_is_accepted_by_emit(un, catalog_id, variant):
    entry = CATALOG[catalog_id]
    for language in ("zh", "en"):
        for host in ("cc", "codex"):
            for entrypoint in ("cli", ""):
                rendered = render_entry(entry, variant=variant, language=language, host=host,
                                        params=_params(entry), entrypoint=entrypoint)
                assert un._valid_line(rendered.line) == "", (language, host, entrypoint, rendered.plain)


@pytest.mark.parametrize("catalog_id", sorted(un_id for un_id in CATALOG if CATALOG[un_id].local))
def test_local_line_with_markup_params_matches_the_api_and_is_accepted(un, catalog_id):
    entry = CATALOG[catalog_id]
    for variant in entry.variants:
        for language in ("zh", "en"):
            line, model = un.render_local(catalog_id, _params(entry), host="cc", language=language,
                                          variant=variant, entrypoint="cli")
            api = render_entry(entry, variant=variant, language=language, host="cc",
                               params=_params(entry), entrypoint="cli")
            assert (line, model) == (api.line, api.model)
            assert un._valid_line(line) == ""


def test_model_note_keeps_the_original_text(un):
    """Only the user line is neutralised; the model still gets the real value."""
    entry = CATALOG["decisions_pending"]
    rendered = render_entry(entry, language="en", host="cc",
                            params={"n": 1, "title": "fix `x` via https://h.invalid"})
    assert "`" not in rendered.plain and "https://" not in rendered.plain
    header, note = rendered.model.split("\n", 1)
    assert rendered.plain in header, "the model is told exactly what the user saw"
    assert "fix `x` via https://h.invalid" not in note  # the E08 note names no title


def test_branch_with_http_in_its_name_is_shown(un, tmp_path, monkeypatch, capsys):
    """End to end in the hook: a block on branch feat/http2 states its reason, not nothing."""
    monkeypatch.setattr(un, "STATE_DIR_OVERRIDE", str(tmp_path))
    monkeypatch.setattr(un, "_WROTE_DOCUMENT", False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    written = un.emit_block("blocked_foreign_branch", {"branch": "feat/http2"}, session_id="s1",
                            cwd=str(tmp_path), model_text="[OS BLOCK] why")
    captured = capsys.readouterr()
    assert written is True
    assert "dropped" not in captured.err
    assert json.loads(captured.out)["hookSpecificOutput"]["permissionDecisionReason"] == (
        "[AI Team OS] 已拦截提交：分支 feat/HTTP2 正被另一个会话使用，命令未执行")
