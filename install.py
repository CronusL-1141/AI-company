#!/usr/bin/env python3
"""AI Team OS 一键安装脚本."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def check_command(cmd: str) -> bool:
    """检查命令是否可用."""
    return shutil.which(cmd) is not None


def run(args: list[str], cwd: str | None = None, **kwargs) -> subprocess.CompletedProcess:
    """运行子进程，失败时打印友好错误."""
    try:
        return subprocess.run(
            args, cwd=cwd, check=True,
            # Windows 上 npm 需要 shell=True
            shell=(sys.platform == "win32" and args[0] in ("npm", "npx")),
            **kwargs,
        )
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] 命令失败: {' '.join(args)}")
        raise SystemExit(1) from e
    except FileNotFoundError:
        print(f"[FAIL] 找不到命令: {args[0]}")
        raise SystemExit(1)


def main():
    print("=" * 40)
    print("  AI Team OS 安装")
    print("=" * 40)
    print()

    project_root = Path(__file__).resolve().parent

    # 1. 检查 Python 版本
    if sys.version_info < (3, 11):
        print("[FAIL] 需要 Python 3.11+，当前版本:", sys.version)
        sys.exit(1)
    print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # 2. 检查 Node.js（可选，仅 Dashboard 需要）
    has_node = check_command("node")
    has_npm = check_command("npm")
    if has_node and has_npm:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        print(f"[OK] Node.js {result.stdout.strip()}")
    else:
        print("[WARN] 未检测到 Node.js/npm，将跳过 Dashboard 构建")
        print("       Dashboard 是可选的，核心功能不受影响")

    print()

    # 3. 安装 Python 包
    print("[...] 安装 Python 依赖...")
    run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=str(project_root))
    print("[OK] Python 依赖安装完成")
    print()

    # 4. 构建 Dashboard（可选）
    dashboard_dir = project_root / "dashboard"
    if dashboard_dir.exists() and has_node and has_npm:
        print("[...] 安装 Dashboard 依赖...")
        run(["npm", "install"], cwd=str(dashboard_dir))
        print("[OK] Dashboard 依赖安装完成")

        print("[...] 构建 Dashboard...")
        run(["npm", "run", "build"], cwd=str(dashboard_dir))
        print("[OK] Dashboard 构建完成")
        print()
    elif dashboard_dir.exists():
        print("[SKIP] Dashboard 构建跳过（需要 Node.js）")
        print()

    # 5. 创建数据目录
    data_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"[OK] 数据目录: {data_dir}")

    # 6. 生成 .mcp.json（如不存在）
    mcp_json = project_root / ".mcp.json"
    if not mcp_json.exists():
        config = {
            "mcpServers": {
                "ai-team-os": {
                    "command": "python",
                    "args": ["-m", "aiteam.mcp.server"],
                    "cwd": str(project_root).replace("\\", "/"),
                    "env": {
                        "AITEAM_API_URL": "http://localhost:8000"
                    }
                }
            }
        }
        mcp_json.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print("[OK] 生成 .mcp.json")
    else:
        print("[OK] .mcp.json 已存在，跳过")

    # 7. 完成
    print()
    print("=" * 40)
    print("  安装完成!")
    print("=" * 40)
    print()
    print("使用方法:")
    print("  1. 在 Claude Code 中打开此项目目录")
    print("  2. MCP tools 会自动加载")
    print("  3. 运行 /os-up 创建团队并启动系统")
    print("  4. 运行 /os-status 查看系统状态")
    if dashboard_dir.exists() and has_node:
        print("  5. Dashboard: http://localhost:8000")
    print()
    print("更多信息请参阅 plugin/README.md")


if __name__ == "__main__":
    main()
