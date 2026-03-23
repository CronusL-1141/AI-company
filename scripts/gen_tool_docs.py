"""从 MCP server.py 提取所有 @mcp.tool() 函数，生成工具参考文档。

用法: python scripts/gen_tool_docs.py
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

# ---------- 路径 ----------
ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = ROOT / "src" / "aiteam" / "mcp" / "server.py"
OUTPUT_MD = ROOT / "docs" / "mcp-tools-reference.md"

# ---------- 分类映射 ----------
CATEGORY_MAP: dict[str, tuple[str, int]] = {
    # tool_name -> (category_label, sort_order_within_category)
    # 团队管理
    "team_create":      ("团队管理", 1),
    "team_status":      ("团队管理", 2),
    "team_list":        ("团队管理", 3),
    "team_briefing":    ("团队管理", 4),
    "team_setup_guide": ("团队管理", 5),
    # Agent 管理
    "agent_register":      ("Agent 管理", 1),
    "agent_update_status": ("Agent 管理", 2),
    "agent_list":          ("Agent 管理", 3),
    # 任务管理
    "task_run":       ("任务管理", 1),
    "task_decompose": ("任务管理", 2),
    "task_status":    ("任务管理", 3),
    "taskwall_view":  ("任务管理", 4),
    # 循环系统
    "loop_start":     ("循环系统", 1),
    "loop_status":    ("循环系统", 2),
    "loop_next_task": ("循环系统", 3),
    "loop_advance":   ("循环系统", 4),
    "loop_pause":     ("循环系统", 5),
    "loop_resume":    ("循环系统", 6),
    "loop_review":    ("循环系统", 7),
    # 会议
    "meeting_create":        ("会议", 1),
    "meeting_send_message":  ("会议", 2),
    "meeting_read_messages": ("会议", 3),
    "meeting_conclude":      ("会议", 4),
    # 记忆
    "memory_search": ("记忆", 1),
    # 项目管理
    "project_create": ("项目管理", 1),
    "phase_create":   ("项目管理", 2),
    "phase_list":     ("项目管理", 3),
    # 系统
    "os_health_check":  ("系统", 1),
    "event_list":       ("系统", 2),
    "os_report_issue":  ("系统", 3),
    "os_resolve_issue": ("系统", 4),
}

# 分类显示顺序
CATEGORY_ORDER = [
    "团队管理",
    "Agent 管理",
    "任务管理",
    "循环系统",
    "会议",
    "记忆",
    "项目管理",
    "系统",
]


# ---------- AST 解析 ----------

def _is_mcp_tool_decorated(node: ast.FunctionDef) -> bool:
    """判断函数是否有 @mcp.tool() 装饰器。"""
    for deco in node.decorator_list:
        # @mcp.tool()
        if isinstance(deco, ast.Call):
            func = deco.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "tool"
                and isinstance(func.value, ast.Name)
                and func.value.id == "mcp"
            ):
                return True
        # @mcp.tool (无括号)
        if (
            isinstance(deco, ast.Attribute)
            and deco.attr == "tool"
            and isinstance(deco.value, ast.Name)
            and deco.value.id == "mcp"
        ):
            return True
    return False


def _annotation_to_str(node: ast.expr | None) -> str:
    """将 AST 类型注解节点转为可读字符串。"""
    if node is None:
        return ""
    return ast.unparse(node)


def _default_to_str(default: ast.expr) -> str:
    """将默认值 AST 节点转为可读字符串。"""
    return ast.unparse(default)


def _parse_docstring(raw: str | None) -> tuple[str, dict[str, str], str]:
    """解析 docstring，返回 (summary, args_dict, returns_text)。

    支持 Google-style docstring（Args: / Returns:）。
    """
    if not raw:
        return ("", {}, "")

    lines = textwrap.dedent(raw).strip().splitlines()

    summary_lines: list[str] = []
    args_dict: dict[str, str] = {}
    returns_text = ""

    section = "summary"
    current_arg: str | None = None

    for line in lines:
        stripped = line.strip()

        if stripped == "Args:":
            section = "args"
            current_arg = None
            continue
        if stripped == "Returns:":
            section = "returns"
            current_arg = None
            continue

        if section == "summary":
            summary_lines.append(stripped)
        elif section == "args":
            # 新参数行: "name: description"
            if ":" in stripped and not stripped.startswith(" ") and not line.startswith("        "):
                # 检测是否是缩进较少的行（参数行通常缩进 8 个空格）
                indent = len(line) - len(line.lstrip())
                if indent <= 8 and ":" in stripped:
                    parts = stripped.split(":", 1)
                    current_arg = parts[0].strip()
                    args_dict[current_arg] = parts[1].strip()
                    continue
            # 续行
            if current_arg and stripped:
                args_dict[current_arg] += " " + stripped
        elif section == "returns":
            if stripped:
                returns_text += (" " if returns_text else "") + stripped

    summary = " ".join(summary_lines).strip()
    # 去掉 summary 末尾空行产生的多余空格
    summary = summary.strip()
    return (summary, args_dict, returns_text)


def extract_tools(source: str) -> list[dict]:
    """从 Python 源码中提取所有 @mcp.tool() 函数信息。"""
    tree = ast.parse(source)
    tools = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not _is_mcp_tool_decorated(node):
            continue

        name = node.name
        docstring = ast.get_docstring(node)
        summary, args_doc, returns_doc = _parse_docstring(docstring)

        # 提取参数
        args_info: list[dict] = []
        func_args = node.args

        # 计算 defaults 对齐（defaults 对应最后 N 个 args）
        num_args = len(func_args.args)
        num_defaults = len(func_args.defaults)
        default_offset = num_args - num_defaults

        for i, arg in enumerate(func_args.args):
            if arg.arg == "self":
                continue
            type_str = _annotation_to_str(arg.annotation)
            default_str = ""
            default_idx = i - default_offset
            if default_idx >= 0 and default_idx < len(func_args.defaults):
                default_str = _default_to_str(func_args.defaults[default_idx])
            desc = args_doc.get(arg.arg, "")
            args_info.append({
                "name": arg.arg,
                "type": type_str,
                "default": default_str,
                "description": desc,
            })

        tools.append({
            "name": name,
            "summary": summary,
            "returns": returns_doc,
            "args": args_info,
        })

    return tools


# ---------- Markdown 生成 ----------

def generate_markdown(tools: list[dict]) -> str:
    """按分类生成 Markdown 文档。"""
    # 分组
    categorized: dict[str, list[tuple[int, dict]]] = {}
    uncategorized: list[dict] = []

    for tool in tools:
        cat_info = CATEGORY_MAP.get(tool["name"])
        if cat_info:
            cat, order = cat_info
            categorized.setdefault(cat, []).append((order, tool))
        else:
            uncategorized.append(tool)

    # 排序
    for cat in categorized:
        categorized[cat].sort(key=lambda x: x[0])

    lines: list[str] = []
    lines.append("# AI Team OS MCP Tools Reference")
    lines.append("")
    lines.append(f"> 自动生成自 `src/aiteam/mcp/server.py` — 共 {len(tools)} 个工具")
    lines.append("")

    # 目录
    lines.append("## 目录")
    lines.append("")
    for cat in CATEGORY_ORDER:
        if cat not in categorized:
            continue
        lines.append(f"- [{cat}](#{cat.replace(' ', '-').lower()})")
        for _, tool in categorized[cat]:
            anchor = tool["name"].replace("_", "-")
            lines.append(f"  - [{tool['name']}](#{anchor})")
    if uncategorized:
        lines.append("- [其他](#其他)")
        for tool in uncategorized:
            anchor = tool["name"].replace("_", "-")
            lines.append(f"  - [{tool['name']}](#{anchor})")
    lines.append("")

    # 各分类
    for cat in CATEGORY_ORDER:
        if cat not in categorized:
            continue
        lines.append("---")
        lines.append("")
        lines.append(f"## {cat}")
        lines.append("")

        for _, tool in categorized[cat]:
            lines.extend(_render_tool(tool))

    if uncategorized:
        lines.append("---")
        lines.append("")
        lines.append("## 其他")
        lines.append("")
        for tool in uncategorized:
            lines.extend(_render_tool(tool))

    return "\n".join(lines)


def _render_tool(tool: dict) -> list[str]:
    """渲染单个 tool 的 Markdown 段落。"""
    lines: list[str] = []
    lines.append(f"### `{tool['name']}`")
    lines.append("")
    lines.append(tool["summary"])
    lines.append("")

    if tool["args"]:
        lines.append("**参数:**")
        lines.append("")
        lines.append("| 参数 | 类型 | 默认值 | 说明 |")
        lines.append("|------|------|--------|------|")
        for arg in tool["args"]:
            type_str = f"`{arg['type']}`" if arg["type"] else "-"
            default_str = f"`{arg['default']}`" if arg["default"] else "必填"
            desc = arg["description"] or "-"
            lines.append(f"| `{arg['name']}` | {type_str} | {default_str} | {desc} |")
        lines.append("")
    else:
        lines.append("**参数:** 无")
        lines.append("")

    if tool["returns"]:
        lines.append(f"**返回:** {tool['returns']}")
        lines.append("")

    return lines


# ---------- Main ----------

def main() -> None:
    if not SERVER_PY.exists():
        print(f"错误: 找不到 {SERVER_PY}")
        raise SystemExit(1)

    source = SERVER_PY.read_text(encoding="utf-8")
    tools = extract_tools(source)

    if not tools:
        print("警告: 未找到任何 @mcp.tool() 函数")
        raise SystemExit(1)

    md = generate_markdown(tools)

    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(md, encoding="utf-8")
    print(f"已生成 {OUTPUT_MD} — 共 {len(tools)} 个工具")


if __name__ == "__main__":
    main()
