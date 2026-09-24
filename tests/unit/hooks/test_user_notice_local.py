"""user_notice local records, session dedup and the ledger fetch (design §5.6, §5.7).

The fetch runs against a fake API that validates the body with the production
request model; the records it imports are checked on the request that carried
them and on the next one (import is idempotent, the offset only moves after a
successful answer).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from ._notice_fakes import REFUSED_URL, FakeApi


@pytest.fixture()
def un(tmp_path, monkeypatch):
    module = sys.modules["user_notice"]
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return module


def _claim(un, key="api_down", session="s1", event="SessionStart:startup", catalog_id="api_down", **kw):
    return un.claim_local(catalog_id, {}, host="cc", session_id=session, cwd="/tmp", event=event, key=key, **kw)


def test_record_is_one_small_line_with_identity(un):
    un.record_local("cc", "local_notice", catalog_id="api_down", key="api_down", params={"x": "y" * 5000},
                    session_id="s")
    (record,) = un._tail_records("cc")
    assert len(record["uuid"]) == 32 and record["kind"] == "local_notice"
    assert record["params"] == {} and record["params_dropped"] is True
    assert record["at"].endswith("Z")
    raw = un._records_path("cc").read_bytes()
    assert raw.count(b"\n") == 1 and len(raw) <= 1025


def test_concurrent_appends_never_interleave(un):
    """48 writers at once: every line stays a whole JSON object (O_APPEND, one write each)."""
    barrier = threading.Barrier(48)

    def writer(index: int) -> None:
        barrier.wait()
        for step in range(20):
            un.record_local("cc", "local_notice", catalog_id="api_down", key=f"k{index}-{step}",
                            session_id="s", params={"pad": "宽" * 90})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(48)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = un._records_path("cc").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 48 * 20
    assert len({json.loads(line)["key"] for line in lines}) == 48 * 20


def test_once_per_session_and_per_key(un):
    assert _claim(un) is not None
    assert _claim(un) is None
    assert _claim(un, session="s2") is not None
    assert _claim(un, key="api_down:other") is not None


def test_events_filter_and_local_clear(un):
    assert _claim(un, event="SessionStart:resume", reliable=False) is not None
    # A resume line may not have shown: the prompt exit only counts reliable ones.
    reliable = ("UserPromptSubmit", "SessionStart:startup")
    assert un.seen_local("cc", "s1", "api_down", events=reliable) is False
    assert _claim(un, event="UserPromptSubmit", events=reliable) is not None
    assert _claim(un, event="UserPromptSubmit", events=reliable) is None
    un.clear_local("cc", "api_down")
    assert _claim(un, event="UserPromptSubmit", events=reliable) is not None


def test_immediate_lines_cap_at_five_per_session_but_are_recorded(un):
    shown = [
        un.claim_local("blocked_secret_add", {"file": f"f{i}.env"}, host="cc", session_id="s", cwd="/tmp",
                       event="PreToolUse", key=f"blocked_secret_add:s:{i}", immediate=True)
        for i in range(7)
    ]
    assert [bool(item) for item in shown] == [True] * 5 + [False] * 2
    records = [r for r in un._tail_records("cc") if r["kind"] == "local_notice"]
    assert [r["displayed"] for r in records] == [True] * 5 + [False] * 2
    other = un.claim_local("blocked_secret_add", {"file": "x.env"}, host="cc", session_id="other", cwd="/tmp",
                           event="PreToolUse", key="blocked_secret_add:other:x", immediate=True)
    assert other is not None, "the cap is per session"


def test_emit_block_writes_one_red_line_and_returns_the_note(un, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    note = un.emit_block("blocked_secret_add", {"file": "config/.env"}, session_id="s", cwd="/tmp")
    doc = json.loads(capsys.readouterr().out)
    line = "[AI Team OS] 已拦截这条 git add：含敏感文件 config/.env，命令未执行"
    assert doc == {"systemMessage": line.replace("] ", "] \x1b[31m", 1) + "\x1b[39m"}
    assert note == "\n用户界面已显示：" + line
    un._WROTE_DOCUMENT = False
    assert un.emit_block("blocked_secret_add", {"file": "config/.env"}, session_id="s", cwd="/tmp") == ""
    assert capsys.readouterr().out == "", "the same block in the same session shows once"


def test_install_state_progress_window(un):
    now = time.time()
    assert un.install_in_progress({"phase": "installing", "started_at": now - 10}, now)
    assert not un.install_in_progress({"phase": "installing", "started_at": now - 400}, now)
    assert not un.install_in_progress({"phase": "failed", "started_at": now}, now)
    assert not un.install_in_progress({"phase": "installing", "started_at": "garbage"}, now)


# ---------------------------------------------------------------------------
# fetch_pending
# ---------------------------------------------------------------------------


def _payload(**extra):
    return {"session_id": "sess-1", "cwd": "/tmp/project", "transcript_path": "/tmp/t.jsonl", **extra}


def test_fetch_sends_a_valid_request_with_local_records(un, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    _claim(un, session="sess-0")
    un.record_local("cc", "emitted", delivery_ids=["d-9"], event="SessionStart")
    with FakeApi() as api:
        api.pending = [{"language": "zh", "user_text": "[AI Team OS] x", "model_text": "m", "delivery_ids": ["d-1"]}]
        monkeypatch.setenv("AITEAM_API_URL", api.url)
        got = un.fetch_pending("cc", "SessionStart", "resume", _payload(), timeout=2.0)
        (body,) = api.pending_bodies()
    assert (got.language, got.user_text, got.model_text, got.delivery_ids) == ("zh", "[AI Team OS] x", "m", ["d-1"])
    assert body["host"] == "cc" and body["event"] == "SessionStart" and body["source"] == "resume"
    assert body["session_id"] == "sess-1" and body["transcript_path"] == "/tmp/t.jsonl"
    assert body["facts"]["entrypoint"] == "cli" and body["facts"]["fallback_language"] == "zh"
    assert body["facts"]["emitted"] == ["d-9"]
    kinds = [record["kind"] for record in body["facts"]["local_records"]]
    assert kinds == ["local_notice", "emitted"]


def test_records_are_sent_once_and_only_after_a_good_answer(un, monkeypatch):
    _claim(un)
    with FakeApi() as api:
        monkeypatch.setenv("AITEAM_API_URL", api.url)
        api.pending_status = 500
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0) is None
        assert un.last_failure() == "error"
        api.pending_status = 200
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0) is not None
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0) is not None
        sent = [len(body["facts"]["local_records"]) for body in api.pending_bodies()]
    assert sent == [1, 1, 0], "resent after the failure, never after the success"


def test_import_is_bounded_per_request(un, monkeypatch):
    for index in range(450):
        un.record_local("cc", "local_notice", catalog_id="api_down", key=f"api_down:{index}", session_id="s")
    with FakeApi() as api:
        monkeypatch.setenv("AITEAM_API_URL", api.url)
        for _ in range(4):
            un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0)
        sent = [len(body["facts"]["local_records"]) for body in api.pending_bodies()]
    assert sent == [200, 200, 50, 0]


def test_rotation_keeps_every_record_importable(un, monkeypatch):
    monkeypatch.setattr(un, "_ROTATE_BYTES", 2000)
    with FakeApi() as api:
        monkeypatch.setenv("AITEAM_API_URL", api.url)
        for index in range(12):
            un.record_local("cc", "local_notice", catalog_id="api_down", key=f"api_down:{index}", session_id="s",
                            params={"pad": "x" * 150})
        un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0)
        assert un._records_path("cc").with_name("notice-local.cc.jsonl.1").exists(), "rotated once imported"
        un.record_local("cc", "local_notice", catalog_id="api_down", key="api_down:new", session_id="s")
        un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0)
        sent = [[r["key"] for r in body["facts"]["local_records"]] for body in api.pending_bodies()]
    assert len(sent[0]) == 12 and sent[1] == ["api_down:new"]
    # Session dedup still sees the rotated generation.
    assert un.seen_local("cc", "s", "api_down:3")


@pytest.mark.parametrize(("setup", "reason"), [
    pytest.param("refused", "unreachable", id="refused"),
    pytest.param("404", "unsupported", id="old-api"),
    pytest.param("405", "unsupported", id="method"),
    pytest.param("500", "error", id="fault"),
    pytest.param("garbage", "error", id="not-json"),
])
def test_failures_are_classified_and_never_raise(un, monkeypatch, setup, reason):
    if setup == "refused":
        monkeypatch.setenv("AITEAM_API_URL", REFUSED_URL)
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=1.0) is None
        assert un.last_failure() == reason
        return
    with FakeApi() as api:
        monkeypatch.setenv("AITEAM_API_URL", api.url)
        if setup == "garbage":
            # An answer that is JSON but not a response object.
            monkeypatch.setattr(un, "api_url", lambda: api.url + "/garbage")
            api.routes[("POST", "/garbage/api/notices/pending")] = lambda body: (200, ["list"])
        else:
            api.pending_status = int(setup)
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0) is None
    assert un.last_failure() == reason


def test_timeout_is_its_own_failure(un, monkeypatch):
    with FakeApi() as api:
        api.routes[("POST", "/slow/api/notices/pending")] = lambda body: (time.sleep(1.5), (200, {}))[1]
        monkeypatch.setattr(un, "api_url", lambda: api.url + "/slow")
        started = time.monotonic()
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=0.3) is None
        assert time.monotonic() - started < 1.4
    assert un.last_failure() == "timeout"


def test_reaching_the_api_clears_the_service_down_mark(un, monkeypatch):
    assert _claim(un) is not None
    un.mark_api_down("cc")
    assert _claim(un) is None
    with FakeApi() as api:
        monkeypatch.setenv("AITEAM_API_URL", api.url)
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0) is not None
    assert not (un.os_data_dir() / "notice-api-down.cc").exists()
    assert _claim(un) is not None, "down again later: the line shows again"


def test_api_url_honours_the_environment_and_port_file(tmp_path, monkeypatch):
    """The real resolution order (the test-wide guard replaces api_url, so load a fresh copy)."""
    import importlib.util

    from ._notice_fakes import PLUGIN_HOOKS

    spec = importlib.util.spec_from_file_location("user_notice_fresh", PLUGIN_HOOKS / "user_notice.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AITEAM_API_URL", raising=False)
    assert fresh.api_url() == "http://localhost:8000"
    port_file = tmp_path / ".claude" / "data" / "ai-team-os" / "api_port.txt"
    port_file.parent.mkdir(parents=True)
    port_file.write_text("8123\n")
    assert fresh.api_url() == "http://localhost:8123"
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:18731/")
    assert fresh.api_url() == "http://127.0.0.1:18731"
    assert os.environ["AITEAM_API_URL"].endswith("/")
