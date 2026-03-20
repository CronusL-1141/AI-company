"""Agent模板索引和推荐路由."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["agent-templates"])

AGENTS_DIR = Path.home() / ".claude" / "agents"


def _parse_template(path: Path) -> dict[str, Any] | None:
    """解析Agent模板的frontmatter."""
    try:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        # 简单frontmatter解析（不依赖yaml以外的包，pyyaml已在依赖中）
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
async def list_templates() -> dict[str, Any]:
    """列出所有可用Agent模板."""
    templates: list[dict[str, Any]] = []
    if AGENTS_DIR.exists():
        for f in sorted(AGENTS_DIR.glob("*.md")):
            meta = _parse_template(f)
            if meta:
                templates.append(meta)
    # 按category分组（取文件名第一个'-'之前的部分）
    grouped: dict[str, list[dict[str, Any]]] = {}
    for t in templates:
        filename = t.get("filename", "")
        cat = filename.split("-")[0] if "-" in filename else "other"
        grouped.setdefault(cat, []).append(t)
    return {"templates": templates, "grouped": grouped, "total": len(templates)}


@router.get("/api/agent-templates/recommend")
async def recommend_template(task_type: str = "", keywords: str = "") -> dict[str, Any]:
    """根据任务类型推荐合适的Agent模板.

    注意：此路由必须在 /{name} 之前注册，避免路径冲突。
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
    """获取单个模板完整内容."""
    # 防止路径遍历
    if not re.match(r'^[\w\-]+$', name):
        return {"error": "无效的模板名称"}
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        return {"error": f"模板 {name} 不存在"}
    content = path.read_text(encoding="utf-8")
    meta = _parse_template(path) or {}
    return {"meta": meta, "content": content}
