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

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_SUBAGENT_MARKER_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os", "subagent_sessions"
)
_SUBAGENT_MARKER_TTL_SECONDS = 24 * 3600


def _cleanup_stale_subagent_markers() -> None:
    """Delete sub-agent session markers older than 24h (best-effort, silent)."""
    try:
        if not os.path.isdir(_SUBAGENT_MARKER_DIR):
            return
        cutoff = time.time() - _SUBAGENT_MARKER_TTL_SECONDS
        for name in os.listdir(_SUBAGENT_MARKER_DIR):
            path = os.path.join(_SUBAGENT_MARKER_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except Exception:
        pass


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
CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "plugin" / "config"

# Update check cooldown: only check once every 24 hours
_UPDATE_CHECK_COOLDOWN_SECS = 24 * 60 * 60
_UPDATE_CHECK_STATE_FILE = Path.home() / ".claude" / "data" / "ai-team-os" / "last_update_check.json"


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


def _render_direction_memories(items: list, budget: int = 900) -> list:
    """把方向层条目渲染成注入文本；超预算按 kind 优先级截断并注明剩余条数。"""
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


def _load_team_config() -> dict | None:
    """Load team default configuration; return None on failure."""
    config_path = CONFIG_DIR / "team-defaults.json"
    try:
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _build_auto_team_instructions(config: dict) -> list[str]:
    """Generate auto team creation instruction text based on config."""
    if not config.get("auto_create_team"):
        return []

    enabled_members = [m for m in config.get("permanent_members", []) if m.get("enabled")]
    if not enabled_members:
        return []

    # CC v2.1.219 现状（2026-07-25 核对工具面）：TeamCreate 工具已不存在，Agent 的
    # team_name 参数标注 "Deprecated; ignored. The session has a single implicit team."
    # ——每个会话自带唯一隐式团队（CC 自建 ~/.claude/teams/session-<内部id>/），
    # 派 agent 即入队。故不再指示建队/传队名，只派成员；OS 侧归属由
    # hook_translator 的 SubagentStart 自动收编负责。
    lines = []
    lines.append("")
    lines.append("=== 常驻成员派发指引 ===")
    lines.append("本会话已自带隐式团队，直接派成员即可（无需建队、无需传 team_name）：")
    for i, m in enumerate(enabled_members, start=1):
        role = m["role"]
        lines.append(
            f"{i}. Agent(name='{m['name']}', "
            f"subagent_type='{role}', prompt='待命，等待Leader分配任务')"
        )
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


def _get_remote_commit(project_root: "Path") -> str:
    """Fetch from origin and return the short hash of the remote HEAD (main or master)."""
    subprocess.run(
        ["git", "fetch", "--quiet", "origin"],
        cwd=str(project_root),
        capture_output=True,
        timeout=5,
    )
    for branch in ("origin/main", "origin/master"):
        r = subprocess.run(
            ["git", "rev-parse", "--short", branch],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def _get_local_commit(project_root: "Path") -> str:
    """Return the short hash of the local HEAD."""
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _run_background_update(project_root: "Path") -> None:
    """Spawn a background process that pulls the latest code and reinstalls.

    The background process writes its result to a status file so the next
    SessionStart can report success or failure.
    """
    status_file = _UPDATE_CHECK_STATE_FILE.parent / "bg_update_status.json"

    # Build the update script as a single Python command string so we do not
    # need a separate helper file on disk.
    update_script = r"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

project_root = sys.argv[1]
status_file = sys.argv[2]

def run(args, **kw):
    return subprocess.run(args, cwd=project_root, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=30, **kw)

errors = []

# 1. git pull
r = run(["git", "pull", "--ff-only"])
if r.returncode != 0:
    errors.append(f"git pull failed: {r.stderr.strip()}")

# 2. pip install -e .
if not errors:
    r = run([sys.executable, "-m", "pip", "install", "-e", ".", "-q"])
    if r.returncode != 0:
        errors.append(f"pip install failed: {r.stderr.strip()}")

# Get new HEAD commit hash
r2 = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"],
    cwd=project_root,
    capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=5,
)
new_commit = r2.stdout.strip() if r2.returncode == 0 else "unknown"

result = {
    "completed_at": time.time(),
    "success": len(errors) == 0,
    "new_commit": new_commit,
    "errors": errors,
}
Path(status_file).write_text(json.dumps(result), encoding="utf-8")
"""

    try:
        subprocess.Popen(
            [sys.executable, "-c", update_script, str(project_root), str(status_file)],
            # Detach from the parent process completely so it survives hook timeout
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:
        pass


def _check_for_updates() -> str | None:
    """Check if a newer version is available on git remote; auto-update in background.

    Uses a 24-hour cooldown to avoid triggering on every session start.
    """
    _UPDATE_CHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    # --- Report result of a previously-started background update ---
    bg_status_file = _UPDATE_CHECK_STATE_FILE.parent / "bg_update_status.json"
    if bg_status_file.exists():
        try:
            bg = json.loads(bg_status_file.read_text(encoding="utf-8"))
            bg_status_file.unlink(missing_ok=True)
            if bg.get("success"):
                new_commit = bg.get("new_commit", "unknown")
                _UPDATE_CHECK_STATE_FILE.write_text(
                    json.dumps({"last_checked": time.time(), "notice": None}),
                    encoding="utf-8",
                )
                return f"[OS] 已自动更新到最新版本 (commit: {new_commit})"
            else:
                errs = "; ".join(bg.get("errors", ["unknown error"]))
                return f"[OS] 自动更新失败: {errs}"
        except Exception:
            pass

    # --- Cooldown check ---
    try:
        if _UPDATE_CHECK_STATE_FILE.exists():
            state = json.loads(_UPDATE_CHECK_STATE_FILE.read_text(encoding="utf-8"))
            last_checked = state.get("last_checked", 0)
            if time.time() - last_checked < _UPDATE_CHECK_COOLDOWN_SECS:
                return state.get("notice")
    except Exception:
        pass

    # --- Locate project root ---
    project_root = _resolve_project_root()

    notice: str | None = None

    if project_root is not None:
        # Run the blocking git fetch+compare in a thread with a hard 2s timeout
        # so it never delays the bootstrap past the hook timeout.
        def _check_git_update(root: "Path") -> "str | None":
            try:
                local_commit = _get_local_commit(root)
                remote_commit = _get_remote_commit(root)
                if local_commit and remote_commit and local_commit != remote_commit:
                    # 仅当远端严格领先时才更新。本地领先/分叉（开发机常态：已提交未推送）
                    # 不能触发——否则每次会话都误报"检测到新版本"并空跑 git pull。
                    anc = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", remote_commit, "HEAD"],
                        cwd=str(root),
                        capture_output=True,
                        timeout=3,
                    )
                    if anc.returncode == 0:
                        return None  # 远端提交已在本地历史里 → 本地不落后，无需更新
                    _run_background_update(root)
                    return (
                        f"[OS] 检测到新版本 (local: {local_commit} → remote: {remote_commit})，"
                        "正在后台自动更新，下次启动时生效。"
                    )
            except Exception:
                pass
            return None

        try:
            with ThreadPoolExecutor(max_workers=1) as _ex:
                future = _ex.submit(_check_git_update, project_root)
                notice = future.result(timeout=2.0)
        except (FuturesTimeoutError, Exception):
            # Timed out or failed — skip update check silently
            pass

    try:
        _UPDATE_CHECK_STATE_FILE.write_text(
            json.dumps({"last_checked": time.time(), "notice": notice}),
            encoding="utf-8",
        )
    except Exception:
        pass

    return notice


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


def _check_teams_dir_cleanup() -> str | None:
    """Scan ~/.claude/teams/ and warn if too many team directories accumulate."""
    teams_dir = Path.home() / ".claude" / "teams"
    if not teams_dir.exists():
        return None
    try:
        team_dirs = [p for p in teams_dir.iterdir() if p.is_dir()]
        count = len(team_dirs)
        if count > 10:
            # TeamDelete 工具在 CC v2.1.219 已不存在（2026-07-25 工具面核对），
            # 隐式团队目录由 CC 自建自管——只提示手动清理。
            return (
                f"[OS提醒] 检测到 {count} 个历史会话团队目录，可手动清理："
                "rm -rf ~/.claude/teams/<旧目录>（当前会话的目录勿删）"
            )
    except Exception:
        pass
    return None


def _build_unregistered_briefing(cwd: str, is_dismissed: bool) -> str:
    """未注册目录的极简简报（2026-07-21 串线事故根治）。

    只输出：① 身份说明 + 注册/忽略引导；② 合法全局纪律（global/user 桶）+ 本目录
    指纹临时桶记忆。**刻意不输出** 团队列表、历史团队目录清理提醒、Leader 规则、
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
        lines.extend(_render_direction_memories(mem_items, budget=900))
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

    # ===== 已注册项目：完整 Leader 简报（下方内容与旧版逐字一致，仅前移了注册判定）=====
    lines = []
    lines.append("[AI Team OS] Session启动 — Leader简报")
    lines.append("")

    # Team directory cleanup reminder
    cleanup_notice = _check_teams_dir_cleanup()
    if cleanup_notice:
        lines.append(cleanup_notice)
        lines.append("")

    # Update availability notice (24h cooldown, non-blocking)
    update_notice = _check_for_updates()
    if update_notice:
        lines.append(f"[UPDATE] {update_notice}")
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
        "1. 专注统筹: 实施工作委派成员，自己只协调。"
        "派发方式（Agent/Workflow）自判，无预设倾向"
        "（无需建队/传 team_name，OS 自动收编追踪）"
    )
    lines.append("2. 绝不空等: 派出Agent后立即领取下一任务并行推进（最多3方向）。任务墙空时组织会议")
    lines.append("3. 自主决策: 战术决策（任务分配/实施方式）自主做主；战略决策（项目方向/重大架构）才请示用户")
    lines.append("4. 进度保护: 每2个操作用task_memo_add记录进展；同一方法失败3次必须换思路或上报")
    lines.append("5. 上下文: [CONTEXT WARNING]时保存进度；用户回来时先汇报阶段总结+待决事项")
    lines.append("→ 完整规则23条: GET /api/system/rules")
    lines.append("")

    # 3.5 方向记忆节（记忆系统 v2 P1）：有效方向层条目按 kind 分组注入；API 不可达静默跳过
    try:
        mem_items = _fetch_direction_memories(matched_project_id)
        lines.extend(_render_direction_memories(mem_items, budget=900))
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
    briefings = briefings_early
    if briefings and briefings.get("data"):
        items = briefings["data"]
        if items:
            lines.append(f"=== Leader简报: {len(items)}个待决事项 ===")
            for b in items[:5]:
                lines.append(f"  [{b.get('urgency','medium')}] {b['title']}")
                if b.get('recommendation'):
                    lines.append(f"    建议: {b['recommendation'][:60]}")
            lines.append("→ 用户介入时请先汇报以上待决事项，使用 briefing_list 查看详情")
            lines.append("")

    # 5. Auto-wake instruction (v2: event-driven + dynamic interval, replaces 30min cron)
    # 唤醒体系 v2，见 docs/wake-loop-v2-design.md §5：不再指导建每 30 分钟固定 cron，
    # 改为跑一次 /loop（动态间隔）；OS 维护提示由 ~/.claude/loop.md 承载。
    lines.append("=== 自动唤醒（v2 事件驱动 + 动态间隔）===")
    # ③ 催办类改条件句：无条件"请运行 /loop"对专注单一任务的会话是错误指令（实证被
    # 系统性无视）。仅对承担统筹职责的会话建议 /loop，单任务会话可忽略。
    lines.append("若本会话承担统筹职责（Leader/协调），建议运行一次：/loop；专注单一任务的会话可忽略。")
    lines.append(
        "（不带间隔=动态：Claude 每轮自选 1-60 分钟延迟，有活收紧、空闲拉长，趋近零 token）"
    )
    lines.append("OS 维护提示已随安装写入 ~/.claude/loop.md，每轮 loop 会据此工作：")
    lines.append(
        "  1.有 busy agent / running run 在飞 → 武装事件 watcher"
        "（bash scripts/os-watch.sh <session_id> <team_id> & 后台），处理其产出后按需继续；"
    )
    lines.append("  2.有待办 → 自主推进常规任务，需用户决策的用 briefing_add 记录；")
    lines.append("  3.无待办 → 主动行动：研究竞品/新技术、组织会议讨论规划、审查代码、优化功能；")
    lines.append("  4.收到 CONTEXT CRITICAL → 保存进度到记忆，提醒开新 session。")
    lines.append("不要再用 CronCreate 建每 30 分钟固定唤醒（v1 已退役）。")
    lines.append("如有待决简报，在用户首次发言时汇报。")
    lines.append("")

    lines.append("请阅读CLAUDE.md获取项目核心约束，然后查看任务墙决定下一步工作。")
    lines.append("")
    lines.append("=== 可用Skills ===")
    lines.append("- /meeting-facilitate — 需要组织多Agent讨论时使用")
    lines.append("- /meeting-participate — 被邀请参加会议时使用")

    # Available Agent template list
    agents_dir = os.path.join(os.path.expanduser("~"), ".claude", "agents")
    if os.path.isdir(agents_dir):
        templates = [f.replace(".md", "") for f in os.listdir(agents_dir) if f.endswith(".md")]
        if templates:
            groups = {}
            for t in sorted(templates):
                prefix = t.split("-")[0] if "-" in t else "other"
                groups.setdefault(prefix, []).append(t)
            lines.append("")
            lines.append(
        "=== 可用Agent模板 ===（仅供参考：不合适可用 general-purpose+自定义 prompt，"
        "或直接改 plugin/agents/*.md）"
    )
            for prefix, names in sorted(groups.items()):
                lines.append(f"  {prefix}: {', '.join(names)}")

    # Auto team creation instructions
    team_config = _load_team_config()
    if team_config:
        lines.extend(_build_auto_team_instructions(team_config))

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

    _cleanup_stale_subagent_markers()

    # Check if API is reachable (1 retry with short sleep — keeps us under 3s hook timeout)
    health = _api_get("/api/teams")
    if health is None:
        time.sleep(0.3)
        health = _api_get("/api/teams")

    if health is not None:
        # API reachable -> output briefing to stdout (injected into Claude context)
        briefing = _build_briefing()
        sys.stdout.write(briefing)

        # 压缩恢复路径（Q6 裁定 A）：SessionStart 的 source 有五种取值
        # startup/resume/clear/compact/fork，只有 compact 这一种意味着"上一轮
        # 上下文刚被压掉"。此时把 PreCompact 存下的 OS 侧作战态原样递回去——
        # CC 自己的 compact_summary 压缩后本来就在模型上下文里，OS 该补的是模型
        # 没有的那半边（在飞 agent / 未完成任务 / 待裁决项）。
        compact_block = ""
        if session_info.get("source") == "compact":
            compact_block = _fetch_compact_checkpoint(session_info.get("session_id", ""))
            if compact_block:
                sys.stdout.write(compact_block)

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
