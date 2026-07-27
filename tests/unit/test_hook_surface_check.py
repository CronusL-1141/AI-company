"""I8 machine check — bilingual README lifecycle-event anchors.

The first version of check_hook_surface.py pinned only the "Hook System (N
scripts across M Lifecycle Events)" heading, so the same count restated in the
feature list and the directory tree drifted unseen. These tests lock the
exhaustive scan in place: every numbered claim counts, bare prose does not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_hook_surface", ROOT / "scripts" / "check_hook_surface.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


@pytest.mark.parametrize(
    "text",
    [
        "### Hook System (11 scripts across 11 Lifecycle Events — The Bridge)",
        "- **Hook-driven lifecycle**: 11 CC lifecycle events (SessionStart → PreCompact)",
        "│   ├── hooks/         — CC Hook scripts (11 lifecycle events)",
        "### Hook 系统（11 个脚本 / 11 个生命周期事件 — CC 与 OS 的桥梁）",
        "- **Hook 驱动生命周期**：11 个 CC 生命周期事件（SessionStart → PreCompact）",
        "│   ├── hooks/         — CC Hook 脚本（11 个生命周期事件）",
    ],
)
def test_matching_claims_pass(text):
    assert checker.scan_event_anchors(text, 11, "README.md") == []


@pytest.mark.parametrize(
    "text",
    [
        "### Hook System (11 scripts across 12 Lifecycle Events — The Bridge)",
        "- **Hook-driven lifecycle**: 9 CC lifecycle events (SessionStart → PreCompact)",
        "│   ├── hooks/         — CC Hook scripts (12 lifecycle events)",
        "### Hook 系统（11 个脚本 / 12 个生命周期事件 — CC 与 OS 的桥梁）",
        "- **Hook 驱动生命周期**：12 个 CC 生命周期事件（SessionStart → PreCompact）",
        "│   ├── hooks/         — CC Hook 脚本（12 个生命周期事件）",
    ],
)
def test_drifted_claims_fail(text):
    failures = checker.scan_event_anchors(text, 11, "README.md")
    assert len(failures) == 1
    assert "生命周期事件数声明" in failures[0]


@pytest.mark.parametrize(
    "text",
    [
        # Prose with no number in front is a description, not a claim.
        "SubagentStop     → send_event.py     — Record sub-Agent lifecycle event",
        "SubagentStop     → send_event.py     — 记录子 Agent 生命周期事件",
    ],
)
def test_unnumbered_prose_is_not_a_claim(text):
    assert checker.scan_event_anchors(text, 11, "README.md") == []


def test_every_line_is_reported_with_its_number():
    text = "a\n12 lifecycle events\nb\n13 个生命周期事件\n"
    failures = checker.scan_event_anchors(text, 11, "README.zh-CN.md")
    assert [f.split(":")[1] for f in failures] == ["2", "4"]


def test_real_readmes_are_pinned_to_the_manifest():
    """The shipped READMEs must agree with hooks.json — the check's whole point."""
    _manifest, scripts, events, errors = checker._load_manifest()
    assert errors == []
    assert checker._readme_counts(len(scripts), len(events)) == []
