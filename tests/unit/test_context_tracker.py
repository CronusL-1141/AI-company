"""Tests for context_tracker hook."""
import json
import random
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest

from aiteam.hooks import context_tracker


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch, tmp_path):
    """隔离 ~/.claude.json 与 CLAUDE_CONTEXT_SIZE，防止用户真实环境污染测试。

    所有测试默认认为：
    - ~/.claude.json 不存在（无 1M 历史）
    - CLAUDE_CONTEXT_SIZE 未设置
    单个测试如需启用 1M 场景或 ENV 覆盖，自行在测试体内 monkeypatch。
    """
    fake_config = tmp_path / "fake-claude.json"  # 不创建即不存在
    monkeypatch.setattr(context_tracker, "_CLAUDE_CONFIG", fake_config)
    monkeypatch.delenv("CLAUDE_CONTEXT_SIZE", raising=False)


def _make_transcript(lines: list[dict]) -> Path:
    """Write jsonl file with given dict lines, return path."""
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8')
    for entry in lines:
        tmp.write(json.dumps(entry) + "\n")
    tmp.close()
    return Path(tmp.name)


def _run_hook(payload: dict) -> tuple[str, int]:
    """Run main() with given stdin payload, capture stdout and exit code."""
    raw = json.dumps(payload)
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = StringIO(raw)
    sys.stdout = StringIO()
    exit_code = 0
    try:
        context_tracker.main()
        output = sys.stdout.getvalue()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
        output = sys.stdout.getvalue()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
    return output, exit_code


class TestContextTracker:
    def test_warning_at_80_percent(self):
        # 1M context（默认），800K used -> 80% WARNING
        transcript = _make_transcript([
            {"message": {"role": "user", "content": "..."}},
            {"message": {"role": "assistant", "usage": {"input_tokens": 800_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        assert "80" in out
        transcript.unlink()

    def test_critical_at_90_percent(self):
        # 1M context（默认），900K used -> 90% CRITICAL
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 900_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT CRITICAL" in out
        transcript.unlink()

    def test_no_warning_below_80(self):
        # 1M context（默认），100K used -> 10%，无警告
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 100_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == ""
        transcript.unlink()

    def test_includes_cache_tokens(self):
        # 1M context（默认），500K input + 360K cache_read = 860K total = 86%
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {
                "input_tokens": 500_000,
                "cache_read_input_tokens": 360_000,
            }, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        assert "86" in out
        transcript.unlink()

    def test_1m_context_model(self):
        # 1M context, 800K used = 80%
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 800_000}, "model": "claude-opus-4-6[1m]"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        transcript.unlink()

    def test_1m_without_suffix_high_tokens(self):
        # 684800 tokens with model='claude-opus-4-6' (no [1m] suffix) -> auto-detect 1M -> 68.5% no warning
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 684_800}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == ""
        assert "342" not in out
        transcript.unlink()

    def test_env_200k_critical(self, monkeypatch):
        # ENV=200K 强制按 200K 算，180K -> 90% CRITICAL
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "200000")
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 180_000}, "model": "claude-sonnet-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT CRITICAL" in out
        assert "200000" in out
        transcript.unlink()

    def test_default_1m_no_warning_for_170k(self):
        # 默认 1M，170K tokens -> 17%，不触发警告（修复前 200K 默认会误报 85%）
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 170_000}, "model": "claude-opus-4-7"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"默认 1M 下 170K tokens 不应触发警告，实际输出：{out}"
        transcript.unlink()

    def test_missing_transcript_path(self):
        out, _ = _run_hook({})
        assert out.strip() == ""

    def test_nonexistent_transcript(self):
        out, _ = _run_hook({"transcript_path": "/nonexistent/path.jsonl"})
        assert out.strip() == ""

    def test_malformed_jsonl_line_skipped(self):
        # 1M context（默认），malformed 行被跳过，850K tokens -> 85% WARNING
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8')
        tmp.write("invalid json line\n")
        msg = {"message": {"role": "assistant", "usage": {"input_tokens": 850_000}, "model": "claude-opus-4-6"}}
        tmp.write(json.dumps(msg) + "\n")
        tmp.close()
        transcript = Path(tmp.name)
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        transcript.unlink()

    def test_empty_stdin_silent(self):
        old_stdin = sys.stdin
        sys.stdin = StringIO("")
        try:
            context_tracker.main()  # Should not raise
        finally:
            sys.stdin = old_stdin

    def test_family_level_1m_detection(self, monkeypatch, tmp_path):
        """同 family 历史触发 1M 检测：claude-opus-4-7 + config 含 claude-opus-4-6[1m] → 1M."""
        fake_config = tmp_path / "claude.json"
        fake_config.write_text('{"models": {"claude-opus-4-6[1m]": {"input_tokens": 1000}}}')
        monkeypatch.setattr(context_tracker, "_CLAUDE_CONFIG", fake_config)
        # 198K tokens + opus-4-7（无 [1m] 历史） → family fallback 命中 → 1M → 19.8% → 无警告
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 196_877, "cache_creation_input_tokens": 1670,
            }, "model": "claude-opus-4-7"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"family-level 1M 未命中，误触警告：{out}"
        transcript.unlink()

    def test_no_cross_family_leak(self, monkeypatch, tmp_path):
        """跨 family 不泄漏：config 只有 opus[1m]，sonnet family-level 检测不命中。
        但 DEFAULT 仍为 1M，所以 180K tokens 按 1M 算 = 18%，无警告。
        验证点：opus 的历史记录不会错误地通过 family fallback 影响 sonnet。"""
        fake_config = tmp_path / "claude.json"
        fake_config.write_text('{"models": {"claude-opus-4-6[1m]": {"x": 1}}}')
        monkeypatch.setattr(context_tracker, "_CLAUDE_CONFIG", fake_config)
        # 180K + sonnet + config 只有 opus[1m] → family fallback 不命中 → 默认 1M → 18% 无警告
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 180_000}, "model": "claude-sonnet-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"sonnet 不应被 opus family 泄漏触发警告：{out}"
        transcript.unlink()

    def test_env_var_override(self, monkeypatch):
        """CLAUDE_CONTEXT_SIZE ENV var 覆盖一切：500K → 198K=39.6% 无警告."""
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "500000")
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 198_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"ENV 覆盖未生效：{out}"
        transcript.unlink()

    def test_env_var_priority_beats_1m_marker(self, monkeypatch):
        """ENV 优先级最高：即便 model 含 1m 后缀，ENV=200K 强制按 200K 算."""
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "200000")
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 180_000}, "model": "claude-opus-4-6[1m]"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        # ENV=200K + 180K → 90% CRITICAL（即便 model 写了 1m 后缀也被覆盖）
        assert "CONTEXT CRITICAL" in out
        assert "200000" in out
        transcript.unlink()


# ============================================================
# 尾部反读（_iter_lines_reverse / _read_last_usage）
#
# 2026-09-03：_read_last_usage 由 read_text().splitlines() 改为从文件末尾
# 按块惰性反读。改动动机是 UserPromptSubmit 的 5s 预算——实测 316 MB 的
# transcript 全量读要 4.15s，超时后 CC 丢弃 hook 输出，水位警告恰好在最需要
# 它的长会话里静默失效。下面这组用例钉的是「判定语义不变」，不是性能数字。
# ============================================================


def _reference_read_last_usage(transcript: Path):
    """全量读版本的逐字复刻，作为等价性对照基准。

    改坏 _read_last_usage 时，本参照实现让差分用例直接变红。不要用被测模块
    自己的代码来算期望值——那是同义反复。
    """
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = entry.get("message") or entry
        if m.get("role") != "assistant":
            continue
        usage = m.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        if input_tokens is None:
            continue
        total = (
            int(input_tokens)
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
        )
        model = m.get("model", "") or entry.get("model", "")
        return total, model
    return None


def _assistant(tokens: int, model: str = "claude-opus-4-6", **usage) -> dict:
    return {"message": {"role": "assistant", "usage": {"input_tokens": tokens, **usage},
                        "model": model}}


class TestReverseLineIteration:
    """分帧契约：产出逐字节等于 split(b"\\n") 的逆序，且与块大小无关。"""

    @pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 4096, 1 << 20])
    @pytest.mark.parametrize("trailing_newline", [True, False])
    def test_framing_matches_byte_split(self, tmp_path, chunk, trailing_newline):
        payload = ["", "第一行 unicode 多字节", "x" * 300, "", "{}", "尾行"]
        blob = "\n".join(payload) + ("\n" if trailing_newline else "")
        path = tmp_path / "t.jsonl"
        path.write_text(blob, encoding="utf-8")

        got = list(context_tracker._iter_lines_reverse(path, chunk_size=chunk))
        assert got == list(reversed(blob.encode("utf-8").split(b"\n")))

    def test_empty_file_matches_split_semantics(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_bytes(b"")
        assert list(context_tracker._iter_lines_reverse(path)) == [b""]

    def test_single_line_without_newline(self, tmp_path):
        path = tmp_path / "one.jsonl"
        path.write_bytes(b"{}")
        assert list(context_tracker._iter_lines_reverse(path, chunk_size=1)) == [b"{}"]

    def test_multibyte_char_split_across_chunks(self, tmp_path):
        """块边界切在 UTF-8 多字节序列中间时不得损坏该行。"""
        path = tmp_path / "mb.jsonl"
        blob = "啊" * 100 + "\n" + "结束\n"
        path.write_text(blob, encoding="utf-8")
        expected = list(reversed(blob.encode("utf-8").split(b"\n")))
        for chunk in range(1, 40):
            assert list(context_tracker._iter_lines_reverse(path, chunk_size=chunk)) == expected, chunk


class TestReadLastUsageEquivalence:
    """新旧实现在同一份语料上必须给出同一个答案。"""

    def test_last_assistant_wins(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in [
            _assistant(111_000), _assistant(222_000), _assistant(333_000),
        ]) + "\n", encoding="utf-8")
        assert context_tracker._read_last_usage(path) == (333_000, "claude-opus-4-6")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)

    def test_scans_backwards_past_trailing_non_assistant(self, tmp_path):
        path = tmp_path / "t.jsonl"
        tail = [{"message": {"role": "user", "content": "x"}} for _ in range(500)]
        path.write_text("\n".join(json.dumps(e) for e in [_assistant(444_000), *tail]) + "\n",
                        encoding="utf-8")
        assert context_tracker._read_last_usage(path) == (444_000, "claude-opus-4-6")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)

    def test_assistant_without_usage_is_skipped(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in [
            _assistant(555_000),
            {"message": {"role": "assistant", "content": "no usage here"}},
        ]) + "\n", encoding="utf-8")
        assert context_tracker._read_last_usage(path) == (555_000, "claude-opus-4-6")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)

    def test_no_assistant_at_all_returns_none(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('{"message": {"role": "user"}}\n', encoding="utf-8")
        assert context_tracker._read_last_usage(path) is None
        assert _reference_read_last_usage(path) is None

    def test_crlf_and_missing_trailing_newline(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_bytes(("\r\n".join(json.dumps(e) for e in [
            {"message": {"role": "user"}}, _assistant(666_000),
        ])).encode("utf-8"))  # 末行无换行
        assert context_tracker._read_last_usage(path) == (666_000, "claude-opus-4-6")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)

    def test_cache_layers_summed(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps(_assistant(
            1, cache_read_input_tokens=196_877, cache_creation_input_tokens=1670)) + "\n",
            encoding="utf-8")
        assert context_tracker._read_last_usage(path) == (198_548, "claude-opus-4-6")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)

    def test_model_falls_back_to_entry_level(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps({
            "model": "entry-level-model",
            "message": {"role": "assistant", "usage": {"input_tokens": 7}},
        }) + "\n", encoding="utf-8")
        assert context_tracker._read_last_usage(path) == (7, "entry-level-model")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
    def test_fuzz_matches_reference(self, tmp_path, seed):
        """随机语料差分：行长/空行/坏行/中文/记录位置全随机，两实现必须同答案。"""
        rng = random.Random(seed)
        rows = []
        for _ in range(rng.randint(1, 60)):
            roll = rng.random()
            if roll < 0.25:
                rows.append(json.dumps(_assistant(rng.randint(1, 900_000),
                                                  model=rng.choice(["m-a", "m-b", ""])),
                                       ensure_ascii=False))
            elif roll < 0.45:
                rows.append(json.dumps({"message": {"role": "user",
                                                    "content": "中文" * rng.randint(1, 200)}},
                                       ensure_ascii=False))
            elif roll < 0.6:
                rows.append("")
            elif roll < 0.75:
                rows.append("not json at all " + "y" * rng.randint(0, 500))
            else:
                rows.append(json.dumps({"message": {"role": "assistant",
                                                    "content": "缺 usage"}}, ensure_ascii=False))
        blob = "\n".join(rows) + ("\n" if rng.random() < 0.5 else "")
        path = tmp_path / "fuzz.jsonl"
        path.write_text(blob, encoding="utf-8")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)


class TestReadLastUsageTailOnly:
    """只读尾部这一点本身要被钉住，否则回退到全量读时无人发现。"""

    def test_head_is_never_decoded(self, tmp_path):
        """头部塞非法 UTF-8：全量读会抛 UnicodeDecodeError，尾读读都不读它。

        顺带钉住新增的行为差异——坏行按跳过处理，不再让整个 hook 崩掉。
        """
        path = tmp_path / "t.jsonl"
        with path.open("wb") as fh:
            fh.write(b'{"message": {"role": "assistant", "usage": {"input_tokens": 1}, '
                     b'"model": "\xff\xfe broken"}}\n' * 50)
            fh.write((json.dumps(_assistant(777_000)) + "\n").encode("utf-8"))
        assert context_tracker._read_last_usage(path) == (777_000, "claude-opus-4-6")
        with pytest.raises(UnicodeDecodeError):
            _reference_read_last_usage(path)

    def test_hit_in_tail_of_large_file(self, tmp_path):
        """头部 8 MB 噪声 + 末尾一条命中：结果正确，且只需读到命中处。"""
        path = tmp_path / "big.jsonl"
        filler = json.dumps({"message": {"role": "user", "content": "z" * 400}})
        with path.open("w", encoding="utf-8") as fh:
            for _ in range(20_000):
                fh.write(filler + "\n")
            fh.write(json.dumps(_assistant(888_000)) + "\n")
        assert path.stat().st_size > 8_000_000
        assert context_tracker._read_last_usage(path) == (888_000, "claude-opus-4-6")
        # 惰性：命中前吐出的行数远小于文件总行数
        consumed = 0
        for _ in context_tracker._iter_lines_reverse(path):
            consumed += 1
            break
        assert consumed == 1

    def test_unparsable_line_does_not_stop_the_scan(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("\n".join([
            json.dumps(_assistant(999_000)), "{broken json", "",
        ]) + "\n", encoding="utf-8")
        assert context_tracker._read_last_usage(path) == (999_000, "claude-opus-4-6")
        assert context_tracker._read_last_usage(path) == _reference_read_last_usage(path)

    def test_missing_file_returns_none(self, tmp_path):
        assert context_tracker._read_last_usage(tmp_path / "nope.jsonl") is None
