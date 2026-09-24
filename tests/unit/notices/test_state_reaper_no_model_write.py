"""Regression: OS never rewrites the user's default model (docs/user-notice-design.md §7.1).

The removed behaviour: with a fable-family default model unseen for 3 days,
the reaper set ``model`` to ``opus`` in ~/.claude/settings.json and filed a
briefing. A full reap cycle must now leave the file byte-identical.
"""

from __future__ import annotations

import json
import os
import time

from aiteam.api import model_discovery
from aiteam.api.state_reaper import StateReaper
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


class _Bus:
    async def emit(self, *args, **kwargs):
        return None


async def test_reap_cycle_leaves_settings_bytes_unchanged(isolated_home, tmp_path, monkeypatch):
    settings = isolated_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = json.dumps({"model": "fable", "language": "Chinese", "hooks": {}}, indent=4).encode()
    settings.write_bytes(original)
    # A transcript that last mentioned fable four days ago.
    transcript = isolated_home / ".claude" / "projects" / "-p" / "t.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"message": {"model": "claude-fable-1"}}) + "\n" + "x" * 2048)
    old = time.time() - 4 * 86400
    os.utime(transcript, (old, old))
    monkeypatch.setenv("AITEAM_MODEL_AUTOFALLBACK", "on")
    model_discovery._cache.update(ts=0.0, data=None)

    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    await repo.init_db()
    try:
        reaper = StateReaper(repo=repo, event_bus=_Bus())
        await reaper._reap_cycle_for_repo(repo)
        await reaper._reap_cycle_for_repo(repo)
        assert settings.read_bytes() == original
        assert not (settings.parent / "settings.json.bak-aiteam").exists()
        briefings = await repo.list_briefings(status="all")
        assert not any("opus" in item.title or "模型" in item.title for item in briefings)
    finally:
        await close_db()


def test_the_fallback_machinery_is_gone():
    assert not hasattr(StateReaper, "_check_default_model_health")
    assert not hasattr(StateReaper, "_FABLE_MISSING_DAYS")
    # set_default_model stays: model_config_set still uses it explicitly.
    assert callable(model_discovery.set_default_model)
