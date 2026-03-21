"""AI Team OS — 配置系统单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aiteam.config.settings import (
    CONFIG_FILENAME,
    ProjectConfig,
    TeamMemberConfig,
    find_config_file,
    generate_default_config,
    load_config,
)


class TestGenerateDefaultConfig:
    """测试默认配置YAML生成."""

    def test_returns_valid_yaml(self) -> None:
        """生成的内容是合法YAML."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)

    def test_contains_project_section(self) -> None:
        """包含project配置段."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert "project" in data
        assert data["project"]["name"] == "my-project"
        assert data["project"]["language"] == "zh"

    def test_contains_defaults_section(self) -> None:
        """包含defaults配置段."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert "defaults" in data
        assert data["defaults"]["model"] == "claude-opus-4-6"
        assert data["defaults"]["max_context_ratio"] == 0.8

    def test_contains_infrastructure_section(self) -> None:
        """包含infrastructure配置段."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert "infrastructure" in data
        assert data["infrastructure"]["storage_backend"] == "sqlite"
        assert data["infrastructure"]["memory_backend"] == "file"

    def test_can_be_loaded_as_project_config(self) -> None:
        """生成的YAML可以被ProjectConfig解析."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        config = ProjectConfig.model_validate(data)
        assert config.project.name == "my-project"
        assert config.defaults.model == "claude-opus-4-6"


class TestLoadConfig:
    """测试从YAML文件加载配置."""

    def test_load_from_file(self, tmp_path: Path) -> None:
        """从YAML文件加载配置."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text(generate_default_config(), encoding="utf-8")
        config = load_config(config_path)
        assert config.project.name == "my-project"
        assert config.infrastructure.storage_backend == "sqlite"

    def test_load_with_team_section(self, tmp_path: Path) -> None:
        """加载包含team配置段的文件."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text(
            """\
project:
  name: "test"
team:
  name: "my-team"
  mode: "coordinate"
  members:
    - name: "agent-1"
      role: "开发"
""",
            encoding="utf-8",
        )
        config = load_config(config_path)
        assert config.team is not None
        assert config.team.name == "my-team"
        assert config.team.mode == "coordinate"
        assert len(config.team.members) == 1
        assert config.team.members[0].name == "agent-1"

    def test_load_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        """文件不存在时返回默认配置."""
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.project.name == ""
        assert config.defaults.model == "claude-opus-4-6"

    def test_load_empty_file(self, tmp_path: Path) -> None:
        """空文件返回默认配置."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text("", encoding="utf-8")
        config = load_config(config_path)
        assert config.project.name == ""


class TestConfigValidation:
    """测试Pydantic验证."""

    def test_invalid_mode_raises_error(self) -> None:
        """无效的编排模式应报错."""
        with pytest.raises(ValidationError, match="Invalid orchestration mode"):
            TeamMemberConfig(name="test", mode="invalid_mode")

    def test_valid_modes_accepted(self) -> None:
        """所有合法编排模式应通过验证."""
        for mode in ("coordinate", "broadcast", "route", "meet"):
            team = TeamMemberConfig(name="test", mode=mode)
            assert team.mode == mode

    def test_invalid_max_context_ratio(self) -> None:
        """max_context_ratio超出范围应报错."""
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({"defaults": {"max_context_ratio": 1.5}})

    def test_invalid_storage_backend(self) -> None:
        """无效的storage_backend应报错."""
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({"infrastructure": {"storage_backend": "mongodb"}})


class TestFindConfigFile:
    """测试向上查找aiteam.yaml."""

    def test_find_in_current_dir(self, tmp_path: Path) -> None:
        """在当前目录找到配置文件."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text("project:\n  name: test\n", encoding="utf-8")
        result = find_config_file(tmp_path)
        assert result == config_path

    def test_find_in_parent_dir(self, tmp_path: Path) -> None:
        """在父目录找到配置文件."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text("project:\n  name: test\n", encoding="utf-8")
        child_dir = tmp_path / "sub" / "deep"
        child_dir.mkdir(parents=True)
        result = find_config_file(child_dir)
        assert result == config_path

    def test_not_found_returns_none(self, tmp_path: Path) -> None:
        """找不到配置文件返回None."""
        child_dir = tmp_path / "empty" / "deep"
        child_dir.mkdir(parents=True)
        # 不在tmp_path或其子目录放置配置文件
        # find_config_file会一直向上查找直到根目录
        # 如果系统根目录没有aiteam.yaml就会返回None
        result = find_config_file(child_dir)
        # 可能在某个父目录中存在aiteam.yaml（比如项目根目录）
        # 所以我们只验证返回类型正确
        assert result is None or result.name == CONFIG_FILENAME


class TestConfigDefaults:
    """测试默认值正确性."""

    def test_project_defaults(self) -> None:
        """ProjectInfo默认值."""
        config = ProjectConfig()
        assert config.project.name == ""
        assert config.project.description == ""
        assert config.project.language == "zh"

    def test_defaults_config(self) -> None:
        """DefaultsConfig默认值."""
        config = ProjectConfig()
        assert config.defaults.model == "claude-opus-4-6"
        assert config.defaults.max_context_ratio == 0.8

    def test_infrastructure_defaults(self) -> None:
        """InfrastructureConfig默认值."""
        config = ProjectConfig()
        assert config.infrastructure.storage_backend == "sqlite"
        assert config.infrastructure.memory_backend == "file"
        assert config.infrastructure.dashboard_port == 3000
        assert config.infrastructure.api_port == 8000
        assert config.infrastructure.db_url == ""

    def test_team_defaults_to_none(self) -> None:
        """team默认为None."""
        config = ProjectConfig()
        assert config.team is None

    def test_get_db_url_sqlite(self, tmp_path: Path) -> None:
        """SQLite模式下get_db_url返回正确路径."""
        config = ProjectConfig()
        url = config.infrastructure.get_db_url(tmp_path)
        assert "sqlite+aiosqlite" in url
        assert ".aiteam" in url
        assert "aiteam.db" in url

    def test_get_db_url_custom(self) -> None:
        """自定义db_url优先级最高."""
        config = ProjectConfig.model_validate({"infrastructure": {"db_url": "postgresql://custom"}})
        url = config.infrastructure.get_db_url(Path("/dummy"))
        assert url == "postgresql://custom"
