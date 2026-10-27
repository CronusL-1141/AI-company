"""A consent record has one shape, built in one place (user_notice.consent_record).

Three writers record a conversation-authorised change: os_config_change
(mcp/tools/infra.py), the data directory migration and uninstall_main_chain.py.
Each used to spell the envelope itself; now each asks user_notice, and a fourth
hand-written one is caught here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiteam.hooks import user_notice

ROOT = Path(__file__).resolve().parents[3]
_HAND_WRITTEN = re.compile(r"""["']kind["']\s*:\s*["']consent["']|record_local\([^)]*["']consent["']""")
_THE_ONE = {"plugin/hooks/user_notice.py", "src/aiteam/hooks/user_notice.py",
            "plugin/harness/codex/hooks/user_notice.py"}


def test_no_writer_spells_the_consent_envelope_itself():
    found = []
    for folder in ("src/aiteam", "plugin", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if "__pycache__" in path.parts or relative in _THE_ONE:
                continue
            if _HAND_WRITTEN.search(path.read_text(encoding="utf-8")):
                found.append(relative)
    assert found == []


def test_the_shape_and_its_one_line(tmp_path):
    record = user_notice.consent_record("some_tool", change="x", user_quote="好")
    assert set(record) == {"uuid", "kind", "at", "ts", "source", "change", "user_quote"}
    assert (record["kind"], record["source"]) == ("consent", "some_tool")
    path = tmp_path / "notice-local.cc.jsonl"
    assert user_notice.append_record_line(path, record)
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert not user_notice.append_record_line(path, {**record, "user_quote": "x" * 2000})
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
