"""AI Team OS — Workflow observability ingest (I3a).

CC ultracode/Workflow 观测层的纯摄取模块（API 层单副本，非 hook 双副本，规避红线5
同步坑与 install.py 注册漂移）。三个触发点（PostToolUse 回执、reaper 轮询、
SessionStart 对账）共用这里的纯函数：

- ``parse_workflow_receipt(text)``：正则抽 PostToolUse(Workflow) 回执四键。
- ``ingest_run_from_file(repo, event_bus, wf_json_path)``：读 ``wf_<id>.json`` 富快照
  → upsert run + 批量 upsert agents → 盖 team_id/os_agent_id → 回写 team.completed_at
  → emit ``workflow.completed``。幂等、全 try/except。
- ``reconcile(repo, event_bus, project_dir=None, session_id=None)``：proj-slug glob
  ``~/.claude/projects/<slug>/*/workflows/wf_*.json`` 逐文件 ingest。

关键口径：hook 只驱动「时机 + 关联锚点 + 生命周期事件」；文件是「全量遥测真相源」
（token/时长/逐-agent 返回值只在 ``wf_<id>.json.workflowProgress[]``）。两张投影表是
「不可变文件的可重建缓存」，按自然键 UPSERT 单调推进、绝不删行（红线3 append-only）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from aiteam.api.event_bus import EventBus
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent, WorkflowRun

logger = logging.getLogger(__name__)

# wf_<id> 运行 id（与 hook_translator._WF_RUN_ID_RE 同口径）。
_WF_RUN_ID_RE = re.compile(r"wf_[0-9a-z]+(?:-[0-9a-z]+)*", re.IGNORECASE)
# 回执逐行字段（每字段独占一行，用 .+ 抓到行尾再 strip，兼容含空格的 Summary）。
_TASK_ID_RE = re.compile(r"Task ID:\s*(\S+)")
_SUMMARY_RE = re.compile(r"Summary:\s*(.+)")
_TRANSCRIPT_RE = re.compile(r"Transcript dir:\s*(.+)")
_SCRIPT_RE = re.compile(r"Script file:\s*(.+)")


# ============================================================
# 纯工具
# ============================================================


def _to_int(v: Any) -> int:
    """把 str/int/float/None（快照里数值多为字符串）稳健转 int，失败得 0。"""
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _ms_to_dt(ms: int | None) -> datetime | None:
    """epoch 毫秒 → 本地 datetime；0/None/越界返回 None。"""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000)
    except (ValueError, OSError, OverflowError):
        return None


def _trim(s: str, n: int) -> str:
    """截断到 n 字符（防膨胀）。"""
    return s[:n] if s else ""


def _norm_phases(raw: Any) -> list[dict[str, Any]]:
    """把文件 phases（[{title,detail}]）或计划 phases（[str]）归一为 [{index,title}]。"""
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for i, ph in enumerate(raw, start=1):
            if isinstance(ph, dict):
                out.append(
                    {
                        "index": _to_int(ph.get("index")) or i,
                        "title": str(ph.get("title") or ""),
                    }
                )
            elif isinstance(ph, str):
                out.append({"index": i, "title": ph})
    return out


def _trim_result(result: Any, max_chars: int = 8000) -> dict[str, Any] | None:
    """终端 StructuredOutput 结果截断存（防超大 result 膨胀 DB）。"""
    if result is None:
        return None
    if not isinstance(result, dict):
        return {"_raw": str(result)[:max_chars]}
    try:
        s = json.dumps(result, ensure_ascii=False)
    except Exception:
        return {"_repr": str(result)[:max_chars]}
    if len(s) <= max_chars:
        return result
    return {"_truncated": True, "_preview": s[:max_chars]}


def _project_slug(path: str) -> str:
    """把项目 root_path 反解为 CC projects 目录 slug（每个非字母数字字符 → '-'）。

    例：``/Users/cronus/Desktop/AI team OS`` → ``-Users-cronus-Desktop-AI-team-OS``。
    CC 不折叠连续分隔符，故此处也逐字符替换、不折叠。
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", path or "")


def _claude_projects_dir() -> Path:
    """``~/.claude/projects`` 根目录（测试可 monkeypatch 此函数指向临时目录）。"""
    return Path.home() / ".claude" / "projects"


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _name_from_script(script_path: str, wf_id: str) -> str:
    """从脚本文件名反解 workflow 名：去 .js、去尾部 -wf_<id>。

    例：``cnipa-xml-format-research-wf_8e92fe01-67c.js`` → ``cnipa-xml-format-research``。
    """
    if not script_path:
        return ""
    base = script_path.replace("\\", "/").rsplit("/", 1)[-1]
    if base.endswith(".js"):
        base = base[:-3]
    suffix = f"-{wf_id}"
    if wf_id and base.endswith(suffix):
        base = base[: -len(suffix)]
    return base


def parse_workflow_receipt(text: str) -> dict[str, Any]:
    """从 PostToolUse(Workflow) 启动回执明文抽四键（+ transcript_dir、name）。

    回执样本（约 1331 字符明文，< 32KB 不被 send_event._trim_payload 截）：
        Workflow launched in background. Task ID: westwrtgj
        Summary: 多路并行调研...
        Transcript dir: /Users/.../subagents/workflows/wf_8e92fe01-67c
        Script file: /Users/.../workflows/scripts/<name>-wf_8e92fe01-67c.js

    Returns:
        {wf_id, cc_task_id, script_path, name, summary, transcript_dir}；抽不到留空串。
    """
    text = text or ""
    task_m = _TASK_ID_RE.search(text)
    summary_m = _SUMMARY_RE.search(text)
    transcript_m = _TRANSCRIPT_RE.search(text)
    script_m = _SCRIPT_RE.search(text)

    transcript_dir = transcript_m.group(1).strip() if transcript_m else ""
    script_path = script_m.group(1).strip() if script_m else ""

    # wf_id：优先从 transcript_dir，其次整段文本。
    wf_id = ""
    for src in (transcript_dir, script_path, text):
        m = _WF_RUN_ID_RE.search(src.replace("\\", "/"))
        if m:
            wf_id = m.group(0)
            break

    return {
        "wf_id": wf_id,
        "cc_task_id": task_m.group(1).strip() if task_m else "",
        "script_path": script_path,
        "name": _name_from_script(script_path, wf_id),
        "summary": summary_m.group(1).strip() if summary_m else "",
        "transcript_dir": transcript_dir,
    }


def run_json_path_from_transcript_dir(transcript_dir: str, wf_id: str) -> Path | None:
    """由回执的 Transcript dir 反推运行 JSON 路径。

    transcript_dir = ``<session>/subagents/workflows/wf_<id>``，运行 JSON 是其兄弟
    ``<session>/workflows/wf_<id>.json``（不在 subagents 下）。
    """
    if not transcript_dir or not wf_id:
        return None
    try:
        tdir = Path(transcript_dir)
        session_dir = tdir.parent.parent.parent
        return session_dir / "workflows" / f"{wf_id}.json"
    except Exception:
        return None


# ============================================================
# 文件摄取（全量遥测真相源）
# ============================================================


async def ingest_run_from_file(
    repo: StorageRepository,
    event_bus: EventBus,
    wf_json_path: str | Path,
) -> dict[str, Any]:
    """读 ``wf_<id>.json`` 富快照 → upsert run + agents → 回写 team → emit completed。

    幂等：可反复重跑（upsert by 自然键，emit 只在「新完成」时触发，避免事件翻倍）。
    全 try/except，绝不抛（供 hook/reaper best-effort 调用）。
    """
    path = Path(wf_json_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 文件读失败不再学裸 except: pass，落日志
        logger.warning("workflow ingest: read/parse failed %s: %s", path, exc)
        return {"ok": False, "reason": "read_error", "path": str(path)}

    if not isinstance(data, dict):
        return {"ok": False, "reason": "bad_json", "path": str(path)}

    wf_id = str(data.get("runId") or "").strip()
    if not wf_id:
        return {"ok": False, "reason": "no_runId", "path": str(path)}

    # team / project 关联（既有 workflow-<wf_id> 团队；OS 离线期无团队时留空）。
    team = None
    try:
        team = await repo.get_team_by_name(f"workflow-{wf_id}")
    except Exception:
        team = None
    team_id = team.id if team else None
    project_id = (getattr(team, "project_id", None) or "") if team else ""

    # session_id 从路径反解：<session>/workflows/wf_<id>.json。
    session_id: str | None = None
    try:
        session_id = path.parent.parent.name or None
    except Exception:
        session_id = None

    start_ms = _to_int(data.get("startTime"))
    dur_ms = _to_int(data.get("durationMs"))
    started_at = _ms_to_dt(start_ms)
    completed_at = _ms_to_dt(start_ms + dur_ms) if (start_ms and dur_ms) else None
    status = str(data.get("status") or "completed")
    cc_task_id_raw = str(data.get("taskId") or "").strip()

    run = WorkflowRun(
        wf_id=wf_id,
        project_id=project_id,
        team_id=team_id,
        session_id=session_id,
        cc_task_id=cc_task_id_raw or None,
        name=str(data.get("workflowName") or ""),
        status=status,
        source="file",
        phases=_norm_phases(data.get("phases")),
        agent_count=_to_int(data.get("agentCount")),
        total_tokens=_to_int(data.get("totalTokens")),
        total_tool_calls=_to_int(data.get("totalToolCalls")),
        duration_ms=dur_ms or None,
        summary=str(data.get("summary") or ""),
        result=_trim_result(data.get("result")),
        script_path=str(data.get("scriptPath") or ""),
        started_at=started_at,
        completed_at=completed_at,
    )

    # 事件去重护栏：只在「本次首次完成」emit workflow.completed。
    prev = None
    try:
        prev = await repo.get_workflow_run(wf_id)
    except Exception:
        prev = None
    was_completed = bool(prev and prev.status == "completed")

    try:
        await repo.upsert_workflow_run(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow ingest: run upsert failed wf=%s: %s", wf_id, exc)
        return {"ok": False, "reason": "run_upsert_failed", "wf_id": wf_id}

    # 批量 upsert 逐-agent 遥测（type=workflow_agent），并盖 os_agent_id 关联既有成员。
    agent_entries = [
        x
        for x in (data.get("workflowProgress") or [])
        if isinstance(x, dict) and x.get("type") == "workflow_agent"
    ]
    n = 0
    for a in agent_entries:
        cc_agent_id = str(a.get("agentId") or "").strip()
        os_agent_id = None
        if cc_agent_id:
            try:
                existing = await repo.find_agent_by_cc_id(cc_agent_id)
                os_agent_id = existing.id if existing else None
            except Exception:
                os_agent_id = None
        wa = WorkflowAgent(
            run_id=wf_id,
            wf_id=wf_id,
            project_id=project_id,
            cc_agent_id=cc_agent_id,
            os_agent_id=os_agent_id,
            label=str(a.get("label") or ""),
            phase_index=_to_int(a.get("phaseIndex")),
            phase_title=str(a.get("phaseTitle") or ""),
            model=str(a.get("model") or ""),
            state=str(a.get("state") or ""),
            tokens=_to_int(a.get("tokens")),
            tool_calls=_to_int(a.get("toolCalls")),
            duration_ms=_to_int(a.get("durationMs")) or None,
            last_tool_name=str(a.get("lastToolName") or ""),
            last_tool_summary=_trim(str(a.get("lastToolSummary") or ""), 500),
            prompt_preview=_trim(str(a.get("promptPreview") or ""), 2000),
            result_preview=_trim(str(a.get("resultPreview") or ""), 2000),
            started_at=_ms_to_dt(_to_int(a.get("startedAt"))),
            queued_at=_ms_to_dt(_to_int(a.get("queuedAt"))),
        )
        try:
            await repo.upsert_workflow_agent(wa)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow ingest: agent upsert failed wf=%s cc=%s: %s", wf_id, cc_agent_id, exc
            )

    # 顺带回填历史缺口：team.completed_at 恒 None → 用 startTime+durationMs 写回；
    # 对既有 nullable 字段的写入，非删除，合规（红线3）。
    if team is not None:
        try:
            updates: dict[str, Any] = {}
            if completed_at is not None and getattr(team, "completed_at", None) is None:
                updates["completed_at"] = completed_at
            if run.summary and not (getattr(team, "summary", "") or ""):
                updates["summary"] = run.summary[:500]
            if updates:
                await repo.update_team(team.id, **updates)
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow ingest: team backfill failed wf=%s: %s", wf_id, exc)

    emitted = False
    if status == "completed" and not was_completed:
        try:
            await event_bus.emit(
                "workflow.completed",
                f"workflow:{wf_id}",
                {
                    "wf_id": wf_id,
                    "name": run.name,
                    "status": status,
                    "agent_count": run.agent_count,
                    "total_tokens": run.total_tokens,
                    "total_tool_calls": run.total_tool_calls,
                    "duration_ms": run.duration_ms,
                    "team_id": team_id,
                    "project_id": project_id,
                    "source": "file",
                },
                entity_id=wf_id,
                entity_type="workflow",
            )
            emitted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow ingest: emit completed failed wf=%s: %s", wf_id, exc)

    return {
        "ok": True,
        "wf_id": wf_id,
        "agents": n,
        "status": status,
        "emitted": emitted,
        "new_completion": emitted,
    }


# ============================================================
# 对账（reaper 保底 + SessionStart/手动加速，共用同一 ingest 函数）
# ============================================================


async def reconcile(
    repo: StorageRepository,
    event_bus: EventBus,
    project_dir: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """扫 proj-slug 下所有 ``workflows/wf_*.json`` 逐文件 ingest（每文件独立 try/except）。

    Args:
        project_dir: 限定到该目录所属项目的 slug；None 则扫全部已注册项目。
        session_id: 限定到某会话 ``<slug>/<session_id>/workflows/``（hook 流量加速用）。

    Returns:
        {ingested（本次新完成计数）, updated（已完成再对账计数）, errors, scanned}。
    """
    ingested = 0
    updated = 0
    errors = 0
    scanned = 0

    try:
        projects = await repo.list_projects()
    except Exception:
        projects = []

    slug_set: set[str] = set()
    if project_dir:
        pd = _norm_path(project_dir)
        matched = None
        best = -1
        for p in projects:
            rp = _norm_path(p.root_path or "")
            if rp and (pd == rp or pd.startswith(rp + "/")) and len(rp) > best:
                matched = p
                best = len(rp)
        if matched and matched.root_path:
            slug_set.add(_project_slug(matched.root_path))
        else:
            # 未匹配到已注册项目时，直接对 cwd 反解 slug（覆盖尚未注册的项目）。
            slug_set.add(_project_slug(project_dir))
    else:
        for p in projects:
            if p.root_path:
                slug_set.add(_project_slug(p.root_path))

    # 稳态廉价短路：文件 mtime 未晚于该 run 最后一次 upsert（updated_at）即跳过读取/
    # 解析——终态文件不可变时全程只剩 stat 成本（SessionStart 全量对账不再空转 upsert）。
    # 刻意不用「终态即跳过」：resumeFromRunId 会原地重写同名 wf_<id>.json（killed→
    # completed），mtime 变新自然触发重新 ingest，离线缺口回填与 resume 转移都不受影响。
    last_ingest: dict[str, datetime] = {}
    try:
        for known in await repo.list_workflow_runs(limit=1000):
            if known.updated_at:
                last_ingest[known.wf_id] = known.updated_at
    except Exception:  # noqa: BLE001 — 预载失败则退化为全量 ingest（仍幂等）
        last_ingest = {}

    inner = (
        f"{session_id}/workflows/wf_*.json" if session_id else "*/workflows/wf_*.json"
    )
    base = _claude_projects_dir()
    seen: set[str] = set()
    for slug in slug_set:
        proj_dir = base / slug
        if not proj_dir.exists():
            continue
        try:
            files = sorted(proj_dir.glob(inner))
        except Exception:
            files = []
        for jf in files:
            key = str(jf)
            if key in seen:
                continue
            seen.add(key)
            scanned += 1
            prev_ingest = last_ingest.get(jf.stem)
            if prev_ingest is not None:
                try:
                    if datetime.fromtimestamp(jf.stat().st_mtime) <= prev_ingest:
                        continue  # 文件自上次入库后未变更：不读不解析不 upsert
                except OSError:
                    pass
            try:
                res = await ingest_run_from_file(repo, event_bus, jf)
            except Exception as exc:  # noqa: BLE001 — 单文件失败隔离，不阻断其余
                errors += 1
                logger.warning("reconcile: ingest raised %s: %s", jf, exc)
                continue
            if res.get("ok"):
                if res.get("emitted"):
                    ingested += 1
                else:
                    updated += 1
            else:
                errors += 1

    return {"ingested": ingested, "updated": updated, "errors": errors, "scanned": scanned}
