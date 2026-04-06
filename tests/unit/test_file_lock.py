"""Unit tests for file lock / workspace isolation.

Tests cover:
- acquire: success, conflict, re-acquire by same agent
- release: success, wrong agent, no lock
- check: free vs locked, shows remaining TTL
- list: empty, multiple locks, expired excluded
- TTL expiry: expired locks are transparently pruned
"""

from __future__ import annotations

import json
import time

import pytest

from aiteam.api.file_lock import (
    DEFAULT_TTL,
    acquire_lock,
    check_lock,
    list_locks,
    release_lock,
)

# ============================================================
# Fixture: redirect locks file to a temp directory
# ============================================================


@pytest.fixture(autouse=True)
def isolated_locks(tmp_path, monkeypatch):
    """Redirect the locks file to a temporary directory for each test."""
    import aiteam.api.file_lock as fl_module

    locks_dir = tmp_path / "file-locks"
    locks_file = locks_dir / "file-locks.json"
    monkeypatch.setattr(fl_module, "_LOCKS_DIR", locks_dir)
    monkeypatch.setattr(fl_module, "_LOCKS_FILE", locks_file)
    yield locks_file


# ============================================================
# acquire_lock
# ============================================================


class TestAcquireLock:
    def test_acquire_free_file_succeeds(self, tmp_path):
        target = str(tmp_path / "types.py")
        result = acquire_lock(target, "prompt-dev")
        assert result["success"] is True
        assert result["agent"] == "prompt-dev"
        assert result["ttl"] == DEFAULT_TTL

    def test_acquire_persists_to_disk(self, tmp_path, isolated_locks):
        target = str(tmp_path / "models.py")
        acquire_lock(target, "event-dev")
        data = json.loads(isolated_locks.read_text(encoding="utf-8"))
        assert len(data) == 1
        entry = next(iter(data.values()))
        assert entry["agent"] == "event-dev"

    def test_acquire_conflict_returns_failure(self, tmp_path):
        target = str(tmp_path / "shared.py")
        acquire_lock(target, "agent-a")
        result = acquire_lock(target, "agent-b")
        assert result["success"] is False
        assert result["held_by"] == "agent-a"
        assert "expires_in" in result

    def test_acquire_same_agent_blocked(self, tmp_path):
        """Even re-acquiring by the same agent fails — explicit re-lock not allowed."""
        target = str(tmp_path / "config.py")
        acquire_lock(target, "agent-a")
        result = acquire_lock(target, "agent-a")
        assert result["success"] is False

    def test_acquire_custom_ttl(self, tmp_path):
        target = str(tmp_path / "utils.py")
        result = acquire_lock(target, "dev", ttl=60)
        assert result["ttl"] == 60

    def test_acquire_after_expiry_succeeds(self, tmp_path):
        """An expired lock should not block a new acquire."""
        import aiteam.api.file_lock as fl_module
        target = str(tmp_path / "expired.py")
        acquire_lock(target, "old-agent", ttl=1)
        # Back-date the entry so it appears expired
        locks = json.loads(fl_module._LOCKS_FILE.read_text(encoding="utf-8"))
        key = next(iter(locks))
        locks[key]["acquired_at"] = time.time() - 10  # 10s ago, ttl=1 → expired
        fl_module._LOCKS_FILE.write_text(json.dumps(locks), encoding="utf-8")
        result = acquire_lock(target, "new-agent")
        assert result["success"] is True

    def test_path_normalization(self, tmp_path):
        """Different path representations of the same file produce one lock."""
        target = str(tmp_path / "types.py")
        acquire_lock(target, "agent-a")
        # Uppercase variant (simulates Windows case-insensitivity)
        result = acquire_lock(target.upper(), "agent-b")
        assert result["success"] is False


# ============================================================
# release_lock
# ============================================================


class TestReleaseLock:
    def test_release_by_owner_succeeds(self, tmp_path):
        target = str(tmp_path / "types.py")
        acquire_lock(target, "prompt-dev")
        result = release_lock(target, "prompt-dev")
        assert result["success"] is True

    def test_release_clears_lock(self, tmp_path):
        target = str(tmp_path / "types.py")
        acquire_lock(target, "agent-x")
        release_lock(target, "agent-x")
        check_result = check_lock(target)
        assert check_result["locked"] is False

    def test_release_by_non_owner_fails(self, tmp_path):
        target = str(tmp_path / "types.py")
        acquire_lock(target, "agent-a")
        result = release_lock(target, "agent-b")
        assert result["success"] is False
        assert "agent-a" in result["message"]

    def test_release_nonexistent_lock_fails(self, tmp_path):
        target = str(tmp_path / "notlocked.py")
        result = release_lock(target, "anyone")
        assert result["success"] is False

    def test_release_allows_re_acquire(self, tmp_path):
        target = str(tmp_path / "models.py")
        acquire_lock(target, "agent-a")
        release_lock(target, "agent-a")
        result = acquire_lock(target, "agent-b")
        assert result["success"] is True


# ============================================================
# check_lock
# ============================================================


class TestCheckLock:
    def test_free_file_returns_not_locked(self, tmp_path):
        target = str(tmp_path / "free.py")
        result = check_lock(target)
        assert result["locked"] is False

    def test_locked_file_returns_lock_info(self, tmp_path):
        target = str(tmp_path / "locked.py")
        acquire_lock(target, "some-agent", ttl=120)
        result = check_lock(target)
        assert result["locked"] is True
        assert result["held_by"] == "some-agent"
        assert 0 < result["expires_in"] <= 120

    def test_expired_lock_returns_not_locked(self, tmp_path):
        import aiteam.api.file_lock as fl_module
        target = str(tmp_path / "expired.py")
        acquire_lock(target, "old-agent", ttl=1)
        # Manually back-date the entry
        locks = json.loads(fl_module._LOCKS_FILE.read_text(encoding="utf-8"))
        key = next(iter(locks))
        locks[key]["acquired_at"] = time.time() - 10
        fl_module._LOCKS_FILE.write_text(json.dumps(locks), encoding="utf-8")
        result = check_lock(target)
        assert result["locked"] is False


# ============================================================
# list_locks
# ============================================================


class TestListLocks:
    def test_empty_returns_zero_count(self):
        result = list_locks()
        assert result["count"] == 0
        assert result["locks"] == []

    def test_multiple_locks_listed(self, tmp_path):
        acquire_lock(str(tmp_path / "a.py"), "dev-1")
        acquire_lock(str(tmp_path / "b.py"), "dev-2")
        result = list_locks()
        assert result["count"] == 2
        agents = {entry["agent"] for entry in result["locks"]}
        assert agents == {"dev-1", "dev-2"}

    def test_expired_locks_excluded(self, tmp_path):
        import aiteam.api.file_lock as fl_module
        acquire_lock(str(tmp_path / "live.py"), "dev-live", ttl=300)
        acquire_lock(str(tmp_path / "dead.py"), "dev-dead", ttl=1)
        # Back-date the dead lock
        locks = json.loads(fl_module._LOCKS_FILE.read_text(encoding="utf-8"))
        for key, info in locks.items():
            if info["agent"] == "dev-dead":
                info["acquired_at"] = time.time() - 10
        fl_module._LOCKS_FILE.write_text(json.dumps(locks), encoding="utf-8")
        result = list_locks()
        assert result["count"] == 1
        assert result["locks"][0]["agent"] == "dev-live"

    def test_list_sorted_by_path(self, tmp_path):
        acquire_lock(str(tmp_path / "z.py"), "dev-z")
        acquire_lock(str(tmp_path / "a.py"), "dev-a")
        result = list_locks()
        paths = [e["path"] for e in result["locks"]]
        assert paths == sorted(paths)
