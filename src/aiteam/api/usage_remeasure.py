"""Second chance to measure a CC sub-agent's usage: the reaper re-parses what SubagentStop missed.

SubagentStop is the only event that measures a sub-agent (hook_translator
``_capture_subagent_usage``). When it never fires - the workflow was killed, the
session crashed - the row stays unmeasured for good, and before 157bc58 a parse
that raced CC's last write stored a short count that nothing ever corrected
(09-30: 342 rows unmeasured with their transcript on disk; 136 of 631 measured
rows short, down to 22 of 9,424 output tokens). P-5 (09-30, the founder's call):
re-measure inside the existing reaper cycle - no new timer, no daemon.

Each reaper cycle (``StateReaper._remeasure_subagent_usage``):

1. lands the previous batch's results, when that batch has finished;
2. picks offline CC sub-agent rows whose transcript is on disk and either was
   never measured or has changed since it was (stat only, in a worker thread);
3. starts the next batch: one child interpreter parses every picked file.

Parsing is pure Python and runs about 20 ms per MB, and one long line decodes in
a single call that holds the GIL: a 6 MB transcript parsed in a thread stalled the
event loop 75 ms. So the parse runs in a child process (the rule for second-scale
pure-Python work), launched and awaited from a task the reaper owns: the child
spends a second or more just importing, and a batch that has to fit into one
reaper step budget would be killed on a loaded machine before it measured
anything. The results are written the next cycle, through the same row update as
SubagentStop (full re-parse, overwrite).

A result is written only when the file did not change while it was parsed and
still has that content, and - checked in the UPDATE itself - the row is still
offline with the measurement it had when the batch was picked: anything else is a
newer state, which the next cycle picks up. The measurement time written is when
the parse started, so any later write to the file makes the row a candidate again.

Rows in flight (``usage_coverage.is_in_flight``: busy, or active within the last
30 minutes) are not picked: their file may still be growing, and no conclusion is
drawn about them, the rule the unattributed classification uses.

A file that parsed but holds no billed call - no assistant line at all, or only
synthetic ones (rate limit, server error) - is never written as "measured 0" (the
founder's P-3): the row stays unmeasured and gets the same ``usage_unmeasurable``
verdict SubagentStop gives it (``token_attribution.unmeasurable_reason``), and is
left alone until the file changes. A later parse that does find usage clears it.

A batch that fails as a whole (timeout, crash) is retried at half the size, down to
single files; only a single file that fails REMEASURE_MAX_FAILURES times in a row
is left alone until it changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Iterable, MutableMapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from aiteam.api import agent_context
from aiteam.api.usage_parse_child import unbilled
from aiteam.clock import from_timestamp
from aiteam.config.settings import REAPER_CHECK_INTERVAL
from aiteam.services.usage_coverage import is_in_flight
from aiteam.storage.repository import StorageRepository, UsageRemeasureCandidate
from aiteam.types import AgentStatus, TokenSource, UnattributedReason

logger = logging.getLogger(__name__)

# Rows older than this are left alone: CC deletes transcripts after 30 days.
REMEASURE_WINDOW = timedelta(days=30)
# Rows read per cycle. The 30-day window holds about 2,500 offline CC sub-agents.
REMEASURE_CANDIDATE_LIMIT = 5000
# One batch: at most this many files and bytes (a single larger file still goes, alone).
# 128 MB parse in about 3 s; the backlog of 09-30 (about 1,300 files, 1.2 GB) drains
# in about 20 cycles.
REMEASURE_MAX_FILES = 64
REMEASURE_MAX_BYTES = 128 * 1024 * 1024
# A file written this recently may still be growing: leave it for a later cycle.
REMEASURE_QUIET_SECONDS = 5.0
REMEASURE_CHILD_TIMEOUT_SECONDS = 120.0
# A file that failed alone this many times in a row is left alone until it changes.
REMEASURE_MAX_FAILURES = 3
# Bound on each in-process ledger (see RemeasureLedger).
REMEASURE_SKIP_MAX_ENTRIES = 5000
_LAYERS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")


class RemeasureJob(NamedTuple):
    """One file to parse, and the row state a result may be written over."""

    agent_id: str
    path: str
    measured_at: datetime | None  # the row's tokens_measured_at when picked


class RemeasureResult(NamedTuple):
    job: RemeasureJob
    usage: dict[str, Any] | None
    # (size, mtime_ns) the parse covered; None when the file changed during the parse
    # or was gone.
    signature: tuple[int, int] | None
    read_at: float  # epoch seconds when the parse started
    error: str = ""  # the parse raised: nothing to write for this content
    unmeasurable: str | None = None  # why an unbilled parse has nothing to count


class RemeasureChildError(RuntimeError):
    """The parse child failed as a whole (could not start, crashed, timed out, bad output)."""


def _bounded(ledger: MutableMapping[str, Any]) -> None:
    """Drop the oldest half of a ledger that outgrew REMEASURE_SKIP_MAX_ENTRIES."""
    if len(ledger) > REMEASURE_SKIP_MAX_ENTRIES:
        for key in list(ledger)[: len(ledger) // 2]:
            del ledger[key]


class RemeasureLedger:
    """What the reaper remembers per row between cycles; in process, so a restart forgets it.

    Keyed by agent id, each entry holds the (size, mtime_ns) of the file it is about:
    once the file changes, the entry no longer applies.
    """

    def __init__(self) -> None:
        self.nothing_to_write: dict[str, tuple[int, int]] = {}
        self.failures: dict[str, tuple[tuple[int, int], int]] = {}

    def excluded(self, agent_id: str, signature: tuple[int, int]) -> bool:
        if self.nothing_to_write.get(agent_id) == signature:
            return True
        failed = self.failures.get(agent_id)
        return failed is not None and failed[0] == signature and failed[1] >= REMEASURE_MAX_FAILURES

    def parsed_without_usage(self, agent_id: str, signature: tuple[int, int]) -> None:
        self.nothing_to_write[agent_id] = signature
        self.failures.pop(agent_id, None)
        _bounded(self.nothing_to_write)

    def failed(self, agent_id: str, signature: tuple[int, int]) -> None:
        previous = self.failures.get(agent_id)
        count = previous[1] + 1 if previous is not None and previous[0] == signature else 1
        self.failures[agent_id] = (signature, count)
        _bounded(self.failures)

    def written(self, agent_id: str) -> None:
        self.failures.pop(agent_id, None)


def plan_batch(
    candidates: Iterable[UsageRemeasureCandidate],
    ledger: RemeasureLedger,
    *,
    now: float,
    max_files: int = REMEASURE_MAX_FILES,
) -> tuple[list[RemeasureJob], int]:
    """Blocking: locate and stat each candidate's transcript; returns (batch, rows left over).

    Candidates come never-measured first. A row is due when it is not in flight,
    its transcript is on disk, has been quiet for REMEASURE_QUIET_SECONDS, was never
    measured or changed after it was, and the ledger does not hold this very file
    content back.
    """
    moment = from_timestamp(now)
    index = agent_context.build_transcript_index(ttl=float(REAPER_CHECK_INTERVAL))
    jobs: list[RemeasureJob] = []
    used = 0
    left_over = 0
    for candidate in candidates:
        if is_in_flight(AgentStatus.OFFLINE, candidate.last_active_at, moment):
            continue
        path = agent_context.locate_transcript(
            stored_path=candidate.stored_path,
            cc_tool_use_id=candidate.cc_tool_use_id,
            session_id=candidate.session_id,
            project_root=candidate.project_root,
            index=index,
        )
        if path is None:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if ledger.excluded(candidate.agent_id, (st.st_size, st.st_mtime_ns)):
            continue
        measured_at = candidate.measured_at
        if measured_at is not None and st.st_mtime <= measured_at.timestamp():
            continue
        if now - st.st_mtime < REMEASURE_QUIET_SECONDS:
            continue
        if jobs and (len(jobs) >= max_files or used + st.st_size > REMEASURE_MAX_BYTES):
            left_over += 1
            continue
        used += st.st_size
        jobs.append(RemeasureJob(agent_id=candidate.agent_id, path=str(path), measured_at=measured_at))
    return jobs, left_over


# The child imports only the parser module (see usage_parse_child), from the
# parent's own source tree (argv[1]), so both sides run the same parser.
_CHILD_BOOT = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from aiteam.api.usage_parse_child import main; main()"
)


async def run_batch(jobs: list[RemeasureJob]) -> list[RemeasureResult]:
    """Parse every job's transcript in one child interpreter; the child dies with this task."""
    if not sys.executable:
        raise RemeasureChildError("no interpreter to start the usage parse")
    package_root = str(Path(__file__).resolve().parents[2])
    request = json.dumps({"paths": [job.path for job in jobs]}).encode()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _CHILD_BOOT, package_root,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RemeasureChildError(f"usage parse could not start: {exc}") from exc
    try:
        async with asyncio.timeout(REMEASURE_CHILD_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate(request)
    except TimeoutError:
        raise RemeasureChildError(
            f"usage parse killed after {REMEASURE_CHILD_TIMEOUT_SECONDS:.0f}s ({len(jobs)} files)"
        ) from None
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.shield(process.wait())
    if process.returncode != 0:
        tail = (stderr or b"").decode(errors="replace")[-500:].strip()
        raise RemeasureChildError(f"usage parse exited {process.returncode}: {tail}")
    try:
        raw = json.loads(stdout)
        if not isinstance(raw, list) or len(raw) != len(jobs):
            raise ValueError(f"expected {len(jobs)} results")
        return [
            RemeasureResult(
                job=job,
                usage=item["usage"],
                signature=tuple(item["signature"]) if item["signature"] is not None else None,
                read_at=float(item["read_at"]),
                error=str(item.get("error") or ""),
                unmeasurable=item.get("unmeasurable"),
            )
            for job, item in zip(jobs, raw, strict=True)
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise RemeasureChildError(f"usage parse returned a malformed result: {exc}") from exc


def file_signature(path: str) -> tuple[int, int] | None:
    """(size, mtime_ns) of a file, None when it cannot be stat'ed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


async def land_results(
    repo: StorageRepository,
    results: list[RemeasureResult],
    ledger: RemeasureLedger,
) -> int:
    """Write each result that still describes the row's file; returns rows written."""
    written = 0
    for result in results:
        job = result.job
        if result.signature is None:
            continue  # changed while parsed, or gone: the next cycle looks again
        if result.error:
            logger.warning("Usage re-measure could not parse %s: %s", job.path, result.error)
            ledger.failed(job.agent_id, result.signature)
            continue
        if unbilled(result.usage):
            # No billed call in this content: never "measured 0". The row keeps no
            # tokens_measured_at; the verdict is written as SubagentStop writes it.
            # Checked here as well as in the child, whatever the parser returns.
            ledger.parsed_without_usage(job.agent_id, result.signature)
            if result.unmeasurable and file_signature(job.path) == result.signature:
                verdict = UnattributedReason(result.unmeasurable).value
                row = await repo.get_agent(job.agent_id)
                # Already recorded (by SubagentStop, or before a restart): no second write.
                if row is not None and row.usage_unmeasurable != verdict and await repo.update_remeasured_usage(
                    job.agent_id, measured_at=job.measured_at, values={"usage_unmeasurable": verdict},
                ):
                    written += 1
            continue
        try:
            row = await repo.get_agent(job.agent_id)
        except Exception:  # noqa: BLE001
            continue
        if row is None or file_signature(job.path) != result.signature:
            continue
        usage = result.usage
        shrunk = [layer for layer in _LAYERS
                  if getattr(row, layer) is not None and usage[layer] < getattr(row, layer)]
        if shrunk:
            # A transcript only grows, so a smaller total is unexpected (none of 941
            # rows re-measured on 09-30 shrank). Written anyway, as SubagentStop would.
            logger.warning(
                "Usage re-measure of agent %s is lower than stored for %s (%s -> %s), path %s",
                job.agent_id, ", ".join(shrunk),
                [getattr(row, layer) for layer in shrunk], [usage[layer] for layer in shrunk], job.path,
            )
        updates: dict[str, Any] = {layer: usage[layer] for layer in _LAYERS}
        updates["tokens_measured_at"] = from_timestamp(result.read_at)
        updates["tokens_source"] = TokenSource.TRANSCRIPT.value
        if usage.get("model"):
            updates["model"] = usage["model"]
        if row.transcript_path != job.path:
            # The stored path is empty or stale; the file was found by cc id.
            updates["transcript_path"] = job.path
        if row.usage_unmeasurable is not None:
            updates["usage_unmeasurable"] = None  # billed calls now: the verdict no longer holds
        if await repo.update_remeasured_usage(job.agent_id, measured_at=job.measured_at, values=updates):
            ledger.written(job.agent_id)
            written += 1
    return written
