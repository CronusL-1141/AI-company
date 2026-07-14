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

import asyncio
import json
import logging
import os
import re
import stat as stat_module
from datetime import datetime
from pathlib import Path
from typing import Any

from aiteam.api.event_bus import EventBus
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent, WorkflowRun

logger = logging.getLogger(__name__)

# —— Phase2 live 追踪常量（实测标定；改动须重跑 agent 行间隔分布再定，见设计 §10.3） ——
WF_STALL_SECONDS = 900  # interrupted 静止阈值：实测健康 agent 最长静默 173.8s
#                         (p99=77.6s, n=3378 间隔)；取 900s ≈ 5.2× 最大观测值——宁可迟判不误判
WF_INTERRUPTED_RECHECK_HOURS = 24  # interrupted 后继续复查窗口（终态自愈通道）；过窗老化
#                                    出 reaper 视野，稳态回归零 stat
WF_LIVE_TAIL_MAX_RUNS = 10  # 单 tick live tail run 数上限（_reap_cycle 30s 硬超时保护）
WF_AGENT_TAIL_BYTES = 65536  # agent jsonl 尾窗（单条 assistant 行远小于 64KB）

# 终态集合（与 repository._WF_STATUS_RANK 同秩语义对齐，红线8 不改 rank 本体）
_WF_TERMINAL_STATUSES = frozenset({"completed", "killed", "failed"})

# WP10 事件去重：per-wf_id 的进程内锁，串行化同一 run 的「upsert→判定 became_*」临界区。
# 三条驱动（reaper 对账 / SessionStart 对账 / hook 回执 ingest）在同进程同事件循环上
# 可在 await 点自由交错——纯事务内判定在跨连接的文件 WAL 下靠快照冲突中止兜底，但在
# 单连接拓扑（如内存库）下三协程可同时读到旧 status 各自判 became_completed=True →
# 重复 emit。加此锁使临界区严格串行，与连接拓扑无关地保证 exactly-once。跨 API 进程另
# 由 reaper 治理租约 + WAL 快照中止覆盖。dict 按 wf_id memo 化，随进程生命周期有界增长
# （每 run 一个轻量 Lock），进程重启即清——不设后台清理守护（对齐「无定时器」纪律）。
_WF_INGEST_LOCKS: dict[str, asyncio.Lock] = {}


def _wf_ingest_lock(wf_id: str) -> asyncio.Lock:
    """Return the per-run ingest lock, creating it on first use (single-threaded loop)."""
    lock = _WF_INGEST_LOCKS.get(wf_id)
    if lock is None:
        lock = asyncio.Lock()
        _WF_INGEST_LOCKS[wf_id] = lock
    return lock

# wf_<id> 运行 id（与 hook_translator._WF_RUN_ID_RE 同口径）。
# Bounded to a single optional dash-suffix, not unbounded `*` — see hook_translator.py's
# _WF_RUN_ID_RE comment for the worktree-suffix over-match this fixes (task f8207497).
_WF_RUN_ID_RE = re.compile(r"wf_[0-9a-z]+(?:-[0-9a-z]+)?", re.IGNORECASE)
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


def _claude_tmp_dir() -> Path:
    """CC 后台 Task 落地目录 ``/tmp/claude-<uid>``（macOS 上 /tmp → /private/tmp）。

    仅供 ``enrich_from_task_output`` 兜底读 ``tasks/<taskId>.output``；重启即清，
    绝非真相源。测试可 monkeypatch。
    """
    return Path(f"/tmp/claude-{os.getuid()}")


def _iso_to_local_naive(s: str) -> datetime | None:
    """agent jsonl 顶层 timestamp（UTC ISO，如 2026-06-24T06:54:31.209Z）→ 本地 naive。

    与库内其余 datetime（datetime.now()/fromtimestamp 本地 naive）口径对齐。
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _first_line_timestamp(path: Path) -> datetime | None:
    """agent jsonl 首行顶层 timestamp → started_at 近似（journal 行内无时间戳）。"""
    try:
        with path.open("rb") as f:
            line = f.readline()
        obj = json.loads(line)
        if isinstance(obj, dict):
            return _iso_to_local_naive(str(obj.get("timestamp") or ""))
    except Exception:  # noqa: BLE001 — best-effort，失败即无 started_at
        return None
    return None


def _first_user_prompt(path: Path, max_chars: int = 160) -> str:
    """读 agent transcript 头部，提取首条 user 消息文本作 running 期语义标签。

    嵌套 run 的 wf_<id>.json 迟写（终态才落盘），label 在 running 期恒空
    （3edd0dc1）——prompt 首行是活跃期唯一可得的语义信息。头部 64KB 内找不到
    或文件未就绪返回空串，下个 tick 重试。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
        for line in head.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("type") != "user":
                continue
            content = (d.get("message") or {}).get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    str(c.get("text") or "")
                    for c in content
                    if isinstance(c, dict)
                )
            else:
                continue
            text = " ".join(text.split())
            if text:
                return text[:max_chars]
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _last_assistant_ctx_tokens(path: Path) -> int | None:
    """agent jsonl 尾窗 64KB 反向找最后一条含 message.usage 的 assistant 行 → lastCtx。

    D1 裁决口径：input + cache_creation_input + cache_read_input + output 四字段和
    （与 wf_<id>.json 终态 per-agent tokens 直接对账：error agent 精确相等，done
    agent 仅偏高 3~12%）。否决跨轮累加（cache_read 重复计入膨胀 ~445 倍）。

    Returns:
        lastCtx int；尾窗内无 assistant usage / 读失败 → None（调用方保留旧值）。
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - WF_AGENT_TAIL_BYTES))
            data = f.read()
    except OSError:
        return None
    for raw in reversed(data.split(b"\n")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001 — 尾窗首段可能是半行，跳过
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        return (
            _to_int(usage.get("input_tokens"))
            + _to_int(usage.get("cache_creation_input_tokens"))
            + _to_int(usage.get("cache_read_input_tokens"))
            + _to_int(usage.get("output_tokens"))
        )
    return None


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


async def _upsert_agents_from_progress(
    repo: StorageRepository,
    wf_id: str,
    project_id: str,
    progress: Any,
    run_team_id: str | None = None,
) -> int:
    """workflowProgress[] 的 type=workflow_agent 条 → 批量 upsert workflow_agents。

    共享纯函数：``ingest_run_from_file``（wf_<id>.json 真相源）与
    ``enrich_from_task_output``（/tmp .output 兜底，7 键子集缺 runId）两处复用。
    顺带盖 os_agent_id 关联既有成员（agents.cc_tool_use_id == cc_agent_id）；
    run_team_id 给出时做「收尸迁移」：权威清单命中的 OS 成员若仍滞留
    workflow-session-* 兜底队（kill 中途永不 promote / 晚到错过回执迁移），
    迁入 run 队（2026-07-08 漏迁实录 wf-a8bda693e5）。
    """
    agent_entries = [
        x
        for x in (progress or [])
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
                existing = None
                os_agent_id = None
            if (
                existing is not None
                and run_team_id
                and getattr(existing, "team_id", None)
                and existing.team_id != run_team_id
            ):
                try:
                    cur = await repo.get_team(existing.team_id)
                    if cur is not None and cur.name.startswith("workflow-session-"):
                        await repo.update_agent(existing.id, team_id=run_team_id)
                except Exception:  # noqa: BLE001 — 收尸失败不阻塞遥测 upsert
                    pass
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
    return n


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
    # Phase2 fingerprint：read_text *之前* stat（TOCTOU 保守向：读后文件再变则存的
    # 是旧 fp → 下 tick 必不命中 → 重新 ingest，方向安全）。
    fp = ""
    try:
        st = path.stat()
        fp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        fp = ""
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

    # session_id 从路径反解：<session>/workflows/wf_<id>.json。
    session_id: str | None = None
    try:
        session_id = path.parent.parent.name or None
    except Exception:
        session_id = None

    # team / project 关联：优先既有 workflow-<wf_id> 团队；没有就认养本会话的
    # workflow-session-<sid> 兜底队（wf_id 迟到时 agents 全挂在那里——只按 wf 名
    # 找会造成「一 run 两队」碎片化）；OS 离线期两者皆无时留空。
    team = None
    try:
        team = await repo.get_team_by_name(f"workflow-{wf_id}")
        if team is None and session_id:
            fallback = await repo.get_team_by_name(
                f"workflow-session-{session_id[:8]}"
            )
            fb_wf = (fallback.config or {}).get("workflow_run_id") if fallback else None
            if fallback is not None and fb_wf in (None, "", wf_id):
                team = fallback
    except Exception:
        team = None
    team_id = team.id if team else None
    project_id = (getattr(team, "project_id", None) or "") if team else ""

    # 归属＝文件真相源（用户 2026-07-07 定案，废止"收纳进 OS"策略）：run 落盘
    # 所在 slug ↔ 已注册项目 root 的 slug 精确匹配；匹配到即覆盖 team 继承值
    #（team 可能被历史收纳策略吸错项目），匹配不到保持原值/留空不猜。
    try:
        _run_slug = path.parent.parent.parent.name
        for _p in await repo.list_projects():
            if _p.root_path and _project_slug(_p.root_path) == _run_slug:
                project_id = _p.id
                break
    except Exception:  # noqa: BLE001
        pass

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
        source_fingerprint=fp or None,  # None=不改（stat 失败时保留旧 fp）
    )

    # 事件去重护栏：只在「本次首次完成」emit workflow.completed；
    # killed/failed 首次入终态则 emit workflow.run_ingested（Phase2，此前静默）。
    # 「首次转移」的判定收进 upsert 事务内(读旧 status→写新 status 原子返回 became_*)，
    # 替代事务外先 get→was_completed 的 check-then-act；再套 per-wf 进程内锁把「upsert→
    # 拿 became_*」临界区串行化——只有一条驱动能拿到 running→completed 的跃迁，其余序后
    # upsert 必见 completed → became_completed=False。emit 在锁外按各自捕获的 became_*
    # 决策，故只有一条真正发射(审计 WP10：三条无串行驱动交错)。
    try:
        async with _wf_ingest_lock(wf_id):
            upsert_res = await repo.upsert_workflow_run(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow ingest: run upsert failed wf=%s: %s", wf_id, exc)
        return {"ok": False, "reason": "run_upsert_failed", "wf_id": wf_id}

    # 批量 upsert 逐-agent 遥测（type=workflow_agent），并盖 os_agent_id 关联既有成员
    # + 收尸迁移滞留兜底队的成员进 run 队。
    n = await _upsert_agents_from_progress(
        repo, wf_id, project_id, data.get("workflowProgress"), run_team_id=team_id
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
            # workflow 队归属跟随 run 的文件真相源（纠正历史收纳吸错的项目）
            if project_id and (getattr(team, "project_id", None) or "") != project_id:
                updates["project_id"] = project_id
            # 队状态跟随 run（2026-07-08 实录）：旁路会话的 SessionEnd 曾把仍在
            # running 的 run 的队误杀成 completed——run 在跑则复活 active（自愈
            # 存量误杀），run 终态则收敛 completed。与 SessionEnd 的 workflow 队
            # 豁免必须同批（否则 ingest 复活↔SessionEnd 再杀 ping-pong）。
            _t_status = str(getattr(team, "status", ""))  # 枚举 str 可能带类名前缀
            if status in _WF_TERMINAL_STATUSES and _t_status.endswith("active"):
                updates["status"] = "completed"
            elif status == "running" and _t_status.endswith("completed"):
                updates["status"] = "active"
            if updates:
                await repo.update_team(team.id, **updates)
            # 成员收工对称补全（2026-07-10 实锤 wf_811593ec 两成员 busy 滞留）：
            # SubagentStop 偶发丢失时终态 run 的成员没人转 offline，只能等
            # reaper 15min 心跳超时。run 终态即队内无活人，直接收工。
            # 排除 leader：历史 Leader 行可能寄生在 workflow 队（03fe7cae），
            # 它的 busy 属于活会话，不归本 run 收（轮35 手工清理误扫实锤）。
            if status in _WF_TERMINAL_STATUSES:
                for member in await repo.list_agents(team.id):
                    if str(getattr(member, "role", "")) == "leader":
                        continue
                    if str(getattr(member, "status", "")).endswith(("busy", "waiting")):
                        await repo.update_agent(member.id, status="offline")
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow ingest: team backfill failed wf=%s: %s", wf_id, exc)

    emitted = False
    if status == "completed" and upsert_res.became_completed:
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

    # Phase2：killed/failed 首次入终态 emit workflow.run_ingested（MVP 此前静默；
    # workflow.completed 语义原样不动，两者互斥——completed 走上面分支）。
    if status in ("killed", "failed") and upsert_res.became_terminal:
        try:
            await event_bus.emit(
                "workflow.run_ingested",
                f"workflow:{wf_id}",
                {
                    "wf_id": wf_id,
                    "name": run.name,
                    "status": status,
                    "agent_count": run.agent_count,
                    "total_tokens": run.total_tokens,
                    "duration_ms": run.duration_ms,
                    "team_id": team_id,
                    "project_id": project_id,
                    "source": "file",
                },
                entity_id=wf_id,
                entity_type="workflow",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow ingest: emit run_ingested failed wf=%s: %s", wf_id, exc)

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

    # 稳态廉价短路（每文件恰好 stat 一次，Phase2 fingerprint 先行、mtime 兜底）：
    # ① run 行 source_fingerprint 非空 → fp(mtime_ns:size) 相等即 skip、不等直接
    #    ingest（不再落 mtime 比较：同秒追加 mtime 相等但 size 变了会被 mtime 规则
    #    误跳过——(mtime_ns,size) 二元组严格强于纯 mtime，只增强不倒退）；
    # ② fp 为空（Phase2 前入库的老行）→ 沿用原 mtime ≤ updated_at 规则（MVP 逐字节
    #    回归）。刻意不用「终态即跳过」：resumeFromRunId 会原地重写同名 wf_<id>.json
    #    （killed→completed），fp/mtime 变新自然触发重新 ingest。
    last_ingest: dict[str, tuple[datetime | None, str]] = {}
    try:
        for known in await repo.list_workflow_runs(limit=1000):
            last_ingest[known.wf_id] = (known.updated_at, known.source_fingerprint or "")
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
            prev_time, prev_fp = last_ingest.get(jf.stem, (None, ""))
            if prev_time is not None or prev_fp:
                try:
                    st = jf.stat()
                except OSError:
                    st = None
                if st is not None:
                    if prev_fp:
                        # fingerprint 先行：相等=文件未变 skip；不等直接 ingest
                        if f"{st.st_mtime_ns}:{st.st_size}" == prev_fp:
                            continue
                    elif (
                        prev_time is not None
                        and datetime.fromtimestamp(st.st_mtime) <= prev_time
                    ):
                        continue  # 老行 mtime 兜底：文件自上次入库后未变更
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


# ============================================================
# Phase2 live 追踪（挂 reaper tick / 手动 reconcile；best-effort，单 run 失败隔离）
# ============================================================


async def _candidate_slugs(repo: StorageRepository, run: WorkflowRun) -> list[str]:
    """run 的 CC projects slug 候选：有 project_id 取其 root_path 反解；缺失/未命中
    则遍历全部已注册项目 slug（对齐 reconcile 的 slug_set 逻辑）。"""
    slugs: list[str] = []
    if run.project_id:
        try:
            proj = await repo.get_project(run.project_id)
        except Exception:
            proj = None
        root = getattr(proj, "root_path", "") if proj else ""
        if root:
            slugs.append(_project_slug(root))
    if not slugs:
        try:
            projects = await repo.list_projects()
        except Exception:
            projects = []
        for p in projects:
            if p.root_path:
                s = _project_slug(p.root_path)
                if s not in slugs:
                    slugs.append(s)
    # 跨项目修复B（窄回退，存量行专用）：老行无 transcript_dir 时，已注册项目的
    # slug 可能都不含其会话（未注册项目的 run）——按 session_id 全局反查一次目录。
    # 观察集（running ∪ 24h 窗内 interrupted）有界，成本可控；新行走 A 直接寻址
    # 不进此路径，稳态零 stat 语义不倒退。
    if run.session_id and not run.transcript_dir:
        try:
            for d in _claude_projects_dir().glob(f"*/{run.session_id}"):
                if d.is_dir():
                    s = d.parent.name
                    if s not in slugs:
                        slugs.append(s)
        except Exception:  # noqa: BLE001
            pass
    return slugs


def _find_wf_dir(
    base: Path, slugs: list[str], session_id: str | None, wf_id: str
) -> Path | None:
    """定位 live 转录目录 ``<slug>/<session>/subagents/workflows/<wf_id>/``。

    session_id 已知直接拼路径；缺失/未命中时 glob ``*/subagents/workflows/<wf_id>``。
    """
    for slug in slugs:
        proj_dir = base / slug
        if not proj_dir.exists():
            continue
        if session_id:
            cand = proj_dir / session_id / "subagents" / "workflows" / wf_id
            if cand.is_dir():
                return cand
        try:
            for cand in proj_dir.glob(f"*/subagents/workflows/{wf_id}"):
                if cand.is_dir():
                    return cand
        except Exception:  # noqa: BLE001 — 目录竞态等，继续下一 slug
            continue
    return None


async def live_wf_dir(repo: StorageRepository, run: WorkflowRun) -> Path | None:
    """live 转录目录定位（设计 §10.2 步骤④-1 的独立入口，测试/复用友好）。"""
    slugs = await _candidate_slugs(repo, run)
    return _find_wf_dir(_claude_projects_dir(), slugs, run.session_id, run.wf_id)


def _find_terminal_json(
    base: Path,
    slugs: list[str],
    session_id: str | None,
    wf_id: str,
    wf_dir: Path | None,
) -> Path | None:
    """终态快照 ``<session>/workflows/<wf_id>.json`` —— *存在* 才返回路径。

    wf_dir 已知时直接取其兄弟目录（<session>/subagents/workflows/<wf_id> 上三层）；
    否则按 slug+session 拼路径 / glob 兜底。
    """
    if wf_dir is not None:
        try:
            cand = wf_dir.parent.parent.parent / "workflows" / f"{wf_id}.json"
            if cand.exists():
                return cand
        except Exception:  # noqa: BLE001
            pass
    for slug in slugs:
        proj_dir = base / slug
        if not proj_dir.exists():
            continue
        if session_id:
            cand = proj_dir / session_id / "workflows" / f"{wf_id}.json"
            if cand.exists():
                return cand
        try:
            for cand in proj_dir.glob(f"*/workflows/{wf_id}.json"):
                return cand
        except Exception:  # noqa: BLE001
            continue
    return None


async def tail_live_run(
    repo: StorageRepository,
    event_bus: EventBus,
    run: WorkflowRun,
) -> dict[str, Any]:
    """对单个 running / interrupted(24h 窗内) run 做一轮 live 采集（§10.2 步骤④）。

    1) 终态 json 已落盘 → 直接 ingest_run_from_file 后返回（D3 终态覆盖 live）；
    2) journal.jsonl 字节 offset 增量 tail：只消费到最后一个 ``\\n``（并发写半行
       防护）；``st_size < offset`` ⇒ 文件被重写 ⇒ 复位 0；
    3) existing map 打底逐 agent 富化：mtime 未前进跳过重算；lastCtx token（D1）、
       首行 timestamp → started_at；journal 已 started 但无 agent jsonl → cached
       记 0；只覆写 state/tokens/started_at/last_activity_at，label/phase 保留；
    4) run 级水位 upsert（journal_offset / live_tokens / last_activity_at；status
       携空串不参与 rank）；**无变化不 upsert** → updated_at 冻结，interrupted
       过 24h 复查窗自然老化，稳态回归零 stat；
    5) interrupted 四条件判定（§10.5，只打标不删行；误判由终态 rank 2→3 自愈）；
    6) 聚合 emit workflow.agent_updated（每 run 每 tick 一条）/ workflow.run_ingested。
    """
    wf_id = run.wf_id
    base = _claude_projects_dir()
    # 跨项目修复A：回执持久化的 transcript_dir 优先直接寻址——不依赖项目注册、
    # 零 glob；目录被清理等失效场景回退 slug 候选（含 B 存量行会话反查）。
    wf_dir: Path | None = None
    slugs: list[str] = []
    if run.transcript_dir:
        try:
            _cand = Path(run.transcript_dir)
            wf_dir = _cand if _cand.is_dir() else None
        except Exception:  # noqa: BLE001
            wf_dir = None
    if wf_dir is None:
        slugs = await _candidate_slugs(repo, run)
        wf_dir = _find_wf_dir(base, slugs, run.session_id, wf_id)

    # 1. 终态优先：wf_<id>.json 已存在 → 文件值覆盖一切 live 近似
    #    （transcript_dir 已知时直接推兄弟路径，转录目录即使已被清理也能命中）
    json_path: Path | None = None
    if run.transcript_dir:
        _direct = run_json_path_from_transcript_dir(run.transcript_dir, wf_id)
        if _direct is not None and _direct.exists():
            json_path = _direct
    if json_path is None:
        json_path = _find_terminal_json(base, slugs, run.session_id, wf_id, wf_dir)
    if json_path is not None:
        res = await ingest_run_from_file(repo, event_bus, json_path)
        return {
            "ok": bool(res.get("ok")),
            "terminal": True,
            "status": str(res.get("status") or ""),
        }

    now = datetime.now()
    file_mtimes: list[datetime] = []
    journal_states: dict[str, str] = {}
    prev_offset = run.journal_offset or 0
    new_offset: int | None = None  # None = 本 tick 无 journal 信息，不动水位

    # 2. journal 增量 tail
    if wf_dir is not None:
        jpath = wf_dir / "journal.jsonl"
        try:
            jst = jpath.stat()
        except OSError:
            jst = None
        if jst is not None:
            file_mtimes.append(datetime.fromtimestamp(jst.st_mtime))
            offset = prev_offset
            if jst.st_size < offset:
                offset = 0  # 文件被重写 → 复位重新 tail
            new_offset = offset
            if jst.st_size > offset:
                chunk = b""
                try:
                    with jpath.open("rb") as f:
                        f.seek(offset)
                        chunk = f.read(jst.st_size - offset)
                except OSError:
                    chunk = b""
                nl = chunk.rfind(b"\n")
                if nl >= 0:
                    # 只消费到最后一个 \n；无换行尾段不解析、offset 不越过它
                    for raw in chunk[: nl + 1].split(b"\n"):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except Exception:  # noqa: BLE001 — 单行损坏跳过不中断
                            continue
                        if not isinstance(obj, dict):
                            continue
                        ccid = str(obj.get("agentId") or "").strip()
                        if not ccid:
                            continue
                        if obj.get("type") == "started":
                            journal_states[ccid] = "running"
                        elif obj.get("type") == "result":
                            journal_states[ccid] = "done"
                    new_offset = offset + nl + 1

    # agent-<ccid>.jsonl 清单（files_known=False 时零信息，绝不套 cached 记 0 规则）
    agent_files: dict[str, Path] = {}
    files_known = False
    if wf_dir is not None:
        try:
            for p in wf_dir.glob("agent-*.jsonl"):
                ccid = p.name[len("agent-") : -len(".jsonl")]
                if ccid:
                    agent_files[ccid] = p
            files_known = True
        except Exception:  # noqa: BLE001
            agent_files = {}

    # 3. existing 打底逐 agent 富化（防清零：只覆写本 tick 新算字段）
    try:
        existing = {
            a.cc_agent_id: a
            for a in await repo.list_workflow_agents(wf_id)
            if a.cc_agent_id
        }
    except Exception:  # noqa: BLE001
        existing = {}

    changed_agents = 0
    live_total = 0
    for ccid in set(existing) | set(journal_states) | set(agent_files):
        base_row = existing.get(ccid)
        fpath = agent_files.get(ccid)
        state_new = journal_states.get(ccid) or (base_row.state if base_row else "")
        tokens_new = (base_row.tokens if base_row else 0) or 0
        started_new = base_row.started_at if base_row else None
        act_new = base_row.last_activity_at if base_row else None
        prompt_new = (base_row.prompt_preview if base_row else "") or ""

        if fpath is not None:
            if not state_new:
                state_new = "running"  # 有转录文件即至少已启动
            try:
                fst = fpath.stat()
            except OSError:
                fst = None
            if fst is not None:
                fmt = datetime.fromtimestamp(fst.st_mtime)
                file_mtimes.append(fmt)
                prev_act = base_row.last_activity_at if base_row else None
                if prev_act is None or fmt > prev_act:
                    # 逐 agent 廉价水位：mtime 未前进则跳过 token 重算
                    ctx = _last_assistant_ctx_tokens(fpath)
                    if ctx is not None:
                        tokens_new = ctx
                    if started_new is None:
                        started_new = _first_line_timestamp(fpath)
                    act_new = fmt
            # running 期语义标签（3edd0dc1）：label 要等终态 wf json 才有，
            # 活跃期用 prompt 首行顶上；已有 label/preview 则零成本跳过。
            if not prompt_new and not (base_row.label if base_row else ""):
                prompt_new = _first_user_prompt(fpath)
        elif files_known and not tokens_new:
            # journal 已 started 但磁盘无 agent jsonl → 跨运行缓存命中，live 记 0
            # （D1 近似）；残余误差由终态文件覆盖（D3，合并规则 int 0 会写入）。
            # 护栏（V2 评审 major）：tokens>0 的行必来自 .output 兜底 enrich
            #（真·缓存命中恒为 0）——不清零，避免 live tick 抹掉已富化的真实遥测。
            tokens_new = 0

        live_total += tokens_new

        # —— running 期队成员收尸（2026-07-10 用户实锤三种失散）：SubagentStart
        # 事件偶发丢失 → 成员行缺失；wf_id 在 Start 时不可见 → 行滞留
        # workflow-session-* 兜底队且 promote 要等下个事件，长跑 agent 期间团队页
        # "少人/只见一个"。tail 手握本 run 权威 ccid 集与 team_id，每 tick 幂等补正；
        # 终态回执/对账仍是最终真相（本处只补行/归队，绝不动非兜底队的行）。
        if run.team_id and ccid:
            try:
                os_row = await repo.find_agent_by_cc_id(ccid)
                if os_row is None:
                    created = await repo.create_agent(
                        team_id=run.team_id,
                        name=f"wf-{ccid[:10]}",
                        # = hook_translator.WORKFLOW_AGENT_TYPE（字面量防循环 import）
                        role="workflow-subagent",
                        source="hook",
                        session_id=run.session_id or "",
                        cc_tool_use_id=ccid,
                        model="",
                    )
                    await repo.update_agent(
                        created.id,
                        status="busy" if state_new == "running" else "offline",
                        project_id=run.project_id or None,
                        last_active_at=act_new or now,
                    )
                else:
                    if os_row.team_id != run.team_id:
                        cur_team = (
                            await repo.get_team(os_row.team_id)
                            if os_row.team_id
                            else None
                        )
                        if cur_team is not None and str(
                            cur_team.name or ""
                        ).startswith("workflow-session-"):
                            await repo.update_agent(os_row.id, team_id=run.team_id)
                    # 活性触摸（2026-07-10 Wenge 实锤"WF 0 成员"）：workflow agent
                    # 长跑期间无人更新心跳，15min 被 reaper 误转 offline——journal
                    # 未见终态即为活着，触摸回 busy + last_active_at 前移（agent 真死
                    # 后 mtime 停滞，reaper 仍会正常收走，不会僵尸永生）。
                    if state_new == "running" and str(
                        getattr(os_row, "status", "")
                    ) not in ("busy",):
                        await repo.update_agent(
                            os_row.id,
                            status="busy",
                            last_active_at=act_new or now,
                        )
            except Exception as exc:  # noqa: BLE001 — 收尸失败不影响投影主链路
                logger.debug("live tail: member reap failed cc=%s: %s", ccid, exc)

        agent_changed = (
            base_row is None
            or state_new != (base_row.state or "")
            or tokens_new != (base_row.tokens or 0)
            or prompt_new != ((base_row.prompt_preview if base_row else "") or "")
            or (started_new is not None and base_row.started_at is None)
            or (
                act_new is not None
                and (
                    base_row.last_activity_at is None
                    or act_new > base_row.last_activity_at
                )
            )
        )
        if not agent_changed:
            continue
        wa = WorkflowAgent(
            run_id=wf_id,
            wf_id=wf_id,
            project_id=(base_row.project_id if base_row else "") or run.project_id or "",
            cc_agent_id=ccid,
            os_agent_id=base_row.os_agent_id if base_row else None,
            label=base_row.label if base_row else "",
            phase_index=base_row.phase_index if base_row else 0,
            phase_title=base_row.phase_title if base_row else "",
            model=base_row.model if base_row else "",
            state=state_new,
            tokens=tokens_new,
            tool_calls=(base_row.tool_calls if base_row else 0) or 0,
            duration_ms=base_row.duration_ms if base_row else None,
            last_tool_name=base_row.last_tool_name if base_row else "",
            last_tool_summary=base_row.last_tool_summary if base_row else "",
            prompt_preview=prompt_new,
            result_preview=base_row.result_preview if base_row else "",
            started_at=started_new,
            queued_at=base_row.queued_at if base_row else None,
            last_activity_at=act_new,
        )
        try:
            await repo.upsert_workflow_agent(wa)
            changed_agents += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: agent upsert failed wf=%s cc=%s: %s", wf_id, ccid, exc)

    # 4/5. run 级水位 + interrupted 四条件判定（§10.5，缺一不可）
    file_activity = max(file_mtimes) if file_mtimes else None
    offset_changed = new_offset is not None and new_offset != prev_offset
    tokens_changed = files_known and live_total != (run.live_tokens or 0)
    activity_advanced = file_activity is not None and (
        run.last_activity_at is None or file_activity > run.last_activity_at
    )
    run_changed = offset_changed or tokens_changed or activity_advanced

    # 判定活动基准：max(journal+agent jsonl mtime)；文件全缺 fallback 已存水位/
    # started_at/created_at（刚启动文件未落地时 created_at 很新 → 条件4 不满足）。
    judge_activity = (
        file_activity or run.last_activity_at or run.started_at or run.created_at
    )
    stall_seconds = (
        int((now - judge_activity).total_seconds()) if judge_activity else 0
    )
    mark_interrupted = (
        run.status == "running"  # 条件1：唯一入口秩
        and json_path is None  # 条件2：终态 json 不存在（存在时已在上方 return）
        and judge_activity is not None  # 条件3：活动基准可得
        and stall_seconds > WF_STALL_SECONDS  # 条件4：静止超阈值
    )

    if run_changed or mark_interrupted:
        try:
            await repo.upsert_workflow_run(
                WorkflowRun(
                    wf_id=wf_id,
                    project_id=run.project_id or "",
                    status="interrupted" if mark_interrupted else "",  # 空串不参与 rank
                    source="",  # 空串合并后保留原值
                    journal_offset=new_offset,  # None=不动；显式 0=复位（水位语义）
                    live_tokens=live_total if files_known else None,
                    last_activity_at=file_activity,  # 仓库侧单调取 max
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: run upsert failed wf=%s: %s", wf_id, exc)
            mark_interrupted = False
            run_changed = False

    # 6. 聚合事件（每 run 每 tick 至多各一条，绝不逐 agent 逐条发）
    if changed_agents:
        try:
            await event_bus.emit(
                "workflow.agent_updated",
                f"workflow:{wf_id}",
                {
                    "wf_id": wf_id,
                    "started": sum(1 for s in journal_states.values() if s == "running"),
                    "done": sum(1 for s in journal_states.values() if s == "done"),
                    "changed": changed_agents,
                    "project_id": run.project_id or "",
                },
                entity_id=wf_id,
                entity_type="workflow",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: emit agent_updated failed wf=%s: %s", wf_id, exc)

    if run_changed or mark_interrupted:
        payload: dict[str, Any] = {
            "wf_id": wf_id,
            "status": "interrupted" if mark_interrupted else run.status,
            "journal_offset": new_offset,
            "live_tokens": live_total if files_known else None,
            "last_activity_at": file_activity.isoformat() if file_activity else None,
            "project_id": run.project_id or "",
        }
        if mark_interrupted:
            payload["stall_seconds"] = stall_seconds
        try:
            await event_bus.emit(
                "workflow.run_ingested",
                f"workflow:{wf_id}",
                payload,
                entity_id=wf_id,
                entity_type="workflow",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: emit run_ingested failed wf=%s: %s", wf_id, exc)

    return {
        "ok": True,
        "terminal": False,
        "status": "interrupted" if mark_interrupted else run.status,
        "marked_interrupted": mark_interrupted,
        "agents_changed": changed_agents,
        "journal_offset": new_offset,
        "live_tokens": live_total if files_known else None,
    }


async def enrich_from_task_output(
    repo: StorageRepository,
    run: WorkflowRun,
) -> dict[str, Any]:
    """``/tmp/claude-<uid>/<slug>/*/tasks/<cc_task_id>.output`` 兜底富化（§10.6）。

    真 .output 是缺 runId 的 7 键子集 —— **绝不喂 ingest_run_from_file**（runId
    gate 必返 no_runId）；复用 ``_upsert_agents_from_progress`` 只富化 agents
    （label/phase/tokens——正是 journal 给不出的泳道数据）与 run 级 live_tokens/
    summary，status / total_tokens / agent_count 一概不动（终态列只归 wf-json）。

    分流（实测坑）：软链 = 普通 Task 的 transcript jsonl 必须跳过（lexists+lstat+
    S_ISLNK，悬空软链 open 会抛）；0 字节 = 任务在跑跳过；非 JSON = 纯文本日志跳过。
    /private/tmp 重启即清（仅单开机周期 best-effort），全 try/except。
    """
    wf_id = run.wf_id
    task_id = (run.cc_task_id or "").strip()
    if not task_id:
        return {"ok": False, "reason": "no_cc_task_id", "wf_id": wf_id}

    base = _claude_tmp_dir()
    slugs = await _candidate_slugs(repo, run)
    data: dict[str, Any] | None = None
    for slug in slugs:
        proj_dir = base / slug
        try:
            candidates = sorted(proj_dir.glob(f"*/tasks/{task_id}.output"))
        except Exception:  # noqa: BLE001
            candidates = []
        for cand in candidates:
            sp = str(cand)
            try:
                if not os.path.lexists(sp):
                    continue
                lst = os.lstat(sp)
                if stat_module.S_ISLNK(lst.st_mode):
                    continue  # 软链 = 普通 Task transcript，误解析会污染数据
                if lst.st_size == 0:
                    continue  # 任务在跑，快照未落
                parsed = json.loads(Path(sp).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — 纯文本子任务日志/半写 JSON，跳过
                continue
            if isinstance(parsed, dict) and parsed.get("workflowProgress"):
                data = parsed
                break
        if data is not None:
            break
    if data is None:
        return {"ok": False, "reason": "no_output", "wf_id": wf_id}

    n = await _upsert_agents_from_progress(
        repo, wf_id, run.project_id or "", data.get("workflowProgress")
    )

    total = _to_int(data.get("totalTokens"))
    summary = str(data.get("summary") or "")
    if total > 0 or summary:
        try:
            await repo.upsert_workflow_run(
                WorkflowRun(
                    wf_id=wf_id,
                    project_id=run.project_id or "",
                    status="",  # 不动 status
                    source="",
                    live_tokens=total if total > 0 else None,
                    summary=summary,  # 合并规则：非空才覆盖
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("output enrich: run upsert failed wf=%s: %s", wf_id, exc)

    return {"ok": True, "wf_id": wf_id, "agents": n}
