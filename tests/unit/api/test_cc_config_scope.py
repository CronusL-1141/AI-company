"""aiteam.api.cc_config_scope: which config folder a request is about (design §4.2, batch 3)."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

from aiteam.api import cc_config_scope
from aiteam.api.cc_config_scope import CcConfigScopeMiddleware, folder_of_transcript
from aiteam.paths import cc_config_dir, own_cc_config_dir


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "own"))
    cc_config_scope.forget_all()
    yield
    cc_config_scope.forget_all()


@pytest.mark.parametrize(("transcript", "folder"), [
    ("/p/cfg/projects/-Users-me-repo/s1.jsonl", "/p/cfg"),
    ("/p/cfg/projects/-Users-me-repo/s1/subagents/workflows/wf_1/agent-x.jsonl", "/p/cfg"),
    ("/home/me/projects/cfg/projects/-home-me-projects-app/s1.jsonl", "/home/me/projects/cfg"),
    ("relative/projects/-x/s1.jsonl", None),
    ("/tmp/no-layout/s1.jsonl", None),
    ("", None),
    (None, None),
], ids=["main", "sub-agent", "projects-in-the-folder-path", "relative", "no-projects", "empty", "none"])
def test_folder_of_transcript(transcript, folder):
    assert folder_of_transcript(transcript) == (Path(folder) if folder else None)


async def _seen(scope_headers: list[tuple[bytes, bytes]]) -> Path:
    seen: list[Path] = []

    async def app(scope, receive, send):
        seen.append(cc_config_dir())

    await CcConfigScopeMiddleware(app)({"type": "http", "headers": scope_headers}, None, None)
    return seen[0]


async def test_the_header_binds_for_the_request_and_the_session_is_remembered(tmp_path):
    folder = tmp_path / "配置 目录"  # non-ASCII and a space travel percent-encoded
    named = [(b"x-cc-session-id", b"s1"), (b"x-cc-config-dir", urllib.parse.quote(str(folder)).encode())]

    assert await _seen(named) == folder
    assert cc_config_dir() == own_cc_config_dir() == tmp_path / "own"  # nothing leaks out of the request
    assert await _seen([(b"x-cc-session-id", b"s1")]) == folder  # a later call of the same session
    assert await _seen([(b"x-cc-session-id", b"s2")]) == tmp_path / "own"  # an unknown session
    assert await _seen([(b"x-cc-config-dir", b"relative/cfg")]) == tmp_path / "own"  # not accepted


@pytest.mark.parametrize("raw", ["/p/cfg/../victim", "/p/cfg/..", "/../x"], ids=["inner", "trailing", "leading"])
def test_a_reported_folder_with_a_parent_segment_is_refused(raw):
    assert cc_config_scope.accept(raw) is None


def _scoped_app():
    """The agents-config and models routes behind the scope middleware, as create_app stacks them."""
    from fastapi import FastAPI

    from aiteam.api.routes import agents_config, models

    app = FastAPI()
    app.include_router(agents_config.router)
    app.include_router(models.router)
    app.add_middleware(CcConfigScopeMiddleware)
    return app


async def test_a_forged_folder_header_never_gets_a_file_written(tmp_path):
    """Reads follow the request's folder; writes only ever go to the API's own folder."""
    import httpx

    victim = tmp_path / "victim"
    victim.mkdir()  # an existing folder: settings.json could be written there without a mkdir
    forged = {"X-CC-Config-Dir": urllib.parse.quote(str(victim))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_scoped_app()), base_url="http://t") as client:
        template = await client.put("/api/agents-config/evil.md", headers=forged,
                                    json={"name": "evil", "prompt": "x"})
        model = await client.put("/api/models/default", headers=forged, json={"model": "some-model"})

    assert template.status_code == 200 and model.status_code == 200
    assert sorted(path.relative_to(tmp_path).as_posix() for path in victim.rglob("*")) == []
    own = tmp_path / "own"
    assert (own / "agents" / "evil.md").is_file()
    assert '"model": "some-model"' in (own / "settings.json").read_text(encoding="utf-8")
