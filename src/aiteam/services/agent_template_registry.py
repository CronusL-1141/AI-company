"""Agent template catalogue - the single truth source for ``subagent_type``.

Templates come from three directories, mirroring CC's own resolution order:

    project (<project>/.claude/agents/)  >  user (~/.claude/agents/)  >  plugin (shipped)

De-duplication is by frontmatter ``name`` - the only identity CC resolves - with the
highest-precedence source winning. (Keying on the filename stem was a second truth
source: 15 of the 25 shipped templates have ``stem != name``.)

This module deliberately lives in the service layer, not behind the HTTP route that
used to own it: the meeting and ecosystem-tagger dispatch builders need the same
catalogue, and a service importing an API route would invert the layering and risk an
import cycle.

Validation policy (2026-08-03 ruling): **templates are an accelerator, not a
whitelist.** The set of types the Agent tool accepts is the OS catalogue *plus* CC's
built-ins, which cannot be enumerated and shift between CC versions. So
``check_subagent_types`` only *reports* - it never rewrites, downgrades or rejects the
type its caller asked for. An unknown name is surfaced as a top-level warning for the
human reading the plan to adjudicate.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

AGENTS_DIR = Path.home() / ".claude" / "agents"
# plugin/agents/ inside the repo - this file is src/aiteam/services/agent_template_registry.py
PLUGIN_AGENTS_DIR = Path(__file__).resolve().parents[3] / "plugin" / "agents"

# The one subagent_type this repo is allowed to hardcode as a fallback: a CC built-in
# that exists even without the OS plugin installed. It is NOT in the template
# catalogue, so it is added explicitly - this is a single sanctioned default, not an
# enumeration of CC's built-in types (which would go stale on every CC release).
FALLBACK_SUBAGENT_TYPE = "general-purpose"

_UNKNOWN_TEMPLATE_MESSAGE = (
    "{name} 不在模板库:若是 CC 内置类型可直接用;"
    "若是拼写错误,建议就近模板 {suggestions} 或 {fallback} + 原 prompt。"
    "OS 不拦截,已按原样放行,由读计划的人定夺。"
)


def project_agents_dir(project_dir: str | None) -> Path | None:
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


def template_sources(project_dir: str | None) -> list[tuple[str, Path]]:
    """Ordered (source label, directory) pairs - highest precedence first."""
    sources: list[tuple[str, Path]] = []
    proj = project_agents_dir(project_dir)
    if proj is not None:
        sources.append(("project", proj))
    sources.append(("user", AGENTS_DIR))
    sources.append(("plugin", PLUGIN_AGENTS_DIR))
    return sources


def parse_template(path: Path) -> dict[str, Any] | None:
    """Parse Agent template frontmatter.

    Frontmatter is parsed as YAML, never scraped with a regex: a regex for
    ``^name:`` silently matches an example line in the body when the template
    itself declares no name, which mints a template that does not exist.
    """
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


def collect_templates(project_dir: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Scan every source once; return (deduped templates, per-source report)."""
    seen: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {}
    for label, directory in template_sources(project_dir):
        found = 0
        if directory.is_dir():
            for f in sorted(directory.glob("*.md")):
                meta = parse_template(f)
                if not meta:
                    continue
                found += 1
                # Keyed on the frontmatter name (stem fallback applied in
                # parse_template) - the same identity CC resolves.
                # Highest-precedence source wins - later sources never override.
                if meta["name"] in seen:
                    continue
                meta["source"] = label
                seen[meta["name"]] = meta
        report[label] = {"dir": str(directory), "found": found}
    templates = [seen[k] for k in sorted(seen)]
    return templates, report


def known_template_names(project_dir: str | None = None) -> set[str]:
    """Every template name CC can resolve from the three sources."""
    templates, _report = collect_templates(project_dir)
    return {str(t["name"]) for t in templates}


def known_subagent_types(project_dir: str | None = None) -> set[str]:
    """Template names plus the one sanctioned hardcoded fallback.

    Deliberately *not* the full set the Agent tool accepts - CC's built-ins are
    unenumerable. Anything outside this set is reported, never blocked.
    """
    return known_template_names(project_dir) | {FALLBACK_SUBAGENT_TYPE}


def suggest_templates(name: str, candidates: Iterable[str], limit: int = 3) -> list[str]:
    """Closest template names by plain string similarity (stdlib only)."""
    return difflib.get_close_matches(name, sorted(candidates), n=limit, cutoff=0.5)


def check_subagent_types(
    usages: Iterable[tuple[str, str]],
    *,
    project_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Report subagent_types absent from the catalogue - report only, never rewrite.

    Args:
        usages: (subagent_type, label of what would be spawned with it) pairs.
        project_dir: Project directory whose .claude/agents also counts as a source.

    Returns:
        One entry per distinct unknown type, each with the affected labels, the
        closest template names and a ready-to-show message. Empty list = all known.
    """
    known = known_subagent_types(project_dir)
    unknown: dict[str, list[str]] = {}
    for subagent_type, label in usages:
        name = (subagent_type or "").strip()
        if not name or name in known:
            continue
        unknown.setdefault(name, [])
        if label and label not in unknown[name]:
            unknown[name].append(label)

    warnings: list[dict[str, Any]] = []
    for name in sorted(unknown):
        closest = suggest_templates(name, known)
        warnings.append({
            "subagent_type": name,
            "used_by": unknown[name],
            "closest_templates": closest,
            "message": _UNKNOWN_TEMPLATE_MESSAGE.format(
                name=name,
                suggestions="/".join(closest) if closest else "(无相近模板)",
                fallback=FALLBACK_SUBAGENT_TYPE,
            ),
        })
    return warnings
