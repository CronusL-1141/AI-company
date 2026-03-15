"""AI Team OS CLI — 主入口."""

import typer
from rich.console import Console

from aiteam import __version__
from aiteam.cli.commands import (
    agent_cmd,
    hooks_cmd,
    init_cmd,
    status_cmd,
    task_cmd,
    team_cmd,
    up_cmd,
)

app = typer.Typer(
    name="aiteam",
    help="AI Team OS — 通用可复用的AI Agent团队操作系统",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold blue]AI Team OS[/bold blue] v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-v", help="显示版本号", callback=version_callback, is_eager=True
    ),
) -> None:
    """AI Team OS — 通用可复用的AI Agent团队操作系统."""


# 注册子命令组
app.add_typer(init_cmd.app, name="init", help="初始化项目，生成 aiteam.yaml 配置文件")
app.add_typer(team_cmd.app, name="team", help="团队管理")
app.add_typer(agent_cmd.app, name="agent", help="Agent管理")
app.add_typer(task_cmd.app, name="task", help="任务管理")
app.add_typer(status_cmd.app, name="status", help="查看团队和任务状态")
app.add_typer(up_cmd.app, name="up", help="启动API服务器")
app.add_typer(hooks_cmd.app, name="hooks", help="管理Claude Code hooks集成")


if __name__ == "__main__":
    app()
