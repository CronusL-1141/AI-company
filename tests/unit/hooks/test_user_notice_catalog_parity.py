"""user_notice.LOCAL_CATALOG is a verbatim copy of the API catalog's local entries.

The hook renders these lines without the API (blocks, install progress, "the
service is down"). Both the texts and the renderer must match the API side,
so the Dashboard shows exactly what the terminal showed: every local entry is
rendered by both sides over languages, hosts, colour gates, reliability and
hostile parameters, and the outputs must be identical.
"""

from __future__ import annotations

import sys

import pytest

from aiteam.services.notices.catalog import CATALOG, CATALOG_ENTRIES, render_entry

LOCAL_IDS = sorted(entry.id for entry in CATALOG_ENTRIES if entry.local)


@pytest.fixture()
def un():
    return sys.modules["user_notice"]


def test_same_local_entries(un):
    assert sorted(un.LOCAL_CATALOG) == LOCAL_IDS


@pytest.mark.parametrize("catalog_id", LOCAL_IDS)
def test_entry_fields_and_texts_are_verbatim(un, catalog_id):
    api = CATALOG[catalog_id]
    local = un.LOCAL_CATALOG[catalog_id]
    assert local["kind"] == api.kind.value
    assert local["frame"] == api.frame
    assert local["params"] == dict(api.params)
    assert tuple(local["tail_params"]) == tuple(sorted(api.tail_params))
    assert set(local["variants"]) == set(api.variants)
    for name, texts in api.variants.items():
        assert local["variants"][name]["user"] == dict(texts.user), name
        assert local["variants"][name]["model"] == dict(texts.model), name


def _param_sets(catalog_id: str) -> list[tuple[str, dict]]:
    names = list(CATALOG[catalog_id].params)
    return [
        ("empty", {}),
        ("plain", {name: f"v{index}" for index, name in enumerate(names)}),
        ("long-wide", {name: "宽字符" * 40 for name in names}),
        ("long-path", {name: "/very/long/path/" + "segment/" * 12 + "tail.env" for name in names}),
        ("hostile", {name: "\x1b[31mred\x1b[0m\n‮line\x07" for name in names}),
    ]


CASES = [
    pytest.param(catalog_id, variant, label, id=f"{catalog_id}-{variant or 'default'}-{label}")
    for catalog_id in LOCAL_IDS
    for variant in CATALOG[catalog_id].variants
    for label, _ in _param_sets(catalog_id)
]


@pytest.mark.parametrize(("catalog_id", "variant", "label"), CASES)
def test_hook_renders_exactly_what_the_api_renders(un, catalog_id, variant, label):
    params = dict(_param_sets(catalog_id))[label]
    for language in ("zh", "en"):
        for host in ("cc", "codex"):
            for entrypoint in ("cli", ""):
                for reliable in (True, False):
                    api = render_entry(CATALOG[catalog_id], variant=variant, language=language, host=host,
                                       params=params, entrypoint=entrypoint, reliable=reliable)
                    line, model = un.render_local(catalog_id, params, host=host, language=language,
                                                  variant=variant, entrypoint=entrypoint, reliable=reliable)
                    assert (line, model) == (api.line, api.model), (language, host, entrypoint, reliable)
                    assert un.display_width(line) <= un.MAX_COLUMNS


def test_parity_would_catch_a_drifted_text(un, monkeypatch):
    """Reverse check: one changed character in the hook copy breaks the comparison."""
    entry = un.LOCAL_CATALOG["api_down"]["variants"][""]
    monkeypatch.setitem(entry["user"], "zh", entry["user"]["zh"] + "。")
    api = render_entry(CATALOG["api_down"], language="zh", host="cc")
    assert un.render_local("api_down", {}, host="cc", language="zh")[0] != api.line
