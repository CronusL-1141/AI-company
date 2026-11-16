"""Two real nodes and the tunnel between them, for test_sync_two_nodes.py.

Each node is its own API process (sync_node_launcher.py) over its own HOME and data
directory; the edge reaches the center only through ``Tunnel``, a loopback TCP
forwarder standing in for the SSH local forward. Stopping the tunnel closes its
port, as a dropped SSH forward does: the edge's connections are refused. The
tunnel can also hold the center's answers back, to make the edge time out.

Processes are started in their own process groups and stopped by those groups
only (the edge's worker is a child of the edge's API).

``Cluster.assert_one_writer_per_origin`` brings a cluster back to steady state
(processes up, tunnel open, queue drained, session face up) and checks that the
center holds exactly the edge's session rows: every two-node test ends with it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from aiteam.sync.cli import session_face_differences
from aiteam.sync.node import write_node_identity
from aiteam.sync.tables import SESSION_TABLES_SQL
from aiteam.types import NodeIdentity, NodeRole

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).with_name("sync_node_launcher.py")
CENTER_ID, EDGE_ID = "mini-c0de", "air-ed9e"
CENTER_HW, EDGE_HW = "CCCCCCCC-0000-0000-0000-00000000000C", "EEEEEEEE-0000-0000-0000-00000000000E"


def wait_for(condition, timeout: float = 15.0, interval: float = 0.05, what: str = "condition") -> Any:
    """Poll until ``condition()`` is truthy; its value, or AssertionError at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


class Node:
    """One API process with its own data directory."""

    def __init__(self, root: Path, node_id: str, hardware: str) -> None:
        self.root = root
        self.node_id = node_id
        self.hardware = hardware
        self.data = root / "data"
        self.home = root / "home"
        self.db = self.data / "aiteam.db"
        self.data.mkdir(parents=True, exist_ok=True)
        (self.home / ".aiteam").mkdir(parents=True, exist_ok=True)
        self.port = 0
        self.process: subprocess.Popen | None = None
        self.log = root / "api.log"
        self.started_with: dict[str, str] = {}

    def env(self, **extra: str) -> dict[str, str]:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "HOME": str(self.home),
               "AITEAM_DATA_DIR": str(self.data), "AITEAM_DB_PATH": str(self.db),
               "AITEAM_DIAGNOSTICS_ENABLED": "0", "SYNC_TEST_HARDWARE": self.hardware,
               "SYNC_TEST_PORT": str(self.port), "AITEAM_SYNC_PROBE_INTERVAL": "0.3",
               "AITEAM_SYNC_PULL_INTERVAL": "0.1",
               "AITEAM_SYNC_PROBE_TIMEOUT": "1", **extra}
        for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(proxy, None)
        return env

    def identify(self, role: NodeRole, center_url: str = "") -> None:
        write_node_identity(self.data / "node.json", NodeIdentity(
            node_id=self.node_id, role=role, center_url=center_url, hardware_uuid=self.hardware))

    def start(self, **extra: str) -> None:
        self.started_with = extra
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, str(LAUNCHER)], env=self.env(**extra), cwd=self.root,
                stdout=subprocess.PIPE, stderr=log, start_new_session=True)
        line = self.process.stdout.readline()  # type: ignore[union-attr]
        assert line, f"node {self.node_id} did not start: {self.log.read_text(errors='replace')[-3000:]}"
        self.port = int(line)
        wait_for(self._healthy, timeout=60, what=f"{self.node_id} to answer")

    def _healthy(self) -> bool:
        try:
            return httpx.get(f"{self.url}/api/health", timeout=1, trust_env=False).status_code == 200
        except httpx.HTTPError:
            return False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self, timeout: float = 20.0) -> httpx.Client:
        return httpx.Client(base_url=self.url, timeout=timeout, trust_env=False)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def hook(self, session: str, event: str, cwd: str = "/work/shared", **fields: Any) -> None:
        """One hook event of a session on this node, as the hook scripts post it."""
        with self.client() as client:
            response = client.post("/api/hooks/event", json={
                "hook_event_name": event, "session_id": session, "cwd": cwd, "transcript_path": "", **fields})
        assert response.status_code == 200, response.text

    def drive_session(self, session: str, *, subagents: tuple[str, ...] = ("team-member",), tools: int = 1,
                      cwd: str = "/work/shared") -> str:
        """A session with tool calls and sub-agents on this node; returns its session team's id."""
        self.hook(session, "SessionStart", cwd, source="startup")
        self.hook(session, "UserPromptSubmit", cwd, prompt="work")
        for n in range(tools):
            tool = {"tool_name": "Bash", "tool_input": {"command": f"echo {n}"}, "tool_use_id": f"tu-{session}-{n}"}
            self.hook(session, "PreToolUse", cwd, **tool)
            self.hook(session, "PostToolUse", cwd, **tool, tool_response={"stdout": f"{n}\n"})
        for n, agent_type in enumerate(subagents):
            self.hook(session, "SubagentStart", cwd, agent_id=f"{session}-agent-{n}", agent_type=agent_type)
        return wait_for(lambda: self.sql("SELECT id FROM teams WHERE name = ?", f"session-{session[:8]}"),
                        what=f"the session team of {session}")[0][0]

    def kill(self) -> None:
        """SIGKILL the API process alone, as a crash would; its worker sees stdin close."""
        if self.process is not None and self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)

    def stop(self) -> None:
        """Stop gracefully (lifespan shutdown), then clear the process group."""
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()

    def sql(self, query: str, *params: Any) -> list[tuple]:
        con = sqlite3.connect(self.db, timeout=30)
        try:
            return con.execute(query, params).fetchall()
        finally:
            con.close()

    def write(self, query: str, *params: Any) -> None:
        con = sqlite3.connect(self.db, timeout=30)
        try:
            con.execute(query, params)
            con.commit()
        finally:
            con.close()


def copy_database(source: Path, target: Path) -> None:
    """The cut-over's whole-database copy, consistent even with a WAL beside it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


class Tunnel:
    """A loopback port forwarding to the center, which can be cut and slowed."""

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.port = 0
        self.delay = 0.0
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run(self, coroutine) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=10)

    @property
    def is_open(self) -> bool:
        return self._server is not None

    def open(self) -> None:
        async def start() -> int:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
            self._server = await asyncio.start_server(self._handle, sock=sock)
            return sock.getsockname()[1]

        self.port = self._run(start())

    def cut(self) -> None:
        """Close the port and every open connection: the edge is refused from now on."""
        async def stop() -> None:
            if self._server is not None:
                self._server.close()
                self._server = None
            for writer in list(self._writers):
                writer.transport.abort()
            self._writers.clear()

        self._run(stop())

    def close(self) -> None:
        self.cut()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            writer.transport.abort()
            return
        self._writers.update({writer, up_writer})

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter, delayed: bool) -> None:
            try:
                while data := await source.read(65536):
                    if delayed and self.delay:
                        await asyncio.sleep(self.delay)
                    sink.write(data)
                    await sink.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                sink.transport.abort()

        await asyncio.gather(pipe(reader, up_writer, False), pipe(up_reader, writer, True))
        self._writers.discard(writer)
        self._writers.discard(up_writer)


class Cluster:
    """A center and an edge cut over from one baseline, joined by a tunnel."""

    def __init__(self, root: Path) -> None:
        self.center = Node(root / "center", CENTER_ID, CENTER_HW)
        self.edge = Node(root / "edge", EDGE_ID, EDGE_HW)
        self.tunnel: Tunnel | None = None
        self.project_id = ""
        self.team_id = ""  # a session team of the baseline: on both nodes, governed by the edge
        # Differences a test has accounted for (rows a fault made the center write itself).
        self.tolerated: set[str] = set()

    def cut_over(self, *, center_env: dict[str, str] | None = None, **edge_env: str) -> None:
        """The cut-over: one baseline, both identities, center first, then the edge."""
        self.center.start()  # standalone: builds the database the baseline is taken from
        with self.center.client() as client:
            project = client.post("/api/projects", json={"name": "shared", "root_path": "/work/shared"}).json()
            self.project_id = project["data"]["id"]
            client.post(f"/api/projects/{self.project_id}/tasks", json={"title": "baseline task"})
            session = {"session_id": "base0000-session", "cwd": "/work/shared"}
            for event in ({"hook_event_name": "SessionStart", "source": "startup"},
                          {"hook_event_name": "UserPromptSubmit", "prompt": "hi"},
                          {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "true"},
                           "tool_use_id": "tu-base"}):
                client.post("/api/hooks/event", json={**session, **event})
        self.team_id = wait_for(lambda: self.center.sql("SELECT id FROM teams LIMIT 1"),
                                what="a session team of the baseline")[0][0]
        self.center.stop()
        copy_database(self.center.db, self.edge.db)
        self.center.identify(NodeRole.CENTER)
        self.center.start(**(center_env or {}))
        self.tunnel = Tunnel(self.center.port)
        self.tunnel.open()
        self.edge.identify(NodeRole.EDGE, center_url=self.tunnel.url)
        self.edge.start(**edge_env)
        self.wait_attached()

    def link(self) -> str:
        rows = self.edge.sql("SELECT link FROM sync_peers WHERE peer = 'center'")
        return rows[0][0] if rows else ""

    def wait_attached(self, timeout: float = 20.0) -> None:
        wait_for(lambda: self.link() == "attached", timeout=timeout, what="the edge to attach")

    def wait_detached(self, timeout: float = 20.0) -> None:
        wait_for(lambda: self.link() == "detached", timeout=timeout, what="the edge to detach")

    def pending(self) -> int:
        return self.edge.sql("SELECT count(*) FROM sync_outbox WHERE state = 'pending'")[0][0]

    def uploaded(self) -> bool:
        """The edge's session face is in on the center as far as the edge has written it."""
        up = self.edge.sql("SELECT up_cursor FROM sync_peers WHERE peer = 'center'")
        top = self.edge.sql(
            f"SELECT coalesce(max(seq), 0) FROM sync_changes WHERE table_name IN ({SESSION_TABLES_SQL})")[0][0]
        return bool(up) and up[0][0] >= top

    def wait_uploaded(self, timeout: float = 30.0) -> None:
        wait_for(self.uploaded, timeout=timeout, what="the edge's session face to be up")

    def converge(self, timeout: float = 60.0) -> None:
        """Back to steady state: both APIs up, the tunnel open, the queue drained, the session face up."""
        assert self.tunnel is not None
        if not self.center.running:
            self.center.start(**self.center.started_with)
            self.tunnel.target_port = self.center.port
        if not self.tunnel.is_open:
            self.tunnel.open()
        self.tunnel.delay = 0.0
        if not self.edge.running:
            self.edge.start(**self.edge.started_with)
        self.wait_attached(timeout=timeout)
        wait_for(lambda: self.pending() == 0, timeout=timeout, what="the queue to drain")
        self.wait_uploaded(timeout=timeout)

    def session_differences(self) -> list[str]:
        return session_face_differences(self.edge.db, self.center.db, EDGE_ID)

    def assert_one_writer_per_origin(self) -> None:
        """The center holds exactly the edge's session rows: nothing written there for the edge.

        Background loops a test turned on (the watchdog) keep writing on the edge: a row
        written after the upload was checked is the edge running ahead, not a divergence,
        so the comparison is taken again once that row is up too.
        """
        self.converge()
        differences: list[str] = []

        def agree() -> bool:
            nonlocal differences
            if not self.uploaded():
                return False
            differences = [line for line in self.session_differences() if line not in self.tolerated]
            return not differences

        try:
            wait_for(agree, timeout=10, interval=0.5, what="the session faces to agree")
        except AssertionError:
            pass
        assert differences == [], differences[:20]

    def close(self) -> None:
        for node in (self.edge, self.center):
            node.stop()
        if self.tunnel is not None:
            self.tunnel.close()


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
