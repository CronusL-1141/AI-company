"""批 9 项(5)：worktree / 后台任务 / Plan mode / Skill 四处盲区接入观测。

四类都属于"CC 有、OS 完全看不见"的新形态。本批只做最小集——事件进得来、
数字看得见——不新建表、不新建页面。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import pytest_asyncio

from aiteam.api import background_jobs
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


class _RecordingBus:
    """与生产同样严格：不认识的事件类型当场炸，stub 不得比生产宽松。"""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event_type, source, data, **_kwargs):
        EventType(event_type)
        self.events.append((event_type, source, data))

    def of(self, event_type: str) -> list[dict]:
        return [d for t, _s, d in self.events if t == event_type]


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    bus = _RecordingBus()
    yield HookTranslator(repo=repo, event_bus=bus), bus


class TestInputSummaries:
    """48K 条工具事件里，这些工具此前只留下一堵 200 字的 JSON 墙。"""

    @pytest.mark.parametrize(
        ("tool", "tool_input", "expected"),
        [
            ("Skill", {"skill": "os-workflow", "args": "x"}, "os-workflow"),
            ("SendMessage", {"to": "main", "summary": "批9 完成", "message": "长正文…"}, "批9 完成"),
            ("TaskCreate", {"subject": "修桥", "description": "d"}, "修桥"),
            ("TaskUpdate", {"subject": "修桥", "status": "completed"}, "修桥"),
            ("TaskStop", {"taskId": "wf_1", "reason": "关机"}, "wf_1"),
            ("AskUserQuestion", {"question": "要不要换判据?"}, "要不要换判据?"),
            ("ExitPlanMode", {"plan": "第一步…"}, "第一步…"),
            ("EnterWorktree", {"name": "wt-a"}, "wt-a"),
            ("WebSearch", {"query": "claude hooks"}, "claude hooks"),
        ],
    )
    def test_the_identifying_field_wins(self, tool, tool_input, expected):
        t = HookTranslator(repo=None, event_bus=None)  # type: ignore[arg-type]
        assert t._extract_input_summary(tool, tool_input) == expected

    def test_unknown_tools_keep_the_old_fallback(self):
        t = HookTranslator(repo=None, event_bus=None)  # type: ignore[arg-type]
        assert t._extract_input_summary("Bash", {"command": "ls"}) == "ls"
        assert t._extract_input_summary("Edit", {"file_path": "/a/b.py"}) == "/a/b.py"

    def test_summaries_are_capped(self):
        t = HookTranslator(repo=None, event_bus=None)  # type: ignore[arg-type]
        assert len(t._extract_input_summary("ExitPlanMode", {"plan": "x" * 9999})) == 200


class TestDecisionMoments:
    @pytest.mark.asyncio
    async def test_plan_text_is_kept_whole_not_truncated(self, translator):
        tr, bus = translator
        plan = "第一步…" * 300
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "ExitPlanMode",
                "tool_input": {"plan": plan},
            }
        )
        (event,) = bus.of("decision.plan_presented")
        assert event["plan"] == plan  # 正文完整，不是 200 字摘要
        assert event["plan_chars"] == len(plan)

    @pytest.mark.asyncio
    async def test_user_question_keeps_options_and_answer(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "AskUserQuestion",
                "tool_input": {"question": "停心跳写入?", "options": ["停", "不停"]},
                "tool_response": "停",
            }
        )
        (event,) = bus.of("decision.user_asked")
        assert event["question"] == "停心跳写入?"
        assert event["options"] == ["停", "不停"]
        assert event["answer"] == "停"

    @pytest.mark.asyncio
    async def test_empty_plan_records_nothing(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "ExitPlanMode",
                "tool_input": {},
            }
        )
        assert bus.of("decision.plan_presented") == []

    @pytest.mark.asyncio
    async def test_ordinary_tools_raise_no_decision_event(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )
        assert bus.of("decision.plan_presented") == []
        assert bus.of("decision.user_asked") == []


class TestWorktreeEvents:
    @pytest.mark.asyncio
    async def test_create_and_remove_record_their_own_fields(self, translator):
        tr, bus = translator
        # CC 的两个载荷字段不对称：create 只给 name，remove 只给 worktree_path。
        await tr.handle_event(
            {"hook_event_name": "WorktreeCreate", "session_id": SESSION, "name": "wt-batch9"}
        )
        await tr.handle_event(
            {
                "hook_event_name": "WorktreeRemove",
                "session_id": SESSION,
                "worktree_path": "/tmp/wt-batch9",
            }
        )
        assert bus.of("cc.worktree_created")[0]["name"] == "wt-batch9"
        assert bus.of("cc.worktree_removed")[0]["worktree_path"] == "/tmp/wt-batch9"


class TestBackgroundJobs:
    def _write(self, tmp_path, monkeypatch, records: list[dict]):
        jobs = tmp_path / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        for r in records:
            d = jobs / r["daemonShort"]
            d.mkdir()
            (d / "state.json").write_text(json.dumps(r), encoding="utf-8")
        monkeypatch.setattr(background_jobs, "_jobs_dir", lambda: jobs)

    def _record(self, short: str, **extra) -> dict:
        base = {
            "daemonShort": short,
            "sessionId": f"{short}-uuid",
            "state": "done",
            "tempo": "idle",
            "backend": "daemon",
            "cwd": "/Users/dev/AI team OS",
            "intent": "",
            "respawnFlags": ["--permission-mode", "auto", "--model", "fable"],
            "createdAt": "2026-07-15T04:09:27.012Z",
            "updatedAt": "2026-07-15T11:41:17.040Z",
            "firstTerminalAt": "2026-07-15T11:41:17.040Z",
        }
        base.update(extra)
        return base

    def test_reads_the_shape_cc_writes(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [self._record("44fbf725")])
        (job,) = background_jobs.read_jobs()
        assert job.job_id == "44fbf725"
        assert job.state == "done"
        assert job.model == "fable"  # 从扁平的 respawnFlags 里挑出来
        assert job.created_at == datetime(2026, 7, 15, 4, 9, 27, 12000)

    def test_in_flight_is_absence_of_a_terminal_stamp(self, tmp_path, monkeypatch):
        """不猜 state 字符串：本机实测有 done / stopped 两种终态，
        而 CC 二进制里没有一处枚举把它们一起列出——白名单必漏。"""
        self._write(
            tmp_path,
            monkeypatch,
            [
                self._record("aaaa"),  # 有 firstTerminalAt = 终态
                self._record("bbbb", state="running", firstTerminalAt=None),
                self._record("cccc", state="某个没见过的状态", firstTerminalAt=None),
            ],
        )
        assert {j.job_id for j in background_jobs.in_flight_jobs()} == {"bbbb", "cccc"}

    def test_cwd_filter_respects_path_boundaries(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            monkeypatch,
            [
                self._record("aaaa", firstTerminalAt=None),
                self._record("bbbb", firstTerminalAt=None, cwd="/Users/dev/AI team OS-backup"),
            ],
        )
        found = background_jobs.in_flight_jobs("/Users/dev/AI team OS")
        assert {j.job_id for j in found} == {"aaaa"}

    def test_corrupt_file_is_skipped(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [self._record("aaaa")])
        bad = tmp_path / "jobs" / "bad"
        bad.mkdir()
        (bad / "state.json").write_text("{oops", encoding="utf-8")
        assert [j.job_id for j in background_jobs.read_jobs()] == ["aaaa"]

    def test_missing_directory_reads_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(background_jobs, "_jobs_dir", lambda: tmp_path / "nope")
        assert background_jobs.read_jobs() == []

    def test_real_machine_parses(self):
        for job in background_jobs.read_jobs():
            assert job.job_id
            assert isinstance(job.in_flight, bool)
