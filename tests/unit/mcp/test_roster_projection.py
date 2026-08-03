"""名册类工具的精简投影回归（agent_list / team_status / team_briefing / team_list）。

2026-08-03 事故：这两个最常用的观测工具在 50 人团队上**事实失效**——
``agent_list`` 实测返回 67,766 字符、``team_status`` 69,660 字符，双双超 MCP
单次结果上限被直接拒绝；同批实测里 173 人的 workflow 队 ``team_status`` 到
170,331 字符，``team_list`` 316 支队到 148,173 字符。量化根因：offline 行占
payload 96.4%，``system_prompt`` 单字段占 24.7%。

安全阈值 ``SAFE_CHARS`` 的依据（口径是 token，不是字符，所以留足余量）：
- CC 2.1.219 的 MCP 结果上限 = ``MAX_MCP_OUTPUT_TOKENS``，默认常量 25,000
  tokens（可被同名环境变量覆盖）；
- 同一套 OS 载荷实测标定：43,916 字符**通过**、67,766 字符**被拒**，即真实
  拒收门槛落在两者之间；
- 断言取 20,000 字符 —— 比"已实测通过"的 43,916 还低一半以上。夹具刻意造得
  比生产更狠（51 名成员 + 每人 900 字 system_prompt），断言仍须成立。

夹具经 ``Agent`` / ``Team`` / ``Task`` 真模型 ``model_dump(mode="json")`` 生成，
替身不得比生产宽松：字段集与真实 API 返回逐一同源。样本内容全部中性自拟。
"""

from __future__ import annotations

import json

import pytest

from aiteam.clock import utc_now
from aiteam.mcp.tools import agent as agent_tools
from aiteam.mcp.tools import team as team_tools
from aiteam.mcp.tools.views import (
    AGENT_LIST_HINT,
    TEAM_BRIEFING_HINT,
    TEAM_LIST_HINT,
    TEAM_STATUS_HINT,
    compact_agent_row,
    minimal_agent_row,
    page,
    project_roster,
)
from aiteam.types import Agent, AgentStatus, Task, Team

# 见模块文档串：实测 43,916 字符可过、67,766 字符被拒，取 20,000 作安全线。
SAFE_CHARS = 20_000

TEAM_ID = "11111111-2222-3333-4444-555555555555"
LIVE_COUNT = 3
OFFLINE_COUNT = 48
# 大字段的现实体量：实测生产行里 system_prompt 单独占 payload 的 24.7%。
BIG_PROMPT = "示例角色提示词，用于压测返回体体积。" * 50


def _agent(index: int, status: AgentStatus) -> dict:
    """一行成员——走真模型序列化，字段集与 /api/teams/{id}/agents 同源。"""
    row = Agent(
        id=f"{index:08d}-0000-4000-8000-{index:012d}",
        team_id=TEAM_ID,
        name=f"成员-{index:02d}",
        role=f"角色-{index % 7}",
        system_prompt=BIG_PROMPT,
        status=status,
        current_task="正在推进一个示例子任务，描述刻意写长以验证摘要截断" * 4,
        session_id=f"sess-{index:04d}",
        transcript_path=f"/tmp/示例路径/transcript-{index}.jsonl",
        ctx_tokens=123_456,
        ctx_window=1_000_000,
        ctx_pct=12.3,
        input_tokens=1000,
        output_tokens=2000,
        last_active_at=utc_now(),
    )
    return row.model_dump(mode="json")


@pytest.fixture
def roster() -> list[dict]:
    """51 名成员：3 名在岗 + 48 名 offline（复刻事故当天的真实构成比例）。"""
    rows = [_agent(i, AgentStatus.BUSY) for i in range(LIVE_COUNT)]
    rows += [_agent(LIVE_COUNT + i, AgentStatus.OFFLINE) for i in range(OFFLINE_COUNT)]
    return rows


@pytest.fixture
def team_row() -> dict:
    return Team(id=TEAM_ID, name="示例团队", project_id="proj-1").model_dump(mode="json")


@pytest.fixture
def task_rows() -> list[dict]:
    return [
        Task(
            title=f"示例任务 {i}",
            description="任务描述刻意写长，用来验证摘要截断而非删除。" * 10,
            team_id=TEAM_ID,
        ).model_dump(mode="json")
        for i in range(40)
    ]


class _Collector:
    """替代 FastMCP 注册器，保留原函数以便直接调用（兼容 @mcp.tool(meta=...)）。"""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *_args, **_kwargs):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn

        return wrap


def _tools(module) -> dict:
    collector = _Collector()
    module.register(collector)
    return collector.tools


def _size(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str))


def _stub(monkeypatch, module, responses: dict[str, object]) -> list[str]:
    """把 _api_call 换成按路径前缀查表的替身；返回被调用路径的记录。"""
    calls: list[str] = []

    def fake(_method: str, path: str, *_a, **_k):
        calls.append(path)
        for prefix, payload in responses.items():
            if prefix in path:
                return payload
        raise AssertionError(f"未预期的 API 路径: {path}")

    monkeypatch.setattr(module, "_api_call", fake)
    return calls


# ------------------------------------------------------------------
# 行级 / 名册级投影
# ------------------------------------------------------------------


class TestRosterRows:
    def test_compact_row_keeps_call_keys_and_drops_big_fields(self, roster):
        row = compact_agent_row(roster[0])
        # 后续调用要用的键完整保留：id 改状态、name 给 SendMessage 寻址
        assert row["id"] == roster[0]["id"]
        assert row["name"] == roster[0]["name"]
        for dropped in (
            "system_prompt",
            "config",
            "ctx_tokens",
            "ctx_window",
            "ctx_pct",
            "transcript_path",
            "input_tokens",
            "output_tokens",
            "trust_score",
        ):
            assert dropped not in row
        # 语义内容降级为截断摘要，不是删除
        assert row["current_task"].endswith("…")
        assert len(row["current_task"]) <= 81

    def test_minimal_offline_row_has_no_stale_current_task(self, roster):
        row = minimal_agent_row(roster[-1])
        assert set(row) == {"id", "name", "role", "last_active_at"}

    def test_project_roster_splits_and_counts(self, roster):
        out = project_roster(roster)
        assert out["counted"] == LIVE_COUNT + OFFLINE_COUNT
        assert out["status_counts"] == {"busy": LIVE_COUNT, "offline": OFFLINE_COUNT}
        assert len(out["members"]) == LIVE_COUNT
        assert out["offline"]["count"] == OFFLINE_COUNT
        assert len(out["offline"]["recent"]) == 5

    def test_include_offline_returns_whole_roster(self, roster):
        out = project_roster(roster, include_offline=True)
        assert len(out["members"]) == LIVE_COUNT + OFFLINE_COUNT
        # 折叠块消失（没折就没有摘要块），但一行都没少
        assert "offline" not in out

    def test_view_all_keeps_full_rows(self, roster):
        out = project_roster(roster, view="all")
        assert out["members"][0]["system_prompt"] == BIG_PROMPT

    def test_offline_digest_is_most_recent_first(self):
        rows = [
            {"id": "a", "name": "旧", "status": "offline", "last_active_at": "2026-01-01T00:00:00Z"},
            {"id": "b", "name": "新", "status": "offline", "last_active_at": "2026-08-01T00:00:00Z"},
            {"id": "c", "name": "无时间", "status": "offline", "last_active_at": None},
        ]
        recent = project_roster(rows, offline_preview=2)["offline"]["recent"]
        assert [r["name"] for r in recent] == ["新", "旧"]

    def test_page_clamps_and_reports_more(self):
        rows = list(range(300))
        shown, size, has_more = page(rows, limit=1000, offset=0)
        assert size == 200 and len(shown) == 200 and has_more is True
        shown, size, has_more = page(rows, limit=10, offset=295)
        assert shown == [295, 296, 297, 298, 299] and has_more is False


# ------------------------------------------------------------------
# agent_list
# ------------------------------------------------------------------


class TestAgentList:
    def test_compact_fits_under_the_ceiling(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        out = tools["agent_list"](TEAM_ID)
        assert _size(out) < SAFE_CHARS
        # 未投影的原始名册本身就在上限之外——回归的对照组
        assert _size(roster) > SAFE_CHARS

    def test_compact_drops_system_prompt_everywhere(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        out = tools["agent_list"](TEAM_ID)
        # hint 里会把 system_prompt 当字段名提一嘴，所以查正文而非整串
        assert BIG_PROMPT not in json.dumps(out, ensure_ascii=False)
        for row in out["agents"] + out["offline"]["recent"]:
            assert "system_prompt" not in row

    def test_self_identifies_and_reports_offline(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        out = tools["agent_list"](TEAM_ID)
        assert out["view"] == "compact"
        assert out["hint"] == AGENT_LIST_HINT
        assert out["total"] == len(roster)
        assert out["status_counts"]["offline"] == OFFLINE_COUNT
        assert out["offline"]["count"] == OFFLINE_COUNT
        assert len(out["agents"]) == LIVE_COUNT

    def test_call_keys_survive_intact(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        out = tools["agent_list"](TEAM_ID)
        live_ids = {a["id"] for a in roster if a["status"] != "offline"}
        assert {row["id"] for row in out["agents"]} == live_ids
        digest_ids = {row["id"] for row in out["offline"]["recent"]}
        assert digest_ids <= {a["id"] for a in roster}

    def test_escape_hatch_include_offline(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        out = tools["agent_list"](TEAM_ID, include_offline=True, limit=200)
        assert len(out["agents"]) == len(roster)
        assert out["has_more"] is False
        # 折叠只是显示策略，历史成员始终可达
        assert {row["id"] for row in out["agents"]} == {a["id"] for a in roster}

    def test_escape_hatch_fields_all(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        out = tools["agent_list"](TEAM_ID, fields="all")
        assert out["view"] == "all"
        assert out["agents"][0]["system_prompt"] == BIG_PROMPT

    def test_paging_reports_more(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        out = tools["agent_list"](TEAM_ID, include_offline=True, limit=10, offset=0)
        assert len(out["agents"]) == 10 and out["has_more"] is True

    def test_bad_fields_rejected(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": len(roster)}})
        assert tools["agent_list"](TEAM_ID, fields="tiny")["success"] is False

    def test_error_response_passes_through(self, monkeypatch):
        tools = _tools(agent_tools)
        err = {"success": False, "error": "HTTP 404: Not Found"}
        _stub(monkeypatch, agent_tools, {"/agents": err})
        assert tools["agent_list"](TEAM_ID) == err

    def test_partial_counts_are_declared(self, monkeypatch, roster):
        tools = _tools(agent_tools)
        # 上游 200 行帽子截断时，计数不得装成全量
        _stub(monkeypatch, agent_tools, {"/agents": {"success": True, "data": roster, "total": 999}})
        assert "counts_partial" in tools["agent_list"](TEAM_ID)


# ------------------------------------------------------------------
# team_status / team_briefing / team_list
# ------------------------------------------------------------------


class TestTeamStatus:
    def _payload(self, team_row, roster, task_rows):
        return {
            "success": True,
            "data": {
                "team": team_row,
                "agents": roster,
                "active_tasks": task_rows,
                "completed_tasks": 7,
                "total_tasks": 47,
            },
            "message": "",
        }

    def test_compact_fits_under_the_ceiling(self, monkeypatch, team_row, roster, task_rows):
        tools = _tools(team_tools)
        _stub(monkeypatch, team_tools, {"/status": self._payload(team_row, roster, task_rows)})
        out = tools["team_status"](TEAM_ID)
        assert _size(out) < SAFE_CHARS
        assert _size(self._payload(team_row, roster, task_rows)) > SAFE_CHARS

    def test_projection_keeps_counts_and_escape_hatches(self, monkeypatch, team_row, roster, task_rows):
        tools = _tools(team_tools)
        _stub(monkeypatch, team_tools, {"/status": self._payload(team_row, roster, task_rows)})
        out = tools["team_status"](TEAM_ID)
        assert out["view"] == "compact"
        assert out["hint"] == TEAM_STATUS_HINT
        assert out["member_total"] == len(roster)
        assert out["offline"]["count"] == OFFLINE_COUNT
        assert out["completed_tasks"] == 7 and out["total_tasks"] == 47
        assert out["team"]["id"] == TEAM_ID
        assert "system_prompt" not in json.dumps(out, ensure_ascii=False)

    def test_active_tasks_capped_and_declared(self, monkeypatch, team_row, roster, task_rows):
        tools = _tools(team_tools)
        _stub(monkeypatch, team_tools, {"/status": self._payload(team_row, roster, task_rows)})
        out = tools["team_status"](TEAM_ID)
        assert out["active_task_total"] == len(task_rows)
        assert len(out["active_tasks"]) < len(task_rows)
        # 截断必须自曝，不能静默丢任务
        assert "active_tasks_omitted" in out

    def test_include_offline_returns_full_roster(self, monkeypatch, team_row, roster, task_rows):
        tools = _tools(team_tools)
        _stub(monkeypatch, team_tools, {"/status": self._payload(team_row, roster, task_rows)})
        out = tools["team_status"](TEAM_ID, include_offline=True, limit=200)
        assert len(out["members"]) == len(roster)

    def test_error_passes_through(self, monkeypatch):
        tools = _tools(team_tools)
        err = {"success": False, "error": "HTTP 404: Not Found"}
        _stub(monkeypatch, team_tools, {"/status": err})
        assert tools["team_status"](TEAM_ID) == err


class TestTeamBriefing:
    def _payload(self, team_row, roster, task_rows):
        return {
            "success": True,
            "data": {
                "team": {"id": TEAM_ID, "name": team_row["name"], "mode": "coordinate"},
                "agents": roster,
                "recent_events": [
                    {
                        "type": "intent.agent_working",
                        "source": "agent:x",
                        "timestamp": "2026-08-03T00:00:00Z",
                        "data": {"intent_summary": "示例意图" * 40},
                    }
                    for _ in range(10)
                ],
                "recent_meeting": None,
                "pending_tasks": task_rows,
                "file_hotspots": [],
                "_hints": "示例提示",
            },
        }

    def test_compact_projects_roster_events_and_tasks(self, monkeypatch, team_row, roster, task_rows):
        tools = _tools(team_tools)
        _stub(monkeypatch, team_tools, {"/briefing": self._payload(team_row, roster, task_rows)})
        out = tools["team_briefing"](TEAM_ID)
        assert _size(out) < SAFE_CHARS
        assert out["view"] == "compact"
        assert out["hint"] == TEAM_BRIEFING_HINT
        assert out["member_total"] == len(roster)
        assert out["offline"]["count"] == OFFLINE_COUNT
        assert out["_hints"] == "示例提示"
        assert out["recent_events"][0]["summary"].endswith("…")
        assert "system_prompt" not in json.dumps(out, ensure_ascii=False)


class TestTeamList:
    def _teams(self, count: int) -> list[dict]:
        rows = []
        for i in range(count):
            team = Team(
                id=f"{i:08d}-aaaa-4000-8000-{i:012d}",
                name=f"workflow-wf_{i:08d}",
                project_id="proj-1",
                config={"kind": "workflow", "wf_id": f"wf_{i:08d}", "note": "示例配置" * 20},
            )
            rows.append(team.model_dump(mode="json"))
        return rows

    def test_compact_fits_under_the_ceiling(self, monkeypatch):
        tools = _tools(team_tools)
        rows = self._teams(316)
        _stub(monkeypatch, team_tools, {"/api/teams": {"success": True, "data": rows, "total": len(rows)}})
        out = tools["team_list"]()
        assert _size(rows) > SAFE_CHARS
        assert _size(out) < SAFE_CHARS
        assert out["view"] == "compact"
        assert out["hint"] == TEAM_LIST_HINT
        assert out["total"] == 316

    def test_status_filter_and_paging(self, monkeypatch):
        tools = _tools(team_tools)
        rows = self._teams(120)
        for row in rows[:100]:
            row["status"] = "completed"
        _stub(monkeypatch, team_tools, {"/api/teams": {"success": True, "data": rows, "total": len(rows)}})
        active = tools["team_list"]()
        assert active["matched"] == 20 and active["status_filter"] == "active"
        every = tools["team_list"](status="")
        assert every["matched"] == 120 and every["has_more"] is True
        assert len(every["teams"]) == 50


class TestActivityProjection:
    def _activities(self, count: int) -> list[dict]:
        return [
            {
                "id": f"act-{i}",
                "agent_id": f"{i:08d}-0000-4000-8000-{i:012d}",
                "session_id": f"sess-{i}",
                "tool_name": "Bash",
                "input_summary": "示例输入摘要" * 10,
                "output_summary": "示例输出，可能是整段命令回显" * 40,
                "timestamp": "2026-08-03T00:00:00Z",
                "duration_ms": 123,
                "status": "completed",
                "error": None,
            }
            for i in range(count)
        ]

    def test_compact_excerpts_and_caps_the_window(self, monkeypatch):
        tools = _tools(agent_tools)
        monkeypatch.setattr(agent_tools, "_resolve_team_id", lambda _t: TEAM_ID)
        rows = self._activities(200)
        seen: list[str] = []

        def fake(_method, path, *_a, **_k):
            seen.append(path)
            return {"success": True, "data": rows[:40], "total": len(rows)}

        monkeypatch.setattr(agent_tools, "_api_call", fake)
        out = tools["agent_activity_query"](team_id=TEAM_ID, limit=200)
        assert _size(out) < SAFE_CHARS
        assert out["limit"] == 40
        assert "limit_capped" in out
        assert "limit=40" in seen[0]
        assert out["activities"][0]["output"].endswith("…")
        # error 恒空时不占位
        assert "error" not in out["activities"][0]


class TestHintsSelfIdentify:
    def test_every_roster_hint_declares_trimmed_not_missing(self):
        for hint in (AGENT_LIST_HINT, TEAM_STATUS_HINT, TEAM_LIST_HINT, TEAM_BRIEFING_HINT):
            assert "非字段缺失" in hint
        # 名册类的逃生舱必须指到"看历史成员"的入口
        for hint in (AGENT_LIST_HINT, TEAM_STATUS_HINT, TEAM_BRIEFING_HINT):
            assert "include_offline=True" in hint
            assert "agent_reuse_recommend" in hint
