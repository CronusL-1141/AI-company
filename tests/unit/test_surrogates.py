"""aiteam.surrogates: the one definition of a lone surrogate, its replacement, and the
JSON serializer the storage layer writes every JSON value through."""

from __future__ import annotations

import json

from aiteam.surrogates import has_lone_surrogate, json_dumps, replace_lone_surrogates

HIGH, LOW = chr(0xD800), chr(0xDC00)
FFFD = chr(0xFFFD)


class TestDetection:
    def test_found_in_nested_values_and_in_keys(self):
        assert has_lone_surrogate({"a": [{"b": "x" + HIGH}]})
        assert has_lone_surrogate({"k" + LOW: "v"})
        assert has_lone_surrogate(["ok", 1, None, ("y" + LOW,)])

    def test_ordinary_text_is_not_flagged(self):
        # An astral character is one code point in a Python str, never a pair.
        assert not has_lone_surrogate({"t": "emoji " + chr(0x1F600), "n": 1, "l": ["中文"]})
        assert not has_lone_surrogate({"literal": "\\" + "ud800"})


class TestReplacement:
    def test_keeps_shape_and_leaves_no_surrogate(self):
        data = {"a" + HIGH: ["x" + LOW, 3, {"b": HIGH + HIGH}], "n": None}
        fixed = replace_lone_surrogates(data)
        assert fixed == {"a" + FFFD: ["x" + FFFD, 3, {"b": FFFD * 2}], "n": None}
        assert not has_lone_surrogate(fixed)


class TestStorageSerializer:
    def test_identical_to_json_dumps_without_a_lone_surrogate(self):
        for value in ({"a": 1, "b": [None, True, 1.5]}, ["中文", chr(0x1F600)], {"lit": "\\" + "ud800"}, "x"):
            assert json_dumps(value) == json.dumps(value)

    def test_a_lone_surrogate_is_stored_as_the_replacement_character(self):
        text = json_dumps({"tags": ["x" + HIGH + "y"], "k" + LOW: chr(0x1F600)})
        assert json.loads(text) == {"tags": ["x" + FFFD + "y"], "k" + FFFD: chr(0x1F600)}
        text.encode("utf-8")  # storable and servable
