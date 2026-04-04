"""AI Team OS — Configuration management.

Responsible for loading and validating aiteam.yaml config files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from aiteam.types import OrchestrationMode

# ============================================================
# Configuration models
# ============================================================


class ProjectInfo(BaseModel):
    """Project basic info."""

    name: str = ""
    description: str = ""
    language: str = "zh"


class InfrastructureConfig(BaseModel):
    """Infrastructure configuration."""

    storage_backend: Literal["sqlite", "postgresql"] = "sqlite"
    memory_backend: Literal["file", "mem0"] = "file"
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    dashboard_port: int = 3000
    api_port: int = 8000
    db_url: str = ""

    def get_db_url(self, project_dir: Path) -> str:
        """Get database URL, defaults to SQLite in project directory."""
        if self.db_url:
            return self.db_url
        if self.storage_backend == "postgresql":
            return "postgresql+asyncpg://localhost/aiteam"
        return f"sqlite+aiosqlite:///{project_dir / '.aiteam' / 'aiteam.db'}"


class DefaultsConfig(BaseModel):
    """Default configuration."""

    model: str = "claude-opus-4-6"
    max_context_ratio: float = Field(default=0.8, ge=0.1, le=1.0)


class AgentConfig(BaseModel):
    """Agent configuration."""

    name: str
    role: str
    system_prompt: str = ""
    model: str | None = None


class TeamMemberConfig(BaseModel):
    """Team configuration."""

    name: str = ""
    mode: str = "coordinate"
    leader: AgentConfig | None = None
    members: list[AgentConfig] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        valid = [m.value for m in OrchestrationMode]
        if v not in valid:
            msg = f"Invalid orchestration mode '{v}', supported modes: {', '.join(valid)}"
            raise ValueError(msg)
        return v


class ProjectConfig(BaseModel):
    """Complete configuration model for aiteam.yaml."""

    project: ProjectInfo = Field(default_factory=ProjectInfo)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    infrastructure: InfrastructureConfig = Field(default_factory=InfrastructureConfig)
    team: TeamMemberConfig | None = None


# ============================================================
# Configuration loading
# ============================================================

CONFIG_FILENAME = "aiteam.yaml"
AITEAM_DIR = ".aiteam"

# ============================================================
# StateReaper timeout configuration
# ============================================================

REAPER_CHECK_INTERVAL = 60  # Reaper polling interval (seconds)
HOOK_SOURCE_TIMEOUT = 300  # hook-source agent heartbeat timeout (5min inactive -> offline)
API_SOURCE_TIMEOUT_WITH_FILE = 1200  # api-source timeout with team file (20 minutes)
API_SOURCE_TIMEOUT_NO_FILE = 600  # api-source timeout without team file (10 minutes)
MEETING_EXPIRY_MINUTES = 45  # Meeting auto-concludes after this many minutes without new messages
WATCHDOG_CHECK_INTERVAL = 60  # Watchdog patrol interval (seconds)
CLAUDE_HOME = "~/.claude"  # Claude Code home directory

# Notification / webhook configuration
# Set SLACK_WEBHOOK_URL in the environment or via the /api/settings/webhook API.
SLACK_WEBHOOK_URL: str = ""
# Events that trigger a webhook notification when SLACK_WEBHOOK_URL is configured.
NOTIFICATION_EVENTS: list[str] = [
    "task.completed",
    "task.failed",
    "briefing.added",
]

# Cost budget settings
COST_BUDGET_WEEKLY_USD: float = 50.0   # Weekly spend limit in USD
COST_ALERT_THRESHOLD: float = 0.8      # Alert when utilization >= 80%

# Wake Agent settings
MAX_CONCURRENT_WAKES: int = 2
WAKE_TIMEOUT_SECONDS: int = 300
WAKE_MAX_TURNS: int = 10
WAKE_FUSE_THRESHOLD: int = 3


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """Find aiteam.yaml by searching upward from current directory."""
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
    """Load and validate configuration file."""
    if config_path is None:
        config_path = find_config_file()
    if config_path is None or not config_path.exists():
        return ProjectConfig()

    with open(config_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    return ProjectConfig.model_validate(raw)


def generate_default_config() -> str:
    """Generate default aiteam.yaml content."""
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
