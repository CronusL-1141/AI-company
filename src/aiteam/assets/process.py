"""One backup batch: read, hash, tell OS's files apart, scan, store. No database.

The caller (``aiteam.assets.ingest``) decides which paths changed and records the
results; this module does the work that grows with the files. A small batch runs
in a worker thread; a larger one in a child process (``python -c`` booting
:func:`main`, request on stdin, results on stdout), because hashing and the secret
scan are pure Python and would hold the API's interpreter.

Per file the outcome is one of:

* ``synced``: stored (or already stored) in the content store;
* ``os_asset``: a file OS installs, identical to the current release: not stored,
  whatever was stored for that path before;
* ``blocked``: the secret scan hit, and the user has not allowed this content;
* ``oversize``: over the size limit, not read;
* ``symlink``: a link; never followed, never read;
* ``unreadable``: could not be read;
* ``missing``: gone since the walk. Not a deletion: the next comparison decides.

An extraction reads its source files and yields virtual assets, each of which goes
through the same scan and store. A source that is a link, cannot be read or does
not parse fails the whole extraction (``BatchResult.failed``): its category is left
as it was, so a config file caught mid-write never reads as "everything removed".

The status names are the values of ``aiteam.types.AssetFileState`` (a test binds
them): the child process does not import ``aiteam.types`` and its dependencies.

Imports the standard library and the leaf modules of this package only.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aiteam.assets import extract
from aiteam.assets.blob_store import BlobStore, digest
from aiteam.assets.os_manifest import OsManifest, is_our_hook
from aiteam.assets.secret_scan import scan_bytes

MAX_ASSET_BYTES = 5 * 1024 * 1024
# A config file assets are extracted from may be large (the global config file
# grows with every project), but not without bound.
MAX_SOURCE_BYTES = 64 * 1024 * 1024

SYNCED, OS_ASSET, BLOCKED, OVERSIZE, SYMLINK, UNREADABLE, MISSING = (
    "synced", "os_asset", "blocked", "oversize", "symlink", "unreadable", "missing")
STATUSES = frozenset({SYNCED, OS_ASSET, BLOCKED, OVERSIZE, SYMLINK, UNREADABLE})

_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass
class FileJob:
    category: str
    rel: str
    path: str


@dataclass
class ExtractJob:
    category: str
    extractor: str
    sources: list[tuple[str, str]]  # (relative path, absolute path)


@dataclass
class Job:
    data_dir: str
    files: list[FileJob] = field(default_factory=list)
    extractions: list[ExtractJob] = field(default_factory=list)
    manifest: dict | None = None
    source_root: str = ""
    # Content hashes the user allowed despite a scan hit.
    allowed_hashes: list[str] = field(default_factory=list)
    max_bytes: int = MAX_ASSET_BYTES

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Job:
        job = cls(**{key: value for key, value in data.items() if key not in ("files", "extractions")})
        job.files = [FileJob(**item) for item in data.get("files", [])]
        job.extractions = [ExtractJob(item["category"], item["extractor"], [tuple(s) for s in item["sources"]])
                           for item in data.get("extractions", [])]
        return job


@dataclass
class AssetResult:
    category: str
    rel: str
    status: str
    hash: str = ""
    size: int = 0
    mtime_ns: int = 0
    executable: bool = False
    hits: list[tuple[str, int]] = field(default_factory=list)
    blob: dict | None = None
    owner_path: str = ""


@dataclass
class SourceResult:
    category: str
    rel: str
    size: int
    mtime_ns: int


@dataclass
class BatchResult:
    assets: list[AssetResult] = field(default_factory=list)
    sources: list[SourceResult] = field(default_factory=list)
    # Extractions that could not run, by category: the reason.
    failed: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> BatchResult:
        assets = [AssetResult(**{**a, "hits": [tuple(h) for h in a["hits"]]}) for a in data["assets"]]
        return cls(assets=assets, sources=[SourceResult(**s) for s in data["sources"]],
                   failed=dict(data.get("failed", {})))


class _SourceFailedError(Exception):
    """A source of an extraction that must not be read as empty."""


def _read_regular(path: str) -> tuple[os.stat_result, bytes]:
    """A regular file's stat and bytes, refusing a link at the last part."""
    handle = os.open(path, os.O_RDONLY | _NO_FOLLOW)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise IsADirectoryError(path)
        chunks = []
        while chunk := os.read(handle, 1 << 20):
            chunks.append(chunk)
        return info, b"".join(chunks)
    finally:
        os.close(handle)


class _Batch:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.store = BlobStore(Path(job.data_dir))
        self.manifest = OsManifest.from_json(job.manifest) if job.manifest else None
        self.allowed = set(job.allowed_hashes)
        self.result = BatchResult()

    def _keep(self, result: AssetResult, content: bytes) -> AssetResult:
        """Scan and store one content, filling in the outcome."""
        result.hash, result.size = digest(content), len(content)
        result.hits = [(hit.rule, hit.line) for hit in scan_bytes(content)]
        if result.hits and result.hash not in self.allowed:
            result.status = BLOCKED
            return result
        result.blob = asdict(self.store.put(content, result.hash))
        result.status = SYNCED
        return result

    def file(self, job: FileJob) -> AssetResult:
        result = AssetResult(job.category, job.rel, MISSING)
        try:
            info = os.lstat(job.path)
        except FileNotFoundError:
            return result
        except OSError:
            result.status = UNREADABLE
            return result
        result.size, result.mtime_ns = info.st_size, info.st_mtime_ns
        result.executable = bool(info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        if stat.S_ISLNK(info.st_mode):
            result.status = SYMLINK
            return result
        if not stat.S_ISREG(info.st_mode):
            result.status = UNREADABLE
            return result
        if info.st_size > self.job.max_bytes:
            result.status = OVERSIZE
            return result
        try:
            info, content = _read_regular(job.path)
        except FileNotFoundError:
            return result
        except OSError:
            result.status = SYMLINK if _is_link(job.path) else UNREADABLE
            return result
        result.size, result.mtime_ns = info.st_size, info.st_mtime_ns
        if self.manifest is not None and self.manifest.os_name(job.rel) \
                and self.manifest.is_release(job.rel, content):
            result.hash, result.size, result.status = digest(content), len(content), OS_ASSET
            return result
        return self._keep(result, content)

    def _read_source(self, category: str, rel: str, path: str) -> bytes | None:
        """A source's bytes, None when it is not there; raises when it must not count as empty."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _SourceFailedError(f"{rel}: {exc.strerror}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise _SourceFailedError(f"{rel}: 符号链接")
        if info.st_size > MAX_SOURCE_BYTES:
            raise _SourceFailedError(f"{rel}: 超过 {MAX_SOURCE_BYTES // (1024 * 1024)}MB")
        try:
            info, content = _read_regular(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _SourceFailedError(f"{rel}: {exc.strerror}") from exc
        self.result.sources.append(SourceResult(category, rel, info.st_size, info.st_mtime_ns))
        return content

    def _extractor(self, name: str) -> Callable[[list[bytes | None]], list[extract.Extracted] | None]:
        root = Path(self.job.source_root) if self.job.source_root else None
        own_server = self.manifest.mcp_server_name if self.manifest else ""
        return {
            "settings_hooks": lambda data: extract.settings_hooks(data[0], lambda c: is_our_hook(c, root)),
            "mcp_servers": lambda data: extract.mcp_servers(data[0], own_server),
            "plugin_manifest": lambda data: extract.plugin_manifest(data[0], data[1] if len(data) > 1 else None),
        }[name]

    def extraction(self, job: ExtractJob) -> None:
        sources_before = len(self.result.sources)
        try:
            contents = [self._read_source(job.category, rel, path) for rel, path in job.sources]
            found = self._extractor(job.extractor)(contents)
            if found is None:
                raise _SourceFailedError("内容不是有效的 JSON（可能正在被写入）")
        except _SourceFailedError as exc:
            del self.result.sources[sources_before:]
            self.result.failed[job.category] = str(exc)
            return
        for item in found:
            result = AssetResult(job.category, item.rel_path, MISSING, owner_path=item.owner_path)
            self.result.assets.append(self._keep(result, item.content))

    def run(self) -> BatchResult:
        for job in self.job.files:
            self.result.assets.append(self.file(job))
        for job in self.job.extractions:
            self.extraction(job)
        return self.result


def _is_link(path: str) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def run(job: Job) -> BatchResult:
    """Process one batch (blocking: a worker thread or the child process)."""
    return _Batch(job).run()


def main() -> None:
    """Child process entry: a job as JSON on stdin, the result as JSON on stdout."""
    job = Job.from_json(json.loads(sys.stdin.buffer.read()))
    json.dump(run(job).to_json(), sys.stdout, separators=(",", ":"))
