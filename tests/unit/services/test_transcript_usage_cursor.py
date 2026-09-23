"""增量游标 ``TranscriptUsageCursor``：成本只与新增字节成正比，结果必须与整份解析逐字段相等。

主会话 transcript 只追加不截断，490MB 的会话整份解析约 2s。游标记住"读到哪了"，下次
只解析新增部分。它能出错的地方恰好都在边界上，所以这里的钉子全是边界：

* **行中间截断**：写入方写到一半的行不能被消费，否则半行 JSON 被丢弃、那次调用的
  用量永远漏记；
* **跨批的同一 requestId**：流式行的 output 是递增快照，跨两次推进也必须是替换不是相加；
* **无 requestId 的行**：兜底键用绝对字节偏移 —— 用行号的话，分批读时行号要么得另存，
  要么在两批之间撞键把两次调用误合并；
* **文件身份与内容变化**：换文件（inode 变）、变短、锚点内容变了，一律整份重解析。

性质测试：随机内容、随机截断点（含行中间）、随机分批续写 —— 每一步的增量结果都等于
"截至最后一个完整换行"那部分内容的整份解析，最后一步等于全文整份解析。
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest

from aiteam.services import token_attribution
from aiteam.services.token_attribution import TranscriptUsageCursor, parse_transcript_usage


def _assistant(req: str | None, model: str, inp: int, out: int, cc: int, cr: int, *, compact: bool) -> str:
    row: dict = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
            },
            "content": [{"type": "text", "text": "tool said \"usage\" and 'assistant' here"}],
        },
    }
    if req is not None:
        row["requestId"] = req
    # 两种写法都要覆盖：CC 写紧凑 JSON，json.dumps 默认带空格
    return json.dumps(row, separators=(",", ":")) if compact else json.dumps(row)


def _random_transcript(rng: random.Random, rows: int) -> bytes:
    lines: list[str] = []
    live_requests: list[str] = []
    for i in range(rows):
        kind = rng.random()
        compact = rng.random() < 0.5
        if kind < 0.15:
            lines.append(json.dumps({"type": "user", "message": {"content": f"turn {i} with \"usage\""}}))
        elif kind < 0.2:
            lines.append("not json at all {")
        elif kind < 0.23:
            lines.append("")
        elif kind < 0.27:
            # user 行里真带 usage 对象（子 agent 结果回执）：预筛放行、解析后照样不计
            lines.append(json.dumps({"type": "user", "toolUseResult": {"usage": {"input_tokens": 999}}}))
        elif kind < 0.32:
            lines.append(_assistant(None, "claude-opus-5", rng.randint(0, 50), rng.randint(0, 50), 0, 0,
                                    compact=compact))
        else:
            # 同一 requestId 可能紧挨着出现，也可能隔很远再出现（流式快照递增）
            if live_requests and rng.random() < 0.6:
                req = rng.choice(live_requests[-4:])
            else:
                req = f"req_{i}"
                live_requests.append(req)
            model = "<synthetic>" if rng.random() < 0.05 else rng.choice(["claude-opus-5", "claude-fable-5-1"])
            lines.append(_assistant(req, model, rng.randint(1, 9), rng.randint(1, 5000),
                                    rng.randint(0, 3000), rng.randint(0, 90000), compact=compact))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _full(path: Path, content: bytes) -> dict | None:
    path.write_bytes(content)
    return parse_transcript_usage(path)


@pytest.mark.parametrize("seed", range(40), ids=lambda s: f"seed{s}")
def test_random_cuts_and_continuations_match_a_full_parse(tmp_path: Path, seed: int):
    rng = random.Random(seed)
    content = _random_transcript(rng, rows=rng.randint(5, 120))
    growing = tmp_path / "growing.jsonl"
    reference = tmp_path / "reference.jsonl"
    growing.write_bytes(b"")
    cursor = TranscriptUsageCursor()

    cuts = sorted(rng.sample(range(1, len(content)), k=min(6, len(content) - 1)))
    written = 0
    for cut in [*cuts, len(content)]:
        with growing.open("ab") as fh:  # 原地追加：inode 不变，游标必须接着读
            fh.write(content[written:cut])
        written = cut
        got = cursor.advance(growing)
        complete = content[: content.rfind(b"\n", 0, cut) + 1]
        assert got == _full(reference, complete), f"cut at {cut} of {len(content)}"
        assert cursor.offset == len(complete)

    assert cursor.advance(growing) == _full(reference, content)


def test_partial_line_is_not_consumed_until_its_newline_arrives(tmp_path: Path):
    line = _assistant("req_1", "claude-opus-5", 3, 40, 5, 60, compact=True).encode()
    path = tmp_path / "t.jsonl"
    path.write_bytes(line[:25])
    cursor = TranscriptUsageCursor()
    assert cursor.advance(path) is None
    assert cursor.offset == 0
    with path.open("ab") as fh:
        fh.write(line[25:])  # 整行写完但还没换行：仍是写到一半
    assert cursor.advance(path) is None
    with path.open("ab") as fh:
        fh.write(b"\n")
    got = cursor.advance(path)
    assert got is not None
    assert (got["input_tokens"], got["output_tokens"], got["api_calls"]) == (3, 40, 1)


def test_same_request_across_two_advances_replaces_instead_of_adding(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    path.write_text(_assistant("req_1", "claude-opus-5", 10, 3, 0, 100, compact=True) + "\n")
    cursor = TranscriptUsageCursor()
    assert cursor.advance(path)["output_tokens"] == 3
    with path.open("a") as fh:
        fh.write(_assistant("req_1", "claude-opus-5", 10, 583, 0, 100, compact=True) + "\n")
    got = cursor.advance(path)
    assert got["output_tokens"] == 583, "跨批的流式快照被相加了"
    assert got["input_tokens"] == 10
    assert got["api_calls"] == 1


def test_rows_without_request_id_never_collide_across_batches(tmp_path: Path):
    """兜底键是绝对字节偏移：两批各自的"第 0 行"不能被当成同一次调用。"""
    path = tmp_path / "t.jsonl"
    path.write_text(_assistant(None, "claude-opus-5", 1, 1, 0, 0, compact=True) + "\n")
    cursor = TranscriptUsageCursor()
    cursor.advance(path)
    with path.open("a") as fh:
        fh.write(_assistant(None, "claude-opus-5", 2, 2, 0, 0, compact=True) + "\n")
    got = cursor.advance(path)
    assert got["api_calls"] == 2
    assert got["input_tokens"] == 3
    assert got == parse_transcript_usage(path)


def test_replaced_file_is_reparsed_from_scratch(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    path.write_text(_assistant("req_old", "claude-opus-5", 100, 100, 0, 0, compact=True) + "\n")
    cursor = TranscriptUsageCursor()
    cursor.advance(path)
    replacement = tmp_path / "new.jsonl"
    replacement.write_text(
        _assistant("req_new", "claude-opus-5", 1, 2, 0, 0, compact=True) + "\n"
        + _assistant("req_new2", "claude-opus-5", 1, 2, 0, 0, compact=True) + "\n"
    )
    os.replace(replacement, path)  # 同名换文件：inode 变了
    got = cursor.advance(path)
    assert got == parse_transcript_usage(path)
    assert got["input_tokens"] == 2


def test_truncated_file_is_reparsed_from_scratch(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    rows = [_assistant(f"req_{i}", "claude-opus-5", 5, 5, 0, 0, compact=True) for i in range(3)]
    path.write_text("\n".join(rows) + "\n")
    cursor = TranscriptUsageCursor()
    cursor.advance(path)
    with path.open("r+b") as fh:  # 原地截短：inode 不变，但比偏移短
        fh.truncate(len(rows[0]) + 1)
    got = cursor.advance(path)
    assert got["api_calls"] == 1
    assert got == parse_transcript_usage(path)


def test_rewrite_near_the_cursor_is_caught_by_the_anchor(tmp_path: Path):
    """原地改写、长度不变：inode 与大小都看不出来，偏移前 64 字节的锚点能看出来。"""
    path = tmp_path / "t.jsonl"
    # usage 放在行尾，改写才落在锚点窗口里（窗口外的等长改写正是注释里写明的局限）
    line = '{"type":"assistant","requestId":"req_1","message":{"model":"claude-opus-5",' \
        '"usage":{"input_tokens":11,"output_tokens":22}}}\n'
    path.write_text(line)
    cursor = TranscriptUsageCursor()
    assert cursor.advance(path)["output_tokens"] == 22
    path.write_text(line.replace('"output_tokens":22', '"output_tokens":99'))
    got = cursor.advance(path)
    assert got["output_tokens"] == 99
    assert got == parse_transcript_usage(path)


def test_prefilter_is_spacing_agnostic():
    """预筛只认 ``"usage"`` / ``"assistant"`` 两个字面量，与键值间有无空格无关。"""
    spaced = json.dumps(
        {"type": "assistant", "requestId": "r", "message": {"model": "m", "usage": {"input_tokens": 4}}}
    ).encode()
    compact = spaced.replace(b": ", b":").replace(b", ", b",")
    assert b'"type": "assistant"' in spaced  # 前提：这确实是带空格的写法
    for raw in (spaced, compact):
        parsed = token_attribution._usage_row(raw + b"\n")  # noqa: SLF001
        assert parsed is not None
        assert parsed[1][0] == 4


def test_unreadable_file_raises_so_callers_can_tell_it_from_no_usage(tmp_path: Path):
    cursor = TranscriptUsageCursor()
    with pytest.raises(OSError):
        cursor.advance(tmp_path / "missing.jsonl")
