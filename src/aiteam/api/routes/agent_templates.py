"""Agent template index and recommendation routes.

Thin HTTP layer over ``aiteam.services.agent_template_registry`` - the catalogue
itself (three sources, CC precedence, de-duplication by frontmatter ``name``) lives
there because the meeting and ecosystem-tagger dispatch builders need the very same
truth source and must not import an API route.

Only the user directory used to be scanned, so the reported catalogue never matched
what ``subagent_type`` would actually accept: a repo-local template was invisible,
and a freshly-installed OS whose templates had not been copied to ``~`` yet looked
empty.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Header

from aiteam.services.agent_template_registry import (
    collect_templates,
    parse_template,
    template_sources,
)

router = APIRouter(tags=["agent-templates"])

# Browse bucket for templates that carry no family prefix.
GENERAL_BUCKET = "general"
# A leading stem segment counts as a family only once this many templates share it.
MIN_FAMILY_MEMBERS = 2


def _stem_family(stem: str) -> str:
    """Leading segment of the filename stem, e.g. engineering-sre -> engineering."""
    head, _sep, rest = stem.partition("-")
    return head if rest else ""


def _group_by_family(templates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket templates for browsing, keyed on the filename stem's family prefix.

    Bucket and row identity are orthogonal questions. Identity is the frontmatter
    name (see aiteam.services.agent_template_registry); the browse bucket stays on
    the stem, because that is where the de-facto family lives - engineering-,
    testing-, support-, management-. Bucketing by name instead scattered 25
    templates across 21 buckets: consistent with the de-dup key, useless to skim.

    A leading segment only counts as a family once at least MIN_FAMILY_MEMBERS
    templates share it; a lone stem such as team-member lands in GENERAL_BUCKET
    rather than minting a bucket of one.
    """
    families: dict[str, int] = {}
    for t in templates:
        family = _stem_family(str(t.get("filename", "")))
        if family:
            families[family] = families.get(family, 0) + 1

    grouped: dict[str, list[dict[str, Any]]] = {}
    for t in templates:
        family = _stem_family(str(t.get("filename", "")))
        bucket = family if families.get(family, 0) >= MIN_FAMILY_MEMBERS else GENERAL_BUCKET
        grouped.setdefault(bucket, []).append(t)
    return grouped


@router.get("/api/agent-templates")
async def list_templates(
    x_project_dir: str | None = Header(default=None, alias="X-Project-Dir"),
):
    """List all Agent templates CC can resolve (project > user > plugin)."""
    templates, sources = collect_templates(x_project_dir)
    grouped = _group_by_family(templates)
    return {
        "templates": templates,
        "grouped": grouped,
        "total": len(templates),
        "sources": sources,
    }


@router.get("/api/agent-templates/recommend")
async def recommend_template(
    task_type: str = "",
    keywords: str = "",
    x_project_dir: str | None = Header(default=None, alias="X-Project-Dir"),
) -> dict[str, Any]:
    """Recommend suitable Agent templates based on task type.

    Note: this route must be registered before /{name} to avoid path conflicts.
    """
    query = (task_type + " " + keywords).lower()
    templates, _sources = collect_templates(x_project_dir)
    matched: list[dict[str, Any]] = []
    for meta in templates:
        desc = (str(meta.get("description", "")) + " " + str(meta.get("name", ""))).lower()
        score = sum(1 for word in query.split() if word and word in desc)
        if score > 0:
            hit = dict(meta)
            hit["match_score"] = score
            matched.append(hit)
    matched.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    return {"recommendations": matched[:5], "query": query}


@router.get("/api/agent-templates/{name}")
async def get_template(
    name: str,
    x_project_dir: str | None = Header(default=None, alias="X-Project-Dir"),
) -> dict[str, Any]:
    """Get full content of a single template (first source that has it wins).

    ``name`` accepts either the frontmatter name the listing reports (the identity CC
    resolves) or the filename stem. Matching the stem only would 404 on 15 of the 25
    shipped templates, whose stem and declared name differ.
    """
    # Prevent path traversal
    if not re.match(r"^[\w\-]+$", name):
        return {"error": "无效的模板名称"}
    for label, directory in template_sources(x_project_dir):
        # Fast path: the stem matches.
        path = directory / f"{name}.md"
        if path.exists():
            meta = parse_template(path) or {}
            meta["source"] = label
            return {"meta": meta, "content": path.read_text(encoding="utf-8"), "source": label}
        # Fall back to the declared name, e.g. engineering-security-engineer.md
        # answering to "security-engineer".
        if directory.is_dir():
            for candidate in sorted(directory.glob("*.md")):
                meta = parse_template(candidate)
                if meta and meta.get("name") == name:
                    meta["source"] = label
                    return {
                        "meta": meta,
                        "content": candidate.read_text(encoding="utf-8"),
                        "source": label,
                    }
    return {"error": f"模板 {name} 不存在"}
