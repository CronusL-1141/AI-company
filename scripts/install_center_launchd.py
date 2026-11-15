#!/usr/bin/env python3
"""Keep a multi-host sync center's API alive with launchd (macOS, center role only).

The single exception to "no timers or background daemons" (CLAUDE.md, deliberate
decisions; task 30215c35): the center must come back on its own after the machine
restarts, without a session to start it. launchd only restarts the API process; it
schedules nothing. The job runs the same command the MCP autostart runs, from the
checkout this script lives in, bound to 127.0.0.1, with the PATH of the shell that
installs it (launchd's bare PATH would hide gh, claude and codex from the API).

Refuses unless this machine's node.json declares the center role and holds here.
Prints the plist and the launchctl commands and stops, unless given ``--apply``;
``--remove`` unloads the job and deletes the plist.

Usage: python3 scripts/install_center_launchd.py [--apply] [--remove] [--port 8000]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "launchd" / "center-api.plist.template"
LABEL = "com.aiteam-os.center-api"
LOG_NAME = "center-api.launchd.log"
# Keys that would make launchd schedule runs: the exception covers keeping alive only.
SCHEDULING_KEYS = ("StartInterval", "StartCalendarInterval", "WatchPaths", "QueueDirectories", "StartOnMount")


def render(*, python: str, repo: Path, port: int, log: Path, path: str, label: str = LABEL) -> str:
    """The plist; ``path`` is the PATH the API gets (launchd's own lacks homebrew and ~/.local/bin)."""
    text = TEMPLATE.read_text(encoding="utf-8")
    return text.format(label=label, python=python, repo=repo, port=port, log=log, path=escape(path))


def plist_path(home: Path, label: str = LABEL) -> Path:
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def center_refusal(db_path: Path) -> str:
    """Why this machine may not run the center service, or ''."""
    from aiteam.sync.node import load_node_status, node_file
    from aiteam.types import NodeRole

    status = load_node_status(node_file(db_path))
    if status.refusal:
        return status.refusal
    if status.role is not NodeRole.CENTER:
        return f"本机节点角色是 {status.role}，只有中心节点才常驻 API"
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("launchd 只在 macOS 上")
        return 2
    sys.path.insert(0, str(ROOT / "src"))
    from aiteam.storage.connection import default_db_path

    db_path = default_db_path()
    target = plist_path(Path.home())
    domain = f"gui/{os.getuid()}"
    if args.remove:
        commands = [["launchctl", "bootout", f"{domain}/{LABEL}"]]
        print("\n".join(" ".join(c) for c in commands) + f"\nrm {target}")
        if args.apply:
            subprocess.run(commands[0], check=False)
            target.unlink(missing_ok=True)
        return 0
    refusal = center_refusal(db_path)
    if refusal:
        print(f"拒绝安装：{refusal}")
        return 1
    content = render(python=sys.executable, repo=ROOT, port=args.port, log=db_path.parent / LOG_NAME,
                     path=os.environ.get("PATH", ""))
    commands = [["launchctl", "bootstrap", domain, str(target)]]
    print(f"# {target}\n{content}")
    print("\n".join(" ".join(c) for c in commands))
    if not args.apply:
        print("（预演：加 --apply 写入并加载；先停掉本机正在运行的 API）")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return subprocess.run(commands[0], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
