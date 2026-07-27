"""批 3 ④：send_event 的 CC 团队归属判据拿错对象。

旧策略 1 拿 payload.agent_type（**模板名**，如 testing-bug-fixer）去匹配 CC 团队
config 的 members[].name（**派单时自定义的成员名**，如 batch3-wrong-object-fixes）。
两者不同源，绝大多数情况根本匹配不上；偶尔撞上同名时反而会把事件归到别的会话的
团队去（比 miss 更糟）。权威判据是 CC 自己写进 config.json 的 leadSessionId。
"""

from __future__ import annotations

import importlib
import json

send_event = importlib.import_module("aiteam.hooks.send_event")


def _write_team(home, dirname: str, config: dict) -> None:
    d = home / ".claude" / "teams" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _fake_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


class TestResolveCCTeamName:
    def test_lead_session_id_is_authoritative(self, monkeypatch, tmp_path):
        home = _fake_home(monkeypatch, tmp_path)
        # 别的会话的队，成员名恰好等于模板名 —— 旧实现会命中它（拿错对象）
        _write_team(
            home,
            "workflow-wf_abc",
            {
                "name": "workflow-wf_abc",
                "leadSessionId": "other-session",
                "members": [{"name": "testing-bug-fixer"}],
            },
        )
        _write_team(
            home,
            "session-11112222",
            {
                "name": "session-11112222",
                "leadSessionId": "sess-1",
                "members": [{"name": "batch3-fixes"}],
            },
        )
        assert send_event._resolve_cc_team_name("sess-1") == "session-11112222"

    def test_no_owning_team_returns_none(self, monkeypatch, tmp_path):
        """本会话没队时返回 None —— 不拿成员名瞎猜别人的队。"""
        home = _fake_home(monkeypatch, tmp_path)
        _write_team(
            home,
            "workflow-wf_abc",
            {
                "name": "workflow-wf_abc",
                "leadSessionId": "other-session",
                "members": [{"name": "testing-bug-fixer"}],
            },
        )
        assert send_event._resolve_cc_team_name("sess-1") is None

    def test_empty_session_id_returns_none(self, monkeypatch, tmp_path):
        home = _fake_home(monkeypatch, tmp_path)
        _write_team(
            home,
            "session-11112222",
            {"name": "session-11112222", "leadSessionId": "sess-1", "members": []},
        )
        assert send_event._resolve_cc_team_name("") is None

    def test_missing_teams_dir_is_silent(self, monkeypatch, tmp_path):
        _fake_home(monkeypatch, tmp_path)
        assert send_event._resolve_cc_team_name("sess-1") is None

    def test_broken_config_skipped(self, monkeypatch, tmp_path):
        home = _fake_home(monkeypatch, tmp_path)
        bad = home / ".claude" / "teams" / "broken"
        bad.mkdir(parents=True)
        (bad / "config.json").write_text("{not json", encoding="utf-8")
        _write_team(
            home,
            "session-11112222",
            {"name": "session-11112222", "leadSessionId": "sess-1", "members": []},
        )
        assert send_event._resolve_cc_team_name("sess-1") == "session-11112222"

    def test_member_name_matching_is_gone(self):
        """源码级红线：不再拿 members[].name 做归属判据。"""
        import inspect

        src = inspect.getsource(send_event._resolve_cc_team_name)
        assert 'config.get("members"' not in src
        assert "leadSessionId" in src
