"""E11 installed_copy_stale: installed copies lag behind the files they came from.

Two variants, by who owns the global copies:

* source install (default variant; ``install_path.txt`` exists, the same
  ownership rule auto_install uses): the source installer copied hooks, skills,
  agents and commands under the Claude Code folder. The baseline is the source
  tree of this API (its ``plugin/`` folder), falling back to the tree recorded
  in ``install_path.txt``. A copy is stale when its bytes differ from the
  baseline file of the same name; a hook the installer distributes (read from
  the baseline ``install.py`` without executing it) is stale when it is
  missing. Files that are not in the baseline (the user's own skills or hooks)
  never take part.
* plugin install (``plugin_sync_failed``): auto_install re-syncs the global hook
  copies itself (E12); when that fails it records ``sync_failed`` in
  install-state.json, and this detector reports it.

The comparison is cached on a stat signature (name, size, mtime of every file
on both sides), so an unchanged install costs one stat pass per request.
:func:`compare_sync` is shared with ``services.config_change``, which previews
and applies the fix for the default variant.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import aiteam
from aiteam.services.notices import install_kind
from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope

RUNTIME_HOOKS_DIRNAME = "ai-team-os"
# Hooks the plugin keeps to itself: the self-heal entry point, and the removal
# script that only a plugin-installed chain carries.
PLUGIN_ONLY_HOOKS = frozenset({"auto_install.py", "uninstall_main_chain.py"})
_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_MAX_LISTED = 50
_cache: dict[str, object] = {}


@dataclass(frozen=True)
class StaleCopy:
    """One installed file that differs from (or is missing next to) its baseline."""

    relative: str  # path under the Claude Code folder, e.g. "hooks/ai-team-os/x.py"
    installed: Path
    source: Path
    missing: bool = False


@dataclass(frozen=True)
class Baseline:
    """Where the expected files come from."""

    root: Path
    branch: str = ""
    version: str = ""

    @property
    def plugin(self) -> Path:
        return self.root / "plugin"


@dataclass(frozen=True)
class CopyDiff:
    """Result of one comparison: the baseline and every stale copy."""

    baseline: Baseline
    config_dir: Path
    stale: tuple[StaleCopy, ...] = field(default_factory=tuple)

    @property
    def digest(self) -> str:
        return hashlib.sha256("\n".join(item.relative for item in self.stale).encode()).hexdigest()[:8]

    @property
    def key(self) -> str:
        return f"installed_copy_stale:cc:{self.digest}"


def _api_source_root() -> Path | None:
    """The checkout this API runs from (editable install), when it ships ``plugin/``."""
    root = Path(aiteam.__file__).resolve().parents[2]
    return root if (root / "plugin" / "hooks").is_dir() else None


def _recorded_source_root() -> Path | None:
    try:
        text = (install_kind.os_data_dir() / "install_path.txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    root = Path(text).expanduser() if text else None
    return root if root is not None and (root / "plugin" / "hooks").is_dir() else None


def git_branch(root: Path) -> str:
    """Current branch of a checkout, read from files (worktrees included); "" when unknown."""
    dot_git = root / ".git"
    try:
        if dot_git.is_file():
            pointer = dot_git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return ""
            git_dir = Path(pointer.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
        else:
            git_dir = dot_git
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return f"detached {head[:8]}" if head else ""


def _read_version(root: Path) -> str:
    try:
        match = _VERSION.search((root / "src" / "aiteam" / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return ""
    return match.group(1) if match else ""


def source_baseline() -> Baseline | None:
    """Baseline for a source install: this API's tree, else the recorded install tree."""
    root = _api_source_root() or _recorded_source_root()
    if root is None:
        return None
    return Baseline(root=root, branch=git_branch(root), version=_read_version(root))


def distributed_hooks(baseline: Baseline) -> frozenset[str] | None:
    """Hook files the baseline ``install.py`` copies, read from its literals (never executed).

    None when the installer cannot be parsed; missing copies are then not reported.
    """
    try:
        tree = ast.parse((baseline.root / "install.py").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return None
    values: dict[str, object] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target in ("HOOK_SURFACE", "HOOK_SUPPORT_MODULES") and node.value is not None:
            try:
                values[target] = ast.literal_eval(node.value)
            except ValueError:
                return None
    surface = values.get("HOOK_SURFACE")
    if not isinstance(surface, list):
        return None
    names: set[str] = set()
    try:
        for _event, _matcher, entries in surface:
            for entry in entries:
                names.add(str(entry[0]))
    except (TypeError, ValueError, IndexError):
        return None
    support = values.get("HOOK_SUPPORT_MODULES", ())
    if isinstance(support, (tuple, list)):
        names.update(str(name) for name in support)
    return frozenset(names)


def _pairs(baseline: Baseline, config_dir: Path) -> Iterable[tuple[str, Path, Path, bool]]:
    """(relative path, installed file, baseline file, report when missing)."""
    plugin = baseline.plugin
    distributed = distributed_hooks(baseline) or frozenset()
    hooks_dir = config_dir / "hooks" / RUNTIME_HOOKS_DIRNAME
    for source in sorted((plugin / "hooks").glob("*.py")):
        if source.name in PLUGIN_ONLY_HOOKS:
            continue
        yield (f"hooks/{RUNTIME_HOOKS_DIRNAME}/{source.name}", hooks_dir / source.name, source,
               source.name in distributed)
    for folder in ("agents", "commands"):
        for source in sorted((plugin / folder).glob("*.md")):
            yield f"{folder}/{source.name}", config_dir / folder / source.name, source, False
    skills = plugin / "skills"
    for source in sorted(skills.rglob("*")) if skills.is_dir() else ():
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(skills).as_posix()
        yield f"skills/{relative}", config_dir / "skills" / relative, source, False


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def _same_bytes(first: Path, second: Path) -> bool:
    try:
        return first.read_bytes() == second.read_bytes()
    except OSError:
        return False


def compare_sync(baseline: Baseline | None = None, config_dir: Path | None = None) -> CopyDiff | None:
    """Blocking comparison for a source install; None when there is no baseline."""
    baseline = baseline or source_baseline()
    if baseline is None:
        return None
    config_dir = config_dir or install_kind.cc_config_dir()
    pairs = list(_pairs(baseline, config_dir))
    signature = (str(baseline.root), str(config_dir), tuple(
        (relative, _stat(installed), _stat(source)) for relative, installed, source, _report in pairs
    ))
    if _cache.get("signature") == signature and isinstance(_cache.get("diff"), CopyDiff):
        # The branch and version may change without touching a compared file.
        return CopyDiff(baseline=baseline, config_dir=config_dir, stale=_cache["diff"].stale)  # type: ignore[union-attr]
    stale: list[StaleCopy] = []
    for relative, installed, source, report_missing in pairs:
        if not installed.exists():
            if report_missing:
                stale.append(StaleCopy(relative, installed, source, missing=True))
            continue
        if not _same_bytes(installed, source):
            stale.append(StaleCopy(relative, installed, source))
    diff = CopyDiff(baseline=baseline, config_dir=config_dir, stale=tuple(stale))
    _cache.update(signature=signature, diff=diff)
    return diff


def plugin_sync_failure() -> tuple[int, list[str]] | None:
    """(count, script names) auto_install could not re-sync (install-state ``sync_failed``)."""
    try:
        state = json.loads((install_kind.os_data_dir() / "install-state.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    failed = state.get("sync_failed") if isinstance(state, dict) else None
    if not isinstance(failed, dict):
        return None
    files = [str(name) for name in failed.get("files") or [] if isinstance(name, str)][:_MAX_LISTED]
    try:
        count = int(failed.get("n") or len(files))
    except (TypeError, ValueError):
        count = len(files)
    return (count, files) if count > 0 else None


class InstalledCopiesDetector:
    """Installed copies behind their source (source install) or a failed plugin re-sync."""

    name = "installed_copies"
    catalog_ids = ("installed_copy_stale",)
    timing = frozenset({"session_start"})
    hosts = frozenset({"cc"})
    timeout_s = 0.8

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("installed_copy_stale:cc:",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        kind = await install_kind.detect("cc")
        # Same ownership rule as auto_install: once a source install recorded
        # itself, it owns the global copies even when the plugin is enabled too.
        source_owned = await asyncio.to_thread(
            (install_kind.os_data_dir() / "install_path.txt").exists,
        )
        if kind.kind == "cc-plugin" and not source_owned:
            failed = await asyncio.to_thread(plugin_sync_failure)
            if failed is None:
                return []
            count, files = failed
            digest = hashlib.sha256(f"{count}|{'|'.join(files)}".encode()).hexdigest()[:8]
            return [Finding(
                catalog_id="installed_copy_stale",
                key=f"installed_copy_stale:cc:plugin:{digest}",
                params={"n": count},
                variant="plugin_sync_failed",
            )]
        if not source_owned:
            return []
        diff = await asyncio.to_thread(compare_sync)
        if diff is None:
            raise NoDataError
        if not diff.stale:
            return []
        return [Finding(
            catalog_id="installed_copy_stale",
            key=diff.key,
            params={"n": len(diff.stale)},
        )]
