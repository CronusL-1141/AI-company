# AI Team OS Installation Guide (for Claude Code)

This file is intended to be read by Claude Code (CC) to assist with installation.
If you are a human reading this, see the main [README.md](README.md) instead.

---

## Pre-flight Checks

Before installing, verify the Python environment:

1. Run: `python -c "import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)"`
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
python -c "
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

### Option A: Plugin Install (Recommended)

```bash
# Install uv (required for MCP server launch)
pip install uv

# Add marketplace source and install plugin
claude plugin marketplace add CronusL-1141/AI-company
claude plugin install ai-team-os

# Restart Claude Code
# First launch takes ~30s to configure dependencies (only happens once)
```

### Option B: Manual Install from Source

```bash
# Clone the repository
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# Run the installer (configures MCP + Hooks + Agent templates)
python install.py

# Restart Claude Code
```

### Option C: pip install (PyPI)

```bash
# Install from PyPI
pip install ai-team-os

# Run the post-install setup script (required — sets up MCP + hooks config)
python -m aiteam.cli.app init

# Restart Claude Code
```

---

## Verification

After restarting Claude Code:

1. Run `/mcp` in Claude Code — `ai-team-os` should appear as connected with ~107 tools
2. Run the `os_health_check` MCP tool — expected response: `{"status": "ok"}`
3. Check the API: `curl http://localhost:8000/api/health` — expected: `{"status": "ok"}`

If tools are not showing up, check:
- On Windows: `%USERPROFILE%\.claude\settings.json` — look for `ai-team-os` in `mcpServers`
- On macOS/Linux: `~/.claude/settings.json`

---

## Known Limitations

- **Do NOT install inside a project `.venv`** — the global hook scripts rely on system Python. Installing in a venv means AI Team OS only works when that specific venv is active.
- If you accidentally installed in a venv: `pip uninstall ai-team-os`, then `deactivate`, then reinstall.
- Requires Python >= 3.11.
- Claude Code with MCP support required (CC version >= 1.0).

---

## Updating

```bash
# Plugin install:
claude plugin update ai-team-os@ai-team-os

# Manual/pip install:
pip install --upgrade ai-team-os
```

## Uninstalling

```bash
# Plugin install:
claude plugin uninstall ai-team-os

# Manual install:
python scripts/uninstall.py

# Clean up residual data:
# Windows: rmdir /s %USERPROFILE%\.claude\plugins\data\ai-team-os-ai-team-os
# macOS/Linux: rm -rf ~/.claude/plugins/data/ai-team-os-*
```
