"""deep_review_link hook unit tests.

The hook fires on PostToolUse for ``mcp__ai-team-os__report_save``. It
extracts ``deep_review_id`` and ``report_id`` from the payload then PATCHes
the OS API. We monkeypatch the HTTP layer so the suite runs offline.
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest


def _load_hook():
    import aiteam.hooks.deep_review_link as m
    return m


def _run_main(payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Invoke the hook main(); returns list of (path, body) HTTP calls."""
    m = _load_hook()
    raw = json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    captured: list[dict] = []

    def fake_post(path: str, body: dict) -> dict | None:
        captured.append({"path": path, "body": body})
        return {"id": body.get("report_id", "")}

    monkeypatch.setattr(m, "_http_post", fake_post)

    m.main()
    return captured


# ---------------------------------------------------------------------------
# Positive path
# ---------------------------------------------------------------------------


def test_deep_review_report_triggers_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook calls link_report when content has both anchors and tool_response has id.

    K5: payload now also carries parsed 5-section fields when the body is
    structured as a deep-review report. We assert ``report_id`` is present
    and section-1 (summary_md) was extracted.
    """
    payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {
            "report_type": "deep-review",
            "content": (
                "# fastmcp 深度审查报告\n\n"
                "repo_id=11111111-aaaa-bbbb-cccc-222222222222\n"
                "deep_review_id=33333333-dddd-eeee-ffff-444444444444\n\n"
                "## 1. 真实定位与成熟度\n- 实际定位: test framework\n"
            ),
        },
        "tool_response": {"id": "report-uuid-xyz"},
    }
    calls = _run_main(payload, monkeypatch)
    assert len(calls) == 1
    assert (
        calls[0]["path"]
        == "/api/ecosystem/deep_reviews/33333333-dddd-eeee-ffff-444444444444/link_report"
    )
    body = calls[0]["body"]
    assert body["report_id"] == "report-uuid-xyz"
    assert "实际定位: test framework" in body.get("summary_md", "")


# ---------------------------------------------------------------------------
# Negative paths — no HTTP call
# ---------------------------------------------------------------------------


def test_non_deep_review_type_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """report_type=research must NOT trigger the link."""
    payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {
            "report_type": "research",
            "content": (
                "repo_id=aaaa\ndeep_review_id=bbbb\n"
            ),
        },
        "tool_response": {"id": "report-1"},
    }
    assert _run_main(payload, monkeypatch) == []


def test_missing_anchors_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body missing the required anchors is silently ignored."""
    payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {
            "report_type": "deep-review",
            "content": "# fastmcp\n\nNo anchors here.",
        },
        "tool_response": {"id": "report-1"},
    }
    assert _run_main(payload, monkeypatch) == []


def test_unrelated_tool_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other tools are no-ops, even if the content shape matches."""
    payload = {
        "tool_name": "mcp__ai-team-os__task_create",
        "tool_input": {
            "report_type": "deep-review",
            "content": "repo_id=a\ndeep_review_id=b\n",
        },
        "tool_response": {"id": "report-1"},
    }
    assert _run_main(payload, monkeypatch) == []


def test_missing_report_id_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a saved report id we can't link, so the hook exits cleanly."""
    payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {
            "report_type": "deep-review",
            "content": (
                "repo_id=11111111-aaaa\n"
                "deep_review_id=33333333-dddd\n"
            ),
        },
        "tool_response": {},
    }
    assert _run_main(payload, monkeypatch) == []


def test_aliased_tool_name_short_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare ``report_save`` name (CC ecosystem) also triggers the hook."""
    payload = {
        "tool_name": "report_save",
        "tool_input": {
            "report_type": "deep-review",
            "content": (
                "repo_id=11111111-aaaa-bbbb-cccc-222222222222\n"
                "deep_review_id=33333333-dddd-eeee-ffff-444444444444\n"
            ),
        },
        "tool_response": {"id": "rep-2"},
    }
    calls = _run_main(payload, monkeypatch)
    assert len(calls) == 1
    assert calls[0]["body"] == {"report_id": "rep-2"}


_FIVE_SECTION_REPORT = """\
# org/repo 深度审查报告

repo_id=11111111-aaaa-bbbb-cccc-222222222222
deep_review_id=33333333-dddd-eeee-ffff-444444444444

## 1. 真实定位与成熟度
- 实际定位: 测试框架
- README claim: ok

## 2. 架构概览
```
repo/
├── src/
└── tests/
```

## 3. 我们能借鉴的点
- 设计点 A: 装饰器风格

## 4. 风险/不可取
- License: 未指定
- 重型依赖: torch

## 5. 集成建议
- **推荐动作**: **integrate**
- **理由**: 价值高

---

## 元数据
- demo_result: success
- demo_log_excerpt: |
    GET https://api.github.com/repos/x/y
    => 200 OK
    stars=1234
- 验证方式: GitHub API
"""


# ---------------------------------------------------------------------------
# K5: 5-section parser
# ---------------------------------------------------------------------------


def test_parser_splits_all_five_sections() -> None:
    """K5 parser must extract all five numeric sections by heading number."""
    m = _load_hook()
    secs = m._split_sections(_FIVE_SECTION_REPORT)
    assert set(secs.keys()) == {"1", "2", "3", "4", "5"}
    assert "测试框架" in secs["1"]
    assert "└── tests/" in secs["2"]
    assert "装饰器风格" in secs["3"]
    assert "License" in secs["4"]
    assert "推荐动作" in secs["5"]
    # Section 5 must NOT bleed into 元数据 — demo_result must be absent.
    assert "demo_result" not in secs["5"]


def test_parser_extracts_demo_metadata_and_recommendation() -> None:
    """demo_result / demo_log / 推荐动作 must come out clean."""
    m = _load_hook()
    demo_result, demo_log = m._parse_demo_metadata(_FIVE_SECTION_REPORT)
    assert demo_result == "success"
    assert demo_log is not None
    assert "stars=1234" in demo_log
    # The leading 4-space indent must have been stripped.
    assert not demo_log.startswith("    ")

    secs = m._split_sections(_FIVE_SECTION_REPORT)
    assert m._parse_recommendation(secs["5"]) == "integrate"


def test_link_payload_carries_all_parsed_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: hook sends summary/architecture/risks/learnings/integration_md
    + demo_result/demo_log_excerpt/integration_recommendation."""
    payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {
            "report_type": "deep-review",
            "content": _FIVE_SECTION_REPORT,
        },
        "tool_response": {"id": "rep-final"},
    }
    calls = _run_main(payload, monkeypatch)
    assert len(calls) == 1
    body = calls[0]["body"]

    assert body["report_id"] == "rep-final"
    assert "测试框架" in body["summary_md"]
    assert "└── tests/" in body["architecture_md"]
    assert "装饰器风格" in body["learnings_md"]
    assert "License" in body["risks_md"]
    assert "推荐动作" in body["integration_md"]
    assert body["demo_result"] == "success"
    assert "stars=1234" in body["demo_log_excerpt"]
    assert body["integration_recommendation"] == "integrate"


def test_invalid_stdin_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage stdin must not raise, and must not leave output behind."""
    m = _load_hook()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(m, "_http_post", lambda *_: None)
    m.main()
    assert out.getvalue() == ""


_LINKABLE = {
    "tool_name": "mcp__ai-team-os__report_save",
    "tool_input": {
        "report_type": "deep-review",
        "content": (
            "repo_id=11111111-aaaa-bbbb-cccc-222222222222\n"
            "deep_review_id=33333333-dddd-eeee-ffff-444444444444\n\n"
            "## 1. positioning\nbody\n"
        ),
    },
    "tool_response": {"id": "report-uuid-xyz"},
}


@pytest.mark.parametrize(
    "payload",
    [
        _LINKABLE,
        {"tool_name": "mcp__ai-team-os__report_save", "tool_input": {"report_type": "research"}},
    ],
    ids=["linked", "skipped"],
)
def test_hook_process_writes_no_stdout(payload: dict[str, Any], tmp_path) -> None:
    """Run the hook the way Claude Code does: a child process fed JSON on stdin.

    CC records any PostToolUse stdout, ``{}`` included, as a hook attachment in the
    transcript (about 1,500 empty ones from this hook in one real session). With
    nothing to say, the hook must print nothing, whether it linked or skipped.
    """
    import os
    import socket
    import subprocess
    from pathlib import Path

    with socket.socket() as s:  # a port nothing listens on: POST fails fast
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    hook = Path(__file__).resolve().parents[3] / "plugin" / "hooks" / "deep_review_link.py"
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["AITEAM_API_URL"] = f"http://127.0.0.1:{dead_port}"
    env["HOME"] = str(tmp_path)

    proc = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
