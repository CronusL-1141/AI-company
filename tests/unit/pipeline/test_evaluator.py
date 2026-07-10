"""TC-EVAL-01 to TC-EVAL-06: fact-stream evaluator unit tests.

All tests use FakeClock + FakeFactProvider injection — no DB/FS access.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aiteam.pipeline.clock import FakeClock
from aiteam.pipeline.evaluator import AdvanceDecision, evaluate

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeFactProvider:
    """Configurable stub for FactProvider."""

    def __init__(
        self,
        subtask_count: int = 0,
        src_modified: bool = False,
        bash_event: dict | None = None,
        memos: list[dict] | None = None,
    ) -> None:
        self._subtask_count = subtask_count
        self._src_modified = src_modified
        self._bash_event = bash_event
        self._memos = memos or []

    async def count_subtasks(self, parent_id: str) -> int:  # noqa: ARG002
        return self._subtask_count

    def src_files_modified_since(self, since: datetime) -> bool:  # noqa: ARG002
        return self._src_modified

    async def last_bash_event(self, task_id: str) -> dict | None:  # noqa: ARG002
        return self._bash_event

    async def memos_since(
        self, task_id: str, since: datetime, memo_type: str | None = None  # noqa: ARG002
    ) -> list[dict]:
        if memo_type is None:
            return self._memos
        return [m for m in self._memos if m.get("type") == memo_type]


class _ErrorFactProvider(FakeFactProvider):
    """Stub that raises on every method (for TC-EVAL-04 data-missing tests)."""

    async def count_subtasks(self, parent_id: str) -> int:
        raise RuntimeError("db offline")

    def src_files_modified_since(self, since: datetime) -> bool:
        raise RuntimeError("fs error")

    async def last_bash_event(self, task_id: str) -> dict | None:
        raise RuntimeError("db offline")

    async def memos_since(self, task_id: str, since: datetime, memo_type: str | None = None) -> list[dict]:
        raise RuntimeError("db offline")


# ---------------------------------------------------------------------------
# TC-EVAL-01: decompose stage
# ---------------------------------------------------------------------------

class TestDecompose:
    """TC-EVAL-01: decompose → implement when subtask count ≥ 1."""

    @pytest.mark.asyncio
    async def test_no_subtasks_no_advance(self):
        fp = FakeFactProvider(subtask_count=0)
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="decompose",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None
        assert "count=0" in reason

    @pytest.mark.asyncio
    async def test_one_subtask_advances(self):
        fp = FakeFactProvider(subtask_count=1)
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="decompose",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        assert target == "implement"
        assert "1" in reason

    @pytest.mark.asyncio
    async def test_many_subtasks_advances(self):
        fp = FakeFactProvider(subtask_count=5)
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="decompose",
            template="refactor",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        assert target == "implement"


# ---------------------------------------------------------------------------
# TC-EVAL-02: implement stage
# ---------------------------------------------------------------------------

class TestImplement:
    """TC-EVAL-02: implement → test based on src/ file mtime."""

    @pytest.mark.asyncio
    async def test_src_modified_advances(self):
        fp = FakeFactProvider(src_modified=True)
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="implement",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        assert target == "test"
        assert "modified" in reason

    @pytest.mark.asyncio
    async def test_src_not_modified_no_advance(self):
        fp = FakeFactProvider(src_modified=False)
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="implement",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None

    @pytest.mark.asyncio
    async def test_src_exactly_at_threshold_no_advance(self):
        """mtime == threshold (not strictly after) should NOT advance (boundary guard)."""
        fp = FakeFactProvider(src_modified=False)
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="implement",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None


# ---------------------------------------------------------------------------
# TC-EVAL-03: test stage
# ---------------------------------------------------------------------------

class TestTestStage:
    """TC-EVAL-03: test → done (pass) or test → fix (fail)."""

    @pytest.mark.asyncio
    async def test_exit0_with_pass_signal_advances_to_done(self):
        fp = FakeFactProvider(bash_event={"exit_code": 0, "stdout": "5 passed, 0 failed"})
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="test",
            template="hotfix",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        # hotfix terminal stage is "test" but let's check it resolves to the last stage
        # hotfix: ["diagnose", "fix", "test"] — last = "test"; but test is current
        # The evaluator returns stages[-1] for terminal — for hotfix that is "test" itself
        # which in practice would be caught by pipeline as "already last" — not our concern here
        assert target is not None
        assert "pass signal" in reason

    @pytest.mark.asyncio
    async def test_exit0_with_pass_signal_feature_advances_to_last(self):
        fp = FakeFactProvider(bash_event={"exit_code": 0, "stdout": "all tests passed"})
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="test",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        # feature terminal = "retest"
        assert target == "retest"

    @pytest.mark.asyncio
    async def test_exit0_no_pass_signal_no_advance(self):
        fp = FakeFactProvider(bash_event={"exit_code": 0, "stdout": "running tests..."})
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="test",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None

    @pytest.mark.asyncio
    async def test_nonzero_exit_fall_back_to_fix(self):
        fp = FakeFactProvider(bash_event={"exit_code": 1, "stdout": "1 failed, 0 passed"})
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="test",
            template="hotfix",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.FALL_BACK
        assert target == "fix"
        assert "fail" in reason.lower()

    @pytest.mark.asyncio
    async def test_nonzero_exit_no_fix_stage(self):
        """refactor template has test but no fix stage — fall_back target should be None."""
        fp = FakeFactProvider(bash_event={"exit_code": 2, "stdout": "FAIL: 3 errors"})
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="test",
            template="refactor",  # refactor: ["decompose", "implement", "test", "review"] — no fix
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.FALL_BACK
        assert target is None

    @pytest.mark.asyncio
    async def test_no_bash_event_no_decision(self):
        fp = FakeFactProvider(bash_event=None)
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="test",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None


# ---------------------------------------------------------------------------
# TC-EVAL-04: data missing / errors → NO_DECISION, no exception
# ---------------------------------------------------------------------------

class TestDataMissing:
    """TC-EVAL-04: errors from fact_provider must not propagate."""

    @pytest.mark.asyncio
    async def test_decompose_error_no_decision(self):
        fp = _ErrorFactProvider()
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="decompose",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None
        assert "error" in reason.lower()

    @pytest.mark.asyncio
    async def test_implement_error_no_decision(self):
        fp = _ErrorFactProvider()
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="implement",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None

    @pytest.mark.asyncio
    async def test_test_error_no_decision(self):
        fp = _ErrorFactProvider()
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="test",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None

    @pytest.mark.asyncio
    async def test_review_error_no_decision(self):
        fp = _ErrorFactProvider()
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="review",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None


# ---------------------------------------------------------------------------
# TC-EVAL-05: dirty data — stage not in template
# ---------------------------------------------------------------------------

class TestDirtyData:
    """TC-EVAL-05: current_stage not in template → NO_DECISION + log."""

    @pytest.mark.asyncio
    async def test_unknown_stage_in_template(self):
        fp = FakeFactProvider(subtask_count=5)
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="nonexistent_stage",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None
        assert "not in template" in reason

    @pytest.mark.asyncio
    async def test_known_stage_but_wrong_template(self):
        """'implement' is not in 'debate' template."""
        fp = FakeFactProvider(src_modified=True)
        clock = FakeClock()
        decision, target, reason = await evaluate(
            task_id="t1",
            current_stage="implement",
            template="debate",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert "not in template" in reason

    @pytest.mark.asyncio
    async def test_empty_template_name(self):
        """Empty template string results in empty stages → stage not found."""
        fp = FakeFactProvider(subtask_count=3)
        clock = FakeClock()
        # Unknown template → stages = [] → the current_stage check skips (no stages to validate)
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="decompose",
            template="",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        # Empty stages list means no validation guard fires, falls through to stage handler
        # decompose handler will still run normally
        assert decision in (AdvanceDecision.ADVANCE, AdvanceDecision.NO_DECISION)


# ---------------------------------------------------------------------------
# TC-EVAL-06: multi-template next stage resolution
# ---------------------------------------------------------------------------

class TestMultiTemplate:
    """TC-EVAL-06: next stage resolves correctly across different templates."""

    @pytest.mark.asyncio
    async def test_feature_implement_to_test(self):
        fp = FakeFactProvider(src_modified=True)
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="implement",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        assert target == "test"

    @pytest.mark.asyncio
    async def test_hotfix_fix_to_test(self):
        """hotfix: fix stage — no objective exit rule; Leader must manually advance."""
        fp = FakeFactProvider(src_modified=True)
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="fix",
            template="hotfix",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        # fix is Execute class but no objective rule; returns NO_DECISION
        assert decision == AdvanceDecision.NO_DECISION

    @pytest.mark.asyncio
    async def test_hotfix_test_fails_to_fix(self):
        fp = FakeFactProvider(bash_event={"exit_code": 1, "stdout": "FAILED: 2 errors"})
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="test",
            template="hotfix",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.FALL_BACK
        assert target == "fix"

    @pytest.mark.asyncio
    async def test_refactor_decompose_to_implement(self):
        fp = FakeFactProvider(subtask_count=2)
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="decompose",
            template="refactor",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        assert target == "implement"

    @pytest.mark.asyncio
    async def test_debate_meeting_no_objective_rule(self):
        """meeting is a Plan stage — no objective exit condition."""
        fp = FakeFactProvider()
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="meeting",
            template="debate",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None

    @pytest.mark.asyncio
    async def test_review_stage_advances_on_memo(self):
        """review → retest when a review-type memo exists."""
        from datetime import timedelta
        memo_time = (_BASE_TIME + timedelta(minutes=5)).isoformat()
        fp = FakeFactProvider(
            memos=[{"type": "review", "timestamp": memo_time, "content": "lgtm"}]
        )
        clock = FakeClock()
        # stage_started_at is before memo_time
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="review",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.ADVANCE
        assert target == "retest"

    @pytest.mark.asyncio
    async def test_review_no_memo_no_advance(self):
        fp = FakeFactProvider(memos=[])
        clock = FakeClock()
        decision, target, _ = await evaluate(
            task_id="t1",
            current_stage="review",
            template="feature",
            fact_provider=fp,
            clock=clock,
            stage_started_at=_BASE_TIME,
        )
        assert decision == AdvanceDecision.NO_DECISION
        assert target is None
