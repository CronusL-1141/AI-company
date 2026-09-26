"""Infrastructure and OS-level MCP tools."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from aiteam.mcp._base import (
    API_DOWN_HINT,
    _api_call,
    _cc_session_id,
    _current_cwd,
    _get_api_url,
    _resolve_project_id,
    pick_active_team,
)
from aiteam.mcp.tools.views import (
    EVENT_COMPACT_CAP,
    EVENT_HINT,
    FIELDS_ERROR,
    compact_event_row,
    resolve_view,
)


def _usage_coverage_line() -> str:
    """One line of token-attribution coverage for ``os_health_check``.

    按需触发、零新增守护（归因设计 P3）：健康检查本来就要打一次 API，顺手多问一句
    覆盖率，不为此多起任何东西。

    这一行刻意报的是**分子/分母**而不是一个百分比：百分比可以在分母被悄悄改小之后
    依然好看，而分子分母摆在一起，分母缩水一眼就能看见（R2）。链路最窄的一跳也一并
    报出来——端到端覆盖率是各跳的乘积，只看采集率会漏掉真正的瓶颈（§4.1）。
    """
    data = _api_call("GET", "/api/usage/coverage")
    if not isinstance(data, dict) or data.get("success") is False:
        return "unavailable"
    payload = data.get("data") or {}
    parts = []
    for row in payload.get("rows") or []:
        total = row.get("dispatches_total")
        if total is None:  # "设计上不采集"是正式取值，不是 0，也不该混进覆盖率摘要
            continue
        # 同一条 path 会按 harness 分成多行（两桶禁相加，所以分列而不是合并）。摘要
        # 里只打 path 的话，这两行会长得一模一样，读的人分不出哪一行是哪个 harness
        # 的——分列呈现的意义正在于此，标签丢了等于没分列。未标注的桶不加后缀：今天
        # 全库未标注，加了只会给每一行挂一个没有信息量的尾巴。
        harness = row.get("harness")
        path = f"{row.get('path')}@{harness}" if harness else f"{row.get('path')}"
        parts.append(
            f"{path}[{row.get('metric') or '—'}] "
            f"{row.get('dispatches_attributed')}/{total}"
        )
    hops = [h for h in (payload.get("hops") or []) if h.get("required")]
    if hops:
        worst = min(hops, key=lambda h: h["resolvable"] / h["required"])
        parts.append(f"narrowest hop {worst['edge']} {worst['resolvable']}/{worst['required']}")
    return " · ".join(parts) if parts else "no data"


HOOK_DELIVERY_WINDOW_HOURS = 24


def _installed_hook_recording() -> dict[str, Any]:
    """Whether the source-installed hooks can record failures at all.

    With send_event.py installed but hook_delivery.py missing, send_event falls back
    to an unrecorded POST, and an empty ledger then means "not recorded", not
    "nothing failed". A plugin-only install runs its hooks from the plugin tree,
    which always ships hook_delivery.py; that case shows as no_source_install.
    """
    from aiteam.services.notices import install_kind

    hooks_dir = install_kind.cc_config_dir() / "hooks" / "ai-team-os"
    send_event = (hooks_dir / "send_event.py").is_file()
    delivery = (hooks_dir / "hook_delivery.py").is_file()
    if send_event and delivery:
        recording = "active"
    elif send_event:
        recording = "fallback_unrecorded"
    else:
        recording = "no_source_install"
    return {
        "dir": str(hooks_dir),
        "send_event": send_event,
        "hook_delivery": delivery,
        "recording": recording,
    }


def _hook_delivery_summary() -> dict[str, Any]:
    """Failed hook POSTs of the last day, from the local delivery ledger.

    Read straight from the file the hooks append to, so the numbers are there
    even when the API is down (which is when they matter most). The ledger holds
    identifiers and timing only; this reports counts, never session ids.

    The ledger keeps two generations, so after a busy day the oldest lines of the
    window may already be gone. ``complete`` says whether the counts cover the whole
    window; when they do not, they are a lower bound covering ``covered_since`` on.
    """
    from datetime import datetime, timedelta

    from aiteam.clock import utc_now
    from aiteam.hooks import hook_delivery

    directory = Path(hook_delivery.ledger_dir())
    since = utc_now() - timedelta(hours=HOOK_DELIVERY_WINDOW_HOURS)
    by_class = dict.fromkeys(hook_delivery.FAILURE_CLASSES, 0)
    by_event: dict[str, int] = {}
    # Replay queue: what happened to failed events (queued or why not) and to
    # queued records (delivered, duplicate, requeued, recovered, dropped).
    spool_counts = dict.fromkeys(("queued", *hook_delivery.NOT_QUEUED), 0)
    shrunk = 0
    outcomes = dict.fromkeys(hook_delivery.REPLAY_OUTCOMES, 0)
    drops = dict.fromkeys(hook_delivery.DROPS, 0)
    # Outcomes by the class of the record's first failure: how often a redelivery
    # of a timeout_after_send found it had landed (duplicate) is the real landing
    # rate of that class; refused ones never land first time and would dilute it.
    by_origin: dict[str, dict[str, int]] = {}
    last_failure_at = None
    oldest = None
    rotated = (directory / hook_delivery.LEDGER_ROTATED_NAME).exists()
    unreadable = 0
    for name in (hook_delivery.LEDGER_ROTATED_NAME, hook_delivery.LEDGER_NAME):
        try:
            lines = (directory / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            unreadable += 1
            continue
        for line in lines:
            try:
                entry = json.loads(line)
                at = datetime.fromisoformat(entry["t"])
            except (ValueError, KeyError, TypeError):
                unreadable += 1
                continue
            if at.tzinfo is None:
                unreadable += 1
                continue
            if oldest is None or at < oldest:
                oldest = at
            if at < since:
                continue
            replay = entry.get("replay")
            if replay is not None:
                if replay in outcomes:
                    outcomes[replay] += 1
                elif replay in drops:
                    drops[replay] += 1
                origin_cls = entry.get("origin_cls")
                if origin_cls and replay != "orphan_recovered":
                    group = by_origin.setdefault(str(origin_cls), {})
                    group[replay] = group.get(replay, 0) + 1
                continue
            cls = entry.get("cls")
            by_class[cls if cls in by_class else "other"] += 1
            event = str(entry.get("ev") or "unknown")
            by_event[event] = by_event.get(event, 0) + 1
            if last_failure_at is None or at > last_failure_at:
                last_failure_at = at
            spool = entry.get("spool")
            if spool in spool_counts:
                spool_counts[spool] += 1
                if spool == "queued" and entry.get("shrunk_from"):
                    shrunk += 1
            elif spool in drops:
                drops[spool] += 1
    # Without a rotation the ledger holds everything ever recorded. After one, lines
    # older than the rotated generation's first line are gone.
    complete = not rotated or (oldest is not None and oldest <= since)
    summary: dict[str, Any] = {
        "window_hours": HOOK_DELIVERY_WINDOW_HOURS,
        "complete": complete,
        "covered_since": (since if complete else oldest).isoformat() if complete or oldest else None,
        "failed_posts": sum(by_class.values()),
        "by_class": {k: v for k, v in by_class.items() if v},
        "by_event": by_event,
        "last_failure_at": last_failure_at.isoformat() if last_failure_at else None,
        "ledger": str(directory / hook_delivery.LEDGER_NAME),
        "replay": {
            # Now, not windowed: what the queue holds at this moment.
            **hook_delivery.queue_state(),
            "queued": spool_counts.pop("queued"),
            "queued_shrunk": shrunk,
            "not_queued": spool_counts,
            "outcomes": outcomes,
            "by_origin_class": {
                cls: {**counts, **(
                    {"landed_share": round(counts.get("duplicate", 0) / answered, 3)}
                    if (answered := counts.get("delivered", 0) + counts.get("duplicate", 0))
                    else {}
                )}
                for cls, counts in sorted(by_origin.items())
            },
            "drops": drops,
        },
        "installed_hooks": _installed_hook_recording(),
    }
    if unreadable:
        summary["unreadable_lines"] = unreadable
    return summary


def _hook_delivery_section() -> dict[str, Any]:
    """The summary, or why it is missing; a health check must not fail on it."""
    try:
        return _hook_delivery_summary()
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break the check
        return {"error": f"{type(exc).__name__}: {exc}"}


def _hook_ingest_section() -> dict[str, Any]:
    """Server-side hook ingest counts over the last day, or why they are missing."""
    try:
        result = _api_call("GET", "/api/hooks/ingest-stats")
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break the check
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict) or result.get("success") is not True:
        return {"error": (result or {}).get("error", "unavailable") if isinstance(result, dict)
                else "unavailable"}
    return {key: result[key] for key in (
        "window_hours", "since", "window", "complete", "processes_without_final_rollup",
        "current_process", "notes",
    ) if key in result}


def _restart_pid_alive(pid: int) -> bool:
    """Return True if *pid* refers to a live (non-zombie) process.

    POSIX 上优雅停机成功后子进程可能滞留为 defunct 僵尸（父 MCP 尚未收尸）：
    僵尸不占端口、也永远不会再"退出"，必须视为已死——否则重启守卫会把成功的
    停机误报成 shutdown_timeout（2026-07-06 巡检实录）。Windows 无僵尸语义。
    Prefers psutil and falls back to os.kill(pid, 0) + ps state check.
    """
    try:
        import psutil

        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    except ImportError:
        pass
    try:
        os.kill(pid, 0)  # signal 0 = existence check only
    except (ProcessLookupError, OSError, SystemError):
        # OSError/SystemError (WinError 87) on Windows when the process is gone
        return False
    except PermissionError:
        # Process exists but is owned by another user — still "alive"
        return True
    if sys.platform != "win32":
        import subprocess

        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "state="],
                text=True, stderr=subprocess.DEVNULL, timeout=3,
            ).strip()
            if out.startswith("Z"):
                return False  # defunct 僵尸 = 已死
        except Exception:  # noqa: BLE001 — ps 不可用时保守视为存活
            pass
    return True


def _restart_local_get(path: str, port: int, timeout: float = 3.0) -> dict[str, Any] | None:
    """GET a localhost API path directly (no project headers), returning JSON or None.

    Used by os_restart_api for raw health/version probes that must not be subject to
    project-scoping headers. Returns None on any connection/parse failure.
    """
    url = f"http://localhost:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _restart_local_post(path: str, port: int, timeout: float = 5.0) -> dict[str, Any] | None:
    """POST (empty body) to a localhost API path directly, returning JSON or None."""
    url = f"http://localhost:{port}{path}"
    req = urllib.request.Request(
        url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _restart_command(port: int) -> list[str]:
    return [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app",
            "--host", "127.0.0.1", "--port", str(port), "--factory"]


def _restart_preflight(source_root: str, port: int) -> dict[str, Any]:
    """Check imports in an isolated child before touching the running service."""
    try:
        root = Path(source_root).expanduser().resolve() if source_root else None
        if root is not None:
            with (root / "pyproject.toml").open("rb") as handle:
                project = tomllib.load(handle)
            if project.get("project", {}).get("name") != "ai-team-os":
                raise ValueError("source_root must identify the ai-team-os project")
            if not (root / "src/aiteam/api/app.py").is_file():
                raise ValueError("source_root is missing src/aiteam/api/app.py")
        env = os.environ.copy()
        if root is not None:
            env["PYTHONPATH"] = str(root / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="aiteam-restart-check-") as temporary:
            env["AITEAM_DB_PATH"] = str(Path(temporary) / "preflight.db")
            checked = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import json, pathlib, uvicorn; import aiteam.api.app as app; "
                 "assert callable(app.create_app); "
                 "print(json.dumps({'source_file': str(pathlib.Path(app.__file__).resolve())}))"],
                env=env, cwd=str(root) if root else None,
                capture_output=True, text=True, timeout=20, check=True,
            )
        actual = Path(json.loads(checked.stdout.splitlines()[-1])["source_file"])
        if root is not None and actual != (root / "src/aiteam/api/app.py").resolve():
            raise ValueError("Imported API does not belong to source_root")
        return {"success": True, "source_file": str(actual),
                "source_root": str(root) if root else str(actual.parents[3]),
                "interpreter": sys.executable, "port": port,
                "command": _restart_command(port)}
    except (OSError, ValueError, subprocess.SubprocessError, IndexError, KeyError) as exc:
        return {"success": False, "error": "preflight_failed", "detail": str(exc)}


def _restart_spawn_on_port(autostart, port: int, *, source_root: str = "") -> dict[str, Any]:
    """Spawn a fresh uvicorn API subprocess on *port*, reusing _autostart bookkeeping.

    Mirrors the spawn step of _autostart._ensure_api_running_locked (same uvicorn
    factory invocation), but pinned to the caller-supplied port and without any
    port-drift fallback — the os_restart_api guards already ensured the port is free.

    Updates the shared PID file and port file so other MCP sessions discover the new
    process. Returns {success, new_pid} or {success: False, error, detail}.
    """
    import subprocess
    import sys

    # Detach fully from the MCP server's stdio. Spawning from inside an MCP
    # *tool call* (unlike _autostart's init-time spawn) inherits the live MCP
    # stdio pipes; an inherited stdin/stderr handle made the child hang before
    # imports (observed: stuck at 9MB forever). stderr goes to the SAME
    # persistent log as _autostart — a tmpdir file survives neither reboots
    # nor the selfcheck loop's gaze (it only watches api-stderr.log).
    from aiteam.mcp._autostart import _API_STDERR_LOG

    stderr_log = _API_STDERR_LOG
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    try:
        spawn_options = {}
        if source_root:
            env = os.environ.copy()
            if env.get("AITEAM_DB_PATH"):
                # Keep the caller's database when the child changes checkout.
                env["AITEAM_DB_PATH"] = str(Path(env["AITEAM_DB_PATH"]).expanduser().absolute())
            env["PYTHONPATH"] = str(Path(source_root) / "src")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            spawn_options = {"env": env, "cwd": source_root}
        with open(stderr_log, "ab") as log_fh:
            proc = subprocess.Popen(
                _restart_command(port),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_fh,
                close_fds=True,
                creationflags=creationflags,
                # Keep the shared API outside the launching MCP's process group.
                start_new_session=os.name == "posix",
                **spawn_options,
            )
    except Exception as exc:
        return {
            "success": False,
            "error": "spawn_failed",
            "detail": f"无法启动 uvicorn 子进程: {exc}",
        }

    previous = autostart._api_process
    if previous is None or previous.poll() is not None:
        autostart._api_process = proc
        atexit.unregister(autostart._cleanup_api)
        atexit.register(autostart._cleanup_api)
    try:
        autostart._write_pid_file(proc.pid)
        autostart._save_api_port(port)
    except OSError as exc:
        return {"success": False, "error": "bookkeeping_failed", "new_pid": proc.pid,
                "detail": f"服务子进程已启动，台账更新失败；请核验后自愈，不要重复启动: {exc}"}
    return {"success": True, "new_pid": proc.pid}


def _local_record_path(host: str) -> Path:
    """The notice record file hooks import on their next fetch (same place as user_notice.py)."""
    return Path.home() / ".claude" / "data" / "ai-team-os" / f"notice-local.{host}.jsonl"


def _append_local_record(host: str, record: dict[str, Any]) -> bool:
    """Append one record line (append-only, at most 1KB), like user_notice.record_local."""
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > 1025:
        return False
    path = _local_record_path(host)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def _caller_host() -> str:
    """Which host this MCP server serves: cc when Claude Code gave it a session id, else codex.

    The OS MCP server is started by one of two hosts. Claude Code always injects
    CLAUDE_CODE_SESSION_ID; the Codex adapter's server runs without it.
    """
    return "cc" if _cc_session_id() else "codex"


def _record_config_write(data: dict[str, Any]) -> dict[str, Any]:
    """Write the decision.user_config_write event: through the API, else the local record file."""
    import uuid

    from aiteam.services.config_change import compact_for_local_record

    host = str(data.get("host") or "cc")
    record = {
        "uuid": uuid.uuid4().hex,
        "kind": "consent",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts": round(time.time(), 3),
        "source": "os_config_change",
        **data,
    }
    result = _api_call("POST", "/api/notices/consent", record)
    if result.get("success") is True:
        return {"recorded": "api", "uuid": record["uuid"]}
    local_host = host if host in ("cc", "codex") else "cc"
    envelope = {key: record[key] for key in ("uuid", "kind", "at", "ts", "source")}
    envelope_bytes = len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    try:
        # Joining two JSON objects costs -1 byte; the terminating newline costs
        # +1. Reserve the entire envelope before compacting the event payload.
        slim = {**envelope, **compact_for_local_record(data, limit=1025 - envelope_bytes)}
    except ValueError as exc:
        return {"recorded": "none", "uuid": record["uuid"], "note": str(exc)}
    if _append_local_record(local_host, slim):
        return {"recorded": "local", "uuid": record["uuid"],
                "note": "API unreachable: the event is imported on the next hook fetch"}
    return {"recorded": "none", "uuid": record["uuid"],
            "note": "API unreachable and the local record could not be written"}


def register(mcp):
    """Register all infrastructure MCP tools."""

    @mcp.tool()
    def context_resolve() -> dict[str, Any]:
        """Get the current active OS context — active project, active teams, member list.

        This is the infrastructure for all simplified operations. A single call returns
        the complete context of the current working environment, allowing Leader or other
        tools to auto-fill parameters like project_id, team_id, etc.

        ``teams`` lists EVERY active team of the current project (a project routinely
        has several at once: the session container team plus one per Workflow run).
        ``team`` keeps the singular shape for backwards compatibility and holds the
        primary team picked by the same 3-tier priority as team_id auto-resolution
        (session container > plain project team > newest).

        Returns:
            Context dict containing project / team / teams / agents
        """
        result: dict[str, Any] = {"project": None, "team": None, "teams": [], "agents": []}

        try:
            projects_data = _api_call("GET", "/api/projects")
            projects = projects_data.get("data", [])
            if projects:
                cwd = _current_cwd().replace("\\", "/").rstrip("/").lower()
                # Longest-prefix match — pick the most specific project
                best_p = None
                best_len = -1
                for p in projects:
                    rp = (p.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
                    if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
                        best_p = p
                        best_len = len(rp)
                if best_p is not None:
                    result["project"] = {"id": best_p["id"], "name": best_p.get("name", "")}

            # v1.5.2 fix: project-aware active team resolution.
            # Filter teams by current project_id so a Leader in cwd=A doesn't pick up
            # another project B's active team (root cause of 2026-05-08 cross-project agent dispatch).
            current_project_id = result["project"]["id"] if result["project"] else None
            teams_data = _api_call("GET", "/api/teams")
            all_active = [t for t in teams_data.get("data", []) if t.get("status") == "active"]
            if current_project_id:
                project_teams = [t for t in all_active if t.get("project_id") == current_project_id]
            else:
                project_teams = []  # No project resolved → no team (avoid cross-project leak)
            # 复数队是常态：一个项目同时挂着 session 容器队 + N 支 workflow per-run 队。
            # 只回单数 team 会让 Leader 看不见另外几支（也就无从发现绑错了对象）。
            result["teams"] = [
                {
                    "id": t["id"],
                    "name": t.get("name", ""),
                    "kind": (t.get("config") or {}).get("kind", ""),
                }
                for t in project_teams
            ]
            team = pick_active_team(project_teams, _cc_session_id(), current_project_id or "")
            if team is not None:
                result["team"] = {"id": team["id"], "name": team.get("name", "")}
                agents_data = _api_call("GET", f"/api/teams/{team['id']}/agents")
                result["agents"] = [
                    {"name": a["name"], "status": a["status"], "role": a.get("role", "")}
                    for a in agents_data.get("data", [])
                ]

        except Exception as e:
            result["error"] = str(e)

        return result

    @mcp.tool()
    def os_health_check() -> dict[str, Any]:
        """Check the health status of the AI Team OS API service.

        Verifies the API service is running normally by accessing the team list
        endpoint, and reports one line of token-attribution coverage alongside it.
        When the API is local and on the port this MCP server manages, it also
        reconciles the shared PID file: a single healthy listener on that port is
        written into the PID file if the file points elsewhere.

        Returns:
            Health status info including API reachability, team count, a
            usage-coverage summary (measured / dispatched per path, plus the
            narrowest link in the attribution chain), pid_reconciliation
            {status: verified / unverified / not_local / not_managed, pid}, and
            hook_delivery: hook events whose POST failed in the last 24h, by
            failure class and by event, read from the local ledger (present
            whether or not the API is up); complete=false marks the counts as a
            lower bound from covered_since, replay shows the redelivery queue
            (pending records, oldest age, queued / shrunk / not queued, outcomes
            overall and by the first failure class with the share that had
            landed after all, and every kind of drop), and
            installed_hooks.recording says whether the
            installed hooks record failures at all; and (API up only) hook_ingest:
            the API side of the same traffic over the last 24h, persisted across
            restarts: client_gone (receipts lost while queued, a lower bound),
            body_lost (events lost before the body was read), slow, and replay
            counts, with complete=false when a process exited without its final
            rollup
        """
        api_url = _get_api_url()
        result = _api_call("GET", "/api/teams")
        if result.get("success") is False:
            return {
                "status": "unhealthy",
                "api_url": api_url,
                "error": result.get("error", "未知错误"),
                "hint": result.get("hint", API_DOWN_HINT),
                "hook_delivery": _hook_delivery_section(),
            }
        from aiteam.mcp import _autostart

        api_target = urllib.parse.urlparse(api_url)
        reconciliation: dict[str, Any] = {"status": "not_local", "pid": None}
        if api_target.hostname in {"localhost", "127.0.0.1", "::1"}:
            port = api_target.port or (443 if api_target.scheme == "https" else 80)
            if port != _autostart._get_api_port():
                reconciliation = {"status": "not_managed", "pid": None}
            else:
                pid = _autostart._reconcile_api_pid(port)
                reconciliation = {"status": "verified" if pid else "unverified", "pid": pid}
        return {
            "status": "healthy",
            "api_url": api_url,
            "teams_count": result.get("total", 0),
            "usage_coverage": _usage_coverage_line(),
            "pid_reconciliation": reconciliation,
            "hook_delivery": _hook_delivery_section(),
            "hook_ingest": _hook_ingest_section(),
        }

    @mcp.tool()
    def os_restart_api(
        force: bool = False, source_root: str = "", dry_run: bool = False,
    ) -> dict[str, Any]:
        """Restart the AI Team OS FastAPI process safely (standardized restart flow).

        Use this after backend code changes to pick up the new version without
        manually killing processes. The flow has three safety guards:

        1. Busy-agent guard — refuses to restart while any agent is working
           (status=busy) unless force=True.
        2. Port-pin guard — only ever restarts on the ORIGINAL port (default 8000,
           read from api_port.txt). If that port is held by an unrelated process it
           aborts rather than drifting to a random port.
        3. Dead-before-spawn guard — waits until the old process has fully exited and
           released the port before spawning the new one; never spawns on a timeout.

        If the API is already down there is nothing to shut down: guards 1 and 3
        do not apply, guard 2 still does, and this becomes a plain start of the API
        on its configured port.

        Args:
            force: Bypass the busy-agent guard and restart even while agents work.
            source_root: Explicit ai-team-os repository root to import and start.
                Empty preserves the current environment. Restore by explicitly
                passing the original repository root through this same flow.
            dry_run: Only preflight imports and return the startup plan, without
                shutting down, spawning, or updating shared runtime files.

        Returns:
            On success: {success, old_version, new_version, old_pid, new_pid, elapsed_ms}.
            On refusal/failure: {success: False, error, detail}.
        """
        from aiteam.mcp import _autostart

        t0 = time.monotonic()
        port = _autostart._get_api_port()
        preflight = None
        if source_root or dry_run:
            preflight = _restart_preflight(source_root, port)
            if not preflight["success"]:
                return preflight
            if dry_run:
                return {**preflight, "dry_run": True,
                        "detail": "仅完成导入预检；未关闭或启动服务，未修改运行台账"}
            source_root = preflight["source_root"]

        # --- 1. Probe current API + read old version (raw localhost, no project headers) ---
        health = _restart_local_get("/api/health", port, timeout=2.0)
        api_was_up = health is not None
        old_version = health.get("version") if health else None
        old_pid = _autostart._read_pid_file()  # None if stale/missing/dead

        if api_was_up:
            # --- 2. Guard: refuse while agents are busy (unless force) ---
            if not force:
                busy_total = 0
                teams = _restart_local_get("/api/teams", port, timeout=3.0)
                for team in (teams or {}).get("data", []):
                    if team.get("status") != "active":
                        continue
                    agents = _restart_local_get(
                        f"/api/teams/{team['id']}/agents?limit=200", port, timeout=3.0
                    )
                    for agent in (agents or {}).get("data", []):
                        if agent.get("status") == "busy":
                            busy_total += 1
                if busy_total > 0:
                    return {
                        "success": False,
                        "error": "busy_agents",
                        "detail": f"{busy_total} 个 agent 工作中，确需重启传 force=true",
                    }

            # --- 3. Request graceful shutdown ---
            resp = _restart_local_post("/api/system/shutdown", port, timeout=5.0)
            if resp is None or not resp.get("success"):
                return {
                    "success": False,
                    "error": "shutdown_failed",
                    "detail": "POST /api/system/shutdown 未成功返回，已中止重启",
                }

            shutdown_pid = resp.get("pid")
            if type(shutdown_pid) is int and shutdown_pid > 0:
                old_pid = shutdown_pid

            # --- 4. Guard: wait for old process to die AND port to release (≤10s) ---
            # Iteration cap: even if the monotonic clock misbehaves (frozen/mocked),
            # this loop must terminate — a runaway here once ate 32GB via mock recording.
            deadline = time.monotonic() + 10.0
            _iters = 0
            while time.monotonic() < deadline and _iters < 200:
                _iters += 1
                pid_dead = old_pid is None or not _restart_pid_alive(old_pid)
                port_free = not _autostart._is_port_open(port=port)
                if pid_dead and port_free:
                    break
                time.sleep(0.3)
            else:
                still_alive = old_pid is not None and _restart_pid_alive(old_pid)
                return {
                    "success": False,
                    "error": "shutdown_timeout",
                    "detail": (
                        f"旧进程未在 10s 内退出/释放端口 {port} "
                        f"(pid={old_pid}, still_alive={still_alive})，未拉起新进程"
                    ),
                }
        else:
            # API already down — make sure the port isn't held by an unrelated process.
            if _autostart._is_port_open(port=port):
                return {
                    "success": False,
                    "error": "port_occupied",
                    "detail": f"端口 {port} 被无关进程占用，无法在原端口拉起，已中止",
                }

        # --- 5. Spawn fresh API on the ORIGINAL port (pinned, never drift) ---
        if _autostart._is_port_open(port=port):
            return {
                "success": False,
                "error": "port_occupied",
                "detail": f"端口 {port} 仍被占用，拒绝漂移到随机端口，已中止",
            }
        spawned = (
            _restart_spawn_on_port(_autostart, port, source_root=source_root)
            if source_root else _restart_spawn_on_port(_autostart, port)
        )
        if not spawned.get("success"):
            return spawned

        # --- 6. Poll new API health (≤15s) for new version ---
        # Iteration cap mirrors step 4 — terminate even under a frozen clock.
        new_version = None
        new_deadline = time.monotonic() + 15.0
        _iters = 0
        while time.monotonic() < new_deadline and _iters < 200:
            _iters += 1
            health = _restart_local_get("/api/health", port, timeout=2.0)
            if health is not None:
                new_version = health.get("version")
                break
            time.sleep(0.5)
        if new_version is None:
            return {
                "success": False,
                "error": "health_timeout",
                "detail": f"新进程在 15s 内未通过 /api/health（端口 {port}）",
                "new_pid": spawned.get("new_pid"),
            }

        return {
            "success": True,
            "old_version": old_version,
            "new_version": new_version,
            "old_pid": old_pid,
            "new_pid": spawned.get("new_pid"),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            **({"source_root": source_root, "source_file": preflight["source_file"],
                "source_verification": "preflight_import_only"}
               if preflight else {}),
        }

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def event_list(
        limit: int = 50,
        type: str = "",
        source: str = "",
        entity_id: str = "",
        project_id: str = "",
        fields: str = "compact",
    ) -> dict[str, Any]:
        """List recent events in the system, optionally filtered.

        Default response is a COMPACT projection (marked by view="compact" +
        hint — it is a trimmed view, NOT missing fields): each row keeps
        id/type/source/ts plus a one-line summary derived from the event
        payload. Use fields="all" for full payloads.

        Args:
            limit: Maximum number of events to return, default 50 (compact view
                caps the window at 60 rows; fields="all" is uncapped)
            type: Exact event type, e.g. "task.completed" / "agent.created"
            source: Exact event source, e.g. "team:<id>" / "agent:<id>" / "repository"
            entity_id: Filter to one entity (task / agent / meeting id)
            project_id: Scope to a project — resolves to that project's teams and
                returns their team/agent/task events (empty = no project scoping;
                pass "auto" to use the active project)
            fields: "compact" (default, trimmed projection) / "all" (full rows)

        Returns:
            Event list with event type, source, timestamp and derived summary;
            compact view adds view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        wanted = max(1, int(limit or 1))
        effective = min(wanted, EVENT_COMPACT_CAP) if view == "compact" else wanted
        params: list[str] = [f"limit={effective}"]
        if type:
            params.append(f"type={urllib.parse.quote(type)}")
        if source:
            params.append(f"source={urllib.parse.quote(source)}")
        if entity_id:
            params.append(f"entity_id={urllib.parse.quote(entity_id)}")
        if project_id:
            resolved = _resolve_project_id("" if project_id == "auto" else project_id)
            if not resolved:
                return {"success": False, "error": "未找到活跃项目，请显式提供 project_id"}
            params.append(f"project_id={urllib.parse.quote(resolved)}")
        result = _api_call("GET", f"/api/events?{'&'.join(params)}")
        if view == "all" or not isinstance(result, dict) or "data" not in result:
            return result
        out: dict[str, Any] = {
            "success": result.get("success", True),
            "total": result.get("total"),
            "limit": effective,
            "data": [compact_event_row(e) for e in result.get("data") or []],
            "view": "compact",
            "hint": EVENT_HINT,
        }
        if effective < wanted:
            out["limit_capped"] = (
                f"compact 视图窗口上限 {EVENT_COMPACT_CAP} 条；要更大窗口用 fields='all' 并自行分页"
            )
        return out

    @mcp.tool()
    def find_skill(
        task_description: str = "",
        level: int = 1,
        category: str = "",
        skill_id: str = "",
    ) -> dict[str, Any]:
        """Find ecosystem skills/plugins using a 3-layer progressive loading system.

        Searches a small curated catalog of third-party skills, plugins and
        integration recipes bundled with the OS. It does not list what is installed
        in the current session and does not query a live marketplace.

        Layer 1 (quick recommend): Describe your task and get the top 5 catalog
            entries with one-line descriptions, install commands and match_score;
            entries with match_score 0 did not match the description.
        Layer 2 (category browse): Browse all skills grouped by category
            (memory / code-quality / frontend / security / dev-workflow /
            integration / etc.).
        Layer 3 (full detail): Get complete documentation for a single skill
            including features, OS complement relationship, and variants.

        The `integration` category holds the ecosystem integration recipes
        (GitHub / Slack / Linear / fullstack team); each one says which external
        MCP server to install and which OS tools it pairs with.

        Args:
            task_description: What you want to accomplish (used for level=1 matching).
                              Examples: "frontend ui design", "security audit web app",
                              "data science jupyter", "code review PR".
            level: Discovery depth — 1=quick (default), 2=category, 3=full detail.
            category: Category filter for level=2 (e.g., "frontend", "security",
                      "integration"). Empty string returns all categories.
            skill_id: Skill identifier for level=3 detail lookup
                      (e.g., "vibesec", "superpowers", "claude-mem",
                      "github-integration").

        Returns:
            Dict with level info, results, and hints for deeper exploration.
        """
        from aiteam.mcp.skill_registry import (
            find_skill_category,
            find_skill_detail,
            find_skill_quick,
        )

        if level == 3:
            if not skill_id:
                return {
                    "error": "level=3 requires skill_id parameter.",
                    "hint": "Use level=1 with task_description to discover skill IDs first.",
                }
            return find_skill_detail(skill_id)

        if level == 2:
            return find_skill_category(category)

        if not task_description:
            return {
                "error": "level=1 requires task_description parameter.",
                "hint": "Describe what you want to do, e.g. 'build a secure REST API'.",
            }
        return find_skill_quick(task_description)

    @mcp.tool()
    async def os_config_change(change: str, confirm_token: str = "", user_quote: str = "") -> dict[str, Any]:
        """Change the user's OS installation after the user approved a preview.

        Two calls. First without confirm_token: returns the preview (every
        file, the action, sha256 before and after, the baseline tree and
        branch, warnings) and a confirm_token valid for 10 minutes. Show the
        preview to the user as is and ask. Only after the user agrees, call
        again with the token and the user's own words. The preview is
        recomputed; if anything changed you must preview again. Existing files
        are backed up next to themselves (.bak-aiteam-<time>) before writing,
        and a decision.user_config_write event is recorded. Never pass a token
        the user has not seen the preview for.

        Args:
            change: What to change: sync_installed_copies (installed hook, skill, agent and
                command copies of a source install behind the source tree, notice E11), or
                update_codex_adapter (Codex adapter files from the recorded installation
                source, notice E13; preserves hooks-only mode and requires preview approval)
            confirm_token: Empty for the preview; the token from that preview to apply it
            user_quote: Required when applying: the user's own words approving the preview
        """
        from aiteam.services import config_change

        # Planning reads and hashes files (and, for the Codex adapter, runs git);
        # applying writes them. Both run in a worker thread, as do the HTTP calls
        # that record the result, so the event loop keeps serving other calls.
        session_id = _cc_session_id()
        caller = {"host": _caller_host(), "session_id": session_id, "tool": "os_config_change"}
        try:
            if not confirm_token:
                return await asyncio.to_thread(config_change.preview, change)
            data = await asyncio.to_thread(config_change.apply, change, confirm_token, user_quote)
        except config_change.ConfigChangeError as exc:
            if exc.partial is None:
                return {"success": False, "error": str(exc)}
            # Files may already have changed: record it and say exactly what was done.
            partial = {**exc.partial, **caller}
            event = await asyncio.to_thread(_record_config_write, partial)
            return {"success": False, "error": str(exc), **partial, "event": event,
                    "hint": "Files listed under targets were written; each has its backup next to it."}
        data.update(caller)
        event = await asyncio.to_thread(_record_config_write, data)
        if data.get("notice_key"):
            await asyncio.to_thread(
                _api_call, "POST", f"/api/notices/{urllib.parse.quote(data['notice_key'], safe=':')}/clear",
            )
        return {"success": True, "mode": "applied", **data, "event": event}

    @mcp.tool()
    def model_config_get(usage_days: int = 7) -> dict[str, Any]:
        """Get model governance state: available models (auto-discovered from
        local CC transcripts — the models you actually used), the current
        default startup model (~/.claude/settings.json "model" key), and
        per-model workflow agent usage over the last N days (orchestration
        charter observability: how much fable vs opus the fleet burned).

        Args:
            usage_days: Aggregation window for usage stats (default 7, max 90)
        """
        avail = _api_call("GET", "/api/models/available")
        default = _api_call("GET", "/api/models/default")
        usage = _api_call("GET", f"/api/models/usage?days={usage_days}")
        return {
            "available": avail.get("data") if isinstance(avail, dict) else avail,
            "default": (default.get("data") or {}).get("model", "")
            if isinstance(default, dict)
            else "",
            "usage": usage.get("data") if isinstance(usage, dict) else usage,
        }

    @mcp.tool()
    def model_config_set(model: str) -> dict[str, Any]:
        """Set the default startup model for new CC sessions (writes the
        "model" key in ~/.claude/settings.json; empty string removes the key,
        restoring CC's own default). Takes effect on NEW sessions.

        Args:
            model: Written verbatim to the "model" key without validation: a full
                model ID or a CC alias such as "opus"; "" removes the key.
        """
        return _api_call("PUT", "/api/models/default", {"model": model})
