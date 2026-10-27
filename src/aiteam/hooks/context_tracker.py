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
import sys
from pathlib import Path

# 默认按 1M context window 计算。
# 理由：Claude Code 全平台现已默认使用 1M window；
# 误报偏低（漏警告）比误报偏高（频繁假警告打断工作）危害更小。
# 如需强制按小 window（如 200K）计算，请设置 CLAUDE_CONTEXT_SIZE 环境变量。
DEFAULT_CONTEXT_SIZE = 1_000_000


def _compute_used_pct(total_tokens: int) -> tuple[float, int]:
    """计算使用率与 context window 大小。

    ``CLAUDE_CONTEXT_SIZE``（任意正整数 token 数）是用户的终极覆盖，可用来强制按
    小 window 计算；否则按默认 1M。

    这里曾经还从 model 字串的 ``1m`` 标记与 Claude Code 全局配置里的 ``[1m]`` 用量
    条目推断 1M：那是默认还是 200K 时的升档逻辑。默认改成 1M 之后，两条推断的结果都
    只能是 1M，却每次提问都要读一遍全局配置文件，所以删掉了。
    """
    env_size = os.environ.get("CLAUDE_CONTEXT_SIZE", "").strip()
    ctx_size = int(env_size) if env_size.isdigit() and int(env_size) > 0 else DEFAULT_CONTEXT_SIZE
    return round((total_tokens / ctx_size) * 100, 1), ctx_size


# 尾部反读的块大小。一条 assistant 记录通常只有几 KB，1 MiB 一块意味着
# 绝大多数会话读一块就命中。
_TAIL_CHUNK_BYTES = 1 << 20


def _iter_lines_reverse(transcript: Path, chunk_size: int = _TAIL_CHUNK_BYTES):
    """从文件末尾向前逐行 yield 原始字节行（不含换行符），惰性读取。

    Why not read_text().splitlines(): transcript 是只追加的 jsonl，最后一条
    assistant 记录几乎总在末尾几行，而整份文件可以到几百 MB。全量读让本 hook
    的耗时随会话长度线性增长并击穿 UserPromptSubmit 的 5s 预算（实测 316 MB
    需 4.15s），超时后 CC 丢弃 hook 输出——也就是说水位警告恰好在最需要它的
    长会话里静默失效。按块反向读之后，耗时与内存只取决于"末尾到命中处"的
    距离，与文件总大小无关。

    分帧用 b"\\n"：jsonl 按定义就是换行分帧，与旧实现的分行结果在合法语料上
    逐行一致。跨块的残段会与前一块拼接后再切，不会把一行劈成两半。

    契约（由 TestReverseLineIteration 钉住）：产出逐字节等于整份内容
    ``split(b"\\n")`` 的逆序，与 chunk_size 取值无关。空段照样产出——由调用方
    自行跳过，好过在这里做一半的过滤留下首尾不对称。
    """
    with transcript.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        carry = b""  # 本块最前面那截，可能还要向前接续
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            segments = (fh.read(read_size) + carry).split(b"\n")
            carry = segments[0]
            yield from reversed(segments[1:])
        yield carry  # 文件第一行（空文件时是唯一的空段，与 split 语义一致）


def _read_last_usage(transcript: Path) -> tuple[int, str] | None:
    """Scan transcript jsonl in reverse for the last assistant message usage.

    Returns (total_tokens, model_name) or None.
    total_tokens = input_tokens + cache_read + cache_creation (all count as context usage)

    判定语义与全量读版本逐条相同，只是换成了尾部惰性读取（见
    ``_iter_lines_reverse``）。唯一的行为差异是坏行的处置：旧版整份 decode，
    一处非法 UTF-8 就让整个 hook 抛异常；这里逐行 decode，坏行按"跳过"处理，
    与本文件其余部分的静默失败取向一致。
    """
    try:
        for raw_line in _iter_lines_reverse(transcript):
            if not raw_line.strip():
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            m = entry.get("message") or entry
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            usage = m.get("usage") or {}
            if not isinstance(usage, dict):
                continue
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
    except OSError:
        return None

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
    used_tokens, _model = result

    pct, _window = _compute_used_pct(used_tokens)

    if pct >= 90:
        print(
            f"[CONTEXT CRITICAL] 上下文已用 {pct}%，接近自动压缩。"
            "尚未落盘的进展与结论现在用 task_memo_add 记下，并提醒用户可执行 /compact。"
        )
    elif pct >= 80:
        print(
            f"[CONTEXT WARNING] 上下文已用 {pct}%。压缩前 OS 会自动为本会话存检查点"
            "（在飞 agent、未完成任务、待裁决项），压缩后递回；"
            "尚未落盘的进展与结论用 task_memo_add 记下。"
        )


def _main_chain_commands() -> list[str]:
    """The commands this session's main chain runs this script with, when this is the plugin's copy.

    With AI Team OS both a marketplace plugin and a source install, CC fires two
    byte-identical copies of every hook; only the plugin copy defers (CLAUDE_PLUGIN_ROOT
    is set for plugin hooks only, and __file__ lives under it). The main chain is what
    the settings.json of this session's config folder registers under hooks/ai-team-os/;
    the folder comes from hook_core's cc_config_dir, the only name taken from it. Any
    error means no commands, so the hook runs (fail-safe).
    """
    import importlib.util
    import json
    import os
    import sys
    from pathlib import Path
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return []
    try:
        if Path(plugin_root).resolve() not in Path(__file__).resolve().parents:
            return []
        _core = sys.modules.get("hook_core")
        if _core is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_core.py")
            spec = importlib.util.spec_from_file_location("hook_core", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _core = sys.modules["hook_core"] = module
        settings = _core.cc_config_dir() / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
        commands = [str(hook.get("command", "")) for groups in registered.values() for group in groups
                    for hook in group.get("hooks", [])]
    except Exception:
        return []
    name = os.path.basename(__file__)
    return [command for command in commands if "ai-team-os" in command and name in command]


def _yield_if_superseded() -> None:
    """Exit 0 when the main chain covers this hook; a hook it does not register keeps running here."""
    if _main_chain_commands():
        raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
