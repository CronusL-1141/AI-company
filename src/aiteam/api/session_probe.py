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

from aiteam.api import agent_context

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


def session_last_active(root_path: str, session_id: str) -> datetime | None:
    """Return the transcript file mtime for one specific session, or None if absent.

    File truth source: the main-session transcript at
    ``~/.claude/projects/<slug>/<session_id>.jsonl`` has its mtime bumped on every
    message CC writes. This exposes a per-session liveness read so the fleet layer
    can judge session death by file mtime (more authoritative than process liveness,
    since ``claude --resume`` spins up a fresh process anyway). Returns None on any
    failure so callers never break on a probe.
    """
    try:
        if not root_path or not session_id:
            return None
        p = _claude_projects_dir() / project_slug(root_path) / f"{session_id}.jsonl"
        if not p.is_file():
            return None
        return datetime.fromtimestamp(p.stat().st_mtime)
    except Exception:  # noqa: BLE001 — probe failure must not affect callers
        return None


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


# 预置 CEO 英文名单（用户裁定 2026-07-10：同项目多会话并行时，每个 session
# 显示为 CEO-<英文名> 而非单一 "Leader"；名字从名单选取且不重复）。
CEO_NAMES = [
    "Atlas", "Nova", "Orion", "Vega", "Lyra", "Miles", "Iris", "Felix",
    "Luna", "Hugo", "Cleo", "Jasper", "Wren", "Silas", "Freya", "Kai",
    "Elara", "Rowan", "Thea", "Ezra", "Selene", "Otto", "Nadia", "Remy",
]


def _assign_ceo_names(session_ids: list[str]) -> dict[str, str]:
    """确定性分配不重复的 CEO 名：md5(session_id) 映射名单 + 开放寻址防撞。

    按 session_id 字典序处理，同一批会话的分配结果稳定（刷新不换名），
    无需持久化状态。名单耗尽（并行 >24 会话）退化为 session 前缀。
    """
    import hashlib

    taken: set[str] = set()
    result: dict[str, str] = {}
    n = len(CEO_NAMES)
    for sid in sorted(session_ids):
        h = int(hashlib.md5(sid.encode()).hexdigest(), 16)
        name = ""
        for i in range(n):
            cand = CEO_NAMES[(h + i) % n]
            if cand not in taken:
                name = cand
                break
        if not name:
            name = sid[:6]
        taken.add(name)
        result[sid] = name
    return result


def detect_live_sessions(root_path: str) -> list[dict]:
    """探测项目目录下全部活跃 CC 主会话（15min 窗内），按 mtime 降序。

    多会话并行时每 session 一条；全部静默时返回最新一条（live=False），
    保持"项目页永远能看到最近一次会话"的语义。每条带确定性 CEO 英文名。
    纯文件系统读取，不查 DB、不依赖 hook 注册。找不到返回空列表。
    """
    try:
        if not root_path:
            return []
        pdir = _claude_projects_dir() / project_slug(root_path)
        if not pdir.is_dir():
            return []
        entries: list[tuple[Path, float]] = []
        for f in pdir.glob("*.jsonl"):
            try:
                entries.append((f, f.stat().st_mtime))
            except OSError:
                continue
        if not entries:
            return []
        entries.sort(key=lambda e: e[1], reverse=True)
        now = datetime.now()
        chosen = [
            e for e in entries
            if (now - datetime.fromtimestamp(e[1])) < LIVE_WINDOW
        ] or entries[:1]
        names = _assign_ceo_names([f.stem for f, _ in chosen])
        result = []
        for f, mt in chosen:
            last_active = datetime.fromtimestamp(mt)
            # 主会话上下文水位（fleet 层 P2 观测，见 docs/fleet-layer-design.md §6.2）：
            # 复用 agent-reuse 批次 1B 抽出的 read_ctx_tokens，同一口径作用于主会话
            # transcript（此前只覆盖子 agent）。读失败一律留空，不影响会话探测本身。
            ctx_tokens = agent_context.read_ctx_tokens(f)
            ctx_window: int | None = None
            ctx_pct: float | None = None
            if ctx_tokens is not None:
                ctx_window, ctx_pct = agent_context.compute_window_pct(ctx_tokens)
            result.append({
                "session_id": f.stem,
                "name": names[f.stem],
                "model": read_session_model(str(f)),
                "last_active_at": last_active.isoformat(),
                "live": (now - last_active) < LIVE_WINDOW,
                "ctx_tokens": ctx_tokens,
                "ctx_window": ctx_window,
                "ctx_pct": ctx_pct,
            })
        return result
    except Exception:  # noqa: BLE001 — 探测失败不影响调用方
        return []


def detect_live_session(root_path: str) -> dict | None:
    """单会话兼容入口：复数版首条（最新会话）。"""
    sessions = detect_live_sessions(root_path)
    return sessions[0] if sessions else None
