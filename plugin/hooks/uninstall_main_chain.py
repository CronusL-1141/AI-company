#!/usr/bin/env python3
"""Remove the global hook chain a removed or disabled plugin left behind (E06).

The plugin's auto_install copies its hooks to ``<CC dir>/hooks/ai-team-os/`` and
registers them in ``<CC dir>/settings.json``. Uninstalling the plugin removes
neither, so every later session keeps running them. The plugin's MCP tools are
gone by then, so this script travels with the chain itself.

Two steps, like every conversation-authorised write (docs/user-notice-design.md §5.9):

    python3 uninstall_main_chain.py
        Preview (read-only): every settings.json hook entry whose command path
        contains hooks/ai-team-os/, every file in that folder, and a token.

    python3 uninstall_main_chain.py --apply <token> --user-quote "<the user's words>"
        Recomputes the preview; refuses if anything changed or the token is
        older than 10 minutes. Backs up settings.json next to itself
        (settings.json.bak-aiteam-<UTC time>), removes those entries, deletes
        the folder, and records the consent for the OS ledger.

It refuses while the plugin is installed and enabled (nothing is orphaned), for
a source install (scripts/uninstall.py in the checkout owns that chain), when
settings.json cannot be parsed, and when the folder is a symbolic link.

The token has no secret: it is a timestamp plus a hash of the previewed
content, so it guarantees "what was previewed is what gets changed", not "the
user saw it"; anyone who can read a preview can re-stamp it, and the user quote
cannot be verified. The consent goes to the API first (no hook runs after the
chain is removed), and to the local record file only when the API is down.
Standard library only; prints one JSON document.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

RUNTIME_HOOKS_DIRNAME = "ai-team-os"
MARKER = f"hooks/{RUNTIME_HOOKS_DIRNAME}/"
PLUGIN_KEY_PREFIX = "ai-team-os@"
TOKEN_TTL_S = 600
API_TIMEOUT_S = 2.0
MAX_QUOTE_CHARS = 120  # CJK is 3 bytes each: the consent record must stay one 1KB line
# Files the chain carries besides the registered hooks.
SUPPORT_FILES = frozenset({"user_notice.py", "hook_core.py", "uninstall_main_chain.py"})


def _notice():
    """The shared notice module next to this file (for the consent record); None if missing."""
    module = sys.modules.get("user_notice")
    if module is not None:
        return module
    path = Path(__file__).resolve().with_name("user_notice.py")
    try:
        spec = importlib.util.spec_from_file_location("user_notice", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["user_notice"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop("user_notice", None)
        return None


def cc_config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def os_data_dir() -> Path:
    return Path.home() / ".claude" / "data" / "ai-team-os"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_settings(path: Path) -> tuple[dict, str]:
    """settings.json as a dict, and an error when it exists but cannot be used."""
    if not path.exists():
        return {}, ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return {}, f"{path} cannot be read as JSON ({type(exc).__name__}); fix it first, then preview again."
    if not isinstance(value, dict):
        return {}, f"{path} is not a JSON object; fix it first, then preview again."
    return value, ""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _is_chain_command(command: object) -> bool:
    return isinstance(command, str) and MARKER in command.replace("\\", "/")


def _plugin_state(config: Path) -> str:
    """"active", "disabled" or "removed"."""
    plugins = _read_json(config / "plugins" / "installed_plugins.json").get("plugins")
    keys = [key for key in plugins if isinstance(key, str) and key.startswith(PLUGIN_KEY_PREFIX)] \
        if isinstance(plugins, dict) else []
    if not keys:
        return "removed"
    enabled = _read_json(config / "settings.json").get("enabledPlugins")
    if isinstance(enabled, dict) and any(enabled.get(key) is False for key in keys):
        return "disabled"
    return "active"


def _without_chain(settings: dict) -> tuple[dict, list[dict]]:
    """settings with every chain hook entry removed, and the removed entries."""
    removed = []
    result = dict(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return result, removed
    kept_events = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            kept_events[event] = groups
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = []
            for hook in group["hooks"]:
                command = hook.get("command") if isinstance(hook, dict) else None
                if _is_chain_command(command):
                    removed.append({"event": event, "matcher": group.get("matcher", ""), "command": command})
                else:
                    kept.append(hook)
            if kept:
                kept_groups.append({**group, "hooks": kept} if len(kept) != len(group["hooks"]) else group)
        if kept_groups:
            kept_events[event] = kept_groups
    if kept_events:
        result["hooks"] = kept_events
    else:
        result.pop("hooks", None)
    return result, removed


def plan() -> dict:
    """Everything the preview shows; the token covers all of it."""
    config = cc_config_dir()
    settings_path = config / "settings.json"
    chain_dir = config / "hooks" / RUNTIME_HOOKS_DIRNAME
    refusal = ""
    settings, settings_error = _read_settings(settings_path)
    if settings_error:
        # Without a readable settings.json the entries cannot be removed, and
        # deleting the folder alone would leave registrations pointing at nothing.
        refusal = settings_error
    elif chain_dir.is_symlink():
        refusal = (f"{chain_dir} is a symbolic link, not the folder the plugin created; remove or "
                   "replace it by hand after checking where it points.")
    elif (os_data_dir() / "install_path.txt").exists():
        refusal = ("This chain belongs to a source install: remove it with scripts/uninstall.py "
                   "in the checkout it was installed from.")
    elif _plugin_state(config) == "active":
        refusal = ("The plugin is installed and enabled, so its chain is not left over; it would be "
                   "put back at the next session start.")
    _rest, removed = _without_chain(settings)
    referenced = {entry["command"].replace("\\", "/").split(MARKER, 1)[1].split('"')[0].split(" ")[0]
                  for entry in removed}
    files = []
    if chain_dir.is_dir():
        for path in sorted(chain_dir.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(chain_dir).as_posix()
                files.append({"name": relative, "sha256": _sha256(path),
                              "unrecognized": relative not in referenced and relative not in SUPPORT_FILES})
    unrecognized = [item["name"] for item in files if item["unrecognized"]]
    warnings = []
    if unrecognized:
        warnings.append("These files in the folder are neither registered chain hooks nor its support "
                        "modules and would be deleted too: " + ", ".join(unrecognized))
    return {
        "settings": str(settings_path),
        "settings_sha256": _sha256(settings_path),
        "folder": str(chain_dir),
        "entries": removed,
        "files": files,
        "plugin": _plugin_state(config),
        "warnings": warnings,
        "refusal": refusal,
    }


def _token(current: dict, issued: int) -> str:
    body = json.dumps({key: current[key] for key in ("settings", "settings_sha256", "folder", "entries", "files")},
                      sort_keys=True, ensure_ascii=False)
    return f"{issued}.{hashlib.sha256(f'{issued}|{body}'.encode()).hexdigest()[:32]}"


def preview(now: float | None = None) -> dict:
    current = plan()
    nothing = not current["entries"] and not current["files"]
    token = "" if current["refusal"] or nothing else _token(current, int(now if now is not None else time.time()))
    return {"mode": "preview", **current, "nothing_to_do": nothing, "confirm_token": token,
            "expires_in_s": TOKEN_TTL_S if token else 0}


def _write_json_atomic(path: Path, value: dict) -> None:
    handle, temporary = tempfile.mkstemp(prefix=".aiteam-uninstall-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False))
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def apply(token: str, user_quote: str, now: float | None = None) -> dict:
    quote = (user_quote or "").strip()
    if not quote:
        return {"success": False, "error": "--user-quote is required: the user's own words approving the preview."}
    moment = now if now is not None else time.time()
    current = plan()
    if current["refusal"]:
        return {"success": False, "error": current["refusal"]}
    issued_text = (token or "").partition(".")[0]
    if not issued_text.isdigit():
        return {"success": False, "error": "The token is malformed; run the preview again."}
    if not 0 <= moment - int(issued_text) <= TOKEN_TTL_S:
        return {"success": False, "error": "The token has expired (10 minutes); run the preview again."}
    if _token(current, int(issued_text)) != token:
        return {"success": False, "error": "Something changed since the preview; run it again and show it to the user."}

    settings_path = Path(current["settings"])
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(moment))
    notice = _notice()  # load before the folder (and the module file) goes away
    backup = ""
    error = ""
    settings_changed = False
    folder = Path(current["folder"])
    try:
        if current["entries"]:
            backup = f"{settings_path}.bak-aiteam-{stamp}"
            shutil.copy2(settings_path, backup)
            settings, settings_error = _read_settings(settings_path)
            if settings_error:
                raise OSError(settings_error)
            rest, _removed = _without_chain(settings)
            _write_json_atomic(settings_path, rest)
            settings_changed = True
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    data = {
        "change": "remove_main_chain", "notice_key": "orphan_main_chain", "host": "cc",
        "tool": "uninstall_main_chain.py", "settings": str(settings_path), "backup": backup,
        "settings_changed": settings_changed, "entries_removed": len(current["entries"]) if settings_changed else 0,
        "folder_removed": str(folder) if not folder.exists() else "",
        "files_removed": len(current["files"]) if not folder.exists() else 0,
        "user_quote": quote[:MAX_QUOTE_CHARS],
    }
    if error:
        data.update(status="partial" if settings_changed else "failed", error=error[:300])
    recorded = _record(notice, data, clear=not error)
    if error:
        return {"success": False, "mode": "applied", **data, "consent_recorded": recorded,
                "hint": "The backup of settings.json (if any) is next to it; nothing else was changed."}
    return {"success": True, "mode": "applied", **data, "consent_recorded": recorded}


def _api_url(notice) -> str:
    if notice is not None:
        try:
            return notice.api_url()
        except Exception:
            pass
    configured = os.environ.get("AITEAM_API_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    try:
        return f"http://localhost:{int((os_data_dir() / 'api_port.txt').read_text(encoding='utf-8').strip())}"
    except (OSError, ValueError):
        return "http://localhost:8000"


def _post(url: str, body: dict | None) -> bool:
    # ASCII escapes, not raw UTF-8: a lone surrogate has no UTF-8 form and would stop
    # the request here; escaped, it reaches the API, which replaces it for this route.
    data = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local API, never a proxy
    try:
        with opener.open(request, timeout=API_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _record(notice, data: dict, *, clear: bool) -> str:
    """Record the consent: the API first (no hook runs after the chain is gone), else the local file.

    Returns "api", "local" or "none".
    """
    record = {"uuid": uuid.uuid4().hex, "kind": "consent",
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ts": round(time.time(), 3),
              "source": "uninstall_main_chain.py", **data}
    base = _api_url(notice)
    if _post(f"{base}/api/notices/consent", record):
        if clear:
            _post(f"{base}/api/notices/orphan_main_chain/clear", None)
        if notice is not None and clear:
            try:
                notice.clear_local("cc", "orphan_main_chain")
            except Exception:
                pass
        return "api"
    if notice is None:
        return "none"
    try:
        # record_local takes the host positionally; the importer stamps it on the event.
        notice.record_local("cc", "consent", **{k: v for k, v in data.items() if k != "host"})
        if clear:
            notice.clear_local("cc", "orphan_main_chain")
        return "local"
    except Exception:
        return "none"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--apply", metavar="TOKEN", default="")
    parser.add_argument("--user-quote", default="")
    args = parser.parse_args(argv)
    result = apply(args.apply, args.user_quote) if args.apply else preview()
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result.get("success", True) else 1


if __name__ == "__main__":
    sys.exit(main())
