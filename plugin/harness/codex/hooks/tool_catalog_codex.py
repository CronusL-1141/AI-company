"""Bounded Codex-only tool hints; never an authorization mechanism.

Descriptions are intentionally short. Tool schemas stay in native discovery.
Local filtering is conservative, not a replacement for the host's effective
configuration (which can also contain per-session overrides).
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

MAX_CATALOG_CHARS = 5000
MAX_CONFIG_BYTES = 1_048_576
SERVER = "ai-team-os"

# Deliberately independent from shared MCP descriptions and other hosts.
CORE_TOOLS = (
    ("context_resolve", "确认当前项目与团队", "Resolve current project and teams"),
    ("unified_search", "搜索任务、memo、报告的短片段", "Search task, memo and report snippets"),
    ("link_query", "查询对象的直接引用关系", "Query direct object references"),
    ("link_trace", "沿引用关系追踪最多两跳", "Trace references up to two hops"),
    ("task_create", "创建项目任务记录", "Create a project task record"),
    ("task_update", "更新任务状态和结果", "Update task status and result"),
    ("task_status", "读取指定任务详情", "Read a task's details"),
    ("task_list_project", "查看项目任务墙", "List the project task wall"),
    ("task_run", "创建团队任务，不启动执行", "Create a team task; does not execute it"),
    ("task_memo_add", "向授权任务追加进展记录", "Append progress to an authorized task"),
    ("task_memo_read", "读取任务memo，长历史可能较大", "Read task memos; long histories can be large"),
    ("task_execution_trace", "查询任务执行轨迹", "Read a task execution trace"),
    ("memory_search", "搜索指定作用域的方向层记忆", "Search direction memories in one scope"),
    ("memory_list", "审阅当前上下文的方向层记忆", "Review direction memories for this context"),
    ("memory_add", "保存跨任务偏好、约束或决策", "Save a lasting preference, constraint or decision"),
    ("memory_invalidate", "将旧方向层记忆标记失效", "Invalidate an outdated direction memory"),
    ("memory_reconcile_candidates", "查找可整理的记忆候选", "Find memory reconciliation candidates"),
    ("memory_reconcile_apply", "应用明确授权的记忆整理", "Apply authorized memory reconciliation"),
    ("report_list", "浏览报告元信息", "List report metadata"),
    ("report_read", "按ID读取报告全文", "Read a report's full text by ID"),
    ("report_save", "保存研究或设计报告", "Save a research or design report"),
    ("team_list", "列出团队", "List teams"),
    ("team_status", "查询团队状态", "Read team status"),
    ("event_list", "查询OS事件记录", "Read OS event records"),
    ("find_skill", "查OS生态技能目录，非本机清单", "Search the OS ecosystem skill catalog"),
    ("model_config_get", "查看OS模型配置", "Read OS model configuration"),
    ("model_config_set", "修改OS模型配置，需授权", "Change OS model configuration with authorization"),
    ("os_health_check", "检查OS健康状态", "Check OS health"),
    ("os_restart_api", "重启OS服务，需操作授权", "Restart the OS service with authorization"),
)
READER_TOOLS = frozenset({
    "context_resolve", "unified_search", "link_query", "link_trace",
    "memory_search", "memory_list", "report_list", "report_read",
    "task_status", "task_list_project", "task_memo_read",
    "task_execution_trace", "os_health_check",
})


def codex_home() -> Path:
    if value := os.environ.get("CODEX_HOME"):
        return Path(value).expanduser()
    here = Path(__file__).resolve()
    # Installed at <codex-home>/hooks/<adapter>/, including custom homes.
    if here.parents[1].name == "hooks":
        return here.parents[2]
    return Path.home() / ".codex"


def _read_config(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("configuration too large")
    return tomllib.loads(raw.decode("utf-8"))


def _filter(names: set[str], config: dict) -> set[str]:
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("invalid MCP table")
    server = servers.get(SERVER, {})
    if not isinstance(server, dict):
        raise ValueError("invalid OS table")
    if "enabled" in server and type(server["enabled"]) is not bool:
        raise ValueError("invalid enabled flag")
    if server.get("enabled") is False:
        return set()
    for key in ("enabled_tools", "disabled_tools"):
        if key not in server:
            continue
        values = server[key]
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("invalid tool list")
        names = names & set(values) if key == "enabled_tools" else names - set(values)
    return names


def _configs(home: Path, payload: dict) -> list[tuple[Path, dict]]:
    paths = [home / "config.toml"]
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and Path(cwd).is_absolute():
        # Conservative intersection: a more local layer cannot re-advertise a
        # name already removed by a parent. This is not native config merging.
        paths.extend(p / ".codex" / "config.toml" for p in reversed(Path(cwd).parents))
        paths.append(Path(cwd) / ".codex" / "config.toml")
    result = []
    for path in dict.fromkeys(paths):
        if path.is_file():
            result.append((path, _read_config(path)))
    role = payload.get("agent_type")
    if isinstance(role, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", role):
        role_paths = [home / "agents" / f"{role}.toml"]
        for config_path, config in result:
            agents = config.get("agents", {})
            declaration = agents.get(role, {}) if isinstance(agents, dict) else {}
            role_file = declaration.get("config_file") if isinstance(declaration, dict) else None
            if isinstance(role_file, str):
                role_paths.append(config_path.parent / Path(role_file).expanduser())
        if isinstance(cwd, str) and Path(cwd).is_absolute():
            role_paths.extend(p / ".codex" / "agents" / f"{role}.toml"
                              for p in (*Path(cwd).parents, Path(cwd)))
        for path in dict.fromkeys(role_paths):
            if path.is_file():
                result.append((path, _read_config(path)))
    return result


def render_catalog(payload: dict, *, audience: str = "main", language: str = "zh") -> str:
    """Return hints filtered by known local policy; never echo config values."""
    if audience not in {"main", "subagent"}:
        return ""
    home = codex_home()
    try:
        configs = _configs(home, payload)
        if not any(SERVER in config.get("mcp_servers", {}) for _, config in configs):
            return ""
        names = {row[0] for row in CORE_TOOLS}
        if audience == "subagent":
            names &= READER_TOOLS
        for _, config in configs:
            names = _filter(names, config)
        if not names:
            return ""
    except (OSError, ValueError, TypeError, UnicodeError):
        # No fallback to an unrestricted catalog on a malformed policy.
        return ""
    if language == "zh":
        header = (
            "[AI Team OS 工具索引 v1]\n"
            "以下仅为名称与用途；先通过当前宿主工具发现核对可用性和参数，再按任务授权调用。"
            "以当前会话实际工具目录为准；本索引不授予权限。历史问题优先 unified_search，"
            "方向层偏好用 memory_search；先看片段，再按需读原文。"
        )
        footer = "其余能力通过工具发现查找；不要一次输出完整工具目录。"
        if audience == "subagent":
            footer += "子 Agent 本索引仅列查询工具；不要重启服务、删除项目/团队或修改模型配置。"
        column = 1
    else:
        header = (
            "[AI Team OS tool index v1]\n"
            "Names and purposes only. Discover current availability and parameters before using a tool "
            "within the task's authorization. The host's actual catalog is authoritative; this index "
            "grants no permissions. Use unified_search for past work, memory_search for direction "
            "memories. Read snippets before requesting originals."
        )
        footer = "Discover other capabilities as needed; do not print the entire catalog."
        if audience == "subagent":
            footer += (
                " This subagent index lists queries only; do not restart services, "
                "delete projects/teams or change model configuration."
            )
        column = 2
    lines = [header]
    for row in CORE_TOOLS:
        if row[0] in names:
            line = f"- {row[0]}: {row[column]}"
            if len("\n".join([*lines, line, footer])) <= MAX_CATALOG_CHARS:
                lines.append(line)
    lines.append(footer)
    return "\n".join(lines)
