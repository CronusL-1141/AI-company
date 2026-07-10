#!/usr/bin/env python3
"""Context Tracker - UserPromptSubmit Hook.

Reads the last assistant message's usage.input_tokens from CC transcript jsonl,
calculates context usage ratio. >=80% triggers warning, >=90% triggers critical.
Data source: Anthropic API exact usage, not estimates.
No intermediate files, no statusline dependency, project isolation built-in
(transcript_path.parent = project directory).

Usage: python -m aiteam.hooks.context_tracker
"""
import json
import os
import re
import sys
from pathlib import Path

# 默认按 1M context window 计算。
# 理由：Claude Code 全平台现已默认使用 1M window；
# 误报偏低（漏警告）比误报偏高（频繁假警告打断工作）危害更小。
# 如需强制按小 window（如 200K）计算，请设置 CLAUDE_CONTEXT_SIZE 环境变量。
DEFAULT_CONTEXT_SIZE = 1_000_000
LARGE_CONTEXT_SIZE = 1_000_000
_CLAUDE_CONFIG = Path.home() / ".claude.json"
_FAMILY_RE = re.compile(r"claude-(opus|sonnet|haiku)")


def _claude_config_has_1m_variant(model: str) -> bool:
    """检测机器是否启用了 1M context 模式（通过 ~/.claude.json 的历史用量条目）。

    两层检测：
    1. **精确匹配**：`{model}[1m]` 存在（最可靠）
    2. **同 family 兜底**：任意 `claude-{family}-*[1m]` 存在 → 认为该 family 用 1M
       （opus/sonnet/haiku 三个 family 各自独立判断）

    Why family fallback: CC transcript 剥离 `[1m]` 后缀，且新 model 变体的 [1m]
    用量条目可能尚未写入 config（model 升级后第一次跑时）。若用户在同 family
    下有 1M 历史，则**新 model 大概率仍在 1M 模式**。这是合理启发式。
    """
    if not model:
        return False
    try:
        if not _CLAUDE_CONFIG.is_file():
            return False
        content = _CLAUDE_CONFIG.read_bytes()
    except OSError:
        return False

    # Level 1: 精确匹配
    exact_marker = f'{model}[1m]'.encode()
    if exact_marker in content:
        return True

    # Level 2: 同 family 兜底
    family_match = _FAMILY_RE.search(model.lower())
    if not family_match:
        return False
    family = family_match.group(1)
    family_pattern = re.compile(
        rf'"claude-{family}-[a-z0-9-]+\[1m\]"'.encode()
    )
    return bool(family_pattern.search(content))


def _compute_used_pct(total_tokens: int, model: str) -> tuple[float, int]:
    """计算使用率 + 检测 context window 大小。

    优先级（高到低）：
    1. **ENV var `CLAUDE_CONTEXT_SIZE`** — 用户终极覆盖（任意正整数 token 数）。
       可用此项强制降级到小 window（如 CLAUDE_CONTEXT_SIZE=200000）。
    2. **model 字串含 `1m` / `1000000`** → 1M（显式确认）
    3. **~/.claude.json 有 `{model}[1m]` 或 同 family `*[1m]`** → 1M（历史用量推断）
    4. **默认** → 1M（Claude Code 全平台现已默认 1M window；
       误报偏低比频繁假警告打断工作危害更小）
    """
    # 1. ENV var 终极覆盖（可用于强制按 200K 等小 window 计算）
    env_size = os.environ.get("CLAUDE_CONTEXT_SIZE", "").strip()
    if env_size.isdigit() and int(env_size) > 0:
        ctx_size = int(env_size)
        pct = round((total_tokens / ctx_size) * 100, 1)
        return pct, ctx_size

    # 2. model 字串明确含 1m 标记
    ctx_size = DEFAULT_CONTEXT_SIZE
    if model:
        ml = model.lower()
        if "1m" in ml or "1000000" in ml:
            ctx_size = LARGE_CONTEXT_SIZE

    # 3. ~/.claude.json 历史用量推断（同 family fallback）
    if ctx_size == DEFAULT_CONTEXT_SIZE and _claude_config_has_1m_variant(model):
        ctx_size = LARGE_CONTEXT_SIZE

    # 4. 默认已是 1M（DEFAULT_CONTEXT_SIZE = 1_000_000），无需额外判断

    pct = round((total_tokens / ctx_size) * 100, 1)
    return pct, ctx_size


def _read_last_usage(transcript: Path) -> tuple[int, str] | None:
    """Scan transcript jsonl in reverse for the last assistant message usage.

    Returns (total_tokens, model_name) or None.
    total_tokens = input_tokens + cache_read + cache_creation (all count as context usage)
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


def main():
    # Windows UTF-8 fix
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return  # silent failure

    transcript_path_str = payload.get("transcript_path", "")
    if not transcript_path_str:
        return
    transcript = Path(transcript_path_str)
    if not transcript.exists():
        return

    result = _read_last_usage(transcript)
    if result is None:
        return
    used_tokens, model = result

    pct, ctx_size = _compute_used_pct(used_tokens, model)

    if pct >= 90:
        print(
            f"[CONTEXT CRITICAL] 上下文使用率: {pct}% ({used_tokens}/{ctx_size}). "
            "立即停止当前工作，保存所有记忆和进度到 memory 文件，"
            "然后提醒用户执行 /compact。不要开始任何新任务。"
        )
    elif pct >= 80:
        print(
            f"[CONTEXT WARNING] 上下文使用率: {pct}% ({used_tokens}/{ctx_size}). "
            "请尽快完成当前节点任务，然后保存记忆和进度到 memory 文件，"
            "并提醒用户执行 /compact。"
        )


if __name__ == "__main__":
    main()
