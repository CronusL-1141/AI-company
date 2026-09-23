"""migrate_to_project_dbs.py 的手抄 DDL 不得与 ORM 的列默认值分叉。

这份脚本为了在同步进程里建库，把 models.py 的表结构手抄成了裸 SQLite DDL。手抄件
没有机检就会漂：agents.model 曾写着 ``DEFAULT 'claude-opus-4-6'``，而 ORM 是空串
——项目的刻意决策是「模型默认值留空，由观测回填」，写死型号的默认值必然过时，还会
让迁移出来的旧行冒充观测值。这里把 DDL 真正建出来，逐列比对字面量默认值。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from aiteam.storage.models import Base

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "migrate_to_project_dbs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_to_project_dbs_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ddl_defaults() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        for stmt in _load_script().SCHEMA_DDL:
            conn.execute(stmt)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        out: dict[tuple[str, str], str] = {}
        for table in tables:
            for _cid, name, _typ, _notnull, dflt, _pk in conn.execute(
                f"PRAGMA table_info('{table}')"
            ):
                if dflt is not None:
                    out[(table, name)] = dflt
        return out
    finally:
        conn.close()


def _normalise_orm_default(value: object) -> str | None:
    """ORM 标量默认值 -> SQLite DDL 字面量形态；非标量（callable/JSON）返回 None。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return "'" + enum_value + "'"
    return None


def test_agents_model_default_is_empty():
    """模型默认值留空：DDL 不得写死任何型号。"""
    assert _ddl_defaults()[("agents", "model")] == "''"


def test_ddl_scalar_defaults_match_orm():
    """每个两边都有标量默认值的列，DDL 字面量必须与 ORM 一致。"""
    drift: list[str] = []
    for (table, column), ddl_default in _ddl_defaults().items():
        orm_table = Base.metadata.tables.get(table)
        if orm_table is None or column not in orm_table.c:
            continue
        default = orm_table.c[column].default
        if default is None or callable(default.arg):
            continue
        expected = _normalise_orm_default(default.arg)
        if expected is None:
            continue
        if ddl_default != expected:
            drift.append(f"{table}.{column}: DDL {ddl_default} != ORM {expected}")
    assert not drift, "手抄 DDL 默认值与 ORM 分叉：" + "; ".join(drift)
