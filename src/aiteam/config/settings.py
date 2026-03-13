"""AI Team OS — 配置管理.

负责加载和验证 aiteam.yaml 配置文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from aiteam.types import OrchestrationMode

# ============================================================
# 配置模型
# ============================================================


class ProjectInfo(BaseModel):
    """项目基本信息."""

    name: str = ""
    description: str = ""
    language: str = "zh"


class InfrastructureConfig(BaseModel):
    """基础设施配置."""

    storage_backend: Literal["sqlite", "postgresql"] = "sqlite"
    memory_backend: Literal["file", "mem0"] = "file"
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    dashboard_port: int = 3000
    api_port: int = 8000
    db_url: str = ""

    def get_db_url(self, project_dir: Path) -> str:
        """获取数据库URL，默认使用项目目录下的SQLite."""
        if self.db_url:
            return self.db_url
        if self.storage_backend == "postgresql":
            return "postgresql+asyncpg://localhost/aiteam"
        return f"sqlite+aiosqlite:///{project_dir / '.aiteam' / 'aiteam.db'}"


class DefaultsConfig(BaseModel):
    """默认配置."""

    model: str = "claude-opus-4-6"
    max_context_ratio: float = Field(default=0.8, ge=0.1, le=1.0)


class AgentConfig(BaseModel):
    """Agent配置."""

    name: str
    role: str
    system_prompt: str = ""
    model: str | None = None


class TeamMemberConfig(BaseModel):
    """团队配置."""

    name: str = ""
    mode: str = "coordinate"
    leader: AgentConfig | None = None
    members: list[AgentConfig] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        valid = [m.value for m in OrchestrationMode]
        if v not in valid:
            msg = f"无效的编排模式 '{v}'，支持的模式: {', '.join(valid)}"
            raise ValueError(msg)
        return v


class ProjectConfig(BaseModel):
    """aiteam.yaml 的完整配置模型."""

    project: ProjectInfo = Field(default_factory=ProjectInfo)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    infrastructure: InfrastructureConfig = Field(default_factory=InfrastructureConfig)
    team: TeamMemberConfig | None = None


# ============================================================
# 配置加载
# ============================================================

CONFIG_FILENAME = "aiteam.yaml"
AITEAM_DIR = ".aiteam"

# ============================================================
# StateReaper 超时配置
# ============================================================

REAPER_CHECK_INTERVAL = 60  # 收割器轮询间隔（秒）
HOOK_SOURCE_TIMEOUT = 180  # hook-source agent超时（3分钟无活动→认为离线）
API_SOURCE_TIMEOUT_WITH_FILE = 1200  # api-source有团队文件时超时（20分钟）
API_SOURCE_TIMEOUT_NO_FILE = 600  # api-source无团队文件时超时（10分钟）
MEETING_EXPIRY_HOURS = 24  # 会议无新消息超过此时间自动结束（小时）
CLAUDE_HOME = "~/.claude"  # Claude Code 主目录


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """从当前目录向上查找 aiteam.yaml."""
    current = start_dir or Path.cwd()
    while True:
        config_path = current / CONFIG_FILENAME
        if config_path.exists():
            return config_path
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(config_path: Path | None = None) -> ProjectConfig:
    """加载并验证配置文件."""
    if config_path is None:
        config_path = find_config_file()
    if config_path is None or not config_path.exists():
        return ProjectConfig()

    with open(config_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    return ProjectConfig.model_validate(raw)


def generate_default_config() -> str:
    """生成默认的 aiteam.yaml 内容."""
    return """\
# AI Team OS 项目配置
project:
  name: "my-project"
  description: "项目描述"
  language: "zh"

defaults:
  model: "claude-opus-4-6"
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"     # sqlite | postgresql
  memory_backend: "file"        # file | mem0
  dashboard_port: 3000
  api_port: 8000

# team:
#   name: "dev-team"
#   mode: "coordinate"           # coordinate | broadcast | route | meet
#   leader:
#     name: "lead"
#     role: "技术总监"
#   members:
#     - name: "dev-1"
#       role: "后端开发"
#     - name: "dev-2"
#       role: "前端开发"
"""
