"""Explicit exec adapter: raw JSONL to caller, minimal facts to a durable spool.

This module never reads authentication/config files, persists arguments or prices
usage, and never runs from a request handler. The CLI is its only execution entry.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import httpx
from pydantic import ValidationError

from aiteam.services.codex_usage_export import _unique_object
from aiteam.storage.external_exec import ExternalExecConflictError, ExternalExecRepository
from aiteam.types import (
    CodexUsageTokens,
    ExternalExecFinish,
    ExternalExecReceipt,
    ExternalExecRegistration,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
MAX_EVENT_BYTES = 256 * 1024


class _OutputRelay:
    """Bounded invocation-local writer; caller backpressure cannot block signals."""

    def __init__(self, output: BinaryIO) -> None:
        self.output = output
        self.queue = queue.Queue(maxsize=4)
        self.failed = False
        self.abandoned = threading.Event()
        try:
            self.descriptor = output.fileno()
        except (AttributeError, OSError):
            self.descriptor = None
        threading.Thread(target=self._write, daemon=True).start()

    def _write(self) -> None:
        while True:
            if self.abandoned.is_set():
                return
            try:
                chunk = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if chunk is None:
                    return
                if self.failed:
                    continue
                try:
                    if self.descriptor is None:
                        self.output.write(chunk)
                        self.output.flush()
                    else:
                        while chunk and not self.abandoned.is_set():
                            chunk = chunk[os.write(self.descriptor, chunk) :]
                except OSError:
                    self.failed = True
            finally:
                self.queue.task_done()

    async def send(self, chunk: bytes, interrupted: Callable[[], bool]) -> bool:
        while self.queue.full() and not self.failed and not interrupted():
            await asyncio.sleep(0.01)
        if self.failed or interrupted():
            return False
        self.queue.put_nowait(chunk)
        return True

    async def finish(self, interrupted: Callable[[], bool]) -> bool:
        while self.queue.unfinished_tasks and not self.failed and not interrupted():
            await asyncio.sleep(0.01)
        complete = not self.failed and not interrupted()
        if complete:
            self.queue.put_nowait(None)
        return complete


class ExternalExecAssociationError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def association_reason(response: httpx.Response) -> str | None:
    """Only versioned association codes are permanent execution blockers."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if code in ("project_not_found", "task_not_found", "task_project_mismatch") else None


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


class ExecEventCollector:
    """Consume a bounded byte stream without retaining content outside each line."""

    def __init__(self, repository: ExternalExecRepository, invocation_id: str) -> None:
        self.repository = repository
        self.invocation_id = invocation_id
        self.thread_id: str | None = None
        self.turn_index = 0
        self.turn_id: str | None = None
        self.turn_open = False
        self.diagnostics: list[str] = []
        self.failed = False
        self.completed = False
        self._pending = bytearray()
        self._discarding = False

    def diagnose(self, code: str) -> None:
        if code not in self.diagnostics and len(self.diagnostics) < 20:
            self.diagnostics.append(code)

    async def feed(self, chunk: bytes) -> None:
        # Large item/command events still reach stdout unchanged but never fill memory.
        for part in chunk.splitlines(keepends=True):
            newline = part.endswith(b"\n")
            if not self._discarding:
                if len(self._pending) + len(part) > MAX_EVENT_BYTES:
                    self._pending.clear()
                    self._discarding = True
                    self.diagnose("oversized_event")
                else:
                    self._pending.extend(part)
            if newline:
                if not self._discarding:
                    await self._event(bytes(self._pending))
                self._pending.clear()
                self._discarding = False

    async def end(self) -> None:
        if self._pending or self._discarding:
            # A complete JSON object without a terminal newline is still an event.
            if self._pending and not self._discarding:
                await self._event(bytes(self._pending))
            self.diagnose("stream_incomplete")
        self._pending.clear()

    async def _event(self, raw: bytes) -> None:
        try:
            event = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            self.diagnose("invalid_event")
            return
        if not isinstance(event, dict):
            self.diagnose("invalid_event")
            return
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = _identifier(event.get("thread_id"))
            if thread_id is None:
                self.diagnose("missing_thread_id")
                return
            try:
                await self.repository.bind_thread(self.invocation_id, thread_id)
                self.thread_id = thread_id
            except ExternalExecConflictError:
                self.diagnose("invalid_event")
        elif kind == "turn.started":
            self.turn_index += 1
            self.turn_id = _identifier(event.get("turn_id"))
            self.turn_open = True
        elif kind == "turn.completed":
            self.completed = True
            # Terminal turn events decide the native result. An earlier error
            # notification can be recoverable (observed native retries before a
            # successful completion); it must not poison this terminal outcome.
            self.failed = False
            native_turn = _identifier(event.get("turn_id")) or self.turn_id
            if native_turn is None:
                self.diagnose("missing_turn_id")
            if self.turn_index == 0:
                # Keep a single unnamed aggregate; never guess the request count.
                self.turn_index = 1
                self.diagnose("stream_incomplete")
            raw_usage = event.get("usage")
            usage = None
            if raw_usage is not None:
                try:
                    if not isinstance(raw_usage, dict):
                        raise ValueError("invalid usage object")
                    usage = CodexUsageTokens.model_validate(
                        {key: value for key, value in raw_usage.items() if key in CodexUsageTokens.model_fields}
                    )
                except (ValueError, ValidationError):
                    self.diagnose("invalid_usage")
            if usage is None or all(value is None for value in usage.model_dump().values()):
                self.diagnose("missing_usage")
                usage = None
            try:
                receipt = ExternalExecReceipt(
                    receipt_id=native_turn or f"turn:{self.turn_index}",
                    thread_id=self.thread_id,
                    turn_id=native_turn,
                    usage=usage,
                    counting_level="thread_aggregate",
                    usage_evidence=(
                        "zero_unverified"
                        if usage is not None and all(number in (None, 0) for number in usage.model_dump().values())
                        else "reported"
                    ),
                    # Exec JSON has no authenticated account or reliable pricing context.
                    metadata_source="unknown",
                )
                await self.repository.receipt(self.invocation_id, receipt)
                if receipt.usage_evidence == "zero_unverified":
                    self.diagnose("usage_zero_unverified")
            except (ValueError, ExternalExecConflictError):
                self.diagnose("invalid_usage")
            self.turn_open = False
        elif kind == "turn.failed":
            self.failed = True
            self.completed = False
            self.turn_open = False
            self.diagnose("event_failure")
        elif kind == "error":
            # Do not persist the message: it may contain endpoints/private data.
            # This is an occurrence diagnostic, not a terminal failure assertion.
            self.diagnose("error_event")


async def run_external_exec(
    repository: ExternalExecRepository,
    registration: ExternalExecRegistration,
    arguments: list[str],
    *,
    executable: str = "codex",
    stdout: BinaryIO,
    warn: Callable[[str], None] | None = None,
) -> int:
    """Return the child's real code; API connectivity cannot overwrite its result.

    Always requests JSONL. Native stdout/stderr use bounded byte relays; stdin
    is inherited. Neither stream is saved in the spool. Arguments are kept
    only in process memory, never in the invocation model.
    """
    await repository.register(registration)
    if os.name != "posix":
        await repository.finish(
            registration.invocation_id,
            ExternalExecFinish(status="failed", exit_code=127, diagnostics=["spawn_failed", "missing_usage"]),
        )
        if warn:
            warn("当前平台不支持本次调用的进程监督；未启动 Codex，登记保留在本地审计账。")
        return 127
    collector = ExecEventCollector(repository, registration.invocation_id)
    relay = _OutputRelay(stdout)
    arguments = list(arguments)
    option_end = arguments.index("--") if "--" in arguments else len(arguments)
    # Existing exec invocations often already request JSON. Clap rejects a second
    # --json; preserve any literal argument after the child's own -- delimiter.
    arguments = [value for index, value in enumerate(arguments) if index >= option_end or value != "--json"]
    option_end = arguments.index("--") if "--" in arguments else len(arguments)
    # --json before exec's optional command, so resume/review work as well.
    command = [executable, "exec", "--json", *arguments]
    if registration.ephemeral and "--ephemeral" not in arguments[:option_end]:
        command.insert(3, "--ephemeral")
    process = None
    interrupted = False
    caught_signal: int | None = None
    installed_signals = []
    termination_task = None
    life_read, life_write = os.pipe()
    status_read, status_write = os.pipe()
    owned_descriptors = {life_read, life_write, status_read, status_write}
    loop = asyncio.get_running_loop()

    def close_owned(descriptor: int) -> None:
        if descriptor not in owned_descriptors:
            return
        owned_descriptors.remove(descriptor)
        try:
            os.close(descriptor)
        except OSError:
            pass

    def interrupt(signum: int) -> None:
        nonlocal interrupted, caught_signal, termination_task
        interrupted, caught_signal = True, signum
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
            if termination_task is None:
                termination_task = loop.create_task(ensure_terminated())

    async def ensure_terminated() -> None:
        # Prevent an ignoring child from holding stdout open forever after Ctrl-C.
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            close_owned(life_write)  # Guardian kills native work on pipe EOF.

    try:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("external_exec_guard.py")),
                "--life-fd",
                str(life_read),
                "--status-fd",
                str(status_write),
                "--db-url",
                repository._db_url,
                "--invocation-id",
                registration.invocation_id,
                "--",
                *command,
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
                pass_fds=(life_read, status_write),
            )
            close_owned(life_read)
            close_owned(status_write)
        except OSError:
            await repository.finish(
                registration.invocation_id,
                ExternalExecFinish(
                    status="failed",
                    exit_code=127,
                    diagnostics=["spawn_failed", "missing_usage"],
                ),
            )
            if warn:
                warn("无法启动 Codex；登记保留在本地审计账。")
            return 127
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(signum, interrupt, signum)
                installed_signals.append(signum)
            except (NotImplementedError, RuntimeError):
                pass
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            if not await relay.send(chunk, lambda: interrupted):
                # A closed downstream pipe interrupts the same execution.
                if not interrupted:
                    interrupt(signal.SIGTERM)
                collector.diagnose("adapter_interrupted")
                collector.diagnose("stream_incomplete")
            await collector.feed(chunk)
        if not await relay.finish(lambda: interrupted):
            collector.diagnose("stream_incomplete")
            if relay.failed and not interrupted:
                interrupt(signal.SIGTERM)
        await collector.end()
        await process.wait()
        packet_raw = os.read(status_read, 4096)
        if packet_raw:
            packet = json.loads(packet_raw)
            code = packet["exit_code"]
            interrupted = interrupted or packet["interrupted"]
            if packet["spawn_failed"]:
                collector.diagnose("spawn_failed")
            if not packet["cleanup_verified"]:
                collector.diagnose("process_cleanup_unverified")
                if warn:
                    warn("本次调用后代完整清理未核实；仅强停已确定进程，原生未知退出码保持未知。")
        else:
            # Guardian status is not the native outcome when the guard dies.
            code = None
            interrupted = True
            collector.diagnose("process_cleanup_unverified")
    except asyncio.CancelledError:
        interrupted = True
        collector.diagnose("adapter_interrupted")
        if process is not None:
            interrupt(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                close_owned(life_write)
                await process.wait()
        packet_raw = os.read(status_read, 4096) if process is not None else b""
        packet = json.loads(packet_raw) if packet_raw else {}
        code = packet.get("exit_code")
        if not packet.get("cleanup_verified", False):
            collector.diagnose("process_cleanup_unverified")
    finally:
        relay.abandoned.set()  # Never join a writer blocked on a dead/slow caller.
        if termination_task is not None:
            termination_task.cancel()
            await asyncio.gather(termination_task, return_exceptions=True)
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        for descriptor in list(owned_descriptors):
            close_owned(descriptor)
    if code is None or code < 0 or interrupted:
        status = "interrupted"
        collector.diagnose("signal_exit")
        collector.diagnose("stream_incomplete")
    else:
        status = "failed" if code or collector.failed else "completed"
    value = await repository.get(registration.invocation_id)
    if value is not None and not any(receipt.usage is not None for receipt in value.receipts.values()):
        collector.diagnose("missing_usage")
    if collector.thread_id is None:
        collector.diagnose("missing_thread_id")
    if collector.turn_open:
        collector.diagnose("stream_incomplete")
    await repository.finish(
        registration.invocation_id,
        ExternalExecFinish(
            status=status,
            exit_code=code,
            thread_id=collector.thread_id,
            diagnostics=collector.diagnostics,
            receipt_ids=list(value.receipts) if value is not None else [],
        ),
    )
    return 75 if code is None else (128 - code if code < 0 else code)


async def upload_invocation(
    repository: ExternalExecRepository,
    invocation_id: str,
    client: httpx.AsyncClient,
    *,
    allow_quarantine: bool = True,
) -> bool:
    """Replay a snapshot; late receipts remain pending if another writer advances it."""
    value = await repository.get(invocation_id)
    if value is None:
        return False
    if value.association_status == "rejected":
        return False  # A known pre-execution rejection is locally resolved.
    registration = ExternalExecRegistration(
        **{name: getattr(value, name) for name in ExternalExecRegistration.model_fields}
    )
    response = await client.post("/api/external-execs", json=registration.model_dump(mode="json"))
    reason = association_reason(response)
    if reason is not None:
        if not allow_quarantine:
            raise ExternalExecAssociationError(reason)
        response = await client.post(
            "/api/external-execs/quarantine",
            json={
                "registration": registration.model_dump(mode="json"),
                "reason_code": reason,
            },
        )
        _confirmed(response)
        if response.json()["data"]["association_status"] == "quarantined":
            value = await repository.association(invocation_id, "quarantined", reason)
        # A racing legitimate registration may have won: preserve its association.
    else:
        _confirmed(response)
    if (
        value.association_status == "unverified"
        and response.json().get("data", {}).get("association_status") == "accepted"
    ):
        value = await repository.association(invocation_id, "accepted")
    if value.thread_id is not None:
        response = await client.post(
            f"/api/external-execs/{invocation_id}/thread",
            json={"thread_id": value.thread_id},
        )
        _confirmed(response)
    for receipt in value.receipts.values():
        response = await client.post(
            f"/api/external-execs/{invocation_id}/receipt",
            json=receipt.model_dump(mode="json"),
        )
        _confirmed(response)
    if value.status != "running":
        finish = ExternalExecFinish(
            status=value.status,
            exit_code=value.exit_code,
            finished_at=value.finished_at,
            thread_id=value.thread_id,
            diagnostics=value.diagnostics,
            receipt_ids=list(value.receipts),
        )
        response = await client.post(
            f"/api/external-execs/{invocation_id}/finish",
            json=finish.model_dump(mode="json"),
        )
        _confirmed(response)
    return await repository.mark_uploaded(invocation_id, value.revision)


def _confirmed(response: httpx.Response) -> None:
    """HTTP success alone is not proof of a durable API acknowledgement."""
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise httpx.HTTPStatusError("external_exec_receipt_not_confirmed", request=response.request, response=response)
