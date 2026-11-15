"""A Claude Code config folder shaped like a real one, and the production HTTP stack over it.

The folder mixes what the backup must keep with what it must never read: OS's own
templates (copied byte for byte from plugin/), an OS template the user changed,
the user's agents, skills, hook script and memory, OS's hook chain and hook
registration, credentials, caches, backups and session records. Secret-looking
values sit in the keys the extractors must drop, so the tests can search the
database and the content store for them.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest_asyncio

from aiteam.api import app as app_module
from aiteam.api import cc_config_scope, debug_log, deps, middleware
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks
from aiteam.api.ws.manager import ConnectionManager
from aiteam.assets.attribution import slug
from aiteam.assets.machine import MachineKey
from aiteam.assets.os_manifest import build_claude_manifest, source_root
from aiteam.assets.service import AssetBackup, project_roots
from aiteam.storage.assets import AssetRepository
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

PLUGIN = source_root() / "plugin"
PROJECT_ROOT = "/work/app"
PROJECT_SLUG = slug(PROJECT_ROOT)
UNBOUND_SLUG = "-home-someone"
NODE = "air-0001"
# This machine, as the backup knows it (the real key comes from macOS hardware).
MACHINE = MachineKey(key="0123456789abcdef0123456789abcdef")
# Values that live in keys the backup must never keep.
SECRETS = ("oauth-email@example.com", "env-value-must-stay-local", "header-value-must-stay-local",
           "url-password-must-stay-local", "credential-file-content")
OS_HOOK = '"/usr/bin/python3" "${CLAUDE_CONFIG_DIR:-$HOME/.x}/hooks/ai-team-os/send_event.py" Stop'
USER_HOOK = "bash ~/bin/notify.sh"


def _write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def build_config_folder(folder: Path) -> dict[str, list[str]]:
    """Fill ``folder``; returns the paths expected to be backed up, by category."""
    agents = sorted((PLUGIN / "agents").glob("*.md"))
    for template in agents[:2]:
        _write(folder / "agents" / template.name, template.read_bytes())
    changed = agents[2]
    _write(folder / "agents" / changed.name, changed.read_bytes() + b"\nMy own rule.\n")
    command = sorted((PLUGIN / "commands").glob("*.md"))[0]
    _write(folder / "commands" / command.name, command.read_bytes())
    os_skill = sorted(p for p in (PLUGIN / "skills").iterdir() if p.is_dir())[0]
    shutil.copytree(os_skill, folder / "skills" / os_skill.name)
    _write(folder / "loop.md", (PLUGIN / "loop.md").read_bytes())
    _write(folder / "hooks" / "ai-team-os" / "send_event.py", "# OS hook copy\n")
    guard = _write(folder / "hooks" / "guard.sh", "#!/bin/sh\nexit 0\n")
    guard.chmod(0o755)
    _write(folder / "CLAUDE.md", "# Global rules\nAnswer in Chinese.\n")
    _write(folder / "agents" / "my-reviewer.md", "---\nname: my-reviewer\n---\nReview carefully.\n")
    _write(folder / "agents" / "my-writer.md", "---\nname: my-writer\n---\nWrite clearly.\n")
    _write(folder / "skills" / "my-skill" / "SKILL.md", "---\nname: my-skill\n---\nDo it.\n")
    _write(folder / "skills" / "my-skill" / "templates" / "a.txt", "template\n")
    memory = folder / "projects" / PROJECT_SLUG / "memory"
    _write(memory / "MEMORY.md", "# Index\n- [notes](notes.md)\n")
    _write(memory / "notes.md", "The API listens on port 8000.\n")
    _write(folder / "projects" / UNBOUND_SLUG / "memory" / "home.md", "Home folder notes.\n")
    _write(folder / "projects" / PROJECT_SLUG / "s1.jsonl", json.dumps({"cwd": PROJECT_ROOT}) + "\n")
    _write(folder / "settings.json", json.dumps({
        "env": {"SOME_KEY": SECRETS[1]}, "permissions": {"allow": ["Bash"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": OS_HOOK},
                                      {"type": "command", "command": USER_HOOK}]}]},
    }, indent=2))
    _write(folder / ".claude.json", json.dumps({
        "oauthAccount": {"emailAddress": SECRETS[0]}, "userID": "u-1",
        "mcpServers": {
            build_claude_manifest().mcp_server_name: {"command": "python3", "args": ["-m", "aiteam.mcp.entry"]},
            "remote": {"type": "http", "url": f"https://me:{SECRETS[3]}@mcp.example.com/v1",
                       "headers": {"Authorization": SECRETS[2]}},
        },
        "projects": {PROJECT_ROOT: {"lastCost": 1.0}},
    }, indent=2))
    _write(folder / "plugins" / "installed_plugins.json", json.dumps({"version": 2, "plugins": {
        "lsp@official": [{"scope": "user", "installPath": "/home/me/cache/lsp", "version": "1.0.0"}]}}))
    _write(folder / "plugins" / "known_marketplaces.json", json.dumps({"official": {
        "source": {"source": "github", "repo": "org/plugins"}, "installLocation": "/home/me/cache/m"}}))
    # Never read.
    _write(folder / ".credentials.json", json.dumps({"token": SECRETS[4]}))
    _write(folder / "skills" / "synced" / "account-skill" / "SKILL.md", "synced\n")
    _write(folder / "settings.json.bak-20261111", "{}")
    _write(folder / "backups" / "old.json", "{}")
    _write(folder / "cache" / "x.json", "{}")
    _write(folder / "sessions" / "1.json", "{}")
    return {
        "global_instructions": ["CLAUDE.md"],
        "agents": [f"agents/{changed.name}", "agents/my-reviewer.md", "agents/my-writer.md"],
        "skills": ["skills/my-skill/SKILL.md", "skills/my-skill/templates/a.txt"],
        "hook_scripts": ["hooks/guard.sh"],
        "project_memory": [f"projects/{PROJECT_SLUG}/memory/MEMORY.md", f"projects/{PROJECT_SLUG}/memory/notes.md",
                           f"projects/{UNBOUND_SLUG}/memory/home.md"],
        "hook_registration": ["hooks/registration.json"],
        "mcp_servers": ["mcp/remote.json"],
        "plugin_manifest": ["plugins/manifest.json"],
    }


def read_store(data_dir: Path) -> list[bytes]:
    """Every content in the store, decompressed."""
    return [zlib.decompress(path.read_bytes()) for path in (data_dir / "asset-store").rglob("*")
            if path.is_file() and not path.name.startswith(".tmp-")]


def dump_database(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


@dataclass
class Stack:
    client: httpx.AsyncClient
    repo: StorageRepository
    backup: AssetBackup
    translator: HookTranslator
    folder: Path
    data_dir: Path
    database: Path
    expected: dict[str, list[str]]
    project_id: str

    async def settle(self) -> None:
        """Wait for every job the requests so far started (hook work and backup)."""
        await self.translator.drain(timeout=20)
        await self.backup.drain(timeout=20)

    def rows(self, sql: str, *args) -> list[tuple]:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    async def hook(self, event: str, *, session_id: str = "s1", **fields) -> httpx.Response:
        payload = {"hook_event_name": event, "session_id": session_id, "cwd": PROJECT_ROOT,
                   "transcript_path": str(self.folder / "projects" / PROJECT_SLUG / f"{session_id}.jsonl"),
                   **fields}
        response = await self.client.post("/api/hooks/event", json=payload)
        assert response.status_code == 200, response.text
        return response

    async def enroll(self, **body) -> dict:
        response = await self.client.post("/api/assets/roots", json={"profile": "rec", "path": str(self.folder),
                                                                     **body})
        assert response.status_code == 200, response.text
        await self.settle()
        return response.json()["data"]


async def open_stack(tmp_path: Path, monkeypatch, *, build: bool = True) -> Stack:
    folder = tmp_path / "config"
    folder.mkdir(exist_ok=True)
    expected = build_config_folder(folder) if build else {}
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    database = data_dir / "aiteam.db"
    url = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.setattr(middleware, "_slow_hook_log", middleware._SlowHookLog())
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    projects = await repo.list_projects()
    project = next((p for p in projects if p.root_path == PROJECT_ROOT), None) \
        or await repo.create_project(name="app", root_path=PROJECT_ROOT)
    bus = EventBus(repo=repo)
    translator = HookTranslator(repo=repo, event_bus=bus)
    backup = AssetBackup(repo=AssetRepository(url), projects=lambda: project_roots(repo), data_dir=data_dir,
                         node_id=NODE, machine=lambda: MACHINE)
    await backup.start()
    app = app_module.create_app()
    app.dependency_overrides.update({
        deps.get_repository: lambda: repo,
        deps.get_event_bus: lambda: bus,
        deps.get_hook_translator: lambda: translator,
        deps.get_asset_backup: lambda: backup,
    })
    for name, value in (("_repository", repo), ("_event_bus", bus), ("_hook_translator", translator),
                        ("_asset_backup", backup)):
        monkeypatch.setattr(deps, name, value)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={cc_config_scope.HEADER: str(folder)})
    return Stack(client, repo, backup, translator, folder, data_dir, database, expected, project.id)


async def close_stack(stack: Stack) -> None:
    await stack.settle()
    await stack.client.aclose()
    await get_engine(stack.repo._db_url).dispose()  # noqa: SLF001
    cc_config_scope.forget_all()


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch):
    opened = await open_stack(tmp_path, monkeypatch)
    try:
        yield opened
    finally:
        await close_stack(opened)
