"""Shared infrastructure for AI Team OS MCP tools.

Contains the HTTP helper, project/team resolvers, and global state
that all tool modules depend on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from aiteam.diagnostics import proxy_snapshot, record_event, response_snapshot, safe_url
from aiteam.mcp._error_recovery import get_business_recovery, get_connection_recovery, get_http_recovery

logger = logging.getLogger(__name__)

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")


def _get_api_port() -> int:
    """Read port from port file. Returns 8000 if file missing or invalid."""
    try:
        return int(open(_PORT_FILE).read().strip())
    except (FileNotFoundError, ValueError):
        return 8000


def _get_api_url() -> str:
    """Return the current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    return f"http://localhost:{_get_api_port()}"


# Module-level alias for backwards compatibility (used in _autostart import guard)
API_URL = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
# Project directory for DB isolation — set by Claude Code environment
PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR", "")

# Process-level project ID — resolved once at startup from cwd → root_path match.
# Safe because each CC session spawns its own MCP server subprocess.
# Set by _init_session_project() after API is ready, before mcp.run().
_session_project_id: str = ""


# ============================================================
# HTTP helper
# ============================================================


def _api_call(
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Unified API call helper using urllib standard library.

    Args:
        method: HTTP method (GET / POST / PUT / DELETE)
        path: API path, e.g., /api/teams
        data: Request body data (used for POST/PUT only)
        extra_headers: Additional headers to merge into the request
            (e.g. {"X-Project-Id": "..."} for ecosystem project scoping).

    Returns:
        API response as a JSON dict
    """
    url = f"{_get_api_url()}{urllib.parse.quote(path, safe='/?&=%')}"
    headers = {"Content-Type": "application/json"}
    if PROJECT_DIR:
        # HTTP headers must be ASCII/latin-1; percent-encode the path so
        # non-ASCII characters (e.g. Chinese directory names) are safe.
        # The API side decodes with urllib.parse.unquote before path matching.
        headers["X-Project-Dir"] = urllib.parse.quote(PROJECT_DIR, safe="/:.-_\\")
    # Stage J: auto-inject session-resolved X-Project-Id (read once at startup
    # via _init_session_project, no recursion risk). Subordinate to extra_headers
    # so callers can still override (e.g. cross-project tools force a different id).
    if _session_project_id:
        headers.setdefault("X-Project-Id", _session_project_id)
    # 归因 v1 §2.4：把当前 CC 会话 id 带给服务端，用来把记账动作里的 author 名解析
    # 到**本会话域内**的那个 agent 行。没有它服务端只能全表按名字找 —— 而 name 在
    # agents 表里不唯一（实测 "Leader" 一个名字 117 行、横跨 79 支队、时间跨度三周），
    # 全表匹配必然跨时重名错绑。服务端拿不到这个头就直接不写边（宁可少一条）。
    session_id = _cc_session_id()
    if session_id:
        headers.setdefault("X-CC-Session-Id", session_id)
    if extra_headers:
        headers.update(extra_headers)
    request_id = uuid4().hex
    headers["X-Aiteam-Request-Id"] = request_id

    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    started = time.monotonic()
    context = {"request_id": request_id, "method": method, "url": safe_url(url)}
    record_event("http.client.started", **context, proxy=proxy_snapshot(url))
    transport: dict[str, Any] = {}

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            transport = response_snapshot(resp)
            result = json.loads(resp.read().decode("utf-8"))
        record_event("http.client.completed", **context, **transport,
                     elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        return result
    except urllib.error.HTTPError as e:
        record_event("http.client.failed", **context, **response_snapshot(e), error_type=type(e).__name__,
                     elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        recovery_info = get_http_recovery(e.code)
        # Upgrade category from business keywords if body carries more context
        business_category = get_business_recovery(error_body)
        result: dict[str, Any] = {
            "success": False,
            "error": f"HTTP {e.code}: {e.reason}",
            "detail": error_body,
            "_error_category": business_category or recovery_info.get("category", "unknown"),
            "_recovery": recovery_info.get("recovery", ""),
        }
        return result
    except urllib.error.URLError as e:
        record_event("http.client.failed", **context, error_type=type(e).__name__,
                     errno=getattr(e.reason, "errno", None),
                     elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        reason_str = str(e.reason)
        recovery_info = get_connection_recovery(reason_str)
        return {
            "success": False,
            "error": f"无法连接到 AI Team OS API ({_get_api_url()}): {e.reason}",
            "hint": "请确保 FastAPI 服务已启动: aiteam serve",
            "_error_category": recovery_info.get("category", "api_unavailable"),
            "_recovery": recovery_info.get("recovery", ""),
        }
    except Exception as e:
        record_event("http.client.failed", **context, **transport, error_type=type(e).__name__,
                     errno=getattr(e, "errno", None),
                     elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        recovery_info = get_connection_recovery(str(e))
        return {
            "success": False,
            "error": f"请求失败: {e!s}",
            "_error_category": recovery_info.get("category", "unknown"),
            "_recovery": recovery_info.get("recovery", ""),
        }


# ============================================================
# Resolvers
# ============================================================


_SESSION_TEAM_NAME_RE = re.compile(r"^session-[0-9a-fA-F]{8}$")


def _cc_session_id() -> str:
    """当前 CC 会话 id —— CC 给每个 MCP server 子进程注入 CLAUDE_CODE_SESSION_ID。

    (实测 CC v2.1.219：`ps eww <mcp-pid>` 可见该变量；每会话一个 MCP 子进程，
    故它在本进程生命周期内恒定。)
    """
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        or os.environ.get("CLAUDE_SESSION_ID", "")
    ).strip()


def _cc_session_id_resolver(ctx: Any = None) -> str | None:
    """CC resolver: the session id CC injects into every MCP subprocess env.

    ``ctx`` is accepted and ignored - the environment is all CC needs. Returns
    None rather than "" when unset, so the dispatcher can tell "not this
    harness" apart from "this harness handed us an empty id".
    """
    return _cc_session_id() or None


# 会话身份解析器表：按序试，首个非空即定。CC 排首位，因为它是唯一零成本判据
# （读两个环境变量，无进程探查、无磁盘 IO），不命中就立刻让位。
#
# 契约三条（填槽位的人必须照做）：
#   1. 签名恒为 ``(ctx: Any = None) -> str | None``；用不上 ctx 的忽略它即可。
#   2. 失败以返回 None 表达，**不得抛异常**——本表的分发是纯循环，一个解析器炸了
#      会连坐所有 MCP 调用。
#   3. 贵的解析器自己负责缓存，本表不代管。
#
# Codex 槽位本期不注册（P0-1 只留接口位）。填它时的判据来自 N1 阶段 A 实证：
#   ① lsof(父进程) 取 sessions/*.jsonl：CLI exec 下唯一命中且确定；多线程宿主下
#      父进程同时开着多份候选 → 歧义；一次性会话下 0 命中。
#   ② rollout 尾部即本次调用："先写后发"成立（落盘早于服务端收到 3~91ms），是多
#      线程下唯一能消歧的线索，但判别力必须来自**本次参数哈希**而非工具名——并发
#      两条线程的尾部都会出现工具名。
#   ③ 宿主 hook 侧记 (session_id, 父进程号) 反查：单进程入口唯一命中；多线程宿主下
#      共用同一父进程 → 退化为集合；一次性会话下是唯一活口。时序上 SessionStart 晚
#      于 MCP 握手，故只能在首次工具调用时刻用，不能在握手时预解析。
# 结构性前提：MCP 服务端进程与会话线程严格 1:1（每起一个线程就 spawn 一个服务端
# 进程），所以身份只需**首次调用惰性解析一次并缓存**，不必每次重算。三线索全失败
# 一律显式降级返回 None，禁止按 cwd 猜。
SESSION_ID_RESOLVERS: tuple[Callable[[Any], str | None], ...] = (
    _cc_session_id_resolver,
)


def resolve_session_id(ctx: Any = None) -> str | None:
    """当前会话 id —— 按 SESSION_ID_RESOLVERS 顺序试，首个非空即返。

    全落空返回 None，调用方据此走"无会话域"分支；宁可少写一条边，也不拿猜出来的
    id 去绑错行（同名 agent 跨队重名，绑错查不出来）。``ctx`` 透传给需要请求上下文
    的 harness，CC 解析器忽略它。
    """
    for resolver in SESSION_ID_RESOLVERS:
        session_id = resolver(ctx)
        if session_id:
            return session_id
    return None


def _team_owned_by_session(team: dict[str, Any], session_id: str) -> bool:
    """该队是否属于本会话（判据同 hook_translator._team_owned_by_session）。

    ① config.owner_session_id 命中（建队时盖的权威归属章）；
    ② 老行兜底：容器队名编码了会话 id 前 8 位（``session-<sid8>``）。
    """
    if not session_id:
        return False
    owner = (team.get("config") or {}).get("owner_session_id")
    if owner:
        return owner == session_id
    return str(team.get("name") or "") == f"session-{session_id[:8]}"


def _is_workflow_team(team: dict[str, Any]) -> bool:
    """workflow per-run 队：一次 Workflow 运行建一支，绝不能当"当前团队"用。"""
    if (team.get("config") or {}).get("kind") == "workflow":
        return True
    return str(team.get("name") or "").startswith("workflow-")


def _is_foreign_session_container(team: dict[str, Any], session_id: str) -> bool:
    """别的会话的 session-<sid8> 容器队（本会话的已在档①里挑走）。"""
    if (team.get("config") or {}).get("kind") == "session":
        return not _team_owned_by_session(team, session_id)
    return bool(_SESSION_TEAM_NAME_RE.match(str(team.get("name") or "")))


def _team_recency_key(team: dict[str, Any]) -> str:
    """按最近活动排序用的键（ISO 串可直接字典序比较）。"""
    return str(team.get("updated_at") or team.get("created_at") or "")


def _newest(teams: list[dict[str, Any]]) -> dict[str, Any]:
    return max(teams, key=_team_recency_key)


def pick_active_team(teams: list[dict[str, Any]], session_id: str, project_id: str) -> dict | None:
    """从活跃队里挑"当前团队"，三档优先级；全落空返回 None。

    档①  本会话的 session 容器队（owner_session_id / session-<sid8> 命中）；
    档②  本项目内既非 workflow-* per-run 队、也非他会话容器队的最新活跃队；
    档③  本项目内最新活跃队（只剩 workflow 队时的兜底）。

    旧实现直接取 ``active_teams[0]``：teams 列表按 created_at 倒序，而活跃队里
    最新的通常是刚起的 workflow-* per-run 队 —— 于是会议 / 团队知识 / 活动查询
    等一切"空参自动用活跃队"的工具全绑到了 workflow 队上（拿错对象）。

    跨项目队一律不入选：宁可返回 None 让调用方显式报错，也不借别项目的队。
    """
    active = [
        t for t in teams
        if isinstance(t, dict) and t.get("id") and t.get("status") == "active"
    ]
    if not active:
        return None

    owned = [t for t in active if _team_owned_by_session(t, session_id)]
    if owned:
        return _newest(owned)

    # 项目内（含 project_id 未标注的历史行）；项目未解析时不设限
    in_scope = [
        t for t in active
        if not project_id or not t.get("project_id") or t.get("project_id") == project_id
    ]
    if not in_scope:
        return None

    plain = [
        t for t in in_scope
        if not _is_workflow_team(t) and not _is_foreign_session_container(t, session_id)
    ]
    if plain:
        return _newest(plain)
    return _newest(in_scope)


def _resolve_team_id(team_id: str) -> str:
    """空参时解析出"当前团队"id；解析不出返回空串。

    优先级见 :func:`pick_active_team`。返回空串时调用方**必须**显式报错
    （现有全部调用点都返回 ``{"success": False, "error": ...}``）——绝不允许
    退回"随便挑一支活跃队"的静默猜测。

    NOTE: This calls _api_call to get context. The context_resolve MCP tool
    in tools/infra.py has the full implementation; this resolver uses a
    lightweight version to avoid circular imports.
    """
    if team_id:
        return team_id
    try:
        teams_data = _api_call("GET", "/api/teams")
        rows = teams_data.get("data") or [] if isinstance(teams_data, dict) else []
        picked = pick_active_team(rows, _cc_session_id(), _session_project_id)
    except Exception:
        logger.warning("Team resolution failed", exc_info=True)
        return ""
    if picked is None:
        logger.info("No resolvable active team for this session/project")
        return ""
    return str(picked["id"])


def _resolve_project_id(project_id: str) -> str:
    """Resolve project_id: explicit param > session constant > empty.

    _session_project_id is set once at startup from cwd matching.
    No dynamic resolution = no recursion risk.
    """
    if project_id:
        return project_id
    return _session_project_id


# ============================================================
# Session init
# ============================================================


def _init_session_project() -> None:
    """Resolve project_id once from cwd at startup. No recursion possible."""
    global _session_project_id
    try:
        projects = _api_call("GET", "/api/projects")
        cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
        # Longest-prefix match — multiple projects can match via prefix
        # (e.g. C:/Users/TUF and C:/Users/TUF/Desktop/AI...); pick the most specific.
        best_p = None
        best_len = -1
        for p in projects.get("data", []):
            rp = (p.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
                best_p = p
                best_len = len(rp)
        if best_p is not None:
            _session_project_id = best_p["id"]
            logger.info("Session project: %s (%s)", best_p.get("name"), best_p["id"][:8])
            return
        logger.info("No project match for cwd=%s", cwd)
    except Exception as e:
        logger.warning("Failed to resolve session project: %s", e)
