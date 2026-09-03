#!/usr/bin/env python3
"""Recompute the Codex golden values (G1-G15) from the local Codex evidence.

Reads (never writes) a Codex home, the probe evidence directory and the AI Team
OS event database, recomputes every number the Codex harness test suite asserts
on, and writes tests/fixtures/codex/golden.json.

Only structural values reach the output: line numbers, row counts, token
counters, thread ids and rollout basenames. No path, no model slug, no
conversation text. Running twice over the same inputs produces byte-identical
output (no timestamp is written).

Usage:
    python3 scripts/compute_codex_golden.py --evidence <dir> [--codex-home ...]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------- constants

# Session ids the golden set is anchored on (public UUIDv7 values).
G1_IMPORT_RESUMED = "019f8d58-610c"
G2_IMPORT_NEVER_RESUMED = "019f8d58-606e"
G3_FORK_REPLAY = "019f8d92-93fd"
G4_SELF_FORK_SESSION = "019f8a5c-8a8b"
G5_CTX_SENTINEL = "019f8d1a-c148"
G6_SUBAGENT_ID_TRAP = "019f8a63-ef67"
G7_MCP_CALL_SHAPE = "019f8d95-d818"
G8_SYSTEM_SESSION = "019f8d5f"
G9_UNPERSISTED_SESSION = "019f99a6"
G12_RUN8_COMPACT = "01a061b0-1d38"

IMPORT_TURN_PREFIX = "external-import-turn-"
IMPORT_MARKER = "<EXTERNAL SESSION IMPORTED>"

# The five counters that are all zero on a phantom total_token_usage.
USAGE_LAYERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")


# --------------------------------------------------------------- helpers


def read_rows(path: Path) -> list[dict]:
    """Parse a rollout / capture JSONL file into a list of objects."""
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def payload(obj: dict) -> dict:
    pl = obj.get("payload")
    return pl if isinstance(pl, dict) else {}


def row_label(obj: dict) -> tuple[str, str | None]:
    return obj.get("type"), payload(obj).get("type")


def token_info(obj: dict) -> dict | None:
    """info block of an event_msg/token_count row, or None."""
    if row_label(obj) != ("event_msg", "token_count"):
        return None
    info = payload(obj).get("info")
    return info if isinstance(info, dict) else {}


def total_usage(info: dict) -> dict:
    tu = info.get("total_token_usage")
    return tu if isinstance(tu, dict) else {}


def all_zero(usage: dict) -> bool:
    return bool(usage) and all(usage.get(k) == 0 for k in USAGE_LAYERS)


def is_phantom(info: dict) -> bool:
    """Phantom row: every usage layer is zero yet total_tokens is positive."""
    tu = total_usage(info)
    return all_zero(tu) and (tu.get("total_tokens") or 0) > 0


def is_real(info: dict) -> bool:
    """Real row: at least one of input/output tokens was actually spent."""
    tu = total_usage(info)
    return bool(tu) and ((tu.get("input_tokens") or 0) + (tu.get("output_tokens") or 0)) > 0


def triple(usage: dict) -> list[int]:
    return [usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens")]


def import_turn_lines(rows: list[dict]) -> list[int]:
    """1-based line numbers whose payload.turn_id is an external-import turn."""
    out = []
    for i, obj in enumerate(rows, 1):
        tid = payload(obj).get("turn_id")
        if isinstance(tid, str) and tid.startswith(IMPORT_TURN_PREFIX):
            out.append(i)
    return out


def import_turn_ids(rows: list[dict]) -> set[str]:
    out = set()
    for obj in rows:
        tid = payload(obj).get("turn_id")
        if isinstance(tid, str) and tid.startswith(IMPORT_TURN_PREFIX):
            out.add(tid)
    return out


def import_baseline(rows: list[dict]) -> tuple[int | None, int | None]:
    """(line, total_tokens) of the last phantom token_count inside the import span."""
    lines = import_turn_lines(rows)
    if not lines:
        return None, None
    found = (None, None)
    for i, obj in enumerate(rows[:lines[-1]], 1):
        info = token_info(obj)
        if info is not None and is_phantom(info):
            found = (i, total_usage(info).get("total_tokens"))
    return found


def marker_line(rows: list[dict]) -> int | None:
    for i, obj in enumerate(rows, 1):
        if IMPORT_MARKER in json.dumps(obj, ensure_ascii=False):
            return i
    return None


def first_session_meta(rows: list[dict]) -> dict:
    """First session_meta payload. Forked rollouts re-emit the parent's meta on
    line 2, so only the first one describes the file itself."""
    for obj in rows:
        if obj.get("type") == "session_meta":
            return payload(obj)
    return {}


def native_rollouts(codex_home: Path) -> list[Path]:
    return sorted(
        [p for sub in ("sessions", "archived_sessions") for p in (codex_home / sub).rglob("*.jsonl")],
        key=lambda p: str(p.relative_to(codex_home)),
    )


def pick(files: list[Path], marker: str) -> Path:
    hits = [p for p in files if marker in p.name]
    if len(hits) != 1:
        raise RuntimeError(f"expected exactly one rollout matching {marker}, got {len(hits)}")
    return hits[0]


def fixture_paths(fixtures: Path, marker: str) -> list[str]:
    if not fixtures.exists():
        return []
    return sorted(
        str(p.relative_to(fixtures))
        for p in fixtures.rglob("*")
        if p.is_file() and marker in p.name
    )


def open_ro(db: Path, tmp: Path) -> sqlite3.Connection:
    """Copy a possibly-live sqlite file (with side files) and open it read-only."""
    last: Exception | None = None
    for attempt in range(4):
        work = tmp / f"{db.name}.{attempt}"
        work.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, work)
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.copy2(side, work.with_name(work.name + suffix))
        try:
            con = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
            con.execute("pragma quick_check(1)").fetchone()
            return con
        except sqlite3.DatabaseError as exc:  # torn copy of a live database
            last = exc
    raise RuntimeError(f"could not read a consistent copy of {db.name}: {last}")


def os_event_counts(con: sqlite3.Connection, prefix: str) -> dict:
    """Event counts for one session id prefix, keyed on the session_id field so
    that unrelated rows merely mentioning the id are not swept in."""
    rows = con.execute(
        "select type, count(*) from events "
        "where json_extract(data, '$.session_id') like ? group by type order by type",
        (prefix + "%",),
    ).fetchall()
    return {t: n for t, n in rows}


def os_event_window(con: sqlite3.Connection, prefix: str) -> tuple[int, str | None, str | None]:
    return con.execute(
        "select count(*), min(timestamp), max(timestamp) from events "
        "where json_extract(data, '$.session_id') like ?",
        (prefix + "%",),
    ).fetchone()


# --------------------------------------------------------------- items


def item(name, sources, fixture_files, method, values, fixture_values=None) -> dict:
    out = {
        "name": name,
        "sources": sources,
        "fixture_files": fixture_files,
        "method": method,
        "values": values,
        "fixture_verifiable": bool(fixture_values),
    }
    if fixture_values:
        out["fixture_values"] = fixture_values
    return out


def g1(path: Path, fixtures: Path, os_con: sqlite3.Connection | None) -> dict:
    rows = read_rows(path)
    lines = import_turn_lines(rows)
    base_line, base_total = import_baseline(rows)
    violations = 0
    checked = 0
    last = None
    for obj in rows:
        info = token_info(obj)
        if info is None or is_phantom(info):
            continue
        tu = total_usage(info)
        if not tu:
            continue
        checked += 1
        if tu["total_tokens"] - (tu["input_tokens"] + tu["output_tokens"]) != base_total:
            violations += 1
        if is_real(info):
            last = triple(tu)

    values = {
        "rollout_lines": len(rows),
        "import_segment_first_line": lines[0],
        "import_segment_last_line": lines[-1],
        "import_segment_lines": lines[-1] - lines[0] + 1,
        "import_turn_count": len(import_turn_ids(rows)),
        "import_marker_line": marker_line(rows),
        "import_baseline_line": base_line,
        "import_baseline_total": base_total,
        "identity_checked_rows": checked,
        "identity_violations": violations,
        "last_real_total_token_usage": last,
    }
    if os_con is not None:
        n, lo, hi = os_event_window(os_con, G1_IMPORT_RESUMED)
        real_start = rows[lines[-1]]["timestamp"]
        real_end = rows[-1]["timestamp"]

        def norm(ts: str) -> str:
            return ts.replace("T", " ").rstrip("Z")

        values["os_event_rows"] = n
        values["os_events_inside_post_import_span"] = bool(
            n and norm(real_start) <= (lo or "") and (hi or "") <= norm(real_end)
        )

    # Fixture slice: head60 + tail6, so the whole import span, the marker, the
    # baseline row and the last real token_count survive; the rows in between
    # do not, hence the identity check is fixture-scoped to what is present.
    fx = fixtures_rows(fixtures, G1_IMPORT_RESUMED)
    fixture_values = {}
    if fx:
        f_lines = import_turn_lines(fx)
        f_base_line, f_base_total = import_baseline(fx)
        f_viol = 0
        f_checked = 0
        f_last = None
        for obj in fx:
            info = token_info(obj)
            if info is None or is_phantom(info) or not total_usage(info):
                continue
            tu = total_usage(info)
            f_checked += 1
            if tu["total_tokens"] - (tu["input_tokens"] + tu["output_tokens"]) != f_base_total:
                f_viol += 1
            if is_real(info):
                f_last = triple(tu)
        fixture_values = {
            "import_segment_first_line": f_lines[0],
            "import_segment_last_line": f_lines[-1],
            "import_segment_lines": f_lines[-1] - f_lines[0] + 1,
            "import_turn_count": len(import_turn_ids(fx)),
            "import_marker_line": marker_line(fx),
            "import_baseline_line": f_base_line,
            "import_baseline_total": f_base_total,
            "identity_checked_rows": f_checked,
            "identity_violations": f_viol,
            "last_real_total_token_usage": f_last,
        }
    return item(
        "G1_import_then_resume",
        [path.name, "os-db"],
        fixture_paths(fixtures, G1_IMPORT_RESUMED),
        "取该 rollout：导入段=payload.turn_id 以 external-import-turn- 开头的首末行区间；"
        "导入基线=该区间内最后一条 total_token_usage 五层全零的 token_count 的 total_tokens；"
        "此后每条 total_token_usage 非幻影的 token_count 须满足 total-(input+output)==基线，违例计数应为 0；"
        "末条真值取最后一条 input+output>0 的 total_token_usage 三元组；"
        "OS 库按 json_extract(data,'$.session_id') 前缀取事件数与时间区间，须整体落在导入段之后的真实段内。"
        "夹具版为 head60+tail6 裁剪，故 identity_checked_rows 只覆盖夹具内留存的行。",
        values,
        fixture_values,
    )


def fixtures_rows(fixtures: Path, marker: str) -> list[dict]:
    hits = [p for p in fixtures.rglob("*.jsonl") if marker in p.name] if fixtures.exists() else []
    return read_rows(hits[0]) if len(hits) == 1 else []


def g2(path: Path, fixtures: Path, os_con: sqlite3.Connection | None) -> dict:
    rows = read_rows(path)
    lines = import_turn_lines(rows)
    base_line, base_total = import_baseline(rows)
    real_rows = sum(1 for obj in rows if (info := token_info(obj)) is not None and is_real(info))
    values = {
        "rollout_lines": len(rows),
        "import_segment_first_line": lines[0],
        "import_segment_last_line": lines[-1],
        "import_turn_rows": len(lines),
        "import_turn_count": len(import_turn_ids(rows)),
        "import_marker_line": marker_line(rows),
        "import_baseline_line": base_line,
        "import_baseline_total": base_total,
        "rows_after_import_segment": len(rows) - lines[-1],
        "real_token_count_rows": real_rows,
    }
    if os_con is not None:
        values["os_event_rows"] = os_event_window(os_con, G2_IMPORT_NEVER_RESUMED)[0]
    fx = fixtures_rows(fixtures, G2_IMPORT_NEVER_RESUMED)
    fixture_values = {}
    if fx:
        fixture_values = {
            "fixture_lines": len(fx),
            "import_turn_rows": len(import_turn_lines(fx)),
            "real_token_count_rows": sum(
                1 for obj in fx if (info := token_info(obj)) is not None and is_real(info)),
        }
    return item(
        "G2_import_never_resumed",
        [path.name, "os-db"],
        fixture_paths(fixtures, G2_IMPORT_NEVER_RESUMED),
        "同 G1 的导入段判据：本份导入段一直延伸到最后一行，导入段之后再无任何行，"
        "全份没有一条 input+output>0 的 token_count，OS 库该 session 事件数为 0——"
        "即导入后从未被续用，导入基线是全份唯一的 token 数。夹具版为 head40 裁剪，"
        "基线行不在其中，故夹具只核对前 40 行全部落在导入段内且无真值行。",
        values,
        fixture_values,
    )


def last_real_total(rows: list[dict]) -> int | None:
    out = None
    for obj in rows:
        info = token_info(obj)
        if info is not None and is_real(info):
            out = total_usage(info).get("total_tokens")
    return out


def g3(child: Path, parent: Path, fixtures: Path) -> dict:
    crows, prows = read_rows(child), read_rows(parent)
    cmeta = first_session_meta(crows)
    c_total, p_total = last_real_total(crows), last_real_total(prows)
    values = {
        "id": cmeta.get("id"),
        "session_id": cmeta.get("session_id"),
        "forked_from_id": cmeta.get("forked_from_id"),
        "parent_rollout": parent.name,
        "own_last_real_total": c_total,
        "parent_last_real_total": p_total,
        "totals_equal": c_total == p_total,
    }
    return item(
        "G3_fork_replay",
        [child.name, parent.name],
        fixture_paths(fixtures, G3_FORK_REPLAY) + fixture_paths(fixtures, "019f8b2f-1617"),
        "读首条 session_meta 的 forked_from_id（注意 fork 份第 2 行会再发一条父线程的 session_meta，"
        "只认第 1 条）；本份与 forked_from_id 指向的父份各取末条 input+output>0 的 total_tokens，"
        "两者相等即证明 fork 份是父线程的重放副本，按谱系去重时不得二次入账。",
        values,
        values,
    )


def g4(files: list[Path], fixtures: Path) -> dict:
    self_by_session, self_by_id, metas = [], [], {}
    for p in files:
        meta = first_session_meta(read_rows(p))
        metas[p.name] = meta
        fork = meta.get("forked_from_id")
        if fork and fork == meta.get("session_id"):
            self_by_session.append(meta.get("id"))
        if fork and fork == meta.get("id"):
            self_by_id.append(meta.get("id"))
    target = pick(files, "019f8b21-a6ae")
    tm = metas[target.name]
    values = {
        "rollouts_scanned": len(files),
        "rollout": target.name,
        "id": tm.get("id"),
        "session_id": tm.get("session_id"),
        "forked_from_id": tm.get("forked_from_id"),
        "forked_from_equals_session_id": tm.get("forked_from_id") == tm.get("session_id"),
        "forked_from_equals_id": tm.get("forked_from_id") == tm.get("id"),
        "self_fork_by_session_id_count": len(self_by_session),
        "self_fork_by_session_id_ids": sorted(self_by_session),
        "self_fork_by_id_count": len(self_by_id),
    }
    fx_files = [p for p in fixtures.rglob("*.jsonl") if "/rollouts/native/" in p.as_posix()]
    fixture_values = {}
    if fx_files:
        fx_self = []
        for p in sorted(fx_files, key=lambda q: q.name):
            meta = first_session_meta(read_rows(p))
            fork = meta.get("forked_from_id")
            if fork and fork == meta.get("session_id"):
                fx_self.append(meta.get("id"))
        fixture_values = {
            "id": tm.get("id"),
            "session_id": tm.get("session_id"),
            "forked_from_id": tm.get("forked_from_id"),
            "forked_from_equals_session_id": True,
            "forked_from_equals_id": False,
            "self_fork_by_session_id_ids": sorted(fx_self),
        }
    return item(
        "G4_self_fork",
        [target.name] + ["<native rollouts>"],
        fixture_paths(fixtures, "019f8b21-a6ae"),
        "thread_spawn 子线程的 session_meta 会继承父线程的 session_id，同时 forked_from_id 也指向父线程，"
        "于是「按 session_id 归档」时这条记录看起来自己 fork 了自己；判据：forked_from_id==session_id 且 "
        "forked_from_id!=id。凡命中者一律不判为重放（真正的重放要按 G3 用 id 比对）。"
        "另在全量 rollout 上统计两种自指份数。",
        values,
        fixture_values,
    )


def g5(path: Path, fixtures: Path) -> dict:
    def compute(rows):
        sentinel = 0
        windows = set()
        last_line = None
        last = None
        total_rows = 0
        for i, obj in enumerate(rows, 1):
            info = token_info(obj)
            if info is None:
                continue
            total_rows += 1
            tu = total_usage(info)
            if all_zero(tu) and tu.get("total_tokens") == info.get("model_context_window"):
                sentinel += 1
                windows.add(info.get("model_context_window"))
            elif is_real(info):
                last_line, last = i, triple(tu)
        return {
            "token_count_rows": total_rows,
            "sentinel_rows": sentinel,
            "sentinel_context_windows": sorted(windows),
            "last_non_sentinel_line": last_line,
            "last_real_total_token_usage": last,
        }

    rows = read_rows(path)
    fx = fixtures_rows(fixtures, G5_CTX_SENTINEL)
    return item(
        "G5_ctx_sentinel",
        [path.name],
        fixture_paths(fixtures, G5_CTX_SENTINEL),
        "哨兵行判据：total_token_usage 五层全零且 total_tokens == info.model_context_window。"
        "这类行是上下文窗口播报而不是用量，计入用量会把整份会话虚增成一个窗口；"
        "真值取最后一条 input+output>0 的三元组（它排在全部哨兵行之前）。",
        compute(rows),
        compute(fx) if fx else None,
    )


def g6(path: Path, fixtures: Path) -> dict:
    meta = first_session_meta(read_rows(path))
    values = {
        "id": meta.get("id"),
        "session_id": meta.get("session_id"),
        "id_equals_session_id": meta.get("id") == meta.get("session_id"),
        "parent_thread_id": meta.get("parent_thread_id"),
        "source": meta.get("source"),
    }
    return item(
        "G6_subagent_id_trap",
        [path.name],
        fixture_paths(fixtures, G6_SUBAGENT_ID_TRAP),
        "读首条 session_meta：payload.id 是本 rollout 自己的线程 id，payload.session_id 却是父线程 id，"
        "两者不等；parent_thread_id 显式指向父线程，source 形如 {\"subagent\":{\"other\":\"guardian\"}}。"
        "凡按 session_id 建索引者会把这份并进父会话——识别 Codex 子会话必须用 id 而不是 session_id。",
        values,
        values,
    )


def g7(path: Path, fixtures: Path) -> dict:
    def compute(rows):
        counts = {"custom_tool_call": 0, "function_call": 0, "mcp_tool_call_end": 0}
        for obj in rows:
            kind, sub = row_label(obj)
            if kind == "response_item" and sub in ("custom_tool_call", "function_call"):
                counts[sub] += 1
            elif kind == "event_msg" and sub == "mcp_tool_call_end":
                counts["mcp_tool_call_end"] += 1
        counts["response_item_calls_total"] = counts["custom_tool_call"] + counts["function_call"]
        return counts

    rows = read_rows(path)
    fx = fixtures_rows(fixtures, G7_MCP_CALL_SHAPE)
    return item(
        "G7_mcp_call_shape",
        [path.name],
        fixture_paths(fixtures, G7_MCP_CALL_SHAPE),
        "分别数两类行：response_item.payload.type ∈ {custom_tool_call, function_call} 是工具调用记录，"
        "event_msg.payload.type == mcp_tool_call_end 是 MCP 调用结束播报。两者不是同一口径也不成倍数关系，"
        "把它们当同一件事统计会同时漏记本地工具调用并错配 MCP 调用。",
        compute(rows),
        compute(fx) if fx else None,
    )


def g8(os_con: sqlite3.Connection | None, codex_home: Path, fixtures: Path) -> dict:
    values = {}
    if os_con is not None:
        counts = os_event_counts(os_con, G8_SYSTEM_SESSION)
        cwds = [r[0] for r in os_con.execute(
            "select distinct json_extract(data, '$.cwd') from events "
            "where json_extract(data, '$.session_id') like ? and json_extract(data, '$.cwd') is not null",
            (G8_SYSTEM_SESSION + "%",))]
        values = {
            "session_id_prefix": G8_SYSTEM_SESSION,
            "event_type_counts": counts,
            "cc_session_start": counts.get("cc.session_start", 0),
            "tool_events": counts.get("cc.tool_use", 0) + counts.get("cc.tool_complete", 0),
            "cwd_under_codex_home_memories": bool(cwds) and all(
                Path(c) == codex_home / "memories" for c in cwds),
        }
    fx = fixtures / "os-events" / f"{G8_SYSTEM_SESSION}.jsonl"
    fixture_values = {}
    if fx.exists():
        rows = read_rows(fx)
        counts = {}
        for obj in rows:
            counts[obj["type"]] = counts.get(obj["type"], 0) + 1
        fixture_values = {
            "rows": len(rows),
            "event_type_counts": counts,
            "cc_session_start": counts.get("cc.session_start", 0),
            "tool_events": counts.get("cc.tool_use", 0) + counts.get("cc.tool_complete", 0),
            "cwd_placeholder": rows[0]["data"]["cwd"],
        }
    return item(
        "G8_system_session",
        ["os-db"],
        [f"os-events/{G8_SYSTEM_SESSION}.jsonl"] if fx.exists() else [],
        "OS 库按 json_extract(data,'$.session_id') 前缀 019f8d5f 取事件：只有一条 cc.session_start，"
        "工具事件为 0，cwd 落在 CODEX_HOME/memories 下——这是 Codex 自身的内务会话，"
        "不是用户会话，注册成项目/团队即误收。夹具里 cwd 归一为 /codexhome/memories。",
        values,
        fixture_values,
    )


def g9(os_con: sqlite3.Connection | None, native: list[Path], state: Path | None,
       tmp: Path, fixtures: Path) -> dict:
    values = {}
    if os_con is not None:
        counts = os_event_counts(os_con, G9_UNPERSISTED_SESSION)
        sids = sorted(r[0] for r in os_con.execute(
            "select distinct json_extract(data, '$.session_id') from events "
            "where json_extract(data, '$.session_id') like ?", (G9_UNPERSISTED_SESSION + "%",)))
        in_rollouts = any(G9_UNPERSISTED_SESSION in p.name for p in native)
        in_threads = False
        if state is not None and state.exists():
            con = open_ro(state, tmp)
            try:
                in_threads = bool(con.execute(
                    "select count(*) from threads where id like ?",
                    (G9_UNPERSISTED_SESSION + "%",)).fetchone()[0])
            finally:
                con.close()
        values = {
            "session_ids": sids,
            "event_type_counts": counts,
            "cc_tool_use": counts.get("cc.tool_use", 0),
            "present_in_rollouts": in_rollouts,
            "present_in_state_threads": in_threads,
        }
    fx = fixtures / "os-events" / f"{G9_UNPERSISTED_SESSION}.jsonl"
    fixture_values = {}
    if fx.exists():
        rows = read_rows(fx)
        counts = {}
        for obj in rows:
            counts[obj["type"]] = counts.get(obj["type"], 0) + 1
        sample = fixtures / "state" / "threads.sample.json"
        ids = []
        if sample.exists():
            data = json.loads(sample.read_text(encoding="utf-8"))
            ids = [e["row"]["id"] for e in data["threads_sample"]]
        fixture_values = {
            "cc_tool_use": counts.get("cc.tool_use", 0),
            "present_in_rollouts": any(
                G9_UNPERSISTED_SESSION in p.name for p in fixtures.rglob("rollout-*.jsonl")),
            "present_in_state_sample": any(
                i.startswith(G9_UNPERSISTED_SESSION) for i in ids),
        }
    return item(
        "G9_unpersisted_session",
        ["os-db", "state_5", "<native rollouts>"],
        [f"os-events/{G9_UNPERSISTED_SESSION}.jsonl"] if fx.exists() else [],
        "OS 库按 session_id 前缀 019f99a6 能取到成串 cc.tool_use，但同一 id 在 60 份 rollout 与 "
        "state_5.threads 里都不存在——Codex 有整段会话只落 hook 事件、不落 rollout 也不落线程表，"
        "凡以 rollout/线程表为唯一真源的对账都会把它整段漏掉。",
        values,
        fixture_values,
    )


def hook_shape(rows: list[dict]) -> dict:
    """SubagentStart/Stop transcript wiring and the spawn tool name shape."""
    out = {}
    for obj in rows:
        pl = payload(obj) or obj
        ev = pl.get("hook_event_name")
        if ev == "SubagentStart" and "subagent_start" not in out:
            out["subagent_start"] = {
                "transcript": Path(pl.get("transcript_path", "")).name,
                "agent_transcript": Path(pl["agent_transcript_path"]).name
                if pl.get("agent_transcript_path") else None,
                "agent_id": pl.get("agent_id"),
                "session_id": pl.get("session_id"),
            }
        elif ev == "SubagentStop" and "subagent_stop" not in out:
            out["subagent_stop"] = {
                "transcript": Path(pl.get("transcript_path", "")).name,
                "agent_transcript": Path(pl["agent_transcript_path"]).name
                if pl.get("agent_transcript_path") else None,
                "agent_id": pl.get("agent_id"),
                "session_id": pl.get("session_id"),
            }
        if ev == "PreToolUse" and "spawn_tool_name" not in out:
            name = pl.get("tool_name") or ""
            if name.endswith("spawn_agent"):
                out["spawn_tool_name"] = name
                out["spawn_tool_namespaced"] = name != "spawn_agent"
    start, stop = out.get("subagent_start", {}), out.get("subagent_stop", {})
    out["start_transcript_is_child"] = bool(start) and start["transcript"].endswith(
        f"{start['agent_id']}.jsonl")
    out["stop_transcript_is_parent"] = bool(stop) and stop["transcript"].endswith(
        f"{stop['session_id']}.jsonl")
    out["stop_agent_transcript_is_child"] = bool(stop) and bool(
        stop["agent_transcript"]) and stop["agent_transcript"].endswith(f"{stop['agent_id']}.jsonl")
    return out


def g10(cap142: Path, cap152: Path, fixtures: Path) -> dict:
    values = {
        "probe_0142": hook_shape(read_rows(cap142)),
        "probe_0152": hook_shape(read_rows(cap152)),
    }
    fx142 = fixtures / "hooks/probe-0142/capture-run4-subagent.jsonl"
    fx152 = fixtures / "hooks/probe-0152/capture-run4-subagent.jsonl"
    fixture_values = {}
    if fx142.exists() and fx152.exists():
        fixture_values = {
            "probe_0142": hook_shape(read_rows(fx142)),
            "probe_0152": hook_shape(read_rows(fx152)),
        }
    return item(
        "G10_hook_payload_shape",
        [cap142.name, cap152.name],
        [str(p.relative_to(fixtures)) for p in (fx142, fx152) if p.exists()],
        "派工三路径关系：SubagentStart.transcript_path 指子线程 rollout（basename 以 agent_id 收尾），"
        "SubagentStop.transcript_path 指父线程（以 session_id 收尾）而 agent_transcript_path 才指子线程；"
        "把 Stop 的 transcript_path 当子线程用会把子 agent 的用量记到父身上。"
        "两版探针同形，差别只在派工工具名：0.142 是 spawn_agent，0.152.1 带命名空间前缀。",
        values,
        fixture_values,
    )


def g11(state: Path, os_con: sqlite3.Connection | None, tmp: Path, fixtures: Path) -> dict:
    con = open_ro(state, tmp)
    try:
        spawn, guardian, total = [], {}, 0
        for tid, src in con.execute("select id, source from threads order by id"):
            total += 1
            try:
                parsed = json.loads(src)
            except (TypeError, ValueError):
                continue
            sub = parsed.get("subagent") if isinstance(parsed, dict) else None
            if not isinstance(sub, dict):
                continue
            if "thread_spawn" in sub:
                spawn.append(tid)
            elif "other" in sub:
                key = str(sub["other"])
                guardian[key] = guardian.get(key, 0) + 1
    finally:
        con.close()
    values = {
        "threads_total": total,
        "thread_spawn_thread_ids": spawn,
        "other_bucket_counts": guardian,
    }
    if os_con is not None and spawn:
        marks = ",".join("?" * len(spawn))
        values["os_agents_rows_with_matching_cc_tool_use_id"] = os_con.execute(
            f"select count(*) from agents where cc_tool_use_id in ({marks})", spawn).fetchone()[0]
    sample = fixtures / "state" / "threads.sample.json"
    fixture_values = {}
    if sample.exists():
        data = json.loads(sample.read_text(encoding="utf-8"))
        fixture_values = {
            "threads_total": data.get("threads_total"),
            "thread_spawn_thread_ids": sorted(
                e["child_thread_id"] for e in data.get("thread_spawn_edges", [])),
            "thread_spawn_sample_id": next(
                (e["row"]["id"] for e in data["threads_sample"] if e["sample"] == "thread-spawn"),
                None),
        }
    return item(
        "G11_subagent_reach",
        ["state_5", "os-db"],
        ["state/threads.sample.json"] if sample.exists() else [],
        "state_5.threads.source 是 JSON 串：含 subagent.thread_spawn 键的才是真正被派出去的子线程"
        "（同一集合也出现在 thread_spawn_edges.child_thread_id）；含 subagent.other 的按 other 值分桶，"
        "是 Codex 内务子会话，单列为免检桶。把这批线程 id 拿去比 OS 库 agents.cc_tool_use_id，命中 0 行——"
        "Codex 派出的子线程完全不出现在 OS 的 agent 台账里。",
        values,
        fixture_values,
    )


def g12(files: list[Path], fixtures: Path) -> dict:
    def compute(paths):
        per = {}
        for p in sorted(paths, key=lambda q: q.name):
            last = None
            for obj in read_rows(p):
                info = token_info(obj)
                if info is not None and total_usage(info):
                    last = triple(total_usage(info))
            per[p.name] = last
        return per

    values = {
        "rollouts": len(files),
        "last_total_token_usage_by_rollout": compute(files),
        "run8_compaction_rollout": pick(files, G12_RUN8_COMPACT).name,
        "run8_last_total_tokens": compute([pick(files, G12_RUN8_COMPACT)])[
            pick(files, G12_RUN8_COMPACT).name][2],
    }
    fx = sorted((fixtures / "rollouts/probe-0142").rglob("*.jsonl"), key=lambda p: p.name)
    fixture_values = {}
    if fx:
        fixture_values = {
            "rollouts": len(fx),
            "last_total_token_usage_by_rollout": compute(fx),
            "run8_last_total_tokens": compute([pick(fx, G12_RUN8_COMPACT)])[
                pick(fx, G12_RUN8_COMPACT).name][2],
        }
    return item(
        "G12_baseline_0142",
        sorted(p.name for p in files),
        sorted(str(p.relative_to(fixtures)) for p in fx),
        "0.142 探针 10 份 rollout 各取末条 token_count 的 total_token_usage 三元组，作为 0.142 口径基线；"
        "run8 是压缩用例（会话被 compact 过），其末条 total 单列——压缩后 total_token_usage 仍是累计值，"
        "不因压缩回落，任何「压缩后重新计数」的假设都会被这一份证伪。",
        values,
        fixture_values,
    )


def partition_phantoms(files: list[Path]) -> dict:
    sentinel = baseline = residual = 0
    residual_rows = []
    last_zero_by_cli: dict[str, int] = {}
    for p in files:
        rows = read_rows(p)
        cli = first_session_meta(rows).get("cli_version")
        _, base_total = import_baseline(rows)
        for i, obj in enumerate(rows, 1):
            info = token_info(obj)
            if info is None:
                continue
            tu = total_usage(info)
            if all_zero(tu) and (tu.get("total_tokens") or 0) > 0:
                if tu["total_tokens"] == info.get("model_context_window"):
                    sentinel += 1
                elif base_total is not None and tu["total_tokens"] == base_total:
                    baseline += 1
                else:
                    residual += 1
                    residual_rows.append([p.name, i, tu["total_tokens"]])
            lu = info.get("last_token_usage")
            if isinstance(lu, dict) and all_zero(lu) and (lu.get("total_tokens") or 0) > 0:
                last_zero_by_cli[cli] = last_zero_by_cli.get(cli, 0) + 1
    return {
        "rollouts_scanned": len(files),
        "phantom_rows": sentinel + baseline + residual,
        "sentinel_rows": sentinel,
        "import_baseline_rows": baseline,
        "residual_rows": residual,
        "residual_examples": residual_rows[:5],
        "last_token_usage_zero_positive_by_cli_version": dict(sorted(last_zero_by_cli.items())),
    }


def g13(files: list[Path], fixtures: Path) -> dict:
    fx = sorted((fixtures / "rollouts/native").rglob("*.jsonl"), key=lambda p: p.name)
    return item(
        "G13_phantom_partition",
        ["<native rollouts>"],
        sorted(str(p.relative_to(fixtures)) for p in fx),
        "对全量 rollout 的每条 token_count：total_token_usage 五层全零且 total_tokens>0 的即幻影行；"
        "幻影行须被两条判据穷尽——要么 total_tokens==info.model_context_window（哨兵），"
        "要么 total_tokens== 本份的导入基线（导入基线在续用后会被反复重发，且可能带上 context_window 字段，"
        "只按 context_window 是否为空分类会留残余）。residual_rows 必须为 0。"
        "另按 cli_version 分列 last_token_usage 五层全零而 total>0 的行数。"
        "夹具口径只覆盖夹具内的 native 份（含两份裁剪版），数值必然小于生产口径。",
        partition_phantoms(files),
        partition_phantoms(fx) if fx else None,
    )


def lineage(files: list[Path]) -> dict:
    entries = []
    for p in files:
        rows = read_rows(p)
        meta = first_session_meta(rows)
        first = last = None
        for obj in rows:
            info = token_info(obj)
            if info is not None and is_real(info):
                tu = total_usage(info)
                if first is None:
                    first = tu.get("total_tokens")
                last = tu.get("total_tokens")
        entries.append({
            "rollout": p.name, "id": meta.get("id"), "session_id": meta.get("session_id"),
            "fork": meta.get("forked_from_id"), "first": first, "last": last,
        })
    non_self = [[e["id"], e["fork"]] for e in entries
                if e["fork"] and e["fork"] != e["session_id"] and e["fork"] != e["id"]]
    self_by_session = [e["id"] for e in entries if e["fork"] and e["fork"] == e["session_id"]]
    naive = sum(e["last"] for e in entries if e["last"])
    groups: dict[int, int] = {}
    for e in entries:
        if e["last"] is None:
            continue
        groups[e["first"]] = max(groups.get(e["first"], 0), e["last"])
    dedup = sum(groups.values())
    return {
        "rollouts_scanned": len(files),
        "rollouts_with_real_usage": sum(1 for e in entries if e["last"] is not None),
        "non_self_fork_count": len(non_self),
        "non_self_fork_pairs": sorted(non_self),
        "self_fork_by_session_id_count": len(self_by_session),
        "self_fork_by_session_id_ids": sorted(self_by_session),
        "naive_last_total_sum": naive,
        "lineage_dedup_total": dedup,
        "lineage_groups": len(groups),
        "inflation_ratio": round((naive - dedup) / dedup, 6) if dedup else None,
    }


def g14(files: list[Path], fixtures: Path) -> dict:
    fx = sorted((fixtures / "rollouts/native").rglob("*.jsonl"), key=lambda p: p.name)
    return item(
        "G14_fork_lineage_dedup",
        ["<native rollouts>"],
        sorted(str(p.relative_to(fixtures)) for p in fx),
        "谱系口径：forked_from_id 既不为空也不等于本份 session_id/id 的才是真 fork（G4 的自指份要排除）；"
        "「首条真值 total 相同即同谱系」分组后每组取末条真值 total 的最大值求和，"
        "与各份末条真值朴素求和相比即虚增比例——朴素求和把 fork 重放份整份重复计入。"
        "夹具口径只覆盖夹具内 native 份，数值必然小于生产口径。",
        lineage(files),
        lineage(fx) if fx else None,
    )


def g15(files: list[Path], imports_json: Path, fixtures: Path) -> dict:
    hits = [p.name for p in files if import_turn_lines(read_rows(p))]
    records = json.loads(imports_json.read_text(encoding="utf-8")).get("records", [])
    values = {
        "rollouts_scanned": len(files),
        "rollouts_with_import_turn_prefix": len(hits),
        "external_import_records": len(records),
        "counts_match": len(hits) == len(records),
    }
    fixture_values = {}
    manifest = fixtures / "MANIFEST.json"
    if manifest.exists():
        fx = sorted(fixtures.rglob("rollout-*.jsonl"), key=lambda p: p.name)
        fx_hits = [p for p in fx if import_turn_lines(read_rows(p))]
        fixture_values = {
            "rollouts_with_import_turn_prefix": len(fx_hits),
            "manifest_import_samples": json.loads(
                manifest.read_text(encoding="utf-8")).get("import_samples"),
        }
    return item(
        "G15_import_predicate_count",
        ["<native rollouts>", "imports.json"],
        ["MANIFEST.json"] if manifest.exists() else [],
        "导入判据 = 任一行 payload.turn_id 以 external-import-turn- 开头。"
        "全量 rollout 上的命中份数必须等于 external_agent_session_imports.json 的 records 条数——"
        "两个独立来源互证判据既不漏也不多。夹具层面：夹具内命中该判据的 rollout 数须等于 "
        "MANIFEST.import_samples。",
        values,
        fixture_values,
    )


# --------------------------------------------------------------- main


def build(args) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    evidence = Path(args.evidence).expanduser().resolve()
    fixtures = Path(args.fixtures).expanduser().resolve()
    out = Path(args.out).expanduser().resolve() if args.out else fixtures / "golden.json"
    os_db = Path(args.os_db).expanduser()

    probe142 = evidence / "codex-probe-proj"
    probe152 = evidence / "codex-probe-proj-0152"
    native = native_rollouts(codex_home)
    probe142_rollouts = sorted((probe142 / "evidence/rollouts").rglob("*.jsonl"), key=lambda p: p.name)

    tmp = Path(tempfile.mkdtemp(prefix="codex-golden-"))
    os_con = open_ro(os_db, tmp) if os_db.exists() else None
    try:
        state = codex_home / "state_5.sqlite"
        items = [
            g1(pick(native, G1_IMPORT_RESUMED), fixtures, os_con),
            g2(pick(native, G2_IMPORT_NEVER_RESUMED), fixtures, os_con),
            g3(pick(native, G3_FORK_REPLAY), pick(native, "019f8b2f-1617"), fixtures),
            g4(native, fixtures),
            g5(pick(native, G5_CTX_SENTINEL), fixtures),
            g6(pick(native, G6_SUBAGENT_ID_TRAP), fixtures),
            g7(pick(native, G7_MCP_CALL_SHAPE), fixtures),
            g8(os_con, codex_home, fixtures),
            g9(os_con, native, state, tmp, fixtures),
            g10(probe142 / "capture-run4-subagent.jsonl",
                probe152 / "capture-run4-subagent.jsonl", fixtures),
            g11(state, os_con, tmp, fixtures),
            g12(probe142_rollouts, fixtures),
            g13(native, fixtures),
            g14(native, fixtures),
            g15(native, codex_home / "external_agent_session_imports.json", fixtures),
        ]
    finally:
        if os_con is not None:
            os_con.close()
        shutil.rmtree(tmp, ignore_errors=True)

    doc = {
        "golden_set": "codex-evidence",
        "schema": 1,
        "note": "值由 scripts/compute_codex_golden.py 从本机真实 Codex 环境重算，禁止手工编辑。"
                "values = 生产口径（全量原件）；fixture_values = 夹具切片上可独立复算的同名子集，"
                "两者同名不同值时以 method 里写明的口径差为准。",
        "items": items,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(items)} items)")
    return 0


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--os-db", default=str(Path.home() / ".claude/data/ai-team-os/aiteam.db"))
    ap.add_argument("--fixtures", default=str(repo / "tests" / "fixtures" / "codex"))
    ap.add_argument("--out", default="")
    return build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
