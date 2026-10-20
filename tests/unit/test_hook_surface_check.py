"""I8 machine check — bilingual README lifecycle-event anchors.

The first version of check_hook_surface.py pinned only the "Hook System (N
scripts across M Lifecycle Events)" heading, so the same count restated in the
feature list and the directory tree drifted unseen. These tests lock the
exhaustive scan in place: every numbered claim counts, bare prose does not.
"""

from __future__ import annotations

import importlib.util
import shutil
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


@pytest.mark.parametrize(
    ("description", "drifted"),
    [
        pytest.param("with 25 role templates and 11 hooks across 15 lifecycle events.", False, id="aligned"),
        pytest.param("with 25 role templates and 13 hooks across 15 lifecycle events.", True, id="stale-hooks"),
        pytest.param("with 25 role templates and 11 hooks across 14 lifecycle events.", True, id="stale-events"),
        pytest.param("with 25 role templates and hooks.", True, id="claim-removed"),
    ],
)
def test_plugin_description_counts(description, drifted):
    failures = checker.scan_plugin_description(description, 11, 15)
    assert bool(failures) is drifted


def test_real_plugin_manifest_is_pinned_to_the_manifest():
    _manifest, scripts, events, errors = checker._load_manifest()
    assert errors == []
    assert checker._plugin_counts(len(scripts), len(events)) == []


@pytest.mark.parametrize(
    ("installer", "plugin", "drifted"),
    [
        pytest.param(("a.py", "b.py"), ("b.py", "a.py"), False, id="same-set"),
        pytest.param(("a.py", "b.py"), ("a.py",), True, id="plugin-missing-one"),
        pytest.param(("a.py",), ("a.py", "b.py"), True, id="installer-missing-one"),
    ],
)
def test_retired_parity(installer, plugin, drifted):
    assert bool(checker.scan_retired_parity(installer, plugin)) is drifted


def test_real_retired_lists_agree_and_none_is_registered():
    _manifest, scripts, _events, errors = checker._load_manifest()
    assert errors == []
    assert checker._retired_parity(scripts) == []
    assert checker._retired_parity(scripts | {"cc_task_bridge.py"}) != []


HOOK_DIRECTORIES = ("plugin/hooks", "src/aiteam/hooks", "plugin/harness/codex/hooks")


@pytest.fixture
def hook_repo(tmp_path, monkeypatch):
    """Run main against shipped registration sources in an isolated checkout."""
    for directory in (*HOOK_DIRECTORIES, "plugin/.claude-plugin"):
        shutil.copytree(ROOT / directory, tmp_path / directory, ignore=shutil.ignore_patterns("__pycache__"))
    for filename in ("install.py", "README.md", "README.zh-CN.md", "plugin/harness/codex/surface.py"):
        shutil.copyfile(ROOT / filename, tmp_path / filename)
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    return tmp_path


def test_registered_hooks_libraries_and_manual_entries_pass(hook_repo, capsys):
    assert checker.main() == 0
    assert "三处目录无孤儿" in capsys.readouterr().out


@pytest.mark.parametrize("directory", HOOK_DIRECTORIES)
def test_main_rejects_orphan_in_each_distribution(hook_repo, capsys, directory):
    (hook_repo / directory / "orphan_hook.py").write_text("# not registered\n", encoding="utf-8")
    assert checker.main() == 1
    output = capsys.readouterr().out
    assert f"{directory}/orphan_hook.py: 孤儿 Python 文件" in output


def test_nested_python_file_cannot_hide_from_scan(hook_repo, capsys):
    nested = hook_repo / "plugin/hooks/hidden"
    nested.mkdir()
    (nested / "orphan.py").write_text("", encoding="utf-8")
    assert checker.main() == 1
    assert "plugin/hooks/hidden/orphan.py: 孤儿 Python 文件" in capsys.readouterr().out


def test_importing_orphan_does_not_make_it_a_library(hook_repo, capsys):
    directory = hook_repo / "plugin/hooks"
    (directory / "orphan.py").write_text("", encoding="utf-8")
    with (directory / "send_event.py").open("a", encoding="utf-8") as stream:
        stream.write("\nimport orphan\n")
    assert checker.main() == 1
    assert "plugin/hooks/orphan.py: 孤儿 Python 文件" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("directory", "script"),
    [
        pytest.param("plugin/hooks", "send_event.py", id="plugin"),
        pytest.param("src/aiteam/hooks", "send_event.py", id="source"),
        pytest.param("plugin/harness/codex/hooks", "send_event_codex.py", id="codex"),
    ],
)
def test_main_rejects_missing_registered_script(hook_repo, capsys, directory, script):
    (hook_repo / directory / script).unlink()
    assert checker.main() == 1
    assert f"{directory}/{script}: 注册了不存在的脚本" in capsys.readouterr().out


def test_main_rejects_missing_codex_directory(hook_repo, capsys):
    shutil.rmtree(hook_repo / "plugin/harness/codex/hooks")
    assert checker.main() == 1
    assert "plugin/harness/codex/hooks: hook 目录不存在" in capsys.readouterr().out


def test_missing_directory_is_not_an_empty_success(tmp_path):
    assert checker.scan_hook_directory(tmp_path / "missing", set(), {}, "missing") == [
        "missing: hook 目录不存在",
    ]


@pytest.mark.parametrize(
    ("exceptions", "registered", "message"),
    [
        pytest.param({"library.py": ""}, set(), "白名单缺少理由", id="empty-reason"),
        pytest.param({"library.py": "  "}, set(), "白名单缺少理由", id="blank-reason"),
        pytest.param({"library.py": None}, set(), "白名单缺少理由", id="invalid-reason"),
        pytest.param({"gone.py": "library"}, set(), "白名单文件不存在", id="stale-entry"),
        pytest.param({"library.py": "library"}, {"library.py"}, "同时注册", id="registered-exception"),
        pytest.param({"../library.py": "library"}, set(), "白名单必须", id="outside-directory"),
    ],
)
def test_misconfigured_exception_fails(tmp_path, exceptions, registered, message):
    (tmp_path / "library.py").write_text("", encoding="utf-8")
    failures = checker.scan_hook_directory(tmp_path, registered, exceptions, "hooks")
    assert any(message in failure for failure in failures)


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        pytest.param(("library.py", "new.py"), "缺少带理由", id="new-support-needs-review"),
        pytest.param((), "安装时会漏分发", id="missing-distribution"),
        pytest.param(("library.py", "library.py"), "重复登记", id="duplicate-support"),
    ],
)
def test_support_declarations_do_not_grant_automatic_exceptions(declared, message):
    failures = checker.scan_support_modules(declared, {"library.py": "shared library"}, "surface")
    assert any(message in failure for failure in failures)


@pytest.mark.parametrize(
    ("source", "directory", "declaration"),
    [
        pytest.param("install.py", "src/aiteam/hooks", "HOOK_SUPPORT_MODULES", id="source"),
        pytest.param("plugin/harness/codex/surface.py", "plugin/harness/codex/hooks",
                     "CODEX_SUPPORT_MODULES", id="codex"),
    ],
)
def test_declared_support_still_needs_reason_in_main(hook_repo, capsys, source, directory, declaration):
    path = hook_repo / source
    text = path.read_text(encoding="utf-8")
    # The source and Codex declarations use different annotations; retain both.
    marker = f"{declaration}: "
    start = text.index("= (", text.index(marker)) + len("= (")
    path.write_text(text[:start] + '\n    "unreviewed.py",' + text[start:], encoding="utf-8")
    (hook_repo / directory / "unreviewed.py").write_text("", encoding="utf-8")
    assert checker.main() == 1
    assert f"{declaration}: unreviewed.py 缺少带理由的库模块白名单" in capsys.readouterr().out


def test_cc_exceptions_do_not_apply_to_codex(hook_repo, capsys):
    (hook_repo / "plugin/harness/codex/hooks/hook_delivery.py").write_text("", encoding="utf-8")
    assert checker.main() == 1
    assert "plugin/harness/codex/hooks/hook_delivery.py: 孤儿" in capsys.readouterr().out


def test_real_hook_directories_have_no_orphans():
    _manifest, scripts, _events, errors = checker._load_manifest()
    assert errors == []
    assert checker._directory_coverage(scripts, checker._load_installer_surface()) == []
