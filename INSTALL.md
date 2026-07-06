# AI Team OS Installation Guide (for Claude Code)

This file is intended to be read by Claude Code (CC) to assist with installation.
If you are a human reading this, see the main [README.md](README.md) instead.

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

AI Team OS requires specific settings in `~/.claude/settings.json`. **Run this script to auto-configure all required settings**:

```bash
python3 -c "
import json, os

p = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
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

1. Run `/mcp` in Claude Code — `ai-team-os` should appear as connected with ~155 tools
2. Run the `os_health_check` MCP tool — expected response: `{"status": "ok"}`
3. Check the API: `curl http://localhost:8000/api/health` — expected: `{"status": "ok"}`

If tools are not showing up, check the **Global MCP registration** in `~/.claude.json` (CC reads global MCP servers from here, **not** `settings.json`):
- Look for `ai-team-os` under `mcpServers` in `~/.claude.json`
- On Windows the file is `%USERPROFILE%\.claude.json`

---

## Known Limitations

- **Do NOT install inside a project `.venv`** — the global hook scripts rely on system Python. Installing in a venv means AI Team OS only works when that specific venv is active.
- If you accidentally installed in a venv: `pip uninstall ai-team-os`, then `deactivate`, then reinstall into system Python.
- Requires Python >= 3.11.
- Claude Code with MCP support required (CC version >= 1.0).

---

## Updating

```bash
# Track A (plugin):
claude plugin update ai-team-os@ai-team-os

# Track B (from source):
git pull
python3 install.py --update
```

## Uninstalling

```bash
# Track A (plugin):
claude plugin uninstall ai-team-os

# Track B (from source):
python3 scripts/uninstall.py

# Clean up residual data:
# Windows: rmdir /s %USERPROFILE%\.claude\plugins\data\ai-team-os-ai-team-os
# macOS/Linux: rm -rf ~/.claude/plugins/data/ai-team-os-*
```
