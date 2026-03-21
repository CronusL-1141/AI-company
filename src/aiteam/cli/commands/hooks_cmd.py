"""AI Team OS CLI — hooks command.

Manage Claude Code hooks integration configuration.
"""

from __future__ import annotations

from pathlib import Path

import typer

from aiteam.cli.display import print_error, print_success, print_warning

app = typer.Typer(name="hooks", help="管理Claude Code hooks集成")


@app.command("install")
def install(
    project_dir: str = typer.Argument(".", help="项目目录路径"),
    api_url: str = typer.Option(
        "http://localhost:8000",
        "--api-url",
        "-u",
        help="OS API服务地址",
    ),
) -> None:
    """在项目中安装Claude Code hooks，将CC操作事件同步到OS Dashboard."""
    from aiteam.hooks.install import install_hooks

    resolved = str(Path(project_dir).resolve())
    path = install_hooks(resolved, api_url)
    print_success(f"Hooks已配置到: {path}")
    print_success(f"API地址: {api_url}")
    print_success("现在CC中的操作会自动同步到OS Dashboard")


@app.command("remove")
def remove(
    project_dir: str = typer.Argument(".", help="项目目录路径"),
) -> None:
    """移除项目中的Claude Code hooks配置."""
    from aiteam.hooks.install import uninstall_hooks

    resolved = str(Path(project_dir).resolve())
    if uninstall_hooks(resolved):
        print_success("Hooks配置已移除")
    else:
        print_warning("未找到hooks配置")


@app.command("status")
def status(
    project_dir: str = typer.Argument(".", help="项目目录路径"),
) -> None:
    """Check hooks configuration status in the project."""
    import json

    settings_path = Path(project_dir).resolve() / ".claude" / "settings.local.json"
    if not settings_path.exists():
        print_warning(f"未找到配置文件: {settings_path}")
        return

    with open(settings_path, encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError:
            print_error("配置文件格式错误")
            return

    if "hooks" not in config:
        print_warning("未配置hooks")
        return

    hooks = config["hooks"]
    print_success(f"已配置 {len(hooks)} 个hook事件:")
    for event_name in hooks:
        typer.echo(f"  - {event_name}")
