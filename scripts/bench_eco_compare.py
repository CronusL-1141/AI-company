"""K1 — Controlled before/after benchmark on the SAME 50K DB.

Uses the existing seeded after-fix DB, drops the K1 indexes for "before",
re-runs the same queries, then re-creates indexes and re-runs for "after".
This isolates the index effect from random seed / cache variance.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiteam.storage.connection import close_db  # noqa: E402
from aiteam.storage.repository import StorageRepository  # noqa: E402

PROJECT = "p-test"
DB = r"C:\Users\TUF\AppData\Local\Temp\bench-eco-after-idx-50k.db"
DB_URL = f"sqlite+aiosqlite:///{DB}"

K1_INDEXES = [
    "ix_ecosystem_profiles_project_stars",
    "ix_ecosystem_profiles_project_category_stars",
    "ix_ecosystem_profiles_project_lang_stars",
    "ix_ecosystem_profiles_project_pushed",
    "ix_ecosystem_profiles_project_archived_stars",
    # Removed (cause perf regression): facet_cat / facet_lang
    "ix_ecosystem_profiles_facet_cat",
    "ix_ecosystem_profiles_facet_lang",
]


async def run_queries(label: str, repo: StorageRepository, runs: int = 80) -> None:
    rng = random.Random(42)  # FIXED seed for fair compare
    keywords = ["agent", "memory", "rag", "vector", "core"]
    cats = ["framework", "tooling", "agent", "memory", "core"]
    langs = ["Python", "TypeScript", "Go", "Rust", "Java"]

    res: dict[str, list[float]] = {
        "search_keyword": [],
        "search_tags_empty": [],
        "search_category_lang": [],
        "facet_counts": [],
    }

    for _ in range(runs):
        t = time.perf_counter()
        await repo.search_ecosystem_profiles_extended(keyword=rng.choice(keywords), limit=50)
        res["search_keyword"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await repo.search_ecosystem_profiles_extended(tags=None, limit=50)
        res["search_tags_empty"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await repo.search_ecosystem_profiles_extended(
            category=rng.choice(cats), language=rng.choice(langs), min_stars=1000, limit=50
        )
        res["search_category_lang"].append((time.perf_counter() - t) * 1000)

    for _ in range(20):
        t = time.perf_counter()
        await repo.compute_ecosystem_facet_counts(min_stars=500)
        res["facet_counts"].append((time.perf_counter() - t) * 1000)

    print(f"\n=== [{label}] ===")
    print(f"{'query':<26} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8}")
    for k, v in res.items():
        s = sorted(v)
        p95 = s[int(len(s) * 0.95) - 1]
        p99 = s[int(len(s) * 0.99) - 1] if len(s) >= 100 else max(s)
        print(
            f"{k:<26} {statistics.median(s):>7.2f}m {p95:>7.2f}m {p99:>7.2f}m "
            f"{statistics.mean(s):>7.2f}m"
        )


def drop_indexes() -> None:
    import sqlite3
    con = sqlite3.connect(DB)
    for idx in K1_INDEXES:
        try:
            con.execute(f"DROP INDEX IF EXISTS {idx}")
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()
    print("[ctrl] dropped K1 indexes")


def create_indexes() -> None:
    import sqlite3
    cmds = [
        "CREATE INDEX IF NOT EXISTS ix_ecosystem_profiles_project_stars"
        " ON ecosystem_repo_profiles (project_id, stars)",
        "CREATE INDEX IF NOT EXISTS ix_ecosystem_profiles_project_category_stars"
        " ON ecosystem_repo_profiles (project_id, relevance_category, stars)",
        "CREATE INDEX IF NOT EXISTS ix_ecosystem_profiles_project_lang_stars"
        " ON ecosystem_repo_profiles (project_id, language, stars)",
        "CREATE INDEX IF NOT EXISTS ix_ecosystem_profiles_project_pushed"
        " ON ecosystem_repo_profiles (project_id, pushed_at)",
        "CREATE INDEX IF NOT EXISTS ix_ecosystem_profiles_project_archived_stars"
        " ON ecosystem_repo_profiles (project_id, is_archived, stars)",
    ]
    con = sqlite3.connect(DB)
    for c in cmds:
        con.execute(c)
    con.commit()
    con.close()
    print("[ctrl] (re)created K1 indexes")


async def main() -> None:
    print(f"[setup] DB: {DB}")
    repo = StorageRepository(db_url=DB_URL, project_scope=PROJECT)

    # Phase BEFORE: drop K1 indexes + measure
    drop_indexes()
    await run_queries("BEFORE (no K1 indexes)", repo, runs=80)

    # Phase AFTER: recreate K1 indexes + measure
    create_indexes()
    await run_queries("AFTER (with K1 indexes)", repo, runs=80)

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
