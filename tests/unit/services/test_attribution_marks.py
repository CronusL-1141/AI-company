"""用量归因打标规则 —— 只收证据确凿的行（缔造者 09-30 裁定，报告 c20b37a8）。

夹具取 09-30 生产库的真实形态：
* abff40af 那份 transcript 挂着 12 个 Leader 行：最早一行 session_id 被抹、身上是
  08-03 回采写下的中途快照（output 6,386,926），最新一行是活体采集的真值
  （27,552,558），中间十行从没测过；
* resume 空壳：会话 id 存在、没有 transcript、零工具调用、存活几毫秒；
* 16 行 session_id 为空、也没有路径的 Leader 行：认不出它们是谁的重复，不许排除。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiteam.services.attribution_marks import (
    RESUME_SHELL_GRACE,
    AgentSnapshot,
    identify_exclusions,
    identify_unmeasurable,
    infer_harness,
)
from aiteam.types import AttributionExclusion, HarnessId, UnattributedReason

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
ABFF = "/Users/x/.claude/projects/-Users-x-P/abff40af-58a1-4a7e-84ee-a68f07f72ed3.jsonl"


def _leader(agent_id: str, **kw) -> AgentSnapshot:
    fields = {
        "id": agent_id,
        "role": "leader",
        "source": "hook",
        "status": "offline",
        "harness": None,
        "session_id": None,
        "cc_tool_use_id": None,
        "transcript_path": None,
        "tokens_measured_at": None,
        "created_at": NOW - timedelta(days=2),
        "team_name": "session-00000000",
        "activity_count": 0,
    }
    fields.update(kw)
    return AgentSnapshot(**fields)


def _never(_value: str) -> bool:
    return False


def _marks(rows, exists=_never):
    return {m.agent_id: m for m in identify_exclusions(rows, session_transcript_exists=exists, now=NOW)}


class TestDuplicateTranscript:
    def test_live_row_represents_and_the_backfill_snapshot_is_excluded(self):
        rows = [
            _leader("ghost-measured", transcript_path=ABFF,
                    tokens_measured_at=datetime(2026, 8, 3, 6, 6, 20, tzinfo=UTC),
                    created_at=datetime(2026, 7, 14, tzinfo=UTC)),
            _leader("ghost-1", transcript_path=ABFF, created_at=datetime(2026, 7, 21, tzinfo=UTC)),
            _leader("ghost-2", transcript_path=ABFF, created_at=datetime(2026, 7, 28, tzinfo=UTC)),
            _leader("live", transcript_path=ABFF, session_id="abff40af-58a1-4a7e-84ee-a68f07f72ed3",
                    tokens_measured_at=datetime(2026, 9, 30, 5, 0, tzinfo=UTC),
                    created_at=datetime(2026, 7, 29, tzinfo=UTC), activity_count=21341),
        ]
        marks = _marks(rows)
        assert set(marks) == {"ghost-measured", "ghost-1", "ghost-2"}, "代表行取错了：最早那行是幽灵行"
        assert {m.of for m in marks.values()} == {"live"}
        assert {m.kind for m in marks.values()} == {AttributionExclusion.DUPLICATE_TRANSCRIPT}

    def test_only_measured_row_represents_when_no_row_has_a_session(self):
        """ee2f4c61 形态：两行都没有 session_id，只有一行测过 —— 测过的那行代表。"""
        path = "/p/ee2f4c61.jsonl"
        rows = [
            _leader("measured", transcript_path=path, tokens_measured_at=NOW - timedelta(days=60)),
            _leader("later", transcript_path=path, created_at=NOW - timedelta(days=1)),
        ]
        assert set(_marks(rows)) == {"later"}

    def test_single_row_transcripts_are_never_touched(self):
        rows = [_leader("a", transcript_path="/p/a.jsonl"), _leader("b", transcript_path="/p/b.jsonl")]
        assert _marks(rows) == {}

    def test_subagent_rows_are_out_of_scope(self):
        rows = [
            _leader("x", role="worker", transcript_path="/p/agent-a.jsonl"),
            _leader("y", role="worker", transcript_path="/p/agent-a.jsonl"),
        ]
        assert _marks(rows) == {}


class TestResumeShell:
    def _shell(self, **kw) -> AgentSnapshot:
        base = {"session_id": "91ca1de1-162f-47c5-a9e3-1510f4f7a667"}
        base.update(kw)
        return _leader("shell", **base)

    def test_real_shape_is_marked(self):
        marks = _marks([self._shell()])
        assert marks["shell"].kind is AttributionExclusion.RESUME_SHELL
        assert marks["shell"].of is None

    def test_any_counter_evidence_keeps_the_row(self):
        """任何一条不满足就不算空壳 —— 宁可留在分母里算未归因。"""
        counter = {
            "made tool calls": {"activity_count": 24},
            "has a transcript path": {"transcript_path": "/p/x.jsonl"},
            "was measured": {"tokens_measured_at": NOW - timedelta(days=1)},
            "still busy": {"status": "busy"},
            "inside the grace period": {"created_at": NOW - RESUME_SHELL_GRACE / 2},
            "is a Codex row": {"harness": HarnessId.CODEX.value},
            "has no session id": {"session_id": None},
        }
        for why, override in counter.items():
            assert _marks([self._shell(**override)]) == {}, f"{why} 却被判成空壳"

    def test_a_session_whose_transcript_exists_is_not_a_shell(self):
        assert _marks([self._shell()], exists=lambda _sid: True) == {}

    def test_rows_without_session_or_path_stay_counted(self):
        """16 行 session_id 为空、没有路径的 Leader 行：认不出是谁的重复，不排除。"""
        rows = [_leader(f"nosid-{i}", team_name="workflow-wf_8cf3dc03-6b6") for i in range(3)]
        assert _marks(rows) == {}


class TestInferHarness:
    def _infer(self, row, cc=_never, codex=_never):
        mark = infer_harness(row, cc_session_transcript_exists=cc, codex_rollout_exists=codex)
        return (mark.harness, mark.evidence) if mark else None

    def test_evidence_tiers(self):
        cases = [
            (_leader("a", transcript_path=ABFF), (HarnessId.CLAUDE_CODE, "cc_transcript_path")),
            (_leader("b", transcript_path="/Users/x/.codex/sessions/2026/09/03/rollout-1.jsonl"),
             (HarnessId.CODEX, "codex_rollout_path")),
            (_leader("c", role="workflow-subagent"), (HarnessId.CLAUDE_CODE, "workflow_subagent")),
            (_leader("d", team_name="workflow-wf_8cf3dc03-6b6"), (HarnessId.CLAUDE_CODE, "workflow_team")),
            (_leader("e", role="worker", session_id="01a0a4b3-1111-7abc-8def-000000000001",
                     cc_tool_use_id="01a0a4b4-2222-7abc-8def-000000000002"),
             (HarnessId.CODEX, "dual_uuid7_ids")),
            (_leader("f", role="worker", cc_tool_use_id="a7bf3e26417e47c19"),
             (HarnessId.CLAUDE_CODE, "cc_agent_id_format")),
            (_leader("g", session_id="91ca1de1-162f-47c5-a9e3-1510f4f7a667"),
             (HarnessId.CLAUDE_CODE, "session_id_uuid4")),
        ]
        for row, expected in cases:
            assert self._infer(row) == expected, row.id

    def test_session_files_are_stronger_than_the_id_shape(self):
        codex_leader = _leader("fdbc7bf3", session_id="01a066a0-cace-7a80-8b69-06d0e1c032ce")
        assert self._infer(codex_leader) is None, "单列 v7 不足以认定 Codex"
        assert self._infer(codex_leader, codex=lambda _sid: True) == (
            HarnessId.CODEX, "codex_rollout_by_session",
        )
        cc_leader = _leader("h", session_id="39c7d5a3-52c6-4735-b866-1fa68229709a")
        assert self._infer(cc_leader, cc=lambda _sid: True) == (
            HarnessId.CLAUDE_CODE, "cc_session_transcript",
        )

    def test_unknown_rows_and_labelled_rows_are_left_alone(self):
        assert self._infer(_leader("api", source="api", role="worker")) is None
        assert self._infer(_leader("done", harness=HarnessId.CODEX.value, transcript_path=ABFF)) is None


class TestUnmeasurable:
    """没有可计用量的结论只下给已经结束的行（审稿 00c439d9 第 1.4 条）。"""

    def _marks(self, rows):
        return {
            m.agent_id: m.reason
            for m in identify_unmeasurable(
                rows,
                reason_of=lambda _path: "no_usage_lines",
                transcript_exists=lambda _path: True,
                now=NOW,
            )
        }

    def test_finished_row_is_marked(self):
        row = _leader("done", role="workflow-subagent", transcript_path="/p/agent-a.jsonl",
                      last_active_at=NOW - timedelta(hours=2))
        assert self._marks([row]) == {"done": UnattributedReason.NO_USAGE_LINES}

    def test_running_rows_get_no_verdict(self):
        """首个回复可能还没落盘：busy 或 30 分钟内有活动的行，此刻不下结论。"""
        rows = [
            _leader("busy", role="workflow-subagent", status="busy", transcript_path="/p/a.jsonl"),
            _leader("recent", role="workflow-subagent", transcript_path="/p/b.jsonl",
                    last_active_at=NOW - timedelta(minutes=5)),
        ]
        assert self._marks(rows) == {}

    def test_measured_codex_and_pathless_rows_are_out_of_scope(self):
        rows = [
            _leader("measured", role="worker", transcript_path="/p/c.jsonl",
                    tokens_measured_at=NOW - timedelta(days=1)),
            _leader("codex", role="worker", transcript_path="/p/d.jsonl", harness=HarnessId.CODEX.value),
            _leader("nopath", role="worker"),
        ]
        assert self._marks(rows) == {}

    def test_codex_rows_take_the_parser_verdict_under_the_same_liveness_rule(self):
        """Codex 行用解析器的结论，不借 CC 的判据、不要求登记路径；在飞与已测的行同样
        不下结论，连解析都不做。"""
        codex = HarnessId.CODEX.value
        parsed = []

        def codex_reason(row):
            parsed.append(row.id)
            return None if row.id == "has-usage" else "no_usage_lines"

        rows = [
            _leader("ended", session_id="t1", harness=codex, last_active_at=NOW - timedelta(hours=2)),
            _leader("child", role="worker", session_id="root", cc_tool_use_id="t2", harness=codex,
                    transcript_path="/c/rollout-t2.jsonl"),
            _leader("has-usage", session_id="t3", harness=codex),
            _leader("running", session_id="t4", harness=codex, status="busy"),
            _leader("recent", session_id="t5", harness=codex, last_active_at=NOW - timedelta(minutes=5)),
            _leader("measured", session_id="t6", harness=codex, tokens_measured_at=NOW - timedelta(days=1)),
        ]
        marks = {
            m.agent_id: m
            for m in identify_unmeasurable(
                rows,
                reason_of=lambda _path: "api_error_only",
                transcript_exists=lambda _path: True,
                now=NOW,
                codex_reason_of=codex_reason,
            )
        }
        assert {k: m.reason for k, m in marks.items()} == {
            "ended": UnattributedReason.NO_USAGE_LINES,
            "child": UnattributedReason.NO_USAGE_LINES,
        }
        # A child's thread is its own id, never the root session it runs under.
        assert marks["ended"].evidence == "rollout of thread t1 has no usage event"
        assert marks["child"].evidence == "rollout of thread t2 has no usage event"
        assert parsed == ["ended", "child", "has-usage"]
