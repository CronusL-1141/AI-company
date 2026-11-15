"""Rows of the workflow asset backup (aiteam.assets): profiles, roots, items, versions.

Every batch of observations is recorded in one transaction: blob rows, items,
versions, findings and this machine's comparison list together. A version is
appended only when the folder's latest version of that item says otherwise, read
inside the same transaction, so a batch replayed after a crash adds nothing twice.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert

from aiteam.clock import utc_now
from aiteam.ids import content_id, new_id
from aiteam.storage.connection import get_session
from aiteam.storage.models import (
    AssetBlobModel,
    AssetItemModel,
    AssetProfileModel,
    AssetRootModel,
    AssetRootStateModel,
    AssetScanFindingModel,
    AssetVersionModel,
)
from aiteam.types import (
    AssetCategory,
    AssetFileState,
    AssetHost,
    AssetOwnerKind,
    AssetRoot,
    AssetRootStatus,
    AssetScanDisposition,
    AssetVersionSource,
)

# SQLite's limit on bound parameters is far above this; chunks keep statements small.
_CHUNK = 400


def profile_id(host: AssetHost, name: str) -> str:
    return content_id("asset_profiles", str(host), name)


def root_id(machine_key: str, host: AssetHost, path: str) -> str:
    return content_id("asset_roots", machine_key, str(host), path)


@dataclass(frozen=True)
class ItemKey:
    """An asset's identity: profile, category, owner, path (inside the folder or the memory folder)."""

    profile_id: str
    category: AssetCategory
    owner_kind: AssetOwnerKind
    owner_ref: str
    owner_subpath: str
    rel_path: str

    @property
    def id(self) -> str:
        return content_id("asset_items", self.profile_id, str(self.category), str(self.owner_kind),
                          self.owner_ref, self.owner_subpath, self.rel_path)


@dataclass(frozen=True)
class Blob:
    hash: str
    raw_size: int
    stored_size: int
    encoding: str


@dataclass(frozen=True)
class StateRow:
    """One row of this machine's comparison list."""

    category: AssetCategory
    rel_path: str
    status: AssetFileState
    item_id: str = ""
    content_hash: str = ""
    size: int = 0
    mtime_ns: int = 0
    executable: bool = False


@dataclass
class Observation:
    """What a batch saw at one path, and what to record for it.

    ``item_id`` with ``content_hash`` set: a content (appended if it differs from the
    folder's latest version of the item); ``new_item`` is created first when absent.
    ``item_id`` with ``content_hash`` empty: the path left the folder (a tombstone,
    unless the latest version is one already). ``rebound_from``: the path's previous
    item, which gets a ``rebound`` marker. ``state`` None takes the path off the
    comparison list.
    """

    category: AssetCategory
    rel_path: str
    state: StateRow | None
    item_id: str = ""
    new_item: ItemKey | None = None
    os_template: bool = False
    content_hash: str = ""
    size: int = 0
    executable: bool = False
    rebound_from: str = ""
    findings: list[tuple[str, int]] = field(default_factory=list)
    # The stored content (already in the content store) the version names.
    blob: Blob | None = None


@dataclass(frozen=True)
class Finding:
    id: str
    root_id: str
    category: AssetCategory
    rel_path: str
    content_hash: str
    rule: str
    line: int
    disposition: AssetScanDisposition
    created_at: datetime


def _chunks(values: list, size: int = _CHUNK) -> Iterable[list]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _new_versions(observations: list[Observation], latest: dict[str, tuple[str | None, str]],
                  source: AssetVersionSource) -> list[dict]:
    """The versions a batch adds, in order: only where the folder's latest version says otherwise.

    ``latest`` (item -> (content hash, source)) is read in the recording transaction
    and advanced here, so a batch replayed after a crash, or a file touched without a
    change, adds nothing.
    """
    added: list[dict] = []

    def add(item_id: str, content_hash: str | None, size: int, executable: bool, kind: AssetVersionSource) -> None:
        previous = latest.get(item_id)
        added.append({"item_id": item_id, "content_hash": content_hash, "size": size, "executable": executable,
                      "parent_hash": previous[0] if previous else None, "source": str(kind)})
        latest[item_id] = (content_hash, str(kind))

    for o in observations:
        if o.rebound_from:
            previous = latest.get(o.rebound_from)
            if previous is not None and previous[1] != AssetVersionSource.REBOUND:
                add(o.rebound_from, None, 0, False, AssetVersionSource.REBOUND)
        if not o.item_id:
            continue
        previous, current = latest.get(o.item_id), o.content_hash or None
        if current is None and (previous is None or previous[0] is None):
            continue  # nothing recorded here, or already a tombstone or a rebound marker
        if previous is None or previous[0] != current or previous[1] == AssetVersionSource.REBOUND:
            add(o.item_id, current, o.size, o.executable, source)
    return added


def _root(row: AssetRootModel, profile_name: str = "") -> AssetRoot:
    return AssetRoot(
        id=row.id, machine_key=row.machine_key, host=AssetHost(row.host), path=row.path, profile_id=row.profile_id,
        profile_name=profile_name, status=AssetRootStatus(row.status),
        categories=[AssetCategory(c) for c in row.categories or []], include_sessions=row.include_sessions,
        enrolled_at=row.enrolled_at, updated_at=row.updated_at,
    )


def _finding(row: AssetScanFindingModel) -> Finding:
    return Finding(row.id, row.root_id, AssetCategory(row.category), row.rel_path, row.content_hash, row.rule,
                   row.line, AssetScanDisposition(row.disposition), row.created_at)


class AssetRepository:
    """The asset tables of one database."""

    def __init__(self, db_url: str | None) -> None:
        self._db_url = db_url

    # -- profiles and roots ---------------------------------------------------

    async def save_root(self, *, machine_key: str, host: AssetHost, path: str, profile: str,
                        status: AssetRootStatus, categories: list[AssetCategory]) -> AssetRoot:
        """Enroll or ignore a folder (creating its profile); returns the root as stored."""
        now = utc_now()
        rid, pid = root_id(machine_key, host, path), profile_id(host, profile)
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            await session.execute(insert(AssetProfileModel).values(
                id=pid, host=str(host), name=profile, created_at=now).on_conflict_do_nothing())
            values = {"profile_id": pid, "status": str(status), "categories": [str(c) for c in categories],
                      "updated_at": now}
            await session.execute(insert(AssetRootModel).values(
                id=rid, machine_key=machine_key, host=str(host), path=path, include_sessions=False, enrolled_at=now,
                **values).on_conflict_do_update(index_elements=["id"], set_=values))
        root = await self.get_root(rid)
        assert root is not None
        return root

    async def get_root(self, rid: str) -> AssetRoot | None:
        roots = await self.list_roots(ids=[rid])
        return roots[0] if roots else None

    async def list_roots(self, *, host: AssetHost | None = None, ids: list[str] | None = None) -> list[AssetRoot]:
        query = select(AssetRootModel, AssetProfileModel.name).join(
            AssetProfileModel, AssetProfileModel.id == AssetRootModel.profile_id, isouter=True)
        if host is not None:
            query = query.where(AssetRootModel.host == str(host))
        if ids is not None:
            query = query.where(AssetRootModel.id.in_(ids))
        async with get_session(self._db_url) as session:
            rows = (await session.execute(query.order_by(AssetRootModel.enrolled_at))).all()
        return [_root(row, name or "") for row, name in rows]

    # -- what a batch needs to know --------------------------------------------

    async def root_state(self, rid: str) -> dict[tuple[AssetCategory, str], StateRow]:
        async with get_session(self._db_url) as session:
            rows = (await session.execute(
                select(AssetRootStateModel).where(AssetRootStateModel.root_id == rid))).scalars().all()
        return {
            (AssetCategory(r.category), r.rel_path): StateRow(
                AssetCategory(r.category), r.rel_path, AssetFileState(r.status), r.item_id, r.content_hash,
                r.size, r.mtime_ns, r.executable)
            for r in rows
        }

    async def allowed_hashes(self, rid: str) -> set[str]:
        async with get_session(self._db_url) as session:
            rows = await session.execute(select(AssetScanFindingModel.content_hash).where(
                AssetScanFindingModel.root_id == rid,
                AssetScanFindingModel.disposition == str(AssetScanDisposition.ALLOWED)))
            return {value for (value,) in rows}

    async def excluded_paths(self, rid: str) -> set[tuple[AssetCategory, str]]:
        async with get_session(self._db_url) as session:
            rows = await session.execute(select(AssetScanFindingModel.category, AssetScanFindingModel.rel_path)
                                         .where(AssetScanFindingModel.root_id == rid,
                                                AssetScanFindingModel.disposition ==
                                                str(AssetScanDisposition.EXCLUDED)).distinct())
            return {(AssetCategory(category), path) for category, path in rows}

    async def max_hlc(self) -> str | None:
        async with get_session(self._db_url) as session:
            return (await session.execute(select(func.max(AssetVersionModel.hlc)))).scalar()

    # -- recording -------------------------------------------------------------

    async def record(self, *, root: AssetRoot, observations: list[Observation], source: AssetVersionSource,
                     session_id: str, source_node: str, next_hlc: Callable[[], str]) -> int:
        """Record observations in one transaction; returns how many versions were appended.

        Callers keep transactions short (``aiteam.assets.ingest`` sends a few dozen
        observations at a time): hook requests wait for the same write lock.
        """
        now = utc_now()
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            await self._insert(session, AssetBlobModel, [{**o.blob.__dict__, "created_at": now}
                                                         for o in observations if o.blob is not None])
            items = {o.new_item.id: (o.new_item, o.os_template) for o in observations if o.new_item is not None}
            await self._insert(session, AssetItemModel, [
                {"id": iid, "profile_id": key.profile_id, "category": str(key.category),
                 "owner_kind": str(key.owner_kind), "owner_ref": key.owner_ref, "owner_subpath": key.owner_subpath,
                 "rel_path": key.rel_path, "os_template": os_template, "created_at": now}
                for iid, (key, os_template) in items.items()])
            latest = await self._latest(session, root.id, [
                *(o.item_id for o in observations if o.item_id),
                *(o.rebound_from for o in observations if o.rebound_from)])
            stamp = {"root_id": root.id, "source_node": source_node, "observed_at": now, "session_id": session_id,
                     "snapshot_group": None}
            versions = [{**version, **stamp, "id": new_id(), "hlc": next_hlc()}
                        for version in _new_versions(observations, latest, source)]
            for chunk in _chunks(versions):
                await session.execute(insert(AssetVersionModel).values(chunk))
            await self._write_state(session, root.id, observations, now)
            await self._insert(session, AssetScanFindingModel, [
                {"id": content_id("asset_scan_findings", root.id, str(o.category), o.rel_path, o.state.content_hash,
                                  rule, str(line)),
                 "root_id": root.id, "category": str(o.category), "rel_path": o.rel_path,
                 "content_hash": o.state.content_hash, "rule": rule, "line": line,
                 "disposition": str(AssetScanDisposition.PENDING), "created_at": now, "updated_at": now}
                for o in observations if o.state is not None for rule, line in o.findings])
        return len(versions)

    @staticmethod
    async def _insert(session, model, rows: list[dict]) -> None:
        """Insert rows keyed by content (blobs, items, findings); a row already there stays as it is."""
        for chunk in _chunks(rows):
            await session.execute(insert(model).values(chunk).on_conflict_do_nothing())

    @staticmethod
    async def _latest(session, rid: str, item_ids: list[str]) -> dict[str, tuple[str | None, str]]:
        """The folder's latest (content hash, source) per item."""
        latest: dict[str, tuple[str | None, str, str]] = {}
        for chunk in _chunks(sorted(set(item_ids))):
            rows = await session.execute(select(
                AssetVersionModel.item_id, AssetVersionModel.content_hash, AssetVersionModel.source,
                AssetVersionModel.hlc).where(AssetVersionModel.root_id == rid, AssetVersionModel.item_id.in_(chunk)))
            for item_id, content_hash, kind, hlc in rows:
                if item_id not in latest or hlc > latest[item_id][2]:
                    latest[item_id] = (content_hash, kind, hlc)
        return {item_id: (value[0], value[1]) for item_id, value in latest.items()}

    @staticmethod
    async def _write_state(session, rid: str, observations: list[Observation], now: datetime) -> None:
        gone = [(str(o.category), o.rel_path) for o in observations if o.state is None]
        for category, rel_path in gone:
            await session.execute(delete(AssetRootStateModel).where(
                AssetRootStateModel.root_id == rid, AssetRootStateModel.category == category,
                AssetRootStateModel.rel_path == rel_path))
        rows = [
            {"root_id": rid, "category": str(s.category), "rel_path": s.rel_path, "item_id": s.item_id,
             "content_hash": s.content_hash, "size": s.size, "mtime_ns": s.mtime_ns, "executable": s.executable,
             "status": str(s.status), "updated_at": now}
            for s in (o.state for o in observations) if s is not None
        ]
        for chunk in _chunks(rows, 100):
            statement = insert(AssetRootStateModel).values(chunk)
            await session.execute(statement.on_conflict_do_update(
                index_elements=["root_id", "category", "rel_path"],
                set_={name: statement.excluded[name] for name in
                      ("item_id", "content_hash", "size", "mtime_ns", "executable", "status", "updated_at")}))

    # -- findings --------------------------------------------------------------

    async def findings(self, *, root_ids: list[str] | None = None,
                       disposition: AssetScanDisposition | None = None) -> list[Finding]:
        query = select(AssetScanFindingModel)
        if root_ids is not None:
            query = query.where(AssetScanFindingModel.root_id.in_(root_ids))
        if disposition is not None:
            query = query.where(AssetScanFindingModel.disposition == str(disposition))
        async with get_session(self._db_url) as session:
            rows = (await session.execute(query.order_by(AssetScanFindingModel.created_at))).scalars().all()
        return [_finding(row) for row in rows]

    async def get_finding(self, finding_id: str) -> Finding | None:
        async with get_session(self._db_url) as session:
            row = await session.get(AssetScanFindingModel, finding_id)
            return _finding(row) if row is not None else None

    async def resolve_finding(self, finding: Finding, disposition: AssetScanDisposition) -> int:
        """Allow every hit on that content of that path, or exclude the path; returns rows changed."""
        query = update(AssetScanFindingModel).where(
            AssetScanFindingModel.root_id == finding.root_id, AssetScanFindingModel.category == str(finding.category),
            AssetScanFindingModel.rel_path == finding.rel_path)
        if disposition == AssetScanDisposition.ALLOWED:
            query = query.where(AssetScanFindingModel.content_hash == finding.content_hash)
        async with get_session(self._db_url) as session:
            result = await session.execute(query.values(disposition=str(disposition), updated_at=utc_now()))
            return result.rowcount or 0

    # -- reading ---------------------------------------------------------------

    async def state_rows(self, root_ids: list[str],
                         statuses: Iterable[AssetFileState] | None = None) -> list[tuple[str, StateRow, datetime]]:
        """The comparison list of these folders, or its rows in ``statuses``: (root id, row, when it changed)."""
        query = select(AssetRootStateModel).where(AssetRootStateModel.root_id.in_(root_ids))
        if statuses is not None:
            query = query.where(AssetRootStateModel.status.in_([str(status) for status in statuses]))
        async with get_session(self._db_url) as session:
            rows = (await session.execute(query)).scalars().all()
        return [(r.root_id, StateRow(AssetCategory(r.category), r.rel_path, AssetFileState(r.status), r.item_id,
                                     r.content_hash, r.size, r.mtime_ns, r.executable), r.updated_at)
                for r in rows]

    async def last_versions(self, root_ids: list[str]) -> dict[str, datetime]:
        """When each folder last had a version recorded."""
        async with get_session(self._db_url) as session:
            rows = await session.execute(select(AssetVersionModel.root_id, func.max(AssetVersionModel.observed_at))
                                         .where(AssetVersionModel.root_id.in_(root_ids))
                                         .group_by(AssetVersionModel.root_id))
            return {rid: when for rid, when in rows}

    async def stored_bytes(self) -> int:
        async with get_session(self._db_url) as session:
            return int((await session.execute(select(func.coalesce(func.sum(AssetBlobModel.stored_size), 0))))
                       .scalar() or 0)
