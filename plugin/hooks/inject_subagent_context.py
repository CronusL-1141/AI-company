#!/usr/bin/env python3
"""SubagentStart hook — inject OS environment context into sub-agents."""

import json
import os
import sys


def main():
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
