"""AI Team OS — Memory retriever.

Provides keyword search, built-in BM25 search, relevance ranking, and
context-string building.

BM25 is implemented in pure Python (Okapi BM25: term-frequency saturation +
IDF weighting + document-length normalization) with the existing Chinese
bigram + single-character tokenization. There is no third-party dependency —
``keyword_search`` remains only as a degenerate-corpus fallback.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from aiteam.types import Memory

# Okapi BM25 hyperparameters (standard defaults)
_BM25_K1 = 1.5  # term-frequency saturation point
_BM25_B = 0.75  # document-length normalization strength


def bm25_available() -> bool:
    """BM25 is a built-in pure-Python implementation — always available."""
    return True


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
    absent — e.g. "人工智能" produces ["人", "工", "智", "能", "人工", "工智", "智能"].
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


def _bm25_scores(corpus: list[list[str]], query_tokens: list[str]) -> list[float]:
    """Compute Okapi BM25 scores for every document against the query.

    Pure-Python implementation:
    - Term-frequency saturation controlled by k1
    - IDF via the non-negative variant ``ln(1 + (N - df + 0.5) / (df + 0.5))``
      (guarantees non-negative scores even for tiny corpora, unlike the
      classic BM25Okapi IDF that clamps negatives to zero)
    - Document-length normalization controlled by b and the average length

    Args:
        corpus: Token lists, one per document.
        query_tokens: Tokenized query (duplicates allowed for term emphasis).

    Returns:
        A BM25 score per document, in corpus order.
    """
    n_docs = len(corpus)
    doc_counters = [Counter(doc) for doc in corpus]
    doc_lens = [len(doc) for doc in corpus]
    avgdl = sum(doc_lens) / n_docs if n_docs else 0.0

    # IDF per unique query term (document frequency = docs containing the term)
    idf: dict[str, float] = {}
    for term in set(query_tokens):
        df = sum(1 for counter in doc_counters if term in counter)
        idf[term] = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

    scores: list[float] = []
    for counter, dl in zip(doc_counters, doc_lens):
        length_norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0.0))
        score = 0.0
        for term in query_tokens:
            tf = counter.get(term, 0)
            if tf == 0:
                continue
            score += idf[term] * (tf * (_BM25_K1 + 1)) / (tf + length_norm)
        scores.append(score)
    return scores


def bm25_search(memories: list[Memory], query: str) -> list[Memory]:
    """BM25-ranked memory search with Chinese bigram + English word tokenization.

    Uses the built-in pure-Python Okapi BM25. BM25 advantages over simple
    keyword matching:
    - Term frequency saturation: avoids over-rewarding repeated terms
    - IDF weighting: rare terms score higher than common terms
    - Document length normalization: shorter docs don't get an unfair advantage

    Args:
        memories: List of memories to search.
        query: Search query string.

    Returns:
        List of memories sorted by BM25 score descending (zero-score items excluded).
    """
    if not memories:
        return []

    query_tokens = _tokenize_bm25(query)
    if not query_tokens:
        return list(memories)

    # Build corpus — one token list per memory
    corpus = [_tokenize_bm25(mem.content) for mem in memories]

    # Edge case: all documents tokenize to empty
    if all(len(doc) == 0 for doc in corpus):
        return list(memories)

    scores = _bm25_scores(corpus, query_tokens)

    # Pair (score, memory) and filter zero-score results
    scored = [(score, mem) for score, mem in zip(scores, memories) if score > 0]

    # No positive BM25 signal (no query token appears in any document) — fall
    # back to keyword_search so degenerate/tiny corpora still surface hits.
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

    Uses the built-in BM25 to rank matching memories; memories with zero score
    are appended last (order preserved).

    Args:
        memories: List of memories to rank.
        query: Query string.

    Returns:
        Sorted list of memories (all inputs are returned).
    """
    if not _tokenize_bm25(query):
        return list(memories)

    ranked = bm25_search(memories, query)
    # Append unranked items (those with zero BM25 score) at the end
    ranked_ids = {id(m) for m in ranked}
    unranked = [m for m in memories if id(m) not in ranked_ids]
    return ranked + unranked


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
