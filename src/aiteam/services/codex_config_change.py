"""Run receipt-selected adapters outside the MCP interpreter and its HMAC key.

Children receive an environment allowlist and a temporary working directory.
This isolates Python memory, not same-user filesystem privileges. Callbacks
block and must remain behind os_config_change's worker thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiteam.services.notices.detectors import NoDataError
from aiteam.services.notices.detectors.codex_copies import OBSERVER_DIRNAME, RECEIPT_NAME, codex_home, read_receipt

if TYPE_CHECKING:
    from aiteam.services.config_change import Plan

_CLI_TIMEOUT_S = 30
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"[a-f0-9]{64}")
_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR")


def _environment(home: Path) -> dict[str, str]:
    # Exclude credentials, session IDs, PYTHONPATH/startup hooks, and ambient
    # MCP listener options. The receipt/config owns adapter options.
    env = {name: os.environ[name] for name in _ENV_NAMES if name in os.environ}
    env.update(HOME=str(Path.home()), CODEX_HOME=str(home), PATH=env.get("PATH", os.defpath),
               PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


def _run_process(command: list[str], home: Path, *, payload: bytes | None = None,
                 timeout: float = _CLI_TIMEOUT_S) -> bytes:
    # Disk-backed output bounds what gets loaded into MCP even for a noisy
    # script. Relative writes go to a temporary directory, never MCP's cwd.
    with tempfile.TemporaryDirectory(prefix="aiteam-codex-change-") as directory:
        env = _environment(home)
        env["TMPDIR"] = directory
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                stdin = {"input": payload} if payload is not None else {"stdin": subprocess.DEVNULL}
                result = subprocess.run(command, **stdin, stdout=stdout, stderr=stderr, close_fds=True,
                                        cwd=directory, env=env, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Codex adapter command timed out after {timeout:g}s") from exc
            stdout.seek(0)
            output = stdout.read(_MAX_OUTPUT_BYTES + 1)
            if result.returncode:
                stderr.seek(0)
                detail = stderr.read(2048).decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Codex adapter command exited {result.returncode}: {detail}")
            if len(output) > _MAX_OUTPUT_BYTES:
                raise ValueError("Codex adapter output exceeds the JSON size limit")
            return output


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _installation() -> tuple[Path, Path, Path, dict[str, str]]:
    from aiteam.services.config_change import ConfigChangeError

    try:
        home = codex_home()
        receipt = read_receipt(home)
    except NoDataError as exc:
        raise ConfigChangeError("No valid Codex install receipt; check the service's CODEX_HOME.") from exc
    try:
        recorded = receipt.get("repo_root")
        if not isinstance(recorded, str) or not Path(recorded).is_absolute():
            raise ValueError("The Codex install receipt has no absolute repo_root")
        root = Path(recorded).resolve(strict=True)
        git = shutil.which("git", path=_environment(home)["PATH"])
        if not git:
            raise ValueError("Git is required to verify the recorded adapter source")
        top = _run_process([git, "-C", str(root), "rev-parse", "--show-toplevel"], home, timeout=5)
        if Path(top.decode("utf-8").strip()).resolve(strict=True) != root:
            raise ValueError("The recorded adapter source is not its Git top-level directory")
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        metadata = project.get("project")
        if not isinstance(metadata, dict) or metadata.get("name") != "ai-team-os":
            raise ValueError("The recorded adapter source is not project ai-team-os")
        python = receipt.get("python") or sys.executable
        if not isinstance(python, str) or not Path(python).is_absolute() or not Path(python).is_file():
            raise ValueError("The Codex install receipt has no usable absolute Python interpreter")
        if "hooks_only" in receipt and type(receipt["hooks_only"]) is not bool:
            raise ValueError("The Codex install receipt has an invalid hooks_only mode")
        identity = {
            "root": str(root), "codex_home": str(home), "python": python,
            "adapter_sha256": _sha256(root / "scripts/codex_adapter.py"),
            "receipt_sha256": _sha256(home / "hooks" / OBSERVER_DIRNAME / RECEIPT_NAME),
        }
        return root, home, Path(python), identity
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConfigChangeError(f"Cannot verify the recorded Codex installation: {exc}") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate adapter JSON field: {key}")
        result[key] = value
    return result


def _absolute(value: object) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("Adapter JSON contains a non-absolute or traversing path")
    return Path(value)


def _validate(document: Any, root: Path, home: Path, python: Path, *, applied: bool) -> dict[str, Any]:
    fields = {"schema", "change", "mode", "baseline", "options", "targets", "registration_changed",
              "warnings", "summary", "nothing_to_do"}
    if (not isinstance(document, dict) or set(document) != fields or type(document["schema"]) is not int
            or document["schema"] != 1 or document["change"] != "update_codex_adapter"
            or document["mode"] != ("applied" if applied else "preview")):
        raise ValueError("Invalid Codex adapter JSON envelope")
    baseline, options = document["baseline"], document["options"]
    if (not isinstance(baseline, dict) or set(baseline) != {"root", "branch", "commit", "version", "state_sha256"}
            or not all(isinstance(value, str) for value in baseline.values())
            or baseline["root"] != str(root) or not _SHA256.fullmatch(baseline["state_sha256"])):
        raise ValueError("Invalid Codex adapter baseline")
    if (not isinstance(options, dict) or set(options) != {"python", "api_url", "runtime_dir", "hooks_only"}
            or options["python"] != str(python) or not isinstance(options["api_url"], str)
            or type(options["hooks_only"]) is not bool):
        raise ValueError("Invalid Codex adapter options")
    _absolute(options["runtime_dir"])
    if (type(document["registration_changed"]) is not bool or type(document["nothing_to_do"]) is not bool
            or not isinstance(document["summary"], str) or not isinstance(document["warnings"], list)
            or not all(isinstance(warning, str) for warning in document["warnings"])
            or not isinstance(document["targets"], list) or len(document["targets"]) > 512):
        raise ValueError("Invalid Codex adapter result fields")
    seen = set()
    for target in document["targets"]:
        target_fields = {"path", "action", "before_sha256", "after_sha256", "summary", "source"}
        if applied:
            target_fields.add("backup")
        if (not isinstance(target, dict) or set(target) != target_fields
                or not all(isinstance(value, str) for value in target.values())):
            raise ValueError("Invalid Codex adapter target")
        path = _absolute(target["path"])
        if (not path.is_relative_to(home) or path == home or str(path) in seen
                or target["action"] not in {"write", "create"}
                or not _SHA256.fullmatch(target["after_sha256"])
                or (target["action"] == "write" and not _SHA256.fullmatch(target["before_sha256"]))
                or (target["action"] == "create" and target["before_sha256"] != "")):
            raise ValueError("Invalid Codex adapter target path/action/hash")
        seen.add(str(path))
        if target["source"] and not _absolute(target["source"]).is_relative_to(root):
            raise ValueError("Codex adapter source escapes the recorded repository")
        if applied:
            if target["action"] == "write":
                backup = _absolute(target["backup"])
                if (backup.parent != path.parent or not str(backup).startswith(str(path) + ".bak-aiteam-")
                        or _sha256(backup) != target["before_sha256"]):
                    raise ValueError("Codex adapter backup does not match the approved target")
            elif target["backup"]:
                raise ValueError("Created Codex adapter target unexpectedly has a backup")
            if _sha256(path) != target["after_sha256"]:
                raise ValueError("Codex adapter result does not match the written file")
    if document["nothing_to_do"] != (not document["targets"]):
        raise ValueError("Codex adapter nothing_to_do does not match its targets")
    return document


def _invoke(root: Path, home: Path, python: Path, *, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    command = [str(python), "-I", "-B", str(root / "scripts/codex_adapter.py"), "update",
               "--repo-root", str(root), "--codex-home", str(home), "--python", str(python), "--json"]
    payload = None
    if expected is None:
        command.append("--dry-run")
    else:
        options = expected["options"]
        command += ["--expected-preview", "-", "--runtime-dir", options["runtime_dir"],
                    "--hooks-only" if options["hooks_only"] else "--no-hooks-only"]
        if options["api_url"]:
            command += ["--api-url", options["api_url"]]
        payload = json.dumps(expected, ensure_ascii=False).encode("utf-8")
    output = _run_process(command, home, payload=payload, timeout=_CLI_TIMEOUT_S)
    document = json.loads(output.decode("utf-8"), object_pairs_hook=_unique_object)
    result = _validate(document, root, home, python, applied=expected is not None)
    if expected is not None:
        normalized = {**result, "mode": "preview", "targets": [
            {key: value for key, value in target.items() if key != "backup"} for target in result["targets"]]}
        if normalized != expected:
            raise ValueError("Codex adapter applied result differs from the approved preview")
    return result


def plan() -> Plan:
    from aiteam.services.config_change import ConfigChangeError, Plan, Target

    root, home, python, identity = _installation()
    try:
        preview = _invoke(root, home, python)
        if _installation()[3] != identity:
            raise ValueError("The Codex installation changed while previewing; run the preview again")
        options = preview["options"]
        baseline = {**preview["baseline"], **identity,
                    **{name: str(options[name]) for name in ("python", "api_url", "runtime_dir", "hooks_only")}}
        return Plan(
            change="update_codex_adapter", targets=tuple(Target(**target) for target in preview["targets"]),
            # Only a fresh detector pass may clear E13, including retired files.
            baseline=baseline, notice_key="", payload=preview, warnings=tuple(preview["warnings"]),
            summary=preview["summary"] + f" Codex 目录: {home}；模式: "
                    + ("hooks-only。" if options["hooks_only"] else "hooks + MCP。"),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConfigChangeError(f"Cannot preview the recorded Codex adapter: {exc}") from exc


def apply_plan(approved: Plan) -> dict[str, Any]:
    from aiteam.services.config_change import ConfigChangeError

    root, home, python, identity = _installation()
    if any(approved.baseline.get(key) != value for key, value in identity.items()):
        raise ConfigChangeError("The Codex installation changed since the preview; run the preview again.")
    result = _invoke(root, home, python, expected=approved.payload)
    # Execution evidence stays complete online; approval-only fields stay out
    # of the bounded offline record. Never pass token, quote or HMAC to a child.
    return {"targets": result["targets"], "registration_changed": result["registration_changed"]}
