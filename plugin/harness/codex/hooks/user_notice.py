#!/usr/bin/env python3
"""AI Team OS - the one place hook scripts write user-visible lines.

Every line a hook shows the user (a ``systemMessage``) leaves through ``emit``
in this file, so the rules that make those lines readable are enforced once:
the ``[AI Team OS] `` prefix, at most 160 display columns, no Markdown or URL,
colour only on the CLI, one JSON document per hook process, and the host's
field whitelist (Codex rejects the whole document when it meets a field it
does not know). ``scripts/check_user_notice_exit.py`` (I22) fails the build
when another hook writes ``systemMessage`` or user-facing wording itself.

Two sources feed it:

* ``fetch_pending`` asks the OS API (``POST /api/notices/pending``) which
  ledger notices this exit should show. The API renders them; this file only
  validates and writes. It also carries this host's local records (lines this
  file rendered on its own, delivery ids already written) so the ledger and the
  Dashboard see them.
* ``LOCAL_CATALOG`` holds the notices a hook must render without the API:
  blocks (the hook exits 2 before any HTTP), install progress, and "the API is
  down". Its texts are a verbatim copy of the API catalog's local entries;
  ``tests/unit/hooks/test_user_notice_catalog_parity.py`` compares the two.

Blocks follow what Claude Code 2.1.281 shows. A PreToolUse block always appears
as one red "PreToolUse:<tool> hook error: <reason>" line, whatever the output
form, so ``emit_block`` makes the reason the user line and sends no
systemMessage. A Stop block shows the red line as a systemMessage and hands the
model its reason as additionalContext, which the host shows as "Stop hook
feedback" (a decision:block reason shows as "Stop hook error").

Shared core, like ``hook_core.py``: the copies in ``plugin/hooks``,
``src/aiteam/hooks`` and ``plugin/harness/codex/hooks`` are byte-identical (I1).
It is not a Codex support module.

Standard library only, and importable on old interpreters: ``auto_install``
loads it to explain that the interpreter is too old.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from pathlib import Path

PREFIX = "[AI Team OS] "
MAX_COLUMNS = 160

# Test seam: a non-empty value replaces the OS data directory. Hooks never set it.
STATE_DIR_OVERRIDE = ""

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ELLIPSIS = "…"
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
# "{n?one|other}": same count agreement as aiteam.services.notices.render.
_PLURAL_RE = re.compile(r"\{([a-z_]+)\?([^|{}]*)\|([^|{}]*)\}")
_FG_RESET = "\x1b[39m"
_KIND_COLOR = {
    "action": "\x1b[33m",
    "decision": "\x1b[33m",
    "blocked": "\x1b[31m",
    "done": "\x1b[32m",
}
_FORBIDDEN = ("**", "`", "](", "http")
_URL_SCHEME_RE = re.compile(r"(?i)https?://")
_STARS_RE = re.compile(r"\*{2,}")
_ASSISTANT = {"cc": "Claude", "codex": "Codex"}
_HOST_APP = {"cc": "Claude Code", "codex": "Codex"}

# Per host and event, the output fields a hook may send. PreToolUse carries a
# model-only context as well, so a warning and a user line fit one document; a
# block adds the deny decision and its reason (emit_block). Stop carries its
# reason for the model as additionalContext, never decision:block.
_CC_FIELDS = {
    "SessionStart": frozenset({"systemMessage", "additionalContext"}),
    "UserPromptSubmit": frozenset({"systemMessage", "additionalContext"}),
    "PreToolUse": frozenset({"systemMessage", "additionalContext", "permissionDecision",
                             "permissionDecisionReason"}),
    "Stop": frozenset({"systemMessage", "additionalContext"}),
}
# The fields above that live inside hookSpecificOutput rather than at the top level.
_CC_SPECIFIC = frozenset({"additionalContext", "permissionDecision", "permissionDecisionReason"})
_CODEX_TOP_LEVEL = frozenset({"continue", "stopReason", "suppressOutput", "systemMessage"})

# Local record file: one JSON object per line, appended with O_APPEND.
_RECORD_MAX_BYTES = 1024
_DEDUP_SCAN_BYTES = 64 * 1024
_ROTATE_BYTES = 1024 * 1024
_IMPORT_MAX_RECORDS = 200
_IMPORT_MAX_BYTES = 256 * 1024
_IMPORTED_KINDS = frozenset({"emitted", "local_notice", "consent", "notice_dismiss"})
# Immediate lines (branch switched, a held turn end): one per key per session,
# and at most this many per session; beyond it they are recorded but not shown.
# A PreToolUse block reason is not one of them: the host shows a reason with
# every block, so emit_block states it every time.
_IMMEDIATE_IDS = frozenset({"branch_switched", "blocked_turn_end"})
_IMMEDIATE_SESSION_CAP = 5
# Install state older than this is a killed attempt, not one in progress.
INSTALL_STALE_S = 300

# Model-note frames, same sentences as aiteam.services.notices.render. {line} is
# the plain user line (prefix included).
MODEL_HEADER = {
    ("zh", True): "AI Team OS 刚在界面上向用户显示了以下提示（systemMessage 不进你的上下文，这里是原文）：{line}",
    ("zh", False): "AI Team OS 尝试向用户显示以下提示，界面可能没有显示：{line}",
    ("en", True): (
        "AI Team OS just showed the user this notice "
        "(systemMessage does not reach your context; this is the original text): {line}"
    ),
    ("en", False): "AI Team OS tried to show the user this notice; the interface may not have displayed it: {line}",
}
MODEL_CLOSING = {
    "zh": "用户问起或说出动作句时再处理，不必主动复述。",
    "en": "Act on it when the user asks or says the action phrase; do not repeat it unprompted.",
}

# Entries a hook renders without the API: a verbatim copy of the local entries
# of aiteam.services.notices.catalog (the parity test renders both and compares).
# params: name -> maximum characters; tail_params keep the end of a long value.
# frame: "notice" = header + note + closing, "raw" = note alone.
LOCAL_CATALOG = {
    "api_down": {
        "kind": "action",
        "frame": "notice",
        "params": {},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": (
                        "服务未启动，任务墙与记忆暂不可用。重启 {host_app}，或对 {assistant} "
                        "说「重启 OS 服务」"
                    ),
                    "en": (
                        "Service is not running, so tasks and memory are unavailable. Restart "
                        "{host_app} or tell {assistant} \"restart OS service\""
                    ),
                },
                "model": {
                    "zh": (
                        "OS 的 MCP 工具可用时调用 os_restart_api；不可用时请用户重启 {host_app}。"
                        "不要手动再起一个 uvicorn 实例：它会和自动拉起的实例并存，造成重复唤醒。"
                    ),
                    "en": (
                        "If the OS MCP tools are available, call os_restart_api; otherwise ask the "
                        "user to restart {host_app}. Do not start another uvicorn instance by hand: "
                        "it would run next to the auto-started one and cause duplicate wakes."
                    ),
                },
            },
        },
    },
    "install_in_progress": {
        "kind": "status",
        "frame": "notice",
        "params": {"attempt": 4},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "正在安装依赖（第 {attempt} 次），装好之前 OS 工具不可用",
                    "en": (
                        "Installing dependencies (attempt {attempt}). OS tools are unavailable until "
                        "it finishes"
                    ),
                },
                "model": {
                    "zh": (
                        "依赖安装在后台进行，OS 的 MCP 工具暂不可用；不要自己去跑 pip。次数大于 1 "
                        "说明上一次安装被宿主超时中断。"
                    ),
                    "en": (
                        "Dependencies are installing in the background and the OS MCP tools are "
                        "unavailable meanwhile; do not run pip yourself. An attempt above 1 means "
                        "the previous run was cut off by the host timeout."
                    ),
                },
            },
        },
    },
    "install_done": {
        "kind": "done",
        "frame": "notice",
        "params": {"ver": 12},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "{ver} 已装好，重启 Claude Code 后生效",
                    "en": "{ver} installed. Restart Claude Code to load it",
                },
                "model": {
                    "zh": (
                        "重启之前 OS 工具不在工具列表里，属正常；重启后可以用 /os-help 看 OS "
                        "能做什么。"
                    ),
                    "en": (
                        "Until the restart the OS tools are missing from the tool list, which is "
                        "expected; after it, /os-help shows what OS can do."
                    ),
                },
            },
        },
    },
    "install_upgraded": {
        "kind": "done",
        "frame": "notice",
        "params": {"ver": 12, "old": 12},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "已升级到 {ver}（原 {old}），重启 Claude Code 后生效",
                    "en": "Upgraded to {ver} (was {old}). Restart Claude Code to apply",
                },
                "model": {
                    "zh": (
                        "磁盘上的包已是新版；运行中的服务可能还是旧版，"
                        "重启后若仍落后会再出服务版本提示。"
                    ),
                    "en": (
                        "The package on disk is the new version; the running service may still be "
                        "the old one, and a service-version notice follows after the restart if it "
                        "still lags."
                    ),
                },
            },
        },
    },
    "install_failed": {
        "kind": "action",
        "frame": "notice",
        "params": {"py": 12},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "依赖安装失败：原因未识别。对 {assistant} 说「诊断 OS 安装」",
                    "en": (
                        "Dependency install failed: unrecognized error. Tell {assistant} \"diagnose "
                        "OS install\""
                    ),
                },
                "model": {
                    "zh": (
                        "查看 auto_install 的输出与 hook 日志找出原因，再给出修复步骤。诊断本身只读，"
                        "任何修复都要用户确认。"
                    ),
                    "en": (
                        "Read the auto_install output and hook log to find the cause, then propose "
                        "fix steps. Diagnosis is read-only; any fix needs the user's confirmation."
                    ),
                },
            },
            "unknown": {
                "user": {
                    "zh": "依赖安装失败：原因未识别。对 {assistant} 说「诊断 OS 安装」",
                    "en": (
                        "Dependency install failed: unrecognized error. Tell {assistant} \"diagnose "
                        "OS install\""
                    ),
                },
                "model": {
                    "zh": (
                        "查看 auto_install 的输出与 hook 日志找出原因，再给出修复步骤。诊断本身只读，"
                        "任何修复都要用户确认。"
                    ),
                    "en": (
                        "Read the auto_install output and hook log to find the cause, then propose "
                        "fix steps. Diagnosis is read-only; any fix needs the user's confirmation."
                    ),
                },
            },
            "pep668": {
                "user": {
                    "zh": (
                        "依赖安装失败：系统 Python 禁止 pip 安装（PEP 668）。对 {assistant} 说「诊断 "
                        "OS 安装」"
                    ),
                    "en": (
                        "Dependency install failed: system Python blocks pip (PEP 668). Tell "
                        "{assistant} \"diagnose OS install\""
                    ),
                },
                "model": {
                    "zh": (
                        "换一个允许安装的 Python 解释器；只有在用户明确同意、并讲清风险之后，"
                        "才可以用 --break-system-packages。诊断本身只读，任何修复都要用户确认。"
                    ),
                    "en": (
                        "Switch to a Python interpreter that allows installs; use "
                        "--break-system-packages only after the user explicitly agrees and the risk "
                        "is explained. Diagnosis is read-only; any fix needs the user's confirmation."
                    ),
                },
            },
            "python_old": {
                "user": {
                    "zh": (
                        "依赖安装失败：Python 版本低于 3.11（当前 {py}）。对 {assistant} 说「诊断 OS "
                        "安装」"
                    ),
                    "en": (
                        "Dependency install failed: Python {py} is older than 3.11. Tell {assistant} "
                        "\"diagnose OS install\""
                    ),
                },
                "model": {
                    "zh": (
                        "安装 Python 3.11 及以上版本后重启 Claude Code。诊断本身只读，"
                        "任何修复都要用户确认。"
                    ),
                    "en": (
                        "Install Python 3.11 or newer, then restart Claude Code. Diagnosis is "
                        "read-only; any fix needs the user's confirmation."
                    ),
                },
            },
            "no_git": {
                "user": {
                    "zh": "依赖安装失败：未找到 git。对 {assistant} 说「诊断 OS 安装」",
                    "en": (
                        "Dependency install failed: git not found. Tell {assistant} \"diagnose OS "
                        "install\""
                    ),
                },
                "model": {
                    "zh": (
                        "macOS 上运行 xcode-select --install，或用系统包管理器安装 git。"
                        "诊断本身只读，任何修复都要用户确认。"
                    ),
                    "en": (
                        "On macOS run xcode-select --install, or install git with the system package "
                        "manager. Diagnosis is read-only; any fix needs the user's confirmation."
                    ),
                },
            },
            "network": {
                "user": {
                    "zh": "依赖安装失败：网络不通。对 {assistant} 说「诊断 OS 安装」",
                    "en": (
                        "Dependency install failed: network unreachable. Tell {assistant} \"diagnose "
                        "OS install\""
                    ),
                },
                "model": {
                    "zh": "检查网络或代理后稍后重试。诊断本身只读，任何修复都要用户确认。",
                    "en": (
                        "Check the network or proxy and retry later. Diagnosis is read-only; any fix "
                        "needs the user's confirmation."
                    ),
                },
            },
        },
    },
    "orphan_main_chain": {
        "kind": "action",
        "frame": "notice",
        "params": {},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "插件已卸载或停用，但全局 hook 仍在运行。对 {assistant} 说「清理 OS 残留」",
                    "en": (
                        "The plugin is removed or disabled, but its global hooks still run. Tell "
                        "{assistant} \"clean up OS leftovers\""
                    ),
                },
                "model": {
                    "zh": (
                        "运行 Claude Code 配置目录（默认 ~/.claude）下的 "
                        "hooks/ai-team-os/uninstall_main_chain.py：先不带参数预览，"
                        "把将要删除的条目原样给用户看；用户确认后，再带 --apply <token> --user-quote "
                        "\"<用户原话>\" 执行。如果插件只是停用，先问用户要不要重新启用。"
                    ),
                    "en": (
                        "Run hooks/ai-team-os/uninstall_main_chain.py under the Claude Code config "
                        "folder (default ~/.claude): first without arguments to preview, show the "
                        "user exactly what would be removed, and only after they confirm run it with "
                        "--apply <token> --user-quote \"<the user's words>\". If the plugin is only "
                        "disabled, first ask whether to enable it again."
                    ),
                },
            },
        },
    },
    "installed_copy_synced": {
        "kind": "done",
        "frame": "notice",
        "params": {"n": 4},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "已自动同步 {n} 个落后的 hook 副本，即刻生效",
                    "en": "Synced {n} outdated hook {n?copy. It takes|copies. They take} effect now",
                },
                "model": {
                    "zh": "hook 在下一次调用时就读新文件，无需任何操作。",
                    "en": "Hooks read the new files on their next run; nothing to do.",
                },
            },
        },
    },
    "branch_switched": {
        "kind": "action",
        "frame": "notice",
        "params": {"repo": 16, "ob": 20, "nb": 20},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": (
                        "{repo} 的分支已从 {ob} 换成 {nb}，可能有别的会话在用这个目录。对 "
                        "{assistant} 说「查分支变更」"
                    ),
                    "en": (
                        "{repo} switched from {ob} to {nb}; another session may be using it. Tell "
                        "{assistant} \"check branch change\""
                    ),
                },
                "model": {
                    "zh": (
                        "运行 git -C <仓库> reflog -n 10 和 git worktree list 查清是谁换的；"
                        "不要自动切回；建议按多会话纪律另开独立的 worktree。"
                    ),
                    "en": (
                        "Run git -C <repo> reflog -n 10 and git worktree list to find who switched "
                        "it; do not switch back automatically; suggest a separate worktree per the "
                        "multi-session rule."
                    ),
                },
            },
        },
    },
    "blocked_secret_add": {
        "kind": "blocked",
        "frame": "raw",
        "params": {"file": 32},
        "tail_params": ("file",),
        "variants": {
            "": {
                "user": {
                    "zh": "已拦截这条 git add：含敏感文件 {file}，命令未执行",
                    "en": (
                        "Blocked this git add: it includes a sensitive file ({file}). The command "
                        "did not run"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
        },
    },
    "blocked_teardown": {
        "kind": "blocked",
        "frame": "raw",
        "params": {"target": 32},
        "tail_params": ("target",),
        "variants": {
            "": {
                "user": {
                    "zh": "已拦截删除：{target} 有未保存的工作，删了找不回，命令未执行",
                    "en": (
                        "Blocked a deletion: {target} has unsaved work that would be lost. The "
                        "command did not run"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
            "unsaved": {
                "user": {
                    "zh": "已拦截删除：{target} 有未保存的工作，删了找不回，命令未执行",
                    "en": (
                        "Blocked a deletion: {target} has unsaved work that would be lost. The "
                        "command did not run"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
            "timeout": {
                "user": {
                    "zh": "已拦截删除：安全检查超时，没能确认 {target} 可以安全删除，命令未执行",
                    "en": (
                        "Blocked a deletion: the safety check timed out before {target} was "
                        "confirmed safe. The command did not run"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
            "unverified": {
                "user": {
                    "zh": "已拦截删除：没能确认 {target} 可以安全删除，命令未执行",
                    "en": (
                        "Blocked a deletion: {target} could not be confirmed safe to delete. The "
                        "command did not run"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
        },
    },
    "blocked_foreign_branch": {
        "kind": "blocked",
        "frame": "raw",
        "params": {"branch": 20},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "已拦截提交：分支 {branch} 正被另一个会话使用，命令未执行",
                    "en": (
                        "Blocked a commit: branch {branch} is in use by another session. The command "
                        "did not run"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
        },
    },
    "blocked_dispatch_model": {
        "kind": "blocked",
        "frame": "raw",
        "params": {},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "已拦截派工：没有写明模型档位，{assistant} 需补上后重派",
                    "en": (
                        "Blocked a dispatch: no model tier was given. {assistant} must add it and "
                        "dispatch again"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
            "no_reason": {
                "user": {
                    "zh": "已拦截派工：用 fable 或 fork 派工没有写理由，{assistant} 需补上后重派",
                    "en": (
                        "Blocked a dispatch: a fable or fork dispatch gave no reason. {assistant} "
                        "must add it and dispatch again"
                    ),
                },
                "model": {
                    "zh": "拦截理由随工具结果送达，完整原因与下一步见同时送达的 [OS BLOCK] 说明。",
                    "en": (
                        "The block reason arrives with the tool result; the [OS BLOCK] note delivered "
                        "with it gives the full cause and the next step."
                    ),
                },
            },
        },
    },
    "blocked_turn_end": {
        "kind": "blocked",
        "frame": "raw",
        "params": {"n": 4},
        "tail_params": (),
        "variants": {
            "": {
                "user": {
                    "zh": "还有 {n} 项在后台运行，已拦下收工让 {assistant} 继续等；说「停」即可结束",
                    "en": (
                        "{n} {n?task is|tasks are} still running in the background, so {assistant} keeps "
                        "waiting. Say \"stop\" to end"
                    ),
                },
                "model": {
                    "zh": (
                        "后台还有 {n} 项在运行，{assistant} 继续等待：{assistant} 需以后台任务方式运行 bash "
                        "scripts/os-watch.sh <session_id> <team_id> 武装 watcher 后再停，或回复用户后收工；"
                        "用户说「停」即结束。"
                    ),
                    "en": (
                        "{n} background {n?task is|tasks are} still running, so {assistant} keeps waiting: "
                        "{assistant} should arm a watcher with bash scripts/os-watch.sh <session_id> <team_id> as "
                        "a background task before stopping, or reply to the user and stop. The user can say "
                        "\"stop\" to end."
                    ),
                },
            },
        },
    },
}

_LAST_FAILURE = ""
_WROTE_DOCUMENT = False


# ---------------------------------------------------------------------------
# Paths and API address
# ---------------------------------------------------------------------------


def os_data_dir() -> Path:
    """The OS data directory, the same one ``hook_core`` and every hook use."""
    if STATE_DIR_OVERRIDE:
        return Path(STATE_DIR_OVERRIDE)
    return Path.home() / ".claude" / "data" / "ai-team-os"


def cc_config_dir() -> Path:
    """Claude Code's config directory: CLAUDE_CONFIG_DIR, else ~/.claude."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def api_url() -> str:
    """AITEAM_API_URL wins, then the port the API bound to, then 8000."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        port = int((os_data_dir() / "api_port.txt").read_text(encoding="utf-8").strip())
        return f"http://localhost:{port}"
    except (OSError, ValueError):
        return "http://localhost:8000"


def sha8(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:8]


def _diag(message: str) -> None:
    try:
        sys.stderr.write(f"[aiteam-notice] {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Language (same rules as aiteam.api.language, read straight from the files)
# ---------------------------------------------------------------------------


def _normalize_language(value: str) -> str:
    value = value.strip().lower()
    return "zh" if value.startswith("zh") or "chinese" in value or "中文" in value else "en"


def _cc_settings_language(cwd: str) -> str | None:
    paths = []
    if cwd:
        project = Path(cwd).expanduser()
        paths.extend((project / ".claude/settings.local.json", project / ".claude/settings.json"))
    paths.append(cc_config_dir() / "settings.json")
    for path in paths:
        try:
            if path.stat().st_size > 65536:
                continue
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        value = config.get("language") if isinstance(config, dict) else None
        if isinstance(value, str) and value.strip():
            return _normalize_language(value)
    return None


def system_language() -> str | None:
    """The user's preferred UI language: AppleLanguages, then the locale variables."""
    if sys.platform == "darwin":
        try:
            import plistlib

            path = Path.home() / "Library/Preferences/.GlobalPreferences.plist"
            languages = plistlib.loads(path.read_bytes()).get("AppleLanguages", [])
            if isinstance(languages, list):
                for value in languages:
                    if isinstance(value, str) and value.strip():
                        return _normalize_language(value)
        except Exception:
            pass
    for key in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = os.environ.get(key, "").strip()
        if value:
            return _normalize_language(value)
    try:
        import locale

        value = locale.getlocale()[0]
    except (ValueError, TypeError):
        value = None
    return _normalize_language(value) if value else None


def resolve_language_local(host: str, cwd: str) -> str:
    """Dashboard choice, then (cc only) Claude Code's language setting, then the system."""
    try:
        config = json.loads((os_data_dir() / "wake_config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        config = None
    mode = config.get("language_mode", "follow") if isinstance(config, dict) else "follow"
    if mode in ("zh", "en"):
        return mode
    if host == "cc":
        language = _cc_settings_language(cwd)
        if language:
            return language
    return system_language() or "en"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def display_width(text: str) -> int:
    """Terminal columns: East Asian wide/fullwidth count 2, ANSI sequences 0."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in strip_ansi(text))


# Unassigned code points kept in one line: the emoji and symbol blocks, where newer
# Unicode versions add pictographs this Python's data does not know yet. Same table as
# aiteam.text_safety.clean_text; a parity test holds the two equal.
_SYMBOL_BLOCKS = ((0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x1F000, 0x1FBFF))


def _unsafe_char(ch: str) -> bool:
    kind = unicodedata.category(ch)
    if kind[0] != "C":
        return False
    return kind != "Cn" or not any(low <= ord(ch) <= high for low, high in _SYMBOL_BLOCKS)


def clean_text(value: object) -> str:
    """One safe line: control and format characters (ESC included) to spaces, whitespace folded."""
    text = "" if value is None else str(value)
    text = "".join(" " if _unsafe_char(ch) else ch for ch in text)
    return " ".join(text.split())


def line_safe(text: str) -> str:
    """Neutralise, inside one parameter, the tokens ``emit`` refuses in a user line.

    Same rule as aiteam.services.notices.render.line_safe: without it a branch
    named feat/http2 or a title with a backtick would make the whole line vanish.
    """
    text = _URL_SCHEME_RE.sub("", text)
    text = text.replace("http", "HTTP").replace("`", "'").replace("](", "] (")
    return _STARS_RE.sub("*", text)


def truncate(text: str, limit: int, tail: bool = False) -> str:
    """Cut to ``limit`` characters including the ellipsis; ``tail`` keeps the end."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return _ELLIPSIS
    return _ELLIPSIS + text[-(limit - 1):] if tail else text[: limit - 1] + _ELLIPSIS


def _fill(template: str, values: dict) -> str:
    template = _PLURAL_RE.sub(
        lambda m: m.group(2) if str(values.get(m.group(1), "")).strip() == "1" else m.group(3), template,
    )
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), template)


def _fit_line(template: str, base: dict, params: dict, limits: dict, tails) -> str:
    """PREFIX + template within MAX_COLUMNS: parameters shrink, static text never does."""
    lengths = {name: min(len(value), limits.get(name, len(value))) for name, value in params.items()}
    used = set(_PLACEHOLDER_RE.findall(template))
    while True:
        values = {name: truncate(value, lengths[name], name in tails) for name, value in params.items()}
        line = PREFIX + _fill(template, dict(base, **values))
        if display_width(line) <= MAX_COLUMNS:
            return line
        shrinkable = [name for name in sorted(used) if name in values and lengths[name] > 1]
        if not shrinkable:
            return line
        widest = max(shrinkable, key=lambda name: display_width(values[name]))
        lengths[widest] -= 1


def render_local(catalog_id: str, params: dict, *, host: str, language: str,
                 variant: str = "", entrypoint: str = "", reliable: bool = True) -> tuple[str, str]:
    """Render a local entry: (user line, coloured on the CC CLI only; model note)."""
    entry = LOCAL_CATALOG[catalog_id]
    texts = entry["variants"].get(variant) or entry["variants"][""]
    language = language if language in ("zh", "en") else "en"
    tails = frozenset(entry["tail_params"])
    base = {"assistant": _ASSISTANT.get(host, "Claude"), "host_app": _HOST_APP.get(host, "Claude Code")}
    cleaned = {name: clean_text((params or {}).get(name, "")) for name in entry["params"]}
    safe = {name: line_safe(value) for name, value in cleaned.items()}
    plain = _fit_line(texts["user"][language], base, safe, dict(entry["params"]), tails)
    body = plain[len(PREFIX):]
    color = _KIND_COLOR.get(entry["kind"], "")
    line = PREFIX + color + body + _FG_RESET if color and host == "cc" and entrypoint == "cli" else plain
    shown = {name: truncate(value, entry["params"][name], name in tails) for name, value in cleaned.items()}
    note = _fill(texts["model"][language], dict(base, line=plain, **shown))
    if entry["frame"] != "raw":
        note = MODEL_HEADER[(language, bool(reliable))].replace("{line}", plain) + "\n" + note
        if entry["frame"] == "notice":
            note += "\n" + MODEL_CLOSING[language]
    return line, note


def _valid_line(line: str) -> str:
    """Why a user line must be dropped, or "" when it is fine."""
    if not line.startswith(PREFIX):
        return "missing prefix"
    if display_width(line) > MAX_COLUMNS:
        return "wider than 160 columns"
    plain = _ANSI_RE.sub("", line)
    if any(token in plain for token in _FORBIDDEN):
        return "Markdown or URL"
    if any(unicodedata.category(ch)[0] == "C" for ch in plain):
        return "control character"
    return ""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def emit(host: str, event: str, *, user_text: str = "", model_text: str = "",
         extra: dict | None = None, delivery_ids: list | tuple | None = None) -> int:
    """Write this hook's one stdout JSON document. Nothing at all when there is nothing to say.

    Invalid user lines are dropped (with a stderr diagnostic), never raised. When
    every line survived, ``delivery_ids`` are recorded as written so the next
    fetch reports them to the ledger. Return the number of stdout characters
    actually written, for the Codex invocation audit (zero on silence/failure).
    """
    global _WROTE_DOCUMENT
    try:
        kept = []
        dropped = 0
        for line in (user_text or "").split("\n"):
            if not line:
                continue
            if host == "codex":
                line = strip_ansi(line)  # Colour is unverified in the Codex TUI.
            why = _valid_line(line)
            if why:
                dropped += 1
                _diag(f"dropped a user line ({why}): {_ANSI_RE.sub('', line)[:80]!r}")
            else:
                kept.append(line)
        doc: dict = {}
        if host == "codex":
            # The ledger supplies one line and at most one delivery for Codex.
            # Fail conservatively if an older/incompatible API violates that
            # contract: never report a hidden second notice as emitted.
            if len(kept) > 1:
                dropped += len(kept) - 1
                kept = kept[:1]
                _diag("codex output accepts one user line; remaining lines dropped")
            if dropped and isinstance(model_text, str):
                for language in ("zh", "en"):
                    model_text = model_text.replace(
                        MODEL_HEADER[(language, True)].split("{line}")[0],
                        MODEL_HEADER[(language, False)].split("{line}")[0],
                    )
            if kept:
                doc["systemMessage"] = "\n".join(kept)
            if isinstance(model_text, str) and model_text:
                doc["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": model_text}
            for key, value in (extra or {}).items():
                if key in _CODEX_TOP_LEVEL and key != "systemMessage":
                    expected_type = str if key == "stopReason" else bool
                    if type(value) is expected_type:
                        doc[key] = value
                    else:
                        _diag(f"codex field {key!r} has an invalid type; dropped")
                else:
                    _diag(f"codex output does not accept field {key!r}; dropped")
        else:
            allowed = _CC_FIELDS.get(event, frozenset())
            specific: dict = {}
            if kept:
                if "systemMessage" in allowed:
                    doc["systemMessage"] = "\n".join(kept)
                else:
                    _diag(f"{event} shows no user lines; dropped {len(kept)}")
            if model_text:
                if "additionalContext" in allowed:
                    specific["additionalContext"] = model_text
                else:
                    _diag(f"{event} carries no model context; dropped")
            for key, value in (extra or {}).items():
                if key not in allowed or key in ("systemMessage", "additionalContext"):
                    _diag(f"{event} output does not accept field {key!r}; dropped")
                    continue
                if key == "permissionDecision" and value != "deny":
                    # Only a block speaks for the permission; allow or ask would
                    # override the user's permission mode (2026-07-27 ruling).
                    _diag(f"{event} permissionDecision {value!r} refused; only deny is sent")
                    continue
                if key == "permissionDecisionReason":
                    # The host shows it to the user as the block's line, colouring it itself.
                    value = strip_ansi(value) if isinstance(value, str) else ""
                    why = _valid_line(value)
                    if why:
                        dropped += 1
                        _diag(f"dropped a block reason ({why}): {value[:80]!r}")
                        continue
                (specific if key in _CC_SPECIFIC else doc)[key] = value
            if "permissionDecisionReason" in specific and "permissionDecision" not in specific:
                _diag(f"{event} block reason without a deny decision; dropped")
                del specific["permissionDecisionReason"]
            if specific:
                doc["hookSpecificOutput"] = {"hookEventName": event, **specific}
        if not doc:
            return 0
        if _WROTE_DOCUMENT:
            _diag("a second output document in one hook run was dropped")
            return 0
        data = json.dumps(doc, ensure_ascii=False)
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
        sys.stdout.write(data)
        sys.stdout.flush()
        _WROTE_DOCUMENT = True
        delivered = bool(kept) and not dropped
        if host == "codex" and delivery_ids and len(delivery_ids) > 1:
            delivered = False
            _diag("codex multiple delivery ids are ambiguous; not recorded as emitted")
        if delivery_ids and delivered:
            record_local(host, "emitted", delivery_ids=[str(i) for i in delivery_ids][:50], event=event)
        return len(data)
    except Exception as exc:  # a notice must never break its hook
        _diag(f"emit failed: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Local records
# ---------------------------------------------------------------------------


def _records_path(host: str) -> Path:
    return os_data_dir() / f"notice-local.{host}.jsonl"


def record_local(host: str, kind: str, **fields) -> None:
    """Append one record (append-only, one line of at most 1KB). Never raises."""
    try:
        record = {
            "uuid": uuid.uuid4().hex,
            "kind": kind,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ts": round(time.time(), 3),
            "source": os.path.basename(sys.argv[0] or "") if sys.argv else "",
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(line.encode("utf-8")) > _RECORD_MAX_BYTES and "params" in record:
            record["params"] = {}
            record["params_dropped"] = True
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        if len(data) > _RECORD_MAX_BYTES + 1:
            _diag(f"record over 1KB not written ({kind})")
            return
        path = _records_path(host)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception as exc:
        _diag(f"local record not written: {exc}")


def _tail_records(host: str, limit: int = _DEDUP_SCAN_BYTES) -> list:
    """Records in the last ``limit`` bytes, oldest first (the rotated file makes up the rest)."""
    chunks = []
    remaining = limit
    for path in (_records_path(host), _records_path(host).with_name(_records_path(host).name + ".1")):
        if remaining <= 0:
            break
        try:
            with open(path, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                start = max(0, size - remaining)
                handle.seek(start)
                data = handle.read()
        except OSError:
            continue
        if start:
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        chunks.insert(0, data)
        remaining -= size
    records = []
    for raw in b"".join(chunks).split(b"\n"):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def seen_local(host: str, session_id: str, key: str, *, events=None) -> bool:
    """Was ``key`` already recorded for this session since its last local clear?

    ``events`` narrows the match to records made at those exits (for example
    only the reliable ones).
    """
    seen = False
    for record in _tail_records(host):
        if record.get("key") != key:
            continue
        if record.get("kind") == "local_clear":
            seen = False
        elif record.get("kind") == "local_notice" and record.get("session_id") == session_id:
            if events is None or record.get("event") in events:
                seen = True
    return seen


def _immediate_shown(host: str, session_id: str) -> int:
    return sum(
        1 for record in _tail_records(host)
        if record.get("kind") == "local_notice" and record.get("session_id") == session_id
        and record.get("catalog_id") in _IMMEDIATE_IDS and record.get("displayed")
    )


def _local_params(catalog_id: str, params: dict) -> dict:
    """A local entry's declared parameters, cleaned and cut to their limits."""
    entry = LOCAL_CATALOG[catalog_id]
    tails = frozenset(entry["tail_params"])
    return {
        name: truncate(clean_text((params or {}).get(name, "")), limit, name in tails)
        for name, limit in entry["params"].items()
    }


def claim_local(catalog_id: str, params: dict, *, host: str, session_id: str, cwd: str,
                event: str, key: str, variant: str = "", events=None,
                immediate: bool = False, reliable: bool = True) -> tuple[str, str] | None:
    """Render a local entry once per session and record it; None means stay silent."""
    try:
        if seen_local(host, session_id, key, events=events):
            return None
        clean = _local_params(catalog_id, params)
        displayed = not (immediate and _immediate_shown(host, session_id) >= _IMMEDIATE_SESSION_CAP)
        language = resolve_language_local(host, cwd)
        entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") if host == "cc" else ""
        line, note = render_local(catalog_id, clean, host=host, language=language, variant=variant,
                                  entrypoint=entrypoint, reliable=reliable)
        record_local(host, "local_notice", catalog_id=catalog_id, key=key, variant=variant,
                     params=clean, session_id=session_id, event=event, displayed=displayed,
                     language=language)
        return (line, note) if displayed else None
    except Exception as exc:
        _diag(f"local notice {catalog_id} not rendered: {exc}")
        return None


def clear_local(host: str, key: str) -> None:
    """Forget per-session dedup for ``key`` (a later occurrence shows again)."""
    record_local(host, "local_clear", key=key)


def block_key(catalog_id: str, params: dict, session_id: str, variant: str = "") -> str:
    params = params or {}
    if catalog_id == "blocked_secret_add":
        return f"blocked_secret_add:{session_id}:{sha8(params.get('file', ''))}"
    if catalog_id == "blocked_teardown":
        return f"blocked_teardown:{session_id}:{sha8(params.get('target', ''))}"
    if catalog_id == "blocked_foreign_branch":
        return f"blocked_foreign_branch:{session_id}:{params.get('branch', '')}"
    return f"{catalog_id}:{session_id}:{variant}"


def local_model_note(catalog_id: str, params: dict, *, host: str, cwd: str, variant: str = "") -> str:
    """A local entry's model note in the session's language, without claiming or recording it."""
    try:
        return render_local(catalog_id, _local_params(catalog_id, params), host=host,
                            language=resolve_language_local(host, cwd), variant=variant)[1]
    except Exception as exc:
        _diag(f"local note {catalog_id} not rendered: {exc}")
        return ""


def emit_block(catalog_id: str, params: dict, *, session_id: str, cwd: str,
               variant: str = "", key: str = "", model_text: str = "") -> bool:
    """Refuse a PreToolUse call: deny with the entry's user line as the reason.

    Claude Code shows the reason to the user as the block's one red line and
    hands the same text to the model, so the line goes out plain (the host
    colours it) and without a systemMessage, which would only repeat it.
    ``model_text``, the hook's own explanation, rides as additionalContext,
    which only the model sees. Every block states its reason; the local record
    that brings it to the Dashboard is written once per key and session.

    Call it right before ``sys.exit(2)`` with the same explanation on stderr:
    Claude Code falls back to stderr only when this document is unusable. Makes
    no HTTP call. Returns whether the document was written.
    """
    try:
        key = key or block_key(catalog_id, params, session_id, variant)
        clean = _local_params(catalog_id, params)
        language = resolve_language_local("cc", cwd)
        reason = render_local(catalog_id, clean, host="cc", language=language, variant=variant)[0]
        if not seen_local("cc", session_id, key):
            record_local("cc", "local_notice", catalog_id=catalog_id, key=key, variant=variant,
                         params=clean, session_id=session_id, event="PreToolUse", displayed=True,
                         language=language)
    except Exception as exc:
        _diag(f"block reason {catalog_id} not rendered: {exc}")
        return False
    return emit("cc", "PreToolUse", model_text=model_text,
                extra={"permissionDecision": "deny", "permissionDecisionReason": reason}) > 0


# ---------------------------------------------------------------------------
# API down marker (E01 shows again after the API was reachable in between)
# ---------------------------------------------------------------------------


def _api_down_flag(host: str) -> Path:
    return os_data_dir() / f"notice-api-down.{host}"


def mark_api_down(host: str) -> None:
    try:
        flag = _api_down_flag(host)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
    except OSError:
        pass


def _api_up(host: str) -> None:
    flag = _api_down_flag(host)
    try:
        if not flag.exists():
            return
        flag.unlink()
    except OSError:
        return
    clear_local(host, "api_down")


# ---------------------------------------------------------------------------
# Fetch from the ledger
# ---------------------------------------------------------------------------


class Pending:
    """What the ledger wants this exit to show."""

    __slots__ = ("language", "user_text", "model_text", "delivery_ids", "project_id")

    def __init__(self, language: str, user_text: str, model_text: str, delivery_ids: list,
                 project_id: str = "") -> None:
        self.language = language
        self.user_text = user_text
        self.model_text = model_text
        self.delivery_ids = delivery_ids
        self.project_id = project_id


def last_failure() -> str:
    """Why the last ``fetch_pending`` returned None: unreachable, timeout, unsupported or error."""
    return _LAST_FAILURE


def _offset_path(host: str) -> Path:
    return _records_path(host).with_name(_records_path(host).name + ".offset")


def _read_offset(host: str) -> tuple[int, int]:
    """(inode, byte offset) of the last import. The inode ties the offset to one file generation."""
    try:
        inode, offset = _offset_path(host).read_text(encoding="utf-8").split()
        return int(inode), max(0, int(offset))
    except (OSError, ValueError):
        return 0, 0


def _write_offset(host: str, inode: int, value: int) -> None:
    """Keep the larger offset of the same file; a new file generation starts over."""
    path = _offset_path(host)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        stored_inode, stored = _read_offset(host)
        if stored_inode == inode and stored >= value:
            return
        temporary.write_text(f"{inode} {value}", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _unimported(host: str) -> tuple[list, int, int]:
    """Records appended since the last import, the file's inode and the offset they end at."""
    path = _records_path(host)
    try:
        with open(path, "rb") as handle:
            stat = os.fstat(handle.fileno())
            stored_inode, offset = _read_offset(host)
            if stored_inode != stat.st_ino or offset > stat.st_size:
                offset = 0
            handle.seek(offset)
            data = handle.read(_IMPORT_MAX_BYTES)
    except OSError:
        return [], 0, 0
    end = data.rfind(b"\n")
    if end < 0:
        return [], stat.st_ino, offset
    records = []
    consumed = 0
    for raw in data[: end + 1].split(b"\n")[:-1]:
        if len(records) >= _IMPORT_MAX_RECORDS:
            break
        consumed += len(raw) + 1
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(record, dict) and record.get("kind") in _IMPORTED_KINDS:
            records.append(record)
    return records, stat.st_ino, offset + consumed


def _rotate(host: str) -> None:
    """Keep one old generation once everything in the current file was imported.

    The old generation holds only imported records, so losing it loses nothing
    that is not already in the ledger.
    """
    path = _records_path(host)
    try:
        stat = path.stat()
        inode, offset = _read_offset(host)
        if stat.st_size <= _ROTATE_BYTES or inode != stat.st_ino or offset < stat.st_size:
            return
        if path.stat().st_ino != stat.st_ino:
            return  # another hook rotated it first
        old = path.with_name(path.name + ".1")
        os.replace(path, old)
        # A record appended between the size check and the rename moved with the
        # file; carry it into the new generation so it is still imported.
        with open(old, "rb") as handle:
            handle.seek(stat.st_size)
            late = handle.read()
        if late:
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, late)
            finally:
                os.close(fd)
    except OSError:
        pass


def _failure(reason: str) -> None:
    global _LAST_FAILURE
    _LAST_FAILURE = reason


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


# Claude Code's global config (not a settings file): CLAUDE_CONFIG_DIR/.claude.json,
# else ~/.claude.json. Read only for the fullscreen crash latch; a larger file is
# treated as unreadable rather than parsed on a prompt.
_GLOBAL_CONFIG_MAX_BYTES = 16 * 1024 * 1024


def _cc_global_config() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(configured).expanduser() if configured else Path.home()) / ".claude.json"


def _fullscreen_crash_latched() -> bool:
    """Did Claude Code switch fullscreen off after crashing in it? Unreadable counts as yes."""
    try:
        path = _cc_global_config()
        if path.stat().st_size > _GLOBAL_CONFIG_MAX_BYTES:
            return True
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return True
    return not isinstance(config, dict) or bool(config.get("fullscreenAutoDisabled"))


def tui_env() -> str:
    """What this session's side forces on the renderer, "" when the ``tui`` setting decides.

    The API reads the settings but sees neither this environment nor Claude
    Code's global config, so the exits report them, in Claude Code's own order:
    screen-reader mode (CLAUDE_AX_SCREEN_READER) keeps the classic renderer;
    CLAUDE_CODE_NO_FLICKER=0 or CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN turns
    fullscreen off and CLAUDE_CODE_NO_FLICKER=1 on; after that, a fullscreen
    session that crashed leaves ``fullscreenAutoDisabled`` in the global config
    and the classic renderer is used until the user turns fullscreen back on.
    "default" whenever that config cannot be read: a /clear line then counts as
    unconfirmed, and an action-level line is shown once more rather than lost.
    """
    if _env_flag("CLAUDE_AX_SCREEN_READER"):
        return "default"
    no_flicker = _env_flag("CLAUDE_CODE_NO_FLICKER")
    if _env_flag("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN") or no_flicker is False:
        return "default"
    if no_flicker:
        return "fullscreen"
    return "default" if _fullscreen_crash_latched() else ""


def fetch_pending(host: str, event: str, source: str, payload: dict, *, reader: str = "",
                  project_id: str = "", timeout: float) -> Pending | None:
    """POST /api/notices/pending with this host's local records. None on any failure, never raises."""
    import urllib.error
    import urllib.request

    _failure("")
    payload = payload if isinstance(payload, dict) else {}
    try:
        records, inode, end = _unimported(host)
        emitted = []
        for record in records:
            if record.get("kind") == "emitted":
                emitted.extend(str(i) for i in record.get("delivery_ids") or [] if i)
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
        body = {
            "host": host,
            "event": event,
            "source": source or "",
            "session_id": str(payload.get("session_id") or "")[:256],
            "cwd": cwd or os.getcwd(),
            "project_id": project_id or "",
            "reader": reader or "",
            "transcript_path": str(payload.get("transcript_path") or "")[:4096],
            "facts": {
                "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") if host == "cc" else "",
                "tui_env": tui_env() if host == "cc" else "",
                "fallback_language": system_language() or "",
                "local_records": records,
                "emitted": emitted[:500],
            },
        }
        request = urllib.request.Request(
            f"{api_url()}/api/notices/pending",
            # ASCII escapes, not raw UTF-8: a lone surrogate (from a path, say) has no
            # UTF-8 form and would stop the request here; escaped, it reaches the API,
            # which replaces it with U+FFFD for this route.
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            _failure("error")
            return None
        document = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _failure("unsupported" if exc.code in (404, 405) else "error")
        return None
    except urllib.error.URLError as exc:
        reason = exc.reason
        timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason)
        _failure("timeout" if timed_out else "unreachable")
        return None
    except TimeoutError:
        _failure("timeout")
        return None
    except ConnectionError:
        _failure("unreachable")
        return None
    except Exception:
        _failure("error")
        return None
    if isinstance(document, dict) and document.get("success") is False:
        _failure("error")
        return None
    if isinstance(document, dict) and isinstance(document.get("data"), dict):
        document = document["data"]
    if not isinstance(document, dict):
        _failure("error")
        return None
    user_text = document.get("user_text")
    model_text = document.get("model_text")
    ids = document.get("delivery_ids")
    language = document.get("language")
    resolved_project_id = document.get("project_id", "")
    # An old API may answer 200 with an unrelated object. Treat that as a
    # failed fetch so callers retain their channel fallback, without advancing
    # the local import offset or clearing a service-down marker.
    if (language not in ("zh", "en") or not isinstance(user_text, str)
            or not isinstance(model_text, str) or not isinstance(ids, list)
            or not isinstance(resolved_project_id, str)
            or any(not isinstance(item, str) or not item for item in ids)):
        _failure("error")
        return None
    if inode:
        _write_offset(host, inode, end)
        _rotate(host)
    _api_up(host)
    return Pending(
        language if language in ("zh", "en") else "en",
        user_text if isinstance(user_text, str) else "",
        model_text if isinstance(model_text, str) else "",
        [str(i) for i in ids if isinstance(i, (str, int))] if isinstance(ids, list) else [],
        resolved_project_id,
    )


# ---------------------------------------------------------------------------
# Install state (written by auto_install, read by session_bootstrap)
# ---------------------------------------------------------------------------


def install_state_path() -> Path:
    return os_data_dir() / "install-state.json"


def read_install_state() -> dict:
    try:
        state = json.loads(install_state_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def install_in_progress(state: dict, now: float | None = None) -> bool:
    """An install that started less than INSTALL_STALE_S ago and has not finished."""
    if state.get("phase") != "installing":
        return False
    try:
        started = float(state.get("started_at") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= (time.time() if now is None else now) - started < INSTALL_STALE_S
