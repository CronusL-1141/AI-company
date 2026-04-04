"""Unit tests for BM25 retriever upgrade.

Covers:
- _tokenize_bm25(): Chinese bigram + English word tokenization
- bm25_search(): BM25Okapi ranking, graceful fallback when unavailable
- bm25_available(): reflects rank_bm25 install state
- rank_by_relevance(): uses BM25 when available
- MemoryStore.retrieve(): BM25 integrated in hot cache layer
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from aiteam.memory.retriever import (
    _tokenize_bm25,
    bm25_available,
    bm25_search,
    keyword_search,
    rank_by_relevance,
)
from aiteam.memory.store import MemoryStore
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory, MemoryScope


# ============================================================
# Helpers
# ============================================================


def _make_memory(content: str, scope_id: str = "test") -> Memory:
    return Memory(
        scope=MemoryScope.AGENT,
        scope_id=scope_id,
        content=content,
        metadata={},
        created_at=datetime.now(),
        accessed_at=datetime.now(),
    )


# ============================================================
# _tokenize_bm25
# ============================================================


class TestTokenizeBM25:
    """Test the BM25 tokenizer."""

    def test_english_words(self) -> None:
        """English text produces lowercased word tokens."""
        tokens = _tokenize_bm25("FastAPI is a Python framework")
        assert "fastapi" in tokens
        assert "python" in tokens
        assert "framework" in tokens

    def test_english_short_words_excluded(self) -> None:
        """Single-character English tokens are excluded (noise)."""
        tokens = _tokenize_bm25("a b c hello")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "hello" in tokens

    def test_chinese_individual_chars(self) -> None:
        """Chinese text produces individual character tokens."""
        tokens = _tokenize_bm25("人工智能")
        assert "人" in tokens
        assert "工" in tokens
        assert "智" in tokens
        assert "能" in tokens

    def test_chinese_bigrams(self) -> None:
        """Chinese text produces bigram tokens."""
        tokens = _tokenize_bm25("人工智能")
        assert "人工" in tokens
        assert "工智" in tokens
        assert "智能" in tokens

    def test_single_chinese_char_no_bigrams(self) -> None:
        """Single Chinese character produces no bigrams."""
        tokens = _tokenize_bm25("好")
        assert "好" in tokens
        # No bigrams possible from single char
        bigrams = [t for t in tokens if len(t) == 2]
        assert len(bigrams) == 0

    def test_mixed_chinese_english(self) -> None:
        """Mixed text produces tokens from both languages."""
        tokens = _tokenize_bm25("Python 人工智能 API")
        assert "python" in tokens
        assert "api" in tokens
        assert "人工" in tokens
        assert "智能" in tokens

    def test_empty_string(self) -> None:
        """Empty string returns empty list."""
        tokens = _tokenize_bm25("")
        assert tokens == []

    def test_returns_list(self) -> None:
        """Result is always a list (required by BM25Okapi)."""
        tokens = _tokenize_bm25("some text")
        assert isinstance(tokens, list)


# ============================================================
# bm25_available
# ============================================================


class TestBM25Available:
    """Test bm25_available() reflects import state."""

    def test_returns_bool(self) -> None:
        result = bm25_available()
        assert isinstance(result, bool)

    def test_false_when_import_fails(self) -> None:
        """When rank_bm25 is mocked as unavailable, bm25_available() returns False."""
        with patch("aiteam.memory.retriever._BM25_AVAILABLE", False):
            assert bm25_available() is False

    def test_true_when_import_succeeds(self) -> None:
        """When rank_bm25 is mocked as available, bm25_available() returns True."""
        with patch("aiteam.memory.retriever._BM25_AVAILABLE", True):
            assert bm25_available() is True


# ============================================================
# bm25_search
# ============================================================


class TestBM25Search:
    """Test BM25 search function."""

    def test_empty_memories(self) -> None:
        """Empty memory list returns empty list."""
        result = bm25_search([], "query")
        assert result == []

    def test_empty_query_returns_all(self) -> None:
        """Empty query returns all memories unchanged."""
        mems = [_make_memory("Python FastAPI"), _make_memory("React JavaScript")]
        result = bm25_search(mems, "")
        assert len(result) == 2

    def test_ranks_relevant_first(self) -> None:
        """More relevant memory appears first."""
        mems = [
            _make_memory("React is a JavaScript UI library"),
            _make_memory("Python FastAPI backend framework for APIs"),
            _make_memory("Python is great for data science and API development"),
        ]
        result = bm25_search(mems, "Python API")
        assert len(result) >= 1
        # Python-related content should rank above React
        contents = [m.content for m in result]
        python_positions = [i for i, c in enumerate(contents) if "Python" in c]
        react_positions = [i for i, c in enumerate(contents) if "React" in c]
        if react_positions:
            assert min(python_positions) < min(react_positions)

    def test_ranks_unique_term_first(self) -> None:
        """Memory containing a query term unique to that doc ranks first.

        With 3+ docs, BM25 IDF is positive for a term appearing in 1 doc,
        so that doc scores highest.
        """
        mems = [
            _make_memory("completely unrelated content xyz"),
            _make_memory("completely unrelated other text abc"),
            _make_memory("Python programming language"),
        ]
        result = bm25_search(mems, "Python")
        assert len(result) >= 1
        # Python memory should rank first (only doc containing "python")
        assert "Python" in result[0].content

    def test_fallback_when_all_scores_zero(self) -> None:
        """Falls back to keyword_search when all BM25 scores are zero (small corpus)."""
        # 2-doc corpus: BM25Okapi IDF = log(0.5/1.5) < 0 → clamped to 0
        mems = [
            _make_memory("completely unrelated content xyz"),
            _make_memory("Python programming language"),
        ]
        result = bm25_search(mems, "Python")
        # Should still return Python-related result via keyword fallback
        assert any("Python" in m.content for m in result)

    def test_chinese_query(self) -> None:
        """Chinese query matches Chinese content via bigrams."""
        mems = [
            _make_memory("Python后端开发框架"),
            _make_memory("前端React组件开发"),
            _make_memory("人工智能机器学习算法"),
        ]
        result = bm25_search(mems, "人工智能")
        if result:
            assert "人工智能" in result[0].content

    def test_fallback_when_bm25_unavailable(self) -> None:
        """Falls back to keyword_search when rank_bm25 is not available."""
        mems = [
            _make_memory("Python FastAPI"),
            _make_memory("React JavaScript"),
        ]
        with patch("aiteam.memory.retriever._BM25_AVAILABLE", False):
            result = bm25_search(mems, "Python")
        # Should return Python-related result (keyword fallback works)
        assert any("Python" in m.content for m in result)

    def test_all_empty_docs_returns_all(self) -> None:
        """When all documents tokenize to empty, returns all memories."""
        # Memories with only punctuation/spaces that produce no tokens
        mems = [_make_memory("   "), _make_memory("!!!")]
        result = bm25_search(mems, "query")
        assert len(result) == 2


# ============================================================
# rank_by_relevance (BM25-upgraded)
# ============================================================


class TestRankByRelevance:
    """Test rank_by_relevance uses BM25 when available."""

    def test_returns_all_memories(self) -> None:
        """All memories are returned (relevant + unranked appended at end)."""
        mems = [
            _make_memory("Python backend"),
            _make_memory("completely irrelevant xyz123"),
            _make_memory("Python API development"),
        ]
        result = rank_by_relevance(mems, "Python")
        assert len(result) == 3

    def test_relevant_before_irrelevant(self) -> None:
        """Relevant memories appear before irrelevant ones.

        Needs 3+ docs for BM25 IDF to be positive (BM25Okapi clamps negative IDF to 0).
        """
        relevant = _make_memory("Python FastAPI is excellent for APIs")
        filler = _make_memory("Java Spring Boot web framework development")
        irrelevant = _make_memory("this has no matching content zzzxxx")
        mems = [irrelevant, filler, relevant]
        result = rank_by_relevance(mems, "Python FastAPI")
        # Relevant should appear ahead of irrelevant
        result_contents = [m.content for m in result]
        relevant_pos = result_contents.index(relevant.content)
        irrelevant_pos = result_contents.index(irrelevant.content)
        assert relevant_pos < irrelevant_pos

    def test_empty_query_preserves_order(self) -> None:
        """Empty query returns memories in original order."""
        mems = [_make_memory("first"), _make_memory("second")]
        result = rank_by_relevance(mems, "")
        assert [m.content for m in result] == ["first", "second"]

    def test_fallback_without_bm25(self) -> None:
        """Without BM25, falls back to keyword hit count ranking."""
        mems = [
            _make_memory("Python Python Python heavily repeated"),
            _make_memory("Python once"),
            _make_memory("unrelated content"),
        ]
        with patch("aiteam.memory.retriever._BM25_AVAILABLE", False):
            result = rank_by_relevance(mems, "Python")
        # Should rank Python content above unrelated
        contents = [m.content for m in result]
        unrelated_pos = next(i for i, c in enumerate(contents) if "unrelated" in c)
        assert unrelated_pos == len(result) - 1


# ============================================================
# MemoryStore integration with BM25
# ============================================================


class TestMemoryStoreBM25Integration:
    """Test MemoryStore.retrieve() uses bm25_search on hot cache."""

    @pytest.mark.asyncio
    async def test_retrieve_uses_bm25_on_hot_cache(
        self, db_repository: StorageRepository
    ) -> None:
        """MemoryStore.retrieve() uses bm25_search for hot cache ranking."""
        store = MemoryStore(db_repository)

        await store.store("agent", "agent-bm25", "Python FastAPI backend development")
        await store.store("agent", "agent-bm25", "React JavaScript frontend UI")
        await store.store("agent", "agent-bm25", "Python data science and machine learning")

        results = await store.retrieve("agent", "agent-bm25", "Python backend", limit=5)
        assert len(results) >= 1
        # Python-related content should appear in results
        assert any("Python" in m.content for m in results)

    @pytest.mark.asyncio
    async def test_retrieve_fallback_without_bm25(
        self, db_repository: StorageRepository
    ) -> None:
        """MemoryStore.retrieve() still works when rank_bm25 unavailable."""
        store = MemoryStore(db_repository)

        await store.store("agent", "agent-fallback", "Python FastAPI")
        await store.store("agent", "agent-fallback", "React JavaScript")

        with patch("aiteam.memory.retriever._BM25_AVAILABLE", False):
            results = await store.retrieve("agent", "agent-fallback", "Python", limit=5)

        assert len(results) >= 1
        assert any("Python" in m.content for m in results)

    @pytest.mark.asyncio
    async def test_bm25_ranking_better_than_keyword_for_idf(
        self, db_repository: StorageRepository
    ) -> None:
        """BM25 IDF weighting: rare term match scores higher than common term match."""
        store = MemoryStore(db_repository)

        # "database" appears in many docs (common) — low IDF
        # "postgresql" appears in only one doc (rare) — high IDF
        await store.store("agent", "agent-idf", "database connection database query database")
        await store.store("agent", "agent-idf", "postgresql database for persistent storage")
        await store.store("agent", "agent-idf", "database ORM sqlalchemy database model")

        results = await store.retrieve("agent", "agent-idf", "postgresql", limit=3)
        # The memory mentioning postgresql specifically should rank highest
        if results:
            assert "postgresql" in results[0].content.lower()
