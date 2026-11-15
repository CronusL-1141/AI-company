"""Which relative paths of a config folder are assets, and of which category.

The rules are data (``catalog_claude``, ``catalog_codex``); this module only matches
them. The classifier is a pure function of (host, relative path): string work only,
no disk. It answers in two steps:

1. the deny table (shared rules plus the host's): a hit is never an asset, whatever
   a category says. Every deny rule carries sample paths, and every sample must
   come out unmanaged (machine-checked);
2. the categories, in order: the first whose patterns match and whose excludes do
   not. The catalog is a whitelist: a path no category names is not managed.

Patterns are globs over ``/``-separated segments: ``*`` and ``?`` within a segment,
``**`` for any number of segments (none included). Deny rules match case-insensitively,
categories exactly: a deny rule that misses is the dangerous direction.

A category with an ``extractor`` names config files that assets are extracted from
(a key of a JSON file, say); the virtual assets themselves never exist on disk.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cache

from aiteam.types import AssetCategory, AssetHost

Segments = tuple[str, ...]


@dataclass(frozen=True)
class CategoryRule:
    """One category of a host's catalog."""

    category: AssetCategory
    patterns: tuple[str, ...]
    excludes: tuple[str, ...] = ()
    # Empty: each matching file is an asset. Otherwise the matching files are the
    # sources of an extraction (see aiteam.assets.extract).
    extractor: str = ""
    default_selected: bool = True


@dataclass(frozen=True)
class DenyRule:
    """A path that is never read, with sample paths it must catch."""

    pattern: str
    samples: tuple[str, ...]
    reason: str
    # Paths the rule leaves alone (the two plugin lists inside an otherwise denied folder).
    unless: tuple[str, ...] = ()

    def catches(self, path: Segments) -> bool:
        return matches(self.pattern, path, fold=True) and not _any(self.unless, path, fold=True)

    def covers_folder(self, folder: Segments) -> bool:
        """Whether every path below ``folder`` is caught (a ``<folder pattern>/**/*`` rule)."""
        return not self.unless and self.pattern.endswith(_WHOLE_FOLDER) \
            and matches(self.pattern[: -len(_WHOLE_FOLDER)] + "/**", folder, fold=True)


_WHOLE_FOLDER = "/**/*"


@dataclass(frozen=True)
class HostCatalog:
    """A host's catalog: its version (a restore compares them), categories and own deny rules."""

    host: AssetHost
    version: int
    categories: tuple[CategoryRule, ...]
    deny: tuple[DenyRule, ...]

    def rule(self, category: AssetCategory) -> CategoryRule | None:
        return next((rule for rule in self.categories if rule.category == category), None)

    @property
    def default_categories(self) -> list[AssetCategory]:
        return [rule.category for rule in self.categories if rule.default_selected]


def segments(rel_path: str) -> Segments | None:
    """A relative path as segments, or None when it is absolute, empty or climbs out (``..``)."""
    if not rel_path or rel_path.startswith("/") or "\x00" in rel_path:
        return None
    parts = tuple(part for part in rel_path.split("/") if part not in ("", "."))
    if not parts or ".." in parts:
        return None
    return parts


@cache
def _split(pattern: str) -> Segments:
    return tuple(pattern.split("/"))


def _match(pattern: Segments, path: Segments, i: int = 0, j: int = 0) -> bool:
    if i == len(pattern):
        return j == len(path)
    if pattern[i] == "**":
        return _match(pattern, path, i + 1, j) or (j < len(path) and _match(pattern, path, i, j + 1))
    return j < len(path) and fnmatchcase(path[j], pattern[i]) and _match(pattern, path, i + 1, j + 1)


def _viable(pattern: Segments, folder: Segments, i: int = 0, j: int = 0) -> bool:
    """Whether some file below ``folder`` could match ``pattern``."""
    if j == len(folder):
        return i < len(pattern)
    if i == len(pattern):
        return False
    if pattern[i] == "**":
        return _viable(pattern, folder, i + 1, j) or _viable(pattern, folder, i, j + 1)
    return fnmatchcase(folder[j], pattern[i]) and _viable(pattern, folder, i + 1, j + 1)


def matches(pattern: str, path: Segments, *, fold: bool = False) -> bool:
    """Whether ``path`` matches the glob ``pattern`` (``fold``: ignoring case)."""
    if fold:
        return _match(_split(pattern.lower()), tuple(part.lower() for part in path))
    return _match(_split(pattern), path)


def _any(patterns: Iterable[str], path: Segments, *, fold: bool = False) -> bool:
    return any(matches(pattern, path, fold=fold) for pattern in patterns)


def _catalogs() -> dict[AssetHost, HostCatalog]:
    from aiteam.assets.catalog_claude import CATALOG as CLAUDE
    from aiteam.assets.catalog_codex import CATALOG as CODEX

    return {CLAUDE.host: CLAUDE, CODEX.host: CODEX}


@cache
def catalog(host: AssetHost) -> HostCatalog:
    """The catalog of ``host``."""
    return _catalogs()[AssetHost(host)]


# Never read in any host's folder: backups and leftovers of other tools, lock and
# temporary files, SQLite side files, caches and telemetry.
SHARED_DENY: tuple[DenyRule, ...] = (
    DenyRule("**/*.bak*", ("settings.json.bak-20261111", "projects/-p/memory/MEMORY.md.bak"), "backup copy"),
    DenyRule("**/*.backup*", ("CLAUDE.md.backup", "agents/reviewer.md.backup-2"), "backup copy"),
    DenyRule("**/*.before-*", ("settings.json.before-install",), "copy kept by an installer"),
    DenyRule("**/*.recovery", ("CLAUDE.md.recovery",), "recovery copy"),
    DenyRule("**/*.lock", ("auth.json.lock", "skills/tool/yarn.lock"), "lock file"),
    DenyRule("**/*.tmp", ("settings.json.tmp",), "temporary file"),
    DenyRule("**/*-shm", ("state_5.sqlite-shm",), "SQLite side file"),
    DenyRule("**/*-wal", ("history.db-wal",), "SQLite side file"),
    DenyRule("**/*backup*/**/*", ("backups/settings.json", "projects/-p/memory/old-backup/a.md"),
             "folder of backups"),
    DenyRule("**/cache/**/*", ("cache/changelog.md", "skills/tool/cache/a.json"), "cache"),
    DenyRule("**/telemetry/**/*", ("telemetry/events.json",), "telemetry"),
    DenyRule("**/shell-snapshots/**/*", ("shell-snapshots/snapshot-zsh.sh",), "shell snapshots"),
)


def deny_rules(host: AssetHost) -> tuple[DenyRule, ...]:
    """The deny rules that apply to ``host``: the shared ones first."""
    return SHARED_DENY + catalog(host).deny


def denied_by(host: AssetHost, rel_path: str) -> DenyRule | None:
    """The deny rule ``rel_path`` falls under, or None. A path that is not a clean relative path
    is refused by a placeholder rule, never classified."""
    path = segments(rel_path)
    if path is None:
        return _INVALID
    return next((rule for rule in deny_rules(host) if rule.catches(path)), None)


_INVALID = DenyRule(pattern="", samples=(), reason="not a relative path inside the folder")


def classify(host: AssetHost, rel_path: str) -> AssetCategory | None:
    """The category of ``rel_path`` in a ``host`` config folder, or None when it is not managed."""
    if denied_by(host, rel_path) is not None:
        return None
    path = segments(rel_path)
    assert path is not None  # denied_by refuses every other shape
    for rule in catalog(host).categories:
        if _any(rule.patterns, path) and not _any(rule.excludes, path):
            return rule.category
    return None


def worth_walking(host: AssetHost, rel_dir: str, categories: Iterable[AssetCategory]) -> bool:
    """Whether a walk should descend into ``rel_dir``: some file below it could be an asset
    of one of ``categories`` and the folder itself is not denied as a whole."""
    return folder_category(host, rel_dir, categories) is not None


def folder_category(host: AssetHost, rel_dir: str, categories: Iterable[AssetCategory]) -> AssetCategory | None:
    """The first of ``categories`` some file below ``rel_dir`` could belong to, unless the
    folder is denied as a whole. Decides where a walk descends, and what a skipped folder
    link would have held."""
    folder = segments(rel_dir)
    if folder is None or any(rule.covers_folder(folder) for rule in deny_rules(host)):
        return None
    chosen = set(categories)
    for rule in catalog(host).categories:
        if rule.category in chosen and not rule.extractor \
                and any(_viable(_split(pattern), folder) for pattern in rule.patterns):
            return rule.category
    return None
