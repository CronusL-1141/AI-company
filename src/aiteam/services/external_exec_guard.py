"""Invocation-scoped parent-death supervisor with fixed process birth identities.

The adapter exclusively owns the liveness pipe's writer. EOF therefore catches
SIGKILL as well as ordinary crashes without a global daemon. Native stdin is
inherited; stdout/stderr use bounded byte relays, never inspected or saved. A private process group
lets the guard stop Codex and its active command descendants before exiting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil


class ProcessDiscoveryError(RuntimeError):
    """Process metadata discovery failed; never proof of a closed ownership set."""


class _ExitObserver:
    """Observe native exit without waitpid/poll: its PID cannot be reused yet."""

    def __init__(self, child: subprocess.Popen) -> None:
        self.child = child
        self.events = None
        self.failed = False
        if hasattr(select, "kqueue"):
            try:
                self.events = select.kqueue()
                event = select.kevent(
                    child.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                    fflags=select.KQ_NOTE_EXIT,
                )
                self.events.control([event], 0, 0)
            except OSError:
                # A very short child can be a zombie before registration.
                if psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
                    self.failed = True
        elif not (hasattr(os, "waitid") and hasattr(os, "WNOWAIT")):
            raise ProcessDiscoveryError("native_exit_observer_unavailable")

    @staticmethod
    def supported() -> bool:
        return hasattr(select, "kqueue") or (hasattr(os, "waitid") and hasattr(os, "WNOWAIT"))

    def exited(self) -> bool:
        if self.child.returncode is not None:
            raise ProcessDiscoveryError("native_already_reaped")
        try:
            if self.failed:
                return False
            if self.events is not None:
                return bool(self.events.control(None, 1, 0)) or (
                    psutil.Process(self.child.pid).status() == psutil.STATUS_ZOMBIE
                )
            if hasattr(os, "waitid"):
                result = os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                return result is not None
            return psutil.Process(self.child.pid).status() == psutil.STATUS_ZOMBIE
        except (OSError, psutil.Error):
            self.failed = True
            return False

    def close(self) -> None:
        if self.events is not None:
            self.events.close()


def _trusted_ps() -> str:
    for path in ("/bin/ps", "/usr/bin/ps"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise ProcessDiscoveryError("process_metadata_unavailable")


def _is_api_command(args: list[str]) -> bool:
    """Pure argv entry-point matching, kept aligned with _autostart; no ports."""
    app = "aiteam.api.app:create_app"
    if args[1:3] == ["-m", "uvicorn"]:
        return app in args[3:]
    if args[1:4] == ["-m", "aiteam.cli.app", "up"]:
        return True
    for index in (0, 1):
        if index >= len(args):
            continue
        program = os.path.basename(args[index]).lower().removesuffix(".exe").removesuffix("-script.py")
        if program == "uvicorn":
            return app in args[index + 1 :]
        if program == "aiteam":
            return args[index + 1 : index + 2] == ["up"]
    # Preserve API cold-start _child before its exec and even before record save.
    # Unknown copies are deliberately protected too, not asserted authenticated.
    if len(args) >= 6 and Path(args[1]).name == "codex_runtime.py" and args[2] == "_child":
        try:
            launch = json.loads(args[5])
            return (
                isinstance(launch, list) and all(isinstance(item, str) for item in launch) and _is_api_command(launch)
            )
        except (ValueError, RecursionError):
            return True  # Ambiguous runtime launcher is preserved, never killed.
    return False


_PROTECTED: dict[int, float] = {}
_PARENTS: dict[tuple[int, float], int] = {}


def _is_api_process(process: psutil.Process) -> bool:
    args = process.cmdline()
    if not _is_api_command(args):
        return False
    # Positive executable evidence prevents `/bin/echo -m uvicorn APP` from
    # becoming an API exemption. Unreadable identity is handled conservatively.
    executable = Path(process.exe()).name.lower()
    return executable.startswith("python") or executable in ("uvicorn", "aiteam")


def _tree(root: int) -> dict[int, psutil.Process]:
    """Proven current descendants plus anchored native-session members.

    Only metadata stays in memory. API commands and their whole families are
    preserved before the first signal, including cached identities after exec.
    Fully detached, previously unseen sessions cannot be attributed here.
    """
    try:
        sampled_before = time.time()
        listing = subprocess.run(
            [_trusted_ps(), "-axo", "pid=,ppid=,pgid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=1,
        ).stdout.decode("ascii", errors="strict")
        rows = [tuple(map(int, line.split())) for line in listing.splitlines() if line.strip()]
        if any(len(row) != 3 for row in rows) or not any(pid == root for pid, _, _ in rows):
            raise ProcessDiscoveryError("process_metadata_invalid")
        native_sid = root  # Fixed at Popen(start_new_session=True), not reassigned.
        try:
            if os.getsid(root) != native_sid:
                raise ProcessDiscoveryError("native_session_identity_changed")
        except ProcessLookupError:
            # Darwin getsid(zombie) returns ESRCH while the unreaped PID is held.
            if psutil.Process(root).status() != psutil.STATUS_ZOMBIE:
                raise ProcessDiscoveryError("native_identity_unavailable") from None
        parents = {}
        sampled_parents = {}
        for pid, parent, _ in rows:
            parents.setdefault(parent, []).append(pid)
            sampled_parents[pid] = parent
        candidates = {root}
        pending = [root]
        while pending:
            for pid in parents.get(pending.pop(), ()):
                if pid not in candidates:
                    candidates.add(pid)
                    pending.append(pid)
        for pid, _, _ in rows:
            try:
                if os.getsid(pid) == native_sid:
                    candidates.add(pid)
            except ProcessLookupError:
                pass
        processes = {}
        current_parents = {}
        current_sessions = {}
        protected = set()
        persistent_protection = set()
        for pid in candidates:
            try:
                process = psutil.Process(pid)
                birth = process.create_time()
                processes[pid] = process
                if process.status() == psutil.STATUS_ZOMBIE:
                    if pid == root:
                        current_sessions[pid] = native_sid
                    else:
                        processes.pop(pid, None)
                    continue
                if process.uids().real != os.getuid():
                    raise ProcessDiscoveryError("process_owner_unknown")
                if _PROTECTED.get(pid) == birth or _is_api_process(process):
                    protected.add(pid)
                    persistent_protection.add(pid)
                    _PROTECTED[pid] = birth
                parent = process.ppid()
                current_parents[pid] = parent
                current_sessions[pid] = os.getsid(pid)
                if birth > sampled_before or (parent != sampled_parents[pid] and current_sessions[pid] != native_sid):
                    protected.add(pid)  # Changed snapshot membership is not ownership evidence.
                _PARENTS[(pid, birth)] = parent
            except psutil.NoSuchProcess:
                continue
            except psutil.ZombieProcess:
                if pid == root:
                    processes[pid] = psutil.Process(pid)
            except (psutil.Error, OSError, ValueError):
                # Unknown classification is not permission to signal this family.
                protected.add(pid)
                persistent_protection.add(pid)
        pending = list(protected)
        while pending:
            for pid in parents.get(pending.pop(), ()):
                if pid not in protected:
                    protected.add(pid)
                    pending.append(pid)
        pending = list(persistent_protection)
        while pending:
            for pid in parents.get(pending.pop(), ()):
                if pid not in persistent_protection:
                    persistent_protection.add(pid)
                    pending.append(pid)
        # A transient stale snapshot may become attributable on a later fresh
        # probe; only API/inspection-unknown families get permanent exemptions.
        for pid in persistent_protection:
            if pid in processes:
                _PROTECTED[pid] = processes[pid].create_time()
        if root in protected:
            raise ProcessDiscoveryError("native_identity_protected_or_unknown")
        owned = {root}
        pending = set(processes) - protected - {root}
        while pending:
            added = set()
            for pid in pending:
                parent = current_parents.get(pid)
                if current_sessions.get(pid) == native_sid or (
                    parent in owned
                    and parent in processes
                    and processes[pid].create_time() >= processes[parent].create_time()
                ):
                    owned.add(pid)
                    added.add(pid)
            if not added:
                break
            pending -= added
        return {pid: process for pid, process in processes.items() if pid in owned and pid not in protected}
    except (OSError, ValueError, psutil.Error, subprocess.SubprocessError) as error:
        raise ProcessDiscoveryError("process_metadata_unavailable") from error


def _signal_tree(tree: dict[int, psutil.Process], signum: int) -> bool:
    """Never killpg: a protected API may share a group with ordinary work."""
    success = True
    protected = set()
    parents = {}
    for pid, process in tree.items():
        try:
            birth = process.create_time()
            parents[pid] = {process.ppid(), _PARENTS.get((pid, birth))}
            if _PROTECTED.get(process.pid) == process.create_time() or _is_api_process(process):
                protected.add(pid)
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            protected.add(pid)
    while True:
        found = {pid for pid, family in parents.items() if family & protected} - protected
        if not found:
            break
        protected.update(found)
    for pid in protected:
        try:
            _PROTECTED[pid] = tree[pid].create_time()
        except psutil.Error:
            pass
    for pid, process in tree.items():
        if pid in protected:
            success = False
            continue
        try:
            # psutil validates its cached creation identity before sending.
            process.send_signal(signum)
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError):
            success = False
    return success


def _stop(child: subprocess.Popen, _signum: int, *, hard: bool = True) -> tuple[int | None, bool]:
    """Bounded force-stop of identified work; complete escape exclusion unknown.

    Root must stay unreaped until discovery/signals end. Fully detached unseen
    sessions and deliberate API exemptions prevent a full-tree verified claim.
    """
    del hard
    if child.returncode is not None:
        return child.returncode, False  # No discovery or signal of a reusable PID.
    known = {}
    closed = False
    for _ in range(4):
        try:
            found = _tree(child.pid)
        except ProcessDiscoveryError:
            time.sleep(0.02)
            continue
        new = {(pid, process.create_time()) for pid, process in found.items()} - {
            (pid, process.create_time()) for pid, process in known.items()
        }
        # A same numeric PID with a new birth is never upgraded from an old owner.
        for pid, process in found.items():
            if pid not in known:
                known[pid] = process
        _signal_tree(known, signal.SIGSTOP)
        if not new:
            closed = True
            break
    # Only the unreaped root is safe even if metadata discovery failed.
    if child.pid not in known:
        known[child.pid] = psutil.Process(child.pid)
    _signal_tree(known, signal.SIGKILL)
    try:
        code = child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        code = None
    del closed  # Identified scope closure cannot prove absence of escaped work.
    return code, False


class _QueuedWriter:
    """Invocation-owned daemon writer; blocking caller IO cannot block liveness."""

    def __init__(self, destination: int) -> None:
        self.destination = destination
        self.queue = queue.Queue(maxsize=4)
        self.failed = False
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def _write(self) -> None:
        while True:
            chunk = self.queue.get()
            try:
                if chunk is None:
                    return
                if not self.failed:
                    try:
                        while chunk:
                            chunk = chunk[os.write(self.destination, chunk) :]
                    except OSError:
                        self.failed = True
            finally:
                self.queue.task_done()

    def relay(self, descriptor: int) -> bool:
        try:
            chunk = os.read(descriptor, 65536)
        except BlockingIOError:
            return True
        if not chunk:
            return False
        self.queue.put_nowait(chunk)
        return True

    @property
    def pending(self) -> bool:
        return bool(self.queue.unfinished_tasks)


async def _save_parent_loss(db_url: str, invocation_id: str, exit_code: int | None, cleanup_verified: bool) -> None:
    # Invoked after parent death and bounded cleanup; failure stays unverified.
    # This is the already-owned local invocation, never the shared API database.
    # A worktree caller may insert its package path without exporting PYTHONPATH.
    # Load storage/types beside this guard, never an older installed checkout.
    sys.path.insert(0, str(Path(__file__).parents[2]))
    from aiteam.storage.engine_pool import engine_pool
    from aiteam.storage.external_exec import ExternalExecRepository
    from aiteam.types import ExternalExecFinish

    repository = ExternalExecRepository(db_url)
    try:
        value = await repository.get(invocation_id)
        if value is not None and value.status == "running":
            await repository.finish(
                invocation_id,
                ExternalExecFinish(
                    status="interrupted",
                    exit_code=exit_code,
                    thread_id=value.thread_id,
                    receipt_ids=list(value.receipts),
                    diagnostics=[
                        "parent_lost",
                        "signal_exit",
                        "stream_incomplete",
                        *([] if cleanup_verified else ["process_cleanup_unverified"]),
                    ],
                ),
            )
    finally:
        await engine_pool.get_engine(db_url).dispose()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--life-fd", type=int, required=True)
    parser.add_argument("--status-fd", type=int, required=True)
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    pending_signal = None
    parent_lost = False
    spawned = True
    cleanup_verified = True

    def forwarded(signum, _frame):
        nonlocal pending_signal
        pending_signal = signum

    try:
        if os.name != "posix":
            raise ProcessDiscoveryError("unsupported_process_platform")
        if not _ExitObserver.supported():
            raise ProcessDiscoveryError("native_exit_observer_unavailable")
        _trusted_ps()
        # exec preserves ignored SIGCHLD. Normalize only this private guardian;
        # otherwise the kernel can auto-reap native while returncode stays None.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, forwarded)
        child = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ProcessDiscoveryError):
        spawned, code = False, 127
    else:
        observer = _ExitObserver(child)
        # Native processes receive private pipes. A surviving/uninterruptible
        # native process cannot keep the adapter's outer stdout/stderr open after
        # the bounded guardian exits. The guardian copies bytes, never saves them.
        writers = [_QueuedWriter(1), _QueuedWriter(2)]
        streams = {child.stdout.fileno(): writers[0], child.stderr.fileno(): writers[1]}
        for descriptor in streams:
            os.set_blocking(descriptor, False)
        drain_deadline = None
        stopped = False
        while not observer.exited() or streams or any(writer.pending for writer in writers):
            readable, _, _ = select.select(
                [options.life_fd, *[fd for fd, writer in streams.items() if not writer.queue.full()]], [], [], 0.1
            )
            if readable and options.life_fd in readable and os.read(options.life_fd, 1) == b"":
                parent_lost = True
                code, cleanup_verified = _stop(child, signal.SIGKILL)
                stopped = True
                break
            if observer.failed or pending_signal is not None or any(writer.failed for writer in writers):
                pending_signal = pending_signal or signal.SIGTERM
                code, cleanup_verified = _stop(child, pending_signal)
                stopped = True
                break
            for descriptor in readable:
                if descriptor in streams and not streams[descriptor].relay(descriptor):
                    del streams[descriptor]
            if any(writer.pending for writer in writers):
                # Caller backpressure is ordinary flow control; never truncate
                # queued normal output because its consumer reads slowly.
                drain_deadline = None
            elif observer.exited() and streams:
                if drain_deadline is None:
                    drain_deadline = time.monotonic() + 3
                elif time.monotonic() >= drain_deadline:
                    cleanup_verified = False
                    break
        if not stopped:
            code = child.wait(timeout=3) if observer.exited() else None
        observer.close()
        # Drain only already-produced bytes after a forced stop. This is bounded
        # even if failed discovery leaves an unidentified holder of a native pipe.
        until = time.monotonic() + 1
        while streams and time.monotonic() < until:
            readable, _, _ = select.select(
                [fd for fd, writer in streams.items() if not writer.queue.full()], [], [], 0.02
            )
            if not readable:
                break
            for descriptor in readable:
                if not streams[descriptor].relay(descriptor):
                    del streams[descriptor]
        child.stdout.close()
        child.stderr.close()
        if not stopped and cleanup_verified:
            # Normal completion exits only after both queues have sent every byte.
            for writer in writers:
                writer.queue.put_nowait(None)
        # Adapter may die while the signal cleanup was in progress (TERM→KILL).
        ready, _, _ = select.select([options.life_fd], [], [], 0)
        if ready and os.read(options.life_fd, 1) == b"":
            parent_lost = True
        if parent_lost:
            try:
                asyncio.run(
                    asyncio.wait_for(
                        _save_parent_loss(options.db_url, options.invocation_id, code, cleanup_verified), timeout=5
                    )
                )
            except Exception:
                pass  # Do not expose paths or native content on audit-write failure.
    packet = {
        "exit_code": code,
        "interrupted": parent_lost or pending_signal is not None or not cleanup_verified,
        "parent_lost": parent_lost,
        "spawn_failed": not spawned,
        "cleanup_verified": cleanup_verified,
    }
    try:
        os.write(options.status_fd, json.dumps(packet).encode())
    except BrokenPipeError:
        pass
    finally:
        os.close(options.life_fd)
        os.close(options.status_fd)
    return 75 if code is None else (128 - code if code < 0 else code)


if __name__ == "__main__":
    raise SystemExit(main())
