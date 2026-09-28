"""Shared fixtures for the user-notice tests.

Every test gets an isolated HOME (Claude Code config, OS data folder, release
cache), a fresh per-process ledger memory and a file-backed SQLite database, so
nothing here can read or write the real ~/.claude or the real database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.services.notices import ledger
from aiteam.services.notices.detectors import DetectContext, Finding, Scope
from aiteam.services.notices.detectors import decisions as decisions_module
from aiteam.services.notices.detectors import registration as registration_module
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C")
    from aiteam.api.routes import settings

    monkeypatch.setattr(settings, "_CONFIG_PATH", tmp_path / "wake_config.json")
    # The shared release checker caches under the real home (path fixed at
    # import) and talks to GitHub: replace it with an offline one.
    import httpx

    from aiteam.api import release_updates

    def offline(request):
        raise httpx.ConnectError("offline in tests")

    monkeypatch.setattr(release_updates, "release_checker", release_updates.ReleaseChecker(
        tmp_path / "cache" / "release-check.json", transport=httpx.MockTransport(offline),
    ))
    ledger.reset_memory()
    decisions_module._last_expiry.clear()
    registration_module._last_sweep.clear()
    yield home
    ledger.reset_memory()


# For cases that need the registration offer at a session start as a step, not as
# the thing under test. With the production budgets (0.3s for the detector, 1.5s
# for the whole SessionStart fetch) a busy machine times the detector out and the
# session start offers nothing. A timed-out run is "no data", so nothing else changes.
UNHURRIED_DETECTOR_S = 10.0


@pytest.fixture()
def unhurried_registration(monkeypatch):
    monkeypatch.setattr(registration_module.RegistrationDetector, "timeout_s", UNHURRIED_DETECTOR_S)
    monkeypatch.setitem(ledger.DEADLINE_S, "SessionStart", UNHURRIED_DETECTOR_S)


@pytest_asyncio.fixture()
async def repo(tmp_path):
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'notices.db'}")
    await repository.init_db()
    yield repository
    await close_db()


class StubDetector:
    """Test detector; its findings still pass the production catalog checks."""

    def __init__(self, name, catalog_ids, findings=(), *, timing=("session_start", "prompt"),
                 hosts=("cc", "codex"), prefixes=None, project_id=None, delay=0.0, error=None,
                 timeout_s=0.3):
        self.name = name
        self.catalog_ids = tuple(catalog_ids)
        self.timing = frozenset(timing)
        self.hosts = frozenset(hosts)
        self.timeout_s = timeout_s
        self.findings = list(findings)
        self.prefixes = tuple(prefixes if prefixes is not None else (f"{cid}:" for cid in catalog_ids))
        self.project_id = project_id
        self.delay = delay
        self.error = error
        self.calls = 0

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=self.prefixes, project_id=self.project_id)

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        import asyncio

        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.findings)


def finding(catalog_id: str, suffix: str = "x", **params) -> Finding:
    return Finding(catalog_id=catalog_id, key=f"{catalog_id}:{suffix}", params=params)


def request(event="SessionStart", source="startup", session="s1", host="cc", **facts):
    from aiteam.types import PendingRequest

    body = {"entrypoint": "cli", "fallback_language": "en"}
    body.update(facts)
    return PendingRequest(host=host, event=event, source=source if event == "SessionStart" else "",
                          session_id=session, facts=body)


def write_json(path: Path, value) -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
