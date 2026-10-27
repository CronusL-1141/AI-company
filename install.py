#!/usr/bin/env python3
"""AI Team OS one-click installer."""
import functools
import json
import os
import shutil
import subprocess
import sys

# 版本门卫必须先于任何含 PEP 604 注解（X | Y）的 def——Python <3.10 在 def 时
# 即抛 TypeError，放在 main() 里的检查永远执行不到（macOS 自带 3.9.6 首当其冲）。
if sys.version_info < (3, 11):  # noqa: UP036
    _v = ".".join(str(x) for x in sys.version_info[:3])
    print(f"[FAIL] Python 3.11+ required, got {_v} - try: python3 install.py")
    sys.exit(1)

from pathlib import Path

# Marker in the first line of plugin/loop.md. If the config folder's loop.md lacks
# it, the file is treated as user-customized and never overwritten.
LOOP_TEMPLATE_SENTINEL = "ai-team-os-loop-template"

MCP_SERVER_NAME = "ai-team-os"


@functools.cache
def _hook_core():
    """The path section every hook and the package share (src/aiteam/hooks/hook_core.py), by path.

    The installer is standard library only and runs before aiteam is importable,
    so it loads the one definition from this checkout instead of spelling paths
    itself. Every location is resolved again on each call: only the module is kept.
    """
    import importlib.util

    path = Path(__file__).resolve().parent / "src" / "aiteam" / "hooks" / "hook_core.py"
    spec = importlib.util.spec_from_file_location("_aiteam_install_hook_core", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_dir() -> Path:
    """The Claude Code config folder this run installs into (docs/config-dir-compat-design.md §4.2)."""
    return _hook_core().cc_config_dir()


def apply_config_dir(config_dir: str | None) -> None:
    """``--config-dir``: install into this Claude Code config folder instead of the session's.

    Applied as CLAUDE_CONFIG_DIR for this process and everything it starts (the
    claude CLI included), so every step resolves the same folder through the path
    section. Naming the default folder drops the variable: the sessions using it
    run without one and read the global config file next to it, not inside it.
    The folder must exist: a mistyped path would otherwise become a new, empty
    config folder that no session reads.
    """
    if not config_dir:
        return
    chosen = os.path.abspath(os.path.expanduser(config_dir))
    if not os.path.isdir(chosen):
        print(f"[ERROR] --config-dir {chosen} does not exist. Name an existing Claude Code config folder "
              "(start Claude Code with CLAUDE_CONFIG_DIR set to it once, or create it), then run again.")
        raise SystemExit(2)
    if chosen == os.path.abspath(os.path.expanduser(_hook_core().CC_DEFAULT_CONFIG_DIR)):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = chosen


def print_install_target() -> None:
    """Say up front where this run writes: the config folder, its global config file, the data directory."""
    core = _hook_core()
    origin = "CLAUDE_CONFIG_DIR" if os.environ.get("CLAUDE_CONFIG_DIR", "").strip() else "default"
    resolution = core.resolve_data_dir()
    print(f"[..] Install target: Claude Code config folder {core.cc_config_dir()} ({origin})")
    print(f"     global config {core.cc_global_config()}; OS data directory {resolution.path} ({resolution.mode})")


def source_version(project_root: Path) -> str:
    """__version__ of this checkout (src/aiteam/__init__.py), read as text; "unknown" when unreadable."""
    init = project_root / "src" / "aiteam" / "__init__.py"
    try:
        for line in init.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip('"').strip("'")
    except (OSError, IndexError):
        pass
    return "unknown"


def prepare_data_dir() -> Path | None:
    """Step 5: resolve the OS data directory and create it only when nothing exists anywhere.

    docs/config-dir-compat-design.md §5.2. A legacy directory is used where it
    is (moving it is a later update's job); with two databases the canonical one
    is used and both are named; data left elsewhere (a moved Claude Code config
    folder) is never shadowed by a new empty directory. None means "do not go on".
    """
    core = _hook_core()
    resolution, created = core.create_data_dir_if_absent()
    if resolution.mode == "missing":
        print("[FAIL] OS data directory not found, and an earlier one was left elsewhere:")
        for candidate in resolution.evidence:
            if candidate.state != "absent":
                print(f"       {candidate.path} ({candidate.state})")
        print(f"       Move that folder to {resolution.path} (or set AITEAM_DATA_DIR to it), then run")
        print("       install.py again. Nothing was created, so the old data cannot be shadowed.")
        return None
    label = {"env": "AITEAM_DATA_DIR", "canonical": "", "legacy": "legacy location, used in place",
             "conflict": "two databases found, using this one"}[resolution.mode]
    print(f"[OK] Data directory: {resolution.path}" + (" (created)" if created else "")
          + (f" ({label})" if label else ""))
    if resolution.mode == "conflict":
        for candidate in resolution.evidence:
            if candidate.state == "database":
                print(f"[WARN]   {candidate.path}: {candidate.db_size} bytes, user_version "
                      f"{candidate.user_version}")
        print("[WARN] Keep one of them; run aiteam paths to compare. Nothing was merged or removed.")
    return resolution.path


def check_command(cmd: str) -> bool:
    """Check if a command is available."""
    return shutil.which(cmd) is not None


def run(args: list[str], cwd: str | None = None, **kwargs) -> subprocess.CompletedProcess:
    """Run subprocess, print friendly error on failure."""
    try:
        return subprocess.run(
            args, cwd=cwd, check=True,
            shell=(sys.platform == "win32" and args[0] in ("npm", "npx")),
            **kwargs,
        )
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] Command failed: {' '.join(args)}")
        raise SystemExit(1) from e
    except FileNotFoundError:
        print(f"[FAIL] Command not found: {args[0]}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Hook registration surface
# ---------------------------------------------------------------------------
# HOOK_SURFACE is the single source of truth for the source-install chain and
# must stay 1:1 with plugin/hooks/hooks.json (event set, matcher, script, arg and
# timeout). scripts/check_invariants.sh I8 machine-checks the two against each
# other — before that check existed the two paths shipped two different OSes
# (the source path was missing TaskCreated / UserPromptSubmit / PermissionDenied /
# PreCompact entirely, and matched PreToolUse hooks with the wrong matcher).
#
# auto_install.py is deliberately absent: it is the plugin's self-heal entry and
# must never enter the installed chain (it would re-register the chain from
# inside the chain). I8 whitelists it as plugin-only.
#
# Entry form: (event, matcher, [(script, arg, timeout), ...]); matcher "" means
# the group carries no matcher key at all, matching the plugin manifest.
HOOK_SURFACE: list[tuple[str, str, list[tuple[str, str, int]]]] = [
    ("SessionStart", "", [
        ("session_bootstrap.py", "", 15),
        ("send_event.py", "SessionStart", 5),
    ]),
    # On resume and fork CC drops a SessionStart batch that holds nothing new,
    # notice line included; this one-line tick is new on every start, so the
    # batch (and the line) survives while the repeated briefing is still deduped.
    ("SessionStart", "resume|fork", [
        ("session_bootstrap.py", "resume-tick", 5),
    ]),
    ("SubagentStart", "", [
        ("inject_subagent_context.py", "", 10),
        ("send_event.py", "SubagentStart", 5),
    ]),
    ("SubagentStop", "", [
        ("send_event.py", "SubagentStop", 5),
    ]),
    # workflow_reminder only speaks for the tools it can act on; send_event keeps
    # the full "*" telemetry surface (its own inert-tool guard caps the cost).
    ("PreToolUse", "Agent|Bash|Edit|Write|Workflow", [
        ("workflow_reminder.py", "PreToolUse", 5),
    ]),
    ("PreToolUse", "*", [
        ("send_event.py", "PreToolUse", 5),
    ]),
    # workflow_reminder has no PostToolUse entry: it exits at once on that event,
    # so registering it there only spawned a process per tool call.
    ("PostToolUse", "*", [
        ("send_event.py", "PostToolUse", 5),
    ]),
    # A failed or interrupted tool call gets this instead of PostToolUse; without it
    # the running activity row its PreToolUse opened was never closed.
    ("PostToolUseFailure", "*", [
        ("send_event.py", "PostToolUseFailure", 5),
    ]),
    # Reads stdin and takes no argv (plugin mode registers it from hooks.json; the
    # source install once copied but never registered it, so deep-review auto-link
    # silently died, found by end-to-end test 2026-07-10).
    ("PostToolUse", "mcp__ai-team-os__report_save", [
        ("deep_review_link.py", "", 5),
    ]),
    # TaskCompleted / TaskCreated 只观测、不上墙。宿主 08-18 起不再提供 TaskCreate
    # 任务清单工具，镜像 CC 任务上墙的桥已于 09-23 退役（见 RETIRED_HOOK_SCRIPTS）。
    ("TaskCompleted", "", [
        ("send_event.py", "TaskCompleted", 5),
    ]),
    ("TaskCreated", "", [
        ("send_event.py", "TaskCreated", 5),
    ]),
    # CC's own "this teammate stopped working" signal. Wired for parallel
    # observation against the OS's transcript-mtime liveness track (C13); it does
    # not yet decide anything.
    ("TeammateIdle", "", [
        ("send_event.py", "TeammateIdle", 5),
    ]),
    ("SessionEnd", "", [
        ("send_event.py", "SessionEnd", 5),
    ]),
    ("Stop", "", [
        ("send_event.py", "Stop", 5),
        # 盲停守卫：有活在飞却没武装 watcher 时拦一道。脚本与测试早就在，
        # 却一直零注册面——本该拦住"忘记武装"的守卫自己没上岗。
        ("turn_end_guard.py", "", 5),
    ]),
    ("UserPromptSubmit", "", [
        ("context_tracker.py", "", 5),
        # 信道未读徽章。argv 显式带角色标识——身份不嗅探环境，也不用 session_id
        # （按会话记水位会让每开一个新会话就把历史消息重算成未读）。
        ("channel_unread.py", "leader-cc", 5),
        # 用户在场标记：用户每次发言刷新 manual 窗口，Stop 守卫在窗口内不拦收工。
        ("turn_end_guard.py", "user-prompt", 5),
    ]),
    ("PermissionDenied", "", [
        ("permission_denied_recovery.py", "", 5),
    ]),
    ("PreCompact", "", [
        ("pre_compact_save.py", "", 10),
    ]),
    # 与 PreCompact 配对：压缩真的完成了（PreCompact 触发后压缩仍可能取消），
    # 检查点据此收口。
    ("PostCompact", "", [
        ("send_event.py", "PostCompact", 5),
    ]),
    # 隔离工作区的消失。只挂 WorktreeRemove（观测型）——WorktreeCreate 是
    # CC 的"接管型"hook：注册即替代默认 git 创建逻辑，必须返回 worktree 路径，
    # 无旁观模式；挂观测钩会让全机所有 isolation:worktree 派发失败
    # （2026-07-28 Wenge 现场事故）。创建侧观测走 worktree_probe 兜底。
    ("WorktreeRemove", "", [
        ("send_event.py", "WorktreeRemove", 5),
    ]),
]

# Every script the source install distributes, derived from the surface above so
# a new hook can never be registered without being copied.
HOOK_SCRIPTS: tuple[str, ...] = tuple(
    dict.fromkeys(script for _e, _m, entries in HOOK_SURFACE for script, _a, _t in entries)
)

# Modules the registered hooks import from their own directory. They are not
# hooks (nothing registers them), but a hook copied without them loses every
# user-visible line it would show. I22 checks that every such import is listed.
HOOK_SUPPORT_MODULES: tuple[str, ...] = (
    "user_notice.py",  # the single exit for user-visible hook lines
    "hook_core.py",    # the one cleaner for quoted text injected into model context
    "hook_delivery.py",  # the one classified, ledgered POST to /api/hooks/event (I25)
)

# Hooks that were retired from the manifest. Their runtime copies and settings.json
# entries are actively removed on install/update — otherwise a retired hook keeps
# firing forever on every machine that ever installed it.
RETIRED_HOOK_SCRIPTS: tuple[str, ...] = (
    "task_completed_gate.py",   # retired 2026-07-27: CC task ids are ints, OS ids UUIDs
    "pipeline_gate.py",         # retired with the pipeline subsystem
    "autopilot_auto_stop.py",
    # retired 2026-09-23: the host no longer offers the TaskCreate checklist tool,
    # so the bridge that mirrored finished CC tasks onto the OS wall has nothing
    # left to mirror
    "cc_task_bridge.py",
    # retired 2026-09-23: its PostToolUse stdout never reached the model; the
    # ecosystem writeback hint now rides in the meeting_conclude result instead
    "meeting_ecosystem_writeback.py",
)

# Slash commands retired from plugin/commands/. Same rule as retired hooks: the
# copy under ~/.claude/commands/ is removed on install/update, otherwise a
# command that no longer exists in the source keeps showing up in every
# machine that ever installed it. scripts/uninstall.py carries the same list.
RETIRED_COMMAND_FILES: tuple[str, ...] = (
    "os-init.md",   # retired 2026-09-14: it wrote aiteam.yaml, which nothing reads
)

RUNTIME_HOOKS_DIRNAME = "ai-team-os"


def _is_our_hook(command: str) -> bool:
    """True only for hook commands this installer owns.

    Both spellings of the runtime directory count: the absolute path older
    installs (and Windows) write, and the ``${CLAUDE_CONFIG_DIR:-...}`` form POSIX
    installs write now; both contain ``/hooks/ai-team-os/``. Deliberately
    name-based rather than "is 'ai-team-os' in the path": the runtime dir also
    hosts hooks shipped by other branches of this project (e.g. the channel
    sentinel) and users add their own hooks next to ours. Purging by path
    substring would silently delete them on every update.
    """
    marker = f"/hooks/{RUNTIME_HOOKS_DIRNAME}/"
    normalized = command.replace("\\", "/")
    if marker not in normalized:
        return False
    # Isolate the script name: '<py> "<dir>/send_event.py" PreToolUse' → send_event.py
    name = normalized.split(marker, 1)[1].split('"')[0].split(" ")[0].strip()
    return name in HOOK_SCRIPTS or name in RETIRED_HOOK_SCRIPTS


def copy_hook_scripts(project_root: Path) -> int:
    """Refresh the config folder's hooks/ai-team-os/ from plugin/hooks/ and drop retired copies."""
    src_hooks_dir = project_root / "plugin" / "hooks"
    installed_hooks_dir = _hook_core().cc_main_chain_dir()
    installed_hooks_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for fname in HOOK_SCRIPTS + HOOK_SUPPORT_MODULES:
        src = src_hooks_dir / fname
        if src.exists():
            shutil.copy2(src, installed_hooks_dir / fname)
            copied += 1
        else:
            print(f"[WARN] Hook source missing, skipped: {src}")

    for fname in RETIRED_HOOK_SCRIPTS:
        stale = installed_hooks_dir / fname
        if stale.exists():
            stale.unlink()
            print(f"[OK] Removed retired hook: {stale.name}")

    print(f"[OK] Hook scripts copied ({copied}/{len(HOOK_SCRIPTS) + len(HOOK_SUPPORT_MODULES)})"
          f" → {installed_hooks_dir}")
    return copied


def _quoted(value: Path | str) -> str:
    """Double-quoted, forward slashes: survives spaces, and the shell still expands ${...} inside."""
    return f'"{str(value).replace(chr(92), "/")}"'


def hook_command(script: str, arg: str = "") -> str:
    """The settings.json command for one main-chain script.

    ``"<this interpreter>" "<runtime dir>/<script>" [arg]``, with the runtime dir
    spelled by the path section (``${CLAUDE_CONFIG_DIR:-...}`` on POSIX, the
    absolute path on Windows). Byte-identical to auto_install's commands, so the
    plugin self-heal path and this installer can never double-register the chain.
    """
    command = f"{_quoted(sys.executable)} {_quoted(_hook_core().cc_main_chain_command_dir() + '/' + script)}"
    return f"{command} {arg}" if arg else command


def _drop_our_hooks(existing_hooks: dict) -> int:
    """Remove every hook entry this installer owns (stale matchers/timeouts/retired hooks); returns how many."""
    removed = 0
    for event in list(existing_hooks):
        surviving_groups = []
        for group in existing_hooks.get(event) or []:
            kept = [h for h in group.get("hooks", []) if not _is_our_hook(h.get("command", ""))]
            removed += len(group.get("hooks", [])) - len(kept)
            if kept:
                surviving_groups.append({**group, "hooks": kept})
        if surviving_groups:
            existing_hooks[event] = surviving_groups
        else:
            del existing_hooks[event]
    return removed


def register_hooks(project_root: Path) -> None:
    """Copy hook scripts to the config folder's hooks/ai-team-os/, register them, and record the owner.

    Our own entries are rebuilt from HOOK_SURFACE on every run (so a matcher or
    timeout change actually lands, retired hooks disappear, and absolute-path
    commands of older installs become the ``${CLAUDE_CONFIG_DIR:-...}`` form);
    every hook we do not own is left exactly where it was. The chain is then
    marked as this source install's (``.chain-owner.json``), which is what tells
    a source chain from a plugin's (docs/config-dir-compat-design.md §4.3).
    """
    copy_hook_scripts(project_root)

    core = _hook_core()
    hooks_dir = core.cc_main_chain_dir()
    settings_path = core.cc_config_dir() / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _read_json(settings_path)
    existing_hooks: dict = settings.setdefault("hooks", {})
    removed = _drop_our_hooks(existing_hooks)

    added = 0
    for event, matcher, entries in HOOK_SURFACE:
        entries = [e for e in entries if (hooks_dir / e[0]).exists()]
        if not entries:
            continue
        event_list: list = existing_hooks.setdefault(event, [])
        target_group = next((g for g in event_list if g.get("matcher", "") == matcher), None)
        if target_group is None:
            target_group = {"matcher": matcher, "hooks": []} if matcher else {"hooks": []}
            event_list.append(target_group)
        for script, arg, timeout in entries:
            target_group.setdefault("hooks", []).append({
                "type": "command",
                "command": hook_command(script, arg),
                "timeout": timeout,
            })
            added += 1

    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    events = len({event for event, _m, _e in HOOK_SURFACE})
    print(f"[OK] Registered {added} hook(s) across {events} event(s) into {settings_path}"
          f" (replaced {removed} previous entr{'y' if removed == 1 else 'ies'})")
    owner = core.write_chain_owner("source", baseline=str(project_root), version=source_version(project_root))
    print(f"[OK] Main chain owner recorded: source → {owner}")


def copy_agent_templates(project_root: Path, overwrite: bool = False) -> None:
    """Copy plugin/agents/*.md to the config folder's agents/.

    Source is plugin/agents (the plugin-mode superset: 25 templates, incl.
    debate-advocate/debate-critic/team-member). Using the same source as the
    plugin keeps independent-install parity with marketplace-install. Never
    .claude/agents: those are project-level overrides carrying facts about
    developing this repo only (I23 keeps them a prefix-extension of plugin/agents).

    Args:
        project_root: Root directory of the ai-team-os project.
        overwrite: When True (update mode), overwrite existing templates.
                   When False (fresh install), skip existing files.
    """
    src_agents = project_root / "plugin" / "agents"
    dst_agents = _config_dir() / "agents"

    if not src_agents.exists():
        print("[SKIP] No agent templates found in plugin/agents/")
        return

    dst_agents.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for template in src_agents.glob("*.md"):
        dst = dst_agents / template.name
        if dst.exists() and not overwrite:
            skipped += 1
        else:
            shutil.copy2(template, dst)
            copied += 1

    if overwrite:
        print(f"[OK] Agent templates: {copied} refreshed → {dst_agents}")
    else:
        print(f"[OK] Agent templates: {copied} copied, {skipped} already existed (skipped)")


def copy_skills(project_root: Path, overwrite: bool = False) -> None:
    """Copy plugin/skills/<name>/ trees to the config folder's skills/<name>/.

    CC discovers user-level skills at <config folder>/skills/<name>/SKILL.md, so each
    skill is a whole directory (SKILL.md plus nested resources like templates/).
    We copy the tree recursively. Without this, uninstalling the plugin drops the
    /os-* skills entirely on independent installs (they were never distributed).

    Args:
        project_root: Root directory of the ai-team-os project.
        overwrite: When True (update mode), refresh files in existing skill dirs.
                   When False (fresh install), skip skills whose dir already exists
                   (never clobber a user-customized skill).
    """
    src_skills = project_root / "plugin" / "skills"
    dst_skills = _config_dir() / "skills"

    if not src_skills.exists():
        print("[SKIP] No skills found in plugin/skills/")
        return

    dst_skills.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for skill_dir in sorted(p for p in src_skills.iterdir() if p.is_dir()):
        dst = dst_skills / skill_dir.name
        if dst.exists() and not overwrite:
            skipped += 1
            continue
        # dirs_exist_ok merges on update: our files overwrite, user-added files stay.
        shutil.copytree(skill_dir, dst, dirs_exist_ok=True)
        copied += 1

    if overwrite:
        print(f"[OK] Skills: {copied} refreshed → {dst_skills}")
    else:
        print(f"[OK] Skills: {copied} copied, {skipped} already existed (skipped)")


def copy_commands(project_root: Path, overwrite: bool = False) -> None:
    """Copy plugin/commands/*.md to the config folder's commands/.

    CC discovers user-level slash commands at <config folder>/commands/*.md. Without
    this, the /os-* commands vanish when the plugin is uninstalled on independent
    installs (they were never distributed by the source installer).

    Args:
        project_root: Root directory of the ai-team-os project.
        overwrite: When True (update mode), overwrite existing command files.
                   When False (fresh install), skip existing files.
    """
    src_commands = project_root / "plugin" / "commands"
    dst_commands = _config_dir() / "commands"

    if not src_commands.exists():
        print("[SKIP] No commands found in plugin/commands/")
        return

    dst_commands.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for command in sorted(src_commands.glob("*.md")):
        dst = dst_commands / command.name
        if dst.exists() and not overwrite:
            skipped += 1
        else:
            shutil.copy2(command, dst)
            copied += 1

    for fname in RETIRED_COMMAND_FILES:
        stale = dst_commands / fname
        if stale.exists():
            stale.unlink()
            print(f"[OK] Removed retired command: {stale.name}")

    if overwrite:
        print(f"[OK] Commands: {copied} refreshed → {dst_commands}")
    else:
        print(f"[OK] Commands: {copied} copied, {skipped} already existed (skipped)")


def install_loop_md(project_root: Path) -> None:
    """Install the OS `/loop` maintenance prompt to the config folder's loop.md (idempotent).

    session_bootstrap.py tells every session the maintenance prompt lives in its
    config folder's loop.md, so a source install must actually write it (previously only
    the now-deprecated scripts/install.py did — this is the surviving single path).
    Installed at user level so bare `/loop` picks it up in any project. If the
    destination exists without our sentinel it is treated as user-customized and
    left untouched.
    """
    src = project_root / "plugin" / "loop.md"
    dst = _config_dir() / "loop.md"

    if not src.is_file():
        print(f"[WARN] loop.md template missing at {src}")
        return

    if dst.exists():
        existing = dst.read_text(encoding="utf-8", errors="replace")
        if LOOP_TEMPLATE_SENTINEL not in existing:
            print(f"[SKIP] {dst} exists and looks user-customized - left untouched")
            return

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[OK] /loop maintenance prompt → {dst}")


# Plugin identity as it appears in CC's own records: the key is
# "<plugin>@<marketplace>", so the marketplace part is matched by prefix.
PLUGIN_KEY_PREFIX = "ai-team-os@"

# plugin_install_state() results.
PLUGIN_ENABLED = "enabled"          # explicitly enabled, its MCP server is already loaded
PLUGIN_PRESENT_UNKNOWN = "present"  # installed, but no explicit enable/disable on record
PLUGIN_ABSENT = "absent"            # not installed


def _read_json(path: Path) -> dict:
    """Read a JSON object, or return an empty dict for anything unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_install_state() -> str:
    """Report whether the ai-team-os CC plugin is already active in the target config folder.

    The plugin ships its own MCP server entry. Registering the source install
    globally on top of an enabled plugin loads the same server twice, so the
    two install paths have to see each other.

    Enablement needs an explicit ``true`` in settings.json ``enabledPlugins``:
    a plugin can be installed and switched off, and reading "installed" as
    "active" would skip the registration that such a setup depends on. When a
    plugin is installed with no explicit entry, say so and keep registering -
    a duplicate server is a far smaller problem than no server at all.
    """
    config = _config_dir()
    enabled = _read_json(config / "settings.json").get("enabledPlugins")
    if isinstance(enabled, dict):
        for key, value in enabled.items():
            if isinstance(key, str) and key.startswith(PLUGIN_KEY_PREFIX) and value is True:
                return PLUGIN_ENABLED

    installed = _read_json(config / "plugins" / "installed_plugins.json")
    plugins = installed.get("plugins")
    if isinstance(plugins, dict):
        for key in plugins:
            if isinstance(key, str) and key.startswith(PLUGIN_KEY_PREFIX):
                return PLUGIN_PRESENT_UNKNOWN

    return PLUGIN_ABSENT


def _mcp_registered(global_config: Path) -> bool:
    servers = _read_json(global_config).get("mcpServers")
    return isinstance(servers, dict) and MCP_SERVER_NAME in servers


def _default_global_config() -> Path:
    """The global config file of sessions that run without CLAUDE_CONFIG_DIR."""
    return Path(os.path.expanduser(_hook_core().CC_DEFAULT_GLOBAL_CONFIG))


def _register_with_cli(mcp_entry: dict) -> bool:
    """``claude mcp add-json --scope user``; the CLI inherits CLAUDE_CONFIG_DIR (set by apply_config_dir)."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "add-json", "--scope", "user", MCP_SERVER_NAME, json.dumps(mcp_entry)],
            capture_output=True, text=True, timeout=15, env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _write_global_mcp(global_config: Path, mcp_entry: dict) -> None:
    """Add the server to the global config file directly (merging; an unparsable file is left alone)."""
    try:
        claude_json = json.loads(global_config.read_text(encoding="utf-8")) if global_config.exists() else {}
        if not isinstance(claude_json, dict):
            raise ValueError("not a JSON object")
        servers = claude_json.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers is not a JSON object")
        servers[MCP_SERVER_NAME] = mcp_entry
        global_config.parent.mkdir(parents=True, exist_ok=True)
        global_config.write_text(json.dumps(claude_json, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[OK] Registered global MCP in {global_config}")
    except (OSError, ValueError) as e:
        print(f"[WARN] Could not register global MCP in {global_config}: {e}")


def register_global_mcp(project_root: Path, *, force: bool = False) -> None:
    """Register the ai-team-os MCP server in the target config folder's global config file.

    Claude Code loads global MCP servers from its global config file (not from
    settings.json): ``.claude.json`` inside a configured CLAUDE_CONFIG_DIR, next
    to the default folder otherwise (hook_core.cc_global_config). The claude CLI
    is tried first; whether it wrote there is then checked by reading that file,
    since where the CLI writes under CLAUDE_CONFIG_DIR is not documented. When
    the entry is not there it is written directly. Only when that fails too is a
    project-level .mcp.json generated as the fallback.

    Skipped when the CC plugin is already enabled, because the plugin brings
    its own copy of the same MCP server. Pass ``force=True`` (installer flag
    ``--force-mcp``) to register anyway.
    """
    global_config = _hook_core().cc_global_config()
    state = plugin_install_state()
    if state == PLUGIN_ENABLED and not force:
        print(
            "[SKIP] CC plugin 'ai-team-os' is enabled and already provides this MCP "
            "server - skipping global registration (use --force-mcp to register anyway)"
        )
        return
    if state == PLUGIN_PRESENT_UNKNOWN:
        print(
            "[WARN] CC plugin 'ai-team-os' is installed but not explicitly enabled in "
            "settings.json - registering globally anyway; if you later enable the "
            f"plugin, remove 'ai-team-os' from {global_config} to avoid two servers"
        )

    mcp_entry = {
        "command": sys.executable,
        "args": ["-m", "aiteam.mcp.server"],
    }
    if _register_with_cli(mcp_entry) and _mcp_registered(global_config):
        print(f"[OK] Registered global MCP via 'claude mcp add-json --scope user' (verified in {global_config})")
    elif _mcp_registered(global_config):
        print(f"[OK] Global MCP 'ai-team-os' already in {global_config}")
    else:
        _write_global_mcp(global_config, mcp_entry)

    # Only generate project-level .mcp.json if global registration failed
    if not _mcp_registered(global_config):
        print("[WARN] Global MCP registration failed, falling back to project-level .mcp.json")
        _write_project_mcp_json(project_root)


def _write_project_mcp_json(project_root: Path) -> None:
    """Write project-level .mcp.json as fallback when global registration fails."""
    mcp_json = project_root / ".mcp.json"
    config = {
        "mcpServers": {
            MCP_SERVER_NAME: {
                # sys.executable, not bare "python": this fallback fires exactly when
                # global registration failed — don't stack a venv-hijack risk on top (e2d0fbb).
                "command": sys.executable,
                "args": ["-m", "aiteam.mcp.server"],
                "cwd": str(project_root).replace("\\", "/"),
            }
        }
    }
    mcp_json.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"[OK] Generated project-level .mcp.json at {mcp_json}")


def verify_installation(project_root: Path) -> bool:
    """Run post-install checks against the config folder this run installed into."""
    print()
    print("Verifying installation...")

    core = _hook_core()
    config = core.cc_config_dir()
    agents_dir = config / "agents"
    has_templates = agents_dir.exists() and any(agents_dir.glob("*.md"))

    # Skills / commands: derive expected names from the plugin source (source of
    # truth) and confirm each landed in the user-level dir. Deriving avoids a
    # hardcoded list drifting out of sync when assets are added/removed.
    skills_ok, skills_detail = _verify_assets_present(
        project_root / "plugin" / "skills",
        config / "skills",
        lambda p: [d.name for d in p.iterdir() if d.is_dir()],
        lambda dst, name: (dst / name / "SKILL.md").exists(),
    )
    commands_ok, commands_detail = _verify_assets_present(
        project_root / "plugin" / "commands",
        config / "commands",
        lambda p: [f.name for f in p.glob("*.md")],
        lambda dst, name: (dst / name).exists(),
    )

    settings_path = config / "settings.json"
    has_hooks = bool(_read_json(settings_path).get("hooks"))
    owner = core.chain_owner() or {}

    # Must be the file register_global_mcp writes, or verify always fails.
    global_config = core.cc_global_config()
    has_global_mcp = _mcp_registered(global_config)

    # Project-level .mcp.json is present if global registration failed (fallback)
    has_project_mcp = (project_root / ".mcp.json").exists()

    # The plugin ships the same MCP server, so exactly one of the two paths
    # should provide it. Both at once means every session loads it twice.
    plugin_state = plugin_install_state()
    plugin_enabled = plugin_state == PLUGIN_ENABLED
    one_mcp_source = not (plugin_enabled and has_global_mcp)
    mcp_source_label = (
        "MCP server registered once ("
        + ("plugin enabled" if plugin_enabled else f"plugin {plugin_state}")
        + ", global "
        + ("present" if has_global_mcp else "absent")
        + ")"
    )

    # (label, ok, required) - a not-required check prints [WARN] instead of
    # [FAIL] and never fails the install.
    checks = [
        # Global registration is what the source install provides; when the
        # plugin is enabled it is deliberately absent, so it is not required.
        (f"Global MCP in {global_config}", has_global_mcp, not plugin_enabled),
        (mcp_source_label, one_mcp_source, False),
        ("Project .mcp.json (fallback)", has_project_mcp, False),
        (f"{agents_dir}/ templates{_asset_suffix(agents_dir, '*.md')}", has_templates, True),
        (f"{config / 'skills'}/ ({skills_detail})", skills_ok, True),
        (f"{config / 'commands'}/ ({commands_detail})", commands_ok, True),
        (f"{config / 'loop.md'} (/loop prompt)", (config / "loop.md").exists(), True),
        (f"{settings_path} hooks", has_hooks, True),
        (f"Main chain owner {core.CHAIN_OWNER_FILE} (source)", owner.get("installed_by") == "source", True),
        ("Hook scripts (plugin/hooks/)", (project_root / "plugin" / "hooks" / "send_event.py").exists(), True),
        ("Python package (aiteam)", _importable("aiteam"), True),
    ]

    all_ok = True
    for label, ok, required in checks:
        status = "[OK]" if ok else ("[FAIL]" if required else "[WARN]")
        print(f"  {status} {label}")
        if not ok and required:
            all_ok = False

    # Another config folder's sessions may use the server too: harmless, worth knowing.
    default_global = _default_global_config()
    if os.path.abspath(default_global) != os.path.abspath(global_config) and _mcp_registered(default_global):
        print(f"  [INFO] {default_global} also registers ai-team-os (another config folder; harmless)")

    return all_ok


def _asset_suffix(dst_dir: Path, pattern: str) -> str:
    """Return a ' (N present)' suffix for a verify label, or '' if dir absent."""
    if not dst_dir.exists():
        return ""
    return f" ({len(list(dst_dir.glob(pattern)))} present)"


def _verify_assets_present(src_dir: Path, dst_dir: Path, list_expected, is_present):
    """Confirm every asset the source ships also landed under dst_dir.

    Args:
        src_dir: Plugin source directory (source of truth for expected names).
        dst_dir: User-level install directory to check.
        list_expected: callable(src_dir) -> list[str] of expected asset names.
        is_present: callable(dst_dir, name) -> bool, True if the asset is installed.

    Returns:
        (ok, detail) where ok is True when all expected assets are present and
        detail is a short "<present>/<expected> present" string for the report.
    """
    if not src_dir.exists():
        return True, "no source"
    expected = list_expected(src_dir)
    if not expected:
        return True, "none to install"
    present = sum(1 for name in expected if is_present(dst_dir, name))
    return present == len(expected), f"{present}/{len(expected)} present"


# 依赖名 → import 名。通用规则是"剥掉 extras 与版本规格、把 - 换成 _"，
# 全表只有这一条对不上。
_IMPORT_NAME_OVERRIDES = {"pyyaml": "yaml"}


def declared_dependencies(project_root: Path) -> list[tuple[str, str]]:
    """从 pyproject.toml 的 [project].dependencies 读出 [(import 名, pip 规格)]。

    现读而不在代码里抄一份：依赖清单已经有三个安装入口要对齐，再立第四份就多一处
    会腐烂的地方。读不到（文件缺失/格式变了）返回空——校验缺席也好过安装器自己崩。
    """
    import re
    import tomllib

    try:
        data = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
        specs = data.get("project", {}).get("dependencies", [])
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return []

    pairs: list[tuple[str, str]] = []
    for spec in specs:
        if not isinstance(spec, str) or not spec.strip():
            continue
        # 取到第一个 extras / 版本运算符 / 环境标记之前的部分即为分发名
        name = re.split(r"[\[<>=!~;\s]", spec.strip(), maxsplit=1)[0].lower()
        if not name:
            continue
        module = _IMPORT_NAME_OVERRIDES.get(name, name.replace("-", "_"))
        pairs.append((module, spec.strip()))
    return pairs


def _importable(module: str) -> bool:
    """能否 import。判据只认这个，不认 pip 的返回码——装没装上以能不能用为准。

    在新起的解释器里真 import 一次，与之后拉起 MCP、API 的进程看到的一样：本进程启动后
    pip 新写的 .pth（可编辑安装、首次创建的用户 site）本进程看不见；find_spec 只证明找得到，
    证明不了 import 不炸。aiteam 自身与 pyproject 声明的依赖都走这一个判据。
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import importlib, sys; importlib.import_module(sys.argv[1])", module],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_capture(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """跑命令并收走输出，失败不抛——补装是尽力而为，不能拖垮整个安装/更新。"""
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _pip_install_specs(specs: list[str], project_root: Path) -> bool:
    """装指定的几个包；被 PEP 668 拒掉时自动改用 --break-system-packages 重试。

    不重试就等于 Homebrew / 系统 Python 的用户永远装不上新增依赖，而按本仓的
    venv 禁令，系统 Python 正是默认的运行环境——四类进程共享它。这里落的位置也是
    它本来就该在的位置（用户显式跑了安装/更新脚本），不是背着用户改系统。
    非 PEP 668 的失败不重试：重试治不了网络不通，只会让用户多等一轮。
    """
    base = [sys.executable, "-m", "pip", "install"]
    code, out, err = _run_capture([*base, *specs], cwd=str(project_root))
    if code == 0:
        return True
    if "externally-managed-environment" in f"{out}\n{err}":
        print("       解释器带 PEP 668 外部管理标记，改用 --break-system-packages 重试")
        code, _, _ = _run_capture(
            [*base, "--break-system-packages", *specs], cwd=str(project_root)
        )
        return code == 0
    return False


def verify_runtime_dependencies(project_root: Path) -> list[str]:
    """装完之后证明每条声明的依赖真的能 import，缺的当场补装。返回仍然缺的规格。

    这一步存在的理由是一种**静默**的失败：PEP 668 的解释器会直接拒绝
    `pip install -e .`，而更新脚本对该失败是容忍的（不容忍则后面的 hook / skill /
    settings 刷新全做不成，那是更大的漂移）。于是新增依赖悄悄没装上，而更新脚本
    自己并不知道这一版加没加依赖——它只能印一句"依赖若有变动请手动重装"，
    等于把判断推给不可能知道答案的用户。

    表现不是报错，是"工具在列表里、一调就炸"。所以这里不问 pip 装成功没有，
    直接问能不能 import。
    """
    deps = declared_dependencies(project_root)
    if not deps:
        print("[WARN] 读不到 pyproject 依赖声明，跳过依赖校验")
        return []

    missing = [(mod, spec) for mod, spec in deps if not _importable(mod)]
    if not missing:
        print(f"[OK] 依赖齐备（{len(deps)} 项声明全部可导入）")
        return []

    specs = [spec for _, spec in missing]
    print(f"[...] {len(specs)} 项声明的依赖不可导入，正在补装: {', '.join(specs)}")
    if _pip_install_specs(specs, project_root):
        # 复检：装成功不等于能 import（装错解释器、装了同名不同包都可能）
        still = [spec for mod, spec in missing if not _importable(mod)]
        if not still:
            print("[OK] 缺失依赖已补齐")
            return []
    else:
        still = specs

    print("[FAIL] 以下依赖仍不可用，OS 的部分功能会在调用时报错:")
    for spec in still:
        print(f"       - {spec}")
    print("       请手动执行:")
    # 规格必须带引号：裸写 pkg>=1.0 的 '>' 会被 shell 当成重定向，照抄这条命令的人
    # 得到的是一个名为 "=1.0" 的空文件和一个仍然缺依赖的环境。双引号在 POSIX shell
    # 与 cmd.exe 下都成立。
    quoted = " ".join(f'"{spec}"' for spec in still)
    print(f"       {sys.executable} -m pip install --break-system-packages {quoted}")
    return still


def _import_package_module(project_root: Path, name: str):
    """Import a module of this checkout's aiteam package (the update has installed its dependencies by then)."""
    import importlib

    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    return importlib.import_module(name)


def migrate_data_dir(project_root: Path, *, apply: bool = True) -> dict | None:
    """Update step: move the data directory out of the legacy location (docs/config-dir-compat-design.md §4.1).

    Only an update the user starts does this, before the install surface. The
    seven gates of aiteam.data_dir_migration decide; a gate that does not pass
    leaves the data where it is (it keeps working there) and the update goes on.
    Returns the outcome to record in installs.json, whose reason the
    data_dir_legacy notice shows; None when the data is not in the legacy place.
    """
    resolution = _hook_core().resolve_data_dir()
    if resolution.mode != "legacy":
        print(f"[OK] Data directory: {resolution.path} ({resolution.mode}), nothing to migrate")
        return None
    try:
        migration = _import_package_module(project_root, "aiteam.data_dir_migration")
        result = migration.migrate(apply=apply)
    except Exception as exc:  # noqa: BLE001 - the update goes on with the data in place
        print(f"[WARN] Data directory migration did not run ({type(exc).__name__}: {exc});"
              f" the data stays at {resolution.path}")
        return {"action": "apply" if apply else "dry_run", "ok": False, "reason": "not_run",
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    print(migration.format_result(result))
    return result.outcome()


def record_install(project_root: Path, *, migration: dict | None = None) -> None:
    """Add this run to the data directory's install registry (installs.json; queries only)."""
    core = _hook_core()
    resolution = core.resolve_data_dir()
    record = {"host": "cc", "kind": "source", "config_dir": str(core.cc_config_dir()),
              "version": source_version(project_root), "baseline": str(project_root),
              "data_dir": str(resolution.path), "data_dir_mode": resolution.mode}
    if migration is not None:
        record["migration"] = migration
        if migration.get("ok") and migration.get("migrated_at"):
            record["data_migrated_at"] = migration["migrated_at"]
    try:
        print(f"[OK] Install recorded in {core.append_install_record(record)}")
    except OSError as exc:
        print(f"[WARN] Install not recorded: {exc}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="AI Team OS installer / updater",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python install.py            # fresh install\n"
            "  python install.py --update   # update existing installation\n"
            "  python install.py --config-dir ~/.other-claude  # install into another config folder\n"
            "  python install.py --force-mcp  # register global MCP even with the plugin enabled\n"
        ),
    )
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        help="Claude Code config folder to install into (default: CLAUDE_CONFIG_DIR, else the default folder)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Run in update mode (delegates to scripts/update.py)",
    )
    parser.add_argument(
        "--force-mcp",
        action="store_true",
        help=(
            "Register the global MCP server even when the CC plugin is enabled "
            "(the plugin already provides the same server, so this loads it twice)"
        ),
    )
    parser.add_argument(
        "--no-migrate-data",
        action="store_true",
        help="With --update: leave the OS data directory where it is (no migration this time)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="With --update: do not pull code or install packages; refresh from this checkout as it is",
    )
    args = parser.parse_args()
    if (args.no_migrate_data or args.offline) and not args.update:
        parser.error("--no-migrate-data and --offline go with --update")
    apply_config_dir(args.config_dir)

    if args.update:
        # Delegate to the dedicated update script
        update_script = Path(__file__).resolve().parent / "scripts" / "update.py"
        if not update_script.exists():
            print("[FAIL] scripts/update.py not found — cannot run update")
            sys.exit(1)
        import importlib.util
        spec = importlib.util.spec_from_file_location("update_module", update_script)
        if spec is None or spec.loader is None:
            print("[FAIL] Could not load scripts/update.py")
            sys.exit(1)
        update_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(update_mod)  # type: ignore[union-attr]
        update_mod.run_update(Path(__file__).resolve().parent, migrate_data=not args.no_migrate_data,
                              offline=args.offline)
        return

    print("=" * 50)
    print("  AI Team OS Installer")
    print("=" * 50)
    print()

    project_root = Path(__file__).resolve().parent
    print_install_target()

    # 1. Python version (the gate at the top of this file already refused anything older than 3.11)
    print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # 2. Check Node.js (optional, for Dashboard)
    has_node = check_command("node")
    has_npm = check_command("npm")
    if has_node and has_npm:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        print(f"[OK] Node.js {result.stdout.strip()}")
    else:
        print("[WARN] Node.js/npm not found — Dashboard build will be skipped")
        print("       Dashboard is optional; core functionality is unaffected")

    print()

    # 3. Install Python packages
    print("[...] Installing Python dependencies...")
    try:
        run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=str(project_root))
        print("[OK] Python dependencies installed")
    except SystemExit:
        print("[WARN] pip install -e . failed — trying direct dependency install...")
        try:
            # fastmcp 必须带上界：4.0 移除 3.x 兼容 shim，裸名会把新装机器直接
            # 拉到不兼容的大版本（同 pyproject.toml / plugin/requirements.txt）。
            run([sys.executable, "-m", "pip", "install",
                 "fastapi", "uvicorn", "sqlalchemy", "aiosqlite",
                 "pydantic", "pydantic-settings", "pyyaml", "anyio",
                 "httpx>=0.28.1", "websockets>=15.0",
                 "psutil>=5.9.8",
                 "fastmcp>=3.4.5,<4"],
                cwd=str(project_root))
            print("[OK] Core dependencies installed (fallback)")
        except SystemExit:
            print("[WARN] Some dependencies may be missing — continuing with setup")
    # 上面两条路径都可能被 PEP 668 拒掉而只留一句 WARN。这里以"能不能 import"
    # 复核一遍并补装缺的，别让安装看起来成功、用起来才炸。
    verify_runtime_dependencies(project_root)
    print()

    # 4. Build Dashboard (optional)
    dashboard_dir = project_root / "dashboard"
    if dashboard_dir.exists() and has_node and has_npm:
        print("[...] Installing Dashboard dependencies...")
        run(["npm", "install"], cwd=str(dashboard_dir))
        print("[OK] Dashboard dependencies installed")

        print("[...] Building Dashboard...")
        run(["npm", "run", "build"], cwd=str(dashboard_dir))
        print("[OK] Dashboard built")
        print()
    elif dashboard_dir.exists():
        print("[SKIP] Dashboard build skipped (Node.js required)")
        print()

    # 5. Resolve (and only on an empty machine create) the data directory, and
    # record the install path for the update checker
    data_dir = prepare_data_dir()
    if data_dir is None:
        sys.exit(1)

    # Save project root path so session_bootstrap.py can locate the repo for update checks
    install_path_file = data_dir / "install_path.txt"
    install_path_file.write_text(str(project_root), encoding="utf-8")
    print(f"[OK] Install path recorded: {project_root}")

    # 6. Register the MCP server in the config folder's global config file, so the
    # ai-team-os tools are available in ALL projects, not just this directory.
    # Falls back to writing a project-level .mcp.json if global registration fails.
    config = _config_dir()
    print("[...] Registering MCP server globally...")
    register_global_mcp(project_root, force=args.force_mcp)

    # 7. Register hooks into the config folder's settings.json (and record the chain owner)
    print(f"[...] Registering hooks into {config / 'settings.json'}...")
    register_hooks(project_root)

    # 8. Agent templates, skills, commands and the /loop maintenance prompt
    print(f"[...] Copying agent templates, skills, commands and loop.md to {config}...")
    copy_agent_templates(project_root)
    copy_skills(project_root)
    copy_commands(project_root)
    install_loop_md(project_root)

    # 9. Record the install, then verify it
    record_install(project_root)
    all_ok = verify_installation(project_root)

    # 10. Done
    print()
    print("=" * 50)
    print("  Installation complete!")
    print("=" * 50)
    print()
    print("Next steps:")
    print()
    print("  Step 1 - Restart Claude Code")
    print("    Hooks and MCP tools activate on next launch.")
    print("    The MCP server starts the local API automatically when it connects.")
    print("    Verify: run /mcp in Claude Code and check ai-team-os tools are mounted.")
    print()
    print("  Step 2 - Verify the API is running:")
    print("    curl http://localhost:8000/api/health")
    print("    Expected: {\"status\": \"ok\"}")
    print()
    if dashboard_dir.exists() and has_node:
        print("  Optional - Start the Dashboard:")
        print("    cd dashboard && npm run dev")
        print("    Visit: http://localhost:5174")
        print()
    print("  Step 3 - Create your first team in Claude Code:")
    print('    Tell it: "Create a web dev team with frontend, backend, and QA"')
    print()
    print("  Step 4 - First words to your session (do not skip):")
    print('    Tell it: "This project runs on AI Team OS - learn its tools and rules')
    print('    (start with /os-help), and use its task wall, memos and memory for')
    print('    everything you do." One sentence makes the OS the working protocol.')
    print()
    if not all_ok:
        print("[WARN] Some checks failed. Review the output above before proceeding.")
    print("For more information: plugin/README.md")


if __name__ == "__main__":
    main()
