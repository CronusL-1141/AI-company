"""Fact-stream evaluator — Phase 2 auto-advance decision centre.

Each stage transition's exit condition is evaluated by a pure function that
receives injected fact_provider and clock — no direct DB/FS access.

AdvanceDecision:
    ADVANCE     — objective condition met, advance immediately
    SUGGEST     — subjective condition met, suggest to Leader but don't force
    NO_DECISION — insufficient data or stage not in evaluation scope
    FALL_BACK   — failure signal, revert to recovery stage (e.g. test → fix)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)

_MTIME_TOLERANCE_SECONDS = 2  # Council R1 Issue 7: CI mtime jitter guard


class AdvanceDecision(Enum):
    ADVANCE = "advance"
    SUGGEST = "suggest"
    NO_DECISION = "no_decision"
    FALL_BACK = "fall_back"


class FactProvider(Protocol):
    """Fact query interface — tests inject stubs, production injects DbFactProvider."""

    async def count_subtasks(self, parent_id: str) -> int:
        """Return number of child tasks for parent_id."""
        ...

    def src_files_modified_since(self, since: datetime) -> bool:
        """Return True if any src/ file has mtime strictly after since + tolerance."""
        ...

    async def last_bash_event(self, task_id: str) -> dict | None:
        """Return the most recent Bash PostToolUse event dict, or None.

        Expected keys: exit_code (int), stdout (str).
        """
        ...

    async def memos_since(
        self, task_id: str, since: datetime, memo_type: str | None = None
    ) -> list[dict]:
        """Return memo records added after since, optionally filtered by type."""
        ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_aware(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware (UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _evaluate_decompose(
    task_id: str,
    fact_provider: FactProvider,
) -> tuple[AdvanceDecision, str | None, str]:
    """decompose → implement: subtask count >= 1 (Council R1 U-7: N=1)."""
    try:
        n = await fact_provider.count_subtasks(task_id)
    except Exception as exc:
        logger.warning("evaluator: count_subtasks failed for %s: %s", task_id, exc)
        return AdvanceDecision.NO_DECISION, None, f"count_subtasks error: {exc}"

    if n >= 1:
        return AdvanceDecision.ADVANCE, "implement", f"{n} subtask(s) created"
    return AdvanceDecision.NO_DECISION, None, f"no subtasks yet (count={n})"


async def _evaluate_implement(
    task_id: str,
    fact_provider: FactProvider,
    stage_started_at: datetime,
) -> tuple[AdvanceDecision, str | None, str]:
    """implement → test: src/ file mtime > stage_started_at + 2s tolerance."""
    threshold = _ensure_aware(stage_started_at) + timedelta(seconds=_MTIME_TOLERANCE_SECONDS)
    try:
        modified = fact_provider.src_files_modified_since(threshold)
    except Exception as exc:
        logger.warning("evaluator: src_files_modified_since failed: %s", exc)
        return AdvanceDecision.NO_DECISION, None, f"src_files_modified_since error: {exc}"

    if modified:
        return AdvanceDecision.ADVANCE, "test", "src/ files modified since stage start"
    return AdvanceDecision.NO_DECISION, None, "no src/ file changes detected"


async def _evaluate_test(
    task_id: str,
    fact_provider: FactProvider,
    template: str,
) -> tuple[AdvanceDecision, str | None, str]:
    """test → done/completed (pass) or test → fix (fail)."""
    from aiteam.pipeline.signals import is_fail_signal, is_pass_signal
    from aiteam.pipeline.templates import LIFECYCLE_TEMPLATES

    try:
        event = await fact_provider.last_bash_event(task_id)
    except Exception as exc:
        logger.warning("evaluator: last_bash_event failed for %s: %s", task_id, exc)
        return AdvanceDecision.NO_DECISION, None, f"last_bash_event error: {exc}"

    if event is None:
        return AdvanceDecision.NO_DECISION, None, "no Bash event found"

    exit_code: int = event.get("exit_code", -1)
    stdout: str = event.get("stdout", "") or ""

    if is_pass_signal(stdout, exit_code):
        # Determine terminal stage for this template
        stages = LIFECYCLE_TEMPLATES.get(template, [])
        terminal = stages[-1] if stages else "done"
        return AdvanceDecision.ADVANCE, terminal, "bash exit=0 with pass signal"

    if is_fail_signal(stdout, exit_code):
        # fall_back to fix if it exists in template, else NO_DECISION
        stages = LIFECYCLE_TEMPLATES.get(template, [])
        if "fix" in stages:
            return AdvanceDecision.FALL_BACK, "fix", f"bash exit={exit_code} with fail signal"
        return AdvanceDecision.FALL_BACK, None, f"bash exit={exit_code} fail signal, no fix stage in template"

    # exit=0 but no pass signal pattern found
    return AdvanceDecision.NO_DECISION, None, f"bash exit={exit_code} but no pass/fail signal matched"


async def _evaluate_review(
    task_id: str,
    fact_provider: FactProvider,
    stage_started_at: datetime,
) -> tuple[AdvanceDecision, str | None, str]:
    """review → retest: a review-type memo exists since stage start."""
    try:
        memos = await fact_provider.memos_since(task_id, _ensure_aware(stage_started_at), memo_type="review")
    except Exception as exc:
        logger.warning("evaluator: memos_since failed for %s: %s", task_id, exc)
        return AdvanceDecision.NO_DECISION, None, f"memos_since error: {exc}"

    if memos:
        return AdvanceDecision.ADVANCE, "retest", f"{len(memos)} review memo(s) found"
    return AdvanceDecision.NO_DECISION, None, "no review memos yet"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def evaluate(
    task_id: str,
    current_stage: str,
    template: str,
    fact_provider: FactProvider,
    clock: object,
    stage_started_at: datetime,
) -> tuple[AdvanceDecision, str | None, str]:
    """Evaluate whether the current stage's exit condition is met.

    Returns:
        (decision, target_stage_or_None, reason)
        - decision: AdvanceDecision value
        - target_stage: next stage name (ADVANCE/FALL_BACK) or None
        - reason: human-readable explanation string
    """
    from aiteam.pipeline.templates import LIFECYCLE_TEMPLATES

    # Validate that current_stage is in the template (Issue 5 dirty-data guard)
    stages = LIFECYCLE_TEMPLATES.get(template, [])
    if stages and current_stage not in stages:
        logger.warning(
            "evaluator: stage '%s' not found in template '%s' stages=%s",
            current_stage, template, stages,
        )
        return AdvanceDecision.NO_DECISION, None, (
            f"stage '{current_stage}' not in template '{template}'"
        )

    if current_stage == "decompose":
        return await _evaluate_decompose(task_id, fact_provider)

    if current_stage == "implement":
        return await _evaluate_implement(task_id, fact_provider, stage_started_at)

    if current_stage == "test" or current_stage == "retest":
        return await _evaluate_test(task_id, fact_provider, template)

    if current_stage == "review":
        return await _evaluate_review(task_id, fact_provider, stage_started_at)

    # Plan-class stages (research, meeting, diagnose, report, decision, fix, spike)
    # are SUGGEST-only — cannot be objectively determined
    return AdvanceDecision.NO_DECISION, None, (
        f"stage '{current_stage}' has no objective exit condition (use force=True or manual advance)"
    )
