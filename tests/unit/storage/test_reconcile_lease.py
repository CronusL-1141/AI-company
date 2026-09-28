"""项目整理权（reconcile lease）的存储层原子性。

整理权记在 project.config['memory'] 里，与 last_reconcile_at 同住一个 JSON 列。
读改写若不先拿写锁，并发的认领各自读到"无人持有"、各自写回，最后谁都以为
自己拿到了；整理时间戳的写入也会把别人刚写的租约冲掉。文件库 + 48 路并发。

连接池要先预热：冷池里第一个认领者用的是建项目时留下的热连接，别人还在建连接
它就已经提交完了，竞争窗口根本打不开（实测去掉写锁后冷池 10/10 次仍只有 1 个赢家，
预热后 10/10 次是 5 个）。常驻 API 的池子本来就是热的，这才是生产的形状。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

from aiteam.clock import utc_now
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

CONCURRENCY = 48
ROUNDS = 8
TTL = 1800


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _file_repo(tmp_path) -> tuple[StorageRepository, str]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}"
    return StorageRepository(db_url=url), url


async def _warm_pool(url: str) -> None:
    """Open every pooled connection once so all contenders start from a warm connection.

    Only for a file database: its engine uses a sized queue pool. In-memory engines
    get a StaticPool or NullPool, which has no size() and would raise here.
    """
    engine = get_engine(url)
    conns = await asyncio.gather(*(engine.connect() for _ in range(engine.sync_engine.pool.size())))
    for conn in conns:
        await conn.exec_driver_sql("SELECT 1")
    for conn in conns:
        await conn.close()


def test_concurrent_claims_grant_exactly_one_lease(tmp_path) -> None:
    repo, url = _file_repo(tmp_path)

    async def scenario() -> tuple[list[tuple[str, dict | None, str | None]], dict]:
        await repo.init_db()
        project = await repo.create_project(name="lease-race")
        await _warm_pool(url)
        outcomes = await asyncio.gather(*(
            repo.claim_reconcile_lease(
                project.id, session_id=f"sess-{i:02d}", lease_id="", ttl_seconds=TTL, allow_new=True
            )
            for i in range(CONCURRENCY)
        ))
        stored = (await repo.get_project(project.id)).config["memory"]["reconcile_lease"]
        await get_engine(url).dispose()
        return outcomes, stored

    outcomes, stored = asyncio.run(scenario())
    winners = [issued for outcome, _, issued in outcomes if outcome == "acquired"]
    assert len(winners) == 1, f"{len(winners)} sessions were each told they hold the lease"
    assert stored["lease_id_hash"] == _sha(winners[0])
    assert all(outcome == "held" for outcome, _, _ in outcomes if outcome != "acquired")


def test_reconcile_timestamp_writes_do_not_drop_the_lease(tmp_path) -> None:
    """A lease taken while timestamps land in the same JSON column must still be there.

    The loss needs a writer to read before the claim commits and write after it, which
    one round does not always produce; several rounds on fresh projects make it reliable.
    """
    repo, url = _file_repo(tmp_path)

    async def scenario() -> list[tuple[str, str | None, bool]]:
        await repo.init_db()
        rounds = []
        for i in range(ROUNDS):
            project = await repo.create_project(
                name=f"lease-vs-timestamp-{i}", root_path=f"/tmp/lease-vs-timestamp-{i}"
            )
            await _warm_pool(url)
            writers = [repo.set_last_reconcile_at(project.id) for _ in range(CONCURRENCY - 1)]
            claim = repo.claim_reconcile_lease(
                project.id, session_id="holder", lease_id="", ttl_seconds=TTL, allow_new=True
            )
            results = await asyncio.gather(*writers[:2], claim, *writers[2:])
            outcome, lease, _ = results[2]
            memory = (await repo.get_project(project.id)).config["memory"]
            kept = memory.get("reconcile_lease", {}).get("lease_id_hash") == lease["lease_id_hash"]
            rounds.append((outcome, memory.get("last_reconcile_at"), kept))
        await get_engine(url).dispose()
        return rounds

    rounds = asyncio.run(scenario())
    assert all(outcome == "acquired" and stamped for outcome, stamped, _ in rounds), rounds
    lost = sum(1 for *_, kept in rounds if not kept)
    assert lost == 0, (
        f"{lost}/{ROUNDS} rounds: a timestamp write read the config before the claim committed "
        "and wrote it back without the lease"
    )


def test_concurrent_config_edits_do_not_drop_the_lease(tmp_path) -> None:
    """PUT /api/projects config edits racing a claim: the lease and the edit both land."""
    repo, url = _file_repo(tmp_path)

    async def scenario() -> list[tuple[str, bool, bool]]:
        await repo.init_db()
        rounds = []
        for i in range(ROUNDS):
            project = await repo.create_project(
                name=f"lease-vs-config-{i}", root_path=f"/tmp/lease-vs-config-{i}"
            )
            await _warm_pool(url)
            edits = [
                repo.update_project(project.id, config={"edit": n}) for n in range(CONCURRENCY - 1)
            ]
            claim = repo.claim_reconcile_lease(
                project.id, session_id="holder", lease_id="", ttl_seconds=TTL, allow_new=True
            )
            results = await asyncio.gather(*edits[:2], claim, *edits[2:])
            outcome, lease, _ = results[2]
            config = (await repo.get_project(project.id)).config
            stored = (config.get("memory") or {}).get("reconcile_lease", {})
            kept = stored.get("lease_id_hash") == lease["lease_id_hash"]
            rounds.append((outcome, kept, "edit" in config))
        await get_engine(url).dispose()
        return rounds

    rounds = asyncio.run(scenario())
    assert all(outcome == "acquired" and edited for outcome, _, edited in rounds), rounds
    lost = sum(1 for _, kept, _ in rounds if not kept)
    assert lost == 0, f"{lost}/{ROUNDS} rounds: a config edit wrote back a config without the lease"


def test_ownership_expiry_and_release(tmp_path) -> None:
    repo, url = _file_repo(tmp_path)

    async def scenario() -> list:
        await repo.init_db()
        pid = (await repo.create_project(name="lease-rules")).id
        seen = []
        seen.append(await repo.claim_reconcile_lease(
            pid, session_id="", lease_id="", ttl_seconds=TTL, allow_new=False))
        _, _, first = await repo.claim_reconcile_lease(
            pid, session_id="", lease_id="", ttl_seconds=TTL, allow_new=True)
        # Anonymous callers are recognised by lease_id only: an empty session never matches.
        seen.append((await repo.claim_reconcile_lease(
            pid, session_id="", lease_id="", ttl_seconds=TTL, allow_new=True))[0])
        seen.append((await repo.claim_reconcile_lease(
            pid, session_id="", lease_id=first, ttl_seconds=TTL, allow_new=False))[0])
        # Not the holder: release is a no-op.
        seen.append(await repo.release_reconcile_lease(pid, session_id="other", lease_id=""))
        seen.append(await repo.release_reconcile_lease(pid, session_id="", lease_id=first))
        seen.append((await repo.get_project(pid)).config["memory"].get("reconcile_lease"))
        await get_engine(url).dispose()
        return seen

    not_held, held, renewed, foreign_release, own_release, after = asyncio.run(scenario())
    assert not_held == ("not_held", None, None)
    assert held == "held"
    assert renewed == "renewed"
    assert foreign_release is False and own_release is True
    assert after is None


def test_unparsable_expiry_counts_as_expired() -> None:
    assert StorageRepository.reconcile_lease_expired({"expires_at": "not a time"})
    assert StorageRepository.reconcile_lease_expired({})
    future = (utc_now() + timedelta(minutes=5)).isoformat()
    assert not StorageRepository.reconcile_lease_expired({"expires_at": future})


def test_hand_edited_hashes_never_match_and_never_raise() -> None:
    """compare_digest raises on non-ASCII str; a hand-edited config must just not match."""
    for stored in ("é" * 64, 12345, None, ""):
        lease = {"lease_id_hash": stored, "holder_hash": stored, "expires_at": "2999-01-01T00:00:00+00:00"}
        assert StorageRepository.reconcile_lease_owned(lease, "sess", "lease") is False
    real = {"lease_id_hash": _sha("lease"), "holder_hash": _sha("sess")}
    assert StorageRepository.reconcile_lease_owned(real, "", "lease") is True
    assert StorageRepository.reconcile_lease_owned(real, "sess", "") is True
    assert StorageRepository.reconcile_lease_owned(real, "", "") is False
