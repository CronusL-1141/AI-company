"""事务化任务管理：关系、等待、乐观并发、完成补偿与批量幂等。"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import load_only

from aiteam.clock import utc_now
from aiteam.storage.connection import get_session
from aiteam.storage.models import AgentModel, TaskCreateBatchModel, TaskModel, TeamModel
from aiteam.types import Task, TaskStatus, TaskWaitingOn

MANAGEMENT_KEY = "task_management"


class TaskManagementError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def management(config: dict[str, Any] | None) -> dict[str, Any]:
    value = config.get(MANAGEMENT_KEY, {}) if isinstance(config, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _set_management(row: TaskModel, values: dict[str, Any]) -> None:
    # Reassign JSON, preserving frozen config.memo and all other unrelated keys.
    original = row.config
    config = original if isinstance(original, dict) else (
        {"legacy_config_value": original} if original is not None else {}
    )
    row.config = {**config, MANAGEMENT_KEY: values}


def _validate_relationships(
    rows: dict[str, TaskModel], changed: list[TaskModel], new_parent_ids: set[str],
) -> None:
    for row in changed:
        if row.parent_id or row.depends_on:
            if not row.project_id:
                raise TaskManagementError(400, "任务关系需要明确的项目归属")
        if row.parent_id and row.id in new_parent_ids:
            parent = rows.get(row.parent_id)
            if parent is None:
                raise TaskManagementError(400, f"父任务 {row.parent_id} 不存在")
            if parent.id == row.id:
                raise TaskManagementError(400, "任务不能以自身为父任务")
            if parent.project_id != row.project_id:
                raise TaskManagementError(400, "父任务必须归属目标项目，不能跨项目或使用无归属父任务")
            if parent.parent_id is not None or parent.depth != 0:
                raise TaskManagementError(400, "父任务必须是顶层任务，当前任务墙仅支持一层子任务")
            if row.id in new_parent_ids and parent.status == TaskStatus.COMPLETED.value:
                raise TaskManagementError(400, "已完成的父任务不能新增子任务，请先重新打开父任务")
            if any(t.parent_id == row.id for t in rows.values()):
                raise TaskManagementError(400, "有子任务的顶层任务不能改挂为子任务")
        row.depth = 1 if row.parent_id else 0
        row.depends_on = list(dict.fromkeys(row.depends_on or []))
        for dep_id in row.depends_on:
            dependency = rows.get(dep_id)
            if dependency is None:
                raise TaskManagementError(400, f"依赖任务 {dep_id} 不存在")
            if dependency.project_id != row.project_id:
                raise TaskManagementError(400, "依赖任务必须归属目标项目")

    # Parent completion waits for its children, so child -> parent dependencies
    # are also cycles. This catches deadlocks spanning the two relationship kinds.
    graph = {row.id: list(row.depends_on or []) for row in rows.values()}
    for row in rows.values():
        if row.parent_id in graph:
            graph[row.parent_id].append(row.id)
    done: set[str] = set()
    for start in changed:
        stack: list[tuple[str, bool]] = [(start.id, False)]
        visiting: set[str] = set()
        while stack:
            node, leaving = stack.pop()
            if leaving:
                visiting.remove(node)
                done.add(node)
                continue
            if node in visiting:
                raise TaskManagementError(400, "父子或依赖关系形成循环")
            if node in done:
                continue
            visiting.add(node)
            stack.append((node, True))
            stack.extend((target, False) for target in graph.get(node, []))


def _apply_state(
    row: TaskModel, rows: dict[str, TaskModel], patch: dict[str, Any], *, creating: bool,
    old_dependencies: list[str] | None = None,
) -> list[str]:
    info = management(row.config)
    previous_waiting = info.get("waiting_on")
    if previous_waiting:
        previous_waiting = TaskWaitingOn.model_validate(previous_waiting).model_dump(mode="json")
    old_status = row.status
    warnings: list[str] = []
    if "waiting_on" in patch:
        waiting = patch["waiting_on"]
        if waiting is not None:
            try:
                waiting = TaskWaitingOn.model_validate(waiting).model_dump(mode="json")
            except ValidationError as exc:
                raise TaskManagementError(400, str(exc)) from exc
            if waiting["kind"] == "agent" and not waiting.get("target_id"):
                raise TaskManagementError(400, "等待 agent 需要 target_id")
            if not waiting.get("reason", "").strip():
                raise TaskManagementError(400, "等待对象需要明确的 reason")
        info["waiting_on"] = waiting
    if "status_note" in patch:
        info["status_note"] = patch["status_note"] or ""
    requested = patch.get("status", row.status)
    if creating and patch.get("auto_start"):
        requested = TaskStatus.RUNNING.value
    try:
        requested = TaskStatus(requested).value
    except ValueError as exc:
        raise TaskManagementError(400, f"无效的 status 值: {requested}") from exc
    missing = [dep for dep in row.depends_on or [] if dep not in rows or rows[dep].status != "completed"]
    waiting = info.get("waiting_on")
    explicit_status = "status" in patch
    if creating and patch.get("status") == "running" and (waiting or missing):
        raise TaskManagementError(409, {
            "message": "显式创建 running 任务前需先满足依赖并清除等待；auto_start 可用于按条件推导状态",
            "blocked_by": missing, "waiting_on": waiting,
        })
    if requested == "completed":
        children = [t for t in rows.values() if t.parent_id == row.id and t.status != "completed"]
        new_conflict = ("waiting_on" in patch and waiting and waiting != previous_waiting) or (
            "depends_on" in patch and any(dep not in (old_dependencies or []) for dep in missing)
        )
        enforce = creating or old_status != "completed" or bool(new_conflict)
        if children and enforce:
            raise TaskManagementError(409, {
                "message": "父任务尚有未完成子任务",
                "open_subtasks": [{"id": c.id, "title": c.title, "status": c.status} for c in children],
            })
        if (waiting or missing) and enforce:
            raise TaskManagementError(409, {"message": "任务仍在等待，不能完成", "blocked_by": missing,
                                            "waiting_on": waiting})
        if not enforce and (children or waiting or missing):
            warnings.append("任务已完成但存在历史未关子任务/等待/依赖矛盾，保留完成事实并补检查下游")
        if creating or old_status != "completed":
            row.completed_at = utc_now()
        elif row.completed_at is None:
            warnings.append("旧完成任务的完成时间未知，保留未知，未以当前时间补造")
        info["block_reason"] = None
    elif requested in ("pending", "running", "blocked"):
        waiting_changed = "waiting_on" in patch and waiting != previous_waiting
        if not creating and explicit_status and requested == "running" and waiting:
            raise TaskManagementError(409, {
                "message": "任务仍在等待，开始执行前请明确清除 waiting_on",
                "waiting_on": waiting,
            })
        newly_missing = [dep for dep in missing if dep not in (old_dependencies or [])]
        if not creating and requested == "running" and missing:
            if old_status != "running" or newly_missing:
                raise TaskManagementError(409, {
                    "message": "未完成依赖不能直接开始执行；运行中新增依赖请显式暂停后修改",
                    "blocked_by": missing,
                })
            warnings.append("运行任务的已有依赖已重开，保留 running 执行事实；如需暂停请显式更新状态")
        if waiting:
            if not creating and old_status == "running" and requested == "running" and not waiting_changed:
                warnings.append("运行任务存在历史等待记录，保留 running 执行事实；请明确清除等待或显式暂停")
            else:
                requested, info["block_reason"] = "blocked", "waiting"
        elif requested == "blocked" and explicit_status and patch.get("block_reason") != "dependency":
            info["block_reason"] = "manual"
        elif not creating and old_status == "blocked" and not explicit_status:
            reason = info.get("block_reason")
            if reason == "waiting" and "waiting_on" in patch:
                requested = "blocked" if missing else "pending"
                info["block_reason"] = "dependency" if missing else None
            elif reason == "dependency":
                requested = "blocked" if missing else "pending"
                info["block_reason"] = "dependency" if missing else None
            elif reason is None:
                warnings.append("旧 blocked 任务缺少阻塞原因，保留状态；请明确等待对象或显式更新 status")
        elif missing and not (not creating and old_status == "running" and requested == "running"):
            requested, info["block_reason"] = "blocked", "dependency"
        else:
            info["block_reason"] = "manual" if requested == "blocked" else None
        if requested == "running" and (creating or old_status != "running"):
            row.started_at = utc_now()
        if old_status == "completed" and requested != "completed":
            row.completed_at = None
    else:
        info["block_reason"] = None
    row.status = requested
    if old_status == "completed" and requested != "completed":
        row.completed_at = None
    info["revision"] = int(info.get("revision", 0)) + 1
    info.setdefault("status_note", "")
    info.setdefault("waiting_on", None)
    _set_management(row, info)
    return warnings


class TaskManagement:
    def __init__(self, repo: Any):
        self.repo = repo

    async def _waiting_target(self, session, project_id: str | None, patch: dict) -> None:
        waiting = patch.get("waiting_on")
        if waiting and waiting.get("kind") == "agent":
            agent = await session.get(AgentModel, waiting.get("target_id"))
            owner = agent.project_id if agent else None
            if agent and not owner:
                team = await session.get(TeamModel, agent.team_id)
                owner = team.project_id if team else None
            if not agent or not project_id or owner != project_id:
                raise TaskManagementError(400, "等待 agent 必须是同项目的真实 agent ID")

    async def _rows(self, session, project_id: str | None, referenced: set[str]) -> dict[str, TaskModel]:
        # Include referenced foreign rows so errors distinguish missing from wrong ownership.
        result = await session.execute(select(TaskModel).options(load_only(
            TaskModel.id, TaskModel.project_id, TaskModel.team_id, TaskModel.parent_id,
            TaskModel.depends_on, TaskModel.depth, TaskModel.status, TaskModel.title,
        )).where(
            (TaskModel.project_id == project_id) | TaskModel.id.in_(referenced),
        ))
        return {row.id: row for row in result.scalars().all()}

    async def _record(self, session, task: Task, changes: list[str], old_status: str) -> None:
        snapshot = {"status": task.status.value, "assigned_to": task.assigned_to, "title": task.title}
        await self.repo.create_event(
            "task.updated", "repository", {"task_id": task.id, "changes": changes},
            entity_id=task.id, entity_type="task", state_snapshot=snapshot,
            _session=session,
        )
        if task.status == TaskStatus.FAILED and old_status != "failed":
            await self.repo.create_event(
                "task.failed", "repository",
                {"task_id": task.id, "from_status": old_status, "to_status": "failed",
                 "failure_context": task.result or "", "team_id": task.team_id, "project_id": task.project_id,
                 "assigned_to": task.assigned_to, "title": task.title, "detected_by": "state_machine"},
                entity_id=task.id, entity_type="task", state_snapshot=snapshot,
                _session=session,
            )

    async def create(self, project_id: str | None, team_id: str | None, values: dict[str, Any]) -> Task:
        result = await self.create_batch(project_id, team_id, [{"key": "task", **values}], None)
        return result["tasks"][0]

    async def create_batch(
        self, project_id: str | None, team_id: str | None, entries: list[dict[str, Any]], key: str | None,
    ) -> dict[str, Any]:
        if not entries or len(entries) > 100:
            raise TaskManagementError(400, "批量任务数量必须在 1 到 100 之间")
        if key and not project_id:
            raise TaskManagementError(400, "批量幂等需要明确项目归属")
        fingerprint = hashlib.sha256(json.dumps(
            {"team_id": team_id, "tasks": entries}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        keys = [entry["key"] for entry in entries]
        if len(set(keys)) != len(keys) or any(not k or k.startswith("@") for k in keys):
            raise TaskManagementError(400, "本批 key 必须非空、不重复且不以 @ 开头")
        key_map = {k: str(uuid4()) for k in keys}

        def reference(value: str | None) -> str | None:
            if value and value.startswith("@"):
                if value[1:] not in key_map:
                    raise TaskManagementError(400, f"批内引用 {value} 不存在")
                return key_map[value[1:]]
            return value

        async with get_session(self.repo._db_url) as session:
            # Defense in depth for concurrent idempotent creation: the receipt's
            # composite primary key already prevents duplicate committed batches.
            # This lock serializes lookup-and-create so competing retries replay
            # instead of exposing a uniqueness error. Removing it need not make
            # duplicate-batch tests fail when the PK or observed scheduling closes
            # that window. Separately, it protects the relationship/state snapshot
            # through insertion from concurrent dependency edits; keep both checks.
            await session.execute(text("BEGIN IMMEDIATE"))
            if key:
                known = await session.get(TaskCreateBatchModel, (project_id, key))
                if known:
                    if known.payload_hash != fingerprint:
                        raise TaskManagementError(409, "idempotency_key 已被不同批量参数使用")
                    result = await session.execute(select(TaskModel).where(TaskModel.id.in_(known.task_ids)))
                    found = {row.id: row.to_pydantic() for row in result.scalars().all()}
                    if any(task_id not in found for task_id in known.task_ids):
                        raise TaskManagementError(409, "已记录批次含被删除任务，不能冒充完整回放")
                    return {"tasks": [found[i] for i in known.task_ids], "key_map": known.key_map, "replayed": True}
            patches: list[dict[str, Any]] = []
            new: list[TaskModel] = []
            referenced: set[str] = set()
            for entry in entries:
                if not str(entry.get("title", "")).strip():
                    raise TaskManagementError(400, "title 不接受空字符串或空白标题")
                values = {k: v for k, v in entry.items() if k not in (
                    "key", "auto_start", "waiting_on", "status_note", "block_reason", "expected_revision",
                )}
                values["parent_id"] = reference(values.get("parent_id"))
                values["depends_on"] = [reference(dep) for dep in values.get("depends_on", [])]
                referenced.update(values["depends_on"])
                if values["parent_id"]:
                    referenced.add(values["parent_id"])
                try:
                    task = Task(id=key_map[entry["key"]], team_id=team_id, project_id=project_id, **values)
                except ValidationError as exc:
                    raise TaskManagementError(400, str(exc)) from exc
                new.append(TaskModel.from_pydantic(task))
                patches.append(entry)
            rows = await self._rows(session, project_id, referenced)
            rows.update({row.id: row for row in new})
            # Every dependency sees the same batch intention, irrespective of input
            # order. In particular auto_start overrides a requested completed row
            # before a dependent can mistake that request for final completion.
            for row, patch in zip(new, patches, strict=True):
                intended = "running" if patch.get("auto_start") else patch.get("status", row.status)
                try:
                    row.status = TaskStatus(intended).value
                except ValueError as exc:
                    raise TaskManagementError(400, f"无效的 status 值: {intended}") from exc
            _validate_relationships(rows, new, {row.id for row in new})
            for row, patch in zip(new, patches, strict=True):
                await self._waiting_target(session, project_id, patch)
                _apply_state(row, rows, patch, creating=True)
                session.add(row)
            if key:
                session.add(TaskCreateBatchModel(
                    project_id=project_id, idempotency_key=key, payload_hash=fingerprint,
                    task_ids=[row.id for row in new], key_map=key_map,
                ))
            tasks = [row.to_pydantic() for row in new]
        return {"tasks": tasks, "key_map": key_map, "replayed": False}

    async def update(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        for field in ("priority", "horizon", "title"):
            if field in patch and isinstance(patch[field], str) and not patch[field].strip():
                raise TaskManagementError(400, f"{field} 不接受空字符串")
        async with get_session(self.repo._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(TaskModel, task_id)
            if row is None:
                raise TaskManagementError(404, f"任务 {task_id} 不存在")
            expected = patch.get("expected_revision")
            revision = int(management(row.config).get("revision", 0))
            if expected is not None and expected != revision:
                raise TaskManagementError(409, {"message": "任务已被更新，请刷新后重试", "current_revision": revision,
                                                "current_task": row.to_pydantic().model_dump(mode="json")})
            previous_status = row.status
            previous_parent = row.parent_id
            previous_dependencies = list(row.depends_on or [])
            if not any(key != "expected_revision" for key in patch):
                return {"task": row.to_pydantic(), "unblocked": [], "warnings": [], "old_status": previous_status,
                        "blocked_by": []}
            referenced = set(patch.get("depends_on") or row.depends_on or [])
            if patch.get("parent_id"):
                referenced.add(patch["parent_id"])
            rows = await self._rows(session, row.project_id, referenced)
            for field in ("title", "description", "result", "assigned_to", "priority", "horizon", "tags",
                          "parent_id", "depends_on"):
                if field in patch:
                    setattr(row, field, patch[field])
            if "assigned_to" in patch and row.assigned_to == "":
                row.assigned_to = None
            rows[row.id] = row
            parent_changed = row.parent_id != previous_parent
            dependencies_changed = set(row.depends_on or []) != set(previous_dependencies)
            if parent_changed or dependencies_changed:
                _validate_relationships(rows, [row], {row.id} if parent_changed else set())
            await self._waiting_target(session, row.project_id, patch)
            warnings = _apply_state(row, rows, patch, creating=False, old_dependencies=previous_dependencies)
            try:
                row.to_pydantic()  # Enums and shared type validation, before commit.
            except (ValidationError, ValueError) as exc:
                raise TaskManagementError(400, str(exc)) from exc
            unblocked: list[Task] = []
            if row.status == "completed":
                downstream_ids = [t.id for t in rows.values()
                                  if row.id in (t.depends_on or []) and t.status == "blocked"]
                if downstream_ids:
                    result = await session.execute(select(TaskModel).where(TaskModel.id.in_(downstream_ids)))
                    rows.update({t.id: t for t in result.scalars().all()})
                for downstream in rows.values():
                    if row.id not in (downstream.depends_on or []) or downstream.status != "blocked":
                        continue
                    info = management(downstream.config)
                    if info.get("block_reason") != "dependency" or info.get("waiting_on"):
                        if info.get("block_reason") is None:
                            warnings.append(f"下游 {downstream.id} 阻塞原因未知，未自动解锁；请明确状态或等待对象")
                        continue
                    if all(dep in rows and rows[dep].status == "completed" for dep in downstream.depends_on):
                        downstream.status = "pending"
                        info.update(block_reason=None, revision=int(info.get("revision", 0)) + 1)
                        _set_management(downstream, info)
                        unblocked.append(downstream.to_pydantic())
            updated = row.to_pydantic()
            changes = [key for key in patch if key != "expected_revision"]
            if previous_status != updated.status.value and "status" not in changes:
                changes.append("status")
            await self._record(session, updated, changes, previous_status)
            for task in unblocked:
                await self._record(session, task, ["status", "block_reason"], "blocked")
        blocked_by = [dep for dep in row.depends_on or [] if dep not in rows or rows[dep].status != "completed"]
        return {"task": updated, "unblocked": unblocked, "warnings": warnings, "old_status": previous_status,
                "blocked_by": blocked_by}
