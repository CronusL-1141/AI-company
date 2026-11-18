#!/usr/bin/env python3
"""Release check: fresh install and update on a PEP 668 interpreter, end to end.

Homebrew Python and the system Python of Debian 12+ / Ubuntu 23.04+ carry the
EXTERNALLY-MANAGED marker, so a plain ``pip install -e .`` is refused there. This
runs both user paths of /os-release step 1 in an Ubuntu 24.04 container (its
system python3, a normal user, no venv) and checks what a user would get:

* fresh: ``python3 install.py`` on the branch tip;
* update: the previous release, installed the way such a user had to (by hand with
  --break-system-packages), then ``python3 install.py --update`` pulling the tip,
  run by that release's own updater.

After each: the installer exits 0, pip records the tip's version, the MCP server
starts from the command the installer registered (outside the checkout, no
PYTHONPATH) and lists its tools, and the API it starts reports the tip's version.

Needs a running Docker daemon (Colima works). Local only: the repository goes
into the container as a git bundle; nothing is pushed or published.

    python3 scripts/check_pep668_lifecycle.py                # tip of the current branch vs the latest tag
    python3 scripts/check_pep668_lifecycle.py --old v1.16.0  # update from another release
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "aiteam-pep668:ubuntu-24.04"
DOCKERFILE = """\
FROM ubuntu:24.04
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
    python3 python3-pip git curl ca-certificates >/dev/null && rm -rf /var/lib/apt/lists/*
USER ubuntu
WORKDIR /home/ubuntu
"""

# Starts the MCP server the way Claude Code does and calls one tool, which starts the API.
MCP_PROBE = """\
import json, os, pathlib, subprocess, sys
from aiteam.paths import cc_global_config  # check() has already proven aiteam imports
entry = json.loads(cc_global_config().read_text())["mcpServers"]["ai-team-os"]
project = pathlib.Path.home() / "some-project"
project.mkdir(exist_ok=True)
env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
server = subprocess.Popen([entry["command"], *entry.get("args", [])], cwd=entry.get("cwd") or project, env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
def call(message, wait=True):
    server.stdin.write(json.dumps(message) + "\\n")
    server.stdin.flush()
    while wait:
        line = server.stdout.readline()
        if not line:
            sys.exit("MCP server exited: " + server.stderr.read()[-800:])
        try:
            return json.loads(line)
        except ValueError:
            continue
init = call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "pep668-check", "version": "0"}}})
call({"jsonrpc": "2.0", "method": "notifications/initialized"}, wait=False)
tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
health = call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "os_health_check", "arguments": {}}})
server.kill()
print(json.dumps({"server": init["result"]["serverInfo"]["version"], "tools": len(tools),
                  "health_error": health.get("error") or health["result"].get("isError")}))
"""

# The two user paths; $1 = the branch, $2 = the previous release tag.
PATHS = """\
set -u
fail() { echo "FAIL: $*"; exit 1; }
check() {  # $1 = path name; the checkout is the current directory
  want=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
  got=$(python3 -m pip show ai-team-os 2>/dev/null | sed -n 's/^Version: //p')
  [ "$got" = "$want" ] || fail "$1: pip records '$got', the checkout is $want"
  probe=$(python3 /work/mcp_probe.py) || fail "$1: MCP server did not start: $probe"
  echo "$1: MCP $probe"
  case "$probe" in *'"server": "'"$want"'"'*'"health_error": '[fn]*) ;; *) fail "$1: MCP probe $probe";; esac
  api=$(curl -s -m 5 localhost:8000/api/health)
  case "$api" in *'"version":"'"$want"'"'*) echo "$1: API $want up";; *) fail "$1: API says $api";; esac
}
cd ~ && git clone -q /work/repo.bundle checkout 2>/dev/null && cd checkout && git checkout -q "$1"
case "$PATH_KIND" in
fresh)
  python3 install.py > ~/install.log 2>&1; rc=$?
  [ $rc -eq 0 ] || { tail -30 ~/install.log; fail "fresh: install.py exited $rc"; }
  check fresh ;;
update)
  git reset -q --hard "$2"
  python3 -m pip install -q --break-system-packages -e . > ~/old-pip.log 2>&1 \
    || fail "update: could not install $2 by hand"
  python3 install.py > ~/install-old.log 2>&1 || true
  python3 install.py --update > ~/update.log 2>&1; rc=$?
  [ $rc -eq 0 ] || { tail -30 ~/update.log; fail "update: install.py --update exited $rc"; }
  [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$1")" ] || fail "update: did not reach the tip of $1"
  check "update from $2" ;;
esac
"""


def sh(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, **kwargs)


def git(*args: str) -> str:
    return sh("git", *args, cwd=ROOT, capture_output=True).stdout.strip()


def run_path(kind: str, branch: str, old: str, work: Path) -> bool:
    name = f"aiteam-pep668-{kind}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    sh("docker", "run", "-d", "--name", name, IMAGE, "sleep", "infinity", capture_output=True)
    try:
        sh("docker", "exec", "-u", "root", name, "mkdir", "-p", "/work")
        for item in work.iterdir():
            sh("docker", "cp", str(item), f"{name}:/work/{item.name}", capture_output=True)
        sh("docker", "exec", "-u", "root", name, "chmod", "-R", "a+rX", "/work")
        result = subprocess.run(["docker", "exec", "-e", f"PATH_KIND={kind}", name,
                                 "bash", "/work/paths.sh", branch, old], text=True)
        return result.returncode == 0
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--branch", default=None, help="branch whose tip is checked (default: the current branch)")
    parser.add_argument("--old", default=None, help="release to update from (default: the latest tag before the tip)")
    args = parser.parse_args()
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode:
        print("FAIL: no running Docker daemon (start Colima or Docker Desktop first)")
        return 2
    branch = args.branch or git("branch", "--show-current")
    old = args.old or git("describe", "--tags", "--abbrev=0", f"{branch}^")
    print(f"PEP 668 lifecycle check: fresh install of {branch} ({git('rev-parse', '--short', branch)}), "
          f"update from {old}")

    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        (work / "Dockerfile").write_text(DOCKERFILE)
        sh("docker", "build", "-q", "-t", IMAGE, str(work), capture_output=True)
        (work / "Dockerfile").unlink()
        sh("git", "bundle", "create", str(work / "repo.bundle"), branch, "--tags", cwd=ROOT, capture_output=True)
        (work / "mcp_probe.py").write_text(MCP_PROBE)
        (work / "paths.sh").write_text(textwrap.dedent(PATHS))
        results = {kind: run_path(kind, branch, old, work) for kind in ("fresh", "update")}

    for kind, ok in results.items():
        print(f"[{'OK' if ok else 'FAIL'}] {kind}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
