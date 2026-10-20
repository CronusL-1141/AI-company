"""Shared test helpers (importable — `tests` is on pythonpath)."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import threading
from pathlib import Path

#: On-disk Codex evidence fixtures (see tests/fixtures/codex/README.md).
CODEX_FIXTURES = Path(__file__).parent / "fixtures" / "codex"

#: How often a stub server's loop checks for shutdown(). serve_forever's default
#: of 0.5s is how long every shutdown() then idles: 0.5s per stub, ~78s per run.
STUB_POLL_INTERVAL = 0.01


def serve_in_background(server) -> threading.Thread:
    """Start ``server.serve_forever`` on a daemon thread and return the thread.

    Teardown stays with the caller (``shutdown()`` / ``server_close()`` / ``join()``).
    """
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": STUB_POLL_INTERVAL}, daemon=True,
    )
    thread.start()
    return thread


def make_team(payload: dict | None = None, **overrides) -> dict:
    """Create a team for test setup, straight through the injected repository.

    ``POST /api/teams`` was retired 2026-07-27 (batch 7): in production a team is
    only ever minted by the hook chain (SubagentStart auto-enrolment / workflow
    ingest), never over HTTP. Tests therefore mint theirs the same way — through
    the repository the app is already wired to (``deps._repository``), which the
    ``app_client`` / ``integration_client`` fixtures set up.

    Takes the same body dict the retired route accepted, so call sites read the
    same as before. Returns the created team as a plain dict.
    """
    from aiteam.api import deps

    body = dict(payload or {})
    body.update(overrides)
    name = body.pop("name")
    mode = body.pop("mode", "coordinate")
    config = body.pop("config", None)
    repo = deps._repository
    if repo is None:  # pragma: no cover — misuse guard
        raise RuntimeError("make_team() requires an app fixture that sets deps._repository")
    team = asyncio.get_event_loop().run_until_complete(
        repo.create_team(name=name, mode=mode, config=config, **body)
    )
    return team.model_dump()


# ============================================================
# Agent template guard - shared by every test that asserts a
# hardcoded subagent_type is real.
# ============================================================


def shipped_template_names() -> set[str]:
    """Frontmatter names of the templates shipped in ``plugin/agents``.

    Deliberately the shipped directory only, not the live three-source catalogue:
    the guard has to give the same answer on a CI box with no ``~/.claude/agents``.

    Parsing goes through the production parser (``parse_template``), never a regex.
    A regex such as ``^name:\\s*(\\S+)`` skips a template that declares no name and
    then happily matches a ``name:`` line inside another template's body - the guard
    set silently shrinks and gains a name nobody can spawn. The count assertion below
    is what makes that failure loud.
    """
    from aiteam.services.agent_template_registry import PLUGIN_AGENTS_DIR, parse_template

    files = sorted(PLUGIN_AGENTS_DIR.glob("*.md"))
    assert files, f"no agent templates found under {PLUGIN_AGENTS_DIR}"
    parsed = [(f, parse_template(f)) for f in files]
    unparsed = [f.name for f, meta in parsed if not meta]
    assert not unparsed, f"templates that failed to parse (catalogue would drop them): {unparsed}"
    names = {str(meta["name"]) for _f, meta in parsed if meta}
    assert len(names) == len(files), (
        f"parsed {len(names)} distinct template names from {len(files)} *.md files - "
        "a name collision or a parse fallback is hiding a template"
    )
    return names


def spawnable_subagent_types() -> set[str]:
    """What a subagent_type hardcoded *in this repo* is allowed to be.

    Shipped templates plus the single sanctioned fallback constant. CC's own built-in
    types are NOT enumerated here: the list is unknowable and moves with every CC
    release, which is exactly why the runtime check only warns. This set governs
    static assertions about repo-hardcoded names, nothing else.
    """
    from aiteam.services.agent_template_registry import FALLBACK_SUBAGENT_TYPE

    return shipped_template_names() | {FALLBACK_SUBAGENT_TYPE}


# ============================================================
# Launching-session environment - shared by every test that
# proves a launched process does not inherit it.
# ============================================================

#: Stand-ins for what a Claude Code or Codex session puts in its children's environment:
#: the inbox socket and token, the session ids, credential and markers, the project and thread. All
#: values are fake. The names are spelled out here, not taken from aiteam.session_env, so
#: dropping a name from the production list turns these tests red.
FAKE_SESSION_ENV = {
    "CLAUDE_CODE_MESSAGING_SOCKET": "/nonexistent/cc-socks/fake.sock",
    "CLAUDE_CODE_MESSAGING_TOKEN": "fake-token-for-tests",
    "CLAUDE_CODE_SESSION_ID": "00000000-0000-4000-8000-0000000000a1",
    "CLAUDE_SESSION_ID": "00000000-0000-4000-8000-0000000000a2",
    "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_fake_remote_session",
    "CLAUDE_CODE_REMOTE_SESSION_UUID": "00000000-0000-4000-8000-0000000000a4",
    "CLAUDE_CODE_BRIDGE_SESSION_ID": "fake-bridge-session",
    "CLAUDE_CODE_CLOUD_SESSION_ID": "fake-cloud-session",
    "CLAUDE_CODE_HOST_SESSION_ID": "fake-host-session",
    "CLAUDE_RUNNER_SESSION_ID": "session_fake_runner",
    "CLAUDE_RUNNER_SESSION_UUID": "00000000-0000-4000-8000-0000000000a5",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN": "fake-session-access-token",
    "CLAUDE_SESSION_INGRESS_TOKEN_FILE": "/nonexistent/session-ingress-token",
    "CLAUDE_CODE_SESSION_KIND": "bg",
    "CLAUDE_CODE_SESSION_NAME": "fake-launcher-name",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_SESSION_ATTENDED": "1",
    "CLAUDE_PID": "4242",
    "CLAUDE_PROJECT_DIR": "/nonexistent/launching-session-project",
    "CODEX_THREAD_ID": "00000000-0000-7000-8000-0000000000a3",
    "CODEX_SESSION_ID": "00000000-0000-7000-8000-0000000000a6",
}

_ENV_ENTRY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def initial_env_names(pid: int) -> set[str]:
    """Variable names in a live process's initial environment block.

    This is what the process was started with, kept by the kernel from exec time:
    later edits to its own os.environ do not show here, so it tells what the
    launcher passed. macOS reads it with ``ps -Eww``, Linux from /proc. Only names
    are returned; values never leave this function.
    """
    if sys.platform == "darwin":
        listing = subprocess.run(
            ["ps", "-Eww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        return {token.split("=", 1)[0] for token in listing.split() if _ENV_ENTRY.match(token)}
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return {entry.split(b"=", 1)[0].decode() for entry in raw.split(b"\0") if b"=" in entry}

