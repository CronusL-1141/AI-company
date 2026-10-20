"""AI Team OS - keep a launching session's credentials and identity out of the API.

The API is one shared, long-lived service. Whatever session happens to start it
(the MCP autostart inside a Claude Code session, ``os_restart_api``, the Codex
runtime helpers, ``aiteam up`` typed into an agent's shell) hands it an
environment that belongs to that one session:

* ``CLAUDE_CODE_MESSAGING_*`` is the session's inbox socket and its auth token.
  A process holding the token can write to that inbox as the session's own
  child instead of as an outside peer, so the host never frames the message as
  foreign.
* ``CLAUDE_CODE_SESSION_ACCESS_TOKEN`` and ``CLAUDE_SESSION_INGRESS_TOKEN_FILE``
  are a remote session's own credential and where to read it.
* The session id variables and the Claude Code session markers make the API,
  and every ``claude`` run it starts (fleet dispatch), look like part of that
  session: Claude Code reads ``CLAUDECODE``, ``CLAUDE_CODE_CHILD_SESSION``,
  ``CLAUDE_CODE_SESSION_ATTENDED``, ``CLAUDE_CODE_ENTRYPOINT``,
  ``CLAUDE_CODE_SESSION_KIND`` and ``CLAUDE_CODE_SESSION_NAME`` from its own
  environment at startup, and drops the same markers itself when it detaches a
  new session.
* ``CLAUDE_PROJECT_DIR``, ``CODEX_THREAD_ID`` and ``CODEX_SESSION_ID`` are where
  and which the launching session is. The API serves every project and every thread.

Removal is by name, never by allow-list: ``PATH``, ``HOME``, ``AITEAM_*``,
``CLAUDE_CONFIG_DIR``, ``CLAUDE_PLUGIN_ROOT`` and the like are configuration the
API needs. Nor by pattern: names such as ``CLAUDE_CODE_OAUTH_TOKEN`` (the login a
fleet ``claude`` run may need), ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` or
``CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION`` look like session variables and are
configuration. The names below were checked against Claude Code 2.1.285; a new
Claude Code release can add session variables this list does not know yet.

Every launch of the API passes ``env=scrub_session_env(...)``; the API module
also calls ``scrub_process_env()`` as its first statement, which covers
in-process starts. The first is the real fix: a process's initial environment
block stays readable (``ps -E``, ``/proc/<pid>/environ``) whatever it later does
to ``os.environ``. I26 in scripts/check_invariants.sh checks both.

Standard library only: scripts/codex_runtime.py and scripts/codex_adapter.py load
this file by path from their own tree.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Every variable with one of these prefixes: the launching session's inbox.
SESSION_ENV_PREFIXES: tuple[str, ...] = ("CLAUDE_CODE_MESSAGING_",)

SESSION_ENV_NAMES: frozenset[str] = frozenset({
    # Session ids: local, remote, bridge (Remote Control), cloud, host and runner.
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "CLAUDE_CODE_REMOTE_SESSION_UUID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_CLOUD_SESSION_ID",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_RUNNER_SESSION_ID",
    "CLAUDE_RUNNER_SESSION_UUID",
    # A remote session's own credential and where to read it.
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
    # Markers Claude Code reads at startup to place itself relative to its launcher.
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_KIND",
    "CLAUDE_CODE_SESSION_NAME",
    "CLAUDE_PID",
    # Where and which the launching session is.
    "CLAUDE_PROJECT_DIR",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
})


def is_session_env_name(name: str) -> bool:
    """True for a variable that belongs to the launching session, not to the API."""
    return name in SESSION_ENV_NAMES or name.startswith(SESSION_ENV_PREFIXES)


def scrub_session_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``environ`` (default ``os.environ``) without the launching session's variables."""
    source = os.environ if environ is None else environ
    return {name: value for name, value in source.items() if not is_session_env_name(name)}


def scrub_process_env() -> None:
    """Remove the launching session's variables from this process's ``os.environ``."""
    for name in [name for name in os.environ if is_session_env_name(name)]:
        os.environ.pop(name, None)
