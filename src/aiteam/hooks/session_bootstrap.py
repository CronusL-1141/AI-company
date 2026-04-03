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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "http://localhost:8000"
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

    team_name = f"{config.get('team_name_prefix', 'auto')}-team"
    lines = []
    lines.append("")
    lines.append("=== Auto team creation instructions ===")
    lines.append("请立即执行以下操作创建团队和常驻成员：")
    lines.append(f"1. TeamCreate(team_name='{team_name}')")
    for i, m in enumerate(enabled_members, start=2):
        role = m["role"]
        lines.append(
            f"{i}. Agent(team_name='{team_name}', name='{m['name']}', "
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
        try:
            local_commit = _get_local_commit(project_root)
            remote_commit = _get_remote_commit(project_root)

            if local_commit and remote_commit and local_commit != remote_commit:
                _run_background_update(project_root)
                notice = (
                    f"[OS] 检测到新版本 (local: {local_commit} → remote: {remote_commit})，"
                    "正在后台自动更新，下次启动时生效。"
                )
        except Exception:
            pass

    try:
        _UPDATE_CHECK_STATE_FILE.write_text(
            json.dumps({"last_checked": time.time(), "notice": notice}),
            encoding="utf-8",
        )
    except Exception:
        pass

    return notice


def _check_teams_dir_cleanup() -> str | None:
    """Scan ~/.claude/teams/ and warn if too many team directories accumulate."""
    teams_dir = Path.home() / ".claude" / "teams"
    if not teams_dir.exists():
        return None
    try:
        team_dirs = [p for p in teams_dir.iterdir() if p.is_dir()]
        count = len(team_dirs)
        if count > 3:
            return (
                f"[OS提醒] 检测到 {count} 个历史团队目录，建议清理："
                "使用 TeamDelete 或手动删除 ~/.claude/teams/ 下的旧目录"
            )
    except Exception:
        pass
    return None


def _build_briefing() -> str:
    """Build Leader briefing."""
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

    # 0. Check if current project is registered
    import os as _os
    cwd = _os.getcwd().replace("\\", "/")
    projects_data = _api_get("/api/projects")
    project_matched = False
    if projects_data and projects_data.get("data"):
        for proj in projects_data["data"]:
            rp = (proj.get("root_path") or "").replace("\\", "/").rstrip("/")
            if rp and cwd.rstrip("/").lower().startswith(rp.lower()):
                project_matched = True
                break
    if not project_matched:
        lines.append("=== 项目未注册 ===")
        lines.append(f"当前目录 {cwd} 尚未注册到AI Team OS。")
        lines.append("如需OS管理此项目，请执行: project_create(name='项目名', root_path='" + cwd + "')")
        lines.append("如不需要，可忽略此提示。OS功能（任务墙、团队管理等）在未注册项目中不可用。")
        lines.append("")

    # 1. Team status
    teams_data = _api_get("/api/teams")
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

    # 2. Top tasks from task wall
    if projects_data and projects_data.get("data"):
        project_id = projects_data["data"][0].get("id", "")
        if project_id:
            wall_data = _api_get(f"/api/projects/{project_id}/task-wall")
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

    # 3. Rule reminders
    lines.append("=== Leader行为规则 ===")
    lines.append("1. Leader专注统筹——除极快小改动(<2min)外，所有实施工作分配给团队成员执行")
    lines.append("2. 统筹并行: 同时推进多方向，动态添加/Kill成员，QA问题分派后继续其他任务")
    lines.append("3. 添加成员必须用 Agent(team_name=...) 创建CC团队成员，不用local agent")
    lines.append("4. 创建Agent时优先使用OS模板: agent_template_recommend(任务描述)查推荐 → Agent(subagent_type=模板名, team_name=..., name=...)。禁止Explore/Plan+team_name组合（它们不支持SendMessage团队通讯）。无匹配模板时才用general-purpose")
    lines.append("5. 团队组成: 按需创建成员，任务完成后Kill临时成员释放资源；团队保持到项目完成")
    lines.append("6. QA按需创建: 需要测试验收时创建QA Agent，不必常驻占用资源")
    lines.append("7. 绝对不空等——派出Agent后立刻从任务墙领取下一个任务并行推进。绝不出现'等X返回'然后什么都不做的情况。最多3方向并行。任务墙空了就组织会议讨论下一步")
    lines.append("8. 任务拆分基于Leader判断，不用模板")
    lines.append("9. 每个任务完成需编写测试验证")
    lines.append("10. 瓶颈讨论: 任务不足时组织会议（loop_review），充分评估必要性，不能没事找事干")
    lines.append("11. 会议动态成员: 根据议题添加参与者，讨论中随时招募专家")
    lines.append("12. 成员工具限制: 成员遇限制由Leader安装解决，MCP刷新用/mcp→Reconnect")
    lines.append("13. 记忆权威: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem")
    lines.append("14. 记忆原则: 只记不可推导的人类意图，技术细节交给代码和git")
    lines.append("15. 上下文管理: [CONTEXT WARNING]时完成当前任务后保存；[CRITICAL]时立即停止")
    lines.append("16. 完整规则: GET /api/system/rules 查询全部规则")
    lines.append("17. 自主推进: 战术决策自行决定（选哪个任务、怎么拆分），不停下来问用户")
    lines.append(
        "18. 决策分级: 战术决策自主做主（任务分配、实施方式）；战略决策请示用户（项目方向、重大架构变更）"
    )
    lines.append("19. 阻塞切换: 某任务需要用户批准时暂停该任务，切换到其他不需要批准的任务继续推进")
    lines.append("20. 统一汇报: 用户回来时先做阶段汇报，统一列出待决策事项，不逐步询问")
    lines.append(
        "21. 先研究再实施: 系统级新功能必须先多角度外部研究+竞品分析，召开会议讨论后再实施"
    )
    lines.append(
        "22. 2-Action规则: 每执行2个实质性操作（编辑文件/运行命令/创建资源）后，用task_memo_add记录进展（防上下文压缩丢失）"
    )
    lines.append(
        "23. 3次失败升级: 同一任务用同一方法连续失败3次，必须：1)改变方法 2)请求其他Agent协助 3)上报Leader。任务最终失败时调用failure_analysis工具生成antibody+vaccine+catalyst系统性学习"
    )
    lines.append("")

    # In-progress task reminders
    if projects_data and projects_data.get("data"):
        project_id = projects_data["data"][0].get("id", "")
        if project_id:
            wall_data = _api_get(f"/api/projects/{project_id}/task-wall")
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

    # 4. Pending Leader Briefings
    briefings = _api_get("/api/leader-briefings?status=pending")
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

    # 5. Auto-wake instruction
    lines.append("=== 自动唤醒 ===")
    lines.append("请在session开始后立即执行以下操作:")
    lines.append("1. 调用 CronCreate 设置定时唤醒（每30分钟），prompt模板:")
    lines.append('   "【自动唤醒】先读取 ~/.claude/context-monitor.json 获取上下文使用比例并报告。然后：1.有待办→自主推进常规任务，需用户决策的用briefing_add记录；2.无待办→主动行动：研究竞品/新技术、组织会议讨论规划、审查代码、优化功能；3.上下文>80%→保存进度到记忆，提醒开新session"')
    lines.append("2. 如有待决简报，在用户首次发言时汇报")
    lines.append("")

    lines.append("请阅读CLAUDE.md获取项目核心约束，然后查看任务墙决定下一步工作。")
    lines.append("")
    lines.append("=== 可用Skills ===")
    lines.append("- /meeting-facilitate — 需要组织多Agent讨论时使用")
    lines.append("- /meeting-participate — 被邀请参加会议时使用")
    lines.append("- /continuous-mode — 启动自动循环领取任务模式")

    # Available Agent template list
    import os

    agents_dir = os.path.join(os.path.expanduser("~"), ".claude", "agents")
    if os.path.isdir(agents_dir):
        templates = [f.replace(".md", "") for f in os.listdir(agents_dir) if f.endswith(".md")]
        if templates:
            groups = {}
            for t in sorted(templates):
                prefix = t.split("-")[0] if "-" in t else "other"
                groups.setdefault(prefix, []).append(t)
            lines.append("")
            lines.append("=== 可用Agent模板 ===")
            for prefix, names in sorted(groups.items()):
                lines.append(f"  {prefix}: {', '.join(names)}")

    # Auto team creation instructions
    team_config = _load_team_config()
    if team_config:
        lines.extend(_build_auto_team_instructions(team_config))

    return "\n".join(lines)


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

    # Check if API is reachable (retry up to 3 times — MCP may still be starting it)
    health = None
    for attempt in range(3):
        health = _api_get("/api/teams")
        if health is not None:
            break
        time.sleep(2)

    if health is not None:
        # API reachable -> output briefing to stdout (injected into Claude context)
        briefing = _build_briefing()
        sys.stdout.write(briefing)

        sys.stderr.write(
            f"[aiteam-bootstrap] AI Team OS API reachable at {API_URL}\n"
            f"[aiteam-bootstrap] session_id={session_info.get('session_id', 'unknown')}\n"
            f"[aiteam-bootstrap] briefing injected ({len(briefing)} chars)\n"
        )
    else:
        # API not reachable
        sys.stdout.write(
            "[AI Team OS] API未启动。请运行以下命令启动服务:\n"
            "cd ai-team-os && python -m uvicorn aiteam.api.app:create_app "
            "--factory --host 0.0.0.0 --port 8000 --reload\n"
        )
        sys.stderr.write(f"[aiteam-bootstrap] AI Team OS API not reachable at {API_URL}\n")


if __name__ == "__main__":
    main()
