"""Agent template index and recommendation routes.

Templates come from three directories, mirroring CC's own resolution order:

    project (<project>/.claude/agents/)  >  user (~/.claude/agents/)  >  plugin (shipped)

Only the user directory used to be scanned, so the reported catalogue never matched
what ``subagent_type`` would actually accept: a repo-local template was invisible,
and a freshly-installed OS whose templates had not been copied to ``~`` yet looked
empty.

De-duplication is by frontmatter ``name`` - the only identity CC resolves - with the
highest-precedence source winning. Keying on the filename stem instead was a second
source of truth: 15 of the 25 shipped templates have ``stem != name``
(``engineering-security-engineer.md`` declares ``name: security-engineer``), so a
user-level ``security-engineer.md`` and the plugin-level file were reported as two
templates while CC sees exactly one.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header

router = APIRouter(tags=["agent-templates"])

AGENTS_DIR = Path.home() / ".claude" / "agents"
# plugin/agents/ inside the repo — this file is src/aiteam/api/routes/agent_templates.py
PLUGIN_AGENTS_DIR = Path(__file__).resolve().parents[4] / "plugin" / "agents"


def _project_agents_dir(project_dir: str | None) -> Path | None:
    """Resolve <project>/.claude/agents for the caller's project directory."""
    raw = (project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    if not raw:
        return None
    try:
        import urllib.parse  # noqa: PLC0415

        path = Path(urllib.parse.unquote(raw)).expanduser()
    except Exception:
        return None
    candidate = path / ".claude" / "agents"
    return candidate if candidate.is_dir() else None


def _template_sources(project_dir: str | None) -> list[tuple[str, Path]]:
    """Ordered (source label, directory) pairs — highest precedence first."""
    sources: list[tuple[str, Path]] = []
    proj = _project_agents_dir(project_dir)
    if proj is not None:
        sources.append(("project", proj))
    sources.append(("user", AGENTS_DIR))
    sources.append(("plugin", PLUGIN_AGENTS_DIR))
    return sources


def _parse_template(path: Path) -> dict[str, Any] | None:
    """Parse Agent template frontmatter."""
    try:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        # Simple frontmatter parsing (no extra deps, pyyaml already in dependencies)
        import yaml  # noqa: PLC0415

        meta = yaml.safe_load(parts[1])
        if not isinstance(meta, dict):
            return None
        meta["filename"] = path.stem
        # Identity CC resolves subagent_type against; stem is only a fallback for
        # templates that omit (or blank out) the frontmatter name.
        declared = meta.get("name")
        meta["name"] = declared.strip() if isinstance(declared, str) and declared.strip() else path.stem
        meta["body_preview"] = parts[2].strip()[:200]
        return meta
    except Exception:
        return None


def _collect_templates(project_dir: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Scan every source once; return (deduped templates, per-source report)."""
    seen: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {}
    for label, directory in _template_sources(project_dir):
        found = 0
        if directory.is_dir():
            for f in sorted(directory.glob("*.md")):
                meta = _parse_template(f)
                if not meta:
                    continue
                found += 1
                # Keyed on the frontmatter name (stem fallback applied in
                # _parse_template) — the same identity CC resolves.
                # Highest-precedence source wins — later sources never override.
                if meta["name"] in seen:
                    continue
                meta["source"] = label
                seen[meta["name"]] = meta
        report[label] = {"dir": str(directory), "found": found}
    templates = [seen[k] for k in sorted(seen)]
    return templates, report


@router.get("/api/agent-templates")
async def list_templates(
    x_project_dir: str | None = Header(default=None, alias="X-Project-Dir"),
):
    """List all Agent templates CC can resolve (project > user > plugin)."""
    templates, sources = _collect_templates(x_project_dir)
    # Group by category (first segment before '-' in the resolved name) — same key
    # as de-duplication, so grouped can never re-split a template CC sees as one.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for t in templates:
        name = str(t.get("name", ""))
        cat = name.split("-")[0] if "-" in name else "other"
        grouped.setdefault(cat, []).append(t)
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
    templates, _sources = _collect_templates(x_project_dir)
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
    for label, directory in _template_sources(x_project_dir):
        # Fast path: the stem matches.
        path = directory / f"{name}.md"
        if path.exists():
            meta = _parse_template(path) or {}
            meta["source"] = label
            return {"meta": meta, "content": path.read_text(encoding="utf-8"), "source": label}
        # Fall back to the declared name, e.g. engineering-security-engineer.md
        # answering to "security-engineer".
        if directory.is_dir():
            for candidate in sorted(directory.glob("*.md")):
                meta = _parse_template(candidate)
                if meta and meta.get("name") == name:
                    meta["source"] = label
                    return {
                        "meta": meta,
                        "content": candidate.read_text(encoding="utf-8"),
                        "source": label,
                    }
    return {"error": f"模板 {name} 不存在"}
