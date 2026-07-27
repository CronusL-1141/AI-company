"""批 3 ①②：空参团队解析「拿错对象」+ context_resolve 复数队/死 loop 块。

复现的旧错误行为（红 → 绿）：
- ``_resolve_team_id`` 空参时静默取 ``active_teams[0]``。活跃队里常年并排着
  workflow-* per-run 队（每跑一次 Workflow 建一个，且是最新的），于是会议 /
  团队知识 / 活动查询等一切"自动用活跃队"的工具全绑到 workflow 队上。
- ``team_close`` / ``team_delete`` 也走同一条空参解析 —— 删队这种不可逆动作
  绝不能靠猜。
- ``context_resolve`` 只回单数 ``team``（同项目并排多队时另外几支不可见），
  且 ``loop`` 块实测恒为 phase=idle/cycle=0 的死占位（循环编排已退役）。
"""

from __future__ import annotations

from unittest.mock import patch

import aiteam.mcp._base as base
import aiteam.mcp.tools.infra as infra
import aiteam.mcp.tools.team as team_tools

SID = "4d04341d-1398-4c4b-bd71-e71ade52b82b"
SID8 = SID[:8]
PROJ = "3aa58dc7-a771-4745-8fa6-efbd5819956a"
OTHER_PROJ = "99999999-9999-9999-9999-999999999999"


class _ToolCapture:
    """最简 mock：捕获 @mcp.tool() 注册的函数。"""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _team(
    tid: str,
    name: str,
    *,
    project_id: str | None = PROJ,
    status: str = "active",
    config: dict | None = None,
    created_at: str = "2026-07-27T10:00:00",
) -> dict:
    return {
        "id": tid,
        "name": name,
        "project_id": project_id,
        "status": status,
        "config": config or {},
        "created_at": created_at,
        "updated_at": created_at,
    }


def _teams_response(*rows: dict) -> dict:
    return {"success": True, "data": list(rows), "total": len(rows)}


# ---------------------------------------------------------------------------
# ① _resolve_team_id 三档优先级
# ---------------------------------------------------------------------------


class TestResolveTeamIdPriority:
    def test_session_container_beats_newest_workflow_team(self, monkeypatch):
        """档①：本会话 session-<sid8> 容器队优先于更新的 workflow-* per-run 队。

        旧实现返回 active_teams[0]（列表按 created_at 倒序 → 最新的 workflow 队），
        这正是"会议误绑 workflow 队"的成因。
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-1", "workflow-wf_abc123", created_at="2026-07-27T12:00:00"),
            _team("sess-1", f"session-{SID8}", created_at="2026-07-27T09:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "sess-1"

    def test_owner_session_id_is_authoritative(self, monkeypatch):
        """档①：队名任意，但 config.owner_session_id 命中本会话即为容器队。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-1", "workflow-wf_abc123", created_at="2026-07-27T12:00:00"),
            _team(
                "own-1",
                "ai-team-os-dev",
                config={"kind": "session", "owner_session_id": SID},
                created_at="2026-07-27T08:00:00",
            ),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "own-1"

    def test_other_session_container_not_picked(self, monkeypatch):
        """别的会话的 session-* 容器队既不算档①，档②也要让位给普通队（防串台）。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("sess-other", "session-a78b767c", created_at="2026-07-27T12:00:00"),
            _team("dev-1", "ai-team-os-dev", created_at="2026-07-27T11:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "dev-1"

    def test_non_workflow_project_team_beats_workflow(self, monkeypatch):
        """档②：无本会话容器队时，本项目内非 workflow-* 活跃队优先。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-1", "workflow-wf_zzz", created_at="2026-07-27T12:00:00"),
            _team("wf-2", "workflow-wf_yyy", created_at="2026-07-27T11:30:00"),
            _team("dev-1", "ai-team-os-dev", created_at="2026-07-26T10:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "dev-1"

    def test_workflow_kind_config_also_excluded(self, monkeypatch):
        """workflow 队判据同时认 config.kind=workflow（队名被改也拦得住）。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team(
                "wf-1",
                "某次运行",
                config={"kind": "workflow"},
                created_at="2026-07-27T12:00:00",
            ),
            _team("dev-1", "ai-team-os-dev", created_at="2026-07-26T10:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "dev-1"

    def test_newest_active_as_last_resort(self, monkeypatch):
        """档③：本项目内只剩 workflow 队时才退回"最新活跃队"。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-old", "workflow-wf_old", created_at="2026-07-20T10:00:00"),
            _team("wf-new", "workflow-wf_new", created_at="2026-07-27T12:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "wf-new"

    def test_cross_project_team_never_borrowed(self, monkeypatch):
        """跨项目队绝不冒充本项目上下文——全落空时返回空串让调用方显式报错。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("other-1", "别项目的队", project_id=OTHER_PROJ),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == ""

    def test_no_active_team_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(_team("done-1", "旧队", status="completed"))
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == ""

    def test_explicit_team_id_short_circuits(self, monkeypatch):
        """显式传参不查 API（省一次 HTTP，也不可能拿错）。"""
        with patch.object(base, "_api_call") as call:
            assert base._resolve_team_id("explicit-id") == "explicit-id"
            call.assert_not_called()

    def test_api_failure_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        with patch.object(base, "_api_call", return_value={"success": False, "error": "boom"}):
            assert base._resolve_team_id("") == ""


# ---------------------------------------------------------------------------
# ① 危险动作禁用空参解析
# ---------------------------------------------------------------------------


class TestDestructiveToolsRequireExplicitTeam:
    def _tools(self) -> dict:
        cap = _ToolCapture()
        team_tools.register(cap)
        return cap.tools

    def test_team_delete_refuses_empty_team_id(self):
        tools = self._tools()
        with patch.object(team_tools, "_api_call") as call:
            result = tools["team_delete"]("")
        assert result["success"] is False
        call.assert_not_called()

    def test_team_close_refuses_empty_team_id(self):
        tools = self._tools()
        with patch.object(team_tools, "_api_call") as call:
            result = tools["team_close"]("")
        assert result["success"] is False
        call.assert_not_called()

    def test_destructive_tools_do_not_auto_resolve(self):
        """连解析函数都不许引入——空参解析在这两个工具上彻底禁用。"""
        import inspect

        src = inspect.getsource(team_tools)
        close_src = src.split("def team_close")[1].split("@mcp.tool()")[0]
        delete_src = src.split("def team_delete")[1].split("@mcp.tool()")[0]
        assert "_resolve_team_id(" not in close_src
        assert "_resolve_team_id(" not in delete_src

    def test_team_delete_with_explicit_id_still_works(self):
        tools = self._tools()
        with patch.object(team_tools, "_api_call", return_value={"success": True}) as call:
            result = tools["team_delete"]("team-42")
        assert result["success"] is True
        call.assert_called_once_with("DELETE", "/api/teams/team-42")


# ---------------------------------------------------------------------------
# ② context_resolve：复数 teams[] + 删死 loop 块
# ---------------------------------------------------------------------------


class TestContextResolve:
    def _call(self, monkeypatch, teams: list[dict], cwd: str = "/repo/demo"):
        cap = _ToolCapture()
        infra.register(cap)
        monkeypatch.setattr(infra.os, "getcwd", lambda: cwd)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)

        projects = {
            "success": True,
            "data": [{"id": PROJ, "name": "demo", "root_path": "/repo/demo"}],
        }

        def fake_api(method: str, path: str, *args, **kwargs):
            if path.startswith("/api/projects"):
                return projects
            if path == "/api/teams":
                return {"success": True, "data": teams}
            if path.endswith("/agents"):
                return {"success": True, "data": []}
            if "loop/status" in path:
                raise AssertionError("loop 状态块已退役，不应再被调用")
            return {"success": True, "data": []}

        with patch.object(infra, "_api_call", side_effect=fake_api):
            return cap.tools["context_resolve"]()

    def test_returns_all_active_project_teams(self, monkeypatch):
        result = self._call(
            monkeypatch,
            [
                _team("wf-1", "workflow-wf_abc", created_at="2026-07-27T12:00:00"),
                _team("sess-1", f"session-{SID8}", created_at="2026-07-27T09:00:00"),
                _team("other-1", "别项目的队", project_id=OTHER_PROJ),
                _team("done-1", "旧队", status="completed"),
            ],
        )
        names = [t["name"] for t in result["teams"]]
        assert set(names) == {"workflow-wf_abc", f"session-{SID8}"}
        assert "别项目的队" not in names
        assert "旧队" not in names

    def test_singular_team_is_the_session_container(self, monkeypatch):
        """单数 team 保留兼容，但取的是三档优先级选出的主队（不再是列表首个）。"""
        result = self._call(
            monkeypatch,
            [
                _team("wf-1", "workflow-wf_abc", created_at="2026-07-27T12:00:00"),
                _team("sess-1", f"session-{SID8}", created_at="2026-07-27T09:00:00"),
            ],
        )
        assert result["team"]["id"] == "sess-1"

    def test_loop_block_removed(self, monkeypatch):
        """loop 块（phase=idle/cycle=0 死占位）已删——键不存在，也不再打那个端点。"""
        result = self._call(
            monkeypatch,
            [_team("sess-1", f"session-{SID8}")],
        )
        assert "loop" not in result

    def test_no_project_means_no_team(self, monkeypatch):
        """cwd 匹配不到项目时不借别项目的队（归属铁律）。"""
        cap = _ToolCapture()
        infra.register(cap)
        monkeypatch.setattr(infra.os, "getcwd", lambda: "/somewhere/else")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)

        def fake_api(method: str, path: str, *args, **kwargs):
            if path.startswith("/api/projects"):
                return {
                    "success": True,
                    "data": [{"id": PROJ, "name": "demo", "root_path": "/repo/demo"}],
                }
            if path == "/api/teams":
                return {"success": True, "data": [_team("sess-1", f"session-{SID8}")]}
            return {"success": True, "data": []}

        with patch.object(infra, "_api_call", side_effect=fake_api):
            result = cap.tools["context_resolve"]()
        assert result["project"] is None
        assert result["team"] is None
        assert result["teams"] == []
