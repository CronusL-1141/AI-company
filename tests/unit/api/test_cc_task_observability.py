"""CC 任务观测挂载点补齐 — 观测断链闭合批 ②。

取证结论(2026-07-28,对照 CC 官方 hook 文档 + 本机注册面 + 生产账本):

* CC 与任务/子 agent 相关的 hook 事件**只有 4 个**:SubagentStart / SubagentStop /
  TaskCreated / TaskCompleted。**中止侧没有任何 hook** —— 不存在 TaskStop、
  TaskAborted、SubagentAbort 这类事件,Esc 打断也不触发 hook。``TaskStop`` 是一个
  **工具**而非事件,只能经 PreToolUse/PostToolUse 观测到。
* 桥(cc_task_bridge.py)只挂在 TaskCompleted 上,而该事件**没有并挂 send_event.py**
  —— 于是"桥被触发过几次、滤掉了几条"在事件流里零遥测,桥是不是活的都无从判断。
* TaskCreated 完全未注册。
* 实测"TaskStop 40 > TaskCreate 14"口径是 cc.tool_use 里的**工具调用**观测,不是
  hook 事件;且 TaskCreate/TaskUpdate 自 7/7-7/8 后归零,TaskStop 持续在用——新版
  CC 里后台 subagent 即 task,停 agent 走的就是 TaskStop,中止确实是主路径。

本批据此补三处观测,**不动 Q1 的"完成时点记账"裁定**:上墙逻辑仍只由桥负责,
新增的都是只观测不记账的事件。
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


class TestBridgeTelemetry:
    """桥零遥测:TaskCompleted 到底触发过没有,此前查无实据。"""

    @pytest.mark.asyncio()
    async def test_task_completed_lands_observation_event(self, translator):
        tr, bus = translator
        result = await tr.handle_event(
            {
                "hook_event_name": "TaskCompleted",
                "session_id": SESSION,
                "task_id": "cc-task-77",
                "task_subject": "修桥",
                "teammate_name": "d1-worker",
                "team_name": "workflow-abc",
            }
        )
        (event,) = bus.of("cc.task_completed")
        assert event["cc_task_id"] == "cc-task-77"
        assert event["subject"] == "修桥"
        assert event["owner"] == "d1-worker"
        assert result["status"] != "ignored", "TaskCompleted 仍被当成未知事件丢弃"

    @pytest.mark.asyncio()
    async def test_task_created_is_observed_only(self, translator):
        """TaskCreated 只观测不上墙——Q1 的完成时点记账裁定不动。"""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "TaskCreated",
                "session_id": SESSION,
                "task_id": "cc-task-78",
                "task_subject": "新任务",
            }
        )
        (event,) = bus.of("cc.task_created")
        assert event["cc_task_id"] == "cc-task-78"
        assert event["observed_only"] is True
        # 不得顺手建 OS 任务
        assert bus.of("task.created") == []


class TestAbortObservation:
    """中止侧无 hook 可挂,只能从工具调用面把 TaskStop 拎成一等事件。

    挂在 **PreToolUse** 而非 Post:①实测那 40 条中止信号本来就落在 cc.tool_use
    （Pre）里;②停 agent 这个动作可能把对侧连同回执一起带走,Pre 是唯一保证发得
    出去的时点。
    """

    @pytest.mark.asyncio()
    async def test_task_stop_tool_becomes_first_class_event(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": SESSION,
                "tool_name": "TaskStop",
                "tool_input": {"taskId": "cc-task-79", "reason": "缔造者打断"},
            }
        )
        (event,) = bus.of("cc.task_stopped")
        assert event["cc_task_id"] == "cc-task-79"
        assert event["reason"] == "缔造者打断"

    @pytest.mark.asyncio()
    async def test_not_double_counted_on_post(self, translator):
        """Post 侧不得重复发——中止是一个事实,不是两个。"""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "TaskStop",
                "tool_input": {"taskId": "cc-task-79", "reason": "缔造者打断"},
            }
        )
        assert bus.of("cc.task_stopped") == []

    @pytest.mark.asyncio()
    async def test_other_tools_raise_no_stop_event(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": SESSION,
                "tool_name": "TaskGet",
                "tool_input": {"taskId": "cc-task-80"},
            }
        )
        assert bus.of("cc.task_stopped") == []


class TestRegistrationSurface:
    """四件套税:两条安装链必须同时认得新挂载点。"""

    def test_task_events_registered_on_both_chains(self):
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        manifest = json.loads((root / "plugin" / "hooks" / "hooks.json").read_text())
        hooks = manifest["hooks"]

        # TaskCompleted 必须并挂 send_event(桥 + 遥测两条腿)
        completed = [
            h["command"] for grp in hooks["TaskCompleted"] for h in grp["hooks"]
        ]
        assert any("cc_task_bridge.py" in c for c in completed)
        assert any("send_event.py" in c for c in completed), "桥仍零遥测"

        # TaskCreated 必须存在且只挂观测
        created = [h["command"] for grp in hooks["TaskCreated"] for h in grp["hooks"]]
        assert any("send_event.py" in c for c in created)
        assert not any("cc_task_bridge.py" in c for c in created), "TaskCreated 不该上墙"

    def test_source_install_surface_matches(self):
        """install.py 的 HOOK_SURFACE 是源码安装链的唯一真源(I8 机检两链 1:1)。"""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location("_install", root / "install.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        surface = {evt: scripts for evt, _m, scripts in mod.HOOK_SURFACE}
        assert any(s[0] == "send_event.py" for s in surface["TaskCompleted"])
        assert any(s[0] == "cc_task_bridge.py" for s in surface["TaskCompleted"])
        assert any(s[0] == "send_event.py" for s in surface["TaskCreated"])
