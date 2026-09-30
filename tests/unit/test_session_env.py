"""The API never holds the launching session's inbox credentials or identity.

The launch paths themselves (MCP autostart, os_restart_api, the two Codex scripts)
are covered by the subprocess tests that read the started API's initial environment
(tests/integration). This file covers the scrub list, the API module scrubbing its
own process on import, `aiteam up`, and the PyPI entry point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from testlib import FAKE_SESSION_ENV

from aiteam.session_env import scrub_process_env, scrub_session_env

ROOT = Path(__file__).resolve().parents[2]

# Configuration the API needs; none of it may be dropped.
CONFIGURATION = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent/home",
    "AITEAM_DB_PATH": "/nonexistent/aiteam.db",
    "AITEAM_API_URL": "http://127.0.0.1:1",
    "CLAUDE_CONFIG_DIR": "/nonexistent/.claude",
    "CLAUDE_PLUGIN_ROOT": "/nonexistent/plugin",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "CODEX_HOME": "/nonexistent/.codex",
    "ANTHROPIC_API_KEY": "fake-api-key",
    # Names that look like session variables and are configuration.
    "CLAUDE_CODE_OAUTH_TOKEN": "fake-login",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": "8",
    "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "1500",
    "CLAUDE_EFFORT": "high",
}


def _child_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = scrub_session_env()
    env.update(HOME=str(tmp_path), AITEAM_DB_PATH=str(tmp_path / "aiteam.db"),
               PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


def test_scrub_drops_session_variables_and_keeps_configuration():
    source = {**CONFIGURATION, **FAKE_SESSION_ENV, "CLAUDE_CODE_MESSAGING_FUTURE_FIELD": "x"}
    before = dict(source)

    scrubbed = scrub_session_env(source)

    assert scrubbed == CONFIGURATION
    assert source == before


def test_scrub_process_env_edits_os_environ(monkeypatch):
    for name, value in {**FAKE_SESSION_ENV, "AITEAM_SCRUB_MARKER": "kept"}.items():
        monkeypatch.setenv(name, value)

    scrub_process_env()

    assert not set(FAKE_SESSION_ENV) & set(os.environ)
    assert os.environ["AITEAM_SCRUB_MARKER"] == "kept"


def test_importing_the_api_module_scrubs_its_own_process(tmp_path):
    """Covers hand-started servers: `uvicorn aiteam.api.app:create_app` typed into a session shell."""
    probe = (
        "import json, os; import aiteam.api.app; "
        "print(json.dumps(sorted(os.environ)))"
    )
    env = _child_env(tmp_path, AITEAM_SCRUB_MARKER="kept", **FAKE_SESSION_ENV)

    result = subprocess.run([sys.executable, "-c", probe], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr[-3000:]
    names = set(json.loads(result.stdout.splitlines()[-1]))
    assert not set(FAKE_SESSION_ENV) & names, sorted(set(FAKE_SESSION_ENV) & names)
    assert {"AITEAM_SCRUB_MARKER", "HOME", "AITEAM_DB_PATH", "PATH"} <= names


def test_aiteam_up_scrubs_before_serving(monkeypatch):
    """`aiteam up` serves in-process and is the parent of any reload worker."""
    import uvicorn

    from aiteam.cli.commands import up_cmd

    for name, value in {**FAKE_SESSION_ENV, "AITEAM_SCRUB_MARKER": "kept"}.items():
        monkeypatch.setenv(name, value)
    seen: list[set[str]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: seen.append(set(os.environ)))

    up_cmd.up(api_port=1, no_dashboard=True, reload=False)

    assert len(seen) == 1
    assert not set(FAKE_SESSION_ENV) & seen[0], sorted(set(FAKE_SESSION_ENV) & seen[0])
    assert "AITEAM_SCRUB_MARKER" in seen[0]


def test_pypi_entry_starts_the_api_as_a_subprocess_and_keeps_its_session(tmp_path):
    """ai-team-os-serve is an MCP server: it keeps the session id it was given and starts the
    API through the launcher that scrubs, never inside its own process."""
    probe = """
import json, os, sys, threading
import aiteam.mcp.server as server
import uvicorn

calls = []
server._ensure_api_running = lambda: calls.append("ensure")
uvicorn.run = lambda *args, **kwargs: calls.append("in-process")

def run(*args, **kwargs):
    for thread in threading.enumerate():
        if thread is not threading.current_thread():
            thread.join(60)

server.mcp.run = run
from aiteam.mcp import entry
entry.main()
print(json.dumps({"calls": calls, "api_imported": "aiteam.api.app" in sys.modules,
                  "session_kept": os.environ.get("CLAUDE_CODE_SESSION_ID") == sys.argv[1]}))
"""
    env = _child_env(tmp_path, FASTMCP_CHECK_FOR_UPDATES="off", **FAKE_SESSION_ENV)

    result = subprocess.run(
        [sys.executable, "-c", probe, FAKE_SESSION_ENV["CLAUDE_CODE_SESSION_ID"]],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr[-3000:]
    outcome = json.loads(result.stdout.splitlines()[-1])
    assert outcome == {"calls": ["ensure"], "api_imported": False, "session_kept": True}
