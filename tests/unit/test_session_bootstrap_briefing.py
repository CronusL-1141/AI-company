"""Registered-project Leader briefing emitted by the SessionStart hook.

Pins four things:
- the pending-decision section reads what GET /api/leader-briefings really
  returns ({"items": [...]}); it read a "data" key before and never showed;
- briefings the permission-denied hook files on its own do not count as
  pending decisions (the hook tags them; untagged rows filed before it did are
  caught by its title prefix), and other projects' decisions stay out;
- the Top5 rules carry the current delegation and memo cadence wording;
- retired outputs stay retired: the teams-dir cleanup notice, the permanent
  member dispatch guide, and the sub-agent marker sweep.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import aiteam.hooks.permission_denied_recovery as pdr
import aiteam.hooks.session_bootstrap as sb
from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

PROJECT = "11111111-1111-4111-8111-111111111111"
OTHER_PROJECT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def api():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = EventBus(repo=repo)
    deps._manager = TeamManager(repository=repo, memory=memory)
    app = create_app()

    @asynccontextmanager
    async def _no_lifespan(app):
        yield

    app.router.lifespan_context = _no_lifespan
    yield TestClient(app)
    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None


def _registered_briefing(monkeypatch, api_get=lambda *a, **kw: None) -> str:
    monkeypatch.setattr(sb, "_api_get", api_get)
    monkeypatch.setattr(sb, "_check_project_registration",
                        lambda *a, **kw: (True, False, {"id": PROJECT}))
    monkeypatch.setattr(sb, "_fetch_direction_memories", lambda *a, **kw: [])
    return sb._build_briefing()


def _file_denial_like_production(api, monkeypatch) -> None:
    """Let the real permission-denied hook file its briefing through the real route."""
    def _post_json(url, payload, timeout=None):
        if url.endswith("/api/leader-briefings"):
            return api.post("/api/leader-briefings", json=payload).json()
        return None  # diagnose unreachable -> keyword fallback -> needs_user_approval

    monkeypatch.setattr(pdr, "_post_json", _post_json)
    payload = {"session_id": "s-denied", "tool_name": "Bash",
               "tool_input": {"command": "ls /private"}, "reason": "denied by classifier"}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    with pytest.raises(SystemExit):
        pdr.main()


def test_pending_decisions_read_the_real_route_and_skip_noise(api, monkeypatch, capsys):
    _file_denial_like_production(api, monkeypatch)
    capsys.readouterr()
    for body in (
        {"title": "tagged auto denial", "tags": ["auto:permission-denied"]},
        {"title": "other project decision", "project_id": OTHER_PROJECT},
        {"title": "keep the legacy DB?", "project_id": PROJECT, "urgency": "high",
         "recommendation": "A, archive read-only"},
        {"title": "default model fell back", "urgency": "high"},
        {"title": "already settled", "project_id": PROJECT},
    ):
        assert api.post("/api/leader-briefings", json={"urgency": "medium", **body}).status_code == 200
    settled = next(i for i in api.get("/api/leader-briefings").json()["items"]
                   if i["title"] == "already settled")
    api.put(f"/api/leader-briefings/{settled['id']}/resolve", json={"resolution": "done"})

    def api_get(path, timeout=2.0):
        if path.startswith("/api/leader-briefings"):
            return api.get(path).json()
        return None

    out = _registered_briefing(monkeypatch, api_get)

    assert "=== Leader简报: 2个待决事项 ===" in out
    assert "  [high] keep the legacy DB?" in out
    assert "    建议: A, archive read-only" in out
    assert "  [high] default model fell back" in out
    for absent in ("Agent denied", "tagged auto denial", "other project decision", "already settled"):
        assert absent not in out


def test_permission_denied_hook_tags_what_it_files(api, monkeypatch, capsys):
    """The tag the briefing filters on is the one the real hook writes, through the real route."""
    _file_denial_like_production(api, monkeypatch)
    capsys.readouterr()
    rows = api.get("/api/leader-briefings").json()["items"]
    assert len(rows) == 1
    assert rows[0]["title"].startswith("Agent denied:")
    assert rows[0]["tags"] == [sb._AUTO_BRIEFING_TAG]


def test_tag_alone_keeps_a_hook_briefing_out(api, monkeypatch, capsys):
    """Retitling the hook's briefing must not bring the denial noise back."""
    monkeypatch.setattr(sb, "_AUTO_BRIEFING_TITLE_PREFIX", "\x00never-matches")
    _file_denial_like_production(api, monkeypatch)
    capsys.readouterr()
    out = _registered_briefing(
        monkeypatch, lambda path, timeout=2.0: api.get(path).json()
        if path.startswith("/api/leader-briefings") else None)
    assert "Leader简报:" not in out


def test_no_pending_section_when_only_noise(api, monkeypatch, capsys):
    _file_denial_like_production(api, monkeypatch)
    capsys.readouterr()
    out = _registered_briefing(
        monkeypatch, lambda path, timeout=2.0: api.get(path).json()
        if path.startswith("/api/leader-briefings") else None)
    assert "Leader简报:" not in out


def test_top5_rules_wording(monkeypatch):
    out = _registered_briefing(monkeypatch)
    rules = out[out.index("=== Leader核心规则 (Top5) ==="):]
    rule1 = next(line for line in rules.splitlines() if line.startswith("1. "))
    rule4 = next(line for line in rules.splitlines() if line.startswith("4. "))
    assert "几次工具调用能做完的自己做" in rule1
    assert "多文件实施、长时间调试这类会占住统筹面的活派给成员" in rule1
    assert "有了可交接的进展" in rule4
    assert "同一方法失败3次必须换思路或上报" in rule4
    assert "team_name" not in out
    assert "每2个操作" not in out


def test_teams_dir_cleanup_notice_is_retired(tmp_path, monkeypatch):
    teams = tmp_path / ".claude" / "teams"
    for i in range(12):
        (teams / f"session-{i}").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out = _registered_briefing(monkeypatch)
    assert "团队目录" not in out
    assert "rm -rf" not in out


def test_permanent_member_guide_is_retired(tmp_path, monkeypatch):
    (tmp_path / "team-defaults.json").write_text(json.dumps({
        "auto_create_team": True,
        "permanent_members": [{"name": "qa-observer", "role": "qa", "enabled": True}],
    }), encoding="utf-8")
    monkeypatch.setattr(sb, "CONFIG_DIR", tmp_path, raising=False)
    out = _registered_briefing(monkeypatch)
    assert "常驻成员派发指引" not in out
    assert "qa-observer" not in out


def test_session_start_leaves_subagent_markers_alone(tmp_path):
    """Nothing writes these markers any more, so the hook must not sweep that dir either."""
    marker = tmp_path / ".claude" / "data" / "ai-team-os" / "subagent_sessions" / "old-session"
    marker.parent.mkdir(parents=True)
    marker.touch()
    stale = time.time() - 3 * 24 * 3600
    os.utime(marker, (stale, stale))
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env.update(HOME=str(tmp_path), AITEAM_API_URL="http://127.0.0.1:9")
    proc = subprocess.run(
        [sys.executable, sb.__file__], input=b'{"session_id": "s1", "source": "startup"}',
        capture_output=True, cwd=str(tmp_path), env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "API未启动" in proc.stdout.decode("utf-8")
    assert marker.exists()
