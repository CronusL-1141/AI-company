"""Two Claude Code config folders, one API: each session's files are read in its own folder.

docs/config-dir-compat-design.md §4.2 and §6 batch 3. The API is one process for
every session on the machine; here it runs with no CLAUDE_CONFIG_DIR, so its own
folder is the default one, where neither session keeps anything. Session A runs
from one moved config folder and session B from another, each with its transcript
(``projects/``), its process registration (``sessions/``) and its team
(``teams/``) there. Everything goes through the real entries: the MCP client's
``_api_call`` in a process with the session's environment, the distributed
``session_bootstrap.py`` and ``send_event.py`` hooks, and a real API process;
results are read back over HTTP.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .test_config_dir_relocation import CC_HOOKS, ROOT, Machine, _installer
from .test_config_dir_relocation import machine as machine  # noqa: F401 - pytest fixture

SESSIONS = {
    "a": ("aaaaaaaa-0000-4000-8000-00000000000a", 41001),
    "b": ("bbbbbbbb-0000-4000-8000-00000000000b", 41002),
}
_SUMMARY = (
    "import json, sys\n"
    "from aiteam.mcp._base import _api_call\n"
    "print(json.dumps(_api_call('GET', sys.argv[1])))\n"
)


def _slug(path: Path) -> str:
    return "".join(char if char.isalnum() else "-" for char in str(path))


def _profile(machine: Machine, name: str) -> Path:
    """A moved config folder holding one session's transcript, registration and team."""
    session_id, pid = SESSIONS[name]
    profile = machine.home / "profiles" / name / "config"
    transcript = profile / "projects" / _slug(machine.tmp) / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "assistant", "message": {
        "role": "assistant", "model": f"model-{name}", "usage": {"input_tokens": 10}}}) + "\n", encoding="utf-8")
    (profile / "sessions").mkdir()
    (profile / "sessions" / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": session_id, "cwd": str(machine.tmp), "status": "busy"}), encoding="utf-8")
    team = profile / "teams" / f"team-{name}"
    team.mkdir(parents=True)
    (team / "config.json").write_text(json.dumps({"name": f"team-{name}", "leadSessionId": session_id}),
                                      encoding="utf-8")
    return profile


def _session_env(machine: Machine, profile: Path, session_id: str) -> dict[str, str]:
    return {**machine.env, "CLAUDE_CONFIG_DIR": str(profile), "CLAUDE_CODE_SESSION_ID": session_id,
            "PYTHONPATH": str(ROOT / "src")}


def _mcp_summary(machine: Machine, profile: Path, session_id: str, project_id: str) -> dict:
    """GET the project summary the way this session's MCP server calls the API."""
    result = subprocess.run([sys.executable, "-c", _SUMMARY, f"/api/projects/{project_id}/summary"],
                            text=True, capture_output=True, env=_session_env(machine, profile, session_id),
                            cwd=machine.tmp, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _sessions_in(summary: dict) -> set[str]:
    return {row["session_id"] for row in summary.get("sessions") or []}


def _hook(machine: Machine, profile: Path, script: str, payload: dict, *args: str) -> None:
    result = subprocess.run([sys.executable, str(CC_HOOKS / script), *args], input=json.dumps(payload),
                            text=True, capture_output=True, env={**machine.env, "CLAUDE_CONFIG_DIR": str(profile)},
                            cwd=machine.tmp, timeout=60)
    assert result.returncode == 0, result.stderr


def _container(machine: Machine, session_id: str) -> dict:
    teams = machine.client.get("/api/teams").json()["data"]
    return next(team for team in teams if team["name"] == f"session-{session_id[:8]}")


def _two_sessions(machine: Machine, monkeypatch) -> tuple[dict[str, Path], str]:
    monkeypatch.setenv("HOME", str(machine.home))
    _installer().prepare_data_dir()
    machine.start()  # no CLAUDE_CONFIG_DIR: the API's own folder is the default one
    assert machine.client.get("/api/health").json()["cc_config_dir"] == str(machine.home / ".claude")
    project = machine.client.post("/api/projects", json={"name": "two-folders", "root_path": str(machine.tmp)})
    return {name: _profile(machine, name) for name in SESSIONS}, project.json()["data"]["id"]


def test_each_sessions_mcp_calls_read_its_own_folder_concurrently(machine, monkeypatch):
    """projects/ per request: the MCP header names the folder; parallel calls never cross."""
    profiles, project_id = _two_sessions(machine, monkeypatch)
    calls = [name for _ in range(12) for name in SESSIONS]

    with ThreadPoolExecutor(max_workers=len(calls)) as workers:
        seen = list(workers.map(
            lambda name: (name, _sessions_in(_mcp_summary(machine, profiles[name], SESSIONS[name][0], project_id))),
            calls))

    for name, sessions in seen:
        assert sessions == {SESSIONS[name][0]}, (name, sessions)
    # A request that names no folder (the Dashboard) reads the API's own: nothing there.
    summary = machine.client.get(f"/api/projects/{project_id}/summary").json()
    assert _sessions_in(summary) == set()


def test_a_session_start_teaches_the_api_its_folder_for_later_events(machine, monkeypatch):
    """sessions/ per session: the facts of session A's start bind A's later hook events to A's folder."""
    profiles, _project_id = _two_sessions(machine, monkeypatch)
    session_a, pid_a = SESSIONS["a"]

    _hook(machine, profiles["a"], "session_bootstrap.py",
          {"session_id": session_a, "source": "startup", "cwd": str(machine.tmp)})
    # Events without a transcript path: only the folder the start reported can place them.
    _hook(machine, profiles["a"], "send_event.py",
          {"session_id": session_a, "cwd": str(machine.tmp), "source": "startup"}, "SessionStart")
    _hook(machine, profiles["a"], "send_event.py",
          {"session_id": session_a, "cwd": str(machine.tmp), "agent_id": "agent-a",
           "agent_type": "general-purpose"}, "SubagentStart")

    assert _container(machine, session_a)["config"].get("cc_pid") == pid_a


def test_an_events_transcript_places_it_in_its_folder(machine, monkeypatch):
    """sessions/ and teams/ per event: B's events find B's registration and team, A's find A's."""
    profiles, _project_id = _two_sessions(machine, monkeypatch)
    for name, profile in profiles.items():
        session_id, _pid = SESSIONS[name]
        transcript = profile / "projects" / _slug(machine.tmp) / f"{session_id}.jsonl"
        _hook(machine, profile, "send_event.py",
              {"session_id": session_id, "cwd": str(machine.tmp), "source": "startup",
               "transcript_path": str(transcript)}, "SessionStart")
        _hook(machine, profile, "send_event.py",
              {"session_id": session_id, "cwd": str(machine.tmp), "transcript_path": str(transcript),
               "agent_id": f"agent-{name}", "agent_type": "general-purpose"}, "SubagentStart")

    teams = {team["name"] for team in machine.client.get("/api/teams").json()["data"]}
    for name in SESSIONS:
        session_id, pid = SESSIONS[name]
        assert _container(machine, session_id)["config"].get("cc_pid") == pid, name
        assert f"team-{name}" in teams, teams  # the hook found the team in its own folder
    assert not (machine.home / ".claude").exists()


def test_a_session_starts_notices_are_judged_in_its_own_folder(machine, monkeypatch):
    """The start's detectors run in the folder the start reports: A's stale copy is found, B has none."""
    monkeypatch.setenv("HOME", str(machine.home))
    _installer().prepare_data_dir()
    profile_a = machine.home / "profiles" / "a" / "config"
    profile_b = machine.home / "profiles" / "b" / "config"
    profile_b.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_a))
    _installer().register_hooks(ROOT)  # a source install into A: copies and the owner record
    (profile_a / "hooks" / "ai-team-os" / "send_event.py").write_text("# edited copy\n", encoding="utf-8")
    machine.start()  # its own folder is the default one, where nothing is installed

    def stale_notices() -> list[str]:
        rows = machine.client.get("/api/notices", params={"status": "all"}).json()["items"]
        return [row["key"] for row in rows if row["key"].startswith("installed_copy_stale:")]

    _hook(machine, profile_b, "session_bootstrap.py",
          {"session_id": SESSIONS["b"][0], "source": "startup", "cwd": str(machine.tmp)})
    assert stale_notices() == []
    _hook(machine, profile_a, "session_bootstrap.py",
          {"session_id": SESSIONS["a"][0], "source": "startup", "cwd": str(machine.tmp)})
    assert len(stale_notices()) == 1
