"""Comparing an enrolled folder and recording what changed: walk, batch, observe, record.

``aiteam.assets.service`` decides when; this module does the work of one job. A
job walks the folder (or looks at the paths written), keeps what changed by size and
mtime, processes it in batches (``aiteam.assets.process``: in a worker thread for
small batches, in a child process above ``CHILD_MIN_FILES`` or ``CHILD_MIN_BYTES``,
extraction sources counted, because hashing, parsing and the secret scan are pure
Python) and records the results a few at a time (``RECORD_CHUNK`` observations per
transaction: hook requests wait on the same write lock).

What a job cannot see is never recorded as a deletion: a folder it cannot list, an
enrolled folder that is gone, a source that does not parse. The fast path records
files it can see; a written path that is no longer there is left to a comparison,
which can confirm it by listing the folder.

Each folder's comparison list is cached here once read (one job per folder at a
time, so the cache follows the database): the fast path and the notice check read
it, not the table.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from aiteam.assets import attribution, walk
from aiteam.assets.blob_store import StoreMissing
from aiteam.assets.catalog import catalog, classify
from aiteam.assets.os_manifest import OsManifest, build_claude_manifest
from aiteam.assets.process import MISSING, SYNCED, BatchResult, ExtractJob, FileJob, Job, run
from aiteam.storage.assets import AssetRepository, Blob, ItemKey, Observation, StateRow
from aiteam.sync import hlc
from aiteam.types import AssetCategory, AssetFileState, AssetOwnerKind, AssetRoot, AssetVersionSource

# Above either, a batch goes to a child process.
CHILD_MIN_FILES = 20
CHILD_MIN_BYTES = 256 * 1024
# Files processed per batch (and per child process), observations per transaction.
BATCH_FILES = 200
RECORD_CHUNK = 25
CHILD_TIMEOUT_S = 300.0
_PROJECT_MEMORY_DEPTH = 3  # projects/<slug>/memory/

StateKey = tuple[AssetCategory, str]


class ChildError(RuntimeError):
    """The backup child process failed; the batch is retried by the next comparison."""


@dataclass
class RunSummary:
    """What one job did, by outcome, and what it could not look at."""

    root_id: str
    files: int = 0
    versions: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)
    error: str = ""

    def count(self, status: str) -> None:
        self.by_status[status] = self.by_status.get(status, 0) + 1


class _Clock:
    """HLC values for versions, never going back, seeded from the newest stored one."""

    def __init__(self, node: str) -> None:
        self.node = node
        self.last: hlc.Hlc | None = None
        self.seeded = False

    def seed(self, stored: str | None) -> None:
        if not self.seeded:
            self.last = hlc.parse(stored) if stored else None
            self.seeded = True

    def next(self) -> str:
        self.last = hlc.tick(self.last, self.node, hlc.now_ms())
        return hlc.encode(self.last)


class Ingestor:
    """Runs comparisons for one node: its content store, its clock, OS's release manifest."""

    def __init__(self, *, repo: AssetRepository, projects: Callable[[], Awaitable[list[attribution.ProjectRoot]]],
                 data_dir: Path, node_id: str, source: Path) -> None:
        self.repo = repo
        self._projects = projects
        self.data_dir = data_dir
        self.node_id = node_id
        self._source = source
        self._clock = _Clock(node_id)
        self._manifest: OsManifest | None = None
        self._states: dict[str, dict[StateKey, StateRow]] = {}

    async def state(self, root_id: str) -> dict[StateKey, StateRow]:
        """The folder's comparison list (read once, then kept in step with what is recorded)."""
        if root_id not in self._states:
            self._states[root_id] = await self.repo.root_state(root_id)
        return self._states[root_id]

    def forget(self, root_id: str) -> None:
        self._states.pop(root_id, None)

    async def compare(self, root: AssetRoot, source: AssetVersionSource, session_id: str,
                      forced: set[StateKey], summary: RunSummary) -> None:
        """Walk the whole folder. Raises ``walk.FolderMissing`` or ``StoreMissing``: nothing recorded."""
        await self._check_store()
        seen = await asyncio.to_thread(walk.walk, root.host, Path(root.path), root.categories)
        summary.unreadable = seen.unreadable
        state = await self.state(root.id)
        present = {(s.category, s.rel) for s in seen.files} | set(seen.links)
        gone = [key for key, row in state.items()
                if key[0] in root.categories and row.status != AssetFileState.SOURCE and key not in present
                and not (_extracted(root, key[0]) and row.status != AssetFileState.SYMLINK)
                and seen.confirmed_absent(key[1])]
        changed = [s for s in seen.files
                   if (s.category, s.rel) in forced or _differs(state.get((s.category, s.rel)), s)]
        extractions = _changed_sources(root, seen, state, forced)
        work = _Work(changed, gone, extractions, _new_links(seen.links, state), _bytes(seen, extractions))
        await self._ingest(root, work, source, session_id, summary)

    async def ingest_paths(self, root: AssetRoot, pending: dict[str, str], summary: RunSummary) -> bool:
        """The fast path: the paths written. Returns True when some path needs a comparison
        (it is not there any more, or could not be looked at)."""
        await self._check_store()
        state = await self.state(root.id)
        files, links, unsure = [], [], False
        extracted: set[AssetCategory] = set()
        for rel in pending:  # classified and checked against the folder's categories when queued
            category = classify(root.host, rel)
            if category is None:
                continue
            if _extracted(root, category):
                extracted.add(category)
                continue
            found = await asyncio.to_thread(walk.inspect, root.host, Path(root.path), rel, category, root.categories)
            if found.link is not None:
                links.append(found.link)
            elif found.seen is None:
                unsure = True
            elif _differs(state.get((category, rel)), found.seen):
                files.append(found.seen)
        extractions, source_bytes = [], 0
        if extracted:
            sources = await asyncio.to_thread(walk.sources, root.host, Path(root.path), extracted)
            extractions = _changed_sources(root, sources, state, {(c, "") for c in extracted}, only=extracted)
            links += sources.links
            source_bytes = _bytes(sources, extractions)
        work = _Work(files, [], extractions, _new_links(links, state), source_bytes)
        await self._ingest(root, work, AssetVersionSource.EVENT, next(iter(pending.values()), ""), summary)
        return unsure

    async def _check_store(self) -> None:
        if not await asyncio.to_thread(self.data_dir.is_dir):
            raise StoreMissing(f"数据目录不在：{self.data_dir}")

    async def _ingest(self, root: AssetRoot, work: _Work, source: AssetVersionSource, session_id: str,
                      summary: RunSummary) -> None:
        state = await self.state(root.id)
        excluded = await self.repo.excluded_paths(root.id)
        upfront = [_gone(key, state.get(key)) for key in work.gone] + work.links
        upfront += [Observation(s.category, s.rel, _kept(state.get((s.category, s.rel)), s.category, s.rel,
                                                         AssetFileState.EXCLUDED, "", s.size, s.mtime_ns, False))
                    for s in work.changed if (s.category, s.rel) in excluded]
        changed = [s for s in work.changed if (s.category, s.rel) not in excluded]
        if not changed and not work.extractions and not upfront:
            return
        context = _Context(
            root=root, state=state, manifest=await self._os_manifest(), excluded=excluded,
            allowed=await self.repo.allowed_hashes(root.id),
            projects=await self._projects() if _needs_projects(changed, work.extractions) else [],
        )
        context.owners = await asyncio.to_thread(context.memory_owners, changed)
        self._clock.seed(await self.repo.max_hlc())
        await self._record(root, upfront, source, session_id, summary)
        batches = [changed[start:start + BATCH_FILES] for start in range(0, len(changed), BATCH_FILES)] or [[]]
        for index, batch in enumerate(batches):
            extractions = work.extractions if index == 0 else []
            if not batch and not extractions:
                continue
            job = context.job(self.data_dir, self._source, batch, extractions)
            result = await self._process(job, sum(s.size for s in batch) + (work.source_bytes if index == 0 else 0))
            summary.files += len(batch)
            await self._record(root, context.observe(result, extractions, summary), source, session_id, summary)

    async def _record(self, root: AssetRoot, observations: list[Observation], source: AssetVersionSource,
                      session_id: str, summary: RunSummary) -> None:
        state = await self.state(root.id)
        for start in range(0, len(observations), RECORD_CHUNK):
            chunk = observations[start:start + RECORD_CHUNK]
            summary.versions += await self.repo.record(
                root=root, observations=chunk, source=source, session_id=session_id, source_node=self.node_id,
                next_hlc=self._clock.next)
            for observation in chunk:  # only once the transaction committed
                key = (observation.category, observation.rel_path)
                if observation.state is None:
                    state.pop(key, None)
                else:
                    state[key] = observation.state

    async def _os_manifest(self) -> OsManifest:
        if self._manifest is None:
            self._manifest = await asyncio.to_thread(build_claude_manifest, self._source)
        return self._manifest

    async def _process(self, job: Job, size: int) -> BatchResult:
        if len(job.files) < CHILD_MIN_FILES and size < CHILD_MIN_BYTES:
            return await asyncio.to_thread(run, job)
        return await run_child(job)


@dataclass
class _Work:
    """What one job processes: changed files, paths confirmed gone, categories to re-extract, links found."""

    changed: list[walk.Seen]
    gone: list[StateKey]
    extractions: list[AssetCategory]
    links: list[Observation]
    source_bytes: int = 0  # what the extractions will parse


def _extracted(root: AssetRoot, category: AssetCategory) -> bool:
    rule = catalog(root.host).rule(category)
    return rule is not None and bool(rule.extractor)


def _differs(row: StateRow | None, seen: walk.Seen) -> bool:
    return row is None or row.status == AssetFileState.SYMLINK or (row.size, row.mtime_ns) != (seen.size, seen.mtime_ns)


def _changed_sources(root: AssetRoot, seen: walk.Walk, state: dict[StateKey, StateRow], forced: set[StateKey],
                     only: Iterable[AssetCategory] | None = None) -> list[AssetCategory]:
    """Extraction categories whose sources appeared, vanished or changed size or mtime.

    A category whose sources could not be looked at (unreadable, a link) is left alone.
    """
    chosen = set(only) if only is not None else set(root.categories)
    changed = []
    for category in root.categories:
        if category not in chosen or not _extracted(root, category) or category in seen.source_errors:
            continue
        now = {(s.rel, s.size, s.mtime_ns) for s in seen.sources if s.category == category}
        before = {(row.rel_path, row.size, row.mtime_ns) for key, row in state.items()
                  if key[0] == category and row.status == AssetFileState.SOURCE}
        if now != before or any(key[0] == category for key in forced):
            changed.append(category)
    return changed


def _bytes(seen: walk.Walk, extractions: list[AssetCategory]) -> int:
    return sum(s.size for s in seen.sources if s.category in extractions)


def _new_links(links: list[StateKey], state: dict[StateKey, StateRow]) -> list[Observation]:
    """Links not on the comparison list as links yet: listed, never followed."""
    return [Observation(category, rel, _kept(state.get((category, rel)), category, rel, AssetFileState.SYMLINK,
                                             "", 0, 0, False))
            for category, rel in links
            if (state.get((category, rel)) or StateRow(category, rel, AssetFileState.SYNCED)).status
            != AssetFileState.SYMLINK]


def _needs_projects(files: list[walk.Seen], extractions: list[AssetCategory]) -> bool:
    return AssetCategory.MCP_SERVERS in extractions or any(
        s.category == AssetCategory.PROJECT_MEMORY for s in files)


def _gone(key: StateKey, row: StateRow | None) -> Observation:
    """A path confirmed gone: a tombstone for its item, and off the comparison list."""
    return Observation(key[0], key[1], None, item_id=row.item_id if row is not None else "")


def _kept(previous: StateRow | None, category: AssetCategory, rel: str, status: AssetFileState, content_hash: str,
          size: int, mtime_ns: int, executable: bool) -> StateRow:
    """A comparison row for content that was not stored: the path keeps its item (and its last version)."""
    return StateRow(category, rel, status, previous.item_id if previous is not None else "", content_hash, size,
                    mtime_ns, executable)


@dataclass
class _Context:
    """What turning one job's results into observations needs."""

    root: AssetRoot
    state: dict[StateKey, StateRow]
    manifest: OsManifest
    excluded: set[StateKey]
    allowed: set[str]
    projects: list[attribution.ProjectRoot]
    owners: dict[str, attribution.Owner] = field(default_factory=dict)

    def memory_owners(self, files: list[walk.Seen]) -> dict[str, attribution.Owner]:
        """Owners of the project-memory folders among ``files``, by slug (blocking: may read session files)."""
        base = Path(self.root.path) / "projects"
        slugs = {s.rel.split("/")[1] for s in files if s.category == AssetCategory.PROJECT_MEMORY}
        return {name: attribution.attribute(name, self.projects, base / name) for name in slugs}

    def _sources(self, category: AssetCategory) -> list[tuple[str, str]]:
        root = Path(self.root.path)
        return [(rel, str(walk.source_path(self.root.host, root, rel)))
                for rel in catalog(self.root.host).rule(category).patterns]

    def job(self, data_dir: Path, source: Path, files: list[walk.Seen], extractions: Iterable[AssetCategory]) -> Job:
        extract_jobs = [ExtractJob(str(category), catalog(self.root.host).rule(category).extractor,
                                   self._sources(category)) for category in extractions]
        return Job(data_dir=str(data_dir), files=[FileJob(str(s.category), s.rel, s.path) for s in files],
                   extractions=extract_jobs, manifest=self.manifest.to_json(), source_root=str(source),
                   allowed_hashes=sorted(self.allowed))

    def _item(self, category: AssetCategory, rel: str, owner_path: str) -> ItemKey:
        """The item a stored content belongs to: owner, and the path inside the folder or the memory folder."""
        if category == AssetCategory.PROJECT_MEMORY:
            parts = rel.split("/")
            owner, rel = self.owners[parts[1]], "/".join(parts[_PROJECT_MEMORY_DEPTH:])
        elif owner_path:
            owner = attribution.attribute_path(owner_path, self.projects)
        else:
            owner = attribution.Owner(AssetOwnerKind.CONFIG)
        return ItemKey(self.root.profile_id, category, owner.kind, owner.ref, owner.subpath, rel)

    def observe(self, result: BatchResult, extractions: list[AssetCategory], summary: RunSummary) -> list[Observation]:
        observations = []
        produced: set[StateKey] = set()
        for asset in result.assets:
            category = AssetCategory(asset.category)
            key = (category, asset.rel)
            produced.add(key)
            summary.count(asset.status)
            previous = self.state.get(key)
            if asset.status == MISSING:
                continue  # gone since the walk: the next comparison confirms it
            if key in self.excluded:  # an extracted asset the user excluded
                observations.append(Observation(category, asset.rel, _kept(
                    previous, category, asset.rel, AssetFileState.EXCLUDED, asset.hash, asset.size, 0, False)))
                continue
            if asset.status != SYNCED:
                row = _kept(previous, category, asset.rel, AssetFileState(asset.status), asset.hash, asset.size,
                            asset.mtime_ns, asset.executable)
                observations.append(Observation(category, asset.rel, row, findings=list(asset.hits)))
                continue
            item = self._item(category, asset.rel, asset.owner_path)
            row = StateRow(category, asset.rel, AssetFileState.SYNCED, item.id, asset.hash, asset.size,
                           asset.mtime_ns, asset.executable)
            observations.append(Observation(
                category, asset.rel, row, item_id=item.id, new_item=item,
                os_template=self.manifest.os_name(asset.rel), content_hash=asset.hash, size=asset.size,
                executable=asset.executable, findings=list(asset.hits), blob=Blob(**asset.blob),
                rebound_from=previous.item_id if previous is not None and previous.item_id not in ("", item.id)
                else ""))
        for source in result.sources:
            category = AssetCategory(source.category)
            produced.add((category, source.rel))
            observations.append(Observation(category, source.rel, StateRow(
                category, source.rel, AssetFileState.SOURCE, size=source.size, mtime_ns=source.mtime_ns)))
        # An extraction that ran saw everything of its category: what it did not produce is gone.
        succeeded = {category for category in extractions if str(category) not in result.failed}
        observations += [_gone(key, row) for key, row in self.state.items()
                         if key[0] in succeeded and key not in produced]
        return observations


# The child imports the batch module and the standard library only, from this
# process's own source tree (argv[1]), so both sides run the same code.
_CHILD_BOOT = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from aiteam.assets.process import main; main()"
)


async def run_child(job: Job) -> BatchResult:
    """Process a batch in a child interpreter; the child dies with this task."""
    if not sys.executable:
        raise ChildError("no interpreter to start the backup child")
    package_root = str(Path(__file__).resolve().parents[2])
    request = json.dumps(job.to_json()).encode()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _CHILD_BOOT, package_root,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(CHILD_TIMEOUT_S):
            stdout, stderr = await process.communicate(request)
    except TimeoutError:
        raise ChildError(f"backup child killed after {CHILD_TIMEOUT_S:.0f}s ({len(job.files)} files)") from None
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.shield(process.wait())
    if process.returncode != 0:
        tail = (stderr or b"").decode(errors="replace")[-500:].strip()
        raise ChildError(f"backup child exited {process.returncode}: {tail}")
    try:
        return BatchResult.from_json(json.loads(stdout))
    except (ValueError, TypeError, KeyError) as exc:
        raise ChildError(f"backup child returned a malformed result: {exc}") from exc
