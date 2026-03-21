#!/usr/bin/env python3
"""AI Team OS — CLAUDE.md rules section sync script [DEPRECATED]

⚠️ 此脚本已废弃。规则现在通过 session_bootstrap.py 在 SessionStart hook
中以 stdout 方式注入到 Claude 上下文，不再需要写入用户的 CLAUDE.md 文件。

保留此文件仅用于向后兼容。如需手动同步规则到 CLAUDE.md，仍可直接运行：
    python plugin/hooks/sync_rules.py [claude_md_path]

但推荐方式是依赖 SessionStart hook 自动注入规则。
只使用 Python 标准库。
"""

import os
import re
import sys

START_MARKER = "<!-- AI-TEAM-OS-RULES-START -->"
END_MARKER = "<!-- AI-TEAM-OS-RULES-END -->"

RULES_BLOCK = f"""{START_MARKER}
## AI Team OS Leader行为准则（Plugin自动管理，请勿手动修改此段）
- **统筹并行**: 同时推进多方向，动态添加/Kill成员，QA问题分派后继续其他任务
- **团队组成**: 常驻QA+Bug-fixer不Kill；临时开发/研究完成后Kill；团队不关闭
- **瓶颈讨论**: 任务不足时组织会议（loop_review），充分评估必要性，不能没事找事干
- **会议动态成员**: 根据议题添加参与者，讨论中随时招募专家
- **成员工具限制**: 成员遇限制由Leader安装解决，MCP刷新用/mcp→Reconnect
- **添加成员**: 必须用team_name参数，不得降级为普通subagent
- **记忆权威**: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem
- **记忆原则**: 只记不可推导的人类意图，技术细节交给代码和git
- **上下文管理**: [CONTEXT WARNING]时完成当前任务后保存；[CRITICAL]时立即停止
- **完整规则**: GET /api/system/rules 查询全部31条规则
{END_MARKER}"""


def sync_rules(claude_md_path: str = "CLAUDE.md") -> str:
    """同步规则段到指定的 CLAUDE.md 文件。

    Returns:
        操作结果描述字符串
    """
    if os.path.exists(claude_md_path):
        with open(claude_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        if START_MARKER in content:
            # 已有规则段，更新
            pattern = re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER)
            content = re.sub(pattern, RULES_BLOCK, content, flags=re.DOTALL)
            result = "规则段已更新"
        else:
            # 追加规则段
            content = content.rstrip() + "\n\n" + RULES_BLOCK + "\n"
            result = "规则段已追加"

        with open(claude_md_path, "w", encoding="utf-8") as f:
            f.write(content)
    else:
        # 创建新文件
        with open(claude_md_path, "w", encoding="utf-8") as f:
            f.write(RULES_BLOCK + "\n")
        result = "CLAUDE.md已创建并写入规则"

    return result


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "CLAUDE.md"
    result = sync_rules(path)
    print(result)


if __name__ == "__main__":
    main()
