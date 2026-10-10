"""Real isolated API persistence; no account sampling or subprocess model runs."""

from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiteam.api.routes import external_exec as routes
from aiteam.clock import utc_now
from aiteam.services.external_exec import ExecEventCollector, upload_invocation
from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.storage.models import AgentModel, CodexUsageObservationModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    Agent,
    CodexUsageObservation,
    CodexUsageTokens,
    ExternalExecFinish,
    ExternalExecInvocation,
    ExternalExecReceipt,
    ExternalExecRegistration,
)


def register_body(project_id=None, task_id=None):
    return ExternalExecRegistration(
        invocation_id=str(uuid4()), source_namespace="a" * 64, project_id=project_id, task_id=task_id
    ).model_dump(mode="json")


@pytest.fixture
def client(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'api.sqlite'}"
    storage = StorageRepository(url)
    external = ExternalExecRepository(url)
    seed = {}

    @asynccontextmanager
    async def lifespan(app):
        await storage.init_db()
        first = await storage.create_project("first", root_path="/first")
        other = await storage.create_project("other", root_path="/other")
        task = await storage.create_task(None, "first task", project_id=first.id)
        seed.update(project_id=first.id, other_project_id=other.id, task_id=task.id)
        yield
        await engine_pool.get_engine(url).dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_repository] = lambda: storage
    app.dependency_overrides[routes.get_external_exec_repository] = lambda: external
    with TestClient(app) as connected:
        yield connected, seed, external


def test_same_project_binding_and_cross_project_rejection(client):
    connected, seed, _ = client
    body = register_body(seed["project_id"], seed["task_id"])
    assert connected.post("/api/external-execs", json=body).status_code == 200
    assert connected.post("/api/external-execs", json=body).json()["data"]["revision"] == 1
    wrong = register_body(seed["other_project_id"], seed["task_id"])
    assert connected.post("/api/external-execs", json=wrong).status_code == 409
    no_project = register_body()
    no_project["task_id"] = seed["task_id"]
    assert connected.post("/api/external-execs", json=no_project).status_code == 422
    listed = connected.get("/api/external-execs").json()["data"]
    assert listed["total"] == 1 and listed["items"][0]["task_id"] == seed["task_id"]
    assert "receipts" not in listed["items"][0] and listed["items"][0]["receipts_count"] == 0


def test_direct_registration_replay_after_project_deletion_bypasses_new_association_validation(client):
    from sqlalchemy import delete

    from aiteam.storage.models import ProjectModel

    connected, seed, repository = client
    body = register_body(seed["project_id"])
    assert connected.post("/api/external-execs", json=body).status_code == 200

    async def remove():
        async with get_session(repository._db_url) as session:
            await session.execute(delete(ProjectModel).where(ProjectModel.id == seed["project_id"]))
            await session.commit()

    connected.portal.call(remove)
    response = connected.post("/api/external-execs", json=body)
    assert response.status_code == 200
    assert response.json()["data"]["association_status"] == "accepted"
    assert response.json()["data"]["project_id"] == seed["project_id"]


@pytest.mark.parametrize("level,expected", [(None, "unknown"), ("turn_aggregate", "turn_aggregate")])
def test_http_receipt_default_unknown_and_explicit_old_turn_survive_requests(client, level, expected):
    connected, _, _ = client
    body = register_body()
    identifier = body["invocation_id"]
    assert connected.post("/api/external-execs", json=body).status_code == 200
    receipt = {"receipt_id": "turn:1", "thread_id": "receipt-thread", "usage": {"input_tokens": 5}}
    if level is not None:
        receipt["counting_level"] = level
    assert connected.post(f"/api/external-execs/{identifier}/receipt", json=receipt).status_code == 200
    stored = connected.get(f"/api/external-execs/{identifier}").json()["data"]
    assert stored["receipts"]["turn:1"]["counting_level"] == expected


def test_cross_request_usage_seal_and_conflicting_retry(client):
    connected, seed, _ = client
    body = register_body(seed["project_id"])
    identifier = body["invocation_id"]
    assert connected.post("/api/external-execs", json=body).status_code == 200
    finish = {"status": "interrupted", "exit_code": -15, "thread_id": "thread", "finished_at": utc_now().isoformat()}
    receipt = {"receipt_id": "turn:1", "thread_id": "thread", "usage": {"input_tokens": 20, "output_tokens": 3}}
    assert connected.post(f"/api/external-execs/{identifier}/receipt", json=receipt).status_code == 200
    finish["receipt_ids"] = ["turn:1"]
    assert connected.post(f"/api/external-execs/{identifier}/finish", json=finish).status_code == 200
    actual = connected.get(f"/api/external-execs/{identifier}").json()["data"]
    revision = actual["revision"]
    assert (
        connected.post(f"/api/external-execs/{identifier}/receipt", json=receipt).json()["data"]["revision"] == revision
    )
    assert actual["status"] == "interrupted" and actual["receipts"]["turn:1"]["usage"]["cached_input_tokens"] is None
    receipt["usage"]["input_tokens"] = 21
    assert connected.post(f"/api/external-execs/{identifier}/receipt", json=receipt).status_code == 409
    assert connected.post(f"/api/external-execs/{identifier}/thread", json={"thread_id": "another"}).status_code == 409
    assert (
        connected.post(
            f"/api/external-execs/{identifier}/receipt", json={"receipt_id": "bad", "prompt": "PRIVATE"}
        ).status_code
        == 422
    )


def test_list_pagination_and_missing_usage_bucket(client):
    connected, seed, _ = client
    bodies = [register_body(seed["project_id"]) for _ in range(3)]
    for body in bodies:
        connected.post("/api/external-execs", json=body)
        connected.post(f"/api/external-execs/{body['invocation_id']}/finish", json={"status": "failed", "exit_code": 1})
    page = connected.get("/api/external-execs?limit=1").json()["data"]
    assert page["total"] == 3 and page["truncated"] is True
    reconciliation = connected.get("/api/external-execs/reconciliation").json()["data"]
    assert reconciliation["counts"]["external_invocations"] == 3
    assert len(reconciliation["buckets"]["finished_without_usage"]) == 3
    assert "不可观察" in "".join(reconciliation["statements"])
    assert connected.get("/api/external-execs/reconciliation?since=2026-01-01").status_code == 422


def test_cross_project_and_truncation_do_not_invent_missing_registration_or_evidence(client):
    connected, seed, external = client

    async def populate():
        now = utc_now()
        async with get_session(external._db_url) as session:
            for thread_id, project_id, age in [
                ("other-project-thread", seed["other_project_id"], 3),
                ("first-old-usage", seed["project_id"], 1),
            ]:
                agent = Agent(
                    team_id="team",
                    name=thread_id,
                    role="worker",
                    session_id="root-thread",
                    cc_tool_use_id=thread_id,
                    harness="codex",
                    project_id=project_id,
                    created_at=now - timedelta(days=age),
                )
                session.add(AgentModel.from_pydantic(agent))
            for index, thread_id, age in [
                (1, "first-old-usage", 9),
                (2, "other-project-thread", 0),
                (3, "new-unknown-thread", -1),
            ]:
                observation = CodexUsageObservation(
                    observation_id=f"{index:064x}",
                    source_id="b" * 64,
                    source_namespace="a" * 64,
                    generation=0,
                    byte_start=0,
                    byte_end=1,
                    line_sha256="c" * 64,
                    kind="legacy",
                    session_id=thread_id,
                    occurred_at=now - timedelta(days=age),
                    saved_at=now - timedelta(days=age),
                    usage=CodexUsageTokens(input_tokens=10),
                )
                session.add(
                    CodexUsageObservationModel(
                        observation_id=observation.observation_id,
                        source_id=observation.source_id,
                        source_namespace=observation.source_namespace,
                        saved_at=observation.saved_at,
                        payload=observation.model_dump(mode="json"),
                    )
                )

    connected.portal.call(populate)
    first = connected.get(f"/api/external-execs/reconciliation?project_id={seed['project_id']}&limit=2").json()["data"]
    assert first["truncated"]["journal"] is True
    assert {item["thread_id"] for item in first["buckets"]["unregistered_threads"]} == {"new-unknown-thread"}
    assert first["buckets"]["registered_without_evidence"] == []
    assert any(
        item["thread_id"] == "other-project-thread" and item["registered_globally"]
        for item in first["buckets"]["journal_project_unknown"]
    )
    all_projects = connected.get("/api/external-execs/reconciliation?limit=1").json()["data"]
    assert all_projects["truncated"]["agents"] is True
    assert all_projects["buckets"]["registered_without_evidence"] == []


def test_overlapping_receipt_and_old_rollout_do_not_become_missing_journal(client):
    connected, seed, external = client
    body = register_body(seed["project_id"])
    identifier = body["invocation_id"]
    connected.post("/api/external-execs", json=body)
    connected.post(
        f"/api/external-execs/{identifier}/receipt",
        json={
            "receipt_id": "turn:1",
            "thread_id": "overlap",
            "usage": {"input_tokens": 10},
        },
    )

    async def populate():
        async with get_session(external._db_url) as session:
            for index, thread in [(1, "overlap"), (2, "newest")]:
                when = utc_now() - timedelta(days=10 if index == 1 else 0)
                observation = CodexUsageObservation(
                    observation_id=f"{index:064x}",
                    source_id="b" * 64,
                    source_namespace="a" * 64,
                    generation=0,
                    byte_start=0,
                    byte_end=1,
                    line_sha256="c" * 64,
                    kind="legacy",
                    session_id=thread,
                    occurred_at=when,
                    saved_at=when,
                    usage=CodexUsageTokens(input_tokens=10),
                )
                session.add(
                    CodexUsageObservationModel(
                        observation_id=observation.observation_id,
                        source_id=observation.source_id,
                        source_namespace=observation.source_namespace,
                        saved_at=observation.saved_at,
                        payload=observation.model_dump(mode="json"),
                    )
                )

    connected.portal.call(populate)
    data = connected.get("/api/external-execs/reconciliation?limit=1").json()["data"]
    assert data["truncated"]["journal"] is True and data["counts"]["overlap_threads"] == 1
    assert data["buckets"]["external_without_journal"] == []
    assert (
        len([entry for entry in data["buckets"]["tokens_without_complete_pricing"] if entry["thread_id"] == "overlap"])
        == 1
    )


@pytest.mark.parametrize(
    "case", ["empty-receipt", "other-project", "old-invocation", "paged-out", "other-namespace", "visible-usage"]
)
def test_only_visible_measured_same_namespace_receipt_replaces_journal_pricing_gap(client, case):
    connected, seed, external = client
    now = utc_now()
    project = seed["other_project_id"] if case == "other-project" else seed["project_id"]
    body = register_body(project)
    if case == "old-invocation":
        body["started_at"] = (now - timedelta(days=10)).isoformat()
    if case == "other-namespace":
        body["source_namespace"] = "e" * 64
    assert connected.post("/api/external-execs", json=body).status_code == 200
    identifier = body["invocation_id"]
    assert (
        connected.post(f"/api/external-execs/{identifier}/thread", json={"thread_id": "observed-thread"}).status_code
        == 200
    )
    if case != "empty-receipt":
        assert (
            connected.post(
                f"/api/external-execs/{identifier}/receipt",
                json={
                    "receipt_id": "turn:1",
                    "thread_id": "observed-thread",
                    "usage": {"input_tokens": 100},
                },
            ).status_code
            == 200
        )
    if case == "paged-out":
        assert connected.post("/api/external-execs", json=register_body(seed["project_id"])).status_code == 200

    async def populate():
        observation = CodexUsageObservation(
            observation_id="b" * 64,
            source_id="c" * 64,
            source_namespace="a" * 64,
            generation=0,
            byte_start=0,
            byte_end=1,
            line_sha256="d" * 64,
            kind="legacy",
            session_id="observed-thread",
            occurred_at=now,
            usage=CodexUsageTokens(input_tokens=100),
        )
        async with get_session(external._db_url) as session:
            session.add(
                CodexUsageObservationModel(
                    observation_id=observation.observation_id,
                    source_id=observation.source_id,
                    source_namespace=observation.source_namespace,
                    saved_at=observation.saved_at,
                    payload=observation.model_dump(mode="json"),
                )
            )

    connected.portal.call(populate)
    parameters = {}
    if case == "other-project":
        parameters["project_id"] = seed["project_id"]
    elif case == "old-invocation":
        parameters["since"] = (now - timedelta(days=1)).isoformat()
    elif case == "paged-out":
        parameters["limit"] = 1
    data = connected.get("/api/external-execs/reconciliation", params=parameters).json()["data"]
    assert data["counts"]["journal_observations"] == 1
    gaps = [
        item for item in data["buckets"]["tokens_without_complete_pricing"] if item["thread_id"] == "observed-thread"
    ]
    journal_gaps = [item for item in gaps if item["source"] == "journal"]
    if case == "visible-usage":
        assert journal_gaps == [] and len(gaps) == 1 and gaps[0]["source"] == "external_exec"
    else:
        assert len(journal_gaps) == 1
        assert {"missing_model", "missing_provider", "missing_service_tier"} <= set(journal_gaps[0]["reasons"])
    if case in ("other-project", "old-invocation"):
        assert data["counts"]["external_invocations"] == 0
    if case == "paged-out":
        assert data["truncated"]["invocations"] is True


@pytest.mark.parametrize("empty_field", ["usage", "total_token_usage"])
def test_empty_native_counter_container_does_not_hide_later_measured_usage(client, empty_field):
    connected, _, external = client

    async def populate():
        observation = CodexUsageObservation(
            observation_id="b" * 64,
            source_id="c" * 64,
            source_namespace="a" * 64,
            generation=0,
            byte_start=0,
            byte_end=1,
            line_sha256="d" * 64,
            kind="legacy",
            session_id="observed-thread",
            last_token_usage=CodexUsageTokens(input_tokens=100),
            **{empty_field: CodexUsageTokens()},
        )
        async with get_session(external._db_url) as session:
            session.add(
                CodexUsageObservationModel(
                    observation_id=observation.observation_id,
                    source_id=observation.source_id,
                    source_namespace=observation.source_namespace,
                    saved_at=observation.saved_at,
                    payload=observation.model_dump(mode="json"),
                )
            )

    connected.portal.call(populate)
    data = connected.get("/api/external-execs/reconciliation").json()["data"]
    assert data["counts"]["journal_observations"] == 1 and data["counts"]["external_invocations"] == 0
    assert data["buckets"]["tokens_without_complete_pricing"][0]["thread_id"] == "observed-thread"


@pytest.mark.parametrize("explicit_zero", [False, True], ids=["cache-write-missing", "cache-write-measured-zero"])
def test_native_usage_missing_and_measured_zero_survive_spool_http_and_target_db(client, tmp_path, explicit_zero):
    connected, seed, _ = client
    body = register_body(seed["project_id"], seed["task_id"])
    registration = ExternalExecRegistration.model_validate(body)

    async def roundtrip():
        import json

        local = ExternalExecRepository(f"sqlite+aiosqlite:///{tmp_path / 'native-replay-spool.sqlite'}")
        try:
            await local.init_db()
            await local.register(registration)
            collector = ExecEventCollector(local, registration.invocation_id)
            # Exact four-field shape captured by the native smoke metadata.
            usage = {
                "input_tokens": 22143,
                "cached_input_tokens": 7168,
                "output_tokens": 5,
                "reasoning_output_tokens": 0,
            }
            if explicit_zero:
                usage["cache_write_input_tokens"] = 0
            for event in [
                {"type": "thread.started", "thread_id": "native-replay-thread"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": usage},
            ]:
                await collector.feed((json.dumps(event) + "\n").encode())
            await local.finish(registration.invocation_id, ExternalExecFinish(status="completed", exit_code=0))
            observed = await ExternalExecRepository(local._db_url).get(registration.invocation_id)
            assert observed.receipts["turn:1"].usage.cache_write_input_tokens == (0 if explicit_zero else None)
            async with httpx.AsyncClient(
                base_url="http://test", transport=httpx.ASGITransport(app=connected.app)
            ) as http:
                assert await upload_invocation(local, registration.invocation_id, http)
                assert await upload_invocation(local, registration.invocation_id, http)
            assert (await local.list(pending_only=True))[1] == 0
        finally:
            await engine_pool.get_engine(local._db_url).dispose()

    connected.portal.call(roundtrip)
    target = connected.get(f"/api/external-execs/{registration.invocation_id}").json()["data"]
    assert target["status"] == "completed" and target["receipts_count"] == 1
    assert target["receipts"]["turn:1"]["usage"]["cache_write_input_tokens"] == (0 if explicit_zero else None)


def test_child_codex_identity_uses_child_thread_and_never_borrows_root_evidence(client):
    connected, seed, external = client

    async def populate():
        async with get_session(external._db_url) as session:
            for name, role, child in [
                ("root", "leader", None),
                ("observed-child", "worker", "observed-child"),
                ("missing-child", "worker", "missing-child"),
                ("unknown-child", "worker", None),
            ]:
                agent = Agent(
                    team_id="team",
                    name=name,
                    role=role,
                    harness="codex",
                    project_id=seed["project_id"],
                    session_id="root-thread",
                    cc_tool_use_id=child,
                )
                session.add(AgentModel.from_pydantic(agent))
            for index, thread in enumerate(("root-thread", "observed-child"), 1):
                observation = CodexUsageObservation(
                    observation_id=f"{index:064x}",
                    source_id="c" * 64,
                    source_namespace="a" * 64,
                    generation=0,
                    byte_start=0,
                    byte_end=1,
                    line_sha256="d" * 64,
                    kind="legacy",
                    session_id=thread,
                    usage=CodexUsageTokens(input_tokens=100),
                )
                session.add(
                    CodexUsageObservationModel(
                        observation_id=observation.observation_id,
                        source_id=observation.source_id,
                        source_namespace=observation.source_namespace,
                        saved_at=observation.saved_at,
                        payload=observation.model_dump(mode="json"),
                    )
                )

    connected.portal.call(populate)
    data = connected.get("/api/external-execs/reconciliation").json()["data"]
    assert data["buckets"]["unregistered_threads"] == []
    missing = data["buckets"]["registered_without_evidence"]
    assert len(missing) == 2
    assert any(row.get("thread_id") == "missing-child" for row in missing)
    assert any("missing_child_thread_id" in row["reasons"] for row in missing)


def test_receipt_manifest_keeps_out_of_order_finish_open_and_terminal_is_sealed(client):
    connected, _, _ = client
    body = register_body()
    identifier = body["invocation_id"]
    connected.post("/api/external-execs", json=body)
    finish = {"status": "completed", "exit_code": 0, "receipt_ids": ["one", "two"]}
    assert connected.post(f"/api/external-execs/{identifier}/finish", json=finish).status_code == 409
    assert connected.get(f"/api/external-execs/{identifier}").json()["data"]["status"] == "running"
    receipts = [{"receipt_id": key, "usage": {"input_tokens": 1}} for key in ("one", "two")]
    for receipt in reversed(receipts):
        assert connected.post(f"/api/external-execs/{identifier}/receipt", json=receipt).status_code == 200
    assert connected.post(f"/api/external-execs/{identifier}/finish", json=finish).status_code == 200
    for receipt in receipts:
        assert connected.post(f"/api/external-execs/{identifier}/receipt", json=receipt).status_code == 200
    assert connected.post(f"/api/external-execs/{identifier}/receipt", json={"receipt_id": "new"}).status_code == 409
    assert (
        connected.post(
            f"/api/external-execs/{identifier}/receipt",
            json={
                "receipt_id": "one",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ).status_code
        == 409
    )


def test_compact_lists_and_detail_pages_do_not_validate_full_receipt_map(client, monkeypatch):
    connected, _, external = client
    body = register_body()
    connected.post("/api/external-execs", json=body)

    async def populate():
        for index in range(120):
            await external.receipt(
                body["invocation_id"],
                ExternalExecReceipt(
                    receipt_id=f"turn:{index}",
                    usage=CodexUsageTokens(input_tokens=1),
                ),
            )

    connected.portal.call(populate)

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP reads must not parse a full invocation receipt map")

    monkeypatch.setattr(ExternalExecInvocation, "model_validate", forbidden)
    first = connected.get("/api/external-execs").json()["data"]["items"][0]
    assert "receipts" not in first and first["receipts_count"] == 120
    assert connected.get("/api/external-execs/reconciliation").status_code == 200
    page = connected.get(f"/api/external-execs/{body['invocation_id']}").json()["data"]
    assert len(page["receipts"]) == 100 and page["receipts_truncated"]
    last = connected.get(f"/api/external-execs/{body['invocation_id']}?receipt_offset=100&receipt_limit=100").json()[
        "data"
    ]
    assert len(last["receipts"]) == 20 and not last["receipts_truncated"]


def test_quarantine_validates_actual_association_and_never_changes_registered_identity(client):
    connected, seed, _ = client
    valid = register_body(seed["project_id"], seed["task_id"])
    claimed = {"registration": valid, "reason_code": "project_not_found"}
    assert connected.post("/api/external-execs/quarantine", json=claimed).status_code == 409
    assert connected.get("/api/external-execs").json()["data"]["total"] == 0
    accepted = connected.post("/api/external-execs", json=valid).json()["data"]
    repeat = connected.post("/api/external-execs/quarantine", json=claimed).json()["data"]
    assert repeat["association_status"] == "accepted" and repeat["project_id"] == valid["project_id"]
    invalid = dict(valid, project_id=str(uuid4()))
    assert (
        connected.post(
            "/api/external-execs/quarantine",
            json={
                "registration": invalid,
                "reason_code": "project_not_found",
            },
        ).status_code
        == 409
    )
    after = connected.get(f"/api/external-execs/{valid['invocation_id']}").json()["data"]
    assert after["project_id"] == valid["project_id"] and after["revision"] == accepted["revision"]
