"""The activity signal: newest write among the Codex session journals."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from aiteam.clock import from_timestamp
from aiteam.services import codex_activity


def _journal(root, relative, mtime):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"event_msg"}\n')
    os.utime(path, (mtime, mtime))
    return path


def test_newest_journal_write_across_both_trees(tmp_path):
    _journal(tmp_path, "sessions/2026/09/21/rollout-a.jsonl", 1_790_000_000)
    _journal(tmp_path, "archived_sessions/rollout-b.jsonl", 1_790_000_500)
    _journal(tmp_path, "sessions/2026/09/29/notes.txt", 1_799_999_999)  # not a journal
    assert codex_activity.latest_journal_write(tmp_path) == 1_790_000_500


def test_symlinks_are_not_followed(tmp_path):
    _journal(tmp_path, "sessions/rollout-a.jsonl", 1_790_000_000)
    target = _journal(tmp_path, "elsewhere/rollout-z.jsonl", 1_799_000_000)
    (tmp_path / "sessions" / "link.jsonl").symlink_to(target)
    assert codex_activity.latest_journal_write(tmp_path) == 1_790_000_000


@pytest.mark.parametrize("setup", ["missing", "too-many"])
def test_what_cannot_be_established_is_unknown(tmp_path, setup):
    if setup == "too-many":
        for index in range(5):
            _journal(tmp_path, f"sessions/rollout-{index}.jsonl", 1_790_000_000)
        assert codex_activity.latest_journal_write(tmp_path, max_entries=3) is None
    else:
        assert codex_activity.latest_journal_write(tmp_path) is None


@pytest.mark.asyncio
async def test_active_since_compares_with_the_last_capture(tmp_path):
    _journal(tmp_path, "sessions/2026/09/21/rollout-a.jsonl", 1_790_000_000)
    written = from_timestamp(1_790_000_000)
    assert await codex_activity.active_since(written - timedelta(seconds=1), tmp_path) is True
    assert await codex_activity.active_since(written, tmp_path) is False
    assert await codex_activity.active_since(written, tmp_path / "nowhere") is None


def test_the_probe_reads_the_capture_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_activity.codex_home() == tmp_path
