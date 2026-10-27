"""The Claude Code config folder moves away; the OS data directory must not follow it into nothing.

docs/config-dir-compat-design.md §7.1 (T1, T2, T5, T6). The incident: a tool
moved the whole Claude Code config folder elsewhere and set CLAUDE_CONFIG_DIR.
The database (then inside that folder) went with it; hooks recreated an empty
data directory in the old place and wrote state there; the session start
reported a source install as an orphaned plugin chain; any API restart would
have started on a new empty database.

Each case uses a throwaway HOME, real install functions (install.py), the
distributed hook scripts as subprocesses, and a real uvicorn API on a free
port; state is read back over HTTP after restarts. CLAUDE_CONFIG_DIR and
CODEX_HOME are set explicitly. Nothing touches port 8000 or the real HOME.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
CC_HOOKS = ROOT / "plugin" / "hooks"
CODEX_HOOKS = ROOT / "plugin" / "harness" / "codex" / "hooks"
RELEASE_API = "https://api.github.com/repos/CronusL-1141/AI-company/releases/latest"
E06 = "插件已卸载或停用"
MISSING_FOUND = "OS 数据目录不在原处"
SPLIT = "本会话与正在运行的 OS 服务用的不是同一个数据目录"


def _installer():
    spec = importlib.util.spec_from_file_location("_relocation_install", ROOT / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")) if root.exists() else []


class Machine:
    """One isolated HOME with a real API process that can be started on demand."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        cache = tmp_path / "cache" / "ai-team-os"
        cache.mkdir(parents=True)
        now = time.time()
        (cache / "release-check.json").write_text(json.dumps({
            "source": RELEASE_API, "version": "99.0.0", "tag": "v99.0.0", "checked_at": now, "attempted_at": now,
        }))
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {key: value for key, value in os.environ.items()
               if key not in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "AITEAM_DATA_DIR",
                              "AITEAM_DB_PATH", "LANG", "LANGUAGE", "LC_MESSAGES", "CLAUDECODE",
                              "CLAUDE_PROJECT_DIR", "XDG_STATE_HOME", "AITEAM_UNREAD_AUDIT_PATH")}
        env.update({
            "HOME": str(self.home), "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
            "AITEAM_API_URL": self.base, "XDG_CACHE_HOME": str(tmp_path / "cache"), "TMPDIR": str(tmp_path),
            "NO_PROXY": "127.0.0.1,localhost", "LC_ALL": "zh_CN.UTF-8", "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CODEX_HOME": str(tmp_path / "codex"),
        })
        self.env = env
        self.process: subprocess.Popen | None = None
        self.client = httpx.Client(base_url=self.base, timeout=10, trust_env=False)

    def start(self, **extra: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.tmp, env={**self.env, **extra}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail("isolated API exited: " + self.process.stderr.read().decode(errors="replace")[-2000:])
            try:
                if self.client.get("/api/health").json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.05)
        pytest.fail("isolated API health timeout")

    def start_refused(self, **extra: str) -> subprocess.CompletedProcess[str]:
        """Start the API expecting it to refuse; returns the finished process."""
        return subprocess.run(
            [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.tmp, env={**self.env, **extra}, capture_output=True, text=True, timeout=60,
        )

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def project_with_task(self) -> str:
        """One project with one task, created over HTTP; returns the task id."""
        project = self.client.post("/api/projects", json={"name": "relocation", "root_path": str(self.tmp)})
        project_id = project.json()["data"]["id"]
        response = self.client.post(f"/api/projects/{project_id}/tasks", json={"title": "survives the move"})
        assert response.status_code in (200, 201), response.text
        return response.json()["data"]["id"]

    def tasks(self, count: int) -> list[str]:
        """*count* tasks in one project, created over HTTP; returns their ids."""
        project = self.client.post("/api/projects", json={"name": "relocation", "root_path": str(self.tmp)})
        project_id = project.json()["data"]["id"]
        ids = []
        for index in range(count):
            response = self.client.post(f"/api/projects/{project_id}/tasks", json={"title": f"task {index}"})
            assert response.status_code in (200, 201), response.text
            ids.append(response.json()["data"]["id"])
        return ids

    def wait_healthy(self, seconds: float = 30) -> dict:
        """Health of an API this test did not start (the migration's restart returns before it is up)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                return self.client.get("/api/health").json()
            except (httpx.HTTPError, ValueError):
                time.sleep(0.1)
        pytest.fail("the API the migration restarted never answered")

    def task_count(self) -> int:
        """COUNT(*) over the tasks of the database this API serves (the gate 7 reading)."""
        return self.client.get("/api/health", params={"task_count": 1}).json()["task_count"]

    def stop_spawned_api(self) -> None:
        """Stop the API a migration started (detached, recorded in this test's own PID file)."""
        pid_file = self.tmp / "aiteam-api.pid"
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        probe = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True)
        if f"--port {self.port}" in probe.stdout and "aiteam.api.app:create_app" in probe.stdout:
            os.kill(pid, 15)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and subprocess.run(["kill", "-0", str(pid)],
                                                                 capture_output=True).returncode == 0:
                time.sleep(0.1)

    def migrate(self, *args: str) -> subprocess.CompletedProcess[str]:
        """The manual entry, as a user (or the assistant on their word) runs it."""
        return subprocess.run([sys.executable, *args], text=True, capture_output=True, env=self.env,
                              cwd=self.tmp, timeout=300)

    def task_title(self, task_id: str) -> str | None:
        """The task's title read back over HTTP from whatever database this API opened."""
        response = self.client.get(f"/api/tasks/{task_id}")
        if response.status_code != 200:
            return None
        data = response.json()
        return (data.get("data") or data).get("title")

    def install(self, monkeypatch) -> None:
        """The source installer's hook step, writing into the default config folder of this HOME."""
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        _installer().register_hooks(ROOT)

    def move_config(self) -> Path:
        """What the tool did: move the whole config folder and point CLAUDE_CONFIG_DIR at it."""
        profile = self.home / "profiles" / "one" / "config"
        profile.parent.mkdir(parents=True)
        shutil.move(str(self.home / ".claude"), str(profile))
        self.env["CLAUDE_CONFIG_DIR"] = str(profile)
        return profile

    def session_start(self, hooks: Path, *, source: str = "startup") -> dict:
        payload = {"session_id": f"s-{source}", "source": source, "cwd": str(self.tmp),
                   "hook_event_name": "SessionStart"}
        result = subprocess.run([sys.executable, str(hooks / "session_bootstrap.py")], input=json.dumps(payload),
                                text=True, capture_output=True, env=self.env, cwd=self.tmp, timeout=60)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def hook_data_dir(self, hooks: Path) -> str:
        """The data directory as the hooks of this session resolve it."""
        code = ("import importlib.util,sys; spec=importlib.util.spec_from_file_location('hook_core', sys.argv[1]); "
                "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.os_data_dir())")
        result = subprocess.run([sys.executable, "-c", code, str(hooks / "hook_core.py")], text=True,
                                capture_output=True, env=self.env, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()


@pytest.fixture()
def machine(tmp_path):
    instance = Machine(tmp_path)
    try:
        yield instance
    finally:
        instance.stop()
        instance.stop_spawned_api()
        instance.client.close()


def _text(document: dict) -> tuple[str, str]:
    return (document.get("systemMessage", ""),
            (document.get("hookSpecificOutput") or {}).get("additionalContext", ""))


def test_t1_legacy_data_carried_away_with_the_config_folder(machine, monkeypatch):
    """T1: the old layout (data inside the config folder) is moved away with it."""
    legacy = machine.home / ".claude" / "data" / "ai-team-os"
    machine.start(AITEAM_DATA_DIR=str(legacy))  # the old API built its database there
    machine.stop()
    machine.start()
    assert machine.client.get("/api/health").json()["data_dir_mode"] == "legacy"
    machine.project_with_task()
    machine.stop()
    (legacy / "install_path.txt").write_text(str(ROOT), encoding="utf-8")  # the old installer's record
    machine.install(monkeypatch)

    profile = machine.move_config()
    document = machine.session_start(profile / "hooks" / "ai-team-os")
    line, note = _text(document)

    assert not (machine.home / ".claude").exists(), _tree(machine.home / ".claude")
    assert MISSING_FOUND in line
    assert str(profile / "data" / "ai-team-os" / "aiteam.db") in note
    assert E06 not in line + note
    assert not (machine.home / ".aiteam").exists()

    refused = machine.start_refused()
    assert refused.returncode != 0
    assert "data directory not found" in refused.stderr
    assert str(profile / "data" / "ai-team-os") in refused.stderr
    assert not (machine.home / ".claude").exists() and not (machine.home / ".aiteam").exists()


def test_t2_neutral_data_directory_survives_the_move(machine, monkeypatch):
    """T2: data in the neutral directory; moving the config folder changes nothing for it."""
    monkeypatch.setenv("HOME", str(machine.home))
    data_dir = _installer().prepare_data_dir()  # install.py step 5 on an empty machine
    assert data_dir == machine.home / ".aiteam" and data_dir.is_dir()
    machine.start()
    task_id = machine.project_with_task()
    machine.stop()
    machine.install(monkeypatch)

    profile = machine.move_config()
    hooks = profile / "hooks" / "ai-team-os"
    machine.start(CLAUDE_CONFIG_DIR=str(profile))
    document = machine.session_start(hooks)
    line, note = _text(document)
    for unwanted in ("数据目录", E06, "数据有两份"):
        assert unwanted not in line, line
    assert machine.task_title(task_id) == "survives the move"
    health = machine.client.get("/api/health").json()
    assert health["data_dir"] == machine.hook_data_dir(hooks) == str(machine.home / ".aiteam")
    assert health["data_dir_mode"] == "canonical"
    assert health["cc_config_dir"] == str(profile)


def test_t5_both_hosts_relocated_write_to_one_data_directory(machine):
    """T5: CLAUDE_CONFIG_DIR and CODEX_HOME both set; the two hosts meet only in the data directory."""
    (machine.home / ".aiteam").mkdir()
    profile = machine.tmp / "profile"
    profile.mkdir()
    codex_home = machine.tmp / "codex"
    observer = codex_home / "hooks" / "ai-team-os-observer"
    shutil.copytree(CODEX_HOOKS, observer, ignore=shutil.ignore_patterns("__pycache__"))
    machine.env.update(CLAUDE_CONFIG_DIR=str(profile), CODEX_HOME=str(codex_home),
                       AITEAM_API_URL="http://127.0.0.1:9")  # the API is down: both start offline

    machine.session_start(CC_HOOKS)
    payload = {"session_id": "codex-s", "source": "startup", "cwd": str(machine.tmp)}
    codex = subprocess.run([sys.executable, str(observer / "session_bootstrap_codex.py")],
                           input=json.dumps(payload), text=True, capture_output=True, env=machine.env,
                           cwd=machine.tmp, timeout=60)
    assert codex.returncode == 0, codex.stderr

    data_dir = machine.home / ".aiteam"
    assert (data_dir / "notice-local.cc.jsonl").is_file()
    assert (data_dir / "notice-local.codex.jsonl").is_file()
    strays = [path for path in machine.tmp.rglob("notice-local.*.jsonl") if path.parent != data_dir]
    assert strays == []

    def codex_home_of(code: str, value: str | None) -> str:
        env = dict(machine.env)
        env.pop("CODEX_HOME", None)
        if value is not None:
            env["CODEX_HOME"] = value
        result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, env=env,
                                cwd=machine.tmp, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    catalog = ("import importlib.util; spec=importlib.util.spec_from_file_location('c', "
               f"{str(observer / 'tool_catalog_codex.py')!r}); m=importlib.util.module_from_spec(spec); "
               "spec.loader.exec_module(m); print(m.codex_home())")
    package = "from aiteam.paths import codex_home; print(codex_home())"
    monitor = "from aiteam.services.account_monitor import _login_source_stamp; print(_login_source_stamp()[0])"
    default = str(machine.home / ".codex")
    assert codex_home_of(catalog, None) == str(codex_home)  # unset: where the hook is installed
    assert codex_home_of(catalog, "") == str(codex_home)  # blank is unset, never the working directory
    assert codex_home_of(package, None) == default
    assert codex_home_of(package, "") == default
    assert codex_home_of(monitor, "") == os.path.realpath(default)


def test_t6_a_service_on_another_data_directory_is_reported(machine):
    """T6: the API was started with AITEAM_DATA_DIR=A; this session resolves B."""
    elsewhere = machine.tmp / "data-a"
    (machine.home / ".aiteam").mkdir()
    machine.start(AITEAM_DATA_DIR=str(elsewhere))
    assert machine.client.get("/api/health").json()["data_dir"] == str(elsewhere)
    line, note = _text(machine.session_start(CC_HOOKS))
    assert SPLIT in line
    assert str(elsewhere) in note and str(machine.home / ".aiteam") in note


def _legacy_machine(machine: Machine, tasks: int) -> tuple[Path, list[str], int]:
    """The v1.15.2 layout: data inside the config folder, the API serving it, its port recorded."""
    legacy = machine.home / ".claude" / "data" / "ai-team-os"
    machine.start(AITEAM_DATA_DIR=str(legacy))  # the old API built its database there
    machine.stop()
    machine.start()
    assert machine.client.get("/api/health").json()["data_dir_mode"] == "legacy"
    ids = machine.tasks(tasks)
    (legacy / "api_port.txt").write_text(str(machine.port), encoding="utf-8")  # autostart records it
    with sqlite3.connect(f"file:{legacy / 'aiteam.db'}?mode=ro", uri=True) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    return legacy, ids, user_version


def _real_databases(root: Path) -> list[Path]:
    return sorted(Path(d) / n for d, _s, files in os.walk(root) for n in files if n == "aiteam.db")


def test_t7_v1152_layout_moved_by_the_conversation_entry(machine, monkeypatch):
    """T7 through the manual entry (the installer wires the same call in batch 2): every gate on real processes."""
    legacy, ids, user_version = _legacy_machine(machine, 3)
    machine.install(monkeypatch)
    old_api = machine.process
    result = machine.migrate("-m", "aiteam.data_dir_migration", "--apply", "--user-quote", "好，迁移 OS 数据")
    assert result.returncode == 0, result.stdout + result.stderr
    assert old_api.wait(timeout=30) is not None  # gate 3 stopped the old API
    machine.process = None

    canonical = machine.home / ".aiteam"
    assert legacy.is_symlink() and os.path.samefile(legacy, canonical)
    assert _real_databases(machine.home) == [canonical / "aiteam.db"]
    with sqlite3.connect(f"file:{canonical / 'aiteam.db'}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == user_version
    connection.close()
    assert not (canonical / "MIGRATING").exists()
    health = machine.client.get("/api/health").json()
    assert (health["data_dir"], health["data_dir_mode"]) == (str(canonical), "canonical")
    assert machine.task_count() == 3 and all(machine.task_title(task_id) for task_id in ids)
    assert "用户确认: 已记录（api）" in result.stdout
    line, _note = _text(machine.session_start(machine.home / ".claude" / "hooks" / "ai-team-os"))
    assert "数据仍在旧位置" not in line


HOLDER = r"""
import sqlite3, sys
db, source = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("SELECT COUNT(*) FROM tasks").fetchone()
print("ready", flush=True)
sys.stdin.readline()
con.close()
con = sqlite3.connect(db)  # a leftover service: reconnect by the original path and write
columns = [row[1] for row in con.execute("PRAGMA table_info(tasks)")]
names = ", ".join(f'"{c}"' for c in columns)
picked = ", ".join("'late'" if c == "id" else f'"{c}"' for c in columns)
con.execute(f"INSERT INTO tasks ({names}) SELECT {picked} FROM tasks WHERE id = ?", (source,))
con.commit()
con.close()
print("done", flush=True)
"""


def test_t8_a_process_holding_the_database_keeps_the_data_in_place(machine):
    """T8, manual-entry variant: db_in_use, nothing moved, the API back on the old place, one database."""
    legacy, ids, _ = _legacy_machine(machine, 3)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(legacy / "aiteam.db"), ids[0]],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        result = machine.migrate(str(ROOT / "scripts" / "migrate_data_dir.py"), "--apply")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "(db_in_use)" in result.stdout and str(holder.pid) in result.stdout

        canonical = machine.home / ".aiteam"
        assert not os.path.lexists(canonical)
        assert legacy.is_dir() and not legacy.is_symlink() and (legacy / "aiteam.db").is_file()
        assert sorted(p.name for p in legacy.parent.iterdir()) == ["ai-team-os"], "no .migrated-*, no stub"
        assert not (legacy / "MOVED-TO").exists() and not (legacy / "MIGRATING").exists()
        health = machine.wait_healthy()  # restarted by the migration, on the old place
        assert (health["data_dir"], health["data_dir_mode"]) == (str(legacy), "legacy")
        assert machine.task_count() == 3

        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "done"
        assert holder.wait(timeout=30) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)
        holder.stdin.close()
        holder.stdout.close()
    assert machine.task_count() == 4  # the write by the old path landed in the one database served
    assert _real_databases(machine.home) == [legacy / "aiteam.db"]

