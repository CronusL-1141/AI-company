"""K1 — Ecosystem search performance baseline benchmark.

Builds a 10K profile + 50K repo_tag dataset, then measures p50/p95/p99
of common /api/ecosystem/profiles search queries. Also dumps EXPLAIN
QUERY PLAN for each shape so we can confirm index usage.

Usage:
    python scripts/bench_ecosystem_search.py
    python scripts/bench_ecosystem_search.py --label after-fix
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import string
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Ensure src on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiteam.storage.connection import close_db, get_session  # noqa: E402
from aiteam.storage.repository import StorageRepository  # noqa: E402
from aiteam.types import (  # noqa: E402
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)

CATEGORIES = ["framework", "tooling", "agent", "memory", "observability", "core"]
LANGUAGES = ["Python", "TypeScript", "JavaScript", "Go", "Rust", "Java"]
TAG_NAMES = [f"capability-{i:03d}" for i in range(40)]


def _rand_word(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _make_profile(i: int) -> EcosystemRepoProfile:
    pushed = datetime.now(tz=UTC) - timedelta(days=random.randint(0, 365))
    return EcosystemRepoProfile(
        repo_full_name=f"owner{i % 200}/repo-{i:05d}",
        name=f"repo-{i:05d}",
        owner=f"owner{i % 200}",
        description=f"Some library doing {_rand_word()} with {_rand_word(6)} support.",
        stars=random.randint(50, 200_000),
        language=random.choice(LANGUAGES),
        topics=random.sample(["claude", "ai", "agent", "rag", "memory", "vector"], k=2),
        relevance_category=random.choice(CATEGORIES),
        relevance_score=random.randint(0, 100),
        one_line_summary=f"Quick blurb {_rand_word(10)}",
        pushed_at=pushed,
        is_archived=random.random() < 0.05,
        description_excerpt=f"{_rand_word(20)} {_rand_word(20)}",
    )


async def seed(repo: StorageRepository, n_profiles: int, n_tags: int, edges_per_repo: int) -> None:
    """Insert n_profiles + tag dictionary + repo_tag edges."""
    print(f"[seed] inserting {n_profiles} profiles ...")
    t0 = time.perf_counter()
    profiles = [_make_profile(i) for i in range(n_profiles)]
    # Bulk insert via individual upsert (repo doesn't expose bulk; fast enough for benchmark)
    batch = 500
    for i in range(0, len(profiles), batch):
        for p in profiles[i : i + batch]:
            await repo.upsert_ecosystem_profile(p)
        if i and i % 2000 == 0:
            print(f"  {i}/{n_profiles}")
    print(f"[seed] profiles done in {time.perf_counter() - t0:.1f}s")

    print(f"[seed] inserting {n_tags} tags ...")
    tags: list[EcosystemTag] = []
    for i in range(n_tags):
        t = EcosystemTag(
            name=TAG_NAMES[i % len(TAG_NAMES)] if i < len(TAG_NAMES) else f"tag-{i}",
            category=EcosystemTagCategory(random.choice(["capability", "tech_stack", "maturity", "positioning"])),
        )
        await repo.upsert_tag(t)
        tags.append(t)

    print(f"[seed] inserting ~{n_profiles * edges_per_repo} repo_tags ...")
    t0 = time.perf_counter()
    all_tag_objs = await repo.list_tags(limit=10000)
    for p in profiles:
        chosen = random.sample(all_tag_objs, k=min(edges_per_repo, len(all_tag_objs)))
        for tag in chosen:
            rt = EcosystemRepoTag(
                repo_id=p.id,
                tag_id=tag.id,
                confidence=1.0,
                source=EcosystemTagSource.MANUAL,
            )
            try:
                await repo.add_repo_tag(rt)
            except Exception:
                pass
    print(f"[seed] repo_tags done in {time.perf_counter() - t0:.1f}s")


async def explain(db_url: str, sql_label: str, sql: str) -> None:
    """Print EXPLAIN QUERY PLAN for raw SQL."""
    from sqlalchemy import text
    async with get_session(db_url) as session:
        rows = (await session.execute(text(f"EXPLAIN QUERY PLAN {sql}"))).all()
        print(f"--- EXPLAIN [{sql_label}] ---")
        for r in rows:
            print("  ", tuple(r))


async def bench(repo: StorageRepository, db_url: str, label: str, runs: int = 100) -> dict:
    keywords = ["agent", "memory", "rag", "vector", _rand_word(5), "core"]
    results: dict[str, list[float]] = {
        "search_keyword": [],
        "search_tags_empty": [],
        "search_tags_one": [],
        "search_category_lang": [],
        "summary_health": [],
    }

    # Pre-fetch a sample tag for tag-based search
    all_tags = await repo.list_tags(limit=10)
    sample_tag = all_tags[0].name if all_tags else "capability-001"

    print(f"[bench {label}] running {runs} iterations per query ...")

    for _ in range(runs):
        kw = random.choice(keywords)
        t = time.perf_counter()
        await repo.search_ecosystem_profiles_extended(keyword=kw, limit=50)
        results["search_keyword"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        # tags=[] (the bug — empty list goes through extended path with no tag filter)
        await repo.search_ecosystem_profiles_extended(tags=None, limit=50)
        results["search_tags_empty"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await repo.search_ecosystem_profiles_extended(tags=[sample_tag], limit=50)
        results["search_tags_one"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        await repo.search_ecosystem_profiles_extended(
            category=random.choice(CATEGORIES),
            language=random.choice(LANGUAGES),
            min_stars=1000,
            limit=50,
        )
        results["search_category_lang"].append((time.perf_counter() - t) * 1000)

    # facet_counts
    for _ in range(20):
        t = time.perf_counter()
        await repo.compute_ecosystem_facet_counts(min_stars=500)
        results["summary_health"].append((time.perf_counter() - t) * 1000)

    summary: dict[str, dict[str, float]] = {}
    for k, vals in results.items():
        vals_sorted = sorted(vals)
        summary[k] = {
            "n": len(vals),
            "p50": statistics.median(vals_sorted),
            "p95": vals_sorted[int(len(vals_sorted) * 0.95) - 1] if vals_sorted else 0,
            "p99": vals_sorted[int(len(vals_sorted) * 0.99) - 1] if vals_sorted else 0,
            "mean": statistics.mean(vals_sorted),
            "max": max(vals_sorted) if vals_sorted else 0,
        }

    print(f"\n=== Bench results [{label}] ===")
    print(f"{'query':<26} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8} {'max':>8}")
    for k, s in summary.items():
        print(
            f"{k:<26} {s['p50']:>7.2f}m {s['p95']:>7.2f}m {s['p99']:>7.2f}m "
            f"{s['mean']:>7.2f}m {s['max']:>7.2f}m"
        )

    print("\n--- EXPLAIN QUERY PLAN ---")
    # NOTE: EXPLAIN with project_id filter to mirror production query shape
    pid = "p-test"
    await explain(
        db_url,
        "search project+keyword + sort stars",
        f"SELECT * FROM ecosystem_repo_profiles "
        f"WHERE project_id='{pid}' "
        "AND (name LIKE '%foo%' OR description LIKE '%foo%') "
        "ORDER BY stars DESC LIMIT 50",
    )
    await explain(
        db_url,
        "search project+category+lang+min_stars",
        f"SELECT * FROM ecosystem_repo_profiles "
        f"WHERE project_id='{pid}' AND relevance_category='tooling' "
        "AND language='Python' AND stars>=1000 "
        "ORDER BY stars DESC LIMIT 50",
    )
    await explain(
        db_url,
        "search project sort stars only",
        f"SELECT * FROM ecosystem_repo_profiles WHERE project_id='{pid}' "
        "ORDER BY stars DESC LIMIT 50",
    )
    await explain(
        db_url,
        "search project sort pushed_at",
        f"SELECT * FROM ecosystem_repo_profiles WHERE project_id='{pid}' "
        "ORDER BY pushed_at DESC LIMIT 50",
    )
    await explain(
        db_url,
        "tags subquery (1 tag) + project",
        f"SELECT * FROM ecosystem_repo_profiles WHERE project_id='{pid}' "
        "AND id IN (SELECT repo_id FROM ecosystem_repo_tags WHERE tag_id='abc') "
        "ORDER BY stars DESC LIMIT 50",
    )
    await explain(
        db_url,
        "facet project + count by category",
        f"SELECT relevance_category, COUNT(id) FROM ecosystem_repo_profiles "
        f"WHERE project_id='{pid}' AND stars>=500 GROUP BY relevance_category",
    )
    await explain(
        db_url,
        "facet project + count by language",
        f"SELECT language, COUNT(id) FROM ecosystem_repo_profiles "
        f"WHERE project_id='{pid}' AND stars>=500 GROUP BY language",
    )

    return summary


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--profiles", type=int, default=10000)
    parser.add_argument("--tags", type=int, default=40)
    parser.add_argument("--edges", type=int, default=5)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--db", default="")
    args = parser.parse_args()

    random.seed(args.seed)

    db_path = args.db or str(Path(tempfile.gettempdir()) / f"bench-eco-{args.label}.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if Path(db_path).exists() and not args.db:
        Path(db_path).unlink()
    db_url = f"sqlite+aiosqlite:///{db_path}"
    print(f"[setup] db_url={db_url}")

    # Use project_scoped repo to mirror production (project_id filter is the
    # default in the OS; without it the composite (project_id, ...) indexes
    # wouldn't be picked.)
    project_id = "p-test"
    repo = StorageRepository(db_url=db_url, project_scope=project_id)
    await repo.init_db()

    # Seed if empty
    existing, _ = await repo.search_ecosystem_profiles_extended(limit=1)
    if not existing:
        await seed(repo, args.profiles, args.tags, args.edges)

    summary = await bench(repo, db_url, args.label, runs=args.runs)

    out = ROOT / "tmp" / f"bench-{args.label}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[done] saved {out}")

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
