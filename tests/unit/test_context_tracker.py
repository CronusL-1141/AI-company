"""Tests for context_tracker hook."""
import json
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
