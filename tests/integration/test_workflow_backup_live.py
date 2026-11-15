"""The workflow backup through the real pieces: a uvicorn API process and the distributed hook script.

An isolated HOME, a config folder shaped like a real one, the API started with
``aiteam.api.app:create_app`` on a free port, and ``plugin/hooks/send_event.py``
run as Claude Code runs it (payload on stdin). State is read back from the
database file and over HTTP, also after an API restart. Nothing touches port
8000, the real HOME, or a real config folder.

macOS only: the API process keys enrolled folders by this Mac's hardware identifier
and refuses to enroll elsewhere (aiteam.assets.machine).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.unit.assets.conftest import PROJECT_ROOT, PROJECT_SLUG, SECRETS, build_config_folder, read_store

ROOT = Path(__file__).resolve().parents[2]
SEND_EVENT = ROOT / "plugin" / "hooks" / "send_event.py"
MEMORY = f"projects/{PROJECT_SLUG}/memory"
DEADLINE_SECONDS = 20.0
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the backup enrolls folders on macOS only")


def _free_port() -> int:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Api:
    """An isolated API process over a throwaway HOME, started and restarted on demand."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        (self.home / ".aiteam").mkdir(parents=True)
        self.folder = tmp_path / "config"
        self.folder.mkdir()
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("CLAUDE_PLUGIN_ROOT", "AITEAM_DATA_DIR", "AITEAM_DB_PATH", "CLAUDECODE",
                                   "CLAUDE_PROJECT_DIR", "CODEX_HOME")}
        self.env.update({"HOME": str(self.home), "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                         "AITEAM_API_URL": self.base, "CLAUDE_CONFIG_DIR": str(self.folder),
                         "NO_PROXY": "127.0.0.1,localhost", "TMPDIR": str(tmp_path),
                         "XDG_CACHE_HOME": str(tmp_path / "cache"), "AITEAM_DIAGNOSTICS_ENABLED": "0"})
        self.process: subprocess.Popen | None = None
        self.client = httpx.Client(base_url=self.base, timeout=30, trust_env=False,
                                   headers={"X-CC-Config-Dir": str(self.folder)})

    @property
    def database(self) -> Path:
        return self.home / ".aiteam" / "aiteam.db"

    def start(self) -> None:
        # The API's stderr goes to a file: a pipe nobody reads fills up and stalls the API.
        self.log = open(self.home / "api-stderr.log", "ab")  # noqa: SIM115 - closed in stop()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.home, env=self.env, stdout=subprocess.DEVNULL, stderr=self.log,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                tail = (self.home / "api-stderr.log").read_bytes().decode(errors="replace")[-2000:]
                pytest.fail("isolated API exited: " + tail)
            try:
                if self.client.get("/api/health").json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.05)
        pytest.fail("isolated API health timeout")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.log.close()
            self.process = None

    def hook(self, event: str, session_id: str, **fields) -> None:
        """Run the distributed hook script the way Claude Code does."""
        payload = {"hook_event_name": event, "session_id": session_id, "cwd": PROJECT_ROOT,
                   "transcript_path": str(self.folder / "projects" / PROJECT_SLUG / f"{session_id}.jsonl"), **fields}
        done = subprocess.run([sys.executable, str(SEND_EVENT), event], input=json.dumps(payload), text=True,
                              env=self.env, capture_output=True, timeout=30)
        assert done.returncode == 0, done.stderr

    def rows(self, sql: str, *args) -> list[tuple]:
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def versions(self, rel_path: str) -> list[tuple]:
        return self.rows("SELECT v.source, v.session_id, v.content_hash FROM asset_versions v "
                         "JOIN asset_items i ON i.id = v.item_id WHERE i.rel_path = ? ORDER BY v.hlc", rel_path)

    def wait_for(self, predicate, what: str):
        deadline = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        pytest.fail(f"timed out waiting for {what}")


@pytest.fixture
def api(tmp_path):
    instance = Api(tmp_path)
    build_config_folder(instance.folder)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()
        instance.client.close()


def test_real_hook_script_backs_up_within_the_turn_and_after_a_shell_change(api):
    response = api.client.post("/api/assets/roots", json={"profile": "main"})
    assert response.status_code == 200, response.text
    api.client.post("/api/assets/run", json={})
    baseline = len(api.rows("SELECT * FROM asset_versions"))
    assert baseline == 13

    # A write tool in a session: the hook script alone, no turn end.
    notes = api.folder / MEMORY / "notes.md"
    notes.write_text("Learned in this turn.\n", encoding="utf-8")
    api.hook("PostToolUse", "live-1", tool_name="Write", tool_use_id="toolu_live_1",
             tool_input={"file_path": str(notes), "content": "Learned in this turn."})
    latest = api.wait_for(lambda: len(api.versions("notes.md")) == 2 and api.versions("notes.md")[-1], "the edit")
    assert latest[0] == "event" and latest[1] == "live-1"

    # A shell command changes a file; the turn end compares the folder.
    claude = api.folder / "CLAUDE.md"
    claude.write_text("# Global rules\nChanged by a shell command.\n", encoding="utf-8")
    api.hook("PostToolUse", "live-1", tool_name="Bash", tool_use_id="toolu_live_2",
             tool_input={"command": "echo changed > CLAUDE.md"})
    api.hook("Stop", "live-1")
    latest = api.wait_for(lambda: len(api.versions("CLAUDE.md")) == 2 and api.versions("CLAUDE.md")[-1], "the turn end")
    assert latest[0] == "reconcile"

    # Across a restart: the rows are on disk, the folder is still enrolled, nothing leaked.
    api.stop()
    api.start()
    [root] = api.client.get("/api/assets/status", params={"host": "claude"}).json()["data"]["roots"]
    assert root["status"] == "enrolled" and sum(root["backed_up"].values()) == 13
    everything = "\n".join(sqlite3.connect(api.database).iterdump()) + "".join(
        content.decode("utf-8", "replace") for content in read_store(api.home / ".aiteam"))
    assert [secret for secret in SECRETS if secret in everything] == []
