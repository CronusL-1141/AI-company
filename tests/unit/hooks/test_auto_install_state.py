"""auto_install: install-state, PEP 668, no re-run after a failure, the chain self-heal (design §7.2, E02-E05, E12).

main() runs in-process against a throwaway plugin tree and HOME; pip is the
only thing replaced, and every call to it is counted.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def ai(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("auto_install_state_test", REPO_ROOT / "plugin/hooks/auto_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/session_bootstrap.py"', "timeout": 15},
    ]}]}}))
    for name in ("auto_install.py", "session_bootstrap.py", "user_notice.py"):
        (plugin / "hooks" / name).write_text(f"# {name}\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setattr(module, "_self_heal_interpreter", lambda: None)
    module.test_home = home
    module.test_plugin = plugin
    module.pip_calls = []
    return module


def _versions(ai, monkeypatch, plugin="1.14.0", installed="1.13.0"):
    (ai.test_plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": plugin}))
    monkeypatch.setattr(ai, "_installed_version", lambda: installed)


def _pip(ai, monkeypatch, ok=True, error=None):
    def fake(upgrade):
        ai.pip_calls.append(upgrade)
        state = ai._user_notice().read_install_state()
        ai.state_during_pip = state
        return ok, error
    monkeypatch.setattr(ai, "_pip_install", fake)


def _state(ai) -> dict:
    return ai._user_notice().read_install_state()


def _next_run(ai, session: str) -> None:
    """A new hook process: fresh one-document latch, the given session on stdin."""
    ai._user_notice()._WROTE_DOCUMENT = False
    ai._read_payload = lambda: {"session_id": session, "source": "startup"}


def _line(capsys) -> str:
    out = capsys.readouterr().out
    return json.loads(out).get("systemMessage", "") if out else ""


def test_state_says_installing_before_pip_and_installed_after(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch)
    _pip(ai, monkeypatch)
    ai.main()
    during = ai.state_during_pip
    assert during["phase"] == "installing" and during["attempt"] == 1
    assert during["plugin_version"] == "1.14.0" and during["interpreter"] == sys.executable
    assert during["pid"] == os.getpid() and time.time() - during["started_at"] < 60
    assert _state(ai)["phase"] == "installed"
    assert _line(capsys) == "[AI Team OS] 已升级到 v1.14.0（原 v1.13.0），重启 Claude Code 后生效"


def test_fresh_install_line(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch, installed=None)
    _pip(ai, monkeypatch)
    ai.main()
    assert ai.pip_calls == [False]
    assert _line(capsys) == "[AI Team OS] v1.14.0 已装好，重启 Claude Code 后生效"


def test_a_killed_install_counts_the_next_attempt(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch)
    ai._write_install_state(ai._user_notice(), {
        "phase": "installing", "plugin_version": "1.14.0", "attempt": 1,
        "started_at": time.time() - 400, "pid": 999999})
    _pip(ai, monkeypatch)
    ai.main()
    assert ai.state_during_pip["attempt"] == 2


def test_a_running_install_is_not_started_twice(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch)
    ai._write_install_state(ai._user_notice(), {
        "phase": "installing", "plugin_version": "1.14.0", "attempt": 1,
        "started_at": time.time() - 5, "pid": os.getppid()})
    _pip(ai, monkeypatch)
    ai.main()
    assert ai.pip_calls == []
    assert capsys.readouterr().out == "", "session_bootstrap shows the progress line"


@pytest.mark.parametrize(("error", "marker", "reason"), [
    pytest.param("error: externally-managed-environment\n× This environment is externally managed", False,
                 "pep668", id="pep668-output"),
    pytest.param("ERROR: something else", True, "pep668", id="pep668-marker"),
    pytest.param("ERROR: Package requires a different Python: 3.10.4 not in '>=3.11'", False, "python_old",
                 id="requires-python"),
    pytest.param("ERROR: Cannot find command 'git' - do you have 'git' installed?", False, "no_git", id="git"),
    pytest.param("fatal: unable to access 'https://github.com/x': Could not resolve host: github.com", False,
                 "network", id="network"),
    pytest.param("Traceback: weird", False, "unknown", id="unknown"),
])
def test_failure_reasons(ai, monkeypatch, error, marker, reason):
    monkeypatch.setattr(ai, "_externally_managed", lambda: marker)
    assert ai._failure_reason(error) == reason


def test_pep668_marker_is_read_next_to_the_stdlib(ai, monkeypatch, tmp_path):
    import sysconfig

    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(stdlib))
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    assert ai._externally_managed() is False
    (stdlib / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
    assert ai._externally_managed() is True
    monkeypatch.setattr(sys, "base_prefix", "/elsewhere")  # inside a virtual environment
    assert ai._externally_managed() is False


def test_failure_is_not_retried_every_session(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch)
    _pip(ai, monkeypatch, ok=False, error="error: externally-managed-environment")
    _next_run(ai, "s1")
    ai.main()
    first = _line(capsys)
    assert first == "[AI Team OS] 依赖安装失败：系统 Python 禁止 pip 安装（PEP 668）。对 Claude 说「诊断 OS 安装」"
    state = _state(ai)
    assert (state["phase"], state["reason"], len(state["err_hash"])) == ("failed", "pep668", 8)
    # Next session start: no pip, same line.
    _next_run(ai, "s2")
    ai.main()
    assert ai.pip_calls == [True]
    assert _line(capsys) == first
    # Same session again: no pip and no second line.
    _next_run(ai, "s2")
    ai.main()
    assert ai.pip_calls == [True] and capsys.readouterr().out == ""


@pytest.mark.parametrize("change", ["plugin_version", "interpreter", "a_day_later"])
def test_failure_is_retried_when_something_changed(ai, monkeypatch, capsys, change):
    _versions(ai, monkeypatch)
    ai._write_install_state(ai._user_notice(), {
        "phase": "failed", "plugin_version": "1.14.0", "interpreter": sys.executable, "reason": "network",
        "err_hash": "abcd1234", "failed_at_ts": time.time() - 60, "attempt": 1})
    if change == "plugin_version":
        _versions(ai, monkeypatch, plugin="1.14.1")
    elif change == "interpreter":
        monkeypatch.setattr(sys, "executable", "/opt/other/python3")
    else:
        state = _state(ai)
        state["failed_at_ts"] = time.time() - 25 * 3600
        ai._write_install_state(ai._user_notice(), state)
    _pip(ai, monkeypatch)
    ai.main()
    assert ai.pip_calls == [True]


def test_old_python_fails_without_running_pip(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch)
    _pip(ai, monkeypatch)
    monkeypatch.setattr(ai, "_preflight_failure", lambda: "python_old")
    monkeypatch.setattr(ai, "_python_version", lambda: "3.9.6")
    ai.main()
    assert ai.pip_calls == []
    expected = "[AI Team OS] 依赖安装失败：Python 版本低于 3.11（当前 3.9.6）。对 Claude 说「诊断 OS 安装」"
    assert _line(capsys) == expected


# ---------------------------------------------------------------------------
# Main-chain content self-heal (E12)
# ---------------------------------------------------------------------------


def _converged(ai) -> Path:
    ai._sync_main_chain()
    runtime = ai.test_home / ".claude" / "hooks" / "ai-team-os"
    assert (runtime / "session_bootstrap.py").exists() and not (runtime / "auto_install.py").exists()
    return runtime


def test_current_and_identical_is_silent(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch, installed="1.14.0")
    _converged(ai)
    _pip(ai, monkeypatch)
    ai.main()
    assert capsys.readouterr().out == "" and ai.pip_calls == []


def test_an_outdated_copy_is_resynced_and_reported(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch, installed="1.14.0")
    runtime = _converged(ai)
    (runtime / "session_bootstrap.py").write_text("# stale\n")
    (runtime / "user_notice.py").unlink()
    _next_run(ai, "s1")
    ai.main()
    assert _line(capsys) == "[AI Team OS] 已自动同步 2 个落后的 hook 副本，即刻生效"
    assert (runtime / "session_bootstrap.py").read_text() == "# session_bootstrap.py\n"
    assert (runtime / "user_notice.py").exists()
    marker = json.loads((ai._user_notice().os_data_dir() / "main-chain.json").read_text())
    assert marker["installed_by"] == "plugin"
    _next_run(ai, "s2")
    ai.main()
    assert capsys.readouterr().out == "", "healed: silent again"


def test_a_source_install_keeps_its_own_copies(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch, installed="1.14.0")
    runtime = _converged(ai)
    (runtime / "session_bootstrap.py").write_text("# from the source checkout\n")
    data = ai._user_notice().os_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / "install_path.txt").write_text("/somewhere/AI-company")
    ai.main()
    assert capsys.readouterr().out == ""
    assert (runtime / "session_bootstrap.py").read_text() == "# from the source checkout\n"


def test_a_failed_resync_is_recorded_not_claimed(ai, monkeypatch, capsys):
    _versions(ai, monkeypatch, installed="1.14.0")
    runtime = _converged(ai)
    (runtime / "session_bootstrap.py").write_text("# stale\n")
    monkeypatch.setattr(ai, "_sync_main_chain", lambda: 0)  # copy blocked (permissions)
    ai.main()
    assert capsys.readouterr().out == "", "no 'synced' line for a sync that did not happen"
    assert _state(ai)["sync_failed"]["files"] == ["session_bootstrap.py"]


def test_hooks_json_gives_pip_its_budget():
    manifest = json.loads((REPO_ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))
    (timeout,) = [hook["timeout"] for group in manifest["hooks"]["SessionStart"] for hook in group["hooks"]
                  if "auto_install.py" in hook["command"]]
    spec = importlib.util.spec_from_file_location("ai_budget", REPO_ROOT / "plugin/hooks/auto_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert timeout == 300 and module.PIP_BUDGET_S < timeout
