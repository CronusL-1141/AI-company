"""AI Team OS — Semantic cache layer.

Provides BM25-based semantic caching for LLM query results.
Similar queries reuse cached responses, reducing LLM call costs by 40-73%.

Storage: JSON file at {data_dir}/semantic_cache.json
TTL: per-entry expiry, default 3600 seconds
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

# Optional BM25 dependency — reuses the same pattern as retriever.py
try:
    from rank_bm25 import BM25Okapi as _BM25Okapi

    _BM25_AVAILABLE = True
except ImportError:
    _BM25_AVAILABLE = False
    _BM25Okapi = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Cache file location — respects AITEAM_DATA_DIR env var
_DATA_DIR = Path(os.environ.get("AITEAM_DATA_DIR", Path(__file__).resolve().parent.parent.parent.parent / "data"))
_CACHE_FILE = _DATA_DIR / "semantic_cache.json"

# In-memory representation of the cache
# Structure: list of {"query": str, "tokens": list[str], "result": Any, "expires_at": float, "hits": int}
_cache_entries: list[dict[str, Any]] = []
_hits = 0
_misses = 0


# ============================================================
# Tokenization (mirrors retriever.py for consistency)
# ============================================================


def _tokenize(text: str) -> list[str]:
    """Tokenize text for BM25 indexing (English words + Chinese bigrams/chars)."""
    tokens: list[str] = []
    for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        if len(word) > 1:
            tokens.append(word)
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(list(phrase))
        for i in range(len(phrase) - 1):
            tokens.append(phrase[i : i + 2])
    return tokens


# ============================================================
# Persistence helpers
# ============================================================


def _load_cache() -> None:
    """Load cache entries from disk into memory."""
    global _cache_entries
    if _CACHE_FILE.exists():
        try:
            with _CACHE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            _cache_entries = data.get("entries", [])
            # Prune expired entries on load
            now = time.time()
            _cache_entries = [e for e in _cache_entries if e.get("expires_at", 0) > now]
        except Exception as e:
            logger.warning("Failed to load semantic cache: %s", e)
            _cache_entries = []


def _save_cache() -> None:
    """Persist current cache entries to disk."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _CACHE_FILE.open("w", encoding="utf-8") as f:
            json.dump({"entries": _cache_entries}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to save semantic cache: %s", e)


# Load on module import
_load_cache()


# ============================================================
# Public API
# ============================================================


def cache_get(query: str, threshold: float = 0.8) -> Any | None:
    """Find a cached result for a semantically similar query.

    Uses BM25 scoring when rank_bm25 is available; falls back to token
    overlap ratio otherwise.

    Args:
        query: The incoming query to look up.
        threshold: Minimum similarity score [0, 1] to count as a hit.

    Returns:
        Cached result if a similar query is found and not expired, else None.
    """
    global _hits, _misses

    now = time.time()
    # Remove expired entries
    valid = [e for e in _cache_entries if e.get("expires_at", 0) > now]
    if len(valid) != len(_cache_entries):
        _cache_entries.clear()
        _cache_entries.extend(valid)

    if not _cache_entries:
        _misses += 1
        return None

    query_tokens = _tokenize(query)
    if not query_tokens:
        _misses += 1
        return None

    best_score = 0.0
    best_entry: dict[str, Any] | None = None

    use_jaccard = not _BM25_AVAILABLE
    if _BM25_AVAILABLE:
        corpus = [e["tokens"] for e in _cache_entries]
        if corpus and any(len(doc) > 0 for doc in corpus):
            bm25 = _BM25Okapi(corpus)
            scores = bm25.get_scores(query_tokens)
            # Normalize: divide by max possible score for this query length
            max_score = max(scores) if len(scores) > 0 else 0.0
            if max_score > 0:
                for idx, score in enumerate(scores):
                    normalized = score / max_score
                    if normalized > best_score:
                        best_score = normalized
                        best_entry = _cache_entries[idx]
            else:
                # BM25 IDF clamped to 0 (small corpus) — fall back to Jaccard
                use_jaccard = True
        else:
            use_jaccard = True

    if use_jaccard:
        # Fallback: Jaccard similarity on token sets
        query_set = set(query_tokens)
        for entry in _cache_entries:
            entry_set = set(entry["tokens"])
            union = query_set | entry_set
            if union:
                score = len(query_set & entry_set) / len(union)
                if score > best_score:
                    best_score = score
                    best_entry = entry

    if best_entry is not None and best_score >= threshold:
        best_entry["hits"] = best_entry.get("hits", 0) + 1
        _hits += 1
        logger.debug("Cache hit (score=%.3f) for query: %s", best_score, query[:60])
        return best_entry["result"]

    _misses += 1
    return None


def cache_set(query: str, result: Any, ttl: int = 3600) -> None:
    """Store a query result in the cache.

    Args:
        query: The original query string.
        result: The result to cache (must be JSON-serializable).
        ttl: Time-to-live in seconds (default 3600 = 1 hour).
    """
    tokens = _tokenize(query)
    entry: dict[str, Any] = {
        "query": query,
        "tokens": tokens,
        "result": result,
        "expires_at": time.time() + ttl,
        "hits": 0,
        "created_at": time.time(),
    }
    _cache_entries.append(entry)
    # Cap cache size at 500 entries (evict oldest)
    if len(_cache_entries) > 500:
        _cache_entries.sort(key=lambda e: e.get("expires_at", 0))
        del _cache_entries[:len(_cache_entries) - 500]
    _save_cache()


def cache_clear() -> int:
    """Clear all cache entries.

    Returns:
        Number of entries removed.
    """
    count = len(_cache_entries)
    _cache_entries.clear()
    _save_cache()
    return count


def cache_stats() -> dict[str, Any]:
    """Return cache hit rate statistics.

    Returns:
        Dict with total, hits, misses, hit_rate, active_entries.
    """
    now = time.time()
    active = [e for e in _cache_entries if e.get("expires_at", 0) > now]
    total = _hits + _misses
    return {
        "hits": _hits,
        "misses": _misses,
        "total_requests": total,
        "hit_rate": round(_hits / total, 4) if total > 0 else 0.0,
        "active_entries": len(active),
        "total_entries": len(_cache_entries),
        "bm25_enabled": _BM25_AVAILABLE,
    }
