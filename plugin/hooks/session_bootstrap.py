#!/usr/bin/env python3
"""AI Team OS — Session启动引导脚本

SessionStart hook触发时执行：
1. 检测OS API是否可达
2. 如可达，输出Leader简报（任务墙Top3、团队状态、规则提醒）
3. 如不可达，提示启动服务

stdout输出会被注入到Claude的系统提示中，用于指引Leader行为。
只使用Python标准库。
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "http://localhost:8000"
CONFIG_DIR = Path(__file__).parent.parent / "config"


def _api_get(path: str, timeout: float = 2.0):
    """GET请求API，返回JSON或None."""
    try:
        req = urllib.request.Request(f"{API_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _load_team_config() -> dict | None:
    """加载团队默认配置，失败返回None."""
    config_path = CONFIG_DIR / "team-defaults.json"
    try:
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _build_auto_team_instructions(config: dict) -> list[str]:
    """根据配置生成自动团队创建指令文本."""
    if not config.get("auto_create_team"):
        return []

    enabled_members = [
        m for m in config.get("permanent_members", []) if m.get("enabled")
    ]
    if not enabled_members:
        return []

    team_name = f"{config.get('team_name_prefix', 'auto')}-team"
    lines = []
    lines.append("")
    lines.append("=== 自动团队创建指令 ===")
    lines.append("请立即执行以下操作创建团队和常驻成员：")
    lines.append(f"1. TeamCreate(team_name='{team_name}')")
    for i, m in enumerate(enabled_members, start=2):
        lines.append(
            f"{i}. Agent(team_name='{team_name}', name='{m['name']}', "
            f"subagent_type='general-purpose', prompt='你是{m['role']}')"
        )
    return lines


def _build_briefing() -> str:
    """构建Leader简报."""
    lines = []
    lines.append("[AI Team OS] Session启动 — Leader简报")
    lines.append("")

    # 1. 团队状态
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

    # 2. 任务墙Top任务（从项目级task-wall获取）
    # 尝试获取第一个项目的task-wall
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
                # 按score排序取前5
                pending.sort(key=lambda t: t.get("score", 0), reverse=True)
                if pending:
                    lines.append("任务墙Top5:")
                    for t in pending[:5]:
                        priority = t.get("priority", "medium")
                        horizon = t.get("horizon", "mid")
                        score = t.get("score", 0)
                        lines.append(
                            f"  [{priority}/{horizon}] {t['title']} (score:{score:.1f})"
                        )
                else:
                    lines.append("任务墙: 无待办任务")
                lines.append("")

                stats = wall_data.get("stats", {})
                if stats:
                    lines.append(
                        f"统计: 总{stats.get('total',0)}任务, "
                        f"已完成{stats.get('completed_count',0)}, "
                        f"待办{stats.get('by_status',{}).get('pending',0)}"
                    )
                    lines.append("")

    # 3. 规则提醒（完整规则集，覆盖原sync_rules.py写入CLAUDE.md的所有规则）
    lines.append("=== Leader行为规则 ===")
    lines.append("1. Leader专注统筹——除极快小改动(<2min)外，所有实施工作分配给团队成员执行")
    lines.append("2. 统筹并行: 同时推进多方向，动态添加/Kill成员，QA问题分派后继续其他任务")
    lines.append("3. 添加成员必须用 Agent(team_name=...) 创建CC团队成员，不用local agent")
    lines.append("4. 只用CC Agent(team_name)创建成员，不要用MCP agent_register预注册")
    lines.append("5. TeamCreate后立即创建常驻成员(QA+bug-fixer)，然后才创建临时成员")
    lines.append("6. 团队组成: 常驻QA+Bug-fixer不Kill；临时开发/研究完成后Kill；团队不关闭")
    lines.append("7. 不空等——等结果时继续从任务墙领取任务（最多3个并行）")
    lines.append("8. 任务拆分基于Leader判断，不用模板")
    lines.append("9. 每个任务完成需编写测试验证")
    lines.append("10. 瓶颈讨论: 任务不足时组织会议（loop_review），充分评估必要性，不能没事找事干")
    lines.append("11. 会议动态成员: 根据议题添加参与者，讨论中随时招募专家")
    lines.append("12. 成员工具限制: 成员遇限制由Leader安装解决，MCP刷新用/mcp→Reconnect")
    lines.append("13. 记忆权威: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem")
    lines.append("14. 记忆原则: 只记不可推导的人类意图，技术细节交给代码和git")
    lines.append("15. 上下文管理: [CONTEXT WARNING]时完成当前任务后保存；[CRITICAL]时立即停止")
    lines.append("16. 完整规则: GET /api/system/rules 查询全部规则")
    lines.append("")

    # 进行中任务提醒
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

    # 可用Agent模板列表
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

    # 自动团队创建指令
    team_config = _load_team_config()
    if team_config:
        lines.extend(_build_auto_team_instructions(team_config))

    return "\n".join(lines)


def main() -> None:
    # 读取stdin中的session信息
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        session_info = json.loads(raw) if raw.strip() else {}
    except Exception:
        session_info = {}

    # 检测API是否可达
    health = _api_get("/api/teams")

    if health is not None:
        # API可达 → 输出简报到stdout（注入Claude上下文）
        briefing = _build_briefing()
        sys.stdout.write(briefing)

        # stderr记录（不影响stdout注入）
        sys.stderr.write(
            f"[aiteam-bootstrap] AI Team OS API reachable at {API_URL}\n"
            f"[aiteam-bootstrap] session_id={session_info.get('session_id', 'unknown')}\n"
            f"[aiteam-bootstrap] briefing injected ({len(briefing)} chars)\n"
        )
    else:
        # API不可达
        sys.stdout.write(
            "[AI Team OS] API未启动。请运行以下命令启动服务:\n"
            "cd ai-team-os && python -m uvicorn aiteam.api.app:create_app "
            "--factory --host 0.0.0.0 --port 8000 --reload\n"
        )
        sys.stderr.write(
            f"[aiteam-bootstrap] AI Team OS API not reachable at {API_URL}\n"
        )


if __name__ == "__main__":
    main()
