"""Codex context hints respect local exclusions and remain distinct from ACLs."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "plugin/harness/codex/hooks"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('[mcp_servers.ai-team-os]\nurl="http://localhost:8000/mcp/"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("AITEAM_NOTICE_LANGUAGE", "zh")
    return home


@pytest.mark.parametrize("language", ["zh", "en"])
def test_main_catalog_covers_core_without_parameter_schemas(home, language):
    catalog = load("tool_catalog_codex")
    result = catalog.render_catalog({}, language=language)
    names = {line.split(":", 1)[0][2:] for line in result.splitlines() if line.startswith("- ")}
    assert len(names) == 32
    assert {"unified_search", "task_create", "report_read"} <= names
    assert len(result) <= catalog.MAX_CATALOG_CHARS
    assert "inputSchema" not in result and "properties" not in result


@pytest.mark.parametrize("config", [
    '[mcp_servers.ai-team-os]\nenabled=false\n',
    '[mcp_servers.ai-team-os]\nenabled_tools=[]\n',
    '[mcp_servers.ai-team-os]\nenabled_tools="bad"\n',
    '[mcp_servers.ai-team-os]\nenabled="false"\n',
    'invalid = [',
    '[mcp_servers.other]\nenabled=true\n',
])
def test_disabled_or_unreadable_policy_never_falls_back_to_all(home, config):
    (home / "config.toml").write_text(config)
    assert load("tool_catalog_codex").render_catalog({}) == ""


def test_user_and_project_exclusions_are_both_respected(home, tmp_path):
    (home / "config.toml").write_text(
        '[mcp_servers.ai-team-os]\n'
        'enabled_tools=["unified_search","report_read","task_status"]\n'
        'disabled_tools=["task_status"]\n'
    )
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex/config.toml").write_text(
        '[mcp_servers.ai-team-os]\ndisabled_tools=["report_read"]\n'
    )
    result = load("tool_catalog_codex").render_catalog({"cwd": str(project)})
    lines = [line for line in result.splitlines() if line.startswith("- ")]
    assert lines == ["- unified_search: 搜索任务、memo、报告的短片段"]


def test_role_policy_filters_subagent_index(home):
    (home / "agents").mkdir()
    (home / "agents/reviewer.toml").write_text(
        '[mcp_servers.ai-team-os]\nenabled_tools=["report_read"]\n'
    )
    result = load("tool_catalog_codex").render_catalog({"agent_type": "reviewer"}, audience="subagent")
    assert [line for line in result.splitlines() if line.startswith("- ")] == [
        "- report_read: 按ID读取报告全文"
    ]


def test_subagent_index_has_no_management_or_write_tools(home):
    result = load("tool_catalog_codex").render_catalog({}, audience="subagent")
    assert "- unified_search:" in result
    for name in ["os_restart_api", "team_delete", "model_config_set", "task_create", "memory_add", "report_save"]:
        assert f"- {name}:" not in result


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_session_start_preserves_notice_and_adds_index(home, monkeypatch, capsys, source):
    bootstrap = load("session_bootstrap_codex")
    monkeypatch.setattr(bootstrap, "_get", lambda *a, **k: {})
    monkeypatch.setattr(bootstrap, "_update_notice", lambda payload: {"notice": "有新版", "language": "zh"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": source})))
    bootstrap.main()
    result = json.loads(capsys.readouterr().out)
    assert result["systemMessage"] == "有新版"
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "API 可达" in context and "- unified_search:" in context
    assert "有新版" in context


def test_native_subagent_without_task_binding_gets_queries_only(home):
    result = subprocess.run(
        [sys.executable, str(HOOKS / "inject_subagent_context_codex.py")],
        input=json.dumps({"hook_event_name": "SubagentStart", "agent_type": "worker", "agent_id": "test-child"}),
        env={**os.environ, "AITEAM_API_URL": "http://127.0.0.1:1"},
        text=True, capture_output=True, timeout=5, check=True,
    )
    data = json.loads(result.stdout)
    context = data["hookSpecificOutput"]["additionalContext"]
    assert "- unified_search:" in context
    assert "- os_restart_api:" not in context
    assert "派单绑定已核对" not in context
    assert "task_memo_add" not in context


def test_catalog_names_exist_in_current_registered_surface(home):
    from aiteam.mcp.tools import register_all

    class Capture:
        def __init__(self):
            self.names = set()

        def tool(self, *args, **kwargs):
            def decorator(fn):
                self.names.add(fn.__name__)
                return fn
            return decorator

    capture = Capture()
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("AITEAM_TOOLSETS", raising=False)
        patch.delenv("AITEAM_READONLY", raising=False)
        register_all(capture)
    assert {row[0] for row in load("tool_catalog_codex").CORE_TOOLS} <= capture.names
