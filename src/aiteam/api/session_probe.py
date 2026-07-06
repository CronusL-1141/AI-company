"""会话探测 — 文件真相源直读，零注册依赖。

用户裁定（2026-07-07）：Leader 就是"在此项目目录下启动的 CC session"，
其模型/活跃状态应由后端自动检测，而不是让 leader 经 hook 链注册进 DB 再展示。
数据一直都在文件系统里：

    ~/.claude/projects/<slug>/<session-uuid>.jsonl   ← 主会话 transcript

- 文件 mtime = 最后活跃时间（CC 每条消息落盘即更新）
- 尾部最后一条 assistant 行的 message.model = 当前真实模型
  （/model 随时切换也能跟上；排除 compact 合成行 "<synthetic>"）
- 子 agent / workflow journal 在 <slug>/<session-uuid>/ 子目录内，
  顶层 glob("*.jsonl") 天然只命中主会话，无需再区分。

hook 注册链继续负责活动流水与事件，但展示层的 Leader 身份不再依赖它。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

# CC 在 compact 等场景写入的合成 assistant 行标记，不是真实模型
SYNTHETIC_MODEL = "<synthetic>"

# 与观测层口径一致：15 分钟内有落盘即视为"进行中"
LIVE_WINDOW = timedelta(minutes=15)

_TAIL_BYTES = 200_000


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def project_slug(root_path: str) -> str:
    """与 CC 的目录命名逐字符一致（含中文等非 ASCII 均替换为 '-'）。"""
    import re

    return re.sub(r"[^a-zA-Z0-9]", "-", root_path)


def read_session_model(transcript_path: str) -> str:
    """尾读主会话 transcript，取最后一条真实 assistant 消息的 model。

    尾部 200KB 内向后覆盖扫描；跳过 compact 合成行（model="<synthetic>"）。
    失败/缺失一律返回空串，绝不抛出。
    """
    try:
        if not transcript_path:
            return ""
        p = Path(transcript_path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > _TAIL_BYTES:
                f.seek(size - _TAIL_BYTES)
            data = f.read().decode("utf-8", errors="replace")
        model = ""
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001 — seek 截断的首行等
                continue
            if d.get("type") == "assistant":
                m = (d.get("message") or {}).get("model")
                if m and str(m) != SYNTHETIC_MODEL:
                    model = str(m)
        return model
    except Exception:  # noqa: BLE001
        return ""


def detect_live_session(root_path: str) -> dict | None:
    """探测项目目录下最新的 CC 主会话：session_id / 模型 / 活跃度。

    纯文件系统读取，不查 DB、不依赖 hook 注册。找不到返回 None。
    """
    try:
        if not root_path:
            return None
        pdir = _claude_projects_dir() / project_slug(root_path)
        if not pdir.is_dir():
            return None
        newest: Path | None = None
        newest_mtime = 0.0
        for f in pdir.glob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt > newest_mtime:
                newest, newest_mtime = f, mt
        if newest is None:
            return None
        last_active = datetime.fromtimestamp(newest_mtime)
        return {
            "session_id": newest.stem,
            "model": read_session_model(str(newest)),
            "last_active_at": last_active.isoformat(),
            "live": (datetime.now() - last_active) < LIVE_WINDOW,
        }
    except Exception:  # noqa: BLE001 — 探测失败不影响调用方
        return None
