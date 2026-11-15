"""When the backup runs, and what it records: hook events in, versions out.

Two triggers, no background process (the hook scripts are not changed; the API
already receives every tool call and session boundary):

* fast path: a ``PostToolUse`` of a file-writing tool whose ``file_path`` lies in an
  enrolled folder and is an asset queues that path. The request does a dictionary
  lookup and a string classification, nothing more; the work runs after it;
* comparison: ``SessionStart``, ``Stop``, ``SessionEnd``, ``PostCompact`` and
  ``PreCompact`` of a session in an enrolled folder compare the folder by size and
  mtime, catching what the fast path cannot see (shell commands, other programs,
  the editor). At most once per folder in ``RECONCILE_MIN_INTERVAL_S``, unless a
  shell command ran in that folder's sessions since the last one; an explicit
  ``run_now`` is never throttled.

Per folder one job runs at a time; submits while it runs coalesce into one more
round (``aiteam.single_flight``). The job itself is ``aiteam.assets.ingest``.

Nothing here touches an unenrolled folder, and with no folder enrolled a hook
event costs one emptiness check.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aiteam.assets import attribution
from aiteam.assets.blob_store import StoreMissing
from aiteam.assets.catalog import catalog, classify
from aiteam.assets.ingest import Ingestor, RunSummary
from aiteam.assets.machine import MachineKey, read_machine_key
from aiteam.assets.os_manifest import source_root
from aiteam.assets.walk import FolderMissing
from aiteam.ids import normalize_root_path
from aiteam.single_flight import KeyedSingleFlight
from aiteam.storage.assets import AssetRepository, Finding, StateRow
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AssetCategory,
    AssetFileState,
    AssetHost,
    AssetRoot,
    AssetRootStatus,
    AssetScanDisposition,
    AssetVersionSource,
)

logger = logging.getLogger(__name__)

RECONCILE_MIN_INTERVAL_S = 30.0
FAST_PATH_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash"})
RECONCILE_EVENTS = frozenset({"SessionStart", "Stop", "SessionEnd", "PostCompact"})
MAX_PENDING_PATHS = 256
JOB_KIND = "asset-backup"
_RECOVERY = {JOB_KIND: "rerun by the folder's next session boundary, or asset_backup_run"}
# History is never pruned (deleting it is not reversible); past these, the user is told.
STORAGE_WARN_BYTES = 2 * 1024**3
DISK_FREE_WARN_RATIO = 0.10
# Paths listed per folder and state in a status answer, and pending findings listed.
MAX_LISTED = 20
# File states that need the user's attention: content held back, too large, a link, unreadable.
ATTENTION_STATES = (AssetFileState.BLOCKED, AssetFileState.OVERSIZE, AssetFileState.SYMLINK,
                    AssetFileState.UNREADABLE)


async def project_roots(repo: StorageRepository) -> list[attribution.ProjectRoot]:
    """Every OS project with a root folder, for project-memory attribution."""
    return [attribution.ProjectRoot(p.id, p.root_path) for p in await repo.list_projects() if p.root_path]


class EnrollRefusedError(ValueError):
    """This machine or install cannot back a folder up; the message says why."""


@dataclass(frozen=True)
class Attention:
    """One thing of an enrolled folder the backup could not keep, or a folder it could not read.

    ``status`` is an ``AssetFileState`` value for a path, ``unreadable_dir`` for a folder
    that could not be listed, or ``folder_missing`` / ``store_missing`` for a stop.
    """

    root: AssetRoot
    rel_path: str
    status: str


FOLDER_MISSING = "folder_missing"
STORE_MISSING = "store_missing"
UNREADABLE_DIR = "unreadable_dir"


@dataclass
class _Folder:
    """An enrolled folder and what its next job must do."""

    root: AssetRoot
    pending: dict[str, str] = field(default_factory=dict)  # fast-path paths -> session id
    forced: set[tuple[AssetCategory, str]] = field(default_factory=set)
    full: AssetVersionSource | None = None
    full_session: str = ""
    dirty: bool = False
    last_full: float | None = None
    waiters: list[asyncio.Future[RunSummary]] = field(default_factory=list)
    # What the last job could not do: a stop (folder or store missing), folders it could not list.
    stopped: str = ""
    unreadable: list[str] = field(default_factory=list)


class AssetBackup:
    """The backup of this machine's enrolled config folders."""

    def __init__(self, *, repo: AssetRepository, projects: Callable[[], Awaitable[list[attribution.ProjectRoot]]],
                 data_dir: Path, node_id: str = "", source: Path | None = None,
                 machine: Callable[[], MachineKey] = read_machine_key) -> None:
        self.repo = repo
        self.data_dir = Path(data_dir)
        self.node_id = node_id
        self._source = source or source_root()
        self._read_machine = machine
        self.machine = MachineKey(refusal="未启动")
        self._folders: dict[str, _Folder] = {}
        self._by_path: dict[str, _Folder] = {}
        self._runner = KeyedSingleFlight(recovery=_RECOVERY, label="asset backup")
        self._ingestor = Ingestor(repo=repo, projects=projects, data_dir=self.data_dir, node_id=node_id,
                                  source=self._source)

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Read this machine's key (blocking: in a thread, once) and load its enrolled folders."""
        self.machine = await asyncio.to_thread(self._read_machine)
        if not self.machine.key:
            logger.info("Workflow backup disabled on this machine: %s", self.machine.refusal)
            return
        for root in await self.repo.list_roots():
            if root.machine_key == self.machine.key:
                self._remember(root)

    async def drain(self, timeout: float | None = None, *, leave_leftovers: bool = False) -> bool:
        return await self._runner.drain(timeout, leave_leftovers=leave_leftovers)

    def _remember(self, root: AssetRoot) -> None:
        old = self._folders.pop(root.id, None)
        if old is not None:
            self._by_path.pop(normalize_root_path(old.root.path), None)
        if root.status != AssetRootStatus.ENROLLED:
            self._ingestor.forget(root.id)
            return
        folder = old or _Folder(root)
        folder.root = root
        self._folders[root.id] = folder
        self._by_path[normalize_root_path(root.path)] = folder

    @property
    def enrolled(self) -> list[AssetRoot]:
        return [folder.root for folder in self._folders.values()]

    # -- hook events (request path: lookups only) ------------------------------

    def note_hook_event(self, payload: dict, folder: Path | None) -> None:
        """Queue whatever a hook event asks of an enrolled folder. Never reads a file."""
        if not self._folders or payload.get("harness") == "codex":
            return  # the Codex home has its own stage; Claude Code events only
        event = payload.get("hook_event_name")
        tool = payload.get("tool_name")
        session_id = str(payload.get("session_id") or "")
        if event == "PostToolUse" and tool in FAST_PATH_TOOLS:
            self._note_write(payload.get("tool_input"), session_id)
            return
        enrolled = self._folder(folder)
        if enrolled is None:
            return
        if event in ("PostToolUse", "PostToolUseFailure") and tool in SHELL_TOOLS:
            enrolled.dirty = True
        elif event in RECONCILE_EVENTS:
            self._request_full(enrolled, AssetVersionSource.RECONCILE, session_id)

    def note_compact(self, session_id: str, folder: Path | None) -> None:
        """PreCompact reaches the API as a checkpoint, not a hook event."""
        enrolled = self._folder(folder)
        if enrolled is not None:
            self._request_full(enrolled, AssetVersionSource.RECONCILE, session_id)

    def _folder(self, path: Path | str | None) -> _Folder | None:
        return self._by_path.get(normalize_root_path(str(path))) if path is not None and self._folders else None

    def _locate(self, path: str) -> tuple[_Folder, str] | None:
        """The enrolled folder ``path`` lies in, and the path relative to it (literal, like the keys)."""
        normalized = normalize_root_path(path)
        for root_path, folder in self._by_path.items():
            if normalized.startswith(root_path + "/"):
                return folder, normalized[len(root_path) + 1:]
        return None

    def _note_write(self, tool_input: object, session_id: str) -> None:
        if not isinstance(tool_input, dict):
            return
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str) or not os.path.isabs(path):
            return
        found = self._locate(path)
        if found is None:
            return
        folder, rel = found
        category = classify(folder.root.host, rel)
        if category is None or category not in folder.root.categories:
            return
        if rel not in folder.pending and len(folder.pending) >= MAX_PENDING_PATHS:
            return  # bounded: the next comparison picks it up
        folder.pending[rel] = session_id
        self._submit(folder)

    def _request_full(self, folder: _Folder, source: AssetVersionSource, session_id: str,
                      *, throttle: bool = True) -> None:
        now = time.monotonic()
        if throttle and not folder.dirty and folder.last_full is not None \
                and now - folder.last_full < RECONCILE_MIN_INTERVAL_S:
            return
        folder.last_full = now
        if folder.full is None or source == AssetVersionSource.MANUAL:
            folder.full, folder.full_session = source, session_id
        self._submit(folder)

    def _submit(self, folder: _Folder) -> None:
        rid = folder.root.id
        self._runner.submit((JOB_KIND, rid), lambda: self._job(rid))

    # -- explicit calls ----------------------------------------------------------

    async def enroll(self, *, host: AssetHost, path: str, profile: str, status: AssetRootStatus,
                     categories: list[AssetCategory] | None) -> AssetRoot:
        """Enroll or ignore a folder; an enrolled folder is compared right away.

        Raises ``EnrollRefusedError`` when this machine has no key or OS is not installed from source.
        """
        if not self.machine.key:
            raise EnrollRefusedError(self.machine.refusal)
        if not await asyncio.to_thread(_source_installed, self._source):
            raise EnrollRefusedError("工作流备份需要源码安装的 OS：OS 自装文件的清单取自源码树的 plugin/ 与 install.py")
        chosen = categories if categories is not None else catalog(host).default_categories
        root = await self.repo.save_root(machine_key=self.machine.key, host=host, path=normalize_root_path(path),
                                         profile=profile, status=status, categories=chosen)
        self._remember(root)
        if status == AssetRootStatus.ENROLLED:
            self._request_full(self._folders[root.id], AssetVersionSource.MANUAL, "", throttle=False)
        return root

    async def run_now(self, root_id: str, *, timeout: float) -> RunSummary | None:
        """Compare a folder now and wait up to ``timeout``; None when it is still running."""
        folder = self._folders.get(root_id)
        if folder is None:
            raise KeyError(root_id)
        waiter: asyncio.Future[RunSummary] = asyncio.get_running_loop().create_future()
        folder.waiters.append(waiter)
        self._request_full(folder, AssetVersionSource.MANUAL, "", throttle=False)
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout)
        except TimeoutError:
            return None

    async def resolve(self, finding_id: str, disposition: AssetScanDisposition) -> tuple[Finding, int] | None:
        """The user's answer to a scan hit; the path is read again at once. None: no such hit.

        Allowing binds to that content of that path (a later edit is scanned again);
        excluding binds to the path (it is never backed up again).
        """
        finding = await self.repo.get_finding(finding_id)
        if finding is None:
            return None
        changed = await self.repo.resolve_finding(finding, disposition)
        folder = self._folders.get(finding.root_id)
        if folder is not None:
            folder.forced.add((finding.category, finding.rel_path))
            self._request_full(folder, AssetVersionSource.MANUAL, "", throttle=False)
        return finding, changed

    async def known_profile(self, host: AssetHost, path: str) -> str:
        """The profile this machine's folder at ``path`` was enrolled into, or ""."""
        for root in await self.repo.list_roots(host=host):
            if root.path == normalize_root_path(path) and root.machine_key == self.machine.key:
                return root.profile_name
        return ""

    def folder_of(self, path: Path | None) -> AssetRoot | None:
        folder = self._folder(path)
        return folder.root if folder is not None else None

    # -- reading -----------------------------------------------------------------

    async def attention(self) -> list[Attention]:
        """What this machine's enrolled folders could not keep or read: stops first, then paths.

        Reads the cached comparison lists, not the table.
        """
        stops, items = [], []
        for folder in self._folders.values():
            if folder.stopped:
                stops.append(Attention(folder.root, "", folder.stopped))
            items += [Attention(folder.root, rel, UNREADABLE_DIR) for rel in folder.unreadable]
            state = await self._ingestor.state(folder.root.id)
            items += [Attention(folder.root, row.rel_path, str(row.status)) for row in state.values()
                      if row.status in ATTENTION_STATES]
        return stops + sorted(items, key=lambda item: item.rel_path)

    async def status(self, host: AssetHost | None = None) -> dict:
        """Every folder known here (this machine's and others'), with what is backed up and what is not."""
        roots = await self.repo.list_roots(host=host)
        ids = [root.id for root in roots]
        rows = await self.repo.state_rows(ids) if ids else []
        last = await self.repo.last_versions(ids) if ids else {}
        findings = await self.repo.findings(root_ids=ids, disposition=AssetScanDisposition.PENDING) if ids else []
        stored = await self.repo.stored_bytes()
        by_root: dict[str, list[StateRow]] = {}
        for rid, row, _ in rows:
            by_root.setdefault(rid, []).append(row)
        paths = {root.id: root.path for root in roots}
        return {
            "roots": [self._root_status(root, by_root.get(root.id, []), last.get(root.id)) for root in roots],
            "pending_findings": {
                "total": len(findings),
                "items": [{"finding_id": f.id, "root_path": paths.get(f.root_id, ""), "rel_path": f.rel_path,
                           "category": f.category, "rule": f.rule, "line": f.line}
                          for f in findings[:MAX_LISTED]],
            },
            "stored_bytes": stored,
            "warnings": await self._storage_warnings(stored),
            "machine_refusal": self.machine.refusal,
        }

    def _root_status(self, root: AssetRoot, rows: list[StateRow], last: datetime | None) -> dict:
        counts: dict[str, int] = {}
        problems: dict[str, list[dict]] = {}
        for row in rows:
            if row.status == AssetFileState.SYNCED:
                counts[row.category] = counts.get(row.category, 0) + 1
            elif row.status in (*ATTENTION_STATES, AssetFileState.EXCLUDED):
                problems.setdefault(row.status, []).append({"rel_path": row.rel_path, "category": row.category})
        folder = self._folders.get(root.id)
        if folder is not None and folder.unreadable:
            problems[UNREADABLE_DIR] = [{"rel_path": rel, "category": ""} for rel in folder.unreadable]
        return {
            "id": root.id, "host": root.host, "path": root.path, "profile": root.profile_name,
            "status": root.status, "this_machine": root.machine_key == self.machine.key,
            "stopped": folder.stopped if folder is not None else "",
            "categories": root.categories, "enrolled_at": root.enrolled_at.isoformat(),
            "last_backup_at": last.isoformat() if last is not None else None,
            "backed_up": counts,
            "not_backed_up": {state: {"total": len(items), "items": items[:MAX_LISTED]}
                              for state, items in problems.items()},
        }

    async def _storage_warnings(self, stored: int) -> list[dict]:
        """``storage_over_limit`` (history is never pruned) and ``disk_low``, with their numbers."""
        warnings: list[dict] = []
        if stored > STORAGE_WARN_BYTES:
            warnings.append({"code": "storage_over_limit", "stored_bytes": stored, "limit_bytes": STORAGE_WARN_BYTES})
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, self.data_dir)
        except OSError:
            return warnings
        if usage.total and usage.free / usage.total < DISK_FREE_WARN_RATIO:
            warnings.append({"code": "disk_low", "free_ratio": round(usage.free / usage.total, 3)})
        return warnings

    # -- the job -----------------------------------------------------------------

    async def _job(self, rid: str) -> None:
        folder = self._folders.get(rid)
        if folder is None:
            return
        waiters, folder.waiters = folder.waiters, []
        full, session = folder.full, folder.full_session
        pending, forced = folder.pending, folder.forced
        folder.full, folder.full_session, folder.pending, folder.forced = None, "", {}, set()
        if full is not None:
            folder.dirty = False
        summary = RunSummary(rid)
        try:
            if full is not None:
                await self._ingestor.compare(folder.root, full, session, forced, summary)
                folder.unreadable = summary.unreadable
            elif pending and await self._ingestor.ingest_paths(folder.root, pending, summary):
                # A written path is gone or unreadable: only a comparison can say which.
                self._request_full(folder, AssetVersionSource.RECONCILE, next(iter(pending.values())),
                                   throttle=False)
            folder.stopped = ""
        except (FolderMissing, StoreMissing) as exc:
            folder.stopped = STORE_MISSING if isinstance(exc, StoreMissing) else FOLDER_MISSING
            summary.error = str(exc)
            logger.info("asset backup of %s stopped: %s", folder.root.path, exc)
        except Exception as exc:  # noqa: BLE001 - logged; the next trigger retries
            summary.error = f"{type(exc).__name__}: {exc}"
            logger.warning("asset backup of %s failed", folder.root.path, exc_info=True)
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(summary)


def _source_installed(source: Path) -> bool:
    return (source / "install.py").is_file() and (source / "plugin").is_dir()
