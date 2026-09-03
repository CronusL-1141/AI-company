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
}


@pytest.mark.parametrize("label", sorted(FORBIDDEN_PATTERNS))
def test_forbidden_literals_absent(label):
    pattern = re.compile(FORBIDDEN_PATTERNS[label])
    hits = []
    for path in data_files():
        match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            hits.append(f"{path.relative_to(FIXTURES)}: {match.group(0)[:24]}")
    assert hits == []


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
    sample = json.loads((FIXTURES / "state" / "threads.sample.json").read_text(encoding="utf-8"))
    spawn_row = next((e["row"]["id"] for e in sample["threads_sample"] if e["sample"] == "thread-spawn"), None)
    return {
        "threads_total": sample.get("threads_total"),
        "thread_spawn_thread_ids": sorted(e["child_thread_id"] for e in sample["thread_spawn_edges"]),
        "thread_spawn_sample_id": spawn_row,
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
    assert set(expected) <= set(actual), f"{name} 复算缺少键：{sorted(set(expected) - set(actual))}"
    for key in expected:
        assert actual[key] == expected[key], f"{name}.{key} 复算不一致"


def test_golden_items_are_well_formed():
    names = [it["name"] for it in GOLDEN["items"]]
    assert len(names) == len(set(names)) == 15
    for item in GOLDEN["items"]:
        assert set(item) >= {"name", "sources", "fixture_files", "method", "values", "fixture_verifiable"}
        assert item["sources"] and item["method"] and item["values"]
        for rel in item["fixture_files"]:
            assert (FIXTURES / rel).exists(), f"{item['name']} 指向不存在的夹具文件 {rel}"


# --------------------------------------------------------- 交叉核对与形状锚点


def test_import_samples_matches_manifest():
    hits = [p.name for p in FIXTURES.rglob("rollout-*.jsonl") if import_lines(jsonl(p))]
    assert len(hits) == MANIFEST["import_samples"]


def test_run8_tool_response_lengths_are_preserved():
    """H6 大字符串保留精确长度：0.142 run8 的两条 tool_response 必须恒为同一长度。"""
    lengths = [len(pl(obj)["tool_response"])
               for obj in jsonl(FIXTURES / "hooks/probe-0142/capture-run8-compact.jsonl")
               if isinstance(pl(obj).get("tool_response"), str) and len(pl(obj)["tool_response"]) > 1024]
    assert lengths == [40106, 40106]
