#!/usr/bin/env python3
"""Manage Codex-only hooks and HTTP MCP configuration with transactional rollback.

Installation never starts or stops services. Explicit ``start`` runs a foreground
API; the installed helper opts into the separately owned on-demand runtime.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    return tuple(dict.fromkeys((*surface.CODEX_HOOK_SCRIPTS, *surface.CODEX_SUPPORT_MODULES, "hook_core.py")))


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


def _installed_manifest(surface: Any, install_dir: Path, interpreter: Path) -> dict[str, list[dict[str, Any]]]:
    rendered = surface.render_manifest()
    windows = os.name == "nt"
    result: dict[str, list[dict[str, Any]]] = {}
    for event, groups in rendered["hooks"].items():
        result[event] = []
        for group in groups:
            item = copy.deepcopy(group)
            for handler in item.get("hooks", []):
                tokens = shlex.split(handler["command"])
                tokens = [token.replace(surface.PLACEHOLDER_PY, str(interpreter))
                          .replace(surface.PLACEHOLDER_HOOKS_DIR, str(install_dir)) for token in tokens]
                handler["command"] = subprocess.list2cmdline(tokens) if windows else shlex.join(tokens)
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
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise RuntimeError(f"无效的 Hook 事件列表: {event}")
        if remove:
            # Only trim tails. Removing an interior slot transfers another
            # command into the previous command's position-based trust key.
            for group in reversed(groups):
                handlers = group.get("hooks", [])
                while handlers and _is_owned({"hooks": [handlers[-1]]}, install_dir, names):
                    handlers.pop()
                if any(_is_owned({"hooks": [handler]}, install_dir, names) for handler in handlers):
                    raise RuntimeError(f"{event}: 无法安全卸载中间 handler；会移动第三方授信槽位。"
                                       "请先在 Codex 管理界面审阅注册")
            while groups and not groups[-1].get("hooks"):
                groups.pop()
            if any(not group.get("hooks") for group in groups):
                raise RuntimeError(f"{event}: 无法安全卸载中间 group；会移动第三方授信槽位。"
                                   "请先在 Codex 管理界面审阅注册")
    if remove:
        result["hooks"] = {event: groups for event, groups in hooks.items() if groups}
    else:
        for event, groups in desired.items():
            current = hooks.setdefault(event, [])
            present = {name for group in current for handler in group.get("hooks", [])
                       if _is_owned({"hooks": [handler]}, install_dir, names)
                       for name in _command_files({"hooks": [handler]})}
            for group in groups:
                addition = copy.deepcopy(group)
                addition["hooks"] = [handler for handler in addition["hooks"]
                                     if not set(_command_files({"hooks": [handler]})) & present]
                if addition["hooks"]:
                    current.append(addition)
    return result, result != document


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
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
        "sha256": {name: _sha256(repo_root / "plugin/harness/codex/hooks" / name) for name in names},
    }


def _published_hashes(repo_root: Path, relative: str) -> set[str]:
    """Recognize byte-identical released files without trusting a missing receipt."""
    hashes = set()
    published = repo_root / "plugin/harness/codex/legacy-installed-hashes.json"
    if published.exists():
        manifest = json.loads(published.read_text(encoding="utf-8"))
        if manifest.get("schema") != 1:
            raise RuntimeError("旧安装摘要清单版本不支持")
        for release in manifest.get("releases", []):
            digest = release.get("sha256", {}).get(relative)
            if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
                hashes.add(digest)
    try:
        refs = subprocess.run(
            ["git", "-C", str(repo_root), "for-each-ref", "--format=%(refname)",
             "refs/tags", "refs/remotes/origin/master"], capture_output=True, text=True,
        )
    except OSError:
        return hashes
    for ref in refs.stdout.splitlines():
        result = subprocess.run(["git", "-C", str(repo_root), "show", f"{ref}:{relative}"],
                                capture_output=True)
        if result.returncode == 0:
            hashes.add(hashlib.sha256(result.stdout).hexdigest())
    return hashes


def _receipt(install_dir: Path) -> dict[str, Any]:
    path = install_dir / META_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _config_text(text: str, values: dict[str, Any], *, remove: set[str] | None = None) -> str:
    """Edit only a standalone OS table and verify the parsed result afterwards."""
    before = tomllib.loads(text)
    expected = copy.deepcopy(before)
    server = expected.setdefault("mcp_servers", {}).setdefault("ai-team-os", {})
    server.update(values)
    for key in remove or ():
        server.pop(key, None)
    match = re.search(r"(?m)^\[mcp_servers\.ai-team-os\][ \t]*(?:#.*)?\r?\n", text)
    if not match:
        if "ai-team-os" in before.get("mcp_servers", {}):
            raise RuntimeError("仅支持独立 [mcp_servers.ai-team-os] 表；其它 TOML 形态需人工核对")
        text = text.rstrip() + "\n\n[mcp_servers.ai-team-os]\n"
        match = re.search(r"(?m)^\[mcp_servers\.ai-team-os\]\n", text)
    following = re.search(r"(?m)^\[", text[match.end():])
    end = match.end() + following.start() if following else len(text)
    block = text[match.end():end]
    for key in remove or ():
        block = re.sub(rf"(?m)^{re.escape(key)}[ \t]*=.*\n?", "", block)
    for key, value in values.items():
        line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
        pattern = rf"(?m)^{re.escape(key)}[ \t]*=.*$"
        if re.search(pattern, block):
            block = re.sub(pattern, lambda _: line, block)
        else:
            block = block.rstrip() + "\n" + line + "\n\n"
    rendered = text[:match.end()] + block + text[end:]
    if not server:
        rendered = text[:match.start()] + text[end:]
        expected["mcp_servers"].pop("ai-team-os")
        if not expected["mcp_servers"] and "mcp_servers" not in tomllib.loads(rendered):
            expected.pop("mcp_servers")
    if tomllib.loads(rendered) != expected:
        raise RuntimeError("MCP 配置变更超出允许字段，未写入")
    return rendered


def _mcp_plan(repo_root: Path, home: Path, interpreter: Path, explicit_url: str | None,
              runtime_dir: Path | None) -> tuple[dict[Path, bytes], dict[str, Any]]:
    if os.name != "posix":
        raise RuntimeError("完整 Codex 按需安装目前仅支持 POSIX；其它平台尚未验收")
    url, server = _connection(home, explicit_url)
    if urlsplit(url).port is None:
        raise RuntimeError("首次配置必须指定本机 API 端口")
    if server.get("url") and _connection(home, server["url"])[0] != url:
        raise RuntimeError("现有 OS MCP URL 与目标不同；未改动已有连接")
    if any(key in server for key in ("command", "args", "startup_timeout_ms")):
        raise RuntimeError("已有 stdio 或旧 startup_timeout_ms 配置需明确迁移，未改动")
    helper = home / "bin/aiteam-http-headers.py"
    runtime_script = repo_root / "scripts/codex_runtime.py"
    if not runtime_script.is_file():
        raise RuntimeError("仓库缺少 Codex runtime")
    runtime = runtime_dir or home / "ai-team-os/runtime"
    command = [str(interpreter), str(helper), "--api-url", url, "--runtime-script",
               str(runtime_script), "--runtime-dir", str(runtime)]
    updates = {"url": url + "/mcp/", "http_headers_helper": shlex.join(command)}
    if "startup_timeout_sec" not in server:
        updates["startup_timeout_sec"] = 45
    config = home / "config.toml"
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    receipt = _receipt(home / "hooks" / OBSERVER_DIRNAME)
    prior_helper = receipt.get("mcp_installed", {}).get("http_headers_helper")
    if prior_helper and server.get("http_headers_helper") != prior_helper:
        raise RuntimeError("检测到用户修改的 MCP helper 命令；保留并停止更新")
    original_fields = receipt.get("mcp_before", {})
    for key in updates:
        if key not in original_fields:
            original_fields[key] = {"present": key in server, "value": server.get(key)}
    content = (repo_root / "src/aiteam/mcp/http_headers.py").read_bytes()
    if helper.exists():
        installed_hash = _sha256(helper)
        known = {receipt.get("helper_sha256"), hashlib.sha256(content).hexdigest()}
        if installed_hash not in known and (
            receipt or installed_hash not in _published_hashes(repo_root, "src/aiteam/mcp/http_headers.py")
        ):
            raise RuntimeError("检测到用户修改或来源未知的 MCP helper；保留并停止更新")
    return {config: _config_text(original, updates).encode(), helper: content}, {
        "mcp_before": original_fields, "mcp_installed": {**receipt.get("mcp_installed", {}), **updates},
        "helper_sha256": hashlib.sha256(content).hexdigest(), "runtime_dir": str(runtime),
    }


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(repo_root: Path, codex_home: Path, interpreter: Path, *, dry_run: bool,
            api_url: str | None = None, runtime_dir: Path | None = None, hooks_only: bool = False) -> int:
    surface = _surface(repo_root)
    names = _owned_names(surface)
    source_dir = repo_root / "plugin" / "harness" / "codex" / "hooks"
    install_dir = codex_home / "hooks" / OBSERVER_DIRNAME
    hooks_path = codex_home / MANIFEST_NAME
    desired = _installed_manifest(surface, install_dir, interpreter)
    document = _load_hooks(hooks_path)
    for groups in document.get("hooks", {}).values():
        for group in groups if isinstance(groups, list) else []:
            for handler in group.get("hooks", []):
                item = {"hooks": [handler]}
                if set(_command_files(item)) & set(names) and not _is_owned(item, install_dir, names):
                    raise RuntimeError("发现旧路径或另一安装目录的同名 Hook；请先在 Codex 管理界面核对，"
                                       "不会自动追加重复注册")
    merged, registration_changed = _merge_hooks(document, desired, install_dir, names)
    missing = [name for name in names if not (source_dir / name).is_file()]
    if missing:
        raise RuntimeError(f"仓库缺少 Codex 文件: {', '.join(missing)}")
    if not interpreter.is_file():
        raise RuntimeError(f"Python 解释器不存在: {interpreter}")
    if hooks_only:
        pending = {}
        receipt = _receipt(install_dir)
        mcp_meta = {key: receipt[key] for key in
                    ("mcp_before", "mcp_installed", "helper_sha256", "runtime_dir") if key in receipt}
    else:
        pending, mcp_meta = _mcp_plan(repo_root, codex_home, interpreter, api_url, runtime_dir)
    print(f"目标安装目录: {install_dir}")
    print(f"目标注册文件: {hooks_path}")
    print(f"将同步文件: {', '.join(names)}")
    print("注册声明变化：需要重新授信" if registration_changed else "注册声明未变化：无需重新授信")
    targets = [install_dir / name for name in names] + [install_dir / META_NAME, hooks_path, *pending]
    if any(parent.is_symlink() for path in targets for parent in (path, *path.parents)):
        raise RuntimeError("拒绝写入符号链接安装面；请使用独立 Codex 目录")
    previous = _installed_hashes(install_dir, source_dir, names)
    for name in names:
        path = install_dir / name
        if path.exists():
            installed_hash = _sha256(path)
            known = {previous.get(name), _sha256(source_dir / name)}
            if installed_hash not in known and (
                (install_dir / META_NAME).exists()
                or installed_hash not in _published_hashes(repo_root, f"plugin/harness/codex/hooks/{name}")
            ):
                raise RuntimeError(f"检测到用户修改或来源未知的文件，保留并停止更新: {path}")
    if dry_run:
        return 0
    saved = {path: path.read_bytes() if path.exists() else None for path in targets}
    backup = _backup(hooks_path)
    config_backup = _backup(codex_home / "config.toml") if not hooks_only else None
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy2(source_dir / name, install_dir / name)
        for path, content in pending.items():
            if not path.exists() or path.read_bytes() != content:
                _write_bytes(path, content)
        metadata = _metadata(repo_root, install_dir, interpreter, names)
        metadata.update(mcp_meta)
        _atomic_json(install_dir / META_NAME, metadata)
        if merged != document or not hooks_path.exists():
            _atomic_json(hooks_path, merged)
    except BaseException:
        for path, content in saved.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    scope = "Hook 副本与注册" if hooks_only else "适配器与 MCP"
    print(f"已更新 Codex {scope}（hooks.json 备份: {backup or '无'}；config.toml 备份: {config_backup or '无'}）")
    print("Hook 副本在下次调用生效；运行中的 API 不会热更新。需更新 API 时先显式 stop 所属 runtime，再重连。")
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
    broken = []
    for groups in document.get("hooks", {}).values():
        for group in groups if isinstance(groups, list) else []:
            for handler in group.get("hooks", []):
                if not set(_command_files({"hooks": [handler]})) & set(names):
                    continue
                for index, token in enumerate(shlex.split(handler["command"])):
                    if index == 0 or token.endswith(".py"):
                        if not Path(token).is_file() and not (index == 0 and shutil.which(token)):
                            broken.append(token)
    for path in broken:
        print(f"Hook 命令路径不存在: {path}")
    registration_ok = current == document and registered and not broken
    print(f"安装目录: {'存在' if install_dir.is_dir() else '缺失'} {install_dir}")
    print(f"文件: {len(installed)}/{len(names)} 同步, 漂移 {len(stale)}, 缺失 {len(missing)}")
    print(f"注册: {'已登记（保留定制声明）' if registration_ok else '需要安装或更新'}")
    return 0 if not stale and not missing and registration_ok else 1


def _installed_hashes(install_dir: Path, source_dir: Path, names: tuple[str, ...]) -> dict[str, str]:
    metadata = install_dir / META_NAME
    if metadata.exists():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        if "sha256" in data:
            return data["sha256"]
    return {name: _sha256(source_dir / name) for name in names}


def uninstall(repo_root: Path, codex_home: Path, interpreter: Path, *, dry_run: bool) -> int:
    surface = _surface(repo_root)
    names = _owned_names(surface)
    install_dir = codex_home / "hooks" / OBSERVER_DIRNAME
    hooks_path = codex_home / MANIFEST_NAME
    if any(path.is_symlink() for path in (codex_home, codex_home / "hooks", install_dir, hooks_path)):
        raise RuntimeError("拒绝卸载符号链接安装面")
    document = _load_hooks(hooks_path)
    merged, changed = _merge_hooks(document, {}, install_dir, names, remove=True)
    source_dir = repo_root / "plugin/harness/codex/hooks"
    hashes = _installed_hashes(install_dir, source_dir, names)
    files = []
    for name in names:
        path = install_dir / name
        if path.is_file() and not path.is_symlink() and _sha256(path) == hashes.get(name):
            files.append(path)
        elif path.exists():
            print(f"保留用户修改或来源未知的文件: {path}")
    metadata = install_dir / META_NAME
    receipt = _receipt(install_dir)
    config = codex_home / "config.toml"
    helper = codex_home / "bin/aiteam-http-headers.py"
    config_text = config.read_text(encoding="utf-8") if config.exists() else ""
    rendered = config_text
    before_fields = receipt.get("mcp_before", {})
    installed_fields = receipt.get("mcp_installed", {})
    if installed_fields and config.exists():
        server = tomllib.loads(config_text).get("mcp_servers", {}).get("ai-team-os", {})
        restore, remove = {}, set()
        for key, value in installed_fields.items():
            if server.get(key) != value or key not in before_fields:
                continue
            prior = before_fields[key]
            if prior["present"]:
                restore[key] = prior["value"]
            else:
                remove.add(key)
        # A user-added field in a new server must not be stranded without URL.
        remaining = set(server) - remove
        if "url" in remove and remaining:
            print("保留用户修改的 MCP 表及 helper；避免留下缺少连接地址的配置")
        else:
            rendered = _config_text(config_text, restore, remove=remove)
    if (helper.is_file() and not helper.is_symlink()
            and _sha256(helper) == receipt.get("helper_sha256") and str(helper) not in rendered):
        files.append(helper)
    if any(parent.is_symlink() for path in (config, helper, metadata) for parent in (path, *path.parents)):
        raise RuntimeError("拒绝卸载符号链接安装面")
    if metadata.exists():
        files.append(metadata)
    print(f"将移除注册组: {'是' if changed else '否'}")
    print(f"将移除文件: {', '.join(str(path) for path in files) if files else '无'}")
    if dry_run:
        return 0
    targets = [hooks_path, config, *files]
    saved = {path: path.read_bytes() if path.exists() else None for path in targets}
    try:
        if changed:
            backup = _backup(hooks_path)
            _atomic_json(hooks_path, merged)
            print(f"已更新注册文件（备份: {backup or '无'}）")
        if rendered != config_text:
            _backup(config)
            _write_bytes(config, rendered.encode())
        for path in files:
            path.unlink(missing_ok=True)
    except BaseException:
        for path, content in saved.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    try:
        install_dir.rmdir()
    except OSError:
        pass
    print("已移除 Codex 适配器文件与注册；共享 API、数据库和 Claude 文件未改动")
    return 0


def _connection(codex_home: Path, explicit_url: str | None) -> tuple[str, dict[str, Any]]:
    config = codex_home / "config.toml"
    data = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    mcp = data.get("mcp_servers", {}).get("ai-team-os", {})
    url = explicit_url or os.environ.get("AITEAM_API_URL") or mcp.get("url")
    if not url:
        raise RuntimeError("缺少 HTTP MCP 地址；配置 mcp_servers.ai-team-os.url 或传 --api-url http://127.0.0.1:端口")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/", "/mcp", "/mcp/"}):
        raise RuntimeError("仅支持无凭据的本机 HTTP API/MCP 地址")
    # Validate the port before printing or starting anything.
    port = parsed.port or 80
    if not 1 <= port <= 65535:
        raise RuntimeError("API 端口无效")
    return f"{parsed.scheme}://{parsed.netloc}", mcp


def _ready(url: str) -> tuple[bool, str]:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/api/mcp/http-readiness", timeout=2) as response:
            data = json.load(response)
        if (data.get("status") == "ok" and data.get("transport") == "streamable-http"
                and data.get("project_context") == "connection-cwd-v1"):
            return True, "HTTP MCP 已就绪"
        return False, "服务缺少 HTTP MCP / connection-cwd-v1 支持"
    except (OSError, ValueError) as error:
        return False, f"HTTP MCP 不可达或不兼容: {type(error).__name__}"


def connection_status(codex_home: Path, explicit_url: str | None = None) -> int:
    try:
        url, mcp = _connection(codex_home, explicit_url)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"MCP 配置: {error}")
        return 1
    print(f"MCP API: {url}")
    helper = mcp.get("http_headers_helper")
    missing = False
    if not isinstance(helper, str) or not helper.strip():
        print("MCP helper: 未配置 http_headers_helper")
        missing = True
    else:
        try:
            tokens = shlex.split(helper)
            for index, token in enumerate(tokens):
                if index == 0 or token.endswith(".py"):
                    if not (Path(token).is_file() or (index == 0 and shutil.which(token))):
                        print(f"MCP helper 路径不存在: {token}")
                        missing = True
        except ValueError:
            print("MCP helper: 命令引用不完整")
            missing = True
    ready, detail = _ready(url)
    print(detail)
    receipt = _receipt(codex_home / "hooks" / OBSERVER_DIRNAME)
    runtime_dir = receipt.get("runtime_dir")
    if runtime_dir and Path(runtime_dir).is_dir():
        runtime_script = Path(receipt.get("repo_root", ROOT)) / "scripts/codex_runtime.py"
        result = subprocess.run([str(receipt.get("python", sys.executable)), str(runtime_script),
                                 "status", "--api-url", url, "--runtime-dir", runtime_dir],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            runtime = json.loads(result.stdout)
            if runtime.get("restart_required") is True:
                print("运行中的 API 源码已变化：需要显式 stop 所属 runtime 后重连")
                missing = True
            elif runtime.get("api_ready") and runtime.get("restart_required") is None:
                print("运行中的 API 源码版本未知：不能据此认定更新已生效")
    return 0 if ready and not missing else 1


def start(repo_root: Path, codex_home: Path, interpreter: Path, explicit_url: str | None,
          *, dry_run: bool = False) -> int:
    url, _ = _connection(codex_home, explicit_url)
    ready, detail = _ready(url)
    if ready:
        print(f"复用已运行的服务: {url}")
        return 0
    parsed = urlsplit(url)
    host, port = parsed.hostname, parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=1):
            raise RuntimeError(f"{detail}；端口已被占用，未启动新服务且未停止现有进程: {url}")
    except ConnectionRefusedError:
        pass
    except (TimeoutError, socket.gaierror) as error:
        raise RuntimeError(f"无法确认端口空闲，未启动服务: {url}") from error
    if not interpreter.is_file():
        raise RuntimeError(f"Python 解释器不存在: {interpreter}")
    if not (repo_root / "src/aiteam/api/app.py").is_file():
        raise RuntimeError(f"源码路径不存在或不完整: {repo_root}")
    command = [str(interpreter), "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
               "--host", host, "--port", str(port)]
    print(f"前台启动服务 {url}，Ctrl-C 关闭；另开终端重连 Codex。", flush=True)
    if dry_run:
        print(shlex.join(command))
        return 0
    env = dict(os.environ, PYTHONPATH=str(repo_root / "src"), AITEAM_API_URL=url)
    if os.name != "posix":
        raise RuntimeError("start 的并发安全启动目前仅在 POSIX 支持；其它平台请按 HTTP MCP 文档显式启动服务")
    # Reserve the listening socket before importing/starting the application.
    # Uvicorn otherwise runs lifespan (including monitors) *before* bind, so a
    # preflight TCP check alone cannot prevent two competing lifecycle starts.
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        # Rebind after an orderly shutdown while accepted sockets are in
        # TIME_WAIT. This does not enable SO_REUSEPORT or concurrent listeners.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(socket.SOMAXCONN)
        except OSError as error:
            raise RuntimeError(f"无法独占启动端口（可能正在由另一会话启动），未启动 API: {url}") from error
        # The shared app deliberately tolerates unavailable MCP dependencies;
        # this MCP-specific entry point must not leave a REST-only API alive.
        probe = subprocess.run([str(interpreter), "-c",
                                "from aiteam.mcp.server import mcp; "
                                "mcp.http_app(transport='streamable-http', path='/')"],
                               cwd=repo_root, env=env, capture_output=True, text=True, timeout=15)
        if probe.returncode:
            print(f"HTTP MCP 依赖/配置预检失败（退出码 {probe.returncode}），未启动 API。"
                  "请检查所选系统 Python 的项目依赖和 FastMCP 环境配置。", file=sys.stderr)
            return 1
        command = [str(interpreter), "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
                   "--fd", str(listener.fileno())]
        return subprocess.call(command, cwd=repo_root, env=env, pass_fds=(listener.fileno(),))



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Team OS Codex 适配器生命周期管理")
    parser.add_argument("command", choices=("install", "update", "status", "uninstall", "start"))
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or DEFAULT_CODEX_HOME))
    parser.add_argument("--python", dest="interpreter", type=Path, default=Path(sys.executable))
    parser.add_argument("--api-url", help="本机 HTTP API 根地址；否则读环境或 Codex MCP 配置")
    parser.add_argument("--runtime-dir", type=Path, help="按需 API 运行记录目录，默认在所选 Codex home 内")
    parser.add_argument("--hooks-only", action="store_true",
                        help="install/update/status 仅处理 Hook，保留现有 stdio/MCP")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不写入")
    parser.add_argument("--apply", action="store_true", help="确认执行卸载；install/update 不需要")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.expanduser().resolve()
    codex_home = args.codex_home.expanduser().absolute()
    # Keep the interpreter spelling the user/installer supplied. Resolving a
    # Homebrew symlink changes the command string and falsely looks like a
    # registration change, even though it is the same executable.
    interpreter = args.interpreter.expanduser().absolute()
    if args.command == "start":
        return start(repo_root, codex_home, interpreter, args.api_url, dry_run=args.dry_run)
    if args.command == "status":
        result = status(repo_root, codex_home, interpreter)
        return result if args.hooks_only else max(result, connection_status(codex_home, args.api_url))
    if args.command == "uninstall":
        return uninstall(repo_root, codex_home, interpreter, dry_run=args.dry_run or not args.apply)
    return install(repo_root, codex_home, interpreter, dry_run=args.dry_run,
                   api_url=args.api_url, runtime_dir=args.runtime_dir, hooks_only=args.hooks_only)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Codex 适配器: {error}", file=sys.stderr)
        raise SystemExit(1)
