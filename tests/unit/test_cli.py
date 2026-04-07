"""AI Team OS — CLI命令单元测试."""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from aiteam.cli.app import app
from aiteam.config.settings import CONFIG_FILENAME

runner = CliRunner()


class TestVersion:
    """测试版本号输出."""

    def test_version_flag(self) -> None:
        """`aiteam --version` 输出版本号."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "AI Team OS" in result.output
        assert "1.2.0" in result.output

    def test_version_short_flag(self) -> None:
        """`aiteam -v` 同样输出版本号."""
        result = runner.invoke(app, ["-v"])
        assert result.exit_code == 0
        assert "AI Team OS" in result.output


class TestHelp:
    """测试帮助信息."""

    def test_main_help(self) -> None:
        """`aiteam --help` 显示帮助信息."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "AI Team OS" in result.output

    def test_no_args_shows_help(self) -> None:
        """`aiteam` 无参数显示帮助（Typer返回exit code 2）."""
        result = runner.invoke(app, [])
        # Typer的no_args_is_help返回exit code 2
        assert result.exit_code in (0, 2)
        assert "AI Team OS" in result.output


class TestInitCommand:
    """测试 init 命令."""

    def test_init_creates_yaml(self, tmp_path: Path) -> None:
        """`aiteam init` 生成 aiteam.yaml 文件."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        assert config_file.exists()
        content = config_file.read_text(encoding="utf-8")
        assert "my-project" in content
        assert "sqlite" in content

    def test_init_creates_aiteam_dir(self, tmp_path: Path) -> None:
        """`aiteam init` 创建 .aiteam/ 目录."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / ".aiteam").is_dir()

    def test_init_with_template_research(self, tmp_path: Path) -> None:
        """`aiteam init --template research` 包含研究团队配置."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "research"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        content = config_file.read_text(encoding="utf-8")
        assert "research-team" in content
        assert "lead-researcher" in content
        assert "literature-analyst" in content
        assert "data-analyst" in content

    def test_init_with_template_development(self, tmp_path: Path) -> None:
        """`aiteam init --template development` 包含开发团队配置."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "development"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        content = config_file.read_text(encoding="utf-8")
        assert "dev-team" in content
        assert "tech-lead" in content
        assert "backend-dev" in content

    def test_init_with_template_analysis(self, tmp_path: Path) -> None:
        """`aiteam init --template analysis` 包含分析团队配置."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "analysis"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        content = config_file.read_text(encoding="utf-8")
        assert "analysis-team" in content
        assert "lead-analyst" in content

    def test_init_invalid_template(self, tmp_path: Path) -> None:
        """`aiteam init --template invalid` 报错退出."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "invalid"])
        assert result.exit_code != 0

    def test_init_force_overwrite(self, tmp_path: Path) -> None:
        """`aiteam init --force` 强制覆盖已有配置."""
        os.chdir(tmp_path)
        # 先创建一个配置文件
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text("old content", encoding="utf-8")
        # 强制覆盖
        result = runner.invoke(app, ["init", "--force"])
        assert result.exit_code == 0
        content = config_file.read_text(encoding="utf-8")
        assert "my-project" in content
        assert "old content" not in content

    def test_init_existing_config_cancel(self, tmp_path: Path) -> None:
        """`aiteam init` 已有配置时用户取消."""
        os.chdir(tmp_path)
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text("old content", encoding="utf-8")
        # 输入 "n" 取消覆盖
        result = runner.invoke(app, ["init"], input="n\n")
        assert result.exit_code == 0
        # 内容未改变
        content = config_file.read_text(encoding="utf-8")
        assert content == "old content"


class TestSubcommandHelp:
    """测试子命令帮助信息."""

    def test_team_help(self) -> None:
        result = runner.invoke(app, ["team", "--help"])
        assert result.exit_code == 0

    def test_agent_help(self) -> None:
        result = runner.invoke(app, ["agent", "--help"])
        assert result.exit_code == 0

    def test_task_help(self) -> None:
        result = runner.invoke(app, ["task", "--help"])
        assert result.exit_code == 0

    def test_status_help(self) -> None:
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
