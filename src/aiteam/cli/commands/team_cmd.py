"""AI Team OS CLI — 团队管理命令组."""

from __future__ import annotations

import typer

from aiteam.cli.display import print_error, print_success, print_team, print_teams_table
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="team", help="团队管理")


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", "-n", help="团队名称"),
    mode: str = typer.Option("coordinate", "--mode", "-m", help="编排模式 (coordinate/broadcast/route/meet)"),
) -> None:
    """创建新团队."""
    try:
        manager = get_manager()
        team = run_async(manager.create_team(name=name, mode=mode))
        print_success(f"团队 '{team.name}' 创建成功")
        print_team(team)
    except Exception as e:
        print_error(f"创建团队失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("list")
def list_teams() -> None:
    """列出所有团队."""
    try:
        manager = get_manager()
        teams = run_async(manager.list_teams())
        print_teams_table(teams)
    except Exception as e:
        print_error(f"获取团队列表失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("show")
def show(
    name: str = typer.Argument(help="团队名称或ID"),
) -> None:
    """查看团队详情."""
    try:
        manager = get_manager()
        team = run_async(manager.get_team(name))
        print_team(team)
    except Exception as e:
        print_error(f"获取团队详情失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("delete")
def delete(
    name: str = typer.Argument(help="团队名称或ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认提示"),
) -> None:
    """删除团队."""
    if not yes:
        confirmed = typer.confirm(f"确定要删除团队 '{name}' 吗？此操作不可撤销")
        if not confirmed:
            print_success("已取消删除")
            raise typer.Exit()

    try:
        manager = get_manager()
        result = run_async(manager.delete_team(name))
        if result:
            print_success(f"团队 '{name}' 已删除")
        else:
            print_error(f"团队 '{name}' 不存在")
    except Exception as e:
        print_error(f"删除团队失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("set-mode")
def set_mode(
    name: str = typer.Argument(help="团队名称或ID"),
    mode: str = typer.Option(..., "--mode", "-m", help="编排模式 (coordinate/broadcast/route/meet)"),
) -> None:
    """设置团队编排模式."""
    try:
        manager = get_manager()
        team = run_async(manager.set_mode(name, mode))
        print_success(f"团队 '{team.name}' 编排模式已设为 '{mode}'")
    except Exception as e:
        print_error(f"设置编排模式失败: {e}")
        raise typer.Exit(code=1) from None
