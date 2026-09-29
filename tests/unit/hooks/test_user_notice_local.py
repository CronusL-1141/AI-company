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


def _switch(un, index: int, session: str = "s"):
    return un.claim_local("branch_switched", {"repo": "r", "ob": f"b{index}", "nb": "main"}, host="cc",
                          session_id=session, cwd="/tmp", event="PreToolUse",
                          key=f"branch_switched:x:b{index}:main", immediate=True)


def test_immediate_lines_cap_at_five_per_session_but_are_recorded(un):
    shown = [_switch(un, i) for i in range(7)]
    assert [bool(item) for item in shown] == [True] * 5 + [False] * 2
    records = [r for r in un._tail_records("cc") if r["kind"] == "local_notice"]
    assert [r["displayed"] for r in records] == [True] * 5 + [False] * 2
    assert _switch(un, 0, session="other") is not None, "the cap is per session"


def test_block_reasons_neither_hit_nor_fill_the_cap(un, capsys):
    """CC shows a reason with every PreToolUse block, so the cap never silences one,
    and blocks do not use up the budget of the capped lines either."""
    for index in range(7):
        un._WROTE_DOCUMENT = False
        assert un.emit_block("blocked_secret_add", {"file": f"f{index}.env"}, session_id="s", cwd="/tmp",
                             model_text="[OS BLOCK] x") is True
        reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
        assert f"f{index}.env" in reason
    assert [bool(_switch(un, i)) for i in range(6)] == [True] * 5 + [False]


def test_emit_block_denies_with_the_plain_line_every_time_and_records_once(un, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")  # the host colours the reason, not the hook
    expected = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "[OS BLOCK] secret",
        "permissionDecision": "deny",
        "permissionDecisionReason": "[AI Team OS] 已拦截这条 git add：含敏感文件 config/.env，命令未执行",
    }}
    for _ in range(2):
        un._WROTE_DOCUMENT = False
        assert un.emit_block("blocked_secret_add", {"file": "config/.env"}, session_id="s", cwd="/tmp",
                             model_text="[OS BLOCK] secret") is True
        assert json.loads(capsys.readouterr().out) == expected, "the same block states its reason again"
    records = [r for r in un._tail_records("cc") if r["kind"] == "local_notice"]
    assert len(records) == 1, "the Dashboard record is written once per key and session"
    assert records[0]["key"] == "blocked_secret_add:s:" + un.sha8("config/.env")
    assert records[0]["displayed"] is True and records[0]["event"] == "PreToolUse"


def test_emit_block_speaks_the_session_language(un, capsys, monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    un.emit_block("blocked_dispatch_model", {}, session_id="s", cwd="/tmp", variant="no_reason")
    reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == ("[AI Team OS] Blocked a dispatch: a fable or fork dispatch gave no reason. "
                      "Claude must add it and dispatch again")


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


_RENDERER_ENV = ("CLAUDE_CODE_NO_FLICKER", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN", "CLAUDE_AX_SCREEN_READER")
_LATCH = '{"numStartups": 3, "fullscreenAutoDisabled": {"version": "2.1.281", "at": 1}}'


@pytest.mark.parametrize(
    ("env", "global_config", "expected"),
    [
        pytest.param({}, "{}", "", id="settings-decide"),
        pytest.param({}, _LATCH, "default", id="crash-latch"),
        pytest.param({}, None, "default", id="global-config-missing"),
        pytest.param({}, "{not json", "default", id="global-config-broken"),
        pytest.param({"CLAUDE_CODE_NO_FLICKER": "1"}, "{}", "fullscreen", id="no-flicker-on"),
        pytest.param({"CLAUDE_CODE_NO_FLICKER": "1"}, _LATCH, "fullscreen", id="no-flicker-beats-the-latch"),
        pytest.param({"CLAUDE_CODE_NO_FLICKER": "0"}, "{}", "default", id="no-flicker-off"),
        pytest.param({"CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1"}, "{}", "default", id="alt-screen-off"),
        pytest.param({"CLAUDE_CODE_NO_FLICKER": "1", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1"}, "{}",
                     "default", id="both-set-means-off"),
        pytest.param({"CLAUDE_AX_SCREEN_READER": "1", "CLAUDE_CODE_NO_FLICKER": "1"}, "{}", "default",
                     id="screen-reader-first"),
        pytest.param({"CLAUDE_CODE_NO_FLICKER": "maybe"}, "{}", "", id="unparsable"),
    ],
)
def test_the_session_renderer_override_is_reported(un, monkeypatch, tmp_path, env, global_config, expected):
    """The API decides whether a /clear line was painted; only the hook sees these (CC 2.1.281 rl() order)."""
    for name in _RENDERER_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if global_config is not None:
        (home / ".claude.json").write_text(global_config, encoding="utf-8")
    assert un.tui_env() == expected
    with FakeApi() as api:  # validates the body with the production request model
        monkeypatch.setenv("AITEAM_API_URL", api.url)
        assert un.fetch_pending("cc", "UserPromptSubmit", "", _payload(), timeout=2.0) is not None
        assert un.fetch_pending("codex", "UserPromptSubmit", "", _payload(), timeout=2.0) is not None
        cc, codex = api.pending_bodies()
    assert cc["facts"]["tui_env"] == expected and codex["facts"]["tui_env"] == ""


def test_the_crash_latch_is_read_where_claude_code_keeps_it(un, monkeypatch, tmp_path):
    """CLAUDE_CONFIG_DIR moves the global config to <dir>/.claude.json, like the settings."""
    for name in _RENDERER_ENV:
        monkeypatch.delenv(name, raising=False)
    home, config_dir = tmp_path / "home", tmp_path / "cfg"
    home.mkdir(exist_ok=True)
    config_dir.mkdir()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (config_dir / ".claude.json").write_text(_LATCH, encoding="utf-8")
    assert un.tui_env() == ""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert un.tui_env() == "default"
    (config_dir / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".claude.json").write_text(_LATCH, encoding="utf-8")
    assert un.tui_env() == ""


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


# ---------------------------------------------------------------------------
# Owed start (a session start that could not reach the API)
# ---------------------------------------------------------------------------


def test_an_owed_start_is_kept_per_host_and_session_and_settled_once(un):
    un.mark_start_owed("cc", "s1", "startup")
    un.mark_start_owed("codex", "s1", "resume")
    assert un.start_owed("cc", "s1")["source"] == "startup"
    assert un.start_owed("codex", "s1")["source"] == "resume"
    assert un.start_owed("cc", "s2") is None and un.start_owed("cc", "") is None
    un.mark_start_owed("cc", "s1", "compact")
    assert un.start_owed("cc", "s1")["source"] == "compact", "a later start of the session replaces it"
    assert un.claim_start_owed("cc", "s1") is True
    assert un.claim_start_owed("cc", "s1") is False, "exactly one caller delivers it"
    assert un.start_owed("cc", "s1") is None and un.start_owed("codex", "s1") is not None
    un.mark_start_owed("cc", "", "startup")
    assert not list((un.os_data_dir() / "start-owed").glob("cc.*.json")), "no session, nothing to owe"


def test_only_a_start_that_launched_the_host_counts_as_starting_and_only_for_the_grace(un):
    now = 1_000_000.0
    for source in ("startup", "resume", "fork"):
        assert un.still_starting({"source": source, "at": now}, now=now + un.STARTING_GRACE_S - 1)
        assert not un.still_starting({"source": source, "at": now}, now=now + un.STARTING_GRACE_S)
        assert not un.still_starting({"source": source, "at": now}, now=now - un.STARTING_GRACE_S), (
            "a clock set far back does not hold E01 off")
    for source in ("clear", "compact"):
        assert not un.still_starting({"source": source, "at": now}, now=now)


def test_a_damaged_or_foreign_owed_file_is_ignored_and_old_ones_are_swept(un):
    un.mark_start_owed("cc", "s1", "startup")
    (path,) = (un.os_data_dir() / "start-owed").glob("cc.*.json")
    for bad in ("{", json.dumps({"session_id": "other", "source": "startup", "at": 1.0}),
                json.dumps({"session_id": "s1", "source": "startup", "at": True})):
        path.write_text(bad, encoding="utf-8")
        assert un.start_owed("cc", "s1") is None
    stale = time.time() - 8 * 24 * 3600
    os.utime(path, (stale, stale))
    un.mark_start_owed("cc", "s2", "startup")
    assert not path.exists(), "a week-old owed start of a session that never prompted is dropped"


def test_an_owed_start_that_keeps_running_out_of_time_is_given_up(un):
    un.mark_start_owed("cc", "s1", "startup")
    for _ in range(un.OWED_ATTEMPTS - 1):
        assert un.miss_start_owed("cc", "s1") is False
        assert un.start_owed("cc", "s1") is not None
    assert un.miss_start_owed("cc", "s1") is True
    assert un.start_owed("cc", "s1") is None, "later prompts are ordinary ones"
    assert un.miss_start_owed("cc", "s1") is False, "nothing left to count"
    un.mark_start_owed("cc", "s1", "compact")
    assert un.miss_start_owed("cc", "s1") is (un.OWED_ATTEMPTS == 1), "a new start owes afresh"
