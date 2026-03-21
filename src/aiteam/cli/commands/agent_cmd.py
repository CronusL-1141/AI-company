"""AI Team OS CLI — Agent management command group."""

from __future__ import annotations

import typer

from aiteam.cli.display import print_agents_table, print_error, print_success
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="agent", help="Agent管理")


@app.command("create")
def create(
    team: str = typer.Option(..., "--team", "-t", help="所属团队名称"),
    name: str = typer.Option(..., "--name", "-n", help="Agent名称"),
    role: str = typer.Option(..., "--role", "-r", help="Agent角色"),
    model: str | None = typer.Option(None, "--model", "-m", help="使用的模型"),  # noqa: UP007
) -> None:
    """Create a new Agent."""
    try:
        manager = get_manager()
        kwargs: dict[str, str] = {}
        if model:
            kwargs["model"] = model
        agent = run_async(manager.add_agent(team_name=team, name=name, role=role, **kwargs))
        print_success(f"Agent '{agent.name}' 已添加到团队 '{team}'")
    except Exception as e:
        print_error(f"创建Agent失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("list")
def list_agents(
    team: str = typer.Option(..., "--team", "-t", help="团队名称"),
) -> None:
    """List all Agents in a team."""
    try:
        manager = get_manager()
        agents = run_async(manager.list_agents(team))
        print_agents_table(agents)
    except Exception as e:
        print_error(f"获取Agent列表失败: {e}")
        raise typer.Exit(code=1) from None


@app.command("remove")
def remove(
    team: str = typer.Option(..., "--team", "-t", help="团队名称"),
    name: str = typer.Option(..., "--name", "-n", help="Agent名称"),
) -> None:
    """Remove an Agent from a team."""
    try:
        manager = get_manager()
        result = run_async(manager.remove_agent(team_name=team, agent_name=name))
        if result:
            print_success(f"Agent '{name}' 已从团队 '{team}' 中移除")
        else:
            print_error(f"Agent '{name}' 不存在于团队 '{team}' 中")
    except Exception as e:
        print_error(f"移除Agent失败: {e}")
        raise typer.Exit(code=1) from None
