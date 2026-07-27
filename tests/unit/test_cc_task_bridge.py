"""cc_task_bridge — 完成时点记账，只镜像有主或有依赖链的 CC 任务。

Q1 裁定 C+B（2026-07-27）。改造前的桥有三处问题，本文件逐条钉死：
- 挂在 TaskCreated 上：没完成的任务对项目账目没有意义。
- 全量镜像：CC 的任务列表是会话内清单，无主无依赖的条目上墙就是噪声。
- 项目缓存是**全局单值** + 5 分钟 TTL：本机同时跑着好几个不同项目的 CC 会话，
  谁先解析谁赢，接下来 5 分钟别人的任务全落进他的项目——不是罕见竞态，是必然串台。
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch


def _import_module():
    import importlib
    import sys

    # Re-import fresh each time to avoid state bleed
    mod_name = "aiteam.hooks.cc_task_bridge"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = json.dumps(payload).encode()
    return resp


class TestProjectCacheIsPerCwd:
    def test_two_projects_do_not_share_one_cached_id(self):
        """本机多会话并行的真实场景：两个 cwd 必须各自解析。"""
        mod = _import_module()
        state: dict = {}
        with patch.object(mod, "_load_state", side_effect=lambda: state), patch.object(
            mod, "_save_state", side_effect=lambda s: state.update(s)
        ), patch.object(
            mod.urllib.request,
            "urlopen",
            side_effect=[
                _mock_response({"project_id": "proj-os"}),
                _mock_response({"project_id": "proj-wenge"}),
            ],
        ):
            first = mod._resolve_project_id("/Users/dev/AI team OS")
            second = mod._resolve_project_id("/Volumes/x/Wenge")

        assert first == "proj-os"
        assert second == "proj-wenge"

    def test_same_cwd_reuses_its_own_cache(self):
        mod = _import_module()
        state = {
            "project_id_by_cwd": {"/Users/dev/AI team OS": {"id": "proj-os", "at": time.time()}}
        }
        with patch.object(mod, "_load_state", return_value=state), patch.object(
            mod, "_save_state"
        ) as save, patch.object(mod.urllib.request, "urlopen") as urlopen:
            assert mod._resolve_project_id("/Users/dev/AI team OS") == "proj-os"
        save.assert_not_called()
        urlopen.assert_not_called()

    def test_expired_entry_refetches(self):
        mod = _import_module()
        state = {"project_id_by_cwd": {"/p": {"id": "old", "at": 0}}}
        with patch.object(mod, "_load_state", return_value=state), patch.object(
            mod, "_save_state"
        ), patch.object(
            mod.urllib.request, "urlopen", return_value=_mock_response({"project_id": "new"})
        ):
            assert mod._resolve_project_id("/p") == "new"

    def test_api_unreachable_returns_none(self):
        mod = _import_module()
        with patch.object(mod, "_load_state", return_value={}), patch.object(
            mod.urllib.request, "urlopen", side_effect=OSError("down")
        ):
            assert mod._resolve_project_id("/p") is None


class TestMirrorFilter:
    """有主 or 有依赖链才上墙。"""

    def test_owned_task_is_mirrored(self):
        mod = _import_module()
        assert mod._should_mirror("alice", {}) is True

    def test_task_in_a_dependency_chain_is_mirrored(self):
        mod = _import_module()
        assert mod._should_mirror("", {"blockedBy": ["7"], "blocks": []}) is True
        assert mod._should_mirror("", {"blockedBy": [], "blocks": ["9"]}) is True

    def test_solo_checklist_item_stays_in_cc(self):
        mod = _import_module()
        assert mod._should_mirror("", {"blocks": [], "blockedBy": []}) is False
        assert mod._should_mirror("", {}) is False


class TestReadCcTask:
    def test_reads_the_shape_cc_writes(self, tmp_path, monkeypatch):
        mod = _import_module()
        team_dir = tmp_path / "session-0def8f84"
        team_dir.mkdir()
        (team_dir / "20.json").write_text(
            json.dumps(
                {
                    "id": "20",
                    "subject": "建 fetcher",
                    "description": "d",
                    "activeForm": "a",
                    "status": "completed",
                    "blocks": [],
                    "blockedBy": ["19"],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "_TASKS_DIR", str(tmp_path))
        assert mod._read_cc_task("session-0def8f84", "20")["blockedBy"] == ["19"]

    def test_missing_file_is_no_evidence_not_an_error(self, tmp_path, monkeypatch):
        mod = _import_module()
        monkeypatch.setattr(mod, "_TASKS_DIR", str(tmp_path))
        assert mod._read_cc_task("nope", "1") == {}
        assert mod._read_cc_task("", "") == {}


class TestMain:
    def _run(self, payload: dict, cc_task: dict, monkeypatch):
        mod = _import_module()
        monkeypatch.setattr(mod.sys, "stdin", MagicMock(read=lambda: json.dumps(payload)))
        monkeypatch.setattr(mod, "_read_cc_task", lambda *_a: cc_task)
        monkeypatch.setattr(mod, "_resolve_project_id", lambda _c: "proj-1")
        calls: list = []
        monkeypatch.setattr(mod, "_mirror", lambda *a: calls.append(a))
        mod.main()
        return calls

    def test_completed_owned_task_lands_on_the_wall(self, monkeypatch):
        calls = self._run(
            {
                "hook_event_name": "TaskCompleted",
                "task_id": "20",
                "task_subject": "建 fetcher",
                "task_description": "细节",
                "teammate_name": "alice",
                "team_name": "session-0def8f84",
                "cwd": "/p",
            },
            {},
            monkeypatch,
        )
        assert len(calls) == 1
        project_id, task, _cc = calls[0]
        assert project_id == "proj-1"
        assert task["task_id"] == "20"
        assert task["owner"] == "alice"

    def test_unowned_dependency_free_task_is_skipped(self, monkeypatch):
        calls = self._run(
            {
                "hook_event_name": "TaskCompleted",
                "task_id": "21",
                "task_subject": "随手记一笔",
                "cwd": "/p",
            },
            {"blocks": [], "blockedBy": []},
            monkeypatch,
        )
        assert calls == []

    def test_missing_title_or_id_is_silent(self, monkeypatch):
        assert self._run({"task_id": "1"}, {}, monkeypatch) == []
        assert self._run({"task_subject": "x"}, {}, monkeypatch) == []

    def test_invalid_json_payload_is_silent(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setattr(mod.sys, "stdin", MagicMock(read=lambda: "not json"))
        mod.main()  # must not raise

    def test_mirror_failure_never_blocks_cc(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setattr(
            mod.sys,
            "stdin",
            MagicMock(
                read=lambda: json.dumps(
                    {"task_id": "1", "task_subject": "t", "teammate_name": "bob"}
                )
            ),
        )
        monkeypatch.setattr(mod, "_read_cc_task", lambda *_a: {})
        monkeypatch.setattr(mod, "_resolve_project_id", lambda _c: "proj-1")

        def boom(*_a):
            raise OSError("api down")

        monkeypatch.setattr(mod, "_mirror", boom)
        mod.main()  # must not raise


class TestMirrorRequestBody:
    def test_body_carries_owner_completion_and_cc_ids(self, monkeypatch):
        mod = _import_module()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _mock_response({})

        monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(mod, "_get_api_url", lambda: "http://localhost:9")
        mod._mirror(
            "proj-1",
            {"title": "t", "description": "d", "task_id": "20", "owner": "alice"},
            {"blockedBy": ["19", "18"]},
        )

        assert captured["url"] == "http://localhost:9/api/projects/proj-1/tasks"
        body = captured["body"]
        assert body["status"] == "completed"
        assert body["cc_task_id"] == "20"
        assert body["assigned_to"] == "alice"
        assert body["cc_blocked_by"] == ["19", "18"]
        assert body["tags"] == ["cc-task"]

    def test_unowned_task_sends_no_assignee(self, monkeypatch):
        mod = _import_module()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _mock_response({})

        monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(mod, "_get_api_url", lambda: "http://localhost:9")
        mod._mirror("p", {"title": "t", "description": "", "task_id": "1", "owner": ""}, {})
        assert "assigned_to" not in captured["body"]
