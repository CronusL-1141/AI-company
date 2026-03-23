#!/usr/bin/env python3
"""AI Team OS — Session startup bootstrap script.

Executed when SessionStart hook fires:
1. Detect if OS API is reachable
2. If reachable, output Leader briefing (task wall Top3, team status, rule reminders)
3. If not reachable, prompt to start service

Stdout output is injected into Claude's system prompt to guide Leader behavior.
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
CONFIG_DIR = Path(__file__).parent.parent / "config"

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


def _check_for_updates() -> str | None:
    """Check if a newer version is available on git remote.

    Uses a 24-hour cooldown to avoid slowing down every session start.
    Returns a notice string if updates are available, or None otherwise.
    """
    # Respect cooldown: skip if last check was within 24 hours
    try:
        _UPDATE_CHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _UPDATE_CHECK_STATE_FILE.exists():
            state = json.loads(_UPDATE_CHECK_STATE_FILE.read_text(encoding="utf-8"))
            last_checked = state.get("last_checked", 0)
            if time.time() - last_checked < _UPDATE_CHECK_COOLDOWN_SECS:
                # Return cached result if it was stored
                return state.get("notice")
    except Exception:
        pass

    # Locate the project root from the installed hooks directory
    # Hooks are in: <project_root>/plugin/hooks/  OR  ~/.claude/hooks/ai-team-os/
    # If this file lives in the installed location, __file__ won't point at project root.
    # We rely on a recorded install path stored during install.
    install_info_file = Path.home() / ".claude" / "data" / "ai-team-os" / "install_path.txt"
    project_root: Path | None = None
    if install_info_file.exists():
        try:
            candidate = Path(install_info_file.read_text(encoding="utf-8").strip())
            if candidate.is_dir() and (candidate / ".git").exists():
                project_root = candidate
        except Exception:
            pass

    # Fallback: try to infer from __file__ (works if hooks are not yet copied)
    if project_root is None:
        candidate = Path(__file__).resolve().parent.parent.parent
        if (candidate / ".git").exists():
            project_root = candidate

    notice: str | None = None

    if project_root is not None:
        try:
            # Check git remote silently
            fetch_result = subprocess.run(
                ["git", "fetch", "--quiet", "origin"],
                cwd=str(project_root),
                capture_output=True,
                timeout=5,
            )
            if fetch_result.returncode == 0:
                local = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(project_root),
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=3,
                )
                # Determine remote default branch
                remote_commit = ""
                for branch in ("origin/main", "origin/master"):
                    r = subprocess.run(
                        ["git", "rev-parse", "--short", branch],
                        cwd=str(project_root),
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=3,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        remote_commit = r.stdout.strip()
                        break

                local_commit = local.stdout.strip() if local.returncode == 0 else ""
                if local_commit and remote_commit and local_commit != remote_commit:
                    notice = (
                        "[AI Team OS] 有新版本可用 "
                        f"(local: {local_commit} → remote: {remote_commit}). "
                        "运行 `python scripts/update.py` 或 `python install.py --update` 获取更新。"
                    )
        except Exception:
            pass

    # Save state with cooldown timestamp
    try:
        _UPDATE_CHECK_STATE_FILE.write_text(
            json.dumps({"last_checked": time.time(), "notice": notice}),
            encoding="utf-8",
        )
    except Exception:
        pass

    return notice


def _build_briefing() -> str:
    """Build Leader briefing."""
    lines = []
    lines.append("[AI Team OS] Session启动 — Leader简报")
    lines.append("")

    # Update availability notice (24h cooldown, non-blocking)
    update_notice = _check_for_updates()
    if update_notice:
        lines.append(f"[UPDATE] {update_notice}")
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

    # 2. Top tasks from task wall (fetched from project-level task-wall)
    # Try to get the first project's task-wall
    projects_data = _api_get("/api/projects")
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
                # Sort by score and take top 5
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

    # 3. Rule reminders (complete rule set, supersedes rules previously written to CLAUDE.md by sync_rules.py)
    lines.append("=== Leader行为规则 ===")
    lines.append("1. Leader专注统筹——除极快小改动(<2min)外，所有实施工作分配给团队成员执行")
    lines.append("2. 统筹并行: 同时推进多方向，动态添加/Kill成员，QA问题分派后继续其他任务")
    lines.append("3. 添加成员必须用 Agent(team_name=...) 创建CC团队成员，不用local agent")
    lines.append("4. 创建Agent时优先使用模板: agent_template_recommend(任务描述)查推荐 → Agent(subagent_type=模板名, team_name=..., name=...)。无匹配模板时才用general-purpose")
    lines.append("5. TeamCreate后立即创建常驻成员(QA+bug-fixer)，然后才创建临时成员")
    lines.append("6. 团队组成: 常驻QA+Bug-fixer不Kill；临时开发/研究完成后Kill；团队不关闭")
    lines.append("7. 不空等——等Agent结果时继续从任务墙领取下一个任务并行推进（最多3个并行）")
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
    import time
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

        # Log to stderr (doesn't affect stdout injection)
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
