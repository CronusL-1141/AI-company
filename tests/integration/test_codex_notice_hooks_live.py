"""Codex hook subprocesses against an isolated real API, including DB reopen.

This proves the protocol and delivery ledger, not native TUI visibility.
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.integration.test_user_notice_hooks_live import ROOT, LiveAPI, _project

HOOKS = ROOT / "plugin/harness/codex/hooks"


@pytest.fixture()
def live(tmp_path):
    api = LiveAPI(tmp_path)
    # Detectors and hooks must never read the developer's installed adapter.
    api.env.update(CODEX_HOME=str(api.home / ".codex"),
                   AITEAM_CODEX_STATE_DIR=str(tmp_path / "codex-state"),
                   AITEAM_STATE_DIR=str(tmp_path / "state"))
    api.start()
    try:
        yield api
    finally:
        api.stop()
        api.client.close()


def hook(api, event, session, project, source="startup"):
    script = "session_bootstrap_codex.py" if event == "SessionStart" else "channel_unread_codex.py"
    args = [] if event == "SessionStart" else ["leader-codex"]
    payload = {"session_id": session, "thread_id": session, "source": source,
               "cwd": str(project), "hook_event_name": event, "prompt": "test"}
    result = subprocess.run([sys.executable, str(HOOKS / script), *args], input=json.dumps(payload),
                            text=True, capture_output=True, cwd=project, env=api.env, timeout=20)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout) if result.stdout.strip() else {}
    assert set(document) <= {"continue", "stopReason", "suppressOutput", "systemMessage", "hookSpecificOutput"}
    assert set(document.get("hookSpecificOutput", {})) <= {"hookEventName", "additionalContext"}
    text = document.get("systemMessage", "")
    assert len(text.splitlines()) <= 1
    assert "\x1b" not in text
    return document


def deliveries(api, session):
    return [delivery for key in api.notices() for delivery in api.deliveries(key, session)]


def test_start_prompt_protocol_and_delivery_survive_api_restart(live):
    project = _project(live)
    session = "codex-notice-protocol"
    start = hook(live, "SessionStart", session, project)
    assert start.get("systemMessage", "").startswith("[AI Team OS] ")
    assert start.get("hookSpecificOutput", {}).get("additionalContext")
    assert len(deliveries(live, session)) == 1
    hook(live, "UserPromptSubmit", session, project)
    live.restart()  # only the fixture-owned process, on its isolated DB/port
    rows = deliveries(live, session)
    first = next(row for row in rows if row["event"] == "SessionStart:startup")
    assert first["emitted_at"] is not None
    assert len({row["key"] for row in rows}) == len(rows)
    hook(live, "UserPromptSubmit", session, project)
    rows = deliveries(live, session)
    assert len({row["key"] for row in rows}) == len(rows)


def test_concurrent_start_and_prompt_do_not_duplicate_claims(live):
    project = _project(live)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(hook, live, event, "codex-first-turn", project)
                   for event in ("SessionStart", "UserPromptSubmit")]
        outputs = [future.result() for future in futures]
    rows = deliveries(live, "codex-first-turn")
    assert rows
    assert len({row["key"] for row in rows}) == len(rows)
    assert sum(bool(d.get("systemMessage")) for d in outputs) == len(rows)


def test_offline_codex_line_is_imported_after_recovery(live):
    project = _project(live)
    live.stop()
    down = hook(live, "SessionStart", "codex-offline", project)
    assert down.get("systemMessage", "").startswith("[AI Team OS] OS 服务正在启动")
    assert "OS API 尚未就绪" in down["hookSpecificOutput"]["additionalContext"]
    live.start()
    owed = hook(live, "UserPromptSubmit", "codex-offline", project)
    assert owed["hookSpecificOutput"]["additionalContext"].startswith(
        "[AI Team OS] Codex 适配已加载；OS API 可达。"), "the prompt brings the start it owed"
    rows = deliveries(live, "codex-offline")
    assert any(row["key"] == "api_starting" and row["event"] == "local:SessionStart:startup" for row in rows)
    assert not list((live.home / ".claude/data/ai-team-os/start-owed").glob("codex.*.json"))

    # A compaction inside a running Codex: nothing is starting the API, so it is down.
    live.stop()
    compacted = hook(live, "SessionStart", "codex-offline-2", project, source="compact")
    assert "Codex" in compacted["systemMessage"]
    live.start()
    hook(live, "UserPromptSubmit", "codex-offline-2", project)
    rows = deliveries(live, "codex-offline-2")
    assert any(row["key"].startswith("api_down") and row["event"].startswith("local:") for row in rows)
