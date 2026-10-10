"""Bounded MCP/HTTP contracts on production-shaped, isolated stored evidence."""

import json

from sqlalchemy import insert

from aiteam.mcp.tools import external_exec as mcp_module
from aiteam.storage.connection import get_session
from aiteam.storage.models import CodexUsageObservationModel
from aiteam.types import CodexUsageObservation, CodexUsageTokens, ExternalExecReconciliation
from unit.api.test_external_exec_api import client as client


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


def test_large_source_counts_mcp_summary_and_http_bucket_pages_are_independently_bounded(client, monkeypatch):
    connected, seed, repository = client

    async def populate():
        prototype = CodexUsageObservation(
            observation_id="b" * 64,
            source_id="c" * 64,
            source_namespace="a" * 64,
            generation=0,
            byte_start=0,
            byte_end=1,
            line_sha256="d" * 64,
            kind="legacy",
            usage=CodexUsageTokens(input_tokens=100),
        )
        records = []
        for index in range(1200):
            payload = prototype.model_dump(mode="json")
            payload.update(observation_id=f"{index:064x}", session_id=f"thread-{index}")
            records.append(
                dict(
                    observation_id=payload["observation_id"],
                    source_id=payload["source_id"],
                    source_namespace=payload["source_namespace"],
                    saved_at=prototype.saved_at,
                    payload=payload,
                )
            )
        async with get_session(repository._db_url) as session:
            await session.execute(insert(CodexUsageObservationModel), records)

    connected.portal.call(populate)
    requested = []

    def actual_api(method, path):
        assert method == "GET"
        requested.append(path)
        return connected.get(path).json()

    monkeypatch.setattr(mcp_module, "_api_call", actual_api)
    registry = Registry()
    mcp_module.register(registry)
    result = registry.tools["usage_reconciliation"](project_id=seed["project_id"], limit=10000)
    assert result["success"] is True and len(json.dumps(result, ensure_ascii=False).encode()) <= 16384
    assert "limit=500" in requested[0] and "summary=true" in requested[0]
    data = result["data"]
    assert data["summary"] and data["counts"]["journal_observations"] == 1200 and data["truncated"]["journal"]
    assert data["bucket_counts"]["unregistered_threads"] == 500
    assert all(len(items) <= 3 for items in data["buckets"].values())
    first = connected.get("/api/external-execs/reconciliation?bucket_limit=2").json()["data"]
    second = connected.get("/api/external-execs/reconciliation?bucket_limit=2&bucket_offset=2").json()["data"]
    assert first["bucket_counts"]["unregistered_threads"] == 500
    assert first["bucket_truncated"]["unregistered_threads"] and len(first["buckets"]["unregistered_threads"]) == 2
    assert first["buckets"]["unregistered_threads"] != second["buckets"]["unregistered_threads"]
    assert connected.get("/api/external-execs/reconciliation?limit=501").status_code == 422


def test_mcp_byte_budget_drops_samples_and_truthfully_marks_truncation(client, monkeypatch):
    connected, _, _ = client
    data = connected.get("/api/external-execs/reconciliation").json()["data"]
    for bucket in data["buckets"]:
        data["buckets"][bucket] = [
            {
                "thread_id": "t" * 256,
                "source_namespace": "a" * 64,
                "reasons": ["missing_" + "m" * 200 for _ in range(16)],
            }
            for _ in range(3)
        ]
        data["bucket_counts"][bucket] = 3
        data["bucket_truncated"][bucket] = False
    ExternalExecReconciliation.model_validate(data)
    monkeypatch.setattr(mcp_module, "_api_call", lambda *args: {"success": True, "data": data})
    registry = Registry()
    mcp_module.register(registry)
    result = registry.tools["usage_reconciliation"]()
    assert result["success"] and len(json.dumps(result, ensure_ascii=False).encode()) <= 16384
    assert result["data"]["buckets"] == {} and all(result["data"]["bucket_truncated"].values())


def test_complete_pricing_metadata_is_not_misreported_as_missing_fields(client):
    connected, _, repository = client

    async def populate():
        observation = CodexUsageObservation(
            observation_id="b" * 64,
            source_id="c" * 64,
            source_namespace="a" * 64,
            generation=0,
            byte_start=0,
            byte_end=1,
            line_sha256="d" * 64,
            kind="ledger",
            session_id="known-metadata",
            model="observed-model",
            provider="observed-provider",
            service_tier="observed-tier",
            response_id="request-id",
            usage=CodexUsageTokens(input_tokens=100),
        )
        async with get_session(repository._db_url) as session:
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
    assert data["buckets"]["tokens_without_complete_pricing"] == []
    assert data["buckets"]["pricing_metadata_unverified"][0]["reasons"] == ["catalog_not_checked"]


def test_mcp_small_four_item_page_is_trimmed_with_truthful_truncation(client, monkeypatch):
    connected, _, _ = client
    data = connected.get("/api/external-execs/reconciliation").json()["data"]
    data["buckets"]["unregistered_threads"] = [{"thread_id": str(index)} for index in range(4)]
    data["bucket_counts"]["unregistered_threads"] = 4
    data["bucket_truncated"]["unregistered_threads"] = False
    monkeypatch.setattr(mcp_module, "_api_call", lambda *args: {"success": True, "data": data})
    registry = Registry()
    mcp_module.register(registry)
    result = registry.tools["usage_reconciliation"]()
    assert len(json.dumps(result).encode()) < 16384
    assert len(result["data"]["buckets"]["unregistered_threads"]) == 3
    assert result["data"]["bucket_truncated"]["unregistered_threads"]
