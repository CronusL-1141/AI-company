"""Which Claude Code config folder an API request is about (docs/config-dir-compat-design.md §4.2).

The API is one process for every session on the machine, and sessions can run
from different config folders (CLAUDE_CONFIG_DIR). A session tells the API its
folder in two ways:

* its MCP server sends ``X-CC-Config-Dir`` (percent-encoded, like ``X-Project-Dir``)
  with ``X-CC-Session-Id`` on every call;
* its hooks put ``config_dir`` in the facts of ``POST /api/notices/pending`` at
  session start and on every prompt.

A hook event says it too: Claude Code keeps every transcript under
``<config folder>/projects/``, and every event carries ``transcript_path``. That is
what binds an event that arrives before its session's first facts (the session
start's events race each other) or after an API restart.

The API remembers session id -> folder, and for the length of a request binds
the folder of the session the request is about in the path section
(``hook_core.bind_cc_config_dir``). ``cc_config_dir()`` then answers for that
session wherever the request's work reads it, threads and tasks it starts
included (they copy the context). With no folder for the request, the API's own
environment answers (``own_cc_config_dir``).

Work that is about no request (the state reaper's cycle) runs unbound, on the
API's own folder.

The binding is for reading only. Anything the API writes into a config folder
(agent templates, the default model, installed copies) goes to its own folder
(``own_cc_config_dir``): a folder named in a request is taken on the requester's
word, and a request must not be able to make the API create or change files
wherever it likes.
"""

from __future__ import annotations

import contextlib
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

from aiteam.paths import bind_cc_config_dir, unbind_cc_config_dir

HEADER = "X-CC-Config-Dir"
SESSION_HEADER = "X-CC-Session-Id"
_HEADER_KEY = HEADER.lower().encode("ascii")
_SESSION_HEADER_KEY = SESSION_HEADER.lower().encode("ascii")
MAX_PATH_CHARS = 4096
# Sessions remembered, oldest dropped first; a session reports its folder again on
# its next prompt or MCP call.
MAX_SESSIONS = 4096

_by_session: OrderedDict[str, Path] = OrderedDict()


def accept(raw: object) -> Path | None:
    """A reported folder as a path, or None: absolute, no ``..`` segment, no NUL, of sane length.

    It selects which folder's files the API reads for that session. Links are not
    resolved (a config folder may be one), so a parent segment is refused by its
    spelling: a reported folder names one place, never a way out of another.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS or "\x00" in raw:
        return None
    folder = Path(raw)
    return folder if folder.is_absolute() and ".." not in folder.parts else None


def folder_of_transcript(transcript_path: object) -> Path | None:
    """The config folder a Claude Code transcript lives in: the parent of its nearest ``projects/``.

    Main transcripts sit at ``projects/<slug>/<session>.jsonl``, sub-agent ones a few
    levels deeper; no level below ``projects/`` is named ``projects`` (slugs start
    with "-", the rest are ids and fixed names).
    """
    path = accept(transcript_path)
    return next((parent.parent for parent in path.parents if parent.name == "projects"), None) if path else None


def _remember(session_id: str, folder: Path | None) -> Path | None:
    """Record the folder a session reported (already accepted); returns it."""
    if folder is not None and session_id:
        _by_session[session_id] = folder
        _by_session.move_to_end(session_id)
        while len(_by_session) > MAX_SESSIONS:
            _by_session.popitem(last=False)
    return folder


def for_session(session_id: object) -> Path | None:
    """The folder ``session_id`` last reported, or None."""
    return _by_session.get(session_id) if isinstance(session_id, str) and session_id else None


def session_folder(session_id: object, *reported: Path | None) -> Path | None:
    """The first folder a request reports (remembered for its session), else the session's last one."""
    session_id = session_id if isinstance(session_id, str) else ""
    folder = next((candidate for candidate in reported if candidate is not None), None)
    return _remember(session_id, folder) or for_session(session_id)


def forget_all() -> None:
    """Drop every remembered session (tests)."""
    _by_session.clear()


@contextlib.contextmanager
def bound(folder: Path | None) -> Iterator[None]:
    """Answer for ``folder`` inside the block; None leaves whatever is bound already."""
    if folder is None:
        yield
        return
    token = bind_cc_config_dir(folder)
    try:
        yield
    finally:
        unbind_cc_config_dir(token)


def _header(scope: Scope, key: bytes) -> str:
    raw = next((value for name, value in scope.get("headers", []) if name.lower() == key), b"")
    return urllib.parse.unquote(raw.decode("latin-1"))


class CcConfigScopeMiddleware:
    """Bind the config folder an MCP call names (or its session reported earlier) for the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        folder = session_folder(_header(scope, _SESSION_HEADER_KEY), accept(_header(scope, _HEADER_KEY)))
        with bound(folder):
            await self.app(scope, receive, send)
