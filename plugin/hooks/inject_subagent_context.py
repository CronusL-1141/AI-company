#!/usr/bin/env python3
"""SubagentStart hook — inject OS environment context into sub-agents."""

import json
import os
import sys
import urllib.error
import urllib.request

# Default API base URL — reads from env if overridden
_API_BASE = os.environ.get("AI_TEAM_OS_API", "http://localhost:8000")
# Timeout for API calls (seconds) — keep short to avoid blocking agent startup
_API_TIMEOUT = 2


def _api_get(path: str):
    """Fetch JSON from the OS API. Returns parsed data or None on any failure."""
    try:
        url = f"{_API_BASE}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _fetch_pipeline_context() -> list[str]:
    """Query running tasks from the API and build pipeline context lines.

    Returns an empty list when the API is unavailable or no pipeline is found.
    """
    teams_data = _api_get("/api/teams")
    if not teams_data:
        return []

    # Support both list response and {"teams": [...]} envelope
    teams = teams_data if isinstance(teams_data, list) else teams_data.get("teams", [])

    pipeline_lines: list[str] = []

    for team in teams:
        team_id = team.get("id") or team.get("team_id", "")
        if not team_id:
            continue

        tasks_data = _api_get(f"/api/teams/{team_id}/tasks")
        if not tasks_data:
            continue

        tasks = tasks_data if isinstance(tasks_data, list) else tasks_data.get("tasks", [])

        for task in tasks:
            if task.get("status") != "running":
                continue

            config = task.get("config") or {}
            pipeline = config.get("pipeline")
            if not pipeline:
                continue

            # Extract pipeline metadata
            task_title = task.get("title", task.get("name", "Unknown"))
            pipeline_type = pipeline.get("type", "unknown")
            current_stage = pipeline.get("current_stage", "unknown")
            stage_desc = pipeline.get("description", "")
            task_type = pipeline.get("task_type", pipeline_type)

            # Extract subtask_id for the current stage
            stages = pipeline.get("stages", [])
            current_stage_index = pipeline.get("current_stage_index", 0)
            current_stage_subtask_id: str | None = None
            if current_stage_index < len(stages):
                current_stage_subtask_id = stages[current_stage_index].get("subtask_id")

            # Block 2: pipeline stage context
            pipeline_lines.append("## 当前工作流阶段")
            pipeline_lines.append(f"- 任务: {task_title}")
            pipeline_lines.append(f"- 管道类型: {pipeline_type} (feature/bugfix/research/...)")
            pipeline_lines.append(
                f"- 当前阶段: {current_stage} (Research/Design/Implement/Review/Test/...)"
            )
            if stage_desc:
                pipeline_lines.append(f"- 期望产出: {stage_desc}")
            if current_stage_subtask_id:
                pipeline_lines.append(f"- 你正在执行的子任务 ID: {current_stage_subtask_id}")
                pipeline_lines.append(
                    f"- 完成后通过 task_memo_add(task_id={current_stage_subtask_id}) 记录结果"
                )
            pipeline_lines.append(
                "- 完成后: 向 Leader 汇报，由 Leader 调用 pipeline_advance 推进到下一阶段"
            )
            pipeline_lines.append("")

            # Block 4: task type awareness
            pipeline_lines.append(f"## 任务类型: {task_type}")
            pipeline_lines.append(
                f"这是一个 {task_type} 类型的任务，请按对应工作流标准执行。"
            )
            pipeline_lines.append("")

            # Only inject the first running pipeline task found
            return pipeline_lines

    return pipeline_lines


def main():
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return
        json.loads(raw)
    except Exception:
        return

    # Build injection content
    lines = []
    lines.append("=== AI Team OS 子Agent环境 ===")
    lines.append("")
    lines.append("你正在AI Team OS管理的团队中工作。请遵循以下规则：")
    lines.append("")
    lines.append("## 核心规则（不可违反）")
    lines.append("1. 接到任务后第一步：通过task_memo_read了解历史上下文")
    lines.append("2. 执行过程中：关键进展用task_memo_add记录")
    lines.append("3. 完成时：task_memo_add(type=summary)写入最终总结")
    lines.append("4. 不直接修改不属于你任务范围的文件")
    lines.append("5. 遇到工具限制或阻塞：向Leader汇报，不要绕过")
    lines.append(
        "6. 2-Action规则：每执行2个实质性操作（编辑文件/运行命令/创建资源）后，用task_memo_add记录进展（防上下文压缩丢失）"
    )
    lines.append(
        "7. 3次失败升级：同一任务用同一方法连续失败3次，必须改变方法或向Leader上报，不要继续重试。失败后向Leader汇报以触发failure_analysis系统性学习"
    )
    lines.append("")
    lines.append("## 汇报格式")
    lines.append("完成后使用以下格式向Leader汇报：")
    lines.append("- 完成内容：{具体描述}")
    lines.append("- 修改文件：{列表}")
    lines.append("- 测试结果：{通过/失败}")
    lines.append("- 建议任务状态：→completed / →blocked(原因)")
    lines.append("- 建议memo：{一句话总结}")
    lines.append("")
    lines.append("## 安全规则")
    lines.append("- 禁止rm -rf /或rm -rf ~")
    lines.append("- 禁止硬编码密钥（password/secret/api_key/token）")
    lines.append("- 禁止git add .env/credentials/.pem/.key文件")
    lines.append("")

    # Block 1: report storage convention
    lines.append("## 报告存储")
    lines.append("- 研究/调研类任务完成后，将报告保存到 ~/.claude/data/ai-team-os/reports/")
    lines.append("- 命名格式：{你的agent名}_{课题关键词}_{YYYY-MM-DD}.md")
    lines.append("- 例如：rd-scanner_ai-products-march_2026-03-22.md")
    lines.append("- 报告内容使用 Markdown 格式")
    lines.append("")

    # Block 3: coding conventions
    lines.append("## 代码规范")
    lines.append("- 代码注释使用英文")
    lines.append("- Git commit message 使用英文")
    lines.append("- 变量名和函数名使用英文")
    lines.append("- 文档内容根据项目语言决定（中英文皆可）")
    lines.append("")

    # Blocks 2 & 4: dynamic pipeline context (silently skip on any failure)
    try:
        pipeline_lines = _fetch_pipeline_context()
        lines.extend(pipeline_lines)
    except Exception:
        pass

    # Try to read current team info
    teams_dir = os.path.join(os.path.expanduser("~"), ".claude", "teams")
    if os.path.isdir(teams_dir):
        for team_dir in os.listdir(teams_dir):
            config_path = os.path.join(teams_dir, team_dir, "config.json")
            if os.path.isfile(config_path):
                try:
                    with open(config_path, encoding="utf-8") as f:
                        data = json.load(f)
                    members = data.get("members", [])
                    if members:
                        lines.append(f"## 当前团队: {team_dir}")
                        lines.append(f"成员: {', '.join(m.get('name', '?') for m in members)}")
                        lines.append("")
                except Exception:
                    pass

    # Output
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": "\n".join(lines),
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
