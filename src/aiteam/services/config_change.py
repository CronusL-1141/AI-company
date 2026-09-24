"""Conversation-authorised writes to the user's configuration (docs/user-notice-design.md §5.9).

When a user says an action phrase ("sync OS install"), the model does not edit
files itself. It calls ``os_config_change`` twice:

1. **Preview** (no token): the exact list of files, the action on each and
   their sha256 before and after, the baseline (tree, branch, version) and a
   ``confirm_token``. The token is an HMAC over the preview under a random key
   held by this process, valid for 10 minutes.
2. **Apply** (token + the user's own words): the preview is computed again; if
   anything changed the token no longer matches and the write is refused.
   Otherwise every existing file is backed up next to itself
   (``<file>.bak-aiteam-<UTC time>``) and then replaced.

The caller records a ``decision.user_config_write`` event with the data
:func:`apply` returns (through the API, or the local record file when the API
is down; both import by the same record uuid).

Changes are registered in :data:`CHANGES`. ``update_codex_adapter`` has a
reserved slot (:data:`RESERVED`) that the Codex batch fills.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

TOKEN_TTL_S = 600
MAX_QUOTE_CHARS = 1000
_KEY = secrets.token_bytes(32)


class ConfigChangeError(Exception):
    """A preview or apply that cannot go ahead; the message says why.

    ``partial`` is set when an apply failed after it started writing: the event
    data of what was done (``status`` partial, the targets already written with
    their backups, the target that failed). The caller records it like a
    completed write, because the user's files did change.
    """

    def __init__(self, message: str, *, partial: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.partial = partial


@dataclass(frozen=True)
class Target:
    """One file a change writes."""

    path: str
    action: str  # "write" (replace existing) / "create"
    before_sha256: str
    after_sha256: str
    summary: str
    source: str = ""


@dataclass(frozen=True)
class Plan:
    """Everything a preview shows, and everything the token covers."""

    change: str
    targets: tuple[Target, ...]
    baseline: dict[str, str] = field(default_factory=dict)
    notice_key: str = ""
    summary: str = ""
    warnings: tuple[str, ...] = ()
    # The executor's own preview, handed back to ``apply_plan`` (for example the
    # Codex adapter's preview dict, which it compares again before writing).
    payload: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> bytes:
        body = {
            "change": self.change,
            "targets": [asdict(target) for target in self.targets],
            "baseline": self.baseline,
            "notice_key": self.notice_key,
            "payload": self.payload,
        }
        return json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class ChangeSpec:
    """A registered change: how to plan it, and how to write it.

    Either ``write`` (this module backs up and writes one target at a time) or
    ``apply_plan`` (the executor owns its whole write set and the backups, and
    returns ``{"targets": [{path, action, before_sha256, after_sha256, backup}], ...}``).
    Token, user quote and the consent event stay here either way. ``plan``,
    ``write`` and ``apply_plan`` may block (files, subprocesses): the MCP tool
    runs them in a worker thread.
    """

    name: str
    description: str
    plan: Callable[[], Plan]
    write: Callable[[Target], None] | None = None
    apply_plan: Callable[[Plan], dict[str, Any]] | None = None


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# sync_installed_copies (E11)
# ---------------------------------------------------------------------------


def _plan_sync_installed_copies() -> Plan:
    from aiteam.services.notices import install_kind
    from aiteam.services.notices.detectors import installed_copies

    if not (install_kind.os_data_dir() / "install_path.txt").exists():
        raise ConfigChangeError(
            "No source install is recorded (install_path.txt missing): plugin installs "
            "re-sync their hook copies on session start, nothing to do here."
        )
    diff = installed_copies.compare_sync()
    if diff is None:
        raise ConfigChangeError("The source tree to compare against was not found.")
    targets = []
    edited = []
    for item in diff.stale:
        # The installers copy with the source's mtime, so a copy newer than its
        # source was changed on this machine after it was installed.
        local = not item.missing and _newer(item.installed, item.source)
        if local:
            edited.append(item.relative)
        verb = "create" if item.missing else "replace locally modified" if local else "replace"
        targets.append(Target(
            path=str(item.installed),
            action="create" if item.missing else "write",
            before_sha256="" if item.missing else sha256_file(item.installed),
            after_sha256=sha256_file(item.source),
            summary=f"{verb} {item.relative} from {item.source}",
            source=str(item.source),
        ))
    baseline = diff.baseline
    warnings = []
    if edited:
        warnings.append(
            f"{len(edited)} of these copies were modified on this machine after the source file "
            f"({', '.join(edited[:5])}{', ...' if len(edited) > 5 else ''}); applying replaces the "
            "user's edits (a backup is kept next to each file). Tell the user before applying."
        )
    if baseline.branch and baseline.branch != "master":
        warnings.append(
            f"The baseline tree is on branch {baseline.branch}, not master: these copies would "
            "come from that branch. Confirm with the user before applying."
        )
    hooks = [target for target in targets if "/hooks/ai-team-os/" in target.path.replace("\\", "/")]
    effects = []
    if hooks:
        effects.append("hooks take effect on their next run")
    if len(hooks) < len(targets):
        effects.append("skills, agents and commands need a Claude Code restart")
    restart = "; ".join(effects)
    return Plan(
        change="sync_installed_copies",
        targets=tuple(targets),
        baseline={"root": str(baseline.root), "branch": baseline.branch, "version": baseline.version},
        notice_key=diff.key if targets else "",
        summary=(f"Replace {len(targets)} installed copies with the files from {baseline.root}. "
                 "No file is deleted." + (f" {restart[0].upper()}{restart[1:]}." if restart else "")),
        warnings=tuple(warnings),
    )


def _newer(installed: Path, source: Path) -> bool:
    try:
        return installed.stat().st_mtime > source.stat().st_mtime + 1
    except OSError:
        return False


def _write_copy(target: Target) -> None:
    destination = Path(target.path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".aiteam-sync-", dir=destination.parent)
    os.close(handle)
    try:
        shutil.copy2(target.source, temporary)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


CHANGES: dict[str, ChangeSpec] = {
    "sync_installed_copies": ChangeSpec(
        name="sync_installed_copies",
        description="Bring hook, skill, agent and command copies of a source install back in line "
                    "with the source tree (E11).",
        plan=_plan_sync_installed_copies,
        write=_write_copy,
    ),
}

# Changes with an agreed name whose implementation lands in a later batch.
# update_codex_adapter: the Codex batch registers it with ``apply_plan`` over
# scripts/codex_adapter.py (preview_update for the plan, apply_update with the
# original preview as expected_preview), keeping the adapter's preview dict in
# ``Plan.payload``.
RESERVED: dict[str, str] = {
    "update_codex_adapter": "the Codex batch (it runs codex_adapter.py update with --dry-run for the preview)",
}


def register_change(spec: ChangeSpec) -> None:
    """Add a change (the Codex batch registers update_codex_adapter here)."""
    CHANGES[spec.name] = spec
    RESERVED.pop(spec.name, None)


def _spec(change: str) -> ChangeSpec:
    if change in CHANGES:
        return CHANGES[change]
    if change in RESERVED:
        raise ConfigChangeError(f"{change} is not available yet; it arrives with {RESERVED[change]}.")
    known = ", ".join(sorted(CHANGES))
    raise ConfigChangeError(f"Unknown change {change!r}; known changes: {known}.")


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def _sign(plan: Plan, issued: int) -> str:
    digest = hmac.new(_KEY, str(issued).encode() + b"|" + plan.canonical(), hashlib.sha256).hexdigest()
    return f"{issued}.{digest[:32]}"


def _verify(plan: Plan, token: str, now: float) -> None:
    issued_text, _, _signature = token.partition(".")
    try:
        issued = int(issued_text)
    except ValueError:
        raise ConfigChangeError("The confirm_token is malformed; run the preview again.") from None
    if not 0 <= now - issued <= TOKEN_TTL_S:
        raise ConfigChangeError("The confirm_token has expired (10 minutes); run the preview again.")
    if not hmac.compare_digest(_sign(plan, issued), token):
        raise ConfigChangeError(
            "The files changed since the preview (or the token belongs to another preview or "
            "process); run the preview again and show it to the user."
        )


# ---------------------------------------------------------------------------
# Preview and apply
# ---------------------------------------------------------------------------


def preview(change: str, *, now: float | None = None) -> dict[str, Any]:
    """Blocking preview: the plan plus a ``confirm_token`` (empty when nothing to do)."""
    plan = _spec(change).plan()
    issued = int(now if now is not None else time.time())
    return {
        "change": plan.change,
        "mode": "preview",
        "summary": plan.summary,
        "baseline": plan.baseline,
        "warnings": list(plan.warnings),
        "notice_key": plan.notice_key,
        "targets": [asdict(target) for target in plan.targets],
        "confirm_token": _sign(plan, issued) if plan.targets else "",
        "expires_in_s": TOKEN_TTL_S if plan.targets else 0,
        "nothing_to_do": not plan.targets,
    }


def backup_suffix(now: float) -> str:
    return ".bak-aiteam-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def apply(change: str, confirm_token: str, user_quote: str, *, now: float | None = None) -> dict[str, Any]:
    """Blocking apply: verify the token against a fresh preview, back up, write.

    Returns the data of the ``decision.user_config_write`` event (without host,
    session or tool, which the caller adds).
    """
    quote = (user_quote or "").strip()
    if not quote:
        raise ConfigChangeError("user_quote is required: pass the user's own words that approved the preview.")
    if not confirm_token:
        raise ConfigChangeError("confirm_token is required: run the preview first.")
    moment = now if now is not None else time.time()
    spec = _spec(change)
    plan = spec.plan()
    if not plan.targets:
        raise ConfigChangeError("Nothing to change any more; the preview is out of date.")
    _verify(plan, confirm_token, moment)
    suffix = backup_suffix(moment)
    base = {"change": plan.change, "notice_key": plan.notice_key, "baseline": plan.baseline,
            "user_quote": quote[:MAX_QUOTE_CHARS]}
    if spec.apply_plan is not None:
        try:
            result = spec.apply_plan(plan)
        except ConfigChangeError:
            raise
        except Exception as exc:
            message = f"{change}: the executor failed: {type(exc).__name__}: {exc}"
            raise ConfigChangeError(message, partial={
                **base, "targets": [], "status": "failed", "error": message[:300],
            }) from exc
        if not isinstance(result, dict) or not isinstance(result.get("targets"), list):
            raise ConfigChangeError(f"{change}: the executor returned no targets list.")
        return {**result, **base}
    if spec.write is None:
        raise ConfigChangeError(f"{change} has neither write nor apply_plan.")
    written: list[dict[str, Any]] = []
    for target in plan.targets:
        path = Path(target.path)
        backup = ""
        try:
            if path.exists():
                backup = str(path) + suffix
                shutil.copy2(path, backup)
            spec.write(target)
            after = sha256_file(path)
            if after != target.after_sha256:
                raise OSError(f"{path} does not match the previewed content after writing")
        except OSError as exc:
            message = f"write failed at {path}: {exc}"
            raise ConfigChangeError(message, partial={
                **base, "targets": written, "backup_suffix": suffix,
                "status": "partial" if written else "failed", "error": message[:300],
                "failed_target": {"path": target.path, "action": target.action, "backup": backup},
            }) from exc
        written.append({
            "path": target.path, "action": target.action,
            "before_sha256": target.before_sha256, "after_sha256": after, "backup": backup,
        })
    return {**base, "targets": written, "backup_suffix": suffix}


def compact_for_local_record(data: dict[str, Any], limit: int = 900) -> dict[str, Any]:
    """Shrink event data to fit one local record line (the full list goes to the API when up)."""
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= limit:
        return data
    targets = data.get("targets") or []
    digest = hashlib.sha256(json.dumps(targets, sort_keys=True).encode("utf-8")).hexdigest()
    slim = {key: value for key, value in data.items() if key not in ("targets", "baseline", "failed_target")}
    slim.update(target_count=len(targets), targets_sha256=digest,
                baseline_root=str((data.get("baseline") or {}).get("root", ""))[:200])
    failed = data.get("failed_target")
    if isinstance(failed, dict):
        slim["failed_path"] = str(failed.get("path", ""))[-160:]
    for key, value in list(slim.items()):
        if isinstance(value, str):
            slim[key] = value[:160]
    slim["user_quote"] = str(data.get("user_quote", ""))[:120]
    return slim
