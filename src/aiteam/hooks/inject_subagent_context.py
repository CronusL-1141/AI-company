#!/usr/bin/env python3
"""SubagentStart hook — inject OS environment context into sub-agents.

Usage: python -m aiteam.hooks.inject_subagent_context
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_SUBAGENT_MARKER_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os", "subagent_sessions"
)


def _safe_session_id(session_id: str) -> str:
    """Strip anything that isn't alphanumeric, hyphen, or underscore to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id)


def _mark_subagent_session(session_id: str) -> None:
    """Touch a marker file so workflow_reminder can skip Leader checks for this session."""
    if not session_id:
        return
    safe_id = _safe_session_id(session_id)
    if not safe_id:
        return
    try:
        os.makedirs(_SUBAGENT_MARKER_DIR, exist_ok=True)
        marker = os.path.join(_SUBAGENT_MARKER_DIR, safe_id)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("")
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


# Default API base URL — resolved dynamically from port file
_API_BASE = _get_api_url()
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


# 方向记忆（记忆系统 v2 P1）：kind 标签 + 注入截断优先级（constraint 最先保留）。
_MEM_KIND_LABEL = {
    "constraint": "约束/护栏",
    "design": "设计意图",
    "directive": "工作方式",
    "preference": "格式偏好",
}


def _project_dir() -> str:
    """当前项目目录：优先 CLAUDE_PROJECT_DIR，回退 cwd（供 X-Project-Dir 解析项目）。"""
    return os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()


def _fetch_direction_memories() -> list:
    """查有效方向层条目（API 已按 kind 优先级排序）。不可达返回 []（静默）。

    带 X-Project-Dir 头让 API 解析出当前项目，纳入 project 级方向条目 +
    global/user 全局条目——这就是"每个派出的 agent 出生即继承方向层"。
    """
    try:
        import urllib.parse as _up
        req = urllib.request.Request(f"{_API_BASE}/api/memories", method="GET")
        pdir = _project_dir()
        if pdir:
            req.add_header("X-Project-Dir", _up.quote(pdir, safe="/:.-_\\"))
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("data", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _sanitize_inline(text: str) -> str:
    """注入渲染前的单行化清洗（审查 major：memo/记忆内容含换行可伪造
    『## 章节头』污染其他 agent 的注入上下文）。折叠一切空白为单空格。"""
    return " ".join((text or "").split())


def _render_direction_memories(items: list, budget: int = 900) -> list:
    """渲染方向层条目；超预算按 kind 优先级截断并注明剩余条数。"""
    if not items:
        return []
    lines = ["## 方向记忆（团队共享·你必须遵守）"]
    used = 0
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
        entry = f"- [{label}] {content}"
        if used + len(entry) > budget:
            stop = True
            truncated += 1
            continue
        lines.append(entry)
        used += len(entry)
    if truncated:
        lines.append(f"- …另有 {truncated} 条，Leader 可用 memory_list 查看")
    lines.append("")
    return lines


def _fetch_recent_task_memos(task_id: str, limit: int = 3) -> list:
    """查当前任务最近 limit 条有效 memo（Zep 双读之"最近记录"）。不可达返回 []。"""
    if not task_id:
        return []
    try:
        req = urllib.request.Request(
            f"{_API_BASE}/api/tasks/{task_id}/memo", method="GET"
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        memos = data.get("data", []) if isinstance(data, dict) else []
        recent = memos[-limit:]
        rendered = ["## 当前任务近期记录（情景层）"]
        for m in recent:
            content = _sanitize_inline(m.get("content") or "")
            if content:
                rendered.append(f"- [{m.get('type', 'progress')}] {content[:150]}")
        rendered.append("")
        return rendered if len(rendered) > 2 else []
    except Exception:
        return []


def _fetch_execution_patterns(task_description: str) -> list[str]:
    """Query historical execution patterns relevant to the current task.

    Returns formatted lines for context injection, or empty list on failure.
    """
    if not task_description:
        return []
    try:
        import urllib.parse
        params = urllib.parse.urlencode({"query": task_description[:200], "top_k": 3})
        data = _api_get(f"/api/execution-patterns/search?{params}")
        if not data or not data.get("patterns"):
            return []

        patterns = data["patterns"]
        lines: list[str] = ["## 历史执行经验"]
        for i, p in enumerate(patterns, 1):
            status = "成功" if p.get("type") == "success" else "失败"
            lines.append(f"\n[{i}] [{status}] 任务类型: {_sanitize_inline(p.get('task_type', '未知'))}")
            lines.append(f"    模板: {_sanitize_inline(p.get('agent_template', '未知'))}")
            lines.append(f"    方法: {_sanitize_inline(p.get('approach', ''))}")
            if p.get("type") == "success":
                lines.append(f"    结果: {_sanitize_inline(p.get('result_summary', ''))}")
            else:
                lines.append(f"    错误: {_sanitize_inline(p.get('error', ''))}")
                lines.append(f"    教训: {_sanitize_inline(p.get('lesson', ''))}")
        lines.append("")
        return lines
    except Exception:
        return []


# P0 重接（2026-07-14 审计）：memo/经验注入的触发键直接取自本次派单 prompt。
# 旧实现挂在已退役的 config.pipeline 检测上——恒空（两个注入从未生效）、
# 每次派发空扫全部团队（1+N 次 API）、死文案还教 agent 调已退役的管道推进工具。
_TASK_ID_RE = re.compile(
    r"(?:task_id|任务\s*ID|任务墙|总任务)[^0-9a-fA-F]{0,12}"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _first_user_message(transcript_path: str) -> str:
    """从 transcript 首条 user 消息取派单 prompt（payload 无 prompt 字段时的兜底）。"""
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message")
                if not (isinstance(msg, dict) and msg.get("role") == "user"):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    return content[:2000]
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            return (part.get("text") or "")[:2000]
    except Exception:
        return ""
    return ""


def _extract_task_context(payload: dict) -> tuple[str, str]:
    """从派单上下文提取 (task_id, prompt 文本)。

    prompt 来源优先级：payload.prompt / payload.description → transcript 首条
    user 消息。task_id 只认显式样式（task_id=<uuid>、任务ID: <uuid> 等，
    见 _TASK_ID_RE），避免把 repo_id/deep_review_id 之类的 uuid 误认成任务。
    """
    prompt = str(payload.get("prompt") or payload.get("description") or "")
    if not prompt.strip():
        prompt = _first_user_message(str(payload.get("transcript_path") or ""))
    match = _TASK_ID_RE.search(prompt)
    return (match.group(1) if match else "", prompt)


def main():
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return

    _mark_subagent_session(payload.get("session_id", ""))

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
        "6. 2-Action规则：每执行2个实质性操作（编辑文件/运行命令/创建资源）后，"
        "用task_memo_add记录进展（防上下文压缩丢失）"
    )
    lines.append(
        "7. 3次失败升级：同一任务用同一方法连续失败3次，必须改变方法或向Leader上报，"
        "不要继续重试。失败后向Leader汇报以触发failure_analysis系统性学习"
    )
    lines.append("")
    # 汇报格式段已删（2026-07-14 审计 P2）：与方向记忆"完成即汇报"directive
    # 重复，且对一次性答题类 agent 是误导（曾致纯答题 agent 附全套汇报样板）。
    lines.append("## 安全规则")
    lines.append("- 禁止rm -rf /或rm -rf ~")
    lines.append("- 禁止硬编码密钥（password/secret/api_key/token）")
    lines.append("- 禁止git add .env/credentials/.pem/.key文件")
    lines.append("")

    # Block 1: report storage convention
    lines.append("## 报告存储")
    lines.append("- 研究/调研类任务完成后，必须使用 report_save 工具保存报告，禁止直接用Write写入")
    lines.append(
        "- 报告必须通过 report_save 工具保存（直接Write会被OS阻止）。"
        '格式：report_save(author="你的名字", topic="主题", content="markdown内容",'
        ' report_type="research/design/analysis/meeting-minutes")'
    )
    lines.append("- report_save会自动处理命名、路径、frontmatter和项目关联")
    lines.append("- 报告内容使用 Markdown 格式")
    lines.append("")

    # Block 3: coding conventions
    lines.append("## 代码规范")
    lines.append("- 代码注释使用英文")
    lines.append("- Git commit message 使用英文")
    lines.append("- 变量名和函数名使用英文")
    lines.append("- 文档内容根据项目语言决定（中英文皆可）")
    lines.append("")

    # 方向记忆节（记忆系统 v2 P1）：每个派出 agent 出生即继承团队方向层。
    # 静默跳过——API 不可达绝不能让 hook 报错。
    try:
        lines.extend(_render_direction_memories(_fetch_direction_memories(), budget=900))
    except Exception:
        pass

    # 动态注入触发键：直接来自本次派单 prompt（P0 重接，不再依赖退役 pipeline）
    task_id_for_memos, prompt_text = "", ""
    try:
        task_id_for_memos, prompt_text = _extract_task_context(payload)
    except Exception:
        pass

    # 当前任务最近 3 条有效 memo（Zep 双读之"最近记录"；静默跳过）
    try:
        lines.extend(_fetch_recent_task_memos(task_id_for_memos, limit=3))
    except Exception:
        pass

    # Inject relevant historical execution patterns (silently skip on any failure)
    try:
        lines.extend(_fetch_execution_patterns(prompt_text[:200]))
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

    # Trim context to avoid overwhelming sub-agent with boilerplate.
    # Keep the mandatory header rules (first ~40 lines) and dynamic sections.
    # If total lines exceed the budget, drop the team-membership section (lowest priority).
    _max_lines = 60
    if len(lines) > _max_lines:
        # Find where team membership blocks start (marked by "## 当前团队:")
        team_block_start = next(
            (i for i, ln in enumerate(lines) if ln.startswith("## 当前团队:")), None
        )
        if team_block_start is not None:
            lines = lines[:team_block_start]

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
