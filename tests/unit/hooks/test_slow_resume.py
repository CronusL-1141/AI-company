"""A real isolated uvicorn listener can outlive an unresponsive HTTP loop.

Launch the production API entry point, wrapping responses in the child only
and disabling lifespan (no database, shared service or monitor is started).
The response wrapper validates notice bodies with the production schema.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

from ._notice_fakes import age_owed_starts, hook_env, local_records, owed_starts
from .test_owed_start import E01_ZH, STARTING_ZH, _context, _load, _prompt, _start


@pytest.fixture()
def home(tmp_path):
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


@pytest.fixture()
def slow_api(home, tmp_path):
    """A genuine API command/PID/TCP listener; a file turns HTTP delays on/off."""
    if os.name != "posix":
        pytest.skip("the stdlib process inspection is POSIX-only")
    import shutil
    if not shutil.which("lsof") and not Path("/usr/sbin/lsof").exists():
        pytest.skip("lsof unavailable: process evidence remains unknown")
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    gate = tmp_path / "slow"
    requests = tmp_path / "requests.jsonl"
    (wrapper / "sitecustomize.py").write_text(textwrap.dedent('''
        import asyncio, json, os
        from pathlib import Path
        import aiteam.api.app as api
        from aiteam.types import PendingRequest, PendingResponse
        original = api.create_app
        class Scripted:
            def __init__(self, app): self.app = app
            async def __call__(self, scope, receive, send):
                if scope['type'] != 'http':
                    return await self.app(scope, receive, send)
                path = scope['path']
                if Path(os.environ['SLOW_GATE']).exists():
                    await asyncio.sleep(5)
                if path == '/api/notices/pending':
                    raw = b''
                    while True:
                        event = await receive()
                        raw += event.get('body', b'')
                        if not event.get('more_body'): break
                    request = PendingRequest.model_validate_json(raw)
                    with open(os.environ['REQUEST_LOG'], 'a') as handle:
                        handle.write(request.model_dump_json() + '\\n')
                    doc = PendingResponse(language='zh', user_text='[AI Team OS] RECOVERED',
                                          model_text='NOTICE', delivery_ids=['d-recovered']).model_dump(mode='json')
                elif path == '/api/hooks/compact-checkpoint':
                    doc = {'found': True, 'text': '\\nSLOW-CHECKPOINT'}
                elif path == '/api/context/resolve':
                    doc = {'project_id': 'p1', 'project': {'id': 'p1'}}
                elif path == '/api/memories':
                    doc = {'data': [{'kind': 'constraint', 'content': 'SLOW-DIRECTION'}]}
                else:
                    doc = {'data': [], 'items': [], 'total': 0}
                raw = json.dumps(doc).encode()
                await send({'type': 'http.response.start', 'status': 200,
                            'headers': [(b'content-type', b'application/json')]})
                await send({'type': 'http.response.body', 'body': raw})
        def create_app(): return Scripted(original())
        api.create_app = create_app
    '''), encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = hook_env(home, url)
    server_env = dict(env, SLOW_GATE=str(gate), REQUEST_LOG=str(requests),
                      PYTHONPATH=os.pathsep.join([str(wrapper), *filter(None, sys.path)]))
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--lifespan", "off", "--log-level", "error"],
            env=server_env, cwd=home, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(url + '/api/teams', timeout=0.2):
                        break
                except OSError:
                    assert process.poll() is None, (tmp_path / "server.log").read_text()
                    time.sleep(0.05)
            else:
                pytest.fail((tmp_path / "server.log").read_text())
            yield process, url, env, gate, requests
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("source", ["clear", "compact"])
def test_slow_live_api_defers_start_and_next_prompt_restores_original_source(home, slow_api, source):
    process, url, env, gate, requests = slow_api
    gate.touch()
    started = time.monotonic()
    start = _start(home, env, source=source)
    assert start['systemMessage'] == STARTING_ZH
    assert time.monotonic() - started < 10, "bounded local probe and HTTP retries"
    assert process.poll() is None
    (owed,) = owed_starts(home)
    assert json.loads(owed.read_text())['source'] == source
    assert json.loads(owed.read_text())['grace'] is True
    # A still-unreachable prompt gets the same bounded grace; do not refresh it.
    saved = json.loads(owed.read_text())
    assert _prompt(home, env)[0] == {}
    assert json.loads(owed.read_text())['at'] == saved['at']
    gate.unlink()
    prompt, elapsed = _prompt(home, env)
    assert elapsed < 5
    assert 'Leader简报' in _context(prompt)
    assert 'SLOW-DIRECTION' in _context(prompt)
    assert ('SLOW-CHECKPOINT' in _context(prompt)) is (source == 'compact')
    assert prompt['systemMessage'] == '[AI Team OS] RECOVERED'
    body = json.loads(requests.read_text().splitlines()[-1])
    assert (body['event'], body['source']) == ('SessionStart', source)
    assert not owed_starts(home)
    again, _ = _prompt(home, env)
    assert 'Leader简报' not in _context(again)
    assert not any(row.get('catalog_id') == 'api_down' for row in local_records(home))


def test_slow_resume_grace_expires_and_a_stopped_process_is_not_live(home, slow_api):
    process, url, env, gate, requests = slow_api
    gate.touch()
    assert _start(home, env, source='compact')['systemMessage'] == STARTING_ZH
    age_owed_starts(home)
    assert _prompt(home, hook_env(home))[0]['systemMessage'] == E01_ZH
    process.terminate()
    process.wait(timeout=5)
    assert _start(home, env, session='stopped', source='clear')['systemMessage'] == E01_ZH


def test_a_foreign_listener_and_a_nonlistening_api_pid_are_not_live(home, slow_api):
    process, url, env, gate, requests = slow_api
    hook = _load('session_bootstrap.py', 'slow_resume_identity')
    hook.API_URL = url
    assert hook._local_api_listening()
    # The API process remains alive, but this is a different live TCP listener.
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1', 0))
        foreign.listen()
        hook.API_URL = f'http://127.0.0.1:{foreign.getsockname()[1]}'
        assert not hook._local_api_listening(), 'PID must own the target listener and be this API'
        assert _start(home, hook_env(home, hook.API_URL), source='compact')['systemMessage'] == E01_ZH
    assert process.poll() is None
    assert not hook._local_api_listening(), 'a live API PID on another port is insufficient'
    # An explicitly remote URL must not borrow the local API's process/port.
    hook.API_URL = url.replace('127.0.0.1', 'example.invalid')
    assert not hook._local_api_listening()


def test_unavailable_process_inspection_is_unknown(monkeypatch):
    hook = _load('session_bootstrap.py', 'slow_resume_unknown')
    hook.API_URL = 'http://localhost:8000'
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(subprocess, 'run', unavailable)
    assert not hook._local_api_listening()


class _Inspection:
    """Scripted lsof/ps answers in the formats _local_api_listening parses; nothing is run."""

    API = "/usr/bin/python3 -m uvicorn aiteam.api.app:create_app --factory --port 8000"

    def __init__(self, listeners: list[int], identities: list[str]):
        self.listeners = listeners
        self.identities = identities

    def run(self, args, **kwargs):
        from types import SimpleNamespace

        if args[0].endswith("lsof"):
            pid = self.listeners.pop(0)
            return SimpleNamespace(returncode=0, stdout=f"p{pid}\nn127.0.0.1:8000\n", stderr="")
        assert args[0] == "/bin/ps"
        return SimpleNamespace(returncode=0, stdout=self.identities.pop(0), stderr="")


def _identity(uid: int, started: str = "Thu Oct 16 09:00:00 2026", command: str = _Inspection.API) -> str:
    return f"{uid} S {started} {command}\n"


@pytest.mark.parametrize("listeners,identities,expected", [
    pytest.param([4242, 4242], [_identity(os.getuid()), _identity(os.getuid())], True, id="stable-api"),
    pytest.param([4242, 5151], [_identity(os.getuid()), _identity(os.getuid())], False,
                 id="listener-changed-between-reads"),
    pytest.param([4242, 4242], [_identity(os.getuid()), _identity(os.getuid(), "Thu Oct 16 09:05:00 2026")],
                 False, id="pid-reused-between-reads"),
    pytest.param([4242, 4242], [_identity(os.getuid() + 1), _identity(os.getuid() + 1)], False,
                 id="another-users-api"),
])
def test_listener_evidence_needs_two_agreeing_reads_by_this_user(monkeypatch, listeners, identities, expected):
    if os.name != "posix":
        pytest.skip("the process inspection is POSIX-only")
    hook = _load('session_bootstrap.py', 'slow_resume_scripted')
    hook.API_URL = 'http://127.0.0.1:8000'
    monkeypatch.setattr(subprocess, 'run', _Inspection(list(listeners), list(identities)).run)
    assert hook._local_api_listening() is expected
