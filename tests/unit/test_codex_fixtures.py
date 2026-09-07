"""Codex 磁盘夹具的完整性、脱敏不变量与 golden 复算测试。

四层覆盖，全部只读仓内 `tests/fixtures/codex`，不碰本机 Codex 目录也不碰 OS 库
（CI 上两者都不存在）：

1. **MANIFEST 对账**：每个登记文件的 sha256/bytes/lines 与磁盘一致，且磁盘上没有
   未登记的文件——夹具被手工改过一个字节就会红。
2. **脱敏不变量**：`base_instructions`/`git` 键、`/Users/`、`/Volumes/`、`Asia/`、
   `sk-` 形制密钥、`bearer` 一律不得出现；`timezone` 一律 UTC；模型 slug 只能是
   MANIFEST 登记的占位符。这是"泄漏守卫"在生成器之外的第二道闸。
3. **路径可解析**：样本里每个 `/codexhome` 前缀路径按各自的替换根还原后必须能在
   夹具内找到文件——保证 transcript_path 这类跨文件引用在夹具里是活的。
4. **golden 复算**：`golden.json` 里 `fixture_verifiable=true` 的每一项，用本文件里
   一份独立的最小实现在夹具切片上重算 `fixture_values`，逐键比对。断言值来自
   golden.json 与夹具的交叉，不硬编码任何与生成器同源的常量；且每个可复算项都必须
   在 RECOMPUTE 里有实现，新增 golden 不能悄悄绕过复算。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from testlib import CODEX_FIXTURES

FIXTURES = CODEX_FIXTURES
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))

# 不在 MANIFEST.files 内、但允许出现在夹具目录里的文件（说明性/派生产物）。
UNREGISTERED_OK = {"MANIFEST.json", "README.md", "golden.json"}

IMPORT_PREFIX = "external-import-turn-"
IMPORT_MARKER = "<EXTERNAL SESSION IMPORTED>"
LAYERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens")


# --------------------------------------------------------------------- 工具


def data_files() -> list[Path]:
    """夹具目录下的全部文件（含 README/MANIFEST/golden）。"""
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rollouts(subdir: str) -> list[Path]:
    return sorted((FIXTURES / subdir).rglob("rollout-*.jsonl"), key=lambda p: p.name)


def one(paths: list[Path], marker: str) -> Path:
    hits = [p for p in paths if marker in p.name]
    assert len(hits) == 1, f"{marker}: 期望夹具内恰好一份，实际 {len(hits)}"
    return hits[0]


def walk(node, key=None):
    """深度优先遍历 (key, value)，叶子为标量。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, key)
    else:
        yield key, node


def json_docs(path: Path):
    if path.suffix == ".jsonl":
        yield from jsonl(path)
    elif path.suffix == ".json":
        yield json.loads(path.read_text(encoding="utf-8"))


def pl(obj: dict) -> dict:
    p = obj.get("payload")
    return p if isinstance(p, dict) else {}


def usage(obj: dict) -> dict | None:
    """token_count 行的 total_token_usage，非 token_count 行返回 None。"""
    if obj.get("type") != "event_msg" or pl(obj).get("type") != "token_count":
        return None
    info = pl(obj).get("info") or {}
    tu = info.get("total_token_usage")
    return tu if isinstance(tu, dict) else {}


def context_window(obj: dict):
    return (pl(obj).get("info") or {}).get("model_context_window")


def zeroed(tu: dict) -> bool:
    return bool(tu) and all(tu.get(k) == 0 for k in LAYERS)


def phantom(tu: dict) -> bool:
    return zeroed(tu) and (tu.get("total_tokens") or 0) > 0


def spent(tu: dict) -> bool:
    return bool(tu) and (tu.get("input_tokens") or 0) + (tu.get("output_tokens") or 0) > 0


def triple(tu: dict) -> list[int]:
    return [tu["input_tokens"], tu["output_tokens"], tu["total_tokens"]]


def import_lines(rows: list[dict]) -> list[int]:
    return [i for i, obj in enumerate(rows, 1)
            if isinstance(pl(obj).get("turn_id"), str) and pl(obj)["turn_id"].startswith(IMPORT_PREFIX)]


def import_turns(rows: list[dict]) -> set[str]:
    return {pl(obj)["turn_id"] for obj in rows
            if isinstance(pl(obj).get("turn_id"), str) and pl(obj)["turn_id"].startswith(IMPORT_PREFIX)}


def baseline_of(rows: list[dict]) -> tuple[int | None, int | None]:
    """导入段内最后一条幻影 token_count 的 (行号, total_tokens)。"""
    lines = import_lines(rows)
    if not lines:
        return None, None
    line = total = None
    for i, obj in enumerate(rows[:lines[-1]], 1):
        tu = usage(obj)
        if tu is not None and phantom(tu):
            line, total = i, tu["total_tokens"]
    return line, total


def marker_of(rows: list[dict]) -> int | None:
    for i, obj in enumerate(rows, 1):
        if IMPORT_MARKER in json.dumps(obj, ensure_ascii=False):
            return i
    return None


def first_meta(rows: list[dict]) -> dict:
    """首条 session_meta——fork 份第 2 行会再发父线程的 meta，只认第 1 条。"""
    for obj in rows:
        if obj.get("type") == "session_meta":
            return pl(obj)
    return {}


def last_spent_total(rows: list[dict]) -> int | None:
    out = None
    for obj in rows:
        tu = usage(obj)
        if tu is not None and spent(tu):
            out = tu["total_tokens"]
    return out


def golden_item(name: str) -> dict:
    hits = [it for it in GOLDEN["items"] if it["name"] == name]
    assert len(hits) == 1, f"golden.json 缺少或重复条目 {name}"
    return hits[0]


NATIVE = rollouts("rollouts/native")
PROBE_0142 = rollouts("rollouts/probe-0142")


# --------------------------------------------------------------- 1. MANIFEST


@pytest.mark.parametrize("entry", MANIFEST["files"], ids=lambda e: e["path"])
def test_manifest_entry_matches_disk(entry):
    path = FIXTURES / entry["path"]
    assert path.is_file(), f"MANIFEST 登记了不存在的文件：{entry['path']}"
    blob = path.read_bytes()
    assert hashlib.sha256(blob).hexdigest() == entry["sha256"], f"{entry['path']} 内容与登记 sha256 不符"
    assert len(blob) == entry["bytes"]
    assert blob.decode("utf-8").count("\n") == entry["lines"]


def test_no_unregistered_files_on_disk():
    registered = {e["path"] for e in MANIFEST["files"]} | UNREGISTERED_OK
    on_disk = {str(p.relative_to(FIXTURES)) for p in data_files()}
    assert on_disk - registered == set(), "夹具目录里有 MANIFEST 未登记的文件"
    assert registered - UNREGISTERED_OK - on_disk == set(), "MANIFEST 登记了磁盘上没有的文件"


# ------------------------------------------------------------- 2. 脱敏不变量


def test_no_conversation_or_repo_keys():
    offenders = []
    for path in data_files():
        for doc in json_docs(path):
            for key, _ in walk(doc):
                if key in ("base_instructions", "git"):
                    offenders.append(f"{path.relative_to(FIXTURES)}:{key}")
    assert offenders == []


# 只匹配"后面真的跟着路径段/时区名/密钥体"的形态：README 里作为规则名出现的
# 裸 `/Users/`、`Asia/` 不该被判泄漏，真实路径与真实密钥才该被判。
FORBIDDEN_PATTERNS = {
    "home_path": r"/Users/[\w.\-]",
    "volume_path": r"/Volumes/[\w.\-]",
    "real_timezone": r"Asia/[A-Z]",
    "bearer_value": r"(?i)bearer[\s\"':=]+[A-Za-z0-9._\-]{16,}",
    "api_key": r"sk-[A-Za-z0-9_\-]{12,}",
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    "http_url": r"https?://",
}

# 说明性文件（中文）不受"数据文件纯 ASCII"约束，其余一律受约束。
# golden.json 曾整份在这里豁免——而它是采集器直读原件写出来的跟踪文件，等于给
# 原件正文留了一条绕过纯 ASCII 网的路（P-1 抓到的那次实锤泄漏，一个中文文档标题
# 加 17 个私有脚本名，正是靠这张网抓到的）。现在只豁免它的两个说明键。
PROSE_FILES = {"README.md"}
GOLDEN_PROSE_KEYS = {"method", "note"}


@pytest.mark.parametrize("label", sorted(FORBIDDEN_PATTERNS))
def test_forbidden_literals_absent(label):
    pattern = re.compile(FORBIDDEN_PATTERNS[label])
    hits = []
    for path in data_files():
        match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            hits.append(f"{path.relative_to(FIXTURES)}: {match.group(0)[:24]}")
    assert hits == []


def test_data_files_are_pure_ascii():
    """数据文件里出现非 ASCII 只可能来自原件内容——真实文档名就是这样漏出去的。"""
    offenders = []
    for path in data_files():
        if path.name in PROSE_FILES or path.name == "golden.json":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.isascii():
            bad = next(ch for ch in text if not ch.isascii())
            offenders.append(f"{path.relative_to(FIXTURES)}: U+{ord(bad):04X}")
    assert offenders == []


def test_golden_is_ascii_outside_its_two_prose_keys():
    """golden.json 里只有 method/note 可以是中文，其余每个键和值都必须是 ASCII。

    这两个键是写给人读的口径说明；其它一切都是行号、计数、id 和文件名。非 ASCII
    出现在别处，只可能是从原件里带出来的正文。
    """
    offenders = []

    def visit(node, where: str, prose: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                check(str(key), f"{where}.<key>", False)
                visit(value, f"{where}.{key}", prose or key in GOLDEN_PROSE_KEYS)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                visit(value, f"{where}[{i}]", prose)
        elif isinstance(node, str):
            check(node, where, prose)

    def check(text: str, where: str, prose: bool) -> None:
        if prose or text.isascii():
            return
        bad = next(ch for ch in text if not ch.isascii())
        offenders.append(f"{where}: U+{ord(bad):04X}")

    visit(GOLDEN, "golden", False)
    assert offenders == []


def test_timezone_is_always_utc():
    seen = set()
    for path in data_files():
        for doc in json_docs(path):
            for key, value in walk(doc):
                if key == "timezone" and value is not None:
                    seen.add(value)
    assert seen, "夹具里应至少有一处 timezone 供核对"
    assert seen == {"UTC"}


def test_model_slugs_are_placeholders_only():
    allowed = set(MANIFEST["model_placeholders"])
    seen = set()
    for path in data_files():
        for doc in json_docs(path):
            for key, value in walk(doc):
                if key == "model" and isinstance(value, str) and value:
                    seen.add(value)
    assert seen, "夹具里应至少有一处 model 供核对"
    assert seen <= allowed, f"出现非占位符模型 slug：{sorted(seen - allowed)}"


# ------------------------------------------------------------- 3. 路径可解析

PATH_KEYS = ("transcript_path", "agent_transcript_path", "rollout_path")


def codexhome_roots() -> dict[str, str]:
    return {e["path"]: e.get("codexhome_root", "") for e in MANIFEST["files"]}


def test_codexhome_paths_resolve_inside_fixtures():
    """每个 /codexhome 路径按本文件登记的替换根还原后必须命中夹具内的真实文件。

    state 抽样这类文件 MANIFEST 没有登记 codexhome_root（替换根写在 README 的对照表里），
    对它们放宽为"能在 MANIFEST.codexhome_roots 的任一替换根下解析到"。
    """
    roots = codexhome_roots()
    candidates = list(MANIFEST["codexhome_roots"])
    checked = 0
    missing = []
    for path in data_files():
        rel = str(path.relative_to(FIXTURES))
        tries = [roots[rel]] if roots.get(rel) else candidates
        for doc in json_docs(path):
            for key, value in walk(doc):
                if key not in PATH_KEYS or not isinstance(value, str) or not value.startswith("/codexhome/"):
                    continue
                checked += 1
                tail = value[len("/codexhome/"):]
                if not any((FIXTURES / root / tail).exists() for root in tries):
                    missing.append(f"{rel} -> {value}")
    assert checked > 0, "夹具里应有 /codexhome 路径供核对"
    assert missing == [], f"{len(missing)} 处 /codexhome 路径在夹具内解析不到"


# --------------------------------------------------------------- 4. golden 复算


def rc_g1() -> dict:
    rows = jsonl(one(NATIVE, "019f8d58-610c"))
    lines = import_lines(rows)
    base_line, base_total = baseline_of(rows)
    checked = violations = 0
    last = None
    for obj in rows:
        tu = usage(obj)
        if tu is None or not tu or phantom(tu):
            continue
        checked += 1
        if tu["total_tokens"] - (tu["input_tokens"] + tu["output_tokens"]) != base_total:
            violations += 1
        if spent(tu):
            last = triple(tu)
    return {
        "import_segment_first_line": lines[0],
        "import_segment_last_line": lines[-1],
        "import_segment_lines": lines[-1] - lines[0] + 1,
        "import_turn_count": len(import_turns(rows)),
        "import_marker_line": marker_of(rows),
        "import_baseline_line": base_line,
        "import_baseline_total": base_total,
        "identity_checked_rows": checked,
        "identity_violations": violations,
        "last_real_total_token_usage": last,
    }


def rc_g2() -> dict:
    rows = jsonl(one(NATIVE, "019f8d58-606e"))
    return {
        "fixture_lines": len(rows),
        "import_turn_rows": len(import_lines(rows)),
        "real_token_count_rows": sum(1 for obj in rows if (tu := usage(obj)) is not None and spent(tu)),
    }


def rc_g3() -> dict:
    child = one(NATIVE, "019f8d92-93fd")
    parent = one(NATIVE, "019f8b2f-1617")
    meta = first_meta(jsonl(child))
    own, par = last_spent_total(jsonl(child)), last_spent_total(jsonl(parent))
    return {
        "id": meta.get("id"),
        "session_id": meta.get("session_id"),
        "forked_from_id": meta.get("forked_from_id"),
        "parent_rollout": parent.name,
        "own_last_real_total": own,
        "parent_last_real_total": par,
        "totals_equal": own == par,
    }


def rc_g4() -> dict:
    meta = first_meta(jsonl(one(NATIVE, "019f8b21-a6ae")))
    self_forks = []
    for path in NATIVE:
        m = first_meta(jsonl(path))
        if m.get("forked_from_id") and m["forked_from_id"] == m.get("session_id"):
            self_forks.append(m.get("id"))
    return {
        "id": meta.get("id"),
        "session_id": meta.get("session_id"),
        "forked_from_id": meta.get("forked_from_id"),
        "forked_from_equals_session_id": meta.get("forked_from_id") == meta.get("session_id"),
        "forked_from_equals_id": meta.get("forked_from_id") == meta.get("id"),
        "self_fork_by_session_id_ids": sorted(self_forks),
    }


def rc_g5() -> dict:
    rows = jsonl(one(NATIVE, "019f8d1a-c148"))
    sentinels = 0
    windows = set()
    line = last = None
    total_rows = 0
    for i, obj in enumerate(rows, 1):
        tu = usage(obj)
        if tu is None:
            continue
        total_rows += 1
        if zeroed(tu) and tu.get("total_tokens") == context_window(obj):
            sentinels += 1
            windows.add(context_window(obj))
        elif spent(tu):
            line, last = i, triple(tu)
    return {
        "token_count_rows": total_rows,
        "sentinel_rows": sentinels,
        "sentinel_context_windows": sorted(windows),
        "last_non_sentinel_line": line,
        "last_real_total_token_usage": last,
    }


def rc_g6() -> dict:
    meta = first_meta(jsonl(one(NATIVE, "019f8a63-ef67")))
    return {
        "id": meta.get("id"),
        "session_id": meta.get("session_id"),
        "id_equals_session_id": meta.get("id") == meta.get("session_id"),
        "parent_thread_id": meta.get("parent_thread_id"),
        "source": meta.get("source"),
    }


def rc_g7() -> dict:
    out = {"custom_tool_call": 0, "function_call": 0, "mcp_tool_call_end": 0}
    for obj in jsonl(one(NATIVE, "019f8d95-d818")):
        kind, sub = obj.get("type"), pl(obj).get("type")
        if kind == "response_item" and sub in ("custom_tool_call", "function_call"):
            out[sub] += 1
        elif kind == "event_msg" and sub == "mcp_tool_call_end":
            out["mcp_tool_call_end"] += 1
    out["response_item_calls_total"] = out["custom_tool_call"] + out["function_call"]
    return out


def _os_event_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for obj in rows:
        counts[obj["type"]] = counts.get(obj["type"], 0) + 1
    return counts


def rc_g8() -> dict:
    rows = jsonl(FIXTURES / "os-events" / "019f8d5f.jsonl")
    counts = _os_event_counts(rows)
    return {
        "rows": len(rows),
        "event_type_counts": counts,
        "cc_session_start": counts.get("cc.session_start", 0),
        "tool_events": counts.get("cc.tool_use", 0) + counts.get("cc.tool_complete", 0),
        "cwd_placeholder": rows[0]["data"]["cwd"],
    }


def rc_g9() -> dict:
    rows = jsonl(FIXTURES / "os-events" / "019f99a6.jsonl")
    sample = json.loads((FIXTURES / "state" / "threads.sample.json").read_text(encoding="utf-8"))
    return {
        "cc_tool_use": _os_event_counts(rows).get("cc.tool_use", 0),
        "present_in_rollouts": any("019f99a6" in p.name for p in FIXTURES.rglob("rollout-*.jsonl")),
        "present_in_state_sample": any(
            str(e["row"]["id"]).startswith("019f99a6") for e in sample["threads_sample"]),
    }


def _hook_shape(rows: list[dict]) -> dict:
    out: dict = {}
    for obj in rows:
        body = pl(obj) or obj
        event = body.get("hook_event_name")
        if event in ("SubagentStart", "SubagentStop"):
            slot = "subagent_start" if event == "SubagentStart" else "subagent_stop"
            if slot not in out:
                agent_tp = body.get("agent_transcript_path")
                out[slot] = {
                    "transcript": Path(body.get("transcript_path", "")).name,
                    "agent_transcript": Path(agent_tp).name if agent_tp else None,
                    "agent_id": body.get("agent_id"),
                    "session_id": body.get("session_id"),
                }
        if event == "PreToolUse" and "spawn_tool_name" not in out:
            name = body.get("tool_name") or ""
            if name.endswith("spawn_agent"):
                out["spawn_tool_name"] = name
                out["spawn_tool_namespaced"] = name != "spawn_agent"
    start, stop = out.get("subagent_start", {}), out.get("subagent_stop", {})
    out["start_transcript_is_child"] = bool(start) and start["transcript"].endswith(f"{start['agent_id']}.jsonl")
    out["stop_transcript_is_parent"] = bool(stop) and stop["transcript"].endswith(f"{stop['session_id']}.jsonl")
    out["stop_agent_transcript_is_child"] = (
        bool(stop) and bool(stop["agent_transcript"])
        and stop["agent_transcript"].endswith(f"{stop['agent_id']}.jsonl")
    )
    return out


def rc_g10() -> dict:
    return {
        "probe_0142": _hook_shape(jsonl(FIXTURES / "hooks/probe-0142/capture-run4-subagent.jsonl")),
        "probe_0152": _hook_shape(jsonl(FIXTURES / "hooks/probe-0152/capture-run4-subagent.jsonl")),
    }


def rc_g11() -> dict:
    """第三类线程只能靠聚合减法算：6 行抽样里根本没有它。

    threads_distribution 按 (cli_version, thread_source, archived) 分组，subagent 桶
    有 16 条；thread_spawn_edges 解释掉 2 条。夹具切不出 guardian 桶，所以这里只算
    这两个数，其余两个由生产口径的闭合恒等式钉住（见 ASSERT_SCOPE 的 G11 行）。
    """
    sample = json.loads((FIXTURES / "state" / "threads.sample.json").read_text(encoding="utf-8"))
    spawn_row = next((e["row"]["id"] for e in sample["threads_sample"] if e["sample"] == "thread-spawn"), None)
    return {
        "threads_total": sample.get("threads_total"),
        "thread_spawn_thread_ids": sorted(e["child_thread_id"] for e in sample["thread_spawn_edges"]),
        "thread_spawn_sample_id": spawn_row,
        "subagent_distribution_total": sum(
            row["count"] for row in sample["threads_distribution"]
            if row.get("thread_source") == "subagent"),
        "explained_by_thread_spawn": len(sample["thread_spawn_edges"]),
    }


def rc_g12() -> dict:
    per = {}
    for path in PROBE_0142:
        last = None
        for obj in jsonl(path):
            tu = usage(obj)
            if tu:
                last = triple(tu)
        per[path.name] = last
    run8 = one(PROBE_0142, "01a061b0-1d38")
    return {
        "rollouts": len(PROBE_0142),
        "last_total_token_usage_by_rollout": per,
        "run8_last_total_tokens": per[run8.name][2],
    }


def rc_g13() -> dict:
    sentinel = base = residual = 0
    residual_examples: list[list] = []
    by_cli: dict[str, int] = {}
    for path in NATIVE:
        rows = jsonl(path)
        cli = first_meta(rows).get("cli_version")
        _, base_total = baseline_of(rows)
        for i, obj in enumerate(rows, 1):
            tu = usage(obj)
            if tu is None:
                continue
            if phantom(tu):
                if tu["total_tokens"] == context_window(obj):
                    sentinel += 1
                elif base_total is not None and tu["total_tokens"] == base_total:
                    base += 1
                else:
                    residual += 1
                    residual_examples.append([path.name, i, tu["total_tokens"]])
            last = (pl(obj).get("info") or {}).get("last_token_usage")
            if isinstance(last, dict) and zeroed(last) and (last.get("total_tokens") or 0) > 0:
                by_cli[cli] = by_cli.get(cli, 0) + 1
    return {
        "rollouts_scanned": len(NATIVE),
        "phantom_rows": sentinel + base + residual,
        "sentinel_rows": sentinel,
        "import_baseline_rows": base,
        "residual_rows": residual,
        "residual_examples": residual_examples[:5],
        "last_token_usage_zero_positive_by_cli_version": dict(sorted(by_cli.items())),
    }


def rc_g14() -> dict:
    entries = []
    for path in NATIVE:
        rows = jsonl(path)
        meta = first_meta(rows)
        first = last = None
        for obj in rows:
            tu = usage(obj)
            if tu is not None and spent(tu):
                first = tu["total_tokens"] if first is None else first
                last = tu["total_tokens"]
        entries.append((meta.get("id"), meta.get("session_id"), meta.get("forked_from_id"), first, last))
    non_self = [[i, f] for i, s, f, _, _ in entries if f and f not in (s, i)]
    self_forks = [i for i, s, f, _, _ in entries if f and f == s]
    naive = sum(e[4] for e in entries if e[4])
    groups: dict[int, int] = {}
    for _, _, _, first, last in entries:
        if last is None:
            continue
        groups[first] = max(groups.get(first, 0), last)
    dedup = sum(groups.values())
    return {
        "rollouts_scanned": len(NATIVE),
        "rollouts_with_real_usage": sum(1 for e in entries if e[4] is not None),
        "non_self_fork_count": len(non_self),
        "non_self_fork_pairs": sorted(non_self),
        "self_fork_by_session_id_count": len(self_forks),
        "self_fork_by_session_id_ids": sorted(self_forks),
        "naive_last_total_sum": naive,
        "lineage_dedup_total": dedup,
        "lineage_groups": len(groups),
        "inflation_ratio": round((naive - dedup) / dedup, 6) if dedup else None,
    }


def rc_g15() -> dict:
    hits = [p for p in FIXTURES.rglob("rollout-*.jsonl") if import_lines(jsonl(p))]
    return {
        "rollouts_with_import_turn_prefix": len(hits),
        "manifest_import_samples": MANIFEST["import_samples"],
    }


RECOMPUTE = {
    "G1_import_then_resume": rc_g1,
    "G2_import_never_resumed": rc_g2,
    "G3_fork_replay": rc_g3,
    "G4_self_fork": rc_g4,
    "G5_ctx_sentinel": rc_g5,
    "G6_subagent_id_trap": rc_g6,
    "G7_mcp_call_shape": rc_g7,
    "G8_system_session": rc_g8,
    "G9_unpersisted_session": rc_g9,
    "G10_hook_payload_shape": rc_g10,
    "G11_subagent_reach": rc_g11,
    "G12_baseline_0142": rc_g12,
    "G13_phantom_partition": rc_g13,
    "G14_fork_lineage_dedup": rc_g14,
    "G15_import_predicate_count": rc_g15,
}


def test_every_verifiable_golden_has_a_recomputation():
    verifiable = {it["name"] for it in GOLDEN["items"] if it["fixture_verifiable"]}
    assert verifiable - set(RECOMPUTE) == set(), "有可复算的 golden 条目没写复算实现"
    assert set(RECOMPUTE) - {it["name"] for it in GOLDEN["items"]} == set(), "复算实现指向不存在的 golden"


@pytest.mark.parametrize("name", sorted(RECOMPUTE))
def test_golden_fixture_values_recompute(name):
    item = golden_item(name)
    assert item["fixture_verifiable"] is True, f"{name} 标了不可夹具复算，却写了复算实现"
    expected = item["fixture_values"]
    actual = RECOMPUTE[name]()
    assert set(expected) == set(actual), (
        f"{name} 键集不符：复算缺 {sorted(set(expected) - set(actual))}，"
        f"golden 缺 {sorted(set(actual) - set(expected))}——删键等于悄悄撤断言")
    for key in expected:
        assert actual[key] == expected[key], f"{name}.{key} 复算不一致"


def test_golden_items_are_well_formed():
    names = [it["name"] for it in GOLDEN["items"]]
    assert len(names) == len(set(names)) == 15
    for item in GOLDEN["items"]:
        assert set(item) >= {"name", "sources", "fixture_files", "method", "values", "fixture_verifiable"}
        assert item["sources"] and item["method"] and item["values"]
        if item["fixture_verifiable"]:
            assert item.get("fixture_values"), f"{item['name']} 标了可夹具复算却没有 fixture_values"
        for rel in item["fixture_files"]:
            assert (FIXTURES / rel).exists(), f"{item['name']} 指向不存在的夹具文件 {rel}"


# --------------------------------------------------------- 交叉核对与形状锚点


def test_import_samples_matches_manifest():
    hits = [p.name for p in FIXTURES.rglob("rollout-*.jsonl") if import_lines(jsonl(p))]
    assert len(hits) == MANIFEST["import_samples"]


def test_run8_tool_response_lengths_are_preserved():
    """H6 大字符串保留精确字节长度：0.142 run8 的两条 tool_response 必须恒为同一长度。

    原串含多字节字符（40106 字符 / 40110 字节），filler 按字节长度生成，
    因此夹具里是 40110 个 ASCII 字符。
    """
    values = [pl(obj)["tool_response"]
              for obj in jsonl(FIXTURES / "hooks/probe-0142/capture-run8-compact.jsonl")
              if isinstance(pl(obj).get("tool_response"), str) and len(pl(obj)["tool_response"]) > 1024]
    assert [len(v) for v in values] == [40110, 40110]
    assert all(set(v) <= set("<filler>") for v in values), "大字符串必须换成 filler，不得留原文"


FERNET_RE = re.compile(r"gAAAAA[A-Za-z0-9_\-=]+")


def test_dispatch_ciphertext_is_kept_verbatim():
    """H7：派工正文以密文原样入库——夹具因此能断言 Codex 侧拿到的就是不可读的密文。"""
    hits = []
    for path in data_files():
        if path.name in PROSE_FILES:
            continue
        for doc in json_docs(path):
            for key, value in walk(doc):
                if isinstance(value, str) and FERNET_RE.fullmatch(value):
                    hits.append((str(path.relative_to(FIXTURES)), key, len(value)))
    by_key: dict[str, int] = {}
    for _, key, _ in hits:
        by_key[key] = by_key.get(key, 0) + 1
    # 逐位置钉死：只数总量的话，某一处退化成 <str:N> 会被其他处的数量掩盖。
    assert by_key == {"message": 12, "encrypted_content": 28}, \
        f"密文分布变了：{by_key}——H7 在某个位置上失效了"
    assert all(size >= 100 for _, _, size in hits), "Fernet token 最短也有百余字符，太短说明不是密文"
    dispatch = {path for path, key, _ in hits if key == "message"}
    assert len(dispatch) == 3, "派工正文密文应来自三份 hook 抓取"


def test_compacted_message_keeps_exact_length_filler():
    """H9：压缩摘要不写 <str:N> 而是等长 filler——长度是可比对的结构信息。"""
    seen = []
    for path in FIXTURES.rglob("rollout-*.jsonl"):
        for obj in jsonl(path):
            if obj.get("type") == "compacted":
                msg = pl(obj).get("message")
                assert isinstance(msg, str) and set(msg) <= set("<filler>"), \
                    f"{path.name} 的 compacted.message 不是 filler"
                seen.append(len(msg))
    assert seen, "夹具里应至少有一条 compacted 行供核对"


# ---------------------------------------- 5. 每个生产口径键的归属声明（I19 ③）

# 三种归属，含义各不相同，选哪一种就是在声明"这个数改一位时靠什么发现"：
#
#   CROSS           夹具侧有独立复算，且与生产口径同名同值——改一位当场红。
#   IDENTITY        夹具侧算不出，但 values 内部有恒等式约束它（IDENTITY_CHECKS）。
#                   约束强弱不等：有的把值钉死（run8 那份的 total 必须等于表里的值），
#                   有的只框住范围（导入标记行必须落在导入段内）。
#   PRODUCTION_ONLY 两样都没有。**必须写明理由**——"改一位不会红"是已知且被接受的事实，
#                   写下来是为了下一个人知道它没被保护，而不是以为它被保护着。
#
# 声明集与磁盘上的 values 键集**双向相等**（test_every_production_value_key_has_a_scope）：
# 删一个 golden 键会红（声明了却不存在），加一个不声明也会红。这是 ③ 的要害——
# 只比同名键的老写法漏掉了全部生产独有键，改一位不会红。

CROSS = "cross"
IDENTITY = "identity"
PRODUCTION_ONLY = "production_only"


def scopes(cross=(), identity=(), production_only=()) -> dict[str, tuple[str, str]]:
    """把一项 golden 的键归属写成登记表；production_only 的理由是必填项。"""
    out: dict[str, tuple[str, str]] = {}
    for key in cross:
        out[key] = (CROSS, "")
    for key in identity:
        out[key] = (IDENTITY, "")
    for key, reason in production_only:
        assert reason, f"{key}: production_only 必须写明理由"
        out[key] = (PRODUCTION_ONLY, reason)
    return out


_SCOPE_BASE = "总体基数：夹具只收了生产总体里的一部分原生份，两边分母天然不同，不可交叉核对。"

ASSERT_SCOPE: dict[str, dict[str, tuple[str, str]]] = {
    "G1_import_then_resume": scopes(
        cross=("import_segment_first_line", "import_turn_count", "import_baseline_total",
               "identity_violations", "last_real_total_token_usage"),
        identity=("rollout_lines", "import_segment_last_line", "import_segment_lines",
                  "import_marker_line", "import_baseline_line", "identity_checked_rows",
                  "os_events_inside_post_import_span"),
        production_only=(
            ("os_event_rows", "OS 库口径；夹具不带 OS 库快照，条数无从复算，"
                              "只有『整体落在导入后段』那条布尔被恒等式钉住。"),
        ),
    ),
    "G2_import_never_resumed": scopes(
        cross=("real_token_count_rows",),
        identity=("rollout_lines", "import_segment_first_line", "import_segment_last_line",
                  "import_turn_rows", "import_turn_count", "import_marker_line",
                  "import_baseline_line", "import_baseline_total", "rows_after_import_segment"),
        production_only=(
            ("os_event_rows", "OS 库口径；该会话在 OS 库里零事件，夹具无从复算。"),
        ),
    ),
    "G3_fork_replay": scopes(
        cross=("id", "session_id", "forked_from_id", "parent_rollout",
               "own_last_real_total", "parent_last_real_total", "totals_equal"),
    ),
    "G4_self_fork": scopes(
        cross=("id", "session_id", "forked_from_id", "forked_from_equals_session_id",
               "forked_from_equals_id", "self_fork_by_session_id_ids"),
        identity=("rollout", "self_fork_by_session_id_count", "self_fork_by_id_count"),
        production_only=(("rollouts_scanned", _SCOPE_BASE),),
    ),
    "G5_ctx_sentinel": scopes(
        cross=("token_count_rows", "sentinel_rows", "sentinel_context_windows",
               "last_real_total_token_usage"),
        production_only=(
            ("last_non_sentinel_line", "行号：夹具份被裁过行，同一条真值行在两边的行号必然不同，"
                                       "不可交叉核对；values 内部也没有能钉住它的恒等式。"),
        ),
    ),
    "G6_subagent_id_trap": scopes(
        cross=("id", "session_id", "id_equals_session_id", "parent_thread_id", "source"),
    ),
    "G7_mcp_call_shape": scopes(
        cross=("custom_tool_call", "function_call", "mcp_tool_call_end",
               "response_item_calls_total"),
    ),
    "G8_system_session": scopes(
        cross=("event_type_counts", "cc_session_start", "tool_events"),
        identity=("session_id_prefix", "cwd_under_codex_home_memories"),
    ),
    "G9_unpersisted_session": scopes(
        cross=("cc_tool_use", "present_in_rollouts"),
        identity=("session_ids", "event_type_counts"),
        production_only=(
            ("present_in_state_threads", "生产口径查的是本机 state_5 线程全表；夹具只有 6 行抽样，"
                                         "夹具侧的 present_in_state_sample 是另一个判据，"
                                         "两者不得等同（G9 的覆盖面差已在 method 里登记）。"),
        ),
    ),
    "G10_hook_payload_shape": scopes(
        cross=("probe_0142", "probe_0152"),
    ),
    "G11_subagent_reach": scopes(
        cross=("threads_total", "thread_spawn_thread_ids",
               "subagent_distribution_total", "explained_by_thread_spawn"),
        identity=("other_bucket_counts", "explained_by_other_bucket", "unexplained_vscode_source"),
        production_only=(
            ("os_agents_rows_with_matching_cc_tool_use_id",
             "须查 OS 库 agents 表，CI 上与夹具里都没有。这是 I13 subagent 桶的冻结基线，"
             "生产滚动桶由 check_usage_coverage.py 动态计算，两桶分列同屏、禁相加。"),
        ),
    ),
    "G12_baseline_0142": scopes(
        cross=("rollouts", "last_total_token_usage_by_rollout", "run8_last_total_tokens"),
        identity=("run8_compaction_rollout",),
    ),
    "G13_phantom_partition": scopes(
        cross=("sentinel_rows", "residual_rows", "residual_examples"),
        identity=("phantom_rows", "import_baseline_rows",
                  "last_token_usage_zero_positive_by_cli_version"),
        production_only=(("rollouts_scanned", _SCOPE_BASE),),
    ),
    "G14_fork_lineage_dedup": scopes(
        cross=("rollouts_with_real_usage", "non_self_fork_count", "non_self_fork_pairs",
               "self_fork_by_session_id_count", "self_fork_by_session_id_ids",
               "naive_last_total_sum", "lineage_dedup_total", "lineage_groups",
               "inflation_ratio"),
        production_only=(("rollouts_scanned", _SCOPE_BASE),),
    ),
    "G15_import_predicate_count": scopes(
        cross=(),
        identity=("rollouts_with_import_turn_prefix", "external_import_records", "counts_match"),
        production_only=(("rollouts_scanned", _SCOPE_BASE),),
    ),
}


@pytest.mark.parametrize("name", sorted(ASSERT_SCOPE))
def test_every_production_value_key_has_a_scope(name):
    """values 的每个键都必须有归属声明，且声明不得指向不存在的键。"""
    declared = set(ASSERT_SCOPE[name])
    on_disk = set(golden_item(name)["values"])
    assert declared == on_disk, (
        f"{name} 归属声明与 golden 不符：未声明 {sorted(on_disk - declared)}，"
        f"声明了却不存在 {sorted(declared - on_disk)}——"
        "生产独有键无人声明时，改一位不会红")


def test_scope_registry_covers_every_golden_item():
    assert set(ASSERT_SCOPE) == {it["name"] for it in GOLDEN["items"]}
    total = sum(len(v) for v in ASSERT_SCOPE.values())
    assert total == sum(len(it["values"]) for it in GOLDEN["items"]) == 100


@pytest.mark.parametrize("name", sorted(ASSERT_SCOPE))
def test_cross_scope_keys_agree_across_readings(name):
    """声明为 cross 的键：夹具口径必须也有，且逐键相等。

    夹具里没有原件，无法全量核 values；但这些键的取值来源整份都在夹具内，
    两口径不一致只可能是有人手抄错了其中一边。
    """
    item = golden_item(name)
    keys = [k for k, (scope, _) in ASSERT_SCOPE[name].items() if scope == CROSS]
    fixture_values = item.get("fixture_values") or {}
    for key in sorted(keys):
        assert key in fixture_values, f"{name}.{key} 声明为 cross 却不在 fixture_values 里"
        assert item["values"][key] == fixture_values[key], f"{name}.{key} 两口径不一致"


# ------------------------------------- 6. values 内部恒等式（I19 ②，逐项登记）

# 每个检查函数返回它读过的键集。test_identity_scope_is_backed_by_an_assertion 拿这个
# 集合去对账：声明成 IDENTITY 却没人断言，红。这样"加个键、标成 identity、不写断言"
# 这条静默萎缩的路被堵死。


def _id_g1(v: dict) -> set[str]:
    assert v["import_segment_lines"] == v["import_segment_last_line"] - v["import_segment_first_line"] + 1
    assert v["import_segment_first_line"] <= v["import_marker_line"] < v["import_baseline_line"]
    assert v["import_baseline_line"] <= v["import_segment_last_line"]
    assert v["import_segment_last_line"] <= v["rollout_lines"]
    assert 0 < v["identity_checked_rows"] <= v["rollout_lines"]
    assert v["identity_violations"] == 0
    inp, out, total = v["last_real_total_token_usage"]
    assert total - (inp + out) == v["import_baseline_total"], "末条真值不满足导入基线恒等式"
    assert v["os_events_inside_post_import_span"] is True
    return {"import_segment_lines", "import_segment_last_line", "import_segment_first_line",
            "import_marker_line", "import_baseline_line", "rollout_lines",
            "identity_checked_rows", "identity_violations", "last_real_total_token_usage",
            "import_baseline_total", "os_events_inside_post_import_span"}


def _id_g2(v: dict) -> set[str]:
    # 从未续跑：导入段一直铺到文件末尾，之后一行都没有，也就没有任何真值行。
    assert v["import_segment_last_line"] == v["rollout_lines"]
    assert v["rows_after_import_segment"] == 0
    assert v["import_segment_first_line"] <= v["import_marker_line"] < v["import_baseline_line"]
    assert v["import_baseline_line"] <= v["import_segment_last_line"]
    assert 0 < v["import_turn_count"] <= v["import_turn_rows"]
    assert v["import_baseline_total"] > 0
    assert v["real_token_count_rows"] == 0, "从未续跑的份不该有真值行"
    return {"import_segment_last_line", "rollout_lines", "rows_after_import_segment",
            "import_segment_first_line", "import_marker_line", "import_baseline_line",
            "import_turn_count", "import_turn_rows", "import_baseline_total",
            "real_token_count_rows"}


def _id_g3(v: dict) -> set[str]:
    assert v["totals_equal"] == (v["own_last_real_total"] == v["parent_last_real_total"])
    assert v["totals_equal"] is True
    assert v["forked_from_id"] and v["forked_from_id"] != v["id"]
    return {"totals_equal", "own_last_real_total", "parent_last_real_total",
            "forked_from_id", "id"}


def _id_g4(v: dict) -> set[str]:
    assert v["forked_from_equals_session_id"] is True and v["forked_from_equals_id"] is False
    assert v["forked_from_id"] == v["session_id"]
    assert v["id"] in v["rollout"], "取值那一份的文件名必须带该 id——标号是父 session_id，别拿去找文件"
    assert v["self_fork_by_session_id_count"] == len(v["self_fork_by_session_id_ids"])
    assert v["id"] in v["self_fork_by_session_id_ids"]
    assert v["self_fork_by_id_count"] == 0, "按 id 自指者实测 0 份；非 0 说明判据被改宽了"
    return {"forked_from_equals_session_id", "forked_from_equals_id", "forked_from_id",
            "session_id", "id", "rollout", "self_fork_by_session_id_count",
            "self_fork_by_session_id_ids", "self_fork_by_id_count"}


def _id_g5(v: dict) -> set[str]:
    assert v["sentinel_rows"] < v["token_count_rows"]
    assert len(v["sentinel_context_windows"]) == 1
    inp, out, total = v["last_real_total_token_usage"]
    assert inp + out > 0 and total not in v["sentinel_context_windows"]
    return {"sentinel_rows", "token_count_rows", "sentinel_context_windows",
            "last_real_total_token_usage"}


def _id_g6(v: dict) -> set[str]:
    assert v["id_equals_session_id"] is False and v["parent_thread_id"] == v["session_id"]
    assert v["id"] != v["session_id"]
    return {"id_equals_session_id", "parent_thread_id", "session_id", "id"}


def _id_g7(v: dict) -> set[str]:
    assert v["response_item_calls_total"] == v["custom_tool_call"] + v["function_call"]
    assert v["response_item_calls_total"] != v["mcp_tool_call_end"], \
        "两口径若相等，G7 想证的『禁混』就没有语料背书了"
    return {"response_item_calls_total", "custom_tool_call", "function_call", "mcp_tool_call_end"}


def _id_g8(v: dict) -> set[str]:
    assert v["cc_session_start"] == v["event_type_counts"].get("cc.session_start")
    assert v["tool_events"] == (v["event_type_counts"].get("cc.tool_use", 0)
                                + v["event_type_counts"].get("cc.tool_complete", 0)) == 0
    assert (FIXTURES / "os-events" / f"{v['session_id_prefix']}.jsonl").is_file(), \
        "session_id_prefix 必须指向夹具里那份 OS 事件样本"
    assert v["cwd_under_codex_home_memories"] is True
    return {"cc_session_start", "event_type_counts", "tool_events",
            "session_id_prefix", "cwd_under_codex_home_memories"}


def _id_g9(v: dict) -> set[str]:
    assert v["cc_tool_use"] == v["event_type_counts"]["cc.tool_use"]
    assert len(v["session_ids"]) == 1
    stem = v["session_ids"][0].split("-")[0]
    assert (FIXTURES / "os-events" / f"{stem}.jsonl").is_file()
    assert v["present_in_rollouts"] is False and v["present_in_state_threads"] is False
    return {"cc_tool_use", "event_type_counts", "session_ids",
            "present_in_rollouts", "present_in_state_threads"}


def _id_g10(v: dict) -> set[str]:
    for probe in ("probe_0142", "probe_0152"):
        shape = v[probe]
        assert shape["spawn_tool_namespaced"] == (shape["spawn_tool_name"] != "spawn_agent")
        assert shape["start_transcript_is_child"] is True
        assert shape["stop_transcript_is_parent"] is True
        assert shape["stop_agent_transcript_is_child"] is True
        assert shape["subagent_start"]["agent_id"] == shape["subagent_stop"]["agent_id"]
        assert shape["subagent_start"]["session_id"] == shape["subagent_stop"]["session_id"]
    assert v["probe_0142"]["spawn_tool_namespaced"] != v["probe_0152"]["spawn_tool_namespaced"], \
        "两代探针的派工工具名形态不同，正是 matcher 不能写模型面全名的语料背书"
    return {"probe_0142", "probe_0152"}


def _id_g11(v: dict) -> set[str]:
    # 闭合：subagent 桶的每一条都要有归属，差额就是第三类（source 不是 subagent JSON 串）。
    assert v["explained_by_other_bucket"] == sum(v["other_bucket_counts"].values())
    assert v["explained_by_thread_spawn"] == len(v["thread_spawn_thread_ids"])
    assert (v["subagent_distribution_total"]
            == v["explained_by_thread_spawn"]
            + v["explained_by_other_bucket"]
            + v["unexplained_vscode_source"]), "subagent 桶未被三类穷尽"
    assert v["unexplained_vscode_source"] > 0, \
        "第三类是 G11 的要害；归零说明桶口径被改宽了，缺口就此没人看着"
    assert v["subagent_distribution_total"] <= v["threads_total"]
    return {"explained_by_other_bucket", "other_bucket_counts", "explained_by_thread_spawn",
            "thread_spawn_thread_ids", "subagent_distribution_total",
            "unexplained_vscode_source", "threads_total"}


def _id_g12(v: dict) -> set[str]:
    per = v["last_total_token_usage_by_rollout"]
    assert v["rollouts"] == len(per)
    assert v["run8_compaction_rollout"] in per, "run8 那份必须在逐份表里"
    assert per[v["run8_compaction_rollout"]][2] == v["run8_last_total_tokens"]
    return {"rollouts", "last_total_token_usage_by_rollout",
            "run8_compaction_rollout", "run8_last_total_tokens"}


def _id_g13(v: dict) -> set[str]:
    assert v["phantom_rows"] == v["sentinel_rows"] + v["import_baseline_rows"] + v["residual_rows"]
    assert v["residual_rows"] == 0, "幻影行未被两条判据穷尽"
    assert len(v["residual_examples"]) == v["residual_rows"]
    by_cli = v["last_token_usage_zero_positive_by_cli_version"]
    assert by_cli and set(by_cli) <= set(GOLDEN["population"]["cli_versions"]), \
        "per-cli 分桶出现了冻结总体之外的版本——总体 pin 与该键必须同源"
    assert all(n > 0 for n in by_cli.values())
    return {"phantom_rows", "sentinel_rows", "import_baseline_rows", "residual_rows",
            "residual_examples", "last_token_usage_zero_positive_by_cli_version"}


def _id_g14(v: dict) -> set[str]:
    assert v["lineage_dedup_total"] <= v["naive_last_total_sum"]
    assert v["inflation_ratio"] == round(
        (v["naive_last_total_sum"] - v["lineage_dedup_total"]) / v["lineage_dedup_total"], 6)
    assert v["non_self_fork_count"] == len(v["non_self_fork_pairs"])
    assert v["self_fork_by_session_id_count"] == len(v["self_fork_by_session_id_ids"])
    assert 0 < v["lineage_groups"] <= v["rollouts_with_real_usage"] <= v["rollouts_scanned"]
    return {"lineage_dedup_total", "naive_last_total_sum", "inflation_ratio",
            "non_self_fork_count", "non_self_fork_pairs", "self_fork_by_session_id_count",
            "self_fork_by_session_id_ids", "lineage_groups", "rollouts_with_real_usage",
            "rollouts_scanned"}


def _id_g15(v: dict) -> set[str]:
    assert v["rollouts_with_import_turn_prefix"] == v["external_import_records"]
    assert v["counts_match"] is True
    assert 0 < v["rollouts_with_import_turn_prefix"] <= v["rollouts_scanned"]
    return {"rollouts_with_import_turn_prefix", "external_import_records",
            "counts_match", "rollouts_scanned"}


IDENTITY_CHECKS = {
    "G1_import_then_resume": _id_g1,
    "G2_import_never_resumed": _id_g2,
    "G3_fork_replay": _id_g3,
    "G4_self_fork": _id_g4,
    "G5_ctx_sentinel": _id_g5,
    "G6_subagent_id_trap": _id_g6,
    "G7_mcp_call_shape": _id_g7,
    "G8_system_session": _id_g8,
    "G9_unpersisted_session": _id_g9,
    "G10_hook_payload_shape": _id_g10,
    "G11_subagent_reach": _id_g11,
    "G12_baseline_0142": _id_g12,
    "G13_phantom_partition": _id_g13,
    "G14_fork_lineage_dedup": _id_g14,
    "G15_import_predicate_count": _id_g15,
}


@pytest.mark.parametrize("name", sorted(IDENTITY_CHECKS))
def test_production_values_are_internally_consistent(name):
    """values 内部本可廉价机检的恒等式——手抄错一个数就该在这里红。"""
    consumed = IDENTITY_CHECKS[name](golden_item(name)["values"])
    assert consumed <= set(golden_item(name)["values"]), f"{name} 的恒等式读了不存在的键"


@pytest.mark.parametrize("name", sorted(ASSERT_SCOPE))
def test_identity_scope_is_backed_by_an_assertion(name):
    """声明成 identity 的键，必须真的被某条恒等式读到——否则归属是空头支票。"""
    declared = {k for k, (scope, _) in ASSERT_SCOPE[name].items() if scope == IDENTITY}
    consumed = IDENTITY_CHECKS[name](golden_item(name)["values"])
    assert declared <= consumed, f"{name} 声明为 identity 却无人断言：{sorted(declared - consumed)}"


# ------------------------------------------ 7. MANIFEST 口径标注（I19 ⑤）


def test_row_type_counts_scope_is_declared():
    """kept/dropped_row_types 是脱敏阶段口径，与磁盘裁剪结果不同。

    该字段只是"存在"还不够：没有断言时删掉它不会红，读 MANIFEST 的人就会把裁剪后的
    行数当成登记值去对，两边永远对不上还找不到原因。
    """
    scope = MANIFEST.get("row_type_counts_scope")
    assert isinstance(scope, str) and scope.strip(), "MANIFEST 必须显式标注行类型计数的口径"
    assert "redaction" in scope and "trim_rule" in scope, \
        f"口径说明须写明是脱敏阶段、裁剪之前：{scope!r}"
    counted = [e for e in MANIFEST["files"] if e.get("kept_row_types")]
    assert counted, "至少要有登记了 kept_row_types 的条目，否则该口径标注无所指"


# --------------------------------------- 8. 采集器绑定与总体 pin（I19 首句）


def test_golden_records_its_generator_and_manifest():
    """golden 必须钉住"谁算的、算在哪版夹具上"——否则改采集器不会让任何东西红。"""
    generator = GOLDEN["generator"]
    assert generator["script"] == "scripts/compute_codex_golden.py"
    assert re.fullmatch(r"[0-9a-f]{64}", generator["sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", GOLDEN["manifest_sha256"])
    # 反向不成立：MANIFEST 不登记 golden，否则两份互记成环谁都改不动。
    assert "golden.json" not in {e["path"] for e in MANIFEST["files"]}
    assert "golden.json" in UNREGISTERED_OK


def test_golden_population_is_pinned():
    """生产口径的总体必须钉死在几个 cli_version 上。

    本机 Codex 目录一直在长。总体不钉死，每次重算都会把新会话算进 rollouts_scanned
    与谱系求和，冻结基线在无人察觉的情况下变值——数字照样像模像样，只是不再可比。
    """
    population = GOLDEN["population"]
    versions = population["cli_versions"]
    assert versions == sorted(set(versions)) and len(versions) >= 1
    assert all(re.fullmatch(r"\d+\.\d+\.\d+(-[A-Za-z0-9.]+)?", v) for v in versions)
    assert population["rollouts"] > 0
    assert population["note"].strip()


# ---------------------------------------- 9. 版本字面量单一常量（I-CDX-R8(b)(c)）

SURFACE = Path(__file__).resolve().parents[2] / "plugin" / "harness" / "codex" / "surface.py"
VERSION_LITERAL = re.compile(r"\b\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?\b")


def surface_constant(name: str) -> str | None:
    """surface.py 里登记的版本常量，允许带类型注解写法。"""
    if not SURFACE.exists():
        return None
    hit = re.search(rf'^{name}\s*(?::[^=]+)?=\s*"([^"]+)"',
                    SURFACE.read_text(encoding="utf-8"), re.MULTILINE)
    return hit.group(1) if hit else None


def registered_versions() -> set[str]:
    """允许出现的全部版本字面量，三处登记面的并集。

    冻结总体（golden.population）管原生语料；MANIFEST 的 codexhome_roots 管探针语料
    ——每个探针 Codex home 都在那里写明是哪一版；surface.py 的两个常量管适配器面。
    第四处出现的版本号就是散落字面量：上游滚一次版没人找得全。
    """
    allowed = set(GOLDEN["population"]["cli_versions"])
    for root in MANIFEST["codexhome_roots"].values():
        allowed |= set(VERSION_LITERAL.findall(root))
    for name in ("CODEX_MIN_VERSION", "CODEX_KNOWN_UPPER_VERSION"):
        value = surface_constant(name)
        if value:
            allowed.add(value)
    return allowed


def test_golden_carries_no_stray_version_literal():
    """golden 里的版本字面量只能是登记过的那几个。

    已知上界是滚动值：散落的字面量每次上游发版都要人肉找一遍，找漏一处就是一条
    对着旧版本断言、却永远绿着的死判据。
    """
    allowed = registered_versions()
    text = (FIXTURES / "golden.json").read_text(encoding="utf-8")
    stray = sorted({v for v in VERSION_LITERAL.findall(text)} - allowed)
    assert stray == [], f"golden 里有未登记的版本字面量：{stray}（登记面见 {SURFACE.name} 与总体 pin）"


@pytest.mark.skipif(not SURFACE.exists(), reason="plugin/harness/codex/surface.py 尚未落地")
def test_codex_version_constants_are_registered():
    """R8(b)：版本常量必须写在 surface.py 里，且没有版本号越过已知上界。

    不断言"冻结总体落在 [min, upper] 内"：0.142 那份对照语料本来就在支持下界之前，
    正是它让 G12 的基线有得比。真正的红线是上界——已知上界是滚动值，任何比它还新的
    版本号出现在夹具里，都说明有人手抄了一个没人回来刷新的字面量。
    """
    found = {}
    for const in ("CODEX_MIN_VERSION", "CODEX_KNOWN_UPPER_VERSION"):
        value = surface_constant(const)
        assert value, f"surface.py 必须登记 {const}"
        found[const] = value
    upper = _version_key(found["CODEX_KNOWN_UPPER_VERSION"])
    assert _version_key(found["CODEX_MIN_VERSION"]) < upper
    for version in sorted(registered_versions()):
        assert _version_key(version) <= upper, f"{version} 比登记的已知上界还新"


def _version_key(version: str) -> tuple:
    """0.145.0-alpha.30 → 可比较的元组；预发布段一律排在同号正式版之前。"""
    head, _, tail = version.partition("-")
    numbers = tuple(int(part) for part in head.split("."))
    return numbers, (0, tail) if tail else (1, "")


# ------------------------------------------------- 10. method 措辞（P-1 残留）

# 核验路读出来的四处口径说明缺口：值都是对的，写法会让下一个人对错表。
# 措辞写进 method 就得有人看着，否则改回去不会红。
METHOD_PHRASES = {
    "G14_fork_lineage_dedup": ("只对有真值的份进行", "照字面在全部份上分组会多出空组"),
    "G1_import_then_resume": ("整份里每条非幻影 token_count 行", "不限于导入段之后"),
    "G4_self_fork": ("是被继承的父 session_id", "取值那一份是子线程"),
    "G9_unpersisted_session": ("覆盖面差须显式登记", "生产口径独有"),
}


@pytest.mark.parametrize("name", sorted(METHOD_PHRASES))
def test_method_records_the_scope_caveat(name):
    method = golden_item(name)["method"]
    for phrase in METHOD_PHRASES[name]:
        assert phrase in method, f"{name} 的 method 少了口径说明：{phrase!r}"


@pytest.mark.parametrize("name", sorted(ASSERT_SCOPE))
def test_population_scoped_items_state_their_denominator(name):
    """凡总体基数进了 values 的条目，method 必须写明总体是什么、且钉死不滚动。"""
    if "rollouts_scanned" not in golden_item(name)["values"]:
        return
    method = golden_item(name)["method"]
    assert "全量口径的总体定义" in method and "总体是钉死的" in method, \
        f"{name} 用了总体基数却没在 method 里写明总体口径"
