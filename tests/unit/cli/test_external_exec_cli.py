"""Complete CLI offline path uses a fake executable and a private temporary spool."""

import json
import sqlite3
import sys

import httpx
import pytest
from typer.testing import CliRunner

from aiteam.cli.commands import external_exec_cmd
from aiteam.cli.commands.external_exec_cmd import _spool_path, app


def test_offline_run_preserves_child_stdout_code_and_replayable_minimum_ledger(tmp_path):
    fake = tmp_path / "fake-codex"
    events = [
        {"type": "thread.started", "thread_id": "cli-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "PRIVATE-ANSWER"}},
        {"type": "turn.completed", "usage": {"input_tokens": 40, "cached_input_tokens": 30, "output_tokens": 5}},
    ]
    raw = "".join(json.dumps(event) + "\n" for event in events)
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "assert sys.argv[1:3] == ['exec', '--json']\nassert '--ephemeral' in sys.argv\n"
        "assert sys.argv.count('--json') == 1\n"
        f"sys.stdout.write({raw!r})\nsys.exit(7)\n"
    )
    fake.chmod(0o700)
    path = tmp_path / "spool.sqlite"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--spool-db",
            str(path),
            "--api-url",
            "http://127.0.0.1:9",
            "--executable",
            str(fake),
            "--caller-harness",
            "claude-code",
            "--caller-session-id",
            "caller",
            "--",
            "--json",
            "--ephemeral",
            "PRIVATE-PROMPT",
        ],
    )
    assert result.exit_code == 7, result.output
    assert result.stdout == raw
    assert "暂不可用" in result.stderr and "重放" in result.stderr
    assert "带了 --ephemeral" in result.stderr and "/usage" in result.stderr
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        stored, uploaded, revision = db.execute(
            "SELECT payload, uploaded_revision, revision FROM external_exec_invocations",
        ).fetchone()
    assert "PRIVATE" not in stored
    value = json.loads(stored)
    assert value["ephemeral"] and value["status"] == "failed" and value["exit_code"] == 7
    assert value["receipts"]["turn:1"]["usage"]["input_tokens"] == 40
    assert uploaded < revision
    replay = CliRunner().invoke(app, ["replay", "--spool-db", str(path), "--api-url", "http://127.0.0.1:9"])
    assert replay.exit_code == 2 and "未获确认" in replay.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM external_exec_invocations").fetchone()[0] == 1


def test_run_without_ephemeral_keeps_the_rollout_and_says_nothing_about_it(tmp_path):
    fake = tmp_path / "fake-codex"
    raw = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 1}}) + "\n"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "assert '--ephemeral' not in sys.argv\n"
        f"sys.stdout.write({raw!r})\n"
    )
    fake.chmod(0o700)
    result = CliRunner().invoke(
        app,
        ["run", "--spool-db", str(tmp_path / "spool.sqlite"), "--api-url", "http://127.0.0.1:9",
         "--executable", str(fake), "--", "-s", "read-only", "PRIVATE-PROMPT"],
    )
    assert result.exit_code == 0, result.output
    assert "--ephemeral" not in result.stderr


def test_bad_metadata_fails_before_executable_launch(tmp_path):
    result = CliRunner().invoke(
        app, ["run", "--spool-db", str(tmp_path / "spool.db"), "--task-id", "not-a-uuid", "--", "PRIVATE-PROMPT"]
    )
    assert result.exit_code == 64 and "尚未启动" in result.stderr
    assert not (tmp_path / "spool.db").exists()


def test_spool_symlink_and_credential_url_are_refused(tmp_path):
    target = tmp_path / "not-a-ledger"
    target.write_text("PRIVATE")
    link = tmp_path / "spool-link"
    link.symlink_to(target)
    result = CliRunner().invoke(app, ["run", "--spool-db", str(link), "--", "x"])
    assert result.exit_code == 64 and target.read_text() == "PRIVATE"
    result = CliRunner().invoke(
        app,
        ["run", "--spool-db", str(tmp_path / "spool.db"), "--api-url", "https://user:PRIVATE@example.test", "--", "x"],
    )
    assert result.exit_code == 64 and "PRIVATE" not in result.output


def test_old_api_route_falls_back_but_missing_project_refuses_execution(tmp_path, monkeypatch):
    fake = tmp_path / "fake-codex"
    marker = tmp_path / "launched"
    fake.write_text(f"#!{sys.executable}\nopen({str(marker)!r}, 'w').write('yes')\n")
    fake.chmod(0o700)
    original_client = httpx.AsyncClient
    missing_project = False

    def handler(request):
        return httpx.Response(
            404,
            json={
                "detail": ({"code": "project_not_found", "message": "项目不存在"} if missing_project else "Not Found")
            },
        )

    def factory(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(external_exec_cmd.httpx, "AsyncClient", factory)
    arguments = ["run", "--api-url", "http://test", "--executable", str(fake)]
    old_route = CliRunner().invoke(app, [*arguments, "--spool-db", str(tmp_path / "old.sqlite"), "--", "x"])
    assert old_route.exit_code == 0, old_route.output
    assert marker.exists()
    marker.unlink()
    missing_project = True
    rejected = CliRunner().invoke(app, [*arguments, "--spool-db", str(tmp_path / "rejected.sqlite"), "--", "x"])
    assert rejected.exit_code == 64 and not marker.exists()


@pytest.mark.parametrize("command", ["run", "replay"])
def test_ambiguous_sqlalchemy_path_is_refused_before_any_ledger_file_created(tmp_path, command):
    # The prefix is a symlink: an unsafe URL reparse would follow it and mutate
    # the unrelated target instead of the requested, vetted audit?custom.db path.
    target = tmp_path / "unrelated"
    target.write_text("PRIVATE")
    prefix = tmp_path / "audit"
    prefix.symlink_to(target)
    requested = tmp_path / "audit?custom.db"
    result = CliRunner().invoke(app, [command, "--spool-db", str(requested)])
    assert result.exit_code == 64
    assert not requested.exists() and prefix.is_symlink() and target.read_text() == "PRIVATE"
    assert not (tmp_path / "audit-wal").exists() and not (tmp_path / "audit-shm").exists()


def test_every_new_private_spool_ancestor_is_700(tmp_path, monkeypatch):
    home = tmp_path / "new-home" / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = _spool_path(None)
    for directory in (home.parent, home, home / "data", home / "data/ai-team-os"):
        assert directory.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
