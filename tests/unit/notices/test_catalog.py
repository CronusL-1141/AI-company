"""Catalog completeness and shape (docs/user-notice-design.md §5.3, §9.1).

Every entry and every variant must exist in Chinese and English for both the
user line and the model note, fit 160 columns at its widest, carry no
Markdown/URL/backticks, and (for action and decision items) tell the user what
to do. Test ids are the entry ids, never the texts.
"""

from __future__ import annotations

import pytest

from aiteam.services.notices import render
from aiteam.services.notices.catalog import (
    CATALOG,
    CATALOG_ENTRIES,
    HOSTS,
    RENDER_AT,
    render_entry,
)
from aiteam.types import NoticeColor, NoticeKind

VARIANTS = [(entry, variant) for entry in CATALOG_ENTRIES for variant in entry.variants]
IDS = [f"{entry.id}[{variant or 'default'}]" for entry, variant in VARIANTS]
ENTRY_IDS = [entry.id for entry in CATALOG_ENTRIES]
WIDE = "中"  # East Asian Width W: 2 columns


def _params(entry, char):
    return {name: char * limit for name, limit in entry.params.items()}


def test_twenty_four_entries_in_design_order():
    assert len(CATALOG_ENTRIES) == 24
    assert len(CATALOG) == 24
    assert CATALOG_ENTRIES[0].id == "api_down" and CATALOG_ENTRIES[22].id == "more_pending"
    assert CATALOG_ENTRIES[-1].id == "api_starting"


@pytest.mark.parametrize("entry", CATALOG_ENTRIES, ids=ENTRY_IDS)
def test_entry_fields_use_the_declared_vocabulary(entry):
    assert entry.hosts <= HOSTS and entry.hosts
    assert entry.render_at <= RENDER_AT and entry.render_at
    assert entry.dedup in ("per_session", "once") or entry.cooldown_hours
    assert entry.clear in ("auto", "user_ack", "once", "superseded") or entry.ttl_seconds
    assert "" in entry.variants
    assert entry.local == bool(entry.render_at & {"local", "immediate"})
    assert entry.tail_params <= set(entry.params) and entry.block_params <= set(entry.params)


@pytest.mark.parametrize(("entry", "variant"), VARIANTS, ids=IDS)
def test_every_variant_has_both_languages_for_user_and_model(entry, variant):
    texts = entry.variants[variant]
    for language in ("zh", "en"):
        assert texts.user[language].strip(), language
        assert texts.model[language].strip(), language
        assert not texts.user[language].startswith(render.PREFIX)


@pytest.mark.parametrize(("entry", "variant"), VARIANTS, ids=IDS)
def test_placeholders_are_declared(entry, variant):
    texts = entry.variants[variant]
    allowed = set(entry.params) | {"assistant", "host_app"}
    for language in ("zh", "en"):
        assert render.template_fields(texts.user[language]) <= allowed - entry.block_params
        assert render.template_fields(texts.model[language]) <= allowed | {"line"}


@pytest.mark.parametrize(("entry", "variant"), VARIANTS, ids=IDS)
@pytest.mark.parametrize("host", ["cc", "codex"])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_rendered_lines_fit_and_stay_plain(entry, variant, host, language):
    for char in ("a", WIDE):
        result = render_entry(entry, variant=variant, language=language, host=host,
                              params=_params(entry, char), entrypoint="cli")
        assert result.plain.startswith(render.PREFIX)
        assert "\n" not in result.plain
        assert render.display_width(result.line) <= render.MAX_WIDTH
        assert "{" not in result.plain and "}" not in result.plain
        for banned in ("**", "`", "](", "http"):
            assert banned not in result.plain
        assert render.template_fields(result.model) == set()
        if language == "en":
            assert "—" not in result.plain and "—" not in result.model


@pytest.mark.parametrize(("entry", "variant"), VARIANTS, ids=IDS)
def test_ascii_parameters_at_their_limit_need_no_shrinking(entry, variant):
    """The column budget in §6 holds for realistic (half-width) values."""
    for language in ("zh", "en"):
        for host in ("cc", "codex"):
            values = {"assistant": render.ASSISTANT[host], "host_app": render.HOST_APP[host]}
            values.update({name: "a" * limit for name, limit in entry.params.items()
                           if name not in entry.block_params})
            natural = render.PREFIX + render.fill(entry.variants[variant].user[language], values)
            assert render.display_width(natural) <= render.MAX_WIDTH, (language, host)


def test_kind_to_colour_mapping():
    assert render.KIND_COLOR == {
        NoticeKind.STATUS: NoticeColor.DEFAULT,
        NoticeKind.ACTION: NoticeColor.YELLOW,
        NoticeKind.DECISION: NoticeColor.YELLOW,
        NoticeKind.BLOCKED: NoticeColor.RED,
        NoticeKind.DONE: NoticeColor.GREEN,
    }
    for entry in CATALOG_ENTRIES:
        assert entry.color == render.KIND_COLOR[entry.kind]


@pytest.mark.parametrize(("entry", "variant"), [
    pair for pair in VARIANTS if pair[0].kind in (NoticeKind.ACTION, NoticeKind.DECISION)
], ids=[i for i, pair in zip(IDS, VARIANTS) if pair[0].kind in (NoticeKind.ACTION, NoticeKind.DECISION)])
def test_action_and_decision_items_tell_the_user_what_to_do(entry, variant):
    texts = entry.variants[variant]
    assert "对 {assistant} 说「" in texts.user["zh"] or "运行" in texts.user["zh"]
    assert 'Tell {assistant} "' in texts.user["en"] or "Run " in texts.user["en"] \
        or "tell {assistant}" in texts.user["en"]


def test_assistant_and_host_app_follow_the_host():
    entry = CATALOG["api_down"]
    cc = render_entry(entry, language="en", host="cc").plain
    codex = render_entry(entry, language="en", host="codex").plain
    assert "Restart Claude Code" in cc and 'tell Claude "' in cc
    assert "Restart Codex" in codex and 'tell Codex "' in codex


def test_a_starting_service_is_a_plain_status_that_holds_off_the_restart():
    """E24 replaces E01 on a start whose MCP server is still bringing the API up."""
    entry = CATALOG["api_starting"]
    assert entry.kind == NoticeKind.STATUS and entry.local and entry.hosts == {"cc", "codex"}
    for language in ("zh", "en"):
        result = render_entry(entry, language=language, host="cc", entrypoint="cli")
        assert result.line == result.plain, "not coloured: nothing is wrong yet"
        assert "os_restart_api" in result.model
    assert "不要马上调用 os_restart_api" in render_entry(entry, language="zh").model
    assert "do not call os_restart_api right away" in render_entry(entry, language="en").model


def test_release_variants_carry_the_update_commands():
    entry = CATALOG["release_available"]
    params = {"ver": "v1.15.0", "old": "v1.14.0", "url": "https://github.com/x/releases/tag/v1.15.0"}
    # The line names the command where it fits; the note always carries every step.
    expected = {
        "cc_plugin": ({"zh": "对 Claude 说「更新 OS」", "en": 'Tell Claude "update OS"'},
                      ("claude plugin marketplace update ai-team-os", "claude plugin update ai-team-os@ai-team-os")),
        "cc_source": ("python3 install.py --update", ("python3 install.py --update",)),
        "codex": ("python3 scripts/codex_adapter.py upgrade", ("python3 scripts/codex_adapter.py upgrade",)),
        "unknown": ({"zh": "怎么更新 OS", "en": "how do I update OS"},
                    ("claude plugin marketplace update ai-team-os", "claude plugin update ai-team-os@ai-team-os",
                     "python3 install.py --update", "python3 scripts/codex_adapter.py upgrade")),
    }
    for variant, (line, steps) in expected.items():
        for language in ("zh", "en"):
            result = render_entry(entry, variant=variant, language=language, params=params)
            assert (line[language] if isinstance(line, dict) else line) in result.plain
            positions = [result.model.index(step) for step in steps]
            assert positions == sorted(positions)
            assert "v1.15.0" in result.plain and "v1.14.0" in result.plain
            assert params["url"] in result.model and "http" not in result.plain


def test_model_note_frames():
    notice = render_entry(CATALOG["api_version_stale"], language="zh", params={"old": "v1", "ver": "v2"})
    assert notice.model.startswith("AI Team OS 刚在界面上向用户显示了以下提示")
    assert notice.plain in notice.model and notice.model.endswith(render.MODEL_CLOSING["zh"])
    unreliable = render_entry(CATALOG["api_version_stale"], language="en", reliable=False,
                              params={"old": "v1", "ver": "v2"})
    assert unreliable.model.startswith("AI Team OS tried to show")
    # A block's line is the deny reason, which reaches the model as it is; the
    # note never claims what the user saw (CC 2.1.281 shows it as "hook error").
    blocked = render_entry(CATALOG["blocked_foreign_branch"], language="zh", params={"branch": "main"})
    assert blocked.plain not in blocked.model and "已显示" not in blocked.model
    assert "[OS BLOCK]" in blocked.model
    # The Stop block's note is shown to the user as "Stop hook feedback": no
    # "may not have been shown" hedge, and the model keeps its instruction.
    for language in ("zh", "en"):
        held_turn = render_entry(CATALOG["blocked_turn_end"], language=language, params={"n": 2})
        assert held_turn.plain not in held_turn.model
        assert "os-watch.sh" in held_turn.model and "可能未显示" not in held_turn.model
        assert "may not be visible" not in held_turn.model
    act = render_entry(CATALOG["channel_mention"], language="en",
                       params={"sender": "bob", "channel": "global", "n": 2, "details": "l1\nl2"})
    assert act.model.endswith("l1\nl2") and render.MODEL_CLOSING["en"] not in act.model
    for language in ("zh", "en"):
        held = render_entry(CATALOG["channel_mention"], language=language, held=True,
                            params={"sender": "bob", "channel": "global", "n": 2, "details": "l1"})
        header = render.MODEL_HEADER_HELD[language].replace("{line}", held.plain)
        assert held.model == header + "\n" + held.model.split("\n", 1)[1] and held.model.endswith("l1")
        assert "—" not in held.model


def test_only_entries_the_model_acts_on_are_told_when_held():
    assert {entry.id for entry in CATALOG.values() if entry.tell_model_when_held} == {"channel_mention"}
