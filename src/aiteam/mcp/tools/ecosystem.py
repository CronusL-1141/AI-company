"""Claude ecosystem wide-index MCP tools.

Provides:
- ecosystem_scan: legacy scan, now wraps each invocation in an EcosystemScanRun record
- ecosystem_search: query existing archive
- ecosystem_scan_periodic: incremental / full scan with secondary filtering (Stage C)
- ecosystem_scan_status: query a single scan run by id
- ecosystem_scan_history: list recent scan runs
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id
from aiteam.mcp.tools.views import (
    ECO_LIST_HINT,
    FIELDS_ERROR,
    compact_profile_row,
    resolve_view,
)


def _project_headers(project_id: str = "") -> dict[str, str]:
    """生成 ecosystem 工具 API 调用所需的 X-Project-Id header。

    优先级: 显式 project_id > session 默认值 (cwd 推断)。
    返回空 dict 表示不带显式作用域，路由侧将回退到 X-Project-Dir 解析。
    """
    pid = _resolve_project_id(project_id)
    if pid:
        return {"X-Project-Id": pid}
    return {}


# ============================================================
# Heuristic classification helpers
# ============================================================

_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("mcp-server", ["mcp-server", "mcp_server", "model-context-protocol", " mcp ", "mcp tool"]),
    ("agent-framework",
     ["agent-framework", "agent_framework", "multi-agent", "langgraph", "autogen", "crewai", "agentic"]),
    ("memory-system", ["memory", "mem0", "vector-memory", "rag", "retrieval augmented"]),
    ("skill-system", ["skill", "plugin", "extension", "claude-code", "claude code"]),
    ("tooling", ["cli", "sdk", "api-client", "library", "framework", "toolkit"]),
]

_EXCLUDE_REPOS = {
    "CronusL-1141/AI-company",
    "codeburn",
    "GenericAgent",
}

# JSON fields available from gh search repos (repositoryTopics is NOT supported by gh search repos)
_GH_JSON_FIELDS = "fullName,name,owner,description,stargazersCount,language,homepage,pushedAt"


def _fetch_repo_topics(full_name: str, timeout: int = 10) -> list[str]:
    """Fetch real GitHub topics for a repo via gh api repos/{owner}/{repo}.

    Returns empty list on any failure (network error, rate limit, not found).
    The caller should fall back to hint_topics when this returns [].
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{full_name}", "--jq", ".topics[]"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def _classify_repo(hint_topics: list[str], description: str | None, name: str) -> str | None:
    """Heuristic category assignment based on search-hint topics + description + name."""
    combined = " ".join(hint_topics).lower() + " " + (description or "").lower() + " " + name.lower()
    for category, keywords in _CATEGORY_RULES:
        for kw in keywords:
            if kw.lower() in combined:
                return category
    return "tooling"


def _compute_relevance_score(hint_topics: list[str], description: str | None, stars: int) -> int:
    """0-10 relevance score: keyword matches + stars bonus."""
    combined = " ".join(hint_topics).lower() + " " + (description or "").lower()
    score = 0
    for kw in ["claude", "anthropic", "mcp", "agent", "llm"]:
        if kw in combined:
            score += 2
    score = min(score, 8)
    if stars >= 50000:
        score = min(score + 2, 10)
    elif stars >= 15000:
        score = min(score + 1, 10)
    return score


def _should_exclude(repo_full_name: str) -> bool:
    """Check whether a repo should be excluded."""
    for excl in _EXCLUDE_REPOS:
        if excl.lower() in repo_full_name.lower():
            return True
    return False


def _run_gh_search(
    keyword: str,
    min_stars: int = 5000,
    topics: list[str] | None = None,
    limit: int = 100,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Call gh search repos and return parsed repo list.

    Args:
        keyword: Search keyword injected into query string.
        min_stars: Minimum stars threshold — acts as admission gate.
                   Defaults to 5000 (P1.A popularity_floor).
        topics: Optional list of GitHub topics to filter by.
        limit: Max results per query (gh --limit). Defaults to 100, max 1000.
        timeout: Subprocess timeout in seconds.
    """
    effective_limit = min(max(1, limit), 1000)  # gh max is 1000
    cmd = [
        "gh", "search", "repos",
        f"--limit={effective_limit}",
        f"--stars=>={min_stars}",
        f"--json={_GH_JSON_FIELDS}",
        "--sort=stars",
        "--order=desc",
    ]
    if keyword:
        cmd.insert(2, keyword)
    if topics:
        for t in topics:
            cmd.extend(["--topic", t])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0 or not result.stdout:
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception):
        return []


def _parse_gh_repo(
    item: dict[str, Any],
    min_stars: int,
    hint_topics: list[str] | None = None,
) -> dict[str, Any] | None:
    """Parse a gh JSON item into profile fields, filtering out ineligible items."""
    full_name = item.get("fullName", "")
    if not full_name or _should_exclude(full_name):
        return None

    stars = item.get("stargazersCount", 0)
    if stars < min_stars:
        return None

    owner_obj = item.get("owner") or {}
    owner = owner_obj.get("login", "") if isinstance(owner_obj, dict) else str(owner_obj)
    name = item.get("name", full_name.split("/")[-1])
    description = item.get("description") or None
    language = item.get("language") or None
    homepage = item.get("homepageUrl") or item.get("homepage") or None

    pushed_at_str = item.get("pushedAt") or None
    last_commit_at: datetime | None = None
    if pushed_at_str:
        try:
            last_commit_at = datetime.fromisoformat(pushed_at_str.replace("Z", "+00:00"))
        except ValueError:
            pass

    # Prefer real GitHub topics from the API; fall back to hint_topics from search query
    real_topics = _fetch_repo_topics(full_name)
    effective_topics = real_topics if real_topics else (hint_topics or [])
    needs_deep_review = stars < 15000
    category = _classify_repo(effective_topics, description, name)
    relevance_score = _compute_relevance_score(effective_topics, description, stars)
    one_line_summary = description[:200] if description else None

    return {
        "repo_full_name": full_name,
        "name": name,
        "owner": owner,
        "description": description,
        "stars": stars,
        "language": language,
        "topics": effective_topics,
        "homepage": homepage,
        "last_commit_at": last_commit_at.isoformat() if last_commit_at else None,
        "needs_deep_review": needs_deep_review,
        "relevance_category": category,
        "relevance_score": relevance_score,
        "one_line_summary": one_line_summary,
    }


# ============================================================
# gh search queries: (keyword, topics) pairs
# ============================================================

_SEARCH_QUERIES: list[tuple[str, list[str]]] = [
    ("", ["claude-code"]),
    ("", ["mcp"]),
    ("", ["mcp-server"]),
    ("", ["claude-agent"]),
    ("claude", ["agent-framework"]),
    ("claude", ["ai-agents"]),
    ("claude code plugin", []),
    ("anthropic agent", []),
]


# ============================================================
# Stage C secondary filter helpers (used inside ecosystem_scan and the
# scanner service via the API). Owner blacklist + description keyword
# whitelist; both keep the legacy tool minimal-footprint.
# ============================================================

_DEFAULT_OWNER_BLACKLIST: list[str] = ["Snailclimb", "CronusL-1141"]
_DEFAULT_KEYWORD_WHITELIST: list[str] = [
    "claude",
    "anthropic",
    "mcp",
    "agent",
    "llm",
    "skill",
    "orchestrat",
    "autonom",
]


def _passes_secondary_filter(
    parsed: dict[str, Any],
    owner_blacklist: list[str],
    keyword_whitelist: list[str],
) -> bool:
    """True when parsed repo passes owner blacklist + description keyword whitelist."""
    owner = (parsed.get("owner") or "").lower()
    if any(b.lower() == owner for b in owner_blacklist if b):
        return False
    blob = " ".join(
        [
            (parsed.get("description") or "").lower(),
            (parsed.get("name") or "").lower(),
            " ".join(parsed.get("topics") or []).lower(),
        ]
    )
    if not keyword_whitelist:
        return True
    return any(kw.lower() in blob for kw in keyword_whitelist)


def register(mcp: Any) -> None:
    """Register ecosystem MCP tools."""

    @mcp.tool()
    def ecosystem_scan(min_stars: int = 5000, dry_run: bool = False) -> dict[str, Any]:
        """Scan popular Claude ecosystem repos (>=min_stars) and update ecosystem_repo_profiles.

        Runs 8-10 gh search queries covering:
        - topic:claude-code / topic:mcp / topic:mcp-server / topic:claude-agent
        - topic:agent-framework + "claude" / topic:ai-agents + "claude"
        - "claude code plugin" / "anthropic agent"
        - anthropics org public repos

        Deduplicates + filters >=min_stars + excludes known repos (CronusL-1141/AI-company etc.)
        Sets needs_deep_review=True for stars < 15000.
        relevance_category is auto-classified heuristically (based on topics + description keywords).
        Returns: {scanned: int, new_profiles: int, updated_profiles: int, skipped: int}

        dry_run=True returns what would happen without writing to DB.
        """
        import time

        start = time.time()
        all_repos: dict[str, dict[str, Any]] = {}
        per_query_stats: dict[str, int] = {}
        errors: list[str] = []

        # Stage C: open a ScanRun record (best-effort; legacy callers still work
        # if API not available). Skipped during dry_run.
        scan_run_id: str | None = None
        if not dry_run:
            run_payload = {
                "strategy": "full" if min_stars <= 1000 else "incremental",
                "triggered_by": "manual",
                "notes": f"ecosystem_scan(min_stars={min_stars})",
            }
            run_resp = _api_call("POST", "/api/ecosystem/scan-runs", run_payload)
            if isinstance(run_resp, dict) and run_resp.get("id"):
                scan_run_id = run_resp["id"]

        for keyword, topics in _SEARCH_QUERIES:
            query_key = f"keyword={keyword!r} topics={topics}"
            try:
                items = _run_gh_search(keyword, min_stars=min_stars, topics=topics)
            except Exception as exc:  # graceful degrade — collect, continue
                errors.append(f"gh_search {query_key}: {exc!s}")
                items = []
            count = 0
            for item in items:
                parsed = _parse_gh_repo(item, min_stars, hint_topics=topics)
                if parsed is None:
                    continue
                if not _passes_secondary_filter(
                    parsed, _DEFAULT_OWNER_BLACKLIST, _DEFAULT_KEYWORD_WHITELIST
                ):
                    continue
                fn = parsed["repo_full_name"]
                if fn not in all_repos:
                    all_repos[fn] = parsed
                    count += 1
            per_query_stats[query_key] = count

        # Anthropics org
        try:
            org_result = subprocess.run(
                ["gh", "search", "repos", "--owner=anthropics", "--limit=100",
                 f"--stars=>={min_stars}",
                 f"--json={_GH_JSON_FIELDS}",
                 "--sort=stars", "--order=desc"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            org_items = (
                json.loads(org_result.stdout)
                if org_result.returncode == 0 and org_result.stdout and org_result.stdout.strip()
                else []
            )
        except Exception as exc:
            errors.append(f"gh_search org:anthropics: {exc!s}")
            org_items = []

        org_count = 0
        for item in org_items:
            parsed = _parse_gh_repo(item, min_stars, hint_topics=["anthropic", "claude"])
            if parsed is None:
                continue
            if not _passes_secondary_filter(
                parsed, _DEFAULT_OWNER_BLACKLIST, _DEFAULT_KEYWORD_WHITELIST
            ):
                continue
            fn = parsed["repo_full_name"]
            if fn not in all_repos:
                all_repos[fn] = parsed
                org_count += 1
        per_query_stats["org:anthropics"] = org_count

        total_scanned = len(all_repos)

        if dry_run:
            elapsed = round(time.time() - start, 1)
            return {
                "dry_run": True,
                "would_process": total_scanned,
                "per_query_stats": per_query_stats,
                "elapsed_seconds": elapsed,
                "errors": errors,
                "sample_repos": [
                    {"repo": k, "stars": v["stars"], "category": v["relevance_category"]}
                    for k, v in list(all_repos.items())[:10]
                ],
            }

        # Upsert via API
        new_count = 0
        updated_count = 0
        skipped_count = 0
        now_iso = datetime.now(tz=UTC).isoformat()

        for profile_data in all_repos.values():
            profile_data["last_scanned_at"] = now_iso
            if scan_run_id:
                profile_data["scan_run_id"] = scan_run_id
            result = _api_call("POST", "/api/ecosystem/profiles", profile_data)
            if result is None or (isinstance(result, dict) and result.get("success") is False):
                skipped_count += 1
                if isinstance(result, dict) and result.get("error"):
                    errors.append(f"upsert {profile_data['repo_full_name']}: {result['error']}")
            elif result.get("created"):
                new_count += 1
            else:
                updated_count += 1

        elapsed = round(time.time() - start, 1)

        cat_dist: dict[str, int] = {}
        for p in all_repos.values():
            cat = p.get("relevance_category") or "unknown"
            cat_dist[cat] = cat_dist.get(cat, 0) + 1

        # Close out the ScanRun
        if scan_run_id:
            _api_call(
                "POST",
                f"/api/ecosystem/scan-runs/{scan_run_id}/complete",
                {
                    "duration_seconds": elapsed,
                    "repos_added": new_count,
                    "repos_updated": updated_count,
                    "repos_skipped": skipped_count,
                    "errors": errors,
                },
            )

        return {
            "scanned": total_scanned,
            "new_profiles": new_count,
            "updated_profiles": updated_count,
            "skipped": skipped_count,
            "per_query_stats": per_query_stats,
            "category_distribution": cat_dist,
            "elapsed_seconds": elapsed,
            "errors": errors,
            "scan_run_id": scan_run_id,
        }

    @mcp.tool()
    def ecosystem_search(
        keyword: str = "",
        topic: str = "",
        min_stars: int = 0,
        max_stars: int = 0,
        category: str = "",
        language: str = "",
        pushed_after: str = "",
        is_archived: bool | None = None,
        needs_deep_review: bool | None = None,
        tags: list[str] | None = None,
        tag_match_mode: str = "all",
        sort: str = "stars",
        limit: int = 30,
        offset: int = 0,
        facet_counts: bool = False,
        project_id: str = "",
        fields: str = "compact",
    ) -> dict[str, Any]:
        """Query ecosystem_repo_profiles archive (Stage E enhanced).

        Default response is a COMPACT projection (marked by view="compact" +
        hint — it is a trimmed view, NOT missing fields): each profile row keeps
        repo/stars/lang/status + summary. Full profile of a single repo:
        ecosystem_repo_get(repo_full_name); full rows here: fields="all".

        Args:
            keyword: Keyword match (name / description / summary / repo_full_name / description_excerpt).
            topic: Topic keyword filter (JSON field LIKE match).
            min_stars: Minimum star count.
            max_stars: Maximum star count (0 = no limit).
            category: Category filter (agent-framework / mcp-server / memory-system / skill-system / tooling).
            language: Programming language filter (e.g. Python / TypeScript).
            pushed_after: ISO datetime — only repos with pushed_at >= this. Empty = no filter.
            is_archived: True/False to filter archived repos; None = no filter.
            needs_deep_review: True/False filter; None = no filter.
            tags: Tag name list, semantic capability filter.
            tag_match_mode: "all" (AND) / "any" (OR). Default all.
            sort: stars (default) / recency (pushed_at desc) / relevance (relevance_score desc).
            limit: Max results (default 30, server max 200).
            offset: Pagination offset.
            facet_counts: When True, response includes facet_counts (category/language/archived).
            fields: "compact" (default, trimmed projection) / "all" (full rows).

        Returns:
            {profiles: [...], total: N, limit, offset, [facet_counts]};
            compact view adds view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        params: list[tuple[str, Any]] = [("limit", limit), ("offset", offset)]
        if keyword:
            params.append(("keyword", keyword))
        if topic:
            params.append(("topic", topic))
        if min_stars > 0:
            params.append(("min_stars", min_stars))
        if max_stars > 0:
            params.append(("max_stars", max_stars))
        if category:
            params.append(("category", category))
        if language:
            params.append(("language", language))
        if pushed_after:
            params.append(("pushed_after", pushed_after))
        if is_archived is not None:
            params.append(("is_archived", is_archived))
        if needs_deep_review is not None:
            params.append(("needs_deep_review", needs_deep_review))
        if tags:
            params.append(("tags", ",".join(tags)))
            params.append(("tag_match_mode", tag_match_mode))
        if sort and sort != "stars":
            params.append(("sort", sort))
        if facet_counts:
            params.append(("facet_counts", True))

        query_string = "&".join(f"{k}={v}" for k, v in params)
        result = _api_call(
            "GET",
            f"/api/ecosystem/profiles?{query_string}",
            extra_headers=_project_headers(project_id),
        )
        if result is None:
            return {"profiles": [], "total": 0, "limit": limit, "offset": offset}
        if view == "all" or not isinstance(result, dict) or "profiles" not in result:
            return result
        out: dict[str, Any] = {
            k: result[k] for k in ("total", "limit", "offset") if k in result
        }
        out["profiles"] = [
            compact_profile_row(p) for p in result.get("profiles") or []
        ]
        if "facet_counts" in result:
            out["facet_counts"] = result["facet_counts"]
        out["view"] = "compact"
        out["hint"] = ECO_LIST_HINT
        return out

    @mcp.tool()
    def ecosystem_repo_get(
        repo_full_name: str = "",
        repo_id: str = "",
        relations_limit: int = 50,
        deep_reviews_limit: int = 20,
    ) -> dict[str, Any]:
        """Get holistic detail of an ecosystem repo (profile + tags + deep_reviews + relations + scan_run).

        Args:
            repo_full_name: "owner/repo" form. Mutually exclusive with repo_id (this takes precedence if both given).
            repo_id: Direct primary key.
            relations_limit: Max relations per direction (default 50).
            deep_reviews_limit: Max deep reviews to return (default 20).

        Returns:
            Holistic dict (profile, tags, deep_reviews, relations_from, relations_to, scan_run).
            On not-found: {"error": "not_found", ...}
        """
        identifier = repo_full_name or repo_id
        if not identifier:
            return {"error": "must provide repo_full_name or repo_id"}

        params: list[tuple[str, Any]] = [
            ("relations_limit", relations_limit),
            ("deep_reviews_limit", deep_reviews_limit),
        ]
        query_string = "&".join(f"{k}={v}" for k, v in params)
        result = _api_call(
            "GET",
            f"/api/ecosystem/profiles/{identifier}/full?{query_string}",
        )
        if result is None:
            return {
                "error": "not_found",
                "repo_full_name": repo_full_name,
                "repo_id": repo_id,
            }
        return result

    @mcp.tool()
    def ecosystem_search_by_capability(
        tags: list[str] | None = None,
        match_mode: str = "all",
        min_stars: int = 0,
        max_stars: int = 0,
        sort: str = "stars",
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search ecosystem repos by capability tags (reverse lookup from tag → repo).

        Args:
            tags: Tag name list (e.g. ["memory_system", "vector_db"]).
            match_mode: "all" (AND, default) / "any" (OR).
            min_stars / max_stars: Star range filter.
            sort: stars / recency / relevance.
            limit / offset: Pagination.

        Returns:
            {profiles: [...], total: N, matched_tags: [...], match_mode: ...}
        """
        if not tags:
            return {
                "profiles": [],
                "total": 0,
                "matched_tags": [],
                "match_mode": match_mode,
                "limit": limit,
                "offset": offset,
            }

        params: list[tuple[str, Any]] = [
            ("tags", ",".join(tags)),
            ("match_mode", match_mode),
            ("limit", limit),
            ("offset", offset),
            ("sort", sort),
        ]
        if min_stars > 0:
            params.append(("min_stars", min_stars))
        if max_stars > 0:
            params.append(("max_stars", max_stars))

        query_string = "&".join(f"{k}={v}" for k, v in params)
        result = _api_call("GET", f"/api/ecosystem/search/by_capability?{query_string}")
        if result is None:
            return {
                "profiles": [],
                "total": 0,
                "matched_tags": tags,
                "match_mode": match_mode,
                "limit": limit,
                "offset": offset,
            }
        return result

    # ============================================================
    # Stage C: periodic / incremental scan tools
    # ============================================================

    @mcp.tool()
    def ecosystem_scan_periodic(
        strategy: str = "incremental",
        min_stars: int = 1000,
        triggered_by: str = "manual",
        notes: str = "",
    ) -> dict[str, Any]:
        """Run an incremental or full ecosystem scan via the scanner service.

        Compared to ecosystem_scan, this tool:
        - skips repos last_scanned_at < 7 days (incremental strategy only)
        - applies secondary owner / keyword filters
        - marks repos pushed > 365 days ago as is_archived=True
        - records every run as an EcosystemScanRun for audit

        Args:
            strategy: "incremental" (skip recent), "full" (rescan all),
                "topic" (topic-only), "trending" (trending repos only).
            min_stars: Minimum star threshold for inclusion (default 1000 for Stage C).
            triggered_by: "manual" or "cron" — recorded on the ScanRun.
            notes: Optional human-readable note attached to the ScanRun.

        Returns:
            ScanRun summary with run_id, scanned, new_profiles, updated_profiles,
            skipped, archived_marked, errors, duration_seconds, per_query_stats.
        """
        payload = {
            "strategy": strategy,
            "min_stars": min_stars,
            "triggered_by": triggered_by,
            "notes": notes,
        }
        result = _api_call("POST", "/api/ecosystem/scan-runs/execute", payload)
        if result is None:
            return {
                "success": False,
                "error": "Scanner API unavailable; falling back to ecosystem_scan",
            }
        return result

    @mcp.tool()
    def ecosystem_refresh(notes: str = "") -> dict[str, Any]:
        """On-demand incremental refresh of the project's active ecosystem set.

        Replaces the retired weekly cron (2026-07-10 decision: CC is not
        always-on, so long-running timers are pointless — refresh happens
        when the user asks for it). For each active-set repo (top_n by
        stars) this probes GitHub once, writes a status snapshot, and
        re-queues a Stage 0 shallow summary only when the repo has new
        pushes; 404/403 mark the profile deleted/private.

        The response's ``hint`` field (present when repos were re-queued)
        reminds you to run the actual shallow scans via ultracode/Workflow
        and write results back with ecosystem_apply_shallow_summary.

        Args:
            notes: Optional human-readable note attached to the ScanRun.

        Returns:
            {success, active_total, refreshed, skipped_no_diff,
            snapshots_written, marked_deleted, marked_private,
            transient_errors, errors, scan_run_id[, hint]}.
        """
        result = _api_call("POST", "/api/ecosystem/refresh", {"notes": notes})
        if result is None:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_scan_status(run_id: str) -> dict[str, Any]:
        """Fetch a single EcosystemScanRun by id.

        Args:
            run_id: The scan run UUID.

        Returns:
            ScanRun dict (id / strategy / started_at / completed_at /
            duration_seconds / repos_added / repos_updated / repos_skipped /
            errors / triggered_by) or {"error": "not_found"}.
        """
        result = _api_call("GET", f"/api/ecosystem/scan-runs/{run_id}")
        if result is None:
            return {"error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_scan_history(
        strategy: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """List recent scan runs ordered by started_at descending.

        Args:
            strategy: Optional filter — incremental / full / topic / trending.
            limit: Maximum number of runs to return (default 10, max 100).

        Returns:
            {runs: [ScanRun, ...], total: int}.
        """
        params: dict[str, Any] = {"limit": min(max(limit, 1), 100)}
        if strategy:
            params["strategy"] = strategy
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        result = _api_call("GET", f"/api/ecosystem/scan-runs?{query_string}")
        if result is None:
            return {"runs": [], "total": 0}
        return result

    # ============================================================
    # Deep review (Stage F)
    # ============================================================

    @mcp.tool()
    def ecosystem_deep_review_request(
        repo_id: str,
        priority: str = "medium",
        timeout_minutes: int = 45,
        agent_id: str = "",
    ) -> dict[str, Any]:
        """Queue a deep-review for a repo and return the dispatch prompt.

        Creates an EcosystemDeepReview row queued on the funnel
        (``stage_status='queued'``; the legacy ``status`` column is a
        derived read-only view and returns ``'queued'`` — no more
        ``'running'``), and embeds a sub-agent prompt (5-section template
        + repo metadata) in the row's ``dispatch_prompt`` field. A
        background watchdog advances ``stage_status`` to
        ``shallow_failed`` (status derives to ``failed``) after
        ``timeout_minutes`` if no report has been linked. The Leader is
        responsible for actually spawning the sub-agent (via
        TeamCreate/Agent tool with team_name=ecosystem-platform).

        Args:
            repo_id: EcosystemRepoProfile.id of the target repo.
            priority: medium / high / critical (informational only).
            timeout_minutes: Hard cap before auto-fail (5..180).
            agent_id: Optional pre-assigned agent identifier.

        Returns:
            Deep-review row dict with ``dispatch_prompt`` containing the
            agent dispatch prompt.
        """
        payload: dict[str, Any] = {
            "repo_id": repo_id,
            "priority": priority,
            "timeout_minutes": timeout_minutes,
        }
        if agent_id:
            payload["agent_id"] = agent_id
        result = _api_call("POST", "/api/ecosystem/deep_reviews", payload)
        if result is None:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_deep_review_status(repo_id: str) -> dict[str, Any]:
        """Look up the most recent deep-review for ``repo_id``.

        Args:
            repo_id: EcosystemRepoProfile.id.

        Returns:
            Latest deep-review row, or ``{"success": False, "error": ...}``
            when none exists.
        """
        result = _api_call("GET", f"/api/ecosystem/deep_reviews/by_repo/{repo_id}")
        if result is None:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_deep_review_list(
        status: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List deep-reviews newest-first, optionally filtered by status.

        Args:
            status: queued / completed / failed ('running' only matches
                pre-v1.6.2 historical rows — status is now a derived
                read-only view of stage_status). Empty = all.
            limit: Max rows to return (1..100).

        Returns:
            ``{reviews: [...], total: int}``.
        """
        params: dict[str, Any] = {"limit": min(max(limit, 1), 100)}
        if status:
            params["status"] = status
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        result = _api_call("GET", f"/api/ecosystem/deep_reviews?{query_string}")
        if result is None:
            return {"reviews": [], "total": 0}
        return result

    @mcp.tool()
    def ecosystem_deep_review_cancel(deep_review_id: str) -> dict[str, Any]:
        """Cancel an in-flight (stage_status='queued') deep-review.

        Advances the row's ``stage_status`` to ``shallow_failed`` (the
        legacy ``status`` column derives to ``failed``) with a
        cancellation note. The sub-agent is expected to observe the row
        state and shut down on its own.

        Args:
            deep_review_id: EcosystemDeepReview.id.

        Returns:
            Updated deep-review row.
        """
        result = _api_call(
            "POST", f"/api/ecosystem/deep_reviews/{deep_review_id}/cancel"
        )
        if result is None:
            return {"success": False, "error": "api_unavailable"}
        return result

    # ==========================================================
    # Stage D — Tag system MCP tools
    # ==========================================================

    @mcp.tool()
    def ecosystem_tag_list(category: str = "", limit: int = 200) -> dict[str, Any]:
        """List ecosystem tag dictionary entries.

        Three layers of tagging are supported:
        - GitHub topics direct mapping (Layer 1)
        - Keyword/regex rules (Layer 2)
        - LLM sub-agent fallback (Layer 3)

        This tool only returns the canonical tag dictionary (21 default tags).
        Use ecosystem_tag_apply_batch to actually apply tags to repos.

        Args:
            category: Filter by category — capability / tech_stack / maturity / positioning.
            limit: Max number of tags to return (default 200, max 500).

        Returns:
            {tags: [{id, name, aliases, category, description}], total: int}
        """
        path = f"/api/ecosystem/tags?limit={limit}"
        if category:
            path += f"&category={category}"
        result = _api_call("GET", path)
        if not result:
            return {"tags": [], "total": 0}
        return result

    @mcp.tool()
    def ecosystem_tag_apply_batch(
        repo_ids: list[str] | None = None,
        repo_full_names: list[str] | None = None,
        agent_id: str = "ecosystem-tagger",
        limit: int = 200,
        replace_auto: bool = False,
    ) -> dict[str, Any]:
        """Apply Layer 1 + Layer 2 auto-tagging to a batch of ecosystem repos.

        Layer 1 matches GitHub topics directly (confidence=0.95, source=github_topic).
        Layer 2 matches keyword rules against name+description+topics+owner
        (confidence=0.7, source=auto_rule).

        Repos with fewer than 2 matched tags are flagged via needs_llm=True;
        callers should pass those into ecosystem_tag_dispatch_llm to spawn
        Layer 3 sub-agents.

        If both repo_ids and repo_full_names are empty, the first <limit>
        repos in the database are processed.

        Args:
            repo_ids: Specific EcosystemRepoProfile.id list.
            repo_full_names: Specific 'owner/repo' list.
            agent_id: Caller agent identifier recorded in EcosystemRepoTag rows.
            limit: Max repos to process when filters empty (default 200).
            replace_auto: When True, delete each repo's existing tags whose source
                is github_topic or auto_rule before re-applying. Manual / auto_llm
                tags are preserved. Use after rule upgrades to clear stale false
                positives. Default False keeps the legacy append-only behavior.

        Returns:
            {repos_processed, layer1_applied, layer2_applied, repos_needing_llm,
             repos_failed, by_repo: [...per-repo result...]}
        """
        payload: dict[str, Any] = {
            "repo_ids": repo_ids or [],
            "repo_full_names": repo_full_names or [],
            "agent_id": agent_id,
            "limit": limit,
            "replace_auto": replace_auto,
        }
        result = _api_call("POST", "/api/ecosystem/tags/apply", payload)
        if not result or result.get("success") is False:
            return result or {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_tag_dispatch_llm(
        repo_ids: list[str],
        team_name: str = "ecosystem-platform",
        agent_template: str = "researcher",
        max_concurrency: int = 20,
    ) -> dict[str, Any]:
        """Build a Layer 3 sub-agent dispatch plan for repos that need LLM fallback.

        Returns a dispatch plan; the Leader is expected to spawn each
        sub-agent via the Agent tool using launch_call.params. Each sub-agent
        analyzes the repo and submits results via ecosystem_tag_apply_llm_result.

        Concurrency is capped at max_concurrency (default 20) to limit token spend.
        Excess repos are returned in skipped_due_to_limit.

        Args:
            repo_ids: List of repo ids needing Layer 3 (typically those flagged
                by ecosystem_tag_apply_batch with needs_llm=True).
            team_name: Sub-agent team name (default 'ecosystem-platform').
            agent_template: Sub-agent template (default 'researcher').
            max_concurrency: Max concurrent sub-agents per call (default 20, max 50).

        Returns:
            {team_name, agent_template, max_concurrency, total_requested,
             dispatched, skipped_due_to_limit, dispatch: [...launch_call...],
             instructions: str}
        """
        payload = {
            "repo_ids": repo_ids,
            "team_name": team_name,
            "agent_template": agent_template,
            "max_concurrency": max_concurrency,
        }
        result = _api_call("POST", "/api/ecosystem/tags/llm/dispatch_plan", payload)
        if not result or result.get("success") is False:
            return result or {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_tag_apply_llm_result(
        repo_id: str,
        tags: list[dict[str, Any]],
        agent_id: str = "",
    ) -> dict[str, Any]:
        """Submit Layer 3 LLM tagging result from a sub-agent.

        Sub-agent emits structured JSON like:
            [{"name": "memory_system", "confidence": 0.85},
             {"name": "python", "confidence": 0.95}]
        Tags not present in the canonical dictionary are silently skipped
        (returned in skipped_unknown).

        Args:
            repo_id: Target EcosystemRepoProfile.id.
            tags: list of {name: str, confidence: float (0..1)}.
            agent_id: Sub-agent identifier recorded as EcosystemRepoTag.agent_id.

        Returns:
            {repo_id, layer3_tags, skipped_unknown, total_applied}
        """
        payload: dict[str, Any] = {"repo_id": repo_id, "tags": tags}
        if agent_id:
            payload["agent_id"] = agent_id
        result = _api_call("POST", "/api/ecosystem/tags/llm/result", payload)
        if not result or result.get("success") is False:
            return result or {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_repo_tags(repo_id: str) -> dict[str, Any]:
        """List all tags currently associated with a single ecosystem repo.

        Returns each association with its confidence, source layer
        (github_topic / auto_rule / auto_llm / manual), and tag metadata.

        Args:
            repo_id: EcosystemRepoProfile.id.

        Returns:
            {repo_id, tags: [{tag_name, tag_category, confidence, source, agent_id, ...}],
             total: int}
        """
        result = _api_call("GET", f"/api/ecosystem/repos/{repo_id}/tags")
        if not result:
            return {"repo_id": repo_id, "tags": [], "total": 0}
        return result

    # ==========================================================
    # Stage G — Summary tools (weekly / by_tag / top_n / health)
    #
    # Each tool fetches markdown from the API summarizer endpoint and,
    # unless save_report=False, persists it via report_save so the
    # Dashboard reports page picks it up automatically. The four tools
    # share an _auto_save_report helper to keep behaviour consistent.
    # ==========================================================

    def _auto_save_report(
        *,
        author: str,
        topic: str,
        markdown: str,
        report_type: str,
    ) -> dict[str, Any]:
        """Persist a generated summary as a database-backed report.

        Returns the report-save response (with id / filename / project_id)
        or an {"success": False, "error": ...} dict when the API call fails.
        """
        payload = {
            "author": author,
            "topic": topic,
            "content": markdown,
            "report_type": report_type,
        }
        saved = _api_call("POST", "/api/reports", payload)
        if saved and isinstance(saved, dict) and saved.get("id"):
            return {
                "success": True,
                "id": saved["id"],
                "filename": saved.get("filename", ""),
                "report_type": saved.get("report_type", report_type),
                "project_id": saved.get("project_id", ""),
            }
        return saved if isinstance(saved, dict) else {
            "success": False,
            "error": "report_save api unavailable",
        }

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def ecosystem_summary_weekly(
        window_days: int = 7,
        top_movers_limit: int = 5,
        author: str = "ecosystem-summarizer",
        save_report: bool = True,
    ) -> dict[str, Any]:
        """Generate the past-N-days ecosystem briefing as markdown.

        Aggregates new / updated profiles, completed deep-reviews, archive
        counters and top star movers over the configured window. When
        ``save_report=True`` (default) the markdown is persisted via
        ``report_save`` with ``report_type='ecosystem-weekly'``.

        Args:
            window_days: Look-back window in days (1..90, default 7).
            top_movers_limit: Max repos surfaced under the Top Movers
                section (default 5).
            author: Author name written into the saved report.
            save_report: When True, also calls report_save with the
                generated markdown.

        Returns:
            ``{markdown, window_days, generated_at, [report]}``. The
            ``report`` key is only present when ``save_report=True``.
        """
        params: list[tuple[str, Any]] = [
            ("window_days", window_days),
            ("top_movers_limit", top_movers_limit),
        ]
        query_string = "&".join(f"{k}={v}" for k, v in params)
        result = _api_call("GET", f"/api/ecosystem/summary/weekly?{query_string}")
        if not result:
            return {"success": False, "error": "summarizer api unavailable"}

        if save_report:
            now = datetime.now(tz=UTC)
            topic = f"ecosystem-weekly-{now.strftime('%Y-%m-%d')}"
            result["report"] = _auto_save_report(
                author=author,
                topic=topic,
                markdown=result.get("markdown", ""),
                report_type="ecosystem-weekly",
            )
        return result

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def ecosystem_summary_by_tag(
        tag: str,
        include_archived: bool = False,
        limit: int = 200,
        author: str = "ecosystem-summarizer",
        save_report: bool = True,
    ) -> dict[str, Any]:
        """List every repo carrying ``tag`` as a markdown table.

        Each row contains stars / language / one-line summary plus a deep-
        review id when one exists. Rows are sorted by stars desc.
        Archived repos are excluded unless ``include_archived=True``.

        Args:
            tag: Tag name (e.g. 'memory_system'). Required.
            include_archived: Include is_archived=True repos when True.
            limit: Max repos to enumerate (default 200, max 500).
            author: Author recorded on the saved report.
            save_report: When True, persist via report_save with
                report_type='ecosystem-by-tag'.

        Returns:
            ``{markdown, tag, include_archived, generated_at, [report]}``.
        """
        params: list[tuple[str, Any]] = [
            ("tag", tag),
            ("include_archived", include_archived),
            ("limit", limit),
        ]
        query_string = "&".join(f"{k}={v}" for k, v in params)
        result = _api_call(
            "GET", f"/api/ecosystem/summary/by_tag?{query_string}"
        )
        if not result:
            return {"success": False, "error": "summarizer api unavailable"}

        if save_report:
            topic = f"ecosystem-by-tag-{tag}"
            result["report"] = _auto_save_report(
                author=author,
                topic=topic,
                markdown=result.get("markdown", ""),
                report_type="ecosystem-by-tag",
            )
        return result

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def ecosystem_summary_top_n(
        category: str = "",
        n: int = 10,
        sort: str = "stars",
        author: str = "ecosystem-summarizer",
        save_report: bool = True,
    ) -> dict[str, Any]:
        """Top-N markdown table of ecosystem repos.

        Args:
            category: Optional category filter (agent-framework /
                mcp-server / memory-system / skill-system / tooling).
            n: Number of rows (1..100, default 10).
            sort: ``stars`` (default), ``pushed_at`` (last commit recency)
                or ``scan_freshness`` (last_scanned_at recency).
            author: Author recorded on the saved report.
            save_report: When True, persist via report_save with
                report_type='ecosystem-top-n'.

        Returns:
            ``{markdown, category, n, sort, generated_at, [report]}``.
        """
        params: list[tuple[str, Any]] = [
            ("n", n),
            ("sort", sort),
        ]
        if category:
            params.append(("category", category))
        query_string = "&".join(f"{k}={v}" for k, v in params)
        result = _api_call(
            "GET", f"/api/ecosystem/summary/top_n?{query_string}"
        )
        if not result:
            return {"success": False, "error": "summarizer api unavailable"}

        if save_report:
            cat_slug = category or "all"
            topic = f"ecosystem-top-{n}-{sort}-{cat_slug}"
            result["report"] = _auto_save_report(
                author=author,
                topic=topic,
                markdown=result.get("markdown", ""),
                report_type="ecosystem-top-n",
            )
        return result

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def ecosystem_summary_health(
        author: str = "ecosystem-summarizer",
        save_report: bool = True,
    ) -> dict[str, Any]:
        """Platform self-check markdown: profile / scan / tag coverage / archive ratio.

        Args:
            author: Author recorded on the saved report.
            save_report: When True, persist via report_save with
                report_type='ecosystem-health'.

        Returns:
            ``{markdown, generated_at, [report]}``.
        """
        result = _api_call("GET", "/api/ecosystem/summary/health")
        if not result:
            return {"success": False, "error": "summarizer api unavailable"}

        if save_report:
            now = datetime.now(tz=UTC)
            topic = f"ecosystem-health-{now.strftime('%Y-%m-%d')}"
            result["report"] = _auto_save_report(
                author=author,
                topic=topic,
                markdown=result.get("markdown", ""),
                report_type="ecosystem-health",
            )
        return result

    # ==========================================================
    # v1.5.0-B Stage 0 shallow-scan queue
    # ==========================================================

    @mcp.tool()
    def ecosystem_apply_shallow_summary(
        repo_id: str,
        shallow_summary: str = "",
        deep_review_id: str = "",
        error_kind: str = "",
        error_message: str = "",
        http_status: int = 0,
        rate_limit_remaining: int = -1,
    ) -> dict[str, Any]:
        """Stage 0 worker callback: write back a shallow summary OR report a failure.

        Success path (default): pass shallow_summary (200-400 char Chinese
        markdown) and deep_review_id; the OS will persist the summary,
        advance ``stage_status -> shallow_done``, and mark the deep_review
        row as completed.

        Failure path: leave shallow_summary empty and pass error_kind,
        which routes the failure through the §3.1 classifier so the OS
        can decide whether to immediate-retry, mark deleted/private, or
        feed the self-learning loop. Valid error_kind values:
        ``http`` / ``agent_read`` / ``agent_timeout`` / ``json_parse`` /
        ``fetch_style``.

        Args:
            repo_id: EcosystemRepoProfile.id.
            shallow_summary: 200-400 字中文 markdown 总结 (success path).
            deep_review_id: associated deep_review row id (Stage 0 dispatch).
            error_kind: failure category hint (failure path only).
            error_message: short message stored in profile.last_fetch_error.
            http_status: HTTP status code when error_kind='http'.
            rate_limit_remaining: when http_status=403, ``0`` indicates rate-limit.

        Returns:
            On success: ``{success: True, repo_id, shallow_summary_length,
            stage_status}``. On failure path: ``{success: False, failure_class,
            immediate_retry, retry_delay_seconds, marked_deleted, marked_private}``.
        """
        payload: dict[str, Any] = {
            "repo_id": repo_id,
            "shallow_summary": shallow_summary,
        }
        if deep_review_id:
            payload["deep_review_id"] = deep_review_id
        if error_kind:
            payload["error_kind"] = error_kind
        if error_message:
            payload["error_message"] = error_message
        if http_status:
            payload["http_status"] = http_status
        if rate_limit_remaining >= 0:
            payload["rate_limit_remaining"] = rate_limit_remaining

        result = _api_call(
            "POST",
            "/api/ecosystem/shallow_queue/apply_summary",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "shallow_queue api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_shallow_queue_status() -> dict[str, Any]:
        """Show Stage 0 shallow-scan queue status for the active project.

        Returns counts for active profiles, pending shallow scans,
        in-flight dispatches, terminal failures (shallow_failed), and
        deleted/private-flagged repos. The ``self_learning_pending`` map
        shows how many distinct repos have hit each failure class so far
        (a class fires a ``pattern_record`` entry once the count reaches 3).

        Returns:
            ``{project_id, active_total, pending_shallow, in_flight,
              shallow_failed, deleted, private_now, concurrency,
              self_learning_pending}``.
        """
        result = _api_call(
            "GET",
            "/api/ecosystem/shallow_queue/status",
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "shallow_queue api unavailable"}
        return result

    # ==========================================================
    # v1.5.0-C — Stage 1/2/3 lifecycle trigger tools
    # ==========================================================

    @mcp.tool()
    def ecosystem_deep_review_request_batch(
        tags: list[str] | None = None,
        min_stars: int = 0,
        limit: int = 20,
        research_goal: str = "",
    ) -> dict[str, Any]:
        """Stage 1 — Queue architecture-analysis dispatches for tag-filtered candidates.

        Pulls active+shallow_done profiles whose tag set covers ``tags`` (AND
        semantics), creates an ``EcosystemDeepReview`` row per candidate, and
        returns a list of ``DispatchIntent`` payloads for backend-architect
        sub-agents. Leader is responsible for actually spawning each agent
        via the Agent tool with team_name='ecosystem-platform'. Each agent
        eventually calls ``ecosystem_apply_architecture_md`` to write back.

        Args:
            tags: Required AND-filter tags (e.g. ['memory_system', 'python']).
                Empty list returns 400.
            min_stars: Override min_stars threshold; 0 = use project settings.
            limit: Max candidates to dispatch per call (default 20).
            research_goal: Free-form research-goal text injected into each
                sub-agent prompt (e.g. "升级系统记忆功能").

        Returns:
            ``{success, dispatched, intents: [{repo_id, repo_full_name,
              deep_review_id, prompt, timeout_seconds, project_id}, ...]}``
        """
        payload: dict[str, Any] = {
            "tags": tags or [],
            "limit": limit,
            "research_goal": research_goal,
        }
        if min_stars > 0:
            payload["min_stars"] = min_stars
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/request_batch",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_apply_architecture_md(
        deep_review_id: str,
        architecture_md: str = "",
        agent_id: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        """Stage 1 writeback — submit architecture_md OR report failure.

        Success path (default): pass non-empty architecture_md (800-1500 字
        Chinese markdown). The OS persists it, advances ``stage_status ->
        architecture_done``, and marks the deep_review row completed.

        Failure path: leave architecture_md empty and pass error_message;
        the OS advances ``stage_status -> architecture_failed`` so manual
        retry surfaces in the UI.

        Args:
            deep_review_id: Target deep_review row id.
            architecture_md: 800-1500 字 Chinese markdown (success path).
            agent_id: Optional agent identifier recorded on the review row.
            error_message: Short message stored on review.risks_md (failure path).

        Returns:
            On success: ``{success: True, deep_review_id, stage_status,
            architecture_md_length}``. On failure: ``{success: False,
            stage_status='architecture_failed', error}``.
        """
        payload: dict[str, Any] = {
            "deep_review_id": deep_review_id,
            "architecture_md": architecture_md,
        }
        if agent_id:
            payload["agent_id"] = agent_id
        if error_message:
            payload["error_message"] = error_message
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/apply_architecture_md",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_trigger_debate(
        repo_ids: list[str] | None = None,
        research_goal: str = "",
        suggested_advocate: str = "backend-architect",
        suggested_critic: str = "code-reviewer",
        suggested_judge: str = "team-lead",
    ) -> dict[str, Any]:
        """Stage 2 — Build debate dispatch payload (Leader still calls debate_start).

        Validates that each ``repo_id`` has at least one
        ``architecture_done`` review, then returns a payload (suggested
        topic + roles + linked review_ids) so the caller can invoke the
        existing ``debate_start`` MCP tool. After ``debate_start`` returns
        a meeting id, call ``ecosystem_link_debate_meeting`` to write
        ``debate_meeting_id`` back onto each review row.

        Args:
            repo_ids: 1-5 finalist repo ids selected from the Stage 1 batch.
            research_goal: Drives the suggested meeting topic.
            suggested_advocate / critic / judge: Default debate roles. Caller
                may override when calling debate_start.

        Returns:
            ``{success, review_ids, repo_full_names, suggested_topic,
              suggested_advocate, suggested_critic, suggested_judge,
              project_id, next_action}``
        """
        payload = {
            "repo_ids": repo_ids or [],
            "research_goal": research_goal,
            "suggested_advocate": suggested_advocate,
            "suggested_critic": suggested_critic,
            "suggested_judge": suggested_judge,
        }
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/trigger_debate",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_link_debate_meeting(
        review_ids: list[str],
        meeting_id: str,
    ) -> dict[str, Any]:
        """Stage 2 helper — link ``debate_start`` meeting id back to review rows.

        Called immediately after ``debate_start`` succeeds. Writes
        ``debate_meeting_id`` onto every review in ``review_ids`` so the
        meeting-conclude hook (``meeting_ecosystem_writeback.py``) can
        match concluded meetings to their ecosystem reviews.

        Args:
            review_ids: List of deep_review ids returned by
                ``ecosystem_trigger_debate``.
            meeting_id: Meeting id returned by ``debate_start``.

        Returns:
            ``{success, linked, meeting_id}``.
        """
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/link_debate_meeting",
            {"review_ids": review_ids, "meeting_id": meeting_id},
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_apply_debate_result(
        deep_review_id: str,
        risks_md: str = "",
        learnings_md: str = "",
        integration_md: str = "",
        integration_recommendation: str = "",
        agent_id: str = "",
    ) -> dict[str, Any]:
        """Stage 2 writeback — submit debate conclusion to advance to ``debated``.

        At least one of risks_md / learnings_md / integration_md must be
        non-empty. ``integration_recommendation`` is a short enum:
        ``integrate`` / ``reference`` / ``learn`` / ``skip``.

        Args:
            deep_review_id: Target deep_review row id.
            risks_md: 风险点 markdown.
            learnings_md: 借鉴点 markdown.
            integration_md: 集成建议 markdown.
            integration_recommendation: integrate/reference/learn/skip enum.
            agent_id: Optional agent identifier recorded on the review.

        Returns:
            ``{success, deep_review_id, stage_status, debated_at}``.
        """
        payload: dict[str, Any] = {"deep_review_id": deep_review_id}
        if risks_md:
            payload["risks_md"] = risks_md
        if learnings_md:
            payload["learnings_md"] = learnings_md
        if integration_md:
            payload["integration_md"] = integration_md
        if integration_recommendation:
            payload["integration_recommendation"] = integration_recommendation
        if agent_id:
            payload["agent_id"] = agent_id
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/apply_debate_result",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_mark_as_reference(
        deep_review_id: str,
        agent_id: str = "",
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Stage 3 reference path — add ``lifecycle:reference`` tag + advance to ``referenced``.

        Use when the debate concludes that the repo is worth keeping as an
        architectural reference but not integrated. The repo will appear
        highlighted in future searches as "已研究过" so the team avoids
        re-deep-scanning it.

        Args:
            deep_review_id: Target deep_review row id.
            agent_id: Optional agent identifier recorded on the tag.
            confidence: 0.0-1.0; default 1.0 (manual decision).

        Returns:
            ``{success, deep_review_id, stage_status, stage3_completed_at}``.
        """
        payload: dict[str, Any] = {
            "deep_review_id": deep_review_id,
            "confidence": confidence,
        }
        if agent_id:
            payload["agent_id"] = agent_id
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/mark_as_reference",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_start_integration(
        deep_review_id: str,
        title: str = "",
        description: str = "",
        priority: str = "high",
        horizon: str = "mid",
        extra_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Stage 3 integrate path — build a task_create payload + tag the repo.

        Adds ``lifecycle:integrated`` tag, advances ``stage_status``, and
        returns a task payload (title / description / priority / horizon /
        tags) ready to POST to ``/api/projects/{project_id}/tasks``.
        After the task is created, call ``ecosystem_link_integration_task``
        to write ``integration_task_id`` back onto the review.

        ecosystem 不接管实施 — task ownership 由现有任务/团队系统接管。

        Args:
            deep_review_id: Target deep_review row id (must be debated /
                architecture_done / referenced).
            title: Optional task title (auto-generated if empty).
            description: Optional task description (auto-generated if empty).
            priority: Task priority — critical / high (default) / medium / low.
            horizon: Task horizon — short / mid (default) / long.
            extra_tags: Additional tags appended to the task.

        Returns:
            ``{success, review_id, repo_id, repo_full_name, task_payload,
              project_id, next_action}``.
        """
        payload: dict[str, Any] = {
            "deep_review_id": deep_review_id,
            "title": title,
            "description": description,
            "priority": priority,
            "horizon": horizon,
            "extra_tags": extra_tags or [],
        }
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/start_integration",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    @mcp.tool()
    def ecosystem_link_integration_task(
        deep_review_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Stage 3 helper — link integration task id back to review row.

        Args:
            deep_review_id: Target deep_review row id.
            task_id: Task id returned by ``/api/projects/{project_id}/tasks``.

        Returns:
            ``{success, deep_review_id, integration_task_id, stage_status}``.
        """
        result = _api_call(
            "POST",
            "/api/ecosystem/lifecycle/link_integration_task",
            {"deep_review_id": deep_review_id, "task_id": task_id},
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "lifecycle api unavailable"}
        return result

    # ============================================================
    # Worker pool claim tools (v1.5.3)
    # ============================================================

    @mcp.tool()
    def ecosystem_claim_shallow(worker_id: str) -> dict[str, Any]:
        """Claim the next queued repo for shallow scanning (stage_status='queued').

        Atomic: only one worker gets each row; others get {"claimed": false}.
        v1.7.0: also returns repo_full_name, topics, description, owner, stars, last_commit_at
        so workers can skip a separate ecosystem_repo_get call.

        Args:
            worker_id: Unique worker identifier string.

        Returns:
            {"claimed": true, "dr_id": ..., "repo_id": ..., "claimed_by": ..., "claimed_at": ...,
             "repo_full_name": ..., "topics": [...], "description": ..., "owner": ...,
             "stars": ..., "last_commit_at": ...}
            or {"claimed": false} when queue is empty.
        """
        result = _api_call(
            "POST",
            "/api/ecosystem/shallow_queue/claim",
            {"worker_id": worker_id},
            extra_headers=_project_headers(),
        )
        if result is None:
            return {"claimed": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_claim_review(worker_id: str, min_stars: int = 0) -> dict[str, Any]:
        """Claim the next shallow_done repo for quality review.

        Finds stage_status='shallow_done' rows with no quality_score and no active claim.
        Returns the repo's shallow_summary so the reviewer can evaluate quality.

        Args:
            worker_id: Unique worker identifier string.
            min_stars: Minimum star count filter (0 = no filter).

        Returns:
            {"claimed": true, "dr_id": ..., "repo_id": ..., "shallow_summary": ..., ...}
            or {"claimed": false} when queue is empty.
        """
        result = _api_call(
            "POST",
            "/api/ecosystem/review_queue/claim",
            {"worker_id": worker_id, "min_stars": min_stars},
            extra_headers=_project_headers(),
        )
        if result is None:
            return {"claimed": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_apply_quality_review(
        dr_id: str,
        quality_score: int,
        quality_notes: str = "",
        recommendation: str = "",
    ) -> dict[str, Any]:
        """Submit quality review result and release the claim lock.

        Writes quality_score / quality_notes / reviewed_by / reviewed_at,
        clears claimed_by so other workers can pick up the next row.

        Args:
            dr_id: EcosystemDeepReview.id to update.
            quality_score: 0-100 quality score.
            quality_notes: Reviewer notes / rationale.
            recommendation: integrate / reference / learn / skip.

        Returns:
            {"success": true, "dr_id": ..., "quality_score": ..., "reviewed_by": ...}
        """
        result = _api_call(
            "POST",
            "/api/ecosystem/review_queue/apply",
            {
                "dr_id": dr_id,
                "quality_score": quality_score,
                "quality_notes": quality_notes,
                "recommendation": recommendation,
            },
            extra_headers=_project_headers(),
        )
        if result is None:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_release_claim(dr_id: str, reason: str = "") -> dict[str, Any]:
        """Release a worker claim without submitting a quality review.

        Use when a worker abandons a task (timeout, error). Clears claimed_by
        so another worker can pick up the row. Records reason in quality_notes.

        Args:
            dr_id: EcosystemDeepReview.id to release.
            reason: Short description of why the claim is being released.

        Returns:
            {"success": true, "dr_id": ..., "claimed_by": null, "quality_notes": ...}
        """
        result = _api_call(
            "POST",
            "/api/ecosystem/claims/release",
            {"dr_id": dr_id, "reason": reason},
            extra_headers=_project_headers(),
        )
        if result is None:
            return {"success": False, "error": "api_unavailable"}
        return result

    # ============================================================
    # v1.6.0 P0.3 — DataSource / ScanProfile / IndexUpdate MCP tools
    # ============================================================

    @mcp.tool()
    def ecosystem_quick_setup(
        sources: list[str] | None = None,
        queries: list[str] | None = None,
        use_defaults: bool = True,
        custom_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One-shot ecosystem setup wizard — create data sources + scan profile in one call.

        Use this when bootstrapping a fresh project to ecosystem indexing. Maps to
        ``POST /api/ecosystem/quick_setup`` which creates one DataSource per entry
        in ``sources`` (each enabled by default) and persists either the default
        ScanProfile or the merged ``custom_profile`` override.

        Args:
            sources: Data source kinds to enable, e.g. ``['github', 'huggingface']``.
                Must each be a valid ``DataSourceKind`` value (github / huggingface /
                npm / pypi / hackernews / producthunt / arxiv / custom). Defaults to
                ``['github']`` when empty.
            queries: Keyword / topic list applied to every created data source's
                ``config.queries`` field. Optional.
            use_defaults: When True (default), persist the built-in default
                ScanProfile. When False, the API merges ``custom_profile`` over
                the defaults.
            custom_profile: Advanced override dict; ignored when
                ``use_defaults=True``.

        Returns:
            ``{success: True, data_sources_created: N, data_source_ids: [...],
              scan_profile_id, scan_profile_version, profile_created: bool,
              next_action: 'call ecosystem_index_update(dry_run=True)',
              raw: <API response>}``.
            ``success`` semantics: True means the call completed; ``raw`` echoes
            the underlying ``POST /api/ecosystem/quick_setup`` payload which
            uses ``next_step`` (not ``next_action``) — the wrapper renames it
            here for consistency with other ecosystem tools.
            On failure: ``{success: False, error}``.
        """
        payload: dict[str, Any] = {
            "sources": sources or ["github"],
            "queries": queries or [],
            "use_defaults": use_defaults,
        }
        if custom_profile is not None:
            payload["custom_profile"] = custom_profile

        result = _api_call(
            "POST",
            "/api/ecosystem/quick_setup",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        if isinstance(result, dict) and result.get("success") is False:
            return result

        ds_ids = result.get("data_source_ids", []) if isinstance(result, dict) else []
        return {
            "success": True,
            "data_sources_created": len(ds_ids),
            "data_source_ids": ds_ids,
            "scan_profile_id": result.get("scan_profile_id"),
            "scan_profile_version": result.get("scan_profile_version"),
            "profile_created": result.get("scan_profile_id") is not None,
            "next_action": "call ecosystem_index_update(dry_run=True)",
            "raw": result,
        }

    @mcp.tool()
    def ecosystem_data_source_create(
        kind: str,
        name: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """[DEPRECATED v1.6.1: 多源已否决（用户确认仅 GitHub 单源）。data_source 表已废弃。
        此工具仅向后兼容保留，所有写入被忽略。
        请改用 ecosystem 设置 API/UI（PUT /api/ecosystem/projects/{id}/settings）修改 settings 表。]

        Create a single DataSource configuration for the current project.

        Args:
            kind: DataSourceKind enum value — one of github / huggingface /
                npm / pypi / hackernews / producthunt / arxiv / custom.
            name: Friendly display name shown in the dashboard.
            config: Source-specific config dict. Empty dict allowed.

        Returns:
            ``{success: True, deprecated: True, message: ...}`` — writes are ignored.
        """
        return {
            "success": True,
            "deprecated": True,
            "message": (
                "[DEPRECATED v1.6.1] ecosystem_data_source_create is a no-op. "
                "Multi-source expansion was denied (GitHub-only confirmed). "
                "Use PUT /api/ecosystem/projects/{project_id}/settings to configure scanning."
            ),
        }

    @mcp.tool()
    def ecosystem_scan_profile_update(
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """[DEPRECATED v1.6.1: scan_profile 表已废弃。alert_thresholds.max_new_per_scan
        已迁移至 settings.alert_max_new_per_scan。
        此工具仅向后兼容保留，所有写入被忽略。
        请改用 ecosystem 设置 API/UI（PUT /api/ecosystem/projects/{id}/settings）修改 settings 表。]

        Create a new ScanProfile version (previous version is deactivated).

        Args:
            profile: Complete ScanProfile JSON (ignored — writes are no-op).

        Returns:
            ``{success: True, deprecated: True, message: ...}`` — writes are ignored.
        """
        return {
            "success": True,
            "deprecated": True,
            "message": (
                "[DEPRECATED v1.6.1] ecosystem_scan_profile_update is a no-op. "
                "scan_profile table is superseded by settings. "
                "Use alert_max_new_per_scan in PUT /api/ecosystem/projects/{project_id}/settings."
            ),
        }

    @mcp.tool()
    def ecosystem_index_update(dry_run: bool = True) -> dict[str, Any]:
        """Trigger ecosystem index update — runs scanner + computes diff.

        Maps to ``POST /api/ecosystem/index_update``. Verifies that at least
        one enabled DataSource and an active ScanProfile exist, then runs the
        full pipeline: gh search → NormalizedSignal → classify active status
        → diff against DB → alert threshold check → (if dry_run=False) persist
        index_diff + status_changes. When ``dry_run=True``, no writes touch
        ``ecosystem_repo_profiles`` / ``ecosystem_index_diffs`` /
        ``ecosystem_status_changes`` (BUG #6/#8 fix verified in
        ``test_dry_run_does_not_write_profile_table``).

        Args:
            dry_run: When True (default), simulate the scan and return diff
                preview only. When False, persist profile upserts + index_diff
                + status_changes.

        Returns:
            Normal completion (setup OK, no alert): ``{success: True, dry_run,
              alerted: False, scan_profile_version, total_scanned,
              diff: {id, new_count, reactivated_count, deactivated_count,
                stale_count, archived_count, markdown_summary},
              message}``.
            Threshold breach (BUG #5 fix): ``{success: True, dry_run,
              alerted: True, message, diff: {new_count, reactivated_count,
              deactivated_count, stale_count, archived_count}}``.
            Missing setup: ``{success: False, dry_run, missing_setup:
              [<'data_source'|'scan_profile'>...], message}``.
            gh CLI auth missing: ``{success: False, dry_run,
              missing_setup: ['gh_auth' | 'gh_cli'], message}``.
            ``success`` semantics: True = call completed cleanly (including
            dry_run previews and alerted=True threshold breaches);
            False = configuration is incomplete and the scan did not run.
            Check ``alerted`` and ``missing_setup`` for special states even
            when ``success`` is True/False.
        """
        payload = {"dry_run": dry_run}
        result = _api_call(
            "POST",
            "/api/ecosystem/index_update",
            payload,
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_index_diff_latest() -> dict[str, Any]:
        """Fetch the latest IndexDiff snapshot for the current project.

        Maps to ``GET /api/ecosystem/index_diffs/latest``. Returns the most
        recent diff row produced by a real ``ecosystem_index_update``
        (dry_run=False) run. Dry-run previews are not persisted and therefore
        never appear here.

        Returns:
            Diff available: ``{success: True, diff: {id, diff_type,
              new_count, reactivated_count, deactivated_count, stale_count,
              archived_count, markdown_summary, alerted, generated_at}}``.
            No diffs yet (fresh project): ``{success: True, diff: None,
              message: 'No index diffs found yet.'}``.
            Legacy/unbuilt API: ``{success: False, error:
              'P0.4 will implement', detail}`` (returned when the endpoint
              answers 404).
            Other failure: ``{success: False, error, detail}``.
            ``success`` semantics: True = call completed (``diff`` may be None
            when the project has never run a non-dry index_update);
            False = API/endpoint error. The API field is ``diff`` — older
            internal references to ``index_diff`` are obsolete.
        """
        result = _api_call(
            "GET",
            "/api/ecosystem/index_diffs/latest",
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        # Map 404 / not-found-style responses to a stable stub signal so callers
        # can distinguish "P0.4 not shipped yet" from other API failures.
        if isinstance(result, dict) and result.get("success") is False:
            detail = (result.get("error") or "") + " " + (result.get("detail") or "")
            if "404" in detail or "Not Found" in detail or "not_found" in detail:
                return {
                    "success": False,
                    "error": "P0.4 will implement",
                    "detail": result.get("detail", ""),
                }
        return result

    @mcp.tool()
    def ecosystem_repo_events(repo_id: str, limit: int = 50) -> dict[str, Any]:
        """Return event history for a single ecosystem repo (v1.6.0 event sourcing).

        Each event captures a discrete operation: discovered, rescanned, topics_changed,
        stars_jumped, status_changed, archived, manual_pinned, manual_unpinned, removed_from_query.

        Args:
            repo_id: EcosystemRepoProfile.id to query events for.
            limit: Max events to return (default 50, max 200).

        Returns:
            {success, repo_id, events: [{id, event_type, payload_json, source, triggered_at, ...}], total}
        """
        result = _api_call(
            "GET",
            f"/api/ecosystem/repos/{repo_id}/events?limit={limit}",
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_diff_period(from_date: str, to_date: str) -> dict[str, Any]:
        """Return a time-period diff computed dynamically from the event log (v1.6.0 event sourcing).

        Groups events by type to produce summary counts: new repos discovered, topics changed,
        stars jumped, status changed. This replaces the legacy index_diff snapshot approach.

        Args:
            from_date: Start date in YYYY-MM-DD format (inclusive).
            to_date: End date in YYYY-MM-DD format (inclusive).

        Returns:
            {success, from, to, summary: {new, topics_changed, stars_jumped, status_changed, ...},
             events_count, events_by_type: {event_type: count}}
        """
        import urllib.parse as _up
        qs = _up.urlencode({"from": from_date, "to": to_date})
        result = _api_call(
            "GET",
            f"/api/ecosystem/diff?{qs}",
            extra_headers=_project_headers(),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_rebuild_queries_from_repos(project_id: str = "") -> dict[str, Any]:
        """Return a recap of all search queries that have discovered repos in this project.

        Scans discovered_via_queries across all stored profiles and aggregates counts per query.
        Useful to audit which queries are most productive and which repos are multi-query hits.

        Args:
            project_id: Optional project scope override.

        Returns:
            {queries: list[str], by_query: {query: count}, total_repos: N, repos_with_query_data: N}
        """
        result = _api_call(
            "GET",
            "/api/ecosystem/queries_recap",
            extra_headers=_project_headers(project_id),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_pin_active(repo_id: str, reason: str = "", project_id: str = "") -> dict[str, Any]:
        """Pin a repo to ensure it stays permanently active regardless of future scan results.

        Pinned repos are excluded from the 'removed_from_query' count in index_update diffs
        and their last_active_status is always 'active' even if the fetcher misses them.

        Use this for high-value repos you want to track regardless of GitHub query hits.

        Args:
            repo_id: EcosystemRepoProfile.id of the target repo.
            reason: Short explanation for pinning (stored for audit).
            project_id: Optional project scope override.

        Returns:
            {success, repo_id, manual_status, reason, message}
        """
        result = _api_call(
            "POST",
            f"/api/ecosystem/repos/{repo_id}/manual_status",
            {"status": "pinned", "reason": reason, "set_by": "ecosystem_pin_active"},
            extra_headers=_project_headers(project_id),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_unpin(repo_id: str, project_id: str = "") -> dict[str, Any]:
        """Remove the pin from a repo (undo ecosystem_pin_active).

        Equivalent to clearing manual_status — the repo reverts to normal scan-driven status.

        Args:
            repo_id: EcosystemRepoProfile.id of the target repo.
            project_id: Optional project scope override.

        Returns:
            {success, repo_id, manual_status, message}
        """
        result = _api_call(
            "POST",
            f"/api/ecosystem/repos/{repo_id}/manual_status",
            {"status": None, "reason": "", "set_by": "ecosystem_unpin"},
            extra_headers=_project_headers(project_id),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_mark_no_value(repo_id: str, reason: str = "", project_id: str = "") -> dict[str, Any]:
        """Mark a repo as no_value (manual_archived) after deep review shows it has low value.

        Sets manual_status='no_value' on the repo, immediately changes last_active_status
        to 'manual_archived', and records reason + who set it.

        Args:
            repo_id: EcosystemRepoProfile.id of the target repo.
            reason: Short explanation why this repo has no value (stored for audit).
            project_id: Optional project scope override.

        Returns:
            {success, repo_id, manual_status, reason, message}
        """
        result = _api_call(
            "POST",
            f"/api/ecosystem/repos/{repo_id}/manual_status",
            {"status": "no_value", "reason": reason, "set_by": "ecosystem_mark_no_value"},
            extra_headers=_project_headers(project_id),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result

    @mcp.tool()
    def ecosystem_clear_manual_status(repo_id: str, project_id: str = "") -> dict[str, Any]:
        """Clear manual_status on a repo (undo ecosystem_mark_no_value).

        Restores last_active_status to 'active' (unless repo is GitHub-archived).

        Args:
            repo_id: EcosystemRepoProfile.id of the target repo.
            project_id: Optional project scope override.

        Returns:
            {success, repo_id, manual_status, message}
        """
        result = _api_call(
            "POST",
            f"/api/ecosystem/repos/{repo_id}/manual_status",
            {"status": None, "reason": "", "set_by": "ecosystem_clear_manual_status"},
            extra_headers=_project_headers(project_id),
        )
        if not result:
            return {"success": False, "error": "api_unavailable"}
        return result
