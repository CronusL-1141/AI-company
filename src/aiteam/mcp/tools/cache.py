"""Semantic cache MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register semantic cache MCP tools."""

    @mcp.tool()
    def cache_stats() -> dict[str, Any]:
        """View semantic cache statistics — hit rate, entry count, BM25 availability.

        Returns:
            Dict with hits, misses, hit_rate, active_entries, bm25_enabled
        """
        return _api_call("GET", "/api/cache/stats")

    @mcp.tool()
    def cache_clear() -> dict[str, Any]:
        """Clear all semantic cache entries.

        Use when cache results are stale or after significant data changes.

        Returns:
            Dict with count of removed entries
        """
        return _api_call("POST", "/api/cache/clear")
