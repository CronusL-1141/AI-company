"""Ops one-shot — realign teams/agents whose project attribution contradicts their Leader.

Background (2026-07-27 incident): ``deps._auto_create_projects`` used to bulk-bind
every orphan team to a single project resolved from the **API process**
``os.getcwd()`` (with ``existing_projects[0]`` as an ultimate fallback). That cwd
is shared by every CC session, so whichever directory the server happened to be
launched from claimed all unbound teams. Production ended up with 116 teams filed
under the wrong project — including session container teams whose own Leader row
carried the correct one — and ``_on_subagent_start`` propagated the wrong id onto
each new agent row plus the ``{project_path}`` in its rendered system_prompt.

The registration path is fixed; this script is the *bookkeeping* companion that
heals rows written before the fix.

Repair rule (deliberately conservative): a team is only touched when a Leader row
**inside that team** has a project_id and it differs from the team's. Teams with
no Leader, an unbound Leader, or ``kind=workflow`` are left exactly as they are —
leaving a row unbound beats guessing at it a second time.

Safety: **dry-run by default** — it prints what would change and writes nothing.
Pass ``--apply`` to write.

Usage::

    python3 scripts/repair_team_project_attribution.py            # preview
    python3 scripts/repair_team_project_attribution.py --apply    # write
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Allow ``import aiteam.*`` from src/ when running via repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from aiteam.storage.connection import DEFAULT_DB_URL, close_db  # noqa: E402
from aiteam.storage.repository import StorageRepository  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    repo = StorageRepository(db_url=args.db_url)
    rows = await repo.repair_team_project_attribution(
        dry_run=not args.apply, limit=args.limit
    )

    projects = {p.id: p.name for p in await repo.list_projects()}

    def _name(pid: str | None) -> str:
        if not pid:
            return "(unbound)"
        return f"{projects.get(pid, '?')}[{pid[:8]}]"

    mode = "APPLY (rows rewritten)" if args.apply else "DRY-RUN (nothing written)"
    print(f"db            : {args.db_url}")
    print(f"mode          : {mode}")
    print(f"teams to fix  : {len(rows)}")
    print(f"agents to fix : {sum(r['agents_fixed'] for r in rows)}")
    print()

    if rows:
        print(f"{'team':32} {'from':32} {'to':32} agents")
        for row in rows:
            print(
                f"{row['team_name'][:32]:32} "
                f"{_name(row['from_project_id']):32} "
                f"{_name(row['to_project_id']):32} "
                f"{row['agents_fixed']}"
            )
        print()

    if not args.apply:
        print(
            "No changes written. Re-run with --apply to realign these rows "
            "(nothing is deleted; only project_id is rewritten)."
        )
    else:
        print(f"Realigned {len(rows)} team(s).")

    await close_db()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List (default) or repair teams whose project_id contradicts the "
            "Leader row inside that same team."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually rewrite project_id (default: dry-run only)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5000,
        help="max teams to scan in one sweep (default: 5000)",
    )
    parser.add_argument(
        "--db-url",
        default=DEFAULT_DB_URL,
        help="database URL (default: the OS production DB)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
