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

## Required: Enable Agent Teams

AI Team OS requires Agent Teams feature. **You must write this to the user's settings.json**:

```bash
# Check if already set
python -c "
import json, os
p = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
if os.path.exists(p):
    d = json.load(open(p))
    if d.get('env', {}).get('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS') == '1':
        print('Already enabled')
    else:
        d.setdefault('env', {})['CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS'] = '1'
        json.dump(d, open(p, 'w'), indent=2, ensure_ascii=False)
        print('Enabled — restart CC to activate')
else:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({'env': {'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS': '1'}}, open(p, 'w'), indent=2)
    print('Created and enabled — restart CC to activate')
"
```

**After running this, tell the user to restart Claude Code** for Agent Teams to take effect.

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
