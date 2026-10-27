# AI Team OS Installation Guide (for Claude Code)

This file is intended to be read by Claude Code (CC) to assist with installation.
If you are a human reading this, see the main [README.md](README.md) instead.

> **Codex readers:** use [README Option C](README.md#option-c-codex-adapter-lifecycle). The steps below configure Claude Code and must not be applied to Codex.

There are **two supported install tracks**:

- **Track A — Plugin marketplace** (recommended for most users): zero-clone, self-configuring.
- **Track B — From source** (recommended for developers / contributors): editable, always tracks the latest source.

> The old "pip install ai-team-os" (PyPI) path has been **removed**: the wheel ships without the `plugin/` and config resources, so the feature set is incomplete, and its post-install command never worked. Use Track A or Track B instead.

---

## Pre-flight Checks

Before installing, verify the Python environment:

1. Run: `python3 -c "import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)"`
2. If the output shows a `.venv`, `virtualenv`, or `env` path in `sys.prefix`, WARN the user:

   > WARNING: You are currently in a virtual environment (`{sys.prefix}`).
   > AI Team OS should be installed to your SYSTEM Python, not a project venv.
   > The hook scripts use system Python — if installed in a venv, hooks will fail
   > in all other projects that don't activate this venv.
   > Please run `deactivate` first, or open a new terminal without venv activation,
   > then re-run the installation.

3. Confirm `sys.prefix == sys.base_prefix` — this means you are in system Python and safe to proceed.

---

## Required: Configure Settings

AI Team OS requires specific settings in Claude Code's `settings.json`, in its config folder (`CLAUDE_CONFIG_DIR` when set, else `~/.claude`). **Run this script to auto-configure all required settings**:

```bash
python3 -c "
import json, os

folder = os.environ.get('CLAUDE_CONFIG_DIR', '').strip() or os.path.join(os.path.expanduser('~'), '.claude')
p = os.path.join(os.path.expanduser(folder), 'settings.json')
os.makedirs(os.path.dirname(p), exist_ok=True)

settings = {}
if os.path.exists(p):
    with open(p) as f:
        settings = json.load(f)

changed = False

# 1. Enable Agent Teams (required for team collaboration)
env = settings.setdefault('env', {})
if env.get('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS') != '1':
    env['CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS'] = '1'
    changed = True
    print('[OK] Agent Teams enabled')

# 2. effortLevel — leave for user to choose (do not auto-set)

if changed:
    with open(p, 'w') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    print('[DONE] Settings updated — tell user to restart Claude Code')
else:
    print('[OK] All settings already configured')
"
```

**After running this, tell the user to restart Claude Code** for settings to take effect.

---

## Installation Steps

### Track A: Plugin marketplace (recommended for most users)

```bash
# Add the marketplace source and install the plugin
claude plugin marketplace add CronusL-1141/AI-company
claude plugin install ai-team-os

# Restart Claude Code once.
```

On the **first launch after install**, the `auto_install` hook runs automatically and:
- installs the Python dependencies from source (GitHub) into your system Python;
- self-heals the plugin manifest interpreter to `sys.executable` (an absolute path), so MCP + all hooks keep working even on stock macOS (no `python` shim) or when a project `.venv` is active;
- prints "Please restart Claude Code to activate all features."

So the sequence is: install → restart → (first launch auto-installs deps, ~30s, once) → restart once more. After that everything is ready.

### Track B: From source (recommended for developers / contributors)

```bash
# Clone the repository
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# Run the installer (configures MCP + Hooks + Agent templates against your working tree)
python3 install.py

# Restart Claude Code
```

This installs the package **editable against your working tree**, so it always tracks the latest source you pull. `greenlet` is now a core dependency, so Apple Silicon (arm64) macOS installs directly with no extra steps. The LangGraph legacy CLI path is now an optional extra — install it only if you need `aiteam task run`:

```bash
pip install 'ai-team-os[langgraph]'
```

### Homebrew / PEP 668 (externally-managed-environment)

On Homebrew Python or any PEP 668 environment, `pip` may refuse to install into system Python with an `externally-managed-environment` error. AI Team OS is **designed** to live in system Python (the global hook scripts depend on it — see the venv warning below), so set:

```bash
export PIP_BREAK_SYSTEM_PACKAGES=1
```

before running the install (Track B) or the first-launch auto-install (Track A). Do **not** work around this by installing into a venv.

---

## Verification

After restarting Claude Code:

1. Run `/mcp` in Claude Code - `ai-team-os` should appear as connected. The full toolset contains **118 MCP tools**. <!-- install:mcp-tool-count -->
   This total applies when `AITEAM_TOOLSETS` is unset or `all` and `AITEAM_READONLY` is disabled. The `default` profile, selected groups, or read-only mode may expose fewer tools; verify the count against your configuration.
2. Run the `os_health_check` MCP tool — expected response: `{"status": "ok"}`
3. Check the API: `curl http://localhost:8000/api/health` — expected: `{"status": "ok"}`

If tools are not showing up, check the **Global MCP registration** in Claude Code's global config `.claude.json` (CC reads global MCP servers from here, **not** `settings.json`):
- Its location: inside `CLAUDE_CONFIG_DIR` when that is set, else `~/.claude.json` (`%USERPROFILE%\.claude.json` on Windows); `aiteam paths` prints it as `claude.global_config`
- Look for `ai-team-os` under `mcpServers` in that file

---

## Known Limitations

- **Do NOT install inside a project `.venv`** — the global hook scripts rely on system Python. Installing in a venv means AI Team OS only works when that specific venv is active.
- If you accidentally installed in a venv: `pip uninstall ai-team-os`, then `deactivate`, then reinstall into system Python.
- Requires Python >= 3.11.
- Claude Code with MCP support required (CC version >= 1.0).

---

## Updating

```bash
# Track A (plugin): refresh the marketplace first; plugin update alone reports the old version as latest
claude plugin marketplace update ai-team-os
claude plugin update ai-team-os@ai-team-os
# Restart Claude Code; the first start upgrades dependencies and may ask for one more restart

# Track B (from source):
git pull
python3 install.py --update
```

## Uninstalling

```bash
# Track A (plugin):
claude plugin uninstall ai-team-os

# The plugin leaves its global hook chain in place. Preview those leftovers:
python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/uninstall_main_chain.py"
# Review the entries/files, then use the preview's token and your confirmation:
python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/uninstall_main_chain.py" --apply "<token>" --user-quote "<your confirmation>"
# A disabled plugin can be re-enabled instead. Source-owned chains are refused.

# Track B (from source): preview, then uninstall
python3 scripts/uninstall.py --dry-run
python3 scripts/uninstall.py
# Stops the AI Team OS API on its configured port (a process it cannot verify as
# the OS API is left running) and removes hooks, registrations, MCP, agents,
# skills, commands, loop.md and the ai-team-os pip package.

# Both tracks keep the OS data directory (aiteam.db; `aiteam paths` shows where), and the
# unread hooks' invocation audit in ~/.local/state/ai-team-os (or
# $XDG_STATE_HOME/ai-team-os; each host's log rotates at 5 MB).
# To delete both too (cannot be undone; back them up first and make sure no other
# host, such as Codex, still uses them):
python3 scripts/uninstall.py --purge-data   # Track B: shows the path, size and version, then asks; on Track A remove both by hand

# Clean up leftover plugin files:
# Windows: rmdir /s %USERPROFILE%\.claude\plugins\data\ai-team-os-ai-team-os
# macOS/Linux: rm -rf "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/data/ai-team-os-*
```
