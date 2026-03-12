"""AI Team OS CLI — init 命令."""

from __future__ import annotations

from pathlib import Path

import typer

from aiteam.cli.display import print_error, print_success, print_warning
from aiteam.config.settings import AITEAM_DIR, CONFIG_FILENAME, generate_default_config

app = typer.Typer(name="init", help="初始化项目")


# ============================================================
# 模板配置
# ============================================================

TEMPLATES: dict[str, str] = {
    "research": """\
# AI Team OS 项目配置 — 研究团队模板
project:
  name: "research-project"
  description: "AI研究项目"
  language: "zh"

defaults:
  model: "claude-opus-4-6"
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"
  memory_backend: "file"
  dashboard_port: 3000
  api_port: 8000

team:
  name: "research-team"
  mode: "coordinate"
  leader:
    name: "lead-researcher"
    role: "首席研究员"
    system_prompt: "你是一位资深研究员，负责制定研究计划并综合分析结果。"
  members:
    - name: "literature-analyst"
      role: "文献分析师"
      system_prompt: "你负责检索和分析相关文献资料。"
    - name: "data-analyst"
      role: "数据分析师"
      system_prompt: "你负责数据处理、统计分析和可视化。"
""",
    "development": """\
# AI Team OS 项目配置 — 开发团队模板
project:
  name: "dev-project"
  description: "软件开发项目"
  language: "zh"

defaults:
  model: "claude-opus-4-6"
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"
  memory_backend: "file"
  dashboard_port: 3000
  api_port: 8000

team:
  name: "dev-team"
  mode: "coordinate"
  leader:
    name: "tech-lead"
    role: "技术总监"
    system_prompt: "你是技术总监，负责任务拆解、代码审查和技术决策。"
  members:
    - name: "backend-dev"
      role: "后端开发"
      system_prompt: "你负责后端服务的设计与实现。"
    - name: "frontend-dev"
      role: "前端开发"
      system_prompt: "你负责前端界面的设计与实现。"
    - name: "qa-engineer"
      role: "测试工程师"
      system_prompt: "你负责编写测试用例并确保代码质量。"
""",
    "analysis": """\
# AI Team OS 项目配置 — 分析团队模板
project:
  name: "analysis-project"
  description: "数据分析项目"
  language: "zh"

defaults:
  model: "claude-opus-4-6"
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"
  memory_backend: "file"
  dashboard_port: 3000
  api_port: 8000

team:
  name: "analysis-team"
  mode: "coordinate"
  leader:
    name: "lead-analyst"
    role: "首席分析师"
    system_prompt: "你是首席分析师，负责制定分析方案并汇总结论。"
  members:
    - name: "data-collector"
      role: "数据采集员"
      system_prompt: "你负责数据收集和预处理。"
    - name: "data-modeler"
      role: "数据建模师"
      system_prompt: "你负责构建分析模型和深度分析。"
""",
}


# ============================================================
# 命令实现
# ============================================================


@app.callback(invoke_without_command=True)
def init(
    template: str | None = typer.Option(  # noqa: UP007
        None,
        "--template",
        "-t",
        help="项目模板 (research/development/analysis)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="强制覆盖已有配置"),
) -> None:
    """初始化项目，生成 aiteam.yaml 配置文件."""
    project_dir = Path.cwd()
    config_path = project_dir / CONFIG_FILENAME
    aiteam_dir = project_dir / AITEAM_DIR

    # 检查是否已存在配置
    if config_path.exists() and not force:
        overwrite = typer.confirm(f"{CONFIG_FILENAME} 已存在，是否覆盖？")
        if not overwrite:
            print_warning("已取消初始化")
            raise typer.Exit()

    # 确定配置内容
    if template:
        if template not in TEMPLATES:
            print_error(f"未知模板 '{template}'，可用模板: {', '.join(TEMPLATES.keys())}")
            raise typer.Exit(code=1)
        config_content = TEMPLATES[template]
    else:
        config_content = generate_default_config()

    # 写入配置文件
    config_path.write_text(config_content, encoding="utf-8")
    print_success(f"已生成 {CONFIG_FILENAME}")

    # 创建 .aiteam 目录
    aiteam_dir.mkdir(exist_ok=True)
    print_success(f"已创建 {AITEAM_DIR}/ 目录")

    if template:
        print_success(f"已使用 '{template}' 模板初始化项目")
    else:
        print_success("项目初始化完成，请编辑 aiteam.yaml 配置团队")
