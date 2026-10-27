"""``aiteam paths``: where this session's OS data, Claude Code config and Codex home are.

docs/config-dir-compat-design.md §4.3. Everything comes from the one resolver
(``aiteam.paths``), so a command or skill that needs a location runs this
instead of spelling a path itself. Reads only: nothing is created or changed,
and the API is asked only with ``--api``.

``--locate`` also looks for databases the resolver does not consider: the old
data folder inside the Claude Code config folder, and a bounded walk of the
hidden folders in the home directory (where a tool that moves config folders
around tends to leave them).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

import typer

from aiteam import paths

app = typer.Typer()

# A config folder moved by a tool keeps the old data folder at this place inside it.
_MOVED_DATA = ("data", "ai-team-os")
_WALK_DEPTH = 6
_WALK_DIRS = 20_000
_WALK_SECONDS = 2.0


def _candidate(candidate: paths.DataDirCandidate) -> dict:
    return {key: value for key, value in candidate._asdict().items() if value not in (None, "")}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _main_chain_registered(settings: dict) -> bool:
    hooks = settings.get("hooks")
    return isinstance(hooks, dict) and any(
        "hooks/ai-team-os/" in str(hook.get("command", "")).replace("\\", "/")
        for groups in hooks.values() if isinstance(groups, list)
        for group in groups if isinstance(group, dict)
        for hook in group.get("hooks", []) if isinstance(hook, dict)
    )


def _plugin(config: Path, settings: dict) -> dict:
    plugins = _read_json(config / "plugins" / "installed_plugins.json").get("plugins")
    keys = sorted(key for key in plugins if isinstance(key, str) and key.startswith("ai-team-os@")) \
        if isinstance(plugins, dict) else []
    enabled = settings.get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}
    return {"installed": keys, "enabled": [key for key in keys if enabled.get(key) is not False]}


def _chain_owner() -> dict:
    """Who installed this config folder's main chain, and from what that is known.

    ``record`` is the owner record next to the copies (null without one);
    ``installed_by`` is what the orphan check and the copy check go by (the record,
    else the older machine-wide rule), "" when nothing says.
    """
    return {"file": str(paths.cc_main_chain_dir() / paths.CHAIN_OWNER_FILE), "record": paths.chain_owner(),
            "installed_by": paths.main_chain_owner().get("installed_by", "")}


def _installs() -> dict:
    """The install registry: the newest record of each (host, config folder), newest first.

    For queries only (a moved folder makes a record stale); nothing decides by it.
    """
    records = paths.install_records()
    latest: dict[tuple, dict] = {}
    for record in records:
        latest[(record.get("host"), record.get("config_dir"))] = record
    return {"count": len(records),
            "latest": sorted(latest.values(), key=lambda record: str(record.get("at", "")), reverse=True)}


def _source(name: str) -> str:
    return "env" if os.environ.get(name, "").strip() else "default"


def _walk_for_databases(home: Path) -> list[str]:
    """``data/ai-team-os/aiteam.db`` under the home's hidden folders, bounded in depth, count and time."""
    found: list[str] = []
    deadline = time.monotonic() + _WALK_SECONDS
    visited = 0
    try:
        tops = [entry for entry in os.scandir(home) if entry.name.startswith(".")
                and entry.is_dir(follow_symlinks=False)]
    except OSError:
        return found
    stack = [(Path(entry.path), 1) for entry in tops]
    while stack and visited < _WALK_DIRS and time.monotonic() < deadline:
        directory, depth = stack.pop()
        visited += 1
        if directory.name == _MOVED_DATA[1] and directory.parent.name == _MOVED_DATA[0]:
            if paths.inspect_data_dir("found", directory).state == "database":
                found.append(str(directory))
            continue
        if depth >= _WALK_DEPTH:
            continue
        try:
            for entry in os.scandir(directory):
                if entry.is_dir(follow_symlinks=False) and entry.name not in ("node_modules", ".git"):
                    stack.append((Path(entry.path), depth + 1))
        except OSError:
            continue
    return sorted(found)


def _api(timeout: float = 1.0) -> dict:
    """What the running API reports about its own data directory (asked only with --api)."""
    from aiteam.hooks.hook_core import _get_api_url

    url = _get_api_url().rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=timeout) as response:
            health = json.loads(response.read(65_536))
    except Exception as exc:  # noqa: BLE001 - unreachable is an answer, not a failure
        return {"url": url, "reachable": False, "error": type(exc).__name__}
    health = health if isinstance(health, dict) else {}
    return {"url": url, "reachable": True, "data_dir": health.get("data_dir"),
            "data_dir_mode": health.get("data_dir_mode"), "cc_config_dir": health.get("cc_config_dir")}


def report(*, locate: bool = False, api: bool = False) -> dict:
    """The full report as a JSON-ready dict."""
    resolution = paths.resolve_data_dir()
    config = paths.cc_config_dir()
    settings = _read_json(config / "settings.json")
    global_config = paths.cc_global_config()
    codex = paths.codex_home()
    data = {
        "path": str(resolution.path),
        "mode": resolution.mode,
        "database": str(resolution.path / paths.DB_NAME) if resolution.mode != "missing" else None,
        "evidence": [_candidate(candidate) for candidate in resolution.evidence],
    }
    if locate:
        roles = {candidate.role for candidate in resolution.evidence}
        extra = [] if "config" in roles else [paths.inspect_data_dir("config", config.joinpath(*_MOVED_DATA))]
        data["located"] = [_candidate(candidate) for candidate in extra if candidate.state != "absent"]
        data["found_elsewhere"] = _walk_for_databases(Path.home())
    result = {
        "data_dir": data,
        "claude": {
            "config_dir": str(config),
            "source": _source("CLAUDE_CONFIG_DIR"),
            "settings": str(config / "settings.json"),
            "main_chain_registered": _main_chain_registered(settings),
            "main_chain_dir": str(paths.cc_main_chain_dir()),
            "main_chain_dir_exists": paths.cc_main_chain_dir().is_dir(),
            "global_config": str(global_config),
            "mcp_registered": "ai-team-os" in (_read_json(global_config).get("mcpServers") or {}),
            "plugin": _plugin(config, settings),
            "chain_owner": _chain_owner(),
        },
        "codex": {
            "home": str(codex),
            "source": _source("CODEX_HOME"),
            "hooks_dir": str(codex / "hooks" / "ai-team-os-observer"),
            "receipt_exists": (codex / "hooks" / "ai-team-os-observer" / ".aiteam-codex-install.json").is_file(),
        },
    }
    result["installs"] = _installs()
    if api:
        result["api"] = _api()
    return result


@app.callback(invoke_without_command=True)
def paths_command(
    locate: bool = typer.Option(False, "--locate", help="也去别处找数据库（旧配置目录、家目录下的隐藏目录）"),
    api: bool = typer.Option(False, "--api", help="同时询问正在运行的 API 用的是哪个数据目录"),
) -> None:
    """Print where this session's OS data, Claude Code config and Codex home are (JSON)."""
    typer.echo(json.dumps(report(locate=locate, api=api), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    """``python3 -m aiteam.paths`` entry: same options, same output."""
    import argparse

    parser = argparse.ArgumentParser(prog="python3 -m aiteam.paths", description=__doc__.splitlines()[0])
    parser.add_argument("--locate", action="store_true")
    parser.add_argument("--api", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(report(locate=args.locate, api=args.api), ensure_ascii=False, indent=2))
    return 0
