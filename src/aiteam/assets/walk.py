"""The comparison's walk: stat the managed paths of an enrolled folder, read nothing.

Descends only into folders where a selected category could still match (a project
folder's session files and tool outputs are never listed beyond their names) and
never into a folder the deny table covers.

Two rules keep a failed look from passing for a deletion:

* a folder that cannot be listed is reported, and nothing below it counts as gone;
  a path counts as gone only when its nearest listed ancestor was listed and the
  next part of the path is not among its entries (``Walk.confirmed_absent``);
* a folder that is not there (or not a folder) as a whole stops the comparison
  (``FolderMissing``): nothing is recorded for it.

Symbolic links are never followed, to files or to folders: a link is reported and
skipped, its target never read. The fast path decides each written path with the
same predicate (``inspect``), so the two triggers cannot disagree about a link.

Blocking (system calls only): call from a worker thread.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from aiteam.assets.catalog import catalog, classify, folder_category
from aiteam.assets.catalog_claude import GLOBAL_CONFIG_NAME
from aiteam.paths import CC_DEFAULT_CONFIG_DIR, CC_DEFAULT_GLOBAL_CONFIG
from aiteam.types import AssetCategory, AssetHost


class FolderMissing(OSError):  # noqa: N818 - the condition, as the notice names it
    """The enrolled folder is not a readable folder: nothing about its files is known."""


@dataclass(frozen=True)
class Seen:
    """One managed file as the walk found it."""

    category: AssetCategory
    rel: str
    path: str
    size: int
    mtime_ns: int


@dataclass
class Walk:
    """What one walk established, and what it could not."""

    files: list[Seen] = field(default_factory=list)
    sources: list[Seen] = field(default_factory=list)
    # Extraction sources that could not be looked at (unreadable folder, a link): their
    # categories are left as they were.
    source_errors: dict[AssetCategory, str] = field(default_factory=dict)
    links: list[tuple[AssetCategory, str]] = field(default_factory=list)
    listed: dict[str, frozenset[str]] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    def confirmed_absent(self, rel: str) -> bool:
        """Whether ``rel`` is known to be gone: its nearest listed ancestor lacks the next part."""
        parts = rel.split("/")
        for depth in range(len(parts) - 1, -1, -1):
            names = self.listed.get("/".join(parts[:depth]))
            if names is not None:
                return parts[depth] not in names
        return False


def global_config_path(root: Path) -> Path:
    """The global config file of a Claude Code config folder.

    Inside a configured folder; the default folder keeps it next to itself (the same
    rule as ``aiteam.paths.cc_global_config``, for a folder other than this process's).
    """
    if os.path.abspath(root) == os.path.abspath(os.path.expanduser(CC_DEFAULT_CONFIG_DIR)):
        return Path(os.path.expanduser(CC_DEFAULT_GLOBAL_CONFIG))
    return root / GLOBAL_CONFIG_NAME


def source_path(host: AssetHost, root: Path, rel: str) -> Path:
    """Where an extraction source named ``rel`` lives for the folder ``root``."""
    if host == AssetHost.CLAUDE and rel == GLOBAL_CONFIG_NAME:
        return global_config_path(root)
    return root / rel


def _check_root(root: Path) -> None:
    try:
        info = os.stat(root)
    except OSError as exc:
        raise FolderMissing(exc.errno or errno.ENOENT, f"配置目录读不到：{exc.strerror}", str(root)) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise FolderMissing(errno.ENOTDIR, "配置目录不是一个目录", str(root))


def _stat_sources(host: AssetHost, root: Path, categories: set[AssetCategory], found: Walk) -> None:
    for rule in catalog(host).categories:
        if rule.category not in categories or not rule.extractor:
            continue
        for rel in rule.patterns:
            path = source_path(host, root, rel)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue  # the folder exists (checked first), so the file is not there
            except OSError as exc:
                found.source_errors[rule.category] = f"{rel}: {exc.strerror}"
                continue
            if stat.S_ISLNK(info.st_mode):
                found.source_errors[rule.category] = f"{rel}: 符号链接"
                found.links.append((rule.category, rel))
            elif stat.S_ISREG(info.st_mode):
                found.sources.append(Seen(rule.category, rel, str(path), info.st_size, info.st_mtime_ns))


def walk(host: AssetHost, root: Path, categories: Iterable[AssetCategory]) -> Walk:
    """Every asset of ``categories`` in ``root``, what was skipped, and which folders were listed.

    Raises ``FolderMissing`` when ``root`` itself is not a readable folder.
    """
    _check_root(root)
    chosen = set(categories)
    found = Walk()
    _stat_sources(host, root, chosen, found)
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        try:
            entries = list(os.scandir(root / rel_dir if rel_dir else root))
        except OSError:
            found.unreadable.append(rel_dir)
            continue
        found.listed[rel_dir] = frozenset(entry.name for entry in entries)
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                _visit(host, entry, rel, chosen, found, pending)
            except OSError:
                continue  # the entry went away between listing and stat
    return found


def _visit(host: AssetHost, entry: os.DirEntry, rel: str, chosen: set[AssetCategory], found: Walk,
           pending: list[str]) -> None:
    if entry.is_symlink():
        category = folder_category(host, rel, chosen) if entry.is_dir() else _file_category(host, rel, chosen)
        if category is not None:
            found.links.append((category, rel))
        return
    if entry.is_dir(follow_symlinks=False):
        if folder_category(host, rel, chosen) is not None:
            pending.append(rel)
        return
    category = _file_category(host, rel, chosen)
    if category is not None and entry.is_file(follow_symlinks=False):
        info = entry.stat(follow_symlinks=False)
        found.files.append(Seen(category, rel, entry.path, info.st_size, info.st_mtime_ns))


def _file_category(host: AssetHost, rel: str, chosen: set[AssetCategory]) -> AssetCategory | None:
    """The category of a file path when it is a selected, non-extracted asset."""
    category = classify(host, rel)
    rule = catalog(host).rule(category) if category is not None else None
    return category if rule is not None and not rule.extractor and category in chosen else None


@dataclass(frozen=True)
class Inspected:
    """One written path, decided like the walk decides it: a file, a link, or not there.

    ``link`` is the walk's key for the link: the path itself, or the linked folder above it.
    """

    seen: Seen | None = None
    link: tuple[AssetCategory, str] | None = None


def inspect(host: AssetHost, root: Path, rel: str, category: AssetCategory,
            categories: Iterable[AssetCategory]) -> Inspected:
    """A path the fast path saw written: lstat every part below ``root``; the first link stops it.

    A missing path is not a fact the fast path records: it is left to the next
    comparison, which can confirm it by listing its folder.
    """
    path = root
    parts = rel.split("/")
    for index, part in enumerate(parts):
        path = path / part
        try:
            info = os.lstat(path)
        except OSError:
            return Inspected()
        if stat.S_ISLNK(info.st_mode):
            if index == len(parts) - 1:
                return Inspected(link=(category, rel))
            folder = "/".join(parts[:index + 1])
            folder_kind = folder_category(host, folder, categories)
            return Inspected(link=(folder_kind, folder) if folder_kind is not None else None)
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return Inspected()
    if not stat.S_ISREG(info.st_mode):
        return Inspected()
    return Inspected(seen=Seen(category, rel, str(path), info.st_size, info.st_mtime_ns))


def sources(host: AssetHost, root: Path, categories: Iterable[AssetCategory]) -> Walk:
    """Only the extraction sources of ``categories`` (the fast path's view of an edited config file)."""
    _check_root(root)
    found = Walk()
    _stat_sources(host, root, set(categories), found)
    return found
