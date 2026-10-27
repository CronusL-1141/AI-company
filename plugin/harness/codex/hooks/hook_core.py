#!/usr/bin/env python3
"""AI Team OS - harness-neutral hook core shared by every harness entry script.

Most blocks below are **verbatim** extractions from ``plugin/hooks/send_event.py``
(the frozen Claude Code entry): the size-guard constants, ``_trim_payload`` and
``_get_api_url`` (``AITEAM_API_URL``, then the port file found through the path
section, then 8000).
``post_event`` is the POST-and-record tail of ``main()`` (its fallback branch,
:274-293) lifted into a function that takes its API URL instead of reading a
module global. ``classify_post_failure`` is a verbatim copy of the one in
``hook_delivery.py``, the CC delivery module; ``post_event_detailed`` returns its
class so a harness can record why a POST failed without parsing stderr.

WHY THE DUPLICATION IS DELIBERATE - DO NOT "DE-DUPLICATE"
--------------------------------------------------------
``send_event.py`` imports only the path section of this module (``api_port_file``,
``cc_config_dir`` and ``DataDirMissing``), so the frozen entry and every other hook
resolve the data and config directories from one definition
(docs/config-dir-compat-design.md). Everything else it keeps as its own verbatim
copy, for two reasons:

1. **The CC entry is frozen.** ``scripts/hook_entry_freeze.json`` pins the sha256
   of ``plugin/hooks/send_event.py``; a machine check fails if it moves, so every
   change to it is a deliberate unfreeze with a recomputed golden. Importing more
   of this module would widen what the frozen file silently depends on; the
   equivalence tests pin the imported names to those three. The freeze is what
   lets the Codex work claim CC is untouched - a claim no diff review can make as
   cheaply.
2. **The Codex adapter needs the same core.** Its own entry script reuses these
   blocks, so they have to live somewhere importable that is not the frozen file.

Equivalence between the two copies is not left to good intentions: the tests in
``tests/unit/hooks/test_hook_core_equivalence.py`` push the same stdin through the
``send_event.py`` subprocess and through this module and require the POST bodies to
be byte-identical, and they assert each block's source text still occurs verbatim
inside ``send_event.py``. Change one side and they go red.

This file is also kept byte-identical across its copies (``plugin/hooks/`` and
``src/aiteam/hooks/``, machine-checked by I1 in ``scripts/check_invariants.sh``),
the same rule that governs every other hook script in this repository.

Three blocks are not extractions. The path section resolves the OS data
directory, the Claude Code config folder and the Codex home; it is the only
place in the repository that spells those locations (I30), and package code
reads it through ``aiteam.paths``. ``InvocationAudit`` is shared by the unread
entries with an explicit host so their state directories stay separate, and
``_sanitize_inline``, the single cleaner every
hook applies to text other people wrote (channel messages, memories, briefings,
task memos) before it goes into a model's context. It lives here so no hook
writes its own; ``scripts/check_invariants.sh`` (I24) fails if a second
definition appears anywhere else.

Not extracted on purpose (harness-specific, stays in each entry script):
``_INERT_TOOLS`` / ``_is_inert`` (the tool set differs per harness),
``_resolve_cc_team_name`` and ``_yield_if_superseded`` (both Claude Code only).

Note: standard library only, no third-party packages - a harness may invoke this
from any Python environment.
"""

import errno
import http.client
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from contextvars import ContextVar, Token
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Paths (docs/config-dir-compat-design.md §2, §5.1)
#
# Three kinds of directory, each with its own owner:
# * the OS data directory: machine-wide, shared by Claude Code and Codex;
# * the Claude Code config folder: per session (CLAUDE_CONFIG_DIR, else the default);
# * the Codex home: per install (CODEX_HOME, else the default).
# These constants are the only spellings of those locations in the repository
# (I30); everything else derives from them. Nothing here creates, moves or caches
# anything: a hook is a short process, and the API resolves the data directory once
# at startup and the config folder per request. Only installers create the data
# directory (the list is in aiteam.paths).
# ---------------------------------------------------------------------------

LEGACY_DATA_DIR = "~/.claude/data/ai-team-os"
DEFAULT_DATA_DIR = "~/.aiteam"
CC_DEFAULT_CONFIG_DIR = "~/.claude"
CC_DEFAULT_GLOBAL_CONFIG = "~/.claude.json"
CODEX_DEFAULT_HOME = "~/.codex"

DATA_DIR_ENV = "AITEAM_DATA_DIR"
DATA_DIR_MODES = ("env", "canonical", "legacy", "conflict", "missing")
DB_NAME = "aiteam.db"
# A migrated legacy directory left as a plain directory (where a link cannot be
# made) holds only this file, naming the new location.
MOVED_MARKER = "MOVED-TO"
_SQLITE_MAGIC = b"SQLite format 3\x00"


class DataDirCandidate(NamedTuple):
    """One place the resolver looked at; notices and ``aiteam paths`` show these.

    ``state``: "absent" (nothing there, or a dangling link), "database" (holds a
    file with the SQLite header; size is not looked at), "unreadable" (a database
    file is there but cannot be read, e.g. permissions: still somebody's data),
    "empty" (a directory without one), "stub" (a migrated directory holding
    ``MOVED_MARKER``) or "linked" (the legacy location and the canonical one are
    the same directory reached through a link: migrated).
    ``user_version`` comes from the database header, no connection is opened.
    """

    role: str
    path: str
    state: str
    db_size: int | None = None
    db_mtime: float | None = None
    user_version: int | None = None
    link_target: str = ""


class DataDirResolution(NamedTuple):
    """Where the OS data lives, how it was decided, and what was looked at.

    ``mode``: "env" (``AITEAM_DATA_DIR`` is set, taken as given), "canonical",
    "legacy" (the data is still in the old place and used there), "conflict"
    (both places hold a database; ``path`` is the canonical one) or "missing"
    (``path`` is where an installer would create it; it does not exist).
    """

    path: Path
    mode: str
    evidence: tuple[DataDirCandidate, ...]


class DataDirMissing(FileNotFoundError):  # noqa: N818 - the name the design and notices use
    """No OS data directory. Run time never creates one: an empty one would be
    taken for "no data" (docs/config-dir-compat-design.md §2).

    A FileNotFoundError on purpose: code that already treats a missing file as
    "nothing there" (``except OSError``) degrades the same way, without creating
    the directory to put the file in.
    """

    def __init__(self, resolution: DataDirResolution) -> None:
        self.resolution = resolution
        checked = "; ".join(f"{c.path} ({c.state})" for c in resolution.evidence)
        super().__init__(
            errno.ENOENT,
            "AI Team OS data directory not found and not created at run time. "
            f"Checked: {checked}. Run: aiteam paths --locate",
        )

    def __reduce__(self):
        return (type(self), (self.resolution,))


def _expand(spelling: str) -> Path:
    return Path(os.path.expanduser(spelling))


def _env_path(name: str) -> Path | None:
    """A non-blank environment value as a path (``~`` expanded); blank is unset."""
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def inspect_data_dir(role: str, path: Path) -> DataDirCandidate:
    """Classify one location by looking only: no mkdir, no sqlite connection."""
    target = ""
    try:
        if os.path.islink(path):
            target = os.readlink(path)
        is_dir = os.path.isdir(path)
    except OSError:
        is_dir = False
    if not is_dir:
        return DataDirCandidate(role, str(path), "absent", link_target=target)
    database = path / DB_NAME
    try:
        with open(database, "rb") as handle:
            header = handle.read(64)
        info = os.stat(database)
    except FileNotFoundError:
        header, info = b"", None
    except OSError:
        return DataDirCandidate(role, str(path), "unreadable", link_target=target)
    if info is not None and header[:16] == _SQLITE_MAGIC:
        version = int.from_bytes(header[60:64], "big") if len(header) == 64 else None
        return DataDirCandidate(role, str(path), "database", info.st_size, info.st_mtime, version, target)
    state = "stub" if os.path.exists(path / MOVED_MARKER) else "empty"
    return DataDirCandidate(role, str(path), state, link_target=target)


def _same_directory(legacy_path: Path, canonical_path: Path, legacy: DataDirCandidate,
                    canonical: DataDirCandidate) -> bool:
    """Are the two locations one directory reached through a link (the migrated form)?

    By identity (device and inode, ``samefile``), not by spelling: a link written
    with another letter case on a case-insensitive disk, or a link from the
    neutral directory back to the old one, is still the same directory and must
    not read as two databases. A dangling legacy link counts when it names the
    canonical location.
    """
    if not (legacy.link_target or canonical.link_target):
        return False
    try:
        return os.path.samefile(legacy_path, canonical_path)
    except OSError:
        return bool(legacy.link_target) and os.path.realpath(legacy_path) == os.path.realpath(canonical_path)


def resolve_data_dir() -> DataDirResolution:
    """Find the OS data directory (docs/config-dir-compat-design.md §5.1).

    ``AITEAM_DATA_DIR`` wins as given. Otherwise the canonical directory is used
    when it exists, unless it holds no database while the legacy one does (then
    the data decides: legacy). A legacy location that links to the canonical one,
    or is a stub, counts as migrated. Both holding a database is a conflict.
    """
    canonical_path = _expand(DEFAULT_DATA_DIR)
    legacy_path = _expand(LEGACY_DATA_DIR)
    canonical = inspect_data_dir("canonical", canonical_path)
    legacy = inspect_data_dir("legacy", legacy_path)
    if _same_directory(legacy_path, canonical_path, legacy, canonical):
        legacy = legacy._replace(state="linked", db_size=None, db_mtime=None, user_version=None)
    configured = _env_path(DATA_DIR_ENV)
    if configured is not None:
        return DataDirResolution(configured, "env", (inspect_data_dir("env", configured), canonical, legacy))
    evidence = (canonical, legacy)
    legacy_db = legacy.state == "database"
    if canonical.state == "database" and legacy_db:
        return DataDirResolution(canonical_path, "conflict", evidence)
    # An empty canonical directory never hides data the legacy one may still hold.
    if (canonical.state == "database" or (canonical.state == "empty" and legacy.state != "unreadable")) \
            and not legacy_db:
        return DataDirResolution(canonical_path, "canonical", evidence)
    if legacy_db:
        return DataDirResolution(legacy_path, "legacy", evidence)
    # The config folder may have moved and taken the old data directory with it.
    moved = cc_config_dir() / "data" / "ai-team-os"
    if os.path.abspath(moved) != os.path.abspath(legacy_path):
        evidence += (inspect_data_dir("config", moved),)
    return DataDirResolution(canonical_path, "missing", evidence)


def os_data_dir() -> Path:
    """The OS data directory; raises ``DataDirMissing`` instead of inventing one."""
    resolution = resolve_data_dir()
    if resolution.mode == "missing":
        raise DataDirMissing(resolution)
    return resolution.path


def create_data_dir_if_absent() -> tuple[DataDirResolution, bool]:
    """Installers only (the list is in aiteam.paths): the data directory, created
    only when nothing exists anywhere (docs/config-dir-compat-design.md §5.2).

    Returns the resolution afterwards and whether this call created the
    directory. An explicit ``AITEAM_DATA_DIR`` may be created. A legacy or
    conflicting directory is used as it is, and a missing one with traces left
    elsewhere (a database, readable or not, a stub, a link) is not replaced by an empty one: the
    caller reports the resolution instead.
    """
    resolution = resolve_data_dir()
    if resolution.mode == "env" and not resolution.path.is_dir():
        resolution.path.mkdir(parents=True, exist_ok=True)
        return resolution, True
    if resolution.mode != "missing":
        return resolution, False
    if any(c.state not in ("absent", "empty") for c in resolution.evidence):
        return resolution, False
    resolution.path.mkdir(parents=True, exist_ok=True)
    return resolve_data_dir(), True


# The API is one process serving the sessions of every config folder. While it
# handles a request it binds the folder of the session the request is about
# (aiteam.api.cc_config_scope), and cc_config_dir() and everything derived from it
# answer for that session. Nothing else binds: a hook or a CLI is its own session
# and reads its own environment.
_BOUND_CC_CONFIG_DIR: ContextVar[Path | None] = ContextVar("aiteam_bound_cc_config_dir", default=None)


def own_cc_config_dir() -> Path:
    """This process's config folder: ``CLAUDE_CONFIG_DIR`` when non-blank, else the default."""
    return _env_path("CLAUDE_CONFIG_DIR") or _expand(CC_DEFAULT_CONFIG_DIR)


def cc_config_dir() -> Path:
    """Claude Code's config folder: the one bound for this request (API only), else this process's own."""
    return _BOUND_CC_CONFIG_DIR.get() or own_cc_config_dir()


def bind_cc_config_dir(folder: Path | None) -> Token:
    """API only: make ``cc_config_dir()`` answer ``folder`` in this context (None: the own one)."""
    return _BOUND_CC_CONFIG_DIR.set(folder)


def unbind_cc_config_dir(token: Token) -> None:
    """Undo the ``bind_cc_config_dir`` call that returned ``token``."""
    _BOUND_CC_CONFIG_DIR.reset(token)


def cc_global_config() -> Path:
    """Claude Code's global config file (MCP servers live there).

    Inside a configured folder; without one it sits next to the default folder,
    not inside it. Always this process's own: the API binds the folder only, and
    reads no global config.
    """
    configured = _env_path("CLAUDE_CONFIG_DIR")
    if configured is None:
        return _expand(CC_DEFAULT_GLOBAL_CONFIG)
    return configured / os.path.basename(CC_DEFAULT_GLOBAL_CONFIG)


_MAIN_CHAIN_PARTS = ("hooks", "ai-team-os")


def cc_main_chain_dir() -> Path:
    """Where the installers keep this config folder's runtime hook copies (the main chain)."""
    return cc_config_dir().joinpath(*_MAIN_CHAIN_PARTS)


def cc_main_chain_command_dir() -> str:
    """The main chain directory as the installers spell it in settings.json commands.

    POSIX: ``${CLAUDE_CONFIG_DIR:-<default folder>}/hooks/ai-team-os``. Claude Code
    runs hook commands through ``sh -c``, which expands it, so a config folder that
    is moved and pointed at with the variable keeps running its own copies.
    Windows: the absolute path, since a POSIX shell is not guaranteed there.
    """
    if os.name == "nt":
        return cc_main_chain_dir().as_posix()
    default = CC_DEFAULT_CONFIG_DIR.replace("~", "$HOME", 1)
    return "/".join(("${CLAUDE_CONFIG_DIR:-" + default + "}", *_MAIN_CHAIN_PARTS))


# Who installed this config folder's main chain (docs/config-dir-compat-design.md
# §4.3): a record next to the copies, so it moves with them. Written by the
# installers (install.py: "source", auto_install.py: "plugin"); read by the orphan
# check, the install detection, the copy check and the left-over chain removal.
# Missing means unknown, never "plugin".
CHAIN_OWNER_FILE = ".chain-owner.json"
CHAIN_OWNERS = ("source", "plugin")


def _json_object(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def chain_owner() -> dict | None:
    """This config folder's main chain owner record, or None when there is none to go by."""
    record = _json_object(cc_main_chain_dir() / CHAIN_OWNER_FILE)
    return record if record is not None and record.get("installed_by") in CHAIN_OWNERS else None


def main_chain_owner(data_dir: Path | None = None) -> dict:
    """Who installed this config folder's main chain; {} when nothing says.

    The owner record decides. A chain installed before owner records existed keeps
    the older machine-wide rule for one release (§10 item 3): ``install_path.txt``
    in the data directory (``data_dir``, else the resolved one) means a source
    install, else ``main-chain.json`` there says what it says. Without a data
    directory only the record can answer.
    """
    owner = chain_owner()
    if owner is not None:
        return owner
    try:
        data_dir = os_data_dir() if data_dir is None else data_dir
    except OSError:
        return {}
    if (data_dir / "install_path.txt").exists():
        return {"installed_by": "source"}
    legacy = _json_object(data_dir / "main-chain.json") or {}
    return legacy if legacy.get("installed_by") in CHAIN_OWNERS else {}


def _utc_stamp() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json_atomic(path: Path, document: object) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_chain_owner(installed_by: str, *, baseline: str, version: str) -> Path:
    """Installers only: record who installed this config folder's main chain.

    ``baseline`` is where the copies come from (the source checkout, or the
    plugin's install folder). The copies are in place first: nothing is created
    here but the record. Raises OSError.
    """
    if installed_by not in CHAIN_OWNERS:
        raise ValueError(f"installed_by must be one of {CHAIN_OWNERS}")
    path = cc_main_chain_dir() / CHAIN_OWNER_FILE
    _write_json_atomic(path, {"installed_by": installed_by, "baseline": baseline, "version": version,
                              "installed_at": _utc_stamp()})
    return path


# The install registry in the data directory: one record per install or update
# ({host, config_dir, kind, version, at, ...}; an update adds "migration", the
# outcome of its data directory migration, and "data_migrated_at" when it moved
# the data). For queries only: a moved folder makes a record stale, so nothing at
# run time decides by it; the data_dir_legacy notice only quotes the reason of
# the last migration attempt (docs/config-dir-compat-design.md §4.2, §5.3).
INSTALLS_FILE = "installs.json"
INSTALLS_KEPT = 200


def _install_records(path: Path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, ValueError):
        return []
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def append_install_record(record: dict) -> Path:
    """Installers only: add one record (stamped ``at``) to the registry, keeping the newest INSTALLS_KEPT.

    Raises ``DataDirMissing`` (no data directory) or OSError.
    """
    path = os_data_dir() / INSTALLS_FILE
    records = _install_records(path)
    records.append({**record, "at": _utc_stamp()})
    _write_json_atomic(path, records[-INSTALLS_KEPT:])
    return path


def install_records() -> list[dict]:
    """The registry, oldest first; empty without a data directory or a readable file."""
    try:
        return _install_records(os_data_dir() / INSTALLS_FILE)
    except OSError:
        return []


def last_migration_attempt() -> dict | None:
    """The ``migration`` outcome of the newest install record that tried one; None when none did."""
    return next((record["migration"] for record in reversed(install_records())
                 if isinstance(record.get("migration"), dict)), None)


def codex_home(self_locate: bool = False, script: str | os.PathLike | None = None) -> Path:
    """The Codex home: ``CODEX_HOME`` when non-blank (``~`` expanded), else the default.

    With ``self_locate`` (hooks only), an unset variable is first inferred from
    where the running script is installed: ``<codex-home>/hooks/<adapter>/``,
    custom homes included.
    """
    configured = _env_path("CODEX_HOME")
    if configured is not None:
        return configured
    if self_locate:
        here = Path(script if script is not None else __file__).resolve()
        if len(here.parents) > 2 and here.parents[1].name == "hooks":
            return here.parents[2]
    return _expand(CODEX_DEFAULT_HOME)


# The MIGRATING lock a data directory migration holds (aiteam.data_dir_migration
# writes it): JSON {"pid", "host", "started_at"} in the directory being moved,
# live for MIGRATION_LOCK_TTL seconds since it was taken or last refreshed (the
# file's mtime: a long copy touches it) unless its process is gone on this host.
# Re-implemented here because hooks cannot import aiteam; a parity test holds
# the two readers equal.
MIGRATION_LOCK = "MIGRATING"
MIGRATION_LOCK_TTL = 30 * 60


def migration_lock(now: float | None = None) -> dict | None:
    """The live migration lock on the data directory (``{pid, host, started_at}``), or None.

    Looked for in the resolved, the canonical and the legacy location, since the
    directory moves while the lock sits in it. The migrating process itself is
    not locked out. Never raises.
    """
    import socket
    import time

    try:
        resolution = resolve_data_dir()
        places = [resolution.path] if resolution.mode != "missing" else []
        places += [_expand(DEFAULT_DATA_DIR), _expand(LEGACY_DATA_DIR)]
        host = socket.gethostname()
        now = time.time() if now is None else now
    except Exception:
        return None
    seen = set()
    for place in places:
        path = os.path.join(place, MIGRATION_LOCK)
        if path in seen:
            continue
        seen.add(path)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = None
        data = data if isinstance(data, dict) else {}
        pid = data.get("pid") if type(data.get("pid")) is int and data.get("pid") > 0 else None
        owner = data.get("host") if isinstance(data.get("host"), str) else None
        started = data.get("started_at")
        started = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else mtime
        if pid == os.getpid() and owner == host:
            continue
        if now - max(started, mtime) >= MIGRATION_LOCK_TTL:  # a long copy keeps the file fresh
            continue
        if pid and owner == host and os.name != "nt" and not _process_alive(pid):
            continue
        return {"pid": pid, "host": owner, "started_at": started, "path": path}
    return None


def migration_notice_params(lock: dict) -> dict:
    """The data_dir_migrating parameters for a lock: local start time and process id."""
    import time

    return {"started": time.strftime("%H:%M", time.localtime(lock.get("started_at") or 0)),
            "pid": str(lock.get("pid") or "?")}


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def api_port_file() -> Path:
    """The file the API writes its port to; raises ``DataDirMissing`` like ``os_data_dir``."""
    return os_data_dir() / "api_port.txt"


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(api_port_file().read_text().strip())
        return f"http://localhost:{port}"
    except (DataDirMissing, OSError, ValueError):
        return "http://localhost:8000"


MAX_FIELD_LEN = 500
MAX_PAYLOAD_BYTES = 32_768  # Overall payload limit 32KB; exceeding drops non-essential fields
# Raising it can void the connect_failed guarantee: see the size note in hook_delivery.py.
LARGE_FIELDS = {"last_assistant_message", "agent_transcript_path", "transcript_path"}
# Fields that must be preserved (not dropped even if payload exceeds limit)
ESSENTIAL_FIELDS = {
    "hook_event_name",
    "session_id",
    "tool_name",
    "tool_input",
    "cc_team_name",
    # 路径字段短（LARGE_FIELDS 已截 500 字符）且承载 wf_id 提取——超限剥离会
    # 让 SubagentStop 丢失 wf_id、per-run 建队/迁移失败（2026-07-07 D1 实录）
    "transcript_path",
    "agent_transcript_path",
    # cwd 同样短，且是**唯一**能反查主会话 transcript 的兜底键：
    # leader_usage.locate_main_transcript 在路径不可用时走 <slug(cwd)>/<session_id>.jsonl，
    # 而那条兜底的 docstring 写明兜的就是"超大载荷被剥离到必留字段"这一档——
    # cwd 不在必留里，兜底就在它专为之而生的场景里必然失效（2026-08-03 排查发现）。
    # 项目归属匹配（_resolve_project_id_by_cwd）也吃同一个键。
    "cwd",
    # PostCompact 的两个字段：压缩摘要动辄几万字，整体载荷必然超 32KB 闸，
    # 于是 trigger 与摘要长度双双被剥掉，落库成 {trigger:"", summary_chars:0}
    # ——生产实测就是这条形状。摘要正文本来就刻意不存，改为在 hook 侧算好长度
    # 再把正文扔掉（见 _trim_payload），长度与 trigger 一起进必留字段。
    "trigger",
    "compact_summary_chars",
    # Redelivery identity and subagent attribution; see send_event.py for why.
    "tool_use_id",
    "agent_id",
    "agent_type",
}


def _trim_payload(payload: dict) -> dict:
    """Truncate oversized fields to prevent HTTP timeouts.

    Two-level protection:
    1. Known large fields truncated to MAX_FIELD_LEN (500 chars)
    2. If overall exceeds 50KB, all string fields truncated to 200 chars
    """
    trimmed = {}
    for k, v in payload.items():
        if k == "compact_summary":
            # Measure here and drop the body: the OS deliberately never stores the
            # summary text (it is back in the model's context after the compact),
            # so shipping tens of KB over HTTP only serves to trip the size gate
            # and lose the one number we actually wanted.
            trimmed["compact_summary_chars"] = len(v) if isinstance(v, str) else 0
            continue
        if k in LARGE_FIELDS:
            if isinstance(v, str) and len(v) > MAX_FIELD_LEN:
                trimmed[k] = v[:MAX_FIELD_LEN] + "...(truncated)"
            elif isinstance(v, dict):
                trimmed[k] = str(v)[:MAX_FIELD_LEN] + "...(truncated)"
            else:
                trimmed[k] = v
        elif k == "tool_response" and isinstance(v, dict):
            # Truncate tool output but preserve structure
            tr = {}
            for rk, rv in v.items():
                if isinstance(rv, str) and len(rv) > MAX_FIELD_LEN:
                    tr[rk] = rv[:MAX_FIELD_LEN] + "...(truncated)"
                else:
                    tr[rk] = rv
            trimmed[k] = tr
        else:
            trimmed[k] = v

    # Overall size check: if exceeds 50KB, truncate all string fields recursively
    payload_str = json.dumps(trimmed)
    if len(payload_str) > 50_000:
        for k, v in trimmed.items():
            if isinstance(v, str) and len(v) > 200:
                trimmed[k] = v[:200] + "...(truncated)"

    return trimmed


class HookPostState(StrEnum):
    """Outcome of one hook invocation, from the entry script's point of view.

    Five states, only three of which ``post_event`` can return; ``INVOKED`` and
    ``INERT_DROPPED`` describe what happened *before* the POST and are reported by
    the entry script itself. They live here so every harness names the same
    outcomes. The finer reason for a failed POST is the failure class that
    ``post_event_detailed`` returns alongside the state.
    """

    INVOKED = "invoked"                    # entry script ran, before any decision
    INERT_DROPPED = "inert_dropped"        # dropped by the harness inert-tool guard
    POSTED = "posted"                      # API accepted the event
    POST_UNREACHABLE = "post_unreachable"  # any URLError, HTTP error statuses included
    ERROR = "error"                        # anything else (encode, read timeout, ...)


def classify_post_failure(exc: BaseException) -> tuple[str, int | None]:
    """Sort a failed hook POST into one of the failure classes, with its HTTP status.

    Order matters: HTTPError is a subclass of URLError, and urllib wraps only the
    connect-and-send phase in URLError. Exceptions raised while waiting for the
    response (a read timeout, a dropped connection) come through unwrapped.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if 400 <= exc.code < 500:
            return "http_4xx", exc.code
        if 500 <= exc.code < 600:
            return "http_5xx", exc.code
        return "other", exc.code
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, ConnectionRefusedError):
            return "refused", None
        return "connect_failed", None
    if isinstance(exc, TimeoutError):
        return "timeout_after_send", None
    if isinstance(exc, (ConnectionError, http.client.HTTPException)):
        return "reset_after_send", None
    return "other", None


def post_event(
    payload: dict, api_url: str, *, extra_essential_fields: frozenset[str] = frozenset(),
) -> HookPostState:
    """Serialize, size-guard and POST one hook payload. Never raises.

    The state half of ``post_event_detailed``; the return values and stderr
    lines are unchanged from before the failure classes existed, because the
    Codex entry derives its reason codes from those stderr suffixes.
    """
    return post_event_detailed(
        payload, api_url, extra_essential_fields=extra_essential_fields,
    )[0]


def post_event_detailed(
    payload: dict, api_url: str, *, extra_essential_fields: frozenset[str] = frozenset(),
) -> tuple[HookPostState, str]:
    """``post_event`` plus the failure class: ("posted" or one of the classes of
    ``classify_post_failure``). Never raises.

    By default, the same request body as ``send_event.py`` main(): the
    oversize path keeps only ESSENTIAL_FIELDS and appends ``_stripped`` /
    ``_original_size`` in that order. Failures are written to stderr and nothing
    else - a hook must never block or slow down its host.

    The stderr label comes from the payload's ``hook_event_name`` rather than
    ``sys.argv[1]``; every entry script sets that key before calling in, and the
    request body (the thing under test) is unaffected either way.
    A harness may explicitly retain its own metadata without changing the defaults.
    """
    event_name = payload.get("hook_event_name") or "unknown"
    try:
        data = json.dumps(payload).encode("utf-8")
        if len(data) > MAX_PAYLOAD_BYTES:
            preserved = ESSENTIAL_FIELDS | extra_essential_fields
            stripped = {k: v for k, v in payload.items() if k in preserved}
            stripped["_stripped"] = True
            stripped["_original_size"] = len(data)
            sys.stderr.write(
                f"[aiteam-hook] {event_name}: payload too large "
                f"({len(data)} bytes > {MAX_PAYLOAD_BYTES}), stripped to essentials\n"
            )
            data = json.dumps(stripped).encode("utf-8")
        req = urllib.request.Request(
            f"{api_url}/api/hooks/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=1.5) as resp:
            resp.read()  # Consume response without output - decisions handled by workflow_reminder.py
        return HookPostState.POSTED, "posted"

    except urllib.error.URLError as e:
        # OS service not running; output to stderr for debugging (doesn't block the host)
        sys.stderr.write(f"[aiteam-hook] {event_name}: API unreachable - {e}\n")
        return HookPostState.POST_UNREACHABLE, classify_post_failure(e)[0]
    except Exception as e:
        # Log other errors to stderr as well
        sys.stderr.write(f"[aiteam-hook] {event_name}: error - {e}\n")
        return HookPostState.ERROR, classify_post_failure(e)[0]


def _sanitize_inline(text: str) -> str:
    """Flatten quoted text into one safe line before it is injected.

    Collapsing whitespace is not enough: ``str.split()`` only knows whitespace, so
    Unicode format characters (Cf) and most control characters (Cc) pass through,
    and they are the ones that matter. U+202E makes the shown text read reversed,
    U+200B hides content between two visible characters. None of them has a
    legitimate use in a one-line hint; their only effect is that what a reader
    sees differs from what is there. Replace the whole C category with spaces,
    then collapse. The one exception is an unassigned code point in the emoji and
    symbol blocks: newer Unicode versions keep adding pictographs there, and this
    Python's Unicode data reads them as unassigned (aiteam.text_safety.clean_text
    keeps the same table; a parity test holds the two equal).
    """
    cleaned = "".join(" " if _unsafe_char(char) else char for char in (text or ""))
    return " ".join(cleaned.split())


_SYMBOL_BLOCKS = ((0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x1F000, 0x1FBFF))


def _unsafe_char(char: str) -> bool:
    kind = unicodedata.category(char)
    if kind[0] != "C":
        return False
    return kind != "Cn" or not any(low <= ord(char) <= high for low, high in _SYMBOL_BLOCKS)


class InvocationAudit:
    """Keep only allowlisted metadata; audit failure must never affect the hook.

    The log rotates inline, with no timer: a write that finds the file at
    ``max_bytes`` renames it to ``<name>.1`` (replacing the previous one) and
    starts a new file, so each host keeps at most about twice ``max_bytes``.
    Writers coordinate on the open file's advisory lock and recheck its inode;
    contention costs at most 50 ms before this best-effort audit is skipped.
    Imports stay inside the methods: every PreToolUse hook loads this module,
    only the unread entries build an audit.
    """

    max_bytes = 5 * 1024 * 1024
    lock_wait_seconds = 0.05

    def __init__(self, *, host: str) -> None:
        import threading
        import time
        import uuid

        if host not in {"cc", "codex"}:
            raise ValueError("unsupported audit host")
        self.host = host
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.metadata = {
            "call_id": uuid.uuid4().hex, "stage": "startup", "reader": None,
            "pid": os.getpid(), "ppid": os.getppid(),
            "resolved_project_id": None, "cwd_provided": False, "cwd_hash": None,
        }

    def update(self, **values: object) -> None:
        with self.lock:
            self.metadata.update(values)

    def record(self, event: str, reason: str, output_chars: int = 0) -> None:
        try:
            import time
            from datetime import UTC, datetime
            from pathlib import Path

            state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
            path = Path(os.environ.get("AITEAM_UNREAD_AUDIT_PATH") or (
                state / "ai-team-os" / self.host / "unread-invocations.jsonl"
            ))
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.lock:
                record = {**self.metadata, "event": event, "reason": reason,
                          "timestamp": datetime.now(UTC).isoformat(),
                          "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                          "output_chars": output_chars}
            line = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
            self._append(path, line)
        except Exception:
            pass

    def _append(self, path, line: bytes) -> None:
        import fcntl
        import stat
        import time

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        deadline = time.monotonic() + self.lock_wait_seconds
        while time.monotonic() < deadline:
            fd = os.open(path, flags, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(min(0.001, max(0, deadline - time.monotonic())))
                    continue
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    return
                # Every writer locks, then checks that it still owns the current
                # pathname. A descriptor opened before another writer's rotation
                # must be reopened rather than appended to the archived inode.
                try:
                    current = os.stat(path, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    continue
                os.fchmod(fd, 0o600)
                if info.st_size >= self.max_bytes:
                    os.replace(path, path.with_name(path.name + ".1"))
                    continue
                # One append write preserves line boundaries across hook processes.
                os.write(fd, line)
                return
            finally:
                os.close(fd)
