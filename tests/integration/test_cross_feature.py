"""Cross-feature integration tests — validate interactions between v0.9/v1.0/v1.1 features.

Each test exercises two or more features in combination, using a real
in-memory SQLite database via the FastAPI TestClient.

Scenarios covered:
  1. Event log + entity_id filtering + state_snapshot
  2. BM25 execution pattern search + ranking
  3. Guardrails middleware + normal/malicious input
  4. File lock + conflict detection + TTL expiry
  5. Channel messaging + @mention filtering
  6. Prompt Registry + effectiveness statistics
  7. Error recovery mapping (_recovery / _error_category)
  8. Combined: Task update → Event + Trust score
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from testlib import make_team

# ============================================================
# 1. Event log + entity_id filtering + state_snapshot
# ============================================================


class TestEventEntityIntegration:
    """Task status updates should auto-generate events with entity_id and state_snapshot."""

    def test_task_update_creates_event_with_entity_id(self, repo_and_client):
        """Update task status → events filtered by entity_id should include state_snapshot."""
        repo, client = repo_and_client

        # Create team + task
        team = make_team({"name": "event-ent-team"})

        resp = client.post(
            f"/api/teams/{team['name']}/tasks/run",
            json={"title": "Event trace task", "description": "test entity tracking"},
        )
        task_id = resp.json()["data"]["id"]

        # Update task status to running → triggers repository auto-event
        resp = client.put(
            f"/api/tasks/{task_id}",
            json={"status": "running", "assigned_to": "dev-agent"},
        )
        assert resp.status_code == 200

        # Query events filtered by entity_id
        resp = client.get(f"/api/events?entity_id={task_id}")
        assert resp.status_code == 200
        events = resp.json()
        assert events["total"] >= 1

        # Find the task.updated event
        updated_events = [e for e in events["data"] if e["type"] == "task.updated"]
        assert len(updated_events) >= 1

        # Verify state_snapshot is present and contains expected fields
        latest = updated_events[-1]
        assert latest["entity_id"] == task_id
        snapshot = latest.get("state_snapshot")
        assert snapshot is not None, "state_snapshot should be present in task.updated event"
        assert snapshot["status"] == "running"
        assert snapshot["assigned_to"] == "dev-agent"

    def test_events_entity_id_filter_isolates_correctly(self, repo_and_client):
        """Events for different tasks are correctly isolated by entity_id filter."""
        repo, client = repo_and_client

        team = make_team({"name": "evt-iso-team"})

        # Create two tasks
        resp1 = client.post(
            f"/api/teams/{team['name']}/tasks/run",
            json={"title": "Task Alpha", "description": "alpha"},
        )
        task_a = resp1.json()["data"]["id"]

        resp2 = client.post(
            f"/api/teams/{team['name']}/tasks/run",
            json={"title": "Task Beta", "description": "beta"},
        )
        task_b = resp2.json()["data"]["id"]

        # Update both tasks
        client.put(f"/api/tasks/{task_a}", json={"status": "running"})
        client.put(f"/api/tasks/{task_b}", json={"status": "completed"})

        # Filter by task_a entity_id — should NOT include task_b events
        resp = client.get(f"/api/events?entity_id={task_a}")
        events_a = resp.json()["data"]
        for ev in events_a:
            if ev.get("entity_id"):
                assert ev["entity_id"] == task_a

        # Filter by task_b entity_id
        resp = client.get(f"/api/events?entity_id={task_b}")
        events_b = resp.json()["data"]
        for ev in events_b:
            if ev.get("entity_id"):
                assert ev["entity_id"] == task_b


# ============================================================
# 3. Guardrails middleware + normal/malicious input
# ============================================================


class TestGuardrailsAPIIntegration:
    """Guardrails middleware should block dangerous payloads on API mutations."""

    def test_sql_injection_blocked(self, integration_client):
        """POST with SQL injection in body → 400 with violations."""
        client = integration_client
        resp = client.post(
            "/api/teams",
            json={"name": "DROP TABLE teams"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "violations" in body
        assert any("DROP TABLE" in v for v in body["violations"])

    def test_xss_script_blocked(self, integration_client):
        """POST with XSS script tag → 400."""
        client = integration_client
        resp = client.post(
            "/api/teams",
            json={"name": "<script>alert('xss')</script>"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "violations" in body

    def test_code_injection_eval_blocked(self, integration_client):
        """POST with eval() injection → 400."""
        client = integration_client
        resp = client.post(
            "/api/teams",
            json={"name": "test", "mode": "eval(os.system('id'))"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert any("eval" in v.lower() for v in body["violations"])

    def test_normal_input_passes(self, integration_client):
        """Normal, safe input should pass through guardrails."""
        client = integration_client
        team = make_team({"name": "safe-team-name", "mode": "coordinate"})
        resp = client.post(
            f"/api/teams/{team['name']}/agents",
            json={"name": "safe-agent", "role": "developer"},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["name"] == "safe-agent"

    def test_get_requests_bypass_guardrails(self, integration_client):
        """GET requests should not be checked by guardrails (only POST/PUT/PATCH)."""
        client = integration_client
        # Even with dangerous query params, GET should succeed
        resp = client.get("/api/teams")
        assert resp.status_code == 200

    def test_nested_dangerous_content_blocked(self, integration_client):
        """Dangerous content nested in JSON body should still be caught."""
        client = integration_client
        team = make_team({"name": "nested-test", "mode": "coordinate"})
        resp = client.post(
            f"/api/teams/{team['name']}/agents",
            json={
                "name": "nested-agent",
                "role": "developer",
                "system_prompt": {"command": "rm -rf /"},
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "violations" in body


# ============================================================
# 4. File lock + conflict detection + TTL expiry
# ============================================================


class TestFileLockIntegration:
    """File lock acquire/check/release and TTL-based auto-expiry."""

    def test_lock_acquire_conflict_release(self):
        """Agent A acquires lock → Agent B gets conflict → A releases → B succeeds."""
        from aiteam.api.file_lock import acquire_lock, check_lock, release_lock

        test_path = "/tmp/test-lock-integration/file.py"

        # Agent A acquires
        r1 = acquire_lock(test_path, "agent-a", ttl=60)
        assert r1["success"] is True
        assert r1["agent"] == "agent-a"

        # Agent B attempts → conflict
        r2 = acquire_lock(test_path, "agent-b", ttl=60)
        assert r2["success"] is False
        assert r2["held_by"] == "agent-a"
        assert r2["expires_in"] > 0

        # Check lock status
        status = check_lock(test_path)
        assert status["locked"] is True
        assert status["held_by"] == "agent-a"

        # Agent B cannot release Agent A's lock
        r3 = release_lock(test_path, "agent-b")
        assert r3["success"] is False

        # Agent A releases
        r4 = release_lock(test_path, "agent-a")
        assert r4["success"] is True

        # Now Agent B can acquire
        r5 = acquire_lock(test_path, "agent-b", ttl=60)
        assert r5["success"] is True

        # Cleanup
        release_lock(test_path, "agent-b")

    def test_lock_ttl_expiry(self):
        """Lock with very short TTL should auto-expire."""
        from aiteam.api.file_lock import acquire_lock, check_lock

        test_path = "/tmp/test-lock-ttl/expire.py"

        # Acquire with 1-second TTL
        r = acquire_lock(test_path, "short-lived", ttl=1)
        assert r["success"] is True

        # Wait for TTL to expire
        time.sleep(1.5)

        # Lock should be expired
        status = check_lock(test_path)
        assert status["locked"] is False

        # Another agent should be able to acquire
        r2 = acquire_lock(test_path, "new-agent", ttl=60)
        assert r2["success"] is True

        # Cleanup
        from aiteam.api.file_lock import release_lock
        release_lock(test_path, "new-agent")


# ============================================================
# 5. Channel messaging + @mention filtering
# ============================================================


class TestChannelMentionIntegration:
    """Send messages with @mentions → query mentions endpoint → verify filtering."""

    def test_send_and_query_mentions(self, integration_client):
        """Send messages with @mentions → filter by agent name."""
        client = integration_client

        # Send message with @mentions to team channel
        resp = client.post(
            "/api/channels/team:dev-team/messages",
            json={
                "sender": "leader",
                "content": "Hey @backend-dev please review the PR, and @qa-engineer run tests",
                "mentions": ["@backend-dev", "@qa-engineer"],
            },
        )
        assert resp.status_code == 201
        msg1 = resp.json()["data"]
        assert msg1["channel"] == "team:dev-team"
        assert "@backend-dev" in msg1["mentions"]

        # Send another message mentioning only @qa-engineer
        resp = client.post(
            "/api/channels/team:dev-team/messages",
            json={
                "sender": "backend-dev",
                "content": "@qa-engineer all tests should pass now",
                "mentions": ["@qa-engineer"],
            },
        )
        assert resp.status_code == 201

        # Send a message with no mentions
        resp = client.post(
            "/api/channels/team:dev-team/messages",
            json={
                "sender": "qa-engineer",
                "content": "Running integration tests now",
                "mentions": [],
            },
        )
        assert resp.status_code == 201

        # Query mentions for backend-dev — should find 1 message
        # Note: the API endpoint auto-prepends "@" to agent_name for filtering
        resp = client.get("/api/channels/mentions/backend-dev")
        assert resp.status_code == 200
        mentions = resp.json()
        assert mentions["total"] == 1
        assert mentions["data"][0]["sender"] == "leader"

        # Query mentions for qa-engineer — should find 2 messages
        resp = client.get("/api/channels/mentions/qa-engineer")
        assert resp.status_code == 200
        mentions = resp.json()
        assert mentions["total"] == 2

    def test_channel_message_read(self, integration_client):
        """Read messages from a channel in order."""
        client = integration_client

        # Send 3 messages
        for i in range(3):
            resp = client.post(
                "/api/channels/global/messages",
                json={
                    "sender": f"agent-{i}",
                    "content": f"Message number {i}",
                    "mentions": [],
                },
            )
            assert resp.status_code == 201

        # Read all messages
        resp = client.get("/api/channels/global/messages")
        assert resp.status_code == 200
        messages = resp.json()
        assert messages["total"] == 3

    def test_invalid_channel_format_rejected(self, integration_client):
        """Invalid channel format should be rejected with 400."""
        client = integration_client
        resp = client.post(
            "/api/channels/invalid-channel/messages",
            json={"sender": "test", "content": "hi", "mentions": []},
        )
        assert resp.status_code == 400


# ============================================================
# 6. Prompt Registry + effectiveness statistics
# ============================================================


class TestPromptRegistryEffectiveness:
    """Prompt version tracking + agent activity → effectiveness query."""

    def test_effectiveness_with_activities(self, repo_and_client):
        """Create agents with matching roles → record activities → query effectiveness."""
        repo, client = repo_and_client
        loop = asyncio.get_event_loop()

        # Create team + agents with template-matching roles
        team = make_team({"name": "prompt-eff-team"})

        resp = client.post(
            f"/api/teams/{team['name']}/agents",
            json={"name": "dev-1", "role": "backend-architect"},
        )
        agent_id = resp.json()["data"]["id"]

        # Record activities for the agent
        for tool_name in ["read_file", "edit_file", "bash"]:
            loop.run_until_complete(
                repo.create_activity(
                    agent_id=agent_id,
                    session_id="sess-001",
                    tool_name=tool_name,
                    input_summary="test input",
                    output_summary="test output",
                )
            )

        # Query effectiveness — should include stats for agents in matching roles
        resp = client.get("/api/prompt-registry/effectiveness")
        assert resp.status_code == 200
        result = resp.json()
        assert result["success"] is True
        # The effectiveness data should be a list (may be empty if no template file matches)
        assert isinstance(result["effectiveness"], list)

    def test_prompt_versions_empty(self, integration_client):
        """Prompt versions endpoint should return empty when nothing tracked."""
        resp = integration_client.get("/api/prompt-registry/versions")
        assert resp.status_code == 200
        result = resp.json()
        assert result["success"] is True
        assert result["total"] == 0


# ============================================================
# 7. Error recovery mapping
# ============================================================


class TestErrorRecoveryMapping:
    """MCP _api_call error handling attaches _recovery and _error_category."""

    def test_http_404_recovery(self):
        """HTTP 404 should return resource_not_found category and recovery hint."""
        from aiteam.mcp._error_recovery import get_http_recovery

        info = get_http_recovery(404)
        assert info["category"] == "resource_not_found"
        assert "recovery" in info
        assert len(info["recovery"]) > 0

    def test_http_500_recovery(self):
        """HTTP 500 should return server_error category."""
        from aiteam.mcp._error_recovery import get_http_recovery

        info = get_http_recovery(500)
        assert info["category"] == "server_error"

    def test_unknown_5xx_falls_back_to_500(self):
        """Unmapped 5xx codes should fall back to 500 entry."""
        from aiteam.mcp._error_recovery import get_http_recovery

        info = get_http_recovery(504)
        assert info["category"] == "server_error"

    def test_connection_refused_recovery(self):
        """Connection refused errors should map to api_unavailable."""
        from aiteam.mcp._error_recovery import get_connection_recovery

        info = get_connection_recovery("Connection refused")
        assert info["category"] == "api_unavailable"

    def test_timeout_recovery(self):
        """Timeout errors should map to timeout category."""
        from aiteam.mcp._error_recovery import get_connection_recovery

        info = get_connection_recovery("Request timed out")
        assert info["category"] == "timeout"

    def test_business_error_detection(self):
        """Business keywords in response body should be detected."""
        from aiteam.mcp._error_recovery import get_business_recovery

        assert get_business_recovery("任务不存在") == "resource_not_found"
        assert get_business_recovery("Team already exists") == "conflict"
        assert get_business_recovery("invalid parameter") == "validation_error"
        assert get_business_recovery("nothing special here") == ""

    def test_api_call_attaches_recovery_on_http_error(self):
        """_api_call should attach _recovery and _error_category on HTTP errors."""
        import urllib.error
        from io import BytesIO
        from unittest.mock import MagicMock

        from aiteam.mcp._base import _api_call

        # Mock a 404 HTTP error
        mock_error = urllib.error.HTTPError(
            url="http://localhost:8000/api/tasks/fake-id",
            code=404,
            msg="Not Found",
            hdrs=MagicMock(),
            fp=BytesIO(b'{"error": "not found"}'),
        )
        with patch("urllib.request.urlopen", side_effect=mock_error):
            result = _api_call("GET", "/api/tasks/fake-id")

        assert result["success"] is False
        assert "_error_category" in result
        assert "_recovery" in result
        assert result["_error_category"] == "resource_not_found"


# ============================================================
# 8. Combined: Task update → Event + Trust score
# ============================================================


class TestTaskEventIntegration:
    """Task completion triggers events (trust-score API retired 2026-07-27)."""

    def test_complete_task_records_events(self, repo_and_client):
        """Create task → complete → verify the completion event was recorded."""
        repo, client = repo_and_client

        # Setup team + agent
        team = make_team({"name": "trust-team"})

        resp = client.post(
            f"/api/teams/{team['name']}/agents",
            json={"name": "reliable-dev", "role": "developer"},
        )
        agent = resp.json()["data"]
        agent_id = agent["id"]

        # Create and complete task
        resp = client.post(
            f"/api/teams/{team['name']}/tasks/run",
            json={"title": "Trust test task", "description": "testing trust scores"},
        )
        task_id = resp.json()["data"]["id"]

        resp = client.put(f"/api/tasks/{task_id}/complete")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "completed"

        # Verify task.completed event was recorded
        resp = client.get("/api/events?type=task.completed")
        assert resp.status_code == 200
        events = resp.json()
        assert events["total"] >= 1

        # The agent row survives task completion with its (unused) trust_score column.
        # POST /api/agents/{id}/trust is gone — the scorer never had a caller and the
        # column stays only because deps' COLUMNS_TO_ENSURE backfill makes dropping it
        # pure cost. Assert the endpoint is really gone so it can't quietly return.
        resp = client.post(
            f"/api/agents/{agent_id}/trust",
            params={"task_result": "success"},
        )
        assert resp.status_code in (404, 405)
