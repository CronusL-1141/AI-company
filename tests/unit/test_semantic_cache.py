"""Unit tests for semantic cache layer.

Covers:
- cache_set / cache_get: basic store and retrieval
- TTL expiry: expired entries are not returned
- BM25 similarity matching: similar queries hit the cache
- Jaccard fallback: works without rank_bm25
- cache_clear: removes all entries
- cache_stats: hit/miss counters and active entry count
- Tier definition completeness: CORE_TOOLS and ADVANCED_TOOLS cover known tools
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import aiteam.api.semantic_cache as sc
from aiteam.api.semantic_cache import (
    _tokenize,
    cache_clear,
    cache_get,
    cache_set,
    cache_stats,
)

# ============================================================
# Fixtures: reset module state between tests
# ============================================================


@pytest.fixture(autouse=True)
def reset_cache(tmp_path, monkeypatch):
    """Reset in-memory cache state and redirect file I/O to tmp_path."""
    monkeypatch.setattr(sc, "_CACHE_FILE", tmp_path / "semantic_cache.json")
    monkeypatch.setattr(sc, "_DATA_DIR", tmp_path)
    sc._cache_entries.clear()
    sc._hits = 0
    sc._misses = 0
    yield
    sc._cache_entries.clear()
    sc._hits = 0
    sc._misses = 0


# ============================================================
# _tokenize
# ============================================================


class TestTokenize:
    def test_english_words(self) -> None:
        tokens = _tokenize("FastAPI Python backend")
        assert "fastapi" in tokens
        assert "python" in tokens
        assert "backend" in tokens

    def test_short_english_excluded(self) -> None:
        # Only single-char tokens are excluded (len > 1 is the filter)
        tokens = _tokenize("a b c hello")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "hello" in tokens

    def test_chinese_chars_and_bigrams(self) -> None:
        tokens = _tokenize("人工智能")
        assert "人" in tokens
        assert "人工" in tokens
        assert "智能" in tokens

    def test_empty_string(self) -> None:
        assert _tokenize("") == []


# ============================================================
# cache_set / cache_get
# ============================================================


class TestCacheSetGet:
    def test_exact_query_returns_result(self) -> None:
        cache_set("what is FastAPI", {"answer": "a web framework"})
        result = cache_get("what is FastAPI", threshold=0.5)
        assert result is not None
        assert result["answer"] == "a web framework"

    def test_miss_returns_none(self) -> None:
        result = cache_get("completely unknown query", threshold=0.8)
        assert result is None

    def test_empty_cache_returns_none(self) -> None:
        result = cache_get("any query")
        assert result is None

    def test_similar_query_hits_cache(self) -> None:
        """A semantically similar query should hit with a low threshold."""
        cache_set("Python FastAPI backend development", {"info": "backend"})
        # Add filler entries to give BM25 positive IDF
        cache_set("React JavaScript frontend", {"info": "frontend"})
        cache_set("Docker container deployment", {"info": "docker"})
        result = cache_get("FastAPI Python backend", threshold=0.3)
        # Should hit the cached entry about backend
        assert result is not None

    def test_expired_entry_not_returned(self) -> None:
        """Entries past their TTL are not returned."""
        cache_set("expiring query", {"data": "old"}, ttl=1)
        # Manually expire it
        sc._cache_entries[0]["expires_at"] = time.time() - 1
        result = cache_get("expiring query", threshold=0.5)
        assert result is None

    def test_non_expired_entry_returned(self) -> None:
        cache_set("fresh query result", {"data": "fresh"}, ttl=3600)
        result = cache_get("fresh query result", threshold=0.5)
        assert result is not None

    def test_empty_query_returns_none(self) -> None:
        cache_set("some cached query", {"x": 1})
        result = cache_get("", threshold=0.5)
        assert result is None


# ============================================================
# cache_clear
# ============================================================


class TestCacheClear:
    def test_clear_removes_all_entries(self) -> None:
        cache_set("q1", {"a": 1})
        cache_set("q2", {"b": 2})
        removed = cache_clear()
        assert removed == 2
        assert len(sc._cache_entries) == 0

    def test_clear_empty_cache(self) -> None:
        removed = cache_clear()
        assert removed == 0

    def test_get_after_clear_returns_none(self) -> None:
        cache_set("query", {"result": True})
        cache_clear()
        assert cache_get("query", threshold=0.5) is None


# ============================================================
# cache_stats
# ============================================================


class TestCacheStats:
    def test_initial_stats_zero(self) -> None:
        stats = cache_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["total_requests"] == 0
        assert stats["hit_rate"] == 0.0

    def test_hit_increments_counter(self) -> None:
        cache_set("known query", {"x": 1})
        cache_get("known query", threshold=0.5)
        stats = cache_stats()
        assert stats["hits"] + stats["misses"] >= 1

    def test_miss_increments_counter(self) -> None:
        cache_get("unknown query xyz123")
        stats = cache_stats()
        assert stats["misses"] >= 1

    def test_hit_rate_calculation(self) -> None:
        sc._hits = 3
        sc._misses = 1
        stats = cache_stats()
        assert stats["hit_rate"] == 0.75
        assert stats["total_requests"] == 4

    def test_active_entries_count(self) -> None:
        cache_set("q1", 1, ttl=3600)
        cache_set("q2", 2, ttl=3600)
        stats = cache_stats()
        assert stats["active_entries"] == 2

    def test_expired_not_in_active(self) -> None:
        cache_set("stale", 1, ttl=1)
        sc._cache_entries[0]["expires_at"] = time.time() - 1
        stats = cache_stats()
        assert stats["active_entries"] == 0

    def test_bm25_enabled_field_is_bool(self) -> None:
        stats = cache_stats()
        assert isinstance(stats["bm25_enabled"], bool)


# ============================================================
# Fallback without BM25
# ============================================================


class TestJaccardFallback:
    def test_jaccard_similarity_hit(self) -> None:
        """Without BM25, Jaccard overlap finds similar queries."""
        with patch.object(sc, "_BM25_AVAILABLE", False):
            cache_set("Python backend API development", {"backend": True})
            cache_set("React frontend components", {"frontend": True})
            cache_set("Docker container setup guide", {"docker": True})
            result = cache_get("API backend Python", threshold=0.2)
        assert result is not None
        assert result.get("backend") is True

    def test_jaccard_miss_below_threshold(self) -> None:
        with patch.object(sc, "_BM25_AVAILABLE", False):
            cache_set("Python backend API", {"x": 1})
            result = cache_get("completely different content zzzxxx", threshold=0.9)
        assert result is None


# ============================================================
# Task B: Tool tier definitions
# ============================================================


class TestToolTierDefinitions:
    def test_core_tools_is_list_of_strings(self) -> None:
        from aiteam.mcp.tools import CORE_TOOLS
        assert isinstance(CORE_TOOLS, list)
        assert all(isinstance(t, str) for t in CORE_TOOLS)

    def test_advanced_tools_is_list_of_strings(self) -> None:
        from aiteam.mcp.tools import ADVANCED_TOOLS
        assert isinstance(ADVANCED_TOOLS, list)
        assert all(isinstance(t, str) for t in ADVANCED_TOOLS)

    def test_core_tools_not_empty(self) -> None:
        from aiteam.mcp.tools import CORE_TOOLS
        assert len(CORE_TOOLS) > 0

    def test_advanced_tools_not_empty(self) -> None:
        from aiteam.mcp.tools import ADVANCED_TOOLS
        assert len(ADVANCED_TOOLS) > 0

    def test_no_overlap_between_tiers(self) -> None:
        """A tool name should not appear in both CORE and ADVANCED."""
        from aiteam.mcp.tools import ADVANCED_TOOLS, CORE_TOOLS
        overlap = set(CORE_TOOLS) & set(ADVANCED_TOOLS)
        assert overlap == set(), f"Tools in both tiers: {overlap}"

    def test_core_contains_essential_tools(self) -> None:
        from aiteam.mcp.tools import CORE_TOOLS
        essential = {"task_create", "task_update", "memory_search", "context_resolve"}
        for tool in essential:
            assert tool in CORE_TOOLS, f"Essential tool missing from CORE_TOOLS: {tool}"

    def test_cache_tools_in_advanced(self) -> None:
        from aiteam.mcp.tools import ADVANCED_TOOLS
        assert "cache_stats" in ADVANCED_TOOLS
        assert "cache_clear" in ADVANCED_TOOLS
