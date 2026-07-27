"""Ops one-shot — release Stage 0 queue rows whose claim lease has expired.

Background (2026-07-27 incident): a ``queued`` deep-review row is reserved at
INSERT time (``claimed_by='tick:<id>'``) so ``tick`` dispatch and the pull-based
``claim_next_shallow_repo`` can never grab the same row. The reservation used to
have no expiry, so when a dispatched sub-agent died the row stayed
``queued + claimed`` forever — invisible to every claimer. Production went 17
days with 74 wedged rows while ``shallow_queue_status`` reported ``pending=0``.

The lease TTL now lets claimers take those rows over on their own; this script
is the *bookkeeping* companion: it clears the dead reservations in one sweep so
the queue counters tell the truth again.

Safety: **dry-run by default** — it prints what would be released and writes
nothing. Pass ``--apply`` to actually clear the reservations.

Usage::

    python3 scripts/reclaim_stale_shallow_claims.py                 # preview
    python3 scripts/reclaim_stale_shallow_claims.py --ttl-minutes 30
    python3 scripts/reclaim_stale_shallow_claims.py --project-id <uuid>
    python3 scripts/reclaim_stale_shallow_claims.py --apply         # write

Only ``claimed_by`` / ``claimed_at`` are cleared. ``stage_status`` stays
``queued``, no row is deleted, and rows whose lease is still valid are never
touched — so a live worker can never be interrupted by this script.
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
from aiteam.storage.repository import (  # noqa: E402
    STALE_CLAIM_TTL_SECONDS,
    StorageRepository,
)


def _fmt_age(seconds: float) -> str:
    """Human-readable claim age — ``inf`` marks a row with no ``claimed_at``."""
    if seconds == float("inf"):
        return "unknown (no claimed_at)"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def _run(args: argparse.Namespace) -> int:
    ttl_seconds = (
        args.ttl_minutes * 60 if args.ttl_minutes is not None else STALE_CLAIM_TTL_SECONDS
    )
    repo = StorageRepository(db_url=args.db_url)

    rows = await repo.reclaim_stale_shallow_claims(
        dry_run=not args.apply,
        ttl_seconds=ttl_seconds,
        project_id=args.project_id or None,
        limit=args.limit,
    )

    mode = "APPLY (rows released)" if args.apply else "DRY-RUN (nothing written)"
    print(f"db          : {args.db_url}")
    print(f"project     : {args.project_id or '(all projects)'}")
    print(f"lease TTL   : {ttl_seconds}s ({ttl_seconds // 60} min)")
    print(f"mode        : {mode}")
    print(f"stale rows  : {len(rows)}")
    print()

    if rows:
        print(
            f"{'deep_review_id':38} {'claimed_by':24} {'claimed_at':28} age"
        )
        for row in rows:
            print(
                f"{row['id']:38} "
                f"{(row['claimed_by'] or ''):24} "
                f"{(row['claimed_at'] or '-'):28} "
                f"{_fmt_age(row['stale_seconds'])}"
            )
        print()

    if not args.apply:
        print(
            "No changes written. Re-run with --apply to release these "
            "reservations (stage_status stays 'queued'; nothing is deleted)."
        )
    else:
        print(f"Released {len(rows)} stale claim(s).")

    await close_db()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List (default) or release Stage 0 deep-review rows stuck at "
            "stage_status='queued' with an expired claim lease."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually clear claimed_by/claimed_at (default: dry-run only)",
    )
    parser.add_argument(
        "--ttl-minutes",
        type=int,
        default=None,
        help=(
            "claim lease length in minutes; claims older than this count as "
            f"stale (default: {STALE_CLAIM_TTL_SECONDS // 60})"
        ),
    )
    parser.add_argument(
        "--project-id",
        default="",
        help="restrict to one project (default: every project)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="max rows to process in one sweep (default: 1000)",
    )
    parser.add_argument(
        "--db-url",
        default=DEFAULT_DB_URL,
        help="SQLAlchemy DB URL (default: the OS database)",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
