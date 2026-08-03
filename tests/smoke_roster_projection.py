#!/usr/bin/env python3
"""Measure the roster-projection tools against the live API (real payloads).

Not a unit test - it needs a running OS API and the real database, so it is
named smoke_* to stay out of pytest collection. Usage:

    python3 tests/smoke_roster_projection.py [team_id ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.mcp.tools import agent as agent_tools  # noqa: E402
from aiteam.mcp.tools import team as team_tools  # noqa: E402

# CC 2.1.219: MAX_MCP_OUTPUT_TOKENS defaults to 25,000 tokens. Measured on real
# OS payloads: 43,916 chars passed, 67,766 chars was rejected.
SAFE_CHARS = 20_000


class _Collector:
    """Minimal stand-in for the FastMCP registrar: keeps the plain functions."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *_args, **_kwargs):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn

        return wrap


def _size(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str))


def main(team_ids: list[str]) -> int:
    collector = _Collector()
    agent_tools.register(collector)
    team_tools.register(collector)
    tools = collector.tools

    rows: list[tuple[str, int, str]] = []
    worst = 0

    result = tools["team_list"]()
    rows.append(("team_list()", _size(result), f"matched={result.get('matched')} total={result.get('total')}"))

    for tid in team_ids:
        for name in ("agent_list", "team_status", "team_briefing"):
            out = tools[name](tid)
            members = out.get("member_total") or out.get("counted")
            offline = (out.get("offline") or {}).get("count")
            rows.append((f"{name}({tid[:8]})", _size(out), f"members={members} offline={offline}"))
        act = tools["agent_activity_query"](team_id=tid, limit=60)
        rows.append((f"agent_activity_query({tid[:8]},60)", _size(act), f"rows={len(act.get('activities') or [])}"))

    for label, size, note in rows:
        worst = max(worst, size)
        flag = "OK " if size < SAFE_CHARS else "OVER"
        print(f"[{flag}] {label:44s} {size:>8,d} chars   {note}")

    print(f"\nworst={worst:,d} chars, safe threshold={SAFE_CHARS:,d}")
    return 0 if worst < SAFE_CHARS else 1


if __name__ == "__main__":
    ids = sys.argv[1:] or [
        "c744f317-6009-48f8-9da5-6f2678610eac",  # 51-member session container team
        "ddfac2dc-1d3d-4ff9-aec5-923e1f0bd0ab",  # 173-member workflow team
    ]
    raise SystemExit(main(ids))
