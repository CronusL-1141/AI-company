"""uninstall_main_chain.py: preview, confirm, remove the left-over global chain (E06, design §5.9).

Runs the script the way a user's session does: as a subprocess from inside the
installed chain folder of a throwaway HOME, with the real user_notice.py next
to it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "plugin" / "hooks" / "uninstall_main_chain.py"
CHAIN = '"/usr/bin/python3" "{home}/.claude/hooks/ai-team-os/{name}"'


@pytest.fixture()
def home(tmp_path):
    home = tmp_path / "home"
    chain = home / ".claude" / "hooks" / "ai-team-os"
    chain.mkdir(parents=True)
    for name in ("uninstall_main_chain.py", "user_notice.py"):
        shutil.copy2(REPO_ROOT / "plugin" / "hooks" / name, chain / name)
    for name in ("session_bootstrap.py", "send_event.py"):
        (chain / name).write_text(f"# {name}\n", encoding="utf-8")
    (chain / "mail_reminder.py").write_text("# someone else's hook\n", encoding="utf-8")
    settings = {
        "model": "opus",
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": CHAIN.format(home=home, name="session_bootstrap.py"), "timeout": 15},
                {"type": "command", "command": "/usr/local/bin/my-own-hook"},
            ]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": CHAIN.format(home=home, name="send_event.py") + " PreToolUse"},
            ]}],
        },
    }
    (home / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    (home / ".claude" / "data" / "ai-team-os").mkdir(parents=True)
    return home


DEAD_API = "http://127.0.0.1:9"  # nothing listens: never the real service on 8000


def run(home: Path, *args: str, api_url: str = DEAD_API) -> tuple[int, dict]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_", "AITEAM_"))}
    env["HOME"] = str(home)
    env["AITEAM_API_URL"] = api_url
    script = home / ".claude" / "hooks" / "ai-team-os" / "uninstall_main_chain.py"
    result = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                            env=env, cwd=str(home), timeout=30)
    return result.returncode, json.loads(result.stdout)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_preview_lists_entries_files_and_unrecognized_ones(home):
    before = _md5(home / ".claude" / "settings.json")
    code, shown = run(home)
    assert code == 0 and shown["mode"] == "preview" and shown["plugin"] == "removed"
    assert [entry["event"] for entry in shown["entries"]] == ["SessionStart", "PreToolUse"]
    assert {item["name"] for item in shown["files"]} == {
        "mail_reminder.py", "send_event.py", "session_bootstrap.py", "uninstall_main_chain.py", "user_notice.py",
    }
    assert "mail_reminder.py" in shown["warnings"][0]
    assert shown["confirm_token"] and _md5(home / ".claude" / "settings.json") == before


def test_apply_removes_only_the_chain_backs_up_and_records_consent(home):
    original = (home / ".claude" / "settings.json").read_text(encoding="utf-8")
    _code, shown = run(home)
    code, done = run(home, "--apply", shown["confirm_token"], "--user-quote", "清理 OS 残留，确认")
    assert code == 0 and done["success"] and done["consent_recorded"] == "local"
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"model": "opus", "hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "/usr/local/bin/my-own-hook"}]}]}}
    assert Path(done["backup"]).read_text(encoding="utf-8") == original
    assert not (home / ".claude" / "hooks" / "ai-team-os").exists()
    records = [json.loads(line) for line in
               (home / ".claude" / "data" / "ai-team-os" / "notice-local.cc.jsonl").read_text().splitlines()]
    consent = next(record for record in records if record["kind"] == "consent")
    assert consent["change"] == "remove_main_chain" and consent["user_quote"] == "清理 OS 残留，确认"
    assert consent["entries_removed"] == 2 and consent["files_removed"] == 5
    assert any(record["kind"] == "local_clear" and record["key"] == "orphan_main_chain" for record in records)


def test_apply_without_the_users_words_or_after_a_change_is_refused(home):
    settings = home / ".claude" / "settings.json"
    _code, shown = run(home)
    code, refused = run(home, "--apply", shown["confirm_token"])
    assert code == 1 and "--user-quote" in refused["error"]
    (home / ".claude" / "hooks" / "ai-team-os" / "new_hook.py").write_text("x\n", encoding="utf-8")
    code, refused = run(home, "--apply", shown["confirm_token"], "--user-quote", "yes")
    assert code == 1 and "changed since the preview" in refused["error"]
    code, refused = run(home, "--apply", "nonsense", "--user-quote", "yes")
    assert code == 1 and "malformed" in refused["error"]
    assert (home / ".claude" / "hooks" / "ai-team-os").is_dir() and "ai-team-os" in settings.read_text()


def test_expired_token_is_refused(home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AITEAM_API_URL", DEAD_API)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    spec = importlib.util.spec_from_file_location("uninstall_chain_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = module.preview(now=1000)["confirm_token"]
    assert "expired" in module.apply(token, "yes", now=1000 + 601)["error"]
    assert (home / ".claude" / "hooks" / "ai-team-os").is_dir()


def test_active_plugin_or_source_install_is_refused(home):
    plugins = home / ".claude" / "plugins" / "installed_plugins.json"
    plugins.parent.mkdir(parents=True)
    plugins.write_text(json.dumps({"plugins": {"ai-team-os@m": [{"version": "1.14.0"}]}}), encoding="utf-8")
    _code, shown = run(home)
    assert shown["plugin"] == "active" and shown["confirm_token"] == "" and "enabled" in shown["refusal"]
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    settings["enabledPlugins"] = {"ai-team-os@m": False}
    (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    _code, shown = run(home)
    assert shown["plugin"] == "disabled" and shown["confirm_token"]
    (home / ".claude" / "data" / "ai-team-os" / "install_path.txt").write_text("/src", encoding="utf-8")
    _code, shown = run(home)
    assert shown["confirm_token"] == "" and "source install" in shown["refusal"]


async def test_consent_record_imports_as_one_event(home, tmp_path):
    from aiteam.services.notices import ledger
    from aiteam.storage.connection import close_db
    from aiteam.storage.repository import StorageRepository

    _code, shown = run(home)
    run(home, "--apply", shown["confirm_token"], "--user-quote", "ok")
    lines = (home / ".claude" / "data" / "ai-team-os" / "notice-local.cc.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
    await repo.init_db()
    try:
        await ledger.import_local_records(repo, "cc", records, ledger.utc_now())
        await ledger.import_local_records(repo, "cc", records, ledger.utc_now())
        [event] = await repo.list_events(event_type="decision.user_config_write")
        assert event.data["change"] == "remove_main_chain" and event.data["host"] == "cc"
    finally:
        await close_db()


def test_unparseable_settings_is_refused_before_anything_is_deleted(home):
    settings = home / ".claude" / "settings.json"
    settings.write_text(settings.read_text(encoding="utf-8")[:-3], encoding="utf-8")
    _code, shown = run(home)
    assert shown["confirm_token"] == "" and "JSON" in shown["refusal"]
    code, refused = run(home, "--apply", "1.abc", "--user-quote", "yes")
    assert code == 1 and "JSON" in refused["error"]
    assert (home / ".claude" / "hooks" / "ai-team-os").is_dir()


def test_symlinked_folder_is_refused(home, tmp_path):
    chain = home / ".claude" / "hooks" / "ai-team-os"
    real = tmp_path / "elsewhere"
    shutil.move(str(chain), str(real))
    chain.symlink_to(real, target_is_directory=True)
    _code, shown = run(home)
    assert shown["confirm_token"] == "" and "symbolic link" in shown["refusal"]
    assert (real / "send_event.py").exists()


def test_folder_removal_failure_is_reported_and_recorded(home):
    hooks = home / ".claude" / "hooks"
    _code, shown = run(home)
    hooks.chmod(0o555)  # the chain folder cannot be unlinked from its parent
    try:
        code, done = run(home, "--apply", shown["confirm_token"], "--user-quote", "清理")
    finally:
        hooks.chmod(0o755)
    assert code == 1 and done["success"] is False and done["status"] == "partial"
    assert done["settings_changed"] and Path(done["backup"]).exists() and done["error"]
    assert done["folder_removed"] == "" and done["consent_recorded"] == "local"
    records = [json.loads(line) for line in
               (home / ".claude" / "data" / "ai-team-os" / "notice-local.cc.jsonl").read_text().splitlines()]
    consent = next(record for record in records if record["kind"] == "consent")
    assert consent["status"] == "partial"
    # A partial run does not claim the leftover is gone.
    assert not any(record["kind"] == "local_clear" for record in records)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_consent_and_clear_reach_a_running_api(home, tmp_path):
    """After removal no hook will ever import local records: the script must tell the API itself."""
    import sqlite3
    import time
    import urllib.request

    port = _free_port()
    db = tmp_path / "api.db"
    api_home = tmp_path / "api-home"
    api_home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE_", "AITEAM_"))}
    env.update(HOME=str(api_home), AITEAM_DB_PATH=str(db), PYTHONPATH=str(REPO_ROOT / "src"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), NO_PROXY="*")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for _ in range(100):
            try:
                opener.open(f"{base}/api/health", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        register = urllib.request.Request(
            f"{base}/api/notices", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"key": "orphan_main_chain", "catalog_id": "orphan_main_chain"}).encode())
        opener.open(register, timeout=5).read()
        _code, shown = run(home, api_url=base)
        code, done = run(home, "--apply", shown["confirm_token"], "--user-quote", "清理 OS 残留", api_url=base)
        assert code == 0 and done["consent_recorded"] == "api"
    finally:
        server.terminate()
        server.wait(timeout=10)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        events = connection.execute(
            "SELECT json_extract(data, '$.user_quote') FROM events WHERE type = 'decision.user_config_write'"
        ).fetchall()
        status = connection.execute("SELECT status FROM notices WHERE key = 'orphan_main_chain'").fetchone()
    assert events == [("清理 OS 残留",)] and status == ("cleared",)
    local = home / ".claude" / "data" / "ai-team-os" / "notice-local.cc.jsonl"
    assert not local.exists() or all(json.loads(line)["kind"] != "consent" for line in local.read_text().splitlines())
