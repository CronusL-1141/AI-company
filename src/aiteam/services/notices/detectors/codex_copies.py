"""E13: read-only comparison of installed Codex copies with their adapter.

Like ``check_codex_installed_hooks.py``, the observer directory only accepts
the Codex adapter as its baseline, never the same-named Claude hook. The
install receipt identifies that baseline and distinguishes an old, untouched
copy from user edits. Missing evidence must not clear a persisted notice.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shlex
import stat
from functools import lru_cache
from pathlib import Path

from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope

OBSERVER_DIRNAME = "ai-team-os-observer"
RECEIPT_NAME = ".aiteam-codex-install.json"
ADAPTER_PATH = Path("plugin/harness/codex/hooks")
_MAX_JSON_BYTES = 256 * 1024
_MAX_SCRIPT_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


def codex_home() -> Path:
    """Match native utils/home-dir: explicit home is canonical, default is not.

    An explicit value is a literal path (no tilde expansion or trimming), must
    exist and must be a directory. Only the service's environment is consulted.
    """
    configured = os.environ.get("CODEX_HOME", "")
    if not configured:
        return Path.home() / ".codex"
    try:
        path = Path(configured).resolve(strict=True)
        if not path.is_dir():
            raise NoDataError
        return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise NoDataError from exc


def file_signature(path: Path, *, missing_ok: bool = False) -> tuple[int, ...]:
    """Cheap cache identity, including ctime to detect equal-size/mtime edits."""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        if missing_ok:
            return ()
        raise NoDataError from exc
    except OSError as exc:
        raise NoDataError from exc
    if not stat.S_ISREG(info.st_mode):
        # A custom symlink is not an installer-owned file that OS can diagnose.
        raise NoDataError
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino


def read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise NoDataError from exc
    if len(raw) > limit:
        raise NoDataError
    return raw


@lru_cache(maxsize=8)
def _receipt_at(path: Path, signature: tuple[int, ...]) -> dict:
    try:
        receipt = json.loads(read_bounded(path, _MAX_JSON_BYTES))
    except (ValueError, UnicodeError) as exc:
        raise NoDataError from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != 1:
        raise NoDataError
    names, hashes = receipt.get("files"), receipt.get("sha256")
    if not isinstance(names, list) or not names or len(names) > 256 or not isinstance(hashes, dict):
        raise NoDataError
    retired = receipt.get("retired_files", [])
    retired_hashes = receipt.get("retired_sha256", {})
    if not isinstance(retired, list) or len(retired) > 256 or not isinstance(retired_hashes, dict):
        raise NoDataError
    for tracked, recorded in ((names, hashes), (retired, retired_hashes)):
        for name in tracked:
            if (not isinstance(name, str) or not name or Path(name).name != name
                    or "\\" in name or Path(name).suffix not in {".py", ".sh"}
                    or not isinstance(recorded.get(name), str) or not _SHA256.fullmatch(recorded[name])):
                raise NoDataError
    if (len(set(names + retired)) != len(names + retired)
            or file_signature(path) != signature):
        raise NoDataError
    return receipt


def read_receipt(home: Path) -> dict:
    """Validated installer receipt; called only from IO worker threads."""
    path = home / "hooks" / OBSERVER_DIRNAME / RECEIPT_NAME
    return _receipt_at(path, file_signature(path))


@lru_cache(maxsize=8)
def _distributed_names(surface_path: Path, signature: tuple[int, ...]) -> frozenset[str]:
    """Read distribution declarations without executing the installation repo.

    ``_owned_names`` in scripts/codex_adapter.py distributes surface entry
    scripts, its companions and the two shared modules below. Unknown AST
    shapes are no-data; scanning arbitrary .py files is not an install plan.
    """
    try:
        tree = ast.parse(read_bounded(surface_path, _MAX_SCRIPT_BYTES))
        values = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                values[node.target.id] = node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                values[node.targets[0].id] = node.value
        scripts_node = values["CODEX_HOOK_SCRIPTS"]
        if isinstance(scripts_node, (ast.Tuple, ast.List)):
            scripts = ast.literal_eval(scripts_node)
        else:
            # This expression derives the installed entries from the surface.
            # An arbitrary call/expression must never be evaluated or guessed.
            expected = ast.parse(
                "tuple(dict.fromkeys(script for _e, _m, _k, entries in CODEX_HOOK_SURFACE "
                "for script, _a, _t, _l in entries))", mode="eval",
            ).body
            if ast.dump(scripts_node) != ast.dump(expected):
                raise NoDataError
            rows = values["CODEX_HOOK_SURFACE"]
            if not isinstance(rows, (ast.List, ast.Tuple)):
                raise NoDataError
            scripts = []
            for row in rows.elts:
                if not isinstance(row, (ast.List, ast.Tuple)) or len(row.elts) != 4:
                    raise NoDataError
                entries = row.elts[3]
                if not isinstance(entries, (ast.List, ast.Tuple)):
                    raise NoDataError
                for entry in entries.elts:
                    if not isinstance(entry, (ast.List, ast.Tuple)) or len(entry.elts) != 4:
                        raise NoDataError
                    scripts.append(ast.literal_eval(entry.elts[0]))
        companions = ast.literal_eval(values["CODEX_SUPPORT_MODULES"])
        if not isinstance(companions, (tuple, list)):
            raise NoDataError
        names = [*scripts, *companions, "hook_core.py", "user_notice.py"]
        if len(names) > 256 or any(
            not isinstance(name, str) or not name or Path(name).name != name or "\\" in name
            or Path(name).suffix not in {".py", ".sh"} for name in names
        ):
            raise NoDataError
    except (SyntaxError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise NoDataError from exc
    if file_signature(surface_path) != signature:
        raise NoDataError
    return frozenset(names)


def _registered_retired(home: Path, names: set[str]) -> set[str]:
    """Prove which missing retired scripts still have a registration.

    Only a readable manifest and an understood command can prove cleanup.
    The runtime import shares E16's ownership parser without a startup cycle.
    """
    from aiteam.services.notices.detectors.codex_trust import UnsupportedDeclarationError, _owned

    try:
        document = json.loads(read_bounded(home / "hooks.json", _MAX_JSON_BYTES))
    except (ValueError, UnicodeError) as exc:
        raise NoDataError from exc
    if not isinstance(document, dict) or not isinstance(document.get("hooks"), dict):
        raise NoDataError
    registered = set()
    installed = home / "hooks" / OBSERVER_DIRNAME
    for groups in document["hooks"].values():
        if not isinstance(groups, list):
            raise NoDataError
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise NoDataError
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise NoDataError
                command = (handler.get("commandWindows", handler.get("command_windows", handler.get("command")))
                           if os.name == "nt" else handler.get("command"))
                if not isinstance(command, str) or not any(name in command for name in names):
                    continue
                try:
                    parts = shlex.split(command, posix=os.name != "nt")
                    if not _owned(command, installed):
                        operand = (parts[1] if len(parts) > 1 and re.fullmatch(
                            r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(parts[0]).name,
                        ) else parts[0] if parts else "")
                        if operand and not Path(operand).is_absolute() and Path(operand).name in names:
                            raise NoDataError  # relative execution cannot prove this registration was cleaned up
                        continue
                    # _owned has established a simple direct script or Python
                    # script operand, not a path hidden in arbitrary arguments.
                    script = parts[0] if Path(parts[0]).suffix in {".py", ".sh"} else parts[1]
                except (UnsupportedDeclarationError, ValueError, IndexError) as exc:
                    raise NoDataError from exc
                if Path(script).name in names:
                    registered.add(Path(script).name)
    return registered


class CodexCopiesDetector:
    name = "codex_copies"
    catalog_ids = ("codex_copy_stale",)
    timing = frozenset({"session_start"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def __init__(self) -> None:
        self._cache: tuple[object, tuple[Finding, ...]] | None = None

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("codex_copy_stale:",))

    def _inspect(self) -> list[Finding]:
        home = codex_home()
        receipt = read_receipt(home)
        root = receipt.get("repo_root")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise NoDataError
        source = Path(root) / ADAPTER_PATH
        installed = home / "hooks" / OBSERVER_DIRNAME
        surface_path = source.parent / "surface.py"
        surface_signature = file_signature(surface_path)
        source_names = _distributed_names(surface_path, surface_signature)
        try:
            if not any(source.iterdir()):
                raise NoDataError
        except OSError as exc:
            raise NoDataError from exc
        known_hashes = {**receipt.get("retired_sha256", {}), **receipt["sha256"]}
        known_names = set(receipt["files"]) | set(receipt.get("retired_files", []))
        names = sorted(source_names | known_names)
        signatures = tuple((name, file_signature(source / name, missing_ok=True) if name in source_names else (),
                            file_signature(installed / name, missing_ok=True)) for name in names)
        missing_retired = {name for name, _, signature in signatures if name not in source_names and not signature}
        manifest_signature = file_signature(home / "hooks.json") if missing_retired else None
        receipt_signature = file_signature(installed / RECEIPT_NAME)
        identity = (str(home), str(source), receipt_signature, surface_signature, manifest_signature, signatures)
        if self._cache is not None and self._cache[0] == identity:
            return list(self._cache[1])
        registered_retired = _registered_retired(home, missing_retired) if missing_retired else set()
        differences: dict[str, list[tuple[str, str, str]]] = {
            "": [], "missing": [], "modified": [], "source_missing": [], "retired": [],
        }
        for name, source_signature, installed_signature in signatures:
            if name in source_names and not source_signature:
                actual = (hashlib.sha256(read_bounded(installed / name, _MAX_SCRIPT_BYTES)).hexdigest()
                          if installed_signature else "missing")
                differences["source_missing"].append((name, actual, "source_missing"))
                continue
            expected = (hashlib.sha256(read_bounded(source / name, _MAX_SCRIPT_BYTES)).hexdigest()
                        if name in source_names else "removed")
            if not installed_signature:
                if name in source_names:
                    differences["missing"].append((name, "missing", expected))
                elif name in registered_retired:
                    differences["retired"].append((name, "still_registered", "removed"))
                continue
            actual = hashlib.sha256(read_bounded(installed / name, _MAX_SCRIPT_BYTES)).hexdigest()
            if actual == expected:
                continue
            variant = ("" if name in source_names else "retired") if actual == known_hashes.get(name) else "modified"
            differences[variant].append((name, actual, expected))
        after = tuple((name, file_signature(source / name, missing_ok=True) if name in source_names else (),
                       file_signature(installed / name, missing_ok=True)) for name in names)
        if (after != signatures or file_signature(installed / RECEIPT_NAME) != receipt_signature
                or file_signature(surface_path) != surface_signature
                or (missing_retired and file_signature(home / "hooks.json") != manifest_signature)):
            raise NoDataError  # never cache a partial update observed mid-copy
        hits = []
        for variant, rows in differences.items():
            if rows:
                payload = json.dumps([str(home), variant, rows], separators=(",", ":"))
                digest = hashlib.sha256(payload.encode()).hexdigest()[:8]
                hits.append(Finding(catalog_id="codex_copy_stale", key=f"codex_copy_stale:{digest}",
                                    variant=variant, params={"n": len(rows)}))
        self._cache = identity, tuple(hits)
        return hits

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        return await asyncio.to_thread(self._inspect)
