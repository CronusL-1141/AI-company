"""Request-level ownership/coverage checks; all logs are isolated fixtures."""

import asyncio
import json
from pathlib import Path

import pytest

from aiteam.services import codex_rollout_usage as reader


def usage(i=100, o=20, c=60, w=0, r=5):
    return dict(
        input_tokens=i,
        output_tokens=o,
        cached_input_tokens=c,
        cache_write_input_tokens=w,
        reasoning_output_tokens=r,
        total_tokens=i + o,
    )


def meta(sid="s", fork=None, child=False, at=0):
    return dict(
        timestamp=f"2026-09-30T00:00:{at:02d}Z",
        type="session_meta",
        payload=dict(
            id=sid,
            forked_from_id=fork,
            source={"subagent": {"thread_spawn": {"parent_thread_id": "p"}}} if child else "cli",
        ),
    )


def ledger(rid="r", sid="s", at=1, raw=None, total=None):
    p = dict(response_id=rid, thread_id=sid, usage=raw or usage())
    if total is not None:
        p["thread_token_usage"] = total
    return dict(timestamp=f"2026-09-30T00:00:{at:02d}Z", type="token_usage_record", payload=p)


def old(total=None, last=None, at=2):
    return dict(
        timestamp=f"2026-09-30T00:00:{at:02d}Z",
        type="event_msg",
        payload=dict(type="token_count", info=dict(total_token_usage=total or usage(), last_token_usage=last)),
    )


def log(home, rows, sid="s", archived=False, tail=b""):
    folder = home / ("archived_sessions" if archived else "sessions")
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"rollout-date-{sid}.jsonl"
    p.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + tail)
    return p


def read(home, path=None, sid="s"):
    return reader.parse_codex_rollout_usage(path, expected_session_id=sid, codex_home=home)


def test_owned_ledger_duplicate_legacy_and_reasoning_no_double_count(tmp_path):
    row = ledger(total=usage())
    p = log(tmp_path, [meta(), row, row, old(last=usage()), old(last=usage())])
    u = read(tmp_path, p)
    assert {
        k: u[k]
        for k in [
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_output_tokens",
            "api_calls",
        ]
    } == dict(
        input_tokens=40,
        output_tokens=20,
        cache_read_tokens=60,
        cache_creation_tokens=0,
        reasoning_output_tokens=5,
        api_calls=1,
    )


def test_inherited_ledger_and_parent_meta_do_not_change_owner(tmp_path):
    p = log(tmp_path, [meta(fork="p"), meta("p"), ledger("parent", "p"), ledger(total=usage()), old(last=usage())])
    assert read(tmp_path, p)["api_calls"] == 1
    assert read(tmp_path, p)["diagnostics"]["inherited_records_skipped"] == 1


@pytest.mark.parametrize(
    "case,reason",
    [
        ("duplicate_conflict", "conflict"),
        ("prefix_missing", "ledger_prefix_missing"),
        ("old_uncovered", "mixed_uncovered_legacy"),
        ("cache_overlap", "invalid_log"),
        ("missing_cache", "missing_fields"),
        ("child_no_owner", "unidentified_owner"),
    ],
    ids=[
        "duplicate-conflict",
        "missing-prefix",
        "uncovered-history",
        "cache-overlap",
        "missing-field",
        "unknown-owner",
    ],
)
def test_refuse_incomplete_or_conflicting_measurement(tmp_path, case, reason):
    rows = [meta(), ledger()]
    if case == "duplicate_conflict":
        rows.append(ledger(raw=usage(o=30)))
    if case == "prefix_missing":
        rows = [meta(), ledger(total=usage(i=200))]
    if case == "old_uncovered":
        rows.append(old(last=usage(i=101)))
    if case == "cache_overlap":
        rows = [meta(), ledger(raw=usage(w=50))]
    if case == "missing_cache":
        rows[1]["payload"]["usage"].pop("cached_input_tokens")
    if case == "child_no_owner":
        rows[0] = meta(child=True)
        rows[1]["payload"].pop("thread_id")
    log(tmp_path, rows)
    with pytest.raises(reader.CodexRolloutUsageError, match=reason):
        read(tmp_path)


def test_wire_missing_is_none_and_measured_zero_is_real(tmp_path):
    raw = usage(i=0, o=0, c=0, r=0)
    raw.pop("cache_write_input_tokens")
    raw.pop("reasoning_output_tokens")
    log(tmp_path, [meta(), ledger(raw=raw)])
    u = read(tmp_path)
    assert u["input_tokens"] == u["output_tokens"] == 0
    assert u["cache_creation_tokens"] is u["reasoning_output_tokens"] is None


def test_legacy_cumulative_dedup_provider_independent(tmp_path):
    log(
        tmp_path,
        [meta(), old(last=usage()), old(last=usage()), old(total=usage(i=200, o=40, c=120, r=10), last=usage(), at=3)],
    )
    assert read(tmp_path)["api_calls"] == 2
    assert read(tmp_path)["input_tokens"] == 80


def test_legacy_rollback_rejected_but_covered_ledger_reset_ok(tmp_path):
    rows = [meta(), old(last=usage()), old(total=usage(i=90), last=usage(i=90), at=4)]
    p = log(tmp_path, rows)
    with pytest.raises(reader.CodexRolloutUsageError, match="rollback"):
        read(tmp_path, p)
    log(tmp_path, [meta(), ledger(), old(last=usage()), ledger("r2", at=3, raw=usage(i=90)), rows[-1]])
    assert read(tmp_path)["api_calls"] == 2


def test_fork_none_child_and_true_fork_parent_prefix(tmp_path):
    log(tmp_path, [meta(child=True), old(last=usage())])
    assert read(tmp_path)["api_calls"] == 1
    log(tmp_path, [meta("p"), old(last=usage())], sid="p")
    log(
        tmp_path,
        [meta(fork="p", at=3), old(last=usage()), old(total=usage(i=200, o=40, c=120, r=10), last=usage(), at=4)],
    )
    assert read(tmp_path)["api_calls"] == 1
    assert read(tmp_path)["input_tokens"] == 40


def test_fork_parent_missing_and_unknown_import(tmp_path):
    log(tmp_path, [meta(fork="p"), old(last=usage())])
    with pytest.raises(reader.CodexRolloutUsageError, match="fork_parent_missing"):
        read(tmp_path)
    log(tmp_path, [meta(), {"type": "turn_context", "payload": {"turn_id": "external-import-turn-0"}}, ledger()])
    with pytest.raises(reader.CodexRolloutUsageError, match="import_ambiguous"):
        read(tmp_path)


def test_archived_wrong_path_identity_and_conflicting_copy(tmp_path):
    p = log(tmp_path, [meta(), ledger()], archived=True)
    assert read(tmp_path, tmp_path / "sessions/gone.jsonl")["transcript_path"] == str(p)
    wrong = log(tmp_path, [meta("other"), ledger(sid="other")], sid="other")
    assert read(tmp_path, wrong)["session_id"] == "s"
    log(tmp_path, [meta(), ledger(raw=usage(i=200))])
    with pytest.raises(reader.CodexRolloutUsageError, match="conflicting_rollouts"):
        read(tmp_path)


def test_partial_tail_no_data_and_symlink_refusal(tmp_path):
    p = log(tmp_path, [meta()], tail=b"{broken")
    assert read(tmp_path, p) is None
    p.unlink()
    p.symlink_to(log(tmp_path, [meta("p"), ledger(sid="p")], sid="p"))
    with pytest.raises(reader.CodexRolloutUsageError, match="unreadable"):
        read(tmp_path)


async def test_async_subprocess_real_reader(tmp_path):
    p = log(tmp_path, [meta(), ledger()])
    u = await reader.read_codex_rollout_usage(p, expected_session_id="s", codex_home=tmp_path)
    assert u["input_tokens"] == 40


async def test_found_rollout_without_usage_is_told_apart_from_no_rollout(tmp_path):
    # Only the first is a verdict about usage; the second says nothing about it.
    meta_only = log(tmp_path, [meta()])
    assert reader.parse_codex_rollout_usage_state(meta_only, expected_session_id="s", codex_home=tmp_path) == (
        None,
        True,
    )
    assert reader.parse_codex_rollout_usage_state(None, expected_session_id="t", codex_home=tmp_path) == (None, False)
    assert await reader.read_codex_rollout_state(meta_only, expected_session_id="s", codex_home=tmp_path) == (
        None,
        True,
    )
    assert await reader.read_codex_rollout_state(None, expected_session_id="t", codex_home=tmp_path) == (None, False)
    measured, found = await reader.read_codex_rollout_state(
        log(tmp_path, [meta(), ledger()]), expected_session_id="s", codex_home=tmp_path
    )
    assert (measured["input_tokens"], found) == (40, True)


async def test_async_worker_timeout(tmp_path):
    log(tmp_path, [meta(), ledger()])
    with pytest.raises(TimeoutError):
        await reader.read_codex_rollout_usage(None, expected_session_id="s", codex_home=tmp_path, timeout=0.000001)


def test_frozen_native_import_fixture_is_not_fabricated_usage():
    root = Path(__file__).resolve().parents[2] / "fixtures/codex/rollouts/native"
    p = next(root.rglob("*019f8d58-610c*.jsonl"))
    with pytest.raises(reader.CodexRolloutUsageError, match="import_ambiguous"):
        reader.parse_codex_rollout_usage(p, expected_session_id="019f8d58-610c-7510-a008-154da4f89b91", codex_home=root)


@pytest.mark.parametrize("last_i", [100, 400], ids=["missing-request", "last-exceeds-delta"])
def test_legacy_interval_gap_never_marks_partial_usage_measured(tmp_path, last_i):
    log(tmp_path, [meta(), old(last=usage()), old(total=usage(i=300, o=60, c=180, r=15), last=usage(i=last_i), at=3)])
    with pytest.raises(reader.CodexRolloutUsageError, match="legacy_gap"):
        read(tmp_path)


def test_actual_self_cycle_and_two_thread_cycle_rejected(tmp_path):
    log(tmp_path, [meta(fork="s"), old(last=usage())])
    with pytest.raises(reader.CodexRolloutUsageError, match="fork_cycle"):
        read(tmp_path)
    log(tmp_path, [meta("p", fork="s"), old(last=usage())], sid="p")
    log(
        tmp_path,
        [meta(fork="p", at=3), old(last=usage()), old(total=usage(i=200, o=40, c=120, r=10), last=usage(), at=4)],
    )
    with pytest.raises(reader.CodexRolloutUsageError, match="fork_cycle"):
        read(tmp_path)


async def test_different_actor_workers_are_globally_bounded_and_waiter_cancellation_safe(monkeypatch):
    active = maximum = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(reader, "_WORKERS", asyncio.Semaphore(2))

    async def controlled(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return None, False

    monkeypatch.setattr(reader, "_read_subprocess", controlled)
    tasks = [asyncio.create_task(reader.read_codex_rollout_usage(None, expected_session_id=str(i))) for i in range(8)]
    await entered.wait()
    await asyncio.sleep(0)
    assert active == maximum == 2
    tasks[-1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[-1]
    release.set()
    await asyncio.gather(*tasks[:-1])
    assert maximum == 2 and active == 0


def test_archived_suffix_basename_is_recovered_and_conflicting_old_file_never_wins(tmp_path):
    archived = log(tmp_path, [meta(), ledger()], archived=True)
    suffixed = archived.with_name("rollout-date-s_other-copy-id.jsonl")
    archived.rename(suffixed)
    missing = tmp_path / "sessions" / suffixed.name
    assert read(tmp_path, missing)["transcript_path"] == str(suffixed)
    log(tmp_path, [meta(at=5), ledger(at=6, raw=usage(i=200))])
    with pytest.raises(reader.CodexRolloutUsageError, match="conflicting_rollouts"):
        read(tmp_path, missing)


async def test_cancelled_active_worker_is_killed_and_reaped(monkeypatch):
    entered = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            entered.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(reader.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(reader._read_subprocess(None, expected_session_id="s"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited


def test_continuous_segments_and_empty_alias_merge_by_request_chain(tmp_path):
    log(tmp_path, [meta(), ledger(total=usage()), old(last=usage())])
    second = tmp_path / "archived_sessions" / "rollout-date-s_later.jsonl"
    second.parent.mkdir()
    total = usage(i=200, o=40, c=120, r=10)
    second.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [meta(at=3), ledger("r2", at=4, total=total), old(total=total, last=usage(), at=5)]
        )
    )
    empty = tmp_path / "archived_sessions" / "rollout-date-s_empty.jsonl"
    empty.write_text(json.dumps(meta(at=6)) + "\n")
    u = read(tmp_path, second)
    assert u["api_calls"] == 2 and u["input_tokens"] == 80 and u["output_tokens"] == 40
    assert u["transcript_path"] == str(second)
    assert u["diagnostics"]["rollout_sources"] == 3


def test_fork_inherited_baseline_is_verified_against_complete_parent_chain(tmp_path):
    parent = log(tmp_path, [meta("p"), ledger("p1", sid="p", total=usage())], sid="p")
    log(
        tmp_path,
        [
            meta(fork="p", at=3),
            ledger("child", at=4, total=usage(i=200, o=40, c=120, r=10)),
            old(total=usage(i=200, o=40, c=120, r=10), last=usage(), at=5),
        ],
    )
    u = read(tmp_path)
    assert u["api_calls"] == 1 and u["input_tokens"] == 40 and u["output_tokens"] == 20
    assert u["diagnostics"]["fork_baseline_verified"] == 1
    parent.unlink()
    with pytest.raises(reader.CodexRolloutUsageError, match="fork_parent_missing"):
        read(tmp_path)


def test_segment_gap_or_conflicting_branch_is_never_partially_measured(tmp_path):
    log(tmp_path, [meta(), ledger(total=usage())])
    folder = tmp_path / "archived_sessions"
    folder.mkdir()
    p = folder / "rollout-date-s_gap.jsonl"
    p.write_text(
        "".join(
            json.dumps(row) + "\n" for row in [meta(at=3), ledger("r2", at=4, total=usage(i=300, o=60, c=180, r=15))]
        )
    )
    with pytest.raises(reader.CodexRolloutUsageError, match="ledger_prefix_missing"):
        read(tmp_path)
    p.write_text("".join(json.dumps(row) + "\n" for row in [meta(), ledger(raw=usage(i=150), total=usage(i=150))]))
    with pytest.raises(reader.CodexRolloutUsageError, match="conflict"):
        read(tmp_path)


def test_identical_active_and_archived_copy_without_cumulative_wire_is_deduplicated(tmp_path):
    p = log(tmp_path, [meta(), ledger()])
    archived = tmp_path / "archived_sessions" / p.name
    archived.parent.mkdir()
    archived.write_bytes(p.read_bytes())
    assert read(tmp_path, p)["api_calls"] == 1


async def test_worker_pins_parent_package_after_chdir_with_relative_pythonpath(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    for package in (foreign / "aiteam", foreign / "src/aiteam"):
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('raise RuntimeError("wrong checkout imported")\n')
    home = foreign / "codex-data"
    p = log(home, [meta(), ledger()])
    monkeypatch.setenv("HOME", str(foreign))
    monkeypatch.setenv("CODEX_HOME", "codex-data")
    monkeypatch.setenv("PYTHONPATH", "src")
    monkeypatch.chdir(foreign)
    measured = await reader.read_codex_rollout_usage(None, expected_session_id="s")
    assert measured["input_tokens"] == 40
    assert measured["transcript_path"] == str(p)


@pytest.mark.parametrize("pythonpath", [None, ""], ids=["unset", "empty"])
async def test_worker_without_pythonpath_never_imports_from_parent_cwd(tmp_path, monkeypatch, pythonpath):
    # Python adds nothing for an unset or empty PYTHONPATH. Reading it as the parent
    # cwd would put that directory ahead of the standard library in the worker.
    cwd = tmp_path / "api-cwd"
    cwd.mkdir()
    (cwd / "json.py").write_text('raise RuntimeError("parent cwd imported")\n')
    home = tmp_path / "codex-home"
    p = log(home, [meta(), ledger()])
    if pythonpath is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.chdir(cwd)
    measured = await reader.read_codex_rollout_usage(p, expected_session_id="s", codex_home=home)
    assert measured["input_tokens"] == 40


@pytest.mark.parametrize("fault", ["value", "time"], ids=["wrong-baseline", "future-parent-anchor"])
def test_fork_baseline_requires_exact_parent_value_before_child_start(tmp_path, fault):
    parent_values = usage()
    anchor = usage(i=300, o=60, c=180, r=15) if fault == "value" else usage(i=200, o=40, c=120, r=10)
    parent_at = 1 if fault == "value" else 4
    log(tmp_path, [meta("p"), ledger("parent", sid="p", at=parent_at, raw=parent_values, total=parent_values)], sid="p")
    log(tmp_path, [meta(fork="p", at=3), ledger("child", at=5, total=anchor)])
    with pytest.raises(reader.CodexRolloutUsageError, match="fork_baseline_mismatch"):
        read(tmp_path)
    # Only repair the violated predicate; a legitimate fork remains measurable.
    parent_values = usage(i=200, o=40, c=120, r=10) if fault == "value" else usage()
    log(tmp_path, [meta("p"), ledger("parent", sid="p", at=2, raw=parent_values, total=parent_values)], sid="p")
    assert read(tmp_path)["input_tokens"] == 40


async def test_configured_home_root_symlink_and_explicit_override_agree(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    p = log(actual, [meta(), ledger()])
    linked = tmp_path / "configured"
    linked.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(linked))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-user-home"))
    alias_path = linked / "sessions" / p.name
    assert reader.resolve_codex_home() == actual
    assert reader.resolve_codex_rollout_path("s", alias_path) == p
    assert reader.parse_codex_rollout_usage(alias_path, expected_session_id="s")["input_tokens"] == 40
    assert (await reader.read_codex_rollout_usage(alias_path, expected_session_id="s"))["input_tokens"] == 40
    explicit = tmp_path / "explicit"
    log(explicit, [meta(), ledger(raw=usage(i=200))])
    assert reader.parse_codex_rollout_usage(None, expected_session_id="s", codex_home=explicit)["input_tokens"] == 140


def test_resolving_config_root_does_not_resolve_internal_file_links(tmp_path, monkeypatch):
    outside = log(tmp_path / "outside", [meta(), ledger()])
    actual = tmp_path / "actual"
    (actual / "sessions").mkdir(parents=True)
    link = actual / "sessions/manual.jsonl"
    link.symlink_to(outside)
    configured = tmp_path / "configured"
    configured.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(configured))
    with pytest.raises(reader.CodexRolloutUsageError, match="unreadable"):
        reader.parse_codex_rollout_usage(configured / "sessions/manual.jsonl", expected_session_id="s")


async def test_async_canonicalization_stays_in_worker_process(tmp_path, monkeypatch):
    p = log(tmp_path, [meta(), ledger()])

    def forbidden_on_api_process(*args, **kwargs):
        raise AssertionError("root canonicalization must run in the worker")

    monkeypatch.setattr(reader, "resolve_codex_home", forbidden_on_api_process)
    measured = await reader.read_codex_rollout_usage(p, expected_session_id="s", codex_home=tmp_path)
    assert measured["input_tokens"] == 40
