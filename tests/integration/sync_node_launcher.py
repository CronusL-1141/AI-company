"""Start one API node for the two-node sync tests (test_sync_two_nodes.py).

A real uvicorn server over the real app (lifespan, middleware, the edge's worker
subprocess), on 127.0.0.1 at ``SYNC_TEST_PORT`` (0: any free port), printing the
port on the first stdout line. The hardware id is pinned from ``SYNC_TEST_HARDWARE``
so a node.json written by the test holds on any machine. Timer-driven loops stay off
(state reaper, watchdog, account monitor, MCP HTTP), as in the equivalence driver,
except the watchdog when ``SYNC_TEST_WATCHDOG`` gives its patrol interval in seconds.

Usage: PYTHONPATH=<src> python3 sync_node_launcher.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sync_equivalence_driver as driver  # noqa: E402

from aiteam.sync import node as node_module  # noqa: E402


def main() -> None:
    node_module.hardware_uuid = lambda: os.environ["SYNC_TEST_HARDWARE"]
    from aiteam.loop import watchdog

    patrol = watchdog.WatchdogRunner.start
    driver.quiet_background()
    if os.environ.get("SYNC_TEST_WATCHDOG"):
        watchdog.WATCHDOG_CHECK_INTERVAL = float(os.environ["SYNC_TEST_WATCHDOG"])
        watchdog.WatchdogRunner.start = patrol

    import uvicorn

    from aiteam.api.app import create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", int(os.environ.get("SYNC_TEST_PORT", "0"))))
    print(sock.getsockname()[1], flush=True)
    server = uvicorn.Server(uvicorn.Config(create_app, factory=True, log_level="warning", lifespan="on"))
    asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
