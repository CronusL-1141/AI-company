"""Durable minimal CC launch observations; no shell text is stored."""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select, text

from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import Base, CodexExecLaunchModel
from aiteam.types import CodexExecLaunchObservation


class CodexExecLaunchRepository:
    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

    async def init_db(self) -> None:
        async with engine_pool.get_engine(self._db_url).begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=[CodexExecLaunchModel.__table__])

    async def get(self, launch_id: str) -> CodexExecLaunchObservation | None:
        async with get_session(self._db_url) as session:
            row = await session.get(CodexExecLaunchModel, launch_id)
            return None if row is None else CodexExecLaunchObservation.model_validate(row.payload)

    async def record(self, incoming: CodexExecLaunchObservation, phase: str) -> CodexExecLaunchObservation:
        incoming = CodexExecLaunchObservation.model_validate(incoming.model_dump())
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(CodexExecLaunchModel, incoming.launch_id)
            if row is None:
                value = incoming
                row = CodexExecLaunchModel(launch_id=value.launch_id)
                session.add(row)
            else:
                previous = CodexExecLaunchObservation.model_validate(row.payload)
                identity = (
                    "caller_session_id", "tool_use_id", "command_index", "exec_mode",
                    "requested_service_tier", "requested_model", "requested_reasoning_effort",
                    "output_path", "codex_home", "background",
                )
                if any(getattr(previous, key) != getattr(incoming, key) for key in identity):
                    value = previous.model_copy(update={
                        "state": "unbound", "binding_reason": "conflicting_thread", "thread_id": None,
                    })
                elif previous.binding_reason == "conflicting_thread":
                    value = previous
                elif phase == "pre":
                    value = previous.model_copy(update={
                        "pre_seen": True, "started_at": incoming.started_at,
                        "project_id": previous.project_id or incoming.project_id,
                    })
                    if (previous.binding_reason == "missing_pre_event" and previous.thread_id
                            and previous.source_namespace and incoming.binding_reason == "awaiting_completion"):
                        value = value.model_copy(update={"state": "bound", "binding_reason": "bound"})
                elif previous.state == "failed" and phase != "reconcile":
                    value = previous  # A late replay cannot resurrect a failed tool.
                elif previous.thread_id and incoming.thread_id and previous.thread_id != incoming.thread_id:
                    value = previous.model_copy(update={
                        "state": "unbound", "binding_reason": "conflicting_thread", "thread_id": None,
                    })
                else:
                    value = incoming.model_copy(update={
                        "pre_seen": previous.pre_seen,
                        "started_at": previous.started_at,
                        "project_id": previous.project_id or incoming.project_id,
                    })
                    if value.state == "bound" and not previous.pre_seen:
                        value = value.model_copy(update={"state": "unbound", "binding_reason": "missing_pre_event"})
            if row.payload is None and phase in {"post", "fail"} and value.state == "bound":
                value = value.model_copy(update={"state": "unbound", "binding_reason": "missing_pre_event"})
            value = CodexExecLaunchObservation.model_validate(value.model_dump())
            row.project_id, row.thread_id, row.started_at = value.project_id, value.thread_id, value.started_at
            row.state = value.state
            row.output_path_key = hashlib.sha256(value.output_path.encode()).hexdigest() if value.output_path else None
            row.output_file_key = value.output_file_key
            row.payload = value.model_dump(mode="json")
            await session.flush()
            # All launches participate, including requests whose tier was unknown.
            if row.output_file_key:
                peers = list(await session.scalars(select(CodexExecLaunchModel).where(
                    CodexExecLaunchModel.output_file_key == row.output_file_key,
                    CodexExecLaunchModel.launch_id != row.launch_id,
                )))
                if peers:
                    for peer in [row, *peers]:
                        conflict = CodexExecLaunchObservation.model_validate(peer.payload).model_copy(update={
                            "state": "unbound", "binding_reason": "output_path_conflict", "thread_id": None,
                        })
                        peer.state, peer.thread_id, peer.payload = "unbound", None, conflict.model_dump(mode="json")
                        if peer is row:
                            value = conflict
            return value

    async def list(self, *, project_id: str | None = None, limit: int = 20,
                   offset: int = 0) -> tuple[list[CodexExecLaunchObservation], int]:
        model = CodexExecLaunchModel
        predicates = [model.project_id == project_id] if project_id is not None else []
        async with get_session(self._db_url) as session:
            total = int(await session.scalar(select(func.count()).select_from(model).where(*predicates)))
            rows = await session.scalars(select(model).where(*predicates)
                                        .order_by(model.started_at.desc(), model.launch_id)
                                        .limit(max(1, min(limit, 100))).offset(max(0, offset)))
            return [CodexExecLaunchObservation.model_validate(row.payload) for row in rows], total
