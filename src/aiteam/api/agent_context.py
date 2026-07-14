"""AI Team OS — sub-agent context watermark capture (P1 ledger, batch 1B).

Regular Agent-tool sub-agents are a blind spot for the main-session monitor
(context_tracker hook runs on UserPromptSubmit, and sub-agents never submit a
user prompt). This module fills that gap: it reads the exact "last assistant
context token total" from a sub-agent transcript jsonl, converts it to a window
usage percentage, and is reused by two call sites — the SubagentStop event
(hook_translator) and the reaper backfill scan (state_reaper).

Single source of truth for the token measure: this module delegates to
``workflow_ingest._last_assistant_ctx_tokens`` (the D1-ruled formula:
input + cache_creation + cache_read + output; reconciled against wf terminal
per-agent tokens). No second token formula is introduced here.

Window sizing mirrors context_tracker's dominant path: the CLAUDE_CONTEXT_SIZE
env var is the override, otherwise default to 1M (Claude Code's platform-wide
default window; under-reporting is less harmful than false high-usage alarms).
See docs/agent-reuse-design.md section 4.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from aiteam.api import workflow_ingest

logger = logging.getLogger(__name__)

# Claude Code's platform-wide default context window (same rationale as
# context_tracker: a low estimate that misses a warning is less disruptive than
# frequent false alarms).
DEFAULT_CONTEXT_SIZE = 1_000_000


def read_ctx_tokens(transcript_path: str | Path) -> int | None:
    """Read the last assistant context token total from a sub-agent transcript.

    Single source of truth: delegates to the D1-ruled reader in workflow_ingest
    (four-field sum). Returns None on read failure / no usage line, so the caller
    keeps the previously stored value.
    """
    try:
        return workflow_ingest._last_assistant_ctx_tokens(Path(transcript_path))
    except Exception:  # noqa: BLE001 — watermark reads must never raise
        return None


def compute_window_pct(tokens: int) -> tuple[int, float]:
    """Map a token total to (window_size, usage_pct).

    Window sizing priority (mirrors context_tracker._compute_used_pct's dominant
    path): CLAUDE_CONTEXT_SIZE env override (any positive integer, used to force a
    smaller window), otherwise the 1M default.
    """
    env_size = os.environ.get("CLAUDE_CONTEXT_SIZE", "").strip()
    window = int(env_size) if env_size.isdigit() and int(env_size) > 0 else DEFAULT_CONTEXT_SIZE
    pct = round((tokens / window) * 100, 1) if window else 0.0
    return window, pct


def _projects_dir() -> Path:
    """``~/.claude/projects`` root (monkeypatchable in tests)."""
    return Path.home() / ".claude" / "projects"


def _project_slug(root_path: str) -> str:
    """Reverse a project root_path into its CC projects dir slug.

    Each non-alphanumeric char maps to '-' (CC does not fold separators), same
    convention as workflow_ingest._project_slug.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", root_path or "")


def locate_transcript(
    *,
    stored_path: str | None,
    cc_tool_use_id: str | None,
    session_id: str | None,
    project_root: str | None = None,
) -> Path | None:
    """Locate a sub-agent transcript: prefer the stored path, fall back to cc id.

    Fallback addressing: ``<slug>/<session>/subagents/agent-<ccid>.jsonl``
    (CC stores each sub-agent transcript as ``agent-<agentId>.jsonl`` and the OS
    stores that agentId as cc_tool_use_id). Widens the glob when slug/session are
    unknown. Returns None when cc_tool_use_id is missing (cannot reconstruct).
    """
    if stored_path:
        p = Path(stored_path)
        if p.is_file():
            return p
    if not cc_tool_use_id:
        return None
    base = _projects_dir()
    fname = f"agent-{cc_tool_use_id}.jsonl"
    # 1. Exact slug + session.
    if project_root and session_id:
        cand = base / _project_slug(project_root) / session_id / "subagents" / fname
        if cand.is_file():
            return cand
    # 2. Any slug, known session.
    if session_id:
        try:
            for cand in base.glob(f"*/{session_id}/subagents/{fname}"):
                if cand.is_file():
                    return cand
        except OSError:
            pass
    # 3. Global by cc id (last resort; bounded because the filename is unique).
    try:
        for cand in base.glob(f"*/*/subagents/{fname}"):
            if cand.is_file():
                return cand
    except OSError:
        pass
    return None


def measure(transcript_path: str | Path) -> dict[str, Any] | None:
    """Read one transcript -> {ctx_tokens, ctx_window, ctx_pct, ctx_measured_at}.

    Returns None when the token total cannot be read (caller then skips the write
    and keeps the prior value). The returned dict is spread directly into
    ``repo.update_agent(**dict)``.
    """
    tokens = read_ctx_tokens(transcript_path)
    if tokens is None:
        return None
    window, pct = compute_window_pct(tokens)
    return {
        "ctx_tokens": tokens,
        "ctx_window": window,
        "ctx_pct": pct,
        "ctx_measured_at": datetime.now(),
    }
