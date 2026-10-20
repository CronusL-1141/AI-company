"""The unread prompt hooks' invocation audit: never in the way, named outcomes, bounded size.

* Without hook_core.py (the cleaner and the audit live there) both hosts still
  deliver E01 and ledger notices; user_notice.py does not need the core.
* Claude Code records why a prompt printed what it printed with the same reason
  names as the Codex entry (api_unreachable, api_starting, owed_start_delivered).
* The JSONL log rotates inline at InvocationAudit.max_bytes and refuses a file
  with a second hard link.

Real hook processes, an isolated HOME, and a fake API that validates the
production pending schema (tests/unit/hooks/_notice_fakes.py).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ._notice_fakes import REFUSED_URL, FakeApi, age_owed_starts, hook_env, output
from .test_owed_start import _serve_briefing, _start

ROOT = Path(__file__).resolve().parents[3]
HOSTS = {
    "cc": (ROOT / "plugin/hooks", "channel_unread.py", "leader-cc"),
    "codex": (ROOT / "plugin/harness/codex/hooks", "channel_unread_codex.py", "leader-codex"),
}
LEDGER_LINE = "[AI Team OS] 有 2 项等你决定，最新：测试。对 Claude 说「列出待决事项」"
PRODUCTION_LIMIT = 5 * 1024 * 1024


@pytest.fixture()
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


def prompt(host: str, home: Path, api_url: str, *, hooks: Path | None = None, audit: Path | None = None,
           session: str = "s1") -> subprocess.CompletedProcess[str]:
    directory, script, reader = HOSTS[host]
    extra = {"CODEX_HOME": str(home / ".codex"), "XDG_STATE_HOME": str(home / ".local/state")}
    if audit is not None:
        extra["AITEAM_UNREAD_AUDIT_PATH"] = str(audit)
    payload = {"session_id": session, "cwd": str(home), "prompt": "hi"}
    result = subprocess.run(
        [sys.executable, str((hooks or directory) / script), reader], input=json.dumps(payload),
        text=True, capture_output=True, env=hook_env(home, api_url, **extra), cwd=home, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    return result


def outcomes(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row["event"] == "outcome"]


def without_core(host: str, tmp_path: Path) -> Path:
    directory = HOSTS[host][0]
    bare = tmp_path / f"{host}-hooks-without-core"
    shutil.copytree(directory, bare, ignore=shutil.ignore_patterns("hook_core.py", "__pycache__"))
    return bare


# ── a missing core costs the audit and quoted text, never the notices ──

@pytest.mark.parametrize("host", list(HOSTS))
def test_without_the_core_e01_still_reaches_the_user(host, home, tmp_path):
    result = prompt(host, home, REFUSED_URL, hooks=without_core(host, tmp_path))
    assert "服务未启动" in output(result.stdout).get("systemMessage", ""), result.stdout + result.stderr
    assert "hook_core.py unavailable" in result.stderr
    assert not (home / ".local/state/ai-team-os").exists(), "no audit without the core"


@pytest.mark.parametrize("host", list(HOSTS))
def test_without_the_core_ledger_notices_still_reach_the_user(host, home, tmp_path):
    with FakeApi() as api:
        api.pending = [{"language": "zh", "user_text": LEDGER_LINE, "model_text": "LEDGER-NOTE",
                        "delivery_ids": ["d-1"]}]
        result = prompt(host, home, api.url, hooks=without_core(host, tmp_path))
    document = output(result.stdout)
    assert document.get("systemMessage") == LEDGER_LINE, result.stdout + result.stderr
    assert document["hookSpecificOutput"]["additionalContext"] == "LEDGER-NOTE"


# ── Claude Code outcome reasons, named as the Codex entry names them ──

def test_cc_api_down_is_audited_as_unreachable_not_as_an_empty_ledger(home, tmp_path):
    audit = tmp_path / "audit.jsonl"
    first = prompt("cc", home, REFUSED_URL, audit=audit)
    second = prompt("cc", home, REFUSED_URL, audit=audit)
    assert "服务未启动" in output(first.stdout)["systemMessage"]
    assert second.stdout == "", "E01 once per session"
    reasons = [(row["reason"], row["output_chars"]) for row in outcomes(audit)]
    assert reasons == [("api_unreachable", len(first.stdout)), ("api_unreachable", 0)]


def test_cc_owed_start_outcomes_are_named(home, tmp_path):
    audit = tmp_path / "audit.jsonl"
    _start(home, hook_env(home))  # cold start: owed, says the service is starting
    quiet = prompt("cc", home, REFUSED_URL, audit=audit)
    with FakeApi() as api:
        _serve_briefing(api, home)
        delivered = prompt("cc", home, api.url, audit=audit)
        ordinary = prompt("cc", home, api.url, audit=audit)
    assert quiet.stdout == ""
    assert "Leader简报" in output(delivered.stdout)["hookSpecificOutput"]["additionalContext"]
    assert [(row["reason"], row["output_chars"]) for row in outcomes(audit)] == [
        ("api_starting", 0),
        ("owed_start_delivered", len(delivered.stdout)),
        ("notification_emitted", len(ordinary.stdout)),
    ]


def test_cc_owed_start_after_the_grace_is_audited_as_unreachable(home, tmp_path):
    audit = tmp_path / "audit.jsonl"
    _start(home, hook_env(home))
    age_owed_starts(home)
    late = prompt("cc", home, REFUSED_URL, audit=audit)
    assert "服务未启动" in output(late.stdout)["systemMessage"]
    assert [(row["reason"], row["output_chars"]) for row in outcomes(audit)] == [
        ("api_unreachable", len(late.stdout)),
    ]


def test_cc_ledger_timeout_is_audited_as_deadline(monkeypatch, tmp_path, capsys):
    """A ledger that answers too slowly is a deadline, not an empty ledger (in process)."""
    cu = importlib.import_module("aiteam.hooks.channel_unread")
    notice = cu._user_notice()
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AITEAM_UNREAD_AUDIT_PATH", str(audit))
    monkeypatch.setattr(cu.sys, "argv", ["channel_unread.py", "leader-cc"])
    monkeypatch.setattr(cu, "_read_payload", lambda: {"session_id": "slow", "cwd": str(tmp_path)})

    def slow(*args, **kwargs):
        notice._failure("timeout")
        return None

    monkeypatch.setattr(notice, "fetch_pending", slow)
    cu.main()
    assert capsys.readouterr().out == ""
    assert outcomes(audit)[-1]["reason"] == "http_deadline_exceeded"


# ── size and ownership guards of the shared log ──

def load_core():
    spec = importlib.util.spec_from_file_location("hook_core_audit_under_test", HOSTS["cc"][0] / "hook_core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_log_rotates_inline_and_keeps_one_previous_generation(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AITEAM_UNREAD_AUDIT_PATH", str(path))
    audit = load_core().InvocationAudit(host="cc")
    audit.max_bytes = 1000
    for index in range(40):
        audit.record("outcome", f"reason-{index:02d}")
    rotated = path.with_name(path.name + ".1")
    current = [json.loads(line) for line in path.read_text().splitlines()]
    previous = [json.loads(line) for line in rotated.read_text().splitlines()]
    longest = max(len(line) + 1 for line in (path.read_text() + rotated.read_text()).splitlines())
    assert path.stat().st_size < audit.max_bytes + longest
    assert rotated.stat().st_size < audit.max_bytes + longest
    assert current[-1]["reason"] == "reason-39", "the newest record is always kept"
    reasons = [row["reason"] for row in previous + current]
    assert reasons == sorted(reasons) and len(reasons) < 40, "older generations are dropped"
    assert path.stat().st_mode & 0o777 == rotated.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".2").exists()


@pytest.mark.parametrize("host", list(HOSTS))
def test_real_hook_rotates_at_the_production_limit(host, home, tmp_path):
    path = tmp_path / f"{host}-audit.jsonl"
    with path.open("wb") as stream:
        stream.truncate(PRODUCTION_LIMIT)  # sparse: the size counts, not the content
    path.chmod(0o600)
    prompt(host, home, REFUSED_URL, audit=path)
    rotated = path.with_name(path.name + ".1")
    assert rotated.stat().st_size == PRODUCTION_LIMIT
    assert [json.loads(line)["event"] for line in path.read_text().splitlines()] == ["started", "outcome"]


@pytest.mark.parametrize("host", list(HOSTS))
def test_hard_linked_log_is_neither_written_nor_rotated(host, home, tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("untouched\n")
    os.link(path, tmp_path / "elsewhere")
    prompt(host, home, REFUSED_URL, audit=path)
    assert (tmp_path / "elsewhere").read_text() == path.read_text() == "untouched\n"
    assert not path.with_name(path.name + ".1").exists()


def test_concurrent_rotation_preserves_the_previous_generation(monkeypatch, tmp_path):
    """Pause A before rename; B must not replace its generation using stale evidence."""
    import threading

    fcntl = pytest.importorskip("fcntl")
    core = load_core()
    path = tmp_path / "audit.jsonl"
    old = b"old\n" * 250
    path.write_bytes(old)
    path.chmod(0o600)
    first, second = core.InvocationAudit(host="cc"), core.InvocationAudit(host="cc")
    first.max_bytes = second.max_bytes = len(old)
    first.lock_wait_seconds = second.lock_wait_seconds = 1  # allow the explicit thread handoff
    before_rename = threading.Event()
    contender_done_or_blocked = threading.Event()
    errors = []
    replace, flock = os.replace, fcntl.flock

    def pause_first_rename(src, dst):
        if src == path and threading.current_thread().name == "audit-rotator":
            before_rename.set()
            if not contender_done_or_blocked.wait(3):
                raise TimeoutError("contender neither completed nor met the file lock")
        return replace(src, dst)

    def observe_contention(fd, flags):
        try:
            return flock(fd, flags)
        except BlockingIOError:
            if threading.current_thread().name == "audit-contender":
                contender_done_or_blocked.set()
            raise

    def append(audit, line):
        try:
            audit._append(path, line)
        except Exception as error:
            errors.append(error)
        finally:
            if threading.current_thread().name == "audit-contender":
                contender_done_or_blocked.set()

    monkeypatch.setattr(os, "replace", pause_first_rename)
    monkeypatch.setattr(fcntl, "flock", observe_contention)
    a = threading.Thread(target=append, args=(first, b"A\n"), name="audit-rotator")
    b = threading.Thread(target=append, args=(second, b"B\n"), name="audit-contender")
    a.start()
    try:
        assert before_rename.wait(3), "first writer never reached rotation"
        b.start()
    finally:
        a.join(4)
        if b.ident is not None:
            b.join(4)
    assert not a.is_alive() and not b.is_alive(), "audit writers must remain bounded"
    assert not errors, errors
    assert path.with_name(path.name + ".1").read_bytes() == old
    assert sorted(path.read_bytes().splitlines()) == [b"A", b"B"]


def test_busy_audit_lock_gives_up_without_changing_the_log(monkeypatch, tmp_path):
    import time

    fcntl = pytest.importorskip("fcntl")
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"unchanged\n")
    monkeypatch.setenv("AITEAM_UNREAD_AUDIT_PATH", str(path))
    audit = load_core().InvocationAudit(host="cc")
    with path.open("ab") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        audit.record("outcome", "no_notice")
        elapsed = time.monotonic() - started
    assert elapsed < 0.5, "audit lock contention must never stall a prompt"
    assert path.read_bytes() == b"unchanged\n"
    assert not path.with_name(path.name + ".1").exists()


def test_rotation_restricts_existing_permissions_before_archiving(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"old\n")
    path.chmod(0o644)
    monkeypatch.setenv("AITEAM_UNREAD_AUDIT_PATH", str(path))
    audit = load_core().InvocationAudit(host="cc")
    audit.max_bytes = path.stat().st_size
    audit.record("outcome", "no_notice")
    assert path.with_name(path.name + ".1").stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
