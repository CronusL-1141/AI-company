"""The quoted-text cleaners must agree: hook_core._sanitize_inline and render.clean_text.

Two copies are deliberate. Hooks run on the standard library and cannot import the
aiteam package, while the API renders server-side text with clean_text; the split
is the layer boundary, not duplication (I24 allows exactly these two). What must
not happen is the two drifting apart, so the same corpus goes through both. The
hook-side mirror of clean_text in user_notice.py (local notice rendering) is held
to the same answer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from aiteam.services.notices.render import clean_text

ROOT = Path(__file__).resolve().parents[2]


def _hook_core_copies():
    for directory in ("plugin/hooks", "src/aiteam/hooks", "plugin/harness/codex/hooks"):
        path = ROOT / directory / "hook_core.py"
        name = f"_hook_core_parity_{directory.replace('/', '_')}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield f"hook_core:{directory}", module._sanitize_inline
    for directory in ("plugin/hooks", "src/aiteam/hooks", "plugin/harness/codex/hooks"):
        path = ROOT / directory / "user_notice.py"
        name = f"_user_notice_parity_{directory.replace('/', '_')}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield f"user_notice:{directory}", module.clean_text


COPIES = list(_hook_core_copies())

CORPUS = {
    "plain": "hello world",
    "rlo": "a\u202eb",
    "zwsp": "a\u200bb",
    "controls": "a\x00\x07\x1b\x7fb",
    "newlines": "a\nb\r\nc\x85d",
    "line-para-sep": "a\u2028b\u2029c",
    "isolates": "a\u2066b\u2067c\u2068d\u2069e",
    "lone-surrogate": "a\ud800b\udfffc",
    "bom-word-joiner": "\ufeffa\u2060b",
    "tag-chars": "a\U000e0041\U000e007fb",
    "private-use": "a\ue000b\U000f0000c",
    "unassigned": "a\u0378b",
    "nbsp-ideographic": "a\u00a0b\u3000c",
    "combining": "a" + "\u0301" * 5,
    "empty": "",
    "only-noise": "\u202e\u200b\n\t",
    "cjk": "中文 标题\t测试",
    # Every code point once: any single character the two map differently shows here.
    "all-code-points": "".join(chr(cp) for cp in range(0x110000)),
}


@pytest.mark.parametrize("directory,sanitize", COPIES, ids=[d for d, _ in COPIES])
@pytest.mark.parametrize("sample", list(CORPUS), ids=list(CORPUS))
def test_hook_and_server_cleaners_agree(sample, directory, sanitize):
    text = CORPUS[sample]
    assert sanitize(text) == clean_text(text)
