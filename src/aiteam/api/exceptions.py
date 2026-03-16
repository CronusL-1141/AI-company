"""AI Team OS — 自定义异常."""

from __future__ import annotations


class NotFoundError(ValueError):
    """资源不存在异常 — 映射为HTTP 404."""
