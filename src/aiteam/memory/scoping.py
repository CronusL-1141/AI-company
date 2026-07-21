"""Directory-fingerprint memory bucketing for unregistered project directories.

未注册目录（cwd 不匹配任何 OS 项目）的方向层记忆隔离。

背景（2026-07-21 爱维高串线事故根因之一）：memory.py 的 _resolve_scope_id 在
scope=project 且解析不到注册项目时静默回落到全局 "system" 桶，等于把「本目录的项目
记忆」广播成对所有会话生效的全局记忆。修法是给每个未注册目录一个由规范化 cwd 指纹
派生的临时桶 scope_id，做到「项目内继承，记忆隔离是目录的默认权利，不是注册的特权」。

设计要点：
- 桶前缀 "dir:" 便于人肉辨识（区别于真项目 id 与全局 "system"/"user"），也为将来
  注册转正时把临时桶记忆收编进项目桶留出识别锚点。
- 指纹权威唯一在服务端：写路径（POST /api/memories）、读路径（GET /api/memories）
  都从 X-Project-Dir 头经本函数推导，避免把公式复制进 hook 各副本造成静默漂移
  （静默漂移正是本次事故要根治的类别）。hook 只负责把 cwd 传到 API。
"""

from __future__ import annotations

import hashlib
import os

# 临时桶前缀——scope=project 但目录未注册时的 scope_id 命名空间。
DIR_BUCKET_PREFIX = "dir:"


def dir_bucket_scope_id(cwd: str) -> str:
    """Derive the direction-memory temp-bucket scope_id for an unregistered dir.

    规范化 = os.path.realpath 后的绝对路径（解符号链接 + 去相对段，且幂等：
    realpath(realpath(p)) == realpath(p)，所以传原始 cwd 或已规范化路径结果一致）。
    指纹 = sha1(规范化路径 UTF-8).hexdigest()[:16]，冠以 "dir:" 前缀。

    Args:
        cwd: 目录路径（通常来自 X-Project-Dir 头，或 hook 的当前工作目录）。

    Returns:
        形如 "dir:1a2b3c4d5e6f7a8b" 的桶 scope_id；cwd 为空/纯空白时返回 ""
        （调用方据此拒绝写入或回退为仅 global+user 读取）。
    """
    if not cwd or not cwd.strip():
        return ""
    normalized = os.path.realpath(cwd.strip())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{DIR_BUCKET_PREFIX}{digest}"
