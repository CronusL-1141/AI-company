"""AI Team OS CLI — up 命令.

启动API服务器（uvicorn）。
"""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer()
console = Console()


@app.callback(invoke_without_command=True)
def up(
    api_port: int = typer.Option(8000, "--api-port", help="API服务器端口"),
    no_dashboard: bool = typer.Option(False, "--no-dashboard", help="仅启动API，不启动Dashboard"),
    reload: bool = typer.Option(False, "--reload", help="开发模式，自动重载"),
) -> None:
    """启动 AI Team OS API 服务器."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]错误: uvicorn未安装，请运行 pip install 'uvicorn[standard]'[/red]")
        raise typer.Exit(1)

    console.print("[bold blue]AI Team OS[/bold blue] API 服务器启动中...")
    console.print(f"  API地址: [green]http://localhost:{api_port}[/green]")
    console.print(f"  API文档: [green]http://localhost:{api_port}/docs[/green]")

    if no_dashboard:
        console.print("  Dashboard: [dim]已禁用[/dim]")
    else:
        console.print("  Dashboard: [yellow]尚未集成（使用 --no-dashboard 跳过）[/yellow]")

    console.print()

    uvicorn.run(
        "aiteam.api.app:create_app",
        host="0.0.0.0",
        port=api_port,
        reload=reload,
        factory=True,
    )
