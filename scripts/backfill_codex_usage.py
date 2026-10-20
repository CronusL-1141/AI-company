#!/usr/bin/env python3
"""Codex agents 用量回采。默认物理只读；--apply 必须消费已预览计划。

用法：python3 scripts/backfill_codex_usage.py --db /path/aiteam.db --plan-out /tmp/codex-plan.json
用户核对后：python3 scripts/backfill_codex_usage.py --db /path/aiteam.db --apply
    --plan /tmp/codex-plan.json --backup /path/new-backup.db --journal /path/new-journal.json
不会写 workflow_agents、任务、方向层或任何宿主配置；不覆盖已有测量。
若非空 transcript_path 已失效、同一线程原件迁至 archived_sessions，计划可覆写旧路径；
journal 的 before-image 和数据库备份保留旧值，apply 仍在事务内逐字段比较防止并发覆盖。
测到用量的行同时清掉 usage_unmeasurable（真实测量推翻旧结论）。
已结束的未测量行（与分类共用 usage_coverage.is_in_flight），解析器拒测时记
codex_parser_rejected:<原因码>，查无原件且无登记路径时记 codex_rollout_not_found；
找到原件但没有任何用量事件时记 no_usage_lines 并补实际路径。这些观察记录在 verdicts，
before-image 连同 status 与 last_active_at 一起比较；还在跑的行不下结论。
规格与实际生命周期边界见 docs/codex-usage-parser-design.md。

时钟守卫：库的 PRAGMA user_version 只接受 20260728（全库 UTC 标记）或 0（没有标记），
其余值一律拒绝，计划校验与 apply 事务内的复核同样如此。放行 0 的依据：本脚本只写
harness=codex 的行，写入的 tokens_measured_at 是 naive UTC 此刻（与活体采集同一口径），
判在飞读的 status 与 last_active_at 也只来自这些行；Codex 支持（v1.12.0 起才有 harness
列与 Codex 适配器）晚于 UTC 统一（v1.11.1），任何能写出 codex 行的版本都按 UTC 写，
所以待写行的时钟与全库标记无关。v1.15.2 之前新装的库与 UTC 宿主上的库都停在 0，按全库
标记放行会让它们永远跑不了回采。计划与 journal 如实记下实际的 user_version 和放行
理由（clock_basis），0 不会被写成 20260728，脚本也从不改库的 user_version。apply 时库的
标记必须与计划生成时相同。规则见 docs/utc-unification-design.md §6.7。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pydantic import TypeAdapter, ValidationError  # noqa: E402

from aiteam.clock import naive_utc_now, parse_utc, utc_now  # noqa: E402
from aiteam.services.codex_rollout_usage import (  # noqa: E402
    CodexRolloutUsageError,
    parse_codex_rollout_usage_observation,
    resolve_codex_home,
)
from aiteam.services.usage_coverage import is_in_flight  # noqa: E402
from aiteam.types import (  # noqa: E402
    CODEX_PARSER_REJECTED_PREFIX,
    HarnessId,
    TokenSource,
    UnattributedReason,
    UsageUnmeasurable,
)

UTC_MARKER = 20260728  # same value as aiteam.storage.connection.UTC_CLOCK_MARKER
# Accepted PRAGMA user_version values and why each lets this script write. 0 is accepted
# because the rows written here are harness=codex rows only, and every version able to
# write such a row writes UTC (Codex support arrived in v1.12.0, the UTC unification in
# v1.11.1), so the whole-database marker does not decide their clock. Any other value is
# refused. The plan and the journal record the actual value and this basis verbatim.
CLOCK_BASIS = {
    UTC_MARKER: "user_version=20260728：库带 UTC 标记，全库时间戳按 UTC 存",
    0: (
        "user_version=0：库没有时钟标记；本脚本只写 harness=codex 的行，Codex 支持（v1.12.0）"
        "晚于 UTC 统一（v1.11.1），能写出这些行的版本都按 UTC 写，待写行的时钟与全库标记无关"
    ),
}
PLAN_FORMAT = 4
USAGE_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "reasoning_output_tokens",
    "tokens_measured_at",
    "tokens_source",
)
ALLOWED_COLUMNS = (*USAGE_COLUMNS, "transcript_path", "usage_unmeasurable")
GUARD_COLUMNS = (
    "harness", "role", "session_id", "cc_tool_use_id", "transcript_path", "usage_unmeasurable", *USAGE_COLUMNS
)
# A terminal observation is only for a finished row, so its before-image also pins
# the liveness it was judged on: a row that became active since the plan is refused.
VERDICT_GUARD_COLUMNS = (*GUARD_COLUMNS, "status", "last_active_at")
NO_USAGE = UnattributedReason.NO_USAGE_LINES.value
ROLLOUT_NOT_FOUND = UnattributedReason.CODEX_ROLLOUT_NOT_FOUND.value
_VERDICT_TYPE = TypeAdapter(UsageUnmeasurable)


class PlanConflictError(ValueError):
    pass


def open_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=on")
    return con


def validate_schema(con: sqlite3.Connection) -> int:
    """Return the database's clock marker once it is one of CLOCK_BASIS and the schema fits."""
    marker = con.execute("pragma user_version").fetchone()[0]
    if marker not in CLOCK_BASIS:
        raise PlanConflictError(
            f"未知时钟制式 user_version={marker}；只接受 20260728（UTC 标记）或 0（无标记），不进行隐式换算"
        )
    names = {r[1] for r in con.execute("pragma table_info(agents)")}
    if not set(("id", "source", "created_at", *VERDICT_GUARD_COLUMNS)).issubset(names):
        raise PlanConflictError("缺少当前 agents 用量 schema；本脚本不自动迁移")
    return marker


def require_plan_clock(con: sqlite3.Connection, plan: dict) -> int:
    """The database must still carry the marker the plan was made against."""
    marker = validate_schema(con)
    if marker != plan["user_version"]:
        raise PlanConflictError(
            f"时钟标记已变：计划生成时 user_version={plan['user_version']}，现在是 {marker}；请重做 dry-run"
        )
    return marker


def fingerprint(con: sqlite3.Connection) -> str:
    """Check untouched workflow token rows, without conflating aggregate sums."""
    h = hashlib.sha256()
    for row in con.execute("select id,tokens from workflow_agents order by id"):
        h.update(json.dumps(tuple(row), separators=(",", ":")).encode() + b"\n")
    return h.hexdigest()


def create_plan(db: Path, codex_home: Path) -> dict:
    with open_readonly(db) as con:
        marker = validate_schema(con)
        rows = [
            dict(row)
            for row in con.execute(
                "select id,source,created_at," + ",".join(VERDICT_GUARD_COLUMNS)
                + " from agents where harness=? order by id",
                (HarnessId.CODEX.value,),
            )
        ]
        workflow_before = fingerprint(con)
    reasons = Counter()
    changes = []
    verdicts = []
    verdict_candidates = 0
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
            usage, found_path = parse_codex_rollout_usage_observation(
                row["transcript_path"], expected_session_id=sid, codex_home=codex_home
            )
        except CodexRolloutUsageError as exc:
            reasons[exc.code] += 1
            verdict_candidates += _append_verdict(
                verdicts, row, CODEX_PARSER_REJECTED_PREFIX + exc.code,
                evidence=f"parser refused thread {sid}: {exc.code}",
            )
            continue
        if usage is None:
            # Not finding a rollout proves nothing about usage; only a found rollout
            # without any usage event supports the "no usage" verdict.
            if found_path is None:
                reasons["rollout_not_found"] += 1
                if not row["transcript_path"]:
                    verdict_candidates += _append_verdict(
                        verdicts, row, ROLLOUT_NOT_FOUND,
                        evidence=f"no rollout found for thread {sid}; no registered transcript path",
                    )
                continue
            reasons["no_usage"] += 1
            verdict_candidates += _append_verdict(
                verdicts, row, NO_USAGE, transcript_path=found_path,
                evidence=f"rollout of thread {sid} has no usage event: {found_path}",
            )
            continue
        after = {key: usage[key] for key in USAGE_COLUMNS if key in usage}
        after.update(
            tokens_measured_at=measured,
            tokens_source=TokenSource.CODEX_ROLLOUT.value,
            transcript_path=usage["transcript_path"],
            usage_unmeasurable=None,
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
    if verdict_candidates:
        reasons["verdict_planned"] = len(verdicts)
        reasons["verdict_in_flight"] = verdict_candidates - len(verdicts)
    return {
        "format": PLAN_FORMAT,
        "db": str(db.resolve()),
        "user_version": marker,
        "clock_basis": CLOCK_BASIS[marker],
        "allowed_columns": list(ALLOWED_COLUMNS),
        "workflow_tokens_sha256": workflow_before,
        "counts": dict(reasons),
        "changes": changes,
        "verdicts": verdicts,
    }


def _append_verdict(
    verdicts: list[dict], row: dict, reason: str, *, evidence: str, transcript_path: str | None = None,
) -> int:
    """Freeze an observation only for ended rows, with a guarded before-image.

    New observations can replace a stale refusal or missing-source observation;
    stronger pre-existing conclusions remain intact. Repeating an observation
    writes nothing. Real measurements still clear any earlier conclusion.
    """
    reason = str(_VERDICT_TYPE.validate_python(reason))
    if not _revisable_verdict(row["usage_unmeasurable"], reason):
        return 0
    after = {"usage_unmeasurable": reason}
    if transcript_path is not None:
        after["transcript_path"] = transcript_path
    if all(row[key] == value for key, value in after.items()):
        return 0
    if not is_in_flight(row["status"], parse_utc(row["last_active_at"]), utc_now()):
        verdicts.append({
            "id": row["id"],
            "before": {key: row[key] for key in VERDICT_GUARD_COLUMNS},
            "after": after,
            "evidence": evidence,
        })
    return 1


def _revisable_verdict(existing: str | None, reason: str) -> bool:
    return existing in (None, reason, ROLLOUT_NOT_FOUND) or (
        isinstance(existing, str) and existing.startswith(CODEX_PARSER_REJECTED_PREFIX)
    )


def _validate_plan(plan: dict, db: Path) -> None:
    if (
        plan.get("format") != PLAN_FORMAT
        or type(plan.get("user_version")) is not int
        or plan["user_version"] not in CLOCK_BASIS
        or plan.get("clock_basis") != CLOCK_BASIS[plan["user_version"]]
        or plan.get("db") != str(db.resolve())
        or plan.get("allowed_columns") != list(ALLOWED_COLUMNS)
        or not isinstance(plan.get("verdicts"), list)
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
            or after["usage_unmeasurable"] is not None
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
    now = utc_now()
    for verdict in plan["verdicts"]:
        before = verdict.get("before", {})
        after = verdict.get("after", {})
        try:
            reason = str(_VERDICT_TYPE.validate_python(after.get("usage_unmeasurable")))
        except ValidationError:
            raise PlanConflictError("结论原因码无效") from None
        allowed_reason = reason in (NO_USAGE, ROLLOUT_NOT_FOUND) or reason.startswith(CODEX_PARSER_REJECTED_PREFIX)
        path = after.get("transcript_path")
        valid_after = (
            set(after) == {"usage_unmeasurable", "transcript_path"}
            and isinstance(path, str) and Path(path).is_absolute()
        ) if reason == NO_USAGE else set(after) == {"usage_unmeasurable"}
        if (
            verdict.get("id") in ids
            or set(before) != set(VERDICT_GUARD_COLUMNS)
            or not allowed_reason
            or not valid_after
            or before["harness"] != HarnessId.CODEX.value
            or before["tokens_measured_at"] is not None
            or not _revisable_verdict(before["usage_unmeasurable"], reason)
            or (reason == ROLLOUT_NOT_FOUND and before["transcript_path"])
        ):
            raise PlanConflictError("结论计划越界、重复或试图覆盖已有测量与结论")
        if is_in_flight(before["status"], parse_utc(before["last_active_at"]), now):
            raise PlanConflictError("还在跑的行不下终态结论")
        ids.add(verdict["id"])


def _take_backup(db: Path, plan: dict, backup: Path) -> int:
    """Copy the database into the reserved backup file; return the marker it carried."""
    with closing(open_readonly(db)) as source, closing(sqlite3.connect(backup)) as target:
        marker = require_plan_clock(source, plan)
        source.backup(target)
    return marker


def apply_plan(db: Path, plan: dict, *, backup: Path, journal: Path) -> dict:
    """Explicit user CLI apply; compare every precondition inside BEGIN IMMEDIATE."""
    _validate_plan(plan, db)
    if backup.exists() or journal.exists() or backup.resolve() == db.resolve() or journal.resolve() == db.resolve():
        raise PlanConflictError("备份与账本必须是新文件，不能覆盖已有证据")
    with closing(open_readonly(db)) as con:
        require_plan_clock(con, plan)  # refuse before any evidence file is reserved
    # Reserve paths exclusively before any database mutation.
    backup_fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(backup_fd)
    journal_fd = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(journal_fd)
    marker = _take_backup(db, plan, backup)
    # The actual user_version and the basis it was accepted on, never a substituted 20260728.
    record = {
        "plan": plan, "status": "pending", "backup": str(backup), "updated": 0, "marked": 0,
        "user_version": marker, "clock_basis": CLOCK_BASIS[marker],
    }
    journal.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    con = sqlite3.connect(db, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("begin immediate")
        # The marker may have changed since the backup; only this check holds the write lock.
        require_plan_clock(con, plan)
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
        for verdict in plan["verdicts"]:
            row = con.execute(
                "select " + ",".join(VERDICT_GUARD_COLUMNS) + " from agents where id=?", (verdict["id"],)
            ).fetchone()
            if row is None or dict(row) != verdict["before"]:
                raise PlanConflictError("计划生成后 agent 已变化；整事务回滚，请重做 dry-run")
            after = verdict["after"]
            written = con.execute(
                "update agents set " + ",".join(key + "=?" for key in after) + " where id=?",
                (*after.values(), verdict["id"]),
            ).rowcount
            if written != 1:
                raise PlanConflictError("结论写入行数不符；整事务回滚")
        if fingerprint(con) != baseline:
            raise PlanConflictError("workflow_agents.tokens 越界变化；整事务回滚")
        con.execute("commit")
        record.update(
            status="applied", updated=len(plan["changes"]), marked=len(plan["verdicts"]),
            workflow_tokens_sha256=baseline,
        )
    except Exception:
        if con.in_transaction:
            con.execute("rollback")
        record.update(status="rejected")
        raise
    finally:
        con.close()
        journal.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main() -> int:
    try:
        return _run_cli()
    except PlanConflictError as exc:
        # Every refusal happens before the write or rolls the write back.
        print(f"❌ 回采拒绝执行：{exc}（库未改动）", file=sys.stderr)
        return 3


def _run_cli() -> int:
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
        print(
            json.dumps(
                {key: result[key] for key in ("status", "updated", "marked", "user_version")}, ensure_ascii=False
            )
        )
    else:
        plan = create_plan(args.db, args.codex_home)
        if args.plan_out:
            with args.plan_out.open("x", encoding="utf-8") as out:
                json.dump(plan, out, ensure_ascii=False, indent=2)
        print(
            json.dumps(
                {"mode": "dry-run"} | {key: plan[key] for key in ("user_version", "clock_basis", "counts")},
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
