#!/usr/bin/env python3
"""Auto-install aiteam package on first launch.

This hook runs FIRST in SessionStart, before any other hook that depends
on the aiteam package. It uses only stdlib — no third-party imports.

On first marketplace install, aiteam is not pip-installed. This script
detects that and installs it automatically. User needs to restart CC once
after installation for MCP server to pick up the package.

What the user sees goes through user_notice (one line each, local render):
installed (E03), upgraded (E04), failed with a reason (E05), outdated global
hook copies re-synced (E12). While pip runs, install-state.json says so, and
session_bootstrap shows "installing" instead of "service not running" (E02).
A failed install is not retried on every session: only when the interpreter
or the plugin version changed, or 24 hours after the failure.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time

# Retired 2026-07-27 (batch 5): _ensure_agent_teams_env() silently wrote
# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 into the user's *global* settings.json on
# every SessionStart. Two reasons it had to go: (1) CC's Agent Teams tool group is
# gone, so the flag no longer switches anything on; (2) a plugin editing the user's
# global config behind their back is out of bounds — the only user-global writes
# this project performs must come from an explicit install/update run.


def _self_heal_interpreter():
    """Rewrite plugin manifest interpreter tokens to sys.executable (idempotent).

    Static plugin manifests (hooks/hooks.json, .mcp.json) cannot embed
    per-machine absolute paths, so they ship with a generic `python3` token.
    Bare tokens re-create the two failure modes the project already paid for:
    macOS without a `python` shim (command-not-found) and project .venv
    hijacking resolution (e2d0fbb). This hook runs under a working interpreter
    — the same one that pip-installs aiteam — so we rewrite the token to its
    absolute path, restoring the sys.executable invariant for MCP + all hooks.
    Idempotent: rewritten commands no longer start with python/python3.
    Never blocks SessionStart: every failure is swallowed.
    """
    import os
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    exe = sys.executable
    if not root or not exe:
        return
    quoted_exe = f'"{exe}"' if " " in exe else exe

    # hooks/hooks.json — shell-form commands: replace the leading interpreter token
    hooks_path = os.path.join(root, "hooks", "hooks.json")
    try:
        with open(hooks_path, encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for groups in data.get("hooks", {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    cmd = hook.get("command", "")
                    for token in ("python3 ", "python "):
                        if cmd.startswith(token):
                            hook["command"] = quoted_exe + " " + cmd[len(token):]
                            changed = True
                            break
        if changed:
            with open(hooks_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Silent failure — non-critical

    # .mcp.json — exec form: command field is the bare program (no quoting)
    mcp_path = os.path.join(root, ".mcp.json")
    try:
        with open(mcp_path, encoding="utf-8") as f:
            data = json.load(f)
        server = data.get("mcpServers", {}).get("ai-team-os")
        if server and server.get("command") in ("python", "python3"):
            server["command"] = exe
            with open(mcp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Silent failure — non-critical


GITHUB_URL = "git+https://github.com/CronusL-1141/AI-company.git"

RUNTIME_HOOKS_DIRNAME = "ai-team-os"

# Hooks retired from the manifest. The self-heal sync removes their runtime copies
# and settings.json entries, the same way install.py's update does; otherwise a
# plugin user keeps firing a retired hook on every event forever. Mirror of
# install.py RETIRED_HOOK_SCRIPTS (I8 pins the two lists together).
RETIRED_HOOK_SCRIPTS = (
    "task_completed_gate.py",
    "pipeline_gate.py",
    "autopilot_auto_stop.py",
    "cc_task_bridge.py",
    "meeting_ecosystem_writeback.py",
)


def _version_tuple(v: str) -> tuple:
    """Parse a dotted version into a comparable int tuple ('1.10.2' → (1,10,2)).

    Stdlib-only (no `packaging`): stops at the first non-digit in each part, so
    pre-release suffixes degrade gracefully to their numeric prefix.
    """
    out = []
    for part in str(v).split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out)


def _plugin_version():
    """Version declared by the bundled plugin (CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json)."""
    import os
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not root:
        return None
    try:
        from pathlib import Path
        data = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return data.get("version")
    except Exception:
        return None


def _installed_version():
    """Version of the pip-installed aiteam package, or None if not importable."""
    try:
        import aiteam
        return getattr(aiteam, "__version__", None)
    except Exception:
        return None


def _main_chain_registered() -> bool:
    """True if ~/.claude/settings.json already registers any ai-team-os runtime hook."""
    try:
        from pathlib import Path
        settings = Path.home() / ".claude" / "settings.json"
        cfg = json.loads(settings.read_text(encoding="utf-8"))
        for groups in cfg.get("hooks", {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    if "ai-team-os" in hook.get("command", ""):
                        return True
    except Exception:
        pass
    return False


# pip gets this long; the SessionStart hook timeout in hooks.json is 300s, so a
# slow install still ends with a rendered line instead of a hard hook kill.
PIP_BUDGET_S = 280
# A failed install is retried after this long even when nothing else changed.
RETRY_AFTER_FAILURE_S = 24 * 3600

_NETWORK_MARKERS = (
    "could not resolve host", "failed to connect", "network is unreachable",
    "temporary failure in name resolution", "connection refused", "connection reset",
    "timed out", "unable to access", "max retries exceeded", "proxyerror",
    "name or service not known", "nodename nor servname",
)


def _user_notice():
    """Load the shared notice module next to this file; None if it cannot load."""
    module = sys.modules.get("user_notice")
    if module is not None:
        return module
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_notice.py")
        spec = importlib.util.spec_from_file_location("user_notice", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["user_notice"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop("user_notice", None)
        return None


def _externally_managed() -> bool:
    """PEP 668 marker next to this interpreter's stdlib (not inside a virtual environment)."""
    try:
        import sysconfig
        from pathlib import Path

        if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
            return False
        return (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").is_file()
    except Exception:
        return False


def _failure_reason(error: str) -> str:
    """Classify a failed pip run: pep668, python_old, no_git, network or unknown."""
    text = (error or "").lower()
    if "externally-managed-environment" in text or _externally_managed():
        return "pep668"
    if "requires a different python" in text or "requires-python" in text:
        return "python_old"
    if "cannot find command 'git'" in text or "no such file or directory: 'git'" in text:
        return "no_git"
    if any(marker in text for marker in _NETWORK_MARKERS):
        return "network"
    return "unknown"


def _preflight_failure():
    """A reason pip cannot succeed at all, found without running it; None when pip may run."""
    import shutil

    if sys.version_info < (3, 11):  # noqa: UP036 - the self-heal entry must explain an old Python
        return "python_old"
    if shutil.which("git") is None:
        return "no_git"
    return None


def _python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def _write_install_state(notice, state: dict) -> None:
    """Replace install-state.json atomically. Never raises."""
    if notice is None:
        return
    path = notice.install_state_path()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _pid_alive(pid) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) terminates the process on Windows
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _pip_install(upgrade: bool):
    """Install/upgrade aiteam from GitHub (PyPI may lag). Returns (ok, error_or_None).

    The marketplace plugin dir is not pip-installable (no pyproject), so GitHub is
    the source for both fresh installs and version upgrades. Never raises.
    """
    args = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        args.append("--upgrade")
    args.append(GITHUB_URL)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=PIP_BUDGET_S)
        if proc.returncode == 0:
            return True, None
        return False, (proc.stderr or proc.stdout or "").strip()[-2000:]
    except Exception as e:  # noqa: BLE001 — must never block SessionStart
        return False, str(e)[:2000]


def _sync_main_chain():
    """Converge every entry point onto one runtime chain (the '单运行时' core).

    Copies the plugin's hook scripts to ~/.claude/hooks/ai-team-os/ and registers
    them in ~/.claude/settings.json with absolute sys.executable paths, reading the
    plugin's own hooks.json as the source of truth (minus auto_install itself: the
    self-heal entry must never be in the installed chain). Absolute paths make the
    chain Windows-safe (the plugin's `python3` token can't launch there) and let the
    yield sentinel dedupe the plugin backup copies.

    Same semantics as install.py register_hooks: every entry we own (a current
    manifest script or a retired one, matched by script name inside the runtime
    dir) is dropped and the current manifest is rebuilt, so retired hooks and
    stale matchers disappear while every hook we do not own stays where it was.
    Commands use install.py's exact format, so re-runs and a parallel source
    install converge on the same chain. Stdlib-only; never raises. Returns the
    number of hooks that were not registered before this run.
    """
    import os
    import re
    import shutil
    from pathlib import Path

    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not root:
        return 0
    root = Path(root)
    src_hooks = root / "hooks"
    runtime = Path.home() / ".claude" / "hooks" / RUNTIME_HOOKS_DIRNAME
    settings_path = Path.home() / ".claude" / "settings.json"

    try:
        manifest = json.loads((src_hooks / "hooks.json").read_text(encoding="utf-8"))
    except Exception:
        return 0

    # 1. Copy every hook script (except the self-heal entry) to the runtime dir,
    #    then drop the copies of retired hooks.
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        for py in src_hooks.glob("*.py"):
            if py.name == "auto_install.py":
                continue
            shutil.copy2(py, runtime / py.name)
    except Exception:
        pass  # partial copy is still better than none; never block
    for name in RETIRED_HOOK_SCRIPTS:
        try:
            (runtime / name).unlink()
        except Exception:
            pass  # absent (the common case) or not removable; never block

    py_exe = str(sys.executable).replace("\\", "/")
    runtime_fwd = str(runtime).replace("\\", "/")

    def _build_cmd(script: str, arg: str) -> str:
        cmd = f'"{py_exe}" "{runtime_fwd}/{script}"'
        return f"{cmd} {arg}" if arg else cmd

    _extract = re.compile(r'/hooks/([\w-]+\.py)"?(?:\s+(\S+))?\s*$')

    # 2. Parse the manifest into the chain to register.
    surface = []  # (event, matcher, [(command, timeout)])
    current = set()
    for event, groups in manifest.get("hooks", {}).items():
        for group in groups:
            rebuilt = []
            for hook in group.get("hooks", []):
                m = _extract.search(hook.get("command", ""))
                if not m:
                    continue
                script, arg = m.group(1), (m.group(2) or "")
                if script == "auto_install.py":
                    continue
                current.add(script)
                rebuilt.append((_build_cmd(script, arg), hook.get("timeout")))
            if rebuilt:
                surface.append((event, group.get("matcher", ""), rebuilt))
    owned = current | set(RETIRED_HOOK_SCRIPTS)

    marker = f"/hooks/{RUNTIME_HOOKS_DIRNAME}/"

    def _is_ours(command: str) -> bool:
        # Name-based, never "path contains ai-team-os": the runtime dir also hosts
        # hooks other parts of the project install, and users add their own there.
        normalized = command.replace("\\", "/")
        if marker not in normalized:
            return False
        name = normalized.split(marker, 1)[1].split('"')[0].split(" ")[0].strip()
        return name in owned

    # 3. Load settings, drop our previous entries, rebuild the current surface.
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    before = json.dumps(settings, indent=2, ensure_ascii=False)
    existing_hooks = settings.setdefault("hooks", {})
    if not isinstance(existing_hooks, dict):
        existing_hooks = settings["hooks"] = {}

    previous = set()
    for event in list(existing_hooks):
        surviving = []
        for group in existing_hooks.get(event) or []:
            if not isinstance(group, dict):
                surviving.append(group)
                continue
            hooks = group.get("hooks", [])
            kept = []
            for hook in hooks:
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if isinstance(command, str) and _is_ours(command):
                    previous.add(command)
                else:
                    kept.append(hook)
            if kept:
                surviving.append({**group, "hooks": kept} if len(kept) != len(hooks) else group)
        if surviving:
            existing_hooks[event] = surviving
        else:
            del existing_hooks[event]

    added = 0
    for event, matcher, rebuilt in surface:
        event_list = existing_hooks.setdefault(event, [])
        target = next(
            (g for g in event_list if isinstance(g, dict) and g.get("matcher", "") == matcher), None
        )
        if target is None:
            target = {"matcher": matcher, "hooks": []} if matcher else {"hooks": []}
            event_list.append(target)
        for command, timeout in rebuilt:
            entry = {"type": "command", "command": command}
            if timeout is not None:
                entry["timeout"] = timeout
            target.setdefault("hooks", []).append(entry)
            if command not in previous:
                added += 1

    after = json.dumps(settings, indent=2, ensure_ascii=False)
    if after == before and settings_path.exists():
        return added
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(after, encoding="utf-8")
    except Exception:
        return 0
    return added


def _main_chain_diff():
    """Hook scripts whose runtime copy is missing or differs from the plugin's, by name."""
    import filecmp
    from pathlib import Path

    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not root:
        return []
    runtime = Path.home() / ".claude" / "hooks" / RUNTIME_HOOKS_DIRNAME
    stale = []
    try:
        sources = sorted((Path(root) / "hooks").glob("*.py"))
    except OSError:
        return []
    for source in sources:
        if source.name == "auto_install.py":
            continue
        copy = runtime / source.name
        try:
            same = copy.is_file() and filecmp.cmp(source, copy, shallow=False)
        except OSError:
            same = False
        if not same:
            stale.append(source.name)
    return stale


def _mark_plugin_chain(notice, plugin_ver) -> None:
    """Record that the plugin installed the global chain (read when the plugin is removed)."""
    if notice is None:
        return
    path = notice.os_data_dir() / "main-chain.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "installed_by": "plugin", "plugin_version": plugin_ver or "",
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }), encoding="utf-8")
    except OSError:
        pass


def _source_owned(notice) -> bool:
    """A source install (install.py) owns the global chain; the plugin must not overwrite its copies."""
    return notice is not None and (notice.os_data_dir() / "install_path.txt").exists()


def _sync_and_check(notice, plugin_ver, state: dict):
    """Converge the main chain; returns the scripts still stale afterwards."""
    _sync_main_chain()
    if _source_owned(notice):
        return []
    remaining = _main_chain_diff()
    if remaining:
        state = dict(state)
        state["sync_failed"] = {"n": len(remaining), "files": remaining[:20],
                                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _write_install_state(notice, state)
    else:
        _mark_plugin_chain(notice, plugin_ver)
        if state.get("sync_failed"):
            state = {k: v for k, v in state.items() if k != "sync_failed"}
            _write_install_state(notice, state)
    return remaining


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _show(notice, payload: dict, catalog_id: str, params: dict, key: str, variant: str = "") -> None:
    """One local line for this session start (once per session per key)."""
    if notice is None:
        return
    source = str(payload.get("source") or "startup")
    got = notice.claim_local(
        catalog_id, params, host="cc", session_id=str(payload.get("session_id") or ""),
        cwd=str(payload.get("cwd") or os.getcwd()), event=f"SessionStart:{source}",
        key=key, variant=variant, reliable=source == "startup",
    )
    if got:
        notice.emit("cc", "SessionStart", user_text=got[0], model_text=got[1])


def _install(notice, payload: dict, plugin_ver, installed_ver, state: dict) -> dict:
    """Run pip once (or report the recorded failure); returns the new install state."""
    fresh = installed_ver is None
    now = time.time()
    interpreter = sys.executable
    version = plugin_ver or "?"

    if (state.get("phase") == "failed" and state.get("interpreter") == interpreter
            and state.get("plugin_version") == plugin_ver
            and now - float(state.get("failed_at_ts") or 0) < RETRY_AFTER_FAILURE_S):
        reason = str(state.get("reason") or "unknown")
        _show(notice, payload, "install_failed", {"py": _python_version()},
              f"install_failed:{version}:{reason}:{state.get('err_hash') or ''}", reason)
        return state

    if state.get("phase") == "installing" and state.get("plugin_version") == plugin_ver:
        started = float(state.get("started_at") or 0)
        running = now - started < (notice.INSTALL_STALE_S if notice else 300)
        if running and state.get("pid") != os.getpid() and _pid_alive(state.get("pid")):
            return state  # another session start is installing; session_bootstrap says so
        attempt = int(state.get("attempt") or 1) + 1
    else:
        attempt = 1

    # Written before any network work, so a concurrent session start can tell
    # "installing" from "service not running".
    state = {"phase": "installing", "plugin_version": plugin_ver, "interpreter": interpreter,
             "started_at": now, "attempt": attempt, "pid": os.getpid()}
    _write_install_state(notice, state)

    reason = _preflight_failure()
    error = ""
    if reason is None:
        ok, error = _pip_install(upgrade=not fresh)
        if ok:
            state = {"phase": "installed", "plugin_version": plugin_ver, "interpreter": interpreter,
                     "finished_at": time.time(), "attempt": attempt}
            _write_install_state(notice, state)
            if fresh:
                _show(notice, payload, "install_done", {"ver": f"v{version}"}, f"install_done:{version}")
            else:
                _show(notice, payload, "install_upgraded", {"ver": f"v{version}", "old": f"v{installed_ver}"},
                      f"install_upgraded:{installed_ver}:{version}")
            return state
        reason = _failure_reason(error or "")
        sys.stderr.write(f"[AI Team OS] pip failed ({reason}): {(error or '')[-300:]}\n")
        sys.stderr.write(f"[AI Team OS] retry by hand: {interpreter} -m pip install --upgrade {GITHUB_URL}\n")
    err_hash = hashlib.sha256(f"{reason}\n{(error or '').strip()[-300:]}".encode("utf-8", "replace")).hexdigest()[:8]
    state = {"phase": "failed", "plugin_version": plugin_ver, "interpreter": interpreter,
             "reason": reason, "err_hash": err_hash, "failed_at_ts": time.time(),
             "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "attempt": attempt}
    _write_install_state(notice, state)
    _show(notice, payload, "install_failed", {"py": _python_version()},
          f"install_failed:{version}:{reason}:{err_hash}", reason)
    return state


def main():
    # Diagnostics go to stderr; stdout is reserved for the hook-result JSON.
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass

    # Self-heal: converge the plugin manifest to this interpreter's absolute path.
    _self_heal_interpreter()

    payload = _read_payload()
    notice = _user_notice()
    plugin_ver = _plugin_version()
    installed_ver = _installed_version()

    fresh = installed_ver is None
    behind = fresh or (
        plugin_ver is not None
        and installed_ver is not None
        and _version_tuple(plugin_ver) > _version_tuple(installed_ver)
    )

    if not behind:
        # Current version: a plugin-owned runtime chain must also match the plugin
        # byte for byte, or the hooks that actually run are older than the plugin.
        # A source install keeps its own copies (install.py --update refreshes them).
        stale = [] if _source_owned(notice) else _main_chain_diff()
        if not stale and _main_chain_registered():
            return  # zero output
        state = notice.read_install_state() if notice else {}
        remaining = _sync_and_check(notice, plugin_ver, state)
        if stale and not remaining:
            digest = hashlib.sha256("\n".join(stale).encode("utf-8")).hexdigest()[:8]
            _show(notice, payload, "installed_copy_synced", {"n": str(len(stale))},
                  f"installed_copy_synced:{digest}")
        return

    state = notice.read_install_state() if notice else {}
    state = _install(notice, payload, plugin_ver, installed_ver, state)
    if state.get("phase") == "installing":
        return  # another session start owns this install
    # Register/refresh the absolute-path runtime main chain (Windows-safe, dedup-able).
    _sync_and_check(notice, plugin_ver, state)


if __name__ == "__main__":
    # Always exit 0: the cross-platform launcher in hooks.json chains a fallback
    # interpreter with `||`, so a non-zero exit here would re-run the whole thing.
    # A failed auto-install must never block the session either (SessionStart).
    try:
        main()
    except Exception:
        pass
