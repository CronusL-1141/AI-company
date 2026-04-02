"""Agent template config routes — list and update agent .md files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/agents-config", tags=["agents-config"])

# Directories to scan for agent templates
_HOME_AGENTS_DIR = Path.home() / ".claude" / "agents"
_PLUGIN_AGENTS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "plugin" / "agents"


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (meta_dict, body) tuple. No external YAML dep — simple string splitting.
    Supports scalar values, list values (- item), and skips block scalars (> |).
    """
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter_str = parts[1]
    body = parts[2].lstrip("\n")

    meta: dict = {}
    current_key: str | None = None
    current_is_list = False
    skip_block_scalar = False

    for line in frontmatter_str.splitlines():
        # List item under current key
        stripped = line.strip()
        if stripped.startswith("- ") and current_key is not None and current_is_list:
            meta[current_key].append(stripped[2:].strip())
            continue

        if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            # Block scalar: skip multi-line value, store empty string
            if value in (">", "|", ">-", "|-", ">+", "|+"):
                meta[key] = ""
                current_key = key
                current_is_list = False
                skip_block_scalar = True
                continue

            skip_block_scalar = False

            if value == "":
                # Possibly a list follows
                meta[key] = []
                current_key = key
                current_is_list = True
            else:
                meta[key] = value
                current_key = key
                current_is_list = False
        elif skip_block_scalar:
            # Accumulate block scalar lines into the value
            if current_key is not None:
                sep = " " if meta[current_key] else ""
                meta[current_key] = meta[current_key] + sep + stripped

    return meta, body


def _build_content(original_meta: dict, updates: dict, prompt: str) -> str:
    """Rebuild markdown file content preserving all original fields.

    Merges updates into original_meta so unknown fields (skills, tools, etc.) are retained.
    description is sanitised: newlines replaced with spaces to avoid breaking frontmatter.
    """
    # Sanitise description: strip newlines so frontmatter stays valid
    if "description" in updates and updates["description"]:
        updates = {**updates, "description": updates["description"].replace("\n", " ").replace("\r", "").strip()}

    # Merge: updates override original, but only for non-empty update values
    merged = dict(original_meta)
    for k, v in updates.items():
        if v or k in original_meta:
            merged[k] = v

    lines = ["---"]
    for key, value in merged.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(prompt)
    return "\n".join(lines)


def _scan_templates() -> list[dict[str, Any]]:
    """Scan both directories; ~/.claude/agents/ takes priority over plugin/agents/."""
    # Collect plugin templates first
    plugin_files: dict[str, Path] = {}
    if _PLUGIN_AGENTS_DIR.exists():
        for f in sorted(_PLUGIN_AGENTS_DIR.glob("*.md")):
            plugin_files[f.name] = f

    # Home dir overrides plugin dir
    home_files: dict[str, Path] = {}
    if _HOME_AGENTS_DIR.exists():
        for f in sorted(_HOME_AGENTS_DIR.glob("*.md")):
            home_files[f.name] = f

    # Merge: home takes priority
    all_files: dict[str, Path] = {**plugin_files, **home_files}

    results: list[dict[str, Any]] = []
    for filename, path in sorted(all_files.items()):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _parse_frontmatter(content)
        results.append(
            {
                "filename": filename,
                "name": meta.get("name", path.stem),
                "description": meta.get("description", ""),
                "model": meta.get("model", ""),
                "color": meta.get("color", ""),
                "prompt": body,
            }
        )

    return results


class AgentTemplateUpdate(BaseModel):
    name: str
    description: str = ""
    model: str = ""
    color: str = ""
    prompt: str = ""


@router.get("")
async def list_agent_templates() -> dict[str, Any]:
    """List all agent templates from ~/.claude/agents/ and plugin/agents/."""
    templates = _scan_templates()
    return {"data": templates}


@router.put("/{filename}")
async def update_agent_template(filename: str, body: AgentTemplateUpdate) -> dict[str, Any]:
    """Update an agent template file."""
    # Validate filename — only allow safe names
    if not re.match(r"^[\w\-]+\.md$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Read existing file to preserve unknown frontmatter fields (skills, tools, etc.)
    home_path = _HOME_AGENTS_DIR / filename
    plugin_path = _PLUGIN_AGENTS_DIR / filename

    original_meta: dict = {}
    if home_path.exists():
        original_meta, _ = _parse_frontmatter(home_path.read_text(encoding="utf-8"))
    elif plugin_path.exists():
        original_meta, _ = _parse_frontmatter(plugin_path.read_text(encoding="utf-8"))

    updates = {
        "name": body.name,
        "description": body.description,
        "model": body.model,
        "color": body.color,
    }
    new_content = _build_content(original_meta, updates, body.prompt)

    # Write only to ~/.claude/agents/ — plugin/agents/ is read-only source of defaults
    _HOME_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    home_path.write_text(new_content, encoding="utf-8")

    return {
        "data": {
            "filename": filename,
            "name": body.name,
            "description": body.description,
            "model": body.model,
            "color": body.color,
            "prompt": body.prompt,
        },
        "written_to": [str(home_path)],
    }
