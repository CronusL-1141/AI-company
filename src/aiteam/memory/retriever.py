"""AI Team OS — Memory retriever.

Provides keyword search, BM25 search, relevance ranking, and context string building.
M1 phase uses keyword matching; M1.5 upgrades to BM25 when rank_bm25 is available.

BM25 dependency is optional — falls back to keyword_search gracefully:
    pip install rank-bm25        # or: pip install ai-team-os[bm25]
"""

from __future__ import annotations

import re

from aiteam.types import Memory

# Optional BM25 dependency — import lazily to avoid hard dependency
try:
    from rank_bm25 import BM25Okapi as _BM25Okapi

    _BM25_AVAILABLE = True
except ImportError:
    _BM25_AVAILABLE = False
    _BM25Okapi = None  # type: ignore[assignment,misc]


def bm25_available() -> bool:
    """Return True if rank_bm25 is installed and BM25 search is enabled."""
    return _BM25_AVAILABLE


def _tokenize(text: str) -> set[str]:
    """Split text into a lowercase keyword set (supports Chinese and English)."""
    # English: split by spaces/punctuation; Chinese: split by character
    tokens: set[str] = set()
    # English words
    for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        if len(word) > 1:
            tokens.add(word)
    # Chinese characters (each character as a token + contiguous Chinese as a phrase)
    chinese_chars = re.findall(r"[\u4e00-\u9fff]+", text)
    for phrase in chinese_chars:
        tokens.add(phrase)
        for char in phrase:
            tokens.add(char)
    return tokens


def _tokenize_bm25(text: str) -> list[str]:
    """Tokenize text into a list for BM25 indexing.

    Strategy:
    - English: split into individual words (lowercased, length > 1)
    - Chinese: bigrams (consecutive pairs) + individual characters

    Bigrams improve recall for Chinese phrases where word boundaries are
    absent — e.g. "人工智能" produces ["人工", "工智", "智能", "人", "工", "智", "能"].
    """
    tokens: list[str] = []

    # English tokens
    for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        if len(word) > 1:
            tokens.append(word)

    # Chinese: bigrams + individual characters
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        # Individual characters
        tokens.extend(list(phrase))
        # Bigrams
        for i in range(len(phrase) - 1):
            tokens.append(phrase[i : i + 2])

    return tokens


def bm25_search(memories: list[Memory], query: str) -> list[Memory]:
    """BM25-ranked memory search with Chinese bigram + English word tokenization.

    Uses BM25Okapi from rank_bm25 library. If rank_bm25 is not installed,
    falls back silently to keyword_search.

    BM25 advantages over simple keyword matching:
    - Term frequency saturation: avoids over-rewarding repeated terms
    - IDF weighting: rare terms score higher than common terms
    - Document length normalization: shorter docs don't get unfair advantage

    Args:
        memories: List of memories to search.
        query: Search query string.

    Returns:
        List of memories sorted by BM25 score descending (zero-score items excluded).
    """
    if not _BM25_AVAILABLE:
        # Graceful fallback
        return keyword_search(memories, query)

    if not memories:
        return []

    query_tokens = _tokenize_bm25(query)
    if not query_tokens:
        return list(memories)

    # Build corpus — one token list per memory
    corpus = [_tokenize_bm25(mem.content) for mem in memories]

    # Handle edge case: all documents are empty
    if all(len(doc) == 0 for doc in corpus):
        return list(memories)

    bm25 = _BM25Okapi(corpus)
    scores = bm25.get_scores(query_tokens)

    # Pair (score, memory) and filter zero-score results
    scored = [(score, mem) for score, mem in zip(scores, memories) if score > 0]

    # BM25Okapi clamps negative IDF to 0 in small corpora (N <= 2 with df=1 gives
    # IDF = log(0.5/1.5) < 0 → 0). Fall back to keyword_search in that case so
    # small hot-cache queries still return relevant results.
    if not scored:
        return keyword_search(memories, query)

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored]


def keyword_search(memories: list[Memory], query: str) -> list[Memory]:
    """Simple keyword matching search.

    Calculates the keyword hit count between each memory and the query,
    returning memories with hits > 0.

    Args:
        memories: List of memories to search.
        query: Search query string.

    Returns:
        List of matching memories (sorted by hit count descending).
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return list(memories)

    scored: list[tuple[int, Memory]] = []
    for mem in memories:
        mem_tokens = _tokenize(mem.content)
        hits = len(query_tokens & mem_tokens)
        if hits > 0:
            scored.append((hits, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored]


def rank_by_relevance(memories: list[Memory], query: str) -> list[Memory]:
    """Rank memories by relevance.

    Uses BM25 when available, otherwise falls back to keyword hit count.
    Memories with zero score are placed last.

    Args:
        memories: List of memories to rank.
        query: Query string.

    Returns:
        Sorted list of memories.
    """
    if _BM25_AVAILABLE:
        ranked = bm25_search(memories, query)
        # Append unranked items (those with zero BM25 score) at the end
        ranked_ids = {id(m) for m in ranked}
        unranked = [m for m in memories if id(m) not in ranked_ids]
        return ranked + unranked

    # Fallback: keyword hit count
    query_tokens = _tokenize(query)
    if not query_tokens:
        return list(memories)

    def _score(mem: Memory) -> int:
        mem_tokens = _tokenize(mem.content)
        return len(query_tokens & mem_tokens)

    return sorted(memories, key=_score, reverse=True)


def build_context_string(memories: list[Memory], max_tokens: int = 2000) -> str:
    """Format a memory list into a context string injectable into a prompt.

    Args:
        memories: List of memories.
        max_tokens: Maximum character limit (M1 phase approximates tokens by character count).

    Returns:
        Formatted context string.
    """
    if not memories:
        return ""

    parts: list[str] = []
    current_length = 0
    header = "=== 相关记忆 ===\n"
    current_length += len(header)
    parts.append(header)

    for i, mem in enumerate(memories, 1):
        entry = f"[{i}] ({mem.scope.value}/{mem.scope_id}) {mem.content}\n"
        if current_length + len(entry) > max_tokens:
            break
        parts.append(entry)
        current_length += len(entry)

    return "".join(parts)
