"""The content store: every backed-up content once, by SHA-256, as a file.

``<data dir>/asset-store/blobs/<first two hex>/<sha256>``, zlib-compressed. The
data directory comes from ``aiteam.paths`` and is never created here: a write into
a data directory that is not there fails (``StoreMissing``) and the backup stops.
Only the store's own three levels are created, one at a time, readable by the
owner alone (folders 0700, files 0600, like the config files they copy).

A content is written before any row names it: a temporary file in the same folder,
flushed to disk, renamed into place, the folder flushed. So a version row always has
its content on this node, and a process killed mid-write leaves at most a temporary
file nobody refers to. Writing a content that is already there does nothing.

Contents are not kept in the database: session records reach hundreds of megabytes
per file, and every copy of the database (and every baseline a sync takes) would
carry them.

Standard library only: the backup child process imports this module.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import zlib
from dataclasses import dataclass
from pathlib import Path

STORE_DIR = "asset-store"
ENCODING = "zlib"
_LEVEL = 6
_TEMP_PREFIX = ".tmp-"
_FOLDER_MODE = 0o700
_FILE_MODE = 0o600


class StoreMissing(FileNotFoundError):  # noqa: N818 - the condition, as the notice names it
    """The data directory the store lives in is not there; nothing is created in its place."""


@dataclass(frozen=True)
class StoredBlob:
    """What the database records about a stored content."""

    hash: str
    raw_size: int
    stored_size: int
    encoding: str


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(folder: Path) -> None:
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return  # not every platform opens folders; the rename itself is atomic
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


class BlobStore:
    """The content store under one data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / STORE_DIR / "blobs"

    def path(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise ValueError(f"not a SHA-256 hex digest: {content_hash!r}")
        return self.root / content_hash[:2] / content_hash

    def has(self, content_hash: str) -> bool:
        return self.path(content_hash).is_file()

    def put(self, data: bytes, content_hash: str | None = None) -> StoredBlob:
        """Store ``data`` (its hash is computed unless given) and return what to record."""
        content_hash = content_hash or digest(data)
        target = self.path(content_hash)
        if target.is_file():
            return StoredBlob(content_hash, len(data), target.stat().st_size, ENCODING)
        packed = zlib.compress(data, _LEVEL)
        self._folders(target.parent)
        temporary = target.with_name(f"{_TEMP_PREFIX}{content_hash}.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
            with os.fdopen(handle, "wb") as stream:
                stream.write(packed)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_dir(target.parent)
        return StoredBlob(content_hash, len(data), len(packed), ENCODING)

    def _folders(self, leaf: Path) -> None:
        """Create the store's own folders below the data directory, never the data directory."""
        if not self.data_dir.is_dir():
            raise StoreMissing(f"数据目录不在：{self.data_dir}")
        for folder in (self.data_dir / STORE_DIR, self.root, leaf):
            try:
                os.mkdir(folder, _FOLDER_MODE)
            except FileExistsError:
                pass

    def get(self, content_hash: str) -> bytes:
        """The content, checked against its hash (raises ValueError on a mismatch)."""
        data = zlib.decompress(self.path(content_hash).read_bytes())
        if digest(data) != content_hash:
            raise ValueError(f"stored content does not match its hash {content_hash}")
        return data
