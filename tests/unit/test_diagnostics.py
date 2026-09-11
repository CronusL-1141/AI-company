"""Local diagnostic sink safety and configuration evidence."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aiteam import diagnostics


@pytest.fixture
def diagnostic_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "1")
    yield tmp_path
    assert diagnostics.flush_diagnostics(timeout=2)
    with diagnostics._lock:
        if diagnostics._handler:
            diagnostics._handler.close()
        diagnostics._handler = None
        diagnostics._handler_key = None


def test_private_json_lines_and_sensitive_fields(diagnostic_dir):
    diagnostics.record_event(
        "test.safety", url="http://user:pass-canary@localhost:8000/api/health?key=query-canary#fragment-canary",
        authorization="auth-canary", cmdline=["cmdline-canary"], body="body-canary",
        response_headers={"server": "test", "set-cookie": "cookie-canary"},
    )
    assert diagnostics.flush_diagnostics(timeout=2)
    file = next(diagnostic_dir.glob("*.jsonl"))
    content = file.read_text()
    item = json.loads(content)
    assert item["event"] == "test.safety"
    assert item["pid"] == os.getpid()
    assert item["ppid"] == os.getppid()
    assert item["timestamp"].endswith("+00:00")
    assert item["url"] == "http://localhost:8000/api/health"
    assert "canary" not in content
    if os.name != "nt":
        assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_sink_rotates_and_caps_individual_values(diagnostic_dir, monkeypatch):
    monkeypatch.setattr(diagnostics, "_MAX_BYTES", 1500)
    for index in range(20):
        diagnostics.record_event("test.rotation", index=index, reason="r" * 2000)
    assert diagnostics.flush_diagnostics(timeout=2)
    files = list(diagnostic_dir.glob("runtime-*"))
    assert len(files) == diagnostics._BACKUP_COUNT + 1
    for file in files:
        for line in file.read_text().splitlines():
            assert len(json.loads(line)["reason"]) == 1024


def test_concurrent_writes_are_complete_json_lines(diagnostic_dir):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: diagnostics.record_event("test.concurrent", index=index), range(80)))
    assert diagnostics.flush_diagnostics(timeout=2)
    items = [json.loads(line) for file in diagnostic_dir.glob("*.jsonl") for line in file.read_text().splitlines()]
    assert {item["index"] for item in items} == set(range(80))


def test_disabled_sink_writes_nothing(diagnostic_dir, monkeypatch):
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    diagnostics.record_event("test.disabled")
    assert list(diagnostic_dir.iterdir()) == []


def test_failed_proxy_inspection_is_best_effort(monkeypatch):
    def unavailable():
        raise RuntimeError("do-not-log-exception-message-canary")

    monkeypatch.setattr(diagnostics.urllib.request, "getproxies", unavailable)
    assert diagnostics.proxy_snapshot("http://localhost") == {"snapshot_error_type": "RuntimeError"}


def test_proxy_snapshot_redacts_credentials_and_reports_environment_presence(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://name:env-password-canary@127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,private-domain-canary")
    monkeypatch.setattr(diagnostics.urllib.request, "getproxies", lambda: {
        "http": "http://name:proxy-password-canary@127.0.0.1:7897",
        "no": "localhost,private-domain-canary",
    })
    monkeypatch.setattr(
        diagnostics.urllib.request, "proxy_bypass", lambda host: host in {"localhost", "localhost:8000"},
    )
    item = diagnostics.proxy_snapshot("http://localhost:8000/api/health")
    assert item["proxy_env_present"]["HTTP_PROXY"] is True
    assert item["target_bypass"] is True
    assert item["proxies"]["http"] == {"scheme": "http", "host": "127.0.0.1", "port": 7897}
    assert item["evidence_kind"] == "configuration_snapshot_not_route_proof"
    assert "canary" not in json.dumps(item)


@pytest.mark.parametrize("url, expected", [
    ("http://u:p@[::1]:8000/health?secret=yes#value", "http://[::1]:8000/health"),
    ("http://host:bad/", "<invalid-url>"),
])
def test_url_redaction_handles_ipv6_and_invalid_ports(url, expected):
    assert diagnostics.safe_url(url) == expected


def test_failed_response_inspection_preserves_known_status():
    class Response:
        status = 502

        def geturl(self):
            raise RuntimeError("private-message-canary")

    item = diagnostics.response_snapshot(Response())
    assert item["status"] == 502
    assert item["snapshot_error_type"] == "RuntimeError"
    assert "canary" not in json.dumps(item)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_existing_public_directory_is_rejected(diagnostic_dir):
    diagnostic_dir.chmod(0o755)
    diagnostics.record_event("test.public_directory")
    assert diagnostics.flush_diagnostics(timeout=2)
    assert list(diagnostic_dir.iterdir()) == []
    assert stat.S_IMODE(diagnostic_dir.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_existing_public_file_is_not_appended(diagnostic_dir):
    file = diagnostic_dir / f"runtime-{os.getpid()}.jsonl"
    file.write_text("existing-content")
    file.chmod(0o644)
    diagnostics.record_event("test.public_file")
    assert diagnostics.flush_diagnostics(timeout=2)
    assert file.read_text() == "existing-content"


def test_full_queue_drops_without_blocking_and_reports_count(diagnostic_dir, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = diagnostics._write_record

    def blocked(*args):
        entered.set()
        assert release.wait(timeout=5)
        original(*args)

    monkeypatch.setattr(diagnostics, "_write_record", blocked)
    diagnostics.record_event("test.queue_blocker")
    assert entered.wait(timeout=1)
    try:
        for index in range(diagnostics._QUEUE_SIZE + 10):
            diagnostics.record_event("test.queue_fill", index=index)
        assert diagnostics._dropped >= 10
        assert diagnostics.flush_diagnostics(timeout=0.01) is False
    finally:
        release.set()
    assert diagnostics.flush_diagnostics(timeout=3)
    diagnostics.record_event("test.queue_recovery")
    assert diagnostics.flush_diagnostics(timeout=2)
    records = [json.loads(line) for file in diagnostic_dir.glob("*.jsonl") for line in file.read_text().splitlines()]
    assert records[-1]["event"] == "test.queue_recovery"
    assert records[-1]["dropped_before"] >= 10


def _isolated_probe(tmp_path, code):
    return subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
             "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
             "AITEAM_DIAGNOSTICS_ENABLED": "1", "AITEAM_DIAGNOSTICS_DIR": str(tmp_path / "events")},
        capture_output=True, text=True, timeout=4,
    )


def test_logging_shutdown_does_not_wait_for_diagnostic_writer(tmp_path):
    result = _isolated_probe(tmp_path, """
import logging, threading
from aiteam import diagnostics as d
d.record_event('test.ready')
assert d.flush_diagnostics(1)
entered = threading.Event()
release = threading.Event()
def blocked(line):
    entered.set()
    release.wait(30)
d._handler.write = blocked
d.record_event('test.blocked')
assert entered.wait(1)
assert not d.flush_diagnostics(0.01)
logging.shutdown()
print('shutdown-returned', flush=True)
""")
    assert result.returncode == 0, result.stderr
    assert "shutdown-returned" in result.stdout


def test_reentrant_initialization_keeps_one_writer_and_valid_pending_count(tmp_path):
    result = _isolated_probe(tmp_path, """
import json, threading
from pathlib import Path
from aiteam import diagnostics as d
original = d.os.getpid
fired = False
def reentrant_pid():
    global fired
    if d._initializing and not fired:
        fired = True
        d.record_event('test.reentrant')
    return original()
d.os.getpid = reentrant_pid
d.record_event('test.outer')
assert d.flush_diagnostics(1)
assert fired
assert d._pending == 0, d._pending
assert len([t for t in threading.enumerate() if t.name == 'aiteam-diagnostics']) == 1
records = [json.loads(line) for f in Path(d.os.environ['AITEAM_DIAGNOSTICS_DIR']).glob('*.jsonl')
           for line in f.read_text().splitlines()]
assert len(records) == 1, records
assert records[0]['event'] == 'test.outer'
assert records[0]['dropped_before'] == 1
print('initialization-safe', flush=True)
""")
    assert result.returncode == 0, result.stderr
    assert "initialization-safe" in result.stdout
