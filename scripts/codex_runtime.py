#!/usr/bin/env python3
"""Explicit on-demand OS runtime, independent of the invoking Codex terminal."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import psutil

ROOT = Path(__file__).resolve().parent.parent


def source_fingerprint(root: Path = ROOT) -> str:
    """Identify the Python source snapshot; never inspect credentials or DB data."""
    source = root / "src" / "aiteam"
    if not (source / "api" / "app.py").is_file():
        raise ValueError("OS 源码路径缺失，无法核对运行版本")
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*.py")):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_status(runtime: Path, url: str) -> dict:
    process = owned(runtime / "api.json", strict=False)
    changed = None
    if process:
        record = json.loads((runtime / "api.json").read_text())
        previous = record.get("source_fingerprint")
        if previous:
            changed = previous != source_fingerprint()
    return {"api_ready": ready(url), "api_owned": bool(process),
            "source_changed": changed, "restart_required": changed}


def local_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or not parsed.port):
        raise ValueError("API 地址必须为显式端口的本机 HTTP 根地址")
    return value.rstrip("/")


def directory(path: Path, *, create: bool = False) -> Path:
    if ".." in path.parts:
        raise ValueError("目录不允许包含上级路径 ..")
    path = path.absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("运行目录不允许符号链接")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("运行目录不存在")
    for name in ("runtime.lock", "api.json", "api.log"):
        if (path / name).is_symlink():
            raise ValueError(f"拒绝符号链接: {name}")
    return path


@contextlib.contextmanager
def lock(path: Path, deadline: float):
    import fcntl

    with (path / "runtime.lock").open("a") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("启动锁等待超时") from None
                time.sleep(.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save(path: Path, data: dict) -> None:
    write_bytes(path, json.dumps(data).encode())


def write_bytes(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def ready(url: str) -> bool:
    try:
        handlers = [urllib.request.ProxyHandler({})]
        opener = urllib.request.build_opener(*handlers)
        with opener.open(url + "/api/mcp/http-readiness", timeout=1) as response:
            data = json.load(response)
        return (data.get("status") == "ok" and data.get("transport") == "streamable-http"
                and data.get("project_context") == "connection-cwd-v1")
    except (OSError, ValueError):
        return False


def identity(pid: int, target: str) -> dict:
    process = psutil.Process(pid)
    return {"pid": pid, "created": process.create_time(), "command": process.cmdline(),
            "uid": process.uids().real, "target": target}


def owned(path: Path, *, strict: bool = True) -> psutil.Process | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    try:
        process = psutil.Process(data["pid"])
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        if (process.pid == os.getpid() or process.uids().real != os.getuid()
                or process.uids().real != data["uid"]
                or process.create_time() != data["created"]
                or process.cmdline() not in [data["command"], data.get("launch_command")]):
            if not strict:
                return None  # Stale PID reuse cannot authorize signals or block a fresh bind.
            raise RuntimeError(f"进程身份不匹配，拒绝接管或停止: {path.name}")
        return process
    except psutil.NoSuchProcess:
        return None


def stop_owned(path: Path) -> None:
    process = owned(path)
    if process:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except psutil.TimeoutExpired:
            raise RuntimeError(f"所属进程未正常退出，未强杀: {process.pid}") from None
    path.unlink(missing_ok=True)


def wait_ready(process: subprocess.Popen, url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"启动进程退出，code={process.returncode}")
        if ready(url):
            return
        time.sleep(.05)
    raise RuntimeError("服务启动超过预算")


def spawn(command: list[str], env: dict, runtime: Path, kind: str, target: str,
          deadline: float, *, pass_fds: tuple[int, ...] = ()) -> None:
    if time.monotonic() >= deadline:
        raise RuntimeError("启动预算已耗尽，未创建新进程")
    record = runtime / f"{kind}.json"
    # The final PID registers itself before exec. Killing the invoking helper
    # cannot strand a live service between Popen and parent-side bookkeeping.
    launch = [sys.executable, str(Path(__file__).resolve()), "_child", str(record), target,
              json.dumps(command), json.dumps(pass_fds)]
    with (runtime / f"{kind}.log").open("ab") as log:
        process = subprocess.Popen(launch, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True,
                                   close_fds=True, pass_fds=pass_fds)
    try:
        wait_ready(process, target, deadline)
    except Exception:
        # Only this exact Popen child may be cleaned up; never a port occupant.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"启动失败且所属进程未退出；检查 {record}") from None
        record.unlink(missing_ok=True)
        raise


def child_main() -> int:
    record, target = Path(sys.argv[2]), sys.argv[3]
    command, descriptors = json.loads(sys.argv[4]), json.loads(sys.argv[5])
    data = identity(os.getpid(), target)
    data["source_fingerprint"] = source_fingerprint()
    data["launch_command"] = data["command"]
    data["command"] = list(command)
    if command[0] == sys.executable:
        # macOS framework Python rewrites argv[0] to its app executable.
        data["command"][0] = data["launch_command"][0]
    save(record, data)
    for descriptor in descriptors:
        os.set_inheritable(descriptor, True)
    os.execve(command[0], command, os.environ)
    return 0


def ensure_api(runtime: Path, url: str, deadline: float) -> str:
    record = runtime / "api.json"
    if ready(url):
        return "owned" if owned(record, strict=False) else "reused"
    process = owned(record, strict=False)
    if process:
        # The prior helper may have been interrupted after recording the PID.
        while time.monotonic() < deadline and process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
            if ready(url):
                return "owned"
            time.sleep(.05)
        if owned(record, strict=False):
            raise RuntimeError("已登记的 API 仍在运行但未就绪；保留实例，请检查 api.log")
    if not (ROOT / "src/aiteam/api/app.py").is_file():
        raise ValueError("OS 源码路径缺失，请更新 Codex runtime 安装路径")
    parsed = urlsplit(url)
    family = socket.AF_INET6 if parsed.hostname == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((parsed.hostname, parsed.port))
            listener.listen(socket.SOMAXCONN)
        except OSError as error:
            raise RuntimeError("API 端口已被占用且未通过就绪检查；未停止现有进程") from error
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), AITEAM_API_URL=url,
                   FASTMCP_CHECK_FOR_UPDATES="off")
        command = [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
                   "--fd", str(listener.fileno()), "--log-level", "warning", "--no-access-log",
                   "--timeout-graceful-shutdown", "5"]
        spawn(command, env, runtime, "api", url, deadline, pass_fds=(listener.fileno(),))
    return "started"


def configure(home: Path, configs: list[Path], runtime: Path, url: str) -> dict:
    """Opt in selected existing Codex configurations, preserving unrelated settings."""
    home = directory(home)
    helper = home / "bin/aiteam-http-headers.py"
    directory(helper.parent, create=True)
    if helper.is_symlink():
        raise ValueError("拒绝覆盖链接的 helper")
    command = [sys.executable, str(helper), "--api-url", url, "--runtime-script",
               str(Path(__file__).resolve()), "--runtime-dir", str(runtime)]
    updates = {"http_headers_helper": shlex.join(command)}
    pending = {helper: (ROOT / "src/aiteam/mcp/http_headers.py").read_bytes()}
    for path in configs or [home / "config.toml"]:
        if ".." in path.parts:
            raise ValueError("配置路径不允许包含上级路径 ..")
        path = path.absolute()
        if not path.is_relative_to(home) or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError("仅修改所选 Codex home 内非链接的配置")
        text = path.read_text()
        before = tomllib.loads(text)
        server = before.get("mcp_servers", {}).get("ai-team-os", {})
        if local_url(server.get("url", "").rstrip("/").removesuffix("/mcp")) != url:
            raise ValueError(f"MCP URL 与目标 API 不符，未改动: {path}")
        if "startup_timeout_ms" in server:
            raise ValueError("请先明确迁移旧 startup_timeout_ms，避免产生两个启动预算")
        values = dict(updates, startup_timeout_sec=max(45, server.get("startup_timeout_sec", 0)))
        match = re.search(r"(?m)^\[mcp_servers\.ai-team-os\][ \t]*(?:#.*)?\r?\n", text)
        if not match:
            raise ValueError("仅支持独立 [mcp_servers.ai-team-os] 表；其它 TOML 形态需人工核对")
        end_match = re.search(r"(?m)^\[", text[match.end():])
        end = match.end() + end_match.start() if end_match else len(text)
        block = text[match.end():end]
        for key, value in values.items():
            line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
            pattern = rf"(?m)^{key}[ \t]*=.*$"
            if re.search(pattern, block):
                block = re.sub(pattern, lambda _: line, block)
            else:
                block = block.rstrip() + "\n" + line + "\n\n"
        rendered = text[:match.end()] + block + text[end:]
        expected = copy.deepcopy(before)
        expected["mcp_servers"]["ai-team-os"].update(values)
        if tomllib.loads(rendered) != expected:
            raise ValueError("配置变更超出允许字段，已中止")
        pending[path] = rendered.encode()
    backup = directory(home / "backups" / f"os-runtime-{time.time_ns()}", create=True)
    originals = {path: path.read_bytes() if path.exists() else None for path in pending}
    for path, content in originals.items():
        if content is not None:
            target = backup / path.relative_to(home)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_bytes(target, content)
    try:
        for path, content in pending.items():
            write_bytes(path, content)
    except BaseException:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                write_bytes(path, content)
        raise
    return {"backup": str(backup), "configs": len(pending) - 1, "helper": str(helper)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["configure", "ensure", "status", "stop"])
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--config-file", type=Path, action="append", default=[])
    parser.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    try:
        if os.name != "posix":
            raise ValueError("本运行模式暂只支持 POSIX")
        if not 1 <= args.timeout <= 60:
            raise ValueError("timeout 必须为 1..60 秒")
        url = local_url(args.api_url)
        runtime = directory(args.runtime_dir, create=args.command in {"ensure", "configure"})
        deadline = time.monotonic() + args.timeout
        with lock(runtime, deadline):
            api_record = runtime / "api.json"
            if api_record.exists() and json.loads(api_record.read_text()).get("target") != url:
                raise ValueError("运行目录已绑定不同 API 地址，拒绝改用或停止")
            if args.command == "configure":
                result = configure(args.codex_home, args.config_file, runtime, url)
            elif args.command == "stop":
                stop_owned(runtime / "api.json")
                result = {"status": "stopped", "scope": "owned-processes-only"}
            elif args.command == "status":
                result = runtime_status(runtime, url)
            else:
                result = {"api": ensure_api(runtime, url, deadline)}
            print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, KeyError, psutil.Error) as error:
        print(f"OS runtime: {error}；日志目录: {args.runtime_dir}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(child_main() if len(sys.argv) > 1 and sys.argv[1] == "_child" else main())
