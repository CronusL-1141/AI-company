"""账本数据质量止血 — 观测断链闭合批 ④。

三处实锤（2026-07-28 生产账本取证）：

1. **测试桩漏进真账本**：``tests/unit/test_context_scripts.py`` 会把真 hook
   ``pre_compact_save.py`` 当子进程跑，入参 ``session_id="test-session"``。该 hook
   拿不到端口文件时**回落 localhost:8000**，也就是生产实例——于是每次跑测试都往
   真账本写一条 ``session.compact_checkpoint``。生产库里 18 条检查点有 14 条是这
   么来的（全部 ``trigger=manual`` + ``cwd=""``，与测试入参逐字段吻合），真实数据
   只剩 4 条。护栏做在**写入单一入口**：保留前缀（test-/demo-）的会话身份一律不
   落库——测试再怎么打，账本不脏。

2. **compact_summary 被尺寸闸剥掉**：``send_event.py`` 在整体载荷 >32KB 时只保留
   ``ESSENTIAL_FIELDS``，而 ``compact_summary`` 与 ``trigger`` 都不在其中。压缩摘要
   动辄几万字，于是大摘要场景下服务端收到的载荷里两者双双消失，落库成
   ``summary_chars=0`` —— 恰好就是生产那条 ``{session_id, trigger:"",
   summary_chars:0}`` 的形状。正文本来就**刻意不存**，那就别把正文运过来再算：
   在 hook 侧当场算好长度、扔掉正文，长度作为必留字段过闸。

3. **decision.user_asked 字段错位**：抽取逻辑读的是单数 ``tool_input["question"]``，
   而 AskUserQuestion 的真实载荷是**复数 ``questions`` 数组**（每项含
   question/header/options）。结果生产 6 条裁决记录 question/options/agent_name
   全空，真内容全塞在 answer JSON 里——人审现场等于没记。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


class _RecordingBus:
    """与生产同样严格：不认识的事件类型当场炸。"""

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


# ---------------------------------------------------------------------------
# 1. 写入单一入口的保留 id 护栏
# ---------------------------------------------------------------------------


class TestReservedIdGuardrail:
    """保留身份不得进账本——护栏做在 create_event，绕不过去。"""

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "reserved",
        ["test-session", "test-session-001", "demo-session", "TEST-Session", "demo-1"],
    )
    async def test_reserved_session_id_is_refused(self, repo, reserved):
        before = len(await repo.list_events(limit=200))
        await repo.create_event(
            event_type="session.compact_checkpoint",
            source=f"session:{reserved}",
            data={"session_id": reserved, "trigger": "manual"},
        )
        after = await repo.list_events(limit=200)
        assert len(after) == before, f"保留身份 {reserved} 仍被写进账本"

    @pytest.mark.asyncio()
    async def test_real_session_id_still_writes(self, repo):
        """护栏不能误伤真数据。"""
        await repo.create_event(
            event_type="session.compact_checkpoint",
            source=f"session:{SESSION}",
            data={"session_id": SESSION, "trigger": "auto"},
        )
        hits = await repo.list_events(event_type="session.compact_checkpoint", limit=50)
        assert len(hits) == 1

    @pytest.mark.asyncio()
    async def test_guardrail_matches_on_entity_id_too(self, repo):
        """身份也可能落在 entity_id 上，同样拦。"""
        before = len(await repo.list_events(limit=200))
        await repo.create_event(
            event_type="task.created",
            source="repository",
            data={"task_id": "demo-task-1"},
            entity_id="demo-task-1",
            entity_type="task",
        )
        assert len(await repo.list_events(limit=200)) == before

    @pytest.mark.asyncio()
    async def test_substring_test_is_not_matched(self, repo):
        """只认前缀，不误伤正文里含 test 的正常 id。"""
        await repo.create_event(
            event_type="task.created",
            source="repository",
            data={"task_id": "latest-run-7"},
            entity_id="latest-run-7",
            entity_type="task",
        )
        assert len(await repo.list_events(event_type="task.created", limit=50)) == 1


# ---------------------------------------------------------------------------
# 2. compact_summary 长度必须活着过尺寸闸
# ---------------------------------------------------------------------------


class TestCompactSummaryChars:
    @pytest.mark.asyncio()
    async def test_precomputed_chars_wins(self, translator):
        """hook 侧算好的长度优先——正文已在 hook 侧扔掉，服务端 len() 无从算起。"""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": SESSION,
                "trigger": "auto",
                "compact_summary_chars": 48213,
            }
        )
        (event,) = bus.of("session.compact_completed")
        assert event["summary_chars"] == 48213, "hook 侧算好的长度被丢弃"
        assert event["trigger"] == "auto"

    @pytest.mark.asyncio()
    async def test_falls_back_to_body_length(self, translator):
        """老形态（正文还在）仍要算得对。"""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": SESSION,
                "trigger": "manual",
                "compact_summary": "摘" * 4321,
            }
        )
        (event,) = bus.of("session.compact_completed")
        assert event["summary_chars"] == 4321

    def test_hook_precomputes_and_drops_body(self):
        """hook 侧：正文换成长度，且长度是必留字段（否则尺寸闸一剥就归零）。"""
        from aiteam.hooks import send_event

        payload = send_event._trim_payload(
            {
                "hook_event_name": "PostCompact",
                "session_id": SESSION,
                "trigger": "auto",
                "compact_summary": "摘" * 50_000,
            }
        )
        assert payload.get("compact_summary_chars") == 50_000
        assert "compact_summary" not in payload, "压缩摘要正文不该被运过网络"
        assert "compact_summary_chars" in send_event.ESSENTIAL_FIELDS
        assert "trigger" in send_event.ESSENTIAL_FIELDS


# ---------------------------------------------------------------------------
# 3. decision.user_asked 抽取归位
# ---------------------------------------------------------------------------


class TestUserAskedExtraction:
    @pytest.mark.asyncio()
    async def test_plural_questions_are_extracted(self, translator):
        """真实载荷是 questions 数组——这是 6 条空记录的根因。"""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "question": "两笔账本修改授权哪些?",
                            "header": "账本修数",
                            "options": [
                                {"label": "都做", "description": "留档后删"},
                                {"label": "都不动", "description": "只加护栏"},
                            ],
                        }
                    ]
                },
                "tool_response": {"answers": {"两笔账本修改授权哪些?": "都做"}},
            }
        )
        (event,) = bus.of("decision.user_asked")
        assert event["question"] == "两笔账本修改授权哪些?", f"问题正文仍是空: {event}"
        assert [o["label"] for o in event["options"]] == ["都做", "都不动"]
        assert event["headers"] == ["账本修数"]

    @pytest.mark.asyncio()
    async def test_multi_question_batch_all_recorded(self, translator):
        """一次问多题时不能只留第一题。"""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {"question": "甲?", "header": "A", "options": [{"label": "1"}]},
                        {"question": "乙?", "header": "B", "options": [{"label": "2"}]},
                    ]
                },
                "tool_response": {},
            }
        )
        (event,) = bus.of("decision.user_asked")
        assert event["question"] == "甲?\n乙?"
        assert event["question_count"] == 2
        assert event["headers"] == ["A", "B"]

    @pytest.mark.asyncio()
    async def test_singular_shape_still_supported(self, translator):
        """老的单数形态保持兼容。"""
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
