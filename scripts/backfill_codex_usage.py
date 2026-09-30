#!/usr/bin/env python3
"""Codex agents 用量回采。默认物理只读；--apply 必须消费已预览计划。

用法：python3 scripts/backfill_codex_usage.py --db /path/aiteam.db --plan-out /tmp/codex-plan.json
用户核对后：python3 scripts/backfill_codex_usage.py --db /path/aiteam.db --apply
    --plan /tmp/codex-plan.json --backup /path/new-backup.db --journal /path/new-journal.json
不会写 workflow_agents、任务、方向层或任何宿主配置；不覆盖已有测量。
若非空 transcript_path 已失效、同一线程原件迁至 archived_sessions，计划可覆写旧路径；
journal 的 before-image 和数据库备份保留旧值，apply 仍在事务内逐字段比较防止并发覆盖。
规格与实际生命周期边界见 docs/codex-usage-parser-design.md。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aiteam.clock import naive_utc_now, parse_utc  # noqa: E402
from aiteam.services.codex_rollout_usage import (  # noqa: E402
    CodexRolloutUsageError,
    parse_codex_rollout_usage,
    resolve_codex_home,
)
from aiteam.types import HarnessId, TokenSource  # noqa: E402

UTC_MARKER = 20260728
USAGE_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "reasoning_output_tokens",
    "tokens_measured_at",
    "tokens_source",
)
ALLOWED_COLUMNS = (*USAGE_COLUMNS, "transcript_path")
GUARD_COLUMNS = ("harness", "role", "session_id", "cc_tool_use_id", "transcript_path", *USAGE_COLUMNS)


class PlanConflictError(ValueError):
    pass


def open_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=on")
    return con


def validate_schema(con: sqlite3.Connection) -> None:
    if con.execute("pragma user_version").fetchone()[0] != UTC_MARKER:
        raise PlanConflictError("未知时钟制式；必须是 UTC user_version=20260728，不进行隐式换算")
    names = {r[1] for r in con.execute("pragma table_info(agents)")}
    if not set(("id", *GUARD_COLUMNS)).issubset(names):
        raise PlanConflictError("缺少当前 agents 用量 schema；本脚本不自动迁移")


def fingerprint(con: sqlite3.Connection) -> str:
    """Check untouched workflow token rows, without conflating aggregate sums."""
    h = hashlib.sha256()
    for row in con.execute("select id,tokens from workflow_agents order by id"):
        h.update(json.dumps(tuple(row), separators=(",", ":")).encode() + b"\n")
    return h.hexdigest()


def create_plan(db: Path, codex_home: Path) -> dict:
    with open_readonly(db) as con:
        validate_schema(con)
        rows = [
            dict(row)
            for row in con.execute(
                "select id," + ",".join(GUARD_COLUMNS) + " from agents where harness=? order by id",
                (HarnessId.CODEX.value,),
            )
        ]
        workflow_before = fingerprint(con)
    reasons = Counter()
    changes = []
    measured = naive_utc_now().isoformat(sep=" ", timespec="microseconds")
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        sid = row["session_id"] if row["role"] == "leader" else row["cc_tool_use_id"]
        if not sid:
            reasons["identity_missing"] += 1
            continue
        grouped.setdefault(sid, []).append(row)
    for sid, same_actor in grouped.items():
        # A rollout is a single owning ledger. Do not charge it to multiple DB rows.
        existing = [row for row in same_actor if row["tokens_measured_at"] is not None]
        if existing:
            reasons["already_measured"] += len(existing)
            reasons["duplicate_actor"] += len(same_actor) - len(existing)
            continue
        if len(same_actor) != 1:
            reasons["duplicate_actor"] += len(same_actor)
            continue  # no arbitrary choice by row creation time
        row = same_actor[0]
        try:
            usage = parse_codex_rollout_usage(row["transcript_path"], expected_session_id=sid, codex_home=codex_home)
        except CodexRolloutUsageError as exc:
            reasons[exc.code] += 1
            continue
        if usage is None:
            reasons["no_usage"] += 1
            continue
        after = {key: usage[key] for key in USAGE_COLUMNS if key in usage}
        after.update(
            tokens_measured_at=measured,
            tokens_source=TokenSource.CODEX_ROLLOUT.value,
            transcript_path=usage["transcript_path"],
        )
        changes.append(
            {
                "id": row["id"],
                "before": {key: row[key] for key in GUARD_COLUMNS},
                "after": after,
                "api_calls": usage["api_calls"],
            }
        )
        reasons["planned"] += 1
    return {
        "format": 1,
        "db": str(db.resolve()),
        "user_version": UTC_MARKER,
        "allowed_columns": list(ALLOWED_COLUMNS),
        "workflow_tokens_sha256": workflow_before,
        "counts": dict(reasons),
        "changes": changes,
    }


def _validate_plan(plan: dict, db: Path) -> None:
    if (
        plan.get("format") != 1
        or plan.get("user_version") != UTC_MARKER
        or plan.get("db") != str(db.resolve())
        or plan.get("allowed_columns") != list(ALLOWED_COLUMNS)
    ):
        raise PlanConflictError("计划版本、目标或时钟制式与当前脚本不一致")
    ids = set()
    for change in plan.get("changes", []):
        before, after = change.get("before", {}), change.get("after", {})
        if (
            change.get("id") in ids
            or set(before) != set(GUARD_COLUMNS)
            or set(after) != set(ALLOWED_COLUMNS)
            or before["harness"] != HarnessId.CODEX.value
            or before["tokens_measured_at"] is not None
            or after["tokens_source"] != TokenSource.CODEX_ROLLOUT.value
            or not isinstance(after["tokens_measured_at"], str)
        ):
            raise PlanConflictError("计划越界、重复或试图覆盖已有测量")
        try:
            # sqlite storage uses naive UTC, never an arbitrary string or local offset.
            at = parse_utc(after["tokens_measured_at"])
            canonical = at.isoformat(sep=" ", timespec="microseconds").removesuffix("+00:00") if at else None
            if canonical != after["tokens_measured_at"]:
                raise ValueError("noncanonical timestamp")
        except (ValueError, TypeError):
            raise PlanConflictError("计划测量时刻必须是有效的微秒级 naive UTC") from None
        if not isinstance(after["transcript_path"], str) or not Path(after["transcript_path"]).is_absolute():
            raise PlanConflictError("计划 transcript_path 必须是已验证的绝对路径")
        for key in USAGE_COLUMNS[:5]:
            val = after[key]
            if (val is None and key not in ("cache_creation_tokens", "reasoning_output_tokens")) or (
                val is not None and (type(val) is not int or val < 0)
            ):
                raise PlanConflictError("计划中 token 数值无效")
        if after["reasoning_output_tokens"] is not None and after["reasoning_output_tokens"] > after["output_tokens"]:
            raise PlanConflictError("reasoning 必须是 output 子集")
        ids.add(change["id"])


def apply_plan(db: Path, plan: dict, *, backup: Path, journal: Path) -> dict:
    """Explicit user CLI apply; compare every precondition inside BEGIN IMMEDIATE."""
    _validate_plan(plan, db)
    if backup.exists() or journal.exists() or backup.resolve() == db.resolve() or journal.resolve() == db.resolve():
        raise PlanConflictError("备份与账本必须是新文件，不能覆盖已有证据")
    # Reserve paths exclusively before any database mutation.
    backup_fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(backup_fd)
    journal_fd = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(journal_fd)
    with open_readonly(db) as source, sqlite3.connect(backup) as target:
        validate_schema(source)
        source.backup(target)
    record = {"plan": plan, "status": "pending", "backup": str(backup), "updated": 0}
    journal.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    con = sqlite3.connect(db, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("begin immediate")
        validate_schema(con)
        baseline = fingerprint(con)
        # Concurrent workflow activity since preview is permitted; what matters is
        # no workflow mutation INSIDE this transaction. Agent before-images are strict.
        for change in plan["changes"]:
            row = con.execute(
                "select " + ",".join(GUARD_COLUMNS) + " from agents where id=?", (change["id"],)
            ).fetchone()
            if row is None or dict(row) != change["before"]:
                raise PlanConflictError("计划生成后 agent 已变化；整事务回滚，请重做 dry-run")
            after = change["after"]
            statement = "update agents set " + ",".join(key + "=?" for key in ALLOWED_COLUMNS) + " where id=?"
            con.execute(statement, (*[after[key] for key in ALLOWED_COLUMNS], change["id"]))
        if fingerprint(con) != baseline:
            raise PlanConflictError("workflow_agents.tokens 越界变化；整事务回滚")
        con.execute("commit")
        record.update(status="applied", updated=len(plan["changes"]), workflow_tokens_sha256=baseline)
    except Exception:
        if con.in_transaction:
            con.execute("rollback")
        record.update(status="rejected")
        raise
    finally:
        con.close()
        journal.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    p.add_argument("--plan-out", type=Path)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--plan", type=Path)
    p.add_argument("--backup", type=Path)
    p.add_argument("--journal", type=Path)
    args = p.parse_args()
    if args.apply:
        if not all((args.plan, args.backup, args.journal)):
            p.error("--apply 需要 --plan、--backup、--journal；先生成并核对 dry-run")
        result = apply_plan(args.db, json.loads(args.plan.read_text()), backup=args.backup, journal=args.journal)
        print(json.dumps({"status": result["status"], "updated": result["updated"]}))
    else:
        plan = create_plan(args.db, args.codex_home)
        if args.plan_out:
            with args.plan_out.open("x", encoding="utf-8") as out:
                json.dump(plan, out, ensure_ascii=False, indent=2)
        print(
            json.dumps(
                {"mode": "dry-run", "user_version": plan["user_version"], "counts": plan["counts"]}, ensure_ascii=False
            )
        )


if __name__ == "__main__":
    main()
