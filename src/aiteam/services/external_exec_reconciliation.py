"""Read-only discovery reconciliation over saved evidence, never a global census."""

from __future__ import annotations

import asyncio

from sqlalchemy import case, func, or_, select

from aiteam.clock import utc_now
from aiteam.services.usage_coverage import LEADER_ROLE
from aiteam.storage.connection import get_session
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.storage.models import (
    AgentModel,
    CodexUsageObservationModel,
    CodexUsageSourceCursorModel,
    ExternalExecInvocationModel,
)
from aiteam.types import ExternalExecReconciliation


def _agent_thread():
    # Codex child session_id points to the root; only cc_tool_use_id identifies
    # its own native thread. Unknown child identity must never borrow root usage.
    return case(
        (AgentModel.role == LEADER_ROLE, func.nullif(AgentModel.session_id, "")),
        else_=func.nullif(AgentModel.cc_tool_use_id, ""),
    )


async def _journal(db_url: str, namespace: str | None, since, until, limit: int):
    model = CodexUsageObservationModel
    filters = []
    if namespace is not None:
        filters.append(model.source_namespace == namespace)
    if since is not None:
        filters.append(func.julianday(model.payload["occurred_at"].as_string()) >= func.julianday(since.isoformat()))
    if until is not None:
        filters.append(func.julianday(model.payload["occurred_at"].as_string()) <= func.julianday(until.isoformat()))
    tokens_seen = or_(
        *[
            model.payload[container][field].as_integer().is_not(None)
            for container in ("usage", "total_token_usage", "last_token_usage")
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            )
        ]
    ).label("tokens_seen")
    thread = func.coalesce(
        func.nullif(model.payload["thread_id"].as_string(), ""),
        func.nullif(model.payload["session_id"].as_string(), ""),
    ).label("thread_id")
    columns = [
        model.source_namespace,
        thread,
        tokens_seen,
        model.payload["occurred_at"].as_string().label("occurred_at"),
    ]
    columns.extend(
        model.payload[key].as_string().label(key) for key in ("model", "provider", "service_tier", "response_id")
    )
    async with get_session(db_url) as session:
        total = int(await session.scalar(select(func.count()).select_from(model).where(*filters)))
        rows = await session.execute(
            select(*columns)
            .where(*filters)
            .order_by(
                model.saved_at.desc(),
                model.observation_id,
            )
            .limit(limit)
        )
        observations = [dict(row._mapping) for row in rows]
        cursor_model = CodexUsageSourceCursorModel
        cursor_filters = [] if namespace is None else [cursor_model.source_namespace == namespace]
        cursor_total = int(await session.scalar(select(func.count()).select_from(cursor_model).where(*cursor_filters)))
        cursor_rows = await session.execute(
            select(
                cursor_model.source_namespace,
                cursor_model.payload["context"]["session_id"].as_string().label("thread_id"),
                cursor_model.payload["status"].as_string().label("status"),
            )
            .where(*cursor_filters)
            .order_by(cursor_model.source_id)
            .limit(limit)
        )
        return observations, total, [dict(row._mapping) for row in cursor_rows], cursor_total


async def _agents(db_url: str, project_id: str | None, since, until, limit: int):
    filters = [AgentModel.harness == "codex", AgentModel.attribution_exclusion.is_(None)]
    if project_id is not None:
        filters.append(AgentModel.project_id == project_id)
    if since is not None:
        filters.append(AgentModel.created_at >= since)
    if until is not None:
        filters.append(AgentModel.created_at <= until)
    async with get_session(db_url) as session:
        total = int(await session.scalar(select(func.count()).select_from(AgentModel).where(*filters)))
        # Only IDs: do not load system prompts, configs or transcript paths.
        rows = await session.execute(
            select(AgentModel.id, _agent_thread().label("thread_id"))
            .where(*filters)
            .order_by(
                AgentModel.created_at.desc(),
                AgentModel.id,
            )
            .limit(limit)
        )
        return [{"agent_id": row.id, "thread_id": row.thread_id} for row in rows], total


def _has_tokens(usage) -> bool:
    return usage is not None and any(value is not None for value in usage.model_dump().values())


async def _existence(db_url: str, candidate_ids: set[str]):
    """Query global identity existence, independent of page/project/time filters.

    Distinct IDs prevent old usage facts outside the newest page from becoming a
    false missing-evidence bucket. Chunking also respects SQLite parameter limits.
    """
    registered = set()
    journal = set()
    external = set()
    identifiers = sorted(candidate_ids)
    async with get_session(db_url) as session:
        for start in range(0, len(identifiers), 300):
            chunk = identifiers[start : start + 300]
            rows = await session.scalars(
                select(_agent_thread())
                .where(
                    AgentModel.harness == "codex",
                    _agent_thread().in_(chunk),
                )
                .distinct()
            )
            registered.update(rows)
            observed_thread = func.coalesce(
                CodexUsageObservationModel.payload["thread_id"].as_string(),
                CodexUsageObservationModel.payload["session_id"].as_string(),
            )
            rows = await session.execute(
                select(
                    CodexUsageObservationModel.source_namespace,
                    observed_thread,
                )
                .where(observed_thread.in_(chunk))
                .distinct()
            )
            journal.update((row[0], row[1]) for row in rows)
            cursor_thread = CodexUsageSourceCursorModel.payload["context"]["session_id"].as_string()
            rows = await session.execute(
                select(
                    CodexUsageSourceCursorModel.source_namespace,
                    cursor_thread,
                )
                .where(cursor_thread.in_(chunk))
                .distinct()
            )
            journal.update((row[0], row[1]) for row in rows)
            rows = await session.execute(
                select(
                    ExternalExecInvocationModel.payload["source_namespace"].as_string(),
                    ExternalExecInvocationModel.thread_id,
                )
                .where(ExternalExecInvocationModel.thread_id.in_(chunk))
                .distinct()
            )
            external.update((row[0], row[1]) for row in rows)
    return registered, journal, external


async def reconcile_external_exec(
    repository: ExternalExecRepository,
    *,
    project_id: str | None = None,
    source_namespace: str | None = None,
    since=None,
    until=None,
    limit: int = 500,
    bucket_offset: int = 0,
    bucket_limit: int = 20,
    summary: bool = False,
) -> dict:
    """Bounded light SQL evidence, paginated buckets, never a global usage total."""
    if since is not None and until is not None and since > until:
        raise ValueError("invalid_time_range")
    limit = max(1, min(limit, 500))
    bucket_offset, bucket_limit = max(0, bucket_offset), max(0, min(bucket_limit, 50))
    if summary:
        bucket_offset, bucket_limit = 0, min(bucket_limit, 3)
    journal_result, agent_result, invocation_result = await asyncio.gather(
        _journal(repository._db_url, source_namespace, since, until, limit),
        _agents(repository._db_url, project_id, since, until, limit),
        repository.list_compact(
            project_id=project_id, source_namespace=source_namespace, since=since, until=until, limit=limit
        ),
    )
    observations, journal_total, cursors, cursor_total = journal_result
    agents, agent_total = agent_result
    invocations, invocation_total = invocation_result
    journal_threads = {(row["source_namespace"], row["thread_id"]) for row in observations if row["thread_id"]}
    if since is None and until is None:
        journal_threads |= {(row["source_namespace"], row["thread_id"]) for row in cursors if row["thread_id"]}
    invocation_threads = {(row.source_namespace, row.thread_id) for row in invocations if row.thread_id}
    visible_usage_threads = {
        (row.source_namespace, row.thread_id) for row in invocations if row.thread_id and row.usage_observed
    }
    candidates = {row["thread_id"] for row in agents if row["thread_id"]}
    candidates |= {thread for _, thread in journal_threads | invocation_threads}
    registered, global_journal, global_external = await _existence(repository._db_url, candidates)
    buckets = {
        key: []
        for key in (
            "unregistered_threads",
            "finished_without_usage",
            "tokens_without_complete_pricing",
            "external_without_journal",
            "registered_without_evidence",
            "running_invocations",
            "journal_project_unknown",
            "quarantined_invocations",
            "pricing_metadata_unverified",
        )
    }
    for namespace, thread_id in sorted(journal_threads):
        known = thread_id in registered or (namespace, thread_id) in global_external
        brief = {"thread_id": thread_id, "source_namespace": namespace}
        if project_id is not None:
            buckets["journal_project_unknown"].append({**brief, "registered_globally": known})
        if not known:
            buckets["unregistered_threads"].append(brief)
    pricing = {}
    for row in observations:
        if not row["tokens_seen"]:
            continue
        key = (row["source_namespace"], row["thread_id"])
        if key in visible_usage_threads:
            continue
        reasons = ["missing_" + field for field in ("model", "provider", "service_tier") if not row[field]]
        if not row["response_id"]:
            reasons.append("missing_request_identity")
        entry = pricing.setdefault(
            key, {"source": "journal", "thread_id": key[1], "source_namespace": key[0], "reasons": []}
        )
        entry["reasons"] = sorted(set(entry["reasons"] + reasons))
    for row in pricing.values():
        if row["reasons"]:
            buckets["tokens_without_complete_pricing"].append(row)
        else:
            buckets["pricing_metadata_unverified"].append({**row, "reasons": ["catalog_not_checked"]})
    for row in invocations:
        brief = {
            "invocation_id": row.invocation_id,
            "thread_id": row.thread_id,
            "source_namespace": row.source_namespace,
        }
        if row.status == "running":
            buckets["running_invocations"].append(brief)
        elif not row.usage_observed:
            buckets["finished_without_usage"].append({**brief, "status": row.status})
        if (row.source_namespace, row.thread_id) not in global_journal:
            buckets["external_without_journal"].append({**brief, "ephemeral": row.ephemeral})
        if row.usage_observed:
            buckets["tokens_without_complete_pricing"].append(
                {
                    **brief,
                    "source": "external_exec",
                    "reasons": ["native_aggregate_not_request_ledger", "account_not_verified"],
                }
            )
        if row.association_status in ("quarantined", "rejected"):
            buckets["quarantined_invocations"].append(
                {
                    **brief,
                    "status": row.status,
                    "association_reason": row.association_reason,
                    "requested_project_id": row.requested_project_id,
                    "requested_task_id": row.requested_task_id,
                }
            )
    evidence = {thread for _, thread in global_journal | global_external}
    for row in agents:
        if row["thread_id"] not in evidence:
            buckets["registered_without_evidence"].append(
                {**row, **({"reasons": ["missing_child_thread_id"]} if row["thread_id"] is None else {})}
            )
    counts = {key: len(items) for key, items in buckets.items()}
    pages = {key: items[bucket_offset : bucket_offset + bucket_limit] for key, items in buckets.items()}
    result = ExternalExecReconciliation(
        observed_at=utc_now(),
        scope={
            "project_id": project_id,
            "source_namespace": source_namespace,
            "since": since,
            "until": until,
            "time_basis": {"journal": "occurred_at", "agents": "created_at", "invocations": "started_at"},
        },
        counts={
            "journal_observations": journal_total,
            "journal_threads": len(journal_threads),
            "registered_codex_agents": agent_total,
            "external_invocations": invocation_total,
            "discovered_threads": len(journal_threads | invocation_threads),
            "overlap_threads": len(invocation_threads & global_journal),
        },
        buckets=pages,
        bucket_counts=counts,
        bucket_truncated={key: bucket_offset + len(pages[key]) < total for key, total in counts.items()},
        bucket_offset=bucket_offset,
        bucket_limit=bucket_limit,
        sample_limit=limit,
        summary=summary,
        truncated={
            "journal": journal_total > len(observations),
            "agents": agent_total > len(agents),
            "invocations": invocation_total > len(invocations),
            "cursors": cursor_total > len(cursors),
        },
        journal_status={
            "cursor_count": cursor_total,
            "incomplete_cursors": sum(row["status"] != "caught_up" for row in cursors),
            "unknown_observation_times_in_sample": any(row["occurred_at"] is None for row in observations),
        },
        statements=[
            "分母仅为已保存证据；无日志/回执的历史 ephemeral、其他 home/机器和未采集区间不可观察。",
            "项目筛选只作用于登记与外部关联，journal项目归属未知；不按cwd追认。",
            "Codex子agent用cc_tool_use_id识别自身线程，不借root session_id用量；缺子线程身份保持未知。",
            "源记录数是SQL总数；线程/桶计数限于本次有界样本，桶分页与源扫描截断分别声明，不代表全覆盖。",
            "未登记/缺证由全局身份存在性核对，不因项目、时间或分页样本缺席误报；agent home来源未知。",
            "原生exec回执按线程累计保留，resume不推增量，多回执/rollout不相加；全零未必证明真实零消耗。",
            "外部用量独立于原生套餐；元数据齐全但未查价单单列，不把catalog未检查说成缺元数据。",
            "时间未知观察不进入时间窗；游标只提供无时间过滤时的线程身份，不能证明窗口活动。",
        ],
    )
    return result.model_dump(mode="json")
