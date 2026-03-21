"""AI Team OS — Memory retriever.

Provides keyword search, relevance ranking, and context string building.
M1 phase uses simple keyword matching; M2 will upgrade to vector search.
"""

from __future__ import annotations

import re

from aiteam.types import Memory


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

    M1 phase: sorted by keyword hit count. Memories with zero hits are placed last.

    Args:
        memories: List of memories to rank.
        query: Query string.

    Returns:
        Sorted list of memories.
    """
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
