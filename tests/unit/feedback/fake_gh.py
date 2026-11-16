"""A stand-in for ``gh``, run as a real program (AITEAM_GH_BIN), over the fake GitHub of fake_github.py.

The command line must be exactly what production sends (``api --hostname github.com``,
``--method``, ``--input -``, one endpoint); anything else exits like gh does on an
unknown flag; the environment must carry the variables production sets. gh acts as
the world's ``login`` (exit 4 when there is none) and reaches every repository.
"""

from __future__ import annotations

import json
import os
import sys

from fake_github import (
    REQUIRED_ENV,
    Exit,
    World,
    call,
    graphql,
    hang,
    http_error,
    log,
    operation,
    read_state,
    rest,
)


def parse_argv(argv: list[str]) -> tuple[str, str, bool]:
    """(method, endpoint, reads stdin) from production's command line; Exit otherwise."""
    if not argv or argv[0] != "api":
        raise Exit(1, "", f"unknown command {argv[0] if argv else ''!r} for \"gh\"\n")
    method, endpoint, hostname, stdin = "", "", "", False
    rest = argv[1:]
    while rest:
        arg = rest.pop(0)
        if arg in ("--hostname", "--method", "--input"):
            if not rest:
                raise Exit(1, "", f"flag needs an argument: {arg}\n")
            value = rest.pop(0)
            if arg == "--hostname":
                hostname = value
            elif arg == "--method":
                if value not in ("GET", "POST"):
                    raise Exit(1, "", f"stub: unexpected method {value}\n")
                method = value
            elif value != "-":
                raise Exit(1, "", "stub: bodies must come on standard input\n")
            else:
                stdin = True
        elif arg.startswith("-"):
            raise Exit(1, "", f"unknown flag: {arg}\n\nUsage:  gh api <endpoint> [flags]\n")
        elif endpoint:
            raise Exit(1, "", f"accepts 1 arg(s), received {len(argv) - 1}\n")
        else:
            endpoint = arg
    if hostname != "github.com":
        raise Exit(1, "", "stub: production always passes --hostname github.com\n")
    if not endpoint:
        raise Exit(1, "", "accepts 1 arg(s), received 0\n")
    return method or ("POST" if stdin else "GET"), endpoint, stdin


def main() -> int:
    state_path, log_path = os.environ["FAKE_GH_STATE"], os.environ["FAKE_GH_LOG"]
    stdin = sys.stdin.read() if "--input" in sys.argv else ""
    entry = {"argv": sys.argv[1:], "stdin": stdin, "op": "", "via": "gh"}
    try:
        missing = {k: v for k, v in REQUIRED_ENV.items() if os.environ.get(k) != v}
        if missing:
            raise Exit(2, "", f"stub: production sets {sorted(missing)}\n")
        method, endpoint, reads = parse_argv(sys.argv[1:])
        try:
            payload = json.loads(stdin) if reads else None
        except ValueError:
            raise http_error(400, "Problems parsing JSON") from None
        entry["op"] = operation(method, endpoint, payload)
        log(log_path, entry)
        hang(read_state(state_path), entry["op"], "before")
        result = call(state_path, World, entry["op"], lambda world: graphql(world, payload) if endpoint == "graphql"
                      else rest(world, method, endpoint, payload))
        hang(read_state(state_path), entry["op"], "after")
        outcome = Exit(0, json.dumps(result))
    except Exit as exc:
        if not entry["op"]:
            log(log_path, entry)
        outcome = exc
    sys.stdout.write(outcome.stdout)
    sys.stderr.write(outcome.stderr)
    return outcome.code


if __name__ == "__main__":
    sys.exit(main())
