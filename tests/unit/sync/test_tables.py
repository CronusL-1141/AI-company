"""The ORM seen from sync: every table grouped, unsynced ones explained, synced ones keyed and marked."""

from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table

from aiteam.storage.models import Base
from aiteam.sync import tables


def test_the_real_models_are_consistent() -> None:
    assert tables.schema_problems(Base.metadata.tables) == []
    assert len(tables.SESSION_TABLES) + len(tables.SHARED_TABLES) == len(tables.SYNCED_TABLES) == 50


def _problems_for(*tables_: Table) -> list[str]:
    found = tables.schema_problems({t.name: t for t in tables_})
    # Only the tables built here: the rest of the registry is reported as absent.
    return [p for p in found if "ORM 里没有这张表" not in p]


def test_a_synced_model_without_the_mixin_is_reported() -> None:
    md = MetaData()
    assert _problems_for(Table("tasks", md, Column("id", String, primary_key=True))) == [
        "tasks: 同步表的 ORM 模型缺 origin_node 列（漏挂 OriginNodeMixin）"]


def test_a_synced_table_needs_a_key() -> None:
    md = MetaData()
    keyless = Table("teams", md, Column("name", String), Column("origin_node", String))
    assert _problems_for(keyless) == [
        "teams: 同步表既无主键也无 SYNC_KEYS，变更无从按行记录（补键，或归入不同步并写理由）"]
    links = Table("knowledge_links", md, Column("id", Integer, primary_key=True), Column("origin_node", String))
    assert _problems_for(links)[0].startswith("knowledge_links: SYNC_KEYS 里的列")


def test_unsynced_tables_carry_no_origin_and_give_a_reason(monkeypatch) -> None:
    md = MetaData()
    notices = Table("notices", md, Column("key", String, primary_key=True), Column("origin_node", String))
    unknown = Table("brand_new", md, Column("id", String, primary_key=True))
    monkeypatch.setitem(tables.LOCAL_TABLES, "notices", " ")
    assert _problems_for(notices, unknown) == [
        "brand_new: 未归入 session/shared/local/dead/sync 任何一组（新表必须先定同步面）",
        "notices: 不同步却没写理由",
        "notices: 不同步的表不该有 origin_node 列",
    ]


def test_a_column_numbered_per_database_names_where_the_origin_number_goes() -> None:
    md = MetaData()
    journal = Table("codex_usage_observations", md, Column("observation_id", String, primary_key=True),
                    Column("seq", Integer), Column("origin_node", String))
    assert _problems_for(journal) == [
        "codex_usage_observations: SESSION_RENUMBERED 的列 ['origin_seq'] 不在表上"
        "（按库编号的列上行到中心时另行编号，来源的编号要有处可放）"]
    assert set(tables.SESSION_RENUMBERED) <= tables.SESSION_TABLES


def test_the_bookkeeping_of_phase_two_is_grouped() -> None:
    assert {"sync_intake", "sync_guard"} <= tables.SYNC_BOOKKEEPING_TABLES
