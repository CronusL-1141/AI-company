"""AI Team OS — Hook配置安装器

自动在项目的 .claude/settings.local.json 中配置hooks，
将CC操作事件转发到OS API。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HOOK_EVENTS = [
    "SubagentStart",
    "SubagentStop",
    "PreToolUse",
    "PostToolUse",
    "SessionStart",
    "SessionEnd",
    "Stop",
]


def get_send_event_source() -> Path:
    """获取send_event.py的源文件路径."""
    return Path(__file__).parent / "send_event.py"


def generate_hooks_config(api_url: str = "http://localhost:8000") -> dict:
    """生成Claude Code hooks配置.

    Parameters
    ----------
    api_url:
        OS API服务地址，会通过环境变量传递给send_event.py。
    """
    hooks: dict[str, list] = {}

    for event in HOOK_EVENTS:
        matcher_config: dict[str, str] = {}
        if event == "PreToolUse":
            matcher_config["matcher"] = "Agent|Bash|Edit|Write"

        hooks[event] = [{
            **matcher_config,
            "hooks": [{
                "type": "command",
                "command": f"python .claude/hooks/send_event.py {event}",
            }],
        }]

    return hooks


def install_hooks(
    project_dir: str,
    api_url: str = "http://localhost:8000",
) -> str:
    """在指定项目目录安装CC hooks配置.

    此函数是幂等的——多次运行只会更新hooks配置，不会重复添加。

    Parameters
    ----------
    project_dir:
        要安装hooks的项目根目录路径。
    api_url:
        OS API服务地址。

    Returns
    -------
    str
        生成的settings.local.json的绝对路径。
    """
    claude_dir = Path(project_dir) / ".claude"
    claude_dir.mkdir(exist_ok=True)

    # 复制send_event.py到项目的.claude/hooks/目录（避免路径中文编码问题）
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    src_script = get_send_event_source()
    dst_script = hooks_dir / "send_event.py"
    shutil.copy2(src_script, dst_script)

    settings_path = claude_dir / "settings.local.json"

    # 读取现有配置（保留其他设置）
    existing: dict = {}
    if settings_path.exists():
        with open(settings_path, encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = {}

    # 覆盖hooks配置（幂等）
    existing["hooks"] = generate_hooks_config(api_url)

    # 写回
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    return str(settings_path)


def uninstall_hooks(project_dir: str) -> bool:
    """移除hooks配置.

    Parameters
    ----------
    project_dir:
        项目根目录路径。

    Returns
    -------
    bool
        是否成功移除（True表示有hooks被移除，False表示没有找到hooks）。
    """
    settings_path = Path(project_dir) / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return False

    with open(settings_path, encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError:
            return False

    if "hooks" in config:
        del config["hooks"]
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True

    return False
