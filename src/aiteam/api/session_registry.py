"""CC 会话注册表桥接 — 读 ``~/.claude/sessions/<pid>.json``，零注册依赖。

CC v2.1.219 每个进程各自维护一份会话登记：

    ~/.claude/sessions/<pid>.json
    {"pid":32220,"sessionId":"80d0cc5e-…","cwd":"/…/AI team OS",
     "startedAt":1785116991072,"procStart":"Mon Jul 27 01:49:50 2026",
     "version":"2.1.219","kind":"interactive","entrypoint":"cli",
     "name":"ai-team-os-18","status":"busy","updatedAt":…,"statusUpdatedAt":…}

它比 OS 现有的两路会话信号都更权威：

- 比 ``~/.claude/teams/<team>/config.json`` 的 ``leadSessionId`` 新。队目录在
  建队那一刻盖章后**不再改名**，而同一个进程换会话（结束后重开、resume）时
  只有本文件跟着改 ``sessionId``。实测 pid 32220：队目录仍写
  ``session-0def8f84``，注册表已是 ``80d0cc5e``。
- 比 transcript mtime 直接。mtime 只能推断"最近有没有落盘"，本文件带
  ``status``（idle/busy）与真实 ``pid``，可以直接问进程死没死。

本模块只读、不写、不缓存，任何异常都退化为"不知道"（None / False），
绝不让探测失败反过来影响调用方。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# CC 写 sessions/<pid>.json 时对时间字段有两种写法（二进制内两条代码路径并存）：
# 纪元毫秒（updatedAt: Date.now()）与 ISO 串（updatedAt: new Date().toISOString()）。
# 两种都要认，认不出就留空。
_EPOCH_MS_MIN = 10**11


def _sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def _parse_ts(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)) and value > _EPOCH_MS_MIN:
            return datetime.fromtimestamp(value / 1000)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return None
    return None


@dataclass(frozen=True)
class SessionRecord:
    """一条 CC 会话登记。``procStart`` 刻意不解析 —— 它是 UTC 渲染的字符串，
    和 ``ps -o lstart=`` 的本地时间差一个时区，直接比对必然假性不符
    （实测同一进程：注册表 "Mon Jul 27 01:49:50"，ps "Mon Jul 27 09:49:50"）。
    需要开始时间请用 ``started_at``（由纪元毫秒解析，与 ps 对得上）。"""

    pid: int
    session_id: str
    cwd: str
    status: str
    kind: str
    name: str
    version: str
    entrypoint: str
    job_id: str
    started_at: datetime | None
    updated_at: datetime | None
    status_updated_at: datetime | None

    @property
    def is_background(self) -> bool:
        return self.kind == "bg"


def process_alive(pid: int) -> bool:
    """进程是否还在。

    ⚠️ 判据是 ``os.kill(pid, 0)``，**不防 pid 复用**：进程退出后 pid 被系统
    分配给别的程序时这里会误判为活。会话注册表目前只作并行观察（不参与任何
    下线决策），这个精度够用；在把它切成主判据之前必须先补进程身份校验
    （见 docs 里的双轨切换前置条件）。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于本用户
    except OSError:
        return False
    return True


def read_sessions() -> list[SessionRecord]:
    """读取全部会话登记，坏文件跳过。按 pid 升序返回，结果稳定可比对。"""
    records: list[SessionRecord] = []
    try:
        entries = sorted(_sessions_dir().glob("*.json"))
    except OSError:
        return records
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            pid = int(data.get("pid") or path.stem)
        except (TypeError, ValueError):
            continue
        records.append(
            SessionRecord(
                pid=pid,
                session_id=str(data.get("sessionId") or ""),
                cwd=str(data.get("cwd") or ""),
                status=str(data.get("status") or ""),
                kind=str(data.get("kind") or ""),
                name=str(data.get("name") or ""),
                version=str(data.get("version") or ""),
                entrypoint=str(data.get("entrypoint") or ""),
                job_id=str(data.get("jobId") or ""),
                started_at=_parse_ts(data.get("startedAt")),
                updated_at=_parse_ts(data.get("updatedAt")),
                status_updated_at=_parse_ts(data.get("statusUpdatedAt")),
            )
        )
    return records


def find_session(session_id: str) -> SessionRecord | None:
    """按 CC 会话 id 找登记。同一 id 出现在多个 pid 上时优先返回活着的那个。"""
    if not session_id:
        return None
    matches = [r for r in read_sessions() if r.session_id == session_id]
    if not matches:
        return None
    return next((r for r in matches if process_alive(r.pid)), matches[0])


def sessions_for_cwd(root_path: str) -> list[SessionRecord]:
    """某个项目根目录下的会话登记（含子目录会话），按启动时间倒序。"""
    if not root_path:
        return []
    root = root_path.rstrip("/")
    out = [
        r
        for r in read_sessions()
        if r.cwd == root or r.cwd.startswith(root + "/")
    ]
    out.sort(key=lambda r: r.started_at or datetime.min, reverse=True)
    return out


def pid_for_session(session_id: str) -> int | None:
    """会话属于哪个 CC 进程；登记里没有这条就是 None。

    只认注册表这一条**精确键匹配**（``sessionId`` 相等），刻意不做任何
    cwd / 启动时间窗的近似认亲：本机实测 pid 32147 与 32220 在同一个 cwd 下
    相隔 1.0 秒启动，而队目录的 createdAt 正好落在两者之间——近似匹配在真实
    数据上就是二义的（2026-07-27 用户裁定否掉该路线）。

    注意它只答得出**当前**会话：CC 每进程一份登记，进程换会话时该文件原地改写
    ``sessionId``，旧值不留痕。所以历史会话在这里一律查不到，调用方必须把
    None 当作"不知道"而不是"不属于任何进程"。
    """
    record = find_session(session_id)
    return record.pid if record is not None else None


def session_alive(session_id: str) -> bool | None:
    """会话是否活着：True/False = 注册表说了算；None = 注册表里根本没有这条。

    None 与 False 必须分开——"没登记"多半是老会话或非 CC 来源（测试、
    workflow 注入），不是"已经死了"，调用方不该拿它当死亡证据。
    """
    record = find_session(session_id)
    if record is None:
        return None
    return process_alive(record.pid)
