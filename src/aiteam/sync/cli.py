"""``python3 -m aiteam.sync``: the operator's commands for multi-host sync.

Every command that changes something prints what it would do and stops, unless
given ``--apply``. Nothing here runs by itself: these are the explicit steps the
design reserves for the user (writing ``node.json``, moving a database, updating
the center).

Two machines, one center:

* The center's API listens on 127.0.0.1:8000 only. The edge reaches it through an
  SSH local forward, 127.0.0.1:18000 -> center 127.0.0.1:8000, kept up by the
  network side (not by OS). The edge's ``center_url`` is ``http://127.0.0.1:18000``.
* Cut-over: stop both APIs; ``check-baseline`` the two databases (their
  ``PRAGMA user_version`` must match); copy the edge's database over the center's
  so both start from one baseline; ``enable --role center`` on the center (it
  records where its change log stands); ``enable --role edge`` on the edge; start
  the center, then the edge. The edge's database is not archived: it stays the
  edge's replica. ``node.json`` must never travel between machines (exclude it from
  any copy or sync tool): a copy is refused on the other machine.

Commands:

    enable --role center|edge --name NAME [--center-url URL]   write node.json (keeps an identity that holds)
    repin [--rebuild]                                          edge: accept a new or restored center
    status                                                     this node's sync state
    orphans [--revert]                                         shared rows written outside any operation
    check-baseline DB_A DB_B                                   two databases fit to sync
    compare DB_A DB_B                                          shared face, row by row
    update-center --host HOST --repo PATH                      update the center from the edge
"""

from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import subprocess
from pathlib import Path

from aiteam.sync.protocol import CENTER_API_PORT, DEFAULT_CENTER_URL, schema_version
from aiteam.sync.tables import SHARED_TABLES, SHARED_TABLES_SQL
from aiteam.types import NodeRole


def _db_path(given: str | None) -> Path:
    if given:
        return Path(given).expanduser()
    from aiteam.storage.connection import default_db_path

    return default_db_path()


def _connect(path: Path, *, readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


# -- enable and repin ------------------------------------------------------------

def _write_db(db: Path, statement: str, *params: object) -> None:
    con = _connect(db, readonly=False)
    try:
        con.execute(statement, params)
        con.commit()
    finally:
        con.close()


def enable(args: argparse.Namespace) -> int:
    """Write this machine's node.json; idempotent: an identity that holds here is kept.

    Keeping the node id matters: the center's id is pinned by its edges, an edge's id
    is the origin of its rows. A file written on another machine is not this node's:
    a new identity is made for this machine instead.
    """
    from aiteam.sync.node import load_node_status, new_node_identity, node_file, write_node_identity

    role = NodeRole(args.role)
    if role is NodeRole.STANDALONE:
        print("standalone 不需要 node.json：删掉它即回到单机")
        return 2
    db = _db_path(args.db)
    if not db.is_file():
        print(f"数据库 {db} 不存在：先在本机装好 OS 并启动过一次")
        return 2
    target = node_file(db)
    existing = load_node_status(target) if target.exists() else None
    center_url = args.center_url or (DEFAULT_CENTER_URL if role is NodeRole.EDGE else "")
    plan: list[str] = []
    if existing is not None and existing.identity is not None and not existing.refusal:
        if existing.identity.role is not role:
            print(f"{target} 已声明本机为 {existing.identity.role}（{existing.identity.node_id}）。"
                  "换角色会让对端记下的身份失效：确要换，先删掉它再运行 enable")
            return 2
        identity = existing.identity.model_copy(update={"center_url": center_url or existing.identity.center_url})
        plan.append(f"沿用已有身份 {identity.node_id}（{role}）" + ("，更新 center_url" if identity != existing.identity
                                                                      else "，无需改动"))
    else:
        identity = new_node_identity(args.name, role, center_url=center_url)
        if existing is not None and existing.identity is not None:
            plan.append(f"{target} 里的身份 {existing.identity.node_id} 不属于本机（{existing.refusal}），"
                        "为本机另起新身份；若本机是中心，边缘需要运行 repin 改认新中心")
        plan.append(f"写 {target}：node_id={identity.node_id} role={role} center_url={center_url or '-'}")
    baseline = None
    if role is NodeRole.CENTER and (existing is None or existing.refusal or existing.identity is None):
        with _connect(db) as con:
            baseline = con.execute("SELECT coalesce(max(seq), 0) FROM sync_changes").fetchone()[0]
        plan.append(f"记下中心的基线：sync_state.baseline_seq = {baseline}（边缘从这里开始跟）")
    print("\n".join(plan))
    if not args.apply:
        print("（预演：加 --apply 执行；执行前先停掉本机 API，执行后再启动）")
        return 0
    write_node_identity(target, identity)
    if baseline is not None:
        _write_db(db, "UPDATE sync_state SET baseline_seq = ? WHERE id = 1", baseline)
    print(f"已写入 {target}。重启本机 API 后以 {role} 运行。")
    return 0


def repin(args: argparse.Namespace) -> int:
    """On an edge: forget the pinned center, so the next handshake accepts the one answering.

    For a center that was re-enabled with a new identity, or restored on purpose.
    ``--rebuild`` also replaces the whole replica with the center's on the next attach
    (the queue is kept and replayed): for a center restored from an old backup.
    """
    db = _db_path(args.db)
    with _connect(db) as con:
        peer = con.execute("SELECT center_node_id, cursor FROM sync_peers WHERE peer = 'center'").fetchone()
    if peer is None:
        print("本机没有边缘的同步记录：repin 只用于边缘节点")
        return 2
    print(f"忘掉已钉住的中心 {peer['center_node_id'] or '-'}，下次握手认下应答的中心"
          + ("；并在下次连上时整份重建本机副本（积压的操作保留并照常重放）" if args.rebuild else ""))
    if not args.apply:
        print("（预演：加 --apply 执行；API 不必停）")
        return 0
    cursor = "-1" if args.rebuild else "cursor"
    _write_db(db, f"UPDATE sync_peers SET center_node_id = '', handshake_error = '', link = 'detached', "
                  f"successes = 0, cursor = {cursor}, cursor_hlc = '' WHERE peer = 'center'")
    print("已完成")
    return 0


# -- status and orphans --------------------------------------------------------

def status(args: argparse.Namespace) -> int:
    from aiteam.sync import ledger
    from aiteam.sync.node import load_node_status, node_file
    from aiteam.sync.worker import open_engine

    db = _db_path(args.db)
    node = load_node_status(node_file(db))
    report: dict[str, object] = {"role": node.role.value, "node_id": node.node_id, "refusal": node.refusal}
    engine = open_engine(str(db))
    try:
        with engine.connect() as conn:
            report["link"] = ledger.read_peer(conn)._asdict()
            report["queue"] = ledger.backlog(conn)._asdict()
            report["tentative_rows"] = conn.exec_driver_sql("SELECT count(*) FROM sync_tentative").scalar_one()
            report["pending_conflicts"] = conn.exec_driver_sql(
                "SELECT count(*) FROM sync_conflicts WHERE status = 'pending'").scalar_one()
    finally:
        engine.dispose()
    with _connect(db) as con:
        report["orphan_writes"] = _orphans(con) if node.role is NodeRole.EDGE else []
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if node.refusal or report["orphan_writes"] else 0


def _orphans(con: sqlite3.Connection) -> list[dict[str, object]]:
    rows = con.execute(
        "SELECT seq, table_name, pk, kind, hlc FROM sync_changes "
        f"WHERE table_name IN ({SHARED_TABLES_SQL}) ORDER BY seq")
    return [dict(row) for row in rows]


def orphans(args: argparse.Namespace) -> int:
    """List shared rows an edge wrote outside any operation; ``--revert`` takes the center's."""
    import httpx

    from aiteam.sync.changelog import raise_floor, suppressed
    from aiteam.sync.node import load_node_status, node_file
    from aiteam.sync.replica import Replica
    from aiteam.sync.worker import open_engine
    from aiteam.types import SyncChange

    db = _db_path(args.db)
    with _connect(db) as con:
        found = _orphans(con)
    print(json.dumps(found, ensure_ascii=False, indent=2))
    if not found or not args.revert:
        return 1 if found else 0
    node = load_node_status(node_file(db))
    if node.role is not NodeRole.EDGE or node.identity is None:
        print("只有边缘节点有孤儿写入可还原")
        return 2
    keys = sorted({(row["table_name"], row["pk"]) for row in found})
    print(f"将以中心 {node.identity.center_url} 的版本覆盖这 {len(keys)} 行（中心没有的删除）")
    if not args.apply:
        print("（预演：加 --apply 执行）")
        return 0
    with httpx.Client(base_url=node.identity.center_url, trust_env=False, timeout=30) as client:
        response = client.post("/api/sync/rows", json={"keys": [list(key) for key in keys]})
        response.raise_for_status()
        rows = {(c.table_name, c.pk): c.row for c in
                (SyncChange.model_validate(item) for item in response.json()["rows"])}
    engine = open_engine(str(db))
    try:
        with engine.begin() as conn, suppressed(conn):
            replica = Replica(conn)
            for table, pk in keys:
                replica.write(table, pk, rows.get((table, pk)))
            raise_floor(conn)
            conn.exec_driver_sql(f"DELETE FROM sync_changes WHERE table_name IN ({SHARED_TABLES_SQL})")
    finally:
        engine.dispose()
    print("已还原")
    return 0


# -- baseline ------------------------------------------------------------------

def check_baseline(args: argparse.Namespace) -> int:
    """Two databases fit to start a sync from: same clock convention and schema."""
    problems = []
    facts = []
    for path in (Path(args.db_a), Path(args.db_b)):
        with _connect(path) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            counts = {table: con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                      for table in sorted(SHARED_TABLES)}
        facts.append((path, version, counts))
    (a, version_a, counts_a), (b, version_b, counts_b) = facts
    if version_a != version_b:
        problems.append(f"PRAGMA user_version 不同（{a}: {version_a}，{b}: {version_b}）：时钟制式不一致，中止")
    differing = {t: (counts_a[t], counts_b[t]) for t in counts_a if counts_a[t] != counts_b[t]}
    print(json.dumps({"schema_version": schema_version(), "user_version": [version_a, version_b],
                      "shared_rows_differ": differing}, ensure_ascii=False, indent=2))
    for problem in problems:
        print(problem)
    return 1 if problems else 0


# -- comparing two replicas --------------------------------------------------------

def shared_face_differences(db_a: Path, db_b: Path) -> list[str]:
    """Rows of the shared face that differ between two databases, compared by sync key.

    Columns that only number rows on one node (knowledge_links.id) and those that
    record a node's own use (LOCAL_COLUMNS) are left out: they never travel.
    """
    from sqlalchemy import create_engine

    from aiteam.sync.replica import Replica
    from aiteam.sync.tables import LOCAL_COLUMNS

    def dump(path: Path) -> dict[str, dict[str, dict]]:
        engine = create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")
        try:
            with engine.connect() as conn:
                replica, tables = Replica(conn), {}
                for table in sorted(SHARED_TABLES):
                    shape = replica.shape(table)
                    skip = shape.node_local | LOCAL_COLUMNS.get(table, frozenset())
                    rows = conn.exec_driver_sql(f'SELECT * FROM "{table}"').mappings().all()
                    tables[table] = {replica.key_of(table, row): {k: v for k, v in row.items() if k not in skip}
                                     for row in rows}
                return tables
        finally:
            engine.dispose()

    left, right = dump(db_a), dump(db_b)
    differences = []
    for table in sorted(SHARED_TABLES):
        a, b = left[table], right[table]
        differences += [f"{table} {pk}: 只在 {db_a.name}" for pk in sorted(a.keys() - b.keys())]
        differences += [f"{table} {pk}: 只在 {db_b.name}" for pk in sorted(b.keys() - a.keys())]
        for pk in sorted(a.keys() & b.keys()):
            columns = sorted(c for c in a[pk] if a[pk][c] != b[pk].get(c))
            if columns:
                differences.append(f"{table} {pk}: 列 {columns} 不同")
    return differences


def compare(args: argparse.Namespace) -> int:
    differences = shared_face_differences(Path(args.db_a), Path(args.db_b))
    for line in differences:
        print(line)
    print(f"共享面逐行比对：{len(differences)} 处不同")
    return 1 if differences else 0


# -- updating the center ---------------------------------------------------------

def center_update_steps(repo: str, port: int = CENTER_API_PORT) -> list[str]:
    """The shell steps that update the center in place.

    Pull the code into the center's main checkout, install from that checkout, then
    stop the API: launchd brings the center's API back up with the new code (or the
    next MCP call starts it when the service is not installed), and the check
    waits until it answers as a center again.
    """
    quoted = shlex.quote(repo)
    health = f"http://127.0.0.1:{port}/api/sync/hello"
    return [
        f"cd {quoted} && test -z \"$(git status --porcelain --untracked-files=no)\"",
        f"cd {quoted} && git pull --ff-only",
        f"cd {quoted} && python3 install.py --update",
        f"curl -fsS -X POST http://127.0.0.1:{port}/api/system/shutdown || true",
        f"for i in $(seq 1 60); do curl -fsS {health} && exit 0; sleep 1; done; exit 1",
    ]


def update_center(args: argparse.Namespace) -> int:
    steps = center_update_steps(args.repo, args.port)
    commands = [["ssh", args.host, step] for step in steps]
    for command in commands:
        print(" ".join(shlex.quote(part) for part in command))
    if not args.apply:
        print("（预演：加 --apply 依次执行；任何一步失败即停）")
        return 0
    for command in commands:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            print(f"失败（退出码 {completed.returncode}）：{command[-1]}")
            return completed.returncode
    print("中心已更新并重新应答")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m aiteam.sync", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    sub = commands.add_parser("enable", help="write node.json for this machine")
    sub.add_argument("--role", required=True, choices=[NodeRole.CENTER.value, NodeRole.EDGE.value])
    sub.add_argument("--name", required=True, help="short machine name, e.g. air or mini")
    sub.add_argument("--center-url", default="", help=f"edge only (default {DEFAULT_CENTER_URL})")
    sub.add_argument("--db")
    sub.add_argument("--apply", action="store_true")
    sub.set_defaults(run=enable)

    sub = commands.add_parser("repin", help="edge: accept the center answering now; --rebuild the replica too")
    sub.add_argument("--db")
    sub.add_argument("--rebuild", action="store_true")
    sub.add_argument("--apply", action="store_true")
    sub.set_defaults(run=repin)

    sub = commands.add_parser("status", help="this node's sync state, read from its database")
    sub.add_argument("--db")
    sub.set_defaults(run=status)

    sub = commands.add_parser("orphans", help="shared rows written outside any operation")
    sub.add_argument("--db")
    sub.add_argument("--revert", action="store_true", help="overwrite them with the center's rows")
    sub.add_argument("--apply", action="store_true")
    sub.set_defaults(run=orphans)

    sub = commands.add_parser("check-baseline", help="compare two databases before a cut-over")
    sub.add_argument("db_a")
    sub.add_argument("db_b")
    sub.set_defaults(run=check_baseline)

    sub = commands.add_parser("compare", help="compare the shared face of two databases row by row")
    sub.add_argument("db_a")
    sub.add_argument("db_b")
    sub.set_defaults(run=compare)

    sub = commands.add_parser("update-center", help="update the center's code and restart its API, over SSH")
    sub.add_argument("--host", required=True, help="ssh destination of the center, e.g. mini")
    sub.add_argument("--repo", required=True, help="the center's main checkout")
    sub.add_argument("--port", type=int, default=CENTER_API_PORT)
    sub.add_argument("--apply", action="store_true")
    sub.set_defaults(run=update_center)

    args = parser.parse_args(argv)
    return int(args.run(args))
