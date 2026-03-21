"""AI Team OS CLI — global status command."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from aiteam.cli.display import print_error, print_status, print_warning
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="status", help="状态概览")
console = Console()


@app.callback(invoke_without_command=True)
def status(
    team: str | None = typer.Option(None, "--team", "-t", help="指定团队名称（默认显示全局概览）"),
) -> None:
    """View global status overview."""
    try:
        manager = get_manager()

        if team:
            # Show specified team status
            team_status = run_async(manager.get_status(team))
            print_status(team_status)
        else:
            # Global overview: list all teams and their summaries
            teams = run_async(manager.list_teams())

            if not teams:
                print_warning("暂无团队。使用 'aiteam team create' 创建团队，或 'aiteam init' 初始化项目。")
                return

            # Summary table
            table = Table(title="全局状态概览")
            table.add_column("团队", style="bold cyan")
            table.add_column("编排模式", style="green")
            table.add_column("Agent数", justify="right")
            table.add_column("活跃任务", justify="right", style="yellow")
            table.add_column("已完成", justify="right", style="green")
            table.add_column("总任务", justify="right")

            for t in teams:
                try:
                    s = run_async(manager.get_status(t.name))
                    table.add_row(
                        t.name,
                        t.mode.value,
                        str(len(s.agents)),
                        str(len(s.active_tasks)),
                        str(s.completed_tasks),
                        str(s.total_tasks),
                    )
                except Exception:
                    table.add_row(
                        t.name,
                        t.mode.value,
                        "-",
                        "-",
                        "-",
                        "-",
                    )

            console.print(table)

    except Exception as e:
        print_error(f"获取状态失败: {e}")
        raise typer.Exit(code=1) from None
