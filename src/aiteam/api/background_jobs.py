"""CC 背景任务观测 — 读 ``~/.claude/jobs/<short>/state.json``，零注册依赖。

CC 的 `--bg` 后台会话（daemon backend）不经任何 hook 注册进 OS：它们有自己的
sessionId、自己的 cwd，可以在用户关掉前台窗口之后继续跑。对 OS 来说这是一整
类看不见的在飞工作——项目摘要说"没人在干活"，而实际上有 daemon 在跑。

落盘形状（本机实测）::

    {"state":"done","tempo":"idle","template":"bg","backend":"daemon",
     "sessionId":"44fbf725-…","resumeSessionId":"…","daemonShort":"44fbf725",
     "cwd":"/…","createdAt":"2026-07-15T04:09:27.012Z",
     "updatedAt":"…","firstTerminalAt":"2026-07-15T11:41:17.040Z",
     "respawnFlags":["--permission-mode","auto","--model","fable"],"intent":""}

★"还在飞"的判据用 ``firstTerminalAt`` 是否缺席，**不猜 state 字符串**：本机
四条记录的 state 是 done / stopped，而 CC 二进制里根本没有一处 job 状态枚举
把这两个值一起列出——照着已知取值写白名单，遇到没见过的终态就会把死掉的任务
当成在飞的。缺席即在飞、出现即终态，这个判据不依赖取值集合。

只读、不写、不缓存；任何异常退化为空列表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiteam.clock import parse_utc


def _jobs_dir() -> Path:
    return Path.home() / ".claude" / "jobs"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return parse_utc(value)


@dataclass(frozen=True)
class BackgroundJob:
    job_id: str
    session_id: str
    state: str
    tempo: str
    backend: str
    cwd: str
    intent: str
    model: str
    created_at: datetime | None
    updated_at: datetime | None
    terminal_at: datetime | None

    @property
    def in_flight(self) -> bool:
        return self.terminal_at is None


def _model_from_flags(flags: object) -> str:
    """respawnFlags 是 ['--model', 'fable', …] 这样的扁平数组。"""
    if not isinstance(flags, list):
        return ""
    for i, flag in enumerate(flags):
        if flag == "--model" and i + 1 < len(flags):
            return str(flags[i + 1])
    return ""


def read_jobs() -> list[BackgroundJob]:
    """读全部后台任务记录，坏文件跳过。按更新时间倒序。"""
    jobs: list[BackgroundJob] = []
    try:
        entries = sorted(_jobs_dir().glob("*/state.json"))
    except OSError:
        return jobs
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        jobs.append(
            BackgroundJob(
                job_id=str(data.get("daemonShort") or path.parent.name),
                session_id=str(data.get("sessionId") or ""),
                state=str(data.get("state") or ""),
                tempo=str(data.get("tempo") or ""),
                backend=str(data.get("backend") or ""),
                cwd=str(data.get("cwd") or ""),
                intent=str(data.get("intent") or ""),
                model=_model_from_flags(data.get("respawnFlags")),
                created_at=_parse_iso(data.get("createdAt")),
                updated_at=_parse_iso(data.get("updatedAt")),
                terminal_at=_parse_iso(data.get("firstTerminalAt")),
            )
        )
    jobs.sort(key=lambda j: j.updated_at or datetime.min, reverse=True)
    return jobs


def in_flight_jobs(root_path: str = "") -> list[BackgroundJob]:
    """还在跑的后台任务；给了项目根就只算这个项目下的。"""
    jobs = [j for j in read_jobs() if j.in_flight]
    if not root_path:
        return jobs
    root = root_path.rstrip("/")
    return [j for j in jobs if j.cwd == root or j.cwd.startswith(root + "/")]


def as_dicts(jobs: list[BackgroundJob]) -> list[dict]:
    return [
        {
            "job_id": j.job_id,
            "session_id": j.session_id,
            "state": j.state,
            "tempo": j.tempo,
            "backend": j.backend,
            "cwd": j.cwd,
            "intent": j.intent,
            "model": j.model,
            "in_flight": j.in_flight,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "updated_at": j.updated_at.isoformat() if j.updated_at else None,
        }
        for j in jobs
    ]
