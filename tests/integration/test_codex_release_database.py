"""Upgrade a real released SQLite database across independent API processes."""
from __future__ import annotations

import contextlib
import io
import os
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
# Immutable last published baseline; do not substitute the current candidate.
BASELINE = "dd60e97eaad7e30f5e728ad9fc097f57f8a4bc88"


@contextlib.contextmanager
def api(source: Path, home: Path, database: Path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / ".codex"),
               AITEAM_DB_PATH=str(database), AITEAM_API_URL=url,
               PYTHONPATH=str(source / "src"), FASTMCP_CHECK_FOR_UPDATES="off")
    with (home / f"api-{port}.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--timeout-graceful-shutdown", "3"],
            cwd=source, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=5) as client:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert process.poll() is None, "isolated API exited; inspect fixture log"
                    time.sleep(.05)
                else:
                    raise AssertionError("isolated API startup timed out")
                yield client
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def tables(database):
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_published_database_upgrade_preserves_project_task_memo_and_creates_usage_tables(tmp_path):
    old = tmp_path / "published"
    old.mkdir()
    archive = subprocess.check_output(["git", "archive", BASELINE, "src"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(old, filter="data")
    home = tmp_path / "home"
    home.mkdir()
    database = tmp_path / "upgrade.db"
    with api(old, home, database) as client:
        response = client.post("/api/projects", json={"name": "Upgrade retained project", "root_path": str(home)})
        response.raise_for_status()
        project = response.json()["data"]
        response = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "Retained task"})
        response.raise_for_status()
        task = response.json()["data"]
        response = client.post(f"/api/tasks/{task['id']}/memo", json={"content": "Retained migration evidence"})
        response.raise_for_status()
        memo = client.get(f"/api/tasks/{task['id']}/memo").json()
    before = tables(database)
    assert "account_usage_accounts" not in before  # The release fixture must really precede this feature.
    with sqlite3.connect(database) as db:
        old_clock = db.execute("PRAGMA user_version").fetchone()[0]
    for _ in range(2):  # Reopening the upgraded DB must not duplicate or lose records.
        with api(ROOT, home, database) as client:
            current_project = client.get(f"/api/projects/{project['id']}").json()["data"]
            assert current_project["name"] == project["name"]
            assert current_project["created_at"] == project["created_at"]
            current_task = client.get(f"/api/tasks/{task['id']}").json()["data"]
            assert current_task["title"] == task["title"]
            assert current_task["created_at"] == task["created_at"]
            assert client.get(f"/api/tasks/{task['id']}/memo").json() == memo
            accounts = client.get("/api/account-usage")
            accounts.raise_for_status()
            assert accounts.json()["data"]["accounts"] == []
        assert before <= tables(database)
        assert {"account_usage_accounts", "account_usage_monitors", "account_plan_price_anchors"} <= tables(database)
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == old_clock
            assert db.execute("SELECT count(*) FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0] == 1
