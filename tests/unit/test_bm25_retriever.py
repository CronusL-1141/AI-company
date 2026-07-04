"""Unit tests for the built-in (pure-Python) BM25 retriever.

Covers:
- _tokenize_bm25(): Chinese bigram + English word tokenization
- bm25_search(): Okapi BM25 ranking, keyword_search fallback for degenerate corpora
- bm25_available(): built-in BM25 is always available
- rank_by_relevance(): BM25 ranking with zero-score items appended last
- MemoryStore.retrieve(): BM25 integrated in the hot-cache layer
- StorageRepository.search_memories(): SQL coarse-recall + BM25 rerank, scope-safe
"""

from __future__ import annotations

from datetime import datetime

import pytest

from aiteam.memory.retriever import (
    _tokenize_bm25,
    bm25_available,
    bm25_search,
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
        """Result is always a list."""
        tokens = _tokenize_bm25("some text")
        assert isinstance(tokens, list)


# ============================================================
# bm25_available
# ============================================================


class TestBM25Available:
    """The built-in BM25 has no optional dependency — always available."""

    def test_returns_true(self) -> None:
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

        The rare term gets a high IDF, so the only doc containing it scores highest.
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

    def test_tiny_corpus_still_ranks_match_first(self) -> None:
        """Even a 2-doc corpus surfaces the matching doc.

        The classic BM25Okapi IDF clamps to 0 here (log(0.5/1.5) < 0), collapsing
        all scores; the built-in non-negative IDF keeps the matching doc scored.
        """
        mems = [
            _make_memory("completely unrelated content xyz"),
            _make_memory("Python programming language"),
        ]
        result = bm25_search(mems, "Python")
        assert result[0].content == "Python programming language"

    def test_no_shared_token_falls_back_to_keyword(self) -> None:
        """When no query token appears in any doc, keyword_search fallback runs."""
        mems = [
            _make_memory("Python FastAPI backend"),
            _make_memory("React JavaScript frontend"),
        ]
        # "cobol" shares no bigram/word token with either doc → BM25 all-zero →
        # keyword fallback also finds nothing → empty result.
        result = bm25_search(mems, "cobol")
        assert result == []

    def test_chinese_query(self) -> None:
        """Chinese query matches Chinese content via bigrams."""
        mems = [
            _make_memory("Python后端开发框架"),
            _make_memory("前端React组件开发"),
            _make_memory("人工智能机器学习算法"),
        ]
        result = bm25_search(mems, "人工智能")
        assert result[0].content == "人工智能机器学习算法"

    def test_chinese_multiword_non_contiguous(self) -> None:
        """Chinese multi-term query matches even when terms are non-contiguous.

        The old whole-string LIKE ("%Python 部署%") would miss content where the
        terms are separated ("Python 后端 部署 指南"); token-based BM25 hits it.
        """
        target = _make_memory("Python 后端 部署 指南")
        mems = [
            _make_memory("React 前端 组件 教程"),
            target,
            _make_memory("数据库 索引 优化 手册"),
        ]
        result = bm25_search(mems, "Python 部署")
        assert result[0].content == target.content

    def test_all_empty_docs_returns_all(self) -> None:
        """When all documents tokenize to empty, returns all memories."""
        # Memories with only punctuation/spaces that produce no tokens
        mems = [_make_memory("   "), _make_memory("!!!")]
        result = bm25_search(mems, "query")
        assert len(result) == 2


# ============================================================
# rank_by_relevance
# ============================================================


class TestRankByRelevance:
    """Test rank_by_relevance ranks with BM25 and appends unranked last."""

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
        """Relevant memories appear before irrelevant ones."""
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

    def test_zero_score_memory_placed_last(self) -> None:
        """A memory with no matching token is appended at the end."""
        mems = [
            _make_memory("Python Python Python heavily repeated"),
            _make_memory("Python once"),
            _make_memory("unrelated content"),
        ]
        result = rank_by_relevance(mems, "Python")
        contents = [m.content for m in result]
        unrelated_pos = next(i for i, c in enumerate(contents) if "unrelated" in c)
        assert unrelated_pos == len(result) - 1


# ============================================================
# MemoryStore integration with BM25
# ============================================================


class TestMemoryStoreBM25Integration:
    """Test MemoryStore.retrieve() uses bm25_search on the hot cache."""

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


# ============================================================
# StorageRepository.search_memories: BM25 rerank + scope safety
# ============================================================


class TestSearchMemoriesBM25Rerank:
    """search_memories does SQL coarse-recall then BM25 rerank, scope-isolated."""

    @pytest.mark.asyncio
    async def test_chinese_multiword_hits_where_like_would_miss(
        self, db_repository: StorageRepository
    ) -> None:
        """Multi-term query hits content whose terms are non-contiguous.

        The old whole-string ilike ("%Python 部署%") would not match
        "Python 后端 部署 指南"; the BM25 rerank does.
        """
        await db_repository.create_memory("team", "t1", "Python 后端 部署 指南")
        await db_repository.create_memory("team", "t1", "React 前端 组件 教程")

        results = await db_repository.search_memories("team", "t1", "Python 部署", limit=5)
        assert len(results) >= 1
        assert results[0].content == "Python 后端 部署 指南"

    @pytest.mark.asyncio
    async def test_rerank_respects_scope(
        self, db_repository: StorageRepository
    ) -> None:
        """BM25 rerank never leaks memories from another scope/scope_id."""
        # Same content lives under two different scope_ids.
        await db_repository.create_memory("agent", "agent-a", "Python deployment guide")
        await db_repository.create_memory("agent", "agent-b", "Python deployment guide")
        await db_repository.create_memory("agent", "agent-a", "React frontend notes")

        results = await db_repository.search_memories("agent", "agent-a", "Python", limit=10)
        assert len(results) >= 1
        # Every result must belong to the queried scope_id.
        assert all(m.scope_id == "agent-a" for m in results)
        assert all(m.scope.value == "agent" for m in results)

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(
        self, db_repository: StorageRepository
    ) -> None:
        """A query sharing no token with any in-scope memory returns empty."""
        await db_repository.create_memory("team", "t2", "Python backend service")
        results = await db_repository.search_memories("team", "t2", "Rust", limit=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_respects_limit(
        self, db_repository: StorageRepository
    ) -> None:
        """Result count never exceeds the requested limit."""
        for i in range(6):
            await db_repository.create_memory("team", "t3", f"Python topic number {i}")
        results = await db_repository.search_memories("team", "t3", "Python", limit=3)
        assert len(results) == 3
