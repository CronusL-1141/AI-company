"""Agent template index and recommendation routes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import json as _json

router = APIRouter(tags=["agent-templates"])

AGENTS_DIR = Path.home() / ".claude" / "agents"


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
        meta["body_preview"] = parts[2].strip()[:200]
        return meta
    except Exception:
        return None


@router.get("/api/agent-templates")
async def list_templates():
    """List all available Agent templates."""
    templates: list[dict[str, Any]] = []
    if AGENTS_DIR.exists():
        for f in sorted(AGENTS_DIR.glob("*.md")):
            meta = _parse_template(f)
            if meta:
                templates.append(meta)
    # Group by category (first segment before '-' in filename)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for t in templates:
        filename = t.get("filename", "")
        cat = filename.split("-")[0] if "-" in filename else "other"
        grouped.setdefault(cat, []).append(t)
    return {"templates": templates, "grouped": grouped, "total": len(templates)}


@router.get("/api/agent-templates/recommend")
async def recommend_template(task_type: str = "", keywords: str = "") -> dict[str, Any]:
    """Recommend suitable Agent templates based on task type.

    Note: this route must be registered before /{name} to avoid path conflicts.
    """
    query = (task_type + " " + keywords).lower()
    templates: list[dict[str, Any]] = []
    if AGENTS_DIR.exists():
        for f in AGENTS_DIR.glob("*.md"):
            meta = _parse_template(f)
            if meta:
                desc = (meta.get("description", "") + " " + meta.get("name", "")).lower()
                score = sum(1 for word in query.split() if word and word in desc)
                if score > 0:
                    meta["match_score"] = score
                    templates.append(meta)
    templates.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    return {"recommendations": templates[:5], "query": query}


@router.get("/api/agent-templates/{name}")
async def get_template(name: str) -> dict[str, Any]:
    """Get full content of a single template."""
    # Prevent path traversal
    if not re.match(r'^[\w\-]+$', name):
        return {"error": "无效的模板名称"}
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        return {"error": f"模板 {name} 不存在"}
    content = path.read_text(encoding="utf-8")
    meta = _parse_template(path) or {}
    return {"meta": meta, "content": content}
