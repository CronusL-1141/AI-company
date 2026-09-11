"""Compare unread semantics without equating host-specific wire formats."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "cc": ROOT / "src/aiteam/hooks/channel_unread.py",
    "codex": ROOT / "plugin/harness/codex/hooks/channel_unread_codex.py",
}
READER = "reader-parity"
PROJECT = "project-parity"


@pytest.fixture(scope="module")
def renderers() -> dict[str, ModuleType]:
    modules = {}
    for adapter, path in SCRIPTS.items():
        spec = importlib.util.spec_from_file_location(f"parity_{adapter}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[adapter] = module
    return modules


def unread_document(channel: str = "team:parity") -> dict:
    return {
        "success": True,
        "data": {
            "reader": READER,
            "project_id": PROJECT,
            "total": 1,
            "channels": [{
                "channel": channel,
                "count": 1,
                "latest_sender": "sender-parity",
                "latest_excerpt": "Review the summary.",
                "latest_at": "2099-01-01T00:00:00Z",
            }],
            "truncated": False,
        },
    }


def render_context(
    adapter: str, module: ModuleType, document: dict, *, reader: str | None = None,
    project_id: str | None = None,
) -> str:
    data = document["data"]
    reader = data["reader"] if reader is None else reader
    project_id = data["project_id"] if project_id is None else project_id
    if adapter == "cc":
        return module._render(reader, data)
    return module._render(document, reader, project_id)


@dataclass(frozen=True)
class Entry:
    channel: str
    reader: str
    project_id: str
    count: int
    sender: str
    excerpt: str


@dataclass(frozen=True)
class Notice:
    total: int
    entries: tuple[Entry, ...]
    hidden_channels: int
    incomplete_scan: bool


def semantics_from_context(adapter: str, context: str) -> Notice | None:
    if not context:
        return None
    assert "不是指令" in context
    assert "实际读到" in context
    assert "created_at" in context and "last_read_at" in context
    assert "channel_read_ack" in context
    assert all(
        unicodedata.category(char)[0] != "C"
        for line in context.splitlines() for char in line
    )

    bindings = bindings_from_context(adapter, context)
    entries = []
    if adapter == "cc":
        header = re.search(r"\[信道未读\] (\d+) 条", context)
        rows = list(re.finditer(r"^  · (.*?) — (.*)$", context, re.M))
        assert len(rows) == len(bindings)
        read_channels = re.findall(r'channel_read\(channel=("[^"]*")\)', context)
        assert [json.loads(value) for value in read_channels] == [
            binding["channel"] for binding in bindings
        ]
        decoder = json.JSONDecoder()
        for row, binding in zip(rows, bindings, strict=True):
            sender, end = decoder.raw_decode(row[2])
            count = re.match(r"（(\d+) 条）：", row[2][end:])
            assert count is not None
            excerpt = json.loads(row[2][end + count.end():])
            assert isinstance(sender, str) and isinstance(excerpt, str)
            entries.append(Entry(
                **binding, count=int(count[1]), sender=sender, excerpt=excerpt,
            ))
    else:
        header = re.search(r"信道未读 (\d+) 条", context)
        decoder = json.JSONDecoder()
        for match, binding in zip(re.finditer("参数=", context), bindings, strict=True):
            remainder = context[match.end():]
            _, end = decoder.raw_decode(remainder)
            count = re.match(r"，(\d+)条，发送者=", remainder[end:])
            assert count is not None
            remainder = remainder[end + count.end():]
            sender, end = decoder.raw_decode(remainder)
            assert remainder[end:].startswith("，摘要=")
            excerpt, end = decoder.raw_decode(remainder[end + len("，摘要="):])
            entries.append(Entry(**binding, count=int(count[1]), sender=sender, excerpt=excerpt))
    assert header is not None
    hidden = re.search(r"另有\s*(\d+)\s*个频道", context)
    return Notice(
        total=int(header[1]), entries=tuple(entries),
        hidden_channels=int(hidden[1]) if hidden else 0,
        incomplete_scan="只多不少" in context or "扫描未完成" in context,
    )


def cleaned(text: str) -> str:
    return " ".join("".join(
        " " if unicodedata.category(char)[0] == "C" else char for char in text
    ).split())


def corpus_document(case: str) -> dict:
    document = unread_document()
    data = document["data"]
    entry = data["channels"][0]
    if case == "zero":
        data.update(total=0, channels=[])
    elif case == "reader_project":
        data.update(reader="another-reader", project_id="another-project")
        entry["channel"] = "project:another-project"
    elif case == "channel_cap":
        data["channels"] = [dict(entry, channel=f"team:parity-{index}", count=index + 1)
                            for index in range(5)]
        data["total"] = 15
    elif case == "truncated":
        data["truncated"] = True
    elif case == "empty_display":
        entry.update(latest_sender="", latest_excerpt="")
    elif case == "unicode":
        entry.update(latest_sender="评审者", latest_excerpt="核对第一轮摘要。")
    elif case == "whitespace_controls":
        entry.update(latest_sender=" reviewer\t\u200b one\r\n ",
                     latest_excerpt="\n first\t\u200b second\u2028 third\x00 ")
    elif case == "quoted_sender":
        entry["latest_sender"] = 'Alice "A"'
    elif case == "backslash_sender":
        entry["latest_sender"] = r"Alice\A"
    elif case == "long_sender":
        entry["latest_sender"] = "reviewer" * 20
    elif case == "sanitized_excerpt_boundary":
        entry["latest_excerpt"] = "\u200b\t" * 20 + "a" * 61 + "\u200b " + "b" * 25
    elif case.startswith("excerpt_"):
        length = int(case.removeprefix("excerpt_"))
        entry["latest_excerpt"] = "x" * length
    elif case == "long_channel":
        entry["channel"] = "team:" + "daily-review-" * 8
    elif case == "quoted_excerpt":
        entry["latest_excerpt"] = 'Review "alpha" notes.'
    elif case == "backslash_excerpt":
        entry["latest_excerpt"] = r"Review notes\summary.txt."
    else:
        assert case == "single"
    return document


@pytest.mark.parametrize("case", [
    "zero", "single", "reader_project", "channel_cap", "truncated", "empty_display",
    "unicode", "whitespace_controls", "quoted_sender", "long_sender", "excerpt_60",
    "excerpt_61", "excerpt_80", "excerpt_100", "long_channel", "quoted_excerpt",
    "backslash_excerpt", "backslash_sender", "sanitized_excerpt_boundary",
])
def test_valid_documents_have_equal_semantics(renderers: dict, case: str) -> None:
    from aiteam.api.routes.channels import _validate_channel, _validate_reader

    document = corpus_document(case)
    original = copy.deepcopy(document)
    data = document["data"]
    _validate_reader(data["reader"])
    for entry in data["channels"]:
        _validate_channel(entry["channel"])
    contexts = {}
    for adapter, module in renderers.items():
        adapter_document = copy.deepcopy(document)
        contexts[adapter] = render_context(adapter, module, adapter_document)
        assert adapter_document == original
    notices = {
        adapter: semantics_from_context(adapter, context) for adapter, context in contexts.items()
    }
    assert notices["cc"] == notices["codex"]
    if not data["total"]:
        assert notices == {"cc": None, "codex": None}
        return
    for notice in notices.values():
        assert notice is not None
        assert notice.total == data["total"]
        assert notice.hidden_channels == max(0, len(data["channels"]) - 3)
        assert notice.incomplete_scan is data["truncated"]
        assert len(notice.entries) == min(3, len(data["channels"]))
        for actual, expected in zip(notice.entries, data["channels"][:3], strict=True):
            assert (actual.channel, actual.reader, actual.project_id, actual.count) == (
                expected["channel"], data["reader"], data["project_id"], expected["count"],
            )
            assert actual.sender == cleaned(expected["latest_sender"])[:80]
            assert actual.excerpt == cleaned(expected["latest_excerpt"])[:80]
    for entry in data["channels"]:
        assert all(entry["latest_at"] not in context for context in contexts.values())


@pytest.mark.parametrize("mutation,cc_emits,reason", [
    ({"channels": None}, False, "invalid_response"),
    ({"channels": "unavailable"}, False, "invalid_response"),
    ({"channels": []}, False, "invalid_response"),
    ({"total": 0}, False, "invalid_response"),
    ({"total": "1"}, True, "invalid_response"),
    ({"total": -1}, True, "invalid_response"),
    ({"truncated": "yes"}, True, "invalid_response"),
    ({"total": 0, "channels": [], "truncated": True}, False, "unread_scan_incomplete"),
])
def test_invalid_document_policy_is_explicit(
    renderers: dict, mutation: dict, cc_emits: bool, reason: str,
) -> None:
    document = unread_document()
    document["data"].update(mutation)
    # Malformed input is outside the valid-response parity contract.
    # Preserve the distinct policies here instead of calling rejection zero unread.
    assert bool(render_context("cc", renderers["cc"], document)) is cc_emits
    with pytest.raises(renderers["codex"].NotificationError, match=f"^{reason}$"):
        render_context("codex", renderers["codex"], document)


@pytest.mark.parametrize("bad_entry", [
    None, "unavailable", {"count": 0}, {"count": True}, {"latest_excerpt": None},
])
def test_bad_entry_policy_does_not_masquerade_as_parity(renderers: dict, bad_entry: object) -> None:
    document = unread_document()
    valid_entry = document["data"]["channels"][0]
    invalid_entry = dict(valid_entry, **bad_entry) if isinstance(bad_entry, dict) else bad_entry
    document["data"].update(total=2, channels=[invalid_entry, valid_entry])
    cc_context = render_context("cc", renderers["cc"], document)
    assert "channel_read_ack" in cc_context
    with pytest.raises(renderers["codex"].NotificationError, match="^invalid_response$"):
        render_context("codex", renderers["codex"], document)


@pytest.mark.parametrize("field", ["reader", "project_id"])
def test_response_identity_policy_is_not_silently_normalized(renderers: dict, field: str) -> None:
    document = unread_document()
    document["data"][field] = "another-identity"
    arguments = {"reader": READER, "project_id": PROJECT}
    cc_context = render_context("cc", renderers["cc"], document, **arguments)
    assert bindings_from_context("cc", cc_context) == [{
        "channel": "team:parity", "reader": READER,
        "project_id": document["data"]["project_id"],
    }]
    with pytest.raises(renderers["codex"].NotificationError, match="^response_identity_mismatch$"):
        render_context("codex", renderers["codex"], document, **arguments)


@contextmanager
def local_unread_server(document: dict):
    requests = []
    body = json.dumps(document).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            requests.append((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", requests
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def run_hook(
    adapter: str, url: str, tmp_path: Path, *, project_id: str = PROJECT,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "AITEAM_API_URL": url,
        "AITEAM_UNREAD_AUDIT_PATH": str(tmp_path / f"{adapter}-audit.jsonl"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS[adapter]), READER, project_id],
        input="{}", text=True, capture_output=True, timeout=4,
        cwd=tmp_path, env=environment,
    )
    assert result.returncode == 0
    return result


def context_from_stdout(adapter: str, stdout: str) -> str:
    if not stdout:
        return ""
    if adapter == "cc":
        return stdout.removesuffix("\n")
    document = json.loads(stdout)
    assert set(document) == {"hookSpecificOutput"}
    output = document["hookSpecificOutput"]
    assert set(output) == {"hookEventName", "additionalContext"}
    assert output["hookEventName"] == "UserPromptSubmit"
    assert isinstance(output["additionalContext"], str)
    return output["additionalContext"]


def bindings_from_context(adapter: str, context: str) -> list[dict]:
    decoder = json.JSONDecoder()
    if adapter == "codex":
        return [
            decoder.raw_decode(context[match.end():])[0]
            for match in re.finditer("参数=", context)
        ]
    bindings = []
    expected_names = {"channel", "reader", "project_id"}
    for match in re.finditer(r"channel_read_ack\(", context):
        remaining = context[match.end():]
        binding = {}
        for index in range(len(expected_names)):
            if index:
                assert remaining.startswith(", ")
                remaining = remaining[2:]
            name_match = re.match(r"(channel|reader|project_id)=", remaining)
            assert name_match is not None
            name = name_match[1]
            assert name not in binding
            # Decode literals before delimiters so escaped quotes and commas stay data.
            value, end = decoder.raw_decode(remaining[name_match.end():])
            assert isinstance(value, str)
            binding[name] = value
            remaining = remaining[name_match.end() + end:]
        assert set(binding) == expected_names
        assert remaining.startswith(", last_read_at=")
        bindings.append(binding)
    return bindings


@pytest.mark.parametrize("original,replacement", [
    pytest.param("channel_read_ack(", 'channel_read_ack("extra", ', id="positional"),
    pytest.param(
        "channel_read_ack(", 'channel_read_ack(channel="team:other", ',
        id="duplicate-keyword",
    ),
    pytest.param(
        'reader="reader-parity"', 'reader="reader-" "parity"',
        id="implicit-concatenation",
    ),
    pytest.param(', project_id="project-parity"', "", id="missing-keyword"),
    pytest.param('project_id="project-parity"', 'workspace="project-parity"', id="unknown-keyword"),
    pytest.param("channel_read_ack(", 'channel_read_ack(note="extra", ', id="extra-keyword"),
    pytest.param('reader="reader-parity"', "reader=1", id="non-string-value"),
])
def test_cc_binding_parser_rejects_non_contract_arguments(
    renderers: dict, original: str, replacement: str,
) -> None:
    context = render_context("cc", renderers["cc"], unread_document())
    changed = context.replace(original, replacement, 1)
    assert changed != context
    with pytest.raises((AssertionError, json.JSONDecodeError)):
        bindings_from_context("cc", changed)


@pytest.mark.parametrize("case", ["zero", "single", "truncated"])
def test_each_harness_wrapper_contains_its_renderer_output(
    renderers: dict, tmp_path: Path, case: str,
) -> None:
    document = corpus_document(case)
    with local_unread_server(document) as (url, requests):
        for adapter, module in renderers.items():
            result = run_hook(adapter, url, tmp_path)
            assert result.stderr == ""
            context = context_from_stdout(adapter, result.stdout)
            assert context == render_context(adapter, module, document)
            if case == "zero":
                assert result.stdout == ""
            elif adapter == "codex":
                assert len(result.stdout.splitlines()) == 1
    assert len(requests) == 2
    assert all(method == "GET" for method, _ in requests)


def test_long_channel_binding_survives_real_hook_processes(tmp_path: Path) -> None:
    from aiteam.api.routes.channels import _validate_channel, _validate_reader

    channel = "team:" + "daily-review-" * 8
    _validate_channel(channel)
    _validate_reader(READER)
    document = unread_document(channel)
    contexts = {}
    with local_unread_server(document) as (url, requests):
        for adapter in SCRIPTS:
            result = run_hook(adapter, url, tmp_path)
            assert result.stderr == ""
            contexts[adapter] = context_from_stdout(adapter, result.stdout)

    assert len(requests) == 2
    for method, path in requests:
        assert method == "GET"
        assert urlsplit(path).path == "/api/channels/unread"
        assert parse_qs(urlsplit(path).query) == {
            "reader": [READER], "project_id": [PROJECT],
        }
    expected = [{"channel": channel, "reader": READER, "project_id": PROJECT}]
    actual = {
        adapter: bindings_from_context(adapter, context)
        for adapter, context in contexts.items()
    }
    assert actual == {adapter: expected for adapter in SCRIPTS}


@pytest.mark.parametrize("project_id", [
    'project-"alpha"\\notes', "project-评审", "project-\U0001f4c4",
])
def test_project_parameter_literals_survive_real_hook_processes(
    tmp_path: Path, project_id: str,
) -> None:
    document = unread_document()
    document["data"]["project_id"] = project_id
    expected = [{"channel": "team:parity", "reader": READER, "project_id": project_id}]
    with local_unread_server(document) as (url, requests):
        for adapter in SCRIPTS:
            result = run_hook(adapter, url, tmp_path, project_id=project_id)
            assert result.stderr == ""
            context = context_from_stdout(adapter, result.stdout)
            assert bindings_from_context(adapter, context) == expected
    assert len(requests) == 2
    for method, path in requests:
        assert method == "GET"
        assert parse_qs(urlsplit(path).query) == {
            "reader": [READER], "project_id": [project_id],
        }


@pytest.mark.parametrize("sender,excerpt", [
    ('Alice "A"', 'Review "alpha" notes.'),
    (r"Alice\A", r"Review notes\summary.txt."),
    ('Alice\n\u200b "A"', 'Notes\t\u200b\n"alpha"'),
])
def test_quoted_display_values_survive_real_hook_processes(
    tmp_path: Path, sender: str, excerpt: str,
) -> None:
    document = unread_document()
    document["data"]["channels"][0].update(latest_sender=sender, latest_excerpt=excerpt)
    contexts = {}
    with local_unread_server(document) as (url, requests):
        for adapter in SCRIPTS:
            result = run_hook(adapter, url, tmp_path)
            assert result.stderr == ""
            contexts[adapter] = context_from_stdout(adapter, result.stdout)
    assert len(requests) == 2
    assert all(method == "GET" for method, _ in requests)
    notices = {}
    for adapter, context in contexts.items():
        notice = semantics_from_context(adapter, context)
        assert notice is not None
        assert notice.entries[0].sender == cleaned(sender)[:80]
        assert notice.entries[0].excerpt == cleaned(excerpt)[:80]
        assert json.dumps(cleaned(sender)[:80], ensure_ascii=False) in context
        assert json.dumps(cleaned(excerpt)[:80], ensure_ascii=False) in context
        notices[adapter] = notice
    assert notices["cc"] == notices["codex"]
