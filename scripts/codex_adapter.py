#!/usr/bin/env python3
"""Install, update, inspect and remove the AI Team OS Codex adapter.

This tool owns only ``$CODEX_HOME/hooks/ai-team-os-observer`` and the hook
registration groups that point into that directory. Claude files, MCP data,
the shared API and third-party Codex hooks are outside its write set.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiteam.clock import utc_now  # noqa: E402

CODEX_DIR = ROOT / "plugin" / "harness" / "codex"
DEFAULT_CODEX_HOME = Path.home() / ".codex"
OBSERVER_DIRNAME = "ai-team-os-observer"
MANIFEST_NAME = "hooks.json"
META_NAME = ".aiteam-codex-install.json"


def _surface(repo_root: Path):
    path = repo_root / "plugin" / "harness" / "codex" / "surface.py"
    spec = importlib.util.spec_from_file_location("aiteam_codex_surface", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Codex surface.py: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owned_names(surface: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*surface.CODEX_HOOK_SCRIPTS, *surface.CODEX_SUPPORT_MODULES)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def _load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Codex hooks.json 无法读取: {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("hooks", {}), dict):
        raise RuntimeError(f"Codex hooks.json 结构无效: {path}")
    return document


def _quote(value: Path | str, *, windows: bool) -> str:
    text = str(value)
    if windows:
        return text.replace("/", "\\")
    return text.replace("\\", "/")


def _installed_manifest(surface: Any, install_dir: Path, interpreter: Path) -> dict[str, list[dict[str, Any]]]:
    rendered = surface.render_manifest()
    windows = os.name == "nt"
    result: dict[str, list[dict[str, Any]]] = {}
    for event, groups in rendered["hooks"].items():
        result[event] = []
        for group in groups:
            item = copy.deepcopy(group)
            for handler in item.get("hooks", []):
                field = "command_windows" if windows else "command"
                command = handler[field]
                command = command.replace(surface.PLACEHOLDER_PY, _quote(interpreter, windows=windows))
                command = command.replace(surface.PLACEHOLDER_HOOKS_DIR, _quote(install_dir, windows=windows))
                handler["command"] = command
                handler.pop("command_windows", None)
            result[event].append(item)
    return result


def _command_files(group: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for handler in group.get("hooks", []) if isinstance(group, dict) else []:
        if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
            continue
        try:
            tokens = shlex.split(handler["command"])
        except ValueError:
            continue
        names.extend(Path(token).name for token in tokens if Path(token).suffix == ".py")
    return names


def _is_owned(group: dict[str, Any], install_dir: Path, names: tuple[str, ...]) -> bool:
    marker = install_dir.resolve()
    owned = set(names)
    for handler in group.get("hooks", []) if isinstance(group, dict) else []:
        command = handler.get("command") if isinstance(handler, dict) else None
        if not isinstance(command, str):
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for token in tokens:
            path = Path(token)
            if path.suffix == ".py" and path.name in owned and path.parent.resolve() == marker:
                return True
    return False


def _canonical_group(group: dict[str, Any]) -> dict[str, Any]:
    """Compare the host's empty matcher spelling with the omitted spelling."""
    value = copy.deepcopy(group)
    if value.get("matcher") == "":
        value.pop("matcher", None)
    return value


def _groups_equivalent(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    return [_canonical_group(group) for group in left] == [_canonical_group(group) for group in right]


def _merge_hooks(
    document: dict[str, Any], desired: dict[str, list[dict[str, Any]]],
    install_dir: Path, names: tuple[str, ...], *, remove: bool = False,
) -> tuple[dict[str, Any], bool]:
    result = copy.deepcopy(document)
    hooks = result.setdefault("hooks", {})
    changed_registration = False
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        owned = [index for index, group in enumerate(groups) if _is_owned(group, install_dir, names)]
        if not owned:
            continue
        first = owned[0]
        old = [groups[index] for index in owned]
        for index in reversed(owned):
            groups.pop(index)
        replacement = [] if remove else desired.get(event, [])
        if not remove and _groups_equivalent(old, replacement):
            replacement = old
        groups[first:first] = replacement
        if old != replacement and not _groups_equivalent(old, replacement):
            changed_registration = True
        if not groups:
            hooks.pop(event, None)
    if not remove:
        for event, groups in desired.items():
            current = hooks.setdefault(event, [])
            if not any(_is_owned(group, install_dir, names) for group in current):
                current.extend(copy.deepcopy(groups))
                changed_registration = True
    return result, changed_registration


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.bak-aiteam-{stamp}")
    shutil.copy2(path, target)
    return target


def _metadata(repo_root: Path, install_dir: Path, interpreter: Path, names: tuple[str, ...]) -> dict[str, Any]:
    commit = None
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "schema": 1,
        "installed_at": utc_now().isoformat(),
        "repo_root": str(repo_root),
        "source_commit": commit,
        "python": str(interpreter),
        "files": list(names),
    }


def install(repo_root: Path, codex_home: Path, interpreter: Path, *, dry_run: bool) -> int:
    surface = _surface(repo_root)
    names = _owned_names(surface)
    source_dir = repo_root / "plugin" / "harness" / "codex" / "hooks"
    install_dir = codex_home / "hooks" / OBSERVER_DIRNAME
    hooks_path = codex_home / MANIFEST_NAME
    desired = _installed_manifest(surface, install_dir, interpreter)
    document = _load_hooks(hooks_path)
    merged, registration_changed = _merge_hooks(document, desired, install_dir, names)
    missing = [name for name in names if not (source_dir / name).is_file()]
    if missing:
        raise RuntimeError(f"仓库缺少 Codex 文件: {', '.join(missing)}")
    print(f"目标安装目录: {install_dir}")
    print(f"目标注册文件: {hooks_path}")
    print(f"将同步文件: {', '.join(names)}")
    print("注册声明变化：需要重新授信" if registration_changed else "注册声明未变化：无需重新授信")
    if dry_run:
        return 0
    install_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(source_dir / name, install_dir / name)
    _atomic_json(install_dir / META_NAME, _metadata(repo_root, install_dir, interpreter, names))
    backup = _backup(hooks_path)
    _atomic_json(hooks_path, merged)
    print(f"已更新 Codex 适配器（hooks.json 备份: {backup or '无'}）")
    return 0


def status(repo_root: Path, codex_home: Path, interpreter: Path) -> int:
    surface = _surface(repo_root)
    names = _owned_names(surface)
    source_dir = repo_root / "plugin" / "harness" / "codex" / "hooks"
    install_dir = codex_home / "hooks" / OBSERVER_DIRNAME
    document = _load_hooks(codex_home / MANIFEST_NAME)
    desired = _installed_manifest(surface, install_dir, interpreter)
    installed = [name for name in names if (install_dir / name).is_file()]
    stale = [name for name in names if (install_dir / name).is_file()
             and _sha256(source_dir / name) != _sha256(install_dir / name)]
    missing = [name for name in names if not (install_dir / name).is_file()]
    registered = any(
        _is_owned(group, install_dir, names)
        for groups in document.get("hooks", {}).values() if isinstance(groups, list)
        for group in groups
    )
    current, _ = _merge_hooks(document, desired, install_dir, names)
    registration_ok = current == document and registered
    print(f"安装目录: {'存在' if install_dir.is_dir() else '缺失'} {install_dir}")
    print(f"文件: {len(installed)}/{len(names)} 同步, 漂移 {len(stale)}, 缺失 {len(missing)}")
    print(f"注册: {'已登记且形状正确' if registration_ok else '需要安装或更新'}")
    return 0 if not stale and not missing and registration_ok else 1


def uninstall(repo_root: Path, codex_home: Path, interpreter: Path, *, dry_run: bool) -> int:
    surface = _surface(repo_root)
    names = _owned_names(surface)
    install_dir = codex_home / "hooks" / OBSERVER_DIRNAME
    hooks_path = codex_home / MANIFEST_NAME
    document = _load_hooks(hooks_path)
    merged, changed = _merge_hooks(document, {}, install_dir, names, remove=True)
    files = [install_dir / name for name in names if (install_dir / name).exists()]
    metadata = install_dir / META_NAME
    if metadata.exists():
        files.append(metadata)
    print(f"将移除注册组: {'是' if changed else '否'}")
    print(f"将移除文件: {', '.join(str(path) for path in files) if files else '无'}")
    if dry_run:
        return 0
    if changed:
        backup = _backup(hooks_path)
        _atomic_json(hooks_path, merged)
        print(f"已更新注册文件（备份: {backup or '无'}）")
    for path in files:
        path.unlink(missing_ok=True)
    try:
        install_dir.rmdir()
    except OSError:
        pass
    print("已移除 Codex 适配器文件与注册；共享 API、数据库和 Claude 文件未改动")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Team OS Codex 适配器生命周期管理")
    parser.add_argument("command", choices=("install", "update", "status", "uninstall"))
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--codex-home", type=Path, default=DEFAULT_CODEX_HOME)
    parser.add_argument("--python", dest="interpreter", type=Path, default=Path(sys.executable))
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不写入")
    parser.add_argument("--apply", action="store_true", help="确认执行卸载；install/update 不需要")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.expanduser().resolve()
    codex_home = args.codex_home.expanduser().resolve()
    # Keep the interpreter spelling the user/installer supplied. Resolving a
    # Homebrew symlink changes the command string and falsely looks like a
    # registration change, even though it is the same executable.
    interpreter = args.interpreter.expanduser().absolute()
    if args.command == "status":
        return status(repo_root, codex_home, interpreter)
    if args.command == "uninstall":
        return uninstall(repo_root, codex_home, interpreter, dry_run=args.dry_run or not args.apply)
    return install(repo_root, codex_home, interpreter, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
