"""Measure /api/hooks/event latency with the workflow backup off and on, at 48 concurrent requests.

A manual tool, not a test (pytest does not collect ``tools_*``). Every phase gets a
fresh isolated API process over a throwaway HOME (never port 8000, never the real
HOME) and a config folder shaped like a real one, and sends rounds of 48 concurrent
hook events: file writes into project memory, shell commands and turn ends.

* off: the folder is not enrolled (the backup only checks that nothing is enrolled);
* on: the folder is enrolled, every write is a real change (fast path queued and
  processed), every turn end follows a shell command (a full comparison runs);
* control: a second "off", to show how far two identical phases drift apart.

Each trial runs the three phases in a shuffled order; the verdict compares the
median p95 of "on" with that of "off": it passes when the increase is at most
``MAX_RELATIVE`` of "off" or at most ``MAX_ABSOLUTE_MS``, whichever is larger.

Usage: PYTHONPATH=src python3 tests/manual/tools_asset_hook_latency.py [--trials 4] [--rounds 6] [--seed N]
Prints every phase and the verdict as JSON; exit 1 when the verdict fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.integration.test_workflow_backup_live import MEMORY, Api  # noqa: E402
from tests.unit.assets.conftest import PROJECT_ROOT, PROJECT_SLUG, build_config_folder  # noqa: E402

CONCURRENCY = 48
MAX_RELATIVE = 0.10
MAX_ABSOLUTE_MS = 15.0
PHASES = ("off", "on", "control")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def _events(api: Api, tag: str, number: int, enrolled: bool) -> list[dict]:
    """One round: 40 writes, 4 shell commands, 4 turn ends."""
    events = []
    for n in range(CONCURRENCY - 8):
        path = api.folder / MEMORY / f"bench-{n:02d}.md"
        if enrolled:
            path.write_text(f"{tag} round {number} note {n}\n", encoding="utf-8")
        events.append({"hook_event_name": "PostToolUse", "session_id": f"bench-{n % 4}", "tool_name": "Write",
                       "tool_use_id": f"toolu_{tag}_{number}_{n}",
                       "tool_input": {"file_path": str(path), "content": "x"}})
    for n in range(4):
        events.append({"hook_event_name": "PostToolUse", "session_id": f"bench-{n}", "tool_name": "Bash",
                       "tool_use_id": f"toolu_{tag}_{number}_b{n}", "tool_input": {"command": "true"}})
        events.append({"hook_event_name": "Stop", "session_id": f"bench-{n}"})
    for event in events:
        event.update(cwd=PROJECT_ROOT,
                     transcript_path=str(api.folder / "projects" / PROJECT_SLUG / f"{event['session_id']}.jsonl"))
    return events


async def _measure(api: Api, tag: str, rounds: int, enrolled: bool) -> list[float]:
    async with httpx.AsyncClient(base_url=api.base, timeout=60, trust_env=False,
                                 limits=httpx.Limits(max_connections=CONCURRENCY)) as client:
        async def send(event: dict) -> float:
            started = time.perf_counter()
            (await client.post("/api/hooks/event", json=event)).raise_for_status()
            return (time.perf_counter() - started) * 1000

        samples: list[float] = []
        for number in range(rounds + 1):  # round 0 warms up
            got = await asyncio.gather(*(send(event) for event in _events(api, tag, number, enrolled)))
            if number:
                samples += got
    return samples


def _phase(name: str, rounds: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="asset-bench-") as temp:
        api = Api(Path(temp))
        build_config_folder(api.folder)
        for n in range(CONCURRENCY):
            (api.folder / MEMORY / f"bench-{n:02d}.md").write_text("seed\n", encoding="utf-8")
        api.start()
        try:
            enrolled = name == "on"
            if enrolled:
                api.client.post("/api/assets/roots", json={"profile": "bench"}).raise_for_status()
                api.client.post("/api/assets/run", json={}).raise_for_status()
            samples = asyncio.run(_measure(api, name, rounds, enrolled))
            result = {"phase": name, "p50": round(statistics.median(samples), 1),
                      "p95": round(_percentile(samples, 0.95), 1), "requests": len(samples)}
            if enrolled:
                result["versions"] = api.rows("SELECT count(*) FROM asset_versions")[0][0]
                result["repeated_versions"] = api.rows(
                    "SELECT count(*) FROM asset_versions WHERE content_hash = parent_hash")[0][0]
            return result
        finally:
            api.stop()
            api.client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--seed", type=int, default=int(time.time()))
    args = parser.parse_args()
    shuffle = random.Random(args.seed)
    runs: list[dict] = []
    for trial in range(args.trials):
        order = list(PHASES)
        shuffle.shuffle(order)
        for name in order:
            runs.append({**_phase(name, args.rounds), "trial": trial})
            print(json.dumps(runs[-1]), flush=True)
    p95 = {name: statistics.median(r["p95"] for r in runs if r["phase"] == name) for name in PHASES}
    limit = max(MAX_ABSOLUTE_MS, MAX_RELATIVE * p95["off"])
    verdict = {
        "seed": args.seed, "trials": args.trials, "median_p95_ms": p95,
        "delta_on_vs_off_ms": round(p95["on"] - p95["off"], 1),
        "delta_control_vs_off_ms": round(p95["control"] - p95["off"], 1),
        "limit_ms": round(limit, 1), "pass": p95["on"] - p95["off"] <= limit,
    }
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
