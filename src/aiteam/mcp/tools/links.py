"""Knowledge-link MCP tools — 跨域引用图谱查询（知识层 P1a）。"""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register knowledge-link tools."""

    @mcp.tool()
    def link_query(
        kind: str,
        id: str,
        direction: str = "both",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query cross-domain reference edges for an object (who references it / what it references).

        Edges are extracted automatically (zero-LLM regex) from task memos and
        reports: wf_<id> runs, commit hashes, task UUIDs, [[memory]] links.

        Args:
            kind: Endpoint kind — task_memo / report / task / run / commit / memory
            id: Endpoint ID (wf_id / uuid / short-hash / memory-slug)
            direction: "in" (who references it) / "out" (what it references) / "both"
            limit: Max edges to return (default 50)

        Returns:
            List of edges with link_type (references/fixes) and context snippets.
        """
        return _api_call(
            "GET",
            f"/api/links?kind={kind}&id={id}&direction={direction}&limit={limit}",
        )

    @mcp.tool()
    def link_trace(
        kind: str,
        id: str,
        depth: int = 2,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Trace the reference neighborhood of an object (undirected fanout, depth <= 2).

        Answers questions like "which tasks/reports touched commit 9d8f020"
        or "what work is connected to run wf_cbad7348".

        Args:
            kind: Seed kind — task_memo / report / task / run / commit / memory
            id: Seed ID
            depth: Traversal depth, 1-2 (default 2)
            limit: Max reachable nodes (default 50)

        Returns:
            Reachable nodes with hop distance, via link types, and path.
        """
        return _api_call(
            "GET",
            f"/api/links/fanout?kind={kind}&id={id}&depth={depth}&limit={limit}",
        )

    @mcp.tool()
    def unified_search(
        query: str,
        limit: int = 10,
        project_id: str = "",
    ) -> dict[str, Any]:
        """Search across all OS knowledge: task memos, reports, and tasks.

        Three-arm RRF fusion (k=60): BM25 full-text (Chinese bigram native),
        knowledge-graph fanout (queries containing wf_/commit/uuid IDs pull in
        everything linked to them), and exact ID-prefix / title match.

        Use this to recall past work: "归属铁律怎么修的", "wf_d01f207f",
        "stderr 盲区", commit hashes, etc.

        Args:
            query: Free text or an OS ID (wf_id / commit / task uuid)
            limit: Max results (default 10)
            project_id: Restrict to one project (empty = all)

        Returns:
            Ranked results with kind/id/title/snippet/score.
        """
        import urllib.parse

        q = urllib.parse.quote(query)
        return _api_call(
            "GET",
            f"/api/search?q={q}&limit={limit}&project_id={project_id}",
        )

