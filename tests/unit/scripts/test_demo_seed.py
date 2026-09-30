"""scripts/demo_seed.py builds a demo database the Dashboard reads back as intended.

The README screenshots come from this seed. It writes through the production
models, the notice ledger and the account-usage repository, so a schema or
validator change breaks it here instead of on the next screenshot round. The
assertions read the data back the way the pages do (digest, notice list, plan
estimates), across the persistence boundary.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_seed():
    spec = importlib.util.spec_from_file_location("demo_seed_probe", ROOT / "scripts" / "demo_seed.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed_module = _load_seed()


async def test_seed_reads_back_as_the_pages_show_it(tmp_path: Path) -> None:
    """One seed (it takes seconds) serves both the read-back and the live refresh."""
    from aiteam.api.routes.task_wall import load_task_wall_digest
    from aiteam.clock import utc_now
    from aiteam.storage.account_usage import AccountUsageRepository
    from aiteam.storage.repository import StorageRepository

    db_path = tmp_path / "demo.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    counts = await seed_module.seed(db_url, str(tmp_path / "aurora-web"))
    assert counts["notices"] == 8

    repo = StorageRepository(db_url)
    pid = (await repo.list_projects())[0].id
    digest, _ = await load_task_wall_digest(repo, pid)
    assert digest.open_total == 15
    assert digest.stale_running == 1
    assert digest.dormant_pending == 2
    assert [item.status for item in digest.stuck] == ["blocked", "failed", "running"]

    notices, _ = await repo.list_notices(statuses=None, limit=100)
    by_id: dict[str, list] = {}
    for row in notices:
        by_id.setdefault(row.catalog_id, []).append(row)
    assert set(by_id) == {
        "decisions_pending", "channel_mention", "codex_untrusted", "branch_switched",
        "blocked_secret_add", "blocked_teardown", "blocked_foreign_branch",
    }
    assert by_id["decisions_pending"][0].params["n"] == 3
    assert {row.host for row in by_id["channel_mention"]} == {"cc", "codex"}
    assert by_id["codex_untrusted"][0].status.value == "snoozed"

    accounts = AccountUsageRepository(db_url)
    key = (await accounts.list_accounts())[0].account_key
    estimates = await accounts.pricing_plan_estimates(key, now=utc_now())
    assert sorted(item.window_duration_ms for item in estimates) == [
        seed_module.PLAN_5H_MS, seed_module.PLAN_WEEK_MS,
    ]
    assert all(item.status == "estimated" and item.estimated_total_usd for item in estimates)

    # The API resets busy agents when it starts; --refresh-live puts them back.
    with sqlite3.connect(db_path) as con:
        con.execute("UPDATE agents SET status = 'offline'")

    seed_module.refresh_live(db_path)

    with sqlite3.connect(db_path) as con:
        busy = {name for (name,) in con.execute("SELECT name FROM agents WHERE status = 'busy'")}
    expected = {name for name, (status, *_rest) in seed_module.LIVE_STATE.items() if status.value == "busy"}
    assert expected <= busy
