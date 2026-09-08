#!/usr/bin/env python3
"""UserPromptSubmit hook —— 信道未读徽章。

每轮开口前查一次"有没有人点名叫我"，有就注入一行，没有就一个字都不输出。
读完调 channel_read_ack 推进水位，下一轮自然算出 0 条，那一行自己消失——不需要任何
"清除提示"的机制。

为什么挂在 UserPromptSubmit 而不是 SessionStart：SessionStart 一个会话只响一次，而
消息是随时来的。实测过一次代价——对端连发 8 条点名消息、其中一条明确在等回执，在
库里躺了半小时无人读，因为这一侧根本不知道有信。

设计取舍，改之前先读：

- **reader 与 project_id 都从 argv 显式传**，绝不嗅探环境。reader 是角色标识
  （leader-cc），不是 session_id：按会话记水位会让每开一个新会话就把历史消息重算成
  未读。project_id 省略时才回落到按 cwd 解析。
- **project 解析不出来就不注入，并且不等于"未读 0"**。这两种状态在界面上长得一样，
  但一个是"没人叫你"，一个是"我不知道该查哪个项目"。后者宁可沉默也不能谎报太平。
- **注入行必须自包含**：把 channel、reader、last_read_at 三个参数都写进去。少任何一个，
  模型调 ack 就会失败或推错水位，结果是同一行每轮重复注入到天荒地老——比没有徽章更
  糟，且没有任何机检抓得到。
- **带摘要不带全文**：只给数字没有去读的动机；给全文则水位永远推不动。
- 任何异常一律吞掉并 exit 0。这是每一轮用户发言都要跑的路径，绝不能挡住人说话。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 文件名是 api_port.txt，别漏 .txt：_autostart._save_api_port 与 session_bootstrap 用的
# 都是这个名字。漏掉后果极隐蔽——读不到就静默回落到硬编码的 8000，而 8000 通常恰好是对
# 的，于是单测、机检、真机验收会全部通过，只在 API 换端口那天失效。
_PORT_FILE = str(Path.home() / ".claude" / "data" / "ai-team-os" / "api_port.txt")

# 这一轮的预算。查不到就算了，绝不让用户等——徽章迟一轮出现无所谓，卡住一轮很要命。
_TIMEOUT_SECS = 1.5
_MAX_CHANNELS_SHOWN = 3
_EXCERPT_CHARS = 60


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _api_get(path: str, timeout: float = _TIMEOUT_SECS):
    """GET request to API; return JSON or None."""
    try:
        req = urllib.request.Request(f"{_get_api_url()}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _api_post(path: str, payload: dict, timeout: float = _TIMEOUT_SECS):
    """POST request to API; return JSON or None."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{_get_api_url()}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _read_payload() -> dict:
    """读 hook 的 stdin 载荷；读不到就当空载荷（照常跑，用 cwd 兜底）。"""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _resolve_project(explicit: str, cwd: str) -> str:
    """定出该查哪个项目：显式绑定优先，其次按 cwd 归属，都没有返回空串。

    空串的含义是"不知道该查哪个项目"，调用方必须据此**沉默**，不得当成未读 0。
    worktree 目录尤其会走到这一支：归属解析只认精确匹配与子目录前缀，而 worktree 是
    同级目录，两条都不命中。
    """
    if explicit:
        return explicit
    if not cwd:
        return ""
    got = _api_post("/api/context/resolve", {"cwd": cwd, "auto_create": False})
    if not isinstance(got, dict):
        return ""
    # REST 这个端点回的是**扁平** {"project_id": ...}，而同名的 MCP 工具回的是嵌套
    # {"project": {"id": ...}}。照着 MCP 的形状写会永远解析出空串，于是这个 hook 永远
    # 沉默——而沉默正好是它没有未读时的正常表现，单测里也会照同样的错误假设去 mock。
    # 两种形状都认，别再踩一次。
    flat = got.get("project_id")
    if flat:
        return str(flat)
    project = got.get("project") or {}
    if isinstance(project, dict):
        return str(project.get("id") or "")
    return ""


def _sanitize_inline(text: str) -> str:
    """压成安全的单行：注入的是别人写的内容，不能让它撑开或截断这一行。"""
    flat = " ".join((text or "").split())
    return flat.replace("\r", " ").replace("\n", " ")


def _render(reader: str, data: dict) -> str:
    """把未读摘要渲染成注入行；无未读返回空串。

    ACK 指引里 last_read_at **刻意不预填具体时间戳**。手边唯一现成的值是 latest_at
    （最新一条的时刻），可它代表的是"全部读完了"；调用方分页只取回前 N 条时照填，
    没读到的那些会被一起标成已读，从此再不提示，且没有任何机检抓得到。留成占位符，
    强制调用方回看自己真正读到了哪一条。
    """
    channels = data.get("channels")
    total = data.get("total") or 0
    # 类型也要验，不只是真值。字符串尤其阴险：可切片、可迭代，一路走到 .get 才炸，
    # 而 main 的兜底会把异常吞掉——表现成"没有未读"，与真的没有未读分不开。
    if not isinstance(channels, list) or not channels or not total:
        return ""

    project_id = data.get("project_id", "")
    lines = [f"[信道未读] {total} 条消息点名 {reader}，对方在等你，读完记得清零："]
    for entry in channels[:_MAX_CHANNELS_SHOWN]:
        if not isinstance(entry, dict):
            continue
        channel = entry.get("channel", "?")
        count = entry.get("count", 0)
        sender = entry.get("latest_sender", "?")
        excerpt = _sanitize_inline(str(entry.get("latest_excerpt", "")))[:_EXCERPT_CHARS]
        lines.append(
            f'  · {channel} — {sender}（{count} 条）："{excerpt}"'
        )
        lines.append(
            f'    读: channel_read(channel="{channel}")　'
            f'清零: channel_read_ack(channel="{channel}", reader="{reader}", '
            f'project_id="{project_id}", last_read_at=<你实际读到的最后一条的 created_at>)'
        )
    hidden = len(channels) - _MAX_CHANNELS_SHOWN
    if hidden > 0:
        lines.append(f"  · 另有 {hidden} 个频道有未读，用 channel_unread 查全部")
    if data.get("truncated"):
        lines.append("  · 命中扫描上限，实际条数只多不少")
    return "\n".join(lines)


def main() -> None:
    """查未读并注入一行。无未读、或不确定该查哪个项目时，一个字都不输出。"""
    reader = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not reader:
        return  # 没有身份就没有未读可言，静默退出

    explicit_project = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
    payload = _read_payload()
    cwd = str(payload.get("cwd") or os.getcwd())

    project_id = _resolve_project(explicit_project, cwd)
    if not project_id:
        # 不知道该查哪个项目 ≠ 没有未读。沉默，但不谎报太平。
        return

    query = urllib.parse.urlencode({"reader": reader, "project_id": project_id})
    got = _api_get(f"/api/channels/unread?{query}")
    if not isinstance(got, dict):
        return
    rendered = _render(reader, got.get("data") or {})
    if rendered:
        print(rendered)


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    try:
        main()
    except Exception:
        # 每一轮用户发言都要跑这条路径，绝不能因为它挡住人说话
        pass
    sys.exit(0)
