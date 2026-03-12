"""AI Team OS CLI — 任务命令组."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from aiteam.cli.display import print_error, print_tasks_table
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="task", help="任务管理")
console = Console()


@app.command("run")
def run_task(
    team: str = typer.Option(..., "--team", "-t", help="团队名称"),
    task: str = typer.Option(..., "--task", help="任务描述"),
) -> None:
    """执行任务."""
    try:
        manager = get_manager()
        console.print(f"[bold blue]正在执行任务...[/bold blue] 团队: {team}")
        result = run_async(manager.run_task(team_name=team, task_description=task))

        # 显示结果
        panel = Panel(
            f"[bold]任务ID:[/bold] {result.task_id}\n"
            f"[bold]状态:[/bold] {result.status.value}\n"
            f"[bold]耗时:[/bold] {result.duration_seconds:.1f}秒\n\n"
            f"[bold]结果:[/bold]\n{result.result}",
            title="[bold green]任务执行结果[/bold green]",
            border_style="green",
        )
        console.print(panel)

        # 显示各Agent输出
        if result.agent_outputs:
            for agent_name, output in result.agent_outputs.items():
                agent_panel = Panel(
                    output,
                    title=f"[bold cyan]{agent_name}[/bold cyan]",
                    border_style="cyan",
                )
                console.print(agent_panel)

    except Exception as e:
        print_error(f"执行任务失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("list")
def list_tasks(
    team: str = typer.Option(..., "--team", "-t", help="团队名称"),
) -> None:
    """列出团队的任务."""
    try:
        manager = get_manager()
        tasks = run_async(manager.list_tasks(team))
        print_tasks_table(tasks)
    except Exception as e:
        print_error(f"获取任务列表失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("status")
def task_status(
    task_id: str = typer.Argument(help="任务ID"),
) -> None:
    """查看任务详情."""
    try:
        manager = get_manager()
        task = run_async(manager.get_task_status(task_id))

        status_color = {
            "pending": "yellow",
            "running": "blue",
            "completed": "green",
            "failed": "red",
        }.get(task.status.value, "white")

        content = (
            f"[bold]任务ID:[/bold] {task.id}\n"
            f"[bold]标题:[/bold] {task.title}\n"
            f"[bold]描述:[/bold] {task.description or '-'}\n"
            f"[bold]状态:[/bold] [{status_color}]{task.status.value}[/{status_color}]\n"
            f"[bold]分配给:[/bold] {task.assigned_to or '-'}\n"
            f"[bold]创建时间:[/bold] {task.created_at:%Y-%m-%d %H:%M:%S}"
        )
        if task.started_at:
            content += f"\n[bold]开始时间:[/bold] {task.started_at:%Y-%m-%d %H:%M:%S}"
        if task.completed_at:
            content += f"\n[bold]完成时间:[/bold] {task.completed_at:%Y-%m-%d %H:%M:%S}"
        if task.result:
            content += f"\n\n[bold]结果:[/bold]\n{task.result}"

        panel = Panel(content, title="[bold blue]任务详情[/bold blue]", border_style="blue")
        console.print(panel)

    except Exception as e:
        print_error(f"获取任务详情失败: {e}")
        raise typer.Exit(code=1) from None
