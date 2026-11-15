"""The content store: by hash, once, compressed, verified on read."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from aiteam.assets.blob_store import BlobStore, digest


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def test_same_content_twice_is_one_file(tmp_path: Path):
    store = BlobStore(tmp_path)
    first = store.put(b"memory text\n")
    second = store.put(b"memory text\n")
    assert first == second and first.hash == digest(b"memory text\n")
    assert _files(store.root) == [store.path(first.hash)]
    assert store.get(first.hash) == b"memory text\n"


def test_content_is_compressed_and_checked(tmp_path: Path):
    store = BlobStore(tmp_path)
    blob = store.put(b"a" * 10_000)
    assert blob.stored_size < blob.raw_size == 10_000
    store.path(blob.hash).write_bytes(zlib.compress(b"tampered"))
    with pytest.raises(ValueError):
        store.get(blob.hash)


def test_no_temporary_file_is_left(tmp_path: Path):
    store = BlobStore(tmp_path)
    for n in range(20):
        store.put(f"content {n}".encode())
    assert all(not path.name.startswith(".tmp-") for path in _files(store.root))


@pytest.mark.parametrize("bad", ["", "../etc", "ABCDEF" + "0" * 58, "0" * 63])
def test_only_sha256_hex_names_a_content(tmp_path: Path, bad: str):
    with pytest.raises(ValueError):
        BlobStore(tmp_path).path(bad)
