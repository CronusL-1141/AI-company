#!/usr/bin/env python3
"""AI Team OS — Session startup bootstrap script.

Executed when SessionStart hook fires:
1. Detect if OS API is reachable
2. If reachable, output Leader briefing (task wall Top3, team status, rule reminders)
3. If not reachable, prompt to start service

Stdout output is injected into Claude's system prompt to guide Leader behavior.
Usage: python -m aiteam.hooks.session_bootstrap
Uses only Python standard library.
"""

import hashlib
import json
import locale
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


API_URL = _get_api_url()

def _api_get(path: str, timeout: float = 2.0):
    """GET request to API; return JSON or None."""
    try:
        req = urllib.request.Request(f"{API_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# 方向记忆（记忆系统 v2 P1）：kind 标签 + 注入截断优先级（constraint 最先保留）。
_MEM_KIND_LABEL = {
    "constraint": "约束/护栏",
    "design": "设计意图",
    "directive": "工作方式",
    "preference": "格式偏好",
}

# 方向记忆注入保险丝（记忆 v2.1，2026-07-31）：3000 字方向层总配额 + 格式开销。
# 语义是**保险丝不是预算**——服务端写入侧已按桶字符配额（1200+1500+300=3000）
# 卡死存储量，正常情况下这里永远不会触发截断。它只兜一种异常：有人绕过 API 直
# 改 DB 把方向层撑爆，简报不至于被记忆节淹没。
# 旧值 900 是"常态截断线"：存储红线允许 16,000 字，注入只给 900，实测 48 条里
# 只有头 2-3 条真到得了会话手上，其余被截成一句"另有 46 条"。
# hook 是纯 stdlib 进程，不 import aiteam 包，故常量在此独立定义（两个 hook 各
# 一份，与 plugin/hooks 逐字节副本同步——I1 机检）。
_MEM_INJECT_FUSE = 3400


def _fetch_direction_memories(
    project_id: str = "", project_dir: str = "", timeout: float = 2.0
) -> list:
    """查有效方向层条目（API 已按 kind 优先级 + 时间倒序）。不可达返回 []（静默）。

    注册项目带 X-Project-Id 直取其 project 桶；未注册目录带 X-Project-Dir 让 API
    按目录指纹推导临时桶（dir:<sha1>），从而只继承 global/user + 本目录桶，不串入
    其他项目的 project 记忆（2026-07-21 串线事故根治）。指纹推导权威唯一在 API 侧，
    hook 只负责把 cwd 传过去——避免把公式复制进 hook 副本造成静默漂移。
    """
    try:
        req = urllib.request.Request(f"{API_URL}/api/memories", method="GET")
        if project_id:
            req.add_header("X-Project-Id", project_id)
        elif project_dir:
            import urllib.parse as _up
            req.add_header("X-Project-Dir", _up.quote(project_dir, safe="/:.-_\\"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("data", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _sanitize_inline(text: str) -> str:
    """注入渲染前的单行化清洗（审查 major：memo/记忆内容含换行可伪造
    『## 章节头』污染其他 agent 的注入上下文）。折叠一切空白为单空格。"""
    return " ".join((text or "").split())


def _render_direction_memories(items: list, budget: int = _MEM_INJECT_FUSE) -> list:
    """渲染方向层条目；超保险丝才按 kind 优先级截断并注明剩余条数（正常永不触发）。"""
    if not items:
        return []
    header = "=== 方向记忆（团队共享·所有派出 agent 继承） ==="
    lines = [header]
    used = len(header)
    truncated = 0
    stop = False
    for m in items:
        if stop:
            truncated += 1
            continue
        content = _sanitize_inline(m.get("content") or "")
        if not content:
            continue
        label = _MEM_KIND_LABEL.get(m.get("kind", "preference"), m.get("kind", ""))
        entry = f"  [{label}] {content}"
        if used + len(entry) > budget:
            stop = True
            truncated += 1
            continue
        lines.append(entry)
        used += len(entry)
    if truncated:
        lines.append(f"  …另有 {truncated} 条见 memory_list（按 kind 优先级已截断）")
    lines.append("")
    return lines


def _resolve_project_root() -> "Path | None":
    """Resolve the project root directory from install_path.txt or package location fallback."""
    install_info_file = Path.home() / ".claude" / "data" / "ai-team-os" / "install_path.txt"
    if install_info_file.exists():
        try:
            candidate = Path(install_info_file.read_text(encoding="utf-8").strip())
            if candidate.is_dir() and (candidate / ".git").exists():
                return candidate
        except Exception:
            pass

    # Fallback: infer from package location (src/aiteam/hooks/ -> src/aiteam/ -> src/ -> project_root/)
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / ".git").exists():
        return candidate

    return None



_NOTICE_HOST = "cc"


@lru_cache(maxsize=1)
def _system_language() -> str:
    """Read system preferences once; do not guess unsupported host config keys."""
    value = ""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=0.2, check=False,
            )
            match = re.search(r'^\s*"?([a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]+)*)"?\s*,?\s*$',
                              result.stdout, re.MULTILINE)
            if result.returncode == 0 and match:
                value = match.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
    if not value:
        value = next((os.environ[key] for key in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG")
                      if os.environ.get(key)), "")
    if not value:
        try:
            value = locale.getlocale()[0] or "en"
        except (ValueError, TypeError):
            value = "en"
    return "zh" if re.split(r"[-_.:@]", value.lower())[0] == "zh" else "en"


def _claim_notice(payload: dict) -> bool:
    """Atomically claim one notice per host/session, across hook subprocesses."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        # An unknown identity must never suppress unrelated sessions.
        return payload.get("source", "startup") not in ("resume", "compact")
    try:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    except UnicodeError:
        return False
    directory = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    directory = directory / "ai-team-os" / "release-notices" / _NOTICE_HOST
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(directory / digest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        return True
    except OSError:
        # No durable claim means no popup; never block startup or repeat it on resume.
        return False


def _notice_instruction(release: dict) -> str:
    context = release.get("additional_context")
    if isinstance(context, str) and 0 < len(context) < 8192:
        return context
    notice = release["notice"]
    if release.get("language") == "zh":
        return "用户已看到更新提醒：" + notice + "\n仅提醒；用户要求更新后再按其安装方式操作。"
    return "The user has seen this update notice: " + notice + "\nNotify only; update after the user requests it."


def _valid_release(release: object) -> bool:
    if not isinstance(release, dict) or release.get("status") != "update_available":
        return False
    notice = release.get("notice")
    return (isinstance(notice, str) and 0 < len(notice) < 256
            and not any(character in notice for character in "\r\n")
            and "http://" not in notice and "https://" not in notice)


def _cc_settings_language(cwd: str) -> str | None:
    paths = []
    if cwd:
        paths.extend(Path(cwd) / ".claude" / name for name in ("settings.local.json", "settings.json"))
    paths.append(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "settings.json")
    for path in paths:
        try:
            if path.stat().st_size > 65536:
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            value = document.get("language") if isinstance(document, dict) else None
            if isinstance(value, str) and value.strip():
                value = value.strip().lower()
                return "zh" if (value.startswith("zh") or "chinese" in value or "中文" in value) else "en"
        except (OSError, ValueError):
            pass
    return None


@lru_cache(maxsize=16)
def _notice_language(cwd: str = "") -> str:
    fallback = _cc_settings_language(cwd) or _system_language()
    query = urllib.parse.urlencode({"host": "cc", "cwd": cwd, "fallback_language": fallback})
    result = _api_get("/api/settings/language?" + query, timeout=0.5)
    if isinstance(result, dict) and result.get("effective") in ("zh", "en"):
        return result["effective"]
    return fallback


def _check_for_updates(payload: dict | None = None) -> dict | None:
    """Report a formal release; never mutate a checkout during session startup."""
    payload = payload or {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    query = urllib.parse.urlencode({
        "host": "cc", "cwd": cwd, "fallback_language": _notice_language(cwd),
        "installation": "cc-plugin" if os.environ.get("CLAUDE_PLUGIN_ROOT") else "cc-source",
    })
    release = _api_get("/api/releases/latest?" + query, timeout=1.5)
    if _valid_release(release) and _claim_notice(payload):
        return release
    return None


def _startup_output(briefing: str, compact: str, release: dict | None) -> str:
    """Keep one stdout document when using the native user-visible Hook message."""
    context = briefing + compact
    if not release:
        return context
    return json.dumps({
        "systemMessage": release["notice"],
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context + "\n" + _notice_instruction(release),
        },
    }, ensure_ascii=False)


_DISMISSED_PROJECTS_FILE = Path.home() / ".claude" / "data" / "ai-team-os" / "dismissed_projects.json"


def _normalize_cwd(cwd: str) -> str:
    """Normalize a path for comparison: resolve, lowercase, forward slashes."""
    return str(Path(cwd).resolve()).replace("\\", "/").lower()


def _load_dismissed_projects() -> list[str]:
    """Load the list of dismissed cwd paths from the dismissed_projects.json file."""
    try:
        if _DISMISSED_PROJECTS_FILE.exists():
            data = json.loads(_DISMISSED_PROJECTS_FILE.read_text(encoding="utf-8"))
            return data.get("dismissed", [])
    except Exception:
        pass
    return []


def _check_project_registration(api_url: str, cwd: str) -> tuple[bool, bool, dict]:
    """Check if the current cwd is registered as an OS project.

    Args:
        api_url: Base API URL
        cwd: Current working directory path

    Returns:
        Tuple of (is_registered, is_dismissed, project_info)
        - is_registered: True if cwd matches a project in the OS
        - is_dismissed: True if user previously dismissed registration for this cwd
        - project_info: Project dict if registered, empty dict otherwise
    """
    cwd_norm = _normalize_cwd(cwd)

    # Check dismissed list first (no API call needed)
    dismissed = _load_dismissed_projects()
    is_dismissed = cwd_norm in dismissed

    # Call /api/context/resolve with auto_create=false to check registration
    try:
        payload = json.dumps({"cwd": cwd, "auto_create": False}).encode("utf-8")
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            project_id = result.get("project_id") or (result.get("project", {}) or {}).get("id", "")
            if project_id:
                project_info = result.get("project") or {"id": project_id}
                return True, is_dismissed, project_info
    except Exception:
        # API unreachable or endpoint missing — fall back to projects list matching
        pass

    return False, is_dismissed, {}


# Briefings the permission-denied hook files on its own record a denial; they are
# not decisions waiting on the user. That hook tags them with this value
# (permission_denied_recovery._BRIEFING_TAG); rows filed before it did carry only
# its fixed title prefix, so the prefix check covers those existing rows.
_AUTO_BRIEFING_TAG = "auto:permission-denied"
_AUTO_BRIEFING_TITLE_PREFIX = "Agent denied:"


def _pending_decisions(payload: object, project_id: str) -> list:
    """Real pending decisions for this project from GET /api/leader-briefings.

    The route answers {"items": [...], "total": n}. The request carries no project
    header, so every project's rows come back; keep this project's rows plus the
    unstamped ones, the same scope the route applies for a project-scoped caller.
    """
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tags = row.get("tags")
        if isinstance(tags, list) and _AUTO_BRIEFING_TAG in tags:
            continue
        if str(row.get("title") or "").startswith(_AUTO_BRIEFING_TITLE_PREFIX):
            continue
        if (row.get("project_id") or "") not in ("", project_id):
            continue
        kept.append(row)
    return kept


def _build_unregistered_briefing(cwd: str, is_dismissed: bool) -> str:
    """未注册目录的极简简报（2026-07-21 串线事故根治）。

    只输出：① 身份说明 + 注册/忽略引导；② 合法全局纪律（global/user 桶）+ 本目录
    指纹临时桶记忆。**刻意不输出** 团队列表、Leader 规则、
    任务墙、自动唤醒/loop、skills/模板——那些是注册项目工作台内容，对无关目录是噪音
    （事故实录：未注册目录的会话被灌入其他项目的框架文字）。"""
    lines = []
    lines.append("[AI Team OS] 当前目录未注册为 OS 项目 — 极简简报")
    lines.append("")
    if not is_dismissed:
        dir_name = Path(cwd).name or cwd
        lines.append("⚠️ 当前目录未注册到 AI Team OS 项目系统：")
        lines.append(f"   {cwd}")
        lines.append("")
        lines.append("注册后可用任务墙/会议/报告/Dashboard 与项目隔离；不注册则本会话")
        lines.append("仅继承全局纪律 + 本目录的临时记忆，不参与任何团队工作台。")
        lines.append("")
        lines.append(f"→ 用户说\"注册\"/\"是\"/\"好\" → 执行: project_create(name='{dir_name}', root_path='{cwd}')")
        lines.append("→ 用户说\"不用\"/\"不注册\"/\"不要\" → 执行: dismiss_project_registration(cwd='" + cwd + "')")
        lines.append("")
    # 方向记忆：global/user 真全局纪律 + 本目录指纹临时桶（API 按 X-Project-Dir 推导）。
    # 只继承这些——绝不含其他项目的 project 记忆。API 不可达则静默为空。
    try:
        mem_items = _fetch_direction_memories(project_dir=cwd)
        lines.extend(_render_direction_memories(mem_items, budget=_MEM_INJECT_FUSE))
    except Exception:
        pass
    return "\n".join(lines)


def _build_briefing() -> str:
    """Build Leader briefing (registered project) or minimal briefing (unregistered dir)."""
    # 0. Resolve cwd for project matching
    cwd = os.getcwd().replace("\\", "/")

    # Parallel fetch: projects, teams, briefings (task-wall needs project_id first)
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_projects = pool.submit(_api_get, "/api/projects")
        f_teams = pool.submit(_api_get, "/api/teams")
        f_briefings = pool.submit(_api_get, "/api/leader-briefings?status=pending")
        projects_data = f_projects.result()
        teams_data = f_teams.result()
        briefings_early = f_briefings.result()

    # Resolve matched project: first try /api/context/resolve, then fall back to list matching
    is_registered, is_dismissed, reg_project_info = _check_project_registration(API_URL, cwd)

    # If context/resolve didn't confirm registration, fall back to already-fetched projects list
    matched_project_id = ""
    if is_registered:
        matched_project_id = (reg_project_info.get("id") or "")
    elif projects_data and projects_data.get("data"):
        # Longest-prefix match — pick most specific project when several match
        best_proj = None
        best_len = -1
        cwd_lower = cwd.rstrip("/").lower()
        for proj in projects_data["data"]:
            rp = (proj.get("root_path") or "").replace("\\", "/").rstrip("/")
            if rp and cwd_lower.startswith(rp.lower()) and len(rp) > best_len:
                best_proj = proj
                best_len = len(rp)
        if best_proj is not None:
            is_registered = True
            matched_project_id = best_proj.get("id", "")

    # 未注册目录：极简简报，就此返回——不灌团队/规则/任务墙/唤醒/skills 等工作台内容。
    if not is_registered:
        return _build_unregistered_briefing(cwd, is_dismissed)

    # ===== 已注册项目：完整 Leader 简报 =====
    lines = []
    lines.append("[AI Team OS] Session启动 — Leader简报")
    lines.append("")

    # Fetch task-wall once (used for both top5 and in-progress sections)
    wall_data = None
    if matched_project_id:
        wall_data = _api_get(f"/api/projects/{matched_project_id}/task-wall?limit=20&include_completed=false")

    # 1. Team status
    if teams_data and teams_data.get("data"):
        teams = teams_data["data"]
        active = [t for t in teams if t.get("status") == "active"]
        completed = [t for t in teams if t.get("status") == "completed"]
        lines.append(f"团队: {len(active)}个活跃, {len(completed)}个已完成")
        for t in active:
            lines.append(f"  - {t['name']} (active)")
    else:
        lines.append("团队: 暂无")

    lines.append("")

    # 2. Top tasks from task wall (single fetched result reused below)
    if wall_data and wall_data.get("wall"):
        wall = wall_data["wall"]
        pending = []
        for horizon in ["short", "mid", "long"]:
            for task in wall.get(horizon, []):
                pending.append(task)
        pending.sort(key=lambda t: t.get("score", 0), reverse=True)
        if pending:
            lines.append("任务墙Top5:")
            for t in pending[:5]:
                priority = t.get("priority", "medium")
                horizon = t.get("horizon", "mid")
                score = t.get("score", 0)
                lines.append(f"  [{priority}/{horizon}] {t['title']} (score:{score:.1f})")
        else:
            lines.append("任务墙: 无待办任务")
        lines.append("")

        stats = wall_data.get("stats", {})
        if stats:
            lines.append(
                f"统计: 总{stats.get('total', 0)}任务, "
                f"已完成{stats.get('completed_count', 0)}, "
                f"待办{stats.get('by_status', {}).get('pending', 0)}"
            )
            lines.append("")

    # 3. Rule reminders — top 5 critical rules only (full rules: GET /api/system/rules)
    lines.append("=== Leader核心规则 (Top5) ===")
    lines.append(
        "1. 统筹优先: 几次工具调用能做完的自己做；"
        "多文件实施、长时间调试这类会占住统筹面的活派给成员，派出即自动收编"
    )
    lines.append("2. 派出后不空等: 继续领下一任务并行推进（最多3方向）；任务墙空时自行判断，不为有事干而找活")
    lines.append("3. 自主决策: 战术决策（任务分配/实施方式）自主做主；战略决策（项目方向/重大架构）才请示用户")
    lines.append(
        "4. 进度保护: 有了可交接的进展（改完一处、跑出验证结果、得出结论）就用task_memo_add记录；"
        "同一方法失败3次必须换思路或上报"
    )
    lines.append("5. 上下文: [CONTEXT WARNING]时保存进度；用户回来时先汇报阶段总结+待决事项")
    lines.append("→ 完整规则: GET /api/system/rules")
    lines.append("")

    # 3.5 方向记忆节（记忆系统 v2 P1）：有效方向层条目按 kind 分组注入；API 不可达静默跳过
    try:
        mem_items = _fetch_direction_memories(matched_project_id)
        lines.extend(_render_direction_memories(mem_items, budget=_MEM_INJECT_FUSE))
    except Exception:
        pass

    # In-progress task reminders (reuse already-fetched wall_data — no extra API call)
    if wall_data and wall_data.get("wall"):
        in_progress = []
        for horizon in ["short", "mid", "long"]:
            for task in wall_data["wall"].get(horizon, []):
                status = task.get("status", "")
                if status in ("in_progress", "running"):
                    in_progress.append(task)
        if in_progress:
            lines.append("=== 进行中任务 ===")
            for t in in_progress:
                assignee = t.get("assigned_to", "未分配")
                lines.append(f"  - {t['title']} (分配: {assignee})")
            lines.append("→ 请检查这些任务是否需要更新状态或添加memo")
            lines.append("")

    # 4. Pending Leader Briefings (reuse already-fetched briefings_early)
    items = _pending_decisions(briefings_early, matched_project_id)
    if items:
        lines.append(f"=== Leader简报: {len(items)}个待决事项 ===")
        for b in items[:5]:
            lines.append(f"  [{b.get('urgency') or 'medium'}] {_sanitize_inline(str(b.get('title') or ''))}")
            if b.get("recommendation"):
                lines.append(f"    建议: {_sanitize_inline(str(b['recommendation']))[:60]}")
        lines.append("→ 用户介入时请先汇报以上待决事项，使用 briefing_list 查看详情")
        lines.append("")

    # 5. Auto-wake instruction (v2: event-driven + dynamic interval, replaces 30min cron)
    # 唤醒体系 v2，见 docs/wake-loop-v2-design.md §5：不再指导建每 30 分钟固定 cron，
    # 改为跑一次 /loop（动态间隔）；OS 维护提示由 ~/.claude/loop.md 承载。
    lines.append("=== 自动唤醒 ===")
    # ③ 催办类改条件句：无条件"请运行 /loop"对专注单一任务的会话是错误指令（实证被
    # 系统性无视）。仅对承担统筹职责的会话建议 /loop，单任务会话可忽略。
    lines.append("若本会话承担统筹职责（Leader/协调），建议运行一次 /loop；专注单一任务的会话可忽略。")
    lines.append("（不带间隔=动态：每轮自选 1-60 分钟延迟，有活收紧、空闲拉长）每轮做什么见 ~/.claude/loop.md。")
    lines.append("")

    return "\n".join(lines)


def _fetch_compact_checkpoint(session_id: str) -> str:
    """取本会话最近一条压缩检查点的可注入文本；没有就返回空串。

    刻意不做兜底文案：没有检查点时什么都不注入，绝不拿占位符占用户的上下文。
    """
    if not session_id:
        return ""
    try:
        data = _api_get(f"/api/hooks/compact-checkpoint?session_id={session_id}")
        if isinstance(data, dict) and data.get("found"):
            return str(data.get("text") or "")
    except Exception:
        pass
    return ""


def main() -> None:
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    # Read session info from stdin
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        session_info = json.loads(raw) if raw.strip() else {}
    except Exception:
        session_info = {}

    # Check if API is reachable (1 retry with short sleep — keeps us under 3s hook timeout)
    health = _api_get("/api/teams")
    if health is None:
        time.sleep(0.3)
        health = _api_get("/api/teams")

    if health is not None:
        # API reachable -> output briefing to stdout (injected into Claude context)
        briefing = _build_briefing()

        # 压缩恢复路径（Q6 裁定 A）：SessionStart 的 source 有五种取值
        # startup/resume/clear/compact/fork，只有 compact 这一种意味着"上一轮
        # 上下文刚被压掉"。此时把 PreCompact 存下的 OS 侧作战态原样递回去——
        # CC 自己的 compact_summary 压缩后本来就在模型上下文里，OS 该补的是模型
        # 没有的那半边（在飞 agent / 未完成任务 / 待裁决项）。
        compact_block = ""
        if session_info.get("source") == "compact":
            compact_block = _fetch_compact_checkpoint(session_info.get("session_id", ""))

        sys.stdout.write(_startup_output(briefing, compact_block, _check_for_updates(session_info)))

        sys.stderr.write(
            f"[aiteam-bootstrap] AI Team OS API reachable at {API_URL}\n"
            f"[aiteam-bootstrap] session_id={session_info.get('session_id', 'unknown')}\n"
            f"[aiteam-bootstrap] briefing injected ({len(briefing)} chars)"
            + (f" + compact checkpoint ({len(compact_block)} chars)" if compact_block else "")
            + "\n"
        )
    else:
        # API not reachable
        # D3 阶段D 止血（审计 M50）：不再引导手动起第二个 uvicorn 实例——那会与
        # MCP 自启实例并存，导致双 reaper/重复唤醒。API 随 MCP 启动自动拉起。
        sys.stdout.write(
            "[AI Team OS] API未启动。API 会随 MCP server 自动拉起：\n"
            "1) 重启 Claude Code（推荐，/mcp 确认 ai-team-os 已连接）；\n"
            "2) 或在已连接的会话里调用 MCP 工具 os_restart_api。\n"
            "请勿手动运行 uvicorn 起第二个实例（会与自启实例并存导致重复唤醒）。\n"
        )
        sys.stderr.write(f"[aiteam-bootstrap] AI Team OS API not reachable at {API_URL}\n")


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
