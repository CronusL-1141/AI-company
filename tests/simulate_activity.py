"""AI Team OS — Phase 6 Integration Test: Simulate Dev Team Activity."""

import requests
import json

BASE = "http://localhost:8000"


def main():
    print("=" * 60)
    print("Phase 6: AI Team OS Dev Team Integration Test")
    print("=" * 60)

    # Step 1: Create Project Team
    print("\n--- Step 1: Register Dev Team ---")
    r = requests.post(f"{BASE}/api/teams", json={"name": "AI Team OS Dev", "mode": "coordinate"})
    team = r.json()["data"]
    TEAM_ID = team["id"]
    print(f"  Team created: {team['name']} (id={TEAM_ID[:8]}...)")

    # Step 2: Add 9 Agents
    print("\n--- Step 2: Add 9 Dev Team Members ---")
    agents_config = [
        ("Tech Lead", "tech-lead", "claude-opus-4-6"),
        ("Graph Engineer", "graph-engineer", "claude-sonnet-4-20250514"),
        ("Memory Engineer", "memory-engineer", "claude-sonnet-4-20250514"),
        ("Storage Engineer", "storage-engineer", "claude-sonnet-4-20250514"),
        ("CLI Engineer", "cli-engineer", "claude-sonnet-4-20250514"),
        ("API Engineer", "api-engineer", "claude-sonnet-4-20250514"),
        ("Frontend Engineer", "frontend-engineer", "claude-sonnet-4-20250514"),
        ("Frontend Engineer 2", "frontend-engineer-2", "claude-sonnet-4-20250514"),
        ("QA Engineer", "qa-engineer", "claude-sonnet-4-20250514"),
    ]

    agent_ids = {}
    for name, role, model in agents_config:
        r = requests.post(
            f"{BASE}/api/teams/{TEAM_ID}/agents",
            json={"name": name, "role": role, "model": model, "system_prompt": f"AI Team OS {role}"},
        )
        agent = r.json()["data"]
        agent_ids[role] = agent["id"]
        print(f"  + {name} ({role}) id={agent['id'][:8]}...")

    # Step 3: Set active/inactive statuses
    print("\n--- Step 3: Set Agent Statuses ---")
    active_roles = ["tech-lead", "frontend-engineer", "api-engineer"]
    for role in active_roles:
        r = requests.put(f"{BASE}/api/agents/{agent_ids[role]}/status", json={"status": "busy"})
        print(f"  {role} -> BUSY (active)")

    idle_roles = ["graph-engineer", "memory-engineer", "storage-engineer", "cli-engineer", "frontend-engineer-2", "qa-engineer"]
    for role in idle_roles:
        print(f"  {role} -> IDLE (inactive)")

    # Step 4: Check events
    print("\n--- Step 4: Check Events ---")
    r = requests.get(f"{BASE}/api/events?limit=50")
    events = r.json()
    print(f"  Total events: {events['total']}")
    for ev in events["data"][:5]:
        print(f"  - {ev['type']} | {ev['source']}")
    if events["total"] > 5:
        print(f"  ... and {events['total'] - 5} more")

    # Step 5: Create Meeting
    print("\n--- Step 5: Create Meeting ---")
    participants = [agent_ids[r] for r in ["tech-lead", "frontend-engineer", "api-engineer", "graph-engineer", "qa-engineer"]]
    r = requests.post(
        f"{BASE}/api/teams/{TEAM_ID}/meetings",
        json={"topic": "M3 Sprint Planning - Route/Meet Modes", "participants": participants},
    )
    meeting = r.json()["data"]
    MEETING_ID = meeting["id"]
    print(f"  Meeting: {meeting['topic']} (id={MEETING_ID[:8]}...)")

    # Step 6: Meeting conversation
    print("\n--- Step 6: Meeting Conversation (3 rounds) ---")
    conversations = [
        (1, "tech-lead", "Tech Lead", "Welcome to M3 planning. Key goals: Route + Meet orchestration modes, Dashboard expansion, Docker production config, E2E tests."),
        (1, "graph-engineer", "Graph Engineer", "Route mode can reuse existing LangGraph conditional branching. Meet mode needs a new multi-turn conversation graph with shared state."),
        (1, "frontend-engineer", "Frontend Engineer", "Dashboard needs 4 new pages: meeting notes, memory browser, cost tracking, debug panel. I suggest starting with meeting notes since backend support is ready."),
        (1, "api-engineer", "API Engineer", "We need new WebSocket channels for meeting events and Route mode status updates. I can add SSE as fallback for environments that block WS."),
        (1, "qa-engineer", "QA Engineer", "Playwright E2E framework is set up. We should target 80%+ coverage. Security audit can run in parallel with feature development."),
        (2, "tech-lead", "Tech Lead", "Good input. Priority order: 1) Route mode 2) Meet mode 3) Dashboard pages 4) Docker 5) E2E. Graph Engineer, start Route mode this sprint."),
        (2, "graph-engineer", "Graph Engineer", "Understood. Route mode needs a Router node that classifies tasks and routes to specialist agents. I will implement the classification logic using LLM-based routing."),
        (2, "frontend-engineer", "Frontend Engineer", "I will start the meeting notes page and memory browser this sprint. The cost tracking page depends on usage metrics API which API Engineer can provide."),
        (2, "api-engineer", "API Engineer", "I will add usage metrics endpoints and SSE fallback. Also need to implement the Route mode API integration."),
        (2, "qa-engineer", "QA Engineer", "I will write Playwright tests for existing Dashboard pages first, then expand to new pages as they are built."),
        (3, "tech-lead", "Tech Lead", "Sprint goals confirmed. Graph: Route mode. Frontend: meeting notes + memory browser. API: metrics + SSE. QA: Playwright foundation. Meeting adjourned."),
    ]

    for round_num, role, name, content in conversations:
        r = requests.post(
            f"{BASE}/api/meetings/{MEETING_ID}/messages",
            json={"agent_id": agent_ids[role], "agent_name": name, "content": content, "round_number": round_num},
        )
        status = "OK" if r.status_code in (200, 201) else f"ERR {r.status_code}"
        print(f"  [Round {round_num}] {name}: {content[:50]}... [{status}]")

    # Step 7: Conclude meeting
    print("\n--- Step 7: Conclude Meeting ---")
    r = requests.put(f"{BASE}/api/meetings/{MEETING_ID}/conclude")
    print(f"  Meeting concluded: {r.status_code}")

    # Step 8: Post-meeting status
    print("\n--- Step 8: Post-meeting status ---")
    for role in active_roles:
        requests.put(f"{BASE}/api/agents/{agent_ids[role]}/status", json={"status": "idle"})
    requests.put(f"{BASE}/api/agents/{agent_ids['frontend-engineer']}/status", json={"status": "busy"})
    requests.put(f"{BASE}/api/agents/{agent_ids['api-engineer']}/status", json={"status": "busy"})
    print("  frontend-engineer -> BUSY (implementing)")
    print("  api-engineer -> BUSY (implementing)")
    print("  others -> IDLE")

    # Step 9: Final stats
    print("\n--- Step 9: Final Stats ---")
    r = requests.get(f"{BASE}/api/events?limit=200")
    print(f"  Total events: {r.json()['total']}")

    r = requests.get(f"{BASE}/api/teams/{TEAM_ID}/status")
    status = r.json()["data"]
    print(f"  Team: {status['team']['name']}")
    print(f"  Agents: {len(status['agents'])}")

    r = requests.get(f"{BASE}/api/teams/{TEAM_ID}/meetings")
    meetings = r.json()["data"]
    print(f"  Meetings: {len(meetings)}")

    r = requests.get(f"{BASE}/api/meetings/{MEETING_ID}/messages")
    messages = r.json()["data"]
    print(f"  Meeting messages: {len(messages)}")

    # Save IDs
    with open("/tmp/test_team_data.json", "w") as f:
        json.dump({"team_id": TEAM_ID, "meeting_id": MEETING_ID, "agent_ids": agent_ids}, f)

    print("\n" + "=" * 60)
    print("Integration test COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
