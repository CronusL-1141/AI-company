"""Response-ledger export tests never read a real account or call the network."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiteam.cli.commands.account_usage_cmd import app
from aiteam.types import PricingUsageEntry

START = "2026-09-14T01:00:00Z"
END = "2026-09-14T02:00:00Z"


def context(model="gpt-6-astra", turn="turn-one"):
    return {"type": "turn_context", "payload": {"model": model, "turn_id": turn}}


def ledger(request_id="response-one", timestamp="2026-09-14T01:30:00Z", **changes):
    usage = {"input_tokens": 100, "cached_input_tokens": 60,
             "cache_write_input_tokens": 10, "output_tokens": 20}
    usage.update(changes)
    return {"timestamp": timestamp, "type": "token_usage_record", "payload": {
        "response_id": request_id, "turn_id": "turn-one", "usage": usage,
        "turn_token_usage": {"input_tokens": 999999},
        "thread_token_usage": {"input_tokens": 999999999},
    }}


def notification(timestamp="2026-09-14T01:30:00Z"):
    return {"timestamp": timestamp, "type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "last_token_usage": ledger()["payload"]["usage"],
            "total_token_usage": {"input_tokens": 999999999},
        },
    }}


def write_rows(root, rows, name="session.jsonl"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return path


def run_export(root, **options):
    opts = {"since": START, "until": END, "service-tier": "standard", "session-root": str(root)}
    opts.update(options)
    args = ["export"]
    for key, value in opts.items():
        if value is not None:
            args.extend([f"--{key}", value])
    return CliRunner().invoke(app, args)


def test_export_contract_stdout_and_read_only_boundary(tmp_path):
    source = write_rows(tmp_path, [
        context(),
        {"type": "response_item", "payload": {"type": "message", "content": "PRIVATE_CHAT_SECRET"}},
        ledger(), notification(), notification(),
    ])
    before = source.read_bytes()
    result = run_export(tmp_path)
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert set(rows[0]) == {"occurred_at", "request"}
    assert rows[0]["request"] == {
        "request_id": "response-one", "model": "gpt-6-astra", "service_tier": "standard",
        "input_tokens": 100, "cached_input_tokens": 60, "cache_write_input_tokens": 10, "output_tokens": 20,
    }
    PricingUsageEntry.model_validate(rows[0])
    assert "PRIVATE_CHAT_SECRET" not in result.output
    assert "999999" not in result.stdout
    assert "计价档位假设：standard" in result.stderr
    assert "全部客户端" in result.stderr
    assert "忽略 2 条" in result.stderr
    assert source.read_bytes() == before
    assert list(tmp_path.iterdir()) == [source]


def test_half_open_time_window_and_timezone_offsets(tmp_path):
    write_rows(tmp_path, [context(), ledger("excluded", START),
                          ledger("included", "2026-09-14T10:00:00+08:00"),
                          ledger("after", "2026-09-14T02:00:00.001Z")])
    result = run_export(tmp_path)
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [row["request"]["request_id"] for row in rows] == ["included"]
    assert rows[0]["occurred_at"] == END


def test_global_duplicate_response_ids_are_counted_once(tmp_path):
    write_rows(tmp_path, [context(), ledger()], "parent.jsonl")
    write_rows(tmp_path, [context(), ledger()], "nested/child.jsonl")
    result = run_export(tmp_path)
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 1
    assert "去重 1 条" in result.stderr


@pytest.mark.parametrize("changed", [
    ledger(input_tokens=101), ledger(timestamp="2026-09-14T01:31:00Z"),
])
def test_conflicting_duplicate_rejects_entire_output(tmp_path, changed):
    write_rows(tmp_path, [context(), ledger()], "one.jsonl")
    write_rows(tmp_path, [context(), changed], "two.jsonl")
    result = run_export(tmp_path)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "重复 response_id 内容冲突" in result.stderr
    assert "response-one" not in result.stderr


def test_conflicting_models_for_same_response_rejected(tmp_path):
    write_rows(tmp_path, [context(), ledger()], "one.jsonl")
    write_rows(tmp_path, [context("gpt-5"), ledger()], "two.jsonl")
    assert run_export(tmp_path).exit_code == 1


def test_rewritten_replay_cannot_move_an_existing_response_into_window(tmp_path):
    write_rows(tmp_path, [context(), ledger(timestamp=START)], "parent.jsonl")
    write_rows(tmp_path, [context(), ledger()], "child.jsonl")
    result = run_export(tmp_path)
    assert result.exit_code == 1
    assert not result.stdout
    assert "重复 response_id 内容冲突" in result.stderr


@pytest.mark.parametrize("options", [
    {"since": "2026-09-14T01:00:00"}, {"until": "not-a-time"},
    {"since": END}, {"since": "2026-09-14T03:00:00Z"},
    {"service-tier": None}, {"service-tier": "made-up"},
])
def test_invalid_time_or_absent_explicit_tier_rejected(tmp_path, options):
    write_rows(tmp_path, [context(), ledger()])
    result = run_export(tmp_path, **options)
    assert result.exit_code != 0
    assert not result.stdout


@pytest.mark.parametrize("tier", ["standard", "default", "fast", "priority", "flex", "batch"])
def test_explicit_tier_is_preserved_as_assumption(tmp_path, tier):
    write_rows(tmp_path, [context(), ledger()])
    result = run_export(tmp_path, **{"service-tier": tier})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["request"]["service_tier"] == tier
    assert f"计价档位假设：{tier}" in result.stderr


def test_observed_fast_tier_overrides_cli_fallback(tmp_path):
    row = ledger()
    row["payload"]["service_tier"] = "fast"
    write_rows(tmp_path, [context(), row])
    result = run_export(tmp_path, **{"service-tier": "standard"})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["request"]["service_tier"] == "fast"


def test_thread_settings_fast_tier_overrides_cli_fallback(tmp_path):
    settings = {"type": "event_msg", "payload": {
        "type": "thread_settings_applied", "thread_settings": {"service_tier": "fast"},
    }}
    write_rows(tmp_path, [settings, context(), ledger()])
    result = run_export(tmp_path, **{"service-tier": "standard"})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["request"]["service_tier"] == "fast"


def test_anonymous_legacy_events_are_gaps_not_fabricated_requests(tmp_path):
    write_rows(tmp_path, [context(), notification(), notification()])
    result = run_export(tmp_path)
    assert result.exit_code == 2
    assert json.loads(result.stdout) == []
    assert "无法确定请求 ID 的事件 = 2" in result.stderr
    assert "不能由此断言" in result.stderr


def test_missing_identity_model_and_tokens_are_reported(tmp_path):
    incomplete = ledger("missing-cache")
    del incomplete["payload"]["usage"]["cache_write_input_tokens"]
    write_rows(tmp_path, [ledger("no-model"), context(), ledger(None), incomplete, ledger()])
    result = run_export(tmp_path)
    assert result.exit_code == 2
    assert len(json.loads(result.stdout)) == 1
    assert "模型未知 = 1" in result.stderr
    assert "缺少真实 response_id = 1" in result.stderr
    assert "逐请求 token 字段不完整或无效 = 1" in result.stderr


@pytest.mark.parametrize("changes", [
    {"input_tokens": True}, {"input_tokens": -1}, {"output_tokens": 1.5},
    {"cached_input_tokens": 99}, {"cache_write_input_tokens": None},
])
def test_shared_token_validation_and_no_zero_fallback(tmp_path, changes):
    write_rows(tmp_path, [context(), ledger(**changes)])
    result = run_export(tmp_path)
    assert result.exit_code == 2
    assert json.loads(result.stdout) == []


def test_different_turn_cannot_inherit_stale_model(tmp_path):
    row = ledger()
    row["payload"]["turn_id"] = "unknown-turn"
    write_rows(tmp_path, [context(), row])
    result = run_export(tmp_path)
    assert result.exit_code == 2
    assert json.loads(result.stdout) == []
    assert "模型未知 = 1" in result.stderr


def test_malformed_record_clears_model_and_does_not_leak(tmp_path):
    source = write_rows(tmp_path, [context()])
    with source.open("a") as stream:
        stream.write('\n{"private":"PRIVATE_SECRET",broken}\n' + json.dumps(ledger()))
    result = run_export(tmp_path)
    assert result.exit_code == 2
    assert json.loads(result.stdout) == []
    assert "PRIVATE_SECRET" not in result.output
    assert "无法解析的记录 = 1" in result.stderr
    assert "模型未知 = 1" in result.stderr


def test_invalid_ledger_timestamp_is_reported(tmp_path):
    write_rows(tmp_path, [context(), ledger(timestamp="2026-09-14T01:30:00")])
    result = run_export(tmp_path)
    assert result.exit_code == 2
    assert "账本时间无效 = 1" in result.stderr
    assert json.loads(result.stdout) == []


def test_limit_rejects_instead_of_truncating(tmp_path):
    write_rows(tmp_path, [context(), *(ledger(f"response-{n}") for n in range(1001))])
    result = run_export(tmp_path)
    assert result.exit_code == 1
    assert not result.stdout
    assert "超过 1000 条" in result.stderr


def test_empty_directory_is_not_evidence_of_zero_usage(tmp_path):
    result = run_export(tmp_path)
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
    assert "不能由此断言" in result.stderr


def test_symlink_log_is_not_followed(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("PRIVATE_SECRET")
    (tmp_path / "linked.jsonl").symlink_to(target)
    result = run_export(tmp_path)
    assert result.exit_code == 2
    assert "未读取的符号链接 = 1" in result.stderr
    assert "PRIVATE_SECRET" not in result.output


def test_default_directory_is_codex_sessions(monkeypatch, tmp_path):
    write_rows(tmp_path, [context(), ledger()])
    original = Path.expanduser
    monkeypatch.setattr(
        Path, "expanduser", lambda self: tmp_path if str(self) == "~/.codex/sessions" else original(self),
    )
    result = run_export(tmp_path, **{"session-root": None})
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 1
